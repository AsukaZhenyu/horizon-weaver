# v1.3 exploration and verification demo

This deterministic example writes a wrong candidate, records its counterexample,
retrieves that failure without re-executing tools, rejects premature submission,
repairs the candidate, and reruns the same predicate. It uses the actual loop,
event journal, State IR and verification executor; no model is called.

From the repository root, choose fresh workspace and run directories:

```powershell
New-Item -ItemType Directory -Force runs/v13-demo/work
python -m horizon_weaver run --contract examples/v1.3/task.json --actions examples/v1.3/actions.json --backend local --workspace runs/v13-demo/work --run-dir runs/v13-demo/trace
python -m horizon_weaver inspect --run-dir runs/v13-demo/trace --state-ir
```

For the installed Docker fixture, replace `--backend local` with
`--backend docker --image ghcr.io/laude-institute/t-bench/python-3-13:20250620`.

Expected runtime result: `submitted` after seven decisions. Step 2 fails, step 3
retrieves the retained failure, step 4 returns `verification_required`, and step 6
passes on the repaired state. The external grade is deliberately pending: a
policy self-check cannot grant task success. Full check definitions, results and
source event hashes are available in the exported State IR.
