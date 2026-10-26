# 入口、反馈和首稿后交付局部迭代

## 当前状态

用户批准先理顺入口、工具回复和首稿后交付，再跑核心筒与屋顶两组局部对照，质量不降。此轮仅改变 domain 的 SDK 使用路径；runtime、原生几何规则、审阅证据和完成门槛不变。不是两栋整案提速结论，也不把自动化离线检查当成独立建筑验收。

实现和四腿对照已完成：A两腿完整交付，B两腿均未公共交付，未达到更快/更好/更省门槛。四份局部建筑几何与通达通过，但B两份缺少输入已明确的结构化北向，不能称完整信息质量达标。测试固定 runtime43/domain152、fcbce083；随后仅修发布权限故障到 domain153/f18f4cf6，已过真实Windows受限进程零模型贯通，未把原失败补成成功、未追加模型重试。外部证据根：`D:/Sim-BIM-Agent/cases/minimal-delivery-path-20261026/`。上一轮保持只读，见 [2026-10-25 结果](2026-10-25_minimal_modeling_path.md)。

## 四腿结果

| 题目 / 路径 | 首个保存源 | 原生finish | 完整公共交付或停止 | 实报total tokens | 未缓存input / output |
|---|---:|---:|---|---:|---:|
| 核心筒 A 通用 | 113.703s | 不适用 | 140.125s完整交付 | 149,240 | 28,169 / 6,639 |
| 核心筒 B SDK | 176.375s | 223.038s成功 | 241.813s token保护，发布失败 | ≥330,445 | ≥46,808 / ≥4,341 |
| 屋顶 A 通用 | 152.218s | 不适用 | 169.406s完整交付 | 187,810 | 22,152 / 6,042 |
| 屋顶 B SDK | 110.766s | 未执行 | 177.281s token保护，未交付 | ≥312,967 | ≥29,300 / ≥3,603 |

总已报tokens下界980,462；34个有完整用量回执的响应。A两腿账本完整，B在途未报用量未知，货币与开发/独审会话成本未知。B总tokens分别为A的≥2.21倍/≥1.67倍；两题合计B输出tokens比A少约37%，总tokens反而多≥91%，不可把省代码等同于省整次成本。首源和停止时间不冒充完整交付时间，屋顶B在180s内停止仍失败。单次工程筛选无法剥离服务波动和顺序效应。

## 失败归因与实际收益边界

两个B确实用了新版image、组合build+overlay、primitive_frames/矩形并集/local_baseline；屋顶另用了enclosure intent。四腿均只有一个源版本，B首源后无几何修改。新增入口消除了此前image/name和CLI参数误用，构造脚本更短，但没有取得端到端价值。

- 核心筒B已实际打开图并携真实review调用finish_to，原生finish和绑定产物成功；随后向TemporaryDirectory写inputs.json发生PermissionError，清理再次拒绝，排错后触保护。不能再表述为“没走到finish”。即使只排除发布错误，223s原生完成仍晚于A的140s公共交付。首源前仍读取完整parametric参考，保存后还查schema并另写自检/finish脚本；native build13.127s（其中内部准入9.219s、compile0.055s/export0.189s），其余响应间隙不能伪归因为纯建筑判断。
- 屋顶B110.766s首源、实际看图，自写面积/门宽/围护/通路检查通过；随后追查north_axis=null、读长参考与revision接口，尚未finish即耗尽保护额度。**这是当前parametric/source_polygon路径的真实表达缺口**：brief和共同合同已明确+Y北向，现有顶层白名单、SourcePolygonGeometry与patch都不能承接，输出写null。不能要求模型无视已知北向来省时；也不能以legacy另一路有NorthAxisEvidence代替此路径可达。
- A核心筒一次构造成功；A屋顶有PowerShell展开$defs的读schema错误、自写凹多边形法向检查器误报，修的是接口/检查器，未观察到建筑几何方案返工。

## 独立质量

独立评阅只看匿名原图、完整未改source及独立平面/顶面视图；首次整份JSON读取连带显示producer元数据的轻微盲审暴露已记录，未追查组别/时间。四份各一个distinct source，几何/空间通达均PASS_WITHIN_BRIEF：core8个具名空间/8个正确宿主门洞、3.0m/2.2m台阶走廊；roof真实deck、28m完整外露屏风、开放头顶及两间真实封闭屋面。按原点与方位直接量测，5/10/30cm双向边界覆盖均100%；合成尺寸数值吻合不意味着栅格读图具有亚纳米精度。

北向补核：A两份以vector/direction明确+Y（两种键的下游兼容未验）；B两份均north_axis=null，实际几何方位正确但机器可读北向不完整。此缺口不能被“几何通过”掩盖。核心筒A把楼梯/电梯水平盖面记unknown，B作局部实体封口；原题明确排除跨层构造且未指定封板，不能据此宣称某方更好。未做整栋、下游仿真或模型间能力比较。

## 发布故障修复（与冻结测试分开）

同一Python3.12.10、实际Codex Windows受限token进程的零模型反例中，tempfile.mkdtemp目录首文件写入被拒；继承workspace ACL的新目录及嵌套文件写入成功。domain153仅把发布staging改为UUID新目录：Windows继承权限、POSIX保留0700，原生attest、逐文件hash、最后rename均保持。异常保留原生完成与target/staging路径，结果可能partial/unknown，不自动重跑finish或清理遮蔽原异常。

该修复主库与独立construction/workflow各13项通过（12.36s）。新建无关单房fixture的build与finish均在真实受限进程内运行；未审阅finish仍被拒，实际打开原图和回叠后正常发布，共同collector验证native/public/renderer绑定及源字节不变（2.25s）。这是零工作模型修复验证，不是四腿成功补考，也不解决北向或首源前成本。细节见外部`review/PUBLICATION_SANDBOX_VALIDATION.md`和`PUBLICATION_FIX_REVIEW.md`。

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

## 下一步与交接

下一轮优先贯通已知坐标/北向证据，从统一source声明到parametric/patch、哈希及审阅绑定；不能默认north=0，也不能把local yaw当north。再减少首源前长参考/重复读图与首源后的接口发现工作，使正常路径直接呈现当前完成义务及实际能力缺口。应先在相同受限环境验证完整出口，再做新的局部净收益筛选；本轮不自动进入利苑、幸福中心整案、便宜模型或分工。

全部生产Jobs终态、四根PID不在、563个domain152封印文件和freeze/合同/原图均保持，访问轨迹未观察到GT/其他答案/额外模型，但不是OS强隔离证明。开发工作树已安全收回；原有其他任务树未动，桌面未发布。主库程序最终runtime43/domain153、f18f4cf6；完整事实看外部`benchmark/RESULTS.md`、`audit/four-leg-metrics.json`、`review/PROCESS_ATTRIBUTION.md`、`independent-quality/quality-summary.txt`与`north-axis-addendum.txt`。旧原始run/status不回写。
