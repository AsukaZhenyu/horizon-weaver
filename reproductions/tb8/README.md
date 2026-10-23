# Original Terminal-Bench eight-task reproduction

This directory reproduces the local TB 0.2.18 development suite used by Horizon-Weaver v1.3 dev12 / R13 on 2026-10-11, using native Terminus2 and official mini-swe-agent with the existing tasks, prompts, parsers and graders. It adds no Task IR, Cordis integration, learned policy, task hints or hand-written solving strategy.

The eight tasks were repeatedly used during development. This is an engineering comparison, not a held-out Terminal-Bench score or evidence of generalization.

Terminus2 `terminus2-20261023-fixed8-r4` is complete: **5/8 effective whole-task passes, 4/8 confirmed submissions that passed, 26/30 checks**, 85 HTTP attempts and 1382.0826 Agent seconds. Its unchanged raw JSON reports 4/8 with two invalid grader parses; the effective result includes two separately documented original-test recoveries with zero additional Agent attempts or model calls. See [REPORT.md](REPORT.md) and [results/terminus2-r4-summary.json](results/terminus2-r4-summary.json).

The official mini full-eight run `mini-20261023-fixed8-resumed-r3` is complete: **3/8 effective whole-task passes, 3/8 confirmed submissions that passed, 13/30 checks**, 104 HTTP attempts, 1366.243897 exploration seconds, 1355.001915 installation seconds and 3530.720017 seconds of run wall time. Its unchanged raw results retain `parse_error` for log-summary and fix-permissions; original-test recovery passed 3/3 and 1/1 respectively, with zero additional Agent attempts or model calls. See [REPORT.md](REPORT.md) and [results/mini-r3-summary.json](results/mini-r3-summary.json). Artifacts for all new runs remain under this worktree's ignored `_work/tb8/`.

## Historical result and exact source

| Task | Passed checks | Runtime end | Decisions | Agent seconds |
| --- | ---: | --- | ---: | ---: |
| hello-world | 2/2 | submitted | 3 | 26.8 |
| fix-permissions | 1/1 | submitted | 8 | 79.2 |
| log-summary | 3/3 | no_progress | 21 | 205.9 |
| regex-log | 0/1 | timeout | 15 | 301.1 |
| cancel-async-tasks | 5/6 | timeout | 26 | 301.0 |
| jq-data-processing | 14/14 | no_progress | 25 | 268.1 |
| sqlite-db-truncate | 0/1 | timeout | 18 | 301.0 |
| cprofiling-python | 0/2 | timeout | 14 | 301.0 |

R13 achieved **4/8 whole-task successes, 2/8 confirmed submissions that passed, and 25/30 checks**, with 128 gateway calls and 1784.1 Agent seconds. Log-summary and JQ passed but ended with no progress. SQL and profiling originally had grader bootstrap failures; unchanged tests later ran on hash-verified restored candidates and both failed, with no additional model attempt. Recovered grades remain distinguishable from uninterrupted native grading.

Raw R13 evidence is `C:/Users/89664/Desktop/master degree/horizon-weaver/runs/v1.3-goal-r13-20261011/`. `protocol.json` pins its evidence files, per-task initial IR and 54 Python source files. The exact dev12 snapshot is that directory's `code/`, fingerprint `54e872068b09d775c9f789a89e9d6fc808067a2295983d92467d4bdca561110b`. Git base `731b86e` alone does not reproduce it; `86b0e43` and `codex/legacy-v1` contain later dev19 code. The ignored raw evidence was not moved to `.legacy` and is not supplied by this branch.

## Fixed benchmark, model and containers

`protocol.json` fixes the benchmark root and Python executable. TB HEAD is `d28711d0da2675d0bb1d56de45ae5df6082438a3`, with five pre-existing tracked compatibility changes: hello-world Compose plus asciinema handler, harness, Compose manager and tmux session. Their hashes and 55 task-file hashes are fixed. Original files, hidden tests and graders must not be edited. Reference solutions and hidden-test contents are hashed for integrity only and must never enter policy input.

The model is Qwen3.5-9B Q4_K_M, SHA256 `03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8`; llama-server binary SHA256 `028797554f3e5e6e491d0bc3ac15a9957a748a4734bd6b3abc6982f992d64224` (b8705). Protocol records context 16384, parallel 1, GPU layers 99, BF16 KV, no mmap, reasoning off, four CPU threads and two HTTP threads. The controller launch manifest is pinned and checked against historical arguments. No request-level thinking override is added. Temperature is 0.2; historical seed and other sampling fields were unspecified.

