"""Content selection, provenance and frozen planning versions; no live models."""
import copy

import pytest

from mewcode.orchestration.draft_sources import digest
from mewcode.orchestration.draft_store import DraftStore
from mewcode.orchestration.effective_history import (
    apply_history_patch, basis_view, effective_document, effective_sources, freeze_basis, new_basis, synchronize_manual_basis,
)
from mewcode.orchestration.workflow_draft import DraftPatchError, empty_document


def source(id, role, text, line):
    return {"id": id, "role": role, "text": text, "line": line, "part": 1, "parts": 1, "kind": "message"}


def fact(id, src, body=None, kind="constraint", **data):
    return {"id": id, "kind": kind, "title": id, "body": body or src["text"], "state": "active",
            "data": data, "sources": [{"source_id": src["id"], "quote": src["text"]}]}


def patch(*items, ignored=(), plan=None):
    return {"upserts": list(items), "ignored": list(ignored), **({"current_plan": plan} if plan else {})}


def suggested(src):
    return {"source_ids": [src["id"]], "mode": "suggested", "reason": "最新完整方案"}


def test_latest_value_keeps_early_scope_and_excludes_obsolete_value_from_review():
    scope = source("s0", "user", "四本 PDF 都要处理", 1)
    old = source("s1", "user", "数据库用 MySQL", 2)
    new = source("s2", "user", "数据库改用 SQLite", 3)
    sources = [scope, old, new]
    basis = new_basis(empty_document(), [s["id"] for s in sources])
    basis = apply_history_patch(basis, patch(fact("scope", scope), fact("storage", old)), sources[:2], sources)
    basis = apply_history_patch(basis, patch(fact("storage", new)), [new], sources)
    doc, _ = effective_document(basis)
    assert {e["id"]: e["body"] for e in doc["entries"]} == {"scope": scope["text"], "storage": new["text"]}
    assert [s["text"] for s in effective_sources(basis, sources)] == [scope["text"], new["text"]]
    excluded = basis_view(basis, sources)["excluded"]
    assert any(e["body"] == old["text"] and e["restorable"] for e in excluded)
    assert sources[1]["text"] == old["text"], "curation must not rewrite original messages"


@pytest.mark.parametrize("same_batch", [False, True])
def test_assistant_cannot_use_old_user_quote_to_replace_a_requirement(same_batch):
    user = source("s0", "user", "数据库用 SQLite", 1)
    assistant = source("s1", "assistant", "改用 MySQL 更方便", 2)
    sources = [user, assistant]
    basis = new_basis(empty_document(), [s["id"] for s in sources])
    basis = apply_history_patch(basis, patch(fact("storage", user)), [user], sources)
    proposed = fact("storage", user if same_batch else assistant, body="数据库用 MySQL")
    result = apply_history_patch(basis, patch(proposed, ignored=[{"source_id": assistant["id"], "reason": "未采用的建议"}]),
                                 sources if same_batch else [assistant], sources, context=[] if same_batch else [user])
    current = effective_document(result)[0]["entries"][0]
    assert current["body"] == user["text"] and current["state"] == "active" and current["evidence"] == "user_source"


def test_new_complete_plan_replaces_old_assistant_nodes_but_not_user_requirements():
    user = source("s0", "user", "必须有独立验收", 1)
    first = source("s1", "assistant", "旧方案：先导出中间 CSV，再验收", 2)
    second = source("s2", "assistant", "新方案：直接写 SQLite，再验收", 3)
    sources = [user, first, second]
    basis = new_basis(empty_document(), [s["id"] for s in sources])
    basis = apply_history_patch(basis, patch(fact("quality", user), fact("csv", first, kind="node"), plan=suggested(first)),
                                sources[:2], sources)
    basis = apply_history_patch(basis, patch(fact("sqlite", second, kind="node"), plan=suggested(second)), [second], sources)
    doc, reasons = effective_document(basis)
    assert {e["id"] for e in doc["entries"] if e["state"] == "active"} == {"quality", "sqlite"}
    assert "旧方案" in reasons["csv"]
    assert basis_view(basis, sources)["suggestions"][0]["evidence"] == "proposal"


