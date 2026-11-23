# 本地建模工作台

开发提交 `4235b6f8`，domain-v186 / runtime-v60。桌面双击 `Start.cmd` 或在开发库执行 `python sim_bim.py workbench`，在默认浏览器打开仅绑定本机的服务。

- 拖入 PNG、GLB 或建筑说明 JSON 后，添加按钮把文件复制到工作区的 `materials/<block>/<slot>/`。编辑、排序、删除引用和总说明保存在版本化的 `project.json`。运行时保存独立的 `runs/<run>/input.json` 和素材字节，保留原名并为重名文件分配可供模型调用的名称。
- 主控与可选 worker 可使用 ChatGPT 订阅和 DeepSeek 官方 API。本机实际查询到六个管线支持的 ChatGPT 型号以及 DeepSeek Flash。DeepSeek 凭据经当前 Windows 用户 DPAPI 保护。运行配置只保存模型选择。UI 使用账号目录与当前可执行型号的交集。
- 运行语言贯穿主控、worker、交付报告与三维查看器。查看器继续提供查看、剖切、量测等操作。本轮需求聚焦录入，用户尚待定义最终模型的交互修改方式。
- Chrome 验收覆盖桌面和窄屏、拖放三种素材、同名文件排序、纯文字组、刷新恢复、草稿冲突和切换语言；测试工作区与实际项目分开。工作台测试覆盖从 HTTP 导入到冻结与生产配置。DeepSeek 的 DSH 执行用本地模型响应验证工具调用、续跑和七项交付摘要，实际账号验证为模型目录查询。此轮未用账号生成新的整栋案例。
