"""Tests for collecting a paginated read set from real host stage receipts."""
import asyncio
import copy
import json

import pytest

from mewcode.kernel.read_set import collect_stage_reads
from mewcode.tools.read_stage_result import ReadStageResult, ReadStageResultParams

NODE_ID = "src-a"
ATTEMPT = 1
INPUT_REVISION = "input-1"
PAGE_LIMIT = 2000


def page(tool, source_id, offset=0, limit=PAGE_LIMIT):
    """One real tool call; returns the host observation cursor."""
    result = asyncio.run(tool.execute(
        ReadStageResultParams(node_id=source_id, offset=offset, limit=limit)))
    assert result.is_error is False, result.output
    return result.host_observation["read_cursor"]


def read_all(tool, source_id, limit=PAGE_LIMIT):
    cursors = []
    offset = 0
    while True:
        cursor = page(tool, source_id, offset, limit)
        cursors.append(cursor)
        if not cursor["more"]:
            return cursors
        offset = cursor["next_offset"]


def receipt(cursor, *, node_attempt=ATTEMPT, name="ReadStageResult",
            status="completed", is_error=False):
    return {"name": name, "node_attempt": node_attempt, "status": status,
            "is_error": is_error, "host_observation": {"read_cursor": cursor}}


def host_record(tools, *, node_id=NODE_ID, node_attempt=ATTEMPT,
                input_revision=INPUT_REVISION):
    state = {"node_id": node_id, "node_attempt": node_attempt,
             "input_revision": input_revision, "tools": tools}
    record = {"node_states": {node_id: state}}
    return record


def collect(record, sources, **overrides):
    kwargs = {"node_id": NODE_ID, "node_attempt": ATTEMPT, "input_revision": INPUT_REVISION}
    kwargs.update(overrides)
    return collect_stage_reads(record, sources=sources, **kwargs)


def test_paginated_reads_merge_into_full_coverage():
    sources = {NODE_ID: {"content": "A" * 25000}}
    tool = ReadStageResult(sources)
    cursors = read_all(tool, NODE_ID)
    assert len(cursors) > 1  # the source really needed several pages
    total = cursors[0]["total_chars"]

    result = collect(host_record({f"call-{i}": receipt(c) for i, c in enumerate(cursors)}), sources)

    assert result["format_revision"] == 1
    assert result["source_count"] == 1
    assert result["coverage"] == "complete"
    read = result["reads"][0]
    assert read["node_id"] == NODE_ID
    assert read["source_revision"] == tool.revisions[NODE_ID]
    assert read["total_chars"] == total
    assert read["intervals"] == [[0, total]]
    assert read["receipt_ids"] == sorted(f"call-{i}" for i in range(len(cursors)))
    assert read["complete"] is True


def test_missing_page_is_partial():
    sources = {NODE_ID: {"content": "B" * 25000}}
    tool = ReadStageResult(sources)
    cursors = read_all(tool, NODE_ID)
    assert len(cursors) > 1
    kept = cursors[:-1]

    result = collect(host_record({f"call-{i}": receipt(c) for i, c in enumerate(kept)}), sources)

    assert result["coverage"] == "partial"
    read = result["reads"][0]
    assert read["complete"] is False
    assert read["intervals"] == [[0, kept[-1]["next_offset"]]]
    assert kept[-1]["next_offset"] < read["total_chars"]


def test_index_listing_never_counts_as_a_read():
    sources = {NODE_ID: {"content": "C" * 300}}
    index_cursor = page(ReadStageResult(sources), "")
    assert index_cursor["node_id"] == ""

    result = collect(host_record({"call-0": receipt(index_cursor)}), sources)

    assert result["reads"] == []
    assert result["coverage"] == "none"


def test_wrong_revision_and_old_attempt_are_ignored():
    sources = {NODE_ID: {"content": "D" * 300}}
    cursor = page(ReadStageResult(sources), NODE_ID)
    stale = copy.deepcopy(cursor)
    stale["source_revision"] = "revision-of-an-earlier-input"
    tools = {"stale": receipt(stale), "old-attempt": receipt(cursor, node_attempt=ATTEMPT + 1)}

    result = collect(host_record(tools), sources)

    assert result["reads"] == []
    assert result["coverage"] == "none"


