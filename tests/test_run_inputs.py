"""Runtime values are bounded by the graph's declared input contract."""
import pytest

from mewcode.orchestration.run_inputs import append_run_inputs, resolve_run_inputs
from mewcode.orchestration.spec import GoalContract


DECLARATIONS = [
    {"name": "主题", "description": "工作主题", "required": True, "default": ""},
    {"name": "篇数", "description": "资料篇数", "required": False, "default": "8"},
]


def test_resolve_run_inputs_requires_only_missing_mandatory_values():
    with pytest.raises(ValueError, match="^缺少必填运行参数：主题；未开跑$"):
        resolve_run_inputs(DECLARATIONS, {})
    assert resolve_run_inputs(DECLARATIONS, {"主题": "图学习"}) == {"主题": "图学习", "篇数": "8"}


def test_run_default_can_be_overridden_without_changing_declarations():
    before = [dict(item) for item in DECLARATIONS]
    values = resolve_run_inputs(DECLARATIONS, {"主题": "图学习", "篇数": "12"})
    assert values["篇数"] == "12" and DECLARATIONS == before
    prompt = append_run_inputs("运行研究流程", values)
    assert "本次运行参数：" in prompt and '"篇数": "12"' in prompt


@pytest.mark.parametrize("submitted", [
    {"主题": "主题", "隐藏": "额外命令"},
    ["主题"],
    {"主题": 123},
    {"主题": "x" * 2001},
    {"主题": "abc\x00def"},
])
def test_run_inputs_reject_unknown_shape_or_unbounded_values(submitted):
    with pytest.raises(ValueError, match="未开跑"):
        resolve_run_inputs(DECLARATIONS, submitted)


def test_declaration_names_must_be_unique_and_nonempty():
    with pytest.raises(ValueError, match="未开跑"):
        resolve_run_inputs([{"name": "主题"}, {"name": "主题"}], {})
    with pytest.raises(ValueError, match="未开跑"):
        resolve_run_inputs([{"name": ""}], {})


def test_null_default_is_an_empty_value_and_cannot_bypass_required():
    with pytest.raises(ValueError, match="^缺少必填运行参数：主题；未开跑$"):
        resolve_run_inputs([{"name": "主题", "default": None}], {})
    assert resolve_run_inputs([{"name": "可选", "required": False, "default": None}], {}) == {"可选": ""}


@pytest.mark.parametrize("declarations", [None, {"name": "主题"}, [{"name": "主题", "required": "false"}], [{"name": "主题 "}]])
def test_invalid_declarations_cannot_silently_make_values_optional(declarations):
    with pytest.raises(ValueError, match="未开跑"):
        resolve_run_inputs(declarations, {})


@pytest.mark.parametrize("value", [False, 0, "", {}])
def test_falsey_invalid_contract_input_arrays_are_rejected(value):
    with pytest.raises(ValueError, match="运行参数对象数组"):
        GoalContract.from_value({"objective": "交付报告", "run_inputs": value})
