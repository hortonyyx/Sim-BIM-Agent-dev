# OpenCode Go 接入

Go 使用独立 provider `opencode-go`。凭据文件提供 `OPENCODE_GO_BASE_URL=https://opencode.ai/inference/go/v1` 和 `OPENCODE_GO_API_KEY`，可选 `OPENCODE_GO_DNS_BOOTSTRAP_IPV4`。本机凭据位于 `D:/Sim-BIM-Agent/local-connections/opencode-go/credentials.env`，不进入 Git 或发布包。

## 模型与协议

| 精确型号 | 协议入口 |
|---|---|
| `deepseek-v4.1-flash`、`glm-5.3-flash`、`kimi-k3`、`mimo-v2.6-flash` | `/inference/go/openai/v1/chat/completions` |
| `minimax-m3`、`qwen3.8-max`、`claude-haiku-5-5` | `/inference/go/anthropic/v1/messages` |
| `gpt-6-luna` | `/inference/go/openai/v1/responses` |

协议与精确 ID 以[官方 Go 文档](https://opencode.ai/v2/docs/console/go/)和当前服务目录共同核对。2026-11-01 已只读确认 `deepseek-v4.1-flash` 出现在目录；正式运行结果另行记录。示例见[Flash 图纸配置](../../product/config.opencode-go.example.json)。

Flash 使用 `thinking` 与 `reasoning_effort`，带工具时保留完整 `reasoning_content`；公开合同参考[DeepSeek 思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)。Go、Paratera和官方路径分别保存实际服务身份。图片与文本预算估算标明未校准范围，不伪造价格或零费用结论。

## 网络与会话

Go 在 Windows 使用已连接物理 IPv4 出口。DNS 和推理使用同一物理接口，保留域名与 TLS 验证，不改系统代理或其他 provider 的网络路径。无可用出口时明确报错，不偷偷切换代理。

请求带产品自身 User-Agent 和稳定的 `x-opencode-session`。会话 ID 由运行与任务生成，不发送本机路径。角色可以使用不同的已支持协议，根预算与恢复身份保持统一。

## 使用与验收

单模型和图纸分工均可配置 Go。图纸分工需有限时间预算，顶层与调度员路由一致；所有凭据由所选私有文件提供。

本次验收优先使用 Go Flash；若服务拒绝、不可用或无法完成协议闭环，保存原因，再按用户授权切换 Paratera、官方 API。不会更改账号隐私或地区设置来绕过限制。过往实验记录保存在 `AI_agent/reports`，不当作当前版本已通过的证明。
