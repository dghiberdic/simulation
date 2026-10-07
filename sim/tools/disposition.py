#!/usr/bin/env python3
"""
Pilot T2 disposition aggregation (spec §2, §9.2 T2; G6).

T2 has no runs of its own: after every pilot run a blind disposition jury
rates each model seat's directive-precedence (0..100) from its redacted record,
and pilot.py pools the ratings — with each seat's chosen value — across tests
in data/pilot/disposition_ratings.json. This tool turns the pool into the
per-seat value the main run will play (config/dispositions.json).

Guards
------
Ratings from dry runs or stub jurors are refused unless --allow-stub (they
would pin every seat at the stub's 50). Nothing is written unless --write;
by default the tool prints the resolution.

Juror fixed effect
------------------
Jurors differ in harshness, and own-family exclusion (§7) means each seat is
rated by a different subset of jurors, so a harsh juror would pull down only
the seats it rates. Each rating is therefore juror-centred before averaging:

    adjusted_x = x − offset_j,   offset_j = juror j's mean deviation from the
                                 (adjusted) means of the seats it rated

fitted by alternating means (a two-way additive seat + juror model) and
centred so the grand mean is unchanged. Taking deviations from seat means, not
from the grand mean, keeps a juror who merely rated the high seats from being
read as lenient. Both raw and adjusted values are reported; the decision uses the adjusted
values (--raw uses the raw ones).

Reliability
-----------
We summarise juror agreement with a single intraclass correlation computed by
hand. The coefficient is ICC(1,1) — a ONE-WAY random-effects model, single
rater (Shrout & Fleiss 1979). One-way is the right model here: own-family
exclusion (§7) means a different subset of jurors rates each seat, so the
columns are not a fixed panel and a two-way (rater-as-factor) model does not
apply. Each seat is a group of judged values; group sizes may differ.

With groups i=1..n, group i holding k_i ratings and N ratings in all:

    MS_B = (1/(n−1))·Σ k_i·(mean_i − grand)²          (between seats)
    MS_W = (1/(N−n))·Σ_i Σ_j (x_ij − mean_i)²          (within a seat, across jurors)
    k0   = (N − Σ k_i² / N) / (n−1)                    (mean group size)

    ICC(1,1) = (MS_B − MS_W) / (MS_B + (k0−1)·MS_W)

It is high only when jurors agree within a seat *and* seats genuinely differ;
pure noise, or no between-seat variance, drives it toward zero.

Decision rule (§2)
------------------
Each seat plays at its MEAN judged value rounded to the nearest 10. But if the
jury is unreliable (ICC < 0.4) or a seat's judged values span more than 40
points, that seat instead plays at its MEDIAN CHOSEN value (also rounded to 10)
— the panel is not trusted to pin that seat down.

The span (P19) is taken over the seat's PER-RUN means: in each pilot run the
seat's (juror-centred) ratings are averaged across its jurors, and the span is
max − min of those run means. A single juror's outlier is thus diluted by the
other jurors of that run instead of deciding the seat on its own. The robust
spread P10–P90 of the run means and the span of the individual ratings are
reported alongside; the decision uses the per-run span. Ratings without a run
id (older pools) each count as their own run.

Own-family jurors are excluded per seat (§7). The caller is expected to pass
already-masked ratings, but we re-filter defensively on juror_family.

CLI:
  python tools/disposition.py [data/pilot/disposition_ratings.json] [--write] [--raw] [--allow-stub]
"""

import json
import logging
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.jury import juror_family

logger = logging.getLogger(__name__)

SIM_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = SIM_DIR / "config"
DEFAULT_RATINGS = SIM_DIR / "data" / "pilot" / "disposition_ratings.json"

ICC_RELIABLE_THRESHOLD = 0.4   # below this the jury is "unreliable" (§2)
SPAN_LIMIT = 40                # a per-seat judged span beyond this is untrusted (§2)
DEFAULT_DISPOSITION = 50       # neutral fallback when a seat has nothing to go on


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------

def round_to_10(value: Optional[float]) -> Optional[int]:
    """Nearest multiple of 10, as an int, halves rounding up (None passes through)."""
    if value is None:
        return None
    return int(math.floor(value / 10.0 + 0.5)) * 10


def _value(x: Any) -> Optional[float]:
    """A chosen entry: a number, or {"value": n, ...} as pilot.py pools it."""
    if isinstance(x, dict):
        x = x.get("value")
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


