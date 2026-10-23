"""Authored code only: verify the real Docker tool boundary without calling an LLM."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "_work/mage/upstream/benchmark/math"))
sys.path.insert(0, str(ROOT / "_work/mage/upstream"))
from math_actions import CodeExecutorAction

def main():
    config = json.loads((HERE / "configs/math1-sanity.json").read_text())
    run_id = "containment-" + uuid.uuid4().hex[:12]
    os.environ.update(MAGE_CODE_IMAGE=config["code_image"], MAGE_RUN_ID=run_id,
                      DOCKER_CONTEXT=config["docker_context"])
    code = r'''
import json, os, pathlib, socket
facts = {
    "uid": os.getuid(),
    "gold_file_visible": pathlib.Path("/input/task.json").exists(),
    "agent_source_visible": pathlib.Path("/app/upstream").exists(),
    "docker_socket_visible": pathlib.Path("/var/run/docker.sock").exists(),
    "api_key_visible": bool(os.environ.get("OPENAI_API_KEY")),
}
try:
    pathlib.Path("/containment-write-probe").write_text("should fail")
    facts["root_write_denied"] = False
except OSError:
    facts["root_write_denied"] = True
connection = socket.socket()
connection.settimeout(1)
try:
    connection.connect(("172.21.80.1", 8080))
    facts["network_denied"] = False
except OSError:
    facts["network_denied"] = True
finally:
    connection.close()
print(json.dumps(facts))
'''
    started = time.monotonic()
    tool = CodeExecutorAction()
    tool.TIMEOUT_SECONDS = 30
    result = tool(code=code)
    if not result.startswith("Code Output:\n"):
        raise AssertionError(result)
    facts = json.loads(result.split("\n", 1)[1])
    expected = dict(uid=65534, gold_file_visible=False, agent_source_visible=False,
                    docker_socket_visible=False, api_key_visible=False,
                    root_write_denied=True, network_denied=True)
    if facts != expected:
        raise AssertionError(facts)
    tool.TIMEOUT_SECONDS = 2
    timed = tool(code="import time; time.sleep(60)")
    if "timed out after 2 seconds" not in timed:
        raise AssertionError(timed)
    remaining = subprocess.check_output(
        ["docker", "--context", config["docker_context"], "ps", "-aq",
         "--filter", f"label=hw.mage.run={run_id}"], text=True).strip()
    if remaining:
        raise AssertionError(f"Leaked code containers: {remaining}")
    report = dict(status="passed", facts=facts, timeout_cleanup=True,
                  elapsed_seconds=time.monotonic()-started, model_calls=0)
    path = ROOT / "_work/mage/sandbox-check.json"
    path.write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report,indent=2))

if __name__ == "__main__":
    main()
