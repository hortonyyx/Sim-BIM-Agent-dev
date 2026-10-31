# Sim-BIM Agent dev

从图纸、文字和带贴图三维网格生成轻量 BIM 与 HTML 查看器。当前实现包含图纸分工（调度员加平面、立面读图员）以及 textured mesh 单模型入口，后续产品结构继续由本开发主线迭代。独立成品范围止于 Sim-BIM 与查看器，不包含本库保留的旧评测流程、RAG 或 EnergyPlus 下游。模型识读质量、完整编辑界面和真实服务连通仍有独立验收边界。

本仓库位于 `D:\Sim-BIM-Agent\repo`，GitHub 开发主库为 [hortonyyx/Sim-BIM-Agent-dev](https://github.com/hortonyyx/Sim-BIM-Agent-dev)。唯一实际运行的成品安装是 `C:\Users\Horton\Desktop\Sim BIM Agent`，没有 `.git`。日常在开发库修改，生成候选后原位更新桌面、用桌面自身环境验收，再将同一份文件发布到 [Sim-BIM Agent](https://github.com/hortonyyx/Sim-BIM-Agent)。发布 Git 缓存在 `D:\Sim-BIM-Agent\cache\product-publish\repository`，不建立运行环境；`product/` 只存发布模板。完整流程见 [桌面验收与成品发布](AI_agent/workflow/product_repository.md)。

## 开始开发

读取 [Agent.md](AI_agent/Agent.md) 与 [当前任务](AI_agent/project/roadmap.md)，其余按任务查阅。用户操作说明在 [产品 README](product/README.md)。

```powershell
. ./scripts/activate_windows.ps1
python scripts/dev/check.py
python scripts/dev/check.py --help
python -m src.agent_runtime.agent_registry verify
python scripts/release/desktop_release.py status
```

测试选择按 [开发方式](AI_agent/workflow/development.md)；普通检查不调用真实模型。发布前提交程序改动并登记实际变化的 runtime/domain 版本，再按发布流程执行 `stage` 和 `publish --tag <tag> --notes-file <说明文件>`，完成 Git 与 Release 附件发布。构建暂存不安装环境，桌面验收报告绑定实际桌面路径和 manifest；自动验收与用户人工体验分别记录。

## 目录

| 目录 | 内容 |
|---|---|
| src/agent_runtime、src/harness_contracts | runtime 和公共运行契约 |
| src/agent、scripts/tool_scripts | domain、BIM 工具、指引和查看器 |
| src/agent/legacy | 显式历史流程及其资源，与当前 domain 入口分开 |
| tests | 当前有效检查、小夹具及显式选择的历史检查源码 |
| product、scripts/release | 用户说明、配置、启动模板与桌面验收、发布工具；不是另一份安装 |
| AI_agent | 当前目标、设计、操作规则和本轮简短记录 |
| references | 文献索引、阅读笔记与仅保存在本地的原始参考附件 |

新案例、运行和缓存分别在 D 盘同级的 `cases`、`runs`、`cache`。历史资料留在已核验 D 副本原位并退出当前 Git 版本，准确位置见 [归档索引](AI_agent/reports/archive-index.md)。GT 只供独立评价，不能作为 work model 的输入。Git 保留旧提交；迁移核验与独有证据在同级 `archive`。
