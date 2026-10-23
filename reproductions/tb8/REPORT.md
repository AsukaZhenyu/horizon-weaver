# 固定TB8：R13、原生Terminus2与mini

本次有效整任务通过为 **R13 4/8、Terminus2 5/8、mini 3/8**；正常提交且通过为 **2/8、4/8、3/8**。Terminus2在这组任务上通过更多；mini的较短探索时间不能解释为同等完成度下更快。

这是反复用于开发的本地TB 0.2.18固定八任务比较，非完整榜单、保留集或严格随机种子复现；核心TypeScript未改，未加入Task IR、Cordis、新任务提示或人工解题策略。

## 汇总

| 指标 | R13（2026-10-11） | Terminus2 r4 | mini r3 |
|---|---:|---:|---:|
| 有效整任务通过 | 4/8（50%） | 5/8（62.5%） | 3/8（37.5%） |
| 正常提交且通过 | 2/8 | 4/8 | 3/8 |
| 通过checks | 25/30 | 26/30 | 13/30 |
| 探索/Agent合计秒 | 1784.0737 | 1382.0826 | 1366.2439 |
| 独立安装秒 | 未分列 | 无独立安装阶段 | 1355.0019 |
| 原run墙钟秒 | 未按同口径统计 | 2171.2043 | 3530.7200 |
| 调用 | 128 gateway calls | 85 HTTP attempts | 104 HTTP attempts |
| 输入 / 输出tokens | 477416 / 34105（混合实际与估算） | 318396 / 21932（known） | 588926 / 33610（known） |
| usage未知请求 | 混入部分估算 | 6 | 3 |

Terminus2与mini的未知usage分别来自6、3次HTTP错误，known总量340328、622536不能当完整消耗。恢复评分另耗68.6979、67.2604秒，新增Agent/模型调用均为0；这些时间不计入探索。mini安装与探索分列，`harness_agent_seconds`包含安装，不能拿它与300秒探索预算直接比较。

## 逐任务

下表每格顺序均为 **R13 · Terminus2 · mini**；调用单位见汇总，秒数为各自Agent/探索时间。

| 任务 | checks | 正常submit | Agent/探索秒 | 调用 |
|---|---|---|---|---|
| hello-world | 2/2 · 2/2 · 2/2 | 是 · 是 · 是 | 26.8 · 16.6 · 15.1 | 3 · 3 · 4 |
| fix-permissions | 1/1 · 1/1 · 1/1 | 是 · 是 · 是 | 79.2 · 26.1 · 24.8 | 8 · 4 · 8 |
| log-summary | 3/3 · 3/3 · 3/3 | 否 · 是 · 是 | 205.9 · 39.0 · 40.0 | 21 · 5 · 14 |
| regex-log | 0/1 · 0/1 · 0/1 | 否 · 否 · 否 | 301.1 · 300.3 · 300.8 | 15 · 9 · 4 |
| cancel-async-tasks | 5/6 · 6/6 · 3/6 | 否 · 否 · 否 | 301.0 · 300.4 · 301.3 | 25 · 23 · 12 |
| jq-data-processing | 14/14 · 14/14 · 3/14 | 否 · 是 · 否 | 268.1 · 99.1 · 82.4 | 25 · 12 · 21 |
| sqlite-db-truncate | 0/1 · 0/1 · 0/1 | 否 · 否 · 否 | 301.0 · 300.4 · 301.1 | 18 · 16 · 15 |
| cprofiling-python | 0/2 · 0/2 · 1/2 | 否 · 否 · 否 | 301.0 · 300.3 · 300.7 | 13 · 13 · 26 |

“正常submit”指原运行明确完成提交，与原tests通过分开判断；R13的log/JQ通过却以`no_progress`结束。Terminus2四项提交均通过，另有超时后通过的async；mini仅hello/fix/log三项提交，三项均通过。

## 原生评分与恢复边界

Terminus2 raw报告4/8但只有6项有效评分，SQL/hello为`parse_error`；mini raw报告1/8但也只有6项有效评分，log/fix为`parse_error`，有效原生checks为9/26。独立`effective-*`只覆盖各自两个原无效评分，原raw字节未改；没有重跑Agent、跨run择优或替换有效失败评分。

- Terminus2 SQL：完整终端`cat`、380B大小与执行的`json.dump`支持重建唯一评分输入`recover.json`，原tests恢复0/1；无原tar或原文件SHA观测，不能称完整文件系统/原哈希恢复。
- Terminus2 hello：原捕获完整`/app` tar及Unix元数据恢复，原tests得2/2；捕获在Agent摘要之后，不能断言等于未观测的结束瞬间，也不是整个容器文件系统。
- mini log/fix：原grader在uv下载TLS失败后没有运行pytest；固定镜像恢复原评分前完整`/app` tar，逐项核文件SHA与Unix元数据，原tests源码SHA及候选评分前后均不变，得到3/3与1/1，均退出0且checks完整、无超时，恢复容器均已删除。
- R13 SQL/profiling同样有结束后候选恢复评分，两者仍失败、无额外Agent尝试；恢复成绩与不中断的原生评分保持区分。

## 失败轨迹与通过范围

