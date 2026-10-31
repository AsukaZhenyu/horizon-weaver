"""Export the compact, syncable part of a run into results/<round>/.

Why this exists: a case directory holds the evidence (app tars, full HTTP
traces, session archives) and is far too large for version control, while the
verdicts and aggregated summaries are small and are exactly what has to travel
between machines.  This copies the small part out, keys it by task, and writes
RESULTS.md from it.

    python harness/export_results.py            # writes into results/<round>/
    python harness/export_results.py --check    # report only, write nothing

Copied per case: result.json and, when present, agent-result.json and
grading/result.json.  Everything else stays in the run directory and is
referenced by path, not published, so a reader can see what was not exported.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import paths

PROTOCOL = json.loads((paths.ROUND_DIR / "protocol.json").read_text(encoding="utf-8"))
TASKS = PROTOCOL["tasks"]
ROUND_ID = PROTOCOL["round"]
LABELS = {}
for row in json.loads((paths.TB_EVAL / "config" / "selection.json").read_text(encoding="utf-8"))["selected"]:
    if row.get("task"):
        LABELS[row["task"]] = {"stratum": row["stratum"], "group": row["group"]}

METHODS = {
    "spine": {
        "cases": paths.RUNS / "spine" / "cases",
        "summary": paths.RUNS / "spine" / "summary.json",
        "title": "Pi 1.1.0 + @spinejit/pi-spinejit 0.1.3",
    },
    "sentry": {
        "cases": paths.RUNS / "sentry" / "cases",
        "summary": paths.RUNS / "sentry" / "summary.json",
        "title": "Sentry 0.11.0 core + controlled terminal host",
    },
}
MAX_CASE_BYTES = 512 * 1024


def compact(case_dir):
    """The files that carry a verdict, or nothing if the case never finished."""
    files = {}
    for name in ("result.json", "agent-result.json", "grading/result.json"):
        path = case_dir / name
        if path.is_file():
            files[name] = path
    return files


def export_method(method, out_root, check):
    spec = METHODS[method]
    out = out_root / method
    rows = {}
    skipped = []
    for task in TASKS:
        case = spec["cases"] / task
        if not case.is_dir():
            continue
        files = compact(case)
        if not files:
            continue
        size = sum(p.stat().st_size for p in files.values())
        if size > MAX_CASE_BYTES:
            skipped.append((task, size))
            continue
        if not check:
            (out / "cases").mkdir(parents=True, exist_ok=True)
            payload = {name: json.loads(p.read_text(encoding="utf-8")) for name, p in files.items()}
            (out / "cases" / f"{task}.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        rows[task] = files
    if spec["summary"].is_file():
        if not check:
            out.mkdir(parents=True, exist_ok=True)
            shutil.copy2(spec["summary"], out / "summary.json")
        summary = json.loads(spec["summary"].read_text(encoding="utf-8"))
    else:
        summary = None
    return rows, summary, skipped


def markdown(exported):
    lines = [
        f"# 实验结果：{ROUND_ID}",
        "",
        "由 `python harness/export_results.py` 生成，勿手工编辑；解读写在 `ANALYSIS.md`。",
        "",
        "A = 冻结产物通过原始测试；V = 预算内正常提交；Y = A×V。A 为 null 表示评分无效，不当作 0。",
        "",
    ]
    for method, (rows, summary, skipped) in exported.items():
        lines += [f"## {METHODS[method]['title']}", ""]
        if summary is None:
            lines += ["本轮尚无汇总（`summary.json` 未生成）。", ""]
            continue
        lines += [
            f"n={summary.get('n', summary.get('tasksAttempted', 0))}  "
            f"A={summary.get('A', summary.get('artifactPassCount', 0))}  "
            f"AInvalid={summary.get('AInvalid', len(TASKS) - summary.get('knownArtifactGrades', len(TASKS)))}  "
            f"V={summary.get('V', summary.get('normalSubmissionCount', 0))}  "
            f"Y={summary.get('Y', summary.get('endToEndPassCount', 0))}",
            "",
            "| 任务 | 层 | 组 | A | V | Y | 模型请求 | Agent 秒 | 评分 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        task_rows = {r["task"]: r for r in summary.get("rows", [])}
        for task in TASKS:
            if summary.get("rows") is not None:
                row = task_rows.get(task, {})
                A, V, Y = row.get("A"), row.get("V"), row.get("Y")
                req, wall = row.get("modelRequests"), row.get("agentWallSeconds")
                counts = row.get("gradingCounts")
            else:
                match = next((t for t in summary.get("tasks", []) if t.get("task") == task), None)
                if not match:
                    continue
                A, V, Y = match.get("A"), match.get("V"), match.get("Y")
                req, wall = match.get("modelRequests"), match.get("agentWallSeconds")
                counts = None
            label = LABELS.get(task, {})
            lines.append(
                f"| {task} | {label.get('stratum','?')} | {label.get('group','?')} | "
                f"{('-' if A is None else A)} | {('-' if V is None else V)} | "
                f"{('-' if Y is None else Y)} | {req if req is not None else '-'} | "
                f"{'%.1f' % wall if isinstance(wall, (int, float)) else '-'} | "
                f"{json.dumps(counts, ensure_ascii=False) if counts else '-'} |"
            )
        lines.append("")
        if skipped:
            lines.append("未导出的用例（超过体积上限，证据留在运行目录）：")
            for task, size in skipped:
                lines.append(f"- {task} ({size/1024:.0f} KiB)")
            lines.append("")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    out_root = paths.TB_EVAL / "results" / ROUND_ID
    exported = {}
    for method in METHODS:
        rows, summary, skipped = export_method(method, out_root, args.check)
        exported[method] = (rows, summary, skipped)
        print(f"{method}: {len(rows)} case(s) with results, summary={'yes' if summary else 'no'}")
        for task, size in skipped:
            print(f"  skipped {task} ({size/1024:.0f} KiB over the export limit)")
    if not args.check:
        out_root.mkdir(parents=True, exist_ok=True)
        (out_root / "RESULTS.md").write_text(markdown(exported), encoding="utf-8")
        print("wrote", out_root / "RESULTS.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
