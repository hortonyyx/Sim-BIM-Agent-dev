# 成品 Git 仓库发布

开发主库是程序修改的唯一来源；成品仓库保存已生成、已离线验收的当前 Python 源码发行包。它沿用现有一个产品、图纸和 mesh 两入口，不定义最终产品架构。成品文件不要手改，修复回开发库后重新发布。

- 开发库：`D:/Sim-BIM-Agent/repo`
- 成品库：`D:/Sim-BIM-Agent/product-repo`
- 开发远端：`https://github.com/hortonyyx/Sim-BIM-Agent-dev`（仓库 ID `1216622598`）
- 成品远端：`https://github.com/hortonyyx/Sim-BIM-Agent`（仓库 ID `1440941560`）
- 无 Git 的构建和安装导出：`D:/Sim-BIM-Agent/releases/`
- 验收证据、环境和临时数据：成品 Git 跟踪范围之外。

`Sim-BIM-Agent` 已由成品仓库接管，旧开发 URL 不再用于定位开发历史。首次设置或更换 remote 时按上面的仓库 ID 验证身份，开发与成品不得共用同一个 origin URL；本地目录名称保持不变。

## 当前成品文件边界

成品范围止于 Sim-BIM 和 HTML 查看器。由当前入口、真实工具与必要资源的运行闭包生成发布内容；旧 GT/评测脚本、历史编排、RAG、EnergyPlus 服务/转换/运行以及 IDD/天气数据均不属于成品。开发库保留这些历史代码和完整版本记录。成品使用单独生成的依赖锁和当前版本登记，记录源提交及源版本关系；不能通过沿用旧版本号或跳过核验来裁剪文件。旧运行是否可恢复以身份核验结果为准，不承诺跨版本强行恢复。

输入案例、评测答案、实验记录、开发报告、tests、开发 Git 历史、凭据和运行产物也不进入包。生成策略和资源范围由发布代码管理；增加当前功能时先在开发库修改、测试，再重建成品，不能在成品仓库手工补文件。

构建从固定 Git 提交直接读取文件；普通源码保持提交字节，Windows `.cmd`、`.bat`、`.ps1` 启动脚本显式输出 CRLF。包入口 facade、依赖项目文件和当前版本登记由同一提交内的策略生成，每个成品文件的来源与转换记录在 manifest 的 `projection.file_origins` 中，不依赖开发工作区的换行状态。`.gitignore` 排除本地环境、缓存及用户文件；`.gitattributes` 使用 `* -text` 保留已验证发行字节，避免 Windows Git 自动换行转换破坏清单 SHA-256。成品 Git 的完整跟踪集合必须等于 manifest 中的文件加 `release-manifest.json`。发布说明由开发库的 `product/README.md` 生成；没有另一份需要手工维护的产品源码。

`scripts/release/product_profile.json` 定义入口、必要动态资源、禁入子系统和依赖映射；`product_projection.py` 计算运行闭包，遇到未声明的动态导入或禁入子系统会拒绝构建。依赖范围变化时先运行 `python scripts/release/prepare_product_dependencies.py D:/Sim-BIM-Agent/cache/<new-dependency-stage>`，审阅生成的 `product-runtime.lock` 与元数据再提交；生成器离线复用开发锁版本，不能静默升级共同包。固定代码提交后在开发库正常 `python -m src.agent_runtime.agent_registry register` 并提交新登记，再构建。成品登记由构建器生成，不能在安装目录修改登记或补旧版本 alias 来恢复旧运行。

## 每次发布

先在开发主线完成正常定向检查、提交、版本核验。以下命令里的版本目录每次使用新名称，不覆盖历史构建。

```powershell
python scripts/release/build_product.py D:/Sim-BIM-Agent/releases/<release-id>
& D:/Sim-BIM-Agent/releases/<release-id>/setup.ps1
& D:/Sim-BIM-Agent/releases/<release-id>/.venv/Scripts/python.exe -I -B scripts/release/verify_product.py --product D:/Sim-BIM-Agent/releases/<release-id> --output D:/Sim-BIM-Agent/cache/<release-id>-verification
python scripts/release/sync_product_repo.py plan --repository D:/Sim-BIM-Agent/product-repo --release D:/Sim-BIM-Agent/releases/<release-id>
python scripts/release/sync_product_repo.py sync --repository D:/Sim-BIM-Agent/product-repo --release D:/Sim-BIM-Agent/releases/<release-id> --verification D:/Sim-BIM-Agent/cache/<release-id>-verification/report.json
git -C D:/Sim-BIM-Agent/product-repo diff --stat
git -C D:/Sim-BIM-Agent/product-repo add --all
git -C D:/Sim-BIM-Agent/product-repo commit -m "Publish <release-id> from <source-commit>"
python scripts/release/sync_product_repo.py check --repository D:/Sim-BIM-Agent/product-repo
git -C D:/Sim-BIM-Agent/product-repo tag -a <release-id> -m "Verified source release <release-id>"
git -C D:/Sim-BIM-Agent/product-repo push origin main --follow-tags
```

第一次同步前在空目标目录执行 `git init -b main`。脚本要求目标为独立 Git 根，拒绝本地改动、额外跟踪文件和未知文件冲突；只同步清单所属文件及删除退出旧清单的文件，不清空目录、不覆盖 `.git` 或被忽略的本地环境。成功同步后需要显式提交；脚本不自动 push。验证报告必须通过且目录及 manifest 哈希匹配。

同步过程中的正常异常会还原本次所属文件。进程被强制终止或断电时，可能留下未提交改动和 `.git/sim-bim-publish.lock`；成品 Git 同步没有桌面更新器的持久化崩溃恢复。确认发布进程已结束后，先按 Git 差异检查并保留任何本地修改，再恢复上一提交、只移除本次明确新增的文件及锁，重新发布；不得无差别清空目录。

新发布应额外从成品提交 clone/export 后回读 `check`，确认 Git 储存和检出未改变字节。manifest 的 `source_commit` 始终指向开发来源提交；成品仓库的提交由 Git 自身记录，两者不要混用。

## 安装与回退

现有安装验收和桌面更新工具要求没有 `.git` 的产品目录，继续保持这个约束。由已提交成品导出：

```powershell
python scripts/release/sync_product_repo.py export --repository D:/Sim-BIM-Agent/product-repo --destination D:/Sim-BIM-Agent/releases/<release-id>-install
./scripts/release/update_product.ps1 -Action plan -Release D:/Sim-BIM-Agent/releases/<release-id>-install
./scripts/release/update_product.ps1 -Action update -Release D:/Sim-BIM-Agent/releases/<release-id>-install
```

已有桌面安装使用原更新器的失败/中断恢复与上一版备份；新安装先在无 Git 的导出目录运行 `setup.ps1`。需要旧成品时检出/克隆对应 tag 后导出，不强推历史。源版本和依赖锁不变也不复制 `.venv`。

离线发布验收覆盖程序/资源隔离、图纸与 mesh 配置及输入、合成 BIM、mesh 工具和中断恢复。它不证明当前模型整案质量或性能。真实模型验收需另定模型、输入和预算，不由发布自动触发。
