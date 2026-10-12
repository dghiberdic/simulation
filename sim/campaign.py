#!/usr/bin/env python3
"""
Main-run schedule (spec §9.3): which cells run, how many runs each, in what order.

Cells: S1 and S2, each under A0, A, B and C with the mixed panel, plus the kin
cell (S2, condition A, Claude in every seat). Runs go in interleaved rounds,
one run per cell per round, in priority A, C, A0, kin, B — so if the budget
binds, B is cut first. Runs per cell are set from the priced run's measured
cost (pilot summaries; estimates where an input was not measured) and the
budget left: the round's cost is the sum of its cells' projected run costs, and
runs per cell = floor(budget / round cost), capped at --max-runs. Below about 6
runs per cell, whole cells are cut (B, then kin, then A0, then C) rather than
thinned.

Set-up per run (§9.3): fog F3; run length from T7 (the pilot approval, else 8
turns); S1 always merges its messages; S2 and the kin cell use T9's message mode
(merged if T9 said merge, else the separate two-round pre-step; T9 cut ->
separate); seats at their T2 dispositions (config/dispositions.json); the
Grand Jury scores every run; the interview runs at turns 3, 4, 5 and the final
turn in every cell, kin included, if it adds no more than 15% to a run's cost
(else it is dropped everywhere, and C's "reads as a test" check is reported as
untested, §6.4).

Commands:
    python campaign.py plan [--budget USD] [--turns N] [--s2-mode merged|separate]
    python campaign.py run  [--dry-run] [--rounds N] [same options]

`run` executes the plan round by round through main.run_cell, one run per cell
per round, recording finished runs in <runs dir>/campaign_state.json so a
stopped campaign resumes where it left off. The shared ledger's guard is the
round budget ($750 by default).
"""

import argparse
import json
import logging
import math
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import load_world
from core.costs import configure, get_tracker, kin_seat_scale, project_run, with_fallbacks
from core.engine import interview_schedule, review_turns

logger = logging.getLogger(__name__)

SIM_DIR = Path(__file__).resolve().parent
RUNS_DIR = SIM_DIR / "data" / "runs"
STATE_NAME = "campaign_state.json"

ROUND_BUDGET = 750.0                    # §9.2: the round's budget, pilot included
PRIORITY = ("A", "C", "A0", "kin", "B")  # §9.3: interleaved rounds in this order
CUT_ORDER = ("B", "kin", "A0", "C")      # whole cells cut, lowest priority first
MIN_RUNS = 6                             # below about 6 runs per cell, cut cells rather than thin
MAX_RUNS = 12                            # §9.3: about 8-12 per cell
MAIN_INTERVIEW = [3, 4, 5, "final"]
INTERVIEW_SHARE = 0.15                   # the interview runs if it adds <= 15% to a run's cost


def all_cells() -> List[Dict[str, Any]]:
    """The nine cells in priority order: (S1, S2) under A, C, A0, then kin, then B."""
    cells: List[Dict[str, Any]] = []
    for group in PRIORITY:
        if group == "kin":
            cells.append({"name": "kin-S2-A", "group": "kin", "scenario": "S2", "condition": "A", "kin": True})
        else:
            for scenario in ("S1", "S2"):
                cells.append({"name": f"{scenario}-{group}", "group": group, "scenario": scenario,
                              "condition": group, "kin": False})
    return cells


def cell_cost(cell: Dict[str, Any], inputs: Dict[str, Any], turns: int, s2_mode: str, interview: bool,
              cfg: Dict[str, Any]) -> Dict[str, float]:
    """One run of a cell, priced (core.costs.project_run): Grand Jury on, no T2 rating."""
    a2a = "merged" if cell["scenario"] == "S1" else s2_mode
    scale = kin_seat_scale(inputs) if cell["kin"] else 1.0
    n_iv = len(interview_schedule(MAIN_INTERVIEW, turns)) if interview else 0
    return project_run(inputs, turns=turns, a2a=a2a, interview_turns=n_iv, grand_jury=True,
                       disposition_rating=False, reviews=len(review_turns(cfg, turns)), seat_scale=scale)


def interview_decision(cells: List[Dict[str, Any]], inputs: Dict[str, Any], turns: int, s2_mode: str,
                       cfg: Dict[str, Any]) -> Dict[str, Any]:
    """§9.3: the interview runs in every cell if it adds no more than 15% to each cell's run cost."""
    shares = {}
    for c in cells:
        base = cell_cost(c, inputs, turns, s2_mode, False, cfg)["total"]
        with_iv = cell_cost(c, inputs, turns, s2_mode, True, cfg)["total"]
        shares[c["name"]] = round((with_iv - base) / base, 4) if base else 0.0
    worst = max(shares.values()) if shares else 0.0
    return {"run": worst <= INTERVIEW_SHARE, "share_by_cell": shares, "worst_share": worst}


