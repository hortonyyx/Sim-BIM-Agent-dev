# 收工交接：下一轮利苑整案冷启动

用户于 2026-11-06 明确收工，下一轮另开新会话跑一次整案。本轮只保存交接，不再调用模型、追加测试或开发。新会话按用户当轮指令启动。

## 已完成与边界

- 最新实现提交 `61eeeb7a`，已推送；`runtime-v47-20261106 / domain-v173-20261106` verify 通过。
- GPT-6.1 Sol / high + DeepSeek Harness + 共用 BIM domain 已实际交付利苑整栋：33 次请求、72 次工具、C03、exit 0、delivered true。起点是新案例，但中途四次显式恢复及运行时修复，尚未验证最终版本一次从零完成。
- 已修复几何规整误拒绝、MCP 反馈丢失、失败草稿检查、请求恢复和图片历史管理。原生 runtime 保留，不决定替换。
- 显示三角化、底面误隐藏、浮点薄条、内部接缝、切层残留高亮/测量均修复；单层隐藏 stairwell/shaft，包括本案例电梯、楼梯和管井，整栋仍显示连续核心。25 个单层选项通过独立浏览器验收。源空间没有修改。
- 前一轮上游仍有四次传输中断或处理状态未知，未宣称供应商故障彻底消失。

## 下一轮起点

原案例：`D:/Sim-BIM-Agent/cases/lee-garden-six-full-dsh-sol61-fixed-20261106`。原始资料在 `inputs/`，任务文本在 `shared/task.txt`，配置在 `config.json`。只复用原始资料和用户任务；按当前 launcher 合同建立全新目录、DSH_HOME 和会话，重新冻结当前 domain。不要复制历史候选、轨迹、模型回复或旧 shared 冻结文件作为模型输入。

当前配置：`gpt-6.1-sol`、high、现有项目 ChatGPT 订阅授权 `subscription_store=legacy-project`，DSH 可执行文件 `D:/Program_AI/deepseek harness/DeepSeek Harness.exe`。这一 Sol 路线不是 Paratera Flash；不要混回较早的 Flash 对照任务。现有上限：200 模型请求、1200 工具调用、256 候选、无整案墙钟上限。

使用 `python -m src.dsh_runtime --case <new-case> --prepare` 准备并冻结；实际启动从第一轮即传 `--image-history-batches 3`，让旧图保留 SHA 回读引用，初始图片与最新完整批次继续可见。此前图片策略从请求 29 才启用，不是一个全过程固定条件实验。无需重新登录或读取其他应用凭据。

验收目标为无需人工修改建筑方案或中途修代码的一次整栋完成：真实 `finish_bim`、有效交付证明、覆盖基座/办公/屋顶、源几何与独立视觉核验、完整输入/模型/工具轨迹、耗时和返工分账。运行参数 `minimum_floor_elevations=2` 只是一道最低技术门槛，不能代替整栋覆盖核验。

如果失败，保留失败、未知用量与阶段产物，如实区分传输、工具、模型策略和质量问题。后续修复或恢复不能冒充同一轮无需人工介入的冷启动通过。不要为了交付手工修改候选或向工作模型泄露旧答案。

## 查阅入口

- [整栋交付与运行修复](2026-11-06_deepseek_harness_full_building.md)
- [显示修复与独立验收](2026-11-06_viewer_display_fixes.md)
- 原案例 `evaluation/result/report.md`、`evaluation/trace/index.html`：运行账本和完整轨迹。
- 原案例 `evaluation/display-revision-001/viewer.html`：修正版查看器；`revision.json`、`independent-review.json` 记录显示修订。原 `private/dsh/work/bim/candidate_03` 和 terminal/finish 证明未改。

本轮没有更新桌面安装；开发与下一轮 DSH 启动使用 `D:/Sim-BIM-Agent/repo`。仓库仍保留原有未提交的 README、architecture、roadmap 改动，以及旧报告和 `.playwright-cli/`，不要清理或混入本轮提交。
