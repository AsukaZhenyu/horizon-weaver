"""Opt-in tests against a real Docker engine and an already installed image.

Set HORIZON_WEAVER_DOCKER_TESTS=1 and optionally HORIZON_WEAVER_DOCKER_IMAGE.
No image pulls or changes to unrelated containers are performed.
"""
from __future__ import annotations

import json
import os
import shlex
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from horizon_weaver.app import NodeAgent, read_run
from horizon_weaver.attached import AttachedDockerHarness
from horizon_weaver.core import Action, Budget, Check, TaskContract, Termination
from horizon_weaver.harness import DockerHarness, run_process
from horizon_weaver.policy import ScriptedPolicy


def python_command(code: str) -> str:
    return shlex.join(["python", "-c", code])


@unittest.skipUnless(os.environ.get("HORIZON_WEAVER_DOCKER_TESTS") == "1",
                     "real Docker tests require HORIZON_WEAVER_DOCKER_TESTS=1")
class DockerIntegrationTests(unittest.TestCase):
    def test_state_guard_skips_redundant_file_actions_in_real_container(self):
        calls = []
        class CountingHarness(DockerHarness):
            def execute(self, action, timeout_s):
                calls.append(action.kind.value)
                return super().execute(action, timeout_s)
        with tempfile.TemporaryDirectory(prefix="hw-state-docker-") as temporary:
            root = Path(temporary)
            work = root / "work"
            work.mkdir()
            (work / "note.txt").write_text("known observation", encoding="utf-8")
            actions = (Action(kind="read", path="note.txt"), Action(kind="read", path="note.txt"),
                       Action(kind="write", path="answer.txt", content="42"),
                       Action(kind="write", path="answer.txt", content="42"), Action(kind="done"))
            contract = TaskContract(task_id="docker-state", goal="Write answer 42", acceptance_checks=(
                Check(id="external", kind="external"),), budget=Budget(max_steps=5, no_progress_limit=10))
            with patch("horizon_weaver.app.DockerHarness", CountingHarness):
                result = NodeAgent(backend="docker", image=self.image, policy=ScriptedPolicy(actions)).run(
                    contract, work, root / "run")
            graph = read_run(root / "run")
            self.assertEqual(result.termination, Termination.SUBMITTED)
            self.assertEqual(calls, ["read", "write", "done"])
            self.assertEqual([a.tool_executed for a in graph.attempts], [True, False, True, False, True])
            self.assertEqual((work / "answer.txt").read_text(encoding="utf-8"), "42")
            self.assertFalse(graph.ir.verdict.passed)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hw-docker-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.image = os.environ.get("HORIZON_WEAVER_DOCKER_IMAGE", "python:3.12-slim")

    def task(self, *, allowed_paths=(".",)):
        path = "result.json" if allowed_paths == (".",) else "data/result.json"
        return TaskContract(task_id="docker-answer", goal="Write answer 42 in JSON",
            allowed_paths=allowed_paths, expected_artifacts=(path,),
            acceptance_checks=(Check(id="json", kind="json_equals", path=path,
                                     expected='{"answer":42}'),),
            budget=Budget(max_steps=4, wall_time_s=120, tool_timeout_s=10))

    def start_harness(self, task=None):
        harness = DockerHarness(self.work, self.image)
        self.addCleanup(harness.close)
        harness.start(task or self.task(), 45)
        return harness

    def test_command_acceptance_replay_and_container_cleanup(self):
        captured = []

        class TrackedHarness(DockerHarness):
            def start(inner, task, timeout_s=30):
                super().start(task, timeout_s)
                captured.append(inner.container)

        check_command = python_command(
            "import json; from pathlib import Path; "
            "assert json.loads(Path('result.json').read_text()) == {'answer':42}")
        task = self.task()
        task = TaskContract.model_validate({**task.model_dump(), "acceptance_checks":
            (*task.acceptance_checks, Check(id="python", kind="command", command=check_command))})
        write = Action(kind="command", command=python_command(
            "from pathlib import Path; Path('result.json').write_text('{\"answer\":42}\\n')"))
        run_dir = self.root / "run"
        with patch("horizon_weaver.app.DockerHarness", TrackedHarness):
            result = NodeAgent(backend="docker", image=self.image,
                               policy=ScriptedPolicy((write,))).run(task, self.work, run_dir)
        self.assertEqual(result.termination, Termination.SUCCESS, result.reason)
        self.assertEqual(result.steps, 1)
        self.assertEqual(json.loads((self.work / "result.json").read_text()), {"answer": 42})
        self.assertEqual(read_run(run_dir).result, result)
        self.assertEqual(set(result.verdict.satisfied_requirements),
                         {"json", "python", "artifact:result.json"})
        self.assertEqual(len(captured), 1)
        removed = run_process(["docker", "container", "inspect", captured[0]], None, 10)
        self.assertNotEqual(removed.exit_code, 0)
        self.assertIn("No such", removed.output)

    def test_allowed_mounts_and_daemon_security_settings(self):
        (self.work / "data").mkdir()
        (self.work / "private.txt").write_text("outside allowed scope")
        harness = self.start_harness(self.task(allowed_paths=("data",)))
        inspection = run_process(["docker", "container", "inspect", harness.container], None, 10)
        self.assertEqual(inspection.exit_code, 0, inspection.output)
        info = json.loads(inspection.output)[0]
        host = info["HostConfig"]
        self.assertTrue(host["ReadonlyRootfs"])
        self.assertEqual(host["NetworkMode"], "none")
        self.assertIn("ALL", host["CapDrop"])
        self.assertIn("no-new-privileges", host["SecurityOpt"])
        self.assertEqual(host["PidsLimit"], 128)
        self.assertEqual(host["Memory"], 512 * 1024 * 1024)
        self.assertEqual(host["NanoCpus"], 1_000_000_000)
        binds = [mount["Destination"] for mount in info["Mounts"] if mount["Type"] == "bind"]
        self.assertEqual(binds, ["/workspace/data"])
        probe = python_command(
            "from pathlib import Path; "
            "assert not Path('/workspace/private.txt').exists(); "
            "assert {p.name for p in Path('/sys/class/net').iterdir()} == {'lo'}; "
            "Path('/workspace/data/probe.txt').write_text('mounted')")
        executed = harness.execute(Action(kind="command", command=probe), 10)
        self.assertEqual(executed.exit_code, 0, executed.output)
        self.assertEqual((self.work / "data" / "probe.txt").read_text(), "mounted")
        readonly = harness.execute(Action(kind="command", command=python_command(
            "from pathlib import Path; Path('/hw-root-probe').write_text('forbidden')")), 10)
        self.assertNotEqual(readonly.exit_code, 0)
        self.assertIn("Read-only file system", readonly.output)
        self.assertIsNone(harness.snapshot().file("private.txt"))
        denied = harness.execute(Action(kind="read", path="private.txt"), 10)
        self.assertEqual(denied.error_type, "PermissionError")

    def test_timeout_kills_descendants_without_late_writes(self):
        harness = self.start_harness()
        child = ("import time; from pathlib import Path; Path('started.txt').write_text('started'); "
                 "time.sleep(3); Path('late.txt').write_text('late')")
        parent = ("import subprocess, time; subprocess.Popen(['python','-c'," + repr(child)
                  + "]); time.sleep(10)")
        result = harness.execute(Action(kind="command", command=python_command(parent)), 1)
        self.assertEqual(result.error_type, "timeout", result.output)
        self.assertFalse(result.outcome_unknown)
        self.assertEqual(result.exit_code, 124)
        self.assertTrue(result.execution_terminated)
        self.assertTrue((self.work / "started.txt").exists(), "the child must actually have started")
        time.sleep(3.2)
        self.assertFalse((self.work / "late.txt").exists())
        responsive = harness.execute(Action(kind="command", command="printf ready"), 10)
        self.assertEqual(responsive.output, "ready")
        self.assertEqual(responsive.exit_code, 0)

    def test_completed_command_leaves_no_background_descendants(self):
        harness = self.start_harness()
        child = ("import time; from pathlib import Path; Path('started.txt').write_text('started'); "
                 "time.sleep(2); Path('late.txt').write_text('late')")
        parent = ("import subprocess, time; from pathlib import Path; "
                  "subprocess.Popen(['python','-c'," + repr(child)
                  + "], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                  "deadline=time.monotonic()+3\n"
                  "while not Path('started.txt').exists() and time.monotonic()<deadline: time.sleep(.01)\n"
                  "assert Path('started.txt').exists()")
        result = harness.execute(Action(kind="command", command=python_command(parent)), 10)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue((self.work / "started.txt").exists(), "the child must actually have started")
        time.sleep(2.2)
        self.assertFalse((self.work / "late.txt").exists(),
                         "a completed command must not mutate the workspace later")

    def test_attached_candidate_keeps_container_for_hidden_grading(self):
        owner = self.start_harness()
        attached = AttachedDockerHarness(owner.container, workspace="/workspace", image=self.image)
        task = TaskContract(task_id="hidden", goal="Write candidate",
            acceptance_checks=(Check(id="hidden-grader", kind="external"),),
            require_self_check=True, budget=Budget(max_steps=5, wall_time_s=60, tool_timeout_s=1))
        actions = (Action(kind="verify", command=python_command("import time; time.sleep(10)")),
                   Action(kind="write", path="result.json", content='{"answer":42}\r\n'),
                   Action(kind="read", path="result.json"),
                   Action(kind="verify", command=python_command(
                       "import json; from pathlib import Path; assert json.loads(Path('result.json').read_text())=={'answer':42}")),
                   Action(kind="done"))
        result = NodeAgent(policy=ScriptedPolicy(actions)).run_attached(
            task, attached, self.root / "attached-run", workspace_id=f"docker:{owner.container}:/workspace")
        self.assertEqual(result.termination, Termination.SUBMITTED, result.reason)
        self.assertFalse(result.verdict.passed)
        self.assertEqual((self.work / "result.json").read_bytes(), b'{"answer":42}\r\n')
        attempts = read_run(self.root / "attached-run").attempts
        self.assertEqual(attempts[0].result.error_type, "timeout")
        self.assertTrue(attempts[0].result.execution_terminated)
        self.assertFalse(attempts[0].self_check.passed)
        self.assertEqual(attempts[2].result.error_type, "redundant_action")
        self.assertFalse(attempts[2].tool_executed)
        self.assertEqual(attempts[1].action.content, '{"answer":42}\r\n')
        self.assertTrue(attempts[3].self_check.passed)
        self.assertEqual(attempts[3].self_check.snapshot_hash, result.snapshot.fingerprint)
        # The caller can still run its own grader after the agent releases the adapter.
        grade = owner.execute(Action(kind="command", command=python_command(
            "import json; from pathlib import Path; assert json.loads(Path('result.json').read_text())=={'answer':42}")), 10)
        self.assertEqual(grade.exit_code, 0, grade.output)

    def test_attached_file_scopes_and_command_timeout(self):
        (self.work / "data").mkdir()
        (self.work / "private.txt").write_text("private")
        owner = self.start_harness()
        attached = AttachedDockerHarness(owner.container, workspace="/workspace")
        self.addCleanup(attached.close)
        attached.start(self.task(allowed_paths=("data",)))
        link = owner.execute(Action(kind="command", command="ln -s /workspace/private.txt /workspace/data/escape"), 10)
        self.assertEqual(link.exit_code, 0, link.output)
        denied = attached.execute(Action(kind="read", path="data/escape"), 10)
        self.assertEqual(denied.error_type, "PermissionError")
        self.assertEqual(attached.snapshot().file("data/escape").kind, "symlink")
        child = "import time; from pathlib import Path; Path('data/started').write_text('1'); time.sleep(2); Path('data/late').write_text('1')"
        parent = "import subprocess,time; subprocess.Popen(['python','-c'," + repr(child) + "]); time.sleep(10)"
        timeout = attached.execute(Action(kind="command", command=python_command(parent)), 1)
        self.assertEqual(timeout.error_type, "timeout", timeout.output)
        self.assertFalse(timeout.outcome_unknown)
        self.assertEqual(timeout.exit_code, 124)
        self.assertTrue(timeout.execution_terminated)
        self.assertTrue((self.work / "data" / "started").exists())
        time.sleep(2.2)
        self.assertFalse((self.work / "data" / "late").exists())
        self.assertEqual(owner.execute(Action(kind="command", command="printf alive"), 10).output, "alive")


if __name__ == "__main__":
    unittest.main()
