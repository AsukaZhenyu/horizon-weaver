"""Observers write reports without changing control or acceptance decisions."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .core import RunResult


class JsonReporter:
    def write(self, result: RunResult, run_dir: Path) -> Path:
        destination = run_dir / "result.json"
        fd, temporary = tempfile.mkstemp(prefix=".report-", dir=run_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(result.model_dump_json(indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return destination

