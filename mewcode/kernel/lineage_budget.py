"""Host-owned cross-version budget for a collaboration lineage.

Storage is deliberately not owned here: this module only reads and writes
``value["lineage_budget"]`` inside a collaboration ledger the host already
holds.  Callers run it inside the existing ``ExpertRunStore.mutate``
transaction; it never opens its own lock or file, and it never keeps a second
budget database.

Every public call validates its parameters and works on a private copy of the
budget subtree first.  The ledger is written at most once, at the very end, so
a failure never leaves a half-applied reservation or a freshly created budget
behind.  Returned dictionaries are deep copies; mutating them cannot change the
ledger.

It also never authenticates anyone.  :func:`grant` merely records a decision
the host has already verified through its own user entrance; this function does
not and cannot prove that the caller is a user.
"""
from __future__ import annotations

import copy

FORMAT_REVISION = 1
KINDS = ("attempts", "revisions", "expansions")
STATUSES = ("reserved", "settled", "released")

DEFAULT_ROOT_LIMITS = {"attempts": 600, "revisions": 120, "expansions": 40}
DEFAULT_LINEAGE_LIMITS = {"attempts": 9, "revisions": 6, "expansions": 6}

_RESERVATION_FIELDS = {"reservation_id", "kind", "amount", "lineage_id", "status", "actual"}
_RESERVATION_OPTIONAL = {"migrated"}
_GRANT_FIELDS = {"decision_id", "kind", "amount", "lineage_id"}
_BUDGET_FIELDS = {"format_revision", "limits", "root", "lineages", "reservations", "grants"}


class BudgetExhausted(ValueError):
    """The root or lineage allowance for one kind is already committed.

    ``kind``, ``scope``, ``lineage_id``, ``requested`` and ``available`` repeat
    the failing check so callers can react without parsing the message.
    """

    def __init__(self, message, *, kind=None, scope=None, lineage_id=None,
                 requested=None, available=None):
        super().__init__(message)
        self.kind = kind
        self.scope = scope
        self.lineage_id = lineage_id
        self.requested = requested
        self.available = available


def _text(item, label):
    if not isinstance(item, str) or not item:
        raise ValueError(f"{label}必须是非空文本。")
    return item


def _positive_int(amount, label):
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
        raise ValueError(f"{label}必须是正整数。")
    return amount


def _count(amount, label):
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        raise ValueError(f"{label}必须是非负整数。")
    return amount


def _kind(kind):
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError(f"未知预算类型：{kind!r}，仅支持 {', '.join(KINDS)}。")
    return kind


def _counters():
    return {kind: 0 for kind in KINDS}


def _validate_policy(value):
    policy = value.get("policy")
    if policy is not None and not isinstance(policy, dict):
        raise ValueError("policy 必须是字典。")
    mode = (policy or {}).get('lineage_budget_mode', 'fixed')
    if not isinstance(mode, str) or mode not in {'fixed', 'elastic'}:
        raise ValueError('lineage_budget_mode 必须是 fixed 或 elastic。')
    return policy


def elastic(value):
    """Host policy only: share root allowances without reseeding local history."""
    return (_validate_policy(value) or {}).get('lineage_budget_mode', 'fixed') == 'elastic'


def _limits_from_policy(value):
    """Default allowances, optionally overridden per scope through the policy."""
    limits = {"root": dict(DEFAULT_ROOT_LIMITS), "lineage": dict(DEFAULT_LINEAGE_LIMITS)}
    policy = value.get("policy")
    if policy is None:
        return limits
    override = policy.get("lineage_budget_limits")
    if override is None:
        return limits
    if not isinstance(override, dict) or set(override) - {"root", "lineage"}:
        raise ValueError("lineage_budget_limits 只能包含 root 与 lineage 两个范围。")
    for scope in ("root", "lineage"):
        supplied = override.get(scope)
        if supplied is None:
            continue
        if not isinstance(supplied, dict) or set(supplied) - set(KINDS):
            raise ValueError(f"{scope} 预算上限只能包含 {', '.join(KINDS)}。")
        for kind in KINDS:
            if kind in supplied:
                limits[scope][kind] = _positive_int(supplied[kind], f"{scope}.{kind} 上限")
    return limits


def _attempts(value):
    attempts = value.get("attempts")
    if attempts is None:
        return {}
    if not isinstance(attempts, dict):
        raise ValueError("attempts 必须是字典。")
    return attempts


