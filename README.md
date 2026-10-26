# Sim BIM Agent — 开发仓库

从图纸、文字和带贴图三维网格生成可查看、可修改、可供下游分析使用的轻量 BIM。当前有两条输入线：较完善的图纸分工（调度员加平面、立面读图员），以及开发中的 textured mesh 单模型路线；近期还通过 domain 的 Python SDK 在 Codex runtime 上做对照。桌面发布版目前只含图纸线。EnergyPlus 是可选下游。稳定识读质量、完整编辑界面与真实 ChatGPT 订阅推理仍有独立验收边界。

本仓库位于 `D:\Sim-BIM-Agent\repo`，GitHub 主库为 [hortonyyx/Sim-BIM-Agent](https://github.com/hortonyyx/Sim-BIM-Agent)。桌面固定使用 `Sim BIM Agent`，从已提交的发布包原位更新；上一版只在 D 盘保存一个程序 ZIP，更早版本由 Git 管理，见 [更新与回滚](AI_agent/workflow/development.md)。日常在本仓库开发，验证后发布；不在桌面交付目录累计开发历史。

## 开始开发

读取 [Agent.md](AI_agent/Agent.md) 与 [当前任务](AI_agent/project/roadmap.md)，其余按任务查阅。用户操作说明在 [产品 README](product/README.md)。

```powershell
. ./scripts/activate_windows.ps1
python scripts/dev/check.py
python scripts/dev/check.py --help
python -m src.agent_runtime.agent_registry verify
python scripts/release/build_product.py D:/Sim-BIM-Agent/releases/<版本名>
```

测试选择按 [开发方式](AI_agent/workflow/development.md)；普通检查不调用真实模型。发布前提交程序改动并登记实际变化的 runtime/domain 版本。发布目录内执行 `setup.ps1` 建立运行环境，再运行 `Start.cmd doctor`。

## 目录

| 目录 | 内容 |
|---|---|
| src/agent_runtime、src/harness_contracts | runtime 和公共运行契约 |
| src/agent、scripts/tool_scripts | domain、BIM 工具、指引和查看器 |
| src/agent/legacy | 显式历史流程及其资源，与当前 domain 入口分开 |
| tests | 当前有效检查、小夹具及显式选择的历史检查源码 |
| product、scripts/release | 用户说明、配置、启动入口与发布生成 |
| AI_agent | 当前目标、设计、操作规则和本轮简短记录 |
| references | 文献索引、阅读笔记与仅保存在本地的原始参考附件 |

新案例、运行和缓存分别在 D 盘同级的 `cases`、`runs`、`cache`。历史资料留在已核验 D 副本原位并退出当前 Git 版本，准确位置见 [归档索引](AI_agent/reports/archive-index.md)。GT 只供独立评价，不能作为 work model 的输入。Git 保留旧提交；迁移核验与独有证据在同级 `archive`。
