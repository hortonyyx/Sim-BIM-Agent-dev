# Harness 价值口径与对照基线讨论

日期：2026-10-26。本文记录用户与 Claude 就 harness 价值达成的口径、归因更正和待验证假设。Claude 作为外部讨论方，只读核对了仓库与外部证据。本轮没有产品代码改动、模型调用或发布，实施状态仍以[当前交接](2026-10-26_minimal_delivery_path.md)为准。讨论背景见[跨模型讨论文档](2026-10-26_harness_value_discussion_brief.md)。

## 1 用户澄清的价值口径

- Agent＝model＋harness，harness＝runtime＋domain，model 指 work model。当前不动 model（不做后训练），价值来自 harness。
- **更快**：同 model 下完整合格交付更快。“至少快一半”不是硬性门槛，只是价值的一个方面。
- **更好**：同配置下源 BIM 质量更高、推断更合理。
- **更省**：主要指让更弱的 model 也能做，哪怕只承担部分工作，允许多 Agent 调度。
  - 在不降低输出质量和效率的前提下，work model 中弱模型承担的比重越高越好。
  - 不设硬门槛，能做到哪一步取决于模型自身上限。
  - token 因缓存命中和价格不一，不作通用比较口径，照常分类记录。
- 街区仿真接线不是本线主要工作，用户说明已另行跑通。本线目标是产出合理的源 BIM。

## 2 以往对照的实际配置

按[术语](../project/terminology.md) 2026-10-26 补充的指代：

| 运行 | runtime | harness 构成 | 结果 |
|---|---|---|---|
| 10-24 早期对照（幸福中心，6.1 Sol/high） | Codex runtime | 通用 harness，加 BIM rules（文字：输出合同）和少量 methods（文字：任务优先级、案例提醒）；只有 90 分钟保护线 | 24:38 交付；未按 10-25 流程盲评 |
| 10-25 A（幸福中心、利苑，6.1 Sol/high） | Codex runtime | 通用 harness，加 BIM rules（文字）和 methods（文字：细度、停止、自检原则，600 秒目标） | 幸福中心 9:51 模型结束、9:55 收件器失败；利苑 10:48；盲评 0/2 |
| 10-25 B（同两案） | Codex runtime | 领域 harness：domain 的文字和代码部分（SDK、kernel、BIM rules（代码校验）），规定原生路径 | 10:58、20:10；盲评 0/2 |
| 10-26 局部四腿（6.1 Sol/high） | Codex runtime | A、B 构成同上 | A 2/2 完整交付；B 0/2 公共交付（发布权限缺陷、北向表达缺口） |
| 10-21/22 十案（幸福中心，5 种模型配置） | 自研 runtime | 领域 harness：domain 的文字和代码部分，经 MCP 工具 | 10/10 正式交付；用户评价可用及以上 6/10 |

十案的用户整案评价如下。版本为 runtime39/domain136 与 runtime40/domain137，与近期对照不是同一版本。

| 模型配置 | 两次整案评价 |
|---|---|
| Astra xhigh | 优秀、优秀 |
| Qwen medium | 优秀、可用 |
| Flash max | 可用、不可用 |
| Flash high | 不可用、可用（两次均为助手按用户原文归纳） |
| Kimi high | 不可用、不可用 |

## 3 归因更正与判断

- **以往通用组并非不含本项目 domain。** 各组都拿到了 BIM rules（文字），10-25 起还拿到了 methods（文字）。没拿到的是 kernel、tools（代码）和 roles。
  - “Codex 无本项目 harness 对照”这个旧标签说过头了。
  - 盲评仍然有效。生产侧的限制应称“隔离”，并写明各组拿到了哪些部分。
- **能说明的：** 对 6.1 Sol 这类强模型，BIM rules（文字）和 methods（文字）以文字形式给到即可生效。kernel、tools（模型调用的代码）在规定原生路径下没有增益；完全可选的情况尚未测试。
- **不能说明 harness 无用：** 通用组能交付，依赖的正是上述文字成果，而这些规则本身来自开发、实测和失败分析。
- **也还不能证明 harness 有用：** 文字层缺受控对照，代码层在弱模型上尚未测试。
- **methods（文字）的提速线索（单样本、非受控）：** 早期对照与 10-25 A 同为 6.1 Sol/high、幸福中心。后者多了 methods（文字）和时间目标，耗时从 24:38 降到 9:51。但后者盲评不通过，前者没有按同一流程盲评，所以不知道提速是否以质量为代价。
- **十案是正面线索：** 领域 harness 下，较弱或较便宜的模型也能交付。但缺同一批模型在通用 harness 下的对照，而且幸福中心已多次用于开发。

## 4 暂定对照口径

以下为讨论暂定，待用户最终确认。

- **基线**：通用 harness（Codex runtime）＋原始输入＋BIM rules（文字：输出合同）＋中性任务描述。
  - 输出合同作为任务定义，各组都给；methods（文字）、kernel、tools、roles 都算领域 harness。
  - 案例说明只写真实用户会提供的内容，不写入从同一案例失败中得来的经验。
- **结果指标**：
  - 合格交付率。
  - 每次合格交付时间：全部尝试耗时 ÷ 合格次数，失败的尝试也计入。
  - 质量底线：冻结 major 清单。
  - 分级分：沿用 v0.3 四大类，小项和阈值待冻结后实际算分。
  - 机器可读信息完整度，如北向。
