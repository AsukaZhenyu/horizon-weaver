# dev7 中断开发冒烟

此批在四项已评分后中断，另有 log-summary 未评分轨迹，其余任务未开始。不能当作完整八项得分。

| 任务 | 独立检查 | 结束 |
| --- | ---: | --- |
| regex-log | 0/1 | timeout |
| cprofiling-python | 0/2 | model_error |
| fix-permissions | 1/1 | submitted |
| hello-world | 2/2 | submitted |

中断原因：cprofiling-python 的模型调用收到 HTTP 500；模型服务记录了含 execution_signature 的非法输出，外形对应历史行动 JSON。dev8 改变历史事实呈现并增加受原预算限制的临时模型错误反馈。

原始评分、源码、模型日志、事件和未完成轨迹完整保留。仅清理了该批拥有的进程、容器及空网络；见 interruption.json / interruption-cleanup.json。

dev8 完整八项另存于 ../v1.3-exploration-r8-20261007/，两批不择优合并。
