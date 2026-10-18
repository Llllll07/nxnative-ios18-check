"""Link an unsigned, asset-free iOS 18 diagnostic host.

Only public nxnative sources are used. The draft title constants are boot-test
inputs, not a validated profile. No game, key, shader, or signing material is
read or downloaded. The output shell cannot run the game by itself.
"""
import argparse,importlib.util,json,os,platform,shutil,subprocess,sys,time,zipfile
from pathlib import Path

TITLE='01002B9012E28000'
RUNTIME_CHAIN='6e5dcfec679d'
PROFILE='''# Draft boot-test inputs only. No runtime compatibility is asserted.
# Seventh boot: enable the locally generated broader shader chain.
# The first device boot requested 0xC9400000; the old cap was 0x68000000.
# The second boot could not reserve 6 GiB; retain the first boot's 5 GiB span.
# Device API values remain inherited reference defaults, not measured.
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
mem_alias\t0x50000000
mem_stack\t0x10000000
app_pool\t0xCD500000
mem_account_images\t1
device_api\t55.15
pad_layout\tfull
nvn_tex_base\t0x20
nvn_tex_slots\t32
nvn_samp_base\t0x568
nvn_img_base\t0x120
nvn_img_slots\t8
nvn_ssbo_bases\tvs=0x110 ps=0x510 cs=0x310 gs=0x410
shader_note\tEXPERIMENTAL partial startup cache; assets stay local; not playable
'''

def patch_memory_accounting(source):
    """Opt this draft title into internally consistent image accounting."""
    profile=source/'dm/games/game.py'
    text=profile.read_text(encoding='utf-8')
    anchor='    L = ['
    insert='    account_images = int(g.get("mem_account_images", "0"), 0)\n    if account_images not in (0, 1): raise ValueError("mem_account_images must be 0 or 1")\n'
    if text.count(anchor)!=1:raise ValueError('Profile generator anchor changed')
    text=text.replace(anchor,insert+anchor)
    anchor="         f'#define DM_GAME_APP_POOL {g.int(\"app_pool\"):#x}ull',"
    if text.count(anchor)!=1:raise ValueError('Profile memory header anchor changed')
    text=text.replace(anchor,anchor+"\n         f'#define DM_GAME_MEM_ACCOUNT_IMAGES {account_images}',")
    profile.write_text(text,encoding='utf-8')
    svc=source/'dm/host/hle_svc.c'
    text=svc.read_text(encoding='utf-8')
    changes=[
        ('v = g_heap_cur + 0x4000000 + big_bss_bytes(); break;',
         'v = g_heap_cur + (DM_GAME_MEM_ACCOUNT_IMAGES ? image_bytes() : 0x4000000 + big_bss_bytes()); if (g_flag.svc) LOG("GetInfo used-memory id=%llu heap=%#zx image=%#zx value=%#llx", (unsigned long long)id, g_heap_cur, image_bytes(), (unsigned long long)v); break;'),
        ('if (size + image_bytes() > APP_POOL) { regs[0] = 0x10801; return 0; }',
         'if (size + image_bytes() > APP_POOL) { LOG("【红】SetHeapSize pool limit: req %#zx + image %#zx > pool %#llx", size, image_bytes(), (unsigned long long)APP_POOL); regs[0] = 0x10801; return 0; }'),
        ('        g_heap_cur = size;',
         '        if (rc) { LOG("【红】SetHeapSize mprotect failed: req %#zx errno=%d (%s)", size, errno, strerror(errno)); regs[0] = 0xD401; return 0; }\n        g_heap_cur = size;'),
    ]
    for old,new in changes:
        if text.count(old)!=1:raise ValueError('Memory accounting source anchor changed: '+old)
        text=text.replace(old,new)
    svc.write_text(text,encoding='utf-8')

def patch_save_queries(source):
    """Keep application-qualified save queries within the local save model."""
    fs=source/'dm/host/hle_fs.c'
    text=fs.read_text(encoding='utf-8')
    anchor='HLE_FORCE("_ZN2nn2fs18IsSaveDataExistingERKNS_7account3UidE", "fsp-srv；存档恒已建（返回 bool）", unsigned, h_fs_SaveExists, const void *uid) { (void)uid; return 1; }'
    addition=r'''
// The host models one local application's save. Other application IDs have no
// mounted save here; reporting the current save for them would trigger an
// incorrect cross-title import. The bool/output-bool ABI is confirmed in SDK
// 10.9 and the observed full-resource startup reaches the application overload.
static unsigned save_exists_application(uint64_t application, const void *uid) {
    unsigned found = (!application || application == DM_GAME_PROGRAM_ID) ? h_fs_SaveExists(uid) : 0;
    if (g_flag.fs) LOG("fs save-exists application=%#llx current=%#llx -> %u", (unsigned long long)application, (unsigned long long)DM_GAME_PROGRAM_ID, found);
    return found;
}
HLE_FORCE("_ZN2nn2fs18IsSaveDataExistingENS_13ApplicationIdERKNS_7account3UidE", "fsp-srv；按应用编号查询本地存档", unsigned, h_fs_SaveExistsApplication, uint64_t application, const void *uid) {
    return save_exists_application(application, uid);
}
HLE_FORCE("_ZN2nn2fs18IsSaveDataExistingEPbNS_13ApplicationIdERKNS_7account3UidE", "fsp-srv；本地存档存在状态输出", Result, h_fs_SaveExistsApplicationOut, uint8_t *out, uint64_t application, const void *uid) {
    if (!out) return 0x2EE202u;  // Same null-output error as this SDK's query.
    *out = (uint8_t)save_exists_application(application, uid);
    return RES_OK;
}
'''
    if text.count(anchor)!=1:raise ValueError('Save existence source anchor changed')
    fs.write_text(text.replace(anchor,anchor+addition),encoding='utf-8')

