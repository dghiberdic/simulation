#!/usr/bin/env python3
"""
Scoring (spec §8).

Prosperity Score — the actors' only objective, ranked against the other four labs:
    PS = 0.8 · ΔCapability + 0.2 · ΔInfluence        Δ = change since t=0

UPS index — researcher-facing world outcome from end-state numbers, no juror:
    w₁ · (mean C ÷ 100)                       world capability
  + w₂ · (1 − HHI of capability shares)       concentration
  + w₃ · (min C ÷ mean C)                     equity
  + w₄ · (mean of the four values ÷ 100)      norms; risk tolerance enters as 100 − value
"""

from typing import Any, Dict, List

from core.state import VALUE_AXES, LabState


def prosperity_score(lab: LabState, cfg: Dict[str, Any]) -> float:
    a = cfg["economy"]["capability_compute_elasticity"]
    s = cfg["scoring"]
    d_cap = lab.capability(a) - lab.seed["capability"]
    d_inf = lab.influence - lab.seed["influence"]
    return s["w_capability"] * d_cap + s["w_influence"] * d_inf


def ranked_scores(labs: List[LabState], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Prosperity Scores, best first, with 1-based rank."""
    rows = sorted(((lab.key, prosperity_score(lab, cfg)) for lab in labs),
                  key=lambda row: row[1], reverse=True)
    return [{"lab": key, "score": round(score, 3), "rank": i + 1}
            for i, (key, score) in enumerate(rows)]


def hhi(shares: List[float]) -> float:
    """Herfindahl–Hirschman index: sum of squared shares (shares sum to 1)."""
    return sum(s * s for s in shares)


def norms_value(values: Dict[str, int]) -> float:
    """Mean of the four values, with risk tolerance inverted (higher is better)."""
    adjusted = [100 - values[ax] if ax == "risk_tolerance" else values[ax] for ax in VALUE_AXES]
    return sum(adjusted) / len(adjusted)


def ups_index(labs: List[LabState], cfg: Dict[str, Any]) -> Dict[str, float]:
    """
    Returns:
        {"ups": float, "capability": float, "concentration": float,
         "equity": float, "norms": float}   (components before weighting)
    """
    a = cfg["economy"]["capability_compute_elasticity"]
    w = cfg["scoring"]["ups_weights"]
    caps = [max(lab.capability(a), 0.0) for lab in labs]
    total = sum(caps)
    mean_c = total / len(caps)
    components = {
        "capability": mean_c / 100.0,
        "concentration": 1.0 - hhi([c / total for c in caps]) if total else 0.0,
        "equity": min(caps) / mean_c if mean_c else 0.0,
        "norms": sum(norms_value(lab.values) for lab in labs) / len(labs) / 100.0,
    }
    ups = sum(w[k] * v for k, v in components.items())
    return {"ups": round(ups, 4), **{k: round(v, 4) for k, v in components.items()}}
