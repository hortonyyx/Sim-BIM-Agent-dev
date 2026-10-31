# 开发、检查与发布

## 开发范围

代码按当前使用能力组织。数据模型定义在 `geometry_model`，几何算法在 `geometry`，运行基础设施在 `agent_runtime`，建筑分工与任务集成在 `agent`。开发集成、评价和维护工具留在 SDK 与 `scripts/dev`。

退出功能的代码、入口、依赖和对应旧测试一并从当前树移除；Git 保留其历史。现役性质和必要数据合同继续验证。不能用无调用的兼容层、自动修补数据或忽略异常维持旧设计。

## 文件位置

开发主库为 `D:/Sim-BIM-Agent/repo`，短期工作树放同级 `worktrees`。案例、运行和缓存分别放 `cases`、`runs`、`cache`。未经归属核对，不删除用户资料或旧实验结果。

桌面 `C:/Users/Horton/Desktop/Sim BIM Agent` 是唯一维护的产品安装。构建暂存位于 `cache/release-staging`；成品 Git 缓存与验收证据位于 `cache/product-publish`，这些目录不建立另一个运行环境。`product/` 只存用户说明与配置、启动模板。

## 检查

```powershell
python scripts/dev/check.py
python scripts/dev/check.py targeted tests/test_product_cli.py
python scripts/dev/check.py node
```

`tests/check_groups.json` 显式登记当前检查范围。默认快检验证运行、恢复、预算与关键几何性质；定向检查按改动选择，节点检查覆盖当前接口集成。测试由 `no_billed_gate` 在导入前阻止非本地网络，不调用真实模型。

同一代码和范围的有效结果可以复用。只在新改动、失败或未解决风险需要时扩大检查，避免为收工重复全量。报告实际通过、失败、跳过和覆盖范围。

## 真实模型与独立验收

真实建模按本次用户指定的案例、模型、线路和范围执行。服务连接、模型请求、完整交付和建筑质量分别记录，不能互相代替。新发布完成实现后，由未参与修改的代理独立审查代码结构、使用说明、安装配置、输入、运行、恢复、输出与查看器。

当前已授权验收为 DeepSeek Flash 的 sm24 图纸与利苑 mesh；优先 OpenCode Go，再 Paratera，再官方 API。先保证运行与交付完整，有程序问题修好；建筑质量不作为本次阻断项。

## 提交与发布

正常提交、推送、tag 和可恢复的工作树操作已经授权。主助手统一集成，不强推共享历史，不提交用户未归属修改。源代码或行为变化后登记新的 runtime/domain 身份，既有登记不追改。

```powershell
python scripts/dev/agent_versions.py register
python scripts/dev/agent_versions.py verify
python scripts/release/desktop_release.py stage
python scripts/release/desktop_release.py status
python scripts/release/desktop_release.py publish --tag <tag> --notes-file <说明文件>
```

流程为开发提交 → 更新桌面 → 桌面自身环境验收 → 发布桌面同一份文件。输入、凭据、环境和运行结果不上传。更新保留上一版程序备份；失败按原更新器回退，不能把失败候选记为正式发布。详见[桌面发布流程](product_repository.md)。

## 收尾

保存简短交接和验收证据位置，核对用户原文件、提交和远端状态，收回本轮完成且干净的工作树。任何未完成项或服务限制都如实说明。
