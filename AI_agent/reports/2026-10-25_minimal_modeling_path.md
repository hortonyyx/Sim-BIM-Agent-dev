# 最小建模路径：实施与局部对照

用户已批准继续上一轮计划：先定位错误产生阶段，做普通 Python 的最小构造路径、一次有效反馈和一次性交付；先测两个新局部问题的四腿对照，有净收益后再跑利苑、幸福中心。完整目标仍为单 Agent 合格交付≤10分钟且相对合格同模型通用对照至少快一半；未来生产分工≤5分钟。

本轮基线 `87f39bd4` / runtime43-domain150。当前外部证据根：`D:/Sim-BIM-Agent/cases/minimal-modeling-path-20261025/`。下面是进行中的实施记录，不能当成性能或质量达标结论。

## 已确认归因

- 利苑 B 的门在 draft001–003 是1m；模型扩西楼梯后共享边只剩0.270349m，`patch3.py` 用 `min(1, edge_length-.1)` 主动缩成0.170349m，后续四候选保留。内核没有偷偷把1m门缩成17cm。
- 窄廊在首次声明已存在：x=1.6m处32.573mm，首角点修订后30.074mm，采用后C01 16.699mm，后续重铺为12.286mm。初稿就只有西侧办公入口。不能把它说成合格走廊被规整首次破坏。
- 逐稿/脚本归因保存于 `construction/ATTRIBUTION_AND_IMPLEMENTATION.md`，26份原文件hash不变。旧生产产物没有修写。

## 已实现与独审

- `b9142b7a`：普通Python `region/shared_edges/shared_opening/clearance_report`。保留真实孔洞、组件和指定门宽；不做自动缩门、最近墙猜测、填缝或最大块保留。clearance只是调用者给定圆形通行体宽度的几何探针，不是法规/出入口可达性/建筑质量判定。作者与独审各27项通过。分别变换、细分多边形的浮点碎缝仍可能拒绝，明确要求共同局部构造后统一变换；未宣称任意浮点近邻同墙鲁棒。
- `1952f28e`：显式 `source_enclosure_intent_v1` 在实际采用源上唯一解析边界，绑定真实摘要后走原围护校验。旧v1摘要契约保留；inferred必须有来源和假设；parametric可透传和局修。完整v3语义保留，v2 virtual显示不再画成实体；没有自动推断开放屋面。作者74项、追加工具路径15项、独审22项通过，范围有重叠。
- `b07e23d5`：抽出同一 `compile_source_proposal`，支持可选 `BimSession.check_plan`；无candidate/出图，但保留当前采用、源校验和完整回执。check→build会重复编译，不要求每次强制检查，也不提前宣布提速。新增门跨度摘要给模型，水平检修口/未知宿主不假报门宽；这由独审发现并修正。
- 同提交增加 `finish_to`：走真实finish，复用原生完整交付核验，逐字节复制选中source/viewer到新目录，不覆盖已有输出，不自动审阅或再渲染。独审发现图纸回叠链接漏复制，已补当前source引用的回叠图并记录publication hash；支持文件明确为发布时快照，原finish核心绑定单列。正常overlay→finish_to复验1项通过7.38秒。

根的首次组合检查54通过/5失败：3项tuple/list序列化比较夹具、1项mesh fixture漏frame、1项新增反馈超原摘要字节预算。前三类修正夹具/比较口径；反馈保留全部原finding和完整回执，压缩重复说明/可回查元数据，最终原预算检查通过。随后source/check/反馈47通过/1失败只剩摘要预算；最终单项已通过。SDK construction与inference8项通过9.53秒；独审同源编译/旧completion16项通过11.41秒，发布与completion13项通过10.04秒。均为离线，不计入真实模型用量。

## 新局部题与入口缺口

两题为全新合成图纸/说明：31°旋转核心平面与-17°开放屋顶/机房平剖面，工作模型只看PNG和brief；数值真值/评价材料另存evaluation，不提供出口坐标答案。共同环境从锁文件重新建立（Python3.12.10及172个distribution），A没有editable主库/PYTHONPATH；不是复制旧虚拟环境。

