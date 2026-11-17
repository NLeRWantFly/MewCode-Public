"""Independent, source-checked coverage review for a candidate graph."""
from __future__ import annotations

import asyncio
import copy
import json
from collections import Counter
from os.path import commonprefix
from mewcode.orchestration_guide import orchestration_build_experience

from .draft_sources import digest
from .repair_protocol import is_control_source
from .workflow_draft import quote_matches


REVIEW_SYSTEM = """你是独立审查员，只找覆盖缺口；不批准执行，不修改图，不重画。
输入中 requirements 是用户原话来源，graph 是当前图摘要。
判断当前事实以 graph 为准，history_context 中的旧拓扑或旧字段不能覆盖当前图。报告“没有边、成员、约束或输入”前，逐项核对当前 edges、loops.node_ids/required_node_ids、constraints 和 ledger.inputs；必须说明哪个明确要求仍未被实际字段覆盖，不能把未查到或不确定直接写成不存在。
history_context 标明当前方案、仍有效条目及被替代、退休或排除的旧内容；它帮助理解取舍，不会把已排除内容恢复成要求。若 requirement 带有 chunk 元数据，其 text 是原始用户来源的一段连续文本；完整来源可能在其他批次中，仍须只引用当前原文片段。
若有 history_citations，history_context 的 source_refs 与 field_sources 中的 {"citation_id":"..."} 指向该表的完整来源引用；按编号展开后理解原有归属、引文和取舍。共享引用只去除重复存储，不产生新来源、用户确认或授权。
若有 history_context_tables，它与 history_context 等价：依次读取各表，每个 rows 数组按 columns 列名组成一条记录，再合并该表 shared 中的共同字段。所有记录顺序、状态、原因和原文均保留；展开记录后再解析 history_citations，不能把 excluded 或 superseded 记录当作当前要求。
若 history_field_sources_grouped 为 true，部分历史记录用 field_source_groups 代替 field_sources：每组 fields 列出的每个字段均对应该组完整的 references 列表。先恢复这些字段映射，再展开 history_citations；字段归属、引用顺序及重复引用均保留，分组不增加或撤销要求。
若有 history_quote_prefixes，history_citations 中 quote={"prefix_id":"...","suffix":"..."} 表示该表对应前缀与 suffix 原样拼接得到的完整引文。先还原引文，再展开 citation_id。此编码保留全部字符、来源身份和额外元数据，不是摘要；requirements 与当前 graph 不使用该编码。
若 history_text_refs 为 true，先递归还原历史区中的文字引用，再处理以上历史编码：单字段对象 {"graph_text_path":[...]} 指向当前 graph 内按键名及数组下标逐层查找的完整字符串；{"history_text_id":"..."} 指向 history_texts 中的完整字符串。引用只复用逐字相同的原文，不把历史状态改为当前状态，不增加授权；graph、runtime_semantics 和 requirements 仍是完整明文。
缺口仅指用户明确提出、而图中没有对应的节点、约束、验收或确认点的要求。
明确区分宿主设计服务与待执行的业务图：对宿主程序的源码修复、测试、容量恢复、刷新、构图交接或保存检查的要求，应核对其适用范围及已记录的约束；不能据此要求业务图新增修复节点或专属子图。仍适用于业务执行的授权、模型、验收和内容要求必须逐项核对，不能借此范围区分排除。
措辞风格、实现细节偏好、助手自己的建议、已被节点/约束/验收覆盖的要求、已在 inputs 中声明为运行参数的输入，不算缺口。
“需要用户决定时提供选项、推荐理由和影响”等条件式交互要求，只约束真实待决事项的呈现；不要求创造新的业务取舍、必填偏好或人工门槛来展示选项。若报告此类缺口，须指出已有的必要待决事项及其原文依据，不能只因图中没有选项参数而判缺；真正未解决的用户独有决定仍须保留。
先按原文的对象和阶段确定要求范围：当前设计对话、构图或设计审查服务的模型配置，不自动成为未来业务图节点的模型要求；节点名称含“主控”或“规划”也不能证明它就是当前设计服务。仅当原文明示适用于图内节点、业务角色或全部业务模型时，才据此核对实际 model 绑定；明确的业务模型要求仍必须满足，不能用设计阶段配置或正文承诺代替。
检查可执行字段是否兑现用户要求：节点职责文字里的模型名称不能替代 model 绑定；execution_mode=coordinate 的父节点只协调，实际执行依赖独立子节点的绑定与显式连线；按用户选择限制一条路径须有 input_when，只有至少两条互斥路径才共享 branch_group；正文开关与两条相同 pass 边不能实现互斥分流。依据 runtime_semantics 判断，不能仅凭正文承诺判定已覆盖。后续阶段的业务选择可保留为 collect_at 指定阶段的必填 input，无默认值不算遗漏，不得视为已获授权。
input_bindings 的值可引用运行参数条目的 id；ledger.inputs 同时给出条目 id 和参数 name，二者不是同一个标识。runtime_semantics 是宿主调度规则，不是具体模型能力或实际调用的证据清单；已安排执行前核实及失败阻断时，尚未产生的视觉能力、构图保存或业务运行回执本身不是设计覆盖缺口，也不得据此宣称执行或验收通过。
每个缺口必须给 source_id 和从该来源原文逐字复制的引文，不得改写。没有缺口就返回空数组，最多 10 条。
只输出 JSON：{"gaps":[{"source_id":"","quote":"","summary":"","suggested_fix":""}],"notes":""}。
"""

