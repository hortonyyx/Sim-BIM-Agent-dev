# ChatGPT 订阅直连自有 Runtime

这条通道让 Sim BIM Agent 的自有 runtime 直接使用用户已经拥有的、符合资格的 ChatGPT 订阅。认证采用 OpenAI 的 Sign in with ChatGPT（SIWC）开放客户端流程，请求直接发往公开 Responses API。授权页应用名称为 **Sim BIM Agent**。运行不启动或依赖 Codex CLI、Codex app-server，也不读取、导入或复用 Codex 的登录 token。

provider 名称固定为 `chatgpt-subscription`。这条通道不读取 `OPENAI_API_KEY`，不创建 API key，也不会在订阅认证或额度失败时切换到按量计费 API、其他 provider 或其他模型。要使用按量计费通道，必须另行配置并明确选择，不能作为本通道的自动恢复路径。

## 首次登录和本地会话

从仓库根目录运行：

```powershell
.venv/Scripts/python.exe -m src.agent_runtime.openai_subscription login
.venv/Scripts/python.exe -m src.agent_runtime.openai_subscription status
.venv/Scripts/python.exe -m src.agent_runtime.openai_subscription models
.venv/Scripts/python.exe -m src.agent_runtime.openai_subscription logout
```

默认凭据目录是 `%LOCALAPPDATA%/Sim-BIM-Agent/openai-subscription`。需要隔离测试账号或运行目录时，`--directory` 必须放在子命令前：

```powershell
.venv/Scripts/python.exe -m src.agent_runtime.openai_subscription --directory D:/private/energyplus-chatgpt login
.venv/Scripts/python.exe -m src.agent_runtime.openai_subscription --directory D:/private/energyplus-chatgpt status
.venv/Scripts/python.exe -m src.agent_runtime.openai_subscription --directory D:/private/energyplus-chatgpt models
.venv/Scripts/python.exe -m src.agent_runtime.openai_subscription --directory D:/private/energyplus-chatgpt logout
```

`login` 先在 `127.0.0.1` 的动态端口启动 `/auth/callback`，再打开系统浏览器完成独立授权。它使用动态客户端注册、PKCE、state 和 nonce；收到 token 后校验 OpenAI JWKS 签名、issuer、audience、到期时间和 nonce。Windows 上 token 由当前用户的 DPAPI 保护，更新采用原子写入；刷新由跨进程锁串行化，并把旋转后的 refresh token 与 access token 一起替换。token 不进入仓库、命令行、运行日志或版本证据。

同一主机保留稳定的 `ext_agent_host_id`。再次登录默认复用当前账号已签发的 client ID；`login --new-account` 可注册另一个账号或 workspace，`status` 列出安全的账号标签，再用 `login --client-id <已保存的client_id>` 切换。`logout` 尝试撤销当前 refresh token，然后清除本地 token；若远端撤销未确认，命令会明确报告，不能把本地清除当成远端已成功。

2026-10-11，用户已在系统浏览器内完成本应用的独立授权。开发入口和桌面独立产品均已读取同一账号模型目录，具有 `chatgpt.tokens.use.direct` 权限；文档不记录账号标识或凭据。此次仅核验授权与目录，真实推理和工具循环未执行；`--live` 仍按用户批准的实测范围使用。详见[本次核验](../reports/2026-10-11_repository_subscription_auth.md)。

## 模型目录与资格边界

登录后先运行 `models`。它使用当前账号的 OAuth access token 请求 `GET https://api.openai.com/v1/models`，只显示 `visibility: "list"` 的模型，并保留服务端顺序。运行配置里的 `model` 必须使用这里实际返回的 `slug`；账号、workspace 或目录改变后要重新读取。模型名字出现在本地 profile 中，并不证明当前订阅账号有权使用它。

目前严格本地 profile 收录：

- `gpt-6-astra`
- `gpt-6.1-sol`
- `gpt-6-sol`
- `gpt-6-luna`

后两款于 2026-10-12 为 sm25 混合分工测试补齐，均保留独立型号，不作别名替换。官方 API 上下文与订阅部署边界分开；图片 token 暂沿已标注的未校准代理作本地预留，不能当供应商实报或硬上界。实际接入/整案状态见[本轮记录](../reports/2026-10-12_sm25_gpt_mix/README.md)。

模型必须同时满足“本地严格 profile 已收录”和“当前账号 `/v1/models` 实际列出”两个条件。缺一项就会在推理请求之前停止，不会换模型。

SIWC 的 ChatGPT plan usage 仍是 preview 能力。能否授权取决于账号、套餐、workspace 管理设置和地区等实际资格；可见模型及额度也由当前账号决定。订阅用量可能与 ChatGPT 及其他已授权应用共享，应用没有独立保证额度。额度耗尽、授权被撤销、模型不再可见或 preview 路由拒绝时，本次运行失败并保留证据，不自动使用 `OPENAI_API_KEY` 或付费 fallback。用量与权限应在 ChatGPT Settings 的 Usage 和应用访问设置中核对。

## 单模型与角色分工配置

单模型入口使用：

```json
{
  "mode": "single_model",
  "provider": "chatgpt-subscription",
  "model": "gpt-6.1-sol",
  "reasoning_history": "all",
  "reasoning_effort": "low",
  "output_tokens": 32000,
  "chatgpt_auth_dir": "D:/private/energyplus-chatgpt"
}
```