顺序：core A→core B→roof B→roof A；同GPT-6.1 Sol/high，单Agent，不委派。每腿180秒筛选目标、360秒诊断保护线、最多20已完成响应或300k reported tokens后终止所属进程树，在途消耗照记。四腿保护线约1.2M加在途，开发/独审另计。所有失败保留；首个可解析源立即保存原字节快照，不能用最终稿替代首稿。

原始输入准备后发现两个真实图纸入口缺口，已在正常能力路径修复（`fd7916f1`）：

1. 既有drawings全局0.1m顶点检查会拦合法旋转派生坐标。新drawings复用已有参考线采用政策，像素编译仍保持原网格；仅内部经核验adoption证据允许参考线语义，不让模型JSON自报waiver，旧manifest不变。
2. parametric候选即使有当前原图回叠和有效局部审阅，楼层覆盖只看pixel provenance。现在复用同一有效review判据绑定image→floor，不能凭自报或只有校准就complete。独审补出原floor_facts漏内孔与物理围护语义，已在共同判据补齐：合法source修改内孔/连续core或实体墙→open后旧review失效；height-only可局部复用，building仍需重新审阅。

未将图纸改标unknown、未取消期望平面范围。公共出口统一支持完整v2/v3；两臂使用相同renderer。B只有在真实native finish/source/renderer绑定均核验后复用已经计时的viewer，A由同一实现渲染，所有实际渲染和收尾计入端到端时间。

## 冻结前验证与版本

| 范围 | 结果 | 耗时 | 边界 |
|---|---|---|---|
| 图纸新入口及旧process review/mesh政策独审 | 52通过 | 41.47s | 最后内孔增补前的同组验证 |
| 内孔/围护/height复用修复独审 | 4通过 | 7.32s | 原外部合法source反例也复核失效 |
| compact反馈预算独审 | 2通过 | 4.01s | 仍保留原输送预算 |
| 主库drawing precision/coverage、SDK construction、enclosure intent合并验证 | 46通过 | 19.13s | 四个明确模块，不是全库 |

旧pixel regularization回归最初15通过/1失败，main也复现：成功assemble的过长紧凑回复漏顶层regularization；`a4954cdf`在既有24k预算内保留该小摘要，失败单项复验1通过8.26s。不是隐藏掉失败，也未因此放宽几何规则。独审证据见外部`construction/DRAWING_INTERFACE_REVIEW.md`与`enclosure/DRAWING_SOURCE_POLICY.md`。

统一登记并verify通过：runtime43/domain151（仅domain变化），注册提交`3c76d478f11d6de4ccde250465d3db905dc240cb`。发布包`D:/Sim-BIM-Agent/releases/minimal-modeling-path-20261025-v1`，560文件/26,749,277字节；桌面未更新。两个实施工作树确认补丁已合入、无独有文件/活动进程后收回。实验控制器对admission/CLI/collector分别记录所属进程树终态，计数未齐只标已报告下界，不能把未知在途量写成零。

冻结包四个无模型preflight与真实SDK单房publication复核均通过。单房v1夹具省略外门必填other_space:null，失败证据保留；只改夹具开v2，真实未审阅finish被拒、实际叠图已查看，最终finish_to及公共collector绑定验证通过，未改发布包。

实验freeze写于2026-10-25 21:53:13 +08:00，sha256 `14b77b3b8ea987db48f329fe1b834cf094207fb1a8a28b75c64662ff7be54713`。根读取后批准启动原定四腿，首轮core A自21:54:03开始；每腿仅1次，中途不改产品/prompt，不将局部失败自动重跑为成功。原图、质量事实、共同合同、确切prompt、环境和控制器均绑定于`benchmark/audit/freeze.json`；预检在`audit/preflight_summary.json`。

## 后续门槛

局部先看首个合格结果、构造/修复往返与总用量。若只是换包装、增加检查而没有减少工作，不继续整栋。通过后先利苑、再幸福中心；正式匹配重复只在候选合格后开展。当前没有新的完整整栋、C组、低价模型、生产分工或真实仿真结果。
