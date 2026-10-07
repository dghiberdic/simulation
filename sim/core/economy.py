#!/usr/bin/env python3
"""
Economy (spec §3.1–§3.3), ported from the v8 stage-1 check (income-based
talent target, Capital floored at 0 with no upper cap).

Per turn, before proposals (§5.2 step 1):
  income      I = Cap₀/8 · √((C · Inf) ÷ (C₀ · Inf₀))
  talent      target = max(0.5 · seed, seed · √((I · Inf) ÷ (I₀ · Inf₀)));
              move r of the way to target, then renormalise to 100
  know-how    ΔK = 1.2 · (talent ÷ 20) + N(0, σ)       (no compounding)
  capability  C = a · ln(compute) + K

After execution, purchases are granted pro rata to units requested under the
binding limit min(national cap, scenario ceiling).
"""

import math
import random
from typing import Any, Dict, List

from core.state import VALUE_AXES, LabState, WorldState


# ---------------------------------------------------------------------------
# Formulas
# ---------------------------------------------------------------------------

def base_income(lab: LabState, econ: Dict[str, Any]) -> float:
    """I₀: Capital income at the seed position."""
    return lab.seed["capital"] / econ["income_divisor"]


def capital_income(lab: LabState, econ: Dict[str, Any]) -> float:
    c = lab.capability(econ["capability_compute_elasticity"])
    ratio = (c * lab.influence) / (lab.seed["capability"] * lab.seed["influence"])
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


def value_pull(values: Dict[str, int], state_values: Dict[str, int],
               charter_values: Dict[str, int], disposition: int, max_step: int) -> Dict[str, int]:
    """Move each value at most ±max_step towards (1 − d)·state + d·charter (§3.2)."""
    d = disposition / 100.0
    pulled = {}
    for axis in VALUE_AXES:
        target = (1 - d) * state_values[axis] + d * charter_values[axis]
        current = values[axis]
        step = max(-max_step, min(max_step, round(target - current)))
        pulled[axis] = max(0, min(100, current + step))
    return pulled


# ---------------------------------------------------------------------------
# Turn steps
# ---------------------------------------------------------------------------

def macro_growth(labs: List[LabState], world: WorldState, cfg: Dict[str, Any],
                 rng: random.Random) -> Dict[str, Any]:
    """
    §5.2 step 1 for all seats: invest_capital repayment, Capital income,
    talent drift, know-how growth with shock, and national stock growth.

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
        shock = rng.gauss(0.0, sd) if sd else 0.0
        growth = econ["know_how_growth"] * lab.talent / econ["know_how_reference_talent_share"]
        lab.know_how += growth + shock
        record["shock"][lab.key] = round(shock, 3)
        record["know_how_growth"][lab.key] = round(growth, 3)

    if world.turn > 1:
        world.us_stock += world.us_growth
        world.china_stock += world.china_growth
    return record


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
    Holdings exactly at the ceiling do not exceed it, so purchases stay open (C1-13)."""
    return world.ceiling is not None and sum(lab.compute for lab in labs) > world.ceiling
