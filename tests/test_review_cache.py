from unittest.mock import AsyncMock

import pytest

from mewcode.permissions.reviewer import SafetyReviewer


@pytest.mark.asyncio
async def test_exact_action_allow_cache_is_bound_to_target_bytes_and_risk(tmp_path):
    target = tmp_path / "evidence.txt"
    target.write_text("version one", encoding="utf-8")
    reviewer = SafetyReviewer(None, str(tmp_path))
    reviewer._ask_model = AsyncMock(return_value='{"outcome":"allow","risk_level":"low","rationale":"Within current scope"}')
    args = {"file_path": "evidence.txt"}
    first = await reviewer.review(tool_name="ReadFile", arguments=args, risk={"side_effect": "none"}, conversation=None)
    second = await reviewer.review(tool_name="ReadFile", arguments=args, risk={"side_effect": "none"}, conversation=None)
    assert first.allowed and not first.cache_hit
    assert second.allowed and second.cache_hit and second.review_route["risk_tier"] == 1
    assert reviewer._ask_model.await_count == 1
    target.write_text("version two", encoding="utf-8")
    third = await reviewer.review(tool_name="ReadFile", arguments=args, risk={"side_effect": "none"}, conversation=None)
    assert third.allowed and not third.cache_hit
    await reviewer.review(tool_name="ReadFile", arguments=args, risk={"side_effect": "write"}, conversation=None)
    assert reviewer._ask_model.await_count == 3


@pytest.mark.asyncio
async def test_inline_commands_and_unscoped_paths_never_use_allow_cache(tmp_path):
    reviewer = SafetyReviewer(None, str(tmp_path))
    reviewer._ask_model = AsyncMock(return_value='{"outcome":"allow","risk_level":"low","rationale":"Within current scope"}')
    for _ in range(2):
        await reviewer.review(tool_name="Bash", arguments={"command": "echo okay"},
                              risk={}, conversation=None)
        await reviewer.review(tool_name="WriteFile", arguments={"file_path": "../outside.txt"},
                              risk={}, conversation=None)
    assert reviewer._ask_model.await_count == 4


@pytest.mark.asyncio
async def test_script_allow_cache_invalidates_after_file_change(tmp_path):
    script = tmp_path / "task.py"
    script.write_text("print(1)\n", encoding="utf-8")
    reviewer = SafetyReviewer(None, str(tmp_path))
    reviewer._ask_model = AsyncMock(return_value='{"outcome":"allow","risk_level":"low","rationale":"Within current scope"}')
    request = dict(tool_name="Bash", arguments={"command": "python task.py"},
                   risk={"side_effect": "workspace"}, conversation=None)
    first = await reviewer.review(**request)
    second = await reviewer.review(**request)
    assert first.allowed and not first.cache_hit and second.cache_hit
    assert reviewer._ask_model.await_count == 1
    script.write_text("print(2)\n", encoding="utf-8")
    third = await reviewer.review(**request)
    assert third.allowed and not third.cache_hit
    assert reviewer._ask_model.await_count == 2
