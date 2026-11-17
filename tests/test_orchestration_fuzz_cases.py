"""Property-style boundary tests for the orchestration path.

The contracts asserted here are deliberately broad but strict:

* Garbage input may either parse into something usable or raise
  ``OrchestrationError`` — never a TypeError / AttributeError / KeyError /
  IndexError / StopIteration, because those escape as opaque failures.
* Parsers fed truncated text must not raise and must keep a stable shape.

No external property-testing dependency is used; the input matrices are
deterministic so a failure is reproducible from the test alone.
"""
from __future__ import annotations

import json
import random

import pytest

from mewcode.orchestration import OrchestrationSpec, to_pipeline_graph
from mewcode.orchestration.change_explain import parse_conflict_markers, parse_unified_diff
from mewcode.orchestration.interventions import enqueue_intervention
from mewcode.orchestration.loops import LoopContract, compile_loops
from mewcode.orchestration.process_files import MAX_RECEIPTS, add_process_file, file_receipt
from mewcode.orchestration.spec import (
    GoalContract,
    LoopPolicy,
    ModelBinding,
    OrchestrationEdge,
    OrchestrationError,
    OrchestrationNode,
    PlanBrief,
    SpecAction,
)


GARBAGE = [
    None, True, False, 0, -1, 3.5, float("nan"), float("inf"),
    "", "  ", "x", "1e999", "-", "[]",
    [], [None], [["a"]], [{"a": 1}], {}, {"a": 1}, {"nodes": "x"},
]


def base_spec():
    return {
        "spec_id": "fuzz",
        "label": "fuzz",
        "goal": "g",
        "nodes": [
            {"node_id": "a", "kind": "implement", "label": "a",
             "model": {"provider_key": "p", "model_id": "m"}},
            {"node_id": "b", "kind": "reduce", "label": "b"},
        ],
        "edges": [{"from_node": "a", "to_node": "b"}],
        "finalizer_node_id": "b",
    }


class TestSpecTypeMatrix:
    """Every spec-level field may be garbage; only OrchestrationError is allowed."""

    @pytest.mark.parametrize("field", [
        "spec_id", "label", "version", "nodes", "edges", "finalizer_node_id",
        "template_id", "goal", "require_confirm_gate", "meta", "goal_contract",
        "actions", "loops",
    ])
    def test_spec_field_garbage(self, field):
        for value in GARBAGE:
            payload = base_spec()
            payload[field] = value
            try:
                OrchestrationSpec.from_value(payload)
            except OrchestrationError:
                continue

    @pytest.mark.parametrize("field", [
        "node_id", "kind", "label", "goal", "prefers", "model", "bind_later",
        "audit_target", "max_audit_retries", "max_auto_recovery", "position",
        "plan_brief", "tools", "deny_tools", "fallback_node_ids", "on_blocked",
        "escalation_model", "loop_policy",
    ])
    def test_node_field_garbage(self, field):
        for value in GARBAGE:
            node = {"node_id": "n", "kind": "implement", "label": "n"}
            node[field] = value
            try:
                OrchestrationNode.from_value(node)
            except OrchestrationError:
                continue

    @pytest.mark.parametrize("factory", [
        ModelBinding.from_value, GoalContract.from_value, LoopPolicy.from_value,
        PlanBrief.from_value, OrchestrationEdge.from_value, SpecAction.from_value,
        LoopContract.from_value,
    ])
    def test_sub_object_garbage(self, factory):
        for value in GARBAGE:
            try:
                factory(value)
            except OrchestrationError:
                continue

    def test_spec_that_parses_still_compiles_or_raises_cleanly(self):
        """Anything from_value accepts must not explode in the graph bridge."""
        for value in GARBAGE:
            payload = base_spec()
            payload["meta"] = value
            try:
                spec = OrchestrationSpec.from_value(payload)
            except OrchestrationError:
                continue
            try:
                to_pipeline_graph(spec)
            except OrchestrationError:
                continue