def _contracts(value):
    contracts = value.get("contracts")
    if contracts is None:
        return {}
    if not isinstance(contracts, dict):
        raise ValueError("contracts 必须是字典。")
    return contracts


def _proposals(value):
    proposals = value.get("proposals")
    if proposals is None:
        return {}
    if not isinstance(proposals, dict):
        raise ValueError("proposals 必须是字典。")
    return proposals


def _text_list(item, label):
    """A list of unique non-empty strings; a string is rejected, not iterated."""
    if not isinstance(item, list):
        raise ValueError(f"{label} 必须是字符串列表。")
    texts = []
    for entry in item:
        text = _text(entry, label)
        if text in texts:
            raise ValueError(f"{label} 不得包含重复项。")
        texts.append(text)
    return texts


def _changed_lineages(value, proposal):
    """Derive affected lineages from the change targets named in the payload."""
    payload = proposal.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("提案 payload 必须是字典。")
    changes = payload.get("changes")
    if not isinstance(changes, dict):
        raise ValueError("提案 payload.changes 必须是字典。")
    targets = set(changes)
    affected = set()
    for contract_id, contract in _contracts(value).items():
        contract_id = _text(contract_id, "合同标识")
        if not isinstance(contract, dict):
            raise ValueError("合同记录必须是字典。")
        identifiers = set()
        for field in ("requirement_ids", "constraint_ids"):
            if field in contract:
                identifiers.update(_text_list(contract[field], f"合同 {contract_id} 的 {field}"))
        identifiers.add(_text(contract.get("node_id") or contract_id, "合同节点标识"))
        if identifiers & targets:
            lineage_id = contract.get("lineage_id")
            affected.add(contract_id if lineage_id is None
                         else _text(lineage_id, f"合同 {contract_id} 的谱系"))
    return sorted(affected)


def _affected_lineages(value, proposal):
    """Prefer an explicit list; a missing key falls back to change targets."""
    if "affected_lineages" in proposal:
        return _text_list(proposal["affected_lineages"], "提案 affected_lineages")
    return _changed_lineages(value, proposal)


def lineage_of(value, attempt):
    """Resolve one attempt's lineage without trusting spec or retry counters."""
    if not isinstance(value, dict):
        raise ValueError("协作账本必须是字典。")
    if not isinstance(attempt, dict):
        raise ValueError("历史尝试记录必须是字典。")
    contract_id = attempt.get("contract_id")
    contract = None
    if isinstance(contract_id, str):
        contract = _contracts(value).get(contract_id)
        if contract is not None and not isinstance(contract, dict):
            raise ValueError("合同记录必须是字典。")
    for candidate in (attempt.get("lineage_id"), (contract or {}).get("lineage_id"), contract_id):
        if candidate is None:
            continue
        return _text(candidate, "尝试谱系")
    raise ValueError("历史尝试缺少可确定的谱系。")


def _lineage(budget, lineage_id, create=False):
    entry = budget["lineages"].get(lineage_id)
    if entry is None and create:
        entry = {"used": _counters(), "reserved": _counters()}
        budget["lineages"][lineage_id] = entry
    return entry


def _granted(budget, kind, lineage_id):
    return sum(row["amount"] for row in budget["grants"].values()
               if row["kind"] == kind and row["lineage_id"] == lineage_id)


def _root_limit(budget, kind):
    return budget["limits"]["root"][kind] + _granted(budget, kind, None)


def _lineage_limit(budget, lineage_id, kind):
    return budget["limits"]["lineage"][kind] + _granted(budget, kind, lineage_id)


