# 桌面验收与成品发布

开发库是程序修改的唯一来源；桌面是唯一实际运行的成品安装。日常流程为：开发提交 → 构建候选 → 原位更新桌面 → 桌面自身环境验收 → 将同一份桌面文件发布云端。成品仓库不承接开发，修复回开发库后重新走该流程。当前仍是一个产品、图纸和 mesh 两入口，范围止于 Sim-BIM 与 HTML 查看器。

## 目录与身份

| 位置 | 职责 |
|---|---|
| `D:/Sim-BIM-Agent/repo` | 开发主库，包含产品源码、生成规则和必要检查 |
| 开发库内 `product/` | 用户说明、配置及启动模板，不是安装目录 |
| `C:/Users/Horton/Desktop/Sim BIM Agent` | 唯一实际运行安装，保留自身 `.venv`，没有 `.git` |
| `D:/Sim-BIM-Agent/cache/release-staging` | 本次候选暂存，只含发行文件，不建立 `.venv` |
| `D:/Sim-BIM-Agent/cache/product-publish` | 发布状态及持久化桌面验收证据 |
| 上述管理目录内 `repository` | 成品 Git 发布缓存，不安装、不运行产品 |
| `D:/Sim-BIM-Agent/releases` | 历史导出保留原位，不作为日常运行入口 |

