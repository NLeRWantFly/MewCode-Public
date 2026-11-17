from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mewcode.completion.analyzer import JavaLightweightAnalyzer
from mewcode.completion.api import register_completion_api
from mewcode.completion.catalog import JavaMember
from mewcode.completion.models import CompletionRequest
from mewcode.completion.service import CompletionService
from mewcode.completion.store import CompletionPreferenceStore


def _service(tmp_path: Path, fallback_provider=None) -> CompletionService:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return CompletionService(
        project,
        user_store_path=tmp_path / "user" / "completion.db",
        fallback_provider=fallback_provider,
    )


@pytest.mark.asyncio
async def test_scanner_member_completion_uses_deterministic_graph(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    source = (
        "import java.util.Scanner;\n"
        "class Main {\n"
        "  void run() {\n"
        "    Scanner in = new Scanner(System.in);\n"
        "    in.next\n"
        "  }\n"
        "}\n"
    )

    cursor_offset = source.index("in.next") + len("in.next")
    response = await service.complete(
        CompletionRequest(source=source, cursor_offset=cursor_offset)
    )

    assert response.analysis.receiver == "in"
    assert response.analysis.prefix == "next"
    assert response.analysis.resolved_type == "Scanner"
    assert response.analysis.type_inference == "declaration"
    assert response.analysis.graph_path == [
        "analyze",
        "learn_alias",
        "retrieve",
        "rank",
    ]
    assert response.fallback_used is False
    assert response.candidates
    assert response.candidates[0].label == "next()"
    assert {item.label for item in response.candidates} >= {
        "next()",
        "nextInt()",
        "nextLine()",
    }
    candidate = response.candidates[0]
    assert source[candidate.replacement_start:candidate.replacement_end] == "next"
    assert candidate.insert_text == "next()"


def test_comments_and_literals_do_not_create_false_declarations() -> None:
    analyzer = JavaLightweightAnalyzer()
    source = (
        "class Main { void run() {\n"
        "  // Scanner fake = new Scanner(System.in);\n"
        '  String text = "Scanner fake = new Scanner(System.in);";\n'
        "  fake.next\n"
        "} }"
    )

    context = analyzer.analyze(source, len(source) - len("\n} }"))

    assert context.receiver == "fake"
    assert context.resolved_type == ""
    assert context.type_inference == "unresolved"


@pytest.mark.asyncio
async def test_project_defined_members_are_retrieved(tmp_path: Path) -> None:
    service = _service(tmp_path)
    source = (
        "class UserRepository {\n"
        "  public String findUser(int id) { return \"\"; }\n"
        "  public int count() { return 0; }\n"
        "}\n"
        "class Main { void run() {\n"
        "  UserRepository repository = new UserRepository();\n"
        "  repository.fi"
        "} }"
    )

    cursor_offset = source.index("repository.fi") + len("repository.fi")
    response = await service.complete(
        CompletionRequest(source=source, cursor_offset=cursor_offset)
    )

    assert response.analysis.resolved_type == "UserRepository"
    assert [candidate.label for candidate in response.candidates] == ["findUser(int)"]
    assert response.candidates[0].source == "project_symbol"
    assert response.candidates[0].insert_text == "findUser()"


@pytest.mark.asyncio
async def test_declared_alias_is_learned_without_storing_source(tmp_path: Path) -> None:
    service = _service(tmp_path)
    declared = (
        "class Main { void run() { "
        "Scanner reader = new Scanner(System.in); reader.ne"
        " } }"
    )
    declared_cursor = declared.index("reader.ne") + len("reader.ne")
    await service.complete(
        CompletionRequest(source=declared, cursor_offset=declared_cursor)
    )

    response = await service.complete(
        CompletionRequest(source="class Main { void run() { reader.ne")
    )

    assert response.analysis.resolved_type == "Scanner"
    assert response.analysis.type_inference == "learned_alias"
    assert {candidate.label for candidate in response.candidates} >= {
        "next()",
        "nextInt()",
    }

    with sqlite3.connect(service.project_store.path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(completion_aliases)")
        }
        values = connection.execute(
            "SELECT alias, type_name, last_evidence_hash FROM completion_aliases"
        ).fetchall()
    assert "source" not in columns
    assert values[0][0:2] == ("reader", "Scanner")
    assert declared not in str(values)


@pytest.mark.asyncio
async def test_feedback_reranks_and_duplicate_feedback_is_idempotent(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    source = "Scanner in = new Scanner(System.in); in.next"

    initial = await service.complete(CompletionRequest(source=source))
    initial_next_int = next(
        candidate for candidate in initial.candidates if candidate.label == "nextInt()"
    )
    assert service.record_feedback(
        initial.completion_id,
        initial_next_int.candidate_id,
        True,
    ) is True
    assert service.record_feedback(
        initial.completion_id,
        initial_next_int.candidate_id,
        True,
    ) is False

    for _ in range(5):
        response = await service.complete(CompletionRequest(source=source))
        next_int = next(
            candidate
            for candidate in response.candidates
            if candidate.label == "nextInt()"
        )
        assert service.record_feedback(
            response.completion_id,
            next_int.candidate_id,
            True,
        ) is True

    reranked = await service.complete(CompletionRequest(source=source))
    reranked_next_int = next(
        candidate
        for candidate in reranked.candidates
        if candidate.label == "nextInt()"
    )
    assert reranked_next_int.score > initial_next_int.score
    assert reranked.candidates[0].label == "nextInt()"


@pytest.mark.asyncio
async def test_optional_fallback_only_runs_for_low_confidence(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    async def fallback(request, context):
        calls.append(context.receiver)
        return [
            JavaMember(
                declaring_type="UnknownType",
                name="hello",
                kind="method",
                return_type="String",
                priority=0.7,
                source="llm",
            )
        ]

    service = _service(tmp_path, fallback)
    high_confidence = await service.complete(
        CompletionRequest(
            source="Scanner in = new Scanner(System.in); in.next"
        )
    )
    assert high_confidence.fallback_used is False
    assert calls == []

    low_confidence = await service.complete(
        CompletionRequest(source="mystery.hel")
    )
    assert low_confidence.fallback_used is True
    assert calls == ["mystery"]
    assert low_confidence.analysis.graph_path == [
        "analyze",
        "learn_alias",
        "retrieve",
        "fallback",
        "rank",
    ]
    assert low_confidence.candidates[0].source == "llm"


def test_alias_observation_is_deduplicated_by_evidence(tmp_path: Path) -> None:
    store = CompletionPreferenceStore(tmp_path / "completion.db")
    store.observe_alias("java", "reader", "Scanner", "same-evidence")
    store.observe_alias("java", "reader", "Scanner", "same-evidence")
    assert store.resolve_alias("java", "reader")[0].observations == 1
    store.observe_alias("java", "reader", "Scanner", "different-evidence")
    assert store.resolve_alias("java", "reader")[0].observations == 2


def test_completion_api_validates_bounds_and_feedback_session(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    app = FastAPI()
    register_completion_api(
        app,
        work_dir_provider=lambda: str(project),
        user_store_path=tmp_path / "user" / "completion.db",
    )

    with TestClient(app) as client:
        capabilities = client.get("/api/completion/capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json()["languages"] == ["java"]
        assert capabilities.json()["source_retention"] == "request_only"

        complete = client.post(
            "/api/completion/complete",
            json={
                "language": "java",
                "source": "Scanner in = new Scanner(System.in); in.ne",
                "max_results": 5,
            },
        )
        assert complete.status_code == 200
        payload = complete.json()
        selected = payload["candidates"][0]
        feedback = client.post(
            "/api/completion/feedback",
            json={
                "completion_id": payload["completion_id"],
                "candidate_id": selected["candidate_id"],
                "accepted": True,
            },
        )
        assert feedback.status_code == 200
        assert feedback.json() == {"recorded": True, "accepted": True}

        expired = client.post(
            "/api/completion/feedback",
            json={
                "completion_id": "unknown_session",
                "candidate_id": "a" * 24,
                "accepted": True,
            },
        )
        assert expired.status_code == 404

        invalid_cursor = client.post(
            "/api/completion/complete",
            json={"language": "java", "source": "abc", "cursor_offset": 4},
        )
        assert invalid_cursor.status_code == 422

        unsupported_language = client.post(
            "/api/completion/complete",
            json={"language": "python", "source": "value."},
        )
        assert unsupported_language.status_code == 422
