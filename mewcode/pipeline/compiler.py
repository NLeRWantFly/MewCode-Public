"""Pipeline 图编译器。

负责：
- 图的合法性校验（必选字段、分支标签覆盖、变量引用静态检查；
  回边识别为有界循环而非错误）
- 拓扑排序生成执行顺序
- 条件分支路径计算
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from mewcode.pipeline.models import (
    ConditionMode,
    ExecMode,
    GraphNode,
    NodeType,
    PipelineGraph,
    ValidateResponse,
    is_auto,
)
from mewcode.pipeline.variables import VAR_PATTERN

log = logging.getLogger(__name__)


KNOWN_EFFORT_LEVELS: tuple[str, ...] = ("off", "low", "medium", "high")
"""已知的 effort 档位全集。

与 mewcode/client.py 的 EFFORT_LEVELS 各协议档位的并集保持一致。
compiler 不直接依赖网络/协议信息：能力上下文（model -> 可用档位）
缺失时，effort 只用该全集做基础合法性检查。"""


class CompileError(Exception):
    """图编译错误。"""

    pass


class CycleDetectedError(CompileError):
    """检测到环。"""

    def __init__(self, cycle_path: list[str]) -> None:
        self.cycle_path = cycle_path
        super().__init__(f"Cycle detected: {' -> '.join(cycle_path)}")


@dataclass
class ExecutionPlan:
    """编译后的执行计划。"""

    topo_order: list[GraphNode] = field(default_factory=list)
    """拓扑排序后的节点列表。"""

    node_map: dict[str, GraphNode] = field(default_factory=dict)
    """节点 ID -> 节点映射。"""

    adjacency: dict[str, list[str]] = field(default_factory=dict)
    """邻接表：节点 ID -> 后继节点 ID 列表。"""

    back_edges: set[str] = field(default_factory=set)
    """回边（循环）的边 ID 集合。这些边指向 DFS 祖先，构成有界循环。"""

    in_degree: dict[str, int] = field(default_factory=dict)
    """有效入度：节点 ID -> 前向入边数（不含回边，含重复边）。

    join 调度的等待计数基线：执行器以此计数完成前驱，全部有效前驱
    完成后节点才就绪（菱形汇聚点只执行一次）；回边不计入，否则循环
    入口节点永远无法首次就绪。"""

    loop_scopes: dict[str, list[str]] = field(default_factory=dict)
    """loop 容器作用域：loop 节点 ID -> 循环体节点 ID 列表（拓扑序）。

    循环体 = 从 loop 出发可达 且 能回到 loop 的节点（即位于经过 loop
    的环上），不含 loop 自身与 start/end。体外下游节点不在此列表中，
    循环期间不会被执行（修复旧实现的「体外节点误伤」）。"""

    parallel_scopes: dict[str, list[list[str]]] = field(default_factory=dict)
    """parallel 容器作用域：parallel 节点 ID -> 分支列表，
    每个分支是拓扑序的节点 ID 列表（与出边顺序对应）。

    被多个分支同时可达的节点（汇合点及其后）不属于任何分支，
    由外层调度在容器完成后执行一次。"""

    scope_owner: dict[str, str] = field(default_factory=dict)
    """作用域归属：容器体内节点 ID -> 所属容器节点 ID（嵌套时归最内层）。

    执行器据此把体内节点从外层 join 计数/就绪调度中摘除——它们只由
    容器内部执行，容器完成后沿「边界出边」推进外层下游。"""


class PipelineCompiler:
    """Pipeline 图编译器。"""

    @staticmethod
    def validate(
        graph: PipelineGraph,
        model_efforts: Mapping[str, Sequence[str]] | None = None,
    ) -> ValidateResponse:
        """校验图的合法性。

        Args:
            graph: Pipeline 图定义。
            model_efforts: 可选的模型能力上下文：model id -> 可用 effort
                档位列表（api 层用 client.model_effort_levels 构建后传入，
                compiler 自身不依赖网络）。缺失时跳过 effort-模型匹配校验，
                effort 只做基础合法性检查（值在 KNOWN_EFFORT_LEVELS 内）。

        errors（阻断保存/编译）：
        1. 必须有且仅有一个 start 节点
        2. 节点 ID 唯一
        3. task 节点必须有 goal/prompt/skill 之一；go 模式必须有 boundary
        4. goal 节点必须有 criteria
        5. condition 节点必须有 ref_task；expression 模式需 ref_field/
           compare_value，judgment 模式需 criteria
        6. condition 节点出边 label 必须覆盖 yes/no 两分支
        7. tool/skill 节点必须有 tool_name/skill_name
        8. 所有边引用的节点必须存在
        9. schedule 节点必须有 schedule 配置；支持 cron、一次性时间、
           固定间隔（起点 + 间隔秒数 + 可选次数上限）；允许作为流程关卡
           接收入边，到点后从其出边继续

        warnings（不阻断保存）：
        - 缺少 end 节点
        - 回边（有界循环）提示
        - {{...}} 引用了不存在的上游节点/output_key/全局变量
        - expression 模式的上游任务未定义 output_schema
        - 孤立节点
        - schedule 节点没有入边（未接入主流程）、没有出边（触发后无事
          可做），或只有 schedule_text 尚未解析出 cron（触发不会注册）
        - 显式（非自动挡）effort 不在已知档位全集内，或不在所选模型
          （节点显式模型，缺省回退图默认模型）的可用档位内；
          显式模型不在能力上下文的可用模型列表内
        """
        errors: list[str] = []
        warnings: list[str] = []
        import json
        try:
            progress = json.loads(graph.variables.get("__orch.payload__", "{}" )).get("meta", {}).get("canvas_mode") == "chat_activity"
        except (ValueError, TypeError, AttributeError):
            progress = False
        if progress:
            return ValidateResponse(valid=False, errors=["对话进度是只读活动视图，不能执行"], warnings=[])

        # 检查节点
        start_nodes = [n for n in graph.nodes if n.type == NodeType.START]
        end_nodes = [n for n in graph.nodes if n.type == NodeType.END]

        if len(start_nodes) == 0:
            errors.append("Missing start node")
        elif len(start_nodes) > 1:
            errors.append(f"Multiple start nodes found: {len(start_nodes)}")

        if len(end_nodes) == 0:
            warnings.append("No end node found")

        # 检查节点 ID 唯一性
        node_ids = [n.id for n in graph.nodes]
        if len(node_ids) != len(set(node_ids)):
            errors.append("Duplicate node IDs found")

        # 检查必选字段
        for node in graph.nodes:
            if node.type == NodeType.TASK:
                if not node.goal and not node.prompt and not node.skill:
                    errors.append(f"Task node '{node.id}' missing goal/prompt/skill")
                # go 模式必须划定清晰边界
                if node.mode == ExecMode.GO and not node.boundary.strip():
                    errors.append(
                        f"Task node '{node.id}' uses GO mode but has no boundary defined"
                    )
            elif node.type == NodeType.GOAL:
                if not node.criteria:
                    errors.append(f"Goal node '{node.id}' missing criteria")
            elif node.type == NodeType.CONDITION:
                if not node.ref_task:
                    errors.append(f"Condition node '{node.id}' missing ref_task (which task to verify)")
                if node.condition_mode == ConditionMode.EXPRESSION:
                    if not node.ref_field:
                        errors.append(f"Condition node '{node.id}' expression mode missing ref_field")
                    if not node.compare_value and node.operator not in ("contains",):
                        errors.append(f"Condition node '{node.id}' expression mode missing compare_value")
                    # 指标核验要求上游任务定义了 output_schema
                    ref_node = next((n for n in graph.nodes if n.id == node.ref_task), None)
                    if ref_node and ref_node.type == NodeType.TASK and not ref_node.output_schema:
                        warnings.append(
                            f"Condition '{node.id}' uses expression mode but upstream task "
                            f"'{node.ref_task}' has no output_schema defined"
                        )
                elif node.condition_mode == ConditionMode.JUDGMENT:
                    if not node.criteria:
                        errors.append(f"Condition node '{node.id}' judgment mode missing criteria")
            elif node.type == NodeType.TOOL:
                if not node.tool_name:
                    errors.append(f"Tool node '{node.id}' missing tool_name")
            elif node.type == NodeType.SKILL:
                if not node.skill_name:
                    errors.append(f"Skill node '{node.id}' missing skill_name")
            elif node.type == NodeType.ACTION:
                if not node.action_id:
                    errors.append(f"Action node '{node.id}' missing action_id")
            elif node.type == NodeType.SCHEDULE:
                cfg = node.schedule
                if cfg is not None and cfg.resolved_cron and (
                    cfg.resolved_at
                    or cfg.resolved_interval_seconds
                    or cfg.resolved_max_runs
                ):
                    errors.append(
                        f"Schedule node '{node.id}' cannot combine resolved_cron "
                        f"with resolved_at/interval fields"
                    )
                if cfg is None or (
                    not cfg.schedule_text and not cfg.resolved_cron and not cfg.resolved_at
                ):
                    errors.append(
                        f"Schedule node '{node.id}' missing schedule config "
                        f"(schedule_text, resolved_cron, or resolved_at)"
                    )
                elif cfg.resolved_cron:
                    try:
                        from mewcode.scheduler.cron import CronExpression

                        expr = CronExpression.parse(cfg.resolved_cron)
                        if not expr.validate():
                            errors.append(
                                f"Schedule node '{node.id}' cron "
                                f"'{cfg.resolved_cron}' never matches any time"
                            )
                    except Exception as e:
                        errors.append(
                            f"Schedule node '{node.id}' has invalid cron "
                            f"'{cfg.resolved_cron}': {e}"
                        )
                elif cfg.resolved_at:
                    try:
                        from datetime import datetime

                        value = datetime.fromisoformat(
                            cfg.resolved_at.replace("Z", "+00:00")
                        )
                        if value.tzinfo is None:
                            raise ValueError("timezone is required")
                    except ValueError as e:
                        errors.append(
                            f"Schedule node '{node.id}' has invalid resolved_at "
                            f"'{cfg.resolved_at}': {e}"
                        )
                    if cfg.resolved_interval_seconds < 0:
                        errors.append(
                            f"Schedule node '{node.id}' interval must be positive"
                        )
                    elif cfg.resolved_interval_seconds > 31_536_000:
                        errors.append(
                            f"Schedule node '{node.id}' interval exceeds one year"
                        )
                    if cfg.resolved_max_runs < 0:
                        errors.append(
                            f"Schedule node '{node.id}' max runs cannot be negative"
                        )
                    elif cfg.resolved_max_runs > 1_000_000:
                        errors.append(
                            f"Schedule node '{node.id}' max runs is too large"
                        )
                else:
                    warnings.append(
                        f"Schedule node '{node.id}' has schedule_text but no "
                        f"resolved schedule yet; it has no initial fire time"
                    )
                if (
                    cfg is not None
                    and cfg.resolved_interval_seconds
                    and not cfg.resolved_at
                ):
                    errors.append(
                        f"Schedule node '{node.id}' interval requires resolved_at"
                    )
                if (
                    cfg is not None
                    and cfg.resolved_max_runs
                    and not cfg.resolved_interval_seconds
                ):
                    errors.append(
                        f"Schedule node '{node.id}' max runs requires an interval"
                    )
                if cfg is not None and cfg.adaptive and not cfg.adaptive_policy.strip():
                    errors.append(
                        f"Schedule node '{node.id}' adaptive mode requires "
                        f"adaptive_policy"
                    )
                if (
                    cfg is not None
                    and cfg.overlap_policy not in {"auto", "allow", "skip"}
                ):
                    errors.append(
                        f"Schedule node '{node.id}' has invalid overlap policy "
                        f"'{cfg.overlap_policy}'"
                    )
                # schedule 是可布防的流程关卡：入边表达流程入口，调度到点后
                # 仍以 schedule 自身作为运行入口，仅放行右侧下游。
                if not any(e.to_node == node.id for e in graph.edges):
                    warnings.append(
                        f"Schedule node '{node.id}' has no incoming edge "
                        f"(not connected to the main flow)"
                    )
                if not any(e.from_node == node.id for e in graph.edges):
                    warnings.append(
                        f"Schedule node '{node.id}' has no outgoing edges "
                        f"(fires nothing)"
                    )

        # 检查边引用的节点
        node_id_set = set(node_ids)
        for edge in graph.edges:
            if edge.from_node not in node_id_set:
                errors.append(f"Edge references unknown source node: {edge.from_node}")
            if edge.to_node not in node_id_set:
                errors.append(f"Edge references unknown target node: {edge.to_node}")

        # condition 节点出边 label 必须覆盖 yes/no 两分支
        # （执行器按 edge.label 精确匹配命中分支，缺失分支会静默走不到）
        for node in graph.nodes:
            if node.type != NodeType.CONDITION:
                continue
            out_labels = {e.label for e in graph.edges if e.from_node == node.id}
            missing_labels = [lb for lb in ("yes", "no") if lb not in out_labels]
            if missing_labels:
                errors.append(
                    f"Condition node '{node.id}' missing outgoing branch "
                    f"label(s): {', '.join(missing_labels)}"
                )

        # {{...}} 变量引用静态检查：引用的根名必须是内置命名空间
        # （input/env/loop）、全局变量、节点 ID 或某节点的 output_key
        warnings.extend(PipelineCompiler._check_variable_refs(graph, node_id_set))

        # effort/model 保存期校验：非法值只 warning（向后兼容旧图，
        # 运行时按自动挡回退，不阻断保存）
        warnings.extend(
            PipelineCompiler._check_model_efforts(graph, model_efforts)
        )

        # 环检测：识别回边（循环），不再视为错误
        back_edges = PipelineCompiler._find_back_edges(graph)
        if back_edges:
            warnings.append(
                f"Graph contains {len(back_edges)} loop-back edge(s); "
                f"loops are bounded by max iterations at runtime"
            )

        # 检查孤立节点（没有入边也没有出边，且不是 start/end；
        # schedule 的无出边已单独提示，不重复上报）
        for node in graph.nodes:
            if node.type in (NodeType.START, NodeType.END, NodeType.SCHEDULE):
                continue
            has_incoming = any(e.to_node == node.id for e in graph.edges)
            has_outgoing = any(e.from_node == node.id for e in graph.edges)
            if not has_incoming and not has_outgoing:
                warnings.append(f"Node '{node.id}' is isolated (no connections)")

        return ValidateResponse(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

    @staticmethod
    def _check_variable_refs(
        graph: PipelineGraph, node_id_set: set[str]
    ) -> list[str]:
        """静态检查 {{...}} 变量引用的根名存在性。

        合法根名：input / env / loop 内置命名空间、graph.variables 的键、
        节点 ID、节点的 output_key。引用了未知根名时返回 warning
        （不阻断保存——上游可能在运行时以自定义变量补上）。
        """
        known_roots = {"input", "env", "loop"}
        known_roots.update(graph.variables.keys())
        known_roots.update(node_id_set)
        known_roots.update(n.output_key for n in graph.nodes if n.output_key)

        warnings: list[str] = []
        seen: set[tuple[str, str]] = set()

        def check_text(owner: str, text: str) -> None:
            for match in VAR_PATTERN.finditer(text):
                path = match.group(1)
                root = path.split(".")[0]
                if root in known_roots:
                    continue
                key = (owner, path)
                if key in seen:
                    continue
                seen.add(key)
                warnings.append(
                    f"{owner} references unknown variable '{{{{{path}}}}}'"
                )

        def check_value(owner: str, value: Any) -> None:
            if isinstance(value, str):
                check_text(owner, value)
            elif isinstance(value, dict):
                for v in value.values():
                    check_value(owner, v)

        for node in graph.nodes:
            owner = f"Node '{node.id}'"
            for text in (node.prompt, node.goal, node.criteria, node.boundary):
                check_text(owner, text)
            check_value(owner, node.params)
            check_value(owner, node.args)

        for var_key, var_value in graph.variables.items():
            check_value(f"Variable '{var_key}'", var_value)

        return warnings

    @staticmethod
    def _check_model_efforts(
        graph: PipelineGraph,
        model_efforts: Mapping[str, Sequence[str]] | None,
    ) -> list[str]:
        """effort/model 保存期校验（只产 warning，不阻断）。

        规则：
        - 显式（非自动挡，见 is_auto）的 effort 必须在 KNOWN_EFFORT_LEVELS
          全集内；
        - 提供能力上下文 model_efforts（model -> 可用档位列表，来源为
          client.model_effort_levels）时，进一步要求 effort 在所选模型的
          可用档位内。所选模型 = 节点显式模型，缺省回退图默认模型；
          两者都缺省（自动挡）则无法确定模型能力，跳过该项匹配校验；
          所选模型不在能力上下文中时同样跳过（能力未知）；
        - 显式模型不在能力上下文中时提示未知模型；
        - 图级默认 default_model/default_effort 只检查一次
          （前缀 "Graph default"），不随节点重复上报。
        """
        warnings: list[str] = []

        # 能力上下文归一化：模型 id 与档位值均按小写比对
        normalized: dict[str, list[str]] | None = None
        if model_efforts is not None:
            normalized = {
                str(model).strip().lower(): [
                    str(e).strip().lower() for e in efforts
                ]
                for model, efforts in model_efforts.items()
            }

        def check_model(owner: str, model: str | None) -> None:
            """显式模型必须在能力上下文的可用模型列表内。"""
            if normalized is None or is_auto(model):
                return
            if model.strip().lower() not in normalized:
                warnings.append(
                    f"{owner} uses unknown model '{model}' "
                    f"(not in available models)"
                )

        def check_effort(
            owner: str, effort: str | None, model: str | None
        ) -> None:
            """显式 effort 的基础合法性 + 模型档位匹配校验。"""
            if is_auto(effort):
                return
            effort_val = effort.strip().lower()
            # 基础合法性：值必须在已知 effort 全集内
            if effort_val not in KNOWN_EFFORT_LEVELS:
                warnings.append(
                    f"{owner} has unknown effort '{effort}' "
                    f"(known levels: {', '.join(KNOWN_EFFORT_LEVELS)})"
                )
                return
            # effort-模型匹配：需要能力上下文 + 可确定的模型
            if normalized is None or is_auto(model):
                return
            available = normalized.get(model.strip().lower())
            if available is None:
                # 模型不在能力上下文中（能力未知）→ 跳过匹配校验，
                # 未知模型本身由 check_model 上报
                return
            if not available:
                warnings.append(
                    f"{owner} sets effort '{effort}' but model '{model}' "
                    f"does not support reasoning effort"
                )
            elif effort_val not in available:
                warnings.append(
                    f"{owner} effort '{effort}' is not available for "
                    f"model '{model}' (available: {', '.join(available)})"
                )

        # 图级默认只检查一次，避免随节点数重复
        check_model("Graph default", graph.default_model)
        check_effort("Graph default", graph.default_effort, graph.default_model)

        for node in graph.nodes:
            owner = f"Node '{node.id}'"
            check_model(owner, node.model)
            # effort 匹配所用模型：节点显式模型，缺省回退图默认模型
            resolved_model = (
                node.model if not is_auto(node.model) else graph.default_model
            )
            check_effort(owner, node.effort, resolved_model)

        return warnings

    @staticmethod
    def compile(graph: PipelineGraph) -> ExecutionPlan:
        """编译图生成执行计划。

        Args:
            graph: Pipeline 图定义。

        Returns:
            ExecutionPlan 执行计划。

        Raises:
            CompileError: 图不合法。
        """
        # 先校验
        validation = PipelineCompiler.validate(graph)
        if not validation.valid:
            raise CompileError("; ".join(validation.errors))

        # 构建节点映射
        node_map: dict[str, GraphNode] = {n.id: n for n in graph.nodes}

        # 识别回边（循环），拓扑排序时排除它们
        back_edges = PipelineCompiler._find_back_edges(graph)

        # 构建邻接表（排除回边，得到无环骨架）
        adjacency: dict[str, list[str]] = {n.id: [] for n in graph.nodes}
        in_degree: dict[str, int] = {n.id: 0 for n in graph.nodes}

        for edge in graph.edges:
            if edge.id in back_edges:
                continue
            if edge.from_node in adjacency:
                adjacency[edge.from_node].append(edge.to_node)
            if edge.to_node in in_degree:
                in_degree[edge.to_node] += 1

        # 拓扑排序（Kahn 算法，作用于无环骨架；会消耗计数，传副本）
        topo_order = PipelineCompiler._topological_sort(
            node_map, adjacency, dict(in_degree)
        )

        # 完整邻接表（含回边，供执行器遍历）
        full_adjacency: dict[str, list[str]] = {n.id: [] for n in graph.nodes}
        for edge in graph.edges:
            if edge.from_node in full_adjacency:
                full_adjacency[edge.from_node].append(edge.to_node)

        # loop/parallel 容器作用域（编译期算好，执行器不再运行时遍历）
        loop_scopes, parallel_scopes, scope_owner = (
            PipelineCompiler._compute_scopes(graph, node_map, back_edges, topo_order)
        )

        return ExecutionPlan(
            topo_order=topo_order,
            node_map=node_map,
            adjacency=full_adjacency,
            back_edges=back_edges,
            in_degree=in_degree,
            loop_scopes=loop_scopes,
            parallel_scopes=parallel_scopes,
            scope_owner=scope_owner,
        )

    @staticmethod
    def _compute_scopes(
        graph: PipelineGraph,
        node_map: dict[str, GraphNode],
        back_edges: set[str],
        topo_order: list[GraphNode],
    ) -> tuple[dict[str, list[str]], dict[str, list[list[str]]], dict[str, str]]:
        """计算 loop/parallel 容器的作用域（编译期）。

        - loop 体 = 从 loop 出发可达（不穿过 loop） ∩ 能回到 loop 的节点，
          即位于经过 loop 的环上的节点；无回到 loop 的闭环时体为空。
        - parallel 分支 = 每条出边可达的节点集合（沿前向边，不穿过容器），
          被 ≥2 条分支同时可达的节点（汇合点及其后）从所有分支中剔除。
        - start/end 节点永不入作用域；嵌套容器时节点归最内层
          （raw 作用域更小者优先认领），外层列表只保留直辖节点
          （嵌套容器自身仍在外层列表中，由外层执行时递归展开）。

        Returns:
            (loop_scopes, parallel_scopes, scope_owner)。
        """
        out_edges: dict[str, list[Any]] = {n.id: [] for n in graph.nodes}
        in_edges: dict[str, list[Any]] = {n.id: [] for n in graph.nodes}
        for edge in graph.edges:
            if edge.from_node in out_edges:
                out_edges[edge.from_node].append(edge)
            if edge.to_node in in_edges:
                in_edges[edge.to_node].append(edge)

        def forward_from(container_id: str) -> set[str]:
            """从容器出发的前向可达集合（不穿过容器自身）。"""
            seen: set[str] = set()
            stack = [e.to_node for e in out_edges.get(container_id, [])]
            while stack:
                nid = stack.pop()
                if nid == container_id or nid in seen or nid not in node_map:
                    continue
                seen.add(nid)
                stack.extend(e.to_node for e in out_edges.get(nid, []))
            return seen

        def backward_from(container_id: str) -> set[str]:
            """能到达容器的反向可达集合（不穿过容器自身）。"""
            seen: set[str] = set()
            stack = [e.from_node for e in in_edges.get(container_id, [])]
            while stack:
                nid = stack.pop()
                if nid == container_id or nid in seen or nid not in node_map:
                    continue
                seen.add(nid)
                stack.extend(e.from_node for e in in_edges.get(nid, []))
            return seen

        def usable(nid: str) -> bool:
            return node_map[nid].type not in (
                NodeType.START, NodeType.END, NodeType.SCHEDULE
            )

        raw_scopes: dict[str, set[str]] = {}
        raw_branches: dict[str, list[set[str]]] = {}

        for node in graph.nodes:
            if node.type == NodeType.LOOP:
                body = forward_from(node.id) & backward_from(node.id)
                body.discard(node.id)
                raw_scopes[node.id] = {b for b in body if usable(b)}
            elif node.type == NodeType.PARALLEL:
                entries: list[str] = []
                for e in out_edges.get(node.id, []):
                    if e.id in back_edges or e.to_node == node.id:
                        continue
                    if e.to_node in node_map and e.to_node not in entries:
                        entries.append(e.to_node)
                reach_sets: list[set[str]] = []
                for entry in entries:
                    seen: set[str] = set()
                    stack = [entry]
                    while stack:
                        nid = stack.pop()
                        if nid == node.id or nid in seen or nid not in node_map:
                            continue
                        seen.add(nid)
                        stack.extend(
                            e.to_node for e in out_edges.get(nid, [])
                            if e.id not in back_edges
                        )
                    reach_sets.append(seen)
                # 被多条分支同时可达 → 汇合点及其后，不属于任何分支
                shared: set[str] = set()
                for i in range(len(reach_sets)):
                    for j in range(i + 1, len(reach_sets)):
                        shared |= reach_sets[i] & reach_sets[j]
                branches = [
                    {n for n in rs - shared if usable(n)} for rs in reach_sets
                ]
                raw_branches[node.id] = branches
                raw_scopes[node.id] = set().union(*branches) if branches else set()

        # 归属解析：raw 作用域更小的容器（最内层）先认领
        owner: dict[str, str] = {}
        for cid in sorted(raw_scopes, key=lambda c: (len(raw_scopes[c]), c)):
            for nid in raw_scopes[cid]:
                owner.setdefault(nid, cid)

        topo_index = {n.id: i for i, n in enumerate(topo_order)}

        def ordered(ids: set[str], cid: str) -> list[str]:
            direct = [n for n in ids if owner.get(n) == cid]
            direct.sort(key=lambda n: topo_index.get(n, 0))
            return direct

        loop_scopes: dict[str, list[str]] = {}
        parallel_scopes: dict[str, list[list[str]]] = {}
        for node in graph.nodes:
            if node.type == NodeType.LOOP:
                loop_scopes[node.id] = ordered(raw_scopes[node.id], node.id)
            elif node.type == NodeType.PARALLEL:
                parallel_scopes[node.id] = [
                    ordered(bs, node.id) for bs in raw_branches.get(node.id, [])
                ]

        # scope_owner 只保留真正落入某容器直辖列表的节点
        final_owner = {
            nid: cid for nid, cid in owner.items()
            if (cid in loop_scopes and nid in loop_scopes[cid])
            or (cid in parallel_scopes and any(nid in b for b in parallel_scopes[cid]))
        }
        return loop_scopes, parallel_scopes, final_owner

    @staticmethod
    def _topological_sort(
        node_map: dict[str, GraphNode],
        adjacency: dict[str, list[str]],
        in_degree: dict[str, int],
    ) -> list[GraphNode]:
        """Kahn 算法拓扑排序。"""
        # 找到所有入度为 0 的节点
        queue: deque[str] = deque()
        for node_id, degree in in_degree.items():
            if degree == 0:
                queue.append(node_id)

        result: list[GraphNode] = []
        visited_count = 0

        while queue:
            node_id = queue.popleft()
            result.append(node_map[node_id])
            visited_count += 1

            for successor in adjacency.get(node_id, []):
                in_degree[successor] -= 1
                if in_degree[successor] == 0:
                    queue.append(successor)

        if visited_count != len(node_map):
            raise CycleDetectedError(["cycle detected in graph"])

        return result

    @staticmethod
    def _find_back_edges(graph: PipelineGraph) -> set[str]:
        """识别图中的回边（构成循环的边）。

        回边定义：DFS 中指向当前路径上祖先节点的边。
        排除回边后，剩余图是无环的（可拓扑排序）。

        Returns:
            回边的 edge.id 集合。
        """
        # 邻接表：node_id -> [(to_node, edge_id)]
        adjacency: dict[str, list[tuple[str, str]]] = {n.id: [] for n in graph.nodes}
        for edge in graph.edges:
            if edge.from_node in adjacency:
                adjacency[edge.from_node].append((edge.to_node, edge.id))

        # DFS 状态：0=未访问, 1=在栈中(祖先), 2=已完成
        state: dict[str, int] = {n.id: 0 for n in graph.nodes}
        back_edges: set[str] = set()

        def dfs(node_id: str) -> None:
            state[node_id] = 1
            for to_node, edge_id in adjacency.get(node_id, []):
                if state.get(to_node) == 1:
                    # 指向祖先 → 回边
                    back_edges.add(edge_id)
                elif state.get(to_node) == 0:
                    dfs(to_node)
            state[node_id] = 2

        for node in graph.nodes:
            if state[node.id] == 0:
                dfs(node.id)

        return back_edges

    @staticmethod
    def get_branch_target(
        graph: PipelineGraph,
        condition_node_id: str,
        branch_label: str,
    ) -> str | None:
        """获取条件分支的目标节点。

        Args:
            graph: Pipeline 图。
            condition_node_id: 条件节点 ID。
            branch_label: 分支标签（"yes" / "no"）。

        Returns:
            目标节点 ID，或 None。
        """
        for edge in graph.edges:
            if edge.from_node == condition_node_id and edge.label == branch_label:
                return edge.to_node
        return None
