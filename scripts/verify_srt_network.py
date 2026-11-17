"""Explicit synthetic-only Windows network acceptance; no business uploads."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mewcode.sandbox.srt_acceptance import verify_runtime

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--publish', action='store_true')
    options = parser.parse_args()
    result = verify_runtime(options.output, publish=options.publish)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result['ready'] else 1)