`chatgpt_auth_dir` 可省略；省略时使用默认凭据目录。它对应认证 CLI 的 `--directory`，运行入口会把它传为 `--chatgpt-auth-dir`。纯 ChatGPT 订阅配置不需要 `credentials_file`。不要为这条通道填写 API key 文件。

角色分工入口的固定角色是 `coordinator`、`plan_reader` 和 `elevation_reader`。每个角色都要显式配置 provider、model、reasoning effort 和本地输出预留，例如：

```json
{
  "mode": "role_division",
  "provider": "chatgpt-subscription",
  "model": "gpt-6.1-sol",
  "reasoning_history": "all",
  "reasoning_effort": "low",
  "output_tokens": 32000,
  "chatgpt_auth_dir": "D:/private/energyplus-chatgpt",
  "roles": {
    "coordinator": {
      "provider": "chatgpt-subscription",
      "model": "gpt-6.1-sol",
      "reasoning_effort": "low",
      "output_tokens": 32000
    },
    "plan_reader": {
      "provider": "chatgpt-subscription",
      "model": "gpt-6-astra",
      "reasoning_effort": "low",
      "output_tokens": 32000
    },
    "elevation_reader": {
      "provider": "chatgpt-subscription",
      "model": "gpt-6-astra",
      "reasoning_effort": "low",
      "output_tokens": 32000
    }
  }
}
```

顶层路由必须与 `roles.coordinator` 一致。纯 ChatGPT 角色组合不需要 `credentials_file`；混合其他 provider 时，其他 provider 仍按自己的凭据规则配置。启动时会逐个检查去重后的 ChatGPT 模型 slug 是否存在于当前账号目录。


完整 case 仍须提供输入、输出目录、scope、上下文和调用／工具／时间／token 等预算，并先走配置 `check` 和现有整案批准门。这里的片段只说明订阅路由字段，不代替完整运行配置。

## Runtime 请求与记账边界

SIWC preview 的 Responses 请求固定使用 `store: false` 和 `stream: true`。Runtime 每轮重发所需的完整输入和已保存的 opaque Responses output，不使用 HTTP `previous_response_id`。工具是本地 Runtime 执行的函数工具；模型不能取得本机 token 或任意外部工具权限。

当前 ChatGPT 订阅路由只允许 `reasoning_history: "all"`。`current_tool_chain` 会在发送请求前拒绝：Responses 返回的 reasoning、函数调用及其他 output item 可能是下一轮必须原样重放的 opaque 状态，不能沿用 Chat Completions 的历史裁剪方式删除。

`output_tokens: 32000` 是本地预算预留，用来做上下文和发送前预算判断。当前 preview 不接受把它作为 `max_output_tokens` 发到 wire，因此 32k 不是服务端输出上限，也不是已经观测到的最小输出；实际用量可能超过预留。ChatGPT 订阅路由强制采用 near-limit `stop`：预算不足时停止新请求，不临时缩小输出预留继续发送。当前本地订阅参数入口接受 low/medium/high/xhigh/max；具体模型仍须同时通过本地 profile 与账号目录检查。

每次请求保存实际 wire、来源、耗时和终态。服务返回 usage 时记录真实 input、output、total 和 cached token 字段；缺 usage 时保留 unknown，不填零。图片原件与实际发送内容分别留证，本地 profile 给出图片 token 估计；订阅响应的 input usage 已包含图片时不再重复加入 provider total。缓存命中与图片估算都不能冒充订阅账单或剩余额度。

若流中途报告过用量，但断流、超时或失败终态没有最终用量，已观测计数单独留证；最终总量仍为未知，预留继续保留，不把中途数字当最终结算放行。

## 两请求协议 smoke

smoke 脚本默认完全离线，`--out` 必须是尚不存在的新目录，`--model` 必须显式给出计划使用的 slug：

```powershell
.venv/Scripts/python.exe scripts/dev/smoke_chatgpt_runtime.py `
  --out AI_agent/logs/experiments/chatgpt-runtime-offline-01 `
  --model gpt-6.1-sol
```

离线路径使用固定 SSE fixture，不读取订阅 token，模型请求数为零。指定非默认认证目录时增加：

```powershell
--chatgpt-auth-dir D:/private/energyplus-chatgpt
```

只有用户完成本应用的独立 SIWC 登录并明确开始真实验证后，才加 `--live`：

```powershell
.venv/Scripts/python.exe scripts/dev/smoke_chatgpt_runtime.py `
  --out AI_agent/logs/experiments/chatgpt-runtime-live-01 `
  --model gpt-6.1-sol `
  --live
```

live 路径先核对账号模型目录，再通过 Runtime 发出最多 2 个模型请求，允许 1 次本地只读 `runtime_probe`，总时限 180 秒，模型重试为 0；不做模型、provider 或 API-key 回退。任一认证、目录、额度、流式协议或工具协议失败都在该 run 内结束，不自动换目录补跑。服务缺少 usage 时按上一节保留 unknown，而不是补零或据此重试。smoke 只验证订阅 Responses 与 Runtime 工具循环，不能替代图纸理解、BIM 几何或完整角色分工验收。

## 官方协议来源

- [Open-source token sharing overview](https://developers.openai.com/siwc/token-sharing-open-source)
- [Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Accounts and sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [Token reference](https://developers.openai.com/siwc/token-sharing-open-source/token-reference)
- [Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
- [Errors and recovery](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery)
