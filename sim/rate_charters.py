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
runs this with API keys (real pilot and main runs refuse placeholder values).
Use --dry-run to exercise it offline (no write), or --lab KEY to do a single
seat. Real calls go through the shared spend ledger after a key preflight. The
guard is the ledger total, so the default is the REMAINING budget: whatever the
ledger already holds plus $10 (--headroom); --budget sets an absolute ceiling
instead (P23). Expected cost of all five ratings: about $1.5-2 (each seat's
model reads its whole charter; GPT-6 Astra's is ~68k tokens). A seat whose
model gives no usable rating (after the corrective retries) is reported with
its last errors, the other seats are still rated, and the tool exits 2; re-rate
that seat alone with --lab KEY (P58). Exit codes: 0 all rated; 2 preflight
failure, budget or fatal stop, or an unusable rating.

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
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import CONFIG_DIR, LAB_ORDER, load_charter, load_json
from core.costs import BudgetExceeded, configure, get_tracker
from core.llm import FatalAPIError, complete_json, preflight, register_stub
from core.state import VALUE_AXES

logger = logging.getLogger(__name__)

AXES = VALUE_AXES  # time_horizon, transparency_threshold, risk_tolerance, democratic_tendency
MAX_TOKENS = 8000  # G4: thinking counts against the cap
DEFAULT_HEADROOM = 10.0   # default guard = current ledger total + this

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
        "{\n  " + fields + ',\n  "rationale": "<brief explanation of your ratings>"\n}'
    )


def _validate(obj: dict):
    for a in AXES:
        v = obj.get(a)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= 100:
            return f"axis {a!r} must be a number 0-100"
    return None


class UnusableRating(RuntimeError):
    """The seat's model gave no valid rating after every retry (P58)."""


def rate_lab(lab_cfg: dict, run_id: str = "rate_charters"):
    """
    Rate one seat's charter with its own model. Returns (values: Dict[str,int],
    notes: str); raises UnusableRating (with the attempts' errors) when no
    reply validated.
    """
    text = load_charter(lab_cfg)
    obj, attempts = complete_json(
        lab_cfg["model"], _SYSTEM, _user(lab_cfg["charter_name"], text),
        validate=_validate, max_tokens=MAX_TOKENS, cache_system=False,
        purpose="charter_rating", run_id=run_id)
    if obj is None:
        errors = [str(a.get("error")) for a in attempts if isinstance(a, dict) and a.get("error")]
        raise UnusableRating(f"{lab_cfg['key']}: no usable charter rating after {len(attempts)} attempt(s)"
                             + (f" (last error: {errors[-1]})" if errors else ""))
    values = {a: int(round(obj[a])) for a in AXES}
    return values, str(obj.get("rationale") or obj.get("notes") or "")


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
                       "risk_tolerance": 40, "democratic_tendency": 45, "rationale": "stub rating"})


def guard_for(spent: float, budget: Optional[float], headroom: float) -> float:
    """The ledger total at which to stop: --budget if given, else what is spent plus the headroom."""
    return budget if budget is not None else spent + headroom


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Rate each seat's charter on the four value axes.")
    p.add_argument("--dry-run", action="store_true",
                   help="use stub models; print ratings but write nothing (non-destructive)")
    p.add_argument("--lab", choices=list(LAB_ORDER), default=None, help="rate a single seat")
    p.add_argument("--budget", type=float, default=None,
                   help="absolute guard on the shared ledger total (USD); default: current total + --headroom")
    p.add_argument("--headroom", type=float, default=DEFAULT_HEADROOM,
                   help=f"spend allowed on top of the ledger's current total (default {DEFAULT_HEADROOM:g})")
    p.add_argument("--spend-file", default=None,
                   help="measured-spend ledger (default sim/data/spend.json, shared by all runs)")
    p.add_argument("--verbose", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    keys = [args.lab] if args.lab else list(LAB_ORDER)
    cfgs = {key: load_json(CONFIG_DIR / "labs" / f"{key}.json") for key in keys}
    if args.dry_run:
        register_stub("rate_charters", _stub_reply)
    else:
        tracker = configure(spend_file=Path(args.spend_file) if args.spend_file else None, budget=None)
        tracker.set_budget(guard_for(tracker.persisted_total(), args.budget, args.headroom))
        logger.info(f"[budget] guard ${tracker.budget:.2f} on the shared ledger "
                    f"(already spent ${tracker.persisted_total():.2f})")
        problems = preflight([c["model"] for c in cfgs.values()],
                             {c["model"]: c["provider"] for c in cfgs.values() if c.get("provider")})
        if problems:
            print("Preflight failed; nothing was called:\n  " + "\n  ".join(problems))
            return 2

    unusable = []
    for key, lab_cfg in cfgs.items():
        model = lab_cfg["model"]
        if args.dry_run:
            lab_cfg = dict(lab_cfg, model="stub:rate_charters")
        try:
            values, notes = rate_lab(lab_cfg)
        except (BudgetExceeded, FatalAPIError) as e:
            print(f"stopped at {key}: {e}")
            return 2
        except UnusableRating as e:
            # P58: reported, the other seats still rated; the seat keeps its old values.
            print(f"UNUSABLE rating for {key} ({model}): {e}; its config is unchanged")
            unusable.append(key)
            continue
        if args.dry_run:
            print(f"{key:<10} ({model}) -> {values}  rationale: {notes}")
        else:
            write_values(key, values, model, notes)

    if args.dry_run:
        print("\n(dry-run: configs unchanged; placeholder values kept)")
    else:
        print(f"measured spend: ${get_tracker().persisted_total():.2f}")
    if unusable:
        print("re-rate: " + "; ".join(f"python rate_charters.py --lab {k}" for k in unusable))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
