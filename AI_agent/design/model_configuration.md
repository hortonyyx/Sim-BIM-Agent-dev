# 模型配置

用户配置由 `runtime_configuration.py` 统一校验，产品通过 `sim_bim.py check/run` 使用同一套接口。

| mode | 输入与执行 |
|---|---|
| `single_model` | PNG 或 GLB；主模型直接使用共用工具，mesh 可委派只读观察 |
| `role_division` | PNG 图纸；调度员协调平面与立面读图员 |

`check` 只校验配置与输入。图纸配置的 `roles` 分别绑定 coordinator、plan_reader、elevation_reader；顶层路由须与 coordinator 一致。模型不是建筑算法的一部分，可以按当前已支持的服务与精确型号配置。

服务端点、协议和凭据由 `agent_runtime/connections.py` 解析；型号能力和预算估计由 `model_profiles.json` 与 `estimation.py` 维护。模型出现在在线目录、请求被接受、工具循环成功和建筑质量达标是不同证据。

DeepSeek Flash 当前精确路由：Go 为 `deepseek-v4.1-flash`，Paratera 为 `DeepSeek-V4.1-Flash`，官方为 `deepseek-flash`。思考参数支持 low/high/max；带工具的多轮请求保留完整 reasoning history。网关估算尚未端侧校准时明确标注，不能把本地估算当供应商实报。

配置中的输入和运行目录使用明确路径，凭据通过 `credentials_file` 指定；密钥不写入配置、日志、发布包或 Git。更多操作见[运行案例](../workflow/run_case.md)。
