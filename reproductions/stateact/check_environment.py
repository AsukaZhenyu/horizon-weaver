"""Check native task loading and independent initial states; no model calls."""
import hashlib
import json
import os
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / ".horizon" / "reproductions" / "stateact"
os.environ["ALFWORLD_DATA"] = str(CACHE / "data")
from alfworld.agents.environment import get_environment

config = yaml.safe_load((CACHE / "upstream/alfworld_runs/base_config.yaml").read_text())
checks = []
task_ids = None
for repeat in range(2):
    builder = get_environment(config["env"]["type"])(config, train_eval="eval_out_of_distribution")
    current_ids = [str(Path(path).resolve().relative_to((CACHE / "data").resolve()))
                   for path in builder.game_files]
    if task_ids is None:
        task_ids = current_ids
    elif task_ids != current_ids:
        raise RuntimeError("Native task ordering changed across independent instances")
    env = builder.init_env(batch_size=1)
    observation, info = env.reset()
    checks.append({
        "task_id": str(Path(info["extra.gamefile"][0]).resolve().relative_to((CACHE / "data").resolve())),
        "initial_observation_sha256": hashlib.sha256(observation[0].encode()).hexdigest(),
        "initial_won": bool(info["won"][0]),
    })
    env.close()
if checks[0] != checks[1]:
    raise RuntimeError("Independent initial states are not identical")
if len(task_ids) != 134:
    raise RuntimeError(f"Expected 134 native OOD tasks, found {len(task_ids)}")
result = {"native_task_count": len(task_ids), "native_catalogue_task_ids": task_ids,
          "independent_resets": checks, "model_calls": 0,
          "expert_plan_or_admissible_actions_sent_to_policy": False}
(CACHE / "environment-check.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps({"native_task_count": len(task_ids), "independent_resets": checks}, indent=2))
