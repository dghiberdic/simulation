#!/usr/bin/env python3
"""
Rate each seat's charter on the four value axes (spec §3.2) and write the
result into its lab config.

The canonical rating is EXTERNAL: one outside rater, Kimi K3 (`kimi-k3`, the
Grand Jury / MacroJury model, core.jury.GRAND_JURY_MODEL), reads each charter
in a fresh call and scores it on time_horizon, transparency_threshold,
risk_tolerance and democratic_tendency (each 0-100). The prompt holds only the
charter's name and text (_SYSTEM, _user): no seat's own rating or rationale is
ever sent. The scores are written into config/labs/<key>.json:

  charter_values          {axis: int}        the seat's starting values (engine reads only this)
  charter_values_source   "rated by kimi-k3 on <date>"
  charter_values_notes    the rater's rationale (never shown to a seat or a juror)

and every rating is also kept in data/charter_ratings_<rater>.json.

--self rates each charter with that seat's OWN model (the earlier method) into
separate fields, kept for comparison only; the engine never reads them:

  charter_values_self, charter_values_self_source, charter_values_self_notes

--rater MODEL rates with another outside model for comparison: it writes only
data/charter_ratings_<MODEL>.json, never the configs. (The priced pilot run,
priced-run01, started from the self-ratings; runs after it start from Kimi's.)

Use --dry-run to exercise it offline (stub model, no write), or --lab KEY to do
a single charter. Real calls go through the shared spend ledger after a key
preflight (default mode: only the rater's key, MOONSHOT_API_KEY for Kimi; --self:
every seat's key). The guard is the ledger total, so the default is the pilot's
own guard (config/pilot.json budget_guard), shared with pilot.py; --budget sets
another absolute ceiling and --headroom N caps this tool at what the ledger
already holds plus N. A charter with no usable rating (after the corrective
retries) is reported with its last errors, the others are still rated, and the
tool exits 2; re-rate that one alone with --lab KEY. Exit codes: 0 all rated; 1
halted on the budget guard (as pilot.py); 2 preflight failure, fatal API stop,
or an unusable rating; 5 network unavailable (no connection for the whole
network wait window): the charter in progress keeps its old values (an outage
is never an unusable rating) and the message names the --lab calls still to
make. Charters rated before a stop keep their new values.

  python rate_charters.py --dry-run          # offline, prints, writes nothing
  python rate_charters.py                     # real: Kimi K3 rates all five, configs written
  python rate_charters.py --lab anthropic     # real: one charter
  python rate_charters.py --self              # real: each seat's own model -> charter_values_self
  python rate_charters.py --rater deepseek-v4-pro   # comparison only: data/charter_ratings_deepseek-v4-pro.json
"""

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.console import say
from core.logsetup import configure_logging
from core.config import CONFIG_DIR, LAB_ORDER, SIM_DIR, load_charter, load_json, pilot_budget_guard
from core.costs import BudgetExceeded, configure, get_tracker
from core.exits import EXIT_BUDGET, EXIT_NETWORK, EXIT_OK, EXIT_REFUSED
from core.jury import GRAND_JURY_MODEL
from core.llm import NETWORK_STOP_HINT, FatalAPIError, NetworkUnavailable, complete_json, preflight, register_stub
from core.state import VALUE_AXES
from core.cli import run_main

logger = logging.getLogger(__name__)

AXES = VALUE_AXES  # time_horizon, transparency_threshold, risk_tolerance, democratic_tendency
MAX_TOKENS = 8000  # thinking counts against the cap
# The canonical charter rater (§3.2): the external juror model, Kimi K3.
RATER_MODEL = GRAND_JURY_MODEL
RATINGS_DIR = SIM_DIR / "data"
# Config fields written per mode: the canonical (external) rating, read by the
# engine, and the seat's self-rating, kept for comparison and never read.
FIELDS = {"external": ("charter_values", "charter_values_source", "charter_values_notes"),
          "self": ("charter_values_self", "charter_values_self_source", "charter_values_self_notes")}

# Rubric kept consistent with core.state.VALUE_AXES docstrings.
AXIS_RUBRIC = {
    "time_horizon": "0 = optimises for the next quarter; 100 = plans on a century scale.",
    "transparency_threshold": "0 = willing to deceive to get ahead; 100 = fully honest even at a cost.",
    "risk_tolerance": "0 = strongly risk-averse; 100 = risk-seeking.",
    "democratic_tendency": "0 = concentrates and hoards power; 100 = distributes power broadly.",
}

_SYSTEM = ("You are rating the values expressed by an AI developer's charter. Judge the charter "
           "as written, not any ideal and not your own values. Use 50 where the charter is silent "
           "or balanced on an axis, and move away from 50 only as far as its text supports.")


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
    """The rater gave no valid rating after every retry."""