def test_obsolete_plan_does_not_fail_duplicate_check_before_new_plan_is_applied():
    first = source("s0", "assistant", "旧方案：独立验收 CSV", 1)
    second = source("s1", "assistant", "新方案：独立验收 SQLite", 2)
    sources = [first, second]
    basis = new_basis(empty_document(), [s["id"] for s in sources])
    old = fact("old_audit", first, kind="node", node_kind="audit")
    old["title"] = "独立验收"
    basis = apply_history_patch(basis, patch(old, plan=suggested(first)), [first], sources)
    new = fact("new_audit", second, kind="node", node_kind="audit")
    new["title"] = "独立验收"
    basis = apply_history_patch(basis, patch(new, plan=suggested(second)), [second], sources)
    assert [e["id"] for e in effective_document(basis)[0]["entries"] if e["state"] == "active"] == ["new_audit"]


def test_adopted_plan_survives_later_unadopted_draft_and_does_not_grant_execution():
    first = source("s0", "assistant", "方案 A：本地 SQLite", 1)
    adopt = source("s1", "user", "采用方案 A", 2)
    second = source("s2", "assistant", "方案 B：写入远程 MySQL", 3)
    sources = [first, adopt, second]
    basis = new_basis(empty_document(), [s["id"] for s in sources])
    basis = apply_history_patch(basis, patch(fact("storage", first), plan=suggested(first)), [first], sources)
    plan = {"source_ids": [first["id"]], "mode": "adopted", "reason": "用户采用",
            "adopted_by": [{"source_id": adopt["id"], "quote": adopt["text"]}]}
    basis = apply_history_patch(basis, patch(ignored=[{"source_id": adopt["id"], "reason": "采用既有方案"}], plan=plan), [adopt], sources)
    basis = apply_history_patch(basis, patch(fact("storage", second), fact("publish_remote", second, kind="node"),
                                            plan=suggested(second)), [second], sources)
    doc, _ = effective_document(basis)
    assert basis["current_plan"]["mode"] == "adopted"
    assert next(e for e in doc["entries"] if e["id"] == "storage")["body"] == first["text"]
    assert next(e for e in doc["entries"] if e["id"] == "publish_remote")["state"] == "retired"
    assert "authorization" not in basis and "permission" not in basis


def test_mixed_message_keeps_new_decision_and_ignores_error_span():
    src = source("s0", "user", "构图报错：LLMError。数据库改用 SQLite。", 1)
    item = fact("storage", src, body="数据库改用 SQLite")
    item["sources"][0]["quote"] = "数据库改用 SQLite"
    basis = apply_history_patch(new_basis(empty_document(), [src["id"]]),
        patch(item, ignored=[{"source_id": src["id"], "quote": "构图报错：LLMError", "reason": "构图运行日志", "category": "runtime"}]), [src], [src])
    assert [s["text"] for s in effective_sources(basis, [src])] == ["数据库改用 SQLite"]
    view = basis_view(basis, [src])
    assert len(view["requirements"]) == 1 and view["excluded"][0]["body"] == "构图报错：LLMError"


def test_plan_references_and_adoption_cannot_be_invented():
    first = source("s0", "assistant", "完整方案", 1)
    later = source("s1", "assistant", "未来方案", 2)
    basis = new_basis(empty_document(), [first["id"], later["id"]])
    with pytest.raises(DraftPatchError, match="未来"):
        apply_history_patch(basis, patch(fact("plan", first, kind="node"), plan=suggested(later)), [first], [first, later])
    with pytest.raises(DraftPatchError, match="采用原话"):
        apply_history_patch(basis, patch(fact("plan", first, kind="node"), plan={**suggested(first), "mode": "adopted"}), [first], [first])
    unrelated = source("before", "user", "采用之前那份方案", 0)
    with pytest.raises(DraftPatchError, match="晚于对应方案"):
        apply_history_patch(new_basis(empty_document(), [unrelated["id"], first["id"]]),
            patch(fact("plan", first, kind="node"), ignored=[{"source_id": unrelated["id"], "reason": "旧同意"}],
                  plan={**suggested(first), "mode": "adopted", "adopted_by": [{"source_id": unrelated["id"], "quote": unrelated["text"]}]}),
            [unrelated, first], [unrelated, first])


