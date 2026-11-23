# 本地建模工作台

开发提交 `4235b6f8`，domain-v186 / runtime-v60。Horton 的本机入口为桌面 `Sim-BIM 工作台.lnk`，使用开发库的 Python 执行 `sim_bim.py workbench`，在默认浏览器打开仅绑定本机的服务。发行包的 `Start.cmd` 在双击时打开同一项工作台。

- 拖入 PNG、GLB 或建筑说明 JSON 后，添加按钮把文件复制到工作区的 `materials/<block>/<slot>/`。编辑、排序、删除引用和总说明保存在版本化的 `project.json`。运行时保存独立的 `runs/<run>/input.json` 和素材字节，保留原名并为重名文件分配可供模型调用的名称。
- 主控与可选 worker 可使用 ChatGPT 订阅和 DeepSeek 官方 API。本机实际查询到六个管线支持的 ChatGPT 型号以及 DeepSeek Flash。DeepSeek 凭据经当前 Windows 用户 DPAPI 保护。运行配置只保存模型选择。UI 使用账号目录与当前可执行型号的交集。
- 运行语言贯穿主控、worker、交付报告与三维查看器。查看器继续提供查看、剖切、量测等操作。本轮需求聚焦录入，用户尚待定义最终模型的交互修改方式。
- Chrome 验收覆盖桌面和窄屏、拖放三种素材、同名文件排序、纯文字组、刷新恢复、草稿冲突和切换语言；测试工作区与实际项目分开。工作台测试覆盖从 HTTP 导入到冻结与生产配置。DeepSeek 的 DSH 执行用本地模型响应验证工具调用、续跑和七项交付摘要，实际账号验证为模型目录查询。此轮未用账号生成新的整栋案例。
- 最终快速套件通过 856 项。开发版浏览器入口返回 HTTP 200。中英文交付在实际结果路由的沙箱内验证了楼层筛选、三维点选、下载 PNG、打开图纸回叠和查看源记录。
- 候选包已从 `26959502` 构建并含 254 个文件。原桌面目录的 Python 进程为 Windows Low 完整性，实际不能向普通项目目录或用户级验收目录写入。桌面更新器已将旧安装恢复到 `c12b75a7`，在 LocalLow 的隔离验收通过；记录在 `%USERPROFILE%/AppData/LocalLow/Sim-BIM-Agent/installation-verification/fc70710f97c345a79634b00459773f12/report.json`。新增快捷方式使用工作库的 Python，保留原安装权限。
