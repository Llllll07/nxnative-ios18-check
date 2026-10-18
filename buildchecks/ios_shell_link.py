"""Link an unsigned, asset-free iOS 18 diagnostic host.

Only public nxnative sources are used. The draft title constants are boot-test
inputs, not a validated profile. No game, key, shader, or signing material is
read or downloaded. The output shell cannot run the game by itself.
"""
import argparse,importlib.util,json,os,platform,shutil,subprocess,sys,time,zipfile
from pathlib import Path

TITLE='01002B9012E28000'
PROFILE='''# Draft boot-test inputs only. No runtime compatibility is asserted.
# Memory and device API values are inherited reference defaults, not measured.
title_id\t01002B9012E28000
program_id\t01002B9012E28000
name\tDW9E Boot Check
ipa\tDW9E_BootCheck_UNSIGNED.ipa
app_category\tpublic.app-category.action-games
build\tv65536\t1.0.1\tmain=6c3f685c62070885215710bb810d2628
package\tv65536
shader_versions\tv65536
modules\tmain subsdk0 subsdk1 sdk
romfs_marker\tLINKIDX_000.BIN
zip_store\t.BIN:.metallib
mem_reserve\t0x140000000
mem_alias\t0xB8000000
mem_stack\t0x10000000
app_pool\t0xCD500000
device_api\t55.15
pad_layout\tfull
nvn_tex_base\t0x20
nvn_tex_slots\t32
nvn_samp_base\t0x568
nvn_img_base\t0x120
nvn_img_slots\t8
nvn_ssbo_bases\tvs=0x110 ps=0x510 cs=0x310 gs=0x410
shader_note\tBOOT DIAGNOSTIC ONLY; no shader cache was built for this profile
'''

