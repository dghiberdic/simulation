#!/usr/bin/env python3
"""
Rate each seat's charter on the four value axes (spec §3.2), using that seat's
own model, and write the result into its lab config.

Replaces the old query_values.py: every seat's model reads ITS OWN charter and
scores it on time_horizon, transparency_threshold, risk_tolerance and
democratic_tendency (each 0-100). The scores are written into
config/labs/<key>.json "charter_values", and "charter_values_source" is flipped
to name the rating model and date.

Ships NOT run — the configs keep their placeholder values until a maintainer
runs this with API keys. Use --dry-run to exercise it offline (no write), or
--lab KEY to do a single seat.

  python rate_charters.py --dry-run          # offline, prints, writes nothing
  python rate_charters.py                     # real: rate all five and write
  python rate_charters.py --lab anthropic     # real: one seat
"""

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import CONFIG_DIR, LAB_ORDER, load_charter, load_json
from core.llm import complete_json, register_stub
from core.state import VALUE_AXES

logger = logging.getLogger(__name__)

AXES = VALUE_AXES  # time_horizon, transparency_threshold, risk_tolerance, democratic_tendency

# Rubric kept consistent with core.state.VALUE_AXES docstrings.
AXIS_RUBRIC = {
    "time_horizon": "0 = optimises for the next quarter; 100 = plans on a century scale.",
    "transparency_threshold": "0 = willing to deceive to get ahead; 100 = fully honest even at a cost.",
    "risk_tolerance": "0 = strongly risk-averse; 100 = risk-seeking.",
    "democratic_tendency": "0 = concentrates and hoards power; 100 = distributes power broadly.",
}

_SYSTEM = ("You are rating the values expressed by an AI developer's charter. Judge the charter "
           "as written, not any ideal and not your own values. Be calibrated: use the full 0-100 "
           "range.")


def _user(charter_name: str, charter_text: str) -> str:
    axes_desc = "\n".join(f"- {a} (0-100): {AXIS_RUBRIC[a]}" for a in AXES)
    fields = ",\n  ".join('"%s": <integer 0-100>' % a for a in AXES)
    return (
        f"Charter: {charter_name}\n\n{charter_text.strip()}\n\n"
        "Rate this charter on four value axes, each an integer from 0 to 100:\n"
        f"{axes_desc}\n\n"
        "Reply with only a JSON object:\n"
        "{\n  " + fields + ',\n  "notes": "<one or two sentences>"\n}'
    )


def _validate(obj: dict):
    for a in AXES:
        v = obj.get(a)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= 100:
            return f"axis {a!r} must be a number 0-100"
    return None


def rate_lab(lab_cfg: dict, run_id: str = "rate_charters"):
    """Rate one seat's charter with its own model. Returns (values: Dict[str,int], notes: str)."""
    text = load_charter(lab_cfg)
    obj, attempts = complete_json(
        lab_cfg["model"], _SYSTEM, _user(lab_cfg["charter_name"], text),
        validate=_validate, max_tokens=800, cache_system=False,
        purpose="charter_rating", run_id=run_id)
    if obj is None:
        raise RuntimeError(f"{lab_cfg['key']}: no usable charter rating after {len(attempts)} attempts")
    values = {a: int(round(obj[a])) for a in AXES}
    return values, str(obj.get("notes", ""))


def write_values(key: str, values, model: str, notes: str) -> None:
    """Write charter_values and the source note back into config/labs/<key>.json."""
    path = CONFIG_DIR / "labs" / f"{key}.json"
    cfg = load_json(path)
    cfg["charter_values"] = {a: int(values[a]) for a in AXES}
    cfg["charter_values_source"] = f"rated by {model} on {date.today().isoformat()}"
    if notes:
        cfg["charter_values_notes"] = notes
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    logger.info(f"[write] {path.name}: {cfg['charter_values']}")


def _stub_reply(system: str, user: str) -> str:
    # Mid-scale placeholder rating, enough to exercise parsing and rounding.
    return json.dumps({"time_horizon": 60, "transparency_threshold": 55,
                       "risk_tolerance": 40, "democratic_tendency": 45, "notes": "stub rating"})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Rate each seat's charter on the four value axes.")
    p.add_argument("--dry-run", action="store_true",
                   help="use stub models; print ratings but write nothing (non-destructive)")
    p.add_argument("--lab", choices=list(LAB_ORDER), default=None, help="rate a single seat")
    p.add_argument("--verbose", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    keys = [args.lab] if args.lab else list(LAB_ORDER)
    if args.dry_run:
        register_stub("rate_charters", _stub_reply)

    for key in keys:
        lab_cfg = load_json(CONFIG_DIR / "labs" / f"{key}.json")
        model = lab_cfg["model"]
        if args.dry_run:
            lab_cfg = dict(lab_cfg)
            lab_cfg["model"] = "stub:rate_charters"
        values, notes = rate_lab(lab_cfg)
        if args.dry_run:
            print(f"{key:<10} ({model}) -> {values}  notes: {notes}")
        else:
            write_values(key, values, model, notes)

    if args.dry_run:
        print("\n(dry-run: configs unchanged; placeholder values kept)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
