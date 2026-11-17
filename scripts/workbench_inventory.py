"""Validate the frozen T81 inventory; --after recounts replacement code for T94."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/workbench-baseline-20261013.json"


def union(ranges):
    result = []
    for start, end in sorted(ranges):
        if result and start <= result[-1][1] + 1:
            result[-1][1] = max(end, result[-1][1])
        else:
            result.append([start, end])
    return result


def count(ranges):
    return sum(end - start + 1 for start, end in union(ranges))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--after", action="store_true")
    args = parser.parse_args()
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not args.after:
        for row in data["files"]:
            path = ROOT / row["path"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"], row["path"]
            assert len(path.read_text(encoding="utf-8-sig").splitlines()) == row["lines"]
            assert count(row["counted_ranges"]) == row["counted_lines"], row["path"]
        assert sum(row["counted_lines"] for row in data["files"]) == data["total_lines"]
        assert all(row["destination"] and row["replacement_tasks"] for row in data["surfaces"])
        assert len(data["answer_paths"]) >= 16
        print(json.dumps({"valid": True, "total_lines": data["total_lines"],
                          "surfaces": len(data["surfaces"]), "answer_paths": len(data["answer_paths"])},
                         ensure_ascii=False))
        return
    # Count all replacements, even if moved outside the workbench directory.
    paths = {row["path"] for row in data["files"] if (ROOT / row["path"]).exists()}
    paths.update(path.relative_to(ROOT).as_posix() for path in (ROOT / "frontend/static/workbench").rglob("*")
                 if path.is_file() and path.suffix in {".js", ".css", ".html"})
    paths.update(data.get("additional_replacement_files", []))
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node is required to recount app.js functions")
    parsed = json.loads(subprocess.check_output([node, "scripts/workbench_inventory.cjs"], cwd=ROOT))
    names = {row["name"] for row in data["app_functions"] if row["counted"]}
    names.update(data.get("additional_app_functions", []))
    rows = []
    original = {row["path"]: row for row in data["files"]}
    for path in sorted(paths):
        source = (ROOT / path).read_text(encoding="utf-8-sig")
        lines = len(source.splitlines())
        if path == "frontend/static/app.js":
            ranges = [[row["start"], row["end"]] for row in parsed if row["depth"] == 1 and row["name"] in names]
            ranges.extend(data.get("after_app_extra_ranges", []))
        elif path in original and not original[path]["count_full_file"]:
            # Shared CSS boundaries are frozen. A changed shared file needs an
            # explicit reviewed range list; silently counting stale offsets is unsafe.
            ranges = data.get("after_shared_ranges", {}).get(path)
            if ranges is None:
                assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == original[path]["sha256"], (
                    "Set reviewed after_shared_ranges for " + path)
                ranges = original[path]["counted_ranges"]
        else:
            ranges = [[1, lines]] if lines else []
        assert all(1 <= start <= end <= lines for start, end in ranges), path
        rows.append({"path": path, "lines": lines, "counted_ranges": union(ranges), "counted_lines": count(ranges)})
    data["after"] = {"files": rows, "total_lines": sum(row["counted_lines"] for row in rows)}
    data["after"]["ratio"] = round(data["after"]["total_lines"] / data["total_lines"], 4)
    MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in data["after"].items() if key != "files"}))


if __name__ == "__main__":
    main()