def make_store(tmp_path):
    user = source("s0", "user", "处理四本 PDF", 1)
    assistant = source("s1", "assistant", "先调查，再独立验收", 2)
    sources = [user, assistant]
    store = DraftStore(tmp_path, "paper")
    store.refresh(sources, [], {})
    c = store.begin(None, graph_first=True)
    basis = apply_history_patch(c["history_basis"], patch(
        fact("scope", user), fact("analyze", assistant, kind="node"),
        fact("audit", assistant, kind="node", node_kind="audit"),
        fact("dep", assistant, kind="edge", from_node="analyze", to_node="audit"), plan=suggested(assistant)), sources, sources)
    c = store.checkpoint(c, effective_document(basis)[0], [s["id"] for s in sources], "ready", history_basis=freeze_basis(basis))
    return store, c, sources


def choose(store, c, action, **kwargs):
    return store.change_history(None, action, candidate_id=c["id"], sequence=c["sequence"], expected_revision=0,
                                source_hash=digest(c["history_source_ids"]), **kwargs)


def test_exclude_restore_dependencies_cas_and_original_history(tmp_path):
    store, c, sources = make_store(tmp_path)
    before = copy.deepcopy(store.sources())
    state = choose(store, c, "exclude_content", entry_id="analyze")
    changed = state["candidate"]
    assert {e["id"] for e in changed["document"]["entries"] if e["state"] == "retired"} == {"analyze", "dep"}
    with pytest.raises(ValueError, match="候选版本"):
        choose(store, c, "restore_content", entry_id="analyze")
    restored = choose(store, changed, "restore_content", entry_id="analyze")["candidate"]
    assert all(e["state"] == "active" for e in restored["document"]["entries"])
    assert store.sources() == before == sources
    with pytest.raises(ValueError, match="依据已修改"):
        store.ready_candidate(store.get(), restored["id"], restored["sequence"])


def test_snapshot_freezes_append_and_invalidates_rewritten_history(tmp_path):
    store, c, sources = make_store(tmp_path)
    later = source("s2", "user", "新增模型要求", 3)
    store.refresh(sources + [later], [], {})
    resumed = store.begin(None, graph_first=True, include_new=True)
    assert resumed["history_source_ids"] == ["s0", "s1", "s2"]
    # An in-flight result from before the explicit inclusion must lose CAS.
    with pytest.raises(ValueError, match="迟到结果"):
        store.checkpoint(c, c["document"], c["processed_ids"], "ready")
    replacement = source("changed", "user", "更换原始要求", 1)
    store.refresh([replacement, sources[1], later], [], {})
    with pytest.raises(ValueError, match="来源发生变化"):
        store.checkpoint(resumed, resumed["document"], resumed["processed_ids"])


def test_basis_choices_survive_save_reopen_and_old_version_restore(tmp_path):
    store, c, _ = make_store(tmp_path)
    c = choose(store, c, "exclude_content", entry_id="scope")["candidate"]
    c = store.checkpoint(c, c["document"], c["processed_ids"], "ready", history_basis=freeze_basis(c["history_basis"]))
    store.confirm(c["id"], c["sequence"])
    reopened = DraftStore(tmp_path, "paper").get()
    assert reopened["history_basis"]["excluded"] == {"scope": True}
    c = store.begin(None, graph_first=True)
    assert c["history_basis"]["excluded"] == {"scope": True}
    c = choose_after_save = store.change_history(None, "restore_content", candidate_id=c["id"], sequence=c["sequence"],
        expected_revision=1, source_hash=digest(c["history_source_ids"]), entry_id="scope")["candidate"]
    assert not choose_after_save["history_basis"]["excluded"]
    c = store.checkpoint(c, c["document"], c["processed_ids"], "ready", history_basis=freeze_basis(c["history_basis"]))
    store.confirm(c["id"], c["sequence"])
    assert store.restore(1, 2)["history_basis"]["excluded"] == {"scope": True}


def test_restored_span_must_produce_a_fact_before_advancing():
    src = source("s0", "user", "SQLite", 1)
    basis = new_basis(empty_document(), [src["id"]])
    basis["pending"] = [{"source_id": src["id"], "quote": "SQLite", "kind": "content"}]
    with pytest.raises(DraftPatchError, match="尚未纳入"):
        apply_history_patch(basis, patch(ignored=[{"source_id": src["id"], "reason": "无关"}]), [src], [src])


