# 自研 runtime 与 DeepSeek Harness：逐轮轨迹复盘

分析于 2026-11-06 夜至 11-07 进行。用户要求先看真实障碍，不预设 domain 应该精简，也不局限 DSH。本轮只读历史轨迹、公开输出、工具参数、实际呈现回执和模型工件；没有新模型运行、代码修复或建筑方案修改。

**目前最有证据的共同障碍，在“建筑判断如何变成可保存声明”和“失败后如何定位并修复”这两个环节。** 历史宿主接线、输出上限及退出策略也造成过独立失败。另有明确案例表明：运行完成、几何合法和保留输入中的建筑信息并不是同一件事。上述问题需要分别看，不能统一叫作“domain 太重”或“模型不行”。

用户已对最新 C03 人工检查并表示“质量基本 OK”。本轮将其记作该次成果的可用性反馈，不转换为量化精度评分，也不追改历史失败结论。

## 样本与计数

本轮聚焦同一利苑建筑的整栋 mesh 运行和 23/F 局部先导，涵盖最新成功、早期失败、恢复完成和自研路径的不同模型。下列 13 个分析分段均有逐请求表；另两段 setup-abort 也逐轮列出，但因运行条件不等排除出路径比较。其余幸福中心、图纸输入与十月局部实验未在本轮穷尽回看，结论不外推成所有任务的发生率。

| 分段 | 路径 / 模型 | 实际上游请求 | 终态与使用边界 | 逐轮表 |
|---|---|---:|---|---|
| 11-06 修复版真实冷启动 | DSH / Sol 6.1 high | 28 | C03，用户基本接受；v173，零恢复 | [28轮](D:/Sim-BIM-Agent/runs/trajectory-review-20261106/latest/rounds.md) |
| 11-06 前次冷启动 | DSH / Sol 6.1 high | 22 | R22部分SSE中断；只有C01，无finish；v173 | [22轮](D:/Sim-BIM-Agent/runs/trajectory-review-20261106/earlier-dsh/lee-garden-six-full-dsh-sol61-cold-20261106.md) |
| 11-06 中途修复与恢复整案 | DSH / Sol 6.1 high | 33 | 四次显式恢复后C03；domain冻结v171，运行代码中途变化 | [33轮](D:/Sim-BIM-Agent/runs/trajectory-review-20261106/recovered-local-dsh/full-rounds.md) |
| 11-05 早期整案 | DSH / Sol 6.1 high | 48 | 两次恢复；反馈序列化障碍；最终预算耗尽，诊断TEST候选不等于交付 | [48轮](D:/Sim-BIM-Agent/runs/trajectory-review-20261106/earlier-dsh/lee-garden-six-full-dsh-sol61-20261105.md) |
| 11-05 局部先导初段 | DSH / Sol 6.1 high | 1 | 另有1个提交前relay409步骤 | [2步骤](D:/Sim-BIM-Agent/runs/trajectory-review-20261106/recovered-local-dsh/local-rounds.md) |
| 同局部先导 continued | DSH / Sol 6.1 high | 30 | 另有1个提交前墙钟截止步骤；序列化问题反复 | [31步骤](D:/Sim-BIM-Agent/runs/trajectory-review-20261106/recovered-local-dsh/local-continued-rounds.md) |
| 11-05 初次局部配对 native | 自研 / Flash | 13 | 32k输出上限截断；v169/runtime46 | [13轮](D:/Sim-BIM-Agent/runs/trajectory-review-20261106/recovered-local-dsh/pair-native-rounds.md) |
| 同批 DSH | DSH / Flash | 11 | 输出截断，无有效交付 | [11轮](D:/Sim-BIM-Agent/runs/trajectory-review-20261106/recovered-local-dsh/pair-dsh-rounds.md) |
| 11-05 提高输出上限 native | 自研 / Flash | 33 | 正式finish，但最后2空间、0开口；v169/runtime46 | [33轮](D:/Sim-BIM-Agent/runs/trajectory-review-20261106/recovered-local-dsh/cap-native-rounds.md) |
| 同批 DSH | DSH / Flash | 10 | 宿主先超时，上游完整响应晚到 | [10轮](D:/Sim-BIM-Agent/runs/trajectory-review-20261106/recovered-local-dsh/cap-dsh-rounds.md) |
| 10-24 整栋单Agent | 自研 / Sol 6.1 high | 24 | C04正式交付；domain146/runtime42 | [24轮](D:/Sim-BIM-Agent/runs/trajectory-review-20261106/native-history/sol24-round-review.md) |
| 10-20/21 整栋复测 | 自研 / Sol 6.1 high | 21 | C03正式交付；domain132/runtime36 | [21轮](D:/Sim-BIM-Agent/runs/trajectory-review-20261106/native-history/sol21-round-review.md) |
| 同阶段 Flash 整栋 | 自研 / Flash | 48 | C04正式交付，历史必要质量不通过；domain132/runtime36 | [48轮](D:/Sim-BIM-Agent/runs/trajectory-review-20261106/native-history/flash21-round-review.md) |

