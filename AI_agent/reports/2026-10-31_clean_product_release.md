# 2026-10-31 Sim-BIM Agent 清理版发布

用户明确要求将发布仓库命名为 Sim-BIM Agent、开发仓库命名为 Sim-BIM Agent dev，并将成品范围收窄到 Sim-BIM 与 HTML 查看器。已完成仓库重命名、真正移除旧功能的成品投影、独立安装验收及私有 Release 发布。没有新增前端/交互编辑、调整模型策略或发起真实模型请求；后续产品结构仍由开发主线决定。

## 仓库与固定版本

| 项目 | 当前值 |
|---|---|
| 开发库 | https://github.com/hortonyyx/Sim-BIM-Agent-dev ，public，ID `1216622598` / `R_kgDOSIQwBg` |
| 成品库 | https://github.com/hortonyyx/Sim-BIM-Agent ，private，ID `1440941560` / `R_kgDOVeMF-A` |
| 本地目录 | `D:/Sim-BIM-Agent/repo` 与 `D:/Sim-BIM-Agent/product-repo`，未迁移目录 |
| 开发来源提交 | `9274c161009f425479426500f452f115157c229c`，已合入并推送开发 main |
| 源版本 | `runtime-v44-20261031` / `domain-v156-20261031` |
| 成品提交 | `eec98df93f733464d8027b70bdddbd7438f58a4f` |
| 发布 tag | `release-2026-10-31.2`，旧 tag/历史未重写 |
| 产品 runtime | `runtime-product-v1-e5b43d62eabeb14e8d172d7a` |
| 产品 domain | `domain-product-v1-caedd736b627087d24ce83ae` |
| Manifest SHA-256 | `9ca42a4dd737dff2fa5c44725593e73d083e25213b5e20a671208e6c174ef6b4` |

重命名前后仓库 ID、node ID、可见性相同。先将原开发库重命名为 `-dev` 并更新开发主库/全部关联工作树及本任务新建源码 clone 的 origin，再将旧 `Sim-BIM-Agent-Product` 改为成品名；每次 push 前核对 ID 和 origin。原 `Sim-BIM-Agent` URL 已被成品接管，不能依靠旧 URL 重定向访问开发历史。审计原件：`D:/Sim-BIM-Agent/cache/product-repository-rename-20261031.json`。

