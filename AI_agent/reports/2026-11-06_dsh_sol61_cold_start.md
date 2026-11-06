# 利苑整栋：Sol 6.1 + DeepSeek Harness 冷启动记录

2026-11-06，按用户本轮“开跑吧 6.1sol + DeepSeek harness + Sim-BIM domain”启动最终版本的全新利苑整案。一次启动、无恢复、运行代码和建筑方案未经人工修改；但第 22 次模型请求在接收 HTTP 200 和部分 SSE 后发生 `RemoteProtocolError`，终态为 `exit_code=1 / delivered=false / pending=true`。本次冷启动未完成正式交付，按失败终态封存。

案例：`D:/Sim-BIM-Agent/cases/lee-garden-six-full-dsh-sol61-cold-20261106`。

## 条件与边界

- `gpt-6.1-sol / high`，现有项目 ChatGPT 订阅授权，真实安装的 DeepSeek Harness；`runtime-v47-20261106 / domain-v173-20261106`。
- 原始 GLB、23/F 平面图、building 声明、任务和配置与前案哈希一致；新建案例、DSH_HOME 和会话，重新冻结当前 domain，没有复制历史候选或模型上下文。
- 首轮即启用 `--image-history-batches 3`；预算为 200 次模型请求、1200 次工具、256 个候选，无整案墙钟上限。
- 首个实际请求的模型、high、工具目录、任务、指引及无旧模型历史均通过独立核验。全部冻结输入、领域和实际启动记录的 DSH 代码哈希一致。
- 已封存失败终态，不执行恢复。后续若恢复，必须单独记录，不改变本次冷启动失败结论。

## 实际结果

| 项目 | 结果 |
|---|---|
| 正式启动至终态 | 38.93 分钟 |
| 上游请求等待累计 | 36.37 分钟 |
| 其他墙钟（工具、运行时等） | 2.56 分钟 |
| 启动 / 宿主 / 恢复 | 1 / 1 / 0 |
| 模型请求 | 22；21 次完整 usage，1 次未知 |
| 已知 token | 输入 2,311,371；输出 31,417 |
| 工具 | 54 次；48 完成、6 拒绝 |
| 构建及修改尝试 | 7 次；保存 1 个候选 |
| 首个已保存源模型 | 启动后 29.88 分钟 |
| 正式 finish | 0 次，无有效交付证明 |

第 22 请求只保存 `response.created`、`response.in_progress`、`response.output_item.added` 三条 SSE，没有终帧或完整输出项，用量为未知而非零。失败发生在已有候选后的观察、量测阶段，不推断模型尚未输出的计划。此前的几何拒绝与本次传输失败分别记账，不把所有耗时归为模型返工。

22 次图片投影均可精确还原，45 份图片身份哈希一致。这证明归档与回原的记录完整，不证明外部传输已稳定。头部日志中长工具错误会截断展示，但后续实际 Responses 请求包含完整拒绝详情及精确回读引用，未发现关键错误反馈丢给模型。

## 已保存阶段候选

`private/dsh/work/bim/candidate_01` 包含 151 个空间、257 个门和 90 个窗。独立检查 347 个主宿主及 602 个全部宿主绑定，共面和内含无错误。潜在门连接图可从外部到达 147/151 空间；未达为管井和屋顶排气井，且门状态为 unknown，不能据此证明实际通行或规范合格。

源数据覆盖两层商业、21 层办公和屋顶体量；34 个逻辑高度分组不能称为 34 层。顶部约 83.4 m，mesh 为 83.396 m，高度接近不等于形态准确。23/F 平面只直接支持该层，其他办公重复、商业内部及屋顶用途含明确推断，23/24 层口径冲突仍保留。

候选报告仍记录上部三层窗带/幕墙例外及上层商业外门转玻璃开口的待修改项。尚无最终 `mesh_review.json`，也没有 `finish_bim`，因此只交付阶段文件供查看，不标作建筑质量通过。

浏览器已核验候选打开、整栋显示、mesh 叠加、23F 切层、重置及空间点选；源平面图可见双翼办公区、三部电梯、两处楼梯和卫生间核心。视觉核验为定性观察，没有建筑精度评分。查看器的 save PNG 自动下载事件未取得确认；审计截图由浏览器捕获另存。

## 查阅与交接

- `evaluation/result/report.md`、`ledger.json`、`candidates.json`、`seal.json`：权威终态审计、耗时、用量及封账；516 个正式源文件哈希复核一致。
- `evaluation/cold-start-stop/`：失败终态、pending、首次启动、运行策略及宿主回执快照。
- `evaluation/independent-cold-start-audit.json`：独立首轮核验。
- `evaluation/candidate01-quality.json`：明确标为 `partial_saved_candidate / no_finish / not_final_delivery` 的阶段几何和证据检查。
- `evaluation/browser-qa.json`、`candidate01-browser.jpg`、`candidate01-overlay-browser.jpg`：浏览器核验和截图。
- `evaluation/trace/index.html`：全部模型、工具、输入和失败轨迹。时间轴使用其自身宿主事件时间基准，正式耗时采用上方账本口径。
- `evaluation/early-rejections.json`：前三次拒绝及实际错误反馈完整性的独立观察。

本轮不更新桌面产品安装，不上传输入、候选、授权或运行记录。

## 封账后的离线轨迹显示修订

浏览器核验发现，DSH 失败请求的宿主零用量占位值被直接展示，容易被误解为没有消耗。仅修订 `src/run_trace` 的读取和显示：有上游留档的失败或超时 DSH 请求以实际上游 usage 为准；缺失则明确显示“未知（未获得上游用量）”，宿主原始计数作为单独证据保留，上游明确报告的零仍保留为零。参数化回归覆盖未知、已知非零及明确零，三个轨迹测试模块 14 项通过，独立复核未发现阻断问题。本轮必须按完整 case 路径导出；仅导出未关联 wire 的独立 session 时，宿主计数仍可能直接显示，不宣称该路径已修复。

该模块不在 runtime/domain 注册范围，实际模型运行已在修订前结束并封账；本次改动没有修复或改善该次传输，不改写冷启动失败结论。初版导出保留为 `evaluation/trace-initial-001`，修订版使用 `evaluation/trace`，导出身份和截图见 `evaluation/offline-trace-revision.json` 与 `browser-qa.json`。
