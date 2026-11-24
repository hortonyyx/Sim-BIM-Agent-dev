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

## 分类型点选卡和顶点量测

本次开发版继续使用同一份利苑 C03。点选卡以标准名称为标题，中英文切换保留选中对象。利苑办公空间 `Z174_F22_Office_S` 的水平投影面积为 242.45 m²，模型体积为 775.84 m³。其所在的 F22（原名 O13）包含 18 个空间，投影面积为 321.57 m²，本层体积为 1,029.02 m³。面积按水平投影并集计算并扣洞，体积按高度截面计算，跨层空间仅计入与该楼层重合的高度。表面卡显示被点中面片的三维面积，边线卡显示所属面和两端点的实际长度。

量测采用源几何坐标。展开楼层或空间时，两个端点和连线跟随显示位置，读数保留原值。端点球随相机深度缩放，在不同观察距离下维持可辨认的屏幕尺寸。地面方向标参与视角旋转和 PNG 导出。利苑源模型缺少地理北角，示例用 N / +Y 标示模型北。

开发提交 `5d8d7845`，domain-v192 / runtime-v64。同一路径的 `Sim-BIM.html` 更新为 SHA-256 `8f362928815e031cc19e37e957f649550fd6073a62863a1ba2f535026fc693ff`。电脑端按 1440×900、1366×768、1280×720 核验，点选和量测验收记录在 `D:/Sim-BIM-Agent/cache/sim-bim-viewer-inspection-20261124/`。其中 `interaction/measurement-results.json` 保留近远视距实测，跨空间两点在爆炸展开后显示相距 59.563 m，量测读数始终为 28.817 m。桌面安装版暂缓同步。

## 单层显示与点选卡精简

利苑单层模式下，原楼层裁切面会在视角变化时误裁与它共面的完整顶板、底板。现按各绘制对象的高度范围决定是否使用楼层裁切面。G、U、O01、O05、O13、O21 和 R 层从上下两个方向对照，共 14 次验证；完整面与同相机下保留该面的参照图一致。前后渲染、测试脚本与数值见 `D:/Sim-BIM-Agent/cache/floor-clip-20261124/`。

电脑端点选卡固定在模型视口左侧中间。楼层选择器、楼层卡和其他对象的楼层字段统一显示项目层名；历史空间与构件的完整标准名保留。空间卡展示楼层、功能与体积；楼层卡展示底标高、空间数与本层体积；表面卡展示构件类型、所属关系及面片面积；边线卡展示长度。置信度视图及其对象级证据协议处于方案讨论阶段。

开发提交 `7955bac3`，domain-v193 / runtime-v64，已更新同一利苑单文件，SHA-256 为 `98864f91b70005d5acfa024346528c74b204a861c4420ef7c8e070d2ff944836`。三个电脑分辨率的双语点选、逐层切换和 PNG 导出记录见 `D:/Sim-BIM-Agent/cache/sim-bim-floor-review-20261124/delivered-qa.json`。单个跨层贴图部件内的层界三角片另有 4 个镜距/视角的对照，见 `D:/Sim-BIM-Agent/cache/floor-clip-20261124/final-primitive.json`。

## 空间置信度与构件属性

开发提交 `cd3260a1`，登记 domain-v194 / runtime-v65。新建模任务交付前逐项检查空间、边界面和开口的五档空间评估、双语理由及引用。工作区的 `candidate_*/sim_inspection.json` 与 `source_model.json` 的模型摘要绑定，包含各侧材料、共享构件厚度及来源。HTML 按当前选取层级着色，省略内部引用。旧档未评估对象显示灰色。

工作台默认折叠墙体、横向分隔、屋面、玻璃组件四组候选，各组可多选或自填毫米数值。在真实 Windows DSH 上用本地脚本主控和 worker，从浏览器完成存草稿、刷新、Run、自动打开及下载同一 HTML。两间房、12 个边界、2 个开口均有评估。楼板采用输入的 161.5 mm 并标为 inference，任务明确指定的石材标为 from user。证据见 `D:/Sim-BIM-Agent/cache/sim-bim-confidence-20261124/interactive-w0gyo51l/verification.json`。

利苑查看器沿用 `D:/Sim-BIM-Agent/cases/lee-garden-six-full-dsh-sol61-fixed-20261106/private/dsh/work/bim/candidate_03` 的原几何。根据 23/F 原图、立面纹理、剖切及存档推理重新标注了 249/266 个空间、1,617/2,102 个边界面、354/405 个开口。其余对象保留未评估。办公窗带标为观察到的玻璃，24 mm 总厚度标为推断。回顾规则和完整属性存放在 `D:/Sim-BIM-Agent/cache/sim-bim-confidence-20261124/lee-review/`。公开单文件摘要为 `128417279ab9ee8363ce8959cc7a396acef900fa426f7b312493fd244951602b`，浏览器双语点选记录见同级证据目录的 `lee-verification.json`。

默认离线检查 862 项通过。一对多楼板、双侧墙面材料、局部实测厚度、旧档独立墙厚读数、交付失败回滚及并发读取均有专项覆盖。单模型与分工主控也分别通过真实 MCP 工具的本地脚本验收。产品投影已在缓存中构建并核对资源，桌面安装版按约定留待成熟版本同步。另一次扩展工具回归中，历史校准报告的语言断言和参数化测试桩的 regularization 校验在本轮之前的提交上同样失败。

## 构件主材观察

开发提交 `3c1ebb88`，登记 domain-v195 / runtime-v65。表面卡使用 Material 和 Thickness。主材字段允许简短的中英文观察，门扇两侧读取同一条。确定来源优先，构造分层交给下游插件。新属性文件带有 `properties.material_scope=principal_construction`，旧档重审时显式饰面记录作为私有历史保留。

利苑 C03 的 69 扇电梯层门均按井道连接推测为金属门扇。重新核对底层影像，雨篷下的主入口可见玻璃门扇与金属拉手，商铺入口分别描述可辨认的情况。1,302 个构件的厚度、2,507 个边界面和开口的构件绑定、原空间评估均经逐项比对。更新单文件 SHA-256 为 `43070cb2666bcfe9a7ec53f867787c9eab875399f0008cffb5c9377c1a04a3b2`。案例规则、双语鼠标点选和验收输出保存在 `D:/Sim-BIM-Agent/cache/sim-bim-primary-material-20261124/`。

六组定向检查 117 项通过。本地脚本主控及 worker 通过真实工作台和 DSH 完成新任务，2 个空间、12 个边界面、2 个开口均带空间等级和理由，任务明确给出的楼板主材标为 `from user`。验收记录在上述证据目录的 `interactive-20z9479f/verification.json`。
