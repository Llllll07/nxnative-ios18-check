"""Build a host with zero-filled module sections; game bytes remain on Windows."""
import argparse
import hashlib
import gzip
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile

COMMIT = 'd50ecc1f8469b3697f62177a1fa0b9838cb50769'
ARCHIVE_HASH = 'e83ef87f495148dae6fb27e3f875f51acc78c4db02a94f6533c6193b62472142'
GAME = '010076F0049A2000'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    args = parser.parse_args()
    inputs, results = args.inputs.resolve(), args.results.resolve()
    results.mkdir(parents=True, exist_ok=False)
    report = {'game_content_uploaded': False, 'keys_uploaded': False,
              'module_sections': 'zero-filled placeholders only', 'status': 'running'}

    def run(command, name, cwd=None, env=None):
        with (results / (name + '.log')).open('w', encoding='utf-8') as stream:
            process = subprocess.run(list(map(str, command)), cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT)
        if process.returncode:
            lines = (results / (name + '.log')).read_text(encoding='utf-8', errors='replace').splitlines()
            errors = [line for line in lines if 'error:' in line or 'Undefined symbols' in line]
            print('\n'.join((errors or lines[-35:])[:35]), flush=True)
            raise RuntimeError(name + ' failed')

    try:
        archive = results / 'public-source.zip'
        urllib.request.urlretrieve(f'https://codeload.github.com/seedds/nxnative/zip/{COMMIT}', archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_HASH:
            raise RuntimeError('Pinned public source archive hash differs.')
        with zipfile.ZipFile(archive) as source_zip:
            source_zip.extractall(results / 'source')
        source = results / 'source' / ('nxnative-' + COMMIT)
        patch = results / 'source-overlay.patch'
        patch.write_bytes(gzip.decompress((inputs / 'source-overlay.patch.gz').read_bytes()))
        manifest = json.loads((inputs / 'source-manifest.json').read_text())
        if hashlib.sha256(patch.read_bytes()).hexdigest() != manifest['patch_sha256']:
            raise RuntimeError('Source patch hash differs.')
        run(['git', '-c', 'core.autocrlf=false', 'apply', '--check', patch], 'patch-check', cwd=source)
        run(['git', '-c', 'core.autocrlf=false', 'apply', patch], 'patch-apply', cwd=source)
        run(['xcodebuild', '-version'], 'xcode-version')
        run(['xcrun', '--sdk', 'iphoneos', '--show-sdk-version'], 'iphoneos-sdk')

        fixture = source / 'dm/gfx/tests/t_metal3_present_resources.m'
        test_executable = results / 'present-resources-test'
        run(['xcrun', 'clang', '-std=gnu11', '-O1', '-fobjc-arc', '-Wno-protocol',
             fixture, '-framework', 'Foundation', '-framework', 'Metal', '-framework', 'QuartzCore',
             '-o', test_executable], 'present-test-compile')
        run([test_executable], 'present-test-run')

        ios = source / 'dm/ios'
        gen = ios / 'Gen'
        gen.mkdir(exist_ok=False)
        (gen / 'dm').mkdir()
        (gen / 'Assets.xcassets').mkdir()
        (gen / 'Assets.xcassets/Contents.json').write_text('{"info":{"author":"xcode","version":1}}\n')
        layout = json.loads((inputs / 'embed-layout.json').read_text())
        asm = ['// All pieces are zeros. Local Windows packing supplies owned game data.']
        link_flags = []
        for module in layout['modules']:
            for piece in module['pieces']:
                asm += [f'    .section {piece["segment"]},{piece["section"]}',
                        '    .p2align 14', f'    .space {piece["size"]}', '    .p2align 14']
            link_flags += [f'-Wl,-segprot,__DM{module["name"]}W,rw,rw']
        (gen / 'embed.S').write_text('\n'.join(asm) + '\n', encoding='utf-8')
        env = dict(os.environ, GAME=GAME, DM_GAME=GAME, DM_BUNDLE_ID='com.yolo.bayonetta-metal3',
                   DM_DISPLAY_NAME='Bayonetta Metal3', DM_APP_VERSION='1.1', DM_TEAM_ID='0000000000',
                   DM_APP_CATEGORY='public.app-category.action-games', DM_SHOW_FPS='YES',
                   DM_LANG='zh-Hans', DM_SCHEME_NAME='DmIOS', DM_METAL3_SHADER_CHAIN='84bc63254142')
        with (gen / 'dm.xcconfig').open('w', encoding='utf-8') as stream:
            process = subprocess.run([sys.executable, str(ios / 'tools/gen_build.py'), str(source / 'dm/host'), str(gen)],
                                     env=env, stdout=stream, stderr=subprocess.PIPE, text=True)
        (results / 'gen-build.log').write_text(process.stderr, encoding='utf-8')
        if process.returncode:
            raise RuntimeError('gen_build failed: ' + process.stderr[-3000:])
        with (gen / 'dm.xcconfig').open('a', encoding='utf-8') as stream:
            stream.write('\nOTHER_LDFLAGS = $(inherited) $(DM_FRAMEWORKS) ' + ' '.join(link_flags) + '\n')
        run(['xcodegen', 'generate', '--spec', 'project.yml'], 'xcodegen', cwd=ios, env=env)
        derived = results / 'derived'
        run(['xcodebuild', '-project', 'DmIOS.xcodeproj', '-scheme', 'DmIOS', '-configuration', 'Release',
             '-destination', 'generic/platform=iOS', '-derivedDataPath', derived,
             'CODE_SIGNING_ALLOWED=NO', 'CODE_SIGNING_REQUIRED=NO', 'CODE_SIGN_IDENTITY=',
             'DM_COPY_GAME_RESOURCES=NO', f'DM_XCODE_OUTPUT_ROOT={derived}',
             f'SYMROOT={derived}/Build/Products', f'OBJROOT={derived}/Build/Intermediates.noindex', 'build'],
            'iphone-host-build', cwd=ios, env=env)
        app = derived / 'Build/Products/Release-iphoneos/DmIOS.app'
        with (app / 'Info.plist').open('rb') as stream:
            info = plistlib.load(stream)
        if info.get('MinimumOSVersion') != '18.0':
            raise RuntimeError('Unexpected deployment target: ' + str(info.get('MinimumOSVersion')))
        run(['xcrun', 'otool', '-l', app / 'DmIOS'], 'macho-load-commands')
        output = results / 'Bayonetta-Metal3-host-placeholders.zip'
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
            for path in sorted(app.rglob('*')):
                if path.is_file() and '_CodeSignature' not in path.parts:
                    bundle.write(path, 'Payload/DmIOS.app/' + path.relative_to(app).as_posix())
        report.update(status='passed', minimum_os=info['MinimumOSVersion'],
                      host_archive=output.name, actual_encoder_test_passed=True,
                      device_tested=False, game_code_in_artifact=False)
        print(json.dumps(report), flush=True)
        return 0
    except Exception as error:
        report.update(status='failed', error=str(error))
        print(str(error), flush=True)
        return 1
    finally:
        (results / 'build-result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    sys.exit(main())