# ---------------------------------------------------------------------------
# ICC(1,1)
# ---------------------------------------------------------------------------

def compute_icc(matrix: List[List[float]]) -> float:
    """
    One-way ICC(1,1) over a list of per-seat rating groups (each row is one
    seat's judged values; rows may have different lengths). Returns 0.0 for a
    degenerate input — fewer than 2 seats, any seat with no ratings, no
    within-seat replication, or no between-seat variance. Clamped to [-1, 1].
    """
    groups = [[float(x) for x in row] for row in (matrix or []) if row]
    n = len(groups)
    if n < 2 or any(len(g) == 0 for g in groups):
        return 0.0
    sizes = [len(g) for g in groups]
    total_n = sum(sizes)
    if total_n - n <= 0:                     # no within-seat replication anywhere
        return 0.0

    grand = sum(sum(g) for g in groups) / total_n
    means = [sum(g) / len(g) for g in groups]

    ss_between = sum(len(g) * (m - grand) ** 2 for g, m in zip(groups, means))
    ss_within = sum((x - m) ** 2 for g, m in zip(groups, means) for x in g)
    if ss_between <= 1e-12:                  # seats do not differ → not informative
        return 0.0

    ms_between = ss_between / (n - 1)
    ms_within = ss_within / (total_n - n)
    k0 = (total_n - sum(s * s for s in sizes) / total_n) / (n - 1)

    denom = ms_between + (k0 - 1) * ms_within
    if denom <= 0:
        return 0.0
    icc = (ms_between - ms_within) / denom
    return max(-1.0, min(1.0, round(icc, 4)))


# ---------------------------------------------------------------------------
# Filtering and the juror fixed effect
# ---------------------------------------------------------------------------

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


def _kept_rows(ratings: Dict[str, List[Dict[str, Any]]], families: Dict[str, str]
               ) -> Dict[str, List[Tuple[str, float, str]]]:
    """Drop own-family jurors and ratings with no value; seat -> [(juror, value, run key)]."""
    out: Dict[str, List[Tuple[str, float, str]]] = {}
    for seat, rlist in (ratings or {}).items():
        fam = families.get(seat)
        kept: List[Tuple[str, float, str]] = []
        for i, r in enumerate(rlist or []):
            if not isinstance(r, dict):
                continue
            juror = r.get("juror", "")
            jf = r.get("family") or juror_family(juror)
            if fam is not None and jf == fam:
                continue   # own-family exclusion (§7)
            val = _value(r.get("disposition"))
            if val is not None and 0 <= val <= 100:
                # A rating without a run id is its own "run" (older pools).
                run = f"{r.get('test', '')}/{r['run_id']}" if r.get("run_id") else f"#{i}"
                kept.append((juror, val, run))
        out[seat] = kept
    return out


def _mask_ratings(ratings: Dict[str, List[Dict[str, Any]]], families: Dict[str, str]
                  ) -> Dict[str, List[Tuple[str, float]]]:
    """Drop own-family jurors and ratings with no value; return seat -> [(juror, value)]."""
    return {s: [(j, v) for j, v, _r in rows] for s, rows in _kept_rows(ratings, families).items()}


def _run_ids(ratings: Dict[str, List[Dict[str, Any]]], families: Dict[str, str]) -> Dict[str, List[str]]:
    """seat -> the run key of each kept rating, aligned with _mask_ratings."""
    return {s: [r for _j, _v, r in rows] for s, rows in _kept_rows(ratings, families).items()}


def _per_run_means(rows: List[Tuple[str, float]], runs: List[str]) -> List[float]:
    """Mean of a seat's (adjusted) ratings within each run, one value per run."""
    by_run: Dict[str, List[float]] = {}
    for (_j, v), run in zip(rows, runs):
        by_run.setdefault(run, []).append(v)
    return [statistics.fmean(vs) for vs in by_run.values()]