- **更省**：
  - 按“模型 × 子任务”记录能否达到质量底线，形成能力门槛图，并以弱模型承担的工作比重作汇总，不设硬门槛。
  - 子任务可拆为读平面、读立面、按声明构造、按反馈修复、检查。图纸线现有的平面、立面读图员就是现成的划分。
  - token 分类记录，不做通用折算。
- **过程指标只用于归因**：回合数、首源时间、文档读取量、调试回合、退回通用路径次数。
- **测试集要留出**：两道局部题、幸福中心和利苑都已多次用于开发，最终的价值结论需要留出案例。

## 5 候选验证（均未启动）

1. **补评早期对照**：用 10-25 盲评流程补评 10-24 早期对照的产物，不调用工作模型。目的是判断 methods（文字）的提速是否以质量为代价。
2. **强模型受控对照**：基线、基线＋methods（文字）、再加完全可选的 kernel/tools 三组。第三组保留通用出口，交付义务与前两组等价。每组 3–5 次，交错运行。
3. **弱模型对照**：
   - 十案中的模型在通用 harness 下补对照，并按子任务测能力门槛。
   - 较弱模型需要能接入各家线路的 runtime。自研 runtime 已接通 Paratera、GLM、OpenCode Go 等线路，但要先处理已知的缓存命中问题。
4. **可用性校核零模型回放**：对成品源 BIM 计算门净宽、按通行宽度收缩后的可达性和瓶颈位置。用已有 8 份产物回放，看能否检出已知 major、在合格稿上有没有误报。

## 6 输入线与发布现状

- **图纸线**：分工模式（调度员、平面/立面读图员），迭代至约 domain113 后暂缓。
- **textured mesh 线**：单模型加可选的同模型只读 worker，10-19 起迭代至 domain146。
  - domain147–153 增加了薄 Python SDK，在 Codex runtime 上用于 A/B 对照。图纸局部题和 mesh 整案都走它。
- **两条线的共用与分开**：共用源 BIM、kernel、查看器和自研 runtime；roles、methods（文字）和部分 tools 分开。
- **桌面发布版**：release-manifest 为 runtime-v24-20261017 / domain-v110-20261018（`b72ea6c7`），只含图纸线和基础 GLB 接收，不含 10-19 以后的 mesh 方法、工具和 SDK。开发主库为 runtime43/domain153。

## 7 线路与通用 harness 连通检查

用户随后确认当前聚焦 mesh 线（最终仍是统一的 Agent 产品），并要求检查可作 work model 的线路和已安装的通用 harness。本节只做连通性检查，没有建模运行。共发出 9 次极小模型请求：3 次计 DeepSeek API（用户本次明确要求检查），其余计订阅或 Paratera、Go 的小额用量。

| 项目 | 结论 | 依据 |
|---|---|---|
| Codex CLI（Codex 应用自带 0.164.0-alpha.11.1，即近期基准所用的 codex.exe） | 可用 | ChatGPT 登录；`codex doctor` 的认证、websocket 与可达性检查通过；gpt-6.1-sol 与 gpt-6-luna 各一次极小任务，约 11 秒返回 |
| Sim BIM Agent 自身的订阅授权（OAuth 应用 `oaiapp_…`） | 可用 | `auth status` 显示已登录；订阅目录 7 个模型（gpt-6.1-sol、gpt-6-astra、gpt-6-sol、gpt-6-luna、gpt-5.6-sol、gpt-5.6-terra、gpt-5.6-luna）；`smoke_chatgpt_runtime.py --live` 2 次请求通过，计订阅，无付费回退 |
| DeepSeek API | 可用 | 目录 200（deepseek-flash、deepseek-v4-pro）；deepseek-flash 极小请求 200 |
| Paratera API | 可用 | 目录 200（82 个模型）；GLM-5.3-Flash 极小请求 200 |
| OpenCode Go | 可用 | 目录 200（38 个模型）；glm-5.3-flash 请求 200，计 Go 额度。32 token 上限全部用于思考、没有正文，是探针设置所致 |
| DeepSeek Harness（dsh 0.2.1-rc.2，桌面 44.7.0，`D:\Program_AI\deepseek harness`） | 可用，需在启动环境提供密钥 | `dsh --profile headless` 读不到桌面应用保存的密钥（MISSING_CREDENTIAL）；启动时导出 `DEEPSEEK_API_KEY` 后极小任务成功，默认模型 deepseek-flash |
| OpenCode CLI 1.18.32 | 部分可用 | 经 DeepSeek API 的极小任务成功，单轮约 4.06 万输入 token；授权里只有 Xiaomi 与 DeepSeek，没有 Go 订阅；`opencode models opencode-go` 仍报 `no such column: project_id` |
| Claude 会话内的 codex MCP | 不可用 | 配置调用 `codex mcp-server`，两个 codex 版本都已没有这个子命令；跑测试不依赖它 |

对后续对照的影响：

- GPT 系列可以在同一个 Codex runtime 下比较强弱模型（6.1 Sol 对 6-Luna），两组都走订阅。
- dsh 会读取当前目录的 `.env`。在主库根目录启动会触及其中全部密钥，本次只是因为 `DEEPSEEK_BASE_URL` 被拒才停下。实验工作区不能放 `.env`，密钥只经启动环境传入。
- 要用 OpenCode 跑 Go 模型，需要先由用户添加 Go 凭据，并处理上述本地库错误。

本轮只整理文档和做连通性检查；没有产品代码改动、建模运行、新实验或桌面发布。