def _migrate(value):
    """Seed a fresh budget, counting historical attempts and adopted revisions."""
    budget = {
        "format_revision": FORMAT_REVISION,
        "limits": _limits_from_policy(value),
        "root": {"used": _counters(), "reserved": _counters()},
        "lineages": {},
        "reservations": {},
        "grants": {},
    }
    for attempt_id, attempt in _attempts(value).items():
        lineage_id = lineage_of(value, attempt)
        lineage = _lineage(budget, lineage_id, create=True)
        budget["root"]["used"]["attempts"] += 1
        lineage["used"]["attempts"] += 1
        reservation_id = f"attempt:{attempt_id}"
        budget["reservations"][reservation_id] = {
            "reservation_id": reservation_id, "kind": "attempts", "amount": 1,
            "lineage_id": lineage_id, "status": "settled", "actual": 1, "migrated": True,
        }
    for proposal_key, proposal in _proposals(value).items():
        if not isinstance(proposal, dict):
            raise ValueError("提案记录必须是字典。")
        if proposal.get("status") != "adopted":
            continue
        proposal_id = _text(proposal_key, "提案标识")
        for lineage_id in _affected_lineages(value, proposal):
            lineage = _lineage(budget, lineage_id, create=True)
            budget["root"]["used"]["revisions"] += 1
            lineage["used"]["revisions"] += 1
            reservation_id = f"revision:{proposal_id}:{lineage_id}"
            budget["reservations"][reservation_id] = {
                "reservation_id": reservation_id, "kind": "revisions", "amount": 1,
                "lineage_id": lineage_id, "status": "settled", "actual": 1, "migrated": True,
            }
    return budget


def _validate_limits(limits):
    if not isinstance(limits, dict) or set(limits) != {"root", "lineage"}:
        raise ValueError("lineage_budget.limits 必须恰好包含 root 与 lineage。")
    for scope in ("root", "lineage"):
        table = limits[scope]
        if not isinstance(table, dict) or set(table) != set(KINDS):
            raise ValueError(f"lineage_budget.limits.{scope} 必须恰好包含 {', '.join(KINDS)}。")
        for kind in KINDS:
            _positive_int(table[kind], f"{scope}.{kind} 上限")


def _validate_counters(table, label):
    if not isinstance(table, dict) or set(table) != {"used", "reserved"}:
        raise ValueError(f"{label} 必须恰好包含 used 与 reserved。")
    for field in ("used", "reserved"):
        counters = table[field]
        if not isinstance(counters, dict) or set(counters) != set(KINDS):
            raise ValueError(f"{label}.{field} 必须恰好包含 {', '.join(KINDS)}。")
        for kind in KINDS:
            _count(counters[kind], f"{label}.{field}.{kind}")


