# 分层评测方案（2026-10-31 冻结）

> 本文件是设计本身，冻结后只增不改。可执行的冻结输入在 `config/`：
> 任务表与评分约定见 `config/protocol.json`，分层与排除见 `config/selection.json`，
> 镜像 ID 见 `config/base-images.json`。实验里出现的每一处偏离都要在
> [`ANALYSIS.md`](ANALYSIS.md) 里有对应条目。

本文件对应规划文档《长期自主Agent实验与复现规划》§2.1（分层抽样）、§6.2（24 项开发集）、
§10.1（资源估算）。本地阶段只跑 Terminal-Bench；其余基准在服务器可用后按同一分层规则扩展。

## 1 文档里要测的 benchmark，以及各自回答什么

| 基准 | 文档中的角色 | 本机现状 | 何时跑 |
| --- | --- | --- | --- |
| Terminal Bench 2.1 | 软件/终端整项完成，主证据 | 只有 TB **0.2.18** 的 241 题（`original-tasks`），TB 2.1 未取得 | L0–L1 本地；2.1 待服务器 |
| Long Horizon Terminal Bench 2.0 | 单次执行是否持续进展（数小时） | 未下载 | L2 服务器（先 6 题校准，再 12–20 题） |
| SWE-bench (Lite/Verified) | 真实仓库修复与泛化 | 未下载 | L2 服务器，按仓库隔离 |
| CORE Bench | Research 计算复现 | 未下载 | L2 服务器，仅硬件可跑的文本任务 |
| 受控诊断任务 | 解释机制（依赖跨度/遗忘/干扰/中断恢复） | 未实现 | L2，单列一张表 |

各基准分别报告，不把不同评分平均成一个总成功率（§3.3）。

**本轮的诚实边界**：跑的是 TB 0.2.18，不是 2.1。两版任务问题不同，结果不能标成“TB 2.1 成绩”，
只能写成“TB 0.2.18 分层开发集”。

## 2 分层设计（已冻结，写入 selection.json / protocol.json）

- 抽样单位：任务。
- 主分层：把 26 个原始 `category` 归并成 11 个桶。
- 次分层：`difficulty`（easy/medium/hard），桶内按 easy→medium→hard 轮转取。
- 结构分层：**N = 16 项新任务**（10 个桶按 2/2/2/2/2/1/1/1/1/1 分配），
  **H = 8 项历史任务**（保证与 round1–3 可比），合计 **24**。
- 抽样种子：`20261031`（稳定哈希排序），运行顺序另用同一种子打乱，避免按历史时长排序带来的混杂。

### 2.1 预注册排除规则（在看到任何方法成绩之前执行）

1. Dockerfile/compose 出现 nvidia/cuda/gpu → 超出本机 8 GB 显存。
2. 音频/视频/视觉任务 → 需要离线大权重。
3. **compose 多于 1 个服务** → Spine/Sentry 宿主只起 client 容器，任务本身不可解。
4. `expert_time_estimate_min > 120` → 与 30 分钟预算不相称。
5. 构建失败（记录原始错误）→ 环境不可复现，不算方法失败。

排除项、数量与理由全部写在 `selection.json` 的 `excluded_before_running`，后续构建失败的写进
`protocol.json` 的 `environment_exclusions`。**排除发生在看到成绩之前**。

### 2.2 本轮的桶与题量

| 桶 | 题量 | 说明 |
| --- | --- | --- |
| software-engineering | 2 新 + 2 历史 | regex-log、cancel-async-tasks 属历史 |
| system-administration | 2 新 + 1 历史 | 含 slurm 单节点监控 |
| security | 2 新 | 多服务/注入类已排除 |
| data | 1 新 + 2 历史 | |
| scientific-computing | 2 新 | amuse、modernize-scientific-stack |
| debugging | 1 新 + 1 历史 | |
| mathematics-algorithms | 2 新 | |
| model-training-ml | 2 新 | |
| games | 1 新 | |
| other | 1 新 | personal-assistant |

## 3 共同实验条件（两条方法轨道一致）

- 模型：Qwen3.5-9B Q4_K_M，sha256 `03b7472…52b7e8`；16384 ctx；parallel 1；`--reasoning off`。
- 采样：actor temperature 0.2，单次输出 ≤2048 tokens；Sentry 辅助调用 temperature 0 / ≤4096。
- 预算：每题 **300 s** 总探索；每题一个全新容器、全新会话；跨任务经验清空。
- 反馈：只有任务指令、初始环境和普通工具输出；隐藏测试在候选冻结后才注入。
- 评分：暂停容器 → 取 /app 归档 → `docker commit` → 在独立容器里用**未修改的原测试**评分；
  评分阶段新增模型调用与 Agent 动作必须为 0。
- 评分协议分两条（已写入 `protocol.json.grading`）：
  - **历史 8 题**沿用 round3 的离线固定 wheel 协议（无网络），保证与 round1–3 可比；
  - **新 16 题**用任务自带的 `run-tests.sh`（需要网络）。这是**协议差异**，逐题报告，不隐去。

## 4 指标

- A：冻结候选通过原任务整项测试；V：预算内按宿主原生协议正常交付且无需人工收尾；Y = A·V。
- 主指标 Y，并列 A、V。整题通过才算成功，部分检查通过不算（§3.1）。
- 辅助：重复动作、进展回退、崩溃、人工介入、峰值内存、请求数与 tokens。
- 统计：按桶报告分子/分母；差值区间用任务配对 bootstrap；桶内 n 太小只报描述统计。
  三个种子不能当三题（本轮只有 1 个种子）。

## 5 三层执行计划

| 层 | 范围 | 本机成本 | 产出 |
| --- | --- | --- | --- |
| **L0 本地（现在）** | TB 0.2.18 分层 24 × {Spine, Sentry} × 1 seed × 300 s | ≈48×300 s ≈ 4 h 探索 + 构建/评分；实际 8–12 h | 逐题 A/V/Y、失败分类、成本表 |
| **L1 本地（本周）** | 24 题 3 种子；加基础 Agent 对照 | 3 倍 L0 | 方差估计，决定正式样本量 |
| **L2 服务器（可申请后）** | TB 241 × 3 seeds；LHTB 6 校准 → 12–20 长任务；SWE-bench Lite ~50；CORE 文本子集 | 数十至数百设备小时 | 冻结未见集上的主结论与置信区间 |

L2 的主结论只用排除开发/训练/验证任务后的冻结未见集（规划 §7 E1，80–120 题）。

## 6 本轮明确不做的

- 不声称 TB 2.1 成绩；
- 不把 24 题当未见泛化证据（8 题是长期开发集，16 题是分层开发集）；
- 不做 Sentry/Spine 的组件级归因（宿主不同，比较的是完整集成）；
- 不把服务器吞吐折成本机过夜性能。
