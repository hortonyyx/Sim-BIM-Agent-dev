# 成品 Git 仓库发布

开发主库是程序修改的唯一来源；成品仓库保存已生成、已离线验收的当前 Python 源码发行包。它沿用现有一个产品、图纸和 mesh 两入口，不定义最终产品架构。成品文件不要手改，修复回开发库后重新发布。

- 开发库：`D:/Sim-BIM-Agent/repo`
- 成品库：`D:/Sim-BIM-Agent/product-repo`
- 无 Git 的构建和安装导出：`D:/Sim-BIM-Agent/releases/`
- 验收证据、环境和临时数据：成品 Git 跟踪范围之外。

## 首版文件边界

沿用 `build_product.py` 当前已追踪源文件选择。输入案例、评测答案、实验记录、开发报告、tests、开发 Git 历史、凭据和运行产物不进入包。全部被当前 runtime/domain 登记的文件、完整版本表、共享依赖及可选 EnergyPlus 资源保持原样。部分历史评测工具是当前不可变 domain 的已登记成员；本轮保留其代码以维持核验，不带其评测数据。后续裁剪由开发主线统一实施并登记新版本，不能在发布端悄悄改旧版本。

`.gitignore` 排除本地环境、缓存及用户文件；`.gitattributes` 使用 `* -text` 保留已验证发行字节，避免 Windows Git 自动换行转换破坏清单 SHA-256。成品 Git 的完整跟踪集合必须等于 manifest 中的文件加 `release-manifest.json`。发布说明由开发库的 `product/README.md` 生成；没有另一份需要手工维护的产品源码。

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
