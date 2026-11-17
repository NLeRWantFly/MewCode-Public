"""Fold real host stage receipts into a paginated read set.

Pure helpers only: this module never touches files, the environment or the
network, never mutates its arguments, and never treats receipt text or a model
claim as host observation. Serialized source versions and lengths come from
``ReadStageResult`` itself, so its hashing and serialization rules are reused
instead of duplicated.
"""
from mewcode.tools.read_stage_result import ReadStageResult

FORMAT_REVISION = 1
_TOOL_NAME = "ReadStageResult"


def collect_stage_reads(record, *, node_id, node_attempt, input_revision, sources):
    """Summarize which parts of ``sources`` this node attempt really read.

    Only ``record["node_states"][node_id]`` and the ``stage.tools`` receipts of
    the same attempt are inspected; everything else is ignored.
    """
    _validate(node_id, node_attempt, input_revision, sources)

    # Reuse the tool's own view of each source: its character count and its
    # revision (declared, or the hash of the serialized content).
    tool = ReadStageResult(sources)
    revisions = tool.revisions
    lengths = {key: len(text) for key, text in tool.sources.items()}

    state = None
    if isinstance(record, dict) and isinstance(record.get("node_states"), dict):
        candidate = record["node_states"].get(node_id)
        state = candidate if isinstance(candidate, dict) else None

    reads = []
    if _identity_matches(state, node_id, node_attempt, input_revision):
        grouped = {}
        for receipt_id, receipt in _stage_tools(state).items():
            if not _is_text(receipt_id):
                continue
            span = _valid_span(receipt, sources, revisions, lengths, node_attempt)
            if span is None:
                continue
            source_id, offset, next_offset = span
            grouped.setdefault(source_id, []).append((offset, next_offset, receipt_id))
        for source_id in sorted(grouped):
            spans = grouped[source_id]
            intervals = _merge([(offset, end) for offset, end, _ in spans])
            reads.append({
                "node_id": source_id,
                "source_revision": revisions[source_id],
                "total_chars": lengths[source_id],
                "intervals": intervals,
                "receipt_ids": sorted({receipt_id for _, _, receipt_id in spans}),
                "complete": intervals == [[0, lengths[source_id]]],
            })

    if not sources:
        coverage = "complete"
    elif reads and len(reads) == len(sources) and all(read["complete"] for read in reads):
        coverage = "complete"
    elif reads:
        coverage = "partial"
    else:
        coverage = "none"

    return {
        "format_revision": FORMAT_REVISION,
        "node_id": node_id,
        "node_attempt": node_attempt,
        "input_revision": input_revision,
        "source_count": len(sources),
        "reads": reads,
        "coverage": coverage,
    }


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_text(value):
    return isinstance(value, str) and value.strip() != ""


def _validate(node_id, node_attempt, input_revision, sources):
    if not _is_text(node_id):
        raise ValueError("node_id 必须为非空字符串")
    if not _is_int(node_attempt) or node_attempt <= 0:
        raise ValueError("node_attempt 必须为正整数")
    if not _is_text(input_revision):
        raise ValueError("input_revision 必须为非空字符串")
    if not isinstance(sources, dict):
        raise ValueError("sources 必须为 dict[str, dict]")
    for key, value in sources.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            raise ValueError("sources 必须为 dict[str, dict]")


def _identity_matches(state, node_id, node_attempt, input_revision):
    if not isinstance(state, dict):
        return False
    if state.get("node_id") != node_id:
        return False
    if not _is_int(state.get("node_attempt")) or state["node_attempt"] != node_attempt:
        return False
    return state.get("input_revision") == input_revision


def _stage_tools(state):
    tools = state.get("tools")
    return tools if isinstance(tools, dict) else {}


def _valid_span(receipt, sources, revisions, lengths, node_attempt):
    """Return (source_id, offset, next_offset) for a genuine receipt, else None."""
    if not isinstance(receipt, dict):
        return None
    if receipt.get("name") != _TOOL_NAME or receipt.get("status") != "completed":
        return None
    if receipt.get("is_error") is not False:
        return None
    if not _is_int(receipt.get("node_attempt")) or receipt["node_attempt"] != node_attempt:
        return None

    observation = receipt.get("host_observation")
    cursor = observation.get("read_cursor") if isinstance(observation, dict) else None
    if not isinstance(cursor, dict):
        return None

    source_id = cursor.get("node_id")
    # The frozen-source index is a listing, never a read of real content.
    if not _is_text(source_id) or source_id not in sources:
        return None
    if cursor.get("source_revision") != revisions[source_id]:
        return None

    offset = cursor.get("offset")
    next_offset = cursor.get("next_offset")
    total = cursor.get("total_chars")
    if not (_is_int(offset) and _is_int(next_offset) and _is_int(total)):
        return None
    if not 0 <= offset < next_offset <= total:
        return None
    if total != lengths[source_id]:
        return None
    more = cursor.get("more")
    if not isinstance(more, bool) or more != (next_offset < total):
        return None
    return source_id, offset, next_offset


def _merge(spans):
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged
