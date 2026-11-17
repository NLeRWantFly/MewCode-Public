import json
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from mewcode.orchestration_guide import orchestration_build_experience, orchestration_component_guide


@pytest.mark.asyncio
async def test_guide_is_shared_read_only_and_discoverable(tmp_path, monkeypatch):
    from frontend import server
    from mewcode.prompts import ORCHESTRATION_PLANNING_REMINDER
    from mewcode.orchestration.extract import INVENTORY_SYSTEM, EXTRACT_SYSTEM, REFILL_SYSTEM
    from mewcode.skills.loader import SkillLoader
    from mewcode.tools.load_skill import LoadSkill, LoadSkillParams

    guide = orchestration_component_guide()
    for prompt in (ORCHESTRATION_PLANNING_REMINDER, INVENTORY_SYSTEM, EXTRACT_SYSTEM, REFILL_SYSTEM):
        assert guide in prompt
    # Reading help must not need a project or any persistence store.
    def forbidden(*args, **kwargs):
        raise AssertionError("Guide accessed mutable plan state")
    monkeypatch.setattr(server, "get_plan_session_store", forbidden)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.get("/api/orchestration/guide")
    assert response.status_code == 200
    assert response.json() == {"markdown": guide}
    loader = SkillLoader(str(tmp_path))
    loader._user_dir = tmp_path / "user-skills"
    skill = loader.load_all()["orchestration-diagrams"]
    assert skill.is_directory
    assert (skill.source_path.parent / "references/components.md").read_text(encoding="utf-8").strip() == guide
    activated = []
    catalogs = []
    tool = LoadSkill()
    tool.set_loader(loader)
    tool.set_agent(SimpleNamespace(activate_skill=lambda *args: activated.append(args),
                                  set_skill_catalog=catalogs.append, registry=None))
    result = await tool.execute(LoadSkillParams(name=skill.name))
    assert not result.is_error
    assert activated[0][0] == skill.name
    assert len(catalogs) == 1 and skill.name in catalogs[0]
    assert "references/components.md" in activated[0][1]


@pytest.mark.asyncio
async def test_build_experience_is_packaged_loadable_and_shared_by_scoped_prompts(tmp_path):
    from mewcode.orchestration.extract import INVENTORY_SYSTEM, EXTRACT_SYSTEM, REFILL_SYSTEM
    from mewcode.orchestration.graph_generation import GRAPH_SYSTEM
    from mewcode.orchestration.graph_completion_protocol import COMPLETION_SYSTEM
    from mewcode.orchestration.graph_review import REVIEW_SYSTEM
    from mewcode.orchestration.graph_discussion import DISCUSSION_SYSTEM
    from mewcode.prompts import ORCHESTRATION_PLANNING_REMINDER
    from mewcode.skills.loader import SkillLoader
    from mewcode.tools.load_skill import LoadSkill, LoadSkillParams

    loader = SkillLoader(str(tmp_path))
    loader._user_dir = tmp_path / "user-skills"
    skill = loader.load_all()["orchestration-diagrams"]
    experience = orchestration_build_experience()
    assert '12. 审查不按证据类型拆分。同一审查对象只设一个 audit 节点' in experience
    assert '像素 ReadAuditArtifact 或 InspectVisualEvidence、命令 RunVerification' in experience
    path = skill.source_path.parent / "references/execution-readiness.md"
    assert path.read_text(encoding="utf-8").strip() == experience
    for prompt in (INVENTORY_SYSTEM, EXTRACT_SYSTEM, REFILL_SYSTEM, GRAPH_SYSTEM,
                   COMPLETION_SYSTEM, REVIEW_SYSTEM, DISCUSSION_SYSTEM, ORCHESTRATION_PLANNING_REMINDER):
        assert experience in prompt

    activated = []
    tool = LoadSkill(instruction_only=True)
    tool.set_loader(loader)
    tool.set_agent(SimpleNamespace(activate_skill=lambda *args: activated.append(args),
                                  set_skill_catalog=lambda *_: None, registry=None))
    result = await tool.execute(LoadSkillParams(name=skill.name))
    assert not result.is_error and activated[0][0] == "orchestration-diagrams"
    assert "references/execution-readiness.md" in activated[0][1]
    assert "references/verification.md" in activated[0][1]


def test_documented_loop_compiles_and_roundtrips():
    from mewcode.orchestration import OrchestrationSpec, validate_spec, to_expert_flow, to_pipeline_graph, from_pipeline_graph
    from mewcode.orchestration.spec import OrchestrationError

    schema = Path(__file__).resolve().parents[1] / "mewcode/skills/builtins/orchestration-diagrams/references/schema.md"
    raw = json.loads(re.search(r"```json\n(.*?)\n```", schema.read_text(encoding="utf-8"), re.S)[1])
    spec = OrchestrationSpec.from_value(raw)
    validate_spec(spec)
    with pytest.raises(OrchestrationError):
        validate_spec(spec, require_bindings=True)
    flow = to_expert_flow(spec)
    restored = from_pipeline_graph(to_pipeline_graph(spec))
    validate_spec(restored)
    assert to_expert_flow(restored).meta["loops"] == flow.meta["loops"]
    assert set(restored.node_map()) == {"work", "check", "done"}
    assert flow.node_map["work"].failure_policy.max_attempts == 1
    assert flow.meta["loops"][0]["max_iterations"] == 3
