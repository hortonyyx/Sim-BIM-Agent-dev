# 模型使用约定

产品把模型、服务线路、角色和工具分别配置。当前默认模板使用订阅路径；Go、Paratera、官方 DeepSeek 和其他已接入服务的实际限制由各自 adapter 与精确型号合同处理，不共享未经核实的参数或价格。

真实模型调用按用户当前任务执行。当前已授权的发布验收为 DeepSeek Flash：sm24 图纸、利苑 mesh，优先 OpenCode Go，失败后依次尝试 Paratera、官方 API。程序与交付问题应修复；本次不开展模型比较或建筑质量优化。

一般新增实验先明确输入、实际模型、线路、模式和调用保护线。不得由 pytest、构建或依赖安装自动触发推理。目录查询与真实推理分别记录，不能把目录可见说成任务已跑通。

每次实际运行保留供应商实报用量、请求失败、完成状态和交付路径。未知费用保持未知；Go 订阅用量不套其他线路单价。切换服务使用新运行身份，不在原日志中悄悄替换模型或补写完成记录。

[配置说明](../design/model_configuration.md) · [OpenCode Go](opencode_go.md) · [ChatGPT 授权](chatgpt_subscription_runtime.md)