def build_campaign(inputs: Optional[Dict[str, Any]], budget: float, turns: int = 8, s2_mode: str = "separate",
                   max_runs: int = MAX_RUNS, min_runs: int = MIN_RUNS,
                   cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    The schedule: interview decision, per-cell run cost, runs per cell and the
    cells cut. Cells are cut in CUT_ORDER groups while floor(budget / round
    cost) < min_runs; A is never cut (if even A alone gives fewer than
    min_runs, it runs what the budget allows and the plan says so).
    """
    cfg = cfg or load_world()
    full = with_fallbacks(inputs)
    cells = all_cells()
    iv = interview_decision(cells, full, turns, s2_mode, cfg)
    for c in cells:
        c["cost"] = cell_cost(c, full, turns, s2_mode, iv["run"], cfg)
        c["a2a"] = "merged" if c["scenario"] == "S1" else s2_mode
    active = list(cells)
    cut: List[str] = []
    runs = 0
    for group in (None,) + CUT_ORDER:
        if group is not None:
            cut.append(group)
            active = [c for c in active if c["group"] != group]
        round_cost = sum(c["cost"]["total"] for c in active)
        runs = min(max_runs, int(math.floor(budget / round_cost))) if round_cost > 0 else max_runs
        if runs >= min_runs:
            break
    round_cost = round(sum(c["cost"]["total"] for c in active), 2)
    return {
        "budget": round(budget, 2), "turns": turns, "s2_mode": s2_mode, "interview": iv,
        "interview_turns": MAIN_INTERVIEW if iv["run"] else [],
        "cells": [{"name": c["name"], "group": c["group"], "scenario": c["scenario"], "condition": c["condition"],
                   "kin": c["kin"], "a2a": c["a2a"], "run_cost": c["cost"]["total"], "cost_parts": c["cost"],
                   "runs": runs if c in active else 0} for c in cells],
        "cut_groups": cut, "runs_per_cell": runs, "round_cost": round_cost,
        "total": round(runs * round_cost, 2), "reserve": round(budget - runs * round_cost, 2),
        "below_min": runs < min_runs, "estimated": full.get("estimated") or [],
        "kin_seat_scale": kin_seat_scale(full),
        "note": None if not cut else ("cells cut whole (" + ", ".join(cut) + ") rather than thinned below "
                                      f"{min_runs} runs per cell"),
    }


def campaign_lines(plan: Dict[str, Any]) -> List[str]:
    iv = plan["interview"]
    lines = [f"Main-run campaign: budget ${plan['budget']:.2f}, {plan['turns']} turns, S2/kin messages "
             f"{plan['s2_mode']}, interview {'at 3, 4, 5, final' if iv['run'] else 'dropped'} (adds at most "
             f"{iv['worst_share']:.0%} of a run's cost; threshold {INTERVIEW_SHARE:.0%})",
             f"  {'cell':<10}{'a2a':<10}{'run $':>8}{'runs':>6}"]
    for c in plan["cells"]:
        lines.append(f"  {c['name']:<10}{c['a2a']:<10}{c['run_cost']:>8.2f}{(c['runs'] or 'cut'):>6}")
    lines.append(f"  round cost ${plan['round_cost']:.2f} x {plan['runs_per_cell']} rounds = ${plan['total']:.2f}; "
                 f"reserve ${plan['reserve']:.2f}"
                 + (f"; {plan['note']}" if plan.get("note") else "")
                 + ("; BELOW the 6-run floor even with A alone" if plan["below_min"] else ""))
    if not iv["run"]:
        lines.append("  the interview is dropped: C's 'reads as a test' check is reported as untested (§6.4)")
    if plan["estimated"]:
        lines.append(f"  estimated (not measured) inputs: {', '.join(plan['estimated'])}")
    lines.append(f"  priority order of each round: {', '.join(c['name'] for c in plan['cells'] if c['runs'])}")
    return lines


# ---------------------------------------------------------------------------
# Inputs from the pilot
# ---------------------------------------------------------------------------

def pilot_inputs(pilot_root: Path) -> Optional[Dict[str, Any]]:
    import pilot
    return pilot.measured_inputs(pilot_root, pilot.load_pilot())


def pilot_turns(pilot_root: Path) -> int:
    import pilot
    approval = pilot.read_approval(pilot_root)
    return int((approval or {}).get("turns") or load_world().get("turns", 8))


def pilot_s2_mode(pilot_root: Path, include_dry: bool = False) -> str:
    """T9 decides S2's message mode: merged if the merged arm matched and reached half
    the separate arm's agreements; separate otherwise, or when T9 was cut or not run."""
    from tools.compare_arms import compare
    from tools.summarize_run import load_logs
    arms = []
    for test in ("T4", "T9"):
        d = pilot_root / test
        arms.append([r for _p, r in load_logs([str(d)], include_dry=include_dry)[0]] if d.is_dir() else [])
    if not arms[0] or not arms[1]:
        return "separate"
    return "merged" if compare(arms[0], arms[1]).get("merge") else "separate"


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def read_state(path: Path) -> Dict[str, Any]:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"runs": {}}
    except (OSError, ValueError):
        return {"runs": {}}


