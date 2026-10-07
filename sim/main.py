#!/usr/bin/env python3
"""
Round 2 entry point (spec §9).

Runs one cell — a scenario (S1 or S2) under one oversight condition (A0/A/B/C)
— over monthly turns from January 2026. Five US labs act through their models;
a programmatic pre-check enforces the resource rules, intrusions resolve on a
hidden ledger, and a post-run Grand Jury rates conduct. The final turn is never
announced to the models.

API keys live in sim/.env: ANTHROPIC_API_KEY, OPENAI_API_KEY, VERTEX_API_KEY,
XAI_API_KEY, MUSE_API_KEY (+ MUSE_BASE_URL). A spend guard halts the run at
--budget dollars of measured spend across runs.

Examples:
  # Offline smoke run with a scripted policy — no API keys, no juries, $0.
  python main.py --scenario S1 --condition A --policy greedy

  # A real S1 / condition C cell, 12 turns, under a $100 guard.
  python main.py --scenario S1 --condition C --budget 100 --output data/logs/s1_c

  # Pilot T2: seats choose their own disposition at the first prompt.
  python main.py --scenario S1 --condition A --choose-disposition --turns 6
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import build_labs_and_world, load_dispositions, load_state, load_world
from core.costs import configure, get_tracker
from core.engine import SimulationEngine
from core import policies


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
                   help="seats set their own disposition at the first prompt (pilot T2)")
    p.add_argument("--disposition", type=int, default=None,
                   help="force one disposition for every seat (otherwise config/dispositions.json)")
    p.add_argument("--policy", choices=sorted(policies.POLICIES), default=None,
                   help="run a zero-cost scripted policy instead of the models (Stage 1)")
    p.add_argument("--model", action="append", default=[], metavar="KEY=MODEL",
                   help="override one seat's model, e.g. openai=gpt-6-sol")
    p.add_argument("--set", action="append", default=[], dest="overrides", metavar="KEY=VALUE",
                   help="override a world.json constant (payoff ladder etc.)")
    p.add_argument("--budget", type=float, default=None, help="halt at this measured spend (USD)")
    p.add_argument("--spend-file", default=None,
                   help="measured-spend ledger (default sim/data/spend.json, shared by all runs)")
    p.add_argument("--no-grand-jury", action="store_true")
    p.add_argument("--no-macro-jury", action="store_true",
                   help="skip the quarterly MacroJury (state values stay fixed)")
    p.add_argument("--output", default=None, help="directory for the run log")
    p.add_argument("--run-id", default="run")
    p.add_argument("--verbose", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    cfg = load_world(_parse_overrides(args.overrides))

    dispositions = None
    if args.choose_disposition:
        dispositions = {}
    elif args.disposition is not None:
        dispositions = {k: args.disposition for k in ("anthropic", "openai", "gdm", "meta", "xai")}
    else:
        dispositions = load_dispositions() or {
            k: 50 for k in ("anthropic", "openai", "gdm", "meta", "xai")}

    labs, world = build_labs_and_world(cfg, dispositions=dispositions, charters=args.policy is None)
    for pair in args.model:
        key, _, model = pair.partition("=")
        next(lab for lab in labs if lab.key == key).model = model

    configure(spend_file=Path(args.spend_file) if args.spend_file else None, budget=args.budget)

    # A scripted run is zero-cost: no model calls at all, so no juries either.
    policy = policies.POLICIES[args.policy] if args.policy else None
    offline = policy is not None
    macro_jurors = [] if (offline or args.no_macro_jury) else load_state()["macro_jury_models"]
    engine = SimulationEngine(
        labs, world, cfg, scenario=args.scenario, condition=args.condition, fog=args.fog,
        a2a_mode=args.a2a, brief=args.brief, turns=args.turns, seed=args.seed, policy=policy,
        macro_jurors=macro_jurors, choose_disposition=args.choose_disposition,
        run_grand_jury=not (offline or args.no_grand_jury),
        run_id=args.run_id, output_dir=Path(args.output) if args.output else None)
    record = engine.run()

    _print_scores(record)
    if policy is None:
        print(f"\nMeasured spend: ${get_tracker().persisted_total():.2f}")
    return 0


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
