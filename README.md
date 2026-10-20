# Horizon Weaver

Research on a small executable structure in which agents can learn context operations, generate components, change their composition, and accumulate reusable capabilities.

Development restarts on `codex/learnable-runtime`. The v0 and v1 implementations are preserved in Git rather than carried into the new runtime.

## Historical experiments

Read [the experiment summary and lessons](docs/history/legacy-experiments.md) before interpreting earlier Terminal-Bench results. The old eight tasks were repeatedly used for development and are not a held-out evaluation set.

- Existing `v0.1`, `v0.2`, `v0.3`, and `v0.5` branches retain the original v0 commit history.
- `codex/legacy-v1` preserves the previously uncommitted v1.3 source, tests, design documents, and original experiment reports in commit `86b0e43`.
- `codex/learnable-runtime` contains the cleanup, retrospective, and new implementation.

To inspect the preserved implementation without changing the current checkout:

```sh
git show codex/legacy-v1:horizon_weaver/verification.py
git log --oneline codex/legacy-v1
git diff --stat v0.5..codex/legacy-v1
```

Raw historical runs, third-party source repositories, books, and personal notes remain local and are excluded from new commits. The local `.legacy/` recovery archive also contains a byte-verified copy of the pre-cleanup source.

## Research boundary

The runtime defines references, versions, execution, context operations, budgets, and the provenance of observations. Executable programs decide which information to load, which components to create, how to compose them, and when to submit. Independent task evaluation supplies the score.

An executable prototype and its tests are engineering evidence. Improvements in task accuracy, search efficiency, and transfer require separate matched-budget experiments.

## Run the first substrate

Requires Node.js 22+, Python 3.12+ for trusted fixture tests, and Docker for isolated component execution.

```sh
npm ci
npm test
docker pull python:3.12-alpine
npm run demo
```

For the authored fixture only, `npm run demo -- --trusted-process` runs without Docker. This mode is not a sandbox. Tests use this mode for authored fixtures; `HW_TEST_DOCKER=1 npm test` additionally validates Docker execution. Formal experiments must pin the worker image digest and record the model configuration.

`examples/model-generated.mjs` uses an OpenAI-compatible endpoint configured by `HW_BASE_URL` (including `/v1`), `HW_MODEL`, and optional `HW_API_KEY`. Run `node examples/model-generated.mjs` with Docker available. It generates and invokes a Python component, but does not yet train a proposer.

See [execution semantics and current boundaries](docs/architecture.md). The first slice implements context and component execution. Terminal tools, a learned search policy and fresh benchmark evaluation are still to be implemented.
