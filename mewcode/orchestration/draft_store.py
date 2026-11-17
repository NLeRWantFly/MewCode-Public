"""Conversation/draft-scoped, transactional versions and resumable review candidates."""
from __future__ import annotations

import copy
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from mewcode.orchestration.draft_sources import digest
from mewcode.orchestration.workflow_draft import (
    check_entry_refs, document_diff, empty_document, normalize_legacy_document,
    quote_matches, seed_document, validate_entry,
)

_KEEP = object()


def now():
    return datetime.now(timezone.utc).isoformat()


class DraftStore:
    @staticmethod
    def read_state(project, conversation_id, draft_id="main"):
        """Read an existing snapshot without schema creation or checkpoint writes."""
        if not isinstance(conversation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", conversation_id):
            raise ValueError("请选择明确的对话")
        if not isinstance(draft_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", draft_id):
            raise ValueError("底稿编号无效")
        root = Path(project).resolve()
        path = (root / ".mewcode" / "workflow-drafts.sqlite3").resolve()
        if not path.is_relative_to(root):
            raise ValueError("候选图存储不在当前项目内")
        if not path.is_file():
            return None
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            row = db.execute("SELECT data FROM draft WHERE conversation=? AND name=?",
                             (conversation_id, draft_id)).fetchone()
            return json.loads(row[0]) if row else None
        finally:
            db.close()

    @staticmethod
    def read_planning_sources(project, conversation_id, draft_id="main"):
        """Read durable discussion evidence without adding it to extraction history."""
        if not isinstance(conversation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", conversation_id):
            raise ValueError("请选择明确的对话")
        if not isinstance(draft_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", draft_id):
            raise ValueError("底稿编号无效")
        root = Path(project).resolve()
        path = (root / ".mewcode" / "workflow-drafts.sqlite3").resolve()
        if not path.is_relative_to(root):
            raise ValueError("候选图存储不在当前项目内")
        if not path.is_file():
            return []
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            rows = db.execute("SELECT data FROM source WHERE conversation=? AND name=? "
                              "AND json_extract(data, '$.purpose')='graph_discussion' ORDER BY rowid",
                              (conversation_id, draft_id)).fetchall()
        finally:
            db.close()
        return [{**json.loads(row[0]), "kind": "planning_discussion"} for row in rows]

    def __init__(self, project, conversation_id, draft_id="main"):
        if not isinstance(conversation_id, str) or not conversation_id or len(conversation_id) > 200:
            raise ValueError("请选择明确的对话")
        if not isinstance(draft_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", draft_id):
            raise ValueError("底稿编号无效")
        self.scope = (conversation_id, draft_id)
        self.path = Path(project).resolve() / ".mewcode" / "workflow-drafts.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS draft (conversation TEXT, name TEXT, data TEXT,
                PRIMARY KEY(conversation,name));
              CREATE TABLE IF NOT EXISTS version (conversation TEXT, name TEXT, revision INTEGER, data TEXT,
                PRIMARY KEY(conversation,name,revision));
              CREATE TABLE IF NOT EXISTS source (conversation TEXT, name TEXT, id TEXT, data TEXT,
                PRIMARY KEY(conversation,name,id));
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
            if db.total_changes:
                state = self._get(db)
                design = state.get('task_design') or {}
                from mewcode.orchestration.workbench_events import changed
                changed('draft', project=str(self.path.parent.parent), conversation_id=self.scope[0],
                        task_id=design.get('task_id', ''), run_id=design.get('run_id', ''))
        finally:
            db.close()

    def _get(self, db):
        row = db.execute("SELECT data FROM draft WHERE conversation=? AND name=?", self.scope).fetchone()
        return json.loads(row[0]) if row else {"revision": 0, "document": empty_document(), "processed_ids": [],
                                               "candidate": None, "source_ids": [], "warnings": [], "snapshot": {}}

    def _put(self, db, state):
        db.execute("INSERT OR REPLACE INTO draft VALUES (?,?,?)", (*self.scope, json.dumps(state, ensure_ascii=False)))

    def get(self, *, include_changes=True):
        with self.db() as db:
            state = self._get(db)
        candidate = state.get("candidate")
        processed = candidate["processed_ids"] if candidate else state["processed_ids"]
        state["coverage"] = {"total": len(state["source_ids"]), "processed": len(processed),
                             "pending": max(0, len(state["source_ids"]) - len(processed)),
                             "history_changed": state["source_ids"][:len(processed)] != processed}
        if candidate and candidate.get("mode") == "selected":
            ids = candidate["source_ids"]
            state["coverage"] = {"mode": "selected", "total": len(ids), "processed": len(processed),
                                 "pending": len(ids) - len(processed), "history_total": len(state["source_ids"]),
                                 "history_changed": not set(ids).issubset(state["source_ids"])}
        elif not candidate:
            state["coverage"]["selected_processed"] = len(state.get("selected_ids", []))
        if include_changes:
            state["changes"] = document_diff(state["document"], candidate["document"]) if candidate else []
        return state

    def prepare_local_repair(self, base, *, candidate_id=None, sequence=None,
                             expected_revision=None, expected_base_graph_hash=None):
        """Bind an explicit local repair to the canvas visible to the caller.

        A saved canvas has no working candidate after it is applied.  Local
        repair still needs the normal candidate CAS and provenance checks, so
        create a private, ready, selected-scope snapshot of that canvas.  Its
        source scope is deliberately empty: the repair checks the current
        graph and does not claim unread conversation history was processed.
        """
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            current_hash = digest(base)
            if type(expected_revision) is not int or state.get("revision") != expected_revision:
                raise ValueError("画布版本已变化，请刷新当前图后再修复")
            if not isinstance(expected_base_graph_hash, str) or expected_base_graph_hash != current_hash:
                raise ValueError("画布已被并发修改，请刷新当前图后再修复")
            current = state.get("candidate")
            if current:
                if (current.get("id"), current.get("sequence")) != (candidate_id, sequence):
                    raise ValueError("候选图已变化，请刷新后再修复")
                if current.get("status") != "ready":
                    raise ValueError("请先完成当前构图，再修复当前图")
                if current.get("base_graph_hash") != current_hash:
                    raise ValueError("候选基线落后于当前画布，请先按当前画布重新整理")
                return copy.deepcopy(current)
            if not isinstance(base, dict) or not base.get("nodes"):
                raise ValueError("当前对话没有可修复的已保存画布")
            if candidate_id is not None or sequence is not None:
                raise ValueError("当前候选图已保存或被替换，请刷新后再修复")
            if state.get("warnings"):
                raise ValueError("原始来源存在读取警告，请先刷新并核对来源后再修复")
            basis = state.get("history_basis") or {}
            if basis.get("dirty") or basis.get("pending"):
                raise ValueError("构图依据仍有待处理取舍，请先完成后再修复当前图")
            history_ids = list(state.get("source_ids", []))
            document, _ = normalize_legacy_document(
                seed_document(base, previous=state.get("document")))
            current = {
                "id": uuid.uuid4().hex,
                "sequence": 0,
                "base_revision": state["revision"],
                "base_graph_hash": current_hash,
                "base_snapshot": copy.deepcopy(base),
                "document": document,
                "log": [],
                "processed_ids": [],
                "status": "ready",
                "error": "",
                "at": now(),
                "mode": "selected",
                "repair_scope": True,
                "source_ids": [],
                # Keep the saved canvas' processed prefix as provenance for the
                # private repair candidate.  The candidate itself deliberately
                # starts with no source claims, but adopting it must restore
                # this prefix instead of dropping previously processed history.
                "base_processed_ids": list(state.get("processed_ids", [])),
                "selection_context": {},
                "history_source_ids": history_ids,
                "source_override": {"kind": "saved_canvas", "source_ids": [],
                                    "history_source_ids": history_ids},
            }
            if basis:
                current["history_basis"] = copy.deepcopy(basis)
            state["candidate"] = current
            state.pop("graph_candidate", None)
            state.pop("repair_request", None)
            state.pop("repair_preview", None)
            self._put(db, state)
        return copy.deepcopy(current)

    def start_local_repair(self, candidate_id=None, sequence=None, *,
                           expected_revision=None, expected_base_graph_hash=None):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            candidate = state.get("candidate") or {}
            if expected_revision is not None and state.get("revision") != expected_revision:
                raise ValueError("画布版本已变化，请刷新当前图后再修复")
            if candidate_id is not None or sequence is not None:
                if (candidate.get("id"), candidate.get("sequence")) != (candidate_id, sequence):
                    raise ValueError("候选图已变化，请刷新后再修复")
            if candidate.get("status") != "ready":
                raise ValueError("请先完成当前构图，再修复当前图")
            if expected_base_graph_hash is not None and candidate.get("base_graph_hash") != expected_base_graph_hash:
                raise ValueError("候选基线落后于当前画布，请刷新后再修复")
            previous = state.get("local_repair_run") or {}
            run = {"id": uuid.uuid4().hex, "candidate_id": candidate.get("id"),
                   "sequence": candidate.get("sequence"), "status": "running", "started_at": now(), "error": ""}
            if ((previous.get("candidate_id"), previous.get("sequence")) ==
                    (candidate.get("id"), candidate.get("sequence")) and previous.get("work")):
                run["work"] = copy.deepcopy(previous["work"])
            state["local_repair_run"] = run
            self._put(db, state)
        return run["id"]

    def checkpoint_local_repair(self, run_id, work):
        """Keep intermediate repair work private; never modify the candidate."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            run, candidate = state.get("local_repair_run") or {}, state.get("candidate") or {}
            if (run.get("id") != run_id or (run.get("candidate_id"), run.get("sequence")) !=
                    (candidate.get("id"), candidate.get("sequence"))):
                raise ValueError("候选图已变化，本次修复进度没有覆盖新修改。")
            run["work"] = copy.deepcopy(work)
            self._put(db, state)

    def save_local_repair_preview(self, base, preview):
        from .graph_repair import require_current
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            require_current(state, base, preview["ticket"])
            if not preview.get("local") or not preview.get("validation", {}).get("valid"):
                raise ValueError("修订尚未通过校验，不能作为最终修订展示。")
            state["repair_preview"] = copy.deepcopy(preview)
            self._put(db, state)
        return {k: v for k, v in preview.items() if k != "document"}

    def finish_local_repair(self, run_id, status, error=""):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            run = state.get("local_repair_run") or {}
            if run.get("id") != run_id:
                return
            run.update(status=status, error=error, finished_at=now())
            self._put(db, state)

    def refresh(self, sources, warnings, snapshot):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            db.executemany("INSERT OR IGNORE INTO source VALUES (?,?,?,?)",
                           [(*self.scope, s["id"], json.dumps(s, ensure_ascii=False)) for s in sources])
            # Retrofit host-only classification on already-indexed fragments;
            # source text, IDs, and the immutable evidence fields do not change.
            from .repair_protocol import CONTROL_PURPOSE
            db.executemany("UPDATE source SET data=json_set(data,'$.purpose',?) WHERE conversation=? AND name=? AND id=?",
                           [(CONTROL_PURPOSE, *self.scope, s["id"]) for s in sources if s.get("purpose") == CONTROL_PURPOSE])
            state.update(source_ids=[s["id"] for s in sources], warnings=warnings, snapshot=snapshot)
            self._put(db, state)
        return self.get()

    def sources(self):
        with self.db() as db:
            state = self._get(db)
            rows = db.execute("SELECT id,data FROM source WHERE conversation=? AND name=?", self.scope).fetchall()
        values = {row[0]: json.loads(row[1]) for row in rows}
        return [values[id] for id in state["source_ids"]]

    def source(self, id):
        with self.db() as db:
            row = db.execute("SELECT data FROM source WHERE conversation=? AND name=? AND id=?", (*self.scope, id)).fetchone()
        if not row:
            raise ValueError("此来源不属于当前底稿")
        return json.loads(row[0])

    def begin(self, base_spec, *, rebuild=False, selection=None, context=(), graph_first=False, include_new=False):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            candidate = state.get("candidate")
            if graph_first and not rebuild:
                prefix = (candidate.get("base_processed_ids", state["processed_ids"])
                          if candidate and candidate.get("mode") == "selected"
                          else candidate["processed_ids"] if candidate else state["processed_ids"])
                reused = set(candidate.get("reused_ids", state.get("selected_ids", []))
                             if candidate and candidate.get("mode") != "selected" else state.get("selected_ids", []))
                if state["source_ids"][:len(prefix)] != prefix or not reused.issubset(state["source_ids"]):
                    raise ValueError("已生成图的来源发生变化或缺失，不能复用旧进度；请核对来源后重新扫描。")
            if graph_first and candidate and not rebuild and candidate["base_graph_hash"] != digest(base_spec):
                raise ValueError("画布已被修改，未覆盖；请按新图重新生成")
            if selection is not None:
                if rebuild:
                    raise ValueError("选择性纳入不能同时重扫全部历史")
                if not selection or not set(selection).issubset(state["source_ids"]):
                    raise ValueError("选定来源已变化，请刷新对话后重新选择")
                if not set(context).issubset(state["source_ids"]):
                    raise ValueError("参考上下文已变化，请重新选择")
                if candidate and candidate.get("mode") != "selected":
                    prefix = candidate["processed_ids"]
                    if state["source_ids"][:len(prefix)] != prefix:
                        raise ValueError("已整理的历史发生变化，请先重新扫描")
                    # Preserve prior edits/results, but do not silently resume
                    # the unselected remainder of an old whole-history scan.
                    candidate.update(mode="selected", base_processed_ids=list(prefix), source_ids=[], processed_ids=[],
                                     selection_context={}, status="ready", error="", sequence=candidate["sequence"] + 1,
                                     selection_note="已有待确认条目及人工编辑已保留；仅追加本次选择，其余未扫描历史不自动纳入。")
                    candidate.pop("reused_ids", None)
                    self._put(db, state)
                covered = set(state["processed_ids"]) | set(state.get("selected_ids", []))
                if candidate:
                    covered.update(candidate.get("base_processed_ids", []))
                additions = [id for id in dict.fromkeys(selection) if id not in covered
                             and (not candidate or id not in candidate["source_ids"])]
                if not additions and not candidate:
                    return None  # Already confirmed: do not spend another model call.
                if candidate and additions:
                    if graph_first and "history_source_ids" in candidate:
                        candidate["history_source_ids"] = list(state["source_ids"])
                        if candidate.get("history_basis"):
                            candidate["history_basis"]["source_ids"] = list(state["source_ids"])
                    candidate["source_ids"] += additions
                    candidate.setdefault("selection_context", {}).update({id: list(context) for id in additions})
                    candidate.update(status="working", error="", sequence=candidate["sequence"] + 1, at=now())
                    self._put(db, state)
            if candidate and not rebuild:
                history_updated = False
                if graph_first:
                    frozen = candidate.get("history_source_ids")
                    if frozen is not None and state["source_ids"][:len(frozen)] != frozen:
                        raise ValueError("本轮构图的原始来源发生变化，请核对来源后重新扫描")
                    finished = (candidate.get("status") == "ready" and
                                (candidate.get("autobuild") or {}).get("phase", "done") == "done")
                    if frozen is None or include_new or finished:
                        if include_new and frozen is not None and candidate.get("mode") == "selected":
                            candidate["source_ids"] += [id for id in state["source_ids"][len(frozen):]
                                                        if id not in candidate["source_ids"]]
                        history_updated = frozen != state["source_ids"]
                        candidate["history_source_ids"] = list(state["source_ids"])
                        if candidate.get("history_basis"):
                            candidate["history_basis"]["source_ids"] = list(state["source_ids"])
                normalized, repaired = normalize_legacy_document(candidate["document"])
                if history_updated:
                    repaired.append("history_snapshot")
                # Freeze the reuse decision for this generation. In particular,
                # resuming an explicit full replay must not silently re-import
                # receipts that the user asked us to reprocess (or that are stale).
                prior_reused = candidate.get("reused_ids", state.get("selected_ids", []))
                if (graph_first and candidate.get("mode") != "selected"
                        and set(state["source_ids"]) - set(candidate["processed_ids"])):
                    reused = list(dict.fromkeys(candidate["processed_ids"] + prior_reused))
                    if candidate.get("reused_ids") != reused:
                        candidate["reused_ids"] = reused
                        repaired.append("reuse")
                if candidate.get("status") in {"failed", "paused"}:
                    # A new explicit resume is running, not the previous failure.
                    # Retain the previous attempt in log, not in the live banner.
                    candidate.update(status="working", error="")
                    repaired.append("resumed")
                if repaired:
                    candidate.update(document=normalized, sequence=candidate["sequence"] + 1, at=now())
                if candidate["base_graph_hash"] != digest(base_spec):
                    self._rebase(candidate, base_spec, state)
                    self._put(db, state)
                    return copy.deepcopy(candidate)
                if "log" not in candidate:
                    candidate["log"] = []
                    repaired.append("log")
                if repaired:
                    self._put(db, state)
                return copy.deepcopy(candidate)
            if (graph_first and not rebuild and selection is None and base_spec and state["revision"]
                    and set(state["source_ids"]).issubset(set(state["processed_ids"]) | set(state.get("selected_ids", [])))):
                return None  # No new input: keep the saved graph, without even resolving a model.
            # Once a graph is saved, its user-edited node content is canonical.
            # Never resurrect removed nodes or old requirements from the index.
            document = seed_document(base_spec, previous=state["document"]) if graph_first and base_spec else copy.deepcopy(state["document"])
            if not document["entries"]:
                document = seed_document(base_spec)
            else:
                from .draft_keys import append_seed_entries
                document["entries"] = append_seed_entries(document["entries"], seed_document(base_spec)["entries"])
            document, _ = normalize_legacy_document(document)
            candidate = {"id": uuid.uuid4().hex, "sequence": 0, "base_revision": state["revision"],
                         "base_graph_hash": digest(base_spec), "base_snapshot": copy.deepcopy(base_spec),
                         "document": document, "log": [],
                         "processed_ids": [] if rebuild else state["processed_ids"], "status": "working", "at": now()}
            if graph_first:
                from .effective_history import new_basis
                candidate["history_source_ids"] = list(state["source_ids"])
                candidate["history_basis"] = new_basis(document, state["source_ids"], state.get("history_basis"))
            if selection is not None:
                candidate.update(mode="selected", source_ids=additions, processed_ids=[],
                                 selection_context={id: list(context) for id in additions})
            elif rebuild:
                candidate["reused_ids"] = []
            elif graph_first and not rebuild:
                candidate["reused_ids"] = list(dict.fromkeys(state["processed_ids"] + state.get("selected_ids", [])))
            state["candidate"] = candidate
            state.pop("graph_candidate", None)
            self._put(db, state)
        return candidate

    def freeze_materials(self, candidate, frozen):
        """Attach a one-time planning snapshot without changing the live material ledger."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            current = state.get("candidate")
            if not current or (current["id"], current["sequence"]) != (candidate["id"], candidate["sequence"]):
                raise ValueError("候选已变化，素材快照未写入。")
            if ("materials_snapshot" not in current or
                    current["materials_snapshot"].get("sequence") != frozen["materials_snapshot"].get("sequence")):
                current.update(document=copy.deepcopy(frozen["document"]),
                               materials_snapshot=copy.deepcopy(frozen["materials_snapshot"]))
                if frozen.get("history_basis"):
                    current["history_basis"] = copy.deepcopy(frozen["history_basis"])
                current["sequence"] += 1
                self._put(db, state)
        return copy.deepcopy(current)

    @staticmethod
    def _rebase_documents(existing, canvas, old_base):
        """Merge a saved canvas into a candidate without resurrecting deletes.

        ``canvas`` owns entries that were part of the old baseline.  Entries
        introduced by extraction/materials remain in the candidate.  Baseline
        entries missing from the canvas are retained as retired audit rows,
        which keeps old receipts visible while preventing the next chunk from
        treating them as active requirements again.
        """
        existing = copy.deepcopy(existing or empty_document())
        canvas = copy.deepcopy(canvas or empty_document())
        old_base = copy.deepcopy(old_base or empty_document())
        old_ids = {entry.get("id") for entry in old_base.get("entries", [])
                   if isinstance(entry, dict) and isinstance(entry.get("id"), str)}
        canvas_ids = {entry.get("id") for entry in canvas.get("entries", [])
                      if isinstance(entry, dict) and isinstance(entry.get("id"), str)}
        merged, seen = [], set()

        def append(entry):
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                return
            if entry["id"] in seen:
                return
            seen.add(entry["id"])
            merged.append(copy.deepcopy(entry))

        # New canvas fields are authoritative for all baseline IDs.
        for entry in canvas.get("entries", []):
            append(entry)
        for entry in existing.get("entries", []):
            entry_id = entry.get("id")
            if entry_id in old_ids and entry_id not in canvas_ids:
                retired = copy.deepcopy(entry)
                retired["state"] = "retired"
                append(retired)
            elif entry_id not in old_ids:
                # Candidate-only material, user additions, and their source
                # receipts survive the rebase unchanged.
                append(entry)
        ignored = []
        seen_ignored = set()
        for row in list(existing.get("ignored", [])) + list(canvas.get("ignored", [])):
            marker = digest(row)
            if marker not in seen_ignored:
                seen_ignored.add(marker)
                ignored.append(copy.deepcopy(row))
        result = {"schema_version": max(existing.get("schema_version", 1), canvas.get("schema_version", 1)),
                  "entries": merged, "ignored": ignored}
        for key in ("question_metadata_diagnostics",):
            rows = []
            seen_rows = set()
            for row in list(existing.get(key, [])) + list(canvas.get(key, [])):
                marker = digest(row)
                if marker not in seen_rows:
                    seen_rows.add(marker)
                    rows.append(copy.deepcopy(row))
            if rows:
                result[key] = rows
        return result

    def _rebase(self, current, base_spec, state=None):
        new_hash = digest(base_spec)
        old_base = current.get("base_snapshot")
        if not isinstance(old_base, dict):
            old_base = ((current.get("materials_snapshot") or {}).get("base_spec")
                        if isinstance(current.get("materials_snapshot"), dict) else None)
        if not isinstance(old_base, dict) and isinstance(state, dict):
            old_base = ((state.get("materials") or {}).get("base_spec")
                        if isinstance(state.get("materials"), dict) else None)
        if isinstance(old_base, dict):
            old_canvas = seed_document(old_base)
        else:
            # Legacy candidates may lack base_snapshot/materials metadata.  A
            # saved workflow document is the only trustworthy ownership
            # signal in that case; using an empty baseline would revive
            # deleted saved nodes on the next generation.
            old_canvas = copy.deepcopy((state or {}).get("document") or empty_document())
        # Previous candidate metadata/provenance is supplied only to the
        # current canvas seed.  The canvas fields themselves remain primary.
        current_canvas = seed_document(base_spec, previous=current.get("document"))
        existing_document = copy.deepcopy(current.get("document") or empty_document())
        # A planning snapshot may contain valid proposal rows that have not
        # yet been folded into the candidate document.  Include those rows in
        # the ownership merge so an explicit rebase cannot discard them.
        snapshot_document = (current.get("materials_snapshot") or {}).get("document")
        if isinstance(snapshot_document, dict):
            known = {entry.get("id") for entry in existing_document.get("entries", [])}
            for entry in snapshot_document.get("entries", []):
                if isinstance(entry, dict) and entry.get("id") not in known:
                    existing_document.setdefault("entries", []).append(copy.deepcopy(entry))
                    known.add(entry["id"])
            existing_document["ignored"] = list(existing_document.get("ignored", [])) + list(
                snapshot_document.get("ignored", []))
        merged = self._rebase_documents(existing_document, current_canvas, old_canvas)
        current["document"] = merged

        basis = current.get("history_basis")
        if isinstance(basis, dict):
            # Preserve source freeze, processed/reused IDs, logs, manual
            # exclusions, pending repairs and plan history while moving the
            # effective document to the current canvas baseline.
            basis["document"] = copy.deepcopy(merged)
            entry_sources = basis.setdefault("entry_sources", {})
            for entry in merged.get("entries", []):
                if entry.get("sources") and entry.get("id") not in entry_sources:
                    entry_sources[entry["id"]] = copy.deepcopy(entry["sources"])

        materials = state.get("materials") if isinstance(state, dict) else None
        if isinstance(materials, dict):
            # WorkflowBlocks uses this live ledger as the next generation's
            # baseline.  Move its base snapshot together with the candidate;
            # retain all candidate-only material rows in the ledger.
            materials["base_spec"] = copy.deepcopy(base_spec)
            materials["base_graph_hash"] = new_hash
            materials["document"] = copy.deepcopy(merged)
            current["materials_snapshot"] = copy.deepcopy(materials)
        else:
            snapshot = copy.deepcopy(current.get("materials_snapshot") or {})
            snapshot["base_spec"] = copy.deepcopy(base_spec)
            snapshot["base_graph_hash"] = new_hash
            snapshot["document"] = copy.deepcopy(merged)
            current["materials_snapshot"] = snapshot
        current.update(base_graph_hash=new_hash, base_snapshot=copy.deepcopy(base_spec),
                       sequence=current["sequence"] + 1, at=now())
        current.setdefault("log", [])

    def rebase(self, candidate, base_spec, *, expected_revision=None,
               expected_base_graph_hash=None):
        """Fold a changed canvas baseline into the working candidate.

        Rebase is an explicit, version-checked operation.  The candidate
        identity/sequence protects the draft writer while the global revision
        and the hash supplied by the caller protect the canvas the user saw.
        Keeping these checks in the same transaction prevents a stale editor
        from silently rebasing over a newer saved graph.
        """
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            current = state.get("candidate")
            current_base_hash = digest(base_spec)
            if expected_revision is not None and state.get("revision") != expected_revision:
                raise ValueError("画布版本已变化，本次 rebase 没有写入")
            if expected_base_graph_hash is not None and expected_base_graph_hash != current_base_hash:
                raise ValueError("画布已被并发修改，本次 rebase 没有写入")
            if not current or (current["id"], current["sequence"]) != (candidate["id"], candidate["sequence"]):
                raise ValueError("底稿已被编辑或重新扫描，本次迟到结果没有写入")
            if current["base_graph_hash"] == current_base_hash:
                raise ValueError("候选基线已经是当前画布，无需 rebase")
            self._rebase(current, base_spec, state)
            self._put(db, state)
        return copy.deepcopy(current)

    def checkpoint(self, candidate, document, processed_ids, status="working", error="", log=None,
                   source_limit=None, autobuild=_KEEP, readiness_result=None, history_basis=_KEEP, reused_ids=_KEEP):
        if source_limit is not None and (type(source_limit) is not int or source_limit < 1):
            raise ValueError("底稿分块数量上限必须为正整数")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            current = state.get("candidate")
            if not current or (current["id"], current["sequence"]) != (candidate["id"], candidate["sequence"]):
                raise ValueError("底稿已被编辑或重新扫描，本次迟到结果没有写入")
            selected = current.get("mode") == "selected"
            reused = current.get("reused_ids", []) if reused_ids is _KEEP else reused_ids
            frozen = current.get("history_source_ids", state["source_ids"])
            target = current["source_ids"] if selected else frozen
            prefix = current.get("base_processed_ids", state["processed_ids"]) if selected else []
            if (target[:len(processed_ids)] != processed_ids or not set(target).issubset(state["source_ids"])
                    or not set(reused).issubset(state["source_ids"])
                    or selected and not set(state.get("selected_ids", [])).issubset(state["source_ids"])
                    or state["source_ids"][:len(prefix)] != prefix
                    or state["source_ids"][:len(frozen)] != frozen):
                raise ValueError("来源发生变化，本批次未保存")
            current.update(document=document, processed_ids=processed_ids, status=status, error=error,
                           sequence=current["sequence"] + 1, at=now())
            current.pop("readiness", None)
            if readiness_result is not None:
                current["readiness"] = {**copy.deepcopy(readiness_result), "at": now()}
            if log is not None:
                current["log"] = log
            if source_limit is not None:
                current["chunk_source_limit"] = source_limit
            if autobuild is not _KEEP:
                current["autobuild"] = copy.deepcopy(autobuild)
            if history_basis is not _KEEP:
                current["history_basis"] = copy.deepcopy(history_basis)
            if reused_ids is not _KEEP:
                current["reused_ids"] = list(reused_ids)
            self._put(db, state)
        return copy.deepcopy(current)

    @staticmethod
    def _after_manual_edit(candidate, document):
        build = copy.deepcopy(candidate.get("autobuild"))
        if not build:
            return _KEEP
        from .graph_discussion import pause_for_user, waiting_questions
        if build.get("phase") == "done" and waiting_questions(document):
            pause_for_user({**candidate, "autobuild":build})
        from .graph_autofix import locked_snapshot
        build["locked_baseline"] = locked_snapshot(document)
        build["review_stale"] = bool(build.get("review") or build.get("final_review"))
        build["manual_edit_pending_review"] = build["review_stale"]
        if build.get("discussion"):
            build["discussion"].update(proposal=None, pending=None, error="")
        if build.get("phase") == "done":
            build.update(status="needs_review", reason="manual_edits_after_review")
        return build

    def edit(self, candidate_id, sequence, entry_id, changes=None, keep_previous=False):
        state = self.get()
        c = state.get("candidate")
        if not c or c["id"] != candidate_id or c["sequence"] != sequence:
            raise ValueError("底稿版本已变化，请刷新后再编辑")
        document = copy.deepcopy(c["document"])
        entries = {e["id"]: e for e in document["entries"]}
        if keep_previous:
            old = next((e for e in state["document"]["entries"] if e["id"] == entry_id), None)
            if not old:
                raise ValueError("新增条目没有旧版本；请显式撤回或编辑")
            entries[entry_id] = copy.deepcopy(old)
        else:
            old = entries.get(entry_id)
            if not old:
                raise ValueError("条目不存在")
            if not isinstance(changes, dict) or set(changes) - {"title", "body", "data", "state"}:
                raise ValueError("只允许编辑条目名称、正文、结构与状态")
            mode = (state.get("task_design") or {}).get("orchestration_mode", "classic")
            entry = validate_entry({**old, **changes}, orchestration_mode=mode)
            entry.update(evidence=old.get("evidence", "manual"), reviewed=True)
            if "reclassified_from" in old:
                entry["reclassified_from"] = copy.deepcopy(old["reclassified_from"])
            if entry["state"] == "retired" and old.get("superseded_by"):
                entry["superseded_by"] = old["superseded_by"]
            entries[entry_id] = entry
        check_entry_refs(entries.values(), focus={entry_id})
        document["entries"] = list(entries.values())
        from .draft_keys import reject_new_duplicates
        reject_new_duplicates(c["document"], document, {entry_id})
        basis = copy.deepcopy(c.get("history_basis"))
        if basis:
            basis_entries = {e["id"]: e for e in basis["document"]["entries"]}
            basis_entries[entry_id] = copy.deepcopy(entries[entry_id])
            basis["document"]["entries"] = list(basis_entries.values())
            basis.setdefault("manual_boundaries", {})[entry_id] = len(state["source_ids"]) - 1
        return self.checkpoint(c, document, c["processed_ids"], c["status"],
                               autobuild=self._after_manual_edit(c, document), history_basis=basis if basis else _KEEP)

    def change_history(self, base, action, *, candidate_id, sequence, expected_revision,
                       source_hash, entry_id=None, source_ids=None, quote=None):
        """Content choices are local planning edits, never execution grants."""
        from .effective_history import effective_document, new_basis
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            c = state.get("candidate")
            if state["revision"] != expected_revision:
                raise ValueError("构图依据版本已变化，请刷新后重新选择")
            if c and (c["id"], c["sequence"]) != (candidate_id, sequence):
                raise ValueError("候选版本已变化，本次取舍没有写入")
            if not c and candidate_id is not None:
                raise ValueError("候选已保存或已替换，请刷新构图依据")
            frozen = (c or {}).get("history_source_ids") or (state.get("history_basis") or {}).get("source_ids", state["source_ids"])
            if source_hash != digest(frozen) or state["source_ids"][:len(frozen)] != frozen:
                raise ValueError("原始来源已变化，本次取舍没有写入")
            if c and c["base_graph_hash"] != digest(base):
                raise ValueError("画布已修改，请按新画布更新后再选择构图依据")
            if not c:
                document = seed_document(base, previous=state["document"]) if base else copy.deepcopy(state["document"])
                c = {"id": uuid.uuid4().hex, "sequence": 0, "base_revision": state["revision"],
                     "base_graph_hash": digest(base), "base_snapshot": copy.deepcopy(base),
                     "document": document, "log": [],
                     "processed_ids": list(state["processed_ids"]), "status": "ready", "error": "", "at": now(),
                     "history_source_ids": list(frozen),
                     "history_basis": new_basis(document, frozen, state.get("history_basis"))}
                if state.get("autobuild_result"):
                    c["autobuild"] = copy.deepcopy(state["autobuild_result"])
            basis = copy.deepcopy(c.get("history_basis") or new_basis(c["document"], frozen))
            entries = {e["id"]: e for e in basis["document"]["entries"]}
            _, omitted = effective_document(basis)
            for e in c["document"]["entries"]:
                if e["id"] not in omitted and e.get("state") != "retired":
                    entries[e["id"]] = copy.deepcopy(e)
            rows = db.execute("SELECT id,data FROM source WHERE conversation=? AND name=?", self.scope).fetchall()
            sources = {id: json.loads(data) for id, data in rows if id in frozen}
            if action == "exclude_content":
                if entry_id not in entries:
                    raise ValueError("待排除的具体内容不存在")
                basis["excluded"][entry_id] = True
            elif action == "restore_content":
                old = next((item for item in basis.get("superseded", []) if item["id"] == entry_id), None)
                ignored = next((item for item in basis["document"].get("ignored", [])
                                if "ignored_" + digest(item)[:20] == entry_id), None)
                if old:
                    restored = copy.deepcopy(old["entry"])
                    current = entries.get(restored["id"])
                    if current and current != restored:
                        basis["superseded"].append({"id": "sup_" + digest(current)[:20], "entry": copy.deepcopy(current),
                                                     "source_refs": copy.deepcopy(basis["entry_sources"].get(current["id"], current["sources"])),
                                                     "reason": "你恢复旧内容时替代了此项"})
                    restored.update(state="active", reviewed=True, evidence="manual")
                    entries[restored["id"]] = restored
                    basis["entry_sources"][restored["id"]] = copy.deepcopy(old.get("source_refs", restored["sources"]))
                    basis.setdefault("manual_boundaries", {})[restored["id"]] = len(state["source_ids"]) - 1
                    basis["excluded"].pop(restored["id"], None)
                elif ignored:
                    source = sources.get(ignored["source_id"])
                    from .repair_protocol import is_control_source
                    if source and is_control_source(source):
                        raise ValueError("修图协议只能通过专用修订入口处理，不能恢复为业务要求")
                    text = quote if quote is not None else ignored.get("quote", "")
                    if source is None or not isinstance(text, str) or text and not quote_matches(text, source["text"]):
                        raise ValueError("恢复引文不是本轮真实来源的内容")
                    basis["pending"].append({"source_id": source["id"], "quote": text, "kind": "content"})
                elif entry_id in entries:
                    was_excluded = entry_id in basis["excluded"]
                    basis["excluded"].pop(entry_id, None)
                    # Removing a manual exclusion must also restore its
                    # dependents from the original ledger, not from retired copies.
                    if entries[entry_id]["state"] == "retired" or not was_excluded:
                        entries[entry_id].update(state="active", reviewed=True, evidence="manual")
                else:
                    raise ValueError("待恢复的内容不存在")
            elif action == "select_plan":
                if (not isinstance(source_ids, list) or not source_ids or len(source_ids) != len(set(source_ids))
                        or any(id not in sources or sources[id]["role"] != "assistant" for id in source_ids)):
                    raise ValueError("当前方案必须是本轮真实的助手回复")
                from .repair_protocol import is_control_source
                if any(is_control_source(sources[id]) for id in source_ids):
                    raise ValueError("修图协议不能选为业务方案")
                lines = {sources[id].get("line") for id in source_ids}
                source_ids = [id for id in frozen if id in sources and sources[id].get("line") in lines
                              and sources[id]["role"] == "assistant"]
                basis["manual_plan_source_ids"] = source_ids
                basis["current_plan"] = {"source_ids": source_ids, "mode": "selected", "adopted_by": [],
                                         "reason": "采用选定回复的步骤，保留有效用户要求"}
                if source_ids not in [p["source_ids"] for p in basis["plans"]]:
                    basis["plans"].append(copy.deepcopy(basis["current_plan"]))
                basis["pending"] += [{"source_id": id, "quote": "", "kind": "plan"} for id in source_ids]
            else:
                raise ValueError("未知构图依据操作")
            if action in {"exclude_content", "restore_content"} and entry_id in entries:
                basis.setdefault("manual_boundaries", {})[entry_id] = len(state["source_ids"]) - 1
            basis["pending"] = list({digest(item): item for item in basis["pending"]}.values())
            if c.get("mode") == "selected":
                c["source_ids"] = list(dict.fromkeys(c["source_ids"] + [r["source_id"] for r in basis["pending"]]))
            basis["document"]["entries"] = list(entries.values())
            basis.update(dirty=True, frozen=False)
            document, _ = effective_document(basis)
            build = self._after_manual_edit(c, document)
            if build is not _KEEP:
                build.update(phase="done", status="needs_review", reason="history_basis_changed")
                c["autobuild"] = build
            c.update(document=document, history_basis=basis, status="ready", error="",
                     sequence=c["sequence"] + 1, at=now())
            c.pop("readiness", None)
            state["candidate"] = c
            state.pop("graph_candidate", None)
            self._put(db, state)
        return self.get()

    def discuss(self, base, action, *, candidate_id=None, sequence=None, message=None,
                binding=None, response=None, proposal_id=None, selections=None, error="", material_receipt=None,
                prepared_options=None, choice_notes=None):
        from .graph_discussion import (_bound, _candidate, adopt_decision, begin_turn, complete_turn,
                                       resume_validation, waiting_questions, choose_decisions)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            result = None
            if action == "begin":
                result = begin_turn(state, base, candidate_id, sequence, message)
            elif action == "complete":
                complete_turn(state, base, binding, response, conversation_id=self.scope[0], revision=state["revision"] + 1,
                              material_receipt=material_receipt)
            elif action == "fail":
                candidate, discussion = _bound(state, base, binding)
                discussion.update(pending=None, proposal=None, error=str(error)[:1000])
                candidate["sequence"] += 1
            elif action == "discard":
                candidate = _candidate(state, base, candidate_id, sequence)
                discussion = candidate["autobuild"].setdefault("discussion", {"messages": [], "calls": 0, "decisions": []})
                discussion.update(pending=None, proposal=None, error="")
                candidate["sequence"] += 1
            elif action == "continue":
                candidate = _candidate(state, base, candidate_id, sequence)
                build = candidate["autobuild"]
                chat = build.get("discussion") or {}
                if build.get("phase") != "waiting_user" or waiting_questions(candidate["document"]) or chat.get("pending") or chat.get("proposal"):
                    raise ValueError("请先处理待决问题或采用、撤回待确认方案，再继续校验。")
                resume_validation(build, candidate["document"])
                candidate["sequence"] += 1
                state.pop("graph_candidate", None)
            elif action in {"adopt", "choose"}:
                before = copy.deepcopy(state["candidate"]["document"])
                messages = (choose_decisions(state, base, candidate_id, sequence, selections,
                            conversation_id=self.scope[0], prepared_options=prepared_options, choice_notes=choice_notes)
                    if action == "choose" else adopt_decision(state, base, candidate_id, sequence, proposal_id, conversation_id=self.scope[0]))
                # These remain addressable evidence but outside source_ids:
                # they can never be swept into business extraction by refresh.
                db.executemany("INSERT OR IGNORE INTO source VALUES (?,?,?,?)", [
                    (*self.scope, item["id"], json.dumps({"id": item["id"], "role": "user", "text": item["text"],
                        "purpose": "graph_discussion", "line": None}, ensure_ascii=False)) for item in messages])
                c = state["candidate"]
                if c.get("history_basis"):
                    from .effective_history import synchronize_manual_basis
                    c["history_basis"] = synchronize_manual_basis(c["history_basis"], before, c["document"], state["source_ids"])
            else:
                raise ValueError("未知构图讨论操作。")
            state["candidate"]["at"] = now()
            self._put(db, state)
        return result

    def request_graph_repair(self, base, entry_id=None):
        from .graph_repair import issue_request
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            request, prompt = issue_request(state, base, entry_id)
            state["repair_request"] = request
            state.pop("repair_preview", None)
            self._put(db, state)
        return {"ticket": request["id"], "prompt": prompt, "entry_ids": request["editable_ids"]}

    def request_local_graph_repair(self, base):
        from .graph_local_repair import issue_request
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            request, prompt = issue_request(state, base)
            state["repair_request"] = request
            state.pop("repair_preview", None)
            self._put(db, state)
        return {"ticket": request["id"], "prompt": prompt}

    def preview_graph_dedupe(self, base, choices=None):
        from .draft_dedupe import apply_dedupe
        from .graph_local_repair import issue_request, validate_document
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            request, _ = issue_request(state, base)
            document, changes, unresolved = apply_dedupe(state["candidate"]["document"], choices)
            validation = validate_document(document, base, conversation_id=self.scope[0], revision=state["revision"] + 1)
            preview = {"id": uuid.uuid4().hex, "ticket": request["id"], "local": True, "dedupe": True,
                       "summary": f"合并 {len(changes)} 项重复定义或引用；{len(unresolved)} 组待选择保留版本。原文和来源保留。",
                       "changes": changes, "unresolved": unresolved, "document": document, "validation": validation,
                       "adoption": {"allowed": bool(changes) and not unresolved,
                                    "partial": bool(changes) and not unresolved and not validation["valid"]}}
            state["repair_request"], state["repair_preview"] = request, preview
            self._put(db, state)
        return {k: v for k, v in preview.items() if k != "document"}

    def preview_graph_repair(self, base, proposal):
        from .graph_repair import preview_repair
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            if (state.get("repair_request") or {}).get("local"):
                from .graph_local_repair import preview_repair
            preview = preview_repair(state, base, proposal)
            state["repair_preview"] = preview
            self._put(db, state)
        return {k: v for k, v in preview.items() if k != "document"}

    def adopt_graph_repair(self, base, preview_id):
        from .graph_repair import require_current
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            preview = state.get("repair_preview")
            if not preview or preview["id"] != preview_id:
                raise ValueError("修订预览已失效，请重新查看差异。")
            _, c = require_current(state, base, preview["ticket"])
            if not preview.get("changes"):
                raise ValueError("当前没有可采用的结构变更；请查看修复说明。")
            document = copy.deepcopy(preview["document"])
            from .graph_inspection import diagnose
            validation = diagnose(document, base, conversation_id=self.scope[0], revision=state["revision"] + 1)
            if preview.get("dedupe"):
                from .draft_dedupe import verify_retirements
                if preview.get("unresolved") or not verify_retirements(c["document"], document):
                    raise ValueError("请先选择分歧组的保留版本并重新预览合并。")
            elif preview.get("local"):
                from .graph_local_repair import validate_document
                validation = validate_document(document, base, conversation_id=self.scope[0], revision=state["revision"] + 1)
                if not validation["valid"]:
                    raise ValueError("一次修复尚未通过整图执行校验；未采用或覆盖当前图。")
            from .graph_repair import adoption_status
            edited = {e["entry_id"] for e in preview["changes"]}
            adoption = adoption_status(state, base, document, validation, edited)
            if not adoption["allowed"]:
                raise ValueError("修法仍未通过完整校验，请将试修错误交给 AI 完成修订；未写入候选图。")
            for e in document["entries"]:
                if e["id"] in edited and not (preview.get("local") and e["kind"] == "node"):
                    e["reviewed"] = True  # Explicit UI adoption, never a model assertion.
            if c.get("history_basis"):
                from .effective_history import synchronize_manual_basis
                c["history_basis"] = synchronize_manual_basis(c["history_basis"], c["document"], document, state["source_ids"])
            if c.get("mode") != "selected":
                # The user adopts edits to the bound graph snapshot, not any
                # new business requests posted while discussing the repair.
                c.update(mode="selected", source_ids=list(c["processed_ids"]),
                         base_processed_ids=list(c["processed_ids"]), selection_context={})
                c.pop("reused_ids", None)
            c.update(document=document, sequence=c["sequence"] + 1, at=now(), repair_scope=True)
            from .graph_readiness import readiness
            c["readiness"] = {**readiness(document, base, conversation_id=self.scope[0], revision=state["revision"] + 1), "at": now()}
            edited_build = self._after_manual_edit(c, document)
            if edited_build is not _KEEP:
                if preview.get("local") and not preview.get("dedupe"):
                    from .graph_discussion import waiting_questions
                    if not waiting_questions(document):
                        edited_build.update(phase="done", status="needs_review", reason="manual_edits_after_review")
                c["autobuild"] = edited_build
            if preview.get("local") and validation["valid"]:
                state["local_repair_run"] = {"id": uuid.uuid4().hex, "candidate_id": c["id"],
                    "sequence": c["sequence"], "status": "completed", "adopted": True,
                    "finished_at": now(), "error": ""}
            c.setdefault("log", []).append({"status": "repair", "at": now(), "sources": 0,
                "summary": preview["summary"], "entry_ids": sorted(edited), "accepted_via": "graph_repair_adoption",
                "partial": adoption["partial"], "remaining_diagnostics": validation["diagnostics"]})
            state.pop("graph_candidate", None)
            state.pop("repair_request", None)
            state.pop("repair_preview", None)
            self._put(db, state)
        return self.get()

    @staticmethod
    def ready_candidate(state, candidate_id, sequence, *, check_references=True):
        """Validate a complete snapshot without pretending a person reviewed prose."""
        c = state.get("candidate")
        if not c or c["id"] != candidate_id or c["sequence"] != sequence or c["base_revision"] != state["revision"]:
            raise ValueError("候选底稿已变化，请重新核对")
        if (c.get("history_basis") or {}).get("dirty") or (c.get("history_basis") or {}).get("pending"):
            raise ValueError("构图依据已修改，请先按有效方案更新图后再保存")
        selected = c.get("mode") == "selected"
        frozen = c.get("history_source_ids", state["source_ids"])
        target = c["source_ids"] if selected else frozen
        prefix = c.get("base_processed_ids", state["processed_ids"]) if selected else []
        if (c["status"] != "ready" or target != c["processed_ids"] or state["warnings"]
                or not set(target).issubset(state["source_ids"]) or state["source_ids"][:len(prefix)] != prefix
                or not set(c.get("reused_ids", [])).issubset(state["source_ids"])
                or state["source_ids"][:len(frozen)] != frozen
                or selected and not set(state.get("selected_ids", [])).issubset(state["source_ids"])):
            raise ValueError("历史尚未完整整理或存在读取缺口，请先处理再确认")
        document, _ = normalize_legacy_document(c["document"])
        if check_references:
            check_entry_refs(document["entries"])
            from .draft_keys import duplicate_diagnostics
            if problems := duplicate_diagnostics(document):
                raise ValueError("；".join(p["message"] for p in problems))
        return document

    def _accept(self, db, state, candidate_id, sequence, *, accepted_via="text_review", accepted_graph_hash=None):
        build = (state.get("candidate") or {}).get("autobuild") or {}
        from .graph_local_repair import unchanged_current_check
        structure_checked = unchanged_current_check(state, (state.get("candidate") or {}).get("base_snapshot"))
        from .graph_discussion import waiting_questions
        if ((build.get("phase", "done") != "done" and not structure_checked)
                or build and waiting_questions(state["candidate"]["document"])):
            raise ValueError("当前图仍在讨论或修改后待校验，请继续当前构建后再采用。")
        document = self.ready_candidate(state, candidate_id, sequence)
        c = state["candidate"]
        selected = c.get("mode") == "selected"
        target = c["source_ids"] if selected else c.get("history_source_ids", state["source_ids"])
        prefix = c.get("base_processed_ids", state["processed_ids"]) if selected else []
        state.update(revision=state["revision"] + 1, document=document,
                     processed_ids=prefix if selected else c["processed_ids"], candidate=None,
                     selected_ids=list(dict.fromkeys(state.get("selected_ids", []) + target)) if selected else [])
        if c.get("history_basis"):
            state["history_basis"] = copy.deepcopy(c["history_basis"])
        if c.get("autobuild"):
            build = c["autobuild"]
            state["autobuild_result"] = {
                "revision": state["revision"], "phase": "done", "status": build.get("status"),
                "reason": build.get("reason", ""), "model_calls": build.get("model_calls", 0),
                "call_attempts": build.get("call_attempts", build.get("model_calls", 0)),
                "retired_review_calls": build.get("retired_review_calls", 0),
                "retired_gap_fill_calls": build.get("retired_gap_fill_calls", 0),
                "remaining_active_seconds": build.get("remaining_active_seconds", 0),
                "review": copy.deepcopy(build.get("final_review") or build.get("review")),
                "pending_gaps": copy.deepcopy(build.get("pending_gaps", [])),
                "remaining": copy.deepcopy(build.get("remaining", [])),
                "autofix": copy.deepcopy(build.get("autofix", {})),
                "gap_fill": copy.deepcopy(build.get("gap_fill", {})),
                "gap_fill_history": copy.deepcopy(build.get("gap_fill_history", [])),
                "previous_attempts": copy.deepcopy(build.get("previous_attempts", [])),
                "review_stale": build.get("review_stale", False),
                "discussion": copy.deepcopy(build.get("discussion")),
                "review_history": copy.deepcopy(build.get("review_history", [])),
                "graph_hash": accepted_graph_hash,
            }
            if structure_checked:
                state["autobuild_result"]["structure_check"] = {
                    "valid": True, "graph_hash": accepted_graph_hash,
                    "ticket": state["repair_preview"]["ticket"], "at": now(),
                }
        version = {"revision": state["revision"], "document": document, "processed_ids": state["processed_ids"],
                   "selected_ids": state["selected_ids"], "at": now(), "accepted_via": accepted_via}
        if state.get("history_basis"):
            version["history_basis"] = copy.deepcopy(state["history_basis"])
        if c.get("autobuild"):
            version["autobuild_result"] = copy.deepcopy(state["autobuild_result"])
        db.execute("INSERT INTO version VALUES (?,?,?,?)", (*self.scope, state["revision"], json.dumps(version, ensure_ascii=False)))

    def confirm(self, candidate_id, sequence):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            self._accept(db, state, candidate_id, sequence)
            state.pop("graph_candidate", None)
            self._put(db, state)
        return self.get()

    def versions(self):
        with self.db() as db:
            rows = db.execute("SELECT data FROM version WHERE conversation=? AND name=? ORDER BY revision DESC", self.scope).fetchall()
        return [{key: v[key] for key in ("revision", "at")} for v in (json.loads(r[0]) for r in rows)]

    def restore(self, revision, expected_revision):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            if state["revision"] != expected_revision or state.get("candidate"):
                raise ValueError("有新版本或未确认的整理结果，请先完成核对")
            row = db.execute("SELECT data FROM version WHERE conversation=? AND name=? AND revision=?", (*self.scope, revision)).fetchone()
            if not row:
                raise ValueError("历史版本不存在")
            old = json.loads(row[0])
            state.update(revision=state["revision"] + 1, document=old["document"], processed_ids=old["processed_ids"],
                         selected_ids=old.get("selected_ids", []))
            state["autobuild_result"] = copy.deepcopy(old.get("autobuild_result"))
            state["history_basis"] = copy.deepcopy(old.get("history_basis"))
            if state["autobuild_result"] is not None:
                state["autobuild_result"]["revision"] = state["revision"]
            restored = {"revision": state["revision"], "document": state["document"], "processed_ids": state["processed_ids"],
                        "selected_ids": state["selected_ids"], "at": now(), "restored_from": revision}
            restored["history_basis"] = copy.deepcopy(state.get("history_basis"))
            db.execute("INSERT INTO version VALUES (?,?,?,?)", (*self.scope, state["revision"], json.dumps(restored, ensure_ascii=False)))
            state.pop("graph_candidate", None)
            self._put(db, state)
        return self.get()

    def save_graph_preview(self, spec, base_spec, revision, changes=None, *, candidate=None):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            if state["revision"] != revision or (state.get("candidate") and candidate is None):
                raise ValueError("底稿已更新，请重新生成流程预览")
            preview = {"id": uuid.uuid4().hex, "revision": revision, "base_hash": digest(base_spec), "spec": spec,
                       "changes": changes or [], "source_hash": digest(state["source_ids"])}
            if candidate is not None:
                self.ready_candidate(state, candidate["id"], candidate["sequence"])
                preview.update(candidate_id=candidate["id"], sequence=candidate["sequence"],
                               source_hash=digest(state["source_ids"]),
                               basis_source_hash=digest(candidate.get("history_source_ids", state["source_ids"])))
            state["graph_candidate"] = preview
            self._put(db, state)
        return preview

    def mark_graph_applied(self, preview_id, spec):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._get(db)
            preview = state.get("graph_candidate")
            if not preview or preview["id"] != preview_id:
                raise ValueError("预览已更新，请刷新查看已保存的画布")
            if preview.get("candidate_id") and not preview.get("applied_hash"):
                self._accept(db, state, preview["candidate_id"], preview["sequence"], accepted_via="graph_save",
                             accepted_graph_hash=digest(spec))
                preview["revision"] = state["revision"]
            preview["applied_hash"] = digest(spec)
            self._put(db, state)