开发远端为 [Sim-BIM-Agent-dev](https://github.com/hortonyyx/Sim-BIM-Agent-dev)，仓库 ID `1216622598`；成品远端为 [Sim-BIM-Agent](https://github.com/hortonyyx/Sim-BIM-Agent)，仓库 ID `1440941560`。首次设置或更换 remote 时核对仓库身份，不能混用两个 origin。旧开发 URL 已由成品接管，不能再用于定位开发历史。

旧 `D:/Sim-BIM-Agent/product-repo` 只在确认工作树干净、远端已同步、没有未跟踪或忽略文件后迁入 `cache/product-publish/repository`，不保留第二个发布检出，不顺带删除历史安装或缓存。本次流程整理只将桌面对齐已公开的 `release-2026-10-31.3`，不因开发发布工具变化另发 `.4`。

## 日常操作

使用开发环境中的 `scripts/release/desktop_release.py`，先用 `--help` 查看当前参数。三个动作各有明确职责：

1. `stage` 从已提交、已登记的固定开发来源生成候选暂存，调用现有更新器更新固定桌面，再由桌面自身 Python 完成隔离验收。暂存不安装运行环境；只有验收成功才记录桌面已验证状态，报告必须持久化，不能随着临时目录消失。
2. `publish` 重新检查桌面 manifest、文件哈希及与其绑定的通过报告，只把这份桌面内容同步到成品 Git 缓存，再完成 commit、tag、push 和 GitHub Release 附件上传。它不重新从开发库构建另一份包，也不以缓存中的旧验收代替桌面验收。
3. `status` 拉取成品远端后，分别展示桌面是否有有效自动验收证据、桌面与云端 Git 是否同 manifest，以及 Release 是否就绪。`desktop_verified` 是验收结果，`git_synced` 表示程序与云端 Git 同字节，`release_ready` 表示 tag、非草稿 Release 及 ZIP 哈希通过核对；后两者同时成立才显示 `status=published`。未获取到远端证据时不能把“本地与缓存一致”写成“云端已同步”。

```powershell
python scripts/release/desktop_release.py status
python scripts/release/desktop_release.py stage
python scripts/release/desktop_release.py status
python scripts/release/desktop_release.py publish --tag "<release-id>" --notes-file "D:/Sim-BIM-Agent/cache/product-publish/<release-id>.md"
```

将 `<release-id>` 替换为本次新 tag，事先写好发布说明文件；已与云端同 manifest 时不创建空提交，若该 tag 的 Release 或附件尚未完成，则用同一命令补齐，不另造新版本。`stage` 默认构建当前固定开发提交；安装已冻结的发行包时使用 `stage --release "<existing-release-directory>"`，仍在桌面完成验收，不能拿旧安装报告顶替。`--offline` 传给更新器，限制依赖下载，不代表整个编排不访问 GitHub；发布状态仍需远端核对。状态保存在 `cache/product-publish/state.json`，验收证据保存在同目录 `verification` 下，绑定实际桌面路径及 manifest 哈希。

发布前在开发主线完成所需定向检查、提交程序改动，并按实际变化登记 runtime/domain 及提交登记。文档或发布编排变化不自动改产品版本。旧登记不追改，成品 manifest 的 `source_commit` 始终指向实际开发来源提交；成品 Git 提交和 Release tag 是另外的身份。

自动验收与人工体验分开记录。离线验收覆盖程序/资源隔离、图纸与 mesh 配置和输入、合成 BIM、mesh 工具及中断恢复，不证明真实模型整案质量或性能。桌面自动验收通过、文件及报告核对一致后，即可按已有授权继续发布，不新增人工审批门槛。人工体验只记录“未做”或用户已明确声称完成及其范围；未调用模型、用户尚未手测均如实说明。真实模型验收仍按用户批准的输入、模型和预算另行执行，发布工具不自动发请求。

## 发布内容与字节一致性

成品由当前入口、真实工具和必要资源的运行闭包生成。旧 GT/独立评测、历史编排、RAG、EnergyPlus 服务/转换/运行以及 IDD/天气数据不进入包；开发库保留历史代码与完整版本记录。案例、答案、实验记录、开发报告、tests、凭据和用户运行产物也不进入成品。

构建从固定 Git 提交读取文件；普通源码保持提交字节，Windows `.cmd`、`.bat`、`.ps1` 显式输出 CRLF。包入口 facade、产品依赖文件和当前版本登记由同一提交中的声明规则生成，manifest 的 `projection.file_origins` 记录每个文件的来源和转换。成品 `.gitattributes` 使用 `* -text` 保留已验收字节；Git 完整跟踪集合必须等于 manifest 文件集合加 `release-manifest.json`。发布用户说明来源于 `product/README.md`，无需在成品目录手工维护副本。

`scripts/release/product_profile.json` 定义入口、动态资源、禁入子系统及依赖映射；`product_projection.py` 计算运行闭包，未声明的动态导入或禁入子系统会阻止构建。依赖范围变化时先运行 `python scripts/release/prepare_product_dependencies.py D:/Sim-BIM-Agent/cache/<new-dependency-stage>`，审阅生成的锁与元数据再提交。生成器复用开发锁版本，不能静默升级共同包。

成品使用单独生成的依赖锁及当前内容身份，保留源提交、源版本与完整哈希关系。不能沿用旧 ID 同时改文件表，也不能补旧版本 alias 或跳过核验来强行恢复旧运行；需要旧版本时使用原 tag。父包、动态资源和当前工具仍受成员及哈希核验约束。

`build_product.py`、`sync_product_repo.py` 和 `update_product.ps1` 是编排层使用的底层工具。日常使用 `stage`、`publish`、`status`；只有明确的恢复或维护任务才直接调用底层命令。发布后回读成品 Git 清单、远端提交/tag 及 Release 附件状态，确认与已验收桌面内容一致，不为回读另外建立可运行安装。

## 失败恢复与暂存清理

更新前结束桌面产品运行，维护期间不启动新实例。更新器先检查清单、本地修改、文件冲突和路径边界；依赖不变时复用桌面原路径环境，变化时按锁同步，不能复制虚拟环境。更新不覆盖外部用户数据；验收失败恢复原版，中断则保留恢复状态，使用同一工具处理：

```powershell
./scripts/release/update_product.ps1 -Action status
./scripts/release/update_product.ps1 -Action rollback
```

上一版压缩程序备份及恢复状态由 `D:/Sim-BIM-Agent/archive/product-backup` 管理，不长期备份环境、案例或凭据。成功后仅清理本次受管构建/更新暂存，桌面验收报告保留在发布管理目录；失败或中断的内容留待核查。历史 `releases` 和曾被阻止清理的缓存不在本流程中批量删除。

成品 Git 同步的正常异常会恢复本次所属文件。强制终止或断电可能留下未提交改动、`cache/product-publish/maintenance.lock` 或 `.git/sim-bim-publish.lock`；它不具备桌面更新器的持久化崩溃恢复。先确认进程已结束，核对并保留本地差异，再恢复上一提交、只移除本次明确新增文件和锁，不能无差别清空目录。push 失败时先核对远端实际结果再重试，不覆盖既有 tag；桌面已通过的验收证据仍应保留。若 push 已成功而 Release 或附件失败，使用相同 tag 和发布说明重试 `publish` 补齐，不把 Git 同步成功写成附件已上传。
