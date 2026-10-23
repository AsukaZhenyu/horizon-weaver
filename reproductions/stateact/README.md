# StateAct / ReAct reproduction

This branch runs native ALFWorld and the unmodified official StateAct runner/policy prompts.
It does not use Cordis, Task IR, expert plans, reference answers, or a replacement toy benchmark.

- Upstream: https://github.com/ai-nikolai/StateAct
- Fixed commit: `8fb47be6f5f3978a38726608ae347987341f1d82`.
- Official environment pins: ALFWorld 0.4.2, TextWorld 1.6.2, fast-downward-textworld 20.6.3.
- Official few-shot IDs: `1 0`; fields and order are in config.json.
- Success is native `info["won"]`, aggregated by upstream get_score.py.
- Upstream scorer averages CSV rows without deduplication. New trial directories are mandatory.

## Scope and backend adaptation

The official README reference is Qwen2.5-14B-Instruct on A100/vLLM (108/134 StateAct,
100/134 ReAct). This local smoke test uses an existing Qwen3.5-9B Q4 llama.cpp service,
not the original model/backend. It is a method reproduction under a changed backend,
not a replication of those accuracy numbers.

`transport.py` subclasses llamp 0.0.19's unchanged BaseLLMSystem. It only replaces the
model transport through an in-process VLLMChat import shim. The original history,
prompt generator, action parser, command correction, native environment, repetition
termination and scorer remain unchanged. No upstream source file is patched.

Both methods use temperature 0, seed 42, disabled thinking and a 1024 token output cap.
Official VLLMChat caps output at 2000 and slices input by 16000 *characters*. Here the
complete prompt is sent as one user message with the server chat template. Every call
is checked against 16384 context tokens and fails explicitly on overflow. No history
is silently truncated. The complete prompt/token checks are logged.

## Prepare and run (WSL/Linux)

Run `bash reproductions/stateact/setup.sh` from this worktree. Runtime dependencies,
downloaded source, official full OOD text data, logs and venv stay in the ignored `.horizon/reproductions/stateact`.
The text-data preparer uses the official release archives, extracts the complete valid_unseen split, and skips training/seen files and unused 3D detector weights. No global pip or GPU inference process is used. Model service lifecycle is external.

```bash
PY=.horizon/reproductions/stateact/venv/bin/python
$PY reproductions/stateact/run.py preflight
$PY reproductions/stateact/launch.py run --method react --trial smoke-react-001 --num-envs 1
$PY reproductions/stateact/launch.py run --method stateact --trial smoke-stateact-001 --num-envs 1
$PY reproductions/stateact/run.py score --trial smoke-react-001
$PY reproductions/stateact/run.py score --trial smoke-stateact-001
```

Run paired methods sequentially on the same start index and environment count.
Do not use smoke outcomes to tune the official prompts. Do not mix test scores with
the official 134-task results. Preflight is an integration check, not a success result.

## Audit artifacts

Each ignored run directory contains the untouched copied environment config, a run
manifest, actual transport usage/latency JSONL, original runner CSV and raw histories.
The manifest identifies the source and adapter hashes, model hash and all deviations.
The initial report records setup checks and subsequent actual outcomes.




Completed local check: both methods passed the fixed three consecutive native tasks. See INITIAL_REPORT.md and paired-three-results.json; this is not original-model accuracy replication. collect_results.py validates paired task IDs, exact live observations/goals, native checker consistency and complete raw-request hashes. launch.py measures future full child-process wall time; the initial pilot's missing full-process timing is explicitly reported.
