"""Observation-preserving counters around the native ALFWorld environment."""
import hashlib
import json
import time
from pathlib import Path


def install_environment_audit(log_path, data_root):
    import alfworld.agents.environment as environment
    original_factory = environment.get_environment
    log_path = Path(log_path)
    data_root = Path(data_root).resolve()

    def write(record):
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")

    class AuditedBatch:
        def __init__(self, native):
            self.native = native
            self.episode = -1
            self.actions = 0

        def __getattr__(self, name):
            return getattr(self.native, name)

        def reset(self, *args, **kwargs):
            result = self.native.reset(*args, **kwargs)
            self.episode += 1
            self.actions = 0
            observation, info = result
            gamefile = Path(info["extra.gamefile"][0]).resolve()
            write({"event": "reset", "episode": self.episode,
                   "task_id": str(gamefile.relative_to(data_root)),
                   "gamefile_sha256": hashlib.sha256(gamefile.read_bytes()).hexdigest()})
            return result

        def step(self, actions, *args, **kwargs):
            started = time.perf_counter()
            result = self.native.step(actions, *args, **kwargs)
            self.actions += 1
            observation, reward, done, info = result
            write({"event": "step", "episode": self.episode,
                   "environment_action": self.actions, "action": actions,
                   "won": bool(info["won"][0]), "done": bool(done[0]),
                   "reward": float(reward[0]), "time_s": time.perf_counter() - started})
            return result

    def audited_factory(name):
        native_type = original_factory(name)

        class Builder:
            def __init__(self, *args, **kwargs):
                self.native = native_type(*args, **kwargs)

            def init_env(self, *args, **kwargs):
                return AuditedBatch(self.native.init_env(*args, **kwargs))

        return Builder

    environment.get_environment = audited_factory