REVIEW_SYSTEM += ("\n\n以下经验只帮助核对当前 requirements 与实际字段的对应；"
                 "经验本身不是新要求，没有真实用户引文不能生成缺口。"
                 "本轮仅返回覆盖核验结果，不修改图、不检查应用源码、不改变输出协议。\n\n"
                 + orchestration_build_experience())


class ReviewRequestTooLarge(ValueError):
    """The lossless review request exceeds the configured character limit."""


class ReviewParseError(ValueError):
    """The reviewer response does not match the coverage-gap protocol."""


def _requirements(sources):
    result = []
    for source in sources:
        if source.get("role") != "user" or is_control_source(source):
            continue
        requirement = {"source_id": source["id"], "text": source["text"]}
        chunk = source.get("coverage_chunk")
        if isinstance(chunk, dict):
            requirement["chunk"] = copy.deepcopy(chunk)
        result.append(requirement)
    return result


def _history_context(sources):
    contexts, seen = [], set()
    for source in sources:
        rows = source.get("coverage_context") or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = digest(row)
            if key not in seen:
                seen.add(key)
                contexts.append(copy.deepcopy(row))
    return contexts


def _with_decisions(sources, document):
    values = {source["id"]: source for source in sources}
    for entry in document.get("entries", []):
        if (entry.get("state") == "active" and entry.get("kind") == "decision"
                and entry.get("reviewed") is True and (entry.get("data") or {}).get("category") == "confirmed"):
            source_id = "adopted_" + entry["id"]
            values[source_id] = {"id": source_id, "role": "user", "text": entry.get("body", ""), "purpose": "adopted_decision"}
    return list(values.values())


def _factor_history_citations(value):
    """Losslessly share repeated provenance references in an oversized request."""
    if not value.get("history_context"):
        return None
    result = copy.deepcopy(value)
    citations, identities = {}, {}
    uses = 0
    for row in result["history_context"]:
        fields = row.get("field_sources")
        lists = [row.get("source_refs")]
        if isinstance(fields, dict):
            lists.extend(fields.values())
        for refs in lists:
            if not isinstance(refs, list):
                continue
            for index, ref in enumerate(refs):
                if (not isinstance(ref, dict) or not isinstance(ref.get("source_id"), str)
                        or not isinstance(ref.get("quote"), str)):
                    continue
                uses += 1
                identity = json.dumps(ref, ensure_ascii=False, sort_keys=True)
                citation_id = identities.get(identity)
                if citation_id is None:
                    # These are request-local keys, not source identities.
                    citation_id = "c" + str(len(citations) + 1)
                    identities[identity] = citation_id
                    citations[citation_id] = ref
                refs[index] = {"citation_id": citation_id}
    if uses <= len(citations):
        return None
    result["history_citations"] = citations
    return result


