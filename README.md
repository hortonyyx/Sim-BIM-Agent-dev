# Sim-BIM Agent dev

Sim-BIM Agent 将建筑图纸或带贴图的三维网格转换为结构化 Sim-BIM，并生成 HTML 查看器。两个输入入口共用数据模型、几何工具、运行时和交付格式。

本仓库维护产品源码、测试和发布工具。用户安装和操作说明见 [产品说明](product/README.md)，公开使用版见 [Sim-BIM Agent](https://github.com/hortonyyx/Sim-BIM-Agent)。

## 代码导航

| 位置 | 职责 |
|---|---|
| `sim_bim.py`、`src/agent/product_cli.py` | 用户命令：配置检查、运行、恢复和示例 |
| `src/agent/geometry_model` | 输入几何、空间、楼层与开口的数据模型 |
| `src/agent/geometry` | 几何编译、空间关系、图纸和网格观察、查看器数据 |
| `src/agent/runtime_roles` | 图纸调度、平面与立面读图及装配 |
| `src/agent/runtime_*.py` | 建筑任务与通用运行时的连接 |
| `src/agent_runtime`、`src/harness_contracts` | 模型连接、工具循环、上下文、预算和恢复契约 |
| `scripts/tool_scripts` | 模型可调用的 BIM 工具和 HTML 渲染资源 |
| `src/agent/sdk.py` | 开发集成 API，复用同一套建模和交付能力 |
| `tests`、`scripts/dev` | 当前功能检查、独立评价及开发维护 |
| `product`、`scripts/release` | 用户说明与启动模板、构建和发布工具 |
| `AI_agent` | 开发约定、当前设计和工作记录 |

## 开发与检查

```powershell
. ./scripts/activate_windows.ps1
python scripts/dev/check.py
python scripts/dev/check.py --help
python scripts/dev/agent_versions.py verify
```

检查按 [开发方式](AI_agent/workflow/development.md) 选择。普通测试禁止访问真实模型服务；真实模型验收按指定案例、模型与线路单独运行。历史程序和退役测试通过 Git 提交追溯，不留在当前执行路径。

## 发布

开发库是唯一代码修改来源。构建后更新固定桌面安装，在桌面自身环境验收，再将已验收的同一份文件发布到云端。`product/` 是模板，发布暂存和 Git 缓存不作为另一套安装。

```powershell
python scripts/release/desktop_release.py stage
python scripts/release/desktop_release.py status
python scripts/release/desktop_release.py publish --tag <tag> --notes-file <说明文件>
```

详见 [桌面验收与发布](AI_agent/workflow/product_repository.md)。输入、个人配置、授权与运行结果保存在安装目录之外；发布包只包含使用程序所需的代码和资源。
