# DSH 后端稳定性迭代

本轮从开发分支 `codex/unified-orchestrator` 的 `be9322e3` 开始。首批使用 `0ce6a51f`（domain-v183 / runtime-v57），恢复修订批使用 `c5a870dc`（domain-v184 / runtime-v59），局部编辑修订批使用 `6fa1749a`（domain-v185 / runtime-v59）。每批分别从登记提交导出，原始材料、任务和模型配置相同。

**v185 / v59 可作为前端联调的后端基线。** 最终两案都从原始材料自动完成交付，各启动一次宿主；交付文件摘要与独立重编译均通过。SM25 的平面、开口及连通关系可与图纸对应。利园在多次运行中仍会遗漏入口或误判顶层立面，建筑质量需要逐案复核。

| 冻结构建 | SM25 首个有效候选 / 整案 | 利园首个有效候选 / 整案 | SM25 / 利园建模退回次数 |
|---|---|---|---|
| 历史 R7 | 5:23 / 16:26 | 10:43 / 29:13 | 0 / 5 |
| 11 月 20 日 v182 | 4:08 / 24:13 | 15:03 / 50:30 | 0 / 11 |
| v183 | 无候选；24:46 中断 | 25:57 / 57:21 | 0 / 6 |
| v184 | 14:31 / 62:27 | 18:09 / 58:55 | 0 / 11 |
| **v185** | **11:11 / 31:37** | **13:56 / 32:12** | **1 / 4** |

时间为分:秒，整案包含服务退避。建模退回按所有原生宿主执行段计数，包含对应工具的超时或拒绝。v185 的利园比 v182 快 18 分 18 秒，SM25 慢 7 分 24 秒；每个版本、每案均为单次运行。[历史阶段计时](D:/Sim-BIM-Agent/runs/build-iteration-20261120/baseline.json) · [最终批计时](D:/Sim-BIM-Agent/runs/backend-stability-local-edits-20261123/metrics.json) · [最终交付验签及重编译](D:/Sim-BIM-Agent/runs/backend-stability-local-edits-20261123/delivery-verified.json)

本轮共启动六次真实整案，均使用项目授权的 ChatGPT 订阅；离线验证单独运行。新加坡时间 22:28 读得账户周额度剩余 63%。[订阅额度读数](D:/Sim-BIM-Agent/runs/backend-stability-20261123/usage-observations.jsonl)

## 跨工具读图

主控连续使用普通看图和精确归档回读时，宿主按实际返回的图片字节识别同一视图。不同裁剪分别保留，无论工具是单次还是成批调用。初始用户图和近期其他工具图优先占用预算，比较图只使用剩余图片张数和字节。任务切换、混入其他操作或读取失败时释放比较集合。归档格式仍为 v1，审计投影版本升到 v3。

重放 SM25 的 29 份历史请求，旧投影与当时保存的上游报文相符，两版均能完整还原原始留档。第 28–46 次请求都从仅有三张立面裁图变为四张均可见。第 47 次转入模型检查时两版投影重新相同。第 35 次原请求为 748,808 字节，按图片差额估算新投影为 1,613,849 字节，其中包括四张整图和四张裁图。能同时比较四面提高了单次图片传输量。另以批量读图验证了倒序返回、部分失败和单批超预算时的行为。[真实请求回放](D:/Sim-BIM-Agent/runs/backend-stability-20261123/image-replay/report.json) · [DSH 定向测试](D:/Sim-BIM-Agent/runs/backend-stability-20261123/image-replay/targeted-parallel.log)

## 几何修补反馈

轮廓位移相同时，退回信息会归在一组，同时列出原始草稿中每一项需复核的声明。共享屋面或墙面只在一侧声明开敞时，反馈给出接触双方，以及已有声明的位置。跨层接触关系改变时，反馈定位到原稿里的楼层和空间。参数化草稿与通用 BIM 草稿各自使用其可编辑 JSON 路径，回执绑定提交文件；原有的几何校验继续生效。