def _table_history_context(value):
    """Share column names and common fields while retaining every ordered row."""
    if not value.get("history_context"):
        return None
    result = copy.deepcopy(value)
    tables = []
    for row in result.pop("history_context"):
        columns = sorted(row)
        if not tables or tables[-1]["columns"] != columns:
            tables.append({"columns": columns, "rows": []})
        tables[-1]["rows"].append([row[key] for key in columns])
    for table in tables:
        rows = table["rows"]
        if len(rows) < 2:
            continue
        shared = {}
        for index, key in reversed(list(enumerate(table["columns"]))):
            identity = json.dumps(rows[0][index], ensure_ascii=False, sort_keys=True)
            if all(json.dumps(row[index], ensure_ascii=False, sort_keys=True) == identity for row in rows):
                shared[key] = rows[0][index]
                table["columns"].pop(index)
                for row in rows:
                    row.pop(index)
        if shared:
            table["shared"] = shared
    result["history_context_tables"] = tables
    return result


def _group_history_field_sources(value):
    """Share identical field provenance without omitting fields or references."""
    context = value.get("history_context")
    if not context or any("field_source_groups" in row for row in context):
        return None
    result = copy.deepcopy(value)
    changed = False
    for row in result["history_context"]:
        fields = row.get("field_sources")
        if (not isinstance(fields, dict) or not fields
                or any(not isinstance(key, str) or not isinstance(refs, list)
                       for key, refs in fields.items())):
            continue
        groups = {}
        for key, refs in fields.items():
            identity = json.dumps(refs, ensure_ascii=False, sort_keys=True)
            groups.setdefault(identity, {"fields": [], "references": refs})["fields"].append(key)
        grouped = {"field_source_groups": list(groups.values())}
        original = {"field_sources": fields}
        if len(json.dumps(grouped, ensure_ascii=False)) >= len(json.dumps(original, ensure_ascii=False)):
            continue
        del row["field_sources"]
        row.update(grouped)
        changed = True
    if not changed:
        return None
    result["history_field_sources_grouped"] = True
    return result


def _factor_history_quote_prefixes(value):
    """Share literal provenance prefixes; source quotes remain lossless strings."""
    citations = value.get("history_citations")
    if not isinstance(citations, dict) or "history_quote_prefixes" in value:
        return None
    result = copy.deepcopy(value)
    citations = result["history_citations"]
    candidates = {}
    for width in (64, 128, 256, 512, 1024):
        groups = {}
        for key, row in citations.items():
            quote = row.get("quote") if isinstance(row, dict) else None
            if isinstance(quote, str) and len(quote) >= width:
                groups.setdefault(quote[:width], []).append(key)
        for keys in groups.values():
            if len(keys) > 1:
                candidates[tuple(keys)] = commonprefix([citations[key]["quote"] for key in keys])
    ranked = sorted(candidates.items(), key=lambda pair: len(pair[1]) * (len(pair[0])-1), reverse=True)
    used, prefixes = set(), {}

    def size(item):
        return len(json.dumps(item, ensure_ascii=False, separators=(",", ":")))

    for keys, _prefix in ranked:
        keys = [key for key in keys if key not in used]
        if len(keys) < 2:
            continue
        prefix = commonprefix([citations[key]["quote"] for key in keys])
        identity = "p" + str(len(prefixes)+1)
        replacements = {key: {"prefix_id": identity, "suffix": citations[key]["quote"][len(prefix):]}
                        for key in keys}
        saving = sum(size(citations[key]["quote"])-size(replacements[key]) for key in keys)
        if saving <= size({identity: prefix}) + 32:
            continue
        prefixes[identity] = prefix
        for key in keys:
            citations[key]["quote"] = replacements[key]
        used.update(keys)
    if not prefixes:
        return None
    result["history_quote_prefixes"] = prefixes
    return result


