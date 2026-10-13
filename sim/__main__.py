"""python -m sim (from the repo root): the `sim` command (cli.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cli import main  # noqa: E402

raise SystemExit(main())
