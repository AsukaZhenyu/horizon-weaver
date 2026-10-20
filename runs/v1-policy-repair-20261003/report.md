# v1 development regression repair — 2026-10-03

These are previously selected original Terminal-Bench 0.2 tasks, one attempt per revision. They are development checks, not a full benchmark, holdout result or frozen baseline.

| Matched task | v0 reference | v1 decoder | v1 self-check repair | v1 terminal frontend |
| --- | --- | --- | --- | --- |
| fix-permissions | Pass | Pass | Fail | Pass |
| log-summary | Pass | Fail | Fail | Pass |
| regex-log | Fail | Fail | Fail | Fail |

Matched totals: v0 2/3; v1 decoder 1/3; v1 self-check repair 0/3; v1 terminal frontend 2/3.

Separate complete five-task batches: initial v1 1/5; phase guidance 1/5; decoder 2/5. No cross-revision best-of score is reported.

Matched budget: same Qwen3.5-9B Q4_K_M server, thinking off, temperature 0.2, 2048 output tokens, 40 steps, 300 s, 30 s tools, one attempt. v0's native output cap is 8192 and its native wall budget is longer; this is a bounded matched comparison, not native-default parity.

Separate four-task follow-up: 0/4. Its three matched tasks are compared above; cancel-async-tasks is an additional context regression without a v0 comparison.

Final counting-pipeline guard follow-up: log-summary 0/1. This is a separate one-task revision; it is not added to any previous three/four/five-task score.

Current defaults: terminal frontend, unconstrained gateway output and 8192 output tokens. The frontend lowers one strict decision into Action IR; IR/JSON Schema remains an explicit alternative. Runtime tests: 76/76, including six real Docker tests; 70 runtime tests passed after the final malformed-array guard. Engineering tests do not establish model task-solving ability.

The terminal frontend restored permissions (5 steps, submitted) and correct log counts (original grader 3/3). Log-summary nevertheless ended no_progress after its own false-negative line-count check; regex-log failed. Grading parity is not full workflow reliability, and v1.0 remains unaccepted.

Final default/codec changes were validated by tests and offline replay of 10/10 formerly rejected decisions; no tool ran in replay. They do not create a new three-task score. Every graded revision remains tied to its actual source fingerprint.

Limitations: policy tests may be inadequate even when they exit 0; persistent interactive shells/services remain unsupported; prior failure records and partial/excluded runs are retained. Actual per-task grades, source fingerprints and usage caveats are in overview.json.