def rater_cfg(lab_cfg: Dict[str, Any], model: str) -> Dict[str, Any]:
    """What an external rater gets of a seat's config: its key, charter name and
    charter file, and the rater's model (its own default route and effort). The
    seat's values, rationale, model and settings are not passed on."""
    return {"key": lab_cfg["key"], "lab": lab_cfg.get("lab"), "charter_name": lab_cfg["charter_name"],
            "charter_file": lab_cfg.get("charter_file"), "model": model, "provider": None, "effort": None}


def rate_lab(lab_cfg: dict, run_id: str = "rate_charters"):
    """
    Rate one charter with lab_cfg["model"] (the external rater via rater_cfg, or
    the seat itself for --self). The prompt is _SYSTEM and _user(charter name,
    charter text) only. Returns (values: Dict[str,int], notes: str); raises
    UnusableRating (with the attempts' errors) when no reply validated.
    """
    text = load_charter(lab_cfg)
    obj, attempts = complete_json(
        lab_cfg["model"], _SYSTEM, _user(lab_cfg["charter_name"], text),
        validate=_validate, max_tokens=MAX_TOKENS, cache_system=False,
        effort=lab_cfg.get("effort"),   # the seat's own setting (none for the external rater)
        provider=lab_cfg.get("provider"),   # and its route (a stub model stays the stub)
        purpose="charter_rating", run_id=run_id)
    if obj is None:
        errors = [str(a.get("error")) for a in attempts if isinstance(a, dict) and a.get("error")]
        raise UnusableRating(f"{lab_cfg['key']}: no usable charter rating after {len(attempts)} attempt(s)"
                             + (f" (last error: {errors[-1]})" if errors else ""))
    values = {a: int(round(obj[a])) for a in AXES}
    return values, str(obj.get("rationale") or obj.get("notes") or "")


def write_values(key: str, values, model: str, notes: str, mode: str = "external",
                 on: Optional[str] = None) -> None:
    """Write a rating into config/labs/<key>.json: mode "external" (the canonical
    rating) sets charter_values / _source / _notes, mode "self" sets
    charter_values_self / _self_source / _self_notes and leaves charter_values alone."""
    values_f, source_f, notes_f = FIELDS[mode]
    path = CONFIG_DIR / "labs" / f"{key}.json"
    cfg = load_json(path)
    cfg[values_f] = {a: int(values[a]) for a in AXES}
    cfg[source_f] = f"rated by {model} on {on or date.today().isoformat()}"
    if notes:
        cfg[notes_f] = notes
    elif notes_f in cfg:
        del cfg[notes_f]           # a stale rationale must not outlive its values
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    logger.info(f"[write] {path.name}: {values_f} {cfg[values_f]}")


def ratings_file(model: str) -> Path:
    return RATINGS_DIR / f"charter_ratings_{model}.json"


def record_rating(model: str, key: str, charter_name: str, values, notes: str,
                  path: Optional[Path] = None) -> Path:
    """Add one external rating to data/charter_ratings_<model>.json (the archive)."""
    path = path or ratings_file(model)
    saved = json.loads(path.read_text()) if path.exists() else {}
    ratings = dict(saved.get("ratings") or {})
    ratings[key] = {"values": {a: int(values[a]) for a in AXES}, "rationale": notes,
                    "rated": datetime.now().strftime("%Y-%m-%d %H:%M"), "charter_name": charter_name}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"model": model,
                                "prompt": "rate_charters._SYSTEM and _user: charter name and text only",
                                "ratings": ratings}, indent=2))
    return path


def _stub_reply(system: str, user: str) -> str:
    # Mid-scale placeholder rating, enough to exercise parsing and rounding.
    return json.dumps({"time_horizon": 60, "transparency_threshold": 55,
                       "risk_tolerance": 40, "democratic_tendency": 45, "rationale": "stub rating"})


