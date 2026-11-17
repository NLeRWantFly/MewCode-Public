"""Declared independent branches: settle every branch, then inspect all outputs."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath, PureWindowsPath
import math

from mewcode.orchestration.errors import OrchestrationError


@dataclass(frozen=True)
class ParallelGroup:
    group_id: str
    node_ids: tuple[str, ...]
    join_node_id: str
    min_success: int = 1
    max_concurrency: int = 3
    # Legacy serialized metadata only, retained for frozen contract hashes.
    # Execution uses the stage lifecycle; this is not a branch deadline.
    branch_timeout_seconds: float = 900
    write_scopes: dict | None = None

    @classmethod
    def from_value(cls, value):
        fields = cls.__dataclass_fields__
        if not isinstance(value, dict) or set(value) - fields.keys():
            raise OrchestrationError("并发组必须是包含已声明字段的对象")
        ids = value.get('node_ids')
        if (not isinstance(ids, (list, tuple)) or not 2 <= len(ids) <= 8
                or any(not isinstance(key, str) or not key.strip() for key in ids)
                or len(set(ids)) != len(ids)):
            raise OrchestrationError("并发组需要 2～8 个不重复节点")
        group_id, join = value.get('group_id'), value.get('join_node_id')
        if not isinstance(group_id, str) or not group_id.strip() or not isinstance(join, str) or not join.strip():
            raise OrchestrationError("并发组必须声明编号和汇合节点")
        minimum = value.get('min_success', 1)
        concurrency = value.get('max_concurrency', len(ids))
        timeout = value.get('branch_timeout_seconds', 900)
        if type(minimum) is not int or not 1 <= minimum <= len(ids):
            raise OrchestrationError("并发汇合最少有效结果数必须在 1～分支数之间")
        if type(concurrency) is not int or not 1 <= concurrency <= len(ids):
            raise OrchestrationError("并发数必须在 1～分支数之间")
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 1 <= timeout <= 7200:
            raise OrchestrationError("并发分支时限必须在 1～7200 秒之间")
        scopes = value.get('write_scopes')
        if scopes is not None:
            if (not isinstance(scopes, dict) or set(scopes) != set(ids)
                    or any(not isinstance(paths, list) or len(paths) > 512 for paths in scopes.values())):
                raise OrchestrationError("并发写入范围必须逐分支完整声明")
            scopes = {key: [str(_scope(path)) for path in paths] for key, paths in scopes.items()}
        return cls(group_id.strip(), tuple(ids), join.strip(), minimum, concurrency, float(timeout), scopes)

    def as_dict(self):
        value = asdict(self)
        value['node_ids'] = list(self.node_ids)
        if self.write_scopes is None:
            value.pop('write_scopes')
        return value


def parse_groups(raw):
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)) or len(raw) > 32:
        raise OrchestrationError("parallel_groups 必须是最多 32 项的数组")
    groups = tuple(ParallelGroup.from_value(item) for item in raw)
    if len({g.group_id for g in groups}) != len(groups):
        raise OrchestrationError("并发组编号不能重复")
    return groups


def flow_groups(flow):
    return tuple(group for loop in flow.meta.get('loops', [])
                 for group in parse_groups(loop.get('parallel_groups')))


def optional_failures(groups, results, accepted):
    """Failed alternatives remain failed; only an accepted fan-in resolves them."""
    resolved = set()
    for group in groups:
        if (not set(group.node_ids) <= results.keys() or not accepted(group.join_node_id)
                or sum(accepted(key) for key in group.node_ids) < group.min_success):
            continue
        resolved.update(key for key in group.node_ids if not accepted(key))
    return resolved


def _scope(value):
    if (not isinstance(value, str) or not value or '\\' in value or '\x00' in value
            or PureWindowsPath(value).drive or PurePosixPath(value).is_absolute()
            or any(part in {'.', '..', '.git', '.mewcode'} for part in value.split('/'))
            or any(char in value for char in '*?<>')):
        raise OrchestrationError("并发节点必须声明具体的项目内独立写入路径")
    return PurePosixPath(value.casefold())


def validate_groups(nodes, edges, groups, *, body=None, reviewers=()):
    node_map = {node.node_id: node for node in nodes}
    used, joins = set(), set()
    for group in groups:
        members = set(group.node_ids)
        if (not members <= node_map.keys() or group.join_node_id not in node_map
                or group.join_node_id in members or used & members or group.join_node_id in joins
                or body is not None and not (members | {group.join_node_id}) <= set(body)):
            raise OrchestrationError("并发组的分支及汇合必须是独立、不重复且同一区域内的节点")
        if members & set(reviewers) and group.min_success != len(members):
            raise OrchestrationError("必需循环审查者不能作为可忽略的并发分支")
        inputs = []
        paths = []
        for key in group.node_ids:
            node = node_map[key]
            if getattr(node, 'members', ()) or getattr(node, 'kind', '') in {'pipeline', 'gate'}:
                raise OrchestrationError("并发分支必须是独立角色节点")
            ins = {(edge.from_node, edge.condition) for edge in edges if edge.to_node == key}
            outs = [edge for edge in edges if edge.from_node == key]
            if (not ins or any(source in members for source, _ in ins)
                    or not outs or any(edge.to_node != group.join_node_id or edge.condition not in {'', 'always'} for edge in outs)):
                raise OrchestrationError("并发分支必须从共同输入分叉，并直接汇合，不能串行相互依赖")
            inputs.append(ins)
            capabilities = node.capabilities
            if (not isinstance(capabilities, dict) or capabilities.get('command_execution')
                    or capabilities.get('external_targets') or capabilities.get('network_hosts')):
                raise OrchestrationError("并发分支需声明只读或独立文件写入能力，不支持命令及外部副作用")
            declared = [_scope(path) for path in capabilities.get('write_paths', [])]
            own = ([_scope(path) for path in group.write_scopes[key]]
                   if group.write_scopes is not None else declared)
            if any(not any(path == base or path.is_relative_to(base) for base in declared) for path in own):
                raise OrchestrationError("并发写入范围不能扩大节点已有能力")
            if any(a == b or a.is_relative_to(b) or b.is_relative_to(a) for a in own for b in paths):
                raise OrchestrationError("并发分支的写入范围相互重叠")
            paths.extend(own)
        if any(item != inputs[0] for item in inputs[1:]):
            raise OrchestrationError("同组并发分支必须绑定相同的前置输入")
        used |= members
        joins.add(group.join_node_id)


def branch_policy(policy, node, group=None):
    """A concurrency contract narrows tools and paths; it never grants access."""
    paths = (group.write_scopes[node.node_id] if group and group.write_scopes is not None
             else (node.capabilities or {}).get('write_paths', []))
    root = Path(policy.work_dir or '.').resolve()
    resolved = [(root / path).resolve() for path in paths]
    if any(not path.is_relative_to(root) for path in resolved):
        raise OrchestrationError("并发节点写入路径解析到项目外")
    # Restrict actual tool writes as well as declarations, including task grants.
    return replace(policy, parallel_readonly=not bool(paths), parallel_isolated=bool(paths),
        allow_delegation=False, max_direct_agents=0, allow_recursive_delegation=False,
        expansion_write_paths=tuple(str(path) for path in resolved),
        deny_tools=tuple(sorted(set(policy.deny_tools) | {'Bash', 'Agent', 'TeamCreate', 'TeamDelete'})))


def usable(result):
    handoff = result.handoff or {}
    return (result.status == 'completed' and not result.error_kind
        and handoff.get('status') not in {'blocked', 'needs_attention'}
        and not handoff.get('blockers') and not handoff.get('question'))


def settlement(group, results):
    from mewcode.orchestration.loops import fingerprint
    from dataclasses import asdict
    rows = {key: {'status': results[key].status, 'usable': usable(results[key]),
        'error_kind': results[key].error_kind, 'result_hash': fingerprint(asdict(results[key]))}
        for key in group.node_ids if key in results}
    return {'group': group.as_dict(), 'branches': rows,
        'settled': len(rows) == len(group.node_ids),
        'usable_node_ids': [key for key, row in rows.items() if row['usable']]}


FUSION_PROMPT = """并发分支已全部完成或明确失败；必须综合所有有效成果，不能抢先取第一路或以多数票代替证据。
保留每一路的来源、版本、缺失和失败状态；按共同对象/页/区域对齐，选取各路有证据支持的最佳部分。
列出分歧、遗漏、低置信度和关键字段，附原始证据位置、候选内容、采纳依据及需返修的精确范围。
融合结果交独立审计模型核验；审计未通过只返修对应对象/区域，再审变更及其影响，复用有效未改成果。
分支失败不等于该分支已通过，也不能降低最终验收；缺少足够证据保持未知并交具体补证工作单。"""
