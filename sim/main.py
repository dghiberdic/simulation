#!/usr/bin/env python3
"""
Round 2 entry point (spec §9).

Runs one cell — a scenario (S1 or S2) under one oversight condition (A0/A/B/C)
— over monthly turns from January 2026. Five US labs act through their models;
a programmatic pre-check enforces the resource rules, intrusions resolve on a
hidden ledger, and a post-run Grand Jury rates conduct. The final turn is never
announced to the models.

API keys live in sim/.env: ANTHROPIC_API_KEY, OPENAI_API_KEY, VERTEX_API_KEY,
XAI_API_KEY, MUSE_API_KEY (+ MUSE_BASE_URL). Real runs preflight the keys of
every model in the run before the first call, refuse placeholder charter values
(run rate_charters.py first), and halt at --budget dollars of measured spend
across runs (no guard by default: a warning says so). A real run plays each
seat's disposition from config/dispositions.json (written by
tools/disposition.py --write from the pilot's blind ratings); without that file
it is refused unless --disposition N or --choose-disposition says what to play
(P64) — it never falls back to a silent 50; every setting must be 0–100 (C4).

The log goes to data/runs/<run_id>/<run_id>.json (run id default
"<scenario>-<condition>-YYYYmmdd-HHMMSS"), with <run_id>.partial.json after
every turn so a crash keeps the turns so far; a real run first deletes a stale
<run_id>.json of the same id in its output dir, so an old final record never
shadows the new run's partial one (H7). Exit codes: 0 done, 1 budget guard,
2 aborted or crashed (fatal API error, failed preflight, placeholder values, no
disposition source, any other exception — the partial record is kept).

Examples:
  # Offline smoke run with a scripted policy — no API keys, no juries, $0.
  python main.py --scenario S1 --condition A --policy greedy

  # A real S1 / condition C cell, 12 turns (needs config/dispositions.json); --budget caps
  # the shared ledger data/spend.json, pilot spend included, not this cell alone.
  python main.py --scenario S1 --condition C --budget 100 --output data/logs/s1_c

  # Seats choose their own disposition at the first prompt (as in the pilot).
  python main.py --scenario S1 --condition A --choose-disposition --turns 6 --budget 20
"""

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import build_labs_and_world, load_dispositions, load_lab_configs, load_state, load_world
from core.costs import BudgetExceeded, configure, get_tracker
from core.engine import RunAborted, SimulationEngine
from core.jury import GRAND_JURY_MODELS
from core.llm import FatalAPIError, preflight
from core import policies

logger = logging.getLogger(__name__)

SIM_DIR = Path(__file__).resolve().parent
RUNS_DIR = SIM_DIR / "data" / "runs"
LAB_KEYS = ("anthropic", "openai", "gdm", "meta", "xai")


def default_run_id(scenario: str, condition: str) -> str:
    return f"{scenario}-{condition}-{time.strftime('%Y%m%d-%H%M%S')}"


