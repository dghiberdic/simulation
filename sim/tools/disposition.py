#!/usr/bin/env python3
"""
Pilot T2: the disposition each seat plays in the main runs (spec §2, §9.2 T2;
code contract "Juries").

T2 adds no runs. After every pilot run the juror (core.jury.DispositionJury,
kimi-k3) rates each model seat-run's disposition once, blind, from its
redacted record; pilot.py pools those ratings, with each seat's chosen value,
in data/pilot/disposition_ratings.json:

    {"ratings": {lab: [{"run_id", "test", "juror", "disposition", "dry_run",
                        ["scripted"]}, ...]},
     "chosen":  {lab: [{"run_id", "test", "value", "dry_run"}, ...]},
     "families": {lab: family}}

Before seeing the juror's ratings, we blind-rate the ten seat-runs of the two
T1 runs (priced and the latest T6: after the T6 re-run, T6-run02; our ratings
of a replaced T6 run are left out of the agreement) from the same redacted
transcripts, in
data/pilot/human_ratings.csv with columns run_id,letter,disposition (the seat
letter of the redacted transcript; a `lab` column with the lab key is accepted
instead). Letters are mapped to labs through the letter each pooled rating
carries, so the raters never need the key.

Rule
----
1. Agreement: Spearman rho between our ratings and the juror's over the
   seat-runs we rated (the ten T1 seat-runs) must be >= 0.5 (a floor, not a
   significance test; computed on at least 3 pairs; fewer than ten matched
   pairs is reported).
2. Precision, per seat: the juror's ratings of that seat over every pilot
   seat-run (T5's scripted seats excluded) must number >= 3 with a standard
   error (sample SD / sqrt(n)) <= 5.
Both are compared unrounded (an SE of 5.004 fails; exactly 5 passes); only the
report rounds. If both hold, the seat plays its MEAN JUDGED value rounded to 10
(halves up: 45 -> 50) ("judged");
otherwise its MEDIAN CHOSEN value rounded to 10 ("chosen"). A seat with no
chosen value then plays DEFAULT_DISPOSITION ("default"), flagged; the CLI
exits 3 and writes nothing unless --force. The gap judged - chosen is
reported per seat.

If the human-ratings CSV does not exist, or holds no rating yet (the exported
blank template), the tool says "waiting for human ratings", shows none of the
juror's ratings (so ours stay blind) and writes nothing, exit 4.

Ratings from dry runs or stub jurors are refused unless --allow-stub, and
--write together with --allow-stub is refused (exit 2).

CLI:
  python tools/disposition.py [data/pilot/disposition_ratings.json]
         [--human data/pilot/human_ratings.csv] [--write] [--output config/dispositions.json]
         [--allow-stub] [--force]
"""

import csv
import json
import logging
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import CONFIG_DIR, PILOT_DIR                            # noqa: E402
from core.exits import EXIT_FAILED, EXIT_OK, EXIT_REFUSED, EXIT_WAITING     # noqa: E402
from tools.jury_check import letters_to_labs, missing_columns, seat_ident, spearman

logger = logging.getLogger(__name__)

DEFAULT_RATINGS = PILOT_DIR / "disposition_ratings.json"
DEFAULT_HUMAN = PILOT_DIR / "human_ratings.csv"
HUMAN_COLUMNS = ("run_id", "letter", "disposition")     # or "lab" for "letter"

RHO_MIN = 0.5                  # Spearman floor between our ratings and the juror's (T2)
MIN_PAIRS = 3                  # fewer matched seat-runs: no rho
T1_SEAT_RUNS = 10              # the two T1 runs' seat-runs
SE_MAX = 5.0                   # a seat's judged mean must be this precise
SE_TOL = 1e-9                  # compared unrounded: an exact 5 passes, 5.004 does not
MIN_SEAT_RUNS = 3              # over at least this many rated seat-runs
SCRIPTED_SEATS = {"T5": ("xai",)}   # T5's scripted intruder never counts
DEFAULT_DISPOSITION = 50       # neutral fallback when a seat has nothing to go on
WAITING = "waiting for human ratings"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def round_to_10(value: Optional[float]) -> Optional[int]:
    """Nearest multiple of 10, as an int (None passes through). Halves round up
    (45 -> 50; the spec's "rounded to 10" is silent on halves, decided here), with a
    1e-9 tolerance so a mean that is a half up to float error counts as the half."""
    if value is None:
        return None
    return int(math.floor(value / 10.0 + 0.5 + 1e-9)) * 10


