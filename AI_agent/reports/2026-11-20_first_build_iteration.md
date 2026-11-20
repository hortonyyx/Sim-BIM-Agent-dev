# DSH 首稿、分区与读图效率迭代

两份修订版均自动完成正式交付。SM25 达到认可图纸参照的几何和连通水平，利园修正了用户指出的首层斜墙，标准层有原图依据的斜向核心也保留下来。两案整栋交付均慢于之前的 DSH R7，利园的基座和屋面仍有几何偏差。本轮保留为开发版本。

| 案例与版本 | 首个有效候选 | 首次双层组装 | 整案 | 建模调用被退回 | 主控＋worker 请求 |
|---|---:|---:|---:|---:|---:|
| SM25，历史 R7 | 5:23 | 8:24 | 16:26 | 0 | 23＋5 |
| SM25，本轮 v181 | 12:14 | 15:19 | 33:34 | 1 | 61＋5 |
| SM25，修订 v182 | 4:08 | 8:47 | 24:13 | 0 | 59＋6 |
| 利园，历史 R7 | 10:43 | 整栋一次构建 | 29:13 | 5 | 30＋10 |
| 利园，本轮 v181 | 未生成 | — | 3:09 后服务端过载终止 | 0 | 6＋8 |
| 利园，修订 v182 | 15:03 | 整栋一次构建 | 50:30 | 8 | 52＋10 |

时间为从进程启动起算的分:秒。每个配置实跑一次。所有已交付结果均独立重算文件摘要。11 月 20 日 17:47 新加坡时间，账户周额度显示剩余 15%，从此停止新增整案。最后两份在飞任务陆续原样跑完，18:18 读数为 6%。[本轮逐案指标](D:/Sim-BIM-Agent/runs/build-iteration-20261120/metrics-final.json) · [历史同口径数据](D:/Sim-BIM-Agent/runs/build-iteration-20261120/baseline.json) · [额度观测](D:/Sim-BIM-Agent/runs/build-iteration-20261120/quota-observations.csv)

开发基线为 `31ea18f4`，标签为 `baseline/pre-first-build-iteration-20261120`。整案只使用 DeepSeek Harness 和项目授权的 ChatGPT 订阅，图纸案例沿用 6.2 Sol 主控，混合输入沿用 6.1 Sol 主控，可选 worker 为 6.1 Terra。两案从原始材料冷启动，原始指令、模型预算和历史评阅材料的隔离方式与 R7 相同。房间用途按用户 11 月 20 日的口径评估。[实验口径](D:/Sim-BIM-Agent/runs/build-iteration-20261120/protocol.md) · [建筑复核依据](D:/Sim-BIM-Agent/runs/build-iteration-20261120/partition-controls.md)

## 首稿和建筑分区

统一参数化入口可以从当前冻结的 GLB 绑定模型已经声明的网格坐标框架。显式给出的 `z_min` 和 `z_max` 可直接作为绝对开口标高。输入缺项、冲突或引用其他模型时仍拒绝。模型的原始 JSON 保留，展开使用的表示另行绑定。长错误优先返回可修补的草稿、摘要校验值及具体几何路径。有效稿文件改变或丢失时，候选回读也会拒绝精确修补。

对利园 R7 首稿回放，首轮真实的两处轮廓问题仍然被拦截。原来 DSH 截取的前 8,192 字符中没有任何问题路径，现在两处均可见。沿用历史的第一和第四个几何补丁，省去中间两个格式补丁后，首个有效候选与历史 C02 的 1,319 个边界和 265 个开口逐一一致。该回放验证输入等价与错误可见性，单案时间另用真实冷启动测量。[回放记录](D:/Sim-BIM-Agent/runs/build-iteration-20261120/method-check/firstbuild/report.json)

指导只要求主控为推断分界寻找可能的实体隔墙走向。已知沿街入口可通入合并后的大空间。原图画出的斜墙、楼梯、井道和门洞仍按图保留。没有角度阈值、固定建模次序或强制派工次数。

利园修订版实际在 15 分 03 秒得到首个有效候选，历史 R7 为 10 分 43 秒。生成第一次完整建模调用的时间从 221 秒缩到 142 秒，但主控更晚才开始写入，初次选择的八处轮廓也需要反复修正。后续还要处理屋顶共享面的上下声明、跨层成员与最小空间宽度。运行中一次坐标框架仅声明平移与旋转，缺少来源和理由，自动绑定没有替模型补写这些证据。这解释了为什么离线重放确实省去两次历史修补，实案却仍可能更慢。[历史阶段计时](D:/Sim-BIM-Agent/runs/build-iteration-20261120/baseline.json)

