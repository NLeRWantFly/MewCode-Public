from dataclasses import FrozenInstanceError
import os

import pytest

from mewcode.permissions.effects import EffectSignature, effect_signature


def _effect(tool_name, arguments, root, tool=None):
    return effect_signature(tool_name, arguments, work_dir=root, tool=tool)


def _path_target(path):
    return "path:" + os.path.normcase(os.path.normpath(str(path.resolve())))


def test_file_tools_share_normalized_targets_across_tool_names(tmp_path):
    first = _effect("WriteFile", {"file_path": "src/./module.py", "content": "one"}, tmp_path)
    alias = _effect("EditFile", {"file_path": "src/sub/../module.py", "old_string": "x", "new_string": "y"}, tmp_path)
    read = _effect("ReadFile", {"file_path": "src/module.py"}, tmp_path)
    artifact = _effect("ReportArtifact", {"file_path": "src/module.py"}, tmp_path)

    assert first.kind == alias.kind == "write"
    assert first.targets == alias.targets == read.targets == artifact.targets
    assert first.key == alias.key
    assert read.kind == artifact.kind == "read"
    assert read.key == artifact.key
    assert not first.overlaps(read)
    assert first.reversible == "unknown"
    with pytest.raises(FrozenInstanceError):
        first.kind = "delete"


def test_path_overlap_covers_parent_child_and_write_delete_but_not_siblings(tmp_path):
    output_dir = tmp_path / "build"
    child = output_dir / "nested" / "report.txt"
    sibling = tmp_path / "logs" / "report.txt"
    write_child = _effect("WriteFile", {"file_path": str(child)}, tmp_path)
    delete_parent = _effect("Bash", {"command": f'rm "{output_dir}"'}, tmp_path)
    delete_child = _effect("Bash", {"command": f'rm "{child}"'}, tmp_path)
    write_sibling = _effect("EditFile", {"file_path": str(sibling)}, tmp_path)
    read_parent = _effect("ReadFile", {"file_path": str(output_dir)}, tmp_path)

    assert delete_parent.kind == delete_child.kind == "delete"
    assert write_child.overlaps(delete_parent)
    assert delete_parent.overlaps(write_child)
    assert write_child.overlaps(delete_child)
    assert not write_child.overlaps(write_sibling)
    assert not read_parent.overlaps(write_child)


@pytest.mark.parametrize(
    "command,kind",
    [
        ('rm "tmp/file.txt"', "delete"),
        ('unlink "tmp/file.txt"', "delete"),
        ('Remove-Item -LiteralPath "tmp/file.txt"', "delete"),
        ('del "tmp/file.txt"', "delete"),
        ('truncate -s 0 "tmp/file.txt"', "write"),
        ('Get-Content -LiteralPath "tmp/file.txt"', "read"),
        ('cat "tmp/file.txt"', "read"),
    ],
)
def test_only_simple_complete_shell_file_operations_get_bounded_targets(tmp_path, command, kind):
    signature = _effect("Bash", {"command": command}, tmp_path)
    assert signature.kind == kind
    assert not signature.uncertain
    assert signature.targets == (_path_target(tmp_path / "tmp/file.txt"),)
    assert signature.reversible == "unknown"


def test_runverification_uses_same_narrow_shell_effect_parser(tmp_path):
    signature = _effect("RunVerification", {"command": 'cat "evidence.txt"'}, tmp_path)
    assert signature.kind == "read"
    assert signature.targets == (_path_target(tmp_path / "evidence.txt"),)


@pytest.mark.parametrize(
    "command",
    [
        "rm -r output",
        "rm output && cat output",
        "cat output > result.txt",
        "python inspect.py",
        "Remove-Item -Path output -Force",
        "git push --force origin main",
        "cat $(Resolve-Path output)",
    ],
)
def test_shell_chains_substitutions_scripts_and_unsupported_options_are_unbounded(tmp_path, command):
    signature = _effect("Bash", {"command": command}, tmp_path)
    write = _effect("WriteFile", {"file_path": str(tmp_path / "unrelated.txt")}, tmp_path)
    assert signature.kind == "exec_unbounded"
    assert signature.uncertain
    assert signature.blast == "unknown"
    assert signature.overlaps(write)


def test_unbounded_key_collapses_only_unquoted_whitespace(tmp_path):
    one = _effect("Bash", {"command": 'python   "a  b.py"'}, tmp_path)
    same_spacing = _effect("Bash", {"command": 'python "a  b.py"'}, tmp_path)
    different_quoted_semantics = _effect("Bash", {"command": 'python "a b.py"'}, tmp_path)
    unmatched_one = _effect("Bash", {"command": 'python  "unclosed'}, tmp_path)
    unmatched_two = _effect("Bash", {"command": 'python "unclosed'}, tmp_path)

    assert one.key == same_spacing.key
    assert one.key != different_quoted_semantics.key
    assert unmatched_one.key != unmatched_two.key
    assert "python" not in repr(one.as_dict())


