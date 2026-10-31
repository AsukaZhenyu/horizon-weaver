"""Pin images for the round4 protocol and drop tasks that could not be built.

Writes base-images.json (immutable image IDs) and updates protocol.json:
tasks that failed to build are removed and recorded under
environment_exclusions with the reason, per the pre-registered rule.
"""
import json
import subprocess
from pathlib import Path

import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'harness'))
import paths
HERE = paths.RUNS
ROUND = paths.ROUND_DIR
# Historical rounds live outside the repository; regeneration of the H stratum
# pins needs their recorded image IDs. Point this at them when they exist.
import os
ROUND3 = Path(os.environ.get("HW_LEGACY_ROUND3", paths.CACHE / "legacy" / "round3-tb8"))

proto = json.loads((ROUND / "protocol.json").read_text(encoding="utf-8"))
legacy = {e["task"]: e for e in json.loads((ROUND3 / "base-images.json").read_text(encoding="utf-8"))}
builds = {r["task"]: r for r in json.loads((HERE / "build-log.json").read_text(encoding="utf-8"))}


def inspect(image_id: str) -> dict:
    out = subprocess.run(
        ["docker", "image", "inspect", image_id],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr[:500])
    return json.loads(out.stdout)[0]


entries = []
kept = []
excluded = list(proto.get("environment_exclusions", []))
for task in proto["tasks"]:
    if task in legacy:
        e = legacy[task]
        info = inspect(e["id"])
        entries.append({"task": task, "tag": e["tag"], "id": info["Id"], "created": info["Created"],
                        "lookup": "round3 pinned ID re-verified", "stratum": "H"})
        kept.append(task)
        continue
    b = builds.get(task)
    if not b or b["status"] != "built":
        excluded.append({"task": task, "stage": "image-build",
                         "reason": (b or {}).get("error", "not attempted")})
        continue
    info = inspect(b["image_id"])
    entries.append({"task": task, "tag": b["tag"], "id": info["Id"], "created": info["Created"],
                    "lookup": "built by round4 build_images.py", "stratum": "N"})
    kept.append(task)

(ROUND / "base-images.json").write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")

proto["tasks"] = kept
proto["environment_exclusions"] = excluded
(ROUND / "protocol.json").write_text(json.dumps(proto, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

print(f"pinned {len(entries)} images, {len(kept)} tasks kept, {len(excluded)} excluded")
for e in excluded:
    print(f"  EXCLUDED {e['task']:32s} {e['reason'][:90]}")
print("\nkept:", " ".join(kept))
