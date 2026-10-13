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
    python campaign.py plan [--budget USD | --headroom USD] [--turns N] [--a2a merged|separate]
    python campaign.py run  [--dry-run] [--rounds N] [--replan] [same options]

`run` executes the plan round by round through main.run_cell, one run per cell
per round, recording the plan, the guard and finished runs in
<output dir>/campaign_state.json. A stopped campaign resumes where it left off,
with the saved plan: the same cells, cuts and set-up, and rounds = rounds done +
floor(budget left / round cost) (never more than first planned). On a resume,
--budget, --headroom, --turns, --a2a and --max-runs are refused (they would be
ignored): pass --replan to use them. Only a campaign whose first run was
refused (main.py exit 2 with no run log on disk: nothing paid) is planned
afresh, with its saved guard unless --budget or --headroom is given; a first
run that was stopped or killed resumes the saved plan like any other. `run --replan`
re-plans the cells from the budget left instead: each cell keeps the runs it
has done (a cell counts only the runs it actually ran), runs per cell are the
most R whose remaining cost, the sum over the kept cells of (R − done[cell]) ×
run cost, fits the budget left, and cells are cut whole against that sum. A
cell cut before restarts from run 1 only if the re-plan keeps it (its full cost
is then in the sum), and its catch-up runs go in the rounds the campaign has
reached, never ahead of a higher-priority cell's next run (the plan's
catch_up offsets, read by run_order). A run
stopped by the guard during its Grand Jury is finished and saved: it counts as
done with its jury missing, and the resume asks only that jury. A finished run
whose Grand Jury gave no usable verdict after the whole fallback (main.py exit
3; settled, as in an uninterrupted run) counts as done too and is not re-asked:
the campaign goes on and lists it (main.py --reask-grand-jury asks again).

Exit codes (core.exits): 0 done, 3 done but a run has no usable Grand Jury
verdict, else the stopping run's code (1 budget guard, 2 refused, aborted or
crashed).

