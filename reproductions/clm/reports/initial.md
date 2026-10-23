# CLM reproduction: initial preparation

Status: engineering reproduction completed; one real model task is partial:16/24, budget_wall, not task completion. See local-sanity.md and local-sanity.json for the final result. The earlier pre-HTTP infrastructure failure is preserved separately.

## Completed

- Official CLM commit: `483198229ab7b90c0eed4651306ab3b3505aaf50`.
- Official ContextBench commit: `fb533c18c78a82d3f1b2047a9c1700735cf829ae`.
- Exact tracked source bytes checked against all 102 CLM and 5736 ContextBench HEAD blobs. Git's Windows/WSL stat-cache disagreed initially; no upstream policy/source changes were made.
- Actual license hashes saved in `upstream.lock.json`.
- Ubuntu Python 3.12.3 isolated venv; 104 third-party packages after the declared LiteLLM mcp extra was installed (including Harbor 0.16.1) frozen separately from the two editable upstream sources.
- Official KV R1000 seed42 generated and packaged: 1000 records, 24 queries, 34 operations. Canonical stream hash matches the released task.
- Official gold-oracle task self-check: 24/24 answers and reward 1.0, from snapshots. This checks the generator/protocol/grader and is **not a model result or an Agent baseline**.
- Nine direct upstream mechanism checks passed: actual-list mutation by the official edit path, protected prefix, rejected over-budget growth, official skill/instruction hashes, and pure-edit/no-op accounting.
- Token counting: `tiktoken:o200k_base`; no Hugging Face model/tokenizer weights downloaded.

## Evidence

- `preparation.json`: source/license/package/config manifest.
- `mechanism-check.json`: direct official-code checks, including retained context 1215 → 10 tokens after the controlled edit.
- Raw generated task and complete preparation logs: ignored `_work/clm/`.
- CPU task image pulled: `python:3.12-slim`, repository digest `sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704`. Actual run manifests will capture image inspection output.

## Local-model configuration, not paper reproduction

The prepared configuration keeps the official `ClmStream`, method skill, task stream, parser, edit gate and context policy. It changes the backbone/backend to the existing Qwen3.5-9B Q4 / llama.cpp service, Agent context to 12288 (effective target 10240), output to 2048, thinking off, and finite 64 task-step / 96 model-call / 1800-second limits. All deviations are enumerated in the config.

Model HTTP requests execute on the Ubuntu host. Only bash commands execute in the Docker task. No container access to the model endpoint is required.

The official pricing fallback returns 0.0 for unpriced local aliases. Raw upstream output is preserved, but our summary reports monetary cost as `null / unknown`, with token counts and timing separately. A zero exit code never substitutes for the official post-run reward.

## Remaining

1. Completed: actual Harbor DockerEnvironment upload/edit/readback, next-request context reconciliation and stream/gold isolation; zero model calls.
2. Completed: one fixed real-model trial after dependency repair; partial16/24, stream27/34, budget_wall.
3. Completed: native scorer, real next-request edit evidence, usage and exact calibrated budget trigger saved; official policy unchanged.


## Additional experiment integrity

- Entire packaged task: 22 file hashes, including task.toml, stream, verifier script, gold (hash only) and vendored graders. They are checked against the preparation manifest before a real run.
- Exactly one configured official HTTP attempt per logical call (CLM_LLM_MAX_RETRIES=1; litellm num_retries=0). No failed request is silently retried.
- Whole Harbor subprocess deadline: 2700 seconds, including setup, agent and verifier; independent process group and cleanup by this trial's exact Compose project label only.
- Five offline supervisor/accounting checks passed; deadline kills the authored test child process group, modified task artifacts are refused, and missing response usage remains unknown.
- Local service identity and hashes: configs/model-service-identity.json and the run's external-service-identity.json. llama.cpp commit is unknown; no folder/version inference is used.

- Infrastructure attempt 20261023T065101668321Z failed before model HTTP in LiteLLM optional MCP imports; one logical counter increment is not a server request. The original logs remain unchanged, without a model score.
- Complete authored HTTP transport fixtures passed: real official query path, bash parser, usage, and 503 no-retry behavior. These are explicitly fixtures rather than model outcomes. The temporary server was closed.