class TestDiffTruncation:
    DIFF = (
        "diff --git a/paper.md b/paper.md\n"
        "--- a/paper.md\n"
        "+++ b/paper.md\n"
        "@@ -1,4 +1,5 @@ 摘要\n"
        " 论文标题\n"
        "-旧方法\n"
        "+新方法\n"
        "+补充说明\n"
        " 结论\n"
        "@@ -20,3 +21,3 @@ 融合模块\n"
        "-简单相加\n"
        "+跨模态注意力\n"
        " 结尾\n"
    )

    def test_truncation_never_raises_and_keeps_the_shape(self):
        valid_kinds = {"add", "del", "ctx", "note"}
        for cut in range(len(self.DIFF) + 1):
            head = self.DIFF[:cut]
            result = parse_unified_diff(head)
            assert isinstance(result, dict)
            hunks = result["hunks"]
            assert isinstance(hunks, list)
            for hunk in hunks:
                assert isinstance(hunk["lines"], list)
                for line in hunk["lines"]:
                    assert line["kind"] in valid_kinds, (cut, line)
                assert isinstance(hunk["changed"], int)

    def test_line_truncation_never_raises(self):
        lines = self.DIFF.splitlines()
        for cut in range(len(lines) + 1):
            parse_unified_diff("\n".join(lines[:cut]))

    def test_counts_are_consistent_with_the_lines(self):
        for hunk in parse_unified_diff(self.DIFF)["hunks"]:
            changed = [line for line in hunk["lines"] if line["kind"] in {"add", "del"}]
            assert hunk["changed"] == len(changed)


class TestConflictMarkerFuzz:
    def test_random_marker_soup_never_raises(self):
        rng = random.Random(20260910)
        tokens = [
            "<<<<<<< HEAD", "=======", ">>>>>>> theirs", "||||||| base",
            "<<<<<<<<", "========", ">>>>>>>>", "plain text", "", "   ",
            "content > markers", "<<<<<<< ", ">>>>>>> ",
        ]
        for _ in range(400):
            text = [rng.choice(tokens) for _ in range(rng.randint(0, 12))]
            sections = parse_conflict_markers(text)
            assert isinstance(sections, list)
            for section in sections:
                assert set(section) >= {"start_line", "ours", "base", "theirs", "truncated"}
                assert section["start_line"] >= 1
                assert isinstance(section["ours"], list)
                assert isinstance(section["theirs"], list)
                assert len(section["ours"]) <= 40

    def test_balanced_blocks_always_yield_one_section_each(self):
        rng = random.Random(7)
        for _ in range(50):
            parts = []
            expected = 0
            for _ in range(rng.randint(1, 4)):
                parts += ["<<<<<<< ours", "a" * rng.randint(1, 5), "=======",
                          "b" * rng.randint(1, 5), ">>>>>>> theirs", "gap"]
                expected += 1
            assert len(parse_conflict_markers(parts)) == expected


class TestLoopFuzz:
    def test_garbage_loop_dicts_raise_only_orchestration_errors(self):
        nodes, edges = [], []
        for value in GARBAGE:
            try:
                compile_loops(nodes, edges, [value] if not isinstance(value, list) else value)
            except (OrchestrationError, ValueError):
                continue

    def test_loop_budget_boundaries(self):
        from mewcode.orchestration.loops import LOOP_BUDGET_LIMITS, parse_loop_budget

        for name, rule in LOOP_BUDGET_LIMITS.items():
            low = {name: rule["min"]}
            high = {name: rule["max"]}
            assert parse_loop_budget(low)[name] == rule["min"]
            assert parse_loop_budget(high)[name] == rule["max"]
            for bad in (rule["min"] - 1, rule["max"] + 1, "", None, "many", True, float("nan")):
                with pytest.raises(ValueError):
                    parse_loop_budget({name: bad})


