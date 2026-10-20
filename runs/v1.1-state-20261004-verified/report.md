# v1.1 状态机制开发对照

使用固定历史轨迹和 ScriptedPolicy，工具对照真实执行本地文件操作。此结果只验证机制行为，不代表真实模型任务成功率或 Terminal-Bench 得分。源代码指纹：`2d7982d1353813de760c566ef25663a4f3d100ab7bf3e121d7f957da7d95d8ce`。

| 指标 | recent 或关闭 guard | state 或开启 guard |
| --- | --- | --- |
| 输入 UTF8 字节上界 | 12945 | 10053 |
| 旧关键观察保留 | True | True |
| 实际 READ 和 WRITE 调用 | 4 | 2 |
| 产物内容 | 42 | 42 |
| 运行终态 | submitted | submitted |

输入上界在此历史噪声场景降低 22.34%。字节上界来自既有保守计量方法，不是实际模型 token；固定 Policy 的决策步数保持相同。外部验收保持待评分，没有将提交认定为成功。

原始上下文、固定轨迹、完整事件、IR 导出和源码副本均保存在本目录。Docker 和真实模型验证未在这个脚本中执行。

另行执行全部 95 项检查通过，包含 7 项真实 Docker，无跳过项。日志为 tests-full-docker.txt，源码一致性核对与未执行项目在 verification.json 中记录。新 CLI 的 IR 导出与只读重放相同；三份历史 v1.0 运行可以导出 IR，未重新评分。