def _factor_history_texts(value):
    """Reuse exact graph/history strings without changing their owning records."""
    if "history_text_refs" in value or "history_texts" in value:
        return None
    keys = ("history_context", "history_context_tables", "history_citations", "history_quote_prefixes")

    def walk(item):
        yield item
        if isinstance(item, dict):
            for child in item.values():
                yield from walk(child)
        elif isinstance(item, list):
            for child in item:
                yield from walk(child)

    history = [value[key] for key in keys if key in value]
    # Never reinterpret an existing structured value as one of our references.
    if any(isinstance(item, dict) and set(item) in ({"graph_text_path"}, {"history_text_id"})
           for item in walk(history)):
        return None

    def size(item):
        return len(json.dumps(item, ensure_ascii=False, separators=(",", ":")))

    graph_paths = {}

    def collect(item, path):
        if isinstance(item, str) and len(item) >= 40:
            if item not in graph_paths or size(path) < size(graph_paths[item]):
                graph_paths[item] = path
        elif isinstance(item, dict):
            for key, child in item.items():
                collect(child, [*path, key])
        elif isinstance(item, list):
            for index, child in enumerate(item):
                collect(child, [*path, index])

    collect(value.get("graph", {}), [])
    counts = Counter(item for item in walk(history) if isinstance(item, str) and len(item) >= 40)
    replacements, texts = {}, {}
    for literal, count in counts.items():
        if literal in graph_paths:
            reference = {"graph_text_path": graph_paths[literal]}
            if size(reference) < size(literal):
                replacements[literal] = reference
                continue
        if count > 1:
            identity = "t" + str(len(texts) + 1)
            reference = {"history_text_id": identity}
            if count * (size(literal) - size(reference)) > size({identity: literal}) + 32:
                texts[identity] = literal
                replacements[literal] = reference
    if not replacements:
        return None

    def replace(item):
        if isinstance(item, str):
            return copy.deepcopy(replacements.get(item, item))
        if isinstance(item, dict):
            return {key: replace(child) for key, child in item.items()}
        if isinstance(item, list):
            return [replace(child) for child in item]
        return item

    result = copy.deepcopy(value)
    for key in keys:
        if key in result:
            result[key] = replace(result[key])
    result["history_text_refs"] = True
    if texts:
        result["history_texts"] = texts
    return result if size(result) < size(value) else None


def _encode_review_value(value, *, compact=False):
    # Put the varying source batch after the stable graph and history so a
    # provider supporting prefix caching can reuse those identical inputs.
    # Keep the current graph adjacent to the requirements, after historical
    # alternatives, without dropping history or breaking the stable prefix.
    ordered = {key: item for key, item in value.items()
               if key not in {"runtime_semantics", "graph", "requirements"}}
    for key in ("runtime_semantics", "graph"):
        if key in value:
            ordered[key] = value[key]
    ordered["requirements"] = value["requirements"]
    options = {"separators": (",", ":")} if compact else {}
    return json.dumps(ordered, ensure_ascii=False, **options)