def test_git_push_is_external_repository_effect_and_never_claims_reversibility(tmp_path):
    signature = _effect("Bash", {"command": "git push origin main"}, tmp_path)
    assert signature.kind == "net_egress"
    assert signature.blast == "repository"
    assert signature.targets == ("host:*", _path_target(tmp_path))
    assert signature.reversible == "unknown"
    assert not signature.uncertain


def test_browser_urls_retain_only_host_and_port_not_credentials_or_query(tmp_path):
    url = "https://alice:super-secret@example.test:9443/reports/file.bin?access_token=secret#frag"
    opened = _effect("Browser", {"action": "open", "url": url}, tmp_path)
    downloaded = _effect("Browser", {"action": "download", "url": url, "path": "cache/file.bin"}, tmp_path)

    assert opened.kind == "net_egress"
    assert opened.targets == ("host:example.test:9443",)
    assert downloaded.kind == "write"
    assert downloaded.targets == (
        "host:example.test:9443", _path_target(tmp_path / "cache" / "file.bin")
    )
    assert downloaded.blast == "external"
    rendered = repr(opened.as_dict()) + repr(downloaded.as_dict()) + opened.key + downloaded.key
    assert "alice" not in rendered
    assert "super-secret" not in rendered
    assert "access_token" not in rendered
    assert "secret" not in rendered


def test_host_permission_files_are_authority_changes_only_when_written(tmp_path):
    policy_dir = tmp_path / ".mewcode"
    permission_file = policy_dir / "permissions.yaml"
    local_file = policy_dir / "permissions.local.yaml"
    grant_file = policy_dir / "permissions.grants.json"
    rule_file = policy_dir / "rules" / "workspace.rules"

    for path in (permission_file, local_file, grant_file, rule_file):
        written = _effect("WriteFile", {"file_path": str(path)}, tmp_path)
        edited = _effect("EditFile", {"file_path": str(path)}, tmp_path)
        read = _effect("ReadFile", {"file_path": str(path)}, tmp_path)
        assert written.kind == edited.kind == "authority_change"
        assert read.kind == "read"
        assert written.overlaps(_effect("WriteFile", {"file_path": str(tmp_path / "other.txt")}, tmp_path))

    ordinary_business_file = _effect("WriteFile", {"file_path": str(tmp_path / "docs" / "permissions.yaml")}, tmp_path)
    assert ordinary_business_file.kind == "write"


def test_git_tracking_does_not_make_a_file_write_reversible(tmp_path):
    tracked_looking_file = tmp_path / "tracked.py"
    tracked_looking_file.write_text("already exists", encoding="utf-8")
    signature = _effect("WriteFile", {"file_path": str(tracked_looking_file)}, tmp_path)
    assert signature.kind == "write"
    assert signature.blast == "file"
    assert signature.reversible == "unknown"


def test_unknown_tool_is_uncertain_even_if_it_claims_read_only(tmp_path):
    class ApparentlyReadOnly:
        is_read_only = True

        def permission_risk(self, _arguments):
            from mewcode.tools.base import ToolRiskProfile
            return ToolRiskProfile()

    unknown = _effect("MysteryTool", {"file_path": str(tmp_path / "x")}, tmp_path, ApparentlyReadOnly())
    ordinary_write = _effect("WriteFile", {"file_path": str(tmp_path / "other")}, tmp_path)
    assert unknown.kind == "exec_unbounded"
    assert unknown.uncertain
    assert unknown.overlaps(ordinary_write)


def test_explicit_browser_download_is_not_made_read_only_by_tool_category(tmp_path):
    class ReadCategoryBrowser:
        _current_url = ""

    signature = _effect("Browser", {
        "action": "download", "url": "https://example.test/archive.zip", "path": "archive.zip",
    }, tmp_path, ReadCategoryBrowser())
    assert signature.kind == "write"


@pytest.mark.skipif(os.name != "nt", reason="PowerShell command-mode expressions are Windows-specific")
@pytest.mark.parametrize(
    "command",
    [
        "Remove-Item a,b",
        "rm a,b",
        "cat (Remove-Item('victim'))",
        "cat @(Remove-Item 'victim')",
        "cat @pathArgs",
        "rm `a",
    ],
)
def test_unquoted_powershell_arrays_and_expressions_are_unbounded(tmp_path, command):
    signature = _effect("Bash", {"command": command}, tmp_path)

    assert signature.kind == "exec_unbounded"
    assert signature.uncertain
    assert signature.blast == "unknown"


@pytest.mark.skipif(os.name != "nt", reason="PowerShell command-mode expressions are Windows-specific")
def test_quoted_comma_is_a_literal_path_not_a_powershell_array(tmp_path):
    signature = _effect("Bash", {"command": "Remove-Item 'a,b'"}, tmp_path)

    assert signature.kind == "delete"
    assert signature.targets == (_path_target(tmp_path / "a,b"),)