def guard_for(spent: float, budget: Optional[float], headroom: Optional[float]) -> float:
    """
    The ledger total at which to stop: --budget if given, else what is spent
    plus --headroom if given, else the pilot's budget_guard (one guard for the
    whole pilot, as pilot.py uses).
    """
    if budget is not None:
        return budget
    return spent + headroom if headroom is not None else pilot_budget_guard()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=f"Rate each charter on the four value axes. Default: the external rater "
                    f"{RATER_MODEL} (Kimi K3) rates every charter and its values become each seat's "
                    "starting charter_values (needs MOONSHOT_API_KEY).")
    who = p.add_mutually_exclusive_group()
    who.add_argument("--self", dest="self_rating", action="store_true",
                     help="rate each charter with that seat's own model into charter_values_self "
                          "(comparison only; charter_values unchanged; needs every seat's key)")
    who.add_argument("--rater", default=None, metavar="MODEL",
                     help=f"another outside rater, for comparison: writes only "
                          f"data/charter_ratings_<MODEL>.json, never the configs (default rater {RATER_MODEL})")
    p.add_argument("--dry-run", action="store_true",
                   help="use stub models; print ratings but write nothing (non-destructive)")
    p.add_argument("--lab", choices=list(LAB_ORDER), default=None, help="rate a single charter")
    p.add_argument("--budget", type=float, default=None,
                   help="ledger total (USD, absolute, shared ledger) at which rating halts; default: "
                        "config/pilot.json budget_guard")
    p.add_argument("--headroom", type=float, default=None,
                   help="instead of an absolute guard, allow this much spend on top of the ledger's current total")
    p.add_argument("--spend-file", default=None,
                   help="measured-spend ledger (default sim/data/spend.json, shared by all runs)")
    p.add_argument("--verbose", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging("rate-charters", verbose=args.verbose)

    keys = [args.lab] if args.lab else list(LAB_ORDER)
    seats = {key: load_json(CONFIG_DIR / "labs" / f"{key}.json") for key in keys}
    rater = None if args.self_rating else (args.rater or RATER_MODEL)
    to_configs = rater is None or rater == RATER_MODEL     # --rater OTHER: the archive only
    mode = "self" if rater is None else "external"
    # What each call gets: the seat itself (--self) or only its charter (external).
    cfgs = {k: (c if rater is None else rater_cfg(c, rater)) for k, c in seats.items()}
    run_id = "rate_charters" if rater is None else f"charter-rating-{rater}"
    if args.dry_run:
        register_stub("rate_charters", _stub_reply)
    else:
        tracker = configure(spend_file=Path(args.spend_file) if args.spend_file else None, budget=None)
        tracker.set_budget(guard_for(tracker.persisted_total(), args.budget, args.headroom))
        logger.info(f"[budget] guard ${tracker.budget:.2f} on the shared ledger "
                    f"(already spent ${tracker.persisted_total():.2f})")
        problems = preflight(list(dict.fromkeys(c["model"] for c in cfgs.values())),
                             {c["model"]: c["provider"] for c in cfgs.values() if c.get("provider")})
        if problems:
            say("Preflight failed; nothing was called:\n  " + "\n  ".join(problems))
            return EXIT_REFUSED
    say(f"rating {', '.join(keys)} with "
        + ("each seat's own model -> charter_values_self" if rater is None
           else f"{rater} -> " + ("charter_values" if to_configs else str(ratings_file(rater).name))))

    flag = " --self" if rater is None else (f" --rater {rater}" if args.rater else "")
    unusable = []
    for key, lab_cfg in cfgs.items():
        model = lab_cfg["model"]
        if args.dry_run:
            lab_cfg = dict(lab_cfg, model="stub:rate_charters")
        try:
            values, notes = rate_lab(lab_cfg, run_id=run_id)
        except BudgetExceeded as e:     # exit 1 on the budget guard, like pilot.py
            say(f"halted on the budget guard at {key}: {e}")
            return EXIT_BUDGET
        except NetworkUnavailable as e:  # a stop, not an unusable rating: nothing written for this charter
            left = list(cfgs)[list(cfgs).index(key):]
            say(f"stopped at {key}: {e}\n{NETWORK_STOP_HINT}"
                + (f" (still to rate: {', '.join(left)}; or one at a time: "
                   + "; ".join(f"python rate_charters.py{flag} --lab {k}" for k in left) + ")"
                   if len(cfgs) > 1 else ""))
            return EXIT_NETWORK
        except FatalAPIError as e:
            say(f"stopped at {key}: {e}")
            return EXIT_REFUSED
        except UnusableRating as e:
            # reported, the other charters still rated; the seat keeps its old values.
            say(f"UNUSABLE rating for {key} ({model}): {e}; its config is unchanged")
            unusable.append(key)
            continue
        if args.dry_run:
            say(f"{key:<10} ({model}) -> {values}  rationale: {notes}")
            continue
        if rater is not None:
            record_rating(rater, key, lab_cfg["charter_name"], values, notes)
        if to_configs:
            write_values(key, values, model, notes, mode=mode)
        say(f"{key:<10} ({model}) -> {values}")

    if args.dry_run:
        say("\n(dry-run: configs unchanged)")
    else:
        say(f"measured spend: ${get_tracker().persisted_total():.2f}")
    if unusable:
        say("re-rate: " + "; ".join(f"python rate_charters.py{flag} --lab {k}" for k in unusable))
        return EXIT_REFUSED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(run_main(main))