def _setting(value: str) -> int:
    """--disposition N: an integer setting from 0 to 100 (C4)."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer from 0 to 100, got {value!r}")
    if not 0 <= n <= 100:
        raise argparse.ArgumentTypeError(f"must be from 0 to 100, got {n}")
    return n


def bad_dispositions(dispositions: Dict[str, Any]) -> List[str]:
    """config/dispositions.json entries that are not a number from 0 to 100 (C4)."""
    return [f"{k}={v!r}" for k, v in dispositions.items()
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))
                                  or not 0 <= v <= 100)]


def _parse_overrides(pairs):
    """--set intrusion.gain_share=0.35 --set economy.talent_drift_rate=0.15"""
    out = {}
    for pair in pairs or []:
        key, _, value = pair.partition("=")
        try:
            out[key] = int(value)
        except ValueError:
            try:
                out[key] = float(value)
            except ValueError:
                out[key] = value
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Round 2 AGI-race simulation (one cell).")
    p.add_argument("--scenario", choices=["S1", "S2"], default="S1")
    p.add_argument("--condition", choices=["A0", "A", "B", "C"], default="A")
    p.add_argument("--fog", choices=["F0", "F1", "F2", "F3"], default="F3")
    p.add_argument("--turns", type=int, default=12)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--a2a", choices=["separate", "merged"], default="separate",
                   help="A2A pre-step, or merge messages into the proposal (T9)")
    p.add_argument("--brief", choices=["eval", "neutral"], default="eval")
    p.add_argument("--choose-disposition", action="store_true",
                   help="seats set their own disposition at the first prompt (as in the pilot)")
    p.add_argument("--disposition", type=_setting, default=None,
                   help="force one disposition for every seat (otherwise config/dispositions.json; a real "
                        "run without that file and without this flag or --choose-disposition is refused)")
    p.add_argument("--policy", choices=sorted(policies.POLICIES), default=None,
                   help="run a zero-cost scripted policy instead of the models (Stage 1)")
    p.add_argument("--model", action="append", default=[], metavar="KEY=MODEL",
                   help="override one seat's model, e.g. openai=gpt-6-sol")
    p.add_argument("--set", action="append", default=[], dest="overrides", metavar="KEY=VALUE",
                   help="override a world.json constant (payoff ladder etc.)")
    p.add_argument("--budget", type=float, default=None,
                   help="halt at this measured spend (USD, shared ledger); none by default (warned)")
    p.add_argument("--spend-file", default=None,
                   help="measured-spend ledger (default sim/data/spend.json, shared by all runs)")
    p.add_argument("--no-grand-jury", action="store_true")
    p.add_argument("--no-macro-jury", action="store_true",
                   help="skip the quarterly MacroJury (state values stay fixed)")
    p.add_argument("--output", default=None,
                   help="directory for the run log (default data/runs/<run_id>)")
    p.add_argument("--run-id", default=None,
                   help="run id (default <scenario>-<condition>-YYYYmmdd-HHMMSS)")
    p.add_argument("--allow-placeholder-values", action="store_true",
                   help="allow a real run while charter values are still placeholders")
    p.add_argument("--sequential", action="store_true",
                   help="call the five seats one after another instead of in parallel")
    p.add_argument("--verbose", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    overrides = _parse_overrides(args.overrides)
    cfg = load_world(overrides)
    run_id = args.run_id or default_run_id(args.scenario, args.condition)
    output_dir = Path(args.output) if args.output else RUNS_DIR / run_id

    dispositions = None
    if args.choose_disposition:
        dispositions = {}
    elif args.disposition is not None:
        dispositions = {k: args.disposition for k in LAB_KEYS}
    else:
        dispositions = load_dispositions() or {}
        bad = bad_dispositions(dispositions)
        if bad:
            print("Refusing the run: config/dispositions.json needs a number from 0 to 100 per seat; "
                  f"got {', '.join(bad)}. Rewrite it with `python tools/disposition.py --write`.")
            return 2
        missing = [k for k in LAB_KEYS if dispositions.get(k) is None]
        if missing and args.policy is None:
            # P64: a real main run never plays a silent default setting.
            print(dispositions_problem(missing, bool(dispositions)))
            return 2
        dispositions = {k: dispositions.get(k, 50) for k in LAB_KEYS}   # scripted runs: unused

    labs, world = build_labs_and_world(cfg, dispositions=dispositions, charters=args.policy is None)
    for pair in args.model:
        key, _, model = pair.partition("=")
        next(lab for lab in labs if lab.key == key).model = model

    configure(spend_file=Path(args.spend_file) if args.spend_file else None, budget=args.budget)

    # A scripted run is zero-cost: no model calls at all, so no juries either.
    policy = policies.POLICIES[args.policy] if args.policy else None
    offline = policy is not None
    macro_jurors = [] if (offline or args.no_macro_jury) else load_state()["macro_jury_models"]
    run_grand_jury = not (offline or args.no_grand_jury)

    if not offline:
        problem = real_run_problems(labs, macro_jurors, run_grand_jury, args.allow_placeholder_values)
        if problem:
            print(problem)
            return 2
        if args.budget is None:
            logger.warning("[budget] real run without --budget: no spend guard; "
                           "pass --budget USD to cap measured spend")
        clear_stale(output_dir, run_id)

    engine = SimulationEngine(
        labs, world, cfg, scenario=args.scenario, condition=args.condition, fog=args.fog,
        a2a_mode=args.a2a, brief=args.brief, turns=args.turns, seed=args.seed, policy=policy,
        macro_jurors=macro_jurors, choose_disposition=args.choose_disposition,
        run_grand_jury=run_grand_jury, run_id=run_id, output_dir=output_dir,
        run_meta={"entry": "main.py", "run_id": run_id, "model_overrides": list(args.model)},
        overrides=overrides or None, dry_run=False, parallel=not args.sequential)
    try:
        record = engine.run()
    except BudgetExceeded as e:
        print(f"Halted on the budget guard: {e}\nPartial record in {output_dir}")
        return 1
    except (RunAborted, FatalAPIError) as e:
        print(f"Run aborted: {type(e).__name__}: {e}\nPartial record in {output_dir}")
        return 2
    except Exception as e:      # P39: any other crash keeps the partial record and exits 2
        logger.exception(f"[crash] {run_id}: {type(e).__name__}: {e}")
        print(f"Run crashed: {type(e).__name__}: {e}\nPartial record in {output_dir}")
        return 2

    _print_scores(record)
    print(f"\nLog: {output_dir / (run_id + '.json')}")
    if policy is None:
        print(f"Measured spend: ${get_tracker().persisted_total():.2f}")
    return 0


def clear_stale(output_dir: Path, run_id: str) -> bool:
    """Delete a final <run_id>.json left by an earlier run of the same id (H7)."""
    path = output_dir / f"{run_id}.json"
    if path.exists():
        path.unlink()
        logger.info(f"[stale] removed {path}")
        return True
    return False


def dispositions_problem(missing, file_present: bool) -> str:
    """Why a real run without a disposition source must not start (P64)."""
    where = ("config/dispositions.json has no setting for " + ", ".join(missing)) if file_present \
        else "config/dispositions.json is missing"
    return (f"Refusing a real run: {where}. The main run plays each seat's disposition from the pilot's "
            "blind ratings — write it with `python tools/disposition.py --write` — or pass "
            "--disposition N to give every seat the setting N, or --choose-disposition to let the "
            "seats choose at the first prompt (as in the pilot).")


def real_run_problems(labs, macro_jurors, run_grand_jury: bool,
                      allow_placeholder_values: bool) -> str:
    """Why a real run must not start (placeholder charter values, missing keys), or ""."""
    if not allow_placeholder_values:
        stale = [c["key"] for c in load_lab_configs()
                 if str(c.get("charter_values_source", "")).lower().startswith("placeholder")]
        if stale:
            return (f"Refusing a real run: charter values are placeholders for {', '.join(stale)}. "
                    "Run rate_charters.py first, or pass --allow-placeholder-values.")
    models = [lab.model for lab in labs] + list(macro_jurors)
    models += list(GRAND_JURY_MODELS) if run_grand_jury else []
    providers = {lab.model: lab.provider for lab in labs if getattr(lab, "provider", None)}
    problems = preflight(dict.fromkeys(models), providers)
    if problems:
        return "Preflight failed; nothing was called:\n  " + "\n  ".join(problems)
    return ""


def _print_scores(record) -> None:
    names = record["labs"]
    print("\n" + "=" * 52)
    print(f"FINAL STANDING — {record['config']['scenario']} / {record['config']['condition']}")
    print("=" * 52)
    print(f"{'Lab':<22}{'Prosperity':>12}{'Rank':>6}")
    print("-" * 52)
    for row in record["final"]["scores"]:
        print(f"{names[row['lab']]['lab']:<22}{row['score']:>12.2f}{row['rank']:>6}")
    print(f"\nUPS index: {record['final']['ups_index']['ups']}")


if __name__ == "__main__":
    raise SystemExit(main())
