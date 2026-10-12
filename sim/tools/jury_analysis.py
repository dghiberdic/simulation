#!/usr/bin/env python3
"""
Grand Jury per-seat measures over pooled run records (spec §7, §8, §9.3).

One juror (core/jury.py) scores each seat per run: five rubric items (1-5),
a holistic composite (1-5) and a perceived disposition (0-100). Per seat this
tool reports, over every run with a usable verdict, the number of runs, the
mean and the SD of each item, the composite and the perceived disposition.
There is no juror adjustment: one juror, whose family plays no seat. Runs whose juror skipped the rubric ("no examples yet")
contribute their perceived disposition only. The UPS contribution is an
offline replay (§8), not a jury measure, and is not reported here.

UPS-weight sensitivity
----------------------
The deterministic index is a weighted sum of five components (capability,
concentration, equity, norms, integrity; config/world.json
scoring.ups_weights). The index is recomputed from each run's
final.ups_index components under
  * each weight moved by ±0.1 (floored at 0) and the five renormalised to 1, and
  * --samples (default 200) weightings drawn from a Dirichlet with
    alpha_k = 4 · w_k, seeded (--seed, default 0),
and the order of runs and of cells (scenario/condition means) is compared
with the baseline order: Spearman rho per perturbation, the share of
Dirichlet draws keeping the exact order, and each run's / cell's rank range.
Runs without all five components are left out of the sensitivity.

Runs are keyed by run id; two records sharing one are kept apart by path
with a WARNING.

CLI:
  python tools/jury_analysis.py <log.json|dir> [...] [--samples 200] [--seed 0] [--json]
                                [--include-dry]
"""

import argparse
import json
import random
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.jury import RUBRIC_ITEMS
from tools.jury_check import run_keys, spearman

MEASURES = tuple(f"items.{i}" for i in RUBRIC_ITEMS) + ("composite", "perceived_disposition")
UPS_COMPONENTS = ("capability", "concentration", "equity", "norms", "integrity")
DEFAULT_UPS_WEIGHTS = {k: 0.2 for k in UPS_COMPONENTS}
WEIGHT_STEP = 0.1
DIRICHLET_CONCENTRATION = 4.0


def _num(x: Any) -> Optional[float]:
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _mean(vals: Sequence[Optional[float]]) -> Optional[float]:
    vs = [v for v in vals if v is not None]
    return sum(vs) / len(vs) if vs else None


def _r(x: Optional[float], nd: int = 2) -> Optional[float]:
    return None if x is None else round(x, nd)


def cell_of(record: Dict[str, Any]) -> str:
    cfg = record.get("config") or {}
    return f"{cfg.get('scenario') or '?'}/{cfg.get('condition') or '?'}"


