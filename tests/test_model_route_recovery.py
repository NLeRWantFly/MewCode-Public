from types import SimpleNamespace

from mewcode.experts.flow import ExpertNodeSpec
from mewcode.experts.model_route_recovery import (
    bind_declared_backup, declared_backup, declared_reasoning_upgrade,
)
from mewcode.model_registry import ModelRef


class Registry:
    def __init__(self, capabilities):
        self.capabilities = capabilities

    def resolve(self, ref):
        key = (ref.provider_key, ref.model_id)
        if key not in self.capabilities:
            raise ValueError("unavailable")
        return SimpleNamespace(capabilities=self.capabilities[key])


def test_frozen_backup_requires_matching_modality_and_tool_capacity():
    node = ExpertNodeSpec.from_value({"node_id": "n", "role": "executor", "label": "N",
        "backup_models": [{"provider_key": "p", "model_id": "text"},
                          {"provider_key": "p", "model_id": "vision"}]})
    assert len(node.as_dict()["backup_models"]) == 2
    registry = Registry({("p", "primary"): ("chat", "tools", "vision"),
                         ("p", "text"): ("chat", "tools"),
                         ("p", "vision"): ("chat", "tools", "vision")})
    selected, reason = declared_backup(node, ModelRef("p", "primary"), registry)
    assert selected.model_id == "vision"
    assert reason == "declared_capability_compatible"


def test_undeclared_or_unavailable_backup_fails_closed():
    node = ExpertNodeSpec.from_value({"node_id": "n", "role": "executor", "label": "N"})
    assert declared_backup(node, ModelRef("p", "primary"), Registry({}))[1] == "no_declared_backup"
    node = ExpertNodeSpec.from_value({"node_id": "n", "role": "executor", "label": "N",
        "backup_models": [{"provider_key": "p", "model_id": "missing"}]})
    selected, reason = declared_backup(node, ModelRef("p", "primary"),
        Registry({("p", "primary"): ("chat", "tools")}))
    assert selected is None and reason == "no_compatible_declared_backup"


def test_switch_is_bound_to_frozen_flow_input_and_existing_model():
    primary, backup = ModelRef("p", "primary"), ModelRef("p", "backup")
    snapshot = {"flow_fingerprint": "flow-v1", "locked_flow": {"nodes": [{
        "node_id": "n", "backup_models": [backup.as_dict()]}]},
        "locked_models": {"n": primary.as_dict()},
        "node_states": {"n": {"input_revision": "input-v1", "tools": {}}}}
    receipt = bind_declared_backup(snapshot, node_id="n", role="executor",
        expected_current=primary, backup=backup, input_revision="input-v1",
        flow_fingerprint="flow-v1", receipt_id="r1")
    assert snapshot["locked_models"]["n"] == backup.as_dict()
    assert receipt["changes_contract"] is False
    before = snapshot.copy()
    try:
        bind_declared_backup(snapshot, node_id="n", role="executor", expected_current=primary,
            backup=backup, input_revision="input-v1", flow_fingerprint="flow-v1", receipt_id="r2")
    except ValueError:
        pass
    else:
        raise AssertionError("stale binding unexpectedly switched")
    assert snapshot == before


def test_reasoning_upgrade_requires_frozen_same_model_higher_supported_effort():
    class EffortRegistry:
        def resolve(self, ref):
            if ref.model_id not in {"m", "other"}:
                raise ValueError("missing")
            return SimpleNamespace(protocol="openai", provider=object(),
                effort=ref.effort, capabilities=("chat", "tools", "vision"))

        def _effort_levels(self, protocol, model, provider):
            return ["low", "medium", "high"]

    node = ExpertNodeSpec.from_value({"node_id": "n", "role": "reviewer", "label": "N",
        "backup_models": [ModelRef("p", "other", "high").as_dict(),
                          ModelRef("p", "m", "high").as_dict()]})
    selected, reason = declared_reasoning_upgrade(node, ModelRef("p", "m", "low"), EffortRegistry())
    assert selected == ModelRef("p", "m", "high")
    assert reason == "declared_higher_effort_same_model"
    assert declared_reasoning_upgrade(node, ModelRef("p", "m", "high"), EffortRegistry())[0] is None
    assert declared_reasoning_upgrade(node, ModelRef("p", "m"), EffortRegistry())[0] is None

    snapshot = {"flow_fingerprint": "f", "locked_flow": {"nodes": [node.as_dict()]},
        "locked_models": {"n": ModelRef("p", "m", "low").as_dict()},
        "node_states": {"n": {"input_revision": "i", "tools": {}}}}
    receipt = bind_declared_backup(snapshot, node_id="n", role="reviewer",
        expected_current=ModelRef("p", "m", "low"), backup=selected,
        input_revision="i", flow_fingerprint="f", receipt_id="upgrade",
        reason="declared_reasoning_upgrade")
    assert receipt["reason"] == "declared_reasoning_upgrade"
