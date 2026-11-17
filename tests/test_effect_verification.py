import pytest

from mewcode.experts.command_receipts import command_effect_fingerprint
from mewcode.experts.effect_verification import verify_effect_observations
from mewcode.tools.read_file import Params, ReadFile


def _receipt(command="echo changed > result.txt"):
    request = {"command": command, "verification_command": command,
        "input_scope": [], "declared_write_paths": []}
    return {"node_id": "node", "request": request,
        "effect_fingerprint": command_effect_fingerprint("node", request)}


async def _observation(path, *, tool="ReadFile"):
    result = await ReadFile().execute(Params(file_path=str(path)))
    return {"name": tool, "tool_id": "read-1", "args": {"file_path": str(path)},
        "is_error": result.is_error, "output": result.output,
        "host_observation": result.host_observation}


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "expected"), [
    ("missing", "not_applied"),
    ("changed", "applied"),
    ("different", "partial"),
])
async def test_single_file_overwrite_requires_matching_readfile_host_observation(tmp_path, state, expected):
    target = tmp_path / "result.txt"
    if state == "changed":
        target.write_text("changed\n", encoding="utf-8")
    elif state == "different":
        target.write_text("other\n", encoding="utf-8")
    receipt = _receipt()
    observation = await _observation(target)

    result = verify_effect_observations(receipt, [observation], str(tmp_path))

    assert result is not None
    assert result["effect"] == expected
    assert result["proof"]["host_verified_effect"] == expected
    assert result["proof"]["target"] == str(target.resolve())
    assert result["proof"]["effect_fingerprint"] == receipt["effect_fingerprint"]
    assert result["proof"]["result_sha256"] == observation["host_observation"]["file_state"]["text_sha256"]


@pytest.mark.asyncio
async def test_unrelated_path_and_non_readfile_facts_never_verify_target(tmp_path):
    (tmp_path / "other.txt").write_text("changed\n", encoding="utf-8")
    receipt = _receipt()
    observation = await _observation(tmp_path / "other.txt")

    assert verify_effect_observations(receipt, [observation], str(tmp_path)) is None
    same_state_other_tool = {**observation, "name": "ReadStageResult"}
    assert verify_effect_observations(receipt, [same_state_other_tool], str(tmp_path)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [
    "echo changed >> result.txt",
    "echo changed > result.txt && echo second > other.txt",
    "Write-Output changed",
    "rm result.txt",
    "python -c \"from pathlib import Path; Path('result.txt').write_text('changed')\"",
])
async def test_append_compound_delete_and_opaque_commands_are_not_verified(tmp_path, command):
    target = tmp_path / "result.txt"
    target.write_text("changed\n", encoding="utf-8")
    observation = await _observation(target)

    assert verify_effect_observations(_receipt(command), [observation], str(tmp_path)) is None


@pytest.mark.asyncio
async def test_declared_target_must_match_command_target_and_observation(tmp_path):
    target = tmp_path / "result.txt"
    target.write_text("changed\n", encoding="utf-8")
    observation = await _observation(target)
    receipt = _receipt()
    receipt["request"]["declared_write_paths"] = ["different.txt"]
    receipt["effect_fingerprint"] = command_effect_fingerprint("node", receipt["request"])

    assert verify_effect_observations(receipt, [observation], str(tmp_path)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('initial', [None, 'changed\n'])
async def test_observation_before_a_later_write_cannot_verify_current_effect(tmp_path, initial):
    target = tmp_path / 'result.txt'
    if initial is not None:
        target.write_text(initial, encoding='utf-8')
    observation = await _observation(target)
    target.write_text('later helper output\n', encoding='utf-8')
    assert verify_effect_observations(_receipt(), [observation], str(tmp_path)) is None