def extract_rows(record: Dict[str, Any], run: str,
                 notes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """One row per (measure, seat) of this run's verdict: {run, measure, seat, value}."""
    notes = notes if notes is not None else []
    gj = ((record or {}).get("final") or {}).get("grand_jury")
    if not isinstance(gj, dict) or not isinstance(gj.get("actors"), dict):
        notes.append(f"{run}: no Grand Jury verdict; skipped")
        return []
    if gj.get("error") and not gj.get("juror"):
        notes.append(f"{run}: Grand Jury gave no usable verdict ({gj['error']}); skipped")
        return []
    if gj.get("rubric") not in (None, "scored"):
        notes.append(f"{run}: rubric not scored ({gj.get('rubric')}); perceived disposition only")
    if gj.get("fallback_used"):
        notes.append(f"{run}: scored by the fallback juror {gj.get('juror')}")
    rows = []
    for seat, a in gj["actors"].items():
        if not isinstance(a, dict):
            continue
        items = a.get("items") if isinstance(a.get("items"), dict) else {}
        vals = {f"items.{i}": _num(items.get(i)) for i in RUBRIC_ITEMS}
        vals["composite"] = _num(a.get("composite"))
        vals["perceived_disposition"] = _num(a.get("perceived_disposition"))
        rows += [{"run": run, "measure": m, "seat": seat, "value": v}
                 for m, v in vals.items() if v is not None]
    return rows


def seat_measures(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """measure -> {seat: {n_runs, mean, sd}}."""
    out: Dict[str, Any] = {}
    for measure in MEASURES:
        by_seat: Dict[str, List[float]] = {}
        for r in rows:
            if r["measure"] == measure:
                by_seat.setdefault(r["seat"], []).append(r["value"])
        if by_seat:
            out[measure] = {seat: {"n_runs": len(vs), "mean": _r(statistics.fmean(vs)),
                                   "sd": _r(statistics.stdev(vs)) if len(vs) > 1 else None}
                            for seat, vs in sorted(by_seat.items())}
    return out


# ---------------------------------------------------------------------------
# UPS-weight sensitivity
# ---------------------------------------------------------------------------

def base_weights() -> Dict[str, float]:
    try:
        from core.config import load_world
        w = load_world()["scoring"]["ups_weights"]
        return {k: float(w[k]) for k in UPS_COMPONENTS}
    except Exception:
        return dict(DEFAULT_UPS_WEIGHTS)


def _normalise(w: Dict[str, float]) -> Dict[str, float]:
    total = sum(w.values())
    return {k: v / total for k, v in w.items()} if total > 0 else dict(DEFAULT_UPS_WEIGHTS)


def perturbations(weights: Dict[str, float]) -> List[Tuple[str, Dict[str, float]]]:
    """Each weight ±WEIGHT_STEP (floored at 0), all five renormalised."""
    out = []
    for k in UPS_COMPONENTS:
        for sign, label in ((1, "+"), (-1, "-")):
            w = dict(weights)
            w[k] = max(0.0, w[k] + sign * WEIGHT_STEP)
            out.append((f"{k} {label}{WEIGHT_STEP:g}", _normalise(w)))
    return out


def dirichlet(weights: Dict[str, float], n: int, seed: int) -> List[Dict[str, float]]:
    rng = random.Random(seed)
    alphas = {k: max(DIRICHLET_CONCENTRATION * weights[k], 1e-3) for k in UPS_COMPONENTS}
    out = []
    for _ in range(n):
        draws = {k: rng.gammavariate(a, 1.0) for k, a in alphas.items()}
        out.append(_normalise(draws))
    return out


def _index(components: Dict[str, float], w: Dict[str, float]) -> float:
    return sum(w[k] * components[k] for k in UPS_COMPONENTS)


def _order(scores: Dict[str, float]) -> List[str]:
    return sorted(scores, key=lambda k: (-scores[k], k))


def _ranks(scores: Dict[str, float]) -> Dict[str, int]:
    return {k: i + 1 for i, k in enumerate(_order(scores))}


def _stability(units: Dict[str, Dict[str, float]], weights: Dict[str, float],
               perturbed: List[Tuple[str, Dict[str, float]]], samples: List[Dict[str, float]]
               ) -> Optional[Dict[str, Any]]:
    """Rank stability of `units` (name -> mean components) under the weightings."""
    if len(units) < 2:
        return None
    score = lambda w: {u: _index(c, w) for u, c in units.items()}
    base = score(weights)
    base_order = _order(base)
    keys = sorted(units)
    rho = lambda s: spearman([base[k] for k in keys], [s[k] for k in keys])
    per = []
    for label, w in perturbed:
        s = score(w)
        per.append({"weighting": label, "same_order": _order(s) == base_order, "spearman": rho(s)})
    rank_seen: Dict[str, List[int]] = {u: [] for u in units}
    same = 0
    rhos = []
    for w in samples:
        s = score(w)
        same += int(_order(s) == base_order)
        r = rho(s)
        if r is not None:
            rhos.append(r)
        for u, k in _ranks(s).items():
            rank_seen[u].append(k)
    base_ranks = _ranks(base)
    return {
        "baseline": [{"unit": u, "ups": round(base[u], 4), "rank": base_ranks[u]} for u in base_order],
        "perturbations": per,
        "dirichlet": {"samples": len(samples),
                      "share_same_order": round(same / len(samples), 4) if samples else None,
                      "mean_spearman": _r(_mean(rhos), 4), "min_spearman": _r(min(rhos), 4) if rhos else None,
                      "rank_range": {u: [min(v), max(v)] for u, v in rank_seen.items() if v}},
    }


def ups_sensitivity(records: List[Tuple[str, Dict[str, Any]]], samples: int = 200, seed: int = 0,
                    weights: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    weights = _normalise(weights or base_weights())
    runs: Dict[str, Dict[str, float]] = {}
    cells: Dict[str, List[Dict[str, float]]] = {}
    for run, rec in records:
        idx = ((rec or {}).get("final") or {}).get("ups_index") or {}
        comps = {k: _num(idx.get(k)) for k in UPS_COMPONENTS}
        if any(v is None for v in comps.values()):
            continue
        runs[run] = comps
        cells.setdefault(cell_of(rec), []).append(comps)
    cell_means = {c: {k: statistics.fmean(x[k] for x in lst) for k in UPS_COMPONENTS}
                  for c, lst in cells.items()}
    perturbed = perturbations(weights)
    draws = dirichlet(weights, samples, seed)
    return {"weights": {k: round(v, 4) for k, v in weights.items()},
            "n_runs": len(runs), "seed": seed,
            "runs": _stability(runs, weights, perturbed, draws),
            "cells": _stability(cell_means, weights, perturbed, draws)}


# ---------------------------------------------------------------------------
# Top level
# ---------------------------------------------------------------------------

def analyse(records: List[Dict[str, Any]], samples: int = 200, seed: int = 0,
            paths: Optional[Sequence[Optional[str]]] = None) -> Dict[str, Any]:
    """`paths` (aligned with records) keep runs that share a run id apart."""
    keys, notes = run_keys(records, paths)
    rows: List[Dict[str, Any]] = []
    named: List[Tuple[str, Dict[str, Any]]] = []
    seat_models: Dict[str, set] = {}
    runs: Dict[str, Any] = {}
    for run, rec in zip(keys, records):
        named.append((run, rec))
        for k, lab in ((rec or {}).get("labs") or {}).items():
            seat_models.setdefault(k, set()).add(str((lab or {}).get("model") or "?"))
        rows += extract_rows(rec, run, notes)
        gj = ((rec or {}).get("final") or {}).get("grand_jury") or {}
        runs[run] = {"cell": cell_of(rec or {}), "juror": gj.get("juror"),
                     "rubric": gj.get("rubric"),
                     "index_ups": (((rec or {}).get("final") or {}).get("ups_index") or {}).get("ups")}
    return {"n_runs": len(records),
            "seat_models": {k: sorted(v) for k, v in seat_models.items()},
            "measures": seat_measures(rows), "runs": runs,
            "ups_sensitivity": ups_sensitivity(named, samples, seed), "notes": notes}


def _print(rep: Dict[str, Any]) -> None:
    fmt = lambda x: "-" if x is None else f"{x:.2f}"
    print("=" * 72)
    print("GRAND JURY PER-SEAT MEASURES (one juror; items and composite 1-5, disposition 0-100)")
    print("=" * 72)
    print(f"runs={rep['n_runs']}")
    for measure, seats in rep["measures"].items():
        print("-" * 72)
        print(measure)
        print(f"  {'seat':<11}{'runs':>6}{'mean':>9}{'sd':>8}")
        for seat, s in seats.items():
            print(f"  {seat:<11}{s['n_runs']:>6}{fmt(s['mean']):>9}{fmt(s['sd']):>8}")
    print("-" * 72)
    print("Runs: " + "; ".join(f"{r} [{v['cell']}] juror {v['juror'] or '-'}, rubric {v['rubric'] or '-'}, "
                               f"index UPS (0-1) {fmt(v['index_ups'])}" for r, v in rep["runs"].items()))
    sens = rep["ups_sensitivity"]
    print("-" * 72)
    print(f"UPS-weight sensitivity (index, 0-1; weights {sens['weights']}; {sens['n_runs']} run(s) with "
          f"components; seed {sens['seed']})")
    for unit in ("runs", "cells"):
        st = sens[unit]
        if not st:
            print(f"  {unit}: fewer than 2 — no ranking to test")
            continue
        print(f"  {unit}: baseline order " + ", ".join(f"{b['unit']} {b['ups']:.4f}" for b in st["baseline"]))
        moved = [p["weighting"] for p in st["perturbations"] if not p["same_order"]]
        rhos = [p["spearman"] for p in st["perturbations"] if p["spearman"] is not None]
        print(f"    ±{WEIGHT_STEP:g} per weight: Spearman min {fmt(min(rhos) if rhos else None)}; order kept in "
              f"{len(st['perturbations']) - len(moved)}/{len(st['perturbations'])}"
              + (f" (changed under: {', '.join(moved)})" if moved else ""))
        d = st["dirichlet"]
        print(f"    Dirichlet ({d['samples']} draws): Spearman mean {fmt(d['mean_spearman'])} min "
              f"{fmt(d['min_spearman'])}; same order {fmt(d['share_same_order'])}; rank ranges "
              + ", ".join(f"{u} {lo}-{hi}" for u, (lo, hi) in d["rank_range"].items()))
    for n in rep["notes"]:
        print(f"note: {n}")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Grand Jury per-seat measures (§7, §8).")
    p.add_argument("logs", nargs="+", help="run log JSON files or directories")
    p.add_argument("--samples", type=int, default=200, help="Dirichlet weightings (default 200)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--json", action="store_true")
    p.add_argument("--include-dry", action="store_true", help="read dry-run records too")
    args = p.parse_args(argv)
    from tools.summarize_run import load_logs
    loaded = load_logs(args.logs, include_dry=args.include_dry)[0]
    if not loaded:
        print("no run records (dry-run records need --include-dry)")
        return 0
    rep = analyse([rec for _p, rec in loaded], samples=max(args.samples, 0), seed=args.seed,
                  paths=[p for p, _rec in loaded])
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        _print(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
