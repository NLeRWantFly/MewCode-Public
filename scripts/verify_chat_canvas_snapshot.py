"""Read-only live SQLite backup; validate the new projection in a separate folder."""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from mewcode.orchestration import chat_canvas


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("conversation")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    original = args.project / ".mewcode/node-activity.sqlite3"
    graphs = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (args.project / ".mewcode/pipelines").glob("*.json")}
    destination = args.output / ".mewcode/node-activity.sqlite3"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError("Use a fresh validation directory")
    with sqlite3.connect(original.resolve().as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(destination) as target:
            source.backup(target)
    graph = chat_canvas.ensure_graph(args.output, args.conversation)
    snapshot = chat_canvas.snapshot(args.output, args.conversation, graph)
    data = chat_canvas.projection(args.output, args.conversation)
    partitions = {}
    for event in data["events"]:
        partitions[event["run_id"]] = partitions.get(event["run_id"], 0) + 1
    assert graphs == {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in (args.project / ".mewcode/pipelines").glob("*.json")}
    assert sum(s["activity_count"] for s in snapshot["node_states"].values()) == len(data["events"])
    assert not any(s["status"] == "completed" for s in snapshot["node_states"].values())
    print(json.dumps({"graph_id": graph.graph_id, "events": len(data["events"]), "partitions": partitions,
                      "units": list(data["units"]), "shared": snapshot["node_states"].get(chat_canvas.SHARED, {}).get("activity_count", 0),
                      "files": len(snapshot["process_files"]), "source_graphs_unchanged": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
