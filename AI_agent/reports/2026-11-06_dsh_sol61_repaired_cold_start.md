# 利苑整栋：修复版 Sol 6.1 + DeepSeek Harness 真实冷启动

2026-11-06，按用户要求使用已修复的交付闭环，从原始输入重新启动 `gpt-6.1-sol / high + DeepSeek Harness + Sim-BIM domain`。一次启动、一次宿主执行，在 31.04 分钟后真实调用 `finish_bim` 并交付 candidate_03。终态 `delivered=true / exit_code=0 / pending=false`，独立交付证明和文件哈希检查通过；零自动恢复、零交付续跑，本轮操作及审计记录中无人工介入。

新案例：`D:/Sim-BIM-Agent/cases/lee-garden-six-full-dsh-sol61-recovery-cold-20261106-2253`。下文案例内路径均以此为根。

## 冷启动条件

- 运行源码提交：`dcfbd9fa5eea5c6d0fb26d5881161ff86da187af`。`domain-v173-20261106 / runtime-v47-20261106`；DSH 适配层修订单列为 `dsh-delivery-loop-v1`，14 个实际运行文件以启动记录哈希为准。
- 使用本机真实安装的 `D:/Program_AI/deepseek harness/DeepSeek Harness.exe` 和现有项目订阅授权；新建案例、DSH_HOME 和会话 `session-ec8655d6-c34f-4cf2-bf3a-8651558b7f7e`。
- 只从原失败案例复制 config、task、building.glb、building.json、23f.png 五份原始配置/输入；无旧候选、冻结目录、模型输出、会话或历史上下文。重新冻结当前 domain。
- 首个实际上游请求为 Sol 6.1 / high，仅 developer 与 user 两项输入，无历史 assistant 或 function output；45 个工具（44 个 domain 工具和图片读取）。独立首轮审计通过。
- 从首轮启用 `--image-history-batches 3`；上限为 200 次模型请求、1200 次工具、256 个候选，无整案墙钟上限。全过程未人工修改运行源码、领域配置或模型工件，未重启、恢复或追加人工提示。
- 原失败案例 `lee-garden-six-full-dsh-sol61-cold-20261106` 保持封存，本次成功不改写原失败结论。

运行命令：

```powershell
.venv/Scripts/python.exe -B -X utf8 -m src.dsh_runtime --case D:/Sim-BIM-Agent/cases/lee-garden-six-full-dsh-sol61-recovery-cold-20261106-2253 --prepare
.venv/Scripts/python.exe -u -B -X utf8 -m src.dsh_runtime --case D:/Sim-BIM-Agent/cases/lee-garden-six-full-dsh-sol61-recovery-cold-20261106-2253 --image-history-batches 3
```

## 完成、效率与稳定性

| 项目 | 结果 |
|---|---|
| 正式启动至终态 | 31.04 分钟 |
| 首个已保存源模型 | 启动后 18.44 分钟 |
| 上游请求等待累计 | 27.88 分钟 |
| 其他墙钟（工具、运行时等） | 3.17 分钟 |
| 启动 / 宿主 / 显式恢复 | 1 / 1 / 0 |
| 自动请求恢复 / 交付续跑 | 0 / 0 |
| 模型请求 | 28 次，28 次完整 usage，未知 0 次 |
| Token | 输入 4,115,503；输出 57,506；合计 4,173,009 |
| 缓存输入 | 133,376，占输入 3.24%（输入的子项） |
| 推理输出 | 26,924（输出的子项，不重复相加） |
| 工具调用 | 67 次：59 完成、8 错误 |
| 构建/修改 | 10 次：7 次拒绝、3 个成功候选 |
| 正式交付 | 1 次真实 finish，选择 C03 |
| HTTP / 传输失败 | 0 次 |

8 次工具错误为 7 次构建/修改拒绝和 1 次报告指针读取错误，模型在同一次运行内继续处理并完成交付。它们不属于外部断连。用量以 28 个实际响应的完整 usage 记账；缓存和推理细项不再加入总量。

本次验证了修复版本在真实冷启动中的完整交付路径，但没有发生断流，因此没有实跑触发断流恢复。恢复机制的故障注入证据仍来自[修复记录](2026-11-06_dsh_delivery_recovery.md)中的离线及真实 DSH/localhost 检查。单次成功不构成稳定性统计，也不构成模型或 harness 的公平排名。

## 交付与独立检查

- `private/dsh/work/bim/delivery.html`、`delivery.json`：正式交接。
- `private/dsh/work/bim/candidate_03/viewer.html`、`source_model.json`：最终查看器和源 BIM。
- 独立终态检查重验真实 finish；7 份交付文件及 26 份 mesh review 证据哈希一致。检查前后受保护工件未变化。
- C03 含 161 个空间、1,438 个边界、246 个门、90 个窗。336 个主宿主和 579 个全部宿主绑定，共面及内含检查无错误；最大共面偏差约 `4.19e-10 m`。
- 内容包括两层商业（G/P1）、21 层办公（O01–O21）及五个屋顶部件。35 个逻辑构造组不等于 35 层；模型有 24 个不同的基底标高。
- 围合体最高 80.8 m，输入 mesh 最高 83.396 m，相差 2.596 m；高处冷却、通风细部和栏杆被明确省略，不能称为精确复原。潜在门图可达 156/161 空间，剩余为两处井道及三个屋顶部件；门状态未知，不能证明实际可通行或规范合格。

有效 finish、几何自洽和证据引用不等于建筑真实性评分。23/F 平面对其他楼层的推广、内部功能与屋顶简化等仍含推断，本次没有独立建筑精度评分。

浏览器已验证交接页、整栋模型、真实 GLB 叠加、O21 切层、空间点选、重置和轨迹中的 finish 事件。所测功能可用。控制台捕获一条无来源 URL 的 `MutationObserver.observe` TypeError，未观察到相应功能故障；来源未定，不能宣称浏览器零错误。未改写交付查看器或任何模型工件。

## 审计入口

- `evaluation/cold-start-provenance.json`、`independent-cold-start-audit.json`：输入、源码、首次请求与冷启动证据。
- `evaluation/result/report.md`、`ledger.json`、`candidates.json`、`seal.json`：正式封账；833 份正式源文件的哈希清单。
- `evaluation/independent-terminal-audit.json`、`independent-quality.json`：交付证明、几何、覆盖范围和质量边界。
- `evaluation/trace/index.html`：完整标准轨迹。轨迹自首个宿主事件计时，正式耗时采用上方启动至终态口径。
- `evaluation/browser-qa.json`、`candidate03-browser.jpg`、`candidate03-overlay-browser.jpg`、`candidate03-o21-browser.jpg`、`trace-finish-browser.jpg`：浏览器核验与截图。
- `evaluation/final-integrity-check.json`：浏览器与轨迹导出后的封账文件、运行源码和用户既有修改完整性复核。

本轮仅提交本报告和项目入口更新；输入、模型、凭据与运行记录留在案例目录，桌面产品安装未更新。