这些主样本合计 **322 次实际上游请求，另有2个提交前拒绝步骤**。setup-abort 两侧各6次已发上游，单列12次；连同它们，总共分析 **334 次已发上游请求及2个提交前拒绝步骤**。模型、领域版本、输出上限、任务范围与干预不同，不能把耗时差解释成宿主或模型的单因素优劣。

逐轮表保留行动、反馈、下一步、解释边界及源证据；相邻 JSON 保留结构化标注。实际请求以 wire/adapter_request 对照，不直接数查看器的model事件。早期DSH合并轨迹50个model事件仅对应48次实际请求；重复历史和本地交付确认都不算新增模型请求。等待时间是请求到响应/失败的经过时间，不等于纯思考或可节省时间；未知usage保持未知。

## 1. 建筑声明到几何保存，存在反复的转换障碍

最新DSH R9的屋顶deck有近共线点；规整支撑线交点把一个顶点移动约6.597m，因而拒绝。R10去掉冗余点后，两个共享门采用后的端点相差约 `1.20e-7 m` / `6.56e-7 m`，仍触发shared_host_conflict。R12为此做51项patch，把primitive/local_baseline改成显式共享polygon与端点；之后还需公共frame及真实门宿主修订才推进。

原生Sol 10-24 R12则直接记录：原始平面覆盖通过，规整后共享节点分离约0.00846–0.03482m。原生Sol 10-21 R14还出现把已规整source坐标用于原始声明而被拒，回到原始宿主坐标后才成功。

**证据能支持：模型在建筑之外承担了坐标表示、规整基准及共享关系的维护。** 旧版和最新版本均出现同类阻力；这不是DSH独有。具体算法缺陷是否相同仍须区分。检查本身阻止了大位移、错宿主和破坏拓扑，不能据此认定应取消。最新R13门段跨凹口、旧冷启动R10错误patch造成自交，是模型声明/修改真实有错的反例。

证据：[最新R9–17](D:/Sim-BIM-Agent/runs/trajectory-review-20261106/latest/rounds.md)、[原生Sol24 R9–14](D:/Sim-BIM-Agent/runs/trajectory-review-20261106/native-history/sol24-round-review.md)、[原生Sol21 R13–15](D:/Sim-BIM-Agent/runs/trajectory-review-20261106/native-history/sol21-round-review.md)。

## 2. 错误被分阶段暴露，修复接口有时不能完成建议的修复

两次v173 DSH冷启动都把manifest里的 `architectural_modelling_policy` 错放进plan根部。这个输入错误从初稿就存在，却在多个几何检查之后才报告。模型按反馈remove该字段，patch又因根字段白名单拒绝，最后只能整份重发。

