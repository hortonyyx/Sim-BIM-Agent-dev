# 2026-10-11 仓库改名与 ChatGPT 订阅授权

> 后续命名说明（2026-10-31）：本页记录当时的开发仓库名称。该仓库现位于 [Sim-BIM-Agent-dev](https://github.com/hortonyyx/Sim-BIM-Agent-dev)，原 `Sim-BIM-Agent` 名称现交给成品仓库；查看本页所述源码提交时应使用开发库的新地址。

用户要求将 GitHub 仓库改名为 `Sim-BIM-Agent`，并允许启动此前暂缓的 ChatGPT 订阅授权。本轮完成仓库改名、独立账号授权、开发入口与桌面产品的模型目录核验。程序未修改，runtime/domain 版本不变。

## GitHub 与本地仓库

- 主库：[hortonyyx/Sim-BIM-Agent](https://github.com/hortonyyx/Sim-BIM-Agent)。GitHub 仓库 ID 仍为 `1216622598`，沿用原仓库与历史。
- 开发目录 `D:/Sim-BIM-Agent/repo`，`origin` 的 fetch/push 地址均为 `https://github.com/hortonyyx/Sim-BIM-Agent.git`。
- 改名后通过 GitHub API 回读仓库名称、ID、默认分支和权限；`git ls-remote --symref origin HEAD refs/heads/main` 成功。
- 改名前后核验基准均为 `f56a0e0d6132ef01c7e0eda01c2e733af61f28b4`，本地 main、origin/main 与远程 main 一致。仅一个活动 worktree。

## 授权与核验

授权由用户在系统浏览器完成，应用名为 **Sim BIM Agent**。登录返回成功，包含 `chatgpt.tokens.use.direct`、`resource.invoke`、`offline_access` 及身份 scopes。凭据使用项目原有 Windows DPAPI 存储；不保存邮箱、client ID 或 token 到项目记录，不读取 Codex 登录凭据。

| 检查 | 结果 | 耗时 | 模型推理请求 |
|---|---|---|---|
| 开发入口独立 SIWC 登录 | 通过，1 个已授权账号 | 用户交互，未单计 | 0 |
| 开发入口 `openai_subscription models` | 通过，7 个模型 | 命令约 3.98 秒 | 0 |
| 桌面产品 `Start.cmd auth status` | 通过，已登录且含订阅权限 | 2.112 秒 | 0 |
| 桌面产品 `Start.cmd auth models` | 通过，与开发入口目录一致 | 4.690 秒 | 0 |

两次模型目录读取均通过本应用 OAuth 访问 `GET https://api.openai.com/v1/models`，服务器返回顺序为：`gpt-6.1-sol`、`gpt-6-astra`、`gpt-6-sol`、`gpt-6-luna`、`gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`。当前 runtime 的严格 profile 已收录前两款；其余出现在账号目录不代表已完成本地接入。没有修改默认运行配置。

## 验证边界

- 本轮只完成账号授权、真实目录访问与桌面产品授权识别。没有执行 `smoke_chatgpt_runtime.py --live`、Responses 推理、工具循环或 BIM 案例。
- 产品模型请求、付费 API 请求、GLM/Paratera/DeepSeek 请求均为 0；未查询剩余订阅额度，未用模型目录读取代替推理验收。
- GitHub 名称变更与当前说明更新不改变 runtime/domain 程序，也不需要重建桌面产品。文档变更以 diff 和新增本地链接检查验证，不重复全量测试。
- 原迁移交接仍见[项目精简与独立发布](2026-10-11_product_slim.md)，其历史验证结论不追改。

协议依据：[OpenAI Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in) 与 [Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)。
