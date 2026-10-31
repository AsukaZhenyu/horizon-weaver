"""Report compose topology and build context for the round4 protocol tasks."""
import json
import re
from pathlib import Path

import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'harness'))
import paths
ROOT = paths.require_tasks()
ROUND = paths.ROUND_DIR
tasks = json.loads((ROUND / "protocol.json").read_text(encoding="utf-8"))["tasks"]


def services(text):
    m = re.search(r"^services:\s*$", text, re.M)
    if not m:
        return []
    out = []
    for line in text[m.end():].splitlines():
        if re.match(r"^\S", line) and line.strip():
            break
        if re.match(r"^  [A-Za-z0-9_.-]+:\s*$", line):
            out.append(line.strip()[:-1])
    return out


print(f"{'task':32s} {'services':22s} {'build?':8s} {'dockerfile?':12s}")
flag = []
for t in tasks:
    d = ROOT / t
    dc = d / "docker-compose.yaml"
    text = dc.read_text(encoding="utf-8", errors="replace") if dc.exists() else ""
    svcs = services(text)
    has_build = "build:" in text
    df = (d / "Dockerfile").exists()
    # nested build contexts under client/ or server/
    nested = [p.name for p in d.iterdir() if p.is_dir() and (p / "Dockerfile").exists()]
    mark = ""
    if len(svcs) > 1:
        mark = "  <-- MULTI-SERVICE"
        flag.append(t)
    print(f"{t:32s} {','.join(svcs) or '(none)':22s} {str(has_build):8s} {str(df or nested):12s}{mark}")

print("\nmulti-service:", flag)
