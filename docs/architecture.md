# Minimal execution substrate

The first implementation makes references, context operations and executable composition concrete. It does not yet implement a learned proposer, terminal tools, checkpoint recovery, or Terminal-Bench integration. Its deterministic examples establish execution behavior, not task-solving improvements.

## Values and executables

`runtime/values.mjs` stores immutable JSON records addressed by SHA-256 over content and provenance. A record includes its parents and host-assigned producer. Equal text from distinct producers can have distinct references. Reads verify integrity.

An executable is a value containing `abi`, a source reference, dependency `bindings`, and requested `grants`. The initial ABI is a Python function `main(input, api)` returning JSON. Composition uses ordinary code and `api.request('call', target=..., input=...)`. Definition identity includes source and exact dependency references.

## Host and component boundary

`runtime/host.mjs` registers a service with the actual Cordis 4.0.1 package. The service owns value access, component dispatch, shared budgets, model transport and append-only run journals. It does not assign planner, critic, memory or verifier roles.

Components execute through `sdk/python/worker.py`. The default Docker worker has no network, no writable host mount, no model credentials, and a read-only filesystem. Only the SDK file is mounted. All host operations use a JSON RPC boundary; references must be available to the current run. A component may not acquire capabilities that its caller lacks. A subsequent run can explicitly use a saved executable and its dependencies.

`trusted-process` is an explicit development mode for authored fixtures. It is **not a sandbox**: its Python code has the user's filesystem/process privileges. Do not use it for model-generated or otherwise untrusted code. The model-generation example always uses Docker.

The initial journal records call identities, parent calls, exact input/output references, operation arguments/results and failures. Budgets are shared by nested calls. Worker deadlines are enforced separately from cooperative component behavior. Docker removal on timeout is currently best effort, and crash recovery of orphan workers remains future work.

## Context operations

`values.search` performs literal search over values available to the run and returns explicit pagination. A view is an immutable ordered list of references. `views.load` loads an item for the next accepted model invocation; `views.retain` specifies a positive invocation count or indefinite retention; `views.evict` removes it from the view without deleting its source. Each operation returns a new view reference.

`model.invoke` records the actual messages passed to the adapter and returns a continuation view with consumed residence counts. A rejected oversized request does not call the model. Once dispatched, a model failure returns the continuation view with an error. The initial capacity measurement is UTF-8 bytes of serialized messages, **not tokenizer counts**; provider-reported usage is preserved in replies. Token-aware accounting and additional protocol overhead accounting are follow-up work.

## Evaluation and learning boundary

Returning from the entry component submits its output; no semantic reflection or verification gate is required. `experiments/evaluate.mjs` independently compares JSON fixture outputs with expected values that are not passed to components. It is a narrow evaluator for engineering fixtures.

`compare` executes supplied candidates and selects by passed tasks, then operation count. It does not generate candidates, enforce a token-level matched-compute budget, or establish learning/transfer results. `seeds/generate.py` demonstrates model-generated code; `examples/context-and-components.mjs` uses an authored builder. A learned proposer, reusable library selection, task environments and real matched-budget experiments are the next vertical slice.

## Current verification

The Node test suite exercises actual Cordis lifecycle, immutable storage, context operations, generated component execution and reuse, reference access checks, nested capability/budget checks, deadlines, evaluator separation and the model adapter protocol. Ordinary tests use authored Python and a deterministic adapter. `HW_TEST_DOCKER=1 npm test` additionally exercises the Docker path and fails if the daemon or image is unavailable.

The historical implementations and evaluation lessons are documented in [the retrospective](history/legacy-experiments.md). They remain available on `codex/legacy-v1` and `claude-code/legacy-v0`.
