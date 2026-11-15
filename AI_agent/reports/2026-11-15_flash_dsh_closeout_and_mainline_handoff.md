# 本轮收尾：Flash + DeepSeek Harness 与主线交接

2026-11-15，用户反馈刚完成的 DeepSeek Flash + DeepSeek Harness 运行“质量不理想”，要求本轮收在这里、下一轮回到主线开发。本次仅核对运行记录、保存评价与交接，不追加模型运行、不人工修模、不启动工程或流程改造。

## 新增运行与用户评价

案例：`D:/Sim-BIM-Agent/cases/lee-garden-six-full-dsh-flash-deepseek-20261115`。

| 项目 | 核对结果 |
|---|---|
| 模型与线路 | `deepseek-flash / high`，官方 DeepSeek Chat Completions，经本案例 loopback relay 接入 DeepSeek Harness headless 0.2.2-rc.3 |
| 运行身份 | 代码提交 `19783f346732684eaa9fa60939491c0c625faef4`，domain-v173-20261106；具体relay/launch/verify身份保存在案例provenance。不能直接当作Sol订阅适配器的同条件复测 |
| 输入 | 利苑整栋GLB、building声明、23/F平面，既有运行报告记录与11-06输入哈希一致 |
| 终态 | `exit_code=0`，真实finish选择candidate_01，交付验真通过 |
| 墙钟 | 865.876秒，即14分25.9秒 |
| 请求与工具 | 25次上游请求，全部有完整usage；40次工具，34完成、6错误 |
| 故障与干预 | 既有记录为0 provider/relay错误、0输出截断、一次尝试、无恢复及人工修改建筑方案 |
| 用量 | 输入3,505,304 tokens；输出159,789，其中reasoning135,908为输出子项；输入缓存命中96.09% |
| 已保存内容 | 117空间、970边界、256开口、190连接；29个floor记录、23个不同标高，包含连续核心容器，不能称29个实际楼层 |
| 范围与质量 | `whole_building`为绑定当前工件的模型审阅范围声明，机器几何通过不等于独立建筑质量通过；**用户人工评价：质量不理想** |

收尾时未发现命令行包含该案例路径的活动建模/relay进程。运行报告、交付选择、源文件及轨迹保持原样。

用户未在本轮列出具体质量缺陷，本次也未重做逐轮归因或建筑评分。因此不推断本次质量不足具体由模型能力、工具负担、模板简化、工程缺陷或宿主造成；也不凭单次耗时比Sol判效率胜负。先前Sol C03“质量基本OK”的反馈保持不变。

## 资料入口

- [本次运行报告](D:/Sim-BIM-Agent/cases/lee-garden-six-full-dsh-flash-deepseek-20261115/evaluation/RUN-REPORT.md)
- [完整轨迹](D:/Sim-BIM-Agent/cases/lee-garden-six-full-dsh-flash-deepseek-20261115/evaluation/trace/index.html)
- [最终查看器](D:/Sim-BIM-Agent/cases/lee-garden-six-full-dsh-flash-deepseek-20261115/private/dsh/work/bim/candidate_01/viewer.html)
- [源BIM](D:/Sim-BIM-Agent/cases/lee-garden-six-full-dsh-flash-deepseek-20261115/private/dsh/work/bim/candidate_01/source_model.json)
- [正式交接页](D:/Sim-BIM-Agent/cases/lee-garden-six-full-dsh-flash-deepseek-20261115/private/dsh/work/bim/delivery.html)
- [执行终态](D:/Sim-BIM-Agent/cases/lee-garden-six-full-dsh-flash-deepseek-20261115/private/dsh/session/execution.json)、[后处理后验真](D:/Sim-BIM-Agent/cases/lee-garden-six-full-dsh-flash-deepseek-20261115/private/control/verification-after-postprocessing.json)

这是新增的25请求样本，尚未纳入[11-07逐轮复盘](2026-11-07_cross_runtime_trajectory_analysis.md)的334次实际上游请求统计；旧分析口径不改写。

## 下一轮主线

入口为[Domain改进方案v0.1](2026-11-07_domain_improvement_proposal.md)，结合[已有逐轮分析](2026-11-07_cross_runtime_trajectory_analysis.md)。用户已明确下一轮回主线，不继续围绕Flash追加跑分或修这份产物。

1. **工程改进继续独立推进。** 优先候选仍是E1结构校验与可修复草稿、E3错误定位/模板聚合/分页；E2共享边与规整稳健性从历史最小失败片段验证。E4执行恢复复用既有修复与回归，E5先做资料存取和上下文组成核对。具体实施从当前源码与最小复现开始，不把旧已修问题重做。
2. **流程与domain重设计继续共同决定。** 用户计划提供人类建模师的理想流程、各环节所需能力和模型分工；本轮尚未收到这份流程。届时对齐建筑问题、证据、能力、表达、量测选择、复核和停止条件，再推导工具。缺少这份输入不阻塞独立工程缺陷的诊断与修复。
3. **保留结果边界。** 当前流程并非强制量测或固定工具序列；不能以删工具、放宽建筑规则或删必要对象冒充工程提速。不同产出方式可讨论，最终质量、完整交付和实际成本分别验证。自研runtime与DSH的取舍仍开放。

本轮文档收尾不修改运行源码、domain或桌面产品安装；不上传案例、图片、凭据或运行日志。用户既有README、架构、roadmap等未提交修改保持。
