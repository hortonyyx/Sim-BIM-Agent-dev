# 2026-10-31 成品公开与用户说明

按用户明确要求，将成品仓库公开，并把产品 README 重写为面向使用者的说明。仓库名称仍为 Sim-BIM Agent，开发库仍为 Sim-BIM Agent dev；没有前端、架构、模型策略或功能改动。

- 公开成品：https://github.com/hortonyyx/Sim-BIM-Agent
- 仓库 ID `1440941560` / node ID `R_kgDOVeMF-A` 保持；GitHub API 已确认 `private=false`、`visibility=public`。
- 开发固定来源：`edd9ddaf495132f8055633c3150d9714cafcd945`，已合入开发 main。
- 成品提交：`9fc0f4c11bbbceea1fbe0a5affb6dc34a561d247`。
- [Release](https://github.com/hortonyyx/Sim-BIM-Agent/releases/tag/release-2026-10-31.3)：`release-2026-10-31.3`，最新 ZIP 1,694,417 bytes。
- ZIP SHA-256：`8306b151e445ab8a0750f8dde7ec38860a5a824ff823deb71f7b8393bfa2c055`，GitHub asset digest 相同。
- Manifest SHA-256：`47ef7243a4c532d1d1b293c50d431127ab558808031e9790d6e96efe942412cd`。

README 现在依次介绍产品作用、PNG/GLB 输入、下载与安装、本地示例、资料目录、配置字段、DeepSeek Flash 网格示例、默认账号配置、运行与恢复、JSON/HTML 结果、当前范围、更新和反馈。删除了产品 README 中的开发库关系、内部版本登记、无 Git 验收、发布/同步脚本、内部测试报告和历史凭据目录等开发事务；维护流程继续留在开发库 workflow 文档。Release 说明也仅介绍本次用户可见变化。

本包仍为 275 payload 文件 / 5,857,356 bytes，外加 manifest。相对 .2 只有 README 和自动生成的版本来源记录改变，其他 273 文件逐字节保持，包括全部程序与依赖。源 runtime44/domain156 未变；生成产品身份为 `runtime-product-v1-00b8e71ddba394713b000f87` / `domain-product-v1-a9d1344c28ef22180a20ce74`。没有为文档更新补跑模型或历史测试全集。

| 范围 | 结果 | 边界 |
|---|---|---|
| README 事实与步骤独审 | 两模板及 DeepSeek 局部覆盖实际离线 check 均 ready，示例 JSON/链接通过 | 0 模型；不代表账号连通或建筑质量 |
| 新包自身环境 | 全新 `.venv` 安装42包，doctor ready | 与上一版锁完全相同 |
| 正式离线验收 | 通过，26.734 s，0 live model requests | BIM/HTML、两路输入冻结与工具、mesh渲染、本地MCP中断/恢复 |
| 远端 tag clone/export/ZIP | 276 文件集合及字节完全一致 | GitHub 上传 asset digest 一致 |
| 公开前敏感内容快审 | 当前276 tracked、此前全部3提交/578 blobs及 .1/.2 Release 附件无确切阻断项 | 静态特征审查，不宣称穷尽所有秘密；保留历史，没有重写 |
| 公开状态 | ID/node ID、main、README blob一致；visibility=public | 先推新README和新Release，再公开 |

历史快审中的 credential 字段为空或环境模板，路径命中为示例/通用 denylist；未见实际凭据、私钥或用户真实输入/回答。新 README 的密钥为明确占位符。公开按用户授权进行，没有追加确认。

证据位于 `D:/Sim-BIM-Agent/cache/public-history-review-20261031/history-review.json`、`public-readme-offline-20261031/report.json`、`product-public-20261031-verification/report.json`、`product-public-content-diff-20261031.json`、`product-public-roundtrip-20261031.json`、`product-public-visibility-20261031.json`。

安装导出为 `D:/Sim-BIM-Agent/releases/product-public-20261031-install`；构建/独立环境为 `product-public-20261031`。现有桌面安装不动。用户主库原有39文件继续按哈希保全；源码修改与报告正常提交，未擅自提交用户 roadmap 或资料。
