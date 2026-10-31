"""Report which protocol tasks still need a run, so a batch can resume anywhere.

The runners refuse to repeat an Agent attempt (a case directory that already
carries `agent-started.json` is never run twice).  A resumed batch therefore
has to be given the tasks that have no case yet; this module computes exactly
that set, from the same protocol the runners read.

    python harness/status.py                  # table for both methods
    python harness/status.py --method spine --pending
                                              # comma list for --tasks
Exit code 1 when any method has no pending task but also has unfinished work
(a case that started but never produced a result), so a script can tell the
difference between "done" and "stuck".
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import paths

PROTOCOL = json.loads((paths.ROUND_DIR / "protocol.json").read_text(encoding="utf-8"))
TASKS = PROTOCOL["tasks"]
CASE_ROOT = {"spine": paths.RUNS / "spine" / "cases", "sentry": paths.RUNS / "sentry" / "cases"}
START_MARK = {"spine": "agent-started.json", "sentry": "agent-result.json"}


def state(method, task):
    case = CASE_ROOT[method] / task
    if (case / "result.json").exists():
        return "done"
    if (case / START_MARK[method]).exists():
        return "started"  # an attempt was made; rerunning would repeat it
    if (case / "infrastructure-failure.json").exists():
        return "failed"
    return "pending"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--method", choices=["spine", "sentry", "both"], default="both")
    ap.add_argument("--pending", action="store_true", help="print a comma list for --tasks")
    args = ap.parse_args()
    methods = ["spine", "sentry"] if args.method == "both" else [args.method]

    report = {m: {t: state(m, t) for t in TASKS} for m in methods}
    if args.pending:
        method = methods[0]
        print(",".join(t for t in TASKS if report[method][t] == "pending"))
        return 0

    for method in methods:
        states = report[method]
        counts = {}
        for value in states.values():
            counts[value] = counts.get(value, 0) + 1
        print(f"== {method} ==")
        for task in TASKS:
            print(f"  {task:<32} {states[task]}")
        print("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        print()

    stuck = [
        m for m in methods
        if any(s in ("started", "failed") for s in report[m].values())
    ]
    if stuck:
        print("needs a decision (started or failed case): " + ", ".join(stuck))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
