"""Transactional, checksum-validated event journal."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from .core import SCHEMA_VERSION, RunEvent, canonical, digest


class StoreError(RuntimeError):
    pass


class EventStore:
    def __init__(self, path: Path, run_id: str, *, readonly: bool = False):
        self.run_id = run_id
        self.path = path.resolve()
        if readonly:
            self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("CREATE TABLE IF NOT EXISTS events "
                            "(sequence INTEGER PRIMARY KEY, event TEXT NOT NULL)")
            self.db.commit()
        try:
            self.read()  # Reject corruption or a different run before permitting appends.
        except BaseException:
            self.db.close()
            raise

    def read(self) -> tuple[RunEvent, ...]:
        try:
            events = tuple(RunEvent.model_validate_json(row[0]) for row in
                           self.db.execute("SELECT event FROM events ORDER BY sequence"))
            previous = ""
            for sequence, event in enumerate(events, 1):
                if (event.schema_version != SCHEMA_VERSION or event.run_id != self.run_id
                        or event.sequence != sequence or event.previous_hash != previous):
                    raise StoreError("incompatible or discontinuous event journal")
                expected = digest(canonical(event.model_dump(exclude={"event_hash"}, mode="json")))
                if event.event_hash != expected:
                    raise StoreError(f"event checksum mismatch at {sequence}")
                previous = event.event_hash
            return events
        except StoreError:
            raise
        except Exception as error:
            raise StoreError(f"cannot read event journal: {error}") from error

    def append(self, kind: str, data: object) -> RunEvent:
        return self.append_many([(kind, data)])[0]

    def append_many(self, records: list[tuple[str, object]]) -> tuple[RunEvent, ...]:
        if not records:
            return ()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            # The transaction owns the next sequence; another writer cannot race it.
            events = self.read()
            sequence = len(events)
            previous = events[-1].event_hash if events else ""
            added: list[RunEvent] = []
            for kind, data in records:
                sequence += 1
                payload = data.model_dump(mode="json") if isinstance(data, BaseModel) else data
                fields = dict(schema_version=SCHEMA_VERSION, run_id=self.run_id,
                              sequence=sequence, kind=kind,
                              timestamp=datetime.now(timezone.utc).isoformat(),
                              data_json=canonical(payload), previous_hash=previous)
                event = RunEvent(**fields, event_hash=digest(canonical(fields)))
                self.db.execute("INSERT INTO events VALUES (?, ?)",
                                (sequence, event.model_dump_json()))
                added.append(event)
                previous = event.event_hash
            self.db.commit()
            return tuple(added)
        except BaseException:
            self.db.rollback()
            raise

    def close(self) -> None:
        self.db.close()