class TestReceiptRetention:
    def test_receipts_are_capped_and_the_public_list_matches(self):
        snapshot: dict = {}
        for index in range(MAX_RECEIPTS + 60):
            add_process_file(snapshot, {
                "id": f"r{index:04d}", "node_id": "n", "member_id": "",
                "node_attempt": 1, "tool_id": "t", "name": f"f{index}.md",
                "path": f"/p/f{index}.md", "size": 1, "sha256": "x",
                "created_at": index, "source": "successful_write",
                "accepted": False, "preview_type": "none",
            })
        assert len(snapshot["process_files"]) == MAX_RECEIPTS
        assert len(snapshot["process_file_receipts"]) == MAX_RECEIPTS
        # Newest first out, oldest trimmed; the public list must mirror exactly.
        assert snapshot["process_files"][0]["id"] == "r0060"
        assert snapshot["process_files"][-1]["id"] == f"r{MAX_RECEIPTS + 59:04d}"
        assert [item["id"] for item in snapshot["process_file_receipts"]] == [
            item["id"] for item in snapshot["process_files"]
        ]
        assert all("preview" not in item for item in snapshot["process_file_receipts"])

    def test_duplicate_receipt_id_does_not_grow_the_list(self):
        snapshot: dict = {}
        record = {
            "id": "same", "node_id": "n", "member_id": "", "node_attempt": 1,
            "tool_id": "t", "name": "f.md", "path": "/p/f.md", "size": 1,
            "sha256": "x", "created_at": 1, "source": "successful_write",
            "accepted": False, "preview_type": "none",
        }
        for _ in range(5):
            add_process_file(snapshot, dict(record))
        assert len(snapshot["process_files"]) == 1

    def test_file_receipt_strips_only_the_preview(self):
        record = {"id": "a", "preview": "secret-ish body", "preview_type": "text", "size": 3}
        public = file_receipt(record)
        assert "preview" not in public
        assert public["preview_type"] == "text"
        assert record["preview"] == "secret-ish body", "must not mutate the caller's record"


class TestInterventionBoundaries:
    def snapshot(self):
        # A stopped run rejects supplements by design; these tests are about the
        # text/id/queue boundaries, so start from a running orchestration.
        return {"status": "running"}

    def test_text_length_boundary(self):
        enqueue_intervention(self.snapshot(), "id-1", "a" * 8000)
        for bad in ("", "   ", "b" * 8001):
            with pytest.raises(ValueError):
                enqueue_intervention(self.snapshot(), "id-2", bad)

    def test_identity_must_be_a_safe_token(self):
        for bad in ("", "a b", "中文", "x" * 81, "a/b"):
            with pytest.raises(ValueError):
                enqueue_intervention(self.snapshot(), bad, "text")

    def test_same_id_with_different_text_is_rejected(self):
        snapshot = self.snapshot()
        enqueue_intervention(snapshot, "id-1", "first")
        enqueue_intervention(snapshot, "id-1", "first")
        with pytest.raises(ValueError):
            enqueue_intervention(snapshot, "id-1", "second")

    def test_queue_is_capped(self):
        snapshot = self.snapshot()
        for index in range(32):
            enqueue_intervention(snapshot, f"id-{index}", "text")
        with pytest.raises(ValueError):
            enqueue_intervention(snapshot, "id-99", "text")


class TestPlanSessionBoundaries:
    def test_building_on_a_previous_session_of_another_spec_keeps_the_session_id(self):
        from mewcode.orchestration.plan_session import build_plan_session

        first_spec = OrchestrationSpec.from_value(base_spec())
        session = build_plan_session(first_spec, goal="g")
        other = base_spec()
        other["spec_id"] = "other"
        other["nodes"][0]["label"] = "renamed"
        second_spec = OrchestrationSpec.from_value(other)
        rebuilt = build_plan_session(second_spec, goal="g2", previous=session)
        assert rebuilt.session_id == session.session_id
        assert rebuilt.status in {"drafting", "binding", "ready", "running"}
        assert rebuilt.turn_summaries[-1].turn == 2

    def test_activate_and_confirmed_combinations_are_stable(self):
        from mewcode.orchestration.plan_session import build_plan_session

        spec = OrchestrationSpec.from_value(base_spec())
        for confirmed in (False, True):
            for activate in (False, True):
                session = build_plan_session(spec, goal="g", confirmed=confirmed, activate=activate)
                assert session.status in {"drafting", "binding", "ready", "running"}
                assert json.dumps(session.as_dict(), ensure_ascii=False)
