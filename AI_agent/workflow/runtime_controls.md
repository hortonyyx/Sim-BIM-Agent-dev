# 同次任务的控制与恢复

本入口供 dev model 或操作者调度现有 runtime。读图员的模型、上下文和预算沿用创建任务时的配置；恢复不重置起始时间，也不增加请求或费用额度。

## 定向消息、暂停和继续

先从运行目录读取任务 ID 与控制状态：

```powershell
.venv/Scripts/python.exe -m src.agent_runtime.control RUN_DIR status
.venv/Scripts/python.exe -m src.agent_runtime.control RUN_DIR pause --task TASK_ID --id pause-001
.venv/Scripts/python.exe -m src.agent_runtime.control RUN_DIR message --task TASK_ID --id review-001 --text "本层外轮廓保留东北凹入，先复核该处。"
.venv/Scripts/python.exe -m src.agent_runtime.control RUN_DIR resume --task TASK_ID --id resume-001
```

提交返回 `queued`；任务在安全边界接收后，状态变成 `applied` 并给出日志回执 ID。相同 ID 与相同内容可重复提交，内容不同会拒绝。消息按顺序进入目标任务的用户上下文，只有后续模型请求才能据此行动；已完成的工具操作不会被消息撤销。它不是新的模型会话，也不会替读图员编写几何结果。

暂停等待当前 HTTP 请求和完整工具交互结束，随后停止发出新请求。它不强杀在途请求，不冻结墙钟；超时仍会停止。父调度员正在等待整批委派时，其暂停要等该批交互完成；要在批次内部介入某个读图员，应定向该读图员的 task ID。重启保留已接收的暂停状态和消息，不重复注入。`applied` 表示运行时已接收，不表示模型已经执行了纠正要求。

任务完成后不接收新的控制。失败任务可先排入指令，再用原入口、原配置和原目录恢复；能否续跑仍由预算、状态一致性和工具恢复策略决定。

## 费用证据与未发送预留

无 durable request 的孤立预留可以证明未发出，恢复时会追加释放记录。已有请求而无 usage 的预留仍保留未知费用，不能填零。

有后到的真实用量凭据时，停止该 run 的写入进程后，使用补账入口：

```powershell
.venv/Scripts/python.exe -m src.agent_runtime.budget_recovery RUN_DIR reconcile --receipt RECEIPT_JSON
```

`RECEIPT_JSON` 含 `request_event_id`、`settlement_event_id` 和完整的替代 `settlement`；其中 `settlement.reservation_id` 指向原预留。CLI 将文件原文保存为证据 blob，再构造 `BudgetReconciliation` 合同。证据内容必须与这些引用及结算完全一致，人民币费用按原请求费率核验。历史未知结算保留，补账只追加一次；重复冲突、错请求、漏费用或覆盖原已知用量均拒绝。来源真实与否仍由操作者负责，runtime 能验证关联和计算一致性。CLI 受同一单写者锁约束，不能与在跑的任务并发改账。

## 工具恢复

- 明确声明可重取的纯读取，允许有界重取；旧 unknown 记录和后续执行都保留。
- 委派按保存的完整批次和原 task ID 恢复；已完成的读图员结果复用。
- 权限为 `read_only` 本身不构成重放授权；未知非幂等写入继续要求状态证据。

本轮接口验证与订阅协议小测不能替代 sm25 的图纸保真、空间划分和 BIM 交付验收。