- 前次冷启动：R12报未知字段 → R13删除失败 → R14整份重发。
- 最新冷启动：R15报未知字段 → R16删除失败 → R17整份重发。

这既有模型混淆输入层级的问题，也有保留非法草稿后缺乏原地修复出口的问题。最新R15拒绝至C01构建工具返回195.99秒；这是观察到的区间，不能视为某项改造必然能全额节省的时间。

原生Flash局部 outputcap R13–27经历另一条逐层链：门宿主失败 → 删门后窗宿主失败 → 删所有开口后才报非法 `role=core`。到字段层才发现的错误可能早已存在。没有依赖关系的结构检查与依赖几何的检查应如何安排，仍待设计，本轮只确认延迟暴露现象。

## 3. 反馈有时能直接修，有时迫使模型分页、猜位置或重写

- v173前次冷启动R14报46条约束，实质为两类模板问题各展开23层。R15–17用了三个模型回合回读，R18才统一墙面/小台阶并保留空间与管井保存成功。**46条不是46个独立建筑问题。**
- 最新R5/6分别要求读取60/100条截面项，每次实际只得8项，R7切到字符分页；R11猜错 `/records`，虽然上轮提供了 `/rejections`，随后通用提示又指向不相关的plan_partition。
- 原生Flash整栋R20–24只看到缺 `source_refs`，实际呈现回执也没有嵌套对象路径。模型连续猜instance字段，R25补space级字段才过。此例来自旧版，具体字段诊断在当前版本的状态没有逐函数验证，不标作原样未修复。
- 原生Flash局部R14/16/17持续读changes；R18猜不存在的 `/errors`。它与最新DSH的报告访问摩擦在行为上相似。

反馈并非全部无效：前次冷启动R18确实根据反馈修成；旧Flash R46明确收到字段类型要求后，下一轮就改正。需要区分反馈的对象定位、分页结构、建议路径和实际可执行性，而非只比较回执字数。

## 4. 过检与完成并不保证保住建筑信息，退化也不只有一种原因

最明确的局部链来自11-05自研Flash outputcap：R13/15首稿13空间、36开口 → R23降为2空间、12开口 → R26删门只剩10窗 → R27删所有开口 → R28改合法role后保存 → R33正式finish。参数和最终源对象共同证明该变化，不只依据空间数。删除的是原本声明的核心区分和门窗，不能称无损精简。

旧原生Flash整栋也从独立系统逐渐合并到一个连续shaft，但最初稿已经合并了部分电梯，因此不能把全部退化归因于后面的几何拒绝。时间/预算提示、初始解释能力、修复失败与工具门槛在同一轨迹中并存，没有单因素实验确定权重。原生Sol在多次拒绝后仍保住独立核心，最新Sol也完成可用交付，是重要反例。

最新DSH R19的空间关系检查返回0冲突，其预期却含随后被模型自己推翻的前厅隔墙。R20再看原图，R21才改变空间划分。该工具验证的是源模型与调用者提交预期的一致性，明示不等于独立读图判断；“没有冲突”不能当作建筑解释正确的证明。

证据：[原生局部R13–33](D:/Sim-BIM-Agent/runs/trajectory-review-20261106/recovered-local-dsh/cap-native-rounds.md)、[旧Flash整栋R16–48](D:/Sim-BIM-Agent/runs/trajectory-review-20261106/native-history/flash21-round-review.md)、[最新R19–22](D:/Sim-BIM-Agent/runs/trajectory-review-20261106/latest/rounds.md)。

## 5. 接线、输出上限、超时和预算分别造成过失败，不能统称外部断连