Eight cached image IDs are pinned from 2026-10-23 read-only inspection. R13 did not record enough per-task IDs to establish complete historical image equality. Original Dockerfiles have no hidden-test/solution COPY or broad build-context COPY; static inspection cannot prove hidden tests absent from cached image layers.

Current `run.py` applies a **process-local** Compose wrapper: explicit builds are rejected, the native `up -d` call becomes `up -d --no-build --pull never`, and the returned client container's actual image ID is checked against the protocol before Agent policy starts (normalizing an omitted `:latest` in the tag key). Identity records are saved in each run's `container-identities/`. This changes no upstream source or global Docker configuration; preflight alone is insufficient because a tag can change between inspection and startup.

The first mini run, `mini-20261023-fixed8`, was interrupted on its first task before any task model request; its interruption record is retained. Restart preflight found that old Compose startup had implicitly rebuilt the SQL tag to `sha256:8265de44165d...`, instead of fixed `sha256:ca9c8edc28be...`. The unexpected image was preserved as `horizon-weaver-tb8-preserved/sqlite-interrupted:20261023`, and the original tag was restored. Startup `mini-20261023-fixed8-resumed-r2` then stopped before policy/model calls because the image lookup omitted `:latest`; tag normalization was corrected and passed a stub check with zero Docker/model calls. The formal run is `mini-20261023-fixed8-resumed-r3`. Failed starts remain preserved and unscored; no solving policy changed.

## Budgets and metric definitions

- R13's 40 steps were outer typed decisions, including invalid/rejected proposals. Terminus2's 40 episodes can contain several terminal commands and include completion confirmation. Mini uses its official 40-step limit; its saved `model_stats.api_calls` and proxy HTTP attempts are recorded separately.
- R13's 300-second Supervisor budget included attached-harness startup, assessment, context, calls, tools, checks and cleanup, excluding external provisioning and hidden grading. Terminus2 also has a 300-second adapter budget and 330-second outer Harness limit. Mini installation runs with a separate 300-second script timeout, followed by an independent 300-second exploration budget; its outer Harness limit is 630 seconds (setup 300 + exploration 300 + margin 30). Original test timeout remains 180 seconds.
- For mini, `setup_seconds` measures installation; `exploration_seconds` comes from adapter `elapsed_seconds` after installation and runtime preparation, and can include cancellation, proxy shutdown and trajectory retrieval. `harness_agent_seconds` includes installation; `full_trial_seconds` and run wall time also include Harness work and grading. Report exploration, setup and full wall time separately. Missing telemetry is unknown, not evidence of zero cost or time.
- R13's physical context was 16384 with structured context budget 14000. Default output 2048 could grow to 8192. Terminus2 retains native conversation/context recovery; both native adapters cap each response at 8192. Mini keeps its original conversation policy. A ceiling is not an identical allocation or total output budget.
- Single-call limits differ: R13 default 60 seconds could grow to 120; Terminus2 HTTP is capped at 90 and the remaining Agent budget. Mini has no separate 90-second cap: the native client timeout is 300 seconds and the proxy socket timeout uses the remaining exploration budget, so one request can occupy nearly 300 seconds. Overall deadlines also use host/supervisor checks and cleanup; the socket timeout is not a precise end-to-end deadline. This difference must accompany a three-way comparison.
- Proxy HTTP attempts include retries and failures. Known prompt/completion/total usage is summed only when supplied; failed or pending requests without usage stay explicitly unknown. The 200000-token budget is an observed cumulative stop threshold, not proof of a strict cap on unobserved consumption. R13's mixed actual/estimated usage cannot support an exact token-saving ratio against known-only native usage. Local inference is not assigned a dollar price.
- Normal submission requires the native completion path. For mini this means saved `Submitted`, CLI exit 0 and no supervisor/proxy stop reason; grading success remains a separate result. `normal_submission_and_pass` requires both. Native whole-task passes are primary; partial checks, infrastructure failures and ungraded tasks are separate.
- Terminus2 repeats are exact stripped `session_send` key-string matches. Mini's `sent_commands`/`repeated_commands` count exact **proposed action command strings** in the saved trajectory, including submission or other actions that may not execute. They are not executed-command counts or semantic waste. A killed/incomplete mini trace leaves full counts unknown; saved counts are lower bounds. Neither native definition is directly comparable to R13's typed action/file-state repetition.
- Initial IR hashes cover only `/app` entries, content/hashes and modes; four tasks start with an empty file collection. Equality cannot establish identical packages, processes, network or complete container state; JQ generated files also require checking before another attempt. No automatic retry or best-of selection is used.

