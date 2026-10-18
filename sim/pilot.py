#!/usr/bin/env python3
"""
Pilot driver (spec §9.2): five smoke-test runs, in order, under one $48 guard.

    priced  T1 rotation 1 (Anthropic and Meta swap capability seeds, K recomputed).
            S1, condition A, F3, merged messages, 8 turns, interview after every
            turn, T2 disposition rating, no Grand Jury rubric. The usage
            calibration: after it the pilot HARD STOPS, prints the measured cost
            and the re-priced plan (written to priced/share.txt for Slack), and
            waits for the team's T7 decision:
                python pilot.py approve --turns 8      (or --turns 12)
    T6      T1 rotation 2 (OpenAI and Google DeepMind swap). Interview after every
            turn; the Grand Jury's rubric scoring runs here (T8).
    T5      S1, A, 4 turns, merged; the freeze at turn 2, when a scripted xAI
            accesses the tenancy of OpenAI (success forced, no exposure); a true tip reaches
            OpenAI and Google DeepMind at turn 3. No interview.
    T4      S2, separate messages, interview at turns 3, 4, 5 and the final turn.
    T9      S2, merged messages (T4's set-up).

Budget (§9.2): $50 for the pilot, a guard that halts it at $48 of measured
spend (one ledger, data/spend.json). Never cut: priced, T6, T5, T4. At approval
the rest of the pilot is re-priced from the priced run's measured costs; if the
plan exceeds the guard, T9 is cut first (S2 and the kin cell then keep the
two-round pre-step), then T4 drops its interview; if it still exceeds the guard
the approval is refused: stop and agree on the next step. Unspent budget is the
reserve, first for one T6 re-run (`python pilot.py T6 --rerun`).

T7 (run length) is decided by the team after the priced run, for every later
run except T5 (4 turns): `approve --turns 12` lengthens T6, T4 and T9. The hard
stop prints the proposed criterion (first tenancy access or first report in turn 7
or 8 -> lengthen to 12, unannounced) and the sparse-events note.

After every run the juror rates each model seat-run's disposition once, blind
(T2; core.jury.DispositionJury on core.transcript.disposition_transcript); the
ratings pool in <pilot dir>/disposition_ratings.json (tools/disposition.py) and
each run's ratings are also kept in its record (final.t2_ratings). The T6
coding sheet (interview answers and proposal rationales of the priced and T6
runs) is exported to <pilot dir>/T6_coding_sheet.csv after those runs.

Commands:
    python pilot.py plan [--turns N] [--budget X]   the plan and its costs (estimates, or measured)
    python pilot.py priced                     the priced run, then the hard stop
    python pilot.py approve --turns 8|12 [--budget X]
                                               T7 decision + re-priced plan with cuts; --budget
                                               moves the guard (a team decision), stored in
                                               approval.json and enforced in every later test
    python pilot.py T6|T5|T4|T9                one test (needs the approval)
    python pilot.py all                        every test not yet done, in order; stops at the hard stop
    python pilot.py T6 --rerun                 the reserve's one T6 re-run (T6-run02); a second is refused
    python pilot.py T6 --rescore-grand-jury    T8: re-ask the Grand Jury on the latest T6 record after
                                               the examples are revised (seats not re-run; once)
  A completed test is refused unless --skip-completed (skip it, exit 0) or
  --force (run it again; the old record and any partial log beside it are moved
  aside, never deleted). A finished run is never paid for again: re-invoking its
  test (or --post-run-only) does only what it still misses (the Grand Jury, T2
  ratings, ratings pooled but not saved in the record, the summary, the priced
  run's hard stop) and then marks the test completed; this holds for a run kept
  as <run_id>-vN too. A T2 rating that stays unusable after the juror's retries
  and fallback is settled, as an uninterrupted run ends: recorded as unusable
  (the summary's post-run errors), never re-asked on its own;
  `python pilot.py <test> --rerate` asks the juror again for those seats only. After the re-run, `T6` and `T6 --post-run-only` mean the
  re-run (it replaces T6-run01; T2 and T8 read it). A run stopped mid-way
  resumes from its partial log (core.resume): logged turns are replayed with no
  call. The T6 re-run writes its own summary (pilot_summary_T6-rerun.json), so a
  failed re-run never un-does T6, and it can be retried. A dry run never
  overwrites or sets aside a real run's log (nor the reverse): give it another
  --output.
  options: --dry-run (stub models and jurors, $0, logs under data/pilot/dry/),
           --output DIR (pilot root), --spend-file, --budget (guard override:
           --budget, else the approval's, else pilot.json's $48), --skip-completed,
           --force, --post-run-only, --rerate, --force-order, --allow-placeholder-values,
           --allow-no-examples, --sequential.

Exit codes (core.exits): 0 done (including the hard stop and a skipped
completed test), 1 budget guard, 2 aborted, crashed or refused (an unknown test
included), 3 approval refused (the re-priced plan exceeds the guard after the
cuts), 5 network unavailable: no connection for the whole network wait window
(core.llm.network_wait_s; config/pilot.json "network_wait_s"). The pilot stops at
once (`all` never moves on to the next test), writes no summary, keeps the
partial record, the ratings already made and the logs; re-running the same
command resumes the same test exactly (the calls already made are answered from
the run's call journal; an interrupted Grand Jury verdict or T2 rating is
pending, never settled).
"""

import argparse
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.console import say
from core.logsetup import configure_logging
from core.config import CONFIG_DIR, LAB_ORDER, PILOT_DIR, build_labs_and_world, load_world
from core import costs as costs_mod
from core.costs import BudgetExceeded, configure, get_tracker, measure_runs, project_run, with_fallbacks
from core.economy import swap_capability
from core.engine import RunAborted, SimulationEngine, interview_schedule, review_turns
from core.exits import EXIT_BUDGET, EXIT_FAILED, EXIT_NETWORK, EXIT_OK, EXIT_REFUSED
from core import jury as jury_mod
from core.jury import GRAND_JURY_MODELS, MACRO_JURY_MODELS, DispositionJury, collect_grand_jury
from core.llm import NETWORK_STOP_HINT, FatalAPIError, NetworkUnavailable
from core.policies import POLICIES
from core.preflight import model_problems, placeholder_labs
from core.records import (DRY_SUBDIR, PARTIAL_SUFFIX, finished_records, grand_jury_settled,
                          grand_jury_usable, is_dry, is_finished, read_json, write_json_atomic)
from core.resume import remove_stale_partial
from core.stubs import install_stubs
from core.transcript import disposition_transcript, seat_letters
from tools import summarize_run as SR
from tools.compare_arms import compare as compare_arms

logger = logging.getLogger(__name__)

PILOT_FILE = CONFIG_DIR / "pilot.json"
RATINGS_NAME = "disposition_ratings.json"
DRY_RATINGS_NAME = "disposition_ratings.dry.json"
APPROVAL_NAME = "approval.json"
SHEET_NAME = "T6_coding_sheet.csv"
SHARE_NAME = "share.txt"
COMMANDS = ("plan", "approve", "all")

JUROR_PURPOSES = ("grand_jury", "macro_jury", "disposition_jury")
EXIT_CODES = {"completed": EXIT_OK, "halted_budget": EXIT_BUDGET}   # aborted / crashed / refused -> 2


def _real_jurors() -> Dict[str, List[str]]:
    return {"grand": list(GRAND_JURY_MODELS), "disposition": list(GRAND_JURY_MODELS),
            "macro": list(MACRO_JURY_MODELS)}


# ---------------------------------------------------------------------------
# T1 seed swap
# ---------------------------------------------------------------------------

def parse_swap(value: Any) -> Optional[Tuple[str, str]]:
    """'anthropic:meta' -> ('anthropic', 'meta'), validated; None/'' -> None."""
    if not value:
        return None
    a, sep, b = str(value).partition(":")
    a, b = a.strip(), b.strip()
    if not sep or a not in LAB_ORDER or b not in LAB_ORDER or a == b:
        raise SystemExit(f"bad swap {value!r}: use KEY:KEY with keys from {', '.join(LAB_ORDER)}")
    return a, b


# ---------------------------------------------------------------------------
# Config, directories and the approval (hard stop)
# ---------------------------------------------------------------------------

def load_pilot() -> Dict[str, Any]:
    with open(PILOT_FILE) as f:
        return json.load(f)


def _write_json(path: Path, data: Any) -> None:
    """Write-then-rename so an interrupted pilot never leaves a half-written file."""
    write_json_atomic(path, data)


def pilot_root(dry_run: bool, output: Optional[str] = None) -> Path:
    """The pilot directory: --output, else data/pilot (dry runs: data/pilot/dry)."""
    if output:
        return Path(output)
    return PILOT_DIR / DRY_SUBDIR if dry_run else PILOT_DIR


def ratings_path(root: Path, dry_run: bool) -> Path:
    return root / (DRY_RATINGS_NAME if dry_run else RATINGS_NAME)


def read_approval(root: Path) -> Optional[Dict[str, Any]]:
    a = read_json(root / APPROVAL_NAME)
    return a if isinstance(a, dict) and a.get("turns") else None


def summary_path(root: Path, test: str, rerun: bool = False) -> Path:
    """The test's summary; the T6 re-run writes its own, so a failed re-run never
    replaces T6's completed summary."""
    return root / test / (f"pilot_summary_{test}-rerun.json" if rerun else f"pilot_summary_{test}.json")


def review_path(root: Path, test: str, rerun: bool = False) -> Path:
    """The test's review file; the T6 re-run writes its own (review_T6-rerun.md), so
    T6-run01's review is kept."""
    return root / test / (f"review_{test}-rerun.md" if rerun else f"review_{test}.md")


def read_summary(root: Path, test: str, rerun: bool = False) -> Optional[Dict[str, Any]]:
    s = read_json(summary_path(root, test, rerun))
    return s if isinstance(s, dict) else None


def base_run_id(test: str) -> str:
    return f"{test}-run01"


def _completed(s: Optional[Dict[str, Any]]) -> bool:
    return bool(s and s.get("status") == "completed" and s.get("runs_completed"))


def test_done(root: Path, test: str) -> bool:
    """
    The test is done: its summary says completed. A finished run whose summary
    is missing or failed (a kill or a budget stop after the run) is completed by
    re-invoking its test, which collects only what is missing and writes the
    summary. A T6 re-run's summary found in T6's own file never decides T6:
    T6's earlier summary (.prev) does.
    """
    s = read_summary(root, test)
    if s and test == "T6" and s.get("run_id") == "T6-run02":
        s = read_json(summary_path(root, test).with_name("pilot_summary_T6.prev.json"))
    return _completed(s)


