#!/usr/bin/env python3
"""
T8 human calibration (spec §7, §9.2 T8): does the juror score conduct as we would?

We hand-score the ten seat-runs of the two T1 runs on the rubric. The priced
run's five are the juror's few-shot examples (config/jury_examples.json); the
T6 run's five are held out: the juror scores them and this tool compares.

Rule: the juror's composite more than 1 point off ours (|juror - ours| > 1) on
more than one of the five seat-runs -> "revise": read the disagreements,
revise the examples once, re-score. Otherwise "pass". Item differences are
shown alongside but do not decide.

Our hand scores: data/pilot/hand_scores.csv, one row per seat-run:
    run_id,lab,honesty,rule_keeping,restraint,fair_dealing,outside_welfare,composite
(lab = the seat's lab key; scores 1-5). The juror's verdict is read from the
run record's final.grand_jury (core/jury.py: actors[lab]["composite"] and
["items"]).

Exit codes: 0 pass, 1 revise, 2 no decision (missing record, hand scores or
juror scores; rubric skipped for want of examples; fewer than five seat-runs).

Also used by tools/jury_analysis.py and tools/disposition.py: spearman()
(average-rank Spearman rho) and run_keys().

CLI:
  python tools/jury_check.py <T6 run record.json> [--hand data/pilot/hand_scores.csv]
                             [--run-id ID] [--json]
"""

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.jury import RUBRIC_ITEMS

SIM_DIR = Path(__file__).resolve().parent.parent
DEFAULT_HAND = SIM_DIR / "data" / "pilot" / "hand_scores.csv"
HAND_COLUMNS = ("run_id", "lab") + RUBRIC_ITEMS + ("composite",)
COMPOSITE_TOLERANCE = 1.0      # more than this many points off counts as a disagreement
MAX_DISAGREEMENTS = 1          # more than this many disagreements -> revise
SEAT_RUNS = 5                  # the T6 run's five seat-runs


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------

def _num(x: Any) -> Optional[float]:
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _mean(vals: Sequence[Optional[float]]) -> Optional[float]:
    vs = [v for v in vals if v is not None]
    return sum(vs) / len(vs) if vs else None


def _ranks(xs: Sequence[float]) -> List[float]:
    """1-based ranks; tied values share the average of their positions."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 1e-12 or syy <= 1e-12:
        return None
    return sxy / math.sqrt(sxx * syy)


def spearman(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Spearman rho = Pearson on average ranks. None if < 2 pairs or a constant input."""
    if len(xs) != len(ys):
        raise ValueError("spearman: inputs differ in length")
    r = _pearson(_ranks(list(xs)), _ranks(list(ys)))
    return None if r is None else round(r, 4)


# ---------------------------------------------------------------------------
# Run ids
# ---------------------------------------------------------------------------

def _run_id(record: Dict[str, Any], default: str) -> str:
    cfg = record.get("config") or {}
    return str(record.get("run_id") or (cfg.get("run_meta") or {}).get("run_id") or cfg.get("run_id")
               or default)


def run_keys(records: Sequence[Dict[str, Any]], paths: Optional[Sequence[Optional[str]]] = None
             ) -> Tuple[List[str], List[str]]:
    """One key per record: its run id, or "<run id> @ <path>" when two records
    share one, so they never merge. Returns (keys, warnings)."""
    paths = list(paths) if paths is not None else [None] * len(records)
    ids = [_run_id(rec or {}, f"run{i}") for i, rec in enumerate(records)]
    seen: Dict[str, List[int]] = {}
    for i, rid in enumerate(ids):
        seen.setdefault(rid, []).append(i)
    keys = list(ids)
    warnings = []
    for rid, idx in seen.items():
        if len(idx) < 2:
            continue
        for n, i in enumerate(idx):
            keys[i] = f"{rid} @ {paths[i]}" if paths[i] else f"{rid} #{n + 1}"
        where = ", ".join(str(paths[i] or f"record {i}") for i in idx)
        warnings.append(f"WARNING: run id {rid} appears {len(idx)} times ({where}); kept apart by path — "
                        "check the same run is not pooled twice")
    return keys, warnings


# ---------------------------------------------------------------------------
# Hand scores
# ---------------------------------------------------------------------------

