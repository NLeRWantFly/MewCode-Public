"""Journaled, explicit workspace publication of reviewed immutable artifacts.

Only a trusted host supplies the workspace root. No model-supplied path can
select a different root. Multi-file visibility is not atomic for unmanaged
editors; conflicts freeze the journal instead of overwriting their changes.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath

from .runtime_protocol import MAX_ARTIFACT_BYTES, byte_hash
from .store_v2 import TeamStateError
from .runtime import _id
from mewcode.mcp.coord_protocol import canonical_json, now_iso


class ArtifactPublication:
    def __init__(self, runtime, *, workspace: Path):
        self.runtime = runtime
        self.root = Path(workspace).absolute()
        if not self.root.is_dir() or self.root.resolve() != self.root:
            raise TeamStateError("INVALID_WORKSPACE", "a canonical existing workspace is required")
        self._safe(self.root)
        self.fault_hook = lambda phase, index: None

    @staticmethod
    def _safe(path):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise TeamStateError("UNSAFE_PATH", str(path))
        if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
            raise TeamStateError("UNSAFE_PATH", "only ordinary files/directories are supported")
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise TeamStateError("UNSAFE_PATH", "hard-linked publication targets are unsupported")

    def _target(self, relative):
        if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
            raise TeamStateError("UNSAFE_PATH", "use a relative slash-separated path")
        parts = PurePosixPath(relative).parts
        if relative.startswith("/") or any(part in {".", "..", ""} for part in relative.split("/")):
            raise TeamStateError("UNSAFE_PATH", relative)
        reserved = {"con", "prn", "aux", "nul", *("com" + str(i) for i in range(1, 10)), *("lpt" + str(i) for i in range(1, 10))}
        current = self.root
        self._safe(current)
        for part in parts:
            if (part.rstrip(" .") != part or part.split(".")[0].casefold() in reserved
                    or any(character in part for character in '<>"|?*') or any(ord(c) < 32 for c in part)
                    or part.casefold() in {".git", ".mewcode", ".codex", ".agents"}):
                raise TeamStateError("UNSAFE_PATH", part)
            if current.exists():
                aliases = [entry.name for entry in current.iterdir() if entry.name.casefold() == part.casefold()]
                if aliases and aliases != [part]:
                    raise TeamStateError("PATH_ALIAS_CONFLICT", relative)
            current = current / part
            if current.exists() or current.is_symlink():
                self._safe(current)
        if not current.resolve().is_relative_to(self.root) or current == self.root:
            raise TeamStateError("UNSAFE_PATH", relative)
        if current.exists() and not current.is_file():
            raise TeamStateError("UNSAFE_PATH", "target is not a regular file")
        return current

    def _read(self, relative):
        target = self._target(relative)
        if not target.exists():
            return None
        if target.stat().st_size > MAX_ARTIFACT_BYTES:
            raise TeamStateError("ARTIFACT_TOO_LARGE", relative)
        value = target.read_bytes()
        self._target(relative)
        return value

    def _hash(self, relative):
        value = self._read(relative)
        return byte_hash(value) if value is not None else None

    def _generation(self, relative):
        row = self.runtime.db.execute('SELECT generation FROM team_v2_workspace_heads WHERE workspace=? AND relative_path=?',
                                       (str(self.root), relative.casefold())).fetchone()
        return row[0] if row else 0

    def _advance_heads(self, journal):
        for entry in journal['entries']:
            if self._generation(entry['relative_path']) != entry['before_generation']:
                raise TeamStateError('HEAD_CONFLICT', entry['relative_path'])
            self.runtime.db.execute('INSERT INTO team_v2_workspace_heads VALUES(?,?,?) ON CONFLICT(workspace,relative_path) DO UPDATE SET generation=excluded.generation',
                                    (str(self.root), entry['relative_path'].casefold(), entry['before_generation'] + 1))

    def _publishable(self, actor, run_id, epoch):
        team = self.runtime._owner(actor, run_id, epoch=epoch)
        if team['lifecycle'] != 'active' or self.runtime._runtime_run(run_id)['state'] in {'cancelled', 'failed'}:
            raise TeamStateError('RUN_NOT_ACTIVE', 'publication requires an active team and uncancelled run')
        return team

    def preview(self, actor, run_id, entries):
        self.runtime._owner(actor, run_id)
        result = []
        for entry in entries:
            artifact = self.runtime._artifact_for_run(entry["version_id"], run_id)
            result.append(dict(relative_path=entry["relative_path"], version_id=artifact["version_id"],
                               expected_hash=self._hash(entry["relative_path"]), expected_generation=self._generation(entry['relative_path']), content_hash=artifact["content_hash"]))
        return result

    def prepare(self, actor, run_id, entries, *, control_epoch, command_id):
        if not isinstance(entries, list) or not 1 <= len(entries) <= 64:
            raise ValueError("publication requires 1 to 64 explicit entries")
        def action():
            team = self._publishable(actor, run_id, control_epoch)
            targets, records = [], []
            for entry in entries:
                if set(entry) != {"relative_path", "version_id", "expected_hash", "expected_generation"}:
                    raise ValueError("each entry requires relative_path, version_id, expected_hash and expected_generation")
                relative = entry["relative_path"]
                target = self._target(relative)
                normalized = str(target).casefold()
                if any(normalized == other or normalized.startswith(other + os.sep) or other.startswith(normalized + os.sep) for other in targets):
                    raise TeamStateError("PATH_CONFLICT", relative)
                targets.append(normalized)
                before = self._read(relative)
                current_hash = byte_hash(before) if before is not None else None
                if current_hash != entry["expected_hash"]:
                    raise TeamStateError("VERSION_CONFLICT", relative)
                if type(entry['expected_generation']) is not int or self._generation(relative) != entry['expected_generation']:
                    raise TeamStateError('HEAD_CONFLICT', relative)
                artifact = self.runtime._artifact_for_run(entry["version_id"], run_id)
                if artifact["producer_task_id"] is None:
                    raise TeamStateError("REVIEW_REQUIRED", "workspace publication requires a reviewed task output")
                producer = self.runtime._task(artifact["producer_task_id"])
                packet = self.runtime.db.execute("SELECT packet_json FROM team_v2_handoffs WHERE task_id=? AND revision=? AND state='published'",
                                                 (producer["task_id"], producer["revision"])).fetchone()
                if producer["state"] != "completed" or not packet or entry["version_id"] not in {a["version_id"] for a in json.loads(packet[0])["artifacts"]}:
                    raise TeamStateError("REVIEW_REQUIRED", "candidate is not the current accepted task revision")
                records.append(dict(relative_path=relative, version_id=entry["version_id"], before_hash=current_hash,
                                    before_b64=__import__("base64").b64encode(before).decode() if before is not None else None,
                                    after_hash=artifact["content_hash"], producer_task_id=producer["task_id"],
                                    producer_revision=producer["revision"], before_generation=entry['expected_generation'], applied=False))
            for row in self.runtime.db.execute("SELECT journal_json FROM team_v2_publications WHERE state NOT IN ('committed','rolled_back')").fetchall():
                journal = json.loads(row[0])
                if journal["root"] != str(self.root):
                    continue
                for record in journal["entries"]:
                    other = str(self.root / record["relative_path"]).casefold()
                    if any(target == other or target.startswith(other + os.sep) or other.startswith(target + os.sep) for target in targets):
                        raise TeamStateError("PUBLICATION_BUSY", record["relative_path"])
            journal = dict(root=str(self.root), entries=records)
            publication_id = _id("publication")
            self.runtime.db.execute("INSERT INTO team_v2_publications VALUES(?,?,?,?,?,?,?,?)",
                                    (publication_id, team["team_id"], run_id, None, control_epoch, "prepared", canonical_json(journal), now_iso()))
            return {"team_id": team["team_id"], "publication_id": publication_id, "state": "prepared"}
        return self.runtime.repo._write(actor, "team_publication_prepared", command_id,
                                        dict(run_id=run_id, entries=entries, epoch=control_epoch, root=str(self.root)),
                                        lambda: self.runtime._owner(actor, run_id, epoch=control_epoch), action)

    def _journal(self, actor, publication_id):
        row = self.runtime.db.execute("SELECT * FROM team_v2_publications WHERE publication_id=?", (publication_id,)).fetchone()
        if row is None:
            raise TeamStateError("PUBLICATION_NOT_ACCESSIBLE", publication_id)
        self.runtime._owner(actor, row["team_run_id"])
        journal = json.loads(row["journal_json"])
        if journal["root"] != str(self.root):
            raise TeamStateError("INVALID_WORKSPACE", "journal belongs to another workspace")
        return row, journal

    def _record(self, publication_id, state, journal):
        self.runtime.db.execute("UPDATE team_v2_publications SET state=?,journal_json=? WHERE publication_id=?",
                                (state, canonical_json(journal), publication_id))

    def _replace(self, relative, body):
        target = self._target(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._target(relative)
        fd, temporary = tempfile.mkstemp(prefix=".team-publish-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            self._target(relative)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def commit(self, actor, publication_id, *, control_epoch):
        # One DB writer transaction spans each file step, not the whole copy.
        # The durable prepared/committing journal reserves overlapping targets.
        with self.runtime.lock:
            row, journal = self._journal(actor, publication_id)
            if row["state"] == "committed":
                return {"publication_id": publication_id, "state": "committed"}
            if row["state"] != "prepared":
                raise TeamStateError("NEEDS_RECONCILIATION", publication_id)
            self.runtime.db.execute("BEGIN IMMEDIATE")
            try:
                self._publishable(actor, row["team_run_id"], control_epoch)
                if control_epoch != row["control_epoch"]:
                    raise TeamStateError("STALE_CONTROL_EPOCH", publication_id)
                self._record(publication_id, "committing", journal)
                self.runtime.db.commit()
            except Exception:
                self.runtime.db.rollback()
                raise
            try:
                self.fault_hook("prepared", -1)
                for index, entry in enumerate(journal["entries"]):
                    self.runtime.db.execute("BEGIN IMMEDIATE")
                    try:
                        team = self._publishable(actor, row["team_run_id"], control_epoch)
                        producer = self.runtime._task(entry["producer_task_id"])
                        if (team["lifecycle"] != "active" or producer["revision"] != entry["producer_revision"]
                                or producer["state"] != "completed"):
                            raise TeamStateError("STALE_PUBLICATION", publication_id)
                        artifact = self.runtime._artifact(entry["version_id"], published=True)
                        if self._hash(entry["relative_path"]) != entry["before_hash"]:
                            raise TeamStateError("VERSION_CONFLICT", entry["relative_path"])
                        # Intent is durable before replacement; recovery compares
                        # bytes if the process dies between replace and commit.
                        entry["intent"] = True
                        self._record(publication_id, "committing", journal)
                        self.runtime.db.commit()
                        self.fault_hook("before_replace", index)
                        self.runtime.db.execute("BEGIN IMMEDIATE")
                        current_team = self._publishable(actor, row["team_run_id"], control_epoch)
                        current_producer = self.runtime._task(entry["producer_task_id"])
                        self.runtime._artifact(entry["version_id"], published=True)
                        if (current_team["lifecycle"] != "active" or current_producer["revision"] != entry["producer_revision"]
                                or current_producer["state"] != "completed" or self._hash(entry["relative_path"]) != entry["before_hash"]):
                            raise TeamStateError("VERSION_CONFLICT", entry["relative_path"])
                        self._replace(entry["relative_path"], bytes(artifact["content"]))
                        self.fault_hook("after_replace", index)
                        entry["applied"] = True
                        self._record(publication_id, "committing", journal)
                        self.runtime.db.commit()
                    except BaseException:
                        self.runtime.db.rollback()
                        raise
                self.runtime.db.execute("BEGIN IMMEDIATE")
                final_team = self._publishable(actor, row["team_run_id"], control_epoch)
                if final_team["lifecycle"] != "active":
                    raise TeamStateError("TEAM_NOT_ACTIVE", publication_id)
                for entry in journal["entries"]:
                    self.runtime._artifact(entry["version_id"], published=True)
                    producer = self.runtime._task(entry["producer_task_id"])
                    if producer["revision"] != entry["producer_revision"] or producer["state"] != "completed":
                        raise TeamStateError("STALE_PUBLICATION", publication_id)
                    if self._hash(entry["relative_path"]) != entry["after_hash"]:
                        raise TeamStateError("VERSION_CONFLICT", entry["relative_path"])
                self._advance_heads(journal)
                self._record(publication_id, "committed", journal)
                self.runtime.db.commit()
                return {"publication_id": publication_id, "state": "committed"}
            except Exception:
                self.runtime.db.rollback()
                with self.runtime.db:
                    self._record(publication_id, "needs_reconciliation", journal)
                raise

    def reconcile(self, actor, publication_id, *, control_epoch, action):
        if action not in {"finish", "rollback"}:
            raise ValueError("reconciliation action must be finish or rollback")
        with self.runtime.lock:
            self.runtime.db.execute("BEGIN IMMEDIATE")
            try:
                row, journal = self._journal(actor, publication_id)
                self.runtime._owner(actor, row["team_run_id"], epoch=control_epoch)
                if row["state"] in {"committed", "rolled_back"}:
                    self.runtime.db.commit()
                    return {"publication_id": publication_id, "state": row["state"]}
                if action == "finish":
                    self._publishable(actor, row['team_run_id'], control_epoch)
                    if row["control_epoch"] != control_epoch:
                        raise TeamStateError("STALE_CONTROL_EPOCH", "cannot finish under changed authority")
                    for entry in journal["entries"]:
                        self.runtime._artifact(entry["version_id"], published=True)
                        producer = self.runtime._task(entry["producer_task_id"])
                        if producer["revision"] != entry["producer_revision"] or producer["state"] != "completed":
                            raise TeamStateError("STALE_PUBLICATION", publication_id)
                        if self._hash(entry["relative_path"]) != entry["after_hash"]:
                            raise TeamStateError("NEEDS_RECONCILIATION", "not all files match the reviewed result")
                    state = "committed"
                else:
                    for entry in reversed(journal["entries"]):
                        actual = self._hash(entry["relative_path"])
                        if actual == entry["before_hash"]:
                            continue
                        if actual != entry["after_hash"] or not entry.get("intent"):
                            raise TeamStateError("VERSION_CONFLICT", "third-party edits are preserved")
                        if entry["before_b64"] is None:
                            # Retain newly created files instead of deleting.
                            target = self._target(entry["relative_path"])
                            retained = target.with_name(target.name + ".team-unmerged-" + publication_id)
                            if retained.exists():
                                raise TeamStateError("VERSION_CONFLICT", str(retained))
                            os.replace(target, retained)
                        else:
                            self._replace(entry["relative_path"], __import__("base64").b64decode(entry["before_b64"]))
                    state = "rolled_back"
                self._advance_heads(journal)
                self._record(publication_id, state, journal)
                self.runtime.db.commit()
                return {"publication_id": publication_id, "state": state}
            except Exception:
                self.runtime.db.rollback()
                raise
