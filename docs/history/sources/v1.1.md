# v1.1 Terminal-Bench 八项冒烟测试

2026-10-04，本地 1.1.0.dev1 一次尝试、串行运行 8 项，原始隐藏评分 **2/8 通过（25%）**。隐藏检查共 22/30 通过；任务需要全部检查通过才计为通过。

| 任务 | 结果 | 隐藏检查 | 决策步数 | 运行耗时 | 运行时结束 |
| --- | --- | --- | --- | --- | --- |
| hello-world | 通过 | 2/2 | 3 | 26.2s | submitted |
| fix-permissions | 未通过 | 0/1 | 4 | 32.5s | submitted |
| log-summary | 通过 | 3/3 | 7 | 61.4s | submitted |
| regex-log | 未通过 | 0/1 | 3 | 300.1s | timeout |
| cancel-async-tasks | 未通过 | 4/6 | 6 | 125.0s | timeout |
| jq-data-processing | 未通过 | 13/14 | 8 | 110.4s | submitted |
| sqlite-db-truncate | 未通过 | 0/1 | 17 | 174.3s | no_progress |
| cprofiling-python | 未通过 | 0/2 | 7 | 97.8s | timeout |

运行耗时包含模型、工具和状态处理，排除容器准备、隐藏评分及清理。运行时正常提交 4 项；模型截止超时 1 项、工具通信超时 2 项、无进展停止 1 项。外部评分始终独立于运行时提交。

- **fix-permissions**：只通过 bash 执行脚本，没有设置执行位；自检未验证直接执行权限。
- **regex-log**：模型响应协议失败及 8192-token 截断后耗尽时间；最终正则匹配了无效日期和多余日期。
- **cancel-async-tasks**：取消相关检查通过；普通运行时递归取消报 RecursionError，两个并发检查子进程超时。模型自检另有外层 Docker 工具通信超时。
- **jq-data-processing**：转换及命令复现通过；将 --indent 2 拆成两个 YAML 元素，违反旗标和值必须合为一个字符串的要求。
- **sqlite-db-truncate**：反复执行同一个失败的 SELECT 查询，无进展停止；未生成 recover.json。
- **cprofiling-python**：两份脚本均串行；补装 imagehash 时工具通信超时。隐藏评分中的 Python 缺少 requests，profile 执行失败，未获得有效图片。

配置：Qwen3.5-9B Q4_K_M，llama.cpp b8705，thinking off，temperature 0.2；上下文 16384，单并行 slot；terminal policy、structured_output=none、context_strategy=state、transition_guard=true。每项 40 步、300 秒、8192 输出 token；单工具 30 秒、隐藏评分 180 秒。所有失败项保留，未重跑模型。

8 项均取得全部预期隐藏检查，评分器无安装失败或整体评分超时。事件校验链、只读重放、State IR 序列化和关系验证全部通过；同一源码、归档源码和 8 份 manifest 指纹一致，任务文件及已有 Windows harness 兼容修改前后哈希一致。

源码指纹：`2d7982d1353813de760c566ef25663a4f3d100ab7bf3e121d7f957da7d95d8ce`。本轮有 0 次 Transition Guard 拒绝；当前文件动作 guard 没有阻止 SQLite 的重复 shell 查询。此结果验证状态记录与重放，不证明成功率或性能收益；尚无同配置 v0.x、recent 或 guard 关闭的真实模型对照。

基础设施记录：Buildx 元数据探测挂起，改用 Docker 内置构建器按原始 Dockerfile 构建缺少的 3 个镜像；异步任务 Compose up 自动重试一次。regex 和异步任务在隐藏评分日志落盘后手动清理挂起的 Compose down，未重跑或改变评分。8 项最终均得到完整评分。两个工具通信超时的执行终止未获确认，按运行失败保留；不能把全部失败都归为模型问题。

下一轮修复优先级：动作协议与截断恢复；从公开任务要求生成有覆盖范围的自检；重复失败后的策略切换；工具截止与 Docker 内执行终止确认。性能收益仍需增加真实模型的同条件开发对照。

本轮容器和遗留空网络已清理，所启动模型进程已停止；任务镜像保留以便复现。原始 Terminal-Bench 0.2.18 工作区含既有 Windows 兼容修改，修改已归档，评分脚本未改。本轮只覆盖选定 8 项冒烟任务。

记录：[完整结果](overview.json)、[配置、模型和任务哈希](provenance.json)、[原生评分](tb/smoke8/results.json)、[运行日志](runner.log)、[基础设施操作](infrastructure-actions.jsonl)、[清理确认](cleanup.json)。事件与原始响应位于各 trial 的 agent-logs/horizon-v1；State IR 与完整动作记录位于 state-ir/；源码位于 code/；已有 benchmark 兼容修改位于 benchmark-compatibility/。
