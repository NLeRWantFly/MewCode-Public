import json
from types import SimpleNamespace

import httpx
import pytest

from mewcode.config import ProviderConfig
from mewcode.conversation import ConversationManager, Message, ToolResultBlock
from mewcode.tools.evaluate_evidence import EvaluateEvidence, judgment_choice
from mewcode.tools.evaluate_replan_hypothesis import EvaluateHypothesisParams
from tests.test_typesafe_review import make_client


def registry(records, client=None):
    return SimpleNamespace(records=lambda: records, get=lambda key: next((r for r in records if r.key == key), None),
        resolve=lambda ref, **kwargs: SimpleNamespace(client=client), desktop_revision=lambda: 1)


def record():
    return SimpleNamespace(key="test-jev", provider=ProviderConfig(name="test", model="jev-test",
        protocol="typesafe", base_url="https://example.test", api_key="synthetic-test-key"))


def test_judgment_readiness_never_guesses_ambiguous_route(tmp_path):
    assert judgment_choice(registry([]), tmp_path)[0] is None
    assert judgment_choice(registry([record(), record()]), tmp_path)[0] is None
    selected, data = judgment_choice(registry([record()]), tmp_path)
    assert selected.model_id == "jev-test" and data["status"] == "configured_not_probed"
    assert "synthetic-test-key" not in json.dumps(data)
    folder = tmp_path / ".mewcode"
    folder.mkdir()
    (folder / "config.yaml").write_text("judgment_model: false", encoding="utf-8")
    assert judgment_choice(registry([record()]), tmp_path)[1]["status"] == "disabled"


@pytest.mark.asyncio
async def test_chat_jev_native_batch_durable_dedupe_and_bound_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr("mewcode.runtime_paths.user_data_root", lambda: tmp_path)
    calls = []
    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(200, json={"model": "jev-test-version", "usage": {"input_tokens": 70, "output_tokens": 15},
            "answers": {key: {"type": "choice", "choice": "insufficient", "confidence": .7,
                "probabilities": {"supported": .2, "contradicted": .1, "insufficient": .7}} for key in payload["questions"]}})
    client = make_client(respond)
    conversation = ConversationManager()
    conversation.history = [Message(role="user", content="", tool_results=[ToolResultBlock("receipt", "tests: 2 passed")])]
    agent = SimpleNamespace(work_dir=str(tmp_path), session_id="conversation", _current_conversation=conversation,
        client=SimpleNamespace(get_usage_recorder=lambda: None))
    providers = registry([record()], client)
    params = EvaluateHypothesisParams(hypotheses=[{"claim": claim, "evidence_refs": ["receipt"]}
        for claim in ["tests cover failure", "independent verification occurred", "output preserves source"]])
    result = await EvaluateEvidence(agent, providers).execute(params)
    assert json.loads(result.output)["status"] == "advisory"
    assert len(calls) == 1 and len(calls[0]["questions"]) == 3
    assert agent.auxiliary_judgment_tokens == 85
    repeat = await EvaluateEvidence(agent, providers).execute(params)
    assert json.loads(repeat.output)["status"] == "not_reissued" and len(calls) == 1
    bad = params.model_copy(update={"hypotheses": [params.hypotheses[0].model_copy(update={"evidence_refs": ["invented"]})]})
    assert (await EvaluateEvidence(agent, providers).execute(bad)).is_error
    assert len(calls) == 1 and "permission" in result.output
