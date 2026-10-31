# 2026-10-31 现状成品仓库交付

按用户授权建立独立成品 Git 仓库，沿用当前 Python 源码发行形态与 PNG 图纸、GLB＋辅助材料两个入口。本轮只做发布工程，不新增前端、模型策略、交互编辑或最终产品架构；后续统一多模态产品方向由开发主线继续决定。

## 成品身份

- 私有远端：`https://github.com/hortonyyx/Sim-BIM-Agent-Product`
- 本地：`D:/Sim-BIM-Agent/product-repo`
- 成品提交：`bc69f718088926664a07e373014372ed9e9e2ade`
- 成品 tag：`release-2026-10-31`
- 开发来源：`eb2836f5af3bdad3404a58b6e5794d95e4db2f83`
- runtime：`runtime-v43-20261025`；domain：`domain-v154-20261031`
- 565 个 payload 文件，27,035,061 bytes；加发布清单共 566 个 Git 跟踪文件。
- manifest SHA-256：`5e5ff602d91c738c357d02c3b77c4a7d55c31f6496fc632dfacf68d638eb8781`
- 已验收源码目录：`D:/Sim-BIM-Agent/releases/product-20261031-runtime43-domain154`
- 远端 tag 重新 clone 后导出的无 Git 安装目录：`D:/Sim-BIM-Agent/releases/product-20261031-install`
- ZIP：`D:/Sim-BIM-Agent/releases/Sim-BIM-Agent-release-2026-10-31.zip`，6,526,697 bytes，SHA-256 `ebb604c021ea66e105819074007f9aba56db274394d0f086419a9572155ce84e`。

## 改动与边界

新增 `scripts/release/sync_product_repo.py`，提供 plan/sync/check/export。同步只管理新旧 manifest 所属文件，移除退出清单的旧文件，保留 Git 与本地环境；拒绝 dirty、未知文件覆盖、额外跟踪文件及越界/Windows 大小写别名。正常异常还原所属文件；强制终止后的人工 Git 恢复边界写入发布说明。验证报告精确绑定发行目录和 manifest 哈希。同步工具不自动提交或推送。

新增产品专用 `.gitignore`、保发行字节的 `.gitattributes`；整理 README 与 [开发发布流程](../workflow/product_repository.md)。配置模板默认模型、推理档、worker 和预算保持原样，用户输入、凭据及运行结果仍在程序目录外。

未将开发报告、评测原件、GT 答案、实验/运行日志、缓存、凭据、测试与开发 Git 历史装入成品。16 个旧评测/GT 工具脚本已被当前不可变 domain 登记，保留其代码及现有共享依赖、RAG/legacy 兼容代码、EnergyPlus 资源和完整历史版本表；没有为了体积静默删掉注册成员。后续瘦身需由主线明确修改范围并登记新版本。

新检出发现旧 v153 的 `src/agent/runtime_delivery.py` 登记为 CRLF 字节，而已提交 Git 源码及当前 `.gitattributes` 是 LF。两者语义相同，仅 158 个换行差异。用现有流程登记 v154 的规范 LF 快照：`changed_lines=[domain]`、`changed_modes=[]`；旧 v153 历史记录不变，runtime 不变。集成主库时只把这一文件的同内容工作区字节规范为 LF，保证新的登记可核验。

## 验证

| 范围 | 结果 | 边界 |
|---|---|---|
| 成品入口/配置测试 | 8 通过，9.79 s | 0 真实模型 |
| 新成品 Git 发布测试 | 首轮 15 通过，26.02 s | 清单同步、删除、保护、普通异常还原、clone/export 字节 |
| Windows 路径独审修复复验 | 9 通过、8 未选，0.56 s | 含新增 `.GiT`、`.VENV` 边界；与首轮重叠，不相加 |
| 既有发布路径与桌面更新/回滚 | 56 通过，34.85 s | 现有专项测试；没有更新真实桌面 |
| 新 domain 登记与 verify | 通过 | 仅规范换行身份，四工具模式指纹无变化 |
| 成品自身独立环境隔离验收 | 通过，46.89 s | 按原锁文件建立 172 个依赖，未复制开发 `.venv` |
| 远端 tag 新 clone、清单核验、无 Git export、ZIP 回读 | 通过 | 565 payload 哈希与 566 个文件集合一致 |

隔离验收输出：`D:/Sim-BIM-Agent/cache/product-20261031-verification-001/report.json`。doctor=ready；合成 BIM、独立导入来源、两模板配置与 parser、图纸/mesh 原输入冻结哈希、工具目录、mesh 64×64 渲染通过（1,288 个命中像素）。真实本地 MCP inputs 执行一次；脚本模型共两个固定响应，中断恢复及已完成重复恢复通过；0 真实模型请求、0 计费模型请求。

未做真实模型整栋重建、图纸读图分工模型调用、建筑质量/性能新验收、浏览器交互或 EnergyPlus 仿真。现有桌面安装未更新。历史模型质量证据不因这次离线打包验收而升级为当前质量保证。

独立审阅发现 Windows 保护路径大小写问题，已改为 casefold 并复验；其余发布审计见协作记录。开发主库原有 roadmap 修改及未跟踪资料共 39 个文件按哈希保全，证据：`D:/Sim-BIM-Agent/cache/product-publication-main-preservation-20261031.json`。
