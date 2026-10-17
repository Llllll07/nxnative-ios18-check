"""Asset-free Apple SDK checks. Success does not mean a game or an IPA was built."""
import argparse,hashlib,importlib.util,json,os,platform,shutil,subprocess,sys,time
from pathlib import Path

def execute(command,log,env=None):
    start=time.monotonic()
    with log.open('w',encoding='utf-8') as stream:
        result=subprocess.run([str(c) for c in command],env=env,stdout=stream,stderr=subprocess.STDOUT,text=True)
    print(log.read_text(encoding='utf-8',errors='replace'),end='',flush=True)
    return dict(command=[str(c) for c in command],exit_code=result.returncode,seconds=round(time.monotonic()-start,2),log=log.name)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--source',type=Path,required=True);ap.add_argument('--results',type=Path,required=True)
    args=ap.parse_args();source=args.source.resolve();results=args.results.resolve();results.mkdir(parents=True,exist_ok=True)
    report=dict(scope='Open-source iOS 18 SDK/type checks and a synthetic Metal library only',system=platform.platform(),ipa_created=False,game_files_used=False,iphone_tested=False,steps=[])
    report_path=results/'apple-sdk-check.json'
    if sys.platform!='darwin':
        report.update(status='blocked',reason='This stage must run on a macOS runner with Xcode')
        report_path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8');print(report['reason']);return 2
    for name in ('xcodebuild','xcrun','bash','cpp','cc'):
        if not shutil.which(name):
            report.update(status='blocked',reason='Missing compiler/tool: '+name)
            report_path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8');return 2
    report['steps'].append(execute(['xcodebuild','-version'],results/'xcode-version.log'))
    report['steps'].append(execute(['xcrun','--sdk','iphoneos','--show-sdk-version'],results/'iphoneos-sdk.log'))
    report['steps'].append(execute([sys.executable,source/'dm/ios/tests/test_ios18_port.py'],results/'portable-ios18-tests.log'))
    profiles=sorted(p.name for p in (source/'dm/games').iterdir() if p.is_dir() and (p/'game.tsv').is_file())
    report['reference_profiles']=profiles
    for name in profiles:
        step=execute(['bash',source/'dm/ios/tools/check_m3_apple.sh'],results/f'api-check-{name}.log',dict(os.environ,GAME=name))
        step['reference_profile']=name;report['steps'].append(step)
    metal=results/'smoke.metal';air=results/'smoke.air';lib=results/'smoke.metallib'
    metal.write_text('#include <metal_stdlib>\nusing namespace metal;\nstruct Out { float4 p [[position]]; };\nvertex Out v_main(uint i [[vertex_id]]) { Out o; float2 p[3]={float2(-1,-1),float2(3,-1),float2(-1,3)}; o.p=float4(p[i],0,1); return o; }\nfragment float4 f_main() { return float4(0,0,0,1); }\n',encoding='utf-8')
    spec=importlib.util.spec_from_file_location('ios_metal',source/'dm/ios/tools/ios_metal.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    report['steps'].append(execute([*module.METAL,metal,'-o',air],results/'metal-compile.log'))
    if air.is_file():report['steps'].append(execute([*module.METALLIB,air,'-o',lib],results/'metallib-link.log'))
    valid=lib.is_file() and lib.stat().st_size>16 and lib.read_bytes()[:4]==b'MTLB'
    report['metal_smoke_library_valid']=valid
    if valid:report['metal_smoke_sha256']=hashlib.sha256(lib.read_bytes()).hexdigest()
    success=valid and bool(profiles) and all(s['exit_code']==0 for s in report['steps'])
    report['status']='passed' if success else 'failed'
    report_path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print('Source-only check result:',report['status']);return 0 if success else 1

if __name__=='__main__':sys.exit(main())

