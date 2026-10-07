#!/usr/bin/env python3
"""
Pilot T2 disposition aggregation (spec §2, §9.2 T2).

A blind disposition jury rates each seat's directive-precedence (0..100) from
its redacted actions. This tool turns those ratings into the per-seat value the
main run will play, written to config/dispositions.json.

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

Own-family jurors are excluded per seat (§7). The caller is expected to pass
already-masked ratings, but we re-filter defensively on juror_family.

Pure: nothing is written unless run as __main__ (or the caller writes the
returned mapping itself).
"""

import json
import logging
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.jury import juror_family

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

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
    import math
    return int(math.floor(value / 10.0 + 0.5)) * 10


def _median_chosen(chosen: Any) -> Optional[float]:
    """A seat's chosen value: a scalar, or the median of a list of choices."""
    if chosen is None:
        return None
    if isinstance(chosen, (list, tuple)):
        vals = [float(x) for x in chosen if x is not None]
        return statistics.median(vals) if vals else None
    try:
        return float(chosen)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# ICC(2,1)
# ---------------------------------------------------------------------------

def compute_icc(matrix: List[List[float]]) -> float:
    """
    One-way ICC(1,1) over a list of per-seat rating groups (each row is one
    seat's judged values; rows may have different lengths). Returns 0.0 for a
    degenerate input — fewer than 2 seats, any seat with no ratings, fewer than
    2 ratings in all, or no between-seat variance. Clamped to [-1, 1].
    """
    groups = [[float(x) for x in row] for row in (matrix or []) if row]
    n = len(groups)
    if n < 2 or any(len(g) == 0 for g in groups):
        return 0.0
    sizes = [len(g) for g in groups]
    total_n = sum(sizes)
    if total_n <= n:                         # no within-group replication anywhere
        # still valid if groups have >1 obs collectively; guard N - n == 0
        if total_n - n <= 0:
            return 0.0

    grand = sum(sum(g) for g in groups) / total_n
    means = [sum(g) / len(g) for g in groups]

    ss_between = sum(len(g) * (m - grand) ** 2 for g, m in zip(groups, means))
    ss_within = sum((x - m) ** 2 for g, m in zip(groups, means) for x in g)
    if ss_between <= 1e-12:                  # seats do not differ → not informative
        return 0.0

    df_within = total_n - n
    ms_between = ss_between / (n - 1)
    ms_within = ss_within / df_within if df_within else 0.0
    k0 = (total_n - sum(s * s for s in sizes) / total_n) / (n - 1)

    denom = ms_between + (k0 - 1) * ms_within
    if denom <= 0:
        return 0.0
    icc = (ms_between - ms_within) / denom
    return max(-1.0, min(1.0, round(icc, 4)))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _mask_ratings(ratings: Dict[str, List[Dict[str, Any]]], families: Dict[str, str]
                  ) -> Dict[str, List[Tuple[str, float]]]:
    """Drop own-family jurors and ratings with no value; return seat -> [(juror, value)]."""
    masked: Dict[str, List[Tuple[str, float]]] = {}
    for seat, rlist in (ratings or {}).items():
        fam = families.get(seat)
        kept: List[Tuple[str, float]] = []
        for r in rlist or []:
            if not isinstance(r, dict):
                continue
            juror = r.get("juror", "")
            jf = r.get("family") or juror_family(juror)
            if fam is not None and jf == fam:
                continue   # own-family exclusion (§7)
            val = r.get("disposition")
            if val is None:
                continue
            try:
                kept.append((juror, float(val)))
            except (TypeError, ValueError):
                continue
        masked[seat] = kept
    return masked


