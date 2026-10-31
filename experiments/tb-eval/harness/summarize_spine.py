"""Summarize round4 Spine cases without recomputing any verdict.

Same evidence rules as round3: one row per task, recovery pointers define any
substitution, and a missing result stays null instead of being read as failure.
Round4 adds the stratum/group labels so the 8 historical tasks and the 16 new
tasks can be reported separately and per bucket.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import paths

CASES = paths.RUNS / "spine" / "cases"
PROTOCOL = json.loads((paths.ROUND_DIR / "protocol.json").read_text(encoding="utf-8"))
TASKS = PROTOCOL["tasks"]

SELECTION_PATH = paths.TB_EVAL / "config" / "selection.json"
LABELS = {}
if SELECTION_PATH.exists():
    for row in json.loads(SELECTION_PATH.read_text(encoding="utf-8"))["selected"]:
        if row.get("task"):
            LABELS[row["task"]] = {"stratum": row["stratum"], "group": row["group"]}

USAGE_RE = re.compile(r"completion_tokens\\?\":\s*(\d+),\s*\\?\"prompt_tokens\\?\":\s*(\d+)")


def load_json(path):
    # utf-8-sig: a case file written by a Windows tool can carry a BOM.
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def effective_case(task):
    """Return (case_dir, source_note). Recovery is used only when a pointer says so."""
    base = CASES / task
    pointer = base / "execution-recovery.json"
    if pointer.exists():
        record = load_json(pointer)
        # caseDir is recorded as an absolute path on the machine that ran it.
        # Re-anchor on this machine's case root so a summary can be rebuilt from
        # evidence synced over from another machine.
        recorded = Path(record["caseDir"])
        local = CASES / recorded.name
        return (local if local.exists() or not recorded.is_absolute() else recorded), {
            "kind": "execution-recovery",
            "reason": record.get("reason"),
            "sourceCase": record.get("sourceCase"),
            "priorRequests": record.get("priorRequests"),
            "priorActions": record.get("priorActions"),
        }
    return base, None


def token_usage(case_dir):
    prompt = completion = cached = requests_with_usage = 0
    trace = case_dir / "http-trace.jsonl"
    if trace.exists():
        with trace.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "usage" not in line:
                    continue
                match = USAGE_RE.search(line)
                if match:
                    completion += int(match.group(1))
                    prompt += int(match.group(2))
                    requests_with_usage += 1
                cached_match = re.search(r"cached_tokens\\?\":\s*(\d+)", line)
                if cached_match:
                    cached += int(cached_match.group(1))
    return {
        "promptTokens": prompt,
        "completionTokens": completion,
        "totalTokens": prompt + completion,
        "cachedPromptTokens": cached,
        "responsesWithUsage": requests_with_usage,
    }


def main():
    rows = []
    deviations = []
    for task in TASKS:
        case_dir, recovery = effective_case(task)
        result_path = case_dir / "effective-result.json"
        if not result_path.exists():
            result_path = case_dir / "result.json"
        if not result_path.exists():
            # Same start mark as status.py: an attempt exists only once the
            # Agent was actually launched, so an empty directory stays "pending"
            # rather than being reported as an invalid grading.
            rows.append({"task": task, "case": case_dir.name,
                         "attempted": (case_dir / "agent-started.json").exists(),
                         "A": None, "V": None, "Y": None, **LABELS.get(task, {})})
            continue
        result = load_json(result_path)
        grading = result.get("grading", {})
        row = {
            "task": task,
            "case": case_dir.name,
            "attempted": True,
            **LABELS.get(task, {}),
            "A": result.get("A"),
            "V": result.get("V"),
            "Y": result.get("Y"),
            "gradingValid": grading.get("valid"),
            "gradingCounts": grading.get("counts"),
            "expectedChecks": grading.get("expectedChecks"),
            "gradingProtocol": grading.get("protocol"),
            "gradingSeconds": grading.get("gradingSeconds"),
            "agentWallSeconds": result.get("agentWallSeconds"),
            "fullTrialSeconds": result.get("fullTrialSeconds"),
            "modelRequests": result.get("modelRequests"),
            "apiAttempts": result.get("apiAttempts"),
            "httpStatuses": result.get("httpStatuses"),
            "toolCounts": result.get("toolCounts"),
            "toolErrors": result.get("toolErrors"),
            "exactBashCommands": result.get("exactBashCommands"),
            "repeatedExactBashCommands": result.get("repeatedExactBashCommands"),
            "finalStopReason": result.get("finalStopReason"),
            "deadlineReached": result.get("deadlineReached"),
            "tokens": token_usage(case_dir),
        }
        if recovery:
            row["recovery"] = recovery
            deviations.append({"task": task, **recovery})
        grading_recovery = result.get("gradingRecovery")
        if grading_recovery:
            row["gradingRecovery"] = grading_recovery
            deviations.append({
                "task": task,
                "kind": "grading-recovery",
                "directory": grading_recovery.get("directory"),
                "newModelCalls": grading_recovery.get("newModelCalls"),
                "newAgentActions": grading_recovery.get("newAgentActions"),
                "sameCandidateImageId": grading_recovery.get("sameCandidateImageId"),
            })
        rows.append(row)

    def count(key, value, subset=None):
        rows_ = subset if subset is not None else rows
        return sum(1 for row in rows_ if row.get(key) == value)

    def block(subset):
        # A task that was never attempted has A=None too, but that is "no
        # evidence", not "invalid grading"; keep the two apart so 24 unattempted
        # tasks do not read as 24 broken graders.
        attempted = [r for r in subset if r.get("attempted")]
        return {
            "n": len(subset),
            "attempted": len(attempted),
            "A": count("A", 1, subset),
            "AInvalid": sum(1 for r in attempted if r.get("A") is None),
            "V": count("V", 1, subset),
            "Y": count("Y", 1, subset),
        }

    strata = {}
    for name in ("H", "N"):
        subset = [r for r in rows if r.get("stratum") == name]
        if subset:
            strata[name] = block(subset)
    groups = {}
    for group in sorted({r.get("group") for r in rows if r.get("group")}):
        groups[group] = block([r for r in rows if r.get("group") == group])

    summary = {
        "round": PROTOCOL["round"],
        "method": "spine",
        "tasksAttempted": len(rows),
        **block(rows),
        "strata": strata,
        "groups": groups,
        "totalModelRequests": sum(row.get("modelRequests") or 0 for row in rows),
        "totalPromptTokens": sum(row["tokens"]["promptTokens"] for row in rows if row.get("tokens")),
        "totalCompletionTokens": sum(row["tokens"]["completionTokens"] for row in rows if row.get("tokens")),
        "totalTokens": sum(row["tokens"]["totalTokens"] for row in rows if row.get("tokens")),
        "totalAgentWallSeconds": round(sum(row.get("agentWallSeconds") or 0 for row in rows), 3),
        "totalTrialSeconds": round(sum(row.get("fullTrialSeconds") or 0 for row in rows), 3),
        "totalRepeatedExactBashCommands": sum(row.get("repeatedExactBashCommands") or 0 for row in rows),
        "deviations": deviations,
        "rows": rows,
    }
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else paths.RUNS / "spine" / "summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("rows", "deviations")}, indent=2))
    for row in rows:
        print(
            f"{row['task']:<32} {row.get('stratum','?'):1s} A={str(row['A']):>4} V={row['V']} Y={str(row['Y']):>4} "
            f"req={row.get('modelRequests') or 0:>2} wall={(row.get('agentWallSeconds') or 0):>7.3f}s "
            f"tok={(row.get('tokens') or {}).get('totalTokens',0):>6} grade={row.get('gradingCounts')}"
        )
    print(f"wrote {out}")


if __name__ == "__main__":
    sys.exit(main())