Money: the shared ledger's guard (a ledger total) is fixed when the campaign is
first planned. --budget USD is that total, as in every other tool (default
$750, the round's budget, pilot included); --headroom USD instead sets it to
the ledger's measured spend so far plus USD. The main runs are planned against
the guard minus the spend so far.
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import RUNS_DIR, load_world
from core.costs import configure, get_tracker, kin_seat_scale, project_run, with_fallbacks
from core.engine import interview_schedule, review_turns
from core.exits import EXIT_FAILED, EXIT_OK, EXIT_REFUSED
from core.records import (KIN_CONDITION, KIN_SCENARIO, cell_name, finished_records, grand_jury_settled,
                          is_finished, write_json_atomic)

logger = logging.getLogger(__name__)

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
    passes); the shares are rounded for the report only."""
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
                   done: Optional[Dict[str, int]] = None, restored: Iterable[str] = (),
                   catch_up: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """
    The schedule: interview decision, per-cell run cost, runs per cell and the
    cells cut. Runs per cell R = affordable_runs: the most R whose cost, the sum
    over the kept cells of (R − done[cell]) × run cost, fits the budget. Cells
    are cut in CUT_ORDER groups while R < min_runs; A is never cut (if even A
    alone gives fewer than min_runs, the cuts that buy the most runs are kept
    and the plan says so; a cut that buys no run is not made). A re-plan
    mid-campaign (--replan) passes `done` ({cell: runs it has done}, only the
    cells that ran) and the budget left; `done_rounds` (every cell the same) is
    the older form. It also passes `restored` (the cells the saved plan cut) and
    the saved plan's `catch_up`: a restored cell the new plan keeps starts its
    catch-up in the round the cells it rejoins have reached, so its run r goes in
    round r + catch_up[cell] (run_order); the saved offsets of kept cells carry over.
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
    kept = {c["name"] for c in active}
    restored = set(restored) & kept
    base = min((done.get(n, 0) for n in kept - restored), default=0)
    offsets = {n: o for n, o in (catch_up or {}).items() if n in kept - restored and o}
    offsets.update({n: base - done.get(n, 0) for n in sorted(restored) if base > done.get(n, 0)})
    return {
        "budget": round(budget, 2), "turns": turns, "s2_mode": s2_mode, "interview": iv,
        "interview_turns": interview_schedule(MAIN_INTERVIEW, turns) if iv["run"] else [],
        "cells": [{"name": c["name"], "group": c["group"], "scenario": c["scenario"], "condition": c["condition"],
                   "kin": c["kin"], "a2a": c["a2a"], "run_cost": c["cost"]["total"], "cost_parts": c["cost"],
                   "runs": runs if c in active else 0} for c in cells],
        "cut_groups": cut, "runs_per_cell": runs, "round_cost": round_cost, "done_rounds": done_min,
        "done": {c["name"]: done.get(c["name"], 0) for c in cells if done.get(c["name"], 0)},
        "catch_up": offsets, "total": round(remaining_cost, 2),
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


def _interview_text(plan: Dict[str, Any]) -> str:
    """'at turns 3, 4, 5, 8 (8 = final)': the turns a run of the plan's length has."""
    turns = plan["interview_turns"]
    return f"at turn{'s' if len(turns) > 1 else ''} {', '.join(map(str, turns))} ({plan['turns']} = final)"


def campaign_lines(plan: Dict[str, Any]) -> List[str]:
    iv = plan["interview"]
    lines = [f"Main-run campaign: ${plan['budget']:.2f} for the main runs, {plan['turns']} turns, S2/kin messages "
             f"{plan['s2_mode']}, interview {_interview_text(plan) if iv['run'] else 'dropped'} (adds at most "
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
    The rounds a resumed campaign runs in all: the rounds already done plus
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
    a non-zero exit (budget guard: 1; anything else: 2), except a finished run whose
    Grand Jury gave no usable verdict after the whole fallback (main.py exit 3, settled):
    it counts as done, is listed in state["no_verdict"] and never re-asked, and the
    campaign goes on (exit 3 at the end while such a run exists).
    `n_rounds` (resume) overrides the plan's runs per cell."""
    from main import build_parser as main_parser
    if run_cell is None:
        from main import run_cell
    state_path = runs_dir / STATE_NAME
    state = read_state(state_path)
    done: Dict[str, List[str]] = state.setdefault("runs", {})
    jury_missing: List[str] = state.setdefault("jury_missing", [])
    no_verdict: List[str] = state.setdefault("no_verdict", [])
    state["plan"] = plan
    state.pop("refused", None)
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
        code, record = run_cell(args_for(cells[name], int(num)))
        if code == EXIT_FAILED and is_finished(record) and grand_jury_settled(record):
            jury_missing.remove(run_id)
            no_verdict.append(run_id)
            write_state(state_path, state)
            continue
        if code != 0:
            print(f"[campaign] stopped at {run_id}'s Grand Jury (exit {code}); re-run to resume")
            return code
        jury_missing.remove(run_id)
        write_state(state_path, state)

    total = n_rounds if n_rounds is not None else plan["runs_per_cell"]
    total = min(total, rounds) if rounds else total
    for c, r in run_order(plan, done, total):
        a = args_for(c, r)
        print(f"[campaign] {a.run_id}")
        code, record = run_cell(a)
        if code == EXIT_FAILED and is_finished(record) and grand_jury_settled(record):
            done.setdefault(c["name"], []).append(a.run_id)
            no_verdict.append(a.run_id)
            write_state(state_path, state)
            print(f"[campaign] {a.run_id} is finished; its Grand Jury gave no usable verdict after the "
                  "fallback (settled, not re-asked: main.py --reask-grand-jury asks again)")
            continue
        if code != 0 and is_finished(record):
            done.setdefault(c["name"], []).append(a.run_id)
            jury_missing.append(a.run_id)
            write_state(state_path, state)
            print(f"[campaign] {a.run_id} is finished but its Grand Jury is missing (exit {code}); "
                  "re-run to resume (the jury alone is asked)")
            return code
        if code != 0:
            if code == EXIT_REFUSED:
                state["refused"] = a.run_id
                write_state(state_path, state)
            print(f"[campaign] stopped at {a.run_id} (exit {code}); re-run to resume")
            return code
        done.setdefault(c["name"], []).append(a.run_id)
        write_state(state_path, state)
    print(f"[campaign] {total} round(s) complete")
    if no_verdict:
        print(f"[campaign] no usable Grand Jury verdict (settled) for {', '.join(no_verdict)}")
        return EXIT_FAILED
    return EXIT_OK


def started(state: Dict[str, Any], runs_dir: Path) -> bool:
    """Whether the saved plan has begun: a run finished, or the last `run` did not end
    on a refusal of its first run, or a run log (finished or partial) of one of its
    cells is on disk."""
    if any((state.get("runs") or {}).values()) or state.get("jury_missing") or not state.get("refused"):
        return True
    cells = [c["name"] for c in (state.get("plan") or {}).get("cells", [])]
    return any(any((runs_dir / n).glob(f"{n}-run*.json")) for n in cells if (runs_dir / n).is_dir())


def run_order(plan: Dict[str, Any], done: Dict[str, List[str]], total: int) -> List[Any]:
    """
    The remaining runs (cell, run number) up to `total` per cell, in interleaved
    rounds in priority order (§9.3): run r of a cell goes in round r, so a resume
    finishes the round in progress before the next begins. A cell a re-plan
    restored after a cut catches up from the round the campaign had reached: its
    run r goes in round r + plan["catch_up"][cell] (build_campaign), never ahead
    of a higher-priority cell's next run.
    """
    active = [c for c in plan["cells"] if c["runs"]]
    offsets = plan.get("catch_up") or {}
    slots = []
    for priority, c in enumerate(active):
        d = len(done.get(c["name"], []))
        for r in range(d + 1, total + 1):
            slots.append((r + offsets.get(c["name"], 0), priority, r, c))
    return [(c, r) for _slot, _priority, r, c in sorted(slots, key=lambda x: x[:3])]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Main-run schedule (spec §9.3).")
    p.add_argument("command", choices=["plan", "run"])
    money = p.add_mutually_exclusive_group()
    money.add_argument("--budget", type=float, default=None,
                       help="ledger total (USD, absolute, shared ledger) at which the campaign stops; the "
                            "main runs get this minus the measured spend so far (default: $750)")
    money.add_argument("--headroom", type=float, default=None,
                       help="USD the main runs may add on top of the ledger's measured spend so far")
    p.add_argument("--turns", type=int, default=None, help="run length (default: the pilot's T7 decision, else 8)")
    p.add_argument("--a2a", choices=["merged", "separate"], default=None,
                   help="S2 and kin message mode (default: T9's decision from the pilot logs)")
    p.add_argument("--pilot-dir", default=None, help="pilot root for the measured inputs (default data/pilot)")
    p.add_argument("--max-runs", type=int, default=None, help=f"runs per cell at most (default {MAX_RUNS})")
    p.add_argument("--rounds", type=int, default=None, help="the total number of rounds to have done (rounds already done count), not N more")
    p.add_argument("--replan", action="store_true",
                   help="run: re-plan the cells and rounds from the budget left instead of resuming the "
                        "saved plan (rounds = rounds done + floor(budget left / round cost))")
    p.add_argument("--dry-run", action="store_true", help="stub models and jurors; logs under data/runs/dry")
    p.add_argument("--output", default=None,
                   help="directory for the cells' logs and campaign_state.json (default data/runs; dry: "
                        "data/runs/dry)")
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
    runs_dir = Path(args.output) if args.output else RUNS_DIR / ("dry" if args.dry_run else "")
    state_path = runs_dir / STATE_NAME
    state = read_state(state_path)
    saved = state.get("plan") if isinstance(state.get("plan"), dict) and state["plan"].get("cells") else None
    done = state.get("runs") or {}

    # A campaign whose first run was refused, with nothing paid, is planned afresh (its
    # guard is kept unless --budget/--headroom is given); a stopped or killed first run
    # resumes the saved plan.
    if saved and not started(state, runs_dir):
        saved = None
    if saved and args.command == "run" and not args.replan:
        given = [flag for flag, value in (("--budget", args.budget), ("--headroom", args.headroom),
                                          ("--turns", args.turns), ("--a2a", args.a2a),
                                          ("--max-runs", args.max_runs)) if value is not None]
        if given:
            print(f"Refusing: {runs_dir} holds a campaign in progress ({completed_rounds(saved, done)} round(s) "
                  f"done), resumed with its saved plan and guard; {', '.join(given)} would be ignored. Pass "
                  "--replan to re-plan with them, or drop them to resume.")
            return EXIT_REFUSED
        # Resume: the saved plan's cells, cuts, set-up and guard are kept; the
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
    replan = saved if args.command == "run" and args.replan else None
    done_counts = {name: len(runs) for name, runs in done.items() if runs} if replan else {}
    if args.headroom is not None:
        budget = args.headroom
    elif args.budget is not None:
        budget = max(args.budget - spent, 0.0)
    elif args.command == "run" and state.get("guard") is not None:
        budget = max(float(state["guard"]) - spent, 0.0)
    else:
        budget = max(ROUND_BUDGET - spent, 0.0)
    plan = build_campaign(pilot_inputs(pilot_root), budget,
                          turns=args.turns or (replan or {}).get("turns") or pilot_turns(pilot_root),
                          s2_mode=args.a2a or pilot_s2_mode(pilot_root, include_dry=args.dry_run),
                          max_runs=args.max_runs or MAX_RUNS, done=done_counts,
                          restored=[c["name"] for c in (replan or {}).get("cells", []) if not c["runs"]],
                          catch_up=(replan or {}).get("catch_up"))
    print("\n".join(campaign_lines(plan)))
    if args.command == "plan":
        if saved:
            print(f"  a campaign is in progress in {runs_dir} ({completed_rounds(saved, done)} round(s) done): "
                  "`run` resumes its saved plan; `run --replan` adopts a new one")
        return EXIT_OK
    guard = spent + budget
    print(f"[campaign] guard: ledger total ${guard:.2f} (${spent:.2f} spent so far)")
    state["guard"] = round(guard, 4)
    state["budget_at_plan"] = round(budget, 4)
    write_state(state_path, state)
    return run_campaign(plan, runs_dir, args.dry_run, args.rounds, budget=None if args.dry_run else guard,
                        spend_file=args.spend_file)


if __name__ == "__main__":
    from core.cli import run_main
    raise SystemExit(run_main(main))
