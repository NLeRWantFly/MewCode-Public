"""Multi-round plan session: goal, spec draft, bindings, control plan, status.

Store: JSON at ``user_data_root()/orchestration-sessions.json``, keyed by
project path (same normalization as ExpertFlowStore). An in-memory cache
covers the process lifetime; missing/unreadable files start empty.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from mewcode.orchestration.control_plan import ControlPlan, build_control_plan
from mewcode.orchestration.spec import OrchestrationError, OrchestrationSpec
from mewcode.orchestration.validate import unbound_node_ids
from mewcode.runtime_paths import user_data_root

PlanStatus = Literal["drafting", "binding", "ready", "running"]
PLAN_SESSION_STATUSES = ("drafting", "binding", "ready", "running")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def infer_plan_status(
    spec: OrchestrationSpec,
    *,
    confirmed: bool = False,
    activate: bool = False,
) -> PlanStatus:
    if activate:
        return "running"
    missing = unbound_node_ids(spec)
    if not missing:
        return "ready"
    bound = sum(
        1
        for node in spec.nodes
        if node.kind != "gate" and node.model is not None and node.model.is_complete
    )
    if bound > 0:
        return "binding"
    if confirmed:
        return "binding"
    return "drafting"


@dataclass(frozen=True)
class TurnSummary:
    turn: int
    revision: str = ""
    template_id: str = ""
    note: str = ""
    timestamp: str = ""

    @classmethod
    def from_value(cls, value: Any) -> "TurnSummary":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise OrchestrationError("turn summary must be an object")
        return cls(
            turn=int(value.get("turn") or 0),
            revision=str(value.get("revision") or "").strip(),
            template_id=str(value.get("template_id") or "").strip(),
            note=str(value.get("note") or "").strip(),
            timestamp=str(value.get("timestamp") or "").strip(),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "revision": self.revision,
            "template_id": self.template_id,
            "note": self.note,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class PlanSession:
    session_id: str
    goal: str = ""
    spec: OrchestrationSpec | None = None
    turn_summaries: tuple[TurnSummary, ...] = ()
    bindings: dict[str, Any] = field(default_factory=dict)
    control_plan: ControlPlan | None = None
    status: PlanStatus = "drafting"

    @classmethod
    def from_value(cls, value: Any) -> "PlanSession":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise OrchestrationError("plan session must be an object")
        raw_spec = value.get("spec")
        spec = OrchestrationSpec.from_value(raw_spec) if raw_spec else None
        raw_plan = value.get("control_plan")
        control_plan = ControlPlan.from_value(raw_plan) if raw_plan else None
        status = str(value.get("status") or "drafting").strip()
        if status not in PLAN_SESSION_STATUSES:
            status = "drafting"
        bindings = value.get("bindings") if isinstance(value.get("bindings"), dict) else {}
        turns = tuple(
            TurnSummary.from_value(item) for item in (value.get("turn_summaries") or [])
        )
        return cls(
            session_id=str(value.get("session_id") or value.get("id") or "").strip()
            or uuid.uuid4().hex[:12],
            goal=str(value.get("goal") or (spec.goal if spec else "")).strip(),
            spec=spec,
            turn_summaries=turns,
            bindings=dict(bindings),
            control_plan=control_plan,
            status=status,  # type: ignore[arg-type]
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "goal": self.goal,
            "spec": self.spec.as_dict() if self.spec is not None else None,
            "turn_summaries": [item.as_dict() for item in self.turn_summaries],
            "bindings": dict(self.bindings),
            "control_plan": self.control_plan.as_dict() if self.control_plan is not None else None,
            "status": self.status,
        }


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


def build_plan_session(
    spec: OrchestrationSpec,
    *,
    session_id: str = "",
    goal: str = "",
    bindings: dict[str, Any] | None = None,
    previous: PlanSession | None = None,
    revision: str = "",
    note: str = "",
    confirmed: bool = False,
    activate: bool = False,
    draft_plan: bool = True,
) -> PlanSession:
    sid = session_id or (previous.session_id if previous else "") or new_session_id()
    turns = list(previous.turn_summaries) if previous else []
    turn_no = (turns[-1].turn + 1) if turns else 1
    turns.append(
        TurnSummary(
            turn=turn_no,
            revision=revision,
            template_id=spec.template_id,
            note=note,
            timestamp=_now(),
        )
    )
    status = infer_plan_status(spec, confirmed=confirmed, activate=activate)
    plan = build_control_plan(spec, draft=draft_plan and status in {"drafting", "binding"})
    merged_bindings = dict(previous.bindings) if previous else {}
    if bindings:
        merged_bindings.update(bindings)
    return PlanSession(
        session_id=sid,
        goal=goal or spec.goal,
        spec=spec,
        turn_summaries=tuple(turns),
        bindings=merged_bindings,
        control_plan=plan,
        status=status,
    )


class PlanSessionStore:
    """Project-keyed plan sessions (JSON under user data + in-memory cache)."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path or user_data_root() / "orchestration-sessions.json")
        self._memory: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _project_key(project_path: str | os.PathLike[str]) -> str:
        resolved = Path(project_path).expanduser().resolve()
        return os.path.normcase(str(resolved)).replace("\\", "/")

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            data = value if isinstance(value, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {}
        self._memory.update({key: val for key, val in data.items() if isinstance(val, dict)})
        return dict(self._memory)

    def _save(self, data: dict[str, Any]) -> None:
        self._memory = {key: val for key, val in data.items() if isinstance(val, dict)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)

    def get(self, project_path: str | os.PathLike[str]) -> PlanSession | None:
        value = self._load().get(self._project_key(project_path))
        if not isinstance(value, dict):
            return None
        try:
            return PlanSession.from_value(value)
        except (OrchestrationError, TypeError, ValueError):
            return None

    def put(
        self,
        project_path: str | os.PathLike[str],
        session: PlanSession | dict[str, Any],
    ) -> PlanSession:
        parsed = PlanSession.from_value(session)
        data = self._load()
        data[self._project_key(project_path)] = parsed.as_dict()
        self._save(data)
        return parsed

    def delete(self, project_path: str | os.PathLike[str]) -> None:
        data = self._load()
        if data.pop(self._project_key(project_path), None) is None:
            return
        self._save(data)


_STORE: PlanSessionStore | None = None


def get_plan_session_store() -> PlanSessionStore:
    global _STORE
    if _STORE is None:
        _STORE = PlanSessionStore()
    return _STORE