## 实跑中新增的修复

SM25 的 v181 运行曾连续读取东西南北四张立面，每次请求只有最近三批图片，已读到的第四面又被挤出。v182 在连续、明确读取已归档图片时按原图 SHA 去重，受图片数量与字节预算约束。切换任务后释放扩展的比较组。回放当次第 14–42 轮的 29 份请求，第 18–34 轮原来每次三面，修订后每次四面且每面一份。两种投影都能还原完整请求。[请求回放](D:/Sim-BIM-Agent/cache/sm25-dsh-reads-replay-bekm3rk5/full-window.json)

利园 v181 首次实跑在第 6 个主控请求遇到服务端 `server_is_overloaded`，HTTP 200 的事件流明确返回失败与 324 秒退避。旧分类把它记为不可重试的内部请求错误，未生成正式候选。新宿主识别服务端过载，核对已执行工具与原请求后按服务端时间重试，重试超过剩余墙钟时保留外部失败依据。原失败运行单独保留，新的利园运行在退避结束后从头启动。[实际失败回执](D:/Sim-BIM-Agent/runs/build-iteration-20261120/executions/lee-dsh-sync/control/wire/dsh/06-reply.json)

SM25 v181 首次实跑正式交付 C06。首个楼层用了 12 分 14 秒，双层组装 15 分 19 秒，整案 33 分 34 秒。主控发出 61 个请求，worker 发出 5 个请求。共调用归档取图 33 次，涉及 11 个不同图片摘要，其中第 14–33 轮连续 20 轮只做归档取图。产物含 29 个空间、61 个开口，六项交付摘要经独立重算一致。[完整轨迹](D:/Sim-BIM-Agent/runs/build-iteration-20261120/trace-sm25-dsh-sync/index.html) · [阶段计时](D:/Sim-BIM-Agent/runs/build-iteration-20261120/metrics-sm25-dsh-sync.json)

SM25 v182 的主控主动后台派了两个楼层观察任务，各自完成后主控都发起了结果回读。worker 活跃的 62.49 秒中有 61.21 秒与主控请求重叠。首个楼层在 4 分 08 秒形成，双层在 8 分 47 秒组装。第 7–15 轮依次回读九个不同的归档视图，图片按设计同屏保留。随后主控改用普通 `view_image`。第 20–45 轮连续 26 轮只调用这个工具，多次轮换相同的四向立面裁图。第 23–46 份请求实际都只保留了三个工具图片，第四面仍退出上下文。下一轮首先应让比较中的原图和归档图共享同一份有界视图记录，再用同一组真实请求复验，允许主控结束比较后释放图片。[第 35 轮实际投影](D:/Sim-BIM-Agent/runs/build-iteration-20261120/executions/sm25-dsh-final/control/wire/dsh/35-projection.json)

## 可选并发与 Flash 验证

在 DSH 案例中开启 `worker.background=true` 后，主控可按任务选择同步等待或用 `wait=false` 后台派工。worker 获得冻结快照，主控可继续工作，按需等待读回结果。默认仍为同步。安装版 DSH 的同脚本对照使用 18 秒延迟的本地观察者，后台组主控的独立 BIM 工具调用在观察者返回前完成，同步组在观察者返回后才执行。两组均有正式 finish 和 worker 终态，测试没有上游请求。它证明等待可以重叠，整案收益取决于模型是否采用以及后续工作是否独立。[对照时间戳](D:/Sim-BIM-Agent/cache/dsh-background-experiment-20261120/comparison.json)

Flash 的开发探针在安装版 DSH 下验证了真实 BIM 读图工具、原图逐字节归属、工具身份、推理历史和模拟 503 后的会话恢复。实际 Go 订阅窄测在第一次请求返回 HTTP 403，模型尚未请求图片，无法据此评价建筑理解。未换用按量线路。旧 Flash 案的七项交付哈希可用现行验证器复核，当前统一产品的正式 Flash 整案仍待接通与验证。探针单独保存在 `codex/dsh-flash-protocol-20261120` 的 `000bffe1`。[协议、权限与证据](D:/Sim-BIM-Agent/runs/build-iteration-20261120/flash-observation.md)

## 可复验的开发版本

最终代码为 `0aa25e58`，版本为 domain-v182、runtime-v56。利园最终冷启动使用 `7215e0a0` 的 v182/v55。SM25 最终冷启动使用 v182/v56，两版间只调整了服务端退避的识别与预算边界。初次两案使用 `c1b18e4b` 的 v181/v54。不同版本的轨迹各自封存。

