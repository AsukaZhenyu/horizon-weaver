"""Opt-in, post-agent apt transport repair; native tests and scorer stay unchanged."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import tarfile
import time
from pathlib import Path, PurePosixPath

from terminal_bench.harness.harness import Harness

OUTPUT_ROOT = Path(__file__).resolve().parents[2] / "_work" / "tb8"
APT_HTTP = re.compile(rb"http://(?:(?:[a-zA-Z0-9-]+\.)*archive\.ubuntu\.com|security\.ubuntu\.com|ports\.ubuntu\.com|deb\.debian\.org|security\.debian\.org)(?=/)")


class GradingPreparationError(RuntimeError):
    """Infrastructure failure: no candidate correctness verdict was produced."""


def _json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _archive(container, path, output):
    chunks, stat = container.get_archive(path)
    digest = hashlib.sha256()
    size = 0
    partial = output.with_suffix(output.suffix + ".partial")
    with partial.open("wb") as target:
        for chunk in chunks:
            target.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        target.flush()
        os.fsync(target.fileno())
    members = []
    with tarfile.open(partial, "r:*") as archive:
        for item in archive:
            name = PurePosixPath(item.name)
            if name.is_absolute() or ".." in name.parts:
                raise GradingPreparationError("Unsafe archive member; archive retained without extraction")
            row = dict(name=item.name, type=item.type.decode("ascii", "replace"),
                       mode=item.mode, uid=item.uid, gid=item.gid, mtime=item.mtime,
                       size=item.size, linkname=item.linkname,
                       devmajor=item.devmajor, devminor=item.devminor)
            if item.isfile():
                stream = archive.extractfile(item)
                content_hash = hashlib.sha256()
                read_bytes = 0
                while chunk := stream.read(1024 * 1024):
                    content_hash.update(chunk)
                    read_bytes += len(chunk)
                if read_bytes != item.size:
                    raise GradingPreparationError("Incomplete archive file payload")
                row["sha256"] = content_hash.hexdigest()
            members.append(row)
    if not members:
        raise GradingPreparationError("Empty archive; full candidate not saved")
    os.replace(partial, output)
    members.sort(key=lambda row: row["name"])
    manifest_path = output.with_suffix(".manifest.json")
    _json(manifest_path, members)
    canonical = json.dumps(members, sort_keys=True, separators=(",", ":")).encode()
    return dict(path=str(output), sha256=digest.hexdigest(), bytes=size,
                members=len(members), manifest_path=str(manifest_path),
                manifest_sha256=_sha(canonical), docker_stat=stat, complete=True)


def _patch_sources(archive_path):
    """Return only same-host/path http->https source edits, preserving tar metadata."""
    changes = []
    data = io.BytesIO()
    with tarfile.open(archive_path, "r:*") as archive, tarfile.open(fileobj=data, mode="w") as patch:
        items = archive.getmembers()
        links = {item.name for item in items if item.issym() or item.islnk()}
        for item in items:
            parts = PurePosixPath(item.name).parts
            if not parts or parts[0] != "apt":
                raise GradingPreparationError("Unexpected apt archive root")
            rel = PurePosixPath(*parts[1:])
            selected = (str(rel) == "sources.list" or
                (len(rel.parts) == 2 and rel.parts[0] == "sources.list.d" and
                 rel.suffix in (".list", ".sources")))
            if not selected:
                continue
            if not item.isfile() or any(str(parent) in links for parent in PurePosixPath(item.name).parents):
                raise GradingPreparationError("Refusing linked/nonregular apt source")
            old = archive.extractfile(item).read()
            new = APT_HTTP.sub(lambda match: b"https://" + match.group()[7:], old)
            if new == old:
                continue
            updated = copy.copy(item)
            updated.name = str(rel)
            updated.size = len(new)
            patch.addfile(updated, io.BytesIO(new))
            changes.append(dict(path="/etc/apt/" + str(rel), before_sha256=_sha(old),
                                after_sha256=_sha(new), transport_only=True,
                                replacements=len(APT_HTTP.findall(old))))
    return data.getvalue(), changes


def prepare_grading_transport(container, artifact_dir):
    """Pause one task container, save full /app, edit apt URI schemes, verify /app."""
    artifact_dir = Path(artifact_dir).resolve()
    if not artifact_dir.is_relative_to(OUTPUT_ROOT.resolve()):
        raise ValueError("Grading artifacts must stay within this worktree's _work/tb8")
    artifact_dir.mkdir(parents=True, exist_ok=False)
    manifest = dict(status="preparing", phase="post-agent/pre-native-tests", model_calls=0,
                    app_snapshot_complete=False, original_grader_called=False,
                    candidate_or_tests_modified=False)
    started = time.monotonic()
    owned_pause = False
    error = None
    try:
        container.reload()
        attrs = container.attrs
        manifest["container_identity"] = {key: attrs.get(key) for key in
            ("Id", "Name", "Created", "Image", "Mounts")}
        config = attrs.get("Config", {})
        manifest["container_identity"]["config"] = {key: config.get(key) for key in
            ("Image", "WorkingDir", "User")}
        manifest["container_identity"]["environment_sha256"] = _sha(
            json.dumps(config.get("Env", []), sort_keys=True).encode())
        if attrs.get("State", {}).get("Paused"):
            raise GradingPreparationError("Task container was already paused")
        container.pause()
        owned_pause = True
        manifest["app_before"] = _archive(container, "/app", artifact_dir / "app-before.tar")
        manifest["app_snapshot_complete"] = True
        manifest["apt_before"] = _archive(container, "/etc/apt", artifact_dir / "apt-before.tar")
        patch, changes = _patch_sources(artifact_dir / "apt-before.tar")
        manifest["source_changes"] = changes
        if changes:
            (artifact_dir / "apt-transport-patch.tar").write_bytes(patch)
            if not container.put_archive("/etc/apt", patch):
                raise GradingPreparationError("Docker did not accept apt transport patch")
        manifest["app_after"] = _archive(container, "/app", artifact_dir / "app-after.tar")
        manifest["app_unchanged"] = (manifest["app_before"]["manifest_sha256"] ==
                                     manifest["app_after"]["manifest_sha256"])
        if not manifest["app_unchanged"]:
            raise GradingPreparationError("Candidate /app changed during grading preparation")
        manifest["status"] = "ready_for_original_tests"
    except Exception as exc:
        error = exc
        manifest.update(status="infrastructure_failure", error_type=type(exc).__name__, error=str(exc))
    finally:
        if owned_pause:
            try:
                container.unpause()
                manifest["container_unpaused"] = True
            except Exception as exc:
                error = exc
                manifest.update(status="infrastructure_failure", unpause_error=str(exc))
        manifest["preparation_seconds"] = time.monotonic() - started
        _json(artifact_dir / "manifest.json", manifest)
    if error is not None:
        raise GradingPreparationError(f"Grading preparation failed; candidate retained at {artifact_dir}") from error
    return manifest


class GradingTransportHarness(Harness):
    def _run_tests(self, terminal, session, trial_handler):
        artifact_dir = trial_handler.trial_paths.agent_logging_dir.parent / "grading-transport"
        try:
            manifest = prepare_grading_transport(session.container, artifact_dir)
        except Exception:
            # Native cleanup=False still does compose down. Preserve this failed
            # trial's container/project so an incomplete snapshot is recoverable.
            terminal._compose_manager.stop = lambda: self._logger.error(
                "Preserving task container after grading snapshot/transport failure")
            raise
        manifest["original_grader_called"] = True
        _json(artifact_dir / "manifest.json", manifest)
        return super()._run_tests(terminal, session, trial_handler)
