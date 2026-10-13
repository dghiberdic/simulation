#!/usr/bin/env python3
"""
Economy (spec §3.1–§3.3): an income-based talent target; Capital floored at 0
with no upper cap.

Per turn, before proposals (§5.2 step 1):
  income      I = Cap₀/8 · √((C̃ · Inf) ÷ (C̃₀ · Inf₀))     C̃ = C − 100 (capability_offset)
  talent      target = max(0.5 · seed, seed · √((I · Inf) ÷ (I₀ · Inf₀)));
              move r of the way to target, then renormalise to 100
  know-how    ΔK = 0.85 · (talent ÷ 20) + N(0, σ)      (no compounding)
  capability  C = a · ln(compute) + K                  (a = 3.6)
  stock       US +150 and China +11 units per turn from turn 2 (us_growth_start_turn)

After execution, purchases are granted pro rata to units requested under the
binding limit min(national cap, scenario ceiling).
"""

import math
import random
from statistics import NormalDist
from typing import Any, Callable, Dict, List, Optional

from core.state import VALUE_AXES, LabState, WorldState


# ---------------------------------------------------------------------------
# Formulas
# ---------------------------------------------------------------------------

def base_income(lab: LabState, econ: Dict[str, Any]) -> float:
    """I₀: Capital income at the seed position."""
    return lab.seed["capital"] / econ["income_divisor"]


def capability_offset(econ: Dict[str, Any]) -> float:
    """The offset in C̃ = C − offset (§3.1); used in income and UPS ratios only."""
    return float(econ.get("capability_offset", 0.0))


def capital_income(lab: LabState, econ: Dict[str, Any]) -> float:
    """I = Cap₀/8 · √((C̃ · Inf) ÷ (C̃₀ · Inf₀)), C̃ = C − capability_offset (§3.1).
    A non-positive C̃ (or seed C̃) gives zero income rather than a math error."""
    off = capability_offset(econ)
    c_t = lab.capability(econ["capability_compute_elasticity"]) - off
    c0_t = lab.seed["capability"] - off
    denom = c0_t * lab.seed["influence"]
    if denom <= 0:
        return 0.0
    ratio = (c_t * lab.influence) / denom
    return base_income(lab, econ) * math.sqrt(max(ratio, 0.0))


def talent_target(lab: LabState, income: float, econ: Dict[str, Any]) -> float:
    ratio = (income * lab.influence) / (base_income(lab, econ) * lab.seed["influence"])
    target = lab.seed["talent"] * math.sqrt(max(ratio, 0.0))
    return max(econ["talent_floor_fraction"] * lab.seed["talent"], target)


def purchase_price(units: float, compute_cfg: Dict[str, Any]) -> float:
    """0.25 · units · (1 + (100 − SCR)/100) Capital."""
    scr = compute_cfg["supply_chain_robustness"]
    return compute_cfg["price_base"] * units * (1 + (100 - scr) / 100)


def prorate_purchases(requests: Dict[str, float], headroom: float) -> Dict[str, float]:
    """Share headroom pro rata to units requested (§3.3); full grant if it fits."""
    total = sum(requests.values())
    if total <= 0:
        return {k: 0.0 for k in requests}
    k = min(1.0, max(0.0, headroom) / total)
    return {key: units * k for key, units in requests.items()}


def value_pull(values: Dict[str, float], state_values: Dict[str, float],
               charter_values: Dict[str, float], disposition: int,
               max_step: float) -> Dict[str, float]:
    """Move each value min(max_step, |gap|) towards target = (1 − d)·state + d·charter
    (§3.2: "up to 1 point (not rounded)"). Values are real-valued, so a value within
    max_step of its target lands on it; prompts show them rounded."""
    d = disposition / 100.0
    pulled = {}
    for axis in VALUE_AXES:
        target = (1 - d) * state_values[axis] + d * charter_values[axis]
        current = values[axis]
        step = max(-max_step, min(max_step, target - current))
        pulled[axis] = max(0.0, min(100.0, current + step))
    return pulled


# ---------------------------------------------------------------------------
# Turn steps
# ---------------------------------------------------------------------------

def shock_from_uniform(u: float, sd: float) -> float:
    """Know-how shock N(0, sd) from one uniform draw u (inverse normal CDF), so a
    keyed, logged uniform (CODE_V5_CONTRACT: purpose "shock") fixes the shock."""
    if not sd:
        return 0.0
    u = min(max(u, 1e-12), 1.0 - 1e-12)
    return sd * NormalDist().inv_cdf(u)