重新核对 11 月 20 日利园 C12 的两段原生宿主日志，首次执行记录 8 次失败的建模调用，恢复后还有 3 次，合计 11 次。其中 10 次被建筑校验退回，另 1 次工具调用超时。上轮报告的 8 次只覆盖首次执行。原 R7 仅执行一段，共 5 次。用这 10 份遭拒的草稿和 1 份合法草稿重新调用真实 BIM 工具。首稿 8 项对应 3 组位移，前 8,192 个字符能看到全部 8 个编辑位置。合法对照中的 160 个空间、1,354 个边界、276 个开口和 230 条连接与历史模型逐项相同。[工具回放](D:/Sim-BIM-Agent/runs/backend-stability-20261123/draft-replay/report.json) · [206 项定向测试](D:/Sim-BIM-Agent/runs/backend-stability-20261123/draft-replay/targeted-final.xml)

## 首轮成品与原料

856 项离线快速测试通过，另有 41 项 DSH 测试和 206 项几何诊断相关测试，各组有重叠。从提交导出 240 个成品文件，使用独立 Python 3.12 环境和冻结依赖完成隔离验收。对 11 月 20 日的两个最终候选及利园 R7，使用新版内核从各自完整提案重新生成的源 BIM 与归档 JSON 的全部字段及源模型摘要一致。使用原生 finish 回执重新核对的 6 项或 7 项交付文件摘要均匹配。[快速测试](D:/Sim-BIM-Agent/runs/backend-stability-20261123/fast-final.log) · [隔离成品验收](D:/Sim-BIM-Agent/cache/backend-stability-20261123-verified/report.json) · [历史模型正对照](D:/Sim-BIM-Agent/runs/backend-stability-20261123/controls/reference-recompile.json)

冷启动沿用 11 月 20 日每案的原始任务、模型、推理强度、预算、观察者配置和 9 个原始输入文件，只更换成品和输出目录。SM25 使用 6.2 Sol 主控及可选后台 Terra，利园使用 6.1 Sol 主控及可选同步 Terra。参考模型、旧错误、回放与评估脚本均位于工作模型材料之外。同历史表对比的计时从 DSH 记录的 `private/start.json` 起算，产品命令开始时间单独记录，包含接入检查。运行日志记录发送与响应，质量用正式选定的候选单独复核。[批次身份及原料摘要](D:/Sim-BIM-Agent/runs/backend-stability-20261123/launch-evidence/batch-identity.json)

## 首轮发现的宿主恢复问题

SM25 在 24 分 46 秒时以内部错误结束，尚未生成候选。主控的 40 次普通读图返回了 40 个不同的视图，连续只做普通读图最多 3 轮。第 30 个模型请求返回服务端过载，要求等 304 秒。原生宿主看到第一帧错误就退出，中继仍在等待正式结束帧。主循环用上一轮的状态启动了交付续跑，约 10 秒后才登记的中断请求要求另一种恢复标记，第二次宿主启动因此失败。[首次冷启动独立复核](D:/Sim-BIM-Agent/runs/backend-stability-20261123/quality-sm25-cold1.md)

首轮利园在 57 分 21 秒交付 C07，七份文件验真通过。建模退回 6 次，首个有效候选出现在 25 分 57 秒。11 月 20 日这两个数字是 11 次和 15 分 03 秒。首份有效候选到正式交付由 35 分 27 秒缩到 31 分 24 秒；首稿前，两项同步观察耗时和长建模请求占去了更多时间。模型确实看到了具体修补路径，并据此修复了屋面边界和重复声明。独立复核发现，首层三处有纹理依据的入口建出了两处，餐厅端面的门被建成玻璃窗。裙房 2.2 米截面 P95 偏差由 0.932 米降到 0.183 米，23/F 的斜向核心和顶三层渐收的窗带得以保留。[首批利园耗时](D:/Sim-BIM-Agent/runs/backend-stability-20261123/repair-timing-lee.md) · [首批利园建筑评阅](D:/Sim-BIM-Agent/runs/backend-stability-20261123/quality-lee-cold1.md)

修订版在向宿主转发可识别的服务故障前登记原请求与退避时间。宿主退出后，主循环等当次中继请求完成再判断如何继续。无分类的错误帧在后续有明确终态时采用该终态；仅收到错误消息就结束时保留未分类状态。有效交付只记一次。归档读图遇到真实视图 ID 配错有效格式的 SHA 时，工具回执给出对应的 ID、SHA 与原始读取位置。另修正了观察者读取结果在 DSH 工具别名下的识别。[归档回读回放](D:/Sim-BIM-Agent/runs/backend-stability-20261123/receipt-repair/report.json) · [中继错误独立复核](D:/Sim-BIM-Agent/runs/backend-stability-20261123/controls/independent-stream-results.json)

