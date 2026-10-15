#!/usr/bin/env python3
"""
External charter rating: the prompt rate_charters.py gives each seat (same system
prompt, rubric and reply format) sent to one outside rater model (default Kimi K3,
the juror model), one fresh call per charter. The prompt holds only the charter's
name and text: the seats' self-ratings and their rationales are never sent.

Writes data/charter_ratings_<model>.json (the seat configs are never changed) and
prints the comparison with each seat's self-rating.

  python tools/rate_charters_external.py              # Kimi K3, all five charters
  python tools/rate_charters_external.py --model kimi-k3 --lab xai
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rate_charters as rc                                   # noqa: E402
from core.config import LAB_ORDER, SIM_DIR, load_lab_configs  # noqa: E402
from core.costs import configure, get_tracker                 # noqa: E402
from core.logsetup import configure_logging                   # noqa: E402

AXES = rc.AXES


def rate_external(cfg: dict, model: str) -> dict:
    """One fresh call: the seat's charter rated by `model` with rate_charters' prompt.
    Only the charter's name and file are passed on; the seat's own values and
    rationale stay out of the prompt."""
    rater_cfg = {"key": cfg["key"], "charter_name": cfg["charter_name"], "charter_file": cfg.get("charter_file"),
                 "model": model, "provider": None, "effort": None}
    values, notes = rc.rate_lab(rater_cfg, run_id=f"charter-rating-{model}")
    return {"values": values, "rationale": notes}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Rate every charter with one external model (no self-ratings sent).")
    p.add_argument("--model", default="kimi-k3")
    p.add_argument("--lab", choices=LAB_ORDER, action="append")
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)
    configure_logging("rate-charters-external")
    configure()
    out = Path(args.out) if args.out else SIM_DIR / "data" / f"charter_ratings_{args.model}.json"
    cfgs = {c["key"]: c for c in load_lab_configs()}
    keys = args.lab or [k for k in LAB_ORDER if k in cfgs]
    saved = json.loads(out.read_text()) if out.exists() else {}
    results = dict(saved.get("ratings") or {})
    start = get_tracker().persisted_total()
    for k in keys:
        t = time.monotonic()
        try:
            results[k] = dict(rate_external(cfgs[k], args.model), rated=time.strftime("%Y-%m-%d %H:%M"),
                              charter_name=cfgs[k]["charter_name"])
            print(f"{k:10} {results[k]['values']}  ({time.monotonic() - t:.0f}s)", flush=True)
        except Exception as e:      # noqa: BLE001 — report and continue with the next charter
            print(f"{k:10} FAILED: {type(e).__name__}: {e}", flush=True)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"model": args.model,
                                   "prompt": "rate_charters._SYSTEM and _user, identical to the seats' self-rating",
                                   "ratings": results}, indent=2))
    print(f"\nwrote {out}; spend ${get_tracker().persisted_total() - start:.2f}")

    # Comparison with each seat's self-rating, read only after every external call.
    print(f"\n{'lab':10} {'axis':24} {'self':>5} {args.model:>8} {'diff':>6}")
    for k in keys:
        if k not in results:
            continue
        own = cfgs[k].get("charter_values") or {}
        for a in AXES:
            s, e = own.get(a), results[k]["values"].get(a)
            d = (e - s) if isinstance(s, (int, float)) and isinstance(e, (int, float)) else None
            print(f"{k:10} {a:24} {s!s:>5} {e!s:>8} {('%+d' % d) if d is not None else '–':>6}")
    return 0


if __name__ == "__main__":
    from core.cli import run_main
    raise SystemExit(run_main(main))
