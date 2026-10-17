# iOS 18 source checks

This small CI project fetches nxnative at commit `9579030e5b1f0443c12b7376559560915b6d4c4b`, verifies its source archive, applies the experimental iOS 18 patch, and runs Apple SDK checks on a macOS runner. The upstream MIT license is included.

The workflow checks the two changed graphics implementation files against all seven upstream reference profiles and compiles a synthetic Metal library. It does not use game files, keys, signing credentials or a physical iPhone. It does not build an IPA or establish DW9E compatibility. The existing reference profiles are compilation fixtures, not a replacement DW9E profile.

Trigger: a push to `main` changing CI/patch files, or **Actions → iOS 18 source checks → Run workflow**. A job is limited to 15 minutes. Diagnostic artifacts expire after 3 days. The workflow uses a standard `macos-15` runner; GitHub documents standard runners as free for public repositories. Private repositories have different billing rules.

Local source preparation, without creating a commit:

```text
python ci/prepare_sources.py --dest <new source directory>
```

On macOS:

```text
python3 ci/apple_check.py --source <source directory> --results results
```

## 中文说明

这套配置让 Windows 用户使用云端 macOS 做 iOS 18 源码检查，无需自备 Mac。仓库只需要公开源码补丁和检查脚本，游戏、密钥和游戏派生文件均留在本机。

首次运行的目标是取得真实的 Apple SDK 编译结果，验证实验补丁；不是生成游戏安装包。检查通过后还需完成《真三国无双8 帝国》的专属图形适配、构建及手机测试。

工作流可以手动启动；CI 或补丁文件推送到 main 也会启动。单次最多 15 分钟，日志保留 3 天。公开仓库的标准 macOS 构建机属于 GitHub 文档所列免费范围；私有仓库计费不同。
