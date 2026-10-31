# Terminal-Bench 分层评测装置

在 Terminal-Bench 0.2.18 的 24 题分层开发集上，比较两个完整 Agent 集成：
Pi 1.1.0 + Spine 插件，以及 Sentry 0.11.0 核心 + 受控终端宿主。

装置、协议、结果都在仓库里，所以换机器只要 `git clone`，继续跑只要 `git fetch`。

## 三份文档

| 文档 | 内容 | 谁维护 |
| --- | --- | --- |
| [`DESIGN.md`](DESIGN.md) | 实验设计：抽样、排除规则、共同条件、指标（冻结） | 冻结后只增不改 |
| [`ANALYSIS.md`](ANALYSIS.md) | 分析口径：A/V/Y 怎么判、两条评分路径的差异、威胁 | 随分析更新 |
| [`results/<round>/RESULTS.md`](results/) | 结果表（脚本生成）+ 解释 | 由 `export_results.py` 生成 |

`config/` 里是冻结的输入：`protocol.json`（任务表与评分约定）、`selection.json`（分层与排除）、
`task-meta.json`（241 题的元数据）、`base-images.json`（每题镜像的内容哈希 ID）。

## 一台新机器上开始

```powershell
git clone -b codex/tb-eval-harness https://github.com/AsukaZhenyu/horizon-weaver.git
cd horizon-weaver
powershell -NoProfile -ExecutionPolicy Bypass -File experiments\tb-eval\bootstrap\setup.ps1 -Check
powershell -NoProfile -ExecutionPolicy Bypass -File experiments\tb-eval\bootstrap\setup.ps1
```

`setup.ps1` 幂等：已存在的克隆、卷、wheel 缓存、镜像都直接复用。它做这些事：

1. 克隆 Terminal-Bench 到 `0.2.18` 的 `d28711d`，建 venv 并 `pip install -e`；
2. 克隆 Sentry `1d91bca`、spine-plugin `b599498` 到 `.cache/upstream/`；
3. 下载 uv 0.7.13（Linux）与 pytest 8.4.1 的 wheel 到 `.cache/grading-tools/`；
4. 用 `bootstrap/build-runtime-volume.ps1` 从公开源码重建 Spine 运行时 Docker 卷；
5. 构建 24 题的 client 镜像，并把镜像 ID 钉进 `config/base-images.json`；
6. 校验模型权重（`HW_MODEL_GGUF`，SHA-256 必须与协议一致）。

模型权重不进仓库（5.29 GB）：放到 `.cache/models/qwen3.5-9b-q4_k_m.gguf`，或把 `HW_MODEL_GGUF`
指向已有文件。`setup.ps1` 会打印期望的 SHA-256。

## 跑一轮

```powershell
# 先看还差哪些题
experiments\tb-eval\scripts\run_round.ps1 -Status

# 跑两条轨道里所有未跑过的题，然后出汇总与结果表
experiments\tb-eval\scripts\run_round.ps1

# 只跑一条轨道 / 只跑指定题
experiments\tb-eval\scripts\run_round.ps1 -Method spine
experiments\tb-eval\scripts\run_round.ps1 -Tasks hello-world,regex-log
```

两条轨道共用一个模型服务，`run_round.ps1` 会先查 `/health`，不健康就停下（不把 502 记成 Agent 失败）。

```powershell
# 显存不够时降低 -GpuLayers（-1 = 全放 CPU）
experiments\tb-eval\scripts\launch_model.ps1 -Exe <llama-server.exe> -GpuLayers 99
experiments\tb-eval\scripts\launch_model.ps1 -Stop
```

启动记录（可执行文件、模型 sha256、完整参数）写到 `.cache/server/launch.json`，
并被 `run-config.json` 引用。参数是 `DESIGN.md` §3 的冻结条件：16384 ctx、parallel 1、
`--reasoning off`、bf16 KV。

### 断点续跑

每条用例都有自己的目录，`status.py` 只报三种状态：

- `pending`：没有用例目录 → 会被下一轮自动带上；
- `done`：已有 `result.json` → 永不再跑；
- `started` / `failed`：开了 Agent 却没产出结果 → **不会自动重跑**，
  需要人决定是补评分（`--recovery-label`）还是作废这次尝试。
  自动重跑会把「失败一次」悄悄变成「最好的一次」，这正是实验不允许的。

## 跨机器同步

| 内容 | 位置 | 是否入库 |
| --- | --- | --- |
| 代码、协议、分层、镜像 ID | `experiments/tb-eval/`（除 `runs/`、`node_modules/`） | 是 |
| 逐题裁定与汇总 | `results/<round>/`，由 `export_results.py` 生成 | 是（小） |
| 原始证据（app tar、完整 HTTP trace、会话归档） | `.cache/runs/<method>/cases/<task>/` | 否（GB 级，留在本机） |

所以：台式机跑完 → 提交 `results/` → 笔记本 `git fetch` 后就能看到新断点，
并且 `run_round.ps1` 会接着跑没跑过的题。要复查某题的原始证据，就在跑它的那台机器上看。

`.cache/` 整个目录都不入库；如果某台机器的缓存放在别处，在
`experiments/tb-eval/.local/env.ps1`（gitignored）里覆盖 `HW_*` 变量，详见
`scripts/env.ps1` 与 `harness/paths.py` 的注释。

## 目录

```
experiments/tb-eval/
  config/            冻结输入：protocol / selection / task-meta / base-images
  harness/           运行与评分代码（paths.py 解析所有机器相关路径）
    spine/run.mjs    Spine 轨道：代理、暂停、快照、评分
    sentry/           Sentry 轨道与汇总
    grade_original.py 共用评分器（冻结候选，原测试，零模型调用）
    status.py        断点状态
    export_results.py 把小体积裁定导到 results/
  tools/             抽样、取元数据、构建镜像、钉镜像 ID（设计阶段工具）
  bootstrap/         新机器一次性搭建
  scripts/           跑一轮的入口
  results/           逐轮结果（入库）
  .local/            本机覆盖（不入库）
```

## 环境变量

都有默认值，通常在 `.cache` 下即可；只有缓存放在别处时才需要覆盖。

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `HW_TB_HOME` | Terminal-Bench 检出 | `.cache/terminal-bench` |
| `HW_CACHE` | 第三方与派生产物的根 | `.cache` |
| `HW_RUNS` | 逐题证据 | `.cache/runs` |
| `HW_ROUND_DIR` | `protocol.json` / `base-images.json` | `experiments/tb-eval/config` |
| `HW_MODEL_GGUF` | 模型权重 | `.cache/models/qwen3.5-9b-q4_k_m.gguf` |
| `HW_PYTHON` | 跑辅助脚本的解释器 | 当前解释器 |
| `HW_SPINE_VOLUME` | Spine 运行时卷名 | `week8-spine-linux-20261030` |
| `HW_SPINE_SRC` / `HW_SENTRY_SRC` | 第三方检出位置 | `.cache/upstream/...` |
| `HW_GRADING_TOOLS` | 离线 uv 与 pytest wheel | `.cache/grading-tools` |
| `HW_MODEL_URL` | 模型服务地址 | `http://127.0.0.1:8080` |
| `HW_LLAMA_SERVER` | `llama-server` 可执行文件（`launch_model.ps1` 用） | 必须显式给出 |
| `TB_GRADING_PROXY` | 评分容器注入的代理（仅本机需要） | `http://host.docker.internal:7890` |

每次运行都会把解析后的路径写进 `run-config.json` 与每个用例的 `source.json`，
所以事后能说清「这份证据是哪台机器、哪条路径跑出来的」。