def _value(x: Any) -> Optional[float]:
    """A number, or {"value": n} / {"disposition": n} as pilot.py pools it."""
    if isinstance(x, dict):
        x = x.get("value", x.get("disposition"))
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _median_chosen(chosen: Any) -> Optional[float]:
    """A seat's chosen value: a scalar, or the median of a list of choices."""
    if chosen is None:
        return None
    if isinstance(chosen, (list, tuple)):
        vals = [v for v in (_value(x) for x in chosen) if v is not None]
        return statistics.median(vals) if vals else None
    return _value(chosen)


def is_stub_rating(r: Dict[str, Any]) -> bool:
    return bool(r.get("dry_run")) or str(r.get("juror", "")).startswith("stub:")


def stub_count(payload: Dict[str, Any]) -> int:
    """Ratings and chosen values in the pool that come from dry runs or stub jurors."""
    n = sum(1 for rows in (payload.get("ratings") or {}).values() for r in rows or []
            if isinstance(r, dict) and is_stub_rating(r))
    n += sum(1 for rows in (payload.get("chosen") or {}).values()
             for c in (rows if isinstance(rows, list) else [rows])
             if isinstance(c, dict) and c.get("dry_run"))
    return n


def is_scripted(seat: str, r: Dict[str, Any]) -> bool:
    return bool(r.get("scripted")) or seat in SCRIPTED_SEATS.get(str(r.get("test")), ())


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def read_human_ratings(path: Path) -> Optional[Dict[Tuple[str, str], float]]:
    """{(run_id, seat): 0-100}, seat = the lab key or "Seat X" for a row keyed by
    letter (resolve_t2 maps it); None if the file is missing. Bad rows raise
    ValueError."""
    path = Path(path)
    if not path.exists():
        return None
    out: Dict[Tuple[str, str], float] = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = missing_columns(reader.fieldnames, HUMAN_COLUMNS)
        if missing:
            raise ValueError(f"{path}: missing column(s) {', '.join(missing)}")
        for n, row in enumerate(reader, 2):
            if not (row.get("run_id") or "").strip() or not (row.get("disposition") or "").strip():
                continue                       # a blank line, or a template row not yet rated
            v = _value(row.get("disposition"))
            if v is None or not 0 <= v <= 100:
                raise ValueError(f"{path}, line {n}: disposition must be a number from 0 to 100, "
                                 f"got {row.get('disposition')!r}")
            out[(row["run_id"].strip(), seat_ident(row, path, n))] = v
    return out