## Official mini execution and aggregation

`mini_adapter.py` installs pinned `mini-swe-agent==2.4.6` and `litellm==1.75.8` inside the original task container. It invokes the official `mini` CLI, original `mini.yaml`, InteractiveAgent, bash tool and LocalEnvironment unchanged, with a separate runtime configuration for endpoint, output and resource limits. Identical policy-file hashes are verified for **7/8 tasks**. For cprofiling-python, the original Compose mounts `/logs` but not `/agent-logs`, so container-local installation metadata was not retained; the trajectory, HTTP requests/responses, installation stdout/stderr, budget-summary and final process state remain. Hidden tests stay with the original TB Harness after the Agent returns.

`summarize.py` reads one specified run only, retains ungraded results as null, and exposes complete-suite accuracy only when every expected task has valid grading. It does not merge sibling runs or apply recovery overlays: Terminus2's effective 5/8 and mini's effective 3/8 are documented separately in REPORT and the effective JSON files. Review telemetry coverage and unknown values before comparing cost, time or repeats; a partial or missing log must not be treated as a complete zero.

## Read-only preflight and transport checks

Use Python 3.12+, Git and the existing Docker CLI; preflight uses the standard library and installs nothing:

```powershell
python reproductions/tb8/capture_provenance.py --protocol reproductions/tb8/protocol.json --output _work/tb8/preflight.json
```

It checks fixed task IDs, benchmark HEAD/dirty paths, pinned source/task/legacy evidence hashes, initial IR, static Dockerfiles, cached image IDs, model/binary hashes and launch arguments. It hashes the model in a streaming pass. It starts no WSL/container/Agent, calls no inference and changes no Git/system configuration; output must remain inside ignored `_work/tb8/`. Exit 0 requires `success: true` and no `blocked` entries. Docker pipe denial is an inspection failure, not proof the engine is broken. Successful preflight certifies pinned inputs, not runtime readiness or stochastic equivalence.

`run.py check` uses a loopback fake HTTP server and fake terminal, without Docker or inference. `offline-readiness-005` exercised 8 requests and 9 assertions covering usage, completion confirmation, context summarization, retries and deadline fencing. The process-local LiteLLM registry declares context 16384 without output metadata, because `get_max_tokens` otherwise selects the response limit; requests remain capped at 8192. Upstream prompts/parser are unchanged. Independent `slow-http-final/slow-http-check.json` records a 0.226-second ReadTimeout under a 0.2-second deadline, unknown usage and rejection of a later request before transmission. `factory-final/` verifies the Harness factory receives the explicit model and budget; these fixtures are not capability scores.

Abnormal/unsubmitted Terminus runs receive foreground cancellation before grading; normal submitted background services are preserved. If shell return cannot be confirmed, only that tmux session is restored for grading. Mini's supervisor tracks and cancels its own CLI descendants on abnormal exit. Cleanup uncertainty is reported separately.

## Post-Agent grading recovery

`--grading-transport` selects `GradingTransportHarness`; otherwise the original Harness is used. After the Agent returns and before original test setup, preparation pauses that task container, saves complete `/app` tar archives with Unix metadata/content manifests/SHA256 and identity, changes only apt source URI schemes from HTTP to HTTPS, verifies `/app` unchanged, and unpauses. No packages or test content are supplied to the Agent. Preparation failure is infrastructure failure; partial archives and the container are retained. This grading-environment deviation is recorded separately.

Repeated `--task-id` selects unique fixed-suite IDs for an explicit infrastructure replacement, recording the extra attempt while preserving the original ungraded attempt; it is not automatic retry or best-of selection. Reconstructed visible artifact text must not be called exact full-state recovery. Controller-authorized regrading can use `prepare_grading_transport(container, artifact_dir)` and must require `ready_for_original_tests` plus `app_unchanged`.