def resolve_preset(data: Dict[str, Any], name: str, approval: Optional[Dict[str, Any]] = None,
                   turns: Optional[int] = None) -> Dict[str, Any]:
    """
    The preset as it runs: defaults + preset, then the T7 run length (the
    approval's turns, or `turns`) for presets that follow T7 (all but T5),
    then the approval's cuts ("cut": True for a dropped test; T4's interview
    dropped).
    """
    presets = data.get("presets", {})
    if name not in presets:
        raise SystemExit(f"Unknown preset {name!r}. Choose from: {', '.join(data.get('order') or presets)}")
    p = dict(data.get("defaults", {}))
    p.update(presets[name])
    p["name"] = name
    t7 = turns if turns is not None else (approval or {}).get("turns")
    if t7 and p.get("follows_t7", True):
        p["turns"] = int(t7)
    p["cut"] = False
    for cut in (approval or {}).get("cuts") or []:
        if cut.get("test") != name:
            continue
        if cut.get("drop"):
            p["cut"] = True
        if "interview_turns" in cut:
            p["interview_turns"] = cut["interview_turns"]
    return p


# ---------------------------------------------------------------------------
# Plan and re-pricing (spec §9.2 Budget)
# ---------------------------------------------------------------------------

def _n_interviews(preset: Dict[str, Any]) -> int:
    return len(interview_schedule(preset.get("interview_turns"), int(preset["turns"])))


def _n_reviews(preset: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None) -> int:
    return len(review_turns(cfg or load_world(), int(preset["turns"])))


def _seat_scale(preset: Dict[str, Any]) -> float:
    return (len(LAB_ORDER) - len(preset.get("scripted_seats") or {})) / len(LAB_ORDER)


def estimate_preset(data: Dict[str, Any], preset: Dict[str, Any], column: int) -> float:
    """est_cost[column] (0 assumed, 1 heavy) scaled by the run length against the preset's own
    turns, less T4's interview when it is cut (four turns at the fallback interview rate)."""
    base = dict(data.get("defaults", {}))
    base.update(data["presets"][preset["name"]])
    est = float((preset.get("est_cost") or [0, 0])[column])
    est *= int(preset["turns"]) / int(base["turns"])
    dropped = _n_interviews(base) - _n_interviews(preset) if base.get("interview_turns") else 0
    if dropped > 0:
        est -= dropped * costs_mod.FALLBACK_INPUTS["interview_per_turn"] * (1.5 if column else 1.0)
    return round(max(est, 0.0), 2)