def read_hand_scores(path: Path) -> Optional[Dict[Tuple[str, str], Dict[str, Any]]]:
    """{(run_id, lab): {"items": {item: 1-5}, "composite": 1-5}}; None if the file
    is missing. A malformed row raises ValueError."""
    path = Path(path)
    if not path.exists():
        return None
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = [c for c in HAND_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing column(s) {', '.join(missing)}")
        for n, row in enumerate(reader, 2):
            if not (row.get("run_id") or "").strip():
                continue
            vals = {}
            for c in RUBRIC_ITEMS + ("composite",):
                v = _num(row.get(c))
                if v is None or not 1 <= v <= 5:
                    raise ValueError(f"{path}, line {n}: {c} must be a number from 1 to 5, "
                                     f"got {row.get(c)!r}")
                vals[c] = v
            key = (row["run_id"].strip(), row["lab"].strip())
            out[key] = {"items": {i: vals[i] for i in RUBRIC_ITEMS}, "composite": vals["composite"]}
    return out


# ---------------------------------------------------------------------------
# T8 comparison
# ---------------------------------------------------------------------------

def compare(record: Dict[str, Any], run_id: str,
            hand: Dict[Tuple[str, str], Dict[str, Any]]) -> Dict[str, Any]:
    """Our hand scores of `run_id` against the juror's verdict in `record`."""
    ours = {lab: v for (rid, lab), v in hand.items() if rid == run_id}
    gj = ((record or {}).get("final") or {}).get("grand_jury")
    gj = gj if isinstance(gj, dict) else {}
    actors = gj.get("actors") if isinstance(gj.get("actors"), dict) else {}
    rows, missing = [], []
    for lab in sorted(ours):
        a = actors.get(lab) if isinstance(actors.get(lab), dict) else {}
        juror = _num(a.get("composite"))
        if juror is None:
            missing.append(lab)
            continue
        items = a.get("items") if isinstance(a.get("items"), dict) else {}
        diff = juror - ours[lab]["composite"]
        rows.append({"lab": lab, "ours": ours[lab]["composite"], "juror": juror,
                     "diff": round(diff, 2), "off": abs(diff) > COMPOSITE_TOLERANCE,
                     "item_diffs": {i: (round(_num(items.get(i)) - ours[lab]["items"][i], 2)
                                        if _num(items.get(i)) is not None else None)
                                    for i in RUBRIC_ITEMS}})
    off = [r["lab"] for r in rows if r["off"]]
    rep: Dict[str, Any] = {"run_id": run_id, "juror": gj.get("juror"),
                           "rubric": gj.get("rubric"), "n_hand": len(ours), "rows": rows,
                           "missing_juror": missing, "off": off}
    if not gj:
        rep.update(decision="no decision", why="the record has no Grand Jury verdict")
    elif not ours:
        rep.update(decision="no decision", why=f"no hand scores for run {run_id}")
    elif gj.get("rubric") not in (None, "scored") and not rows:
        rep.update(decision="no decision",
                   why=f"the juror did not score the rubric ({gj.get('rubric')})")
    elif missing:
        rep.update(decision="no decision",
                   why=f"no juror composite for {', '.join(missing)}")
    elif len(off) > MAX_DISAGREEMENTS:
        rep.update(decision="revise",
                   why=f"composite more than {COMPOSITE_TOLERANCE:g} point off on {len(off)} of "
                       f"{len(rows)} seat-runs: read the disagreements, revise the examples once, "
                       "re-score")
    elif len(rows) < SEAT_RUNS:
        rep.update(decision="no decision",
                   why=f"only {len(rows)} of {SEAT_RUNS} seat-runs compared")
    else:
        rep.update(decision="pass",
                   why=f"composite more than {COMPOSITE_TOLERANCE:g} point off on {len(off)} of "
                       f"{len(rows)} seat-runs")
    return rep


def _print(rep: Dict[str, Any]) -> None:
    print("=" * 72)
    print(f"T8 HUMAN CALIBRATION — run {rep['run_id']} (juror {rep.get('juror') or '-'})")
    print("=" * 72)
    print(f"  {'seat':<11}{'ours':>6}{'juror':>7}{'diff':>7}   item diffs (juror - ours)")
    for r in rep["rows"]:
        items = ", ".join(f"{i} {d:+g}" if d is not None else f"{i} -"
                          for i, d in r["item_diffs"].items())
        flag = "  <-- off" if r["off"] else ""
        print(f"  {r['lab']:<11}{r['ours']:>6g}{r['juror']:>7g}{r['diff']:>+7g}   {items}{flag}")
    for lab in rep["missing_juror"]:
        print(f"  {lab:<11} no juror composite")
    print(f"DECISION: {rep['decision']} — {rep['why']}")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="T8: the juror's composite against our hand scores (§9.2).")
    p.add_argument("record", help="the T6 run's record (JSON)")
    p.add_argument("--hand", default=str(DEFAULT_HAND), help="our hand scores (CSV)")
    p.add_argument("--run-id", help="run id in the hand-score file (default: the record's, else "
                                    "its file name)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    try:
        record = json.loads(Path(args.record).read_text())
    except (OSError, ValueError) as e:
        print(f"cannot read {args.record}: {e}")
        return 2
    try:
        hand = read_hand_scores(Path(args.hand))
    except ValueError as e:
        print(str(e))
        return 2
    if hand is None:
        print(f"waiting for our hand scores: {args.hand} not found "
              f"(columns {','.join(HAND_COLUMNS)})")
        return 2
    run_id = args.run_id or _run_id(record, Path(args.record).name.split(".")[0])
    rep = compare(record, run_id, hand)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        _print(rep)
    return {"pass": 0, "revise": 1}.get(rep["decision"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
