"""Print a metadata-only paused-handoff replay report; never resume the run."""
from __future__ import annotations

import argparse
import json

from mewcode.orchestration.paused_handoff_replay import replay_paused_handoff_sync


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_path")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--contract-fingerprint", required=True)
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--end", type=int, required=True)
    parser.add_argument("--critical-record", type=int, action="append", default=[])
    args = parser.parse_args()
    report = replay_paused_handoff_sync(
        log_path=args.log_path, run_id=args.run_id,
        conversation_id=args.conversation_id,
        contract_fingerprint=args.contract_fingerprint,
        start=args.start, end=args.end,
        critical_record_ordinals=args.critical_record,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
