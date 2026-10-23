# StateAct / ReAct local sanity report

This is a fixed three-task native ALFWorld sanity check with a changed local
model/backend. It is **not** a replication of the public 14B accuracy result and
does not establish statistical superiority of either method.

## Source and model/service metadata

- Official upstream: https://github.com/ai-nikolai/StateAct
- Commit: 8fb47be6f5f3978a38726608ae347987341f1d82.
- ALFWorld 0.4.2; TextWorld 1.6.2; fast-downward-textworld 20.6.3; llamp 0.0.19.
- Isolated WSL CPython 3.10.19; resolved packages: requirements-resolved.txt.
- Model: Qwen3.5-9B-Q4_K_M.gguf, API alias repro-qwen35-9b-q4.
- Model SHA256: 03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8.
- llama-server.exe SHA256: 028797554f3e5e6e491d0bc3ac15a9957a748a4734bd6b3abc6982f992d64224.
- Server --version: 0 (unknown); no build revision is claimed.
- Hardware: NVIDIA RTX 4070 Laptop GPU, 8GB.
- Context: 16384 tokens; parallel 1; no-context-shift; K/V cache bf16.
- Both methods: thinking disabled, temperature 0, seed 42, top_p 1,
  repeat_penalty 1, maximum output 1024 tokens.

The official README reference uses Qwen2.5-14B-Instruct on A100/vLLM. These local
results must not be compared numerically against its 108/134 and 100/134 scores.

## Fixed task selection and actual pairing

Native ALFWorld loaded all 134 OOD tasks. We ran consecutive reset indices 0,1,2,
without replacing tasks after observing results:

| Native reset index | Category | Goal |
|---|---|---|
| 0 | examine | Look at a bowl under a desk lamp |
| 1 | puttwo | Put two soap bars in a garbage can |
| 2 | cool | Put a cooled tomato in a microwave |

The static builder catalogue is not the actual TextWorld reset order.
environment-check.json names it native_catalogue_task_ids. Actual task IDs are
taken from reset events and stored per row in paired-three-results.json.

For each pair, task ID, gamefile SHA256, and the exact first live
observation-plus-goal SHA256 match. The latter was recovered from each full first
backend request after asserting/removing the exact official saved few-shot prefix.
All six requests preserved that prefix without truncation. Every recorded prompt
hash was checked against the saved full request, and actual server prompt-token
usage matched the template-token preflight count for every inference call.

Env0 used separate fresh processes. Indices1/2 used one sequential batch per
method, with a native reset and the original llamp policy-history reset for every
task. The two methods' actual observations and goals still matched exactly. The
persistent model service can reuse prefix KV cache; cached-token details are
retained in private usage records. No policy experience is transferred between tasks.

## Objective results

Original get_score.py was run on all four unique trial CSVs. It returned 1/1 for
each env0 trial and 2/2 for each indices1/2 trial. No duplicated rows were appended.
Each CSV success also matches the last native environment event's won flag.

| Method | Native success | Model calls | Environment actions | Backend interval sum | Prompt tokens | Completion tokens | No-effect feedback | Consecutive repeated actions |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ReAct | 3/3 | 60 | 33 | 74.65 s | 123463 | 1711 | 4 | 0 |
| StateAct | 3/3 | 33 | 33 | 101.04 s | 110041 | 3141 | 2 | 0 |

| Index | ReAct calls/actions | StateAct calls/actions | ReAct backend | StateAct backend |
|---|---:|---:|---:|---:|
| 0 | 26/15 | 15/15 | 37.99 s | 46.58 s |
| 1 | 20/10 | 10/10 | 21.40 s | 33.13 s |
| 2 | 14/8 | 8/8 | 15.25 s | 21.32 s |

Both methods had zero HTTP errors and zero length-truncated responses.
Monetary cost is unknown for local inference, not zero. Fewer calls did not imply
less backend time here; StateAct generated substantially more output tokens.

## Timing and counter definitions

Backend interval sum includes the call_model template-budget check and chat HTTP
request. It excludes Python startup, environment setup, some external token-count
requests and final process teardown. It is not end-to-end wall clock.

The original initial commands did not wrap the complete child-process lifecycle.
Exact complete-process wall clock is therefore null, not retrospectively invented.
Observed native-runner artifact spans (manifest start to final CSV write) are:
env0 ReAct 63.02s, env0 StateAct 70.88s; indices1/2 ReAct 69.47s, StateAct 88.71s.
These spans include runner/environment work but exclude wrapper/bootstrap and
final teardown. launch.py now records complete future child-Python wall time
explicitly, without rerunning or selectively replacing these three pairs.

- Model calls: actual chat inference attempts, not thought-plus-action parser turns
  inferred from stdout. All 93 attempts have full private request/response records.
- Environment actions: transparent counts of actual native env.step invocations;
  ReAct think turns are excluded.
- No-effect feedback: native feedback exactly Nothing happens. This is not an
  exhaustive semantic/parser-invalidity classification.
- Repeated actions: adjacent executed environment commands identical after
  whitespace normalization. Thoughts are excluded; this is not a claim that all
  non-adjacent repetition is absent or that every repetition is useless.
- Original exact-output repetition counters are also retained, but are not used
  as a semantic repeated-action metric. Some original auxiliary
  num_no_command/num_no_json counters depend on format flags and are unsuitable
  as cross-method parser-accuracy measures.

## Fidelity and artifacts

The native runner, policy prompt generator, few-shot examples, action parser,
command correction and scorer were not edited. llamp BaseLLMSystem is unchanged;
an in-process VLLMChat import shim replaces only the model transport.

Explicit backend deviations: local 9B Q4 llama.cpp replaces official 14B BF16
vLLM; thinking is disabled; output cap is 1024 instead of 2000. Full official
prompts are retained and rejected explicitly on token overflow, replacing the
legacy VLLM wrapper's character-based slicing. All 12 official few-shot variants
fit before live observations: ReAct 1166–1638 tokens, StateAct 1988–3191.

The official OOD text data were extracted unchanged from fixed release archives,
with source URLs/hashes and actual task IDs recorded. No train/seen data or 3D
weights were needed. OOD ALFWorld does not enable expert_plan; policy inputs
never include reference answers, expert trajectories, or admissible-action lists.
Only original official few-shot examples are supplied.

Private ignored .horizon/reproductions/stateact contains source, data, venv,
full requests/responses, original CSV/history/prompt files, scorer outputs and
environment events. Public summary/configuration files independently contain the
source, model and server metadata. One WSL connection timeout for the last official
archive was resolved with Windows curl from the same official release URL.

No global pip, newly launched GPU process, paid API, Cordis or Task IR was used.
No further samples or prompt changes were made after this fixed three-pair run.


Observed bounds across all 93 requests: maximum prompt 13727 characters / 4335 actual chat-template tokens; maximum completion 163 tokens. Thus neither the original 16000-character slicing boundary nor either 1024/2000 output cap bound these three pairs. The adapter boundary differences remain explicit for future longer tasks.
