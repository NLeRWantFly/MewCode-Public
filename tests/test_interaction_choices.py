"""Read-only discovery and resolution tests for orchestration choices."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pypdf import PdfWriter

from mewcode.orchestration.interaction_choices import (
    prepare_choices,
    project_questions,
    resolve_answers,
)


def _pdf(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as stream:
        writer.write(stream)


def _question(name: str = "sources", **extra: object) -> dict:
    value = {"name": name, "label": "材料", "options": []}
    value.update(extra)
    return value


@pytest.mark.asyncio
async def test_four_real_pdfs_are_one_verified_list_candidate_and_project_cleanly(tmp_path: Path):
    root = tmp_path / "materials"
    root.mkdir()
    files = [root / f"source-{index}.pdf" for index in range(4)]
    for path in files:
        _pdf(path)
    calls: list[tuple[str, str]] = []

    async def complete(system: str, user_text: str) -> str:
        calls.append((system, user_text))
        if len(calls) == 1:
            return json.dumps({"searches": [{"root": str(root), "pattern": "*.pdf"}]})
        return json.dumps(
            {
                "questions": [
                    {
                        "name": "sources",
                        "choices": [
                            {
                                "id": "all-four",
                                "label": "四份材料",
                                "description": "实际发现的 PDF",
                                "kind": "value",
                                "value": [str(path) for path in files],
                                "evidence": [str(path) for path in files],
                                "verification": "verified",
                            }
                        ],
                    }
                ]
            }
        )

    question = _question()
    bundle = await prepare_choices(
        identity={"run_id": "run-1"},
        questions=[question],
        context={},
        complete=complete,
        allowed_roots=[str(root)],
    )

    assert bundle["status"] == "ready"
    assert len(calls) == 2
    prepared = bundle["questions"][0]["choices"]
    value_choice = next(choice for choice in prepared if choice["kind"] == "value")
    assert value_choice["value"] == [str(path) for path in files]
    assert value_choice["verification"] == "verified"
    assert len([choice for choice in prepared if choice["kind"] == "value"]) == 1

    projected = project_questions([question], bundle)[0]
    assert [item["value"] for item in projected["options"]] == [
        choice["id"] for choice in prepared
    ]
    assert projected["allow_other"] is True
    assert projected["default"] is None
    assert projected["prepared_choices"] == prepared

    resolved = resolve_answers(
        bundle,
        {"sources": "all-four"},
        bundle_id=bundle["id"],
        version=bundle["version"],
    )
    assert resolved == {"values": {"sources": [str(path) for path in files]}, "actions": []}


@pytest.mark.asyncio
async def test_custom_value_is_allowed_only_with_manual_and_actions_do_not_bind_values(tmp_path: Path):
    calls = 0

    async def complete(_system: str, _user_text: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return json.dumps({"searches": []})
        return json.dumps(
            {
                "questions": [
                    {
                        "name": "source",
                        "choices": [
                            {
                                "id": "known",
                                "label": "已知",
                                "description": "一个候选",
                                "value": "known-value",
                            }
                        ],
                    }
                ]
            }
        )

    bundle = await prepare_choices(
        identity={"task": "custom"},
        questions=[_question("source")],
        context={},
        complete=complete,
        allowed_roots=[str(tmp_path)],
    )
    choices = bundle["questions"][0]["choices"]
    assert {choice["action"] for choice in choices if choice["kind"] == "action"} >= {
        "manual",
        "defer",
    }

    assert resolve_answers(bundle, {"source": "known"})["values"] == {"source": "known-value"}
    assert resolve_answers(bundle, {"source": "user supplied"}) == {
        "values": {"source": "user supplied"},
        "actions": [],
    }
    action_result = resolve_answers(
        bundle,
        {"source": "manual"},
        choice_inputs={"source": "typed value"},
    )
    assert action_result == {
        "values": {},
        "actions": [{"name": "source", "action": "manual", "input": "typed value"}],
    }


@pytest.mark.asyncio
async def test_closed_enum_cannot_be_extended_and_fabricated_paths_stay_suggested(tmp_path: Path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    fabricated = outside / "fabricated.pdf"
    fabricated.write_bytes(b"%PDF-1.4")
    calls = 0

    async def complete(_system: str, _user_text: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return json.dumps(
                {
                    "searches": [
                        {"root": str(outside), "pattern": "*.pdf"},
                    ]
                }
            )
        return json.dumps(
            {
                "questions": [
                    {
                        "name": "mode",
                        "choices": [
                            {
                                "id": "forged",
                                "label": "越界文件",
                                "value": str(fabricated),
                                "evidence": [str(fabricated)],
                                "verification": "verified",
                            },
                            {
                                "id": "unknown",
                                "label": "不得加入",
                                "value": "unknown",
                            },
                        ],
                    }
                ]
            }
        )

    question = {
        "name": "mode",
        "label": "模式",
        "allow_other": False,
        "options": [
            {"value": "safe", "label": "安全", "description": "", "recommended": False},
            {"value": "fast", "label": "快速", "description": "", "recommended": False},
        ],
    }
    bundle = await prepare_choices(
        identity={"task": "closed"},
        questions=[question],
        context={},
        complete=complete,
        allowed_roots=[str(allowed)],
    )
    choices = bundle["questions"][0]["choices"]
    assert all(choice.get("value") != str(fabricated) for choice in choices)
    assert all(choice.get("value") != "unknown" for choice in choices)
    assert any(choice.get("action") == "manual" for choice in choices)
    assert resolve_answers(bundle, {"mode": {"custom": "safe"}})["values"] == {"mode": "safe"}
    with pytest.raises(ValueError, match="合同"):
        resolve_answers(bundle, {"mode": {"custom": "unknown"}})
    assert resolve_answers(bundle, {"mode": next(c["id"] for c in choices if c.get("value") == "safe")})[
        "values"
    ] == {"mode": "safe"}
    with pytest.raises(ValueError):
        resolve_answers(bundle, {"mode": "arbitrary"})


@pytest.mark.asyncio
async def test_cache_scope_refresh_and_stale_versions(tmp_path: Path):
    calls = 0

    async def complete(_system: str, _user_text: str) -> str:
        nonlocal calls
        calls += 1
        if calls % 2:
            return json.dumps({"searches": []})
        return json.dumps(
            {
                "questions": [
                    {"name": "q", "choices": [{"id": "v", "label": "值", "value": "v"}]}
                ]
            }
        )

    kwargs = {
        "identity": {"task": "cache"},
        "questions": [_question("q")],
        "context": {"graph": "one"},
        "complete": complete,
        "allowed_roots": [str(tmp_path)],
    }
    first = await prepare_choices(**kwargs)
    assert calls == 2
    reused = await prepare_choices(**kwargs, cached=first)
    assert calls == 2
    assert reused == first

    changed_context = await prepare_choices(**{**kwargs, "context": {"graph": "two"}}, cached=first)
    assert calls == 4
    refreshed = await prepare_choices(**kwargs, cached=first, refresh=True)
    assert calls == 6
    assert refreshed["id"] != first["id"]
    assert refreshed["version"] == first["version"]
    with pytest.raises(ValueError):
        resolve_answers(refreshed, {"q": "v"}, bundle_id=first["id"], version=first["version"])
    with pytest.raises(ValueError):
        resolve_answers(changed_context, {"q": "v"}, version=first["version"])


@pytest.mark.asyncio
async def test_malformed_fields_are_sent_back_for_targeted_repair(tmp_path: Path):
    calls = 0

    async def complete(_system: str, _user_text: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return json.dumps({"searches": [], "unexpected": "repair me"})
        if calls == 2:
            return json.dumps({"searches": []})
        if calls == 3:
            return json.dumps(
                {
                    "questions": [
                        {"name": "q", "choices": [{"id": "v", "label": "值", "value": "v", "extra": 1}]}
                    ]
                }
            )
        return json.dumps({"questions": [{"name": "q", "choices": [{"id": "v", "label": "值", "value": "v"}]}]})

    bundle = await prepare_choices(
        identity={"task": "repair"},
        questions=[_question("q")],
        context={},
        complete=complete,
        allowed_roots=[str(tmp_path)],
    )
    assert bundle["status"] == "ready"
    assert calls == 4
