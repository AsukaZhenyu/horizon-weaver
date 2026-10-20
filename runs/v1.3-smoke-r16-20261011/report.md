# v1.3 dev14：完整八项 TB 冒烟测试

源码 `917790938c3c1d01853272b85839729fcb141ccaffb3298f5bd54a0f800319a1`。本轮未修改实现；逐文件确认源码与 R15 相同，沿用该源码的 248/248 回归（含 19 项真实 Docker），没有重新执行回归。

Qwen3.5-9B Q4_K_M、16k、IR/json_schema、历史探索和 enhanced Verify；每项一次、40 步/300 秒、串行。原始 TB 任务和测试、模型权重及参数、初始快照与 dev12 相同。

评分来源：7 项原生评分，1 项完整一致候选的原测试评分恢复；没有额外 Agent 尝试。原生失败和恢复日志分别保存。

| 指标 | dev12 / R13 完整八项 | dev14 / R16 完整八项 |
| --- | ---: | ---: |
| 整项通过 | 4/8 | 4/8 |
| 检查通过 | 25/30 | 25/30 |
| 正常提交且整项通过 | 2/8 | 0/8 |
| Agent 总耗时 | 1784.1s | 2358.5s |
| 模型调用 | 128 | 139 |
| 同动作、同文件起始状态再次提议 | 32 | 26 |
| 同文件起始状态且相同后果重复 | 30 | 18 |
| 相同后果的失败重复 | 21 | 6 |
| 零变化写入提议 | 22 | 6 |
| 被 Verify 阻塞的提交 | 0 | 1 |

| 任务 | dev12 检查 / 结束 | dev14 检查 / 结束 | dev12 时间 | dev14 时间 |
| --- | --- | --- | ---: | ---: |
| hello-world | 2/2 / submitted | 2/2 / no_progress | 26.8s | 250.6s |
| fix-permissions | 1/1 / submitted | 1/1 / timeout | 79.2s | 300.9s |
| log-summary | 3/3 / no_progress | 3/3 / no_progress | 205.9s | 291.6s |
| regex-log | 0/1 / timeout | 0/1 / timeout | 301.1s | 300.6s |
| cancel-async-tasks | 5/6 / timeout | 5/6 / timeout | 301.0s | 304.6s |
| jq-data-processing | 14/14 / no_progress | 14/14 / timeout | 268.1s | 302.7s |
| sqlite-db-truncate | 0/1 / timeout | 0/1 / timeout | 301.0s | 303.9s |
| cprofiling-python | 0/2 / timeout | 0/2 / timeout | 301.0s | 303.5s |

实际执行顺序：

- dev12：cancel-async-tasks → jq-data-processing → regex-log → log-summary → fix-permissions → hello-world → sqlite-db-truncate → cprofiling-python
- dev14：regex-log → fix-permissions → jq-data-processing → cprofiling-python → log-summary → cancel-async-tasks → hello-world → sqlite-db-truncate

整项通过依据原始 TB 外部检查；正常提交且通过还要求运行时以 submitted 结束。产物通过但超时/无进展结束不计入正常提交。

耗时为各 Agent 时间之和，不含容器准备、外部依赖安装、评分和评分恢复。重复指标按同一算法重算两轮，包含被保护而未执行的提议；文件相同不证明网络/进程条件相同，动态输出也可能掩盖语义重复。

本轮是新的完整八项，未与 R14/R15 定向试验挑选拼分。相同源码的单次结果仍受模型采样和执行顺序影响；认识更新或 Verify 提示送达不证明策略采用或目标覆盖。逐项原因见 findings.md。