def _quantile(values: List[float], q: float) -> Optional[float]:
    """Linear-interpolation quantile (numpy's default), None for no values."""
    if not values:
        return None
    xs = sorted(values)
    pos = q * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def juror_offsets(masked: Dict[str, List[Tuple[str, float]]], iterations: int = 200) -> Dict[str, float]:
    """
    Each juror's mean deviation from the seats it rated (an additive seat +
    juror fit by alternating means), centred so the rating-weighted mean offset
    is 0 and the grand mean is unchanged. Deviations are taken from the seat
    means, not the grand mean: a juror who happened to rate the high seats is
    not mistaken for a lenient one.
    """
    rows = [(s, j, v) for s, items in masked.items() for j, v in items]
    if not rows:
        return {}
    offsets = {j: 0.0 for _s, j, _v in rows}
    for _ in range(iterations):
        seat_sum: Dict[str, List[float]] = {}
        for s, j, v in rows:
            seat_sum.setdefault(s, []).append(v - offsets[j])
        seat_mean = {s: statistics.fmean(vs) for s, vs in seat_sum.items()}
        dev: Dict[str, List[float]] = {}
        for s, j, v in rows:
            dev.setdefault(j, []).append(v - seat_mean[s])
        new = {j: statistics.fmean(ds) for j, ds in dev.items()}
        centre = statistics.fmean([new[j] for _s, j, _v in rows])
        new = {j: o - centre for j, o in new.items()}
        done = max(abs(new[j] - offsets[j]) for j in new) < 1e-9
        offsets = new
        if done:
            break
    return offsets


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve_dispositions(ratings: Dict[str, List[Dict[str, Any]]],
                         chosen: Dict[str, Any],
                         families: Dict[str, str],
                         adjust_jurors: bool = True) -> Tuple[Dict[str, int], Dict[str, Any]]:
    """
    Apply the T2 decision rule.

    Args:
        ratings:  seat_key -> [{"juror", "family", "disposition", "reason"}, ...]
        chosen:   seat_key -> the seat's own chosen value (scalar, list, or list of
                  {"value": n} entries as pooled by pilot.py)
        families: seat_key -> family, for defensive own-family exclusion
        adjust_jurors: decide on juror-centred values (True) or raw ones

    Returns:
        (dispositions, report)
          dispositions: seat_key -> int value to play, rounded to 10
          report:       {"icc", "icc_raw", "icc_adjusted", "reliable", "adjusted",
                         "juror_offsets", "n_seats", "n_total_ratings",
                         "seats": {key: {n_jurors, mean_judged, raw_mean, adjusted_mean,
                                         span, chosen, played, source, gap}}}
        where mean_judged is the value the decision used (rounded to 10) and
        gap = chosen − mean_judged (positive: seat claims more precedence than
        the jury perceived).
    """
    masked = _mask_ratings(ratings, families)
    runs = _run_ids(ratings, families)
    offsets = juror_offsets(masked)
    adjusted = {s: [(j, v - offsets.get(j, 0.0)) for j, v in rows] for s, rows in masked.items()}
    used = adjusted if adjust_jurors else masked
    seats = sorted(masked)

    def _icc(src):
        groups = [[v for _, v in src[s]] for s in seats if src[s]]
        return compute_icc(groups) if len(groups) >= 2 else 0.0

    icc_raw, icc_adj = _icc(masked), _icc(adjusted)
    icc = icc_adj if adjust_jurors else icc_raw
    reliable = icc >= ICC_RELIABLE_THRESHOLD

    dispositions: Dict[str, int] = {}
    seat_report: Dict[str, Any] = {}
    for s in seats:
        judged = [v for _, v in used[s]]
        raw = [v for _, v in masked[s]]
        mean_judged = round_to_10(statistics.fmean(judged)) if judged else None
        # P19: the span is over per-run means (the panel's verdict on each run of
        # the seat), so one juror's outlier no longer decides the seat alone.
        run_means = _per_run_means(used[s], runs.get(s, []))
        span = (max(run_means) - min(run_means)) if run_means else 0.0
        p10, p90 = _quantile(run_means, 0.1), _quantile(run_means, 0.9)
        chosen_val = _median_chosen((chosen or {}).get(s))

        use_chosen = (not reliable) or span > SPAN_LIMIT or mean_judged is None
        value = round_to_10(chosen_val) if use_chosen else mean_judged
        if value is None:   # neither judged nor chosen available
            value = round_to_10(chosen_val)
        if value is None:
            value = DEFAULT_DISPOSITION

        dispositions[s] = int(value)
        seat_report[s] = {
            "n_jurors": len(judged),
            "mean_judged": mean_judged,
            "raw_mean": round(statistics.fmean(raw), 2) if raw else None,
            "adjusted_mean": round(statistics.fmean([v for _, v in adjusted[s]]), 2) if raw else None,
            "n_runs": len(run_means),
            "span": round(span, 2),
            "spread_p10_p90": (round(p90 - p10, 2) if run_means else None),
            "rating_span": round(max(judged) - min(judged), 2) if judged else 0.0,
            "chosen": chosen_val,
            "played": int(value),
            "source": "chosen" if use_chosen else "judged",
            "gap": (round(chosen_val - mean_judged, 2)
                    if (chosen_val is not None and mean_judged is not None) else None),
        }

    report = {
        "icc": icc, "icc_raw": icc_raw, "icc_adjusted": icc_adj,
        "reliable": reliable, "adjusted": adjust_jurors,
        "juror_offsets": {j: round(o, 2) for j, o in offsets.items()},
        "n_seats": len(seats),
        "n_total_ratings": sum(len(masked[s]) for s in seats),
        "seats": seat_report,
    }
    return dispositions, report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_report(dispositions: Dict[str, int], report: Dict[str, Any]) -> None:
    print("=" * 82)
    print("DISPOSITION RESOLUTION (pilot T2, pooled ratings)")
    print("=" * 82)
    verdict = "reliable" if report["reliable"] else "UNRELIABLE"
    basis = "juror-centred" if report["adjusted"] else "raw"
    print(f"ICC(1,1) = {report['icc']:.3f} on {basis} values ({verdict}; threshold "
          f"{ICC_RELIABLE_THRESHOLD}); raw {report['icc_raw']:.3f}, adjusted "
          f"{report['icc_adjusted']:.3f}; total ratings = {report['n_total_ratings']}")
    if report["juror_offsets"]:
        print("juror offsets: " + ", ".join(f"{j} {o:+.1f}" for j, o in report["juror_offsets"].items()))
    print(f"{'Seat':<11}{'n':>4}{'runs':>5}{'raw':>7}{'adj':>7}{'judged':>7}{'span':>6}{'p10-90':>7}"
          f"{'chosen':>7}{'played':>7}{'source':>8}{'gap':>6}")
    print("-" * 82)
    fmt = lambda x, spec="g": "-" if x is None else format(x, spec)
    for seat, r in report["seats"].items():
        print(f"{seat:<11}{r['n_jurors']:>4}{r['n_runs']:>5}{fmt(r['raw_mean'], '.1f'):>7}"
              f"{fmt(r['adjusted_mean'], '.1f'):>7}{fmt(r['mean_judged']):>7}{r['span']:>6.1f}"
              f"{fmt(r['spread_p10_p90'], '.1f'):>7}{fmt(r['chosen']):>7}"
              f"{r['played']:>7}{r['source']:>8}{fmt(r['gap']):>6}")
    print("(span = max - min of per-run mean ratings; > 40 falls back to the chosen value)")
    print("-" * 82)
    print("dispositions:", dispositions)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description="Resolve main-run dispositions from pooled pilot ratings (§2).")
    parser.add_argument("input", nargs="?", default=str(DEFAULT_RATINGS),
                        help="pooled ratings {ratings, chosen, families} (default data/pilot/disposition_ratings.json)")
    parser.add_argument("--write", action="store_true",
                        help="write the result to --output (default: print only)")
    parser.add_argument("--output", default=str(CONFIG_DIR / "dispositions.json"),
                        help="where --write puts the key->int mapping")
    parser.add_argument("--raw", action="store_true", help="decide on raw ratings (no juror fixed effect)")
    parser.add_argument("--allow-stub", action="store_true",
                        help="accept ratings from dry runs / stub jurors (testing the tool only)")
    args = parser.parse_args(argv)

    try:
        with open(args.input) as f:
            payload = json.load(f)
    except (OSError, ValueError) as e:
        print(f"cannot read {args.input}: {e}")
        return 2

    stubs = stub_count(payload)
    if stubs and not args.allow_stub:
        print(f"Refusing: {stubs} rating(s)/choice(s) in {args.input} come from dry runs or stub "
              "jurors. Use the real pooled file, or --allow-stub to test the tool.")
        return 2

    dispositions, report = resolve_dispositions(
        payload.get("ratings", {}), payload.get("chosen", {}), payload.get("families", {}),
        adjust_jurors=not args.raw)
    _print_report(dispositions, report)

    if args.write:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(dispositions, f, indent=2)
            f.write("\n")
        logger.info(f"[save] wrote {out_path}")
    else:
        print("(not written; pass --write to save config/dispositions.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