def build_review_request(sources, document, *, max_chars: int | None = None) -> str:
    """Serialize user requirements and the active graph without truncation."""
    # A displayed patch explicitly adopted by the user is an additional,
    # versioned requirement. Raw exploratory discussion remains excluded.
    sources = _with_decisions(sources, document)
    from .run_inputs import resolved_question_ids
    resolved = resolved_question_ids(document)
    graph = {
        "goal": "", "nodes": [], "edges": [], "loops": [], "constraints": [],
        "ledger": {"assumptions": [], "inputs": [], "questions": []},
    }
    goals = []
    for entry in document.get("entries", []):
        if entry.get("state") != "active":
            continue
        kind = entry.get("kind")
        data = entry.get("data") or {}
        if kind == "goal":
            goals.append(entry.get("body", ""))
        elif kind == "node":
            node_kind = data.get("node_kind", "")
            graph["nodes"].append({
                "id": entry.get("id", ""), "title": entry.get("title", ""),
                "kind": node_kind, "gate": node_kind == "gate",
                "goal": entry.get("body", ""), "inputs": data.get("inputs", []),
                "steps": data.get("steps", []), "parent_id": data.get("parent_id", ""),
                "outputs": data.get("outputs", []),
                "acceptance": data.get("acceptance", []),
                **{key: copy.deepcopy(data[key]) for key in ("work_key", "input_bindings", "capabilities") if key in data},
                **({"model":copy.deepcopy(data["model"])} if data.get("model") else {}),
                **({"execution_mode":data["execution_mode"]} if data.get("execution_mode") else {}),
            })
        elif kind == "edge":
            graph["edges"].append({
                "from": data.get("from_node", ""), "to": data.get("to_node", ""),
                "condition": data.get("condition", ""),
                **{key: data[key] for key in ("input_when", "branch_group") if key in data},
            })
        elif kind == "loop":
            graph["loops"].append({
                "loop_id": data.get("loop_id", ""),
                "entry": data.get("entry_node_id", ""),
                "check": data.get("check_node_id", ""),
                "exit_condition": data.get("exit_condition", ""),
                **{key: data[key] for key in ("node_ids", "required_node_ids", "reviewer_node_ids", "criteria", "control_mode",
                    "max_rounds", "token_budget", "time_budget_seconds", "max_iterations", "timeout_seconds",
                    "max_stalled_rounds", "node_max_iterations", "reviewer_max_iterations", "requires_repair",
                    "repair_node_ids", "repair_dispatch") if key in data},
            })
        elif kind in {"constraint", "artifact"} or (kind == "decision" and data.get("category") not in {"designed", "defaulted"}):
            graph["constraints"].append({
                "id": entry.get("id", ""), "kind": kind,
                "body": entry.get("body", ""), "node_id": data.get("node_id", ""),
            })
        elif kind == "decision" and "category" in data:
            graph["ledger"]["assumptions"].append({"body": entry.get("body", ""),
                "category": data.get("category", ""), "value": data.get("value", ""), "node_id": data.get("node_id", "")})
        elif kind == "input":
            graph["ledger"]["inputs"].append({"id": entry.get("id", ""), "name": data.get("name", ""),
                "description": data.get("description", ""), "required": data.get("required", True),
                "default": data.get("default", ""), "collect_at": data.get("collect_at", "start"),
                **{key: copy.deepcopy(data[key]) for key in ("label", "options") if key in data},
                "required_when": data.get("required_when")})
        elif kind == "question" and entry["id"] not in resolved:
            graph["ledger"]["questions"].append(entry.get("body", ""))
    graph["goal"] = "\n".join(goals)
    from .graph_inspection import runtime_semantics
    value = {"requirements": _requirements(sources), "graph": graph,
             "runtime_semantics": runtime_semantics()}
    history_context = _history_context(sources)
    if history_context:
        value["history_context"] = history_context
    payload = _encode_review_value(value)
    if max_chars is not None and len(payload) > max_chars:
        # Whitespace and repeated history fields are storage overhead, not
        # grounds to drop historical exclusions or shorten source text.
        payload = _encode_review_value(value, compact=True)
        factored = _factor_history_citations(value)
        if factored is not None:
            shared_payload = _encode_review_value(factored, compact=True)
            if len(shared_payload) < len(payload):
                payload = shared_payload
                value = factored
        if len(payload) > max_chars:
            tabular = _table_history_context(value)
            if tabular is not None:
                shared_payload = _encode_review_value(tabular, compact=True)
                if len(shared_payload) < len(payload):
                    payload = shared_payload
        if len(payload) > max_chars:
            grouped = _group_history_field_sources(value)
            if grouped is not None:
                grouped = _table_history_context(grouped) or grouped
                shared_payload = _encode_review_value(grouped, compact=True)
                if len(shared_payload) < len(payload):
                    payload = shared_payload
        if len(payload) > max_chars:
            prefixed = _factor_history_quote_prefixes(json.loads(payload))
            if prefixed is not None:
                shared_payload = _encode_review_value(prefixed, compact=True)
                if len(shared_payload) < len(payload):
                    payload = shared_payload
        if len(payload) > max_chars:
            referenced = _factor_history_texts(json.loads(payload))
            if referenced is not None:
                payload = _encode_review_value(referenced, compact=True)
        if len(payload) > max_chars:
            raise ReviewRequestTooLarge(f"审查请求长度 {len(payload)} 超过上限 {max_chars}")
    return payload


def _json_object(raw_text):
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw_text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(raw_text, index)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise ReviewParseError("审查响应缺少 JSON 对象")


def parse_review_response(raw_text) -> dict:
    """Parse a reviewer JSON object, keeping at most ten display-safe gaps."""
    if not isinstance(raw_text, str):
        raise ReviewParseError("审查响应不是文本")
    value = _json_object(raw_text)
    gaps = value.get("gaps")
    if not isinstance(gaps, list):
        raise ReviewParseError("审查响应缺少 gaps 数组")
    for gap in gaps:
        if (not isinstance(gap, dict)
                or any(not isinstance(gap.get(key), str)
                       for key in ("source_id", "quote", "summary"))
                or not isinstance(gap.get("suggested_fix", ""), str)):
            raise ReviewParseError("审查缺口字段无效")
    normalized = []
    for gap in gaps[:10]:
        item = dict(gap)
        item["summary"] = item["summary"][:300]
        if "suggested_fix" in item:
            item["suggested_fix"] = item["suggested_fix"][:300]
        normalized.append(item)
    return {"gaps": normalized, "notes": value.get("notes", "") if isinstance(value.get("notes", ""), str) else "",
            "truncated": max(0, len(gaps) - 10)}


