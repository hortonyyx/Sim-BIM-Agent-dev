# DSH 交付闭环与断流恢复修复

用户要求：只有有证据的外部断连等故障，才允许以显式错误结束而不交付；正常路径应持续到正式交付。此修复针对 GPT-6.1 Sol / high + DeepSeek Harness + Sim-BIM domain，不改变建筑规则或降低交付检查。

## 原问题与修复

原冷启动第 22 请求在 HTTP 200、部分 SSE 后发生 `RemoteProtocolError`。当时 launcher 只允许连接发送前的安全重试，部分流后直接退出；另外，原生宿主返回 0、纯文字结束均不等于 `finish_bim` 成功。旧冷启动仍保持失败封存，详见[冷启动记录](2026-11-06_dsh_sol61_cold_start.md)。

- 模型请求中断后，先核验该请求前的 BIM 文件快照和原生工具 journal。只有工具回执闭合、无未知写入、未取消、文件未变时，才在原 session 继续原请求。原始输入和不透明推理项都校验摘要，已执行 BIM 工具不重放。
- 连续外部请求失败最多自动重试 3 次，间隔 1、2、4 秒；有成功模型响应则重新计算连续故障。耗尽后明确记录外部失败，保留未知用量，不伪造交付。
- 纯文字提前结束自动发起交付续跑，要求模型继续检查、修复并调用 `finish_bim`。输出超长或空响应要求更小的工具动作，半截工具不执行。
- 有效 completed 已传给宿主后，流尾断连或关闭异常仅记诊断，不回滚为 pending；HTTP 503/429 的异常 JSON 格式也保留状态分类。
- 鉴权失败单独按异常类型和 HTTP 证据分类，明确标明模型请求尚未提交。整个 `get_access_token` 不重试：单次轮换 token 可能已成功更新于服务端，而失败发生在后续验证，盲目重放可能破坏登录状态。证实的鉴权断连明确报外部错误，本地凭据问题不冒充外部故障。
- 自动恢复和显式 `--resume` 共用状态验真。宿主没有消费续跑、没有发出新请求时，明确记内部故障，避免无进展重启循环。
- 成功必须由真实原生工具记录中的 `finish_bim` 和当前交付文件哈希共同证明；还验证整栋 mesh review、最低楼层约束、无后续未确认写入。保存了候选、旧 proof 或 exit 0 都不能替代这些条件。

终态包括 `delivered`、`failure.category/code`、`allowed_external_failure`、原生退出码、请求数与恢复次数。内部异常、预算耗尽、未知写入一律不能标成外部豁免；这些终态仍是需要修复的未完成运行，不是达成交付要求。没有无限重试、自动放宽预算或伪造成功。

## 验证边界

普通离线 `fast` **825 passed**，证据 `D:/Sim-BIM-Agent/cache/pytest/a8a730999951/results.xml`；最终 DSH 与 trace 九模块针对性检查 **176 passed**，证据 `D:/Sim-BIM-Agent/cache/pytest/4f0da781f12b/results.xml`。全部经 `no_billed_gate`，未调用真实模型。实现完成后独立审查并复核修复，当前无剩余 P1/P2。

离线单测覆盖恢复不重放、拒绝变化快照、跨进程 resume 先验真、外部故障耗尽、早停续跑、真实 finish 证明、未知用量与日志导出。真实安装 DSH 配合 localhost 脚本上游验证原生 session/MCP 契约；脚本硬禁真实凭据与外网，使用历史完成案例的临时副本作为工具 fixture，未改原案例。

最终固定源码的单进程五场景全部通过，证据 `D:/Sim-BIM-Agent/cache/offline-dsh-delivery-fl_wim3h/verification.json`：

| 场景 | 本地上游请求 | 原生 host attempts | 结果 |
| --- | ---: | ---: | --- |
| 纯文字提前结束 | 3 | 2 | 同 session 继续，真实交付 |
| 部分 SSE 后断连 | 3 | 2 | 原请求恢复，半截工具未执行 |
| EOF 缺少 terminal | 3 | 2 | 原请求恢复，真实交付 |
| max_output_tokens 截断 | 3 | 2 | 更小动作约束续跑，真实交付 |
| 完整 completed 后断连 | 2 | 1 | 不重试已完成响应，直接交付 |

各场景真实 `inputs`、`finish_bim` 均各一次，7 个交付文件哈希均为当前值，源几何未变。14 个 DSH 源文件导入前/结束后哈希一致，每场景 launch 源哈希也相同；历史 fixture 的 335 个文件全量哈希未变。真实 provider 调用 **0**。中途版本行为检查留存于其他 cache，最终验收只使用上述固定源码目录。smoke 脚本亦完成独立只读审查。

这些检查验证恢复机制和交付验真，不构成新一轮真实模型冷启动成功，也不证明建筑质量或外部服务稳定性。没有将旧失败案例续跑后改写成冷启动成功。

旧冷启动 seal 中 **516** 个 source 文件哈希重新核验，均未变化。用户原有 `README.md`、`AI_agent/design/architecture.md`、`AI_agent/project/roadmap.md` 未提交修改与冷启动前留存哈希一致，未纳入本轮修改。

DSH adapter 使用独立的 `dsh-delivery-loop-v1` policy，并在每次 launch 保存源码哈希。domain-v173 与原生 runtime-v47 没有改动；两者现有版本登记范围不含 `src/dsh_runtime`。

复现入口：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 scripts/dev/check.py fast
.\.venv\Scripts\python.exe -B -X utf8 scripts/dev/check.py targeted tests/test_dsh_runtime.py tests/test_dsh_completion.py tests/test_dsh_credentials.py tests/test_dsh_lifecycle_evidence.py tests/test_dsh_transport_diagnostics.py tests/test_dsh_lifecycle.py tests/test_run_trace_wire.py tests/test_run_trace_readers.py tests/test_run_trace_export.py
.\.venv\Scripts\python.exe -B -X utf8 scripts/dev/smoke_dsh_delivery.py
```
