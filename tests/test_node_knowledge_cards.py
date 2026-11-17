from mewcode.experts.node_knowledge_cards import observe_knowledge, select_cards


def test_two_distinct_successful_segments_create_fingerprint_bound_card(tmp_path):
    path = tmp_path / "page.txt"
    path.write_text("source", encoding="utf-8")
    stage = {"input_revision": "input-v1", "segment_health_history": [
        {"segment_id": "s1", "started_at": 1, "ended_at": 2},
        {"segment_id": "s2", "started_at": 3, "ended_at": 4}],
        "tools": {"a": {"tool_id": "a", "name": "ReadFile", "status": "completed",
                         "declared_at": 1.5, "args": {"path": "page.txt"}},
                  "b": {"tool_id": "b", "name": "ReadFile", "status": "completed",
                         "declared_at": 3.5, "args": {"path": "page.txt"}}}}
    stage["segment_health_history"] = stage["segment_health_history"][:1]
    first = observe_knowledge(stage, root=tmp_path)
    stage["knowledge_observations"] = first["observations"]
    stage["segment_health_history"].append({"segment_id": "s2", "started_at": 3, "ended_at": 4})
    learned = observe_knowledge(stage, root=tmp_path)
    assert len(learned["cards"]) == 1
    stage["knowledge_cards"] = learned["cards"]
    assert select_cards(stage, input_revision="input-v1", root=tmp_path)
    assert not select_cards(stage, input_revision="input-v2", root=tmp_path)
    path.write_text("changed", encoding="utf-8")
    assert not select_cards(stage, input_revision="input-v1", root=tmp_path)


def test_model_claim_or_single_segment_is_not_verified(tmp_path):
    (tmp_path / "image.png").write_bytes(b"png")
    stage = {"input_revision": "v1", "segment_health_history": [
        {"segment_id": "s1", "started_at": 1, "ended_at": 2}],
        "tools": {"a": {"tool_id": "a", "name": "ReadFile", "status": "failed",
                         "declared_at": 1.1, "args": {"path": "image.png"}},
                  "b": {"tool_id": "b", "name": "ReadAuditArtifact", "status": "completed",
                         "declared_at": 1.5, "args": {"path": "image.png"}}},
        "text": "I learned page_idx = physical page minus one"}
    assert observe_knowledge(stage, root=tmp_path)["cards"] == []


def test_verified_page_offset_uses_queue_item_and_source_fingerprint(tmp_path):
    (tmp_path / "pages.json").write_text("[]", encoding="utf-8")
    stage = {"input_revision": "v1", "segment_health_history": [
        {"segment_id": "s1", "started_at": 1, "ended_at": 2},
        {"segment_id": "s2", "started_at": 3, "ended_at": 4}],
        "tools": {str(i): {"tool_id": str(i), "name": "ReviewWorkQueue",
            "status": "completed", "declared_at": timestamp,
            "args": {"action": "evidence", "item_id": "p3", "source_path": "pages.json",
                     "source_selector": {"page_idx": 2}}}
            for i, timestamp in enumerate((1.5, 3.5))}}
    stage["segment_health_history"] = stage["segment_health_history"][:1]
    first = observe_knowledge(stage, root=tmp_path,
                              queue={"items": [{"item_id": "p3", "pdf_page": 3}]})
    stage["knowledge_observations"] = first["observations"]
    stage["segment_health_history"].append({"segment_id": "s2", "started_at": 3, "ended_at": 4})
    result = observe_knowledge(stage, root=tmp_path,
                               queue={"items": [{"item_id": "p3", "pdf_page": 3}]})
    assert result["cards"][0]["kind"] == "page_zero_offset"