def verify_gaps(gaps, sources) -> tuple[list[dict], list[dict]]:
    """Keep only gaps whose quote can be found in a real user source."""
    source_by_id = {source.get("id"): source for source in sources}
    verified, dropped = [], []
    for gap in gaps:
        source_id = gap["source_id"]
        source = source_by_id.get(source_id)
        if source is None:
            reason = "unknown_source"
        elif source.get("role") != "user":
            reason = "not_user_source"
        elif is_control_source(source):
            reason = "control_source"
        elif not (1 <= len(gap["quote"]) <= 1500) or not quote_matches(gap["quote"], source["text"]):
            reason = "quote_not_found"
        else:
            verified.append(gap)
            continue
        dropped.append({"source_id": source_id, "reason": reason})
    return verified, dropped


def gap_diagnostics(gaps) -> list[dict]:
    """Turn verified coverage gaps into completion diagnostics."""
    result = []
    for gap in gaps:
        message = f'用户要求「{gap["quote"][:120]}」在图中没有对应内容：{gap["summary"]}'
        if gap.get("suggested_fix"):
            message += f'；建议：{gap["suggested_fix"]}'
        result.append({"code": "coverage_gap", "path": f'sources/{gap["source_id"]}',
                       "entry_ids": [], "message": message})
    return result


def _fits_review(sources, document, max_chars):
    try:
        build_review_request(sources, document, max_chars=max_chars)
        return True
    except ReviewRequestTooLarge:
        return False