用已安装的 DSH、真实中继与 BIM 工具，分别重放当时的错误帧顺序，并控制宿主先退出、明确过载后到的顺序。两种顺序在旧版均触发错误续跑。修复后，3 秒退避会恢复同一请求并验真交付，生成一份新读图记录并执行一次 finish；保留原始 304 秒退避、案例只剩 75 秒时，保留原请求并准确记录服务端原因。这些是本地故障重放，未调用云端模型。[旧新版本回放](D:/Sim-BIM-Agent/runs/backend-stability-20261123/recovery-replay/summary.json) · [最终版 158 项定向测试](D:/Sim-BIM-Agent/runs/backend-stability-20261123/recovery-replay/targeted-v59.log)

最终登记版本 v59 再次通过上述三组安装版验证。两份可恢复样本均校验了同一请求的重试、单次执行和七项交付文件的哈希。错误重放目录中留有两个干净的临时源码副本，Windows 递归清理命令被自动审批拒绝。[v59 安装版验证](D:/Sim-BIM-Agent/runs/backend-stability-20261123/recovery-replay/v59-final/summary.json)

## 恢复修订批的成品

v184 成品包含 240 个文件，独立环境的版本与成品校验通过。快速测试通过 856 项，接口集成检查通过 1,020 项，跳过 1 个须主动选择在线运行的测试。前述定向测试与这两个测试集有交集。用该版内核重建 SM25 C06、利园 C12 和利园 R7，全部源模型字段与各自保存的 JSON 一致，交付文件哈希也一致。[快速测试](D:/Sim-BIM-Agent/runs/backend-stability-confirmation-20261123/fast-v59.log) · [接口集成检查](D:/Sim-BIM-Agent/runs/backend-stability-confirmation-20261123/node-v59.log) · [成品验收](D:/Sim-BIM-Agent/cache/backend-stability-final-20261123-verified/report.json) · [历史交付重建](D:/Sim-BIM-Agent/runs/backend-stability-20261123/controls/reference-recompile-v59.json)

SM25 在第 31、33 个主控请求收到真实服务端过载，分别要求等待 369 秒、364 秒。两次自动恢复使用与中断前字节相同的请求；工具重复执行数为零，最终通过正式 finish 交付 C05。两层外廓 IoU 均为 1.000，最差单室均为 0.975。27 个非走廊房间都通过门直连本层走廊。34 处立面开口中心偏差最大 0.05 米，宽度偏差最大 0.08 米。主控发出 51 个请求、观察者 4 个；99 次直接读图均取得不同像素。整案 62 分 27 秒，其中包含两次退避。[恢复修订批 SM25 评阅](D:/Sim-BIM-Agent/runs/backend-stability-confirmation-20261123/quality-sm25-cold1.md) · [两案交付核验](D:/Sim-BIM-Agent/runs/backend-stability-confirmation-20261123/delivery-verified.json)

利园在 58 分 55 秒交付 C15，11 次建模调用被退回。首个有效候选为 18 分 09 秒。原图显示的餐厅侧门已恢复，正面窄门仍缺失，顶三层的窗带重复了标准层，23/F 前室和两处管井的关系也比历史 C12 简化。模型确曾把一扇已声明的门移到侧面，并把立面退阶判断为屋冠而保留等长窗带。成品的 300 个开口和 507 个宿主绑定均有效，这项检查只能验证已声明的开口。[恢复修订批利园评阅](D:/Sim-BIM-Agent/runs/backend-stability-confirmation-20261123/quality-lee-cold1.md) · [批次计时](D:/Sim-BIM-Agent/runs/backend-stability-confirmation-20261123/metrics.json)

## 已建模型上的局部编辑

首批利园主控第 33 次请求试图在 C06 上改 6 扇窗、增加 6 个开口并更新一条说明。通用编辑工具把已采用的轮廓重新投到网格，报出 25 个楼层共 52 项轮廓位移。只改一扇窗、写回原说明乃至把完全相同的几何再送入原路径，都得到逐项相同的 52 个退回。原参数提案重新展开能逐字段复现 C06；单独用源模型编译器检查这次改动，也能生成 176 个空间、347 个开口。[原版对照和失败清单](D:/Sim-BIM-Agent/runs/backend-stability-20261123/lee-revise-replay/results.json)

