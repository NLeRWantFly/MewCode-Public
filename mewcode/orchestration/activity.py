"""Durable node activity, shared by live hooks and historical replay.

Execution facts are immutable with respect to attribution: associating an old
message with a node does not complete that node. Graph bindings and corrections
are separate from the provider transcript and never schedule work.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
MAX_TEXT = 16000
_EXPERT_FLOW_PHASES = frozenset({
    "setup", "preparation", "executing", "review", "debate", "recovery",
    "rework", "ruling", "verification", "finalizing", "loop", "paused",
    "blocked", "completed", "hitl_blocked",
})


def stamp(value=None):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return str(value or datetime.now(timezone.utc).isoformat())


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _hash(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


_NODE_NONE = {"无", "none", "-"}


def candidates(event, nodes, windows=()):
    """Only explicit unique names auto-bind; content/path matches are suggestions."""
    explicit = str(event.get("node_id") or "")
    names = {str(n.get("id") or n.get("node_id")): n for n in nodes}
    if explicit in names:
        return explicit, "explicit", []
    matches = [key for key, node in names.items() if explicit and explicit in
               {str(node.get("label") or ""), str(node.get("title") or "")}]
    if len(matches) == 1:
        return matches[0], "exact_label", []
    folded = explicit.strip().casefold()
    if folded in _NODE_NONE:
        return "", "unassigned", []
    if folded:
        hits = set()
        for key, node in names.items():
            label = str(node.get("label") or "").strip()
            texts = (key.casefold(), label.casefold(), str(node.get("title") or "").strip().casefold())
            if any(folded in text for text in texts if text):
                hits.add(key)
            elif len(label) >= 2 and label.casefold() in folded:
                hits.add(key)
        if len(hits) == 1:
            return hits.pop(), "explicit", []
    body = (str(event.get("text") or "") + " " + _json(event.get("arguments") or {})).casefold()
    suggestions = []
    for key, node in names.items():
        label = str(node.get("label") or "").strip()
        inputs = node.get("known_inputs") or []
        outputs = node.get("outputs") or []
        paths = [str(v) for v in [*inputs, *outputs] if "/" in str(v) or "\\" in str(v) or "." in str(v)]
        reasons = []
        if label and len(label) >= 3 and label.casefold() in body:
            reasons.append("记录提到节点名称")
        if any(p.casefold() in body for p in paths):
            reasons.append("记录涉及节点的输入或产出文件")
        for window in windows:
            if window["node"] not in {key, label} or not window["start"] or not window["end"]:
                continue
            try:
                parse = lambda v: datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                if parse(window["start"]) <= parse(event.get("at")) <= parse(window["end"]):
                    reasons.append("时间落在该节点已记录的执行区间内（仅候选）")
                    break
            except (TypeError, ValueError):
                pass
        if reasons:
            suggestions.append({"node_id": key, "label": label, "reason": "；".join(reasons)})
    if folded:
        return "", "unresolved", suggestions[:6]
    return "", "unassigned", suggestions[:6]


class ActivityStore:
    def __init__(self, work_dir):
        self.work_dir = str(Path(work_dir).resolve())
        self.path = Path(self.work_dir) / ".mewcode" / "node-activity.sqlite3"

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript("""
                CREATE TABLE IF NOT EXISTS clock (id INTEGER PRIMARY KEY, revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO clock VALUES (1,0);
                CREATE TABLE IF NOT EXISTS activity (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, source_seq INTEGER NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS activity_scope ON activity(conversation_id,run_id,revision);
                CREATE TABLE IF NOT EXISTS binding (
                    conversation_id TEXT NOT NULL, graph_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    data TEXT NOT NULL, PRIMARY KEY(conversation_id,graph_id,run_id));
                CREATE TABLE IF NOT EXISTS attribution (
                    graph_id TEXT NOT NULL, event_id TEXT NOT NULL, node_id TEXT NOT NULL,
                    PRIMARY KEY(graph_id,event_id));
                CREATE TABLE IF NOT EXISTS replay (path TEXT PRIMARY KEY, offset INTEGER NOT NULL,
                    size INTEGER NOT NULL, signature TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS artifact (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    run_id TEXT NOT NULL, data TEXT NOT NULL);
                PRAGMA user_version=1;
                """)
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def tick(db):
        db.execute("UPDATE clock SET revision=revision+1 WHERE id=1")
        return db.execute("SELECT revision FROM clock WHERE id=1").fetchone()[0]

    def bind(self, conversation_id, graph_id, run_id, *, nodes, version=1, title=""):
        if not all((conversation_id, graph_id, run_id)):
            return
        data = {"project": self.work_dir, "conversation_id": conversation_id, "graph_id": graph_id,
                "run_id": run_id, "graph_version": version, "nodes": nodes, "title": title,
                "version_nodes": {str(version): nodes},
                "bound_at": stamp()}
        with self.db() as db:
            previous = db.execute("SELECT data FROM binding WHERE conversation_id=? AND graph_id=? AND run_id=?",
                                  (conversation_id, graph_id, run_id)).fetchone()
            if previous:
                data = json.loads(previous[0])
                versions = data.setdefault("version_nodes", {str(data["graph_version"]): data["nodes"]})
                if str(version) in versions:
                    return
                versions[str(version)] = nodes
            db.execute("INSERT OR REPLACE INTO binding VALUES (?,?,?,?)",
                       (conversation_id, graph_id, run_id, _json(data)))

    def bindings(self, conversation_id, graph_id):
        with self.db() as db:
            rows = db.execute("SELECT data FROM binding WHERE conversation_id=? AND graph_id=?",
                              (conversation_id, graph_id)).fetchall()
        return sorted([json.loads(row[0]) for row in rows], key=lambda x: x["bound_at"], reverse=True)

    def latest_chat_run(self, conversation_id):
        with self.db() as db:
            row = db.execute("SELECT run_id FROM activity WHERE conversation_id=? AND run_id LIKE 'chat:%' "
                             "ORDER BY revision DESC LIMIT 1", (conversation_id,)).fetchone()
        return row[0] if row else ""

    @staticmethod
    def chat_view_run(conversation_id):
        return "chat:conversation:" + conversation_id

    def count_unassigned(self, conversation_id, run_id):
        aggregate = run_id == self.chat_view_run(conversation_id)
        with self.db() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM activity WHERE conversation_id=? AND (run_id=? OR (? AND run_id LIKE 'chat:%'))"
                " AND COALESCE(json_extract(data,'$.node_id'),'')=''"
                " AND json_extract(data,'$.follow_node') IS NULL",
                (conversation_id, run_id, aggregate)).fetchone()
        return int(row[0])

    def bind_chat_history(self, conversation_id, graph_id, *, nodes, version=1):
        """Bind a read projection, not an execution, without changing the graph."""
        run_id = self.chat_view_run(conversation_id)
        self.bind(conversation_id, graph_id, run_id, nodes=nodes, version=version,
                  title="当前对话全部记录（非节点验收结论）")
        # Unlike an execution lock, this overlay follows the currently edited
        # graph; explicit event versions still use version_nodes.
        with self.db() as db:
            row = db.execute("SELECT data FROM binding WHERE conversation_id=? AND graph_id=? AND run_id=?",
                             (conversation_id, graph_id, run_id)).fetchone()
            if row:
                data = json.loads(row[0])
                if data["graph_version"] != version or data["nodes"] != nodes:
                    data.update(graph_version=version, nodes=nodes)
                    db.execute("UPDATE binding SET data=? WHERE conversation_id=? AND graph_id=? AND run_id=?",
                               (_json(data), conversation_id, graph_id, run_id))
        return run_id

    def record(self, conversation_id, run_id, key, data, *, source_seq=0, append=False, _db=None):
        if not conversation_id or not run_id or not key:
            return None
        event_id = _hash(f"{conversation_id}|{run_id}|{key}")
        with (nullcontext(_db) if _db is not None else self.db()) as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT source_seq,data FROM activity WHERE id=?", (event_id,)).fetchone()
            if previous and source_seq and previous["source_seq"] >= source_seq:
                return event_id
            old = json.loads(previous["data"]) if previous else {}
            if append:
                data = {**data, "text": (str(old.get("text") or "") + str(data.get("text") or ""))[:MAX_TEXT]}
            merged = {**old, **data, "id": event_id, "conversation_id": conversation_id, "run_id": run_id}
            if old.get("at"):
                merged["at"] = old["at"]
            merged["text"] = str(merged.get("text") or "")[:MAX_TEXT]
            if merged == old:
                return event_id
            seq = self.tick(db)
            db.execute("INSERT OR REPLACE INTO activity VALUES (?,?,?,?,?,?)",
                       (event_id, conversation_id, run_id, seq, source_seq, _json(merged)))
        return event_id

    def pipeline(self, event, *, historical=False, _db=None):
        if event.get("kind") == "thinking":
            return  # Private reasoning is not a user-facing work report.
        conv = str(event.get("conversation_id") or "")
        task = str(event.get("task_id") or ("legacy:" + conv))
        run_id = "chat:" + task
        meta = event.get("meta") or {}
        args = meta.get("arguments") or {}
        phase = event.get("phase")
        data = {"at": stamp(event.get("started_at") or event.get("timestamp")),
                "updated_at": stamp(event.get("completed_at") or event.get("timestamp") or event.get("started_at")), "kind": event.get("kind", "message"),
                "role": event.get("role"), "name": event.get("name", ""),
                "status": event.get("status") or "running", "turn_id": event.get("turn_id"),
                "text": event.get("text", ""), "source": "history" if historical else "runtime",
                "source_ref": str(event.get("item_id") or ""), "task_id": task}
        if args:
            data["arguments"] = args
        if meta.get("node_id"):
            data["node_id"] = str(meta["node_id"])
        if event.get("name") == "report_step" and isinstance(args, dict) and args.get("node"):
            data.update(node_id=str(args["node"]), reported_status=args.get("status"), source="model_report")
        if phase == "start":
            data["at"] = stamp(event.get("started_at") or event.get("timestamp"))
        elif not historical:
            # Preserve the item's first timestamp on chunk/complete updates.
            data.pop("at", None)
        item_id = event.get("item_id")
        if (data["kind"] in {"call", "artifact"} and item_id and data.get("turn_id")
                and not data.get("node_id")):
            # A tool/file item written while a node is reported running follows
            # that node until the turn's next report; never for prose/lifecycle.
            event_id = _hash(f"{conv}|{run_id}|{item_id}")
            with (nullcontext(_db) if _db is not None else self.db()) as db:
                if phase == "start" or not db.execute(
                        "SELECT 1 FROM activity WHERE id=?", (event_id,)).fetchone():
                    follow = self._follow_anchor(conv, run_id, data["turn_id"], _db=db)
                    if follow:
                        data["follow_node"] = follow
        return self.record(conv, run_id, item_id, data,
                           source_seq=int(event.get("seq") or 0), append=phase in {"chunk", "complete"} and not (meta.get("snapshot") or event.get("snapshot") or "timestamp" not in event), _db=_db)

    def last_reported_node(self, conversation_id, run_id):
        """The node of the newest report_step; the node's own newest event may
        shadow it in overview.latest, so this scans reports across the run."""
        aggregate = run_id == self.chat_view_run(conversation_id)
        with self.db() as db:
            row = db.execute(
                "SELECT json_extract(data,'$.node_id') FROM activity WHERE conversation_id=?"
                " AND (run_id=? OR (? AND run_id LIKE 'chat:%'))"
                " AND json_extract(data,'$.name')='report_step'"
                " AND COALESCE(json_extract(data,'$.node_id'),'') != ''"
                " ORDER BY revision DESC LIMIT 1",
                (conversation_id, run_id, aggregate)).fetchone()
        return str(row[0]) if row else ""

    def _follow_anchor(self, conversation_id, run_id, turn_id, *, _db=None):
        """The turn's newest report_step decides which node later work follows."""
        with (nullcontext(_db) if _db is not None else self.db()) as db:
            row = db.execute(
                "SELECT json_extract(data,'$.node_id'),json_extract(data,'$.reported_status')"
                " FROM activity WHERE conversation_id=? AND run_id=?"
                " AND json_extract(data,'$.turn_id')=? AND json_extract(data,'$.name')='report_step'"
                " ORDER BY revision DESC LIMIT 1",
                (conversation_id, run_id, str(turn_id))).fetchone()
        if not row:
            return ""
        node, status = str(row[0] or ""), str(row[1] or "")
        if status == "running" and node and node.strip().casefold() not in _NODE_NONE:
            return node
        return ""

    def artifact(self, conversation_id, run_id, record, *, turn_id=""):
        identity = _hash(f"{conversation_id}|{run_id}|{record['id']}")
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO artifact VALUES (?,?,?,?)",
                       (identity, conversation_id, run_id, _json(record)))
        receipt = {k: v for k, v in record.items() if k != "preview"}
        receipt["id"] = identity
        data = {"at": stamp(record.get("created_at")), "kind": "artifact", "status": "completed",
                "node_id": record.get("node_id") or "", "text": "已保存 " + str(record.get("name") or record.get("path")),
                "file": receipt, "source": "runtime", "attempt": record.get("node_attempt") or 1}
        if turn_id:
            data["turn_id"] = str(turn_id)
            if not data["node_id"]:
                follow = self._follow_anchor(conversation_id, run_id, turn_id)
                if follow:
                    data["follow_node"] = follow
        self.record(conversation_id, run_id, "file:"+identity, data)
        return identity

    def read_artifact(self, conversation_id, run_id, identity):
        with self.db() as db:
            row = db.execute("SELECT data FROM artifact WHERE id=? AND conversation_id=? AND run_id=?",
                             (identity, conversation_id, run_id)).fetchone()
        if not row:
            raise LookupError("找不到此运行的文件快照")
        return json.loads(row[0])

    def expert(self, event, *, historical=False, _db=None):
        kind = str(event.get("type") or "")
        if event.get("legacy_type") or kind in {"expert_delta", "expert_thinking", "expert_node_delta", "expert_node_thinking",
                                               "expert_member_delta", "expert_member_thinking"}:
            return
        node = str(event.get("node_id") or "")
        if not node and kind not in {"expert_takeover_state", "expert_done", "expert_error"}:
            return
        at = stamp(event.get("server_time") or event.get("at"))
        data = {"at": at, "updated_at": at, "node_id": node, "kind": kind,
                "role": event.get("role") or event.get("member_role") or "",
                "attempt": int(event.get("node_attempt") or event.get("attempt") or 1),
                "member_id": str(event.get("member_id") or ""),
                "graph_version": event.get("graph_version") or event.get("flow_version"),
                "status": "failed" if event.get("is_error") else event.get("status") or "", "name": event.get("name") or event.get("tool_name") or "",
                "text": str(event.get("text") or event.get("output") or event.get("message") or event.get("summary") or ""),
                "source": "history" if historical else "runtime", "source_ref": str(event.get("seq") or ""),
                "arguments": event.get("args") or event.get("arguments") or {}}
        if kind == "expert_step":
            from mewcode.tools.report_step import report_fields
            data.update(report_fields(event))
            data.update(name="report_step", label=event.get("label") or "", source="model_report")
        if kind == "expert_segment_health" and isinstance(event.get("health"), dict):
            data["health"] = event["health"]
            data["text"] = "运行分段健康记录"
        if kind == "expert_usage":
            # UsageEvent.billable_tokens is an integer cumulative counter. Keep
            # only this allowlisted value; raw provider usage/cost metadata is
            # intentionally not copied into the durable activity row.
            node_attempt = event.get("node_attempt")
            data["attempt_identity_known"] = (
                isinstance(node_attempt, int) and not isinstance(node_attempt, bool) and node_attempt > 0
            )
            billable_tokens = event.get("billable_tokens")
            if isinstance(billable_tokens, int) and not isinstance(billable_tokens, bool) and billable_tokens >= 0:
                data["billable_tokens"] = billable_tokens
        if kind == "expert_flow_state":
            # Preserve only a compact, allowlisted phase; never store arbitrary
            # flow-state text as a phase value.
            phase = event.get("phase")
            if isinstance(phase, str) and phase in _EXPERT_FLOW_PHASES:
                data["phase"] = phase
        if kind == "expert_node_started":
            data.update(status="running", text="开始执行这一步")
        elif kind == "expert_node_completed":
            data["status"] = event.get("status") or "completed"
        if event.get("constraints"):
            data["constraints"] = event["constraints"]
        if event.get("file"):
            data["file"] = event["file"]
        key = "expert:" + str(event.get("seq") or _hash(_json(event)))
        return self.record(str(event.get("conversation_id") or ""), str(event.get("run_id") or ""), key, data,
                           source_seq=int(event.get("seq") or 0), _db=_db)

    def replay_expert(self, events, conversation_id, run_id):
        with self.db() as db:
            for event in events:
                self.expert({**event, "conversation_id": conversation_id, "run_id": run_id}, historical=True, _db=db)

    def replay_pending(self, path):
        """Cheap stat-vs-cursor check so polling skips a caught-up replay."""
        path = Path(path)
        if not path.is_file():
            return False
        stat = path.stat()
        signature = str(getattr(stat, "st_ino", 0))
        with self.db() as db:
            cursor = db.execute("SELECT offset,size,signature FROM replay WHERE path=?",
                                (str(path),)).fetchone()
        if cursor is None or cursor["signature"] != signature or stat.st_size < cursor["size"]:
            return True
        return int(cursor["offset"]) < stat.st_size

    def replay_pipeline(self, path, conversation_id, *, limit=2000):
        """Read a bounded increment, never compact or rewrite the source log."""
        path = Path(path)
        if not path.is_file():
            return {"pending": False, "count": 0}
        stat = path.stat()
        signature = str(getattr(stat, "st_ino", 0))
        with self.db() as db:
            cursor = db.execute("SELECT * FROM replay WHERE path=?", (str(path),)).fetchone()
        offset = int(cursor["offset"]) if cursor and cursor["signature"] == signature and stat.st_size >= cursor["size"] else 0
        count = 0
        with path.open("rb") as source, self.db() as db:
            source.seek(offset)
            for _ in range(limit):
                start = source.tell()
                line = source.readline(2 * 1024 * 1024)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    source.seek(start)
                    break
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        event["conversation_id"] = conversation_id
                        self.pipeline(event, historical=True, _db=db)
                except (ValueError, TypeError):
                    pass
                count += 1
            offset = source.tell()
            db.execute("INSERT OR REPLACE INTO replay VALUES (?,?,?,?)", (str(path), offset, stat.st_size, signature))
        return {"pending": offset < stat.st_size, "count": count}

    def page(self, conversation_id, graph_id, run_id, *, node_id=None, after=0, before=0, limit=50, attempt=0, event_ids=None):
        binding = next((b for b in self.bindings(conversation_id, graph_id) if b["run_id"] == run_id), None)
        if binding is None:
            raise LookupError("运行未绑定到此对话和画布")
        aggregate = run_id == self.chat_view_run(conversation_id)
        limit = max(1, min(int(limit), 100))
        with self.db() as db:
            corrections = {r["event_id"]: r["node_id"] for r in db.execute(
                "SELECT event_id,node_id FROM attribution WHERE graph_id=?", (graph_id,))}
            windows = db.execute("""SELECT json_extract(data,'$.node_id') AS node,
                MIN(CASE WHEN json_extract(data,'$.status')='running' THEN json_extract(data,'$.at') END) AS start,
                MAX(CASE WHEN json_extract(data,'$.status') IN ('completed','failed','paused')
                    THEN COALESCE(json_extract(data,'$.updated_at'),json_extract(data,'$.at')) END) AS end
                FROM activity WHERE conversation_id=? AND (run_id=? OR (? AND run_id LIKE 'chat:%')) AND json_extract(data,'$.node_id')<>''
                GROUP BY json_extract(data,'$.node_id'),json_extract(data,'$.attempt'),json_extract(data,'$.graph_version')""",
                (conversation_id, run_id, aggregate)).fetchall()
            # Scan in bounded batches. A node filter must not page over unrelated
            # records as if they had been returned; cursor tracks scan progress.
            selected = tuple(dict.fromkeys(event_ids)) if event_ids is not None else None
            if selected is not None and not 1 <= len(selected) <= 100:
                raise ValueError("每次只能整理 1～100 条记录")
            id_filter = " AND id IN (" + ",".join("?" for _ in selected) + ") " if selected else ""
            rows = db.execute("SELECT * FROM activity WHERE conversation_id=? AND (run_id=? OR (? AND run_id LIKE 'chat:%')) AND revision>? "
                              + id_filter +
                              "AND (?=0 OR revision<?) ORDER BY revision " + ("ASC" if after else "DESC") + " LIMIT 1000",
                              (conversation_id, run_id, aggregate, max(0, after), *(selected or ()), max(0, before), max(0, before))).fetchall()
            current = db.execute("SELECT COALESCE(MAX(revision),0) FROM activity WHERE conversation_id=? AND (run_id=? OR (? AND run_id LIKE 'chat:%'))",
                                 (conversation_id, run_id, aggregate)).fetchone()[0]
        result, removed, scanned, more = [], [], after or before, False
        for index, row in enumerate(rows):
            event = json.loads(row["data"])
            version_nodes = (binding.get("version_nodes") or {}).get(str(event.get("graph_version"))) or binding["nodes"]
            matched, method, options = candidates(event, version_nodes, windows)
            if not matched and method in {"unassigned", "unresolved"}:
                follow = str(event.get("follow_node") or "")
                if follow:
                    followed, _, _ = candidates({"node_id": follow}, version_nodes)
                    if followed:
                        matched, method = followed, "follow"
            if row["id"] in corrections:
                matched, method = corrections[row["id"]], "manual"
            event.update(original_node_id=event.get("node_id") or "", node_id=matched, attribution=method, candidates=options,
                         node_unresolved=method == "unresolved",
                         revision=row["revision"], graph_id=graph_id, graph_version=event.get("graph_version") or binding["graph_version"])
            scanned = row["revision"]
            if node_id is not None and matched != node_id:
                if after:
                    removed.append(row["id"])
                continue
            if attempt and int(event.get("attempt") or 1) != attempt:
                continue
            result.append(event)
            if len(result) >= limit:
                more = index < len(rows)-1 or len(rows) == 1000
                break
        else:
            more = len(rows) == 1000
        return {"items": result, "removed_ids": removed, "cursor": current, "next_cursor": scanned or current,
                "has_more": more, "scope": {k: v for k, v in binding.items() if k != "nodes"}}

    def overview(self, conversation_id, graph_id, run_id):
        """Latest facts per explicit/corrected target and bounded artifact list.

        Unlike the timeline page, a busy node cannot evict another node's
        current action or hide all earlier file receipts from this projection.
        Attribution revisions do not change the source chronology.
        """
        binding = next((b for b in self.bindings(conversation_id, graph_id) if b["run_id"] == run_id), None)
        if binding is None:
            raise LookupError("运行未绑定到此对话和画布")
        aggregate = run_id == self.chat_view_run(conversation_id)
        with self.db() as db:
            corrections = {r["event_id"]:r["node_id"] for r in db.execute(
                "SELECT event_id,node_id FROM attribution WHERE graph_id=?", (graph_id,))}
            latest = db.execute("""
                SELECT * FROM (
                  SELECT a.*, ROW_NUMBER() OVER (PARTITION BY
                    CASE WHEN json_extract(a.data,'$.kind') IN ('turn','lifecycle') THEN '__turn__'
                         ELSE COALESCE(t.node_id,NULLIF(json_extract(a.data,'$.node_id'),''),
                                       json_extract(a.data,'$.follow_node'),'') END
                    ORDER BY COALESCE(json_extract(a.data,'$.updated_at'),json_extract(a.data,'$.at')) DESC,
                             a.revision DESC) AS rank
                  FROM activity a LEFT JOIN attribution t ON t.event_id=a.id AND t.graph_id=?
                  WHERE a.conversation_id=? AND (a.run_id=? OR (? AND a.run_id LIKE 'chat:%'))
                ) WHERE rank=1
                """, (graph_id,conversation_id,run_id,aggregate)).fetchall()
            files = db.execute("SELECT * FROM activity WHERE conversation_id=? AND (run_id=? OR (? AND run_id LIKE 'chat:%')) "
                "AND json_type(data,'$.file')='object' ORDER BY revision DESC LIMIT 200", (conversation_id,run_id,aggregate)).fetchall()
            reports = db.execute("""
                SELECT * FROM (
                  SELECT a.*, ROW_NUMBER() OVER (PARTITION BY COALESCE(t.node_id,json_extract(a.data,'$.node_id'),'')
                    ORDER BY COALESCE(json_extract(a.data,'$.updated_at'),json_extract(a.data,'$.at')) DESC,
                             a.revision DESC) AS rank
                  FROM activity a LEFT JOIN attribution t ON t.event_id=a.id AND t.graph_id=?
                  WHERE a.conversation_id=? AND (a.run_id=? OR (? AND a.run_id LIKE 'chat:%'))
                    AND (json_extract(a.data,'$.name')='report_step' OR json_extract(a.data,'$.kind')='expert_step')
                    AND json_extract(a.data,'$.kind')<>'expert_tool_call'
                ) WHERE rank=1
                """, (graph_id,conversation_id,run_id,aggregate)).fetchall()
            count = db.execute("SELECT COUNT(*) FROM activity WHERE conversation_id=? AND (run_id=? OR (? AND run_id LIKE 'chat:%'))",
                               (conversation_id, run_id, aggregate)).fetchone()[0]
        def project(row):
            event=json.loads(row["data"])
            nodes=(binding.get("version_nodes") or {}).get(str(event.get("graph_version"))) or binding["nodes"]
            node,method,options=candidates(event,nodes)
            if not node and method in {"unassigned","unresolved"} and event.get("follow_node"):
                followed,_,_=candidates({"node_id":str(event["follow_node"])},nodes)
                if followed:
                    node,method=followed,"follow"
            if row["id"] in corrections:
                node,method=corrections[row["id"]],"manual"
            return {**event,"node_id":node,"attribution":method,"candidates":options,
                    "node_unresolved": method == "unresolved"}
        # Exact labels and IDs may be distinct raw partitions for the same node.
        # Apply their projected facts in source chronology, never SQL row order.
        projected = sorted((project(r) for r in latest), key=lambda e: str(e.get("updated_at") or e.get("at") or ""))
        return {"latest":projected, "reports":[project(r) for r in reports],
                "files":[project(r) for r in files], "activity_count": count}

    def assign(self, conversation_id, graph_id, run_id, event_id, node_id):
        binding = next((b for b in self.bindings(conversation_id, graph_id) if b["run_id"] == run_id), None)
        if not binding:
            raise LookupError("运行未绑定到此对话和画布")
        aggregate = run_id == self.chat_view_run(conversation_id)
        allowed_nodes = [*binding["nodes"], *(n for nodes in (binding.get("version_nodes") or {}).values() for n in nodes)]
        if node_id and node_id not in {str(n.get("id") or n.get("node_id")) for n in allowed_nodes}:
            raise ValueError("目标节点不存在")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            event = db.execute("SELECT id FROM activity WHERE id=? AND conversation_id=? AND (run_id=? OR (? AND run_id LIKE 'chat:%'))",
                               (event_id, conversation_id, run_id, aggregate)).fetchone()
            if not event:
                raise LookupError("此记录不属于当前运行")
            if node_id is None:
                db.execute("DELETE FROM attribution WHERE graph_id=? AND event_id=?", (graph_id, event_id))
            else:
                db.execute("INSERT OR REPLACE INTO attribution VALUES (?,?,?)", (graph_id, event_id, node_id))
            revision = self.tick(db)
            db.execute("UPDATE activity SET revision=? WHERE id=?", (revision, event_id))
        return {"event_id": event_id, "node_id": node_id, "revision": revision}


def record_pipeline_activity(work_dir, event):
    try:
        ActivityStore(work_dir).pipeline(event)
    except Exception:
        log.exception("node activity hook failed")


def record_expert_activity(work_dir, event):
    try:
        ActivityStore(work_dir).expert(event)
    except Exception:
        log.exception("node activity hook failed")
