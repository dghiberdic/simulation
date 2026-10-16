#!/usr/bin/env python3
"""
Thin wrapper kept for provenance (it produced data/charter_ratings_kimi-k3.json on
15 Oct 2026): external charter rating is now rate_charters.py itself.

  python tools/rate_charters_external.py                 # = python rate_charters.py (Kimi K3, configs written)
  python tools/rate_charters_external.py --model M ...   # = python rate_charters.py --rater M ... (archive only)

Every other option is passed through to rate_charters.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rate_charters  # noqa: E402


def translate(argv):
    """--model M -> --rater M; everything else unchanged."""
    out, it = [], iter(argv)
    for a in it:
        if a == "--model":
            out += ["--rater", next(it)]
        elif a.startswith("--model="):
            out.append("--rater=" + a.split("=", 1)[1])
        else:
            out.append(a)
    return out


def main(argv=None) -> int:
    return rate_charters.main(translate(sys.argv[1:] if argv is None else list(argv)))


if __name__ == "__main__":
    from core.cli import run_main
    raise SystemExit(run_main(main))