def _validate_budget(budget):
    """Reject corrupt or unknown stored budgets without repairing them."""
    if not isinstance(budget, dict) or set(budget) != _BUDGET_FIELDS:
        raise ValueError("lineage_budget 结构无效：字段不完整或含未知字段。")
    revision = budget["format_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision != FORMAT_REVISION:
        raise ValueError(f"lineage_budget 版本不受支持：{revision!r}，仅支持 {FORMAT_REVISION}。")
    _validate_limits(budget["limits"])
    _validate_counters(budget["root"], "lineage_budget.root")

    lineages = budget["lineages"]
    if not isinstance(lineages, dict):
        raise ValueError("lineage_budget.lineages 必须是字典。")
    for lineage_id, entry in lineages.items():
        _text(lineage_id, "谱系标识")
        _validate_counters(entry, f"谱系 {lineage_id}")

    reservations = budget["reservations"]
    if not isinstance(reservations, dict):
        raise ValueError("lineage_budget.reservations 必须是字典。")
    root_used, root_reserved = _counters(), _counters()
    lineage_used, lineage_reserved = {}, {}
    for reservation_id, record in reservations.items():
        _text(reservation_id, "预留标识")
        if (not isinstance(record, dict) or set(record) - _RESERVATION_FIELDS - _RESERVATION_OPTIONAL
                or not _RESERVATION_FIELDS <= set(record)):
            raise ValueError(f"预留 {reservation_id} 结构无效。")
        if record["reservation_id"] != reservation_id:
            raise ValueError(f"预留 {reservation_id} 的标识与键不一致。")
        kind = _kind(record["kind"])
        amount = _positive_int(record["amount"], f"预留 {reservation_id} 数量")
        lineage_id = _text(record["lineage_id"], f"预留 {reservation_id} 谱系")
        if lineage_id not in lineages:
            raise ValueError(f"预留 {reservation_id} 引用了未知谱系 {lineage_id}。")
        status = record["status"]
        if status not in STATUSES:
            raise ValueError(f"预留 {reservation_id} 状态无效：{status!r}。")
        actual = record["actual"]
        if status == "settled":
            if isinstance(actual, bool) or not isinstance(actual, int) or not 0 <= actual <= amount:
                raise ValueError(f"预留 {reservation_id} 的结算数量无效。")
        elif actual is not None:
            raise ValueError(f"预留 {reservation_id} 尚未结算，不应带有结算数量。")
        if "migrated" in record and not isinstance(record["migrated"], bool):
            raise ValueError(f"预留 {reservation_id} 的迁移标记无效。")
        if status == "reserved":
            root_reserved[kind] += amount
            lineage_reserved.setdefault(lineage_id, _counters())[kind] += amount
        elif status == "settled":
            root_used[kind] += actual
            lineage_used.setdefault(lineage_id, _counters())[kind] += actual

    for kind in KINDS:
        if budget["root"]["used"][kind] != root_used[kind]:
            raise ValueError(f"根 {kind} 已用计数与预留明细不一致。")
        if budget["root"]["reserved"][kind] != root_reserved[kind]:
            raise ValueError(f"根 {kind} 预留计数与预留明细不一致。")
    for lineage_id, entry in lineages.items():
        used = lineage_used.get(lineage_id, _counters())
        reserved = lineage_reserved.get(lineage_id, _counters())
        for kind in KINDS:
            if entry["used"][kind] != used[kind]:
                raise ValueError(f"谱系 {lineage_id} 的 {kind} 已用计数与预留明细不一致。")
            if entry["reserved"][kind] != reserved[kind]:
                raise ValueError(f"谱系 {lineage_id} 的 {kind} 预留计数与预留明细不一致。")

    grants = budget["grants"]
    if not isinstance(grants, dict):
        raise ValueError("lineage_budget.grants 必须是字典。")
    for decision_id, record in grants.items():
        _text(decision_id, "决定标识")
        if not isinstance(record, dict) or set(record) != _GRANT_FIELDS:
            raise ValueError(f"授予 {decision_id} 结构无效。")
        if record["decision_id"] != decision_id:
            raise ValueError(f"授予 {decision_id} 的标识与键不一致。")
        _kind(record["kind"])
        _positive_int(record["amount"], f"授予 {decision_id} 数量")
        if record["lineage_id"] is not None:
            _text(record["lineage_id"], f"授予 {decision_id} 谱系")


def _load(value):
    """Return (private copy of budget, created) and never touch ``value``."""
    if not isinstance(value, dict):
        raise ValueError("协作账本必须是字典。")
    _validate_policy(value)
    budget = value.get("lineage_budget")
    if budget is None:
        fresh = _migrate(value)
        _validate_budget(fresh)
        return fresh, True
    if not isinstance(budget, dict):
        raise ValueError("lineage_budget 结构无效。")
    candidate = copy.deepcopy(budget)
    _validate_budget(candidate)
    return candidate, False


def _commit(value, budget):
    value["lineage_budget"] = budget


def ensure(value):
    """Return a copy of the ledger budget, initializing it at most once."""
    budget, created = _load(value)
    if created:
        _commit(value, budget)
    return copy.deepcopy(budget)


def reserve(value, reservation_id, *, lineage_id, kind, amount=1):
    """Commit budget in both the root and the named lineage before dispatch."""
    reservation_id = _text(reservation_id, "预留标识")
    lineage_id = _text(lineage_id, "谱系标识")
    kind = _kind(kind)
    amount = _positive_int(amount, "预留数量")
    budget, _created = _load(value)
    existing = budget["reservations"].get(reservation_id)
    if existing is not None:
        if existing["status"] == "released":
            raise ValueError(f"预留 {reservation_id} 已释放，不能重新预留。")
        if (existing["kind"] != kind or existing["amount"] != amount
                or existing["lineage_id"] != lineage_id):
            raise ValueError(f"预留标识 {reservation_id} 已被另一内容使用。")
        if _created:
            _commit(value, budget)
        return copy.deepcopy(existing)
    root = budget["root"]
    limit = _root_limit(budget, kind)
    committed = root["used"][kind] + root["reserved"][kind]
    if committed + amount > limit:
        raise BudgetExhausted(
            f"根预算不足：{kind} 上限 {limit}，已用 {root['used'][kind]}，"
            f"已预留 {root['reserved'][kind]}，本次请求 {amount}。",
            kind=kind, scope="root", lineage_id=lineage_id,
            requested=amount, available=max(0, limit - committed))
    lineage = budget["lineages"].get(lineage_id)
    used = lineage["used"][kind] if lineage else 0
    reserved = lineage["reserved"][kind] if lineage else 0
    limit = _lineage_limit(budget, lineage_id, kind)
    if not elastic(value) and used + reserved + amount > limit:
        raise BudgetExhausted(
            f"谱系 {lineage_id} 预算不足：{kind} 上限 {limit}，已用 {used}，"
            f"已预留 {reserved}，本次请求 {amount}。",
            kind=kind, scope="lineage", lineage_id=lineage_id,
            requested=amount, available=max(0, limit - used - reserved))
    if lineage is None:
        lineage = _lineage(budget, lineage_id, create=True)
    root["reserved"][kind] += amount
    lineage["reserved"][kind] += amount
    record = {"reservation_id": reservation_id, "kind": kind, "amount": amount,
              "lineage_id": lineage_id, "status": "reserved", "actual": None}
    budget["reservations"][reservation_id] = record
    _commit(value, budget)
    return copy.deepcopy(record)


def settle(value, reservation_id, actual=None):
    """Move a reservation from reserved to used; ``actual`` may refund the rest."""
    reservation_id = _text(reservation_id, "预留标识")
    if actual is not None and (isinstance(actual, bool) or not isinstance(actual, int)):
        raise ValueError("结算数量必须是整数。")
    budget, _created = _load(value)
    record = budget["reservations"].get(reservation_id)
    if record is None:
        raise ValueError(f"未知预留：{reservation_id}。")
    if record["status"] == "released":
        raise ValueError(f"预留 {reservation_id} 已释放，不能结算。")
    amount = record["amount"]
    requested = amount if actual is None else actual
    if requested < 0 or requested > amount:
        raise ValueError(f"结算数量必须在 0 到预留数量 {amount} 之间。")
    if record["status"] == "settled":
        if requested != record["actual"]:
            raise ValueError(f"重复结算不能改变 {reservation_id} 的数量。")
        if _created:
            _commit(value, budget)
        return copy.deepcopy(record)
    kind = record["kind"]
    lineage = _lineage(budget, record["lineage_id"], create=True)
    budget["root"]["reserved"][kind] -= amount
    budget["root"]["used"][kind] += requested
    lineage["reserved"][kind] -= amount
    lineage["used"][kind] += requested
    record["status"] = "settled"
    record["actual"] = requested
    _commit(value, budget)
    return copy.deepcopy(record)


def release(value, reservation_id):
    """Return a not-yet-executed reservation without charging used budget."""
    reservation_id = _text(reservation_id, "预留标识")
    budget, _created = _load(value)
    record = budget["reservations"].get(reservation_id)
    if record is None:
        raise ValueError(f"未知预留：{reservation_id}。")
    if record["status"] == "released":
        return copy.deepcopy(record)
    if record["status"] == "settled":
        raise ValueError(f"预留 {reservation_id} 已结算，不能释放。")
    kind = record["kind"]
    lineage = _lineage(budget, record["lineage_id"], create=True)
    budget["root"]["reserved"][kind] -= record["amount"]
    lineage["reserved"][kind] -= record["amount"]
    record["status"] = "released"
    _commit(value, budget)
    return copy.deepcopy(record)


def grant(value, decision_id, *, kind, amount, lineage_id=None):
    """Record a host-verified decision; it raises a limit, never lowers usage.

    ``lineage_id=None`` raises the root allowance.  A named lineage only gains
    an independent increment for itself; the default for every other lineage is
    unchanged.  This function does not verify the user identity itself.
    """
    decision_id = _text(decision_id, "决定标识")
    kind = _kind(kind)
    amount = _positive_int(amount, "授予数量")
    if lineage_id is not None:
        lineage_id = _text(lineage_id, "谱系标识")
    budget, _created = _load(value)
    existing = budget["grants"].get(decision_id)
    if existing is not None:
        if (existing["kind"] != kind or existing["amount"] != amount
                or existing["lineage_id"] != lineage_id):
            raise ValueError(f"决定标识 {decision_id} 已被另一内容使用。")
        return copy.deepcopy(existing)
    record = {"decision_id": decision_id, "kind": kind, "amount": amount, "lineage_id": lineage_id}
    budget["grants"][decision_id] = record
    if lineage_id is not None:
        _lineage(budget, lineage_id, create=True)
    _commit(value, budget)
    return copy.deepcopy(record)
