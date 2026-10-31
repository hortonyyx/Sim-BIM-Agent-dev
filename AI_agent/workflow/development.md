# 开发、验证与发布

## 工作范围

项目经理按完整能力包安排实现、检查与独立审阅。先明确当前需求、可观察结果和受影响接口，再做最小必要实现。汇报明确 runtime 或 domain（BIM rules/kernel/tools/methods/roles/guidance）的变化及模式影响。

测试依据当前需求；设计替换时相应阶段性检查退出。有效性质与真实缺陷的最小反例保留，不锁历史文件全文、函数AST、报错文案或无关工具顺序。普通检查离线，`ep_no_billed_gate`保持早加载。

## 文件位置

| 位置 | 用途 |
|---|---|
| D:/Sim-BIM-Agent/repo | 唯一开发Git主库，当前文档、必要测试与产品代码 |
| D:/Sim-BIM-Agent/worktrees | 短期开发检出，完成后收回 |
| D:/Sim-BIM-Agent/cases | 新案例原件与独立GT，逻辑/权限保持隔离 |
| D:/Sim-BIM-Agent/runs | 模型运行、检查点与结果，不复制回主树 |
| D:/Sim-BIM-Agent/archive | 迁移核验、精简前文档及旧树独有证据 |
| D:/Sim-BIM-Agent/cache | pytest、临时工具和可再生产物 |
| D:/Sim-BIM-Agent/cache/release-staging | 固定提交生成的本次候选暂存，不安装环境 |
| D:/Sim-BIM-Agent/cache/product-publish | 发布状态、持久化桌面验收证据；其 repository 仅为成品 Git 发布缓存，无运行环境 |
| D:/Sim-BIM-Agent/releases | 旧发布导出保留原位，不作为日常安装，也不批量清理 |
| C:/Users/Horton/Desktop/Sim BIM Agent | 唯一实际运行的成品安装，无 .git；版本写在发布清单，不另建桌面版本目录 |

历史 `AI_agent/logs`、`AI_agent/archive`、`case_tests`、`backup`、`showcase`、`output` 保留在 D repo 实体目录，已退出当前 Git 树、默认测试和产品。该布局是批量移位遭自动审批拒绝后的保守处理；实际映射见 [归档索引](../reports/archive-index.md)。新记录放 `AI_agent/reports`，原始大输出放外部 runs/cache。

主线只保留必要小夹具、短报告、run/case ID、版本及哈希索引。较大正式证据保存可校验副本后再归档；本地存在、远端已备份、可独立恢复分别报告。不将凭据、完整GT或旧模型答案放入产品。

## 检查选择

使用 `python scripts/dev/check.py --help` 查看实际入口，`tests/check_groups.json`是逐模块范围清单。

- 默认快检：当前runtime协议、恢复/记账/权限与关键几何性质，显式少量worker。
- 定向：本次变更及相邻接口。文件可显式指定，新模块需要归入检查清单；不得静默漏收。
- 节点：当前本地MCP和代表案例贯通；改动波及共用runtime、几何或交付时选择相应集合。
- 历史：只有需要查验旧证据/兼容承诺时才显式选取，并传归档、案例根。退出默认集合不等于已证明旧缺陷消失。
- 真实模型：与离线检查分开，按用户批准的具体范围执行，不由pytest、发布或收工触发。

直接运行pytest时注意终端显示的实际选择范围；`tests`绿色不代表历史全集已运行。普通文档和路径索引改动做diff/链接检查，不跑全量。同代码同范围有效结果复用；收工和换助手不触发重复检查。已失败项修复后复验其受影响范围。

测试临时目录一律放D盘cache，每次有独立basetemp；结束保存小结果后可清缓存。完整案例不要作为普通fixture整树复制，不复制历史候选/仿真输出。

## 模型与质量

模型服务和计费按 [模型约定](models.md)；用户当前指令覆盖旧安排。DeepSeek须本次明确许可，不自动回退付费线路。ChatGPT授权由用户选号后发起。GLM订阅小测遵守已有授权的范围。整案与节点对照先提出配置、输入、模式和保护线再执行获准范围。

优先观察实际行为与质量的对应：首次试建、看图/量测、格式拒绝、返工及最终对象变化。提交成功不等于独立质量通过；图像已返回不等于模型理解正确。历史重放、人工辅助与冷启动分别标注。不能用调整评价口径掩盖空间关系退步。

每次检查给正文表：范围/版本、通过失败跳过、耗时、调用及用量、失败与复验、未覆盖项。真实建模另给角色行为、5/10/30cm分档、平面/回叠/BIM入口；费用未知明确写未知。

## Git与发布

正常commit/push、tag及可恢复的工作树管理已授权。一条main，由项目经理集成；短期树写明文件归属，避免共享文件并发覆盖。删除前核对合入状态、独有未跟踪/忽略资料和活动进程。共享历史不强推。

runtime/domain按实际变更统一登记，旧记录不追改：
```powershell
python -m src.agent_runtime.agent_registry register
python -m src.agent_runtime.agent_registry verify
```

日常顺序固定为：已提交、已登记的开发代码 → `stage` 构建暂存并更新桌面 → 桌面自身环境验收 → `publish` 发布同一份桌面文件及 Release 附件。`status` 分别报告桌面验收、云端 Git 的 manifest 一致性和 Release 就绪状态。具体命令和报告绑定见 [桌面验收与成品发布](product_repository.md)。`product/` 仅是模板；暂存与 Git 发布缓存均不建立 `.venv`，不能先在另一个安装验收后声称桌面已验收。

发布包不含 .git、tests、开发文档、实验、GT、凭据。程序和资源按 release-manifest 校验；运行环境在桌面原路径按锁文件建立，不能复制带旧绝对路径的虚拟环境。更新不覆盖外部用户数据。自动验收覆盖 doctor、离线示例、当前接口/工具/恢复；人工体验只记录未做或用户已明确声称完成，不伪称，也不为同一授权反复增加审批。

`stage` 调用现有 Windows 更新器；异常时使用同一个恢复入口：

```powershell
./scripts/release/update_product.ps1 -Action rollback
./scripts/release/update_product.ps1 -Action status
```

更新前结束产品运行，维护期间不启动新实例。清单哈希、本地修改、文件冲突与路径边界先检查；每次实际切换都由桌面自身 Python 完成隔离验收。验证失败自动恢复原版，进程意外中断则拒绝继续更新，先执行 `rollback`。依赖不变时复用原路径环境，依赖变化时 `uv sync --frozen`；更新器的 `-Offline` 可禁止依赖下载，恢复失败如实保留 pending 状态。云端发布失败不改变桌面验收事实；重试前核对远端提交、tag 和 Release 的实际状态，不覆盖已发布历史。push 成功而 Release 或附件失败时，以同 tag 重试 `publish` 补齐。

按用户要求保持简单：`D:/Sim-BIM-Agent/archive/product-backup` 保留更新器管理的上一版压缩程序备份及恢复状态；不长期备份环境、案例、凭据或大日志。桌面验收报告持久保存在发布管理目录，不随本次暂存清理丢失。`cache/release-staging` 和 `cache/product-update` 只清理本次成功流程所属的临时内容；失败或中断的资料留待核查，旧 releases 和曾 blocked 的缓存不批量删除。更早版本由 Git tag 管理；需要时导出到无环境的受管暂存，再走同一桌面更新工具。

## 收尾

保存简短工作记录和未决项，更新当前状态，核对diff与检查结果，正常commit/push，收回已完成工作树并报告C/D空间。独立审阅由未参与实现者完成；修复其问题后复核。保留实际限制，不把部分完成说成全部完成。
