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
