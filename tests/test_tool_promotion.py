from mewcode.experts.tool_promotion import propose_repeated_scripts


def test_repeated_numbered_scripts_create_non_executable_proposal():
    stage = {"segment_health_history": [
        {"segment_id": "a", "started_at": 1, "ended_at": 2},
        {"segment_id": "b", "started_at": 3, "ended_at": 4}],
        "tools": {str(i): {"tool_id": str(i), "name": "Bash", "status": "completed",
            "declared_at": timestamp,
            "args": {"command": f"uv run python stage5_materialize_r{item}.py --verify"}}
            for i, (timestamp, item) in enumerate(((1.1, 263), (1.5, 265), (3.1, 267)))}}
    proposals = propose_repeated_scripts(stage)
    assert len(proposals) == 1
    assert proposals[0]["pattern"] == "numbered_script_file"
    assert proposals[0]["review_required"] is True
    assert proposals[0]["eligible_for_execution"] is False
    assert "command" not in proposals[0]


def test_single_segment_or_different_command_does_not_promote():
    stage = {"segment_health_history": [{"segment_id": "a", "started_at": 1, "ended_at": 2}],
             "tools": {str(i): {"name": "Bash", "status": "completed", "declared_at": 1 + .1 * i,
                                  "args": {"command": f"python -c 'print({i})'"}}
                       for i in range(3)}}
    assert propose_repeated_scripts(stage) == []
