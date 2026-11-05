# 当前架构

Sim-BIM Agent 由模型、通用运行时和建筑领域能力组成。当前产品提供图纸和带贴图网格两个输入入口，输出统一的 Sim-BIM JSON 与 HTML 查看器。输入模态扩展与前端交互属于后续产品开发，不由这次代码整理预先确定。

## 调用与数据流

`sim_bim.py` → `product_cli` → 配置与输入校验 → 图纸分工或 mesh 单模型运行 → 共用 BIM 工具 → 几何编译与检查 → 保存、查看和正式交付。

| 层 | 职责 | 位置 |
|---|---|---|
| 使用入口 | 配置检查、启动、恢复、示例与授权操作 | `src/agent/product_cli.py` |
| 运行时 | 供应商协议、模型请求、工具循环、上下文、预算、事件与恢复 | `src/agent_runtime` |
| 运行契约 | 角色、消息、产物引用、预算与错误类型 | `src/harness_contracts` |
| 领域编排 | 输入冻结、建筑任务、读图分工、只读 mesh 观察与交付 | `src/agent/runtime_*.py`、`runtime_roles` |
| 几何数据 | 当前输入格式及空间、楼层、开口、依据的数据定义 | `src/agent/geometry_model` |
| 几何能力 | 编译、空间关系、模板、局部编辑、规整、观察与渲染投影 | `src/agent/geometry` |
| 工具 | 模型可调用的操作及反馈 | `scripts/tool_scripts` |
| 产物 | 候选、来源、检查报告和交付记录 | `src/agent/execution` |

## 两个输入入口

图纸入口由调度员协调平面和立面读图员。读图产物携带标定、依据与实际回叠检查；组装复用已经接受的读图结果，并处理楼层、标高、公共坐标和跨图冲突。

mesh 入口由主模型组织建模，必要时委派只读观察任务。原始 GLB、辅助 PNG 和用户声明先冻结；观察任务不能改写主 BIM。未知内部空间采用明确标注的假设，不能把推断写成实测。

两入口使用同一工具服务、几何内核和交付契约。模型、角色、工具及输入形式分别组织，不把模型供应商写死在建筑算法里。当前 provider 配置由共享路由校验，模型连接和模型输出质量分别验证。

## DeepSeek Harness 开发对照

开发库提供 [DeepSeek Harness headless 配置](../workflow/configs/deepseek_harness.yml)。它通过 Harness 自带的 MCP 客户端为已准入的运行目录启动 BIM 工具服务。做对照时，应从相同原始资料用 `prepare_bim_inputs` 分别准备两个独立目录。每个可写目录由一个调度员持有。

在 Windows 上，把 `SIM_BIM_REPO` 设为带有项目 `.venv` 的开发库绝对路径，`SIM_BIM_RUN` 设为已生成 `inputs.json` 的运行目录绝对路径。配置默认只开放 BIM 观察工具，显式设置 `SIM_BIM_WRITER=1` 可开放建模和交付工具。这里的权限只覆盖 BIM 服务，宿主自带的文件、shell 和分工工具另行配置。以下命令可检查配置组合，其他机器按实际 CLI 安装位置调整 `$dsh`。

```powershell
$dsh = 'D:/Program_AI/deepseek harness/resources/runtime/cli/bin/dsh.cmd'
$patch = Join-Path $env:SIM_BIM_REPO 'AI_agent/workflow/configs/deepseek_harness.yml'
& $dsh --profile headless --patch $patch --dump-config
```

正式对照时向两侧提供同版建模说明与任务，核对实际模型请求中的工具、图片、模型路线和预算。当前配置负责挂载工具；角色提示、分工权限、恢复行为和宿主退出条件仍需分别对齐。交付时用同一 `attest_finished_handoff` 核验实际 `finish_bim` 回执、候选 JSON 与查看器，建筑质量另做盲评。

## 数据与状态

`geometry_model` 只负责数据定义和解析，`geometry` 负责算法。当前正交输入与 `source_polygon_v1` 均有实际使用路径；不能因版本号较小就误删。几何中的 `corrections` 保存局部编辑记录。

源模型保留空间、边界、开口、接触、连通、楼层和假设。查看器是源数据的可视化投影。运行事件、当前候选及恢复记录保存在外部运行目录；恢复要求对应的文件和版本身份，不能用兼容别名绕过校验。

## 开发与发布边界

SDK 和独立评价属于开发集成能力，复用产品内核，但不进入普通使用包。退役程序及其旧测试由 Git 历史追溯。开发记录保留当时事实，不作为当前接口说明。

实际运行安装固定在桌面。日常按开发提交、桌面验收、云端发布的顺序更新，发布直接使用桌面已验收文件。产品验收覆盖安装配置、输入、模型与工具循环、保存恢复、交付和查看；建筑结果质量可按验收任务另行评价。
