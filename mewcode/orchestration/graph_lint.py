"""Deterministic checks for orchestration shapes that cannot recover at runtime."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from mewcode.orchestration.spec import OrchestrationSpec


@dataclass(frozen=True)
class LintFinding:
    code: str
    severity: Literal["error", "warning"]
    path: str
    entry_ids: tuple[str, ...]
    message: str


_FULL_SCAN = re.compile(
    r"逐页|每一页|每页|所有页|全部页|逐项|全量|全面|整本|每份|每个文件|"
    r"所有文件|全部文件|所有材料|全部材料|所有文档|全部文档|"
    r"所有内容|全部内容|所有数据|全部数据",
    re.I,
)
_FULL_SCAN_ACTION = re.compile(
    r"读取|阅读|查看|识别|处理|制作|提取|解析|审查|审核|复核|核验|检查|"
    r"校验|标注|生成|采集|下载|转换|写入|分类|验收|扫描|浏览",
    re.I,
)
_FULL_SCAN_REVIEW_ACTION = re.compile(
    r"审查|审核|复核|核验|检查|校验|核对",
    re.I,
)
_FULL_SCAN_DELEGATION = re.compile(
    r"(?:^|[\n。；;，,]\s*|交给|由|让|委托|安排).{0,16}(?:执行节点|下游(?:节点)?|子阶段|循环|批量(?:处理|任务)?)",
    re.I,
)
_FULL_SCAN_OBJECT = re.compile(
    r"文件|页面|材料|文档|内容|数据|图像|图片|报告|产物|输入|记录",
    re.I,
)
_FULL_SCAN_NEGATION = re.compile(
    r"(?:本节点|该节点|计划节点)?(?:不|未|无|无需|不必|不得|禁止|避免|不允许|不可|切勿|不会|暂不|不再|尚未)"
    r"(?:开展|执行|负责|进入|进行|做|处理|包含|读取|阅读|查看|识别|制作|提取|解析|审查|审核|"
    r"复核|核验|检查|校验|标注|生成|采集|下载|转换|写入|分类|验收|扫描|浏览)?$",
    re.I,
)
_FULL_SCAN_METHOD = re.compile(
    r"(?:制定|定义|设计|编写|形成|完善|说明|描述).{0,10}"
    r"(?:方法|方案|标准|流程|规则|清单|列表)|"
    r"(?:逐页|每一页|每页|逐项|全量|全面|整本).{0,12}"
    r"(?:方法|方案|标准|流程|规则|清单|列表)",
    re.I,
)
_FULL_SCAN_TRIAL_REVIEW = re.compile(
    r"试点|样本|抽样|pilot|"
    r"(?:试点|样本|抽样|pilot).{0,18}(?:证据|结果|问题|记录)|"
    r"(?:证据|结果|问题|记录).{0,18}(?:试点|样本|抽样|pilot)",
    re.I,
)
_FULL_SCAN_GAP_REVIEW = re.compile(
    r"(?:缺口|未关闭问题|未解决项).{0,12}"
    r"(?:列出|列表|清单|汇总|记录|复核|检查|核对)",
    re.I,
)
_FULL_SCAN_BOUNDED_SAMPLE = re.compile(
    r"(?:\d+\s*(?:个|份|页|件|项|条)?|所选|选定).{0,8}"
    r"(?:样本|代表(?:页|样本)?|页面|文件)",
    re.I,
)
_FULL_SCAN_BROAD_MARKER = re.compile(
    r"全量|全面|整本|每份|每个文件|所有|全部|所有文件|全部文件|"
    r"所有材料|全部材料|所有文档|全部文档",
    re.I,
)
_FULL_SCAN_CLAUSE_SPLIT = re.compile(
    r"[。；;，,\n]|然后|之后|接着|再|并且|并|但是|但|不过|而且|而",
    re.I,
)
_UNBOUNDED_ACCEPTANCE = re.compile(r"全部|所有|完整")
_BOUNDED_ACCEPTANCE = re.compile(r"\d+|清单|列表|范围|对照|核对")
_PLACEHOLDER = re.compile(r"待提供|待确认|待补充|TBD|TODO", re.I)
_MULTILINE_NEED = re.compile(r"\n")
_ENV_NEED = re.compile(r"(?:\bpython(?:3)?\b|\bpip\b|\bvenv\b|依赖|运行测试|安装库)", re.I)
_ENV_PREP = re.compile(r"(?:PreparePythonEnvironment|RuntimeDoctor|准备.{0,8}环境|环境准备|安装.{0,8}依赖)", re.I)
_PROJECT_VENV = re.compile(r"(?:项目|使用|采用|激活|根目录).{0,8}\.venv|\.venv", re.I)
_PROBE = re.compile(r"(?:探测|检查.{0,8}环境|验证.{0,8}环境|检查版本|probe|inspect environment|verify environment)", re.I)
_WRITE = re.compile(r"(?:写入|写文件|创建文件|修改文件|保存文件|安装|删除|WriteFile|EditFile|\binstall\b|\bwrite file\b)", re.I)
_COMMAND = re.compile(r"(?:\bBash\b|\bPowerShell\b|执行命令|运行命令|命令行|shell command)", re.I)
_WRITE_TOOL = re.compile(r"(?:WriteFile|EditFile|Bash|PowerShell|Write|Edit|CreateFile)", re.I)
_SEPARATED = re.compile(r"(?:分步执行|分阶段执行|分开执行|分别执行|独立步骤|拆分步骤)", re.I)
_USER_ASK = re.compile(r"(?:询问用户|请用户.{0,8}(?:确认|决定|选择|提供)|向用户提问|等待用户选择|AskUserQuestion|ask the user)", re.I)
_USER_ASK_NEGATION = re.compile(r"(?:不要|无需|不必|不得|禁止|避免|不允许|不可|切勿|do not|don't)[^。；，,]{0,16}$", re.I)
_DIRECT_OPERATION_NEGATION = re.compile(r"(?:不|未)(?:实际|主动|直接|自动)?$")
_MULTIBATCH = re.compile(r"(?:多批次|每批|分批|逐批|批次|multiple batches|each batch)", re.I)
_OUTPUT_METHOD = re.compile(r"(?:SubmitHandoff|inline_outputs|产物文件|写入路径|输出到|保存到|提交.{0,8}产物|台账|工作项记录)", re.I)
_PIXEL_REVIEW = re.compile(r'图像|截图|版式|页面|PDF\s*渲染|图|vision|visual|pixel', re.I)
_COMMAND_REVIEW = re.compile(r'测试|命令|退出码|RunVerification|VerifyCandidate', re.I)
_PIXEL_LABEL = re.compile(r'视觉|图像|看图|vision|visual|pixel', re.I)
_TEXT_LABEL = re.compile(r'文本|文字|内容|text', re.I)
_OBSERVE_PIXELS = re.compile(r'观察.{0,8}(?:冻结像素|页面图像)|(?:查看|识别|检查|核验).{0,8}(?:像素|渲染图)|InspectVisualEvidence', re.I)
_AUTH_OPERATIONS = {
    'external_send': re.compile(
        r'(?:内容|图像|页面|页|文件|数据|材料|证据|结果)?外发|'
        r'外发.{0,16}(?:内容|图像|页面|页|文件|数据|材料|服务|模型|范围|授权)|'
        r'(?:发送|提交|交给|传给|调用|使用).{0,16}'
        r'(?:外部模型|外部服务|第三方(?:模型|服务)|远程模型|云模型)|'
        r'(?:授权|确认).{0,12}(?:外发|外部模型|外部服务)',
        re.I,
    ),
    'network': re.compile(r'联网|访问外部网站|连接外部服务', re.I),
    'write_outside_workspace': re.compile(r'写入工作区(?:之)?外|工作区(?:之)?外写入', re.I),
}


def _dominators(parents, required_predecessors=None):
    """Include AND dependencies bound by the host for this exact runtime graph."""
    required = {node: set((required_predecessors or {}).get(node, ())) & incoming
                for node, incoming in parents.items()}
    values = {node: ({node} if not incoming else set(parents)) for node, incoming in parents.items()}
    for _ in range(len(parents) + 1):
        changed = False
        for node, incoming in parents.items():
            new = {node} | (set.intersection(*(values[p] for p in incoming)) if incoming else set())
            for parent in required[node]:
                new |= values[parent]
            if new != values[node]:
                values[node], changed = new, True
        if not changed:
            break
    return values
_READ_ONLY_NAMES = {
    "ReadFile", "ReadAuditArtifact", "ReadStageResult", "ReadReviewQueue", "ReadWorkQueue",
    "ReviewWorkQueue", "Glob", "Grep", "SearchFiles", "ListDirectory", "InspectGitState",
    "WebSearch", "ReadOnlySearch", "ReadProjectState", "RuntimeDoctor",
}


def _get(value, name, default=None):
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _list(value):
    if value is None or isinstance(value, (str, bytes)):
        return []
    try:
        return list(value)
    except TypeError:
        return []


def _text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item) for item in value if item is not None)
    return str(value)


def _node_id(node):
    return str(_get(node, "node_id", "") or "")


def _brief(node):
    return _get(node, "plan_brief") or {}


def _brief_text(node, field):
    return _text(_get(_brief(node), field, ()))


def _path_norm(value):
    return re.sub(r"[\W_]+", "", str(value or "").casefold())


def _contract_items(spec, run_inputs_contract):
    if run_inputs_contract is None:
        contract = _get(_get(spec, "goal_contract"), "run_inputs")
        if contract is None:
            contract = _get(_get(spec, "meta", {}), "run_input_contract", ())
    else:
        contract = run_inputs_contract
    if isinstance(contract, Mapping):
        if isinstance(contract.get("run_inputs"), (list, tuple)):
            contract = contract["run_inputs"]
        elif "name" in contract:
            contract = [contract]
        else:
            contract = [dict(value, name=key) for key, value in contract.items()
                        if isinstance(value, Mapping)]
    return [item for item in _list(contract) if isinstance(item, Mapping)]


def _has_work_item_source(loop):
    return any(bool(loop.get(key)) for key in (
        "items", "work_items", "item_source", "items_source", "work_item_source", "queue_source"
    ))


def _is_loop_edge(edge):
    return str(_get(edge, "condition", "") or "").strip().casefold() in {
        "loop", "repeat", "continue", "loop:continue", "循环", "继续循环", "回边", "重试",
    }


def _graph_links(spec):
    node_ids = {_node_id(node) for node in _list(_get(spec, "nodes"))}
    parents = {node_id: set() for node_id in node_ids}
    children = {node_id: set() for node_id in node_ids}
    for edge in _list(_get(spec, "edges")):
        if _is_loop_edge(edge):
            continue
        source = str(_get(edge, "from_node", "") or "")
        target = str(_get(edge, "to_node", "") or "")
        if source in node_ids and target in node_ids:
            parents[target].add(source)
            children[source].add(target)
    return parents, children


def _ancestors(node_id, parents):
    seen, pending = set(), list(parents.get(node_id, ()))
    while pending:
        parent = pending.pop()
        if parent in seen:
            continue
        seen.add(parent)
        pending.extend(parents.get(parent, ()))
    return seen


def _positive_match(pattern, text):
    for match in pattern.finditer(text):
        prefix = text[max(0, match.start() - 12):match.start()]
        # Bare 不/未 must attach to this operation, not negate another action
        # earlier in the sentence ("不改原件，联网下载" still needs authority).
        if not (_USER_ASK_NEGATION.search(prefix) or _DIRECT_OPERATION_NEGATION.search(prefix)):
            return True
    return False


def _full_scan_clauses(text):
    """Yield short clauses so a local sample qualifier cannot mask later work."""
    return [part.strip() for part in _FULL_SCAN_CLAUSE_SPLIT.split(_text(text)) if part.strip()]


def _has_positive_full_scan(text):
    """Return whether a clause asks a plan node to do real full-scope work.

    Full-scope words also occur in evidence summaries, gap lists and method
    descriptions.  Those are not execution work, so the check requires a
    nearby business action and evaluates each clause independently.
    """
    for clause in _full_scan_clauses(text):
        matches = list(_FULL_SCAN.finditer(clause))
        if not matches:
            continue
        for index, marker in enumerate(matches):
            # A phrase such as "逐页读取全部文件" contains two scope
            # markers.  The later broad marker belongs to the same action;
            # evaluating it independently would lose a negation or handoff
            # attached to the first marker.
            if any(_FULL_SCAN_ACTION.fullmatch(clause[previous.end():marker.start()].strip() or "")
                   for previous in matches[:index]):
                continue
            window_start = max(0, marker.start() - 32)
            window_end = min(len(clause), marker.end() + 32)
            # Prefer the action following the scope marker.  An unrelated
            # review verb earlier in the clause must not mask a later
            # full-scope read/scan action.
            after = clause[marker.end():window_end]
            after_matches = list(_FULL_SCAN_ACTION.finditer(after))
            before = clause[window_start:marker.start()]
            before_matches = list(_FULL_SCAN_ACTION.finditer(before))
            # When the action is directly attached to the marker ("读取所有
            # 页"), it owns the full-scope request.  A temporal connector
            # such as "复核后逐页读取" makes the following action the one to
            # classify instead.
            before_action = before_matches[-1] if before_matches else None
            between = clause[window_start + before_action.end():marker.start()] if before_action else ""
            if before_action and not re.search(r"后|之后|随后|再|并|且|然后|接着|而", between):
                action = before_action
                action_from_after = False
            else:
                action = after_matches[0] if after_matches else None
                action_from_after = action is not None
            if action is None:
                continue

            # A negative verb immediately before the scope marker negates this
            # occurrence; a later independent clause is still evaluated.
            prefix = clause[max(0, marker.start() - 32):marker.start()]
            if _FULL_SCAN_NEGATION.search(prefix):
                continue

            # A plan can describe a full-scope action that belongs to a
            # downstream executor, child stage, or loop.  Only delegation
            # before this scope marker transfers ownership; a later handoff
            # does not excuse work the plan node already performs.
            if action_from_after:
                action_start = marker.end() + action.start()
            else:
                action_start = window_start + action.start()
            if _FULL_SCAN_DELEGATION.search(clause[:action_start]):
                continue

            # "逐项复核试点证据/未关闭问题" and similar review summaries
            # describe evidence, not the full business corpus.  A strong
            # scope marker or a concrete file/page object keeps a real action
            # blocking even when the same clause mentions a pilot.
            scope_removed = clause[:marker.start()] + clause[marker.end():]
            has_object = bool(_FULL_SCAN_OBJECT.search(scope_removed))
            review_action = bool(_FULL_SCAN_REVIEW_ACTION.search(action.group(0)))
            bounded_sample = bool(_FULL_SCAN_BOUNDED_SAMPLE.search(scope_removed))
            if ((_FULL_SCAN_TRIAL_REVIEW.search(clause)
                 or _FULL_SCAN_GAP_REVIEW.search(clause))
                    and review_action and not has_object
                    and not _FULL_SCAN_BROAD_MARKER.search(clause)):
                continue
            if (_FULL_SCAN_TRIAL_REVIEW.search(clause) and bounded_sample
                    and not _FULL_SCAN_BROAD_MARKER.search(clause)):
                continue

            # Method and checklist formulation is planning metadata.  When a
            # concrete business object is named, the actual action remains a
            # full-scan request and is not exempted.
            if _FULL_SCAN_METHOD.search(clause) and not has_object:
                continue
            return True
    return False


def _is_read_only(name):
    return name in _READ_ONLY_NAMES or name.startswith(("Read", "Search", "List", "Get", "Find", "Inspect", "Query", "Fetch"))


def _effective_tools(node):
    explicit = _get(node, "tools")
    if explicit is not None:
        return {str(tool) for tool in _list(explicit)}
    # Unset tools inherit the trusted role contract; graph data cannot define
    # that mapping, and unknown custom roles remain unknown/empty here.
    from mewcode.experts.definitions import DEFAULT_EXPERT_REGISTRY
    from mewcode.orchestration.kinds import expert_kind_registry, kind_roles

    registry = expert_kind_registry.get() or DEFAULT_EXPERT_REGISTRY
    role = kind_roles(registry).get(str(_get(node, "kind", "") or ""))
    definition = registry.get(role) if role else None
    return {str(tool) for tool in getattr(definition, "tools", ())}


def review_channels_for_node(node) -> tuple[str, ...]:
    """Infer evidence requirements only; these never select the decision owner."""
    from mewcode.orchestration.spec import parse_review_channels
    explicit = parse_review_channels(_get(node, 'review_channels'))
    if explicit is not None:
        return explicit
    text = _brief_text(node, 'steps') + '\n' + _brief_text(node, 'acceptance')
    return ('text',) + (('pixel',) if _PIXEL_REVIEW.search(text) else ()) + (
        ('command',) if _COMMAND_REVIEW.search(text) else ())


def _channel_neutral(text):
    return _path_norm(_TEXT_LABEL.sub('', _PIXEL_LABEL.sub('', str(text or ''))))


def lint_graph(
    spec: OrchestrationSpec,
    run_inputs_contract: Mapping | None = None,
    *,
    model_capabilities: Mapping | None = None,
    visual_evidence_available: bool = False,
    required_predecessors: Mapping | None = None,
) -> list[LintFinding]:
    """Thirteen deterministic rules; capability facts are supplied by the host.

    Never read review settings, infer capabilities from names, or construct a
    provider inside a linter. Unknown capability cannot prove pixel readiness.
    """
    nodes = _list(_get(spec, "nodes"))
    node_map = {_node_id(node): node for node in nodes}
    meta = _get(spec, "meta", {}) or {}
    loops = [item for item in _list(_get(meta, "loops", ())) if isinstance(item, Mapping)]
    contract = _contract_items(spec, run_inputs_contract)
    parents, _children = _graph_links(spec)
    dominators = _dominators(parents, required_predecessors)
    findings: list[LintFinding] = []

    def add(code, severity, path, entry_ids, message):
        findings.append(LintFinding(code, severity, path, tuple(entry_ids), message))

    work_loop_members = {
        str(member)
        for loop in loops if _has_work_item_source(loop)
        for member in _list(loop.get("node_ids"))
    }

    for node in nodes:
        node_id = _node_id(node)
        if not node_id:
            continue
        kind = str(_get(node, "kind", "") or "")
        brief = _brief(node)
        steps = _brief_text(node, "steps")
        acceptance = _brief_text(node, "acceptance")
        outputs = _brief_text(node, "outputs")
        authority_text = '\n'.join((_text(_get(node, 'label')), _text(_get(node, 'goal')), steps))
        explicit = (meta.get('authorization_requirements') or {}).get(node_id, [])
        required_ops = {r if isinstance(r, str) else r.get('operation_class', '') for r in explicit}
        if kind != 'gate':
            # Ordinary connectivity follows the host's default-on policy.
            # Only an explicitly declared network approval requirement adds a
            # graph gate; external content transfer remains a separate effect.
            required_ops.update(op for op, pattern in _AUTH_OPERATIONS.items()
                                if op != 'network' and _positive_match(pattern, steps))
            # Pixel inspection sends user pixels to a model even when prose omits "external".
            if _positive_match(_OBSERVE_PIXELS, steps):
                required_ops.add('external_send')
            for op in required_ops & set(_AUTH_OPERATIONS):
                pattern = _AUTH_OPERATIONS[op]
                start_collected = any(item.get('collect_at', 'start') == 'start' and
                    (op in item.get('authorization_operations', []) or _positive_match(pattern, _text(item))) for item in contract)
                prior_gate = any(_get(gate, 'kind') == 'gate' and _node_id(gate) in dominators[node_id] - {node_id}
                    and (any(r.get('operation_class') == op for r in (meta.get('authorization_grants') or {}).get(_node_id(gate), []))
                         or _positive_match(pattern, '\n'.join((_text(_get(gate, 'label')), _text(_get(gate, 'goal')), _brief_text(gate, 'steps')))))
                    for gate in nodes)
                if not start_collected and not prior_gate:
                    add('authority_before_use', 'error', f'nodes.{node_id}.plan_brief.steps', (node_id,),
                        f'节点 {node_id} 需要 {op}；把具体授权放到运行开始收集或该节点所有路径之前的人工门禁。声明不会自动形成运行授权。')
        capabilities = (model_capabilities or {}).get(node_id, {})
        supported = (isinstance(capabilities, Mapping) and capabilities.get('supports_vision') is True
                     and capabilities.get('review_only') is not True)
        if ('pixel' in review_channels_for_node(node) or _positive_match(_OBSERVE_PIXELS, authority_text)) and not supported and visual_evidence_available is not True:
            add('review_channel_capability', 'error', f'nodes.{node_id}.review_channels', (node_id,),
                f'节点 {node_id} 需要查看图像，但所用模型不支持图片且未设置看图能力；请为节点选择支持图片的模型，或在设置中配置看图能力。')

        if (kind == "plan" and node_id not in work_loop_members
                and _has_positive_full_scan(steps + "\n" + acceptance)):
            add("plan_node_full_scan", "error", f"nodes.{node_id}.plan_brief", (node_id,),
                f"计划节点 {node_id} 被要求逐项全量处理；把全量工作交给执行节点或批量循环，计划只做元数据与代表样本。")

        if _UNBOUNDED_ACCEPTANCE.search(acceptance) and not _BOUNDED_ACCEPTANCE.search(acceptance):
            add("acceptance_unbounded", "warning", f"nodes.{node_id}.acceptance", (node_id,),
                "验收包含“全部/所有/完整”，请补充有限数量、清单、范围或核验方法。")

        known_inputs = _list(_get(brief, "known_inputs", ()))
        upstream_outputs = [
            _path_norm(output)
            for parent_id in _ancestors(node_id, parents)
            for output in _list(_get(_brief(node_map[parent_id]), "outputs", ()))
        ]
        for known in known_inputs:
            known_text = _text(known)
            if not _PLACEHOLDER.search(known_text):
                continue
            known_norm = _path_norm(known_text)
            parameter_match = any(
                candidate and candidate in known_norm
                for item in contract
                for candidate in (_path_norm(item.get("name")), _path_norm(item.get("label")))
                if len(candidate) >= 2
            )
            artifact_match = any(output and output in known_norm for output in upstream_outputs)
            if parameter_match or artifact_match:
                add("known_input_placeholder", "error", f"nodes.{node_id}.known_inputs", (node_id,),
                    f"已知输入“{known_text}”仍是占位文本，但对应运行参数或上游产物已经声明。")

        if _ENV_NEED.search(steps) and not _PROJECT_VENV.search(steps):
            prepared = _ENV_PREP.search(steps)
            if not prepared:
                prepared = any(
                    _ENV_PREP.search(_brief_text(node_map[upstream], "steps"))
                    or _PROJECT_VENV.search(_brief_text(node_map[upstream], "steps"))
                    for upstream in _ancestors(node_id, parents)
                )
            if not prepared:
                add("environment_not_prepared", "warning", f"nodes.{node_id}.steps", (node_id,),
                    "步骤需要 Python、pip、venv 或依赖，但本节点和上游没有声明环境准备，也未说明使用项目 .venv。")

        if _PROBE.search(steps) and _WRITE.search(steps) and not _SEPARATED.search(steps):
            add("probe_mixed_with_write", "warning", f"nodes.{node_id}.steps", (node_id,),
                "环境探测和写入操作混在同一组步骤中；请声明分步执行。")

        tool_set = _effective_tools(node)
        needs = []
        if _COMMAND.search(steps) and "*" not in tool_set and not (tool_set & {"Bash", "PowerShell", "Command"}):
            needs.append("命令执行工具")
        if (_WRITE.search(steps) and "*" not in tool_set
                and not any(_WRITE_TOOL.search(tool) for tool in tool_set)):
            needs.append("文件写入工具")
        if needs:
            add("tools_too_narrow", "warning", f"nodes.{node_id}.tools", (node_id,),
                "步骤要求" + "和".join(needs) + "，但节点工具白名单未包含对应工具。")

        on_blocked = str(_get(node, "on_blocked", "") or "")
        if on_blocked == "fallback":
            fallback_ids = tuple(str(value) for value in _list(_get(node, "fallback_node_ids", ())))
            ineffective = (not fallback_ids or any(
                target not in node_map or target == node_id for target in fallback_ids
            ))
            if ineffective:
                add("fallback_ineffective", "error", f"nodes.{node_id}.fallback_node_ids", (node_id,),
                    "on_blocked=fallback 必须引用存在且非自身的备用节点。")

        if kind != "gate" and _positive_match(_USER_ASK, steps):
            add("midrun_user_question", "warning", f"nodes.{node_id}.steps", (node_id,),
                "非 gate 节点的步骤要求运行中询问用户；请把可预知的决定前置到 start 或 gate。")

        read_only_tools = tool_set
        if (read_only_tools and all(_is_read_only(str(tool)) for tool in read_only_tools)
                and _MULTIBATCH.search(acceptance)
                and re.search(r"产物|文件|结果|报告|清单|输出", acceptance)
                and not _OUTPUT_METHOD.search(steps + "\n" + outputs)):
            add("read_only_long_task", "warning", f"nodes.{node_id}.acceptance", (node_id,),
                "只读节点的多批次验收要求产物，但没有声明产物写出或交接方式。")

    # The executor accepts explicit multiline inputs and legacy declarations
    # whose defaults infer multiline. Do not reject a value it can preserve.
    from mewcode.orchestration.run_inputs import _input_type
    for item in contract:
        if _input_type(item) in {"list", "multiline"}:
            continue
        for field in ("default", "example"):
            if _MULTILINE_NEED.search(_text(item.get(field))):
                add("multiline_text_input", "error", f"run_inputs.{item.get('name', '')}.{field}", (),
                    f"运行参数「{item.get('name', '')}」的 {field} 含换行；多值参数应声明 type=list。")
                break

    for loop in loops:
        if not _has_work_item_source(loop):
            continue
        entry_id = str(loop.get("entry_node_id") or "")
        members = [node_map[str(member)] for member in _list(loop.get("node_ids"))
                   if str(member) in node_map]
        member_tools = set().union(*(_effective_tools(member) for member in members)) if members else set()
        qualified = False
        for upstream_id in _ancestors(entry_id, parents):
            upstream = node_map[upstream_id]
            upstream_tools = _effective_tools(upstream)
            pilot_steps = _brief_text(upstream, "steps")
            if (('*' in upstream_tools or member_tools <= upstream_tools)
                    and re.search(r"试点|样本|pilot", pilot_steps, re.I)):
                qualified = True
                break
        if not qualified:
            loop_id = str(loop.get("loop_id") or entry_id)
            add("loop_without_pilot", "error", f"loops.{loop_id}.entry_node_id",
                (loop_id, entry_id, *( _node_id(member) for member in members)),
                "批量循环入口前没有同时覆盖循环成员工具集并执行试点/样本验证的上游节点。")

    audits = [node for node in nodes if str(_get(node, 'kind', '')) == 'audit']
    split_groups = {}
    for node in audits:
        label, goal = _text(_get(node, 'label')), _text(_get(node, 'goal'))
        channel_label = label + '\n' + goal
        has_pixel, has_text = bool(_PIXEL_LABEL.search(channel_label)), bool(_TEXT_LABEL.search(channel_label))
        if has_pixel == has_text:
            continue
        target = str(_get(node, 'audit_target', '') or '') or _channel_neutral(goal)
        if not target:
            continue
        signature = (target, _channel_neutral(label), _channel_neutral(goal))
        split_groups.setdefault(signature, []).append((node, 'pixel' if has_pixel else 'text'))
    for members in split_groups.values():
        if {channel for node, channel in members} != {'text', 'pixel'}:
            continue
        ids = tuple(_node_id(node) for node, channel in members)
        add('split_review_by_channel', 'error', 'nodes.review_channels', ids,
            f'节点 {", ".join(ids)} 把同一审查按视觉和文本拆开；合并为一个审查节点，在节点内按证据类型使用不同工具。')
    return findings


_RUNTIME_CAPABILITY_FINDINGS = frozenset({'review_channel_capability'})


def design_lint_findings(
    spec: OrchestrationSpec,
    run_inputs_contract: Mapping | None = None,
) -> list[LintFinding]:
    """Return graph design findings that do not need host capability facts.

    Graph construction and local repair do not know which model or visual
    observer the host will bind at run time.  Keep those checks on the same
    deterministic linter while omitting only the capability-dependent review
    finding; dispatch preflight still calls :func:`lint_graph` with real host
    capability facts.
    """
    return [finding for finding in lint_graph(spec, run_inputs_contract)
            if finding.code not in _RUNTIME_CAPABILITY_FINDINGS]