def prepare(source):
    gd=source/'dm/games'/TITLE
    if gd.exists():raise ValueError('Refusing to overwrite an existing title profile')
    gd.mkdir();(gd/'game.tsv').write_text(PROFILE,encoding='utf-8')
    # This cache name deliberately accepts no existing candidate or production
    # shader libraries. A later validated shader-chain build is a separate task.
    (gd/'dmsh_chain.h').write_text('#define DMSH_CHAIN "dw9e-boot-unbuilt"\n',encoding='utf-8')
    (gd/'PROFILE_STATUS.txt').write_text('Draft boot inputs only. Memory/device API defaults are unmeasured. No shaders or game content.\n',encoding='utf-8')
    spec=importlib.util.spec_from_file_location('project_m3',source/'dm/ios/tools/project_m3.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    text=mod.transform((source/'dm/ios/project.yml').read_text(encoding='utf-8'))
    if text.count('-DDM_NVN_M3=1')!=1:raise ValueError('Unexpected legacy project shape')
    text=text.replace('-DDM_NVN_M3=1','-DDM_NVN_M3=1 -DDM_SHELL=1')
    path=source/'dm/ios/project_shell_check.yml'
    if path.exists():raise ValueError('Project spec already exists')
    path.write_text(text,encoding='utf-8')
    return gd,path

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source',type=Path,required=True);ap.add_argument('--results',type=Path,required=True)
    ap.add_argument('--prepare-only',action='store_true');args=ap.parse_args()
    source=args.source.resolve();results=args.results.resolve();results.mkdir(parents=True,exist_ok=True)
    report={'scope':'Unsigned asset-free iOS host link check only; draft boot inputs',
        'game_files_used':False,'keys_used':False,'game_shader_libraries_used':False,
        'ipa_created':False,'game_profile_validated':False,'iphone_tested':False,
        'system':platform.platform(),'steps':[],'status':'running'}
    start=time.monotonic()
    def run(argv,name,cwd=None,env=None):
        argv=list(map(str,argv));log=results/(name+'.log')
        with log.open('w',encoding='utf-8') as f:
            r=subprocess.run(argv,cwd=cwd,env=env,stdout=f,stderr=subprocess.STDOUT,text=True)
        report['steps'].append({'command':argv,'exit_code':r.returncode,'log':log.name})
        if r.returncode:
            lines=log.read_text(encoding='utf-8',errors='replace').splitlines()
            errors=[l for l in lines if 'error:' in l or 'Undefined symbols' in l]
            print('\n'.join((errors or lines[-60:])[:60]),flush=True)
            raise RuntimeError(name+' failed')
    try:
        gd,spec=prepare(source)
        report['draft_profile_written']=True
        if args.prepare_only:
            report['status']='prepared_only';return 0
        if sys.platform!='darwin':raise RuntimeError('Full host linking requires the cloud macOS/Xcode runner')
        for tool in ['xcodebuild','xcrun','xcodegen','make']:
            if not shutil.which(tool):raise RuntimeError('Missing tool: '+tool)
        run(['xcodebuild','-version'],'xcode-version')
        ios=source/'dm/ios';gen=ios/'Gen';gen.mkdir()
        for name in ['dm','msl','Assets.xcassets']:(gen/name).mkdir()
        (gen/'Assets.xcassets/Contents.json').write_text('{"info":{"author":"xcode","version":1}}\n',encoding='utf-8')
        (gen/'embed.S').write_text('    .text\n',encoding='utf-8')
        env=dict(os.environ,GAME=TITLE,DM_BUNDLE_ID='org.nxnative.dw9e.bootcheck',
            DM_DISPLAY_NAME='DW9E Boot Check',DM_APP_CATEGORY='public.app-category.action-games',DM_TEAM_ID='0000000000')
        with (gen/'dm.xcconfig').open('w',encoding='utf-8') as f:
            r=subprocess.run([sys.executable,str(ios/'tools/gen_build.py'),str(source/'dm/host'),str(gen)],env=env,stdout=f,stderr=subprocess.PIPE,text=True)
        (results/'gen-build.log').write_text(r.stderr,encoding='utf-8')
        report['steps'].append({'command':['gen_build.py'],'exit_code':r.returncode,'log':'gen-build.log'})
        if r.returncode:raise RuntimeError('gen_build failed: '+r.stderr[-4000:])
        with (gen/'dm.xcconfig').open('a',encoding='utf-8') as f:
            f.write('\nOTHER_LDFLAGS = $(inherited) $(DM_FRAMEWORKS)\n')
        run(['xcodegen','generate','--spec',spec.name], 'xcodegen',cwd=ios,env=env)
        dd=results/'derived-data'
        run(['xcodebuild','-project','DmIOS.xcodeproj','-scheme','DmIOS','-configuration','Release',
            '-destination','generic/platform=iOS','-derivedDataPath',dd,
            'CODE_SIGNING_ALLOWED=NO','CODE_SIGNING_REQUIRED=NO','CODE_SIGN_IDENTITY=','build'],
            'full-host-build',cwd=ios,env=env)
        app=dd/'Build/Products/Release-iphoneos/DmIOS.app'
        if not (app/'DmIOS').is_file():raise RuntimeError('No linked app executable')
        run(['xcrun','otool','-hv',app/'DmIOS'],'macho-header')
        run(['xcrun','otool','-l',app/'DmIOS'],'macho-load-commands')
        archive=results/'ios18-asset-free-shell.zip'
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
            for p in sorted(app.rglob('*')):
                if p.is_file():z.write(p,'Payload/DmIOS.app/'+p.relative_to(app).as_posix())
        report.update(status='passed',asset_free_shell_linked=True,shell_archive=archive.name)
        # DerivedData is not uploaded. Keeping it in RUNNER_TEMP avoids enormous
        # diagnostic artifacts, even if the build fails.
        print('Asset-free full host link passed. This is not a game build or playable IPA.',flush=True)
        return 0
    except Exception as e:
        report.update(status='failed',error=str(e));print(str(e),flush=True);return 1
    finally:
        report['elapsed_seconds']=round(time.monotonic()-start,2)
        (results/'full-host-check.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')

if __name__=='__main__':sys.exit(main())
