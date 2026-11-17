"""Export a single run from node-activity.sqlite3 without raw content."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from mewcode.experts.recovery_replay import export_activity


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="exact read-only node-activity.sqlite3 path")
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--run-id", required=True, help="exact run identifier to export")
    parser.add_argument("--conversation-id", help="optional exact conversation identifier")
    args = parser.parse_args()
    manifest = export_activity(args.database, args.output_dir,
        run_id=args.run_id, conversation_id=args.conversation_id)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
