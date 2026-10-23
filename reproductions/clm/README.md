# CLM 单 Agent 复现

本分支复现官方 CLM 的 actual-context-edit：模型通过 bash 编辑当前消息历史的镜像，官方 parser 将它回写为下一次请求的消息。直接使用官方 `ClmAgent` 和 `clm_contextbench:ClmStream`；没有重写 Agent、追加 Task IR 或修改提示策略。

本机固定任务结果：16/24（66.7%），budget_wall 后 partial；已回答16题全部正确，任务未完成。详见 [结果与预算诊断](reports/local-sanity.md)。

## 范围与固定版本

- CLM: `483198229ab7b90c0eed4651306ab3b3505aaf50`，CC BY-NC 4.0。
- ContextBench: `fb533c18c78a82d3f1b2047a9c1700735cf829ae`，Apache-2.0。
- 依赖：Ubuntu / Python 3.12，Harbor 0.16.1；解析后的第三方版本在 `requirements-ubuntu-py312.lock.txt`。
- 官方任务：`kv-store-R1000-seed42`，1000 条记录、24 个查询、34 个流操作。流 SHA 和上游提交记录在 `upstream.lock.json`。
- 上游 clone、生成任务、venv 外的原始日志均不提交，位于忽略目录 `_work/`。venv 默认为 Linux 用户 cache 中的 `horizon-weaver/repro-clm/venv`。
- 本轮不做 RL、Suffix Cache Reuse、多 Agent、ALFWorld 或论文分数复现。官方 RL 目录只提供外部训练框架补丁，并非完整训练流水线。

## 本机配置

`configs/local-sanity.json` 使用已运行的 Qwen3.5-9B Q4 / llama.cpp 服务，模型名 `repro-qwen35-9b-q4`。这是非论文模型/后端。脚本不启动模型，不调用付费 API。

本机输入预算 12288，保留 2048，实际目标 10240；输出上限 2048；thinking 关闭；64 个任务步骤、96 次逻辑模型调用、Agent 1800 秒，整次 Harbor 流程总时限 2700 秒。官方 ContextBench 任务本身不缩小，仍保留原始 32768-token benchmark 元数据。官方 adapter 允许 Agent 使用更严格的上下文预算，但本配置不能与论文的 32K/8192 输出数字直接比较。

`cost_metric=usd` 仅关闭缺乏可靠模型参数的 FLOPs 估算。未配置价格的本地服务返回的零美元不表示零成本；本次应报告原始 token、模型调用和墙钟时间。纯 context 编辑不计任务 step，但计模型调用与所有推理成本。CLM_LLM_MAX_RETRIES=1 关闭官方默认的 5 次请求尝试，litellm 本身的重试也为 0。失败或缺失 usage 的请求将标为 unknown，不能把未记录的成本当作 0。LITELLM_LOCAL_MODEL_COST_MAP=True 固定使用已安装包中的价格表；本地模型仍然没有已知美元价格。

当前 LiteLLM 1.106.2 在普通 tools 分支也会导入 MCP handler，因此额外安装其官方 mcp extra；全量依赖已重新锁定。check_transport.py 用一次成功与一次 503 的自编 loopback HTTP fixture，走真实官方 CLM/LiteLLM 请求、工具解析和 usage 路径，并确认错误请求没有隐式重试。它不调用模型，不代表任何 benchmark 成绩。

## 准备与检查（Ubuntu/WSL）

在该 worktree 根目录运行：

```bash
bash reproductions/clm/bootstrap.sh
```

`bootstrap.sh` clone 缺失的官方仓库、校验 SHA、安装隔离依赖，调用官方生成器和 task packager，然后检查原始 parser / edit gate / adapter。已有 clone 如果版本不符或有源码改动，会拒绝自动重置。

准备清单对整个打包任务逐文件保存 SHA-256（包括 stream、task.toml、test.sh、gold 与复制的 grader；不输出 gold 内容）；启动前逐项重验，防止忽略目录中的数据变化。

官方 Gold OracleAgent 只用于验证生成器、协议和 grader 相互一致，不是模型 baseline。所有准备阶段的报告都写明 `model_calls: 0`。

Docker 可用后，检查真实上传/编辑/下载/parseback 和 future-stream 隔离，不调用模型：

```bash
$HOME/.cache/horizon-weaver/repro-clm/venv/bin/python reproductions/clm/check_mechanism.py --docker
```

这项检查直接使用 Harbor 的 DockerEnvironment 与官方 ContextEnv，不在宿主执行模型命令，也不用 mock sandbox。它只验证机制连通，不证明模型会有效编辑上下文。

## 单项真实模型 sanity

先取得本机共享 GPU 的独占推理时隙，并确保现有服务已就绪；脚本不会管理服务生命周期：

```bash
CLM_API_BASE=http://<existing-server>:8080/v1 bash reproductions/clm/run-sanity.sh
```

`run-sanity.sh` 调用 `repro.py run`，实际执行官方 Harbor CLI 和 `clm_contextbench:ClmStream`。任务中的 shell 命令全部在 Docker 内执行。Gold 测试数据仅在 Agent 结束后的 verifier 阶段进入容器；future stream 由官方 TurnSource 校验后从容器移除。

日志位于 `_work/clm/runs/<UTC time>/`：

- `launch-manifest.json`：源版本、模型配置、实际 argv、Docker image 身份；不保存 API key。
- `console.log`、`process-exit.json`：整个执行日志和退出码。总时限/异常后仍保留退出信息和已有评分；清理仅针对本次独立进程组与唯一 Compose project 标签下的容器。
- `trials/*/agent/`：官方 usage、stream、skill hash 与逐调用 context snapshots。
- `trials/*/verifier/`：官方外部评分。
- `summary.json`：汇总已有证据。退出码 0 不等价任务成功，没有评分时不补造分数。

验收：合法 scorer 输出、流交付状态、真实 context-edit 计数和下一请求 snapshot、token/调用/耗时。若准确率低或 budget_wall，应保留失败轨迹，不在这一阶段改造官方策略。

## 来源与授权

- [官方仓库](https://github.com/facebookresearch/context-language-models)
- [官方 ContextBench adapter](https://github.com/facebookresearch/context-language-models/tree/483198229ab7b90c0eed4651306ab3b3505aaf50/clm/clm_contextbench)
- [ContextBench](https://github.com/RulinShao/ContextBench/tree/fb533c18c78a82d3f1b2047a9c1700735cf829ae)
- [公开版本与论文内部 benchmark 的差异](https://github.com/RulinShao/ContextBench/blob/fb533c18c78a82d3f1b2047a9c1700735cf829ae/docs/DEVIATIONS.md)

本目录只包含准备/检查/运行与记录代码。上游源码保持原许可、版权和提交身份；未将其并入 Horizon-Weaver 的实现或改称自己的 Agent。