def resolve_dispositions(ratings: Dict[str, List[Dict[str, Any]]],
                         chosen: Dict[str, Any],
                         families: Dict[str, str]) -> Tuple[Dict[str, int], Dict[str, Any]]:
    """
    Apply the T2 decision rule.

    Args:
        ratings:  seat_key -> [{"juror", "family", "disposition", "reason"}, ...]
        chosen:   seat_key -> the seat's own chosen value (scalar or list)
        families: seat_key -> family, for defensive own-family exclusion

    Returns:
        (dispositions, report)
          dispositions: seat_key -> int value to play, rounded to 10
          report:       {"icc", "reliable", "n_seats", "n_total_ratings",
                         "seats": {key: {n_jurors, mean_judged, span, chosen,
                                         played, source, gap}}}
        where gap = chosen − mean_judged (positive: seat claims more precedence
        than the jury perceived).
    """
    masked = _mask_ratings(ratings, families)
    seats = sorted(masked)

    # One-way ICC over each seat's (post-exclusion) judged values.
    groups = [[v for _, v in masked[s]] for s in seats if masked[s]]
    icc = compute_icc(groups) if len(groups) >= 2 else 0.0
    reliable = icc >= ICC_RELIABLE_THRESHOLD

    dispositions: Dict[str, int] = {}
    seat_report: Dict[str, Any] = {}
    for s in seats:
        judged = [v for _, v in masked[s]]
        mean_judged = round_to_10(statistics.fmean(judged)) if judged else None
        span = (max(judged) - min(judged)) if judged else 0.0
        chosen_val = _median_chosen(chosen.get(s))

        wide = span > SPAN_LIMIT
        use_chosen = (not reliable) or wide or mean_judged is None

        if use_chosen:
            value = round_to_10(chosen_val)
        else:
            value = mean_judged
        if value is None:   # neither judged nor chosen available
            value = round_to_10(chosen_val)
        if value is None:
            value = DEFAULT_DISPOSITION

        dispositions[s] = int(value)
        seat_report[s] = {
            "n_jurors": len(judged),
            "mean_judged": mean_judged,
            "span": round(span, 2),
            "chosen": chosen_val,
            "played": int(value),
            "source": "chosen" if use_chosen else "judged",
            "gap": (round(chosen_val - mean_judged, 2)
                    if (chosen_val is not None and mean_judged is not None) else None),
        }

    report = {
        "icc": icc,
        "reliable": reliable,
        "n_seats": len(seats),
        "n_total_ratings": sum(len(g) for g in groups),
        "seats": seat_report,
    }
    return dispositions, report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_report(dispositions: Dict[str, int], report: Dict[str, Any]) -> None:
    print("=" * 64)
    print("DISPOSITION RESOLUTION (pilot T2)")
    print("=" * 64)
    verdict = "reliable" if report["reliable"] else "UNRELIABLE"
    print(f"ICC(1,1) = {report['icc']:.3f} ({verdict}; threshold "
          f"{ICC_RELIABLE_THRESHOLD}), total ratings = {report['n_total_ratings']}")
    print(f"{'Seat':<14}{'judged':>8}{'span':>7}{'chosen':>8}{'played':>8}{'source':>9}{'gap':>7}")
    print("-" * 64)
    for seat, r in report["seats"].items():
        judged = "-" if r["mean_judged"] is None else f"{r['mean_judged']}"
        chosen = "-" if r["chosen"] is None else f"{r['chosen']:g}"
        gap = "-" if r["gap"] is None else f"{r['gap']:g}"
        print(f"{seat:<14}{judged:>8}{r['span']:>7g}{chosen:>8}"
              f"{r['played']:>8}{r['source']:>9}{gap:>7}")
    print("-" * 64)
    print("dispositions:", dispositions)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description="Resolve pilot T2 dispositions (spec §2).")
    parser.add_argument("input", help="JSON file of {ratings, chosen, families}")
    parser.add_argument("--output", default=str(CONFIG_DIR / "dispositions.json"),
                        help="where to write the key->int mapping")
    parser.add_argument("--dry-run", action="store_true", help="print, do not write")
    args = parser.parse_args(argv)

    with open(args.input) as f:
        payload = json.load(f)

    dispositions, report = resolve_dispositions(
        payload.get("ratings", {}), payload.get("chosen", {}), payload.get("families", {}))
    _print_report(dispositions, report)

    if not args.dry_run:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(dispositions, f, indent=2)
        logger.info(f"[save] wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