def pooled_letters(payload: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """{run_id: {lab: letter}} from the letters the pooled juror ratings carry."""
    out: Dict[str, Dict[str, str]] = {}
    for field in ("ratings", "failures", "chosen"):
        for seat, rows in (payload.get(field) or {}).items():
            for r in rows or []:
                if isinstance(r, dict) and r.get("letter") and r.get("run_id") is not None:
                    out.setdefault(str(r["run_id"]), {})[seat] = str(r["letter"])
    return out


def juror_ratings(ratings: Dict[str, List[Dict[str, Any]]]
                  ) -> Tuple[Dict[Tuple[str, str], float], List[str]]:
    """{(run_id, lab): the juror's rating} over model seat-runs (scripted seats
    out); a seat-run rated twice (a re-ask) keeps the mean, noted."""
    seen: Dict[Tuple[str, str], List[float]] = {}
    for seat, rows in (ratings or {}).items():
        for r in rows or []:
            if not isinstance(r, dict) or is_scripted(seat, r):
                continue
            v = _value(r)
            if v is None:
                continue
            seen.setdefault((str(r.get("run_id")), seat), []).append(v)
    notes = [f"{rid}/{seat}: {len(vs)} ratings, mean used" for (rid, seat), vs in seen.items()
             if len(vs) > 1]
    return {k: statistics.fmean(vs) for k, vs in seen.items()}, notes


# ---------------------------------------------------------------------------
# The T2 rule
# ---------------------------------------------------------------------------

def latest_t6_only(human: Dict[Tuple[str, str], float], payload: Dict[str, Any]
                   ) -> Tuple[Dict[Tuple[str, str], float], List[str]]:
    """Our ratings without those of a T6 run the re-run replaced (§9.2 T2: we rate the
    priced run and the latest T6 run), and the replaced run ids. The juror's ratings
    of every pilot seat-run still count for each seat's mean and SE."""
    t6 = sorted({str(r.get("run_id")) for rows in (payload.get("ratings") or {}).values()
                 for r in rows or [] if isinstance(r, dict) and r.get("test") == "T6"})
    superseded = set(t6[:-1])
    kept = {k: v for k, v in human.items() if k[0] not in superseded}
    return kept, sorted({k[0] for k in human if k[0] in superseded})


def agreement(human: Dict[Tuple[str, str], float],
              judged: Dict[Tuple[str, str], float]) -> Dict[str, Any]:
    """Spearman rho between our ratings and the juror's on the seat-runs we rated."""
    keys = sorted(k for k in human if k in judged)
    missing = sorted(k for k in human if k not in judged)
    rho = spearman([human[k] for k in keys], [judged[k] for k in keys]) \
        if len(keys) >= MIN_PAIRS else None
    return {"n": len(keys), "rho": rho, "passes": rho is not None and rho >= RHO_MIN,
            "missing_juror": [f"{r}/{s}" for r, s in missing],
            "pairs": [{"run_id": r, "lab": s, "ours": human[(r, s)], "juror": judged[(r, s)]}
                      for r, s in keys]}


def seat_stats(judged: Dict[Tuple[str, str], float]) -> Dict[str, Dict[str, Any]]:
    """{seat: {"n", "mean", "se"}}, unrounded: the rule compares these, and only the
    report rounds them."""
    by_seat: Dict[str, List[float]] = {}
    for (_rid, seat), v in judged.items():
        by_seat.setdefault(seat, []).append(v)
    out = {}
    for seat, vs in by_seat.items():
        n = len(vs)
        se = statistics.stdev(vs) / math.sqrt(n) if n >= 2 else None
        out[seat] = {"n": n, "mean": statistics.fmean(vs), "se": se}
    return out


def _r2(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 2)


def resolve_t2(payload: Dict[str, Any], human: Optional[Dict[Tuple[str, str], float]]
               ) -> Tuple[Dict[str, int], Dict[str, Any]]:
    """(dispositions {lab: int}, report). With no human rating (None, or a template
    with nothing rated): ({}, {"status": WAITING})."""
    if not human:
        return {}, {"status": WAITING}
    human = letters_to_labs(human, pooled_letters(payload))
    judged, notes = juror_ratings(payload.get("ratings") or {})
    human, superseded = latest_t6_only(human, payload)
    if superseded:
        notes.append(f"our ratings of {', '.join(superseded)} left out of the agreement: the T6 re-run "
                     "replaces it (the ten T1 seat-runs are the priced run's and the latest T6 run's)")
    agree = agreement(human, judged)
    if agree["n"] < T1_SEAT_RUNS:
        notes.append(f"agreement over {agree['n']} seat-run(s), not the {T1_SEAT_RUNS} T1 seat-runs"
                     + (f"; no juror rating for {', '.join(agree['missing_juror'])}"
                        if agree["missing_juror"] else ""))
    stats = seat_stats(judged)
    chosen_pool = payload.get("chosen") or {}
    seats = sorted(set(payload.get("families") or {}) | set(stats) | set(chosen_pool))
    dispositions: Dict[str, int] = {}
    report_seats: Dict[str, Any] = {}
    defaulted = []
    for seat in seats:
        st = stats.get(seat, {"n": 0, "mean": None, "se": None})
        chosen = _median_chosen(chosen_pool.get(seat))
        precise = st["n"] >= MIN_SEAT_RUNS and st["se"] is not None and st["se"] <= SE_MAX + SE_TOL
        if agree["passes"] and precise:
            value, source = round_to_10(st["mean"]), "judged"
            why = f"rho {agree['rho']} >= {RHO_MIN}, SE {_r2(st['se'])} <= {SE_MAX} over {st['n']}"
        else:
            if not agree["passes"]:
                why = (f"rho {agree['rho']} < {RHO_MIN}" if agree["rho"] is not None
                       else f"no rho (fewer than {MIN_PAIRS} matched seat-runs, or no spread)")
            elif st["n"] < MIN_SEAT_RUNS:
                why = f"{st['n']} rated seat-run(s) < {MIN_SEAT_RUNS}"
            else:
                why = f"SE {st['se']:.4f} > {SE_MAX}"
            if chosen is not None:
                value, source = round_to_10(chosen), "chosen"
            else:
                value, source = DEFAULT_DISPOSITION, "default"
                defaulted.append(seat)
                why += "; no chosen value"
        dispositions[seat] = int(value)
        report_seats[seat] = {
            "value": int(value), "source": source, "why": why,
            "judged_mean": _r2(st["mean"]), "judged_se": _r2(st["se"]), "n_rated": st["n"],
            "chosen_median": chosen,
            "gap_judged_minus_chosen": (round(st["mean"] - chosen, 2)
                                        if st["mean"] is not None and chosen is not None else None)}
    return dispositions, {"status": "resolved", "agreement": agree, "seats": report_seats,
                          "defaulted": defaulted, "notes": notes}


def _print_report(report: Dict[str, Any]) -> None:
    print("=" * 72)
    print("T2 DISPOSITIONS (§2, §9.2)")
    print("=" * 72)
    if report.get("status") == WAITING:
        print(f"{WAITING}: the juror's ratings stay hidden until ours are in.")
        return
    a = report["agreement"]
    print(f"Agreement with our blind ratings: Spearman rho {a['rho']} over {a['n']} seat-run(s) "
          f"-> {'passes' if a['passes'] else 'fails'} (floor {RHO_MIN})")
    fmt = lambda x: "-" if x is None else f"{x:g}"
    print(f"  {'seat':<11}{'plays':>6}  {'source':<8}{'judged':>8}{'SE':>7}{'n':>4}{'chosen':>8}"
          f"{'gap':>7}  why")
    for seat, s in report["seats"].items():
        print(f"  {seat:<11}{s['value']:>6}  {s['source']:<8}{fmt(s['judged_mean']):>8}"
              f"{fmt(s['judged_se']):>7}{s['n_rated']:>4}{fmt(s['chosen_median']):>8}"
              f"{fmt(s['gap_judged_minus_chosen']):>7}  {s['why']}")
    for seat in report["defaulted"]:
        print(f"WARNING: {seat} has neither a usable judged value nor a chosen value; "
              f"defaults to {DEFAULT_DISPOSITION}")
    for n in report["notes"]:
        print(f"note: {n}")


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description="T2: main-run dispositions from the pilot (§2, §9.2).")
    parser.add_argument("input", nargs="?", default=str(DEFAULT_RATINGS),
                        help="pooled juror ratings (default data/pilot/disposition_ratings.json)")
    parser.add_argument("--human", default=str(DEFAULT_HUMAN),
                        help="our blind ratings, CSV run_id,letter,disposition (or run_id,lab,...) "
                             "(default data/pilot/human_ratings.csv)")
    parser.add_argument("--write", action="store_true", help="write the result to --output")
    parser.add_argument("--output", default=str(CONFIG_DIR / "dispositions.json"))
    parser.add_argument("--allow-stub", action="store_true",
                        help="accept ratings from dry runs / stub jurors (testing the tool only)")
    parser.add_argument("--force", action="store_true",
                        help=f"accept seats that default to {DEFAULT_DISPOSITION}")
    args = parser.parse_args(argv)
    if args.write and args.allow_stub:
        print("Refusing --write together with --allow-stub: stub or dry-run ratings must never set "
              "main-run dispositions.")
        return EXIT_REFUSED
    try:
        human = read_human_ratings(Path(args.human))
    except ValueError as e:
        print(str(e))
        return EXIT_REFUSED
    if not human:
        where = "not found" if human is None else "has no rating yet"
        print(f"{WAITING}: {args.human} {where} (columns {','.join(HUMAN_COLUMNS)}). Rate the "
              "ten T1 seat-runs blind first; the juror's ratings are not shown until then.")
        return EXIT_WAITING
    try:
        payload = json.loads(Path(args.input).read_text())
    except (OSError, ValueError) as e:
        print(f"cannot read {args.input}: {e}")
        return EXIT_REFUSED
    stubs = stub_count(payload)
    if stubs and not args.allow_stub:
        print(f"Refusing: {stubs} rating(s)/choice(s) in {args.input} come from dry runs or stub "
              "jurors. Use the real pooled file, or --allow-stub to test the tool.")
        return EXIT_REFUSED
    dispositions, report = resolve_t2(payload, human)
    _print_report(report)
    if report["defaulted"] and not args.force:
        print(f"Not resolved: {', '.join(report['defaulted'])} defaulted to {DEFAULT_DISPOSITION}"
              + (" — nothing written" if args.write else "") + ". Pass --force to accept.")
        return EXIT_FAILED
    if args.write:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(dispositions, indent=2) + "\n")
        logger.info(f"[save] wrote {out_path}")
    else:
        print("(not written; pass --write to save config/dispositions.json)")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
