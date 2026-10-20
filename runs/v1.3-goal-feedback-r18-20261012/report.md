# v1.3 dev16：持续目标反馈与完整八项 TB 冒烟测试

源码 `c74876be99088b1a48e018be50e2728a8d40779a5657990867159ecbcc0fbbc5`。测试前完成新的 268/268 回归（含 19 项真实 Docker，无跳过），八项运行期间源码固定。

Qwen3.5-9B Q4_K_M、16k、IR/json_schema、历史探索和 enhanced Verify；每项一次、40 步/300 秒、串行。原始 TB 任务和测试、模型权重及参数、初始快照与 dev14/R16 相同。

评分来源：8 项原生评分，0 项完整一致候选的原测试评分恢复；本完整批次每项一次 Agent，评分无追加 Agent 尝试。此前 R17 的中止诊断另列，不参与本表。

| 指标 | dev14 / R16 完整八项 | dev16 / R18 完整八项 |
| --- | ---: | ---: |
| 整项通过 | 4/8 | 3/8 |
| 检查通过 | 25/30 | 20/30 |
| 正常提交且整项通过 | 0/8 | 3/8 |
| Agent 总耗时 | 2358.5s | 1744.9s |
| 模型调用 | 139 | 122 |
| 同动作、同文件起始状态再次提议 | 26 | 13 |
| 同文件起始状态且相同后果重复 | 18 | 13 |
| 相同后果的失败重复 | 6 | 0 |
| 零变化写入提议 | 6 | 5 |
| 被 Verify 阻塞的提交 | 1 | 0 |

| 任务 | dev14 检查 / 结束 | dev16 检查 / 结束 | dev14 时间 | dev16 时间 |
| --- | --- | --- | ---: | ---: |
| hello-world | 2/2 / no_progress | 2/2 / submitted | 250.6s | 28.7s |
| fix-permissions | 1/1 / timeout | 1/1 / submitted | 300.9s | 35.6s |
| log-summary | 3/3 / no_progress | 3/3 / submitted | 291.6s | 296.4s |
| regex-log | 0/1 / timeout | 0/1 / timeout | 300.6s | 303.5s |
| cancel-async-tasks | 5/6 / timeout | 1/6 / timeout | 304.6s | 300.7s |
| jq-data-processing | 14/14 / timeout | 13/14 / submitted | 302.7s | 171.8s |
| sqlite-db-truncate | 0/1 / timeout | 0/1 / timeout | 303.9s | 306.8s |
| cprofiling-python | 0/2 / timeout | 0/2 / timeout | 303.5s | 301.4s |

实际执行顺序：

- dev14：regex-log → fix-permissions → jq-data-processing → cprofiling-python → log-summary → cancel-async-tasks → hello-world → sqlite-db-truncate
- dev16：cancel-async-tasks → regex-log → sqlite-db-truncate → hello-world → fix-permissions → log-summary → jq-data-processing → cprofiling-python

整项通过依据原始 TB 外部检查；正常提交且通过还要求运行时以 submitted 结束。产物通过但超时/无进展结束不计入正常提交。

耗时为各 Agent 时间之和，不含容器准备、外部依赖安装、评分和评分恢复。重复指标按同一算法重算两轮，包含被保护而未执行的提议；文件相同不证明网络/进程条件相同，动态输出也可能掩盖语义重复。

本轮将未核实的自检预期从隐式提交门槛改为带来源的反馈。正常提交改善可能来自门槛变化，不能据此声称准确率提高；仍以相同原测试衡量产物。未知覆盖不记为通过。

dev15/R17 在发现旧提交阶段标签矛盾后提前停止，仅保留为未完成诊断。修复并重新完整回归后，dev16/R18 的八项全部重新开始；本表只使用 R18 完整批次，没有混合 R17 结果。

本轮是新的完整八项，未与其他定向试验挑选拼分。单次结果受模型采样和执行顺序影响；反馈送达不证明策略采用或目标覆盖。逐项原因见 findings.md。