[正式 Release 与 ZIP](https://github.com/hortonyyx/Sim-BIM-Agent/releases/tag/release-2026-10-31.2)。ZIP 为 1,694,047 bytes，SHA-256 `bf99241476bda985cbdb51ba6087cc7c929b857ccda135e9d874a3f6f0c8652e`；GitHub 上传资产 digest 与本地相同。

## 实际清理与保留边界

成品从 565 文件 / 27,035,061 bytes 降为 **275 文件 / 5,855,833 bytes**，净减 78.34%；同步按旧清单移除 291 文件、新增 `src/agent/geometry/ring_order.py`、修改 13 文件。加 manifest 共 276 个 Git 跟踪文件。没有无差别清空目录，`.git` 与本地环境保护规则保持。

16 个上轮解释为登记兼容而保留的旧 GT/评测脚本本轮全部退出，包括 baseline/GT 制作与复核、独立评分、reading_regression 和 run_stage。其他未使用的诊断/历史工具、legacy 编排、judge、RAG、旧 SDK、EP MCP/转换/runner/database、IDD/天气与 EP 专属依赖也退出成品。开发库保留其代码和历史版本，不修改旧记录。

实际运行仍需的几何、修正算法、源 BIM 合同、建模工具、查看器与自有 runtime 保留。旧 EP data_model 中被几何入口使用的纯环排序算法抽到共享模块；开发旧 API 重导出同一函数，原几何测试通过。三个包 initializer 使用声明的当前运行 facade，避免导入旧聚合 API；来源转换可追溯。不是按文件名把共享算法或依赖直接删掉。

开发依赖锁 178 项 → 成品锁 43 项（含虚拟项目根），直接依赖 27 → 17。新成品环境实际安装 42 个外部包；所有保留包版本和来源逐项与开发锁一致，没有升级。产品 `project.name=sim-bim-agent`，没有 energyplus-mcp 入口。

旧全量版本表 9,740,434 bytes → 当前产品表 56,024 bytes，仅当前 runtime/domain 各一条、0 aliases。新内容身份绑定 source_commit、策略来源、完整成员/hash 和实际重算的工具目录/模式指纹；旧开发登记的全部既有记录语义保持。成品不能自行重新登记或补旧 alias，旧版本运行明确拒绝续跑。同版本恢复仍须通过输入、配置、版本核验。

当前产品只允许 single_model/role_division。独审发现旧 external_coordinator_mcp 使用字符串 `-m` 路由而未在导入闭包，已在产品配置预检与执行路由明确拒绝，开发模式保留；构建同时验证产品允许模式的目标模块确实进入 payload。没有为了兼容旧入口扩大成品。

成品根目录结构：

```text
README.md                      安装、现有输入、授权、版本边界
Start.cmd / setup.ps1           启动与按锁建立本地环境
sim_bim.py                     CLI 入口
config*.example.json           图纸、mesh 与服务配置模板
pyproject.toml / uv.lock        当前运行依赖
release-manifest.json           来源、版本、逐文件 hash、投影说明
.gitignore / .gitattributes     运行数据隔离与字节保真
src/agent/                    当前 BIM 输入、角色、几何、建模、交付
src/agent_runtime/             模型接入、会话、工具调用、恢复和当前登记
src/harness_contracts/         共用运行合同
src/configs,utils,validator/   当前闭包所需的小范围配置/工具/校验
scripts/tool_scripts/          当前模型工具、查看器与 vendor 资源
```

显示名称统一为 Sim-BIM Agent，开发 README 为 Sim-BIM Agent dev。既有 OAuth 凭据目录 `%LOCALAPPDATA%/EnergyPlus-Agent/openai-subscription` 和 DPAPI entropy 保持兼容；它是历史授权存储名称，不代表随包保留 EP 功能。没有迁移或清除授权。

## 验证与证据

| 范围 | 结果 | 耗时/边界 |
|---|---|---|
| 首轮 7 模块定向 | 62 通过、2 因源码尚未登记而被旧身份正确拒绝 | 70.36 s，0 模型；原输出为协作 exec 回执，非独立 stdout 文件 |
| 登记后两失败复验 | 2 通过 | 29.94 s；JUnit `cache/product-clean-registry-recheck-20261031.xml` |
| 动态路由与产品投影/入口 | 14 通过 | 12.34 s；与首轮有重叠，不累加数量 |
| 新自身环境安装与 doctor | 新建 `.venv`，42 包，ready | `uv sync --frozen --no-dev --no-install-project`，没有复制开发环境 |
| 正式隔离验收 | 通过，0 live model requests | 25.828 s；两模板/parser/原输入冻结hash、mesh 64px渲染、BIM/HTML、真实本地MCP输入工具和中断/重复恢复 |
| 独立反例与恢复审计 | 7 类篡改/重登记均拒绝；真实旧 checkpoint 拒绝 | 0 adapter 请求，旧运行原件全部hash不变；本版恢复工具执行始终1次 |
| 源/产品/策略绑定审计 | 275 条来源全覆盖，0 hash/范围差异 | 固定 Git blob 来源、facade/CRLF/依赖转换可复算；0禁入系统/高置信凭据命中 |
| fresh 开发远端浅 clone 重建 | 276 文件逐字节一致 | 包括完整 manifest，不仅比较文件数 |
| 成品远端 tag clone → export → ZIP | 276 文件集合与字节一致 | GitHub ZIP asset digest 也一致 |

首轮范围为 `test_product_projection`、`test_product_release`、`test_product_repository`、`test_f13_kernel_canonical_vertex_order`、`test_f95_concave_canonicalization`、`test_runtime_agent_registry`、`test_runtime_openai_subscription`。新完整性加固定向另通过；没有为收工扩跑历史全集。

正式包：`D:/Sim-BIM-Agent/releases/product-clean-20261031`；其 `.venv` 仅用于本地独立验收，不入包。无 Git 安装导出：`D:/Sim-BIM-Agent/releases/product-clean-20261031-install`。

证据原件：

- `D:/Sim-BIM-Agent/cache/product-clean-20261031-verification/report.json`
- `D:/Sim-BIM-Agent/cache/independent-clean-product-20261031/run-002/independent-review.json`
- `D:/Sim-BIM-Agent/cache/product-clean-rebuild-20261031.json`
- `D:/Sim-BIM-Agent/cache/product-clean-remote-roundtrip-20261031.json`
- `D:/Sim-BIM-Agent/cache/product-clean-preservation-final-20261031.json`

root 另回读合成 `source_model.json`：schema 为 source_bim_v2，2 spaces、2 openings 及 boundaries/connections 等；实际 `viewer.html` 为 742,338 bytes。此为合成演示贯通，不代表真实建筑识读质量。没有新整栋模型、建筑质量/性能、浏览器交互、订阅连通或 EP 测试；桌面现有安装未更新。

## 后续更新

按 [成品仓库发布](../workflow/product_repository.md) 在开发库修改 → 定向检查 → 提交与版本登记 → 固定提交构建 → 自身环境离线验收 → `sync_product_repo.py plan/sync/check` → 成品提交/tag/Release。只更新 manifest 所属文件并删除退出清单成员，保护 Git、本地环境与外置用户材料。发布包不是独立 EXE；安装需 Python 3.12 与 uv。

主库原有 roadmap 修改及 38 个未跟踪资料文件共 39 项已按哈希保全，不覆盖、不擅自提交。此前首次发布留下的中断 clone 清理被自动审批拒绝，仍按首次报告保留该 cache，本轮未重试删除；它不在成品或 ZIP 中。