| 现象 | 证据 | 当前解释 |
|---|---|---|
| DSH反馈序列化失败 | 11-05早期整案13次实际build/patch结果被替换成`value is not lossless JSON`，导致找草稿/试frame/最小复现 | 历史宿主适配阻断；后两次v173冷启动完整反馈已能传入，不能继续当作当前失败 |
| 多次恢复后预算耗尽 | 早期整案R2/R5外部故障经人工恢复，最后48次请求耗尽 | 最终缺交不能全部归外部；TEST单空间是明确的诊断试验，不是正式交付降级 |
| 明确流式中断 | 前次冷启动R22：HTTP200后部分SSE、RemoteProtocolError、无完整usage | 确认的外部传输失败；新版恢复合同后另起冷启动成功，但未实跑触发断流恢复 |
| 输出截断 | 原生Flash整栋两次length；局部32k两侧也有截断 | 模型输出预算/宿主续跑问题，不能记作网络失败；原生整栋续跑确有作用 |
| 宿主先退、上游晚到 | outputcap DSH R10宿主300秒idle timeout，上游303.96秒完整响应，晚约4.10秒 | 有答案但宿主未采用；不能称供应商没有生成。此为历史配置，未证明最新Sol仍受此超时限制 |
| 文字收尾没有finish | 旧原生Flash R43 | 原生runtime继续后最终finish，证明闭环有实际作用，仍需单列建筑质量 |

工具状态也不是充分依据：早期整案宿主标18个error，另有3个返回内容明确错误却标completed。反过来13个序列化error也不能统称模型的几何错误。本轮按实际回执内容与后续输入对照，不只数红色事件。

## 6. 上下文累积是实测负担，但还不能指定唯一原因

最新28轮实际输入从11,696增长至273,003tokens；累计输入4,115,503。供应商逐项usage中45份工具schema累计210,644tokens，占约5.12%。末轮实际发送的投影请求包含14图（约7.70M编码字符），工具返回文本约612,613字符，历史函数参数约82,284字符。schema的token与图片/文字的字符是不同口径，不能相加或直接比较token占比。

三批图归档已削减原始图历史，但当前批次可能包含多图，文字与声明也继续累积。数据能证明每轮携带的上下文在增大，不能单独证明它造成了多少秒延迟，也不能从45个工具直接推导必须删除多少工具。现阶段尚无把图片、说明、回执、声明、推理及排队各自耗时分开的因果证据。

## 不能归为障碍的必要工作与反例

最新R1–8的32次工具没有错误，大多形成后续采用的观察/量测。它们需要优化到多少轮，目前没有反事实。首稿后R18–26又改变了前厅/办公空间解释、底商前窗高度并补端墙窗与入口；至少存在真实内容增量。原生Sol10-24后续补前室双门、上层幕墙差异和底商入口；Sol10-21后续改基座外廓。这里确认的是证据驱动的实质修改，不能仅凭模型自述断言每项修改都建筑正确。

因此不能把“首稿已保存后的全部时间”定为冗余。最新R27对未改变的O21再次做相似关系检查，是更具体的重复候选；同时它绑定最终源哈希，是否能够增量复用仍待验证。旧Sol某轮六次分页发生在同一模型回合，也不能误算成六次模型往返。

## 当前结论与未决事项

本轮确认的重点是：**几何表达/规整转换、错误的定位与可修复性、必要建筑信息是否在修复中被保留，以及上下文如何随执行累积。** 宿主特有的历史接线故障已单独列出，不与这些共同问题合并。

尚不能由本轮决定：哪些工具可以删除、哪些检查可以取消、是否替换自研runtime、哪家模型或宿主更优、可省多少时间，以及是否应先整体重写domain。下一次设计应以这些具体失败链和成功反例为输入；本轮没有形成预定的删改清单。

完整离线分析包位于 `D:/Sim-BIM-Agent/runs/trajectory-review-20261106`；各子目录保留逐轮JSON、可见回执解码和哈希核对。最新正式案例833份封账源文件仍匹配；其他样本的关键源文件由各分析分支分别核对。输入、授权、模型结果和原始运行记录不提交Git；本报告只记录结论和本地证据入口。