def test_webfetch_is_classified_as_network_egress_with_host_only_target(tmp_path):
    signature = _effect("WebFetch", {
        "url": "https://user:secret@example.test:8443/path?token=hidden",
    }, tmp_path)

    assert signature.kind == "net_egress"
    assert signature.targets == ("host:example.test:8443",)
    assert "secret" not in repr(signature.as_dict())
    assert "token" not in repr(signature.as_dict())


@pytest.mark.parametrize("tool_name", ["ResolveAutoReview", "AskUserQuestion"])
def test_host_control_tools_are_read_effects_disjoint_from_denied_writes(tmp_path, tool_name):
    control = _effect(tool_name, {}, tmp_path)
    denied_write = _effect("WriteFile", {"file_path": str(tmp_path / "denied.txt")}, tmp_path)

    assert control.kind == "read"
    assert not control.uncertain
    assert not control.overlaps(denied_write)


@pytest.mark.parametrize("name,arguments,kind", [
    ("ReadWorkflowBlocks", {}, "read"),
    ("AdvanceTaskDesign", {"action": "read"}, "read"),
    ("AdvanceTaskDesign", {"action": "build"}, "write"),
    ("SubmitWorkflowBlocks", {"expected_sequence": 0, "source_hash": "", "base_graph_hash": "",
                              "use_jev": False}, "write"),
])
def test_builtin_workflow_contract_classifies_the_exact_stage(tmp_path, name, arguments, kind):
    from mewcode.orchestration.workflow_blocks import WorkflowBlocks
    from mewcode.tools.task_design import AdvanceTaskDesign
    from mewcode.tools.workflow_blocks import ReadWorkflowBlocks, SubmitWorkflowBlocks
    from mewcode.permissions.effects import workflow_design_contract
    tools = {t.name: t(WorkflowBlocks(tmp_path, "fixture"))
             for t in (ReadWorkflowBlocks, AdvanceTaskDesign, SubmitWorkflowBlocks)}
    signature = _effect(name, arguments, tmp_path, tools[name])
    assert signature.kind == kind and not signature.uncertain
    assert signature.blast == "session" and signature.reversible == "unknown"
    assert any(target.startswith("session:") and target.endswith("/fixture") for target in signature.targets)
    contract = workflow_design_contract(name, arguments, work_dir=tmp_path, tool=tools[name])
    if arguments.get("action") == "build":
        assert "model" in contract["later_phases"] and "cost" in contract["later_phases"]


@pytest.mark.parametrize("invalid", ["plugin", "subclass", "workspace", "session", "guard", "source", "action", "params"])
def test_workflow_contract_cannot_be_claimed_by_an_invalid_tool_or_binding(tmp_path, invalid):
    from types import SimpleNamespace
    from mewcode.orchestration.workflow_blocks import WorkflowBlocks
    from mewcode.tools.task_design import AdvanceTaskDesign
    blocks = WorkflowBlocks(tmp_path, "fixture")
    arguments = {"action": "read"}
    tool = AdvanceTaskDesign(blocks)
    if invalid == "plugin":
        tool = SimpleNamespace(name=tool.name, blocks=blocks, category="read")
    elif invalid == "subclass":
        class FakeBuiltin(AdvanceTaskDesign):
            pass
        tool = FakeBuiltin(blocks)
    elif invalid == "workspace":
        blocks.project = str(tmp_path / "other")
    elif invalid == "session":
        blocks.conversation_id = "../other"
    elif invalid == "guard":
        blocks.guard = lambda: False
    elif invalid == "source":
        blocks.source_path = tmp_path.parent / "fixture.jsonl"
    elif invalid == "action":
        arguments = {"action": "execute"}
    else:
        arguments = {"action": "read", "unexpected": True}
    signature = _effect("AdvanceTaskDesign", arguments, tmp_path, tool)
    assert signature.kind == "exec_unbounded" and signature.uncertain


def test_workflow_submission_keeps_its_optional_model_network_effect(tmp_path):
    from types import SimpleNamespace
    from mewcode.orchestration.workflow_blocks import WorkflowBlocks
    from mewcode.tools.workflow_blocks import SubmitWorkflowBlocks
    from mewcode.permissions.effects import workflow_design_contract
    tool = SubmitWorkflowBlocks(WorkflowBlocks(tmp_path, "fixture"), judgment_runtime=SimpleNamespace())
    args = {"expected_sequence": 0, "source_hash": "", "base_graph_hash": "", "use_jev": True}
    signature = _effect(tool.name, args, tmp_path, tool)
    contract = workflow_design_contract(tool.name, args, work_dir=tmp_path, tool=tool)
    assert signature.kind == "write" and signature.blast == "external" and "host:*" in signature.targets
    assert contract["auxiliary_model_request"] is True