def macro_growth(labs: List[LabState], world: WorldState, cfg: Dict[str, Any],
                 rng: random.Random,
                 shock_uniform: Optional[Callable[[int, str], float]] = None) -> Dict[str, Any]:
    """
    §5.2 step 1 for all seats: invest_capital repayment, Capital income,
    talent drift, know-how growth with shock, and national stock growth
    (from turn compute.us_growth_start_turn).

    shock_uniform(turn, lab_key) -> u in (0, 1), if given, supplies each seat's
    shock as a keyed uniform (e.g. lambda t, k: keyed.uniform(t, "shock", None, k));
    otherwise shocks come from rng.gauss as before.

    Returns:
        {"income": {key: float}, "talent": {key: float}, "shock": {key: float},
         "know_how_growth": {key: float}, "repaid": {key: float}}
    """
    econ = cfg["economy"]
    record: Dict[str, Dict[str, float]] = {
        "income": {}, "talent": {}, "shock": {}, "know_how_growth": {}, "repaid": {},
    }

    for lab in labs:
        if lab.invested:
            repaid = lab.invested * (1 + cfg["actions"]["invest_capital"]["return_per_turn"])
            lab.capital += repaid
            record["repaid"][lab.key] = round(repaid, 3)
            lab.invested = 0.0
        lab.income = capital_income(lab, econ)
        lab.capital = max(0.0, lab.capital + lab.income)
        record["income"][lab.key] = round(lab.income, 3)

    r = econ["talent_drift_rate"]
    moved = [lab.talent + r * (talent_target(lab, lab.income, econ) - lab.talent) for lab in labs]
    total = sum(moved)
    for lab, share in zip(labs, moved):
        lab.talent = share * 100.0 / total
        record["talent"][lab.key] = round(lab.talent, 3)

    sd = econ["know_how_shock_sd"]
    for lab in labs:
        if shock_uniform is not None:
            shock = shock_from_uniform(shock_uniform(world.turn, lab.key), sd)
        else:
            shock = rng.gauss(0.0, sd) if sd else 0.0
        growth = econ["know_how_growth"] * lab.talent / econ["know_how_reference_talent_share"]
        lab.know_how += growth + shock
        record["shock"][lab.key] = round(shock, 3)
        record["know_how_growth"][lab.key] = round(growth, 3)

    if world.turn >= growth_start_turn(cfg):
        world.us_stock += world.us_growth
        world.china_stock += world.china_growth
    return record


def growth_start_turn(cfg: Dict[str, Any]) -> int:
    """First turn on which US (and China) stock grows (§3.1, §5.2 step 1: from turn 2)."""
    return int(cfg.get("compute", {}).get("us_growth_start_turn", 2))


def apply_value_pull(labs: List[LabState], world: WorldState, cfg: Dict[str, Any]) -> None:
    for lab in labs:
        if lab.disposition is None:
            continue
        lab.values = value_pull(lab.values, world.state_values, lab.charter_values,
                                lab.disposition, cfg["values"]["max_pull_per_turn"])


def execute_purchases(labs: List[LabState], world: WorldState, requests: Dict[str, float],
                      cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Grant acquire_compute requests pro rata under the binding holdings limit and
    charge each lab for the units it receives.

    Returns:
        {"limit": float, "headroom": float, "granted": {key: units}, "cost": {key: Capital}}
    """
    holdings = sum(lab.compute for lab in labs)
    limit = world.holdings_limit()
    headroom = max(0.0, limit - holdings)
    granted = prorate_purchases(requests, headroom)
    by_key = {lab.key: lab for lab in labs}
    cost = {}
    for key, units in granted.items():
        if units <= 0:
            continue
        price = purchase_price(units, cfg["compute"])
        lab = by_key[key]
        lab.compute += units
        lab.capital = max(0.0, lab.capital - price)
        cost[key] = round(price, 3)
    return {
        "limit": round(limit, 2),
        "headroom": round(headroom, 2),
        "granted": {k: round(v, 3) for k, v in granted.items()},
        "cost": cost,
    }


def purchases_frozen(labs: List[LabState], world: WorldState) -> bool:
    """acquire_compute does not execute while combined holdings exceed the ceiling (A.4/A.5).
    Holdings exactly at the ceiling do not exceed it, so purchases stay open."""
    return world.ceiling is not None and sum(lab.compute for lab in labs) > world.ceiling


def swap_capability(labs: List[LabState], key_a: str, key_b: str, elasticity: float) -> None:
    """
    T1 (§9.2): swap two seats' capability seeds; each seat's K is recomputed
    against its own compute (K = seed − a · ln(compute)). Charter, talent,
    compute, Capital and Influence stay with the seat.
    """
    by = {lab.key: lab for lab in labs}
    a, b = by[key_a], by[key_b]
    a.seed["capability"], b.seed["capability"] = b.seed["capability"], a.seed["capability"]
    for lab in (a, b):
        lab.know_how = lab.seed["capability"] - elasticity * math.log(lab.compute)


# ---------------------------------------------------------------------------
# Hindcast (§9.2): the engine's turns 1–2 against the real ECI change
# ---------------------------------------------------------------------------

# Real ECI change over turns 1–2 (Epoch releases to 30 September 2026).
HINDCAST_REAL = {"frontier": 4.5, "bottom_three": 2.5}
HINDCAST_TURN = 2
HINDCAST_EXPECTED_FRONTIER_GAP = 2.0     # "We expect the engine to run about 2 ECI low"


def hindcast_gaps(seed: Dict[str, float], delta: Dict[str, float]) -> Dict[str, float]:
    """The mean capability change of the two highest seeds (frontier) and of the
    rest (bottom three), and each one's gap to the real change (simulated − real)."""
    ranked = sorted(seed, key=seed.get, reverse=True)
    frontier = sum(delta[k] for k in ranked[:2]) / 2
    bottom = sum(delta[k] for k in ranked[2:]) / len(ranked[2:])
    return {"frontier": frontier, "bottom_three": bottom,
            "gap_frontier": frontier - HINDCAST_REAL["frontier"],
            "gap_bottom_three": bottom - HINDCAST_REAL["bottom_three"]}
