"""Link an unsigned, asset-free iOS 18 diagnostic host.

Only public nxnative sources are used. The draft title constants are boot-test
inputs, not a validated profile. No game, key, shader, or signing material is
read or downloaded. The output shell cannot run the game by itself.
"""
import argparse,importlib.util,json,os,platform,shutil,subprocess,sys,time,zipfile
from pathlib import Path

TITLE='01002B9012E28000'
RUNTIME_CHAIN='a75bee0c7b1d'
PROFILE='''# Draft boot-test inputs only. No runtime compatibility is asserted.
# Twelfth boot: bound upload scratch and retire it on upload completion.
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

def patch_depth_diagnostics(source):
    """Log the actual clear/range values for the bounded diagnostic frames."""
    path=source/'dm/gfx/nvn/nvn_mtl.m'
    text=path.read_text(encoding='utf-8')
    changes=[
      ('                const p_clear_ds *c = a;\n                if (g_rt_merged) rp_end();',
       '                const p_clear_ds *c = a;\n                PD("H depth-clear=%g depth-mask=%d stencil=%d stencil-mask=%#x", c->depth, c->depth_mask, c->stencil, c->stencil_mask);\n                if (g_rt_merged) rp_end();'),
      ('      enc_r_viewport(en, v); }',
       '      PD("H viewport-depth=%g..%g", v.znear, v.zfar);\n      enc_r_viewport(en, v); }'),
    ]
    for old,new in changes:
      if text.count(old)!=1:raise ValueError('Depth diagnostic source anchor changed')
      text=text.replace(old,new)
    path.write_text(text,encoding='utf-8')


def patch_submission_serialization(source):
    """One replay owns the global command buffer, upload batch and pool at a time.

    Only the experimental shell opts in; existing production profiles retain
    their baseline behavior. Never hold this lock while waiting for presents.
    GPU completion callbacks do not acquire it, so a replay may wait for GPU
    fences without blocking the callback that advances them.
    """
    path = source/'dm/gfx/nvn/nvn_mtl.m'
    text = path.read_text(encoding='utf-8')
    guard = r"""
// BEGIN SERIAL REPLAY GUARD: this block is compiled by the concurrency test.
#if defined(DM_SERIAL_REPLAY) && DM_SERIAL_REPLAY
static pthread_mutex_t g_replay_mu = PTHREAD_MUTEX_INITIALIZER;
static __thread int g_replay_owned;
static void replay_enter(void) {
    // Reject true same-thread recursion before locking instead of deadlocking
    // or silently allowing it to overwrite the active command buffer.
    if (g_replay_owned) MDIE("recursive Metal replay on the submitting thread");
    uint64_t start = now_ns();
    int rc = pthread_mutex_trylock(&g_replay_mu), contended = rc == EBUSY;
    if (contended) rc = pthread_mutex_lock(&g_replay_mu);
    if (rc) MDIE("Metal replay lock failed: %d", rc);
    g_replay_owned = 1;
    static uint64_t submissions, waits;
    submissions++;
    if (contended) {
        waits++;
        if (waits <= 24 || !(waits % 300))
            MLOG("[submit-serial] submit=%llu waits=%llu wait_ms=%.3f thread=%#llx",
                (unsigned long long)submissions, (unsigned long long)waits,
                (now_ns() - start) / 1e6, (unsigned long long)(uintptr_t)pthread_self());
    }
    if (submissions == 1) MLOG("[submit-serial] shared Metal replay serialization enabled");
}
static void replay_require_owner(void) {
    if (!g_replay_owned) MDIE("Metal replay commit without owning its state");
}
static void replay_leave(void) {
    replay_require_owner();
    g_replay_owned = 0;
    int rc = pthread_mutex_unlock(&g_replay_mu);
    if (rc) MDIE("Metal replay unlock failed: %d", rc);
}
#else
static void replay_enter(void) {}
static void replay_require_owner(void) {}
static void replay_leave(void) {}
#endif
// END SERIAL REPLAY GUARD
"""
    anchor = 'static pthread_t g_sub_thread; static _Atomic int g_sub_thread_set;'
    assert text.count(anchor) == 1
    text = text.replace(anchor, guard + '\n' + anchor)
    changes = [
        ('static int on_sub_thread(void) { return atomic_load(&g_sub_thread_set) && pthread_equal(pthread_self(), g_sub_thread); }',
         'static int on_sub_thread(void) {\n#if defined(DM_SERIAL_REPLAY) && DM_SERIAL_REPLAY\n    return g_replay_owned;\n#else\n    return atomic_load(&g_sub_thread_set) && pthread_equal(pthread_self(), g_sub_thread);\n#endif\n}'),
        ('void mtl_sub_begin(void) {\n    mtl_init();',
         'void mtl_sub_begin(void) {\n    replay_enter();\n    mtl_init();'),
        ('uint64_t mtl_sub_commit(void) {\n    defer_fallback();',
         'uint64_t mtl_sub_commit(void) {\n    replay_require_owner();\n    defer_fallback();'),
        ('    objc_autoreleasePoolPop(g_sub_pool); g_sub_pool = NULL;\n    return fence;',
         '    objc_autoreleasePoolPop(g_sub_pool); g_sub_pool = NULL;\n#if defined(DM_SERIAL_REPLAY) && DM_SERIAL_REPLAY\n    atomic_store(&g_sub_thread_set, 0);\n#endif\n    replay_leave();\n    return fence;'),
        ('static uint64_t g_fence;                        // Highest committed fence value (incremented exclusively by the submit thread)',
         '#if defined(DM_SERIAL_REPLAY) && DM_SERIAL_REPLAY\nstatic _Atomic uint64_t g_fence;  // Readers include the present and guest threads.\n#else\nstatic uint64_t g_fence;\n#endif'),
        ('    if (g_fence) mtl_wait(g_fence, UINT64_MAX);',
         '    uint64_t fence = mtl_last_fence();\n    if (fence) mtl_wait(fence, UINT64_MAX);'),
        ('RED_ONCE(40, "类型化缓冲槽绑的不是缓冲贴图（贴图号 %d 类型 %lu）", tid, (unsigned long)t.textureType);',
         'RED_ONCE(40, "类型化缓冲槽绑的不是缓冲贴图（贴图号 %d 类型 %lu）", tid, (unsigned long)t.textureType);\n                static unsigned mismatches; if (mismatches++ < 32) {\n                    const treg_t *r = tid > 0 && tid < g_ntreg ? &g_treg[tid] : NULL;\n                    MLOG("[tbuf-mismatch] shader=%s stage=%d reg=%d handle=%#llx tid=%d metal_type=%lu guest_target=%d guest_fmt=%d serial=%llu", sh->key, nvn_stage, reg, (unsigned long long)S.tex[nvn_stage][reg], tid, (unsigned long)t.textureType, r ? r->t.d.target : -1, r ? r->t.d.format : -1, r ? (unsigned long long)r->t.serial : 0ull);\n                }'),
    ]
    for old, new in changes:
        if text.count(old) != 1: raise ValueError('Submission source anchor changed: ' + old[:90])
        text = text.replace(old, new)
    path.write_text(text, encoding='utf-8')
    path = source/'dm/gfx/nvn/hle_nvn.c'
    text = path.read_text(encoding='utf-8')
    old = '    ((queue_t *)q)->submits++;\n    mtl_sub_begin();'
    assert text.count(old) == 1
    text = text.replace(old, '    mtl_sub_begin();\n    ((queue_t *)q)->submits++;')
    path.write_text(text, encoding='utf-8')



def patch_memstat_startup(source):
    """Retry the existing sampler after the iOS env.txt has been loaded."""
    path=source/'dm/host/memstat.m'
    text=path.read_text(encoding='utf-8')
    old='#include "dm.h"'
    assert text.count(old)==1
    text=text.replace(old,old+'\n#include <stdatomic.h>')
    old='__attribute__((constructor)) static void memstat_start(void) {\n    const char *p = getenv("DM_MEMSTAT");\n    if (!p || !*p) return;'
    new='__attribute__((constructor)) void dm_memstat_start(void) {\n    const char *p = getenv("DM_MEMSTAT");\n    if (!p || !*p) return;\n    static _Atomic int started;\n    if (atomic_exchange(&started, 1)) return;  // Constructor and post-env call share one sampler.'
    assert text.count(old)==1
    path.write_text(text.replace(old,new),encoding='utf-8')
    path=source/'dm/ios/Sources/main.m'
    text=path.read_text(encoding='utf-8')
    anchor='    NSString *romfs = romfs_locate();'
    new='#if defined(DM_MEMSTAT_AFTER_ENV) && DM_MEMSTAT_AFTER_ENV\n    extern void dm_memstat_start(void);\n    dm_memstat_start();  // Documents/env.txt is now available to the sampler.\n#endif\n'
    assert text.count(anchor)==1
    path.write_text(text.replace(anchor,new+anchor),encoding='utf-8')



def patch_bounded_uploads(source):
    """Upload staging is not a main-replay resource; use its own GPU lifetime."""
    path=source/'dm/gfx/nvn/nvn_mtl.m';text=path.read_text(encoding='utf-8')
    anchor='static void up_flush(void) {\n    up_owner_check("up_flush");'
    helper=r"""
// BEGIN UPLOAD COMPLETION RETIRE
#if defined(DM_BOUNDED_UPLOADS) && DM_BOUNDED_UPLOADS && DM_NVN_M3
static void up_retire_completed(NSMutableArray *staging) {
    // This list owns only upload scratch buffers, never sampled textures or
    // guest pools. Called after this upload command buffer has finished.
    for (id<MTLResource> buffer in staging) res_remove(buffer);
    [staging removeAllObjects];  // Do not retain buffers through a captured block.
}
#endif
// END UPLOAD COMPLETION RETIRE
"""
    assert text.count(anchor)==1;text=text.replace(anchor,helper+'\n'+anchor)
    changes=[
      ('    atomic_fetch_add(&g_up_pending, 1);\n    [FB_ON(o, cb) FB_SEL:^(FB_ARG f) {',
       '#if defined(DM_BOUNDED_UPLOADS) && DM_BOUNDED_UPLOADS && DM_NVN_M3\n    NSMutableArray *staging = [[NSMutableArray alloc] initWithArray:g_up_keep];\n    if (g_up_block) [staging addObject:g_up_block];\n#endif\n    atomic_fetch_add(&g_up_pending, 1);\n    [FB_ON(o, cb) FB_SEL:^(FB_ARG f) {'),
      ('        atomic_store(&g_up_alloc_busy[slot], 0);\n        atomic_fetch_sub(&g_up_pending, 1);',
       '#if defined(DM_BOUNDED_UPLOADS) && DM_BOUNDED_UPLOADS && DM_NVN_M3\n        up_retire_completed(staging);\n#endif\n        atomic_store(&g_up_alloc_busy[slot], 0);\n        atomic_fetch_sub(&g_up_pending, 1);'),
      ('    g_up_cb = nil; if (g_up_block) { defer_obj_release(g_up_block); g_up_block = nil; } g_up_block_off = g_up_block_size = 0;\n    for (id obj in g_up_keep) defer_obj_release(obj);',
       '#if defined(DM_BOUNDED_UPLOADS) && DM_BOUNDED_UPLOADS && DM_NVN_M3\n    g_up_cb = nil; g_up_block = nil; g_up_block_off = g_up_block_size = 0;\n#else\n    g_up_cb = nil; if (g_up_block) { defer_obj_release(g_up_block); g_up_block = nil; } g_up_block_off = g_up_block_size = 0;\n    for (id obj in g_up_keep) defer_obj_release(obj);\n#endif'),
      ('    size_t need = (n + 255u) & ~((size_t)255u);\n    if (!g_up_block || g_up_block_off + need > g_up_block_size) {',
       '    size_t need = (n + 255u) & ~((size_t)255u);\n#if defined(DM_BOUNDED_UPLOADS) && DM_BOUNDED_UPLOADS && DM_NVN_M3\n    if (g_up_block && g_up_block_off + need > g_up_block_size) {\n        // Finish and submit this upload before allocating the next block.\n        // The three existing upload slots bound outstanding GPU batches.\n        up_flush(); up_begin();\n    }\n#endif\n    if (!g_up_block || g_up_block_off + need > g_up_block_size) {'),
    ]
    for old,new in changes:
        if text.count(old)!=1:raise ValueError('Upload source anchor changed: '+old[:90])
        text=text.replace(old,new)
    # Bounded creation evidence distinguishes persistent texture growth from
    # transient staging at the failing scene without recording resource bytes.
    anchor='    res_add(mt);   // Both first upload and re-upload register residency here'
    addition="""#if defined(DM_BOUNDED_UPLOADS) && DM_BOUNDED_UPLOADS && DM_NVN_M3
    if (mt.allocatedSize >= (8u << 20)) { static unsigned large; if (large++ < 96)
        MLOG("[upload-alloc] texture ser=%llu %ux%ux%u levels=%u nvn_fmt=%#x bytes=%llu metal_total_mb=%llu",
            (unsigned long long)t->serial, t->d.width, t->d.height, t->d.depth, t->d.levels, t->d.format,
            (unsigned long long)mt.allocatedSize, (unsigned long long)(g_dev.currentAllocatedSize >> 20)); }
#endif
"""
    assert text.count(anchor)==1;text=text.replace(anchor,addition+anchor)
    path.write_text(text,encoding='utf-8')


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
    patch_depth_diagnostics(source)
    patch_submission_serialization(source)
    patch_memstat_startup(source)
    patch_bounded_uploads(source)
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
    text=text.replace('-DDM_NVN_M3=1','-DDM_NVN_M3=1 -DDM_SHELL=1 -DDM_SERIAL_REPLAY=1 -DDM_MEMSTAT_AFTER_ENV=1 -DDM_BOUNDED_UPLOADS=1')
    path=source/'dm/ios/project_shell_check.yml'
    if path.exists():raise ValueError('Project spec already exists')
    path.write_text(text,encoding='utf-8')
    return gd,path


def validate_submission_guard(source, results, compiler=None):
    """Compile the actual guard and stress its shared-state lifetime."""
    text = (source/'dm/gfx/nvn/nvn_mtl.m').read_text(encoding='utf-8')
    guard = text.split('// BEGIN SERIAL REPLAY GUARD:', 1)[1].split('\n', 1)[1].split('// END SERIAL REPLAY GUARD', 1)[0]
    assert text.index('    replay_enter();', text.index('void mtl_sub_begin(')) < text.index('    mtl_init();', text.index('void mtl_sub_begin('))
    commit = text.split('uint64_t mtl_sub_commit(void) {', 1)[1].split('uint64_t mtl_last_fence', 1)[0]
    assert commit.index('g_cb = nil;') < commit.index('objc_autoreleasePoolPop') < commit.index('replay_leave();')
    assert 'static _Atomic uint64_t g_fence;' in text
    harness = r"""
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdatomic.h>
#include <string.h>
#include <errno.h>
#include <time.h>
#undef assert
#define assert(x) do { if (!(x)) { fprintf(stderr, "check failed: %s line %d\n", #x, __LINE__); exit(72); } } while (0)
#ifdef _WIN32
#include <windows.h>
typedef SRWLOCK pthread_mutex_t;
#define PTHREAD_MUTEX_INITIALIZER SRWLOCK_INIT
static int pthread_mutex_trylock(pthread_mutex_t *p) { return TryAcquireSRWLockExclusive(p) ? 0 : EBUSY; }
static int pthread_mutex_lock(pthread_mutex_t *p) { AcquireSRWLockExclusive(p); return 0; }
static int pthread_mutex_unlock(pthread_mutex_t *p) { ReleaseSRWLockExclusive(p); return 0; }
static uintptr_t pthread_self(void) { return GetCurrentThreadId(); }
static uint64_t now_ns(void) { LARGE_INTEGER n, f; QueryPerformanceCounter(&n); QueryPerformanceFrequency(&f); return (uint64_t)(n.QuadPart * (1e9 / f.QuadPart)); }
static void yield_thread(void) { SwitchToThread(); }
#else
#include <pthread.h>
#include <sched.h>
static uint64_t now_ns(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec * 1000000000ull + t.tv_nsec; }
static void yield_thread(void) { sched_yield(); }
#endif
#define MDIE(...) do { fprintf(stderr, __VA_ARGS__); fputc('\n', stderr); exit(73); } while (0)
#define MLOG(...) ((void)0)
#define DM_SERIAL_REPLAY 1
GUARD
static atomic_int active, overlaps, ready, go, mode;
static int command_buffer, upload_owner, pool_owner;
static atomic_uint_fast64_t fence;
static void run_worker(int id) {
    atomic_fetch_add(&ready, 1);
    while (!atomic_load(&go)) yield_thread();
    for (int i=0; i<4000; i++) {
        if (atomic_load(&mode)) replay_enter();
        if (atomic_fetch_add(&active, 1)) atomic_fetch_add(&overlaps, 1);
        if (atomic_load(&mode)) {
            replay_require_owner();
            assert(!command_buffer && !upload_owner && !pool_owner);
            command_buffer = upload_owner = pool_owner = id;
            if (!(i % 13)) yield_thread();
            assert(command_buffer == id && upload_owner == id && pool_owner == id);
            command_buffer = upload_owner = pool_owner = 0;
            atomic_fetch_add(&fence, 1);
        } else {
            for (int j=0;j<4;j++) yield_thread();
        }
        atomic_fetch_sub(&active, 1);
        if (atomic_load(&mode)) replay_leave();
    }
}
#ifdef _WIN32
static DWORD WINAPI worker(LPVOID p) { run_worker((int)(intptr_t)p); return 0; }
#else
static void *worker(void *p) { run_worker((int)(intptr_t)p); return NULL; }
#endif
int main(int argc, char **argv) {
    if (argc > 1 && !strcmp(argv[1], "recursive")) { replay_enter(); replay_enter(); return 9; }
    if (argc > 1 && !strcmp(argv[1], "unowned")) { replay_require_owner(); return 9; }
    for (int pass=0;pass<2;pass++) {
        atomic_store(&ready, 0); atomic_store(&go, 0); atomic_store(&overlaps, 0); atomic_store(&mode, pass);
#ifdef _WIN32
        HANDLE threads[6]; for (int i=0;i<6;i++) threads[i]=CreateThread(NULL,0,worker,(void *)(intptr_t)(i+1),0,NULL);
#else
        pthread_t threads[6]; for (int i=0;i<6;i++) assert(!pthread_create(&threads[i],NULL,worker,(void *)(intptr_t)(i+1)));
#endif
        while (atomic_load(&ready)<6) yield_thread(); atomic_store(&go,1);
        for (int i=0;i<6;i++) {
#ifdef _WIN32
            assert(WaitForSingleObject(threads[i],30000)==WAIT_OBJECT_0); CloseHandle(threads[i]);
#else
            assert(!pthread_join(threads[i],NULL));
#endif
        }
        printf("%s overlaps=%d\n", pass ? "guarded" : "negative-control", atomic_load(&overlaps));
        assert(pass ? !atomic_load(&overlaps) : atomic_load(&overlaps)>0);
    }
    assert(atomic_load(&fence)==24000 && !command_buffer && !upload_owner && !pool_owner);
    puts("24000 guarded submissions preserve command-buffer, upload and pool ownership.");
    return 0;
}
""".replace('GUARD', guard)
    src=results/'submission-guard-test.c'; src.write_text(harness, encoding='utf-8')
    exe=results/('submission-guard-test.exe' if os.name=='nt' else 'submission-guard-test')
    argv=(compiler or ['clang'])+['-std=gnu11','-O1','-Wall','-Wextra','-pthread',str(src),'-o',str(exe)]
    r=subprocess.run(argv, capture_output=True, text=True, timeout=90)
    (results/'submission-guard-compile.log').write_text(r.stdout+r.stderr, encoding='utf-8')
    if r.returncode: raise RuntimeError('Submission guard test did not compile: '+r.stderr)
    checks=[]
    for arg, expected in [([],0),(['recursive'],73),(['unowned'],73)]:
        r=subprocess.run([str(exe),*arg],capture_output=True,text=True,timeout=40)
        checks.append({'mode':arg or ['stress'],'exit_code':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
        if r.returncode!=expected: raise RuntimeError('Submission guard regression: '+str(checks[-1]))
    report={'actual_guard_compiled':True,'threads':6,'guarded_submissions':24000,
        'negative_control_reproduced_overlap':True,'guard_prevents_overlap':True,
        'same_thread_recursion_rejected_without_deadlock':True,'unowned_commit_rejected':True,
        'guard_covers_begin_through_autorelease_cleanup':True,'checks':checks,
        'platform':platform.platform(),'metal_gpu_test':False}
    (results/'submission-guard-validation.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    return report



def validate_bounded_uploads(source,results):
    text=(source/'dm/gfx/nvn/nvn_mtl.m').read_text(encoding='utf-8')
    alloc='static id<MTLBuffer> up_alloc('+text.split('static id<MTLBuffer> up_alloc(',1)[1].split('\n}\n',1)[0]+'\n}\n'
    retire=text.split('// BEGIN UPLOAD COMPLETION RETIRE\n',1)[1].split('// END UPLOAD COMPLETION RETIRE',1)[0]
    old=alloc.replace('up_alloc(', 'old_up_alloc(',1)
    start=old.index('#if defined(DM_BOUNDED_UPLOADS)');end=old.index('#endif',start)+len('#endif')
    old=old[:start]+old[end:]
    code=r"""
#import <Foundation/Foundation.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#define DM_BOUNDED_UPLOADS 1
#define DM_NVN_M3 1
#define MTLResourceStorageModePrivate 0
#define UP_BLOCK_SIZE (32u*1024u*1024u)
#define CHECK(x) do { if (!(x)) { fprintf(stderr,"check failed: %s line %d\n",#x,__LINE__); exit(72); } } while(0)
#define MDIE(...) do { fprintf(stderr,__VA_ARGS__); exit(73); } while(0)
@protocol MTLResource <NSObject> @end
@protocol MTLBuffer <MTLResource>
@property(readonly) NSUInteger length;
@end
static uint64_t live_bytes,peak_bytes;static unsigned removed,flushes;
@interface FakeBuffer : NSObject<MTLBuffer>
@property NSUInteger length;
@end
@implementation FakeBuffer
- (void)dealloc { live_bytes-=self.length; }
@end
@interface FakeDevice : NSObject
- (id<MTLBuffer>)newBufferWithLength:(NSUInteger)n options:(NSUInteger)o;
@end
@implementation FakeDevice
- (id<MTLBuffer>)newBufferWithLength:(NSUInteger)n options:(NSUInteger)o {
    (void)o;FakeBuffer *b=[FakeBuffer new];b.length=n;live_bytes+=n;if(live_bytes>peak_bytes)peak_bytes=live_bytes;return b;
}
@end
static FakeDevice *g_dev;
static id<MTLBuffer> g_up_block;
static size_t g_up_block_size,g_up_block_off;
static uint64_t g_up_blocks_new,g_up_swaps,g_up_swap_bytes;
static NSHashTable *g_up_old;
static NSMutableArray *g_up_keep,*pending;
static NSMutableSet *registered;
static void res_add(id<MTLResource> r) { [registered addObject:r]; }
static void res_remove(id<MTLResource> r) { CHECK([registered containsObject:r]);[registered removeObject:r];removed++; }
RETIRE
static void complete_oldest(void) {
    CHECK(pending.count>0);NSMutableArray *staging=pending[0];up_retire_completed(staging);CHECK(staging.count==0);[pending removeObjectAtIndex:0];
}
static void up_begin(void) { while(pending.count>=3)complete_oldest(); }
static void up_flush(void) {
    NSMutableArray *staging=[[NSMutableArray alloc] initWithArray:g_up_keep];
    if(g_up_block)[staging addObject:g_up_block];
    [pending addObject:staging];g_up_block=nil;g_up_block_off=g_up_block_size=0;[g_up_keep removeAllObjects];flushes++;
}
OLD
ALLOC
static void reset(void) {
    while(pending.count)complete_oldest();g_up_block=nil;[g_up_keep removeAllObjects];[registered removeAllObjects];
    CHECK(live_bytes==0);g_up_block_size=g_up_block_off=0;peak_bytes=0;flushes=removed=0;
}
int main(void) { @autoreleasepool {
    g_dev=[FakeDevice new];g_up_keep=[NSMutableArray new];pending=[NSMutableArray new];registered=[NSMutableSet new];
    size_t off;
    for(int i=0;i<40;i++) { @autoreleasepool { id<MTLBuffer> temp=old_up_alloc(20u<<20,&off);CHECK(temp && off==0); } }
    uint64_t old_peak=peak_bytes;CHECK(old_peak==40ull*UP_BLOCK_SIZE);CHECK(flushes==0);reset();
    for(int i=0;i<40;i++) { @autoreleasepool { id<MTLBuffer> temp=up_alloc(20u<<20,&off);CHECK(temp && off==0);CHECK(pending.count<=3); } }
    uint64_t new_peak=peak_bytes;CHECK(new_peak<=4ull*UP_BLOCK_SIZE);CHECK(flushes==39);
    up_flush();while(pending.count)complete_oldest();CHECK(registered.count==0 && live_bytes==0);
    printf("40 uploads: baseline peak=%llu MiB, bounded peak=%llu MiB; completion retirement leaves no scratch alive\n",old_peak>>20,new_peak>>20);
    reset();
    id<MTLBuffer> a=up_alloc(3,&off);CHECK(off==0);id<MTLBuffer>b=up_alloc(5,&off);CHECK(a==b && off==256);a=nil;b=nil;
    @autoreleasepool { id<MTLBuffer> temp=up_alloc(128u*1024u*1024u+3u,&off);CHECK(temp && off==0 && g_up_block_size==128u*1024u*1024u+256u); }
    up_flush();while(pending.count)complete_oldest();CHECK(live_bytes==0);
    puts("alignment, oversized single upload, delayed GPU completion and exact-once retirement passed; this is an ARC lifetime test, not a GPU execution test");
}return 0;}
""".replace('RETIRE',retire).replace('OLD',old).replace('ALLOC',alloc)
    path=results/'upload-lifetime-test.m';path.write_text(code,encoding='utf-8');exe=results/'upload-lifetime-test'
    r=subprocess.run(['clang','-std=gnu11','-O1','-Wall','-fobjc-arc',str(path),'-framework','Foundation','-o',str(exe)],capture_output=True,text=True,timeout=60)
    (results/'upload-lifetime-compile.log').write_text(r.stdout+r.stderr,encoding='utf-8')
    if r.returncode:raise RuntimeError('Upload lifetime test failed to compile: '+r.stderr)
    r=subprocess.run([str(exe)],capture_output=True,text=True,timeout=30)
    (results/'upload-lifetime-test.log').write_text(r.stdout+r.stderr,encoding='utf-8')
    if r.returncode:raise RuntimeError('Upload lifetime regression: '+r.stdout+r.stderr)
    result={'actual_up_alloc_and_retirement_helper_compiled':True,'objc_arc_lifetime_test':True,
        'negative_control_peak_mib':1280,'bounded_peak_mib_max':128,
        'alignment_oversize_and_delayed_completion_passed':True,'gpu_execution_test':False,'stdout':r.stdout}
    (results/'upload-lifetime-validation.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result


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
        report['submission_guard_test']=validate_submission_guard(source,results)
        report['upload_lifetime_test']=validate_bounded_uploads(source,results)
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
