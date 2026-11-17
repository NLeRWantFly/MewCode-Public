"""Typed runtime inputs and their resolved stage-prompt representation."""
import pytest

from mewcode.orchestration.run_inputs import (
    append_resolved_inputs,
    append_run_inputs,
    resolve_run_inputs,
    stage_input_questions,
    validate_branch_contract,
    validate_input_contract,
)


def test_list_inputs_normalize_arrays_strings_and_empty_items():
    declaration = {"name": "pdf_paths", "type": "list", "required": True}
    assert resolve_run_inputs([declaration], {"pdf_paths": [" a.pdf ", "", "b.pdf"]}) == {
        "pdf_paths": ["a.pdf", "b.pdf"]
    }
    assert resolve_run_inputs([declaration], {"pdf_paths": "a.pdf\n \nb.pdf"}) == {
        "pdf_paths": ["a.pdf", "b.pdf"]
    }
    assert resolve_run_inputs([dict(declaration, separator="|")], {"pdf_paths": "a.pdf| |b.pdf"}) == {
        "pdf_paths": ["a.pdf", "b.pdf"]
    }
    assert '"pdf_paths": ["a.pdf", "b.pdf"]' in append_run_inputs(
        "运行", {"pdf_paths": ["a.pdf", "b.pdf"]}
    )


def test_required_list_must_contain_an_item_and_deferred_list_questions_preserve_arrays():
    declaration = {"name": "files", "type": "list", "required": True, "collect_at": "review"}
    assert resolve_run_inputs([declaration], {}) == {"files": []}
    with pytest.raises(ValueError, match="files"):
        resolve_run_inputs([declaration], {}, node_id="review")
    questions, normalized = stage_input_questions([declaration], {"files": [" a ", "b"]}, "review")
    assert questions == [] and normalized == {"files": ["a", "b"]}


def test_input_type_contract_supports_legacy_multiline_and_rejects_bad_combinations():
    declaration = {"name": "brief", "default": "first line\nsecond line"}
    assert resolve_run_inputs([declaration], {}) == {"brief": "first line\nsecond line"}
    with pytest.raises(ValueError, match="类型无效"):
        validate_input_contract([{"name": "bad", "type": "json"}])
    with pytest.raises(ValueError, match="分隔符"):
        validate_input_contract([{"name": "bad", "type": "text", "separator": "|"}])
    with pytest.raises(ValueError, match="值无效"):
        resolve_run_inputs([{"name": "short", "type": "text"}], {"short": "two\nlines"})
    with pytest.raises(ValueError, match="控制参数"):
        validate_input_contract([
            {"name": "files", "type": "list"},
            {"name": "enabled", "required_when": {"input": "files", "equals": "x"}},
        ])
    with pytest.raises(ValueError, match="分支控制参数"):
        validate_branch_contract(
            [{"from_node": "start", "to_node": "next", "condition": "pass",
              "input_when": {"input": "files", "equals": "x"}}],
            [{"name": "files", "type": "list"}], node_ids={"start", "next"},
        )


def test_resolved_prompt_lists_runtime_values_artifacts_and_replaced_placeholders():
    declarations = [
        {"name": "pdf_paths", "label": "PDF 文件路径", "type": "list"},
        {"name": "mode", "type": "text"},
    ]
    values = {"pdf_paths": [r"C:\inputs\a.pdf", r"C:\inputs\b.pdf", r"C:\inputs\c.pdf", r"C:\inputs\d.pdf"],
              "mode": "survey"}
    upstream_sources = {"N01": {"node_id": "N01", "handoff": {"artifacts": [
        {"path": r"C:\runs\survey.json", "kind": "json", "note": ""},
    ]}}}
    prompt = append_resolved_inputs(
        "逐项审查输入",
        declarations=declarations,
        values=values,
        known_inputs=("完整路径待提供", "问卷 survey.json 待提供"),
        upstream_sources=upstream_sources,
    )
    assert "## 已解析输入" in prompt
    assert "pdf_paths（运行参数，4 项）" in prompt
    for path in values["pdf_paths"]:
        assert path in prompt
    assert "N01 产物 `survey.json`（上游）" in prompt
    assert "完整路径待提供" in prompt and "已被运行参数 `pdf_paths` 取代" in prompt
    assert "问卷 survey.json 待提供" in prompt and "已被上游产物 `survey.json` 取代" in prompt


def test_resolved_prompt_is_unchanged_when_there_are_no_values_or_artifacts():
    assert append_resolved_inputs("任务", declarations=[{"name": "empty", "type": "list"}], values={}) == "任务"
