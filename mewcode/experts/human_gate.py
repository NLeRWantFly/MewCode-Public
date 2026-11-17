"""Current human approval, scoped to the node plan."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def gate_scope_hash(node: Any, meta: dict[str, Any]) -> str:
    payload = {"node_id": node.node_id, "responsibility": node.responsibility,
               "brief": (meta.get("node_plan_briefs") or {}).get(node.node_id)}
    constraints = (meta.get("node_constraints") or {}).get(node.node_id)
    if constraints:
        payload["constraints"] = constraints
    grants = (meta.get("authorization_grants") or {}).get(node.node_id)
    if grants:
        payload["authorization_grants"] = grants
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


async def await_gate_decision(*, node, flow, session, store, run_id, emit, proposal_builder=None):
    from mewcode.experts.gate_dialogue import await_dialogue
    return await await_dialogue(node=node, flow=flow, session=session, store=store,
                                run_id=run_id, emit=emit, proposal_builder=proposal_builder)
