# 运行与检查案例

产品入口为 `sim_bim.py`。输入使用独立案例目录，输出使用新的运行目录。输入只能含本次授权的原始资料及用户声明。

## 准备

图纸使用 `product/config.example.json`；mesh 使用 `product/config.mesh.example.json`。设置输入、输出、案例名、模型和调用保护线。图纸修改模型时同步三个角色配置；凭据由明确的私有文件提供。

```powershell
python sim_bim.py doctor
python sim_bim.py check D:/Sim-BIM-Agent/configs/my-run.json
python sim_bim.py run D:/Sim-BIM-Agent/configs/my-run.json --case my-building
```

`doctor` 与 `check` 不发送推理请求。`run` 使用配置中的服务进行实际建模。产品验收从桌面安装启动，不借用开发库模块。

## 恢复与排障

同版本、同配置和同输入的中断任务可追加 `--resume`。保留原日志和失败证据，使用新输出名进行新尝试；不能编辑检查点或候选来伪造运行成功。版本不兼容时使用原版恢复，或以原始输入在新版开始新运行。

程序错误按输入、配置、服务连接、模型响应、工具执行和交付分别定位。真实服务失败后只按本次授权的线路顺序切换，不能暗中换模型。

## 验收记录

记录版本、实际模型/线路、输入哈希、终态、完整交付路径、用时、请求与用量、失败和人工干预。完成验收需检查 Sim-BIM JSON、查看器及正式交付记录实际存在且能读取。

运行完整性与建筑质量分别评价。本次用户若只要求跑通，不能把模型结果不理想自动扩大为质量优化实验，也不能把有效输出说成建筑质量通过。