v185 在通用局部编辑入口核实已保存的父模型、采用记录和楼层几何。若修改限于开口或说明，可以沿用已通过校验的轮廓，并对新改的开口执行建筑规整、宿主及源模型校验。结构性修改仍经过完整规整。在 C06 的真实编辑重放中，44 个楼层、176 个空间和全部边界与原模型一致，只有所要求的 6 项修改和 6 项新增；下一代候选也能继续编辑。混入越墙、零宽、超高或重叠门窗时，无法正式交付。复核中发现初版会在 67 个未编辑开口上重写浮点数，已修正并逐一比对。[独立编辑对照](D:/Sim-BIM-Agent/runs/backend-stability-20261123/controls/independent-lee-edit-results.json) · [389 项几何定向检查](D:/Sim-BIM-Agent/runs/backend-stability-local-edits-20261123/geometry-targeted-final.log)

v185 冻结包使用自带的 Python 环境和已注册工具，复现相同的正反对照。全新提交历史的无效展开轮廓，仍然触发原 52 项拒绝。856 项快速测试和 1,020 项节点测试分别通过，1 项显式在线测试按离线策略跳过；另有 389 项几何定向测试通过。定向测试曾发现一个历史测试用“成功回执”读取预期拒绝；在前版单独复现后，测试改为直接检查原生错误标记及结构化报告，正常与拒绝两种路径均通过。最终成品验真通过，独立重编译的三份历史终稿源字段一致。[冻结包原案对照](D:/Sim-BIM-Agent/runs/backend-stability-20261123/lee-revise-replay/v185-installed/summary.json) · [冻结包独立反例](D:/Sim-BIM-Agent/runs/backend-stability-20261123/controls/independent-lee-edit-product.json) · [节点测试](D:/Sim-BIM-Agent/runs/backend-stability-local-edits-20261123/node-final.log) · [成品验收](D:/Sim-BIM-Agent/cache/backend-stability-local-edits-20261123-verified/report.json) · [历史终稿重建](D:/Sim-BIM-Agent/runs/backend-stability-20261123/controls/reference-recompile-v185.json)

## v185 冷启动与建筑评阅

SM25 在 31 分 37 秒交付 C05，42 个主控请求、6 个观察者请求，未触发恢复。两层平面外廓与参照的 IoU 均为 1.000，最差单室为 0.9608。14 个一层空间和 15 个二层空间全部对应，27 个非走廊空间各有门直通本层走廊。二层东北房间建成办公室，参照依据家具布置判作休息区，原图没有文字标注用途。61 个开口的 88 条宿主绑定成立。依四张立面图量测的 34 处开口中心最大偏差 0.05 米，宽度最大偏差 0.02 米。主控 44 次直接读图取得 44 个不同视图，连续只做普通读图最长 2 轮；归档回读有 3 次因身份参数错误被拒。6 项交付文件验真通过。[SM25 建筑评阅](D:/Sim-BIM-Agent/runs/backend-stability-local-edits-20261123/quality-sm25-cold1.md) · [SM25 模型](D:/Sim-BIM-Agent/runs/backend-stability-local-edits-20261123/executions/sm25-dsh-cold1/private/dsh/work/bim/candidate_05/viewer.html)

利园 C04 在 32 分 12 秒正式交付，1 次宿主启动，33 个主控请求、7 个观察者请求。与历史 C12 相比，建模退回由 11 次降到 4 次，整案缩短 18 分 18 秒。这两个配置本轮各只有一次结果。C04 的源模型可重编译，7 项交付摘要一致。2.2 米基座截面 P95 偏差为 0.112 米，三处办公截面为 0.091、0.065、0.063 米；23/F 的三座梯井、两处管井及东前室得到保留。首层三处有纹理依据的入口建出了 A 和 B，餐厅端面 C 仍然漏掉，顶三层的两翼窗带仍套用普通层。77.8 米和 81.3 米处只测量闭合屋面构件时，P95 分别为 0.168 和 0.330 米；把半开放检修区也纳入范围时，两者为 2.213 和 8.820 米。整栋质量保留这些局部条件。[利园建筑评阅](D:/Sim-BIM-Agent/runs/backend-stability-local-edits-20261123/quality-lee-cold1.md) · [利园模型](D:/Sim-BIM-Agent/runs/backend-stability-local-edits-20261123/executions/lee-dsh-cold1/private/dsh/work/bim/candidate_04/viewer.html) · [利园独立验签](D:/Sim-BIM-Agent/runs/backend-stability-local-edits-20261123/lee-delivery-verified.json)
