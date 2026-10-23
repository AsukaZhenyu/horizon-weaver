# MAGE math1：本机工程冒烟结果

**不可信准确率：第4子题没有提交答案，却被本地 LLM judge 判对。** 本轮不能把原始 4/5 判分当作可信准确率，也不能把“非空且被该 judge 判对”的 3/5 当作客观正确率。

固定 task ID 0，包含5个连续子题；只运行一次预算受限的本机测试。求解和独立评分进程均正常退出，运行链路完成；原生“最后子题通过”的任务成功指标为 **0/1**。

| 项目 | 结果 |
| --- | --- |
| 原始逐子题 LLM 判分 | [1, 1, 1, 1, 0] |
| 非空提交 mask | [1, 1, 1, 0, 1] |
| 非空且 LLM 判对的交集 | 3/5，仅审计指标 |
| 原生最后子题 SR | 0/1 |
| 所有子题通过 | 否 |
| Solver / judge 调用 | 40 / 5，均成功返回 |
| Solver tokens | 380249 输入 + 20118 输出 = 400367 |
| Judge tokens | 32457 输入 + 297 输出 = 32754 |
| 合计 tokens | 433121；无未知 usage 调用 |
| 总墙钟时间 | 712.172 秒 |
| 768-token 边界命中 | Solver 19/40 |
| 256-token 边界命中 | Judge 0/5 |

## 判分审计

第4子题预测长度为0，参考答案长度为121字符。原始 judge 回应为 CORRECT，并描述了一个实际未提交的匹配答案。原始判分和提示词未修改、未重评、未回灌求解器。只提交长度、SHA256和判分概要，原始参考答案、预测及完整 judge 回应留在忽略的原始运行目录。

证据见 [完整非私有概要](results/math1-sanity.json)。该交集中的其余3项仍只是本地模型的判断，并未由独立可执行证明或人工复核确认为正确。

## 输出预算与实际机制

原后端没有保存 finish_reason。因此19次输出恰为768 token仅称为预算边界命中，不直接认定为 length 截断。对保存输出调用原始解析器：37次动作请求中，18次被识别为动作，19次无法识别；另有3次无参考答案的自检调用。19次边界命中与19次无法识别动作一一对应。这是本机配置下的观察，不能据此证明放宽预算就能答对。

识别出的动作是 Think 10、SymbolicReasoning 4、Finish 4；没有 Store 或模型主动 Backtrace。最终树有5个节点，summary层只有根节点。因此本次真实模型轨迹未展示MAGE的压缩收益。离线原始 Store/Backtrace 机制检查另行通过，不与模型成绩混合。

## 资源与偏离

本机 Qwen3.5-9B Q4 / llama.cpp，16384上下文、单并发、thinking关闭；模型和server二进制SHA均记录于结果概要。GPU时隙由主控制器统一分配，本轮完成后立即释放，没有新增服务或付费API调用。

每子题每次尝试8步，保留原有一次无gold自检重试；solver输出768、judge输出256，单请求尝试1次，gold辅助重试0。Solver/judge阶段上限900/300秒，总模型阶段预算1200秒。原生配置另见 [native-reference.json](configs/native-reference.json)：其27B模型、65536输出、4GPU/131072上下文和更多任务/重试与本轮明显不同。这不是论文分数复现。

原始日志：_work/mage/runs/20261023T065330Z-9a7fda83/。完整cost、原始manifest和私有judge证据仍保留在该目录。

## 准备与安全执行检查

- 上游固定为 ace693139dc43e8bdb3076da4127483b41a5060f；数学数据40项，固定revision和SHA见 sources.json。
- Python 3.12.8 math-only venv、冻结依赖、24个SHA锁定Linux wheels；离线Docker build成功。
- 真实代码沙箱检查通过：UID65534，无网络，无gold/source/socket/key，根文件系统只读；2秒超时后无容器遗留。见 [sandbox-check.json](sandbox-check.json) 和 [code-image.json](code-image.json)。
- 初次Docker内下载曾因Docker代理故障挂起。仅停止经PID/start-time核实的本次build客户端；未reset数据，改为锁定wheels后离线构建。
- 最终7项离线检查通过；CodeExecutor被强制配置为独立无网络代码容器，无宿主执行回退。本轮真实模型没有发出可识别的CodeExecutor请求；实际隔离证据来自另行运行的无模型 authored-code 检查。
- 本轮求解器输入和shared state不含参考答案；全部参考答案只在求解结束后的独立评分进程读取。

## 运行结束后的报告健壮性修复

本轮完成后，仅修复清理异常可能覆盖结果保存的问题：按本run label清理失败时记录cleanup_error，保留原状态并继续保存manifest/costs。新增离线失败注入检查通过，没有重跑模型，没有改变judge、提示词、预算或原始成绩。确切前后diff及wrapper hash见 patches/postrun-cleanup.diff 和结果概要。
