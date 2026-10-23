# MAGE: bounded math1 engineering reproduction

**Completed local sanity: accuracy is not trustworthy. The local LLM judge marked an empty answer on subtask 4 correct.** Raw scores [1,1,1,1,0] are preserved; native final-subtask SR is 0/1. See [the audited report](report.md) and [sanitized evidence](results/math1-sanity.json).

This branch prepares the official [MAGE implementation](https://github.com/Komorebi660/MAGE) on one MemoryArena mathematics task. It is an **engineering sanity run**, not reproduction of the paper's accuracy or token-efficiency claims. No ALFWorld or Task IR adaptation is included.

## Sources and scope

`sources.json` pins upstream commit `ace693139dc43e8bdb3076da4127483b41a5060f` and the official MemoryArena dataset revision and SHA256. The data has 40 tasks; task index 0 / ID 0 contains five interdependent subtasks. Source, data, virtualenv, images' build context and raw runs live under ignored `_work/mage/`. The original agent policy, prompts, tree, compression, backtrace and gold-free self-verification remain upstream code.

The reviewed patch is `patches/bounded-runtime.patch`: configurable request timeout/attempts/template compatibility, optional strict external judging, and an optional isolated CodeExecutor. Its original defaults preserve the upstream configuration; our bounded wrapper explicitly selects the engineering settings. We do not run upstream's native generated-code execution on the host.

## Prepare and check (Windows PowerShell)

From this worktree's repository root:

```powershell
python reproductions/mage/prepare.py --install --download-wheels
& _work/mage/.venv/Scripts/python.exe reproductions/mage/offline_check.py
& _work/mage/.venv/Scripts/python.exe reproductions/mage/run.py --base-url http://127.0.0.1:8080/v1 --model served-model-id --dry-run
```

`prepare.py` checks source/data identity, applies the recorded patch idempotently, installs only pinned math dependencies when requested, and stages a code-container build context. It refuses to reset a checkout at an unexpected revision.

Linux wheels are fixed by filename and SHA256 in linux-wheels.lock.json and verified before staging. The Docker build installs them offline, avoiding dependency downloads inside Docker. Build the generated-code sandbox after Docker is available:

```powershell
docker --context desktop-linux build --network=none --pull=false -t horizon-repro-mage-code:math1 _work/mage/image
```

Then run only in the inference window allocated by the main experiment controller:

```powershell
& _work/mage/.venv/Scripts/python.exe reproductions/mage/run.py --base-url http://127.0.0.1:8080/v1 --model served-model-id
```

The endpoint is mandatory and restricted to local/private HTTP addresses. The runner uses a literal dummy API key, starts no model server, and makes no paid-API call. Each invocation creates a fresh timestamped directory. Its configuration and command are saved before execution.

## Explicit departures from the paper/native configuration

The native-reference.json file records native settings separately and is not accepted as an executable sanity configuration. Native host execution of model-generated code is not performed.

The initial `configs/math1-sanity.json` selects one task, one worker, one process attempt, **zero gold-assisted retries**, 8 steps per subtask attempt, up to one original gold-free self-verification retry, 768 output tokens per solver call, 256 per judge call, thinking disabled, temperature 0, seed 2026. Request attempts are one, request timeout is 90 seconds, solver/judge call caps are 64/5, solver/judge phase caps 900/300 seconds, and the total phase budget is 1200 seconds. Code execution is capped at 30 seconds.

Upstream `run.sh` instead selects 40 math tasks, 8 workers, 30 steps, two extra gold-assisted retries. Its vLLM launch script requests 4 GPUs and 131072 context tokens; its default max output is 65536 with thinking enabled. Upstream evaluation retries exceptions without an outer attempt cap. We use the same agent class through a split entry point rather than that unbounded outer loop. A small local model/context is a further configuration change, recorded per run.

## Solver, generated code and judge separation

The trusted solver process receives only ID, questions and backgrounds. Its shared state contains no reference answers; gold-assisted retry is disabled. Its original gold-free answer self-check is preserved and charged as solver inference.

Every CodeExecutor invocation, including any internal code check, runs in a **separate Docker container** with networking disabled, no host mounts or Docker socket, read-only root filesystem, dropped capabilities, an unprivileged user, and resource/time limits. The only task payload sent to it is the generated code through stdin. The image contains Python/math dependencies, not the agent, tasks, reference answers or solver traces. Docker is required; the bounded worker refuses an unpatched executor. There is no host-execution fallback in this wrapper.

Only after the solver process exits does a separate trusted judge process receive the original reference answers and predictions. Judge outputs never return to the solver. It preserves the original equivalence prompt but fails the run if the judge is unavailable or returns no unambiguous verdict; it never silently substitutes string matching. This differs from native fallback behavior and is explicit.

## Results and interpretation

A completed run writes `manifest.json`, `costs.json`, `scores.json`, public input, solver logs/predictions, and private judge prompts/verdicts. Solver and judge calls and token counts are separate, then summed. Failed requests with unknown usage are counted explicitly rather than assumed free. The locally built code-image ID and optional controller-supplied model artifact hash are recorded. Model-phase deadlines exclude bounded container cleanup. Raw private traces and dataset answers are ignored rather than committed. A timeout/request failure is reported as an incomplete run, never silently retried or counted as a completed task.

The original math SR means **the last subtask passed**. We additionally report every subtask's score and `all_subtasks_pass`. Neither one sanity task nor these local-model scores establish the paper's comparative benefits. Store checks are disabled in the original math entry point; `_check_store` also immediately returns true. Do not describe this run as demonstrating all four paper mechanisms.

Preparation, containment checks, the completed local run and its scoring limitations are recorded in `report.md`.
