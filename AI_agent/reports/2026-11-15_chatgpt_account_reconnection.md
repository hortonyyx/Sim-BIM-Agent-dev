# ChatGPT 新账号接入主线 Agent 验收

2026-11-15。用户明确暂停 Flash 分析，优先补齐 GPT 订阅接入，并说明已更换登录账号。按新账号独立授权处理，没有恢复或导入旧 EnergyPlus-Agent 注册，也没有读取 Codex 的登录凭据。

## 完成结果

- 主线产品 CLI 支持 `auth login --new-account`、`auth login --client-id ...` 和显式 `--directory`；账号选项互斥且只允许用于 login。
- 用户在官方浏览器流程中完成 Sim-BIM Agent 的独立授权。新注册保存到实际用户 AppData 下的默认 `Sim-BIM-Agent/openai-subscription` 目录，使用现有 DPAPI 保护。
- 在线模型目录读取成功，包括 `gpt-6.1-sol`、`gpt-6-astra` 等。目录可见不代表每个模型都已通过本地 profile 或真实推理验证。
- 使用 `gpt-6.1-sol` 完成主线 `resolve_connection` → Responses adapter → Runtime → 本地工具 → 最终回答的真实验证。
- 实跑后，不指定目录的正常产品入口 `sim_bim.py auth status` 与本次授权账号一致。Codex 沙箱的 LOCALAPPDATA 重定向不作为真实用户目录的判断依据；本次在线登录和验证明确使用实际用户目录。

没有在文档、测试或版本注册表中保存 token、邮箱、client ID 或 host ID。旧账号凭据保持原样。

## 真实验证

使用现有 `scripts/dev/smoke_chatgpt_runtime.py`，未另造认证或请求路径。上限为两次模型请求、一次本地只读工具、180 秒、0 模型重试。

| 项目 | 实际结果 |
|---|---|
| 模型 | gpt-6.1-sol，low |
| 路由 | chatgpt-subscription / openai_responses |
| 完成状态 | completed / passed=true |
| 模型请求 | 2 |
| 本地工具 | runtime_probe，1 次 |
| 最终回答 | `{"value":7,"source":"local_runtime"}` |
| 用量 | 199 tokens：171 input、28 output，完整实报 |
| Runtime 耗时 | 7.672 秒，不含浏览器授权及模型目录读取 |
| 按量 API 请求 / fallback | 0 / false |
| 输出截断 | 0 |

证据：[smoke_result.json](D:/Sim-BIM-Agent/repo/AI_agent/logs/experiments/chatgpt-new-account-20261115-live-01/smoke_result.json)、[receipt.json](D:/Sim-BIM-Agent/repo/AI_agent/logs/experiments/chatgpt-new-account-20261115-live-01/receipt.json)、[versions.json](D:/Sim-BIM-Agent/repo/AI_agent/logs/experiments/chatgpt-new-account-20261115-live-01/versions.json)。日志保持本地。本次验证证明新账号订阅及工具循环可用，不新增建筑质量或完整建模任务结论；没有重新运行 Flash 或 BIM 整案。

## 实现与离线检查

- `src/agent/product_cli.py`：透传目录及登录账号选择参数；拒绝其他认证动作误带账号选择参数。
- `tests/test_product_auth_cli.py`：22 项测试经过产品入口和真实认证 CLI 调度，仅替换凭据对象；验证原有动作、显式目录、换账号、重授权、非法选项和错误退出码。
- `tests/test_runtime_openai_subscription.py`：扩展动态注册测试，验证新账号不带旧身份 hint、完成后切 active、保留旧注册和稳定 host。
- `tests/check_groups.json`：登记新测试；接入文档补充换账号用法。
- 最终针对产品认证入口、OAuth 和产品发布契约的组合检查 **46 passed**；前置订阅协议/profile 检查也通过。`git diff --check` 和真实 `sim_bim.py doctor` 通过。

按现有注册机制新增 `domain-v174-20261115`，`changed_modes=[]`；runtime 仍是 `runtime-v47-20261106`。变更只涉及产品登录入口，没有改变建筑提示、BIM 工具目录或几何规则。版本记录追加，不改写历史记录。

本次在开发主线验收，未重建或发布桌面安装包。后续主线配置选择 `provider: chatgpt-subscription` 时，正常 Windows 用户环境默认读取本次授权；显式设置 `chatgpt_auth_dir` 的历史配置仍以其指定目录为准。

## 后续换账号

开发主线可运行：

```powershell
.\.venv\Scripts\python.exe sim_bim.py auth login --new-account
.\.venv\Scripts\python.exe sim_bim.py auth status
.\.venv\Scripts\python.exe sim_bim.py auth models
```

完整说明见 [订阅接入工作流](D:/Sim-BIM-Agent/repo/AI_agent/workflow/chatgpt_subscription_runtime.md)。ChatGPT/Codex 自身切换账号不会替换本应用的独立注册；普通 login 用于当前保存账号重授权，换账号使用 new-account。