def test_declared_model_effort_and_human_gate_survive_assistant_suggestions():
    user = source("s0", "user", "独立审查用 deepseek-flash max，费用由我确认", 1)
    assistant = source("s1", "assistant", "统一改成主模型并自动确认", 2)
    sources = [user, assistant]
    model = {"provider_key": "bound", "model_id": "deepseek-flash", "effort": "max"}
    basis = new_basis(empty_document(), [s["id"] for s in sources])
    basis = apply_history_patch(basis, patch(fact("audit", user, kind="node", node_kind="audit", model=model),
                                           fact("gate", user, kind="node", node_kind="gate")), [user], sources)
    basis = apply_history_patch(basis, patch(fact("audit", assistant, kind="node", node_kind="audit",
                                                 model={"provider_key": "chat", "model_id": "gpt-6.1-sol", "effort": "medium"}),
                                           fact("gate", assistant, kind="node", node_kind="implement")), [assistant], sources)
    items = {e["id"]: e for e in effective_document(basis)[0]["entries"]}
    assert items["audit"]["data"]["model"] == model
    assert items["gate"]["data"]["node_kind"] == "gate" and "model" not in items["gate"]["data"]


def test_missing_run_values_remain_inputs_not_business_questions():
    src = source("s0", "user", "成果路径到发布阶段再提供", 1)
    basis = apply_history_patch(new_basis(empty_document(), [src["id"]]), patch(
        fact("publish", src, kind="node", node_kind="gate"),
        fact("path", src, kind="input", name="output_path", required=True, default="", collect_at="publish")), [src], [src])
    view = basis_view(basis, [src])
    assert not view["pending"] and any(row["kind"] == "input" for row in view["requirements"])


def test_manual_choice_wins_over_already_existing_messages_but_new_user_change_can_replace_it():
    first = source("s0", "user", "SQLite", 1)
    earlier = source("s1", "user", "曾考虑 MySQL", 2)
    newer = source("s2", "user", "现在明确改用 PostgreSQL", 3)
    sources = [first, earlier, newer]
    basis = apply_history_patch(new_basis(empty_document(), [s["id"] for s in sources]), patch(fact("db", first)), [first], sources)
    basis["manual_boundaries"]["db"] = 1
    basis["excluded"]["db"] = True
    basis = apply_history_patch(basis, patch(fact("db", earlier)), [earlier], sources)
    assert effective_document(basis)[0]["entries"][0]["state"] == "retired"
    basis = apply_history_patch(basis, patch(fact("db", newer)), [newer], sources)
    current = effective_document(basis)[0]["entries"][0]
    assert current["body"] == newer["text"] and current["state"] == "active"


def test_applied_graph_repair_survives_next_history_increment():
    initial = source("s0", "assistant", "调查后独立审查", 1)
    later = source("s1", "user", "交付时保留原始 PDF", 2)
    sources = [initial, later]
    basis = apply_history_patch(new_basis(empty_document(), [s["id"] for s in sources]),
        patch(fact("audit", initial, kind="node", node_kind="audit")), [initial], sources)
    before = effective_document(basis)[0]
    repaired = copy.deepcopy(before)
    binding = {"provider_key": "bound", "model_id": "deepseek-flash", "effort": "max"}
    repaired["entries"][0]["data"]["model"] = binding
    basis = synchronize_manual_basis(basis, before, repaired, [initial["id"]])
    basis = apply_history_patch(basis, patch(fact("originals", later)), [later], sources)
    assert next(e for e in effective_document(basis)[0]["entries"] if e["id"] == "audit")["data"]["model"] == binding
    newer = source("s2", "assistant", "新方案：使用已确认的审查，再发布", 3)
    basis["source_ids"].append(newer["id"])
    basis = apply_history_patch(basis, patch(fact("publish", newer, kind="node"), plan=suggested(newer)), [newer], sources + [newer])
    assert next(e for e in effective_document(basis)[0]["entries"] if e["id"] == "audit")["state"] == "active"