def patch_image_registration(source):
    """Use the existing descriptor lookup for storage-image registrations."""
    nvn=source/'dm/gfx/nvn/hle_nvn.c'
    text=nvn.read_text(encoding='utf-8')
    anchor='NVN(nvnTexturePoolRegisterTexture, void, const NVNtexturePool *tp, int id, const NVNtexture *t, const NVNtextureView *v) { (void)tp; mtl_reg_tex(id, (const tex_t *)t, (const view_t *)v); }'
    addition='\n// Image handles use the same pool index and Metal texture/view descriptor.\nNVN(nvnTexturePoolRegisterImage, void, const NVNtexturePool *tp, int id, const NVNtexture *t, const NVNtextureView *v) { (void)tp; mtl_reg_tex(id, (const tex_t *)t, (const view_t *)v); }'
    if text.count(anchor)!=1:raise ValueError('Texture pool registration source anchor changed')
    nvn.write_text(text.replace(anchor,anchor+addition),encoding='utf-8')

def patch_program_stage_binding(source):
    """Keep compute bindings independent from the graphics stage composition."""
    path=source/'dm/gfx/nvn/nvn_mtl.m'
    text=path.read_text(encoding='utf-8')
    changes=[
        ('    const prog_t *prog;', '    const prog_t *prog;\n    const prog_t *cprog;  // Compute binding is independent of graphics bindings.'),
        ('    if (stages & 0x20) return p;   // compute: replaced as a whole as before\n', ''),
        ('        case OP_BIND_PROGRAM: { const p_bind_program *bp_ = a; const prog_t *np = bind_stages(bp_->prog, bp_->stages); if (np != S.prog) { S.prog = np; g_pso_dirty = 1; args_dirty_all(); } break; }',
         '        case OP_BIND_PROGRAM: { const p_bind_program *bp_ = a; bind_program_stages(bp_->prog, bp_->stages); break; }'),
        ('    if (!S.prog) { RED_ONCE(27, "派发前没有绑定程序"); return; }\n    NvShader *cs = prog_get(S.prog)->sh[5];',
         '    if (!S.cprog) { RED_ONCE(27, "派发前没有绑定计算程序"); return; }\n    NvShader *cs = prog_get(S.cprog)->sh[5];'),
    ]
    for old,new in changes:
        if text.count(old)!=1:raise ValueError('Program binding source anchor changed: '+old[:80])
        text=text.replace(old,new)
    anchor='static void state_reset(void) {'
    addition=r'''// NVN stage masks update only the named stages. In particular, unbinding
// compute must not erase a vertex/fragment program before a subsequent draw.
static void bind_program_stages(const prog_t *p, int stages) {
    if (stages & 0x20) { S.cprog = p; args_dirty_all(); }
    if (stages & 0x1f) {
        const prog_t *np = bind_stages(p, stages & 0x1f);
        if (np != S.prog) { S.prog = np; g_pso_dirty = 1; args_dirty_all(); }
    }
    static int diag = -1; static unsigned count;
    if (diag < 0) diag = getenv("DM_NVN_BINDLOG") != NULL;
    if (diag && count++ < 96)
        MLOG("[bind] mask=%#x source=%p graphics=%p compute=%p vertex=%p fragment=%p", stages, p, S.prog, S.cprog, S.bst[0], S.bst[1]);
    PD("G mask=%#x source=%p graphics=%p compute=%p vertex=%p fragment=%p", stages, p, S.prog, S.cprog, S.bst[0], S.bst[1]);
}
'''
    if text.count(anchor)!=1:raise ValueError('Program state reset anchor changed')
    path.write_text(text.replace(anchor,addition+anchor),encoding='utf-8')

def prepare(source):
    gd=source/'dm/games'/TITLE
    if gd.exists():raise ValueError('Refusing to overwrite an existing title profile')
    gd.mkdir();(gd/'game.tsv').write_text(PROFILE,encoding='utf-8')
    # Generated by the upstream chain-header tool in the isolated local title
    # profile. Both Apple targets and c1 metadata were rebuilt locally. Only
    # this non-reversible identity is used here; no shader data is uploaded.
    if len(RUNTIME_CHAIN)!=12 or any(c not in '0123456789abcdef' for c in RUNTIME_CHAIN):raise ValueError('Invalid generated runtime chain')
    (gd/'dmsh_chain.h').write_text(f'#define DMSH_CHAIN "{RUNTIME_CHAIN}"\n',encoding='utf-8')
    (gd/'PROFILE_STATUS.txt').write_text('Sixth-boot diagnostic: retain v5 memory and save fixes, add storage-image registration and the identity of a locally built partial startup shader cache. This asset-free cloud build does not include or validate those shaders. Device graphics compatibility remains unvalidated. No game content.\n',encoding='utf-8')
    patch_memory_accounting(source)
    patch_save_queries(source)
    patch_image_registration(source)
    patch_program_stage_binding(source)
    svc=source/'dm/host/hle_svc.c'
    old='if (g_as == MAP_FAILED) die("reserve guest AS");'
    new='if (g_as == MAP_FAILED) die("reserve guest AS %#llx errno=%d (%s)", (unsigned long long)RESERVE, errno, strerror(errno));'
    code=svc.read_text(encoding='utf-8')
    if code.count(old)!=1:raise ValueError('Unexpected guest reservation diagnostic shape')
    svc.write_text(code.replace(old,new),encoding='utf-8')
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
        'system':platform.platform(),'steps':[],'status':'running','runtime_chain':RUNTIME_CHAIN}
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