def test_changed_source_content_invalidates_old_receipt():
    old_cursor = page(ReadStageResult({NODE_ID: {"content": "E" * 400}}), NODE_ID)
    current = {NODE_ID: {"content": "F" * 400}}  # same key, different bytes

    result = collect(host_record({"call-0": receipt(old_cursor)}), current)

    assert result["reads"] == []
    assert result["coverage"] == "none"


def test_forged_claims_are_not_host_observation():
    sources = {NODE_ID: {"content": "G" * 300}}
    cursor = page(ReadStageResult(sources), NODE_ID)
    tools = {
        "claim": {"name": "ReadSetClaim", "node_attempt": ATTEMPT, "status": "completed",
                  "is_error": False, "host_observation": {"read_set": {"coverage": "complete"}}},
        "prose": {"name": "ReadStageResult", "node_attempt": ATTEMPT, "status": "completed",
                  "is_error": False, "output": "已完整阅读整个来源",
                  "host_observation": {"read_cursor": "fully read"}},
        "error": {"name": "ReadStageResult", "node_attempt": ATTEMPT, "status": "completed",
                  "is_error": False, "host_observation": None},
        "good": receipt(cursor),
    }

    result = collect(host_record(tools), sources)

    assert result["coverage"] == "complete"
    assert result["reads"][0]["receipt_ids"] == ["good"]


