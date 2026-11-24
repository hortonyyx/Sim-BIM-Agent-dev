# 本地建模工作台

开发提交 `4235b6f8`，domain-v186 / runtime-v60。Horton 的本机入口为桌面 `Sim-BIM 工作台.lnk`，使用开发库的 Python 执行 `sim_bim.py workbench`，在默认浏览器打开仅绑定本机的服务。发行包的 `Start.cmd` 在双击时打开同一项工作台。

- 拖入 PNG、GLB 或建筑说明 JSON 后，添加按钮把文件复制到工作区的 `materials/<block>/<slot>/`。编辑、排序、删除引用和总说明保存在版本化的 `project.json`。运行时保存独立的 `runs/<run>/input.json` 和素材字节，保留原名并为重名文件分配可供模型调用的名称。
- 主控与可选 worker 可使用 ChatGPT 订阅和 DeepSeek 官方 API。本机实际查询到六个管线支持的 ChatGPT 型号以及 DeepSeek Flash。DeepSeek 凭据经当前 Windows 用户 DPAPI 保护。运行配置只保存模型选择。UI 使用账号目录与当前可执行型号的交集。
- 运行语言贯穿主控、worker、交付报告与三维查看器。查看器继续提供查看、剖切、量测等操作。本轮需求聚焦录入，用户尚待定义最终模型的交互修改方式。
- Chrome 验收覆盖桌面和窄屏、拖放三种素材、同名文件排序、纯文字组、刷新恢复、草稿冲突和切换语言；测试工作区与实际项目分开。工作台测试覆盖从 HTTP 导入到冻结与生产配置。DeepSeek 的 DSH 执行用本地模型响应验证工具调用、续跑和七项交付摘要，实际账号验证为模型目录查询。此轮未用账号生成新的整栋案例。
- 最终快速套件通过 856 项。开发版浏览器入口返回 HTTP 200。中英文交付在实际结果路由的沙箱内验证了楼层筛选、三维点选、下载 PNG、打开图纸回叠和查看源记录。
- 候选包已从 `26959502` 构建并含 254 个文件。原桌面目录的 Python 进程为 Windows Low 完整性，实际不能向普通项目目录或用户级验收目录写入。桌面更新器已将旧安装恢复到 `c12b75a7`，在 LocalLow 的隔离验收通过；记录在 `%USERPROFILE%/AppData/LocalLow/Sim-BIM-Agent/installation-verification/fc70710f97c345a79634b00459773f12/report.json`。新增快捷方式使用工作库的 Python，保留原安装权限。

## 自定义 API 与独立启动

开发提交 `9140652d`，domain-v189 / runtime-v63。桌面快捷方式已实开，新进程读到六个 ChatGPT 模型与 DeepSeek Flash。另在模型设置中可添加多个 OpenAI 兼容的 Chat Completions 服务，获取模型列表或填写模型 ID。所选主控和 worker 在提交时锁定各自连接的版本。密钥由 Windows 用户凭据保护，仅服务进程按冻结版本读取。

从工作台提交了一张合成平面图。主控读图期间结束原工作台 HTTP 进程，后台继续调用独立 worker、生成一层两间模型并交付。再次启动工作台后，同一工作区返回完成状态，HTML 报告和查看器均可取回。测试服务还核验了工具签名、多轮调用 ID、图像和输出上限字段。该测试使用本地可重复的模型响应。验证记录为 `D:/Sim-BIM-Agent/cache/workbench-standalone-custom-20261124/workbench-standalone-e2e-9ae8sbc5/proof.json`。另一次 DSH 验证覆盖删除当前连接后按冻结配置续跑，以及两个不同型号的 worker，见 `D:/Sim-BIM-Agent/cache/workbench-custom-models-d46dyp49/verification.json`。

## 单文件 Sim-BIM 交付

开发提交 `9a8f8892`，domain-v191 / runtime-v64。工作台直接打开本次运行的 `Sim-BIM.html`，下载也是同一文件。新运行的浏览器结果入口只提供模型，检查报告、源模型、假设和输入依据保存在原工作区。历史完成报告的链接仍可用。桌面产品版本的同步按用户安排留待后续。

查看器每次打开默认英文，可以就地切换中文。点选空间、楼层、构件和边线均只显示名称、楼层和功能，中文房间功能带英文。公开 HTML 只嵌入显示所需模型数据、运行库和贴图，支持离线查看及 PNG 导出。

利苑示例取自 `workbench-custom-models-d46dyp49` 的 C03 和原 GLB，沿用已选模型几何。交付为 `D:/Sim-BIM-Agent/exports/lee-garden-six-viewer-20261124/Sim-BIM.html`，SHA-256 为 `526c2dc12d74436c1b1a194668be7d59bf5c6e3b0052ea69ac947efa68c52917`。Chrome 在 1440×900 和 390×844 的页面及点选验证记录见 `D:/Sim-BIM-Agent/cache/sim-bim-delivery-20261124/final-qa.json`。工作台通过本机 DSH 和可重复模型响应完成从点击运行、自动进入结果、回到草稿直至下载的验证，见 `D:/Sim-BIM-Agent/cache/sim-bim-delivery-20261124/interactive-8kuq3rhi/verification.json`。交付样例的几何未重新建模。
