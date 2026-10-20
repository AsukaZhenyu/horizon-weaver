# v1.3 dev8 反馈与验证开发冒烟

回归 **196/196 通过**，其中 18 项真实 Docker，无跳过。见 regression-docker.log 与 regression-summary.json。

Qwen3.5-9B Q4_K_M；原始 TB 八项任务，每项一次、串行；40 步与 300 秒，IR / JSON Schema。探索历史与行为验证同时启用。

本轮整项 **2/8**，独立检查 **18/30**；Agent 总时间 1713.0s。

按原始任务标签：easy 2/4, medium 0/3, hard 0/1。

| 任务 | 难度 | 外部结果 | 检查 | 运行时结束 | 步数 | Agent 时间 |
| --- | --- | --- | ---: | --- | ---: | ---: |
| hello-world | easy | 通过 | 2/2 | submitted | 3 | 23.5s |
| fix-permissions | easy | 通过 | 1/1 | submitted | 6 | 46.8s |
| log-summary | easy | 失败 | 1/3 | no_progress | 14 | 132.6s |
| regex-log | medium | 失败 | 0/1 | timeout | 28 | 300.9s |
| cancel-async-tasks | hard | 失败 | 1/6 | timeout | 11 | 302.2s |
| jq-data-processing | easy | 失败 | 13/14 | timeout | 18 | 301.5s |
| sqlite-db-truncate | medium | 失败 | 0/1 | timeout | 19 | 304.4s |
| cprofiling-python | medium | 失败 | 0/2 | timeout | 25 | 301.0s |

模型调用 128；解析/截断错误 5；模型超时 11；临时模型服务错误 2。

输入 tokens 462354、输出 tokens 41184；含运行时估算值。

结构化检查 7，其中执行了判定条件的检查 5；错误对照 0；只读历史查询 0。

保存检查引用请求 4，实际执行 0、未知 ID 4；直接 process 检查 3；同条件文件比较复用 0；最新工具回复遗漏 0。请求、执行和收益分别计量。

dev8 修改：优先交付最新工具结果，压缩验证状态与重复说明；按 check_ids 执行已保存检查；增加直接 process 比较、断言操作数和实际调用记录；确定性文件比较复用原来源且不增加证据。仍保持单个 Policy。

历史行动明确作为事实呈现，避免仿照新动作信封；临时模型 HTTP 错误在原预算内反馈。dev7 四项已评分及中断轨迹见 ../v1.3-exploration-r7-20261007/report.md，不纳入本轮八项统计。

前面的 [dev5 四组对照](../v1.3-exploration-r5-20261004/report.md) 使用另一份源码，分别保留。下面只汇总其结果，不能作为 dev8 的同源码消融。

| dev5 配置 | 整项 | 独立检查 | Agent 时间 |
| --- | ---: | ---: | ---: |
| control | 3/8 | 18/30 | 995.2s |
| history | 3/8 | 23/30 | 1178.1s |
| verify | 3/8 | 25/30 | 1614.2s |
| both | 3/8 | 19/30 | 1924.9s |

本轮是单次开发冒烟，不能据此宣称稳定的准确率收益。自检通过不证明需求覆盖，结果只以独立 TB 评分为准；正确候选却超时也分别显示。

具体轨迹分析见 [findings.md](findings.md)：参见本轮轨迹分析与能力采用统计。

审计：任务/兼容文件未改变，初始文件状态与 dev5 完全相同；源码哈希、事件链与 IR 往返通过核对。原始评分、模型响应、检查回执和轨迹保留，详见 both-overview.json 与 both-state-ir/。