def test_overlapping_and_duplicate_pages_merge_stably():
    sources = {NODE_ID: {"content": "H" * 20000}, "src-b": {"content": "I" * 5000}}
    tool = ReadStageResult(sources)
    first = page(tool, NODE_ID, 0)
    overlap = page(tool, NODE_ID, first["next_offset"] // 2)
    duplicate = page(tool, NODE_ID, 0)
    assert overlap["next_offset"] > first["next_offset"]

    tools = {"z-late": receipt(first), "a-early": receipt(overlap), "m-mid": receipt(duplicate)}
    result = collect(host_record(tools), sources)

    assert result["coverage"] == "partial"
    assert [read["node_id"] for read in result["reads"]] == [NODE_ID]
    read = result["reads"][0]
    assert read["intervals"] == [[0, overlap["next_offset"]]]
    assert read["complete"] is False
    assert read["receipt_ids"] == ["a-early", "m-mid", "z-late"]


def test_reads_are_sorted_by_source_id():
    sources = {"src-b": {"content": "J" * 300}, NODE_ID: {"content": "K" * 300}}
    tool = ReadStageResult(sources)
    tools = {"second": receipt(page(tool, "src-b")), "first": receipt(page(tool, NODE_ID))}

    result = collect(host_record(tools), sources)

    assert [read["node_id"] for read in result["reads"]] == [NODE_ID, "src-b"]
    assert result["coverage"] == "complete"
    assert all(read["complete"] for read in result["reads"])


@pytest.mark.parametrize("mutate", [
    lambda c: c.update(offset=-1),
    lambda c: c.update(offset=0, next_offset=0),
    lambda c: c.update(offset=c["next_offset"], next_offset=c["next_offset"]),
    lambda c: c.update(total_chars=c["total_chars"] + 1),
    lambda c: c.update(more=True),
    lambda c: c.update(more=1),
    lambda c: c.update(offset=True),
    lambda c: c.update(next_offset=float(c["next_offset"])),
    lambda c: c.update(offset="0"),
    lambda c: c.update(node_id="unknown-source"),
    lambda c: c.update(source_revision=None),
])
def test_corrupt_cursor_is_ignored(mutate):
    sources = {NODE_ID: {"content": "L" * 300}}
    cursor = copy.deepcopy(page(ReadStageResult(sources), NODE_ID))
    mutate(cursor)

    result = collect(host_record({"call-0": receipt(cursor)}), sources)

    assert result["reads"] == []
    assert result["coverage"] == "none"


@pytest.mark.parametrize("overrides", [
    {"status": "running"},
    {"status": "error"},
    {"is_error": True},
    {"name": "ModelReadClaim"},
])
def test_unqualified_receipts_are_ignored(overrides):
    sources = {NODE_ID: {"content": "Q" * 300}}
    cursor = page(ReadStageResult(sources), NODE_ID)
    tools = {"bad": receipt(cursor, **overrides), "good": receipt(cursor)}

    result = collect(host_record(tools), sources)

    assert result["coverage"] == "complete"
    assert result["reads"][0]["receipt_ids"] == ["good"]


def test_receipt_without_attempt_is_not_the_same_read():
    sources = {NODE_ID: {"content": "O" * 300}}
    broken = receipt(page(ReadStageResult(sources), NODE_ID))
    del broken["node_attempt"]

    result = collect(host_record({"call-0": broken}), sources)

    assert result["reads"] == []
    assert result["coverage"] == "none"


@pytest.mark.parametrize("state", [
    None,
    {"node_id": NODE_ID, "node_attempt": ATTEMPT + 1, "input_revision": INPUT_REVISION},
    {"node_id": NODE_ID, "node_attempt": ATTEMPT, "input_revision": "other-input"},
    {"node_id": "src-z", "node_attempt": ATTEMPT, "input_revision": INPUT_REVISION},
    {"node_id": NODE_ID, "node_attempt": ATTEMPT},
    {"node_id": NODE_ID, "node_attempt": True, "input_revision": INPUT_REVISION},
])
def test_stage_identity_mismatch_yields_no_reads(state):
    sources = {NODE_ID: {"content": "N" * 300}}
    cursor = page(ReadStageResult(sources), NODE_ID)
    record = {"stage": {"tools": {"call-0": receipt(cursor)}}}
    record["node_states"] = {} if state is None else {NODE_ID: state}

    result = collect(record, sources)

    assert result["reads"] == []
    assert result["coverage"] == "none"


def test_unbound_root_or_nested_tools_cannot_supply_reads():
    sources = {NODE_ID: {"content": "P" * 300}}
    cursor = page(ReadStageResult(sources), NODE_ID)

    record = host_record({})
    record["stage"] = {"tools": {"root": receipt(cursor)}}
    record["node_states"][NODE_ID]["stage"] = {"tools": {"nested": receipt(cursor)}}
    result = collect(record, sources)
    assert result["coverage"] == "none" and result["reads"] == []


def test_zero_sources_is_complete():
    result = collect(host_record({}), {})

    assert result["source_count"] == 0
    assert result["reads"] == []
    assert result["coverage"] == "complete"
    # Zero sources stay complete even when no node state matches this attempt.
    assert collect({"node_states": {}}, {}, node_attempt=7)["coverage"] == "complete"


def test_inputs_are_untouched_and_body_is_never_returned():
    marker = "SECRET-BODY-MARKER"
    sources = {NODE_ID: {"content": marker + "M" * 3000}}
    tool = ReadStageResult(sources)
    record = host_record({"call-0": receipt(page(tool, NODE_ID))})
    record_snapshot = copy.deepcopy(record)
    sources_snapshot = copy.deepcopy(sources)

    result = collect(record, sources)

    assert record == record_snapshot
    assert sources == sources_snapshot
    assert set(result) == {"format_revision", "node_id", "node_attempt", "input_revision",
                           "source_count", "reads", "coverage"}
    assert set(result["reads"][0]) == {"node_id", "source_revision", "total_chars",
                                       "intervals", "receipt_ids", "complete"}
    assert result["node_attempt"] == ATTEMPT
    assert result["input_revision"] == INPUT_REVISION
    assert marker not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("overrides", [
    {"node_id": ""},
    {"node_id": "   "},
    {"node_id": 1},
    {"node_attempt": 0},
    {"node_attempt": -1},
    {"node_attempt": True},
    {"node_attempt": 1.0},
    {"node_attempt": "1"},
    {"input_revision": ""},
    {"input_revision": None},
])
def test_invalid_api_arguments_raise_value_error(overrides):
    with pytest.raises(ValueError):
        collect(host_record({}), {}, **overrides)


@pytest.mark.parametrize("bad_sources", [None, [], "sources", {1: {}}, {NODE_ID: "text"}])
def test_invalid_sources_raise_value_error(bad_sources):
    with pytest.raises(ValueError):
        collect(host_record({}), bad_sources)