def _split_large_source(source, document, max_chars):
    """Split one user source into contiguous, lightly overlapping text parts."""
    text = source["text"]
    if not text:
        raise ReviewRequestTooLarge("空来源仍无法放入有界审查请求")

    def candidate(start, end):
        row = copy.deepcopy(source)
        row["text"] = text[start:end]
        # Max-width markers keep final indexes safely below the measured size.
        row["coverage_chunk"] = {"index": 999999, "count": 999999,
                                 "start": start, "end": end}
        return row

    def furthest_end(start, minimum_end):
        low, high, best = minimum_end, len(text), minimum_end - 1
        while low <= high:
            middle = (low + high) // 2
            if _fits_review([candidate(start, middle)], document, max_chars):
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        return best

    capacity = furthest_end(0, 1)
    if capacity < 1:
        # This also catches a graph or decision context that consumes the
        # entire budget before any requirement text can be included.
        raise ReviewRequestTooLarge("图和审查上下文没有为来源文本留下空间")
    overlap = min(1500, capacity // 4)
    ranges = []
    covered_end = 0
    while covered_end < len(text):
        start = max(0, covered_end - overlap)
        end = furthest_end(start, covered_end + 1)
        if end <= covered_end and overlap:
            overlap = 0
            start = covered_end
            end = furthest_end(start, covered_end + 1)
        if end <= covered_end:
            raise ReviewRequestTooLarge("来源文本无法按当前请求上限分片")
        ranges.append((start, end))
        covered_end = end

    count = len(ranges)
    chunks = []
    for index, (start, end) in enumerate(ranges, 1):
        row = copy.deepcopy(source)
        row["text"] = text[start:end]
        row["coverage_chunk"] = {"index": index, "count": count,
                                 "start": start, "end": end}
        if not _fits_review([row], document, max_chars):
            raise ReviewRequestTooLarge("分片元数据超出审查请求上限")
        chunks.append(row)
    return chunks


def _review_batches(sources, document, max_chars):
    """Greedily pack whole requirements, splitting only an oversized source."""
    # Fail early if the repeated graph and runtime contract alone exceed the
    # budget; the graph is intentionally complete in every request.
    build_review_request([], document, max_chars=max_chars)
    requirements = [copy.deepcopy(source) for source in sources
                    if source.get("role") == "user" and not is_control_source(source)]
    batches, current = [], []
    for source in requirements:
        if _fits_review(current + [source], document, max_chars):
            current.append(source)
            continue
        if current:
            batches.append(current)
            current = []
        if _fits_review([source], document, max_chars):
            current = [source]
        else:
            batches.extend([[chunk] for chunk in _split_large_source(source, document, max_chars)])
    if current:
        batches.append(current)
    return batches


def _batch_coverage(index, sources):
    parts = []
    for source in sources:
        chunk = source.get("coverage_chunk") or {}
        parts.append({"source_id": source["id"],
                      "chunk_index": chunk.get("index", 1),
                      "chunk_count": chunk.get("count", 1),
                      "start": chunk.get("start", 0),
                      "end": chunk.get("end", len(source["text"])),
                      "text_chars": len(source["text"])})
    return {"batch": index, "parts": parts, "status": "pending",
            "verified_gaps": 0, "dropped_gaps": 0, "error": ""}


async def review_coverage(sources, document, *, ask_review, max_chars: int = 250000,
                          failure_context=None) -> dict:
    """Review complete bounded batches and verify citations against full sources."""
    sources = _with_decisions(sources, document)
    result = {"gaps": [], "dropped": [], "truncated": 0, "model_calls": 0,
              "skipped": "", "error": "", "notes": ""}

    def failure(exc, stage, batch=None):
        from .extract import OrchestrationStreamTimeout
        from .generation_failure import generation_failure
        value = generation_failure(exc, failure_context)
        value.update(stage=stage, batch=batch)
        if isinstance(exc, ReviewRequestTooLarge):
            value.update(category="capacity", retryable=False)
        elif isinstance(exc, ReviewParseError):
            value.update(category="response_format", retryable=False)
        elif stage != "model_call" or (isinstance(exc, ValueError) and not (
                isinstance(exc, OrchestrationStreamTimeout) and exc.phase == "first_event")):
            value["retryable"] = False
        label = {"request_preparation": "准备请求", "model_call": "调用模型",
                 "response_parse": "解析响应", "citation_validation": "核对引用"}[stage]
        status = f"，HTTP {value['http_status']}" if value["http_status"] is not None else ""
        value["summary"] = f"独立覆盖审查在{label}时失败（{value['exception_type']}{status}）"
        if value["detail"]:
            value["summary"] += "：" + value["detail"][:1200]
        if not result["error"]:
            result.update(error=type(exc).__name__, failure=value)
        return value
    if not _requirements(sources):
        result["skipped"] = "no_requirements"
        return result
    try:
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
            raise ReviewRequestTooLarge("max_chars 必须是正整数")
        batches = _review_batches(sources, document, max_chars)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        failure(exc, "request_preparation")
        return result

    batch_rows = []
    all_gaps, all_dropped, all_notes = [], [], []
    for index, batch in enumerate(batches, 1):
        coverage = _batch_coverage(index, batch)
        batch_rows.append(coverage)
        stage = "request_preparation"
        try:
            request = build_review_request(batch, document, max_chars=max_chars)
            result["model_calls"] += 1
            stage = "model_call"
            raw = await ask_review(REVIEW_SYSTEM, request)
            stage = "response_parse"
            parsed = parse_review_response(raw)
            stage = "citation_validation"
            verified, dropped = verify_gaps(parsed["gaps"], sources)
            all_gaps.extend(verified)
            all_dropped.extend(dropped)
            result["truncated"] += parsed["truncated"]
            if parsed["notes"]:
                all_notes.append((index, parsed["notes"]))
            coverage.update(status="reviewed", verified_gaps=len(verified),
                            dropped_gaps=len(dropped))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = type(exc).__name__
            diagnostic = failure(exc, stage, index)
            coverage.update(status="failed", error=error, failure=diagnostic)

    failures = [row["failure"] for row in batch_rows if row.get("failure")]
    if failures:
        # Every failed batch must be transient before the host retries the review.
        result["failure"] = copy.deepcopy(result["failure"])
        result["failure"]["retryable"] = all(row["retryable"] is True for row in failures)

    seen = set()
    for gap in all_gaps:
        key = (gap["source_id"], gap["quote"], gap["summary"], gap.get("suggested_fix", ""))
        if key not in seen:
            seen.add(key)
            result["gaps"].append(gap)
    result["dropped"] = all_dropped
    result["notes"] = all_notes[0][1] if len(all_notes) == 1 else "\n".join(
        f"批次 {index}：{note}" for index, note in all_notes)
    # Preserve the established result shape for a simple single request.
    # Multi-batch/chunked runs expose exactly what was reviewed and its status.
    if (failures or len(batch_rows) > 1 or any("coverage_context" in source for source in sources)
            or any(part["chunk_count"] > 1
                                   for row in batch_rows for part in row["parts"])
            or any(part["text_chars"] >= max_chars // 2
                   for row in batch_rows for part in row["parts"])):
        result["batches"] = batch_rows
    return result