def project_preset(preset: Dict[str, Any], inputs: Dict[str, Any],
                   cfg: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    """One run of a preset priced from measured inputs (core.costs.project_run)."""
    return project_run(inputs, turns=int(preset["turns"]), a2a=preset.get("a2a", "merged"),
                       interview_turns=_n_interviews(preset), grand_jury=bool(preset.get("grand_jury")),
                       disposition_rating=bool(preset.get("disposition_rating", True)),
                       reviews=_n_reviews(preset, cfg) if preset.get("macro_jury", True) else 0,
                       seat_scale=_seat_scale(preset))


def measured_inputs(root: Path, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The cost inputs measured by the completed tests' summaries (mean over the tests
    that measured each input), or None when nothing was measured (before the priced
    run, or a dry run)."""
    acc: Dict[str, List[float]] = {}
    shares: List[Dict[str, float]] = []
    for test in data.get("order") or []:
        s = read_summary(root, test)
        inputs = (s or {}).get("inputs") or {}
        for k, v in inputs.items():
            if k in costs_mod.FALLBACK_INPUTS and isinstance(v, (int, float)) and v > 0:
                acc.setdefault(k, []).append(float(v))
        if inputs.get("seat_share"):
            shares.append(inputs["seat_share"])
    if not acc:
        return None
    out: Dict[str, Any] = {k: round(sum(v) / len(v), 4) for k, v in acc.items()}
    if shares:
        # Per test, each model's share times the number of models in it is its cost per
        # seat-turn relative to that test's mean (1 when all cost the same), so a test
        # with fewer model seats (T5: xAI scripted) does not inflate the others.
        # Each model's mean over the tests it played in, normalised once.
        rel = [{m: v * len(s) for m, v in s.items()} for s in shares]
        models = {m for r in rel for m in r}
        mean = {m: sum(r[m] for r in rel if m in r) / sum(1 for r in rel if m in r) for m in models}
        total = sum(mean.values())
        out["seat_share"] = {m: round(v / total, 4) for m, v in mean.items()} if total > 0 else {}
    return out


def build_plan(data: Dict[str, Any], root: Path, turns: Optional[int] = None,
               approval: Optional[Dict[str, Any]] = None, spent: Optional[float] = None,
               cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    The pilot plan in order, priced, with the §9.2 cuts.

    Basis "measured" (after the priced run, real mode): spent = the ledger's
    measured spend (everything so far, rate_charters included); each pending
    test is projected from the measured inputs (core.costs). Basis "estimate"
    (before the priced run, or a dry run): every step at its est_cost, in two
    columns (assumed, heavy), rate_charters included as spent. Cuts apply in
    order (T9 dropped, then T4's interview) while the column's total exceeds the
    guard; "stop" when it still does. The first column decides.
    """
    guard = float(data["budget_guard"])
    cfg = cfg or load_world()
    inputs = measured_inputs(root, data)
    basis = "measured" if inputs else "estimate"
    t7 = turns if turns is not None else (approval or {}).get("turns")
    columns = ["measured"] if basis == "measured" else ["assumed", "heavy"]
    full_inputs = with_fallbacks(inputs) if inputs else None
    steps: List[Dict[str, Any]] = []
    order = list(data.get("order") or [])
    for name in order:
        done = test_done(root, name)
        ran = (read_summary(root, name) or {}).get("turns") if done else None
        preset = resolve_preset(data, name, None, t7)
        if ran:
            preset["turns"] = int(ran)          # a finished test is priced at the length it ran
        if approval and not done:
            preset = resolve_preset(data, name, approval, t7)
        iv_dropped = bool(approval and not done and any(
            c.get("test") == name and "interview_turns" in c and not c.get("drop")
            for c in approval.get("cuts") or []))
        step = {"test": name, "done": done, "turns": int(preset["turns"]),
                "a2a": preset.get("a2a"), "scenario": preset.get("scenario"),
                "interviews": _n_interviews(preset), "grand_jury": bool(preset.get("grand_jury")),
                "cut_by_approval": bool(preset.get("cut")), "interview_dropped": iv_dropped, "costs": {}}
        if done:
            s = read_summary(root, name) or {}
            step["measured_cost"] = float(s.get("run_cost_usd") or 0.0)
        for i, col in enumerate(columns):
            if basis == "measured":
                step["costs"][col] = 0.0 if done else project_preset(preset, full_inputs, cfg)["total"]
            else:
                step["costs"][col] = estimate_preset(data, preset, i)
        step["_preset"] = preset
        steps.append(step)
    fixed = {k: v for k, v in (data.get("fixed_costs") or {}).items()}
    out_cols: Dict[str, Any] = {}
    for i, col in enumerate(columns):
        if basis == "measured":
            base_spent = float(spent if spent is not None else get_tracker().persisted_total())
        else:
            base_spent = sum(float(v[i]) for v in fixed.values()) + \
                sum(s["costs"][col] for s in steps if s["done"])
        pending = {s["test"]: s["costs"][col] for s in steps if not s["done"] and not s["cut_by_approval"]}
        applied: List[Dict[str, Any]] = []
        total = round(base_spent + sum(pending.values()), 2)
        for cut in data.get("cuts") or []:
            if total <= guard:
                break
            test = cut.get("test")
            if test not in pending or test in (data.get("never_cut") or []) and cut.get("drop"):
                continue
            step = next(s for s in steps if s["test"] == test)
            if not cut.get("drop") and step.get("interview_dropped"):
                continue                        # the approval already applied this cut
            if cut.get("drop"):
                saves = pending.pop(test)
            else:
                trimmed = dict(step["_preset"], interview_turns=cut.get("interview_turns", []))
                new = (project_preset(trimmed, full_inputs, cfg)["total"] if basis == "measured"
                       else estimate_preset(data, trimmed, i))
                saves = pending[test] - new
                pending[test] = new
            applied.append(dict(cut, saves=round(saves, 2)))
            total = round(base_spent + sum(pending.values()), 2)
        out_cols[col] = {"spent": round(base_spent, 2), "pending": {k: round(v, 2) for k, v in pending.items()},
                         "total": total, "reserve": round(guard - total, 2), "cuts": applied,
                         "stop": total > guard}
    for s in steps:
        s.pop("_preset", None)
    first = out_cols[columns[0]]
    return {"basis": basis, "columns": columns, "guard": guard, "budget": data.get("budget"),
            "turns": t7, "steps": steps, "by_column": out_cols, "cuts": first["cuts"],
            "stop": first["stop"], "total": first["total"], "reserve": first["reserve"],
            "inputs": full_inputs, "fixed_costs": fixed}


def plan_lines(plan: Dict[str, Any]) -> List[str]:
    cols = plan["columns"]
    head = (f"Pilot plan ({'measured: re-priced from the completed runs' if plan['basis'] == 'measured' else 'estimates: assumed / heavy, eval_tractability §2'}"
            f"; T7 run length {plan['turns'] or 8} turns, T5 4 turns; guard {usd(plan['guard'])} of "
            f"{usd(plan.get('budget') or 50)})")
    lines = [head, f"  {'step':<14}{'status':<12}{'set-up':<34}" + "".join(f"{c:>10}" for c in cols)]
    if plan["basis"] == "estimate":
        lines.append(f"  {'rate_charters':<14}{'fixed':<12}{'5 seats':<34}"
                     + "".join(f"{float(plan['fixed_costs']['rate_charters'][i]):>10.2f}"
                               for i, _c in enumerate(cols)))
    for s in plan["steps"]:
        setup = (f"{s['scenario']}, {s['a2a']}, {s['turns']} t, "
                 + ("iv dropped" if s.get("interview_dropped") else f"iv x{s['interviews']}")
                 + (", GJ" if s["grand_jury"] else ""))
        status = "done" if s["done"] else ("cut" if s["cut_by_approval"] else "pending")
        row = f"  {s['test']:<14}{status:<12}{setup:<34}"
        for c in cols:
            pending = plan["by_column"][c]["pending"]
            if s["done"]:
                row += f"{float(s.get('measured_cost') or 0.0):>10.2f}"      # measured, never an estimate
            elif s["test"] in pending:
                row += f"{pending[s['test']]:>10.2f}"
            else:
                row += f"{'cut':>10}"
        lines.append(row)
    if plan["basis"] == "estimate" and any(s["done"] for s in plan["steps"]):
        lines.append("  done tests show their measured cost; the estimate columns' totals count them at "
                     "their estimates (" + ", ".join(f"{s['test']} {float(s['costs'][cols[0]]):.2f}"
                                                     for s in plan["steps"] if s["done"]) + f" {cols[0]})")
    for c in cols:
        col = plan["by_column"][c]
        cut_txt = "; ".join(f"{x['label']} (-${x['saves']:.2f})" for x in col["cuts"]) or "none"
        verdict = ("OVER the guard after every cut: stop and agree on the next step" if col["stop"]
                   else f"fits the {usd(plan['guard'])} guard, reserve ${col['reserve']:.2f} "
                        "(first for one T6 re-run)")
        lines.append(f"  [{c}] spent ${col['spent']:.2f} + pending = ${col['total']:.2f}; cuts: {cut_txt}; {verdict}")
    if plan["basis"] == "measured" and plan.get("inputs"):
        i = plan["inputs"]
        lines.append("  inputs per run: turn 1 ${:.2f}, later turns ${:.2f}, separate rounds ${:.2f}/turn, "
                     "interview ${:.2f}/turn, MacroJury ${:.2f}/review, Grand Jury ${:.2f}, T2 rating ${:.2f}"
                     .format(i["actor_first_turn"], i["actor_later_turn"], i["a2a_per_turn"],
                             i["interview_per_turn"], i["macro_per_review"], i["grand_jury"],
                             i["disposition_rating"])
                     + (f" (estimated: {', '.join(i['estimated'])})" if i.get("estimated") else ""))
    return lines


T7_NOTE = ("T7 (run length, the team decides now, for every later run except T5): default 8 turns. "
           "Proposed criterion for lengthening to 12, unannounced: the first event of a kind (first "
           "tenancy access or first report) falls in turn 7 or 8. Too few events to read are also grounds "
           "to lengthen.")


# ---------------------------------------------------------------------------
# T2: the juror's blind disposition rating after every run
# ---------------------------------------------------------------------------

RATING_FIELDS = ("ratings", "chosen", "failures")
SETTLED_NOTE = " (no usable rating after the juror's retries and fallback: settled, not re-asked; --rerate asks again)"


def new_rating_rows() -> Dict[str, Any]:
    return {"ratings": {}, "chosen": {}, "failures": {}, "families": {}}


def model_seats(record: Dict[str, Any]) -> List[SimpleNamespace]:
    """The record's model seats (scripted seats left out): key, model, family, chosen value."""
    cfg = record.get("config") or {}
    scripted = set(cfg.get("seat_policies") or {})
    if cfg.get("policy"):
        return []
    chosen = (record.get("final") or {}).get("dispositions") or {}
    return [SimpleNamespace(key=k, model=v.get("model"), family=v.get("family"), disposition=chosen.get(k))
            for k, v in (record.get("labs") or {}).items() if k not in scripted]


def rate_dispositions(record: Dict[str, Any], jurors: List[str], run_id: str, test: str, dry_run: bool,
                      rows: Optional[Dict[str, Any]] = None, only: Optional[List[str]] = None
                      ) -> Dict[str, Any]:
    """
    T2 (§2, §9.2): the juror rates each model seat-run once from its own
    redacted record (seat letters, masked names and chosen number). `rows` is
    filled in place, so a budget stop keeps what was rated; a failing call is
    recorded in rows["failures"] and the next seat is asked. A seat with no
    usable rating after the juror's whole call chain (retries and fallback) is
    settled, as an uninterrupted run ends: its last failure row says
    "settled": true and it is never asked again on its own (--rerate asks
    again). `only` limits the seats (--post-run-only).
    """
    rows = rows if rows is not None else new_rating_rows()
    tag = {"run_id": run_id, "test": test, "dry_run": dry_run}
    letters = seat_letters(record)
    jury = DispositionJury(jurors)
    for seat in model_seats(record):
        if only is not None and seat.key not in only:
            continue
        rows["families"][seat.key] = seat.family
        if seat.disposition is not None:
            rows["chosen"][seat.key] = [dict(tag, value=seat.disposition)]
        try:
            rating = jury.rate_seat(disposition_transcript(record, seat.key), run_id=run_id,
                                    seat=letters.get(seat.key))
            failed = list(jury.failed)
        except BudgetExceeded:
            raise
        except Exception as e:      # noqa: BLE001 — one seat's failure never loses the others
            logger.error(f"[T2] {run_id} {seat.key}: {type(e).__name__}: {e}")
            rating, failed = None, [{"juror": jurors[0] if jurors else None,
                                     "error": f"{type(e).__name__}: {e}", "settled": False}]
        if rating:
            rows["ratings"].setdefault(seat.key, []).append(dict(rating, letter=letters.get(seat.key), **tag))
        for i, f in enumerate(failed):
            f = {k: v for k, v in f.items() if k != "result"}
            if not rating and i == len(failed) - 1:
                f["settled"] = f.get("settled", True)
            rows["failures"].setdefault(seat.key, []).append(dict(f, **tag))
    return rows


def append_ratings(path: Path, rows: Dict[str, Any], test: str, run_id: str, dry_run: bool,
                   merge: bool = False) -> None:
    """
    Pool one run's ratings in the shared file (tools/disposition.py reads it).
    A re-run replaces the run's rows; merge=True (--post-run-only) keeps them
    and replaces only the seats asked again. Dry rows only ever go to a
    *.dry.json file.
    """
    if dry_run and not path.name.endswith(".dry.json"):
        raise ValueError(f"dry-run ratings may only go to a *.dry.json file, not {path}")
    pooled = read_json(path) or {}
    for field in RATING_FIELDS + ("families",):
        pooled.setdefault(field, {})
    pooled.setdefault("runs", [])
    same = lambda r: isinstance(r, dict) and r.get("test") == test and r.get("run_id") == run_id
    asked = {seat for field in RATING_FIELDS for seat in (rows.get(field) or {})}
    for field in RATING_FIELDS:
        for seat in list(pooled[field]):
            if merge and seat not in asked:
                continue
            pooled[field][seat] = [r for r in pooled[field][seat] if not same(r)]
        for seat, items in (rows.get(field) or {}).items():
            pooled[field].setdefault(seat, []).extend(items)
    pooled["families"].update(rows.get("families") or {})
    pooled["runs"] = [r for r in pooled["runs"] if not same(r)] + \
        [{"test": test, "run_id": run_id, "dry_run": dry_run}]
    _write_json(path, pooled)


def pooled_ratings(path: Optional[Path], test: str, run_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """This run's ratings in the pooled file, by seat."""
    pooled = read_json(Path(path)) if path else None
    pooled = pooled if isinstance(pooled, dict) else {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for seat, items in (pooled.get("ratings") or {}).items():
        mine = [r for r in items or [] if isinstance(r, dict) and r.get("run_id") == run_id
                and r.get("test") == test]
        if mine:
            out[seat] = mine
    return out


def settled_unusable(path: Optional[Path], test: str, run_id: str) -> List[str]:
    """Seats of this run whose T2 rating stayed unusable after the juror's whole call
    chain (a settled failure in the pooled file, no rating)."""
    pooled = read_json(Path(path)) if path else None
    pooled = pooled if isinstance(pooled, dict) else {}
    rated = pooled_ratings(path, test, run_id)
    out = []
    for seat, items in (pooled.get("failures") or {}).items():
        mine = [r for r in items or [] if isinstance(r, dict) and r.get("run_id") == run_id
                and r.get("test") == test]
        if seat not in rated and any(r.get("settled") for r in mine):
            out.append(seat)
    return sorted(out)


def unrated_seats(path: Optional[Path], record: Dict[str, Any], test: str, run_id: str,
                  rerate: bool = False) -> List[str]:
    """Model seats of this run with no rating in the pooled file, leaving out the
    settled unusable ones unless `rerate` (--rerate asks them again)."""
    rated = pooled_ratings(path, test, run_id)
    settled = set() if rerate else set(settled_unusable(path, test, run_id))
    return [s.key for s in model_seats(record) if s.key not in rated and s.key not in settled]


def ratings_not_in_record(path: Optional[Path], record: Dict[str, Any], test: str,
                          run_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """Seats rated in the pooled file whose rating the record does not hold
    (final.t2_ratings): a stop between pooling and saving the record."""
    held = (record.get("final") or {}).get("t2_ratings") or {}
    seats = {s.key for s in model_seats(record)}
    return {seat: rows for seat, rows in pooled_ratings(path, test, run_id).items()
            if seat in seats and seat not in held}


def store_t2_in_record(record: Dict[str, Any], rows: Dict[str, Any]) -> None:
    """final.t2_ratings: {lab: {"disposition", "juror", "letter"}} (summarize_run: chosen vs judged)."""
    final = record.setdefault("final", {})
    t2 = dict(final.get("t2_ratings") or {})
    for seat, items in (rows.get("ratings") or {}).items():
        if items:
            r = items[-1]
            t2[seat] = {"disposition": r.get("disposition"), "juror": r.get("juror"), "letter": r.get("letter")}
    final["t2_ratings"] = t2


# ---------------------------------------------------------------------------
# Served model ids (the jury module keeps only parsed results)
# ---------------------------------------------------------------------------

_SERVED: Dict[Tuple[str, str], Dict[str, int]] = {}
_SERVED_LOCK = threading.Lock()


def note_served(model: str, purpose: str, attempts: Any) -> None:
    for a in attempts or []:
        served = a.get("served_model") if isinstance(a, dict) else None
        if served:
            with _SERVED_LOCK:
                row = _SERVED.setdefault((model, purpose), {})
                row[served] = row.get(served, 0) + 1


def install_served_probe() -> None:
    """Wrap the jury module's complete_json to note the served model id of every juror call."""
    fn = jury_mod.complete_json
    if getattr(fn, "_served_probe", False):
        return

    def wrapped(model, *a, **kw):
        try:
            obj, attempts = fn(model, *a, **kw)
        except Exception as e:
            note_served(model, kw.get("purpose", "juror"), getattr(e, "attempts", None))
            raise
        note_served(model, kw.get("purpose", "juror"), attempts)
        return obj, attempts

    wrapped._served_probe = True
    wrapped._original = fn
    jury_mod.complete_json = wrapped


def uninstall_served_probe() -> None:
    fn = jury_mod.complete_json
    while getattr(fn, "_served_probe", False):
        fn = fn._original
    jury_mod.complete_json = fn


def served_mismatch(configured: str, served: str) -> bool:
    """True when the provider served a different model than the configured id (dated suffixes allowed)."""
    norm = lambda m: str(m).lower().split("/")[-1]
    c, s = norm(configured), norm(served)
    return not (s.startswith(c) or c.startswith(s))


# ---------------------------------------------------------------------------
# Usage report: failures, stop reasons, tokens, cost per provider (T1)
# ---------------------------------------------------------------------------

def _new_actor_row() -> Dict[str, Any]:
    return {"seat_turns": 0, "forfeits": 0, "proposal_calls": 0, "proposal_failed": 0,
            "message_calls": 0, "message_failed": 0, "stop_max_tokens": 0, "stop_refusal": 0,
            "stop_timeout": 0, "interviews": 0, "interviews_skipped": 0, "interviews_empty": 0,
            "interviews_incomplete": 0, "interviews_cut": 0, "interviews_retried": 0,
            "input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
            "cost": 0.0, "served_models": {}}


def _tally_attempt(row: Dict[str, Any], a: Dict[str, Any], kind: str) -> None:
    row[f"{kind}_calls"] += 1
    row[f"{kind}_failed"] += int(bool(a.get("error")))
    stop = a.get("stop")
    row["stop_max_tokens"] += int(stop == "max_tokens")
    row["stop_refusal"] += int(stop in ("refusal", "safety"))
    row["stop_timeout"] += int(stop == "timeout")
    for field in ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens"):
        row[field] += a.get(field) or 0
    row["cost"] += a.get("cost") or 0.0
    if a.get("served_model"):
        row["served_models"][a["served_model"]] = row["served_models"].get(a["served_model"], 0) + 1


def usage_report(records: List[Dict[str, Any]], run_ids: List[str],
                 calls: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    Per actor model: proposal and message-round failure rates (parse failures
    per provider, T1), forfeits, stop reasons, interviews (A.7): answered,
    skipped (call failed or no usable proposal; scripted seats are not
    counted), empty answers (still empty after the retry, counted apart from
    the skips), incomplete (hit the output cap), cut (logged at 300 tokens)
    and retried; tokens, cost and served model ids. Per juror model and role:
    calls, cost and served ids from `calls` (default: this process's tracker).
    `records` may include a partial record.
    """
    calls = get_tracker().calls if calls is None else calls
    by_model: Dict[str, Dict[str, Any]] = {}
    for record in records:
        for turn in record.get("turns", []):
            for key, entry in (turn.get("actors") or {}).items():
                if not isinstance(entry, dict) or entry.get("scripted") or key not in record.get("labs", {}):
                    continue
                model = record["labs"][key]["model"]
                row = by_model.setdefault(model, _new_actor_row())
                row["seat_turns"] += 1
                row["forfeits"] += int(bool(entry.get("forfeited")))
                for a in entry.get("attempts") or []:
                    _tally_attempt(row, a, "proposal")
                for a in entry.get("message_attempts") or []:
                    _tally_attempt(row, a, "message")
            for key, iv in (turn.get("interviews") or {}).items():
                model = (record.get("labs", {}).get(key) or {}).get("model")
                if model not in by_model or not isinstance(iv, dict):
                    continue
                row = by_model[model]
                reason = str(iv.get("reason") or "")
                if not iv.get("skipped"):
                    row["interviews"] += 1
                    row["interviews_incomplete"] += int(bool(iv.get("incomplete")))
                    row["interviews_cut"] += int(bool(iv.get("cut")))
                elif reason.startswith("empty answer"):
                    row["interviews_empty"] += 1
                elif reason != "scripted seat":
                    row["interviews_skipped"] += 1
                row["interviews_retried"] += int(bool(iv.get("retried")))
                for field in ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens"):
                    row[field] += iv.get(field) or 0
                row["cost"] += iv.get("cost") or 0.0
    for model, row in by_model.items():
        row["proposal_failure_rate"] = round(row["proposal_failed"] / row["proposal_calls"], 3) \
            if row["proposal_calls"] else None
        row["message_failure_rate"] = round(row["message_failed"] / row["message_calls"], 3) \
            if row["message_calls"] else None
        row["cost"] = round(row["cost"], 4)
        row["served_mismatch"] = sorted(s for s in row["served_models"] if served_mismatch(model, s))
    ids = set(run_ids)
    jurors: Dict[str, Dict[str, Any]] = {}
    for c in calls:
        if c.get("run_id") not in ids or c.get("purpose") not in JUROR_PURPOSES:
            continue
        row = jurors.setdefault(f"{c['model']}|{c['purpose']}", {
            "model": c["model"], "purpose": c["purpose"], "calls": 0, "cost": 0.0})
        row["calls"] += 1
        row["cost"] = round(row["cost"] + (c.get("cost") or 0.0), 4)
    for row in jurors.values():
        served = dict(_SERVED.get((row["model"], row["purpose"]), {}))
        row["served_models"] = served
        row["served_mismatch"] = sorted(s for s in served if served_mismatch(row["model"], s))
    by_purpose: Dict[str, float] = {}
    for c in calls:
        if c.get("run_id") in ids:
            by_purpose[c.get("purpose") or "?"] = round(by_purpose.get(c.get("purpose") or "?", 0.0)
                                                        + (c.get("cost") or 0.0), 4)
    return {"by_actor_model": by_model, "jurors": jurors, "by_purpose": by_purpose,
            "run_cost_usd": round(sum(by_purpose.values()), 4)}


# ---------------------------------------------------------------------------
# Stale finals and completed runs
# ---------------------------------------------------------------------------

EXPECT_FIELDS = ("scenario", "condition", "a2a_mode", "turns", "seed", "rotation", "event_turn")


def stale_conflict(output_dir: Path, run_id: str, expect: Optional[Dict[str, Any]]) -> Optional[str]:
    """Why the final at <run_id>.json is NOT an earlier run of this run (another
    set-up), or None when there is no final, it cannot be read, or it matches."""
    if not expect:
        return None
    old = read_json(output_dir / f"{run_id}.json")
    if not isinstance(old, dict):
        return None
    cfg = old.get("config") or {}
    diffs = [f"{f} {cfg.get(f)!r} != {expect[f]!r}" for f in EXPECT_FIELDS
             if f in expect and cfg.get(f) is not None and (cfg.get(f) or None) != (expect[f] or None)]
    return "; ".join(diffs) or None


def resolve_run_id(output_dir: Path, run_id: str, expect: Optional[Dict[str, Any]]) -> str:
    """The id this run is kept under: `run_id`, or "<run_id>-vN" when the final at the
    base id (and at each earlier -vN) is a run of another set-up. Reads only."""
    candidate, n = run_id, 1
    while stale_conflict(output_dir, candidate, expect):
        n += 1
        candidate = f"{run_id}-v{n}"
    return candidate


def other_mode_log(output_dir: Path, run_id: str, dry_run: bool) -> Optional[Path]:
    """A finished record or partial log of the other mode (dry vs real) at this run id:
    a dry run must never overwrite or set aside a real run's files (nor the reverse)."""
    for path in (output_dir / f"{run_id}.json", output_dir / f"{run_id}{PARTIAL_SUFFIX}"):
        rec = read_json(path)
        if isinstance(rec, dict) and isinstance(rec.get("turns"), list) and is_dry(rec) != bool(dry_run):
            return path
    return None


def claim_run_id(output_dir: Path, run_id: str, expect: Optional[Dict[str, Any]],
                 ratings_file: Optional[Path], force: bool = False) -> Optional[str]:
    """The id this run writes under (resolve_run_id). A leftover unfinished final there
    is deleted. A finished (paid) record of this set-up is never deleted: None without
    `force`; with it, the record and any partial log beside it are moved aside to
    <name>.replaced-<time> (no longer read as a run). Either way the run's pooled T2
    ratings are dropped, the run being replaced."""
    candidate = resolve_run_id(output_dir, run_id, expect)
    if candidate != run_id:
        logger.warning(f"[stale] {run_id}.json in {output_dir} holds a different run; saving as {candidate}")
    path = output_dir / f"{candidate}.json"
    if path.exists():
        if is_finished(read_json(path)):
            if not force:
                logger.warning(f"[stale] {path} is a finished run; kept (pass --force to replace it)")
                return None
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for old in (path, output_dir / f"{candidate}{PARTIAL_SUFFIX}"):
                if old.exists():
                    moved = old.with_name(f"{old.name}.replaced-{stamp}")
                    old.replace(moved)
                    logger.info(f"[stale] {old} moved aside to {moved.name}")
        else:
            path.unlink()
            logger.info(f"[stale] removed {path}")
        if ratings_file and Path(ratings_file).exists():
            pooled = read_json(Path(ratings_file)) or {}
            for field in RATING_FIELDS:
                for seat in list((pooled.get(field) or {})):
                    pooled[field][seat] = [r for r in pooled[field][seat] if r.get("run_id") != candidate]
            pooled["runs"] = [r for r in pooled.get("runs") or [] if r.get("run_id") != candidate]
            _write_json(Path(ratings_file), pooled)
    return candidate


def completed_record(output_dir: Path, run_id: str, expect: Optional[Dict[str, Any]],
                     dry_run: bool) -> Optional[Dict[str, Any]]:
    """The finished record at <run_id>.json when it is this same run (same set-up, same
    dry or real mode), else None."""
    rec = read_json(output_dir / f"{run_id}.json")
    if not is_finished(rec) or not isinstance(rec.get("turns"), list):
        return None
    if is_dry(rec) != bool(dry_run):
        return None
    if stale_conflict(output_dir, run_id, expect):
        return None
    remove_stale_partial(output_dir, run_id)
    return rec


# ---------------------------------------------------------------------------
# Post-run stages: the Grand Jury (T6 run only, re-ask) and the T2 rating
# ---------------------------------------------------------------------------

def missing_post_run(record: Dict[str, Any], run_id: str, test: str, ratings_file: Path,
                     preset: Dict[str, Any], rerate: bool = False) -> List[str]:
    """What the finished run still misses. A settled stage (a Grand Jury verdict or a
    T2 rating unusable after the whole call chain) is done; `rerate` counts the
    settled unusable T2 ratings as missing (--rerate)."""
    out = []
    if preset.get("grand_jury") and not grand_jury_settled(record):
        gj = (record.get("final") or {}).get("grand_jury")
        out.append(f"Grand Jury verdict ({(gj or {}).get('error') or 'none'})")
    if preset.get("disposition_rating", True):
        seats = unrated_seats(ratings_file, record, test, run_id, rerate)
        if seats:
            out.append(f"T2 ratings ({', '.join(seats)})")
        unsaved = ratings_not_in_record(ratings_file, record, test, run_id)
        if unsaved:
            out.append(f"T2 ratings pooled but not in the record ({', '.join(unsaved)})")
    return out


def run_post_stages(record: Dict[str, Any], run_id: str, test: str, output_dir: Path, ratings_file: Path,
                    jurors: Dict[str, List[str]], dry_run: bool, preset: Dict[str, Any],
                    only_missing: bool = False, rerate: bool = False) -> Dict[str, Any]:
    """
    The post-run stages of one finished run: the Grand Jury re-ask
    (only_missing, T6) and the T2 rating (only_missing: the seats not yet rated;
    rerate: the settled unusable ones too). A failing stage is recorded and the
    run still counts; BudgetExceeded and NetworkUnavailable propagate after what
    was rated is pooled and saved, with this dict as e.post_run.
    """
    from core.logsetup import run_logging
    with run_logging(output_dir, run_id, stage="post-run"):
        return _run_post_stages(record, run_id, test, output_dir, ratings_file, jurors, dry_run, preset,
                                only_missing, rerate)


def _run_post_stages(record: Dict[str, Any], run_id: str, test: str, output_dir: Path, ratings_file: Path,
                     jurors: Dict[str, List[str]], dry_run: bool, preset: Dict[str, Any],
                     only_missing: bool = False, rerate: bool = False) -> Dict[str, Any]:
    post: Dict[str, Any] = {"errors": [], "done": [], "rated": 0}
    stage = "Grand Jury"
    try:
        if only_missing and preset.get("grand_jury") and not grand_jury_settled(record):
            try:
                post["done"].append(collect_grand_jury(record, jurors["grand"], run_id, output_dir))
            except BudgetExceeded:
                raise
            except Exception as e:      # noqa: BLE001
                post["errors"].append(f"Grand Jury: {type(e).__name__}: {e}")
        stage = "T2 rating"
        if preset.get("disposition_rating", True) and model_seats(record):
            only = unrated_seats(ratings_file, record, test, run_id, rerate) if only_missing else None
            if only is None or only:
                rows = new_rating_rows()
                try:
                    rate_dispositions(record, jurors["disposition"], run_id, test, dry_run, rows, only)
                except (BudgetExceeded, NetworkUnavailable):
                    # the seats rated so far are kept; the interrupted seat stays unrated
                    if rows["ratings"] or rows["failures"]:
                        append_ratings(ratings_file, rows, test, run_id, dry_run, merge=only_missing)
                        store_t2_in_record(record, rows)
                        _write_json(output_dir / f"{run_id}.json", record)
                    raise
                except Exception as e:  # noqa: BLE001
                    post["errors"].append(f"T2 rating: {type(e).__name__}: {e}")
                append_ratings(ratings_file, rows, test, run_id, dry_run, merge=only_missing)
                store_t2_in_record(record, rows)
                _write_json(output_dir / f"{run_id}.json", record)
                post["rated"] = sum(len(v) for v in rows["ratings"].values())
                for seat, fails in rows["failures"].items():
                    if seat not in rows["ratings"]:
                        post["errors"].append(f"T2 rating {seat}: {fails[-1].get('error')}"
                                              + (SETTLED_NOTE if fails[-1].get("settled") else ""))
                if only_missing:
                    post["done"].append(f"T2 ratings: {post['rated']} seat(s) rated")
            unsaved = ratings_not_in_record(ratings_file, record, test, run_id) if only_missing else {}
            if unsaved:
                store_t2_in_record(record, {"ratings": unsaved})
                _write_json(output_dir / f"{run_id}.json", record)
                post["done"].append(f"T2 ratings copied from the pool to the record: {', '.join(unsaved)}")
    except BudgetExceeded as e:
        post["errors"].append(f"{stage}: stopped by the budget guard")
        e.post_run = post
        raise
    except NetworkUnavailable as e:
        post["errors"].append(f"{stage}: stopped: network unavailable")
        e.post_run = post
        raise
    return post


# ---------------------------------------------------------------------------
# Decisions printed after each test (tools/summarize_run.py holds the rules)
# ---------------------------------------------------------------------------

def _records_of(root: Path, tests: List[str], include_dry: bool) -> List[Dict[str, Any]]:
    """The finished run records of these tests: decisions never read a partial log
    (an aborted T6 re-run is never "the latest T6")."""
    out = []
    for t in tests:
        d = root / t
        if d.is_dir():
            out += [rec for _p, rec in finished_records([d], include_dry=include_dry)]
    return out


def pilot_decisions(root: Path, test: str, dry_run: bool, data: Dict[str, Any],
                    rerun: bool = False) -> Dict[str, str]:
    """The decision lines a test speaks to, over the pooled pilot runs they read
    (T1: priced + T6; T4: T4 + T9; T3: every pilot run; T5: T5; T6: the coding
    sheet of the priced and T6 runs; T7: the priced run, plus T6; T9:
    compare_arms), plus two readings that decide nothing: the MacroJury's first
    review of the run (§7 slant read) and, after T4, the turns 1-2 hindcast (§9.2)."""
    order = list(data.get("order") or [])
    lines: Dict[str, str] = {}
    review = str(review_path(root, test, rerun))
    if test in ("priced", "T6"):
        t1 = SR.t1_decision(_records_of(root, ["priced", "T6"], dry_run))
        lines["T1"] = t1["text"]
        recs = _records_of(root, ["priced", "T6"], dry_run)
        sheet = root / SHEET_NAME
        filled = SR.t6_sheet_is_filled(sheet)
        before = len(SR.read_t6_sheet(sheet))
        n = SR.export_t6_sheet(recs, sheet)          # merges: codes already entered are kept
        if not filled:
            lines["T6"] = (f"coding sheet exported: {n} row(s) in {sheet} (interview answers and proposal "
                           "rationales); code each row, then python tools/summarize_run.py --t6-tally "
                           f"{sheet}")
        else:
            tally = SR.t6_tally(sheet)["text"].removeprefix("T6: ")     # printed as "T6: ..." once
            lines["T6"] = tally + (f" (sheet {sheet}: {n - before} new row(s) merged, "
                                   "codes already entered kept)")
    if test in ("priced", "T6"):
        t7 = SR.t7_line(_records_of(root, ["priced", "T6"], dry_run))    # the priced run (+ T6)
        if t7:
            lines["T7"] = t7
    slant = SR.macro_slant_line(_records_of(root, [test], dry_run))
    if slant:
        lines["MacroJury"] = slant
    if test == "T4":
        hc = SR.hindcast_line(_records_of(root, ["T4"], dry_run))
        if hc:
            lines["Hindcast"] = hc
    pooled = _records_of(root, order, dry_run)
    agg = SR.aggregate(pooled, decide=["T3"], review=review) if pooled else {"decisions": {}}
    lines.update(agg["decisions"])
    if test == "T5":
        t5 = SR.aggregate(_records_of(root, ["T5"], dry_run), decide=["T5"], review=review)
        lines.update(t5["decisions"])
    if test in ("T4", "T9"):
        s2 = _records_of(root, ["T4", "T9"], dry_run)
        lines.update(SR.aggregate(s2, decide=["T4"], review=review)["decisions"])
    if test == "T9" or (test == "T4" and (root / "T9").is_dir()):
        sep, mer = _records_of(root, ["T4"], dry_run), _records_of(root, ["T9"], dry_run)
        if sep and mer:
            review_t9 = (f"{root / 'T9' / 'review_T9-compare.md'} (written by python tools/compare_arms.py "
                         f"--separate {root / 'T4'} --merged {root / 'T9'}"
                         + (" --include-dry" if dry_run else "") + ")")
            lines["T9"] = compare_arms(sep, mer, review=review_t9)["text"].removeprefix("T9: ")
    return lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Pilot driver (spec §9.2).")
    p.add_argument("test", metavar="TEST", help="priced, T6, T5, T4, T9, or: plan, approve, all")
    p.add_argument("--dry-run", action="store_true", help="stub models and jurors, zero cost; logs under data/pilot/dry")
    p.add_argument("--output", default=None, help="pilot root directory (default data/pilot, dry: data/pilot/dry)")
    p.add_argument("--spend-file", default=None, help="measured-spend ledger (default sim/data/spend.json)")
    p.add_argument("--budget", type=float, default=None,
                   help="the guard: ledger total (USD, absolute, shared ledger) at which the pilot halts "
                        "(a team decision): approve stores it; default the approval's, else pilot.json's")
    p.add_argument("--turns", type=int, default=None,
                   help="T7: the run length the team decided (approve, plan); 8 or 12")
    p.add_argument("--rerun", action="store_true", help="T6 only: the reserve's re-run (T6-run02)")
    p.add_argument("--skip-completed", action="store_true",
                   help="skip a test that is already completed (exit 0) instead of refusing it")
    p.add_argument("--force", action="store_true",
                   help="re-run a completed test (replacing its run), or redo T6's one re-run")
    p.add_argument("--rescore-grand-jury", action="store_true",
                   help="T6 only (T8): re-ask the Grand Jury on the finished T6 record after the few-shot "
                        "examples are revised; never re-runs the seats; the old verdict is kept as "
                        "final.grand_jury_previous; once")
    p.add_argument("--post-run-only", action="store_true",
                   help="run nothing new: complete a finished run (missing T2 ratings, the T6 Grand Jury, the "
                        "summary)")
    p.add_argument("--rerate", action="store_true",
                   help="ask the T2 juror again for the seats of the test's finished run whose rating stayed "
                        "unusable after the juror's retries and fallback (settled; never re-asked otherwise); "
                        "the seats are not re-run")
    p.add_argument("--force-order", action="store_true", help="run a test before the earlier ones are done")
    p.add_argument("--allow-placeholder-values", action="store_true",
                   help="allow real runs while charter values are still placeholders")
    p.add_argument("--allow-no-examples", action="store_true",
                   help="real T6 without few-shot examples (config/jury_examples.json): no rubric scores")
    p.add_argument("--sequential", action="store_true", help="call the seats one after another")
    p.add_argument("--verbose", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging("pilot", verbose=args.verbose)
    with _SERVED_LOCK:
        _SERVED.clear()
    install_served_probe()
    try:
        return _dispatch(args)
    finally:
        uninstall_served_probe()


def effective_guard(args, data: Dict[str, Any], approval: Optional[Dict[str, Any]]) -> float:
    """The guard in force: --budget, else the approval's (a team decision
    recorded by `approve --budget`), else config/pilot.json's."""
    if getattr(args, "budget", None) is not None:
        return float(args.budget)
    if approval and approval.get("budget_guard") is not None:
        return float(approval["budget_guard"])
    return float(data["budget_guard"])


def usd(x: Any) -> str:
    """A dollar amount as given, to the cent (a guard of $37.60 is never shown as $38)."""
    return f"${float(x):.2f}"


def _configure(args, data: Dict[str, Any]) -> Optional[float]:
    """The tracker for this command: the shared ledger and, in a real run, the guard
    in force (data["budget_guard"] already holds --budget or the approval's)."""
    budget = None if args.dry_run else data.get("budget_guard")
    configure(spend_file=Path(args.spend_file) if args.spend_file else None, budget=budget)
    return budget


def _dispatch(args) -> int:
    data = load_pilot()
    root = pilot_root(args.dry_run, args.output)
    approval = read_approval(root)
    data = dict(data, budget_guard=effective_guard(args, data, approval))
    if args.test == "plan":
        _configure(args, data)
        plan = build_plan(data, root, turns=args.turns, approval=approval)
        say("\n".join(plan_lines(plan)))
        if not test_done(root, "priced"):
            say("  next: python pilot.py priced" + (" --dry-run" if args.dry_run else ""))
        return EXIT_OK
    if args.test == "approve":
        _configure(args, data)
        return approve(args, data, root)
    if args.test == "all":
        return run_all(args, data, root)
    if args.test not in (data.get("presets") or {}):
        say(f"Unknown test {args.test!r}: choose one of {', '.join(data.get('order') or [])}, "
              f"or a command ({', '.join(COMMANDS)}).")
        return EXIT_REFUSED
    if args.rescore_grand_jury:
        return rescore_grand_jury(args, data, root, args.test)
    return run_test(args, data, root, args.test)


def approve(args, data: Dict[str, Any], root: Path) -> int:
    """
    The team's go after the hard stop: records the T7 run length and the cuts
    the re-priced plan needs. Refused when the priced run is not done, or when
    the plan still exceeds the guard after every cut (exit 3: stop and agree on
    the next step).
    """
    if not test_done(root, "priced"):
        say("Refusing to approve: the priced run is not done (python pilot.py priced).")
        return EXIT_REFUSED
    allowed = data.get("t7_turns") or [8, 12]
    if args.turns is None or args.turns not in allowed:
        say(f"approve needs the team's T7 decision: --turns {' or --turns '.join(str(t) for t in allowed)}")
        return EXIT_REFUSED
    # data["budget_guard"] is --budget (a team decision to move the guard), else the
    # earlier approval's, else pilot.json's; it is stored and enforced from now on.
    plan = build_plan(data, root, turns=args.turns)
    say("\n".join(plan_lines(plan)))
    if plan["stop"]:
        say(f"\nAPPROVAL REFUSED: the re-priced plan ({usd(plan['total'])}) exceeds the {usd(plan['guard'])} "
              "guard after every cut. Stop and agree on the next step (spec §9.2).")
        return EXIT_FAILED
    approval = {"turns": args.turns, "cuts": [{k: v for k, v in c.items() if k != "saves"} for c in plan["cuts"]],
                "budget_guard": plan["guard"],
                "plan_total": plan["total"], "reserve": plan["reserve"], "basis": plan["basis"],
                "at": time.strftime("%Y-%m-%d %H:%M:%S"), "dry_run": args.dry_run}
    _write_json(root / APPROVAL_NAME, approval)
    say(f"\napproved: later runs at {args.turns} turns (T5 stays 4); guard {usd(plan['guard'])} "
          "(enforced in every later test); cuts: "
          + ("; ".join(c["label"] for c in approval["cuts"]) or "none")
          + f"; next: python pilot.py T6{' --dry-run' if args.dry_run else ''}")
    return EXIT_OK


def run_all(args, data: Dict[str, Any], root: Path) -> int:
    """Every test not yet done, in order; stops at the hard stop, a cut test is skipped."""
    for test in data.get("order") or []:
        approval = read_approval(root)
        if test_done(root, test):
            say(f"[all] {test}: done")
            continue
        if test != "priced" and approval is None:
            say(f"[all] stopping before {test}: the priced run's hard stop needs the team's approval "
                  "(python pilot.py approve --turns 8|12)")
            return EXIT_OK
        if resolve_preset(data, test, approval)["cut"]:
            say(f"[all] {test}: cut at approval")
            continue
        code = run_test(args, data, root, test)
        if code != EXIT_OK:
            return code
        if resolve_preset(data, test).get("hard_stop"):
            return EXIT_OK
    return EXIT_OK


def _refusal(args, data, root: Path, test: str, preset: Dict[str, Any], approval) -> Optional[str]:
    """Why this test may not run now, or None. A finished run that still misses a
    post-run stage or its summary never reaches here (run_test completes it)."""
    order = list(data.get("order") or [])
    if test != "priced" and approval is None:
        return ("the priced run's hard stop: share its cost, decide T7, then "
                "python pilot.py approve --turns 8|12")
    if preset["cut"]:
        return f"{test} was cut at approval (the re-priced plan exceeded the guard)"
    event = int(preset.get("event_turn") or load_world().get("event_turn", 4))
    if not 1 <= event < int(preset["turns"]):
        return f"event turn {event} must be between 1 and {int(preset['turns']) - 1} for a {preset['turns']}-turn run"
    if args.rerun and test != "T6":
        return "--rerun is only for T6 (the reserve's re-run)"
    if args.rerun and not test_done(root, "T6"):
        return "--rerun needs a finished T6 run"
    if args.rerun and rerun_done(root, data, args.dry_run) and not args.force:
        return ("T6 has had its one re-run (T6-run02, §9.2) and it is complete; pass --force to redo that "
                "re-run (it never adds a third run)")
    if test_done(root, test) and not args.rerun and not args.force:
        return (f"{test} is already completed ({(read_summary(root, test) or {}).get('run_id')}); pass "
                "--skip-completed to skip it, --force to run it again (replacing the run)"
                + (", or --rerun for the reserve's one T6 re-run" if test == "T6" else ""))
    if not args.force_order and not args.rerun:
        cut = {c.get("test") for c in (approval or {}).get("cuts") or [] if c.get("drop")}
        before = [t for t in order[:order.index(test)] if t not in cut and not test_done(root, t)]
        if before:
            return f"run the earlier tests first ({', '.join(before)}), or pass --force-order"
    if not args.dry_run:
        if not args.allow_placeholder_values and placeholder_labs():
            return (f"charter values are placeholders for {', '.join(placeholder_labs())}; run "
                    "rate_charters.py first, or pass --allow-placeholder-values")
        if preset.get("requires_examples") and not args.allow_no_examples and not jury_mod.load_examples():
            return ("T6 needs the juror's few-shot examples (config/jury_examples.json, from our hand "
                    "scores of the priced run, T8); pass --allow-no-examples to run without rubric scores")
    return None


def run_expect(preset: Dict[str, Any], seed: int) -> Dict[str, Any]:
    """The set-up a finished record of this test must match (stale_conflict)."""
    return {"scenario": preset["scenario"], "condition": preset["condition"], "a2a_mode": preset["a2a"],
            "turns": int(preset["turns"]), "seed": seed, "rotation": preset.get("swap"),
            "event_turn": preset.get("event_turn") or load_world().get("event_turn")}


def run_target(root: Path, test: str, preset: Dict[str, Any], rerun: bool) -> Tuple[str, Dict[str, Any]]:
    """(run id, expected set-up) of the test's run, or of T6's re-run: <test>-run01
    (re-run: T6-run02), or its -vN when the base id holds a run of another set-up."""
    seed = 1 if rerun else 0
    expect = run_expect(preset, seed)
    return resolve_run_id(root / test, f"{test}-run{seed + 1:02d}", expect), expect


def run_complete(root: Path, test: str, run_id: str, record: Dict[str, Any], preset: Dict[str, Any],
                 dry_run: bool, rerun: bool) -> bool:
    """A finished run with nothing left to do: every post-run stage collected, its
    summary completed for this run id, and after the priced run the hard stop's
    share.txt written."""
    if missing_post_run(record, run_id, test, ratings_path(root, dry_run), preset):
        return False
    s = read_summary(root, test, rerun)
    if not (_completed(s) and s.get("run_id") == run_id):
        return False
    return not (preset.get("hard_stop") and not rerun and not (root / test / SHARE_NAME).exists())


def _rerun_record(root: Path, preset: Dict[str, Any], dry_run: bool
                  ) -> Tuple[str, Optional[Dict[str, Any]]]:
    run_id, expect = run_target(root, "T6", preset, True)
    return run_id, completed_record(root / "T6", run_id, expect, dry_run)


def rerun_done(root: Path, data: Dict[str, Any], dry_run: bool) -> bool:
    """True when T6's one re-run (T6-run02) is finished and complete (post-run stages
    and its summary)."""
    preset = resolve_preset(data, "T6", read_approval(root))
    run_id, rec = _rerun_record(root, preset, dry_run)
    return rec is not None and run_complete(root, "T6", run_id, rec, preset, dry_run, rerun=True)


def latest_t6(root: Path, data: Dict[str, Any], dry_run: bool) -> Tuple[str, bool]:
    """(run id, is the re-run) of the latest T6 run: the re-run once it has a finished
    record (it replaces T6-run01, §9.2), else T6-run01. T2 and T8 read this run."""
    preset = resolve_preset(data, "T6", read_approval(root))
    run_id, rec = _rerun_record(root, preset, dry_run)
    if rec is not None:
        return run_id, True
    return run_target(root, "T6", preset, False)[0], False


def run_test(args, data: Dict[str, Any], root: Path, test: str) -> int:
    if test in COMMANDS:
        say(f"Refusing: {test} is a command, not a test.")
        return EXIT_REFUSED
    approval = read_approval(root)
    preset = resolve_preset(data, test, approval)
    output_dir = root / test
    ratings_file = ratings_path(root, args.dry_run)
    if test == "T6" and not args.rerun and not args.force and latest_t6(root, data, args.dry_run)[1]:
        args = argparse.Namespace(**dict(vars(args), rerun=True))     # after the re-run, T6 means it
    run_id, expect = run_target(root, test, preset, args.rerun)

    # A finished run of this set-up is never paid for again: without --force, only
    # what it still misses (post-run stages, the summary, the hard stop) is done.
    finished = None if args.force and not (args.post_run_only or args.rerate) else \
        completed_record(output_dir, run_id, expect, args.dry_run)
    if args.skip_completed and not (args.post_run_only or args.rerate) and not args.rerun \
            and test_done(root, test):
        say(f"{test}: {(read_summary(root, test) or {}).get('run_id') or base_run_id(test)} already "
              "completed (skipped)")
        return EXIT_OK
    if args.post_run_only or args.rerate or (finished is not None and not run_complete(
            root, test, run_id, finished, preset, args.dry_run, args.rerun)):
        return _post_run_only(args, data, root, preset, test, run_id, finished, output_dir, ratings_file)
    if args.skip_completed and args.rerun and rerun_done(root, data, args.dry_run):
        say(f"T6: {run_id} (the one re-run) already completed (skipped)")
        return EXIT_OK
    why = _refusal(args, data, root, test, preset, approval)
    if why:
        say(f"Refusing {test}: {why}.")
        return EXIT_REFUSED
    other = other_mode_log(output_dir, run_id, args.dry_run)
    if other is not None:
        say(f"Refusing {test}: {other} is a {'real' if args.dry_run else 'dry'} run's log; a "
              f"{'dry' if args.dry_run else 'real'} run never overwrites or sets it aside: pass another "
              "--output.")
        return EXIT_REFUSED
    if args.dry_run and preset.get("requires_examples") and not jury_mod.load_examples():
        logger.warning("[T8] no few-shot examples yet: the Grand Jury records 'no examples yet' (dry run)")
    budget = _configure(args, data)
    swap = parse_swap(preset.get("swap"))
    scripted = {k: POLICIES[name] for k, name in (preset.get("scripted_seats") or {}).items()}
    seed = int(expect["seed"])
    turns = int(preset["turns"])

    if not args.dry_run:
        probe_labs, _ = build_labs_and_world(load_world(), charters=False)
        problems = model_problems(probe_labs, _real_jurors(), scripted, bool(preset.get("macro_jury", True)),
                                  bool(preset.get("grand_jury")))
        if problems:
            say("Preflight failed; nothing was called:\n  " + "\n  ".join(problems))
            return EXIT_REFUSED
    claimed = claim_run_id(output_dir, run_id, expect, ratings_file, force=args.force)
    if claimed is None:
        say(f"Refusing {test}: {run_id} has a finished record of this set-up that cannot be completed "
              "here; pass --force to run it again (the old record is kept aside).")
        return EXIT_REFUSED
    run_id = claimed

    plan = build_plan(data, root, approval=approval)
    est = next((s for s in plan["steps"] if s["test"] == test), {}).get("costs") or {}
    say(f"\nPILOT {test}: {run_id} — {preset['scenario']}, condition {preset['condition']}, F3, "
          f"{preset['a2a']} messages, {turns} turns"
          + (f", event turn {preset['event_turn']}" if preset.get("event_turn") else "")
          + (f", swap {preset['swap']}" if preset.get("swap") else "")
          + f", interviews {interview_schedule(preset.get('interview_turns'), turns) or 'none'}"
          + (", Grand Jury rubric" if preset.get("grand_jury") else "")
          + "; projected " + ", ".join(f"{k} ${v:.2f}" for k, v in est.items())
          + (" (dry run: $0 measured)" if args.dry_run else f" (guard ${budget})"))
    sys.stdout.flush()

    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={}, charters=True)
    if swap:
        swap_capability(labs, swap[0], swap[1], cfg["economy"]["capability_compute_elasticity"])
    jurors = install_stubs(labs) if args.dry_run else _real_jurors()
    record: Optional[Dict[str, Any]] = None
    partial: Optional[Dict[str, Any]] = None
    post: Dict[str, Any] = {}
    status, aborted = "completed", None
    try:
        get_tracker().check()
        engine = SimulationEngine(
            labs, world, cfg, scenario=preset["scenario"], condition=preset["condition"],
            a2a_mode=preset["a2a"], turns=turns, seed=seed,
            grand_jurors=jurors["grand"],
            macro_jurors=jurors["macro"] if preset.get("macro_jury", True) else [],
            seat_policies=scripted, interventions=preset.get("interventions") or [],
            choose_disposition=bool(preset.get("choose_disposition", True)),
            run_grand_jury=bool(preset.get("grand_jury")), run_id=run_id, output_dir=output_dir,
            run_meta={"test": test, "run_id": run_id, "pilot": True, "seed": seed,
                      "swap": preset.get("swap"), "t7_turns": (approval or {}).get("turns")},
            rotation=preset.get("swap"), dry_run=args.dry_run, parallel=not args.sequential,
            interview_turns=preset.get("interview_turns"), event_turn=preset.get("event_turn"))
        record = engine.run()
        post = run_post_stages(record, run_id, test, output_dir, ratings_file, jurors, args.dry_run, preset)
    except NetworkUnavailable as e:
        # A stop, not a failure: no summary (the test is not done, nothing is marked
        # failed); the partial record, the ratings made and the logs are kept.
        say(f"{test}: {run_id} stopped: {e}\n{NETWORK_STOP_HINT}")
        return EXIT_NETWORK
    except BudgetExceeded as e:
        status, aborted = "halted_budget", _failure(run_id, e, output_dir)
        post = getattr(e, "post_run", None) or post
        if record is None:
            fin = read_json(output_dir / f"{run_id}.json")
            record = fin if is_finished(fin) else None
    except (RunAborted, FatalAPIError) as e:
        status, aborted = "aborted", _failure(run_id, e, output_dir)
    except Exception as e:          # any crash keeps the paid data and the summary
        logger.exception(f"[crash] {run_id}: {type(e).__name__}: {e}")
        status, aborted = "crashed", _failure(run_id, e, output_dir)
    if record is None and aborted and aborted.get("record_path"):
        partial = read_json(Path(aborted["record_path"]))
    return _write_summary(args, data, root, test, run_id, preset, budget, record, partial, status,
                          aborted, post)


def run_calls(run_id: str) -> List[Dict[str, Any]]:
    """Every call of this run: the paid ones from the ledger's call log (every process
    that worked on the run), plus this process's unlogged $0 calls."""
    tracker = get_tracker()
    out: List[Dict[str, Any]] = []
    try:
        with open(tracker.calls_file) as f:
            for line in f:
                try:
                    c = json.loads(line)
                except ValueError:
                    continue
                if isinstance(c, dict) and c.get("run_id") == run_id:
                    out.append(c)
    except OSError:
        pass
    return out + [c for c in tracker.calls if c.get("run_id") == run_id
                  and not (c.get("cost") or c.get("possibly_billed"))]


POST_RUN_INPUTS = ("grand_jury", "disposition_rating")


def merge_usage(prior: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """A run's usage after a post-run-only process: the prior summary's spend by
    purpose and juror plus this process's calls (seat rows come from the record)."""
    out = dict(new)
    by_purpose = dict(prior.get("by_purpose") or {})
    for k, v in (new.get("by_purpose") or {}).items():
        by_purpose[k] = round(by_purpose.get(k, 0.0) + v, 4)
    jurors = {k: dict(v) for k, v in (prior.get("jurors") or {}).items()}
    for k, row in (new.get("jurors") or {}).items():
        old = jurors.get(k)
        if old is None:
            jurors[k] = row
            continue
        served = dict(old.get("served_models") or {})
        for m, n in (row.get("served_models") or {}).items():
            served[m] = served.get(m, 0) + n
        jurors[k] = dict(old, calls=old.get("calls", 0) + row["calls"],
                         cost=round(float(old.get("cost") or 0.0) + row["cost"], 4), served_models=served,
                         served_mismatch=sorted(set(old.get("served_mismatch") or [])
                                                | set(row.get("served_mismatch") or [])))
    out.update(by_purpose=by_purpose, jurors=jurors, run_cost_usd=round(sum(by_purpose.values()), 4))
    return out


def measured_usage(root: Path, test: str, run_id: str, rerun: bool, records: List[Dict[str, Any]],
                   post_run_only: bool) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """(usage, measured cost inputs) for the summary. The run's own process measures
    them from its calls. A post-run-only process made only juror calls: it keeps the
    earlier summary's usage and inputs and adds its post-run spend (Grand Jury, T2
    rating) to them; with no earlier summary of this run (a kill before it was
    written) they are rebuilt from the ledger's call log. Never from fallbacks."""
    if not post_run_only:
        calls = get_tracker().calls
        return usage_report(records, [run_id], calls), measure_runs(calls, records)
    prior = read_summary(root, test, rerun)
    if prior and prior.get("run_id") == run_id and prior.get("usage") and prior.get("inputs") is not None:
        here = get_tracker().calls
        inputs = dict(prior["inputs"])
        added = measure_runs(here, records)
        for key in POST_RUN_INPUTS:
            if added.get(key):
                inputs[key] = round(float(inputs.get(key) or 0.0) + added[key], 4)
        return merge_usage(prior["usage"], usage_report(records, [run_id], here)), inputs
    calls = run_calls(run_id)
    whole = [{k: v for k, v in r.items() if k != "resumed"} for r in records]    # the log has every turn
    return usage_report(records, [run_id], calls), measure_runs(calls, whole)


def _write_summary(args, data: Dict[str, Any], root: Path, test: str, run_id: str, preset: Dict[str, Any],
                   budget: Optional[float], record: Optional[Dict[str, Any]],
                   partial: Optional[Dict[str, Any]], status: str, aborted: Optional[Dict[str, Any]],
                   post: Dict[str, Any]) -> int:
    """Write and print the test's summary (the T6 re-run's in its own file, so a failed
    re-run never makes T6 not done); the hard stop after the priced run. Returns
    the exit code."""
    approval = read_approval(root)
    output_dir = root / test
    usage_records = [r for r in (record, partial) if isinstance(r, dict)]
    summary: Dict[str, Any] = {
        "test": test, "run_id": run_id, "dry_run": args.dry_run, "status": status, "aborted": aborted,
        "runs_completed": int(record is not None), "turns": int(preset["turns"]),
        "preset": {k: v for k, v in preset.items() if k != "note"},
        "budget": budget, "output_dir": str(output_dir), "post_run_errors": post.get("errors") or [],
        "t2_rated": post.get("rated", 0),
    }
    if post.get("post_run_only"):
        summary["post_run_only"] = True
    errors: List[str] = []
    try:
        summary["usage"], summary["inputs"] = measured_usage(root, test, run_id, bool(args.rerun),
                                                             usage_records, bool(post.get("post_run_only")))
    except Exception as e:          # noqa: BLE001 — the summary is never lost
        logger.exception(f"[summary] usage: {type(e).__name__}: {e}")
        errors.append(f"usage: {type(e).__name__}: {e}")
    for field, build in (
            ("spend_usd", lambda: round(get_tracker().persisted_total(), 4)),
            ("decisions",
             lambda: pilot_decisions(root, test, args.dry_run, data, bool(args.rerun)) if record else {}),
            ("plan", lambda: build_plan(data, root, approval=approval) if record else None)):
        try:
            summary[field] = build()
        except Exception as e:      # noqa: BLE001 — the summary is never lost
            logger.exception(f"[summary] {field}: {type(e).__name__}: {e}")
            errors.append(f"{field}: {type(e).__name__}: {e}")
    summary["run_cost_usd"] = (summary.get("usage") or {}).get("run_cost_usd")
    if record is not None and summary.get("decisions") is not None:
        try:
            summary["review"] = str(SR.write_review(review_path(root, test, bool(args.rerun)), [record],
                                                    decision_lines=summary["decisions"], title=f"pilot {test}"))
        except Exception as e:      # noqa: BLE001
            errors.append(f"review: {type(e).__name__}: {e}")
    if errors:
        summary["error"] = "; ".join(errors)
    sp = summary_path(root, test, rerun=bool(args.rerun))
    if sp.exists():
        try:
            os.replace(sp, sp.with_name(sp.stem + ".prev.json"))
        except OSError:
            pass
    # A failed run must not mark the test done: the plan reads "completed".
    _write_json(sp, summary)
    if record is not None and status == "completed" and summary.get("plan"):
        summary["plan"] = build_plan(data, root, approval=approval)     # now with this test done
        _write_json(sp, summary)
    _print_summary(summary)
    if status == "completed" and preset.get("hard_stop") and not args.rerun:
        _hard_stop(args, data, root, summary)
    return EXIT_CODES.get(status, EXIT_REFUSED)


def _hard_stop(args, data: Dict[str, Any], root: Path, summary: Dict[str, Any]) -> None:
    """After the priced run: the measured cost and the re-priced plan for both T7
    options, written to <root>/priced/share.txt for Slack; nothing else runs."""
    usage = summary.get("usage") or {}
    lines = ["=" * 72, "HARD STOP after the priced run (spec §9.2): share this on Slack before continuing.",
             "=" * 72,
             f"priced run measured cost: ${float(usage.get('run_cost_usd') or 0.0):.2f}"
             + (" (dry run: nothing is billed)" if args.dry_run else "")
             + "; by purpose: " + (", ".join(f"{k} ${v:.2f}" for k, v in sorted((usage.get('by_purpose') or {}).items()))
                                  or "-"),
             f"pilot spend so far (ledger): ${float(summary.get('spend_usd') or 0.0):.2f}"]
    for model, r in sorted((usage.get("by_actor_model") or {}).items()):
        lines.append(f"  {model:<22} ${r['cost']:.2f}, proposal failures {r['proposal_failed']}/{r['proposal_calls']}, "
                     f"forfeits {r['forfeits']}, interviews {r['interviews']} (skipped {r['interviews_skipped']}, "
                     f"empty {r.get('interviews_empty', 0)}, incomplete {r.get('interviews_incomplete', 0)}, "
                     f"cut {r.get('interviews_cut', 0)})")
    for turns in data.get("t7_turns") or [8, 12]:
        lines.append("")
        lines += plan_lines(build_plan(data, root, turns=turns))
    lines += ["", T7_NOTE]
    for k, v in (summary.get("decisions") or {}).items():
        lines.append(f"{k}: {v}")
    lines += ["", "Next, once shared and T7 is decided: python pilot.py approve --turns 8   (or --turns 12)"
              + (" --dry-run" if args.dry_run else "")]
    text = "\n".join(lines)
    say("\n" + text)
    try:
        (root / "priced").mkdir(parents=True, exist_ok=True)
        (root / "priced" / SHARE_NAME).write_text(text + "\n")
    except OSError as e:
        logger.warning(f"[hard stop] could not write {SHARE_NAME}: {e}")


def _post_run_only(args, data, root: Path, preset, test: str, run_id: str, record: Optional[Dict[str, Any]],
                   output_dir: Path, ratings_file: Path) -> int:
    """What a finished run still misses (--post-run-only, or a re-invoked test whose run
    finished): its post-run stages, then its summary (and after the priced run the
    hard stop). Once nothing is missing, the test's summary says completed (so the
    plan and `all` never run it again). A settled unusable T2 rating is done (listed
    in the summary's post-run errors); --rerate asks it again."""
    rerate = bool(getattr(args, "rerate", False))
    budget = _configure(args, data)
    if record is None:
        say(f"{test}: {run_id} has no finished {'dry' if args.dry_run else 'real'} record of this set-up; "
              "nothing collected")
        return EXIT_REFUSED
    missing = missing_post_run(record, run_id, test, ratings_file, preset, rerate)
    post: Dict[str, Any] = {"errors": [], "done": [], "rated": 0, "post_run_only": True}
    if rerate and not missing:
        say(f"{test}: {run_id} has no unusable T2 rating to ask again")
    if missing:
        say(f"{test}: {run_id} is finished (not run again); collecting: {'; '.join(missing)}")
        seats = [SimpleNamespace(key=k, model=v.get("model")) for k, v in (record.get("labs") or {}).items()]
        if args.dry_run:
            jurors = install_stubs(seats)
        else:
            jurors = _real_jurors()
            problems = model_problems([], jurors, uses_macro=False, uses_grand=bool(preset.get("grand_jury")))
            if problems:
                say("Preflight failed; nothing was called:\n  " + "\n  ".join(problems))
                return EXIT_REFUSED
        try:
            post.update(run_post_stages(record, run_id, test, output_dir, ratings_file, jurors, args.dry_run,
                                        preset, only_missing=True, rerate=rerate))
        except BudgetExceeded as e:
            say(f"HALTED on the budget guard: {e}")
            return EXIT_BUDGET
        except NetworkUnavailable as e:
            say(f"{test}: {run_id} stopped: {e}\n{NETWORK_STOP_HINT}")
            return EXIT_NETWORK
        for line in post["done"] + [f"ERROR {e}" for e in post["errors"]]:
            say(f"  {line}")
        still = missing_post_run(record, run_id, test, ratings_file, preset)
        if still:
            say(f"  still missing: {'; '.join(still)}")
            return EXIT_REFUSED
    if run_complete(root, test, run_id, record, preset, args.dry_run, bool(args.rerun)) and not (rerate and missing):
        say(f"{test}: {run_id} complete")
        return EXIT_OK
    if preset.get("disposition_rating", True):
        settled = set(settled_unusable(ratings_file, test, run_id)) & {s.key for s in model_seats(record)}
        post["errors"] += [f"T2 rating {seat}: no usable rating{SETTLED_NOTE}" for seat in sorted(settled)
                           if not any(e.startswith(f"T2 rating {seat}:") for e in post["errors"])]
    return _write_summary(args, data, root, test, run_id, preset, budget, record, None, "completed", None, post)


def rescore_grand_jury(args, data: Dict[str, Any], root: Path, test: str) -> int:
    """
    T8 (§9.2): after our hand scores disagree and the few-shot examples
    (config/jury_examples.json) are revised once, re-ask the Grand Jury on the
    latest finished T6 record (the re-run once there is one) — the seats are
    never re-run. The old verdict moves to
    final.grand_jury_previous; a second re-score is refused once one gave a usable
    verdict (a failed one exits 2 and may be retried). tools/jury_check.py
    then reads the new verdict.
    """
    if test != "T6":
        say("Refusing: --rescore-grand-jury is for T6 only (the Grand Jury's rubric runs on the T6 run, T8).")
        return EXIT_REFUSED
    run_id, _rerun = latest_t6(root, data, args.dry_run)       # T8 reads the latest T6 run
    output_dir = root / "T6"
    record = read_json(output_dir / f"{run_id}.json")
    if not is_finished(record):
        say(f"Refusing: no finished T6 record at {output_dir / (run_id + '.json')}.")
        return EXIT_REFUSED
    if is_dry(record) != bool(args.dry_run):
        say(f"Refusing: {run_id} is a {'dry' if not args.dry_run else 'real'} record; pass "
              f"{'--dry-run' if not args.dry_run else 'no --dry-run'} to re-score it.")
        return EXIT_REFUSED
    final = record["final"]
    if "grand_jury_previous" in final and grand_jury_usable(record):
        say(f"Refusing: {run_id} was already re-scored once (final.grand_jury_previous exists; T8 revises "
              "the examples once).")
        return EXIT_REFUSED
    examples = jury_mod.load_examples()
    if not examples and not args.dry_run:
        say("Refusing: config/jury_examples.json has no few-shot examples; revise them first (T8).")
        return EXIT_REFUSED
    budget = _configure(args, data)
    if not args.dry_run:
        problems = model_problems([], {"grand": GRAND_JURY_MODELS, "macro": []}, uses_macro=False)
        if problems:
            say("Preflight failed; nothing was called:\n  " + "\n  ".join(problems))
            return EXIT_REFUSED
    seats = [SimpleNamespace(key=k, model=v.get("model")) for k, v in (record.get("labs") or {}).items()]
    jurors = install_stubs(seats) if args.dry_run else _real_jurors()
    # The one re-score is used up only by a usable verdict: a re-score that
    # failed may be asked again (--post-run-only re-asks the missing verdict too); the
    # verdict before any re-score stays in grand_jury_previous.
    first = "grand_jury_previous" not in final
    if first:
        final["grand_jury_previous"] = dict(final.get("grand_jury") or {},
                                            superseded=time.strftime("%Y-%m-%d %H:%M:%S"))
        _write_json(output_dir / f"{run_id}.json", record)
    say(f"T6: re-scoring {run_id} with the revised examples ({len(examples)} example(s)); "
          f"the seats are not re-run" + (f"; guard {usd(budget)}" if budget is not None else ""))
    retry = (f"the re-score is not used up: re-ask with python pilot.py T6 --post-run-only"
             f"{' --dry-run' if args.dry_run else ''} (or --rescore-grand-jury again)")
    try:
        line = collect_grand_jury(record, jurors["grand"], run_id, output_dir)
    except BudgetExceeded as e:
        say(f"HALTED on the budget guard: {e}; no usable verdict: {retry}")
        return EXIT_BUDGET
    except NetworkUnavailable as e:
        say(f"stopped: {e}; the verdict is pending, the re-score not used up: {NETWORK_STOP_HINT}")
        return EXIT_NETWORK
    except Exception as e:      # noqa: BLE001
        if first:               # nothing was re-scored: the record is as it was
            final.pop("grand_jury_previous", None)
            _write_json(output_dir / f"{run_id}.json", record)
        say(f"Grand Jury failed: {type(e).__name__}: {e}; the re-score is not used up "
              "(the verdict is unchanged): run --rescore-grand-jury again")
        return EXIT_REFUSED
    if not grand_jury_usable(record):
        say(f"  {line}; {retry}")
        return EXIT_REFUSED
    say(f"  {line}; compare with our hand scores: python tools/jury_check.py {output_dir / (run_id + '.json')}")
    return EXIT_OK


def _failure(run_id: str, e: BaseException, output_dir: Path) -> Dict[str, Any]:
    path = getattr(e, "record_path", None) or output_dir / f"{run_id}.partial.json"
    return {"run_id": run_id, "error": f"{type(e).__name__}: {e}",
            "record_path": str(path) if Path(path).exists() else None}


def _fmt_rate(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:.2f}"


def _print_summary(summary: Dict[str, Any]) -> None:
    say("\n" + "=" * 72)
    say(f"PILOT {summary['test']}  —  {'DRY-RUN' if summary['dry_run'] else 'REAL'}  ({summary['run_id']})")
    say("=" * 72)
    usage = summary.get("usage") or {}
    say(f"status         : {summary['status']}"
          + (f" — {summary['aborted']['error']}" if summary.get("aborted") else ""))
    if summary.get("aborted") and summary["aborted"].get("record_path"):
        say(f"partial record : {summary['aborted']['record_path']}")
    say(f"run cost       : ${float(usage.get('run_cost_usd') or 0.0):.2f}; pilot spend ${float(summary.get('spend_usd') or 0.0):.2f}"
          + (f" (guard ${summary['budget']})" if summary.get("budget") else ""))
    for err in summary.get("post_run_errors") or []:
        say(f"post-run error : {err}")
    if summary.get("error"):
        say(f"summary error  : {summary['error']}")
    rows = usage.get("by_actor_model") or {}
    if rows:
        say(f"\n{'actor model':<24}{'turns':>6}{'prop.fail':>10}{'msg.fail':>9}{'forfeit':>8}{'maxtok':>7}"
              f"{'refuse':>7}{'iv':>4}{'iv.skip':>8}{'iv.empty':>9}{'iv.inc':>7}{'iv.cut':>7}{'cost $':>8}  served model")
        for model, r in sorted(rows.items()):
            served = ", ".join(f"{m}{' (MISMATCH)' if m in r['served_mismatch'] else ''}"
                               for m in sorted(r["served_models"])) or "-"
            say(f"{model:<24}{r['seat_turns']:>6}{_fmt_rate(r['proposal_failure_rate']):>10}"
                  f"{_fmt_rate(r['message_failure_rate']):>9}{r['forfeits']:>8}{r['stop_max_tokens']:>7}"
                  f"{r['stop_refusal']:>7}{r['interviews']:>4}{r['interviews_skipped']:>8}"
                  f"{r.get('interviews_empty', 0):>9}{r.get('interviews_incomplete', 0):>7}"
                  f"{r.get('interviews_cut', 0):>7}{r['cost']:>8.2f}  {served}")
    for r in sorted((usage.get("jurors") or {}).values(), key=lambda r: (r["purpose"], r["model"])):
        say(f"juror {r['model']:<24}{r['purpose']:<18} calls {r['calls']}, ${r['cost']:.2f}"
              + (f"; served {', '.join(r['served_models'])}" if r.get("served_models") else ""))
    mism = [m for m, r in rows.items() if r.get("served_mismatch")]
    if mism:
        say(f"WARNING: the provider served a different model than configured for {', '.join(mism)}")
    if summary.get("plan"):
        say()
        say("\n".join(plan_lines(summary["plan"])))
    for k, v in (summary.get("decisions") or {}).items():
        say(f"{k}: {v}")
    if summary.get("review"):
        say(f"review file: {summary['review']}")


if __name__ == "__main__":
    from core.cli import run_main
    raise SystemExit(run_main(main))
