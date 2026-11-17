"""CLI entry point usable from an SSD workspace without copying the application."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mewcode.bookflow.project import main

if __name__ == '__main__':
    main()
