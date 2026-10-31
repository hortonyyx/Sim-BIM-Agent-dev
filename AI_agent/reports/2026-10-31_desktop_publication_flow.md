# 2026-10-31 桌面作为发布版唯一使用入口

按用户要求，将维护流程统一为：**开发提交 → 原位更新桌面 → 桌面自身环境检查 → 从桌面同一份文件发布云端**。开发库 `product/` 只是说明、配置和启动模板；桌面是唯一维护的用户运行安装。产品说明 README、模型策略、前端和架构均未修改，本轮没有新产品 `.4`。

## 当前桌面与云端已一致

| 项目 | 结果 |
|---|---|
| 固定桌面 | `C:/Users/Horton/Desktop/Sim BIM Agent`，路径未迁移，没有 `.git` |
| 当前公开版本 | `release-2026-10-31.3`，成品提交 `9fc0f4c11bbbceea1fbe0a5affb6dc34a561d247` |
| 开发来源 | `edd9ddaf495132f8055633c3150d9714cafcd945` |
| Manifest SHA-256 | `47ef7243a4c532d1d1b293c50d431127ab558808031e9790d6e96efe942412cd` |
| 文件一致性 | 桌面与公开 tag 的 manifest 原字节及全部275个受管文件哈希一致 |
| 当前状态 | `desktop_verified=true`、`git_synced=true`、`release_ready=true`、`status=published` |
| 用户人工体验 | `not_recorded`；没有冒称用户已经手测 |

桌面从旧 `runtime-v24-20261017` / `domain-v110-20261018`、来源 `b72ea6c7` 原位升级。更新预检通过，没有活动产品进程或未受管个人文件冲突；更新117个受管文件、移除283个退出旧清单的文件。依赖按新锁从旧172包收敛到42包，移除130个旧包，没有复制其他环境。

桌面自身 Python 的正式离线验收通过：25.813 s，0真实/计费模型请求。覆盖 doctor、合成 BIM/HTML、两输入配置和冻结哈希、mesh渲染、真实本地MCP与中断/重复恢复。桌面没有遗留旧评测/EP文件；独审确认除 `.venv` 和字节码缓存外没有非manifest文件。

完整验收工件与报告保存在：`D:/Sim-BIM-Agent/cache/product-publish/verification/c554a9bfab2541a98824ba63ecd46c79/report.json`。更新器现在可用 `-VerificationRoot` 将完整工件保留在外部，不再随更新临时目录消失；通过报告严格绑定实际桌面路径与manifest哈希。

旧版程序备份为 `D:/Sim-BIM-Agent/archive/product-backup/previous.zip`（5,217,757 bytes），独审对照更新前全部520个受管文件一致；授权存储3文件哈希未变。更新状态与回退入口保持原实现，不覆盖外置输入、配置和输出。

## 日常操作

开发环境使用：

```powershell
python scripts/release/desktop_release.py stage
python scripts/release/desktop_release.py status
python scripts/release/desktop_release.py publish --tag "<release-id>" --notes-file "D:/Sim-BIM-Agent/cache/product-publish/<release-id>.md"
```

`stage` 从固定已提交程序构建候选到受管暂存，直接调用原更新器更新桌面，再使用桌面自身环境验收；不在候选目录建立第二个 `.venv`。当前对齐已有 `.3` 使用 `stage --release`，只读认领已存在的公开版本，不重新创建tag/Release。

`publish` 只取实际桌面manifest所属文件，重新检查其字节及绑定报告，确认来源提交可追溯到开发远端，然后同步Git缓存、commit/tag/push并发布ZIP。上传后还需tag目标、非草稿Release与ZIP digest一致；Git已push但附件失败时保持待发布，用相同tag补缺，不重造提交、不改已有tag、不覆盖不同资产。`status`分别记录桌面验收、Git一致、Release就绪，后两项都成立才称已发布。

这些是现有builder/updater/verifier/sync的薄编排。没有额外人工批准门槛，也不会自动发真实模型请求；某轮若用户指定人工或真实模型验收，再按其范围执行。

## 目录收敛与保留

- `D:/Sim-BIM-Agent/repo`：唯一开发主库；`product/` 仅模板。
- 桌面 `Sim BIM Agent`：唯一维护的运行安装。
- `D:/Sim-BIM-Agent/cache/product-publish/repository`：自动维护的成品Git缓存，没有 `.venv`，不是用户运行入口。旧 `D:/Sim-BIM-Agent/product-repo` 在clean、0未跟踪/忽略文件、HEAD与远端一致、路径边界确认后完整迁入；迁移后仍clean、来源remote未变。
- `cache/product-publish/state.json` 与 `publication.json`：桌面/云端状态和可重试的发布身份；`verification/` 保存桌面证据。
- `cache/release-staging`：只存本次候选或压缩中转，成功清理本次有归属标识的目录，失败保留；不安装环境。
- 既有 `releases` 历史验证副本留在原位，不再作为日常安装或更新入口，没有批量删除历史资料。此前被自动审批阻止的cache目录未再触碰。

## 定向检查与边界

| 范围 | 结果 |
|---|---|
| 既有Windows更新/回退性质 | 20通过，36.89 s |
| 新桌面发布流程 | 12通过，77.33 s；真实本地bare Git/commit/tag/push/archive，GitHub与安装进程模拟，0外网/模型 |
| 新流程关键反例 | 报告异目录、桌面篡改、无Release不已发布、创建/上传失败后同tag补齐、不同tag/asset拒绝、无旧update state时同版验收、受管清理边界 |
| 实际桌面验证 | 通过，25.813 s；0模型 |
| 独立桌面/备份/授权与发布审查 | PASS，无剩余阻断；根助手另联网逐文件核对公开 `.3` |

JUnit：`D:/Sim-BIM-Agent/cache/desktop-update-flow-20261031.xml`、`D:/Sim-BIM-Agent/cache/pytest/desktop-release-20261101-01.xml`。独审证据：`cache/desktop-workflow-independent-20261031/review-binding-and-preservation.json`、`root-cloud-byte-check.json`；缓存迁移证据：`cache/product-publish/migration.json`。

主库原有roadmap和未跟踪资料共39文件继续按哈希保全，不擅自提交；产品README与云端 `.3` 内容保持。未做新的模型整案、建筑质量、性能、浏览器交互或用户人工体验测试。收尾空间约C盘40.58 GiB、D盘80.57 GiB。
