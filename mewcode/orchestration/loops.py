"""Structured loop regions over a forward DAG, independent of task domain."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any
from mewcode.orchestration.errors import OrchestrationError

LOOP_BUDGET_LIMITS = {
    "max_iterations": {"label": "最多循环轮次", "min": 1, "max": 20, "default": 4},
    "token_budget": {"label": "Token 执行预算", "min": 1024, "max": 5_000_000, "default": 300_000},
    "timeout_seconds": {"label": "总时限（秒）", "min": 1, "max": 7200, "default": 900},
    "max_stalled_rounds": {"label": "连续无改进轮次", "min": 1, "max": 5, "default": 2},
    "node_max_iterations": {"label": "执行节点工具轮次", "min": 4, "max": 200, "default": 40},
    "reviewer_max_iterations": {"label": "审查节点工具轮次", "min": 4, "max": 200, "default": 12},
}
MAX_FOREACH_ITEMS = 256


def parse_loop_budget(value: dict) -> dict:
    result, errors = {}, []
    for name, rule in LOOP_BUDGET_LIMITS.items():
        raw = value.get(name, rule["default"])
        try:
            number = float(raw)
            if (isinstance(raw, bool) or not math.isfinite(number)
                    or not rule["min"] <= number <= rule["max"]
                    or (name != "timeout_seconds" and not number.is_integer())):
                raise OrchestrationError("循环预算数值无效")
            result[name] = number if name == "timeout_seconds" else int(number)
        except (ValueError, TypeError, OverflowError):
            if value.get("control_mode") == "team" and name in {"token_budget", "timeout_seconds", "max_iterations"}:
                # Inactive legacy hints cannot reject a team graph. Keep valid
                # hints for switching to strict; use defaults for invalid ones.
                result[name] = rule["default"]
                continue
            unit = "数值" if name == "timeout_seconds" else "整数"
            errors.append(f"{rule['label']} ({name})={raw!r}，允许 {rule['min']}～{rule['max']} {unit}")
    if errors:
        raise OrchestrationError(f"循环 {value.get('loop_id') or '未命名'} 预算配置无效：" + "；".join(errors))
    return result


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def is_loop_edge(condition: str) -> bool:
    return str(condition or "").strip().casefold() in {
        "loop", "repeat", "continue", "loop:continue", "循环", "继续循环", "回边", "重试",
    }


def _seq(value, field):
    """Read a list field from untrusted JSON without raising TypeError."""
    if value is None or value == "":
        return []
    if isinstance(value, (str, bytes, int, float, bool)):
        raise OrchestrationError(f"循环字段 {field} 必须是数组")
    try:
        return list(value)
    except TypeError:
        raise OrchestrationError(f"循环字段 {field} 必须是数组") from None


@dataclass(frozen=True)
class LoopContract:
    loop_id: str
    entry_node_id: str
    check_node_id: str
    node_ids: tuple[str, ...]
    exit_condition: str
    criteria: tuple[dict, ...]
    reviewer_node_ids: tuple[str, ...] = ()
    max_iterations: int = 4
    token_budget: int = 300_000
    timeout_seconds: float = 900
    max_stalled_rounds: int = 2
    node_max_iterations: int = 40
    reviewer_max_iterations: int = 12
    mode: str = "converge"
    items: tuple[dict, ...] = ()
    control_mode: str = "strict"
    requires_repair: bool = False
    repair_node_ids: tuple[str, ...] = ()
    repair_dispatch: str = "in_round"
    required_node_ids: tuple[str, ...] = ()
    parallel_groups: tuple[dict, ...] = ()

    @classmethod
    def from_value(cls, value: dict) -> "LoopContract":
        # meta.loops comes from untrusted canvas JSON: a non-object entry used to
        # raise AttributeError ('float' has no attribute 'get').
        if not isinstance(value, dict):
            raise OrchestrationError("循环声明必须是对象")
        condition = str(value.get("exit_condition") or "").strip()
        raw_criteria = _seq(value.get("criteria"), "criteria")
        criteria = tuple(raw_criteria or [{"criterion_id": "EXIT-01", "acceptance_test": condition}])
        if any(not isinstance(item, dict) for item in criteria):
            raise OrchestrationError("循环验收条件必须是对象")
        criteria = tuple({"criterion_id": str(item.get("criterion_id") or "").strip(),
                          "acceptance_test": str(item.get("acceptance_test") or "").strip()}
                         for item in criteria)
        # An explicit finite item list is an execution-method signal. Use the
        # existing serial host queue; concurrency requires a separate proof of
        # disjoint writes and is never inferred from repeated-looking calls.
        mode = str(value.get("mode") or ("foreach" if value.get("items") else "converge"))
        control_mode = str(value.get("control_mode") or "strict")
        if control_mode not in {"team", "strict"}:
            raise OrchestrationError("control_mode 必须为 team 或 strict")
        requires_repair = value.get("requires_repair", False)
        if not isinstance(requires_repair, bool):
            raise OrchestrationError("requires_repair 必须为布尔值")
        repair_ids = tuple(str(item).strip() for item in _seq(value.get("repair_node_ids"), "repair_node_ids"))
        if any(not key for key in repair_ids) or len(set(repair_ids)) != len(repair_ids):
            raise OrchestrationError("repair_node_ids 必须非空且不重复")
        dispatch = value.get("repair_dispatch", "in_round")
        if dispatch not in {"in_round", "after_decision"}:
            raise OrchestrationError("repair_dispatch 必须为 in_round 或 after_decision")
        if dispatch == "after_decision" and (not requires_repair or not repair_ids):
            raise OrchestrationError("判定后返修必须显式声明 requires_repair 和 repair_node_ids")
        items = value.get("items") or []
        if mode not in {"converge", "foreach"}:
            raise OrchestrationError("循环 mode 必须为 converge 或 foreach")
        if mode == "foreach":
            if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= MAX_FOREACH_ITEMS:
                raise OrchestrationError(f"foreach 必须冻结 1～{MAX_FOREACH_ITEMS} 个队列项")
            if any(not isinstance(item, dict) or not str(item.get("item_id") or "").strip()
                   or not str(item.get("goal") or "").strip() for item in items):
                raise OrchestrationError("队列项必须有 item_id 和 goal")
            if len({item["item_id"] for item in items}) != len(items):
                raise OrchestrationError("队列项 item_id 不能重复")
        elif items:
            raise OrchestrationError("队列 items 只能用于 foreach 模式")
        entry_id = str(value.get("entry_node_id") or "").strip()
        check_id = str(value.get("check_node_id") or "").strip()
        reviewers = [str(item).strip() for item in _seq(value.get("reviewer_node_ids"), "reviewer_node_ids")]
        reviewers = [item for item in reviewers if item] or [check_id]
        if check_id and check_id not in reviewers:
            reviewers.append(check_id)
        node_ids = [str(item).strip() for item in _seq(value.get("node_ids"), "node_ids")]
        # 入口/判定/审查节点按定义属于循环体；模型常只在 node_ids 列工作节点。
        for required in (entry_id, check_id, *reviewers):
            if required and required not in node_ids:
                node_ids.append(required)
        result = cls(
            loop_id=str(value.get("loop_id") or "").strip(),
            entry_node_id=entry_id,
            check_node_id=check_id,
            node_ids=tuple(dict.fromkeys(node_ids)),
            exit_condition=condition, criteria=criteria,
            reviewer_node_ids=tuple(dict.fromkeys(reviewers)),
            **parse_loop_budget(value),
            mode=mode, items=tuple(dict(item) for item in items), control_mode=control_mode,
            requires_repair=requires_repair, repair_node_ids=repair_ids, repair_dispatch=dispatch,
            required_node_ids=tuple(str(item).strip() for item in _seq(value.get("required_node_ids"), "required_node_ids")),
            parallel_groups=tuple(_seq(value.get("parallel_groups"), "parallel_groups")),
        )
        from mewcode.orchestration.parallel import parse_groups
        parse_groups(result.parallel_groups)
        if not all((result.loop_id, result.entry_node_id, result.check_node_id, condition)):
            raise OrchestrationError("循环必须声明编号、入口、判定节点和退出条件")
        ids = [str(item.get("criterion_id") or "").strip() for item in criteria]
        if not ids or len(ids) > 32 or len(ids) != len(set(ids)) or "" in ids:
            raise OrchestrationError("循环条件编号必须非空、唯一，最多 32 项")
        if any(not str(item.get("acceptance_test") or "").strip() for item in criteria):
            raise OrchestrationError("每个循环条件都必须定义验收方法")
        if (not set(result.required_node_ids).issubset(result.node_ids)
                or len(set(result.required_node_ids)) != len(result.required_node_ids)):
            raise OrchestrationError("退出前必需完成节点必须是循环内不重复的成员")
        return result

    def as_dict(self) -> dict:
        value = asdict(self)
        for key in ("node_ids", "criteria", "reviewer_node_ids", "items", "repair_node_ids", "required_node_ids"):
            value[key] = list(value[key])
        # Persisted old contracts already contain their original token budget.
        # Omit new defaults so their hash and spent-budget checkpoints survive.
        for key, default in (("node_max_iterations", 40), ("reviewer_max_iterations", 12)):
            if value[key] == default:
                value.pop(key)
        if self.mode == "converge":
            value.pop("mode")
            value.pop("items")
        if self.control_mode == "strict":
            value.pop("control_mode")  # Preserve legacy contract hashes.
        if not self.requires_repair:
            value.pop("requires_repair")
        if not self.repair_node_ids:
            value.pop("repair_node_ids")
        if not self.required_node_ids:
            value.pop("required_node_ids")
        if not self.parallel_groups:
            value.pop("parallel_groups")
        else:
            value['parallel_groups'] = list(self.parallel_groups)
        if self.repair_dispatch == "in_round":
            value.pop("repair_dispatch")  # Old contract/checkpoint hashes unchanged.
        return value

    @property
    def contract_hash(self) -> str:
        return fingerprint(self.as_dict())


def repair_workers(loop, node_map) -> list[str]:
    """Declared implementation capability, never a grant of command permission."""
    from mewcode.tools import expand_tool_aliases
    reviewers = set(loop.reviewer_node_ids)
    result = []
    for key in loop.repair_node_ids or loop.node_ids:
        node = node_map.get(key)
        if node is None or key not in loop.node_ids or key in reviewers:
            continue
        if getattr(node, "role", None) != "executor" and getattr(node, "kind", None) != "implement":
            continue
        allowed = getattr(node, "tools", None)
        allowed = {"WriteFile", "EditFile", "Bash"} if allowed is None else expand_tool_aliases(set(allowed))
        denied = expand_tool_aliases(set(getattr(node, "deny_tools", ()) or ()))
        if "*" not in denied and ({"WriteFile", "EditFile", "Bash"} - denied) & (
                {"WriteFile", "EditFile", "Bash"} if "*" in allowed else allowed):
            result.append(key)
    return result


def compile_loops(nodes: Any, edges: Any, raw_loops: Any = ()) -> tuple[list, list[LoopContract]]:
    """Keep forward edges and recover declared return edges as bounded regions."""
    node_map = {node.node_id: node for node in nodes}
    forward = [edge for edge in edges if not is_loop_edge(edge.condition)]
    loops = [LoopContract.from_value(item) for item in raw_loops or []]
    by_return = {(loop.check_node_id, loop.entry_node_id): loop for loop in loops}
    for loop in loops:
        if loop.repair_dispatch == "after_decision":
            by_return.update({(loop.check_node_id, node_id): loop for node_id in loop.repair_node_ids})
    outgoing = {key: [] for key in node_map}
    incoming = {key: [] for key in node_map}
    for edge in forward:
        if edge.from_node not in node_map or edge.to_node not in node_map:
            raise OrchestrationError("流程边引用未知节点")
        outgoing[edge.from_node].append(edge.to_node)
        incoming[edge.to_node].append(edge.from_node)

    def reach(start, adjacency):
        seen, pending = set(), [start]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            pending.extend(adjacency.get(current, []))
        return seen

    for edge in edges:
        if not is_loop_edge(edge.condition) or (edge.from_node, edge.to_node) in by_return:
            continue
        if edge.from_node not in node_map or edge.to_node not in node_map:
            raise OrchestrationError("循环边引用未知节点")
        body = reach(edge.to_node, outgoing) & reach(edge.from_node, incoming)
        check = node_map[edge.from_node]
        condition = getattr(check, "goal", "") or getattr(check, "responsibility", "") or check.label
        loop = LoopContract.from_value({
            "loop_id": f"loop-{edge.to_node}-{edge.from_node}", "entry_node_id": edge.to_node,
            "check_node_id": edge.from_node, "node_ids": [key for key in node_map if key in body],
            "exit_condition": condition,
            "control_mode": "team",
        })
        loops.append(loop)
        by_return[(edge.from_node, edge.to_node)] = loop
    ids = [loop.loop_id for loop in loops]
    if len(ids) != len(set(ids)):
        raise OrchestrationError("循环编号重复")
    for loop in loops:
        body = set(loop.node_ids)
        from mewcode.orchestration.parallel import parse_groups, validate_groups
        validate_groups(nodes, forward, parse_groups(loop.parallel_groups), body=body,
                        reviewers=loop.reviewer_node_ids)
        unknown = sorted(body - node_map.keys())
        if unknown:
            raise OrchestrationError(f"循环 {loop.loop_id} 的循环体引用了不存在的节点：{', '.join(unknown)}")
        workers = repair_workers(loop, node_map)
        if set(loop.repair_node_ids) - set(workers):
            raise OrchestrationError(f"循环 {loop.loop_id} 的返修目标必须是循环内有写入能力的实现节点，不能指向只读审查者")
        if loop.requires_repair and not workers:
            raise OrchestrationError(f"循环 {loop.loop_id} 要求返修，但没有可执行返修节点；请配置实现→审查闭环，不能用只读审查自循环")
        if (not {loop.entry_node_id, loop.check_node_id} <= body
                or not set(loop.reviewer_node_ids) <= body or loop.check_node_id not in loop.reviewer_node_ids):
            raise OrchestrationError(f"循环 {loop.loop_id} 的入口/判定/审查节点不在循环体内")
        reached = reach(loop.entry_node_id, outgoing)
        if loop.repair_dispatch == "after_decision":
            if loop.entry_node_id in loop.repair_node_ids or loop.check_node_id not in reached:
                raise OrchestrationError(f"循环 {loop.loop_id} 首次执行必须能独立到达判定，返修不能充当首次入口")
            for root in loop.repair_node_ids:
                if incoming[root]:
                    raise OrchestrationError(f"循环 {loop.loop_id} 判定后返修 {root} 只能由判定的 loop 调度，不能接普通 fail/always 入边")
                repair_path = reach(root, outgoing)
                if loop.entry_node_id in repair_path:
                    raise OrchestrationError(f"循环 {loop.loop_id} 返修路径不能重新进入首次入口")
                reached |= repair_path
        if not body <= reached or not body <= reach(loop.check_node_id, incoming):
            raise OrchestrationError(f"循环 {loop.loop_id} 含无法到达判定节点的路径")
        exits = []
        for edge in forward:
            if edge.from_node not in body and edge.to_node in body and edge.to_node != loop.entry_node_id:
                raise OrchestrationError(f"循环 {loop.loop_id} 只允许从入口进入")
            if edge.from_node in body and edge.to_node not in body:
                if edge.from_node != loop.check_node_id:
                    raise OrchestrationError(f"循环 {loop.loop_id} 只允许从判定节点退出")
                exits.append(edge)
        if not any(edge.condition in {"", "always", "pass", "true", "通过", "正确", "是"} for edge in exits):
            raise OrchestrationError(f"循环 {loop.loop_id} 没有 pass/always 退出路径")
    for index, left in enumerate(loops):
        for right in loops[index + 1:]:
            a, b = set(left.node_ids), set(right.node_ids)
            if a & b and not (a < b or b < a):
                raise OrchestrationError("循环区域交叉或重复；请改成明确的嵌套或串联循环")
            if left.entry_node_id == right.entry_node_id or left.check_node_id == right.check_node_id:
                raise OrchestrationError("嵌套循环必须各有独立入口与判定节点")
    return forward, loops


LOOP_PROMPT = """你在有界循环中协作。所有角色共同完成同一份冻结验收条件，不开展自由辩论。
只报告本轮新增证据、相对上一轮的变化、未解决条件 ID；不要复述全任务或历史计划。
异议必须对应条件 ID 和可核验的新证据；同一异议没有新证据时不得重复争辩。
不能更换验收标准、降低阈值、把未知记为通过，或以赞成票数代替证据。
执行者提交结果后停止；审查者使用 SubmitLoopDecision 提交固定条件向量。
CONTINUE 只表示仍需改进；EXIT 必须所有条件通过；缺证据或需改变目标时 BLOCKED。
看门狗停止后立即交接未解决项，不允许通过子任务、递归调用或新循环绕过预算。
foreach 模式只执行 current_item；criteria 验收当前项，max_iterations 是每项修订上限。
队列推进和队列全部完成由运行时负责，不把后续尚未执行的队列项判为本项失败。
""".strip()

TEAM_LOOP_PROMPT = """你是协作团队中的一个成员；编排图负责分工与交接，运行器负责调度和恢复。
按当前节点目标完成工作，只读取相关输入，保留已有成果。首轮没有返修单是正常情况，直接按任务目标执行。
历史交接中的预算耗尽、看门狗停止、不构成恢复授权等是当时状态；本次派发说明调度器已接续，
不要仅因这些旧文字停工。用户权限、禁止修改范围和验收要求仍然有效；不能自行扩大授权。
缺普通证据先查材料、补检索或交给对应成员修订。尚未完成必须如实交接，不能把回答结束记作任务完成。
审查者用 SubmitLoopDecision：全部条件有证据才 EXIT；可在团队内部补齐的缺口（含 UNKNOWN）用 CONTINUE，
可用 repair_node_ids 指定需要补做的成员；只有确实需要用户选择、授权或提供无法取得的材料才 BLOCKED 并写 question。
评分持平或暂时退步不是终止条件；报告新证据和具体下一步，不能重复空转或把未知记为通过。
foreach 仅处理 current_item；当前项通过后运行器自动推进队列，未执行的后续项不算本项失败。
Token、总时长和累计修订轮次仅作观测；单次模型/工具执行仍可中断、暂停或取消，由运行器安排接续。
""".strip()


class LoopWatchdog:
    def __init__(self, contract: LoopContract, saved: dict | None = None):
        self.contract = contract
        self.state = {"round": 0, "tokens": 0, "elapsed": 0.0, "stalled": 0,
                      "history": [], "scores": None, "status": "running",
                      "contract_hash": contract.contract_hash, **(saved or {})}
        if self.state["contract_hash"] != contract.contract_hash:
            raise OrchestrationError("循环条件已变化，不能复用旧循环的判断和预算")
        override = self.state.get("loop_resource_policy")
        if override is not None:
            if (not isinstance(override, dict)
                    or override.get("contract_hash") != contract.contract_hash
                    or override.get("authorized_by") != "user"
                    or not override.get("change_id")):
                raise OrchestrationError("循环 Token 授权覆盖无效，不能绕过冻结预算")
            if override.get("mode") == "adaptive":
                ceiling = override.get("limit")
                if (isinstance(ceiling, bool) or not isinstance(ceiling, int)
                        or not 1024 <= ceiling <= LOOP_BUDGET_LIMITS["token_budget"]["max"]):
                    raise OrchestrationError("循环 Token 自适应总额授权无效")
                from mewcode.orchestration.token_window import validate_token_window
                try:
                    validate_token_window(self.state.get("token_window"), ceiling=ceiling,
                                          spent_tokens=int(self.state["tokens"]))
                except (ValueError, TypeError) as exc:
                    raise OrchestrationError("循环 Token 窗口回执无效") from exc
                bucket = (self.state["token_window"] or {}).get("token_bucket") or {}
                if ((override.get("rate_per_minute") is None and bucket) or
                        (override.get("rate_per_minute") is not None and
                         (bucket.get("rate_per_minute") != override["rate_per_minute"] or
                          bucket.get("burst_tokens") != override.get("burst_tokens")))):
                    raise OrchestrationError("循环 Token 节流回执与授权不一致")
            elif override.get("limit", 0) is not None or override.get("mode") not in (None, "unlimited"):
                raise OrchestrationError("循环 Token 授权覆盖无效，不能绕过冻结预算")
        control = self.state.get("loop_control_policy")
        if control is not None and (
            not isinstance(control, dict) or control.get("mode") not in {"team", "strict"}
            or control.get("contract_hash") != contract.contract_hash
            or control.get("authorized_by") != "user" or not control.get("change_id")
        ):
            raise OrchestrationError("循环运行策略授权无效")

    @property
    def team_mode(self) -> bool:
        return (self.state.get("loop_control_policy") or {}).get("mode", self.contract.control_mode) == "team"

    @property
    def time_limit(self) -> float | None:
        return None if self.team_mode else self.contract.timeout_seconds

    @property
    def token_limit(self) -> int | None:
        # A run-local user authorization changes resource policy, not the
        # frozen acceptance contract or its historical evidence/hash.
        if self.team_mode:
            return None
        override = self.state.get("loop_resource_policy") or {}
        if override.get("mode") == "adaptive":
            return self.state["token_window"]["issued_until"]
        return None if override else self.contract.token_budget

    def settle_token_window(self, *, segment_id: str, durable_progress: bool,
                            thrash: bool = False, progress_items: int = 0,
                            remaining_items: int | None = None,
                            median_tokens_per_item: int | None = None) -> dict | None:
        if self.team_mode or (self.state.get("loop_resource_policy") or {}).get("mode") != "adaptive":
            return None
        from mewcode.orchestration.token_window import (
            MAX_WINDOW_RECEIPTS, advance_token_window,
        )
        updated, receipt = advance_token_window(
            self.state["token_window"], segment_id=segment_id,
            spent_tokens=int(self.state["tokens"]), durable_progress=durable_progress,
            thrash=thrash, progress_items=progress_items,
            remaining_items=remaining_items,
            median_tokens_per_item=median_tokens_per_item,
        )
        self.state["token_window"] = updated
        receipts = self.state.setdefault("token_window_receipts", [])
        if not receipts or receipts[-1].get("segment_id") != segment_id:
            receipts.append(receipt)
            del receipts[:-MAX_WINDOW_RECEIPTS]
        return receipt

    def pace_token_bucket(self, *, now: float | None = None) -> float:
        """Queue the next strict-mode slice until measured token debt refills."""
        if self.team_mode or (self.state.get("loop_resource_policy") or {}).get("mode") != "adaptive":
            return 0.0
        from mewcode.orchestration.token_window import token_bucket_delay
        updated, delay = token_bucket_delay(self.state["token_window"],
            spent_tokens=int(self.state["tokens"]), now=now,
            charge_unsettled=True)
        self.state["token_window"] = updated
        return delay

    def remaining(self) -> str:
        rounds = self.state.get("item_round", 0) if self.contract.mode == "foreach" else self.state["round"]
        resource_policy = self.state.get("loop_resource_policy") or {}
        if (not self.team_mode and resource_policy.get("mode") == "adaptive"
                and self.state["tokens"] > resource_policy["limit"]):
            return "Token 用量超过获批总额，须先对账"
        window_receipt = ((self.state.get("token_window") or {}).get("last_receipt") or {}
                          if not self.team_mode and
                          resource_policy.get("mode") == "adaptive"
                          else {})
        if window_receipt.get("action") == "replan":
            return "连续无进展，需重规划执行方式"
        if window_receipt.get("action") == "reconcile_overrun":
            return "Token 用量超过获批总额，须先对账"
        if not self.team_mode and rounds >= self.contract.max_iterations:
            return "达到循环轮次上限"
        if self.token_limit is not None and self.state["tokens"] >= self.token_limit:
            return "达到循环 Token 预算"
        if self.time_limit is not None and self.state["elapsed"] >= self.time_limit:
            return "达到循环时间上限"
        if self.state["stalled"] >= self.contract.max_stalled_rounds:
            return "多轮没有新增证据，请补充材料或说明下一步方向后继续" if self.team_mode else "连续无改进，停止重复循环"
        return ""

    @staticmethod
    def stop_kind(reason):
        if "用量超过获批总额" in reason:
            return "token_accounting_overrun"
        if "重规划执行方式" in reason:
            return "token_window_no_progress"
        if any(word in reason for word in ("预算", "轮次上限", "时间上限")):
            return "budget_exhausted"
        if any(word in reason for word in ("无改进", "振荡", "退化")):
            return "convergence_stalled"
        if any(word in reason for word in ("证据", "判定", "条件", "合同")):
            return "acceptance_blocked"
        return "node_failure"

    def observe(self, reports: list[dict], *, work_progress: dict | None = None) -> tuple[str, str]:
        expected = {item["criterion_id"] for item in self.contract.criteria}
        vectors = []
        for report in reports:
            if report.get("decision") not in {"EXIT", "CONTINUE", "BLOCKED"}:
                return "blocked", "无效的循环决策"
            if report.get("contract_hash") != self.contract.contract_hash:
                return "blocked", "模型更改或遗漏了循环合同"
            checks = report.get("checks") or []
            if len(checks) != len(expected) or {item.get("criterion_id") for item in checks} != expected:
                return "blocked", "模型未完整评价固定条件"
            if any(not item.get("evidence", "").strip() or not item.get("location", "").strip() for item in checks):
                return "blocked", "循环判定缺少证据"
            if any(item.get("state") not in {"PASS", "PARTIAL", "FAIL", "UNKNOWN"} for item in checks):
                return "blocked", "无效的条件状态"
            if report.get("decision") == "BLOCKED":
                return "blocked", str(report.get("question") or "仍有未知条件，需要补齐证据")
            if not self.team_mode and any(item["state"] == "UNKNOWN" for item in checks):
                return "blocked", "仍有未知条件，需要补齐证据"
            vector = {item["criterion_id"]: {"PASS": 0, "PARTIAL": 1, "FAIL": 2, "UNKNOWN": 3}[item["state"]] for item in checks}
            if (report.get("decision") == "EXIT") != all(score == 0 for score in vector.values()):
                return "blocked", "退出声明与条件证据矛盾"
            vectors.append(vector)
        if not vectors:
            return "blocked", "没有结构化循环判定"
        scores = {key: max(vector[key] for vector in vectors) for key in expected}
        disputes = sorted(key for key in expected if len({vector[key] for vector in vectors}) > 1)
        self.state["disputes"] = disputes
        old = self.state["scores"]
        self.state["scores"] = scores
        self.state["score"] = sum(scores.values())
        if all(value == 0 for value in scores.values()):
            return "completed", "所有审查者对固定条件提供通过证据"
        if self.team_mode:
            signature = fingerprint(work_progress) if work_progress is not None else fingerprint([
                sorted(report.get("checks") or [], key=lambda check: check["criterion_id"]) for report in reports])
            self.state['progress_signature'] = signature
            # New, concrete evidence can leave a coarse score unchanged. A
            # repeated evidence vector, not the score alone, detects no progress.
            # Batch work uses host counts instead: rephrasing a verdict or
            # rewriting the same record must not reset the convergence guard.
            seen = self.state.get("evidence_history") or []
            self.state["stalled"] = self.state["stalled"] + 1 if signature in seen else 0
            self.state["evidence_history"] = (seen + [signature])[-20:]
            reason = self.remaining()
            return ("blocked", reason) if reason else ("continue", "按具体缺口补齐证据或修订，保留不受影响的成果")
        if old is not None and any(scores[key] > old[key] for key in scores):
            return "blocked", "条件评价退化或审查意见反复，交接争议证据"
        improved = old is None or any(scores[key] < old[key] for key in scores)
        self.state["stalled"] = 0 if improved else self.state["stalled"] + 1
        signature = fingerprint(scores)
        if len(self.state["history"]) >= 2 and signature == self.state["history"][-2] != self.state["history"][-1]:
            return "blocked", "检测到条件状态来回振荡"
        self.state["history"] = (self.state["history"] + [signature])[-20:]
        reason = self.remaining()
        return ("blocked", reason) if reason else ("continue", "仅根据新增证据处理未通过条件")
