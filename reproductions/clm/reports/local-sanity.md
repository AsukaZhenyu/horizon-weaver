# CLM 本机单项复现结果

固定官方任务 kv-store-R1000-seed42 已跑出有效外部评分：**16/24（66.7%），partial**。已回答16题全部正确；交付27/34个操作后触发 budget_wall，任务未完成、没有正常提交。进程 exit0 只表示 Harbor 正常结束并完成评分。

| 项目 | 原生结果 |
| --- | ---: |
| 官方 grader_valid | 1 |
| 回答 / 正确 / 总查询 | 16 / 16 / 24 |
| 已交付操作 | 27 / 34 |
| 模型调用 / bash | 43 / 43 |
| 实际上下文编辑 | 10 |
| 输入 / 输出 token | 332439 / 4815 |
| 总 token / 其中 cached prompt | 337254 / 223098 |
| Agent 墙钟 | 255.28 秒 |
| Harbor trial 墙钟 | 275.95 秒 |
| 含源校验、评分、清理的 controller 墙钟 | 363.82 秒 |
| 官方逐调用 LLM / bash 时间之和（已四舍五入） | 201.4 / 16.7 秒 |

43次调用的 usage 均完整、没有隐藏重试。美元成本未知；上游未定价本地模型输出的0.0不是实测计费。原生评分见 native-reward.json 和 native-grader-summary.json；去除轨迹正文的汇总见 local-sanity.json。

## 真实上下文编辑证据

10次有效编辑全部发生在SET阶段的step1..10；GET阶段0次。模型将每批SET存到容器内 /tmp/ctx_offload/kv-NNNN.txt，再编辑真实 LIVE_CTX_MAIN.txt，保留 offloaded 标记。

第一轮官方receipt记录上下文7853→2958 token。下一次真正请求的turn-0001.json中，原批次完整正文已消失，offloaded标记存在，system/task前缀保持完全一致；新批次已加入，所以该下一请求总量8049不能当作刚编辑完的2958。real-edit-next-request.json保存原生命令、receipt、正文hash及这些断言，不包含整批数据或gold内容。

## budget_wall 的客观触发

生效配置是Agent预算12288、reserve2048，门槛10240；服务器窗口16384。官方BudgetController默认用服务端prompt token校准tiktoken计数，并非存在8192的隐藏门槛。

最后一次请求：原生o200k计数7947，服务端prompt_tokens10567，比例10567/7947=1.3296841575。最终原生上下文8056，floor(8056×该比例)=10711，精确吻合官方timing的step44 budget_stop。step43先在10485触发一次final_turn；模型在这次机会中执行grep查值，没有压缩上下文，随后停止。grader的peak8133是原生o200k峰值，与校准后的gate不是同一单位。

这是现有轨迹的观察：SET完成后查询/回答历史继续增长，GET阶段没有编辑。它不证明某种结构优于另一种，也不能单凭这一条轨迹断言架构的因果问题。配置、所有源、任务与策略均保持固定，没有重试、换题或调prompt。

## 范围、准备与保留的失败

这是单个官方原生任务的工程复现，不是论文分数或benchmark总体准确率。使用本地Qwen3.5-9B Q4/llama.cpp、12K Agent预算、2048输出、thinking关闭，与论文模型、32K预算等设置不同。源SHA、许可、106包依赖、22项任务/scorer哈希、模型和服务二进制哈希均已记录。

官方Harbor容器上下文roundtrip、完整CLM/LiteLLM loopback HTTP transport、成功/503不重试、超时定向清理、未知usage、数据完整性检查均通过。它们是机制测试，不计入模型成绩。

首目录20261023T065101668321Z在LiteLLM可选mcp导入处、模型HTTP之前失败，原日志保留并单列infrastructure-attempt.json。同版本官方mcp extra修复后，新目录20261023T071537962591Z产生本报告的真实模型结果；原始日志、所有request snapshots、usage和grader仍在忽略目录 _work/clm/runs/。本次模型服务及容器已由controller最终清理，未扩大测试。
