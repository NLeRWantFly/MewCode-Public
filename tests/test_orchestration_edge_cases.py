"""Adversarial boundary and exception-path tests for the orchestration path.

Each test states the behaviour a caller can rely on. A crash with TypeError /
ValueError / IndexError / StopIteration is a bug even when the input is garbage,
because these entry points are reachable from the canvas and from the chat
pipeline with data the user controls.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mewcode.orchestration import (
    OrchestrationSpec,
    from_pipeline_graph,
    to_pipeline_graph,
    validate_spec,
)
from mewcode.orchestration.change_explain import (
    node_change_report,
    parse_unified_diff,
)
from mewcode.orchestration.pipeline_bridge import END_ID, START_ID
from mewcode.orchestration.process_files import capture_process_file
from mewcode.orchestration.spec import OrchestrationError
from mewcode.pipeline.compiler import PipelineCompiler
from mewcode.pipeline.models import GraphEdge, GraphNode, NodeType, PipelineGraph


def spec_value(**overrides):
    value = {
        "spec_id": "edge",
        "label": "边界",
        "goal": "边界用例",
        "nodes": [
            {
                "node_id": "a",
                "kind": "implement",
                "label": "A",
                "model": {"provider_key": "p", "model_id": "m", "effort": "high"},
            },
            {
                "node_id": "b",
                "kind": "implement",
                "label": "B",
                "model": {"provider_key": "p", "model_id": "m", "effort": "high"},
            },
        ],
        "edges": [{"from_node": "a", "to_node": "b"}],
        "finalizer_node_id": "b",
    }
    value.update(overrides)
    return value


def graph_from(value=None) -> PipelineGraph:
    spec = OrchestrationSpec.from_value(value or spec_value())
    graph = to_pipeline_graph(spec)
    assert PipelineCompiler.validate(graph).valid
    return graph


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "tester")
    git(repo, "config", "commit.gpgsign", "false")


# --------------------------------------------------------------------------
# spec parsing
# --------------------------------------------------------------------------

class TestSpecBoundaries:
    def test_nodes_must_be_a_list_not_a_mapping(self):
        with pytest.raises(OrchestrationError):
            OrchestrationSpec.from_value(spec_value(nodes={"a": {"node_id": "a"}}))

    def test_nodes_must_be_a_list_not_a_string(self):
        with pytest.raises(OrchestrationError):
            OrchestrationSpec.from_value(spec_value(nodes="ab"))

    def test_node_must_be_an_object(self):
        with pytest.raises(OrchestrationError):
            OrchestrationSpec.from_value(spec_value(nodes=["a", None]))

    def test_node_without_an_id_is_rejected(self):
        with pytest.raises(OrchestrationError):
            OrchestrationSpec.from_value(spec_value(nodes=[{"kind": "implement"}]))

    def test_duplicate_node_ids_are_rejected(self):
        duplicated = spec_value()
        duplicated["nodes"].append(dict(duplicated["nodes"][0]))
        spec = OrchestrationSpec.from_value(duplicated)
        with pytest.raises(OrchestrationError, match="duplicate node_id"):
            validate_spec(spec, require_bindings=False)

    def test_non_numeric_version_is_an_orchestration_error(self):
        with pytest.raises(OrchestrationError):
            OrchestrationSpec.from_value(spec_value(version="abc"))

    def test_edges_must_be_objects(self):
        with pytest.raises(OrchestrationError):
            OrchestrationSpec.from_value(spec_value(edges=[["a", "b"]]))

    def test_garbage_meta_does_not_break_parsing(self):
        spec = OrchestrationSpec.from_value(spec_value(meta=["nope"], loops="nope"))
        assert isinstance(spec.meta, dict)

    def test_dangling_edge_is_rejected(self):
        spec = OrchestrationSpec.from_value(
            spec_value(edges=[{"from_node": "a", "to_node": "ghost"}])
        )
        with pytest.raises(OrchestrationError, match="ghost"):
            validate_spec(spec, require_bindings=False)

    def test_undeclared_cycle_is_rejected_with_a_loop_hint(self):
        spec = OrchestrationSpec.from_value(
            spec_value(
                edges=[
                    {"from_node": "a", "to_node": "b"},
                    {"from_node": "b", "to_node": "a"},
                ]
            )
        )
        with pytest.raises(OrchestrationError) as excinfo:
            validate_spec(spec, require_bindings=False)
        assert "loop" in str(excinfo.value), "错误信息要告诉用户怎么声明循环"

    def test_declared_loop_back_edge_is_legal(self):
        """A loop back edge is a cycle by design and must stay accepted."""
        spec = OrchestrationSpec.from_value(
            spec_value(
                nodes=[
                    {"node_id": "work", "kind": "implement", "label": "work",
                     "model": {"provider_key": "p", "model_id": "m"}},
                    {"node_id": "check", "kind": "decision", "label": "check"},
                    {"node_id": "done", "kind": "reduce", "label": "done"},
                ],
                edges=[
                    {"from_node": "work", "to_node": "check"},
                    {"from_node": "check", "to_node": "work", "condition": "loop"},
                    {"from_node": "check", "to_node": "done", "condition": "pass"},
                ],
                finalizer_node_id="done",
                meta={"loops": [{
                    "loop_id": "improve", "entry_node_id": "work", "check_node_id": "check",
                    "node_ids": ["work", "check"], "exit_condition": "验证通过",
                    "criteria": [{"criterion_id": "R1", "acceptance_test": "结果通过验证"}],
                }]},
            )
        )
        assert validate_spec(spec, require_bindings=False) is not None
        assert PipelineCompiler.validate(to_pipeline_graph(spec)).valid


# --------------------------------------------------------------------------
# canvas graph -> spec
# --------------------------------------------------------------------------

class TestGraphToSpec:
    def test_graph_without_stage_nodes_is_a_clear_error(self):
        graph = PipelineGraph(
            graph_id="g_empty",
            name="empty",
            nodes=[GraphNode(id=START_ID, type=NodeType.START), GraphNode(id=END_ID, type=NodeType.END)],
            edges=[GraphEdge(id="e1", from_node=START_ID, to_node=END_ID)],
            variables={"__orch.payload__": json.dumps(spec_value())},
        )
        with pytest.raises(OrchestrationError):
            from_pipeline_graph(graph)

    def test_present_but_broken_payload_is_a_clear_error(self):
        """Silently rebuilding here would drop every binding and plan brief."""
        graph = graph_from()
        for payload in ["not json", "{broken", json.dumps(["list"]), json.dumps("str")]:
            graph.variables["__orch.payload__"] = payload
            with pytest.raises(OrchestrationError) as excinfo:
                from_pipeline_graph(graph)
            assert "payload" in str(excinfo.value)

    def test_absent_payload_still_rebuilds_for_older_canvases(self):
        graph = graph_from()
        graph.variables["__orch.payload__"] = ""
        spec = from_pipeline_graph(graph)
        assert [node.node_id for node in spec.nodes] == ["a", "b"]

    def test_payload_with_garbage_node_meta_does_not_crash(self):
        graph = graph_from()
        payload = json.loads(graph.variables["__orch.payload__"])
        payload["nodes"]["a"].update(
            {
                "position": "not-a-point",
                "canvas_size": "wide",
                "tools": ["WriteFile"],
                "fallback_node_ids": [None, "  ", "b"],
                "max_audit_retries": "7",
                "max_auto_recovery": "2",
                "loop_policy": "nope",
            }
        )
        graph.variables["__orch.payload__"] = json.dumps(payload, ensure_ascii=False)
        spec = from_pipeline_graph(graph)
        assert [node.node_id for node in spec.nodes] == ["a", "b"]
        assert spec.node_map()["a"].max_audit_retries == 7
        # null / blank entries in a list field must be dropped, never kept as "None"
        assert spec.node_map()["a"].fallback_node_ids == ("b",)

    def test_tools_as_a_string_is_rejected_with_a_readable_error(self):
        """Invalid is fine; crashing with an untyped error is not."""
        graph = graph_from()
        payload = json.loads(graph.variables["__orch.payload__"])
        payload["nodes"]["a"]["tools"] = "WriteFile"
        graph.variables["__orch.payload__"] = json.dumps(payload, ensure_ascii=False)
        with pytest.raises(ValueError, match="tools must be a list"):
            from_pipeline_graph(graph)

    def test_non_numeric_meta_int_is_an_orchestration_error(self):
        graph = graph_from()
        payload = json.loads(graph.variables["__orch.payload__"])
        payload["nodes"]["a"]["max_audit_retries"] = "many"
        graph.variables["__orch.payload__"] = json.dumps(payload, ensure_ascii=False)
        with pytest.raises(OrchestrationError, match="max_audit_retries"):
            from_pipeline_graph(graph)

    def test_duplicate_stage_ids_in_graph_do_not_silently_merge(self):
        graph = graph_from()
        first = [node for node in graph.nodes if node.id == "a"][0]
        graph.nodes.append(GraphNode(id="a", type=first.type, title="duplicate"))
        with pytest.raises(OrchestrationError, match="重复"):
            from_pipeline_graph(graph)

    def test_self_loop_edge_is_dropped(self):
        graph = graph_from()
        graph.edges.append(GraphEdge(id="e_self", from_node="a", to_node="a"))
        spec = from_pipeline_graph(graph)
        assert all(
            not (edge.from_node == edge.to_node == "a") for edge in spec.edges
        ), "a self loop would make the scheduler spin"
        validate_spec(spec, require_bindings=False)

    def test_edge_to_unknown_node_is_dropped(self):
        graph = graph_from()
        graph.edges.append(GraphEdge(id="e_ghost", from_node="a", to_node="ghost"))
        spec = from_pipeline_graph(graph)
        assert all(edge.to_node != "ghost" for edge in spec.edges)


# --------------------------------------------------------------------------
# diff parsing
# --------------------------------------------------------------------------

class TestDiffParsing:
    def test_empty_diff_is_empty(self):
        assert parse_unified_diff("")["hunks"] == []

    def test_no_trailing_newline_marker_is_a_note(self):
        diff = (
            "diff --git a/x.md b/x.md\n"
            "--- a/x.md\n"
            "+++ b/x.md\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "\\ No newline at end of file\n"
            "+new\n"
        )
        hunks = parse_unified_diff(diff)["hunks"]
        assert len(hunks) == 1
        assert [line["kind"] for line in hunks[0]["lines"]] == ["del", "note", "add"]

    def test_mode_only_change_has_no_hunks(self):
        diff = (
            "diff --git a/x.sh b/x.sh\n"
            "old mode 100644\n"
            "new mode 100755\n"
        )
        assert parse_unified_diff(diff)["hunks"] == []

    def test_combined_merge_diff_does_not_crash_and_reports_nothing_parsed(self):
        combined = (
            "diff --cc paper.md\n"
            "index 1111111,2222222..3333333\n"
            "--- a/paper.md\n"
            "+++ b/paper.md\n"
            "@@@ -1,1 -1,1 +1,1 @@@\n"
            "++new line\n"
        )
        assert parse_unified_diff(combined)["hunks"] == []

    def test_hunk_header_without_counts_is_read_as_one_line(self):
        diff = (
            "diff --git a/x.md b/x.md\n"
            "--- a/x.md\n"
            "+++ b/x.md\n"
            "@@ -7 +9 @@\n"
            "-old\n"
            "+new\n"
        )
        hunks = parse_unified_diff(diff)["hunks"]
        assert hunks[0]["old_range"] == "7"
        assert hunks[0]["new_range"] == "9"

    def test_crlf_lines_do_not_leak_carriage_returns(self):
        diff = (
            "diff --git a/x.md b/x.md\r\n"
            "--- a/x.md\r\n"
            "+++ b/x.md\r\n"
            "@@ -1 +1 @@\r\n"
            "-old\r\n"
            "+new\r\n"
        )
        hunks = parse_unified_diff(diff)["hunks"]
        assert hunks[0]["lines"][-1]["text"] == "new"


# --------------------------------------------------------------------------
# change report exception paths
# --------------------------------------------------------------------------

class TestChangeReportPaths:
    @pytest.mark.asyncio
    async def test_unknown_commit_ref_is_reported_not_raised(self, tmp_path):
        repo = tmp_path / "repo"
        init_repo(repo)
        (repo / "a.md").write_text("x\n", encoding="utf-8")
        git(repo, "add", "a.md")
        git(repo, "commit", "-qm", "a")
        report = await node_change_report(str(repo), [], commit="deadbeefdeadbeef")
        assert report["is_repo"] is True
        assert report["errors"]

    @pytest.mark.asyncio
    async def test_receipt_for_deleted_file_is_handled(self, tmp_path):
        repo = tmp_path / "repo"
        init_repo(repo)
        (repo / "gone.md").write_text("x\n", encoding="utf-8")
        git(repo, "add", "gone.md")
        git(repo, "commit", "-qm", "a")
        (repo / "gone.md").unlink()
        report = await node_change_report(str(repo), [{"node_id": "n", "path": "gone.md"}])
        assert [file["path"] for file in report["nodes"]["n"]["files"]] == ["gone.md"]

    @pytest.mark.asyncio
    async def test_receipt_path_outside_the_project_never_leaks_content(self, tmp_path):  # noqa: D401
        repo = tmp_path / "repo"
        init_repo(repo)
        (repo / "a.md").write_text("inside\n", encoding="utf-8")
        git(repo, "add", "a.md")
        git(repo, "commit", "-qm", "a")
        outside = tmp_path / "secret.txt"
        outside.write_text("TOP-SECRET-CONTENT\n", encoding="utf-8")
        report = await node_change_report(str(repo), [{"node_id": "n", "path": str(outside)}])
        blob = json.dumps(report, ensure_ascii=False)
        assert "TOP-SECRET-CONTENT" not in blob
        assert "n" not in report["nodes"], "项目外的路径不应该成为节点的改动文件"

    @pytest.mark.asyncio
    async def test_write_landed_in_a_merge_commit_still_shows_hunks(self, tmp_path):
        """A merge commit used to yield a combined diff ("@@@") that parses to
        nothing, so the file looked untouched."""
        repo = tmp_path / "repo"
        init_repo(repo)
        paper = repo / "paper.md"
        paper.write_text("base\n", encoding="utf-8")
        git(repo, "add", "paper.md")
        git(repo, "commit", "-qm", "base")
        git(repo, "checkout", "-q", "-b", "side")
        paper.write_text("side\n", encoding="utf-8")
        git(repo, "commit", "-aqm", "side edit")
        git(repo, "checkout", "-q", "-")
        paper.write_text("main\n", encoding="utf-8")
        git(repo, "commit", "-aqm", "main edit")
        merge = git(repo, "merge", "--no-commit", "side")
        assert merge.returncode != 0, "the fixture needs a real conflict"
        paper.write_text("merged by hand\n", encoding="utf-8")  # the merge resolution
        git(repo, "add", "paper.md")
        git(repo, "commit", "--no-edit")
        parents = git(repo, "log", "-1", "--format=%P %H").stdout.split()
        assert len(parents) == 3, "the resolution must be recorded in a merge commit"
        sha = parents[-1]
        # Proof the fix matters: the plain diff of a merge is a combined one.
        naive = git(repo, "show", "--format=", sha, "--", "paper.md").stdout
        assert "@@@" in naive, "fixture no longer produces a combined diff"

        report = await node_change_report(str(repo), [{"node_id": "n", "path": "paper.md"}])
        files = report["nodes"]["n"]["files"]
        assert files and files[0]["hunks"], "合并提交里的写入必须仍然能看到逐行改动"
        text = json.dumps(files[0]["hunks"], ensure_ascii=False)
        assert "merged by hand" in text

    @pytest.mark.asyncio
    async def test_paths_with_spaces_and_unicode_are_matched(self, tmp_path):
        repo = tmp_path / "repo"
        init_repo(repo)
        name = "论文 草稿 v2.md"
        (repo / name).write_text("x\n", encoding="utf-8")
        git(repo, "add", name)
        git(repo, "commit", "-qm", "a")
        (repo / name).write_text("x\ny\n", encoding="utf-8")
        report = await node_change_report(str(repo), [{"node_id": "n", "path": name}])
        files = report["nodes"]["n"]["files"]
        assert [file["path"] for file in files] == [name]
        assert files[0]["dirty"] == "dirty"
        assert files[0]["hunks"], "an uncommitted edit must still produce hunks"


# --------------------------------------------------------------------------
# process-file capture boundaries
# --------------------------------------------------------------------------

class TestProcessFileCapture:
    class Sandbox:
        def check(self, path, read_only=False):
            return (True, "")

    def capture(self, work_dir: Path, target, **kwargs):
        return capture_process_file(
            tool_name="WriteFile",
            arguments={"file_path": str(target)},
            is_error=False,
            work_dir=str(work_dir),
            sandbox=self.Sandbox(),
            node_id="n",
            tool_id="t",
            **kwargs,
        )

    def test_missing_arguments_returns_none(self, tmp_path):
        assert capture_process_file(
            tool_name="WriteFile", arguments=None, is_error=False,
            work_dir=str(tmp_path), sandbox=self.Sandbox(), node_id="n", tool_id="t",
        ) is None

    def test_missing_sandbox_returns_none(self, tmp_path):
        target = tmp_path / "a.md"
        target.write_text("x", encoding="utf-8")
        assert capture_process_file(
            tool_name="WriteFile", arguments={"file_path": str(target)}, is_error=False,
            work_dir=str(tmp_path), sandbox=None, node_id="n", tool_id="t",
        ) is None

    def test_directory_is_not_captured(self, tmp_path):
        assert self.capture(tmp_path, tmp_path) is None

    def test_failed_tool_is_not_captured(self, tmp_path):
        target = tmp_path / "a.md"
        target.write_text("x", encoding="utf-8")
        assert capture_process_file(
            tool_name="WriteFile", arguments={"file_path": str(target)}, is_error=True,
            work_dir=str(tmp_path), sandbox=self.Sandbox(), node_id="n", tool_id="t",
        ) is None

    def test_binary_file_records_metadata_only(self, tmp_path):
        target = tmp_path / "a.bin"
        target.write_bytes(b"\x00\x01\x02binary")
        record = self.capture(tmp_path, target)
        assert record is not None
        assert record["preview_type"] == "none"
        assert "preview" not in record

    def test_oversized_file_keeps_size_but_drops_the_digest(self, tmp_path, monkeypatch):
        from mewcode.orchestration import process_files

        monkeypatch.setattr(process_files, "MAX_FILE_BYTES", 16)
        target = tmp_path / "big.md"
        target.write_text("x" * 64, encoding="utf-8")
        record = self.capture(tmp_path, target)
        assert record["sha256"] == ""
        assert record["size"] == 64
        assert len(record.get("preview") or "") <= process_files.MAX_PREVIEW_BYTES

    def test_utf8_bom_is_stripped(self, tmp_path):
        target = tmp_path / "a.md"
        target.write_bytes("标题\n".encode("utf-8-sig"))
        record = self.capture(tmp_path, target)
        assert record["preview_type"] == "text"
        assert record["preview"].startswith("标题")

    def test_receipt_identity_is_stable_for_the_same_write(self, tmp_path):
        target = tmp_path / "a.md"
        target.write_text("x", encoding="utf-8")
        first = self.capture(tmp_path, target)
        second = self.capture(tmp_path, target)
        assert first["id"] == second["id"]
        assert self.capture(tmp_path, target, attempt=2)["id"] != first["id"]
        assert self.capture(tmp_path, target, member_id="m1")["id"] != first["id"]
