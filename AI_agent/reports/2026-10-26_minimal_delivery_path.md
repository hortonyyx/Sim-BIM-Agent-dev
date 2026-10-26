# 入口、反馈和首稿后交付局部迭代

## 当前状态

用户批准先理顺入口、工具回复和首稿后交付，再跑核心筒与屋顶两组局部对照，质量不降。此轮仅改变 domain 的 SDK 使用路径；runtime、原生几何规则、审阅证据和完成门槛不变。不是两栋整案提速结论，也不把自动化离线检查当成独立建筑验收。

实现已集成、独立审阅通过，准备冻结 runtime43/domain152 后执行四腿。外部证据根：`D:/Sim-BIM-Agent/cases/minimal-delivery-path-20261026/`。上一轮保持只读，见 [2026-10-25 结果](2026-10-25_minimal_modeling_path.md)。

## 改动与边界

- 入口直接提供常用参数化格式、读原图、保存并回叠、实际审阅后 finish；长参考、目录、额外构造检查按需读取。几何 helper 与 CLI 细节移到 SDK_ADVANCED.md。保留已理解的有效空间信息，不为粗而合并。
- `image(image)` 统一图片参数；`build(plan, drawings=[显式标定])` 顺序调用现有 save 和完整源回叠。分别报告候选、source ready、请求视图完成；不自动标定、审阅、修复、重试或完成。
- 成功回复折叠重复声明/身份/正常投影坐标，保留当前问题、异常行、未评估范围、实际政策、图像和完整原生回执。旧真实 build 回复离线减少约42%～43%，原始 data/receipt/PNG 不变；复杂问题不硬裁到字符预算。
- `finish_to(reviews=实际判断)` 继续原生 finish 和相同字节发布，避免自行重写 source、复制或再造验收脚本。首稿保存而回叠失败只补失败视图；未知写结果先查原生回执。
- CLI 明确区分 JSON 文件与 inline JSON，旧误用给出可执行提示。

## 实施验证

| 范围 | 结果 | 耗时 | 限制 |
|---|---|---|---|
| SDK、workflow、presentation、construction、drawing coverage/precision、enclosure intent 集成 | 84 passed | 24.33s | 离线指定模块，不是全库或建筑验收 |
| 独审发现 workflow 日志二次写失败遮蔽原异常，修后 workflow | 9 passed | 7.70s | 含2个真实 native build 后存储失败反例 |
| 修后独立 workflow + presentation 复核 | 29 passed | 7.53s | 无未解决可行动发现；不重复声明独立质量 |

日志修复使用临时文件替换，失败时保留原异常、已知候选、原生回执与勿盲重试提示。此前 presentation 编辑误插守卫曾触发4项失败，修复复验通过；保留在外部实现记录。没有生产模型调用混入这些测试。开发和独审会话用量另列未知。

## 冻结的比较条件

仍为 GPT-6.1 Sol/high，同一原始锁定 Python 环境、原图/brief、共同 source v2/v3 出口、renderer、私有质量事实。A 通用工具；B 同 runtime 加冻结 SDK。A 提示词仅改工作目录路径。4腿顺序 core B → core A → roof A → roof B，每腿一次，无人工补交付或自动重试。目标180s，保护线360s/20完整响应/30万已报tokens，途中越线和未知在途用量保留。

需纠正此前口头政策判断：原 `prepare_bim_inputs` admission 的实际 manifest **没有** drawing_review_policy。新版沿用同一 admission；不能把 section 额外登记变成这轮新增硬门槛，也不能取消原本 parametric floor coverage。剖面仍属建筑判断及独立质量输入。严格全图审阅政策另由离线反例覆盖。

后续记录应分别列首次可解析 source、完整交付、独立建筑质量、总/未缓存/输出tokens与实际接口往返。首稿快不能代替完整交付；低成本须包含失败。局部净收益未建立前不自动启动利苑、幸福中心整案或分工。
