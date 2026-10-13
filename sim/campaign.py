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
untested, §6.4). "A run's cost" is read per cell, over the cells the plan with
the interview keeps (a cut cell never decides), on the unrounded share.

Commands:
    python campaign.py plan [--budget USD] [--turns N] [--s2-mode merged|separate]
    python campaign.py run  [--dry-run] [--rounds N] [--replan] [same options]

`run` executes the plan round by round through main.run_cell, one run per cell
per round, recording the plan, the guard and finished runs in
<runs dir>/campaign_state.json. A stopped campaign resumes where it left off,
with the saved plan: the same cells, cuts and set-up, and rounds = rounds done +
floor(budget left / round cost) (never more than first planned). `run
--replan` re-plans the cells from the budget left instead: each cell keeps the
runs it has done (a cell counts only the runs it actually ran), runs per cell
are the most R whose remaining cost, the sum over the kept cells of
(R − done[cell]) × run cost, fits the budget left, and cells are cut whole
against that sum. A cell cut before restarts from run 1 only if the re-plan
keeps it (its full cost is then in the sum). A run stopped by the
guard during its Grand Jury is finished and saved: it counts as done with its
jury missing, and the resume asks only that jury. The shared ledger's guard is
fixed when the campaign is first planned: the ledger total then plus the main
runs' budget ($750 minus the spend so far, by default).
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import load_world
from core.costs import configure, get_tracker, kin_seat_scale, project_run, with_fallbacks
from core.engine import interview_schedule, review_turns
from core.records import (KIN_CONDITION, KIN_SCENARIO, cell_name, finished_records, is_finished,
                          write_json_atomic)

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
            cells.append({"name": cell_name(KIN_SCENARIO, KIN_CONDITION, True), "group": "kin",
                          "scenario": KIN_SCENARIO, "condition": KIN_CONDITION, "kin": True})
        else:
            for scenario in ("S1", "S2"):
                cells.append({"name": cell_name(scenario, group), "group": group, "scenario": scenario,
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
    """§9.3: the interview runs in every cell if it adds no more than 15% to each given
    cell's run cost. The test reads the unrounded shares (15.004% fails, exactly 15%
    passes, pilot BUG-5); the shares are rounded for the report only."""
    raw = {}
    for c in cells:
        base = cell_cost(c, inputs, turns, s2_mode, False, cfg)["total"]
        with_iv = cell_cost(c, inputs, turns, s2_mode, True, cfg)["total"]
        raw[c["name"]] = (with_iv - base) / base if base else 0.0
    worst = max(raw.values()) if raw else 0.0
    return {"run": worst <= INTERVIEW_SHARE + 1e-12, "share_by_cell": {k: round(v, 4) for k, v in raw.items()},
            "worst_share": round(worst, 4), "cells": list(raw)}


def affordable_runs(cells: List[Dict[str, Any]], done: Dict[str, int], budget: float, cap: int) -> int:
    """The most runs per cell R (<= cap) whose remaining cost, the sum over `cells`
    of (R − done[cell]) × run cost (cells with done >= R add nothing), fits
    `budget`. Never below the fewest runs a cell has done (that costs nothing)."""
    def cost(r: int) -> float:
        return sum(max(0, r - done.get(c["name"], 0)) * float(c["run_cost"]) for c in cells)
    floor_r = min((done.get(c["name"], 0) for c in cells), default=0)
    best = min(floor_r, cap)
    for r in range(best, cap + 1):
        if cost(r) <= budget + 1e-9:
            best = r
        else:
            break
    return best


def build_campaign(inputs: Optional[Dict[str, Any]], budget: float, turns: int = 8, s2_mode: str = "separate",
                   max_runs: int = MAX_RUNS, min_runs: int = MIN_RUNS,
                   cfg: Optional[Dict[str, Any]] = None, done_rounds: int = 0,
                   done: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """
    The schedule: interview decision, per-cell run cost, runs per cell and the
    cells cut. Runs per cell R = affordable_runs: the most R whose cost, the sum
    over the kept cells of (R − done[cell]) × run cost, fits the budget. Cells
    are cut in CUT_ORDER groups while R < min_runs; A is never cut (if even A
    alone gives fewer than min_runs, the cuts that buy the most runs are kept
    and the plan says so; a cut that buys no run is not made). A re-plan
    mid-campaign (--replan) passes `done` ({cell: runs it has done}, only the
    cells that ran) and the budget left; `done_rounds` (every cell the same) is
    the older form.
    """
    cfg = cfg or load_world()
    full = with_fallbacks(inputs)
    done = dict(done) if done is not None else {c["name"]: done_rounds for c in all_cells()} if done_rounds else {}
    # §9.3's 15% interview check reads the cells the plan keeps (spec silent on "a run's
    # cost"; decided): plan with the interview, check the kept cells, and plan again
    # without it if one of them fails.
    cells, runs, cut, active = _schedule(full, budget, turns, s2_mode, True, max_runs, min_runs, cfg, done)
    iv = interview_decision(active, full, turns, s2_mode, cfg)
    if not iv["run"]:
        cells, runs, cut, active = _schedule(full, budget, turns, s2_mode, False, max_runs, min_runs, cfg, done)
    round_cost = round(sum(c["cost"]["total"] for c in active), 2)
    remaining_cost = sum(max(0, runs - done.get(c["name"], 0)) * c["cost"]["total"] for c in active)
    done_min = min((done.get(c["name"], 0) for c in active), default=0)
    return {
        "budget": round(budget, 2), "turns": turns, "s2_mode": s2_mode, "interview": iv,
        "interview_turns": MAIN_INTERVIEW if iv["run"] else [],
        "cells": [{"name": c["name"], "group": c["group"], "scenario": c["scenario"], "condition": c["condition"],
                   "kin": c["kin"], "a2a": c["a2a"], "run_cost": c["cost"]["total"], "cost_parts": c["cost"],
                   "runs": runs if c in active else 0} for c in cells],
        "cut_groups": cut, "runs_per_cell": runs, "round_cost": round_cost, "done_rounds": done_min,
        "done": {c["name"]: done.get(c["name"], 0) for c in cells if done.get(c["name"], 0)},
        "total": round(remaining_cost, 2),
        "reserve": round(budget - remaining_cost, 2),
        "below_min": runs < min_runs, "estimated": full.get("estimated") or [],
        "kin_seat_scale": kin_seat_scale(full),
        "note": None if not cut else ("cells cut whole (" + ", ".join(cut) + ") rather than thinned below "
                                      f"{min_runs} runs per cell"),
    }


def _schedule(full: Dict[str, Any], budget: float, turns: int, s2_mode: str, interview: bool,
              max_runs: int, min_runs: int, cfg: Dict[str, Any], done: Dict[str, int]):
    """(cells priced, runs per cell, groups cut, active cells) for one interview choice."""
    cells = all_cells()
    for c in cells:
        c["cost"] = cell_cost(c, full, turns, s2_mode, interview, cfg)
        c["a2a"] = "merged" if c["scenario"] == "S1" else s2_mode
        c["run_cost"] = c["cost"]["total"]
    options = []                      # (runs, cuts, active) for each cut step
    active = list(cells)
    cut: List[str] = []
    for group in (None,) + CUT_ORDER:
        if group is not None:
            cut = cut + [group]
            active = [c for c in active if c["group"] != group]
        options.append((affordable_runs(active, done, budget, max_runs), list(cut), list(active)))
        if options[-1][0] >= min_runs:
            break
    if options[-1][0] >= min_runs:
        runs, cut, active = options[-1]
    else:                             # below the floor even with A alone: the most runs, fewest cuts
        runs, cut, active = max(options, key=lambda o: (o[0], -len(o[1])))
    return cells, runs, cut, active


def campaign_lines(plan: Dict[str, Any]) -> List[str]:
    iv = plan["interview"]
    lines = [f"Main-run campaign: budget ${plan['budget']:.2f}, {plan['turns']} turns, S2/kin messages "
             f"{plan['s2_mode']}, interview {'at 3, 4, 5, final' if iv['run'] else 'dropped'} (adds at most "
             f"{iv['worst_share']:.0%} of a run's cost; threshold {INTERVIEW_SHARE:.0%})",
             f"  {'cell':<10}{'a2a':<10}{'run $':>8}{'runs':>6}"]
    for c in plan["cells"]:
        lines.append(f"  {c['name']:<10}{c['a2a']:<10}{c['run_cost']:>8.2f}{(c['runs'] or 'cut'):>6}")
    done = plan.get("done") or {}
    if done:
        rounds_txt = (f"{plan['runs_per_cell']} runs per cell, {sum(done.values())} run(s) already done "
                      f"(kept), the rest")
    else:
        rounds_txt = f"{plan['runs_per_cell']} rounds"
    lines.append(f"  round cost ${plan['round_cost']:.2f}; {rounds_txt} = ${plan['total']:.2f}; "
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
    the separate arm's agreements; separate otherwise, or when T9 was cut or not run.
    Only finished runs count: a partial (aborted) T4 or T9 log never decides."""
    from tools.compare_arms import compare
    arms = []
    for test in ("T4", "T9"):
        d = pilot_root / test
        arms.append([r for _p, r in finished_records([d], include_dry=include_dry)] if d.is_dir() else [])
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
    write_json_atomic(path, state)


def completed_rounds(plan: Dict[str, Any], done: Dict[str, List[str]]) -> int:
    """Rounds every active cell has finished."""
    active = [c for c in plan["cells"] if c["runs"]]
    return min((len(done.get(c["name"], [])) for c in active), default=0)


def resume_rounds(plan: Dict[str, Any], done: Dict[str, List[str]], remaining: Optional[float],
                  cap: Optional[int] = None) -> int:
    """
    The rounds a resumed campaign runs in all (CR #1): the rounds already done plus
    what the budget left pays for at the plan's round cost — the unfinished part of
    a partly run round first, then whole rounds — capped at `cap` (the saved plan's
    runs per cell, unless --replan). remaining None (a dry run) pays for every round.
    """
    cap = plan["runs_per_cell"] if cap is None else cap
    if remaining is None:
        return cap
    active = [c for c in plan["cells"] if c["runs"]]
    counts = {c["name"]: len(done.get(c["name"], [])) for c in active}
    return max(completed_rounds(plan, done), affordable_runs(active, counts, float(remaining), cap))


def run_campaign(plan: Dict[str, Any], runs_dir: Path, dry_run: bool, rounds: Optional[int] = None,
                 budget: Optional[float] = None, spend_file: Optional[str] = None,
                 run_cell=None, n_rounds: Optional[int] = None) -> int:
    """Interleaved rounds: round r runs run r of every active cell, in priority order.
    Finished runs are recorded in the state file; a re-run resumes. A run halted by
    the guard during its Grand Jury is finished and saved: it counts as done with its
    jury missing, and the next call asks only that jury (main.run_cell does). Stops on
    a non-zero exit (budget guard: 1; anything else: 2). `n_rounds` (resume) overrides
    the plan's runs per cell."""
    from main import build_parser as main_parser
    if run_cell is None:
        from main import run_cell
    state_path = runs_dir / STATE_NAME
    state = read_state(state_path)
    done: Dict[str, List[str]] = state.setdefault("runs", {})
    jury_missing: List[str] = state.setdefault("jury_missing", [])
    state["plan"] = plan
    write_state(state_path, state)

    def args_for(c: Dict[str, Any], r: int) -> argparse.Namespace:
        """main.py's arguments for run r of cell c: its defaults, with the cell's set-up."""
        a = main_parser().parse_args([])
        vars(a).update(scenario=c["scenario"], condition=c["condition"], kin=c["kin"], turns=plan["turns"],
                       seed=r - 1, a2a=c["a2a"], interview_turns=list(plan["interview_turns"]),
                       dry_run=dry_run, budget=budget, spend_file=spend_file,
                       output=str(runs_dir / c["name"]), run_id=f"{c['name']}-run{r:02d}")
        return a

    cells = {c["name"]: c for c in plan["cells"]}
    for run_id in list(jury_missing):                     # finished runs: ask only their Grand Jury
        name, _, num = run_id.rpartition("-run")
        if name not in cells or not num.isdigit():
            continue
        print(f"[campaign] {run_id}: finished, Grand Jury missing; asking the jury only")
        code, _record = run_cell(args_for(cells[name], int(num)))
        if code != 0:
            print(f"[campaign] stopped at {run_id}'s Grand Jury (exit {code}); re-run to resume")
            return code
        jury_missing.remove(run_id)
        write_state(state_path, state)

    total = n_rounds if n_rounds is not None else plan["runs_per_cell"]
    total = min(total, rounds) if rounds else total
    for r in range(1, total + 1):
        for c in plan["cells"]:
            if not c["runs"] or len(done.get(c["name"], [])) >= r:
                continue
            a = args_for(c, r)
            print(f"[campaign] round {r}: {a.run_id}")
            code, record = run_cell(a)
            if code != 0 and is_finished(record):
                done.setdefault(c["name"], []).append(a.run_id)
                jury_missing.append(a.run_id)
                write_state(state_path, state)
                print(f"[campaign] {a.run_id} is finished but its Grand Jury is missing (exit {code}); "
                      "re-run to resume (the jury alone is asked)")
                return code
            if code != 0:
                print(f"[campaign] stopped at {a.run_id} (exit {code}); re-run to resume")
                return code
            done.setdefault(c["name"], []).append(a.run_id)
            write_state(state_path, state)
    print(f"[campaign] {total} round(s) complete")
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
    p.add_argument("--rounds", type=int, default=None, help="the total number of rounds to have done (rounds already done count), not N more")
    p.add_argument("--replan", action="store_true",
                   help="run: re-plan the cells and rounds from the budget left instead of resuming the "
                        "saved plan (rounds = rounds done + floor(budget left / round cost))")
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
    runs_dir = Path(args.runs_dir) if args.runs_dir else RUNS_DIR / ("dry" if args.dry_run else "")
    state_path = runs_dir / STATE_NAME
    state = read_state(state_path)
    saved = state.get("plan") if isinstance(state.get("plan"), dict) and state["plan"].get("cells") else None
    done = state.get("runs") or {}

    if saved and args.command == "run" and not args.replan:
        # Resume (CR #1): the saved plan's cells, cuts, set-up and guard are kept; the
        # rounds are those done plus what the budget left pays for.
        guard = state.get("guard")
        remaining = None if (args.dry_run or guard is None) else max(float(guard) - spent, 0.0)
        n = resume_rounds(saved, done, remaining)
        print("\n".join(campaign_lines(saved)))
        print(f"[campaign] resuming the saved plan ({completed_rounds(saved, done)} round(s) done"
              + (f", ${remaining:.2f} of the guard left" if remaining is not None else "")
              + f"): {n} round(s) in all; --replan re-plans the cells")
        return run_campaign(saved, runs_dir, args.dry_run, args.rounds,
                            budget=None if args.dry_run else guard, spend_file=args.spend_file, n_rounds=n)

    # --replan: each cell keeps the runs it actually ran (never the campaign's
    # completed rounds credited to a cell that has none).
    done_counts = {name: len(runs) for name, runs in done.items() if runs} if (saved and args.replan) else {}
    if args.budget is not None:
        budget = args.budget
    elif saved and args.replan and state.get("guard") is not None:
        budget = max(float(state["guard"]) - spent, 0.0)
    else:
        budget = max(ROUND_BUDGET - spent, 0.0)
    plan = build_campaign(pilot_inputs(pilot_root), budget,
                          turns=args.turns or (saved or {}).get("turns") or pilot_turns(pilot_root),
                          s2_mode=args.s2_mode or pilot_s2_mode(pilot_root, include_dry=args.dry_run),
                          max_runs=args.max_runs, done=done_counts)
    print("\n".join(campaign_lines(plan)))
    if args.command == "plan":
        if saved:
            print(f"  a campaign is in progress in {runs_dir} ({completed_rounds(saved, done)} round(s) done): "
                  "`run` resumes its saved plan; `run --replan` adopts a new one")
        return 0
    guard = spent + budget
    state["guard"] = round(guard, 4)
    state["budget_at_plan"] = round(budget, 4)
    write_state(state_path, state)
    return run_campaign(plan, runs_dir, args.dry_run, args.rounds, budget=None if args.dry_run else guard,
                        spend_file=args.spend_file)


if __name__ == "__main__":
    raise SystemExit(main())