- **SQL：** mini未生成`recover.json`，原tests止于缺文件；解析过程中已有UTF-8/记录边界错误。请求14输入16236＋输出148＝16384、`length`并截断工具参数，支持物理context顶满；末请求超时，无明确context超限400。Terminus2则写出错误数据，把ASCII `b'01'`按大端整数解成12337，恢复评分仍0/1。
- **regex：** mini仅执行ls及写/运行自测；第3请求229.28秒，输入6050＋输出8192＝14242＜16384，撞8192输出上限，重复注释中的工具参数截断触发`FormatError`，没有`regex.txt`。Terminus2留存的regex是Python不支持的变长lookbehind，自测已报错但只改测试脚本，原tests同样编译失败；首尾HTTP超时共150.57秒，未见其context满。
- **JQ：** mini早期预览命令先输出对象流，下一轮已改数组且成功；后来保存带重定向的`cmd.sh`却没有执行。实际wire的tool content含内嵌JSON字面`\u0027`（官方`tojson`），模型误判为文件损坏而反复重写，实际文件是普通0x27。21次均200/tool_calls、最高prompt12508，无context/格式截断；累计known 207125触发观察预算，缺report，故3/14。
- **async：** mini三个SIGINT检查均先在`Task started.`应2次、实际0且stdout空处失败，stderr未展示，原评分机制未能具体定位。自测错误地让gather的兄弟协程抛取消异常，未取消`run_tasks`，三轮cleanup_count=0后仍重复同实现；这不是收到相同原grader反馈。R13最终477B候选的signal回调参数与注册不符，有明确TypeError；Terminus2为6/6，但仍约300秒、无提交。
- **profiling：** mini速度断言通过（23.0308 / 6.4807秒，约3.55倍，实际条件为2–10倍），图片检查却在fast目录8/9图、缺id3的SSL失败及组合hash不符处停止；脚本吞下载异常仍退出0，fastest评分前还残留8图，不能称完整等量工作下加速达标。Terminus2的原tests也遇SSLEOF，0/2无法评判其加速。

log由R13的205.9秒/21calls、无正常收尾，变为Terminus2的39.0秒/5calls与mini的40.0秒/14calls并正常提交；Terminus2的JQ为99.1秒/12calls并通过。其log/JQ单次最大输出215/290 tokens，均未绑定8192上限，不能把改善归因于输出额度放宽。

## 比较条件与限制

共同条件为固定八任务及原tests、同一Qwen3.5-9B-Q4_K_M文件、物理context16384、temperature0.2、单并发、每任务一次Agent尝试及300秒探索。历史seed/部分后端采样未显式固定，R13逐任务镜像身份不能完全证明一致。

| 条件 | R13 | Terminus2 | mini |
|---|---|---|---|
| 40单位 | typed decisions | episodes，可含多条命令与完成确认 | 官方step limit；重试HTTP另计 |
| 单次输出 | 默认2048，可升8192 | ceiling8192 | ceiling8192 |
| 单模型调用 | 默认60秒，可升120 | HTTP最多90秒且受剩余预算约束 | 客户端300秒；socket使用剩余探索时间 |
| 上下文 | state/IR预算14000 | 原生对话/context recovery | 原官方对话策略 |
| 外层Agent时限 | 原历史执行口径 | 330秒 | 630秒＝安装300＋探索300＋余量30 |

mini单请求可占几乎整个探索预算；socket timeout并非精确端到端硬截止。累计200000 tokens是观察后停止阈值，最后一响应可跨过阈值；R13估算与native known/unknown也不等价。以上交互、上下文及预算语义共同变化，不能作单模块因果结论。

重复指标不统一：Terminus2是16次exact session-key重复；mini **5/8任务完整重复数未知**，其余3项已知0，不能写总重复0。mini统计提出的命令（含可能未执行的提交指令），R13是typed action/文件状态；无法可靠统一成全部实际shell执行重复率，重复字符串也不必然无进展。

## 完整性与证据

mini最终审计验证8个真实固定镜像ID、55个原task文件、8项进程清理及4份executed-input snapshot/current hashes；policy六文件哈希只有 **7/8** 保留且一致。profiling原Compose仅挂`/logs`、未挂`/agent-logs`，安装metadata丢失，但轨迹、HTTP、安装stdout/stderr、budget-summary与最终process状态保留，不能补称8/8源码证明。

当前run启动仅在本进程将Compose up设为`--no-build --pull never`，policy前核实际client镜像ID；初次mini在任务模型请求前中断、SQL tag意外重建及恢复、r2 tag规范化失败均留档，正式成绩只取r3。最终清理审计记录0残留容器/端口及模型PID已退出。

- 机器摘要：[Terminus2](results/terminus2-r4-summary.json)、[mini](results/mini-r3-summary.json)，含来源SHA、逐任务数据与恢复scope。
- mini：`_work/tb8/runs/mini-20261023-fixed8-resumed-r3/effective-results.json`、`effective-summary.json`；原native JSON独立保留，两份aggregate语义相同但字节SHA分别固定。
- 恢复proof：各run的`grading-recovery/<task>/result.json`；Terminus2 SQL另见`artifact-recovery/sqlite-db-truncate/manifest.json`及`grading/result.json`。
- 失败证据：各任务`agent-logs/transport.jsonl`、`mini.traj.json`、HTTP request/response、`panes/post-test.txt`及评分前`grading-transport/app-before.tar`；mini审计见`_work/tb8/analysis/mini-r3-final-integrity.json`、`mini-r3-root-verification.json`与`mini-r3-cleanup.json`。
- R13：主仓`runs/v1.3-goal-r13-20261011/effective-overview.json`、候选tar及原post-test；模型、源码与协议限制见`protocol.json`和README。
