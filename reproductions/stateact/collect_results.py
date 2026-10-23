"""Collect actual native checks and private transport logs; never infer success from text."""
import csv
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE.parents[1] / ".horizon/reproductions/stateact"
TRIALS = ["env0-react-20261023a", "env0-stateact-20261023a",
          "env1-2-react-20261023a", "env1-2-stateact-20261023a"]


def jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def collect():
    rows, runs = [], []
    for trial in TRIALS:
        base = CACHE / "runs" / trial
        manifest = json.loads((base / "manifest.json").read_text())
        csv_path = base / "game_logs" / ("alfworld_eval_" + trial) / "alfworld_results.csv"
        with csv_path.open(newline="") as stream:
            native_rows = list(csv.DictReader(stream))
        if len(native_rows) != manifest["num_envs"]:
            raise RuntimeError("Missing or duplicated native rows: " + trial)
        if len({row["env_idx"] for row in native_rows}) != len(native_rows):
            raise RuntimeError("Duplicate task indices: " + trial)
        calls = jsonl(base / "transport.jsonl")
        events = jsonl(base / "environment.jsonl")
        resets = {event["episode"]: event for event in events if event["event"] == "reset"}
        scorer = (base / "scorer.txt").read_text().strip()
        expected = sum(row["success"] == "True" for row in native_rows)
        if f"with {expected} successful envs and a total of {len(native_rows)} envs." not in scorer:
            raise RuntimeError("Official scorer result absent or inconsistent: " + trial)
        start = datetime.fromisoformat(manifest["started_utc"]).timestamp()
        runs.append({
            "trial": trial, "official_scorer_output": scorer,
            "process_wall_clock_s": None,
            "wall_clock_note": "Not measured around the original entire process; no retrospective estimate labelled exact.",
            "native_runner_artifact_span_s": csv_path.stat().st_mtime - start,
            "native_runner_span_definition": "manifest start to final CSV write; excludes wrapper/bootstrap and final process teardown",
        })
        for native in native_rows:
            index = int(native["env_idx"])
            selected = [call for call in calls
                        if call.get("environment_index", manifest["task_start_index"]) == index]
            if not selected:
                raise RuntimeError("Missing model attempts")
            steps = [event for event in events if event["event"] == "step" and event["episode"] == index]
            first_raw = json.loads((base / selected[0]["raw_record"]).read_text())
            prompt = first_raw["request"]["messages"][0]["content"]
            prefix = (base / native["prompt_file"]).read_text()
            if not prompt.startswith(prefix):
                raise RuntimeError("Official complete few-shot prefix not preserved")
            initial_observation = prompt[len(prefix):]
            for call in selected:
                raw = json.loads((base / call["raw_record"]).read_text())
                actual = raw["request"]["messages"][0]["content"]
                if hashlib.sha256(actual.encode()).hexdigest() != call["prompt_sha256"]:
                    raise RuntimeError("Prompt audit hash mismatch")
                if call.get("usage", {}).get("prompt_tokens") != call.get("prompt_tokens_preflight"):
                    raise RuntimeError("Server usage and template tokenizer disagree")
            commands = [re.sub(r"\s+", " ", step["action"][0]).strip() for step in steps]
            duplicates = sum(a == b for a, b in zip(commands, commands[1:]))
            row = {
                "trial": trial, "method": manifest["method"], "env_idx": index,
                "category": native["env_type"], "success": native["success"] == "True",
                "native_done": native["done"] == "True",
                "task_id": resets[index]["task_id"],
                "gamefile_sha256": resets[index]["gamefile_sha256"],
                "policy_initial_observation_sha256": hashlib.sha256(initial_observation.encode()).hexdigest(),
                "initial_observation_and_goal": initial_observation,
                "model_calls": len(selected), "environment_actions": len(steps),
                "backend_seconds": sum(call["time_s"] for call in selected),
                "prompt_tokens": sum(call["usage"].get("prompt_tokens", 0) for call in selected),
                "completion_tokens": sum(call["usage"].get("completion_tokens", 0) for call in selected),
                "http_errors": sum(call["status"] == "error" for call in selected),
                "truncated_outputs": sum(call["truncated"] for call in selected),
                "no_effect_feedback_count": int(native["num_nothing_happens"]),
                "consecutive_identical_environment_actions": duplicates,
                "official_exact_output_repetitions": int(native["num_repetitions"]),
                "error": native["error"], "early_stop": native["early_stop"],
                "cost_usd": None, "cost_status": "unknown-local-inference",
            }
            if not steps or bool(steps[-1]["won"]) != row["success"]:
                raise RuntimeError("Native checker and CSV disagree")
            rows.append(row)
    for index in [0, 1, 2]:
        pair = [row for row in rows if row["env_idx"] == index]
        if len(pair) != 2:
            raise RuntimeError("Unpaired environment index")
        for field in ["task_id", "gamefile_sha256", "policy_initial_observation_sha256"]:
            if pair[0][field] != pair[1][field]:
                raise RuntimeError(f"Pairing mismatch: env{index}, {field}")
    aggregates = {}
    for method in ["react", "stateact"]:
        selected = [row for row in rows if row["method"] == method]
        aggregates[method] = {"successes": sum(row["success"] for row in selected),
                              "tasks": len(selected)}
        for field in ["model_calls", "environment_actions", "backend_seconds", "prompt_tokens",
                      "completion_tokens", "no_effect_feedback_count",
                      "consecutive_identical_environment_actions", "http_errors", "truncated_outputs"]:
            aggregates[method][field] = sum(row[field] for row in selected)
    result = {"scope": "Local 9B-Q4 sanity check, not original 14B accuracy replication",
              "pairing_checks_passed": True, "rows": rows, "runs": runs, "aggregates": aggregates,
              "no_effect_definition": "Native feedback exactly Nothing happens.; not an exhaustive parser/semantic invalidity label",
              "repeat_definition": "Adjacent executed environment commands identical after whitespace normalization; thoughts excluded; no claim of semantic futility"}
    (HERE / "paired-three-results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(aggregates, indent=2))


if __name__ == "__main__":
    collect()