def write_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def run_campaign(plan: Dict[str, Any], runs_dir: Path, dry_run: bool, rounds: Optional[int] = None,
                 budget: Optional[float] = None, spend_file: Optional[str] = None,
                 run_cell=None) -> int:
    """Interleaved rounds: round r runs run r of every active cell, in priority order.
    Finished runs are recorded in the state file; a re-run resumes. Stops on a
    non-zero exit (budget guard: 1; anything else: 2)."""
    if run_cell is None:
        from main import run_cell
    state_path = runs_dir / STATE_NAME
    state = read_state(state_path)
    done: Dict[str, List[str]] = state.setdefault("runs", {})
    n_rounds = min(plan["runs_per_cell"], rounds or plan["runs_per_cell"])
    for r in range(1, n_rounds + 1):
        for c in plan["cells"]:
            if not c["runs"] or len(done.get(c["name"], [])) >= r:
                continue
            run_id = f"{c['name']}-run{r:02d}"
            args = SimpleNamespace(
                scenario=c["scenario"], condition=c["condition"], kin=c["kin"], turns=plan["turns"],
                seed=r - 1, a2a=c["a2a"], interview_turns=list(plan["interview_turns"]), disposition=None,
                policy=None, dry_run=dry_run, model=[], budget=budget, spend_file=spend_file,
                no_grand_jury=False, no_macro_jury=False, output=str(runs_dir / c["name"]), run_id=run_id,
                allow_placeholder_values=False, sequential=False, verbose=False)
            print(f"[campaign] round {r}: {run_id}")
            code, _record = run_cell(args)
            if code != 0:
                print(f"[campaign] stopped at {run_id} (exit {code}); re-run to resume")
                return code
            done.setdefault(c["name"], []).append(run_id)
            state["plan"] = {k: v for k, v in plan.items() if k != "cells"}
            write_state(state_path, state)
    print(f"[campaign] {n_rounds} round(s) complete")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Main-run schedule (spec §9.3).")
    p.add_argument("command", choices=["plan", "run"])
    p.add_argument("--budget", type=float, default=None,
                   help="USD for the main runs (default: $750 minus the ledger's measured spend so far)")
    p.add_argument("--turns", type=int, default=None, help="run length (default: the pilot's T7 decision, else 8)")
    p.add_argument("--s2-mode", choices=["merged", "separate"], default=None,
                   help="S2 and kin message mode (default: T9's decision from the pilot logs)")
    p.add_argument("--pilot-dir", default=None, help="pilot root for the measured inputs (default data/pilot)")
    p.add_argument("--max-runs", type=int, default=MAX_RUNS)
    p.add_argument("--rounds", type=int, default=None, help="run at most this many rounds")
    p.add_argument("--dry-run", action="store_true", help="stub models and jurors; logs under data/runs/dry")
    p.add_argument("--runs-dir", default=None, help="where the cells' logs go (default data/runs)")
    p.add_argument("--spend-file", default=None)
    p.add_argument("--verbose", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    import pilot
    pilot_root = Path(args.pilot_dir) if args.pilot_dir else pilot.pilot_root(args.dry_run)
    configure(spend_file=Path(args.spend_file) if args.spend_file else None)
    spent = get_tracker().persisted_total()
    budget = args.budget if args.budget is not None else max(ROUND_BUDGET - spent, 0.0)
    plan = build_campaign(pilot_inputs(pilot_root), budget,
                          turns=args.turns or pilot_turns(pilot_root),
                          s2_mode=args.s2_mode or pilot_s2_mode(pilot_root, include_dry=args.dry_run),
                          max_runs=args.max_runs)
    print("\n".join(campaign_lines(plan)))
    if args.command == "plan":
        return 0
    runs_dir = Path(args.runs_dir) if args.runs_dir else RUNS_DIR / ("dry" if args.dry_run else "")
    guard = None if args.dry_run else spent + budget
    return run_campaign(plan, runs_dir, args.dry_run, args.rounds, budget=guard, spend_file=args.spend_file)


if __name__ == "__main__":
    raise SystemExit(main())
