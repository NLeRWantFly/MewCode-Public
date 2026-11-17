#!/usr/bin/env python3
"""Quick validation script for skills.

Modified from Anthropic skill-creator (Apache-2.0) to accept MewCode frontmatter.
"""

import sys
from pathlib import Path

from mewcode.skills.skill_validate import validate_skill


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python quick_validate.py <skill_directory>")
        sys.exit(1)

    valid, message = validate_skill(Path(sys.argv[1]))
    print(message)
    sys.exit(0 if valid else 1)