最终代码的快速离线套件通过 855 项。针对性套件另覆盖 DSH 读图与委派等 94 项，以及最终退避与恢复 27 项。各套件存在重叠。独立导出包含 240 个文件，在隔离环境以冻结依赖验证通过，两份案例配置可用，产品验证过程产生零个真实模型请求。开发分支为 `codex/unified-orchestrator`。[快速套件](D:/Sim-BIM-Agent/runs/build-iteration-20261120/fast-final.log) · [针对性套件](D:/Sim-BIM-Agent/runs/build-iteration-20261120/targeted-v182.log) · [最终退避套件](D:/Sim-BIM-Agent/runs/build-iteration-20261120/retry-final.log) · [隔离产品验证](D:/Sim-BIM-Agent/cache/first-build-final-verified/report.json)

## SM25 图纸质量

最终 C06 用 24 分 13 秒交付，主控 59 个请求、worker 6 个请求、建模工具零次退回。相对历史 R7 的 16 分 26 秒慢了 7 分 46 秒（按未取整时间计算）。与用户认可过的 S25-1/P 正式参考逐一叠图，两层外轮廓交并比均为 1.000，最差单间从 R7 的 0.9518 到本轮的 0.9756。29 个空间、31 扇外窗、27 个内门、3 个外门的宿主和邻接均正确，所有房间通本层走道。34 个外门窗中心对原尺寸链最大偏差 5 cm，宽度最大偏差 2 cm。东北角用途保留歧义。结论来自本轮独立审图，用户尚未对新稿打分。[最终模型查看器](D:/Sim-BIM-Agent/runs/build-iteration-20261120/executions/sm25-dsh-final/private/dsh/work/bim/candidate_06/viewer.html) · [叠图](D:/Sim-BIM-Agent/runs/build-iteration-20261120/review-sm25-dsh-final/overlay/current.png) · [独立复核](D:/Sim-BIM-Agent/runs/build-iteration-20261120/quality-sm25-dsh-final.md) · [完整轨迹](D:/Sim-BIM-Agent/runs/build-iteration-20261120/trace-sm25-dsh-final/index.html)

## 利园六期混合输入质量

C12 的首层保留约 226 m² 连续商业与办公共用入口空间，A、B、C 三处街面入口均接入。D 只有陈列证据，此处没有建行人门。原 R7 横穿大空间的斜隔墙已消失。23/F 左翼的斜向楼梯与三座电梯井、右侧核心和两处管井仍在，前方办公区连续。首层卫生间及服务区的细分借用了标准层线索，属于推断。全案有 188 个空间、310 个开口，均绑定到了有效宿主。[首层同尺度对照](D:/Sim-BIM-Agent/runs/build-iteration-20261120/review-lee-dsh-final/ground.png) · [23/F 与原图](D:/Sim-BIM-Agent/runs/build-iteration-20261120/review-lee-dsh-final/23F.png)

在相同坐标下，沿模型外围每 0.1 m 取样，量到原始 GLB 对应高度的截线。在 12.2、42.2、73.5 m 三个办公层截面，第 95 百分位距离分别为 4.2、2.7、4.0 cm。首层西端凹口仍被简化，2.2 m 截面该距离为 0.932 m，历史认可 C03 为 0.181 m。77.8 m 屋面截面本轮为 0.867 m，R7 为 0.264 m。81.3 m 处，实体通风构件该距离为 0.666 m，将半开放维护空间的空气边界一起算入则为 4.711 m。顶三层渐收窗带得到恢复，东侧核心门路有所简化。独立评阅对整栋给出有保留通过，达到历史 C03 的总体水平。[独立建筑评阅](D:/Sim-BIM-Agent/runs/build-iteration-20261120/quality-lee-dsh-final.md) · [截面与开口量测](D:/Sim-BIM-Agent/runs/build-iteration-20261120/review-lee-dsh-final/measurements.json) · [查看最终模型](D:/Sim-BIM-Agent/runs/build-iteration-20261120/executions/lee-dsh-final/private/dsh/work/bim/candidate_12/viewer.html) · [本轮全过程](D:/Sim-BIM-Agent/runs/build-iteration-20261120/trace-lee-dsh-final/index.html)

下一轮优先回放 SM25 的 26 轮普通看图，验证跨工具比较时能否同时看齐四向立面。随后回放利园首稿的轮廓与屋面错误，把重复模板的同一问题和可修正的部位一起交给主控，看能否用更少轮次形成可用首稿。保留首层、23/F 及外门窗量测作为质量对照。当前耗时差异由单次整案观测得出。
