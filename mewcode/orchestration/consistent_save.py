"""One recoverable commit for the canvas, conversation plan and execution flow."""
from __future__ import annotations

import base64
import copy
import json
import threading
from dataclasses import replace
from pathlib import Path

from mewcode.orchestration.convert import to_expert_flow
from mewcode.orchestration.pipeline_bridge import to_pipeline_graph
from mewcode.orchestration.plan_session import build_plan_session
from mewcode.orchestration.validate import validate_spec
from mewcode.pipeline.store import VersionConflictError

_LOCK = threading.RLock()


def _ledger(store):
    return getattr(store, "store", store)


def _paths(project, graph_id, plan_store, flow_store):
    # All targets are host-selected, never accepted from the journal or a model.
    return [Path(project) / ".mewcode" / "pipelines" / (graph_id + ".json"),
            Path(_ledger(plan_store).path), Path(_ledger(flow_store).path)]


def _journal(plan_store):
    # A shared ledger has one pending commit, even when the desktop switches
    # projects. Backups of other conversations must stay outside the workspace.
    path = Path(_ledger(plan_store).path)
    return path.with_name("." + path.name + ".orchestration-commit.json")


def _restore(paths, backup, plan_store):
    if len(paths) != len(backup) or any(value is not None and not isinstance(value, str) for value in backup):
        raise ValueError("编排保存恢复备份格式无效，未覆盖宿主存储。")
    # Decode every backup before changing any target, including deletion of a
    # graph that did not exist before the interrupted commit.
    decoded = [None if value is None else base64.b64decode(value, validate=True) for value in backup]
    for path, contents in zip(paths, decoded, strict=True):
        if contents is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".restore.tmp")
            temporary.write_bytes(contents)
            temporary.replace(path)
    if hasattr(_ledger(plan_store), "_memory"):
        _ledger(plan_store)._memory.clear()


def recover_pending_save(project, *, plan_store, flow_store):
    """Roll back an interrupted save before another graph can be read/committed."""
    with _LOCK:
        journal = _journal(plan_store)
        if not journal.exists():
            return
        record = json.loads(journal.read_text(encoding="utf-8"))
        identity = record.get("graph_id")
        if not isinstance(identity, str) or not identity or any(c in identity for c in "/\\:") or identity in {".", ".."}:
            raise ValueError("编排保存恢复记录无效，请检查宿主存储。")
        from mewcode.orchestration.maintenance import project_key
        if record.get("project") != project_key(project):
            raise ValueError("另一个项目有未完成的编排保存，请先回到该项目恢复：" + str(record.get("project") or "未知项目"))
        paths = _paths(project, identity, plan_store, flow_store)
        if record.get("paths") != [str(p.resolve()) for p in paths] or len(record.get("backup", [])) != 3:
            raise ValueError("编排保存恢复记录的所属存储不匹配。")
        _restore(paths, record["backup"], plan_store)
        journal.unlink()


def save_consistently(project, *, spec, graph, pipeline_store, plan_store, flow_store,
                      previous=None, note="画布写回编排方案", confirmed=False, increment_version=True):
    """Validate first; journal every old file; rollback even after a late failure.

    There are no awaits in this commit. HTTP readers cannot see a partial save.
    The durable journal also restores the old revision after process interruption.
    """
    with _LOCK:
        recover_pending_save(project, plan_store=plan_store, flow_store=flow_store)
        old = pipeline_store.get_pipeline(graph.graph_id)
        if old and old.version != graph.version:
            raise VersionConflictError(old.version)
        if any(c in graph.graph_id for c in "/\\:") or graph.graph_id in {".", ".."}:
            raise ValueError("画布ID无效。")
        version = (max(spec.version, old.version + 1) if increment_version else old.version) if old else max(spec.version, graph.version)
        from mewcode.orchestration.maintenance import now
        stamp = old.updated_at if old and not increment_version else now()
        spec = replace(spec, version=version, meta={**spec.meta,
            "pipeline_graph_id": graph.graph_id, "pipeline_graph_version": version,
            "pipeline_graph_updated_at": stamp})
        validate_spec(spec, require_bindings=False)
        flow = to_expert_flow(spec)
        canonical = to_pipeline_graph(spec, graph_id=graph.graph_id, version=version)
        # Preserve visual choices from the submitted canvas without importing
        # execution metadata that could disagree with the validated spec.
        visual = json.loads(graph.variables.get("__orch.payload__", "{}"))
        payload = json.loads(canonical.variables["__orch.payload__"])
        for identity, entry in payload.get("nodes", {}).items():
            previous_entry = visual.get("nodes", {}).get(identity, {})
            for field in ("canvas_size", "canvas_group_size", "checkpoint_layout_version"):
                if field in previous_entry:
                    entry[field] = copy.deepcopy(previous_entry[field])
        positions = {node.id: node.position for node in graph.nodes}
        canonical.nodes = [node.model_copy(update={"position": positions[node.id]}) if node.id in positions else node
                           for node in canonical.nodes]
        canonical.variables = {**graph.variables, **canonical.variables,
                               "__orch.payload__": json.dumps(payload, ensure_ascii=False)}
        canonical.default_model, canonical.default_effort = graph.default_model, graph.default_effort
        canonical.created_at = old.created_at if old else graph.created_at or stamp
        canonical.updated_at = stamp
        session = build_plan_session(spec, session_id=previous.session_id if previous else "",
            goal=spec.goal, previous=previous, note=note, confirmed=confirmed, activate=False, draft_plan=not confirmed)
        paths = _paths(project, graph.graph_id, plan_store, flow_store)
        backup = [base64.b64encode(p.read_bytes()).decode("ascii") if p.exists() else None for p in paths]
        for path in paths[1:]:
            if path.exists() and not isinstance(json.loads(path.read_text(encoding="utf-8")), dict):
                raise ValueError("方案或执行流的宿主存储损坏，未写入编排。")
        memory = copy.deepcopy(getattr(_ledger(plan_store), "_memory", None))
        journal = _journal(plan_store)
        journal.parent.mkdir(parents=True, exist_ok=True)
        temporary = journal.with_suffix(".tmp")
        from mewcode.orchestration.maintenance import project_key
        temporary.write_text(json.dumps({"graph_id": graph.graph_id, "project": project_key(project),
            "paths": [str(p.resolve()) for p in paths], "backup": backup}), encoding="utf-8")
        temporary.replace(journal)
        try:
            paths[0].parent.mkdir(parents=True, exist_ok=True)
            pipeline_store._save_graph_file(paths[0], canonical)
            if memory is not None:
                _ledger(plan_store)._memory.clear()
            plan_store.put(project, session)
            flow_store.put(project, flow)
            # This is the commit point. Until here, recovery restores all three.
            journal.unlink()
        except Exception:
            try:
                _restore(paths, backup, plan_store)
            except OSError as rollback:
                raise OSError("编排保存失败，回滚尚未完成；恢复记录已保留，请回到原项目重新打开并核对：" + str(rollback)) from rollback
            if memory is not None:
                _ledger(plan_store)._memory = memory
            journal.unlink(missing_ok=True)
            raise
        return canonical, spec, flow, session
