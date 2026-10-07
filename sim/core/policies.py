#!/usr/bin/env python3
"""
Zero-cost scripted policies (spec §9.2, Stage 1 scripted checks).

Each policy plays one seat from the game state alone, with no LLM call, so the
economy and the intrusion payoff can be stress-tested before any spend. A
policy returns a turn reply in the same shape a model would produce:
{"actions": [...], "reported_actions": [...], "report": {...}, "public_statement", "reasoning"}.
"""

import random
from typing import Any, Dict, List

from core.economy import purchase_price, purchases_frozen
from core.state import LabState, WorldState


def _buy(lab: LabState, labs, world: WorldState, cfg: Dict[str, Any], fraction: float) -> List[Dict[str, Any]]:
    """acquire_compute up to a fraction of affordable units, within the per-turn cap."""
    if purchases_frozen(labs, world):
        return []
    price = purchase_price(1.0, cfg["compute"])
    affordable = lab.capital * fraction / price
    units = round(min(cfg["compute"]["max_purchase_per_turn"], affordable), 1)
    return [{"type": "acquire_compute", "units": units}] if units >= 3 else []


def _best_target(lab: LabState, labs, cfg: Dict[str, Any]) -> LabState:
    a = cfg["economy"]["capability_compute_elasticity"]
    return max((x for x in labs if x.key != lab.key), key=lambda x: x.capability(a))


def _intrude_gain(lab: LabState, target: LabState, cfg: Dict[str, Any]) -> float:
    a = cfg["economy"]["capability_compute_elasticity"]
    icfg = cfg["intrusion"]
    return max(icfg["min_gain"], icfg["gain_share"] * (target.capability(a) - lab.capability(a)))


def _reply(actions: List[Dict[str, Any]], note: str) -> Dict[str, Any]:
    return {"actions": actions, "reported_actions": [a["type"] for a in actions],
            "report": None, "public_statement": "", "reasoning": note}


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

def idle(lab, labs, world, cfg, scenario, rng):
    return _reply([], "idle")


def greedy(lab, labs, world, cfg, scenario, rng):
    """Spend all affordable Capital on compute while the freeze allows it."""
    return _reply(_buy(lab, labs, world, cfg, 1.0), "greedy buyer")


def income(lab, labs, world, cfg, scenario, rng):
    """Spend only this turn's income on compute (steady buyer)."""
    price = purchase_price(1.0, cfg["compute"])
    if purchases_frozen(labs, world) or lab.income < 3 * price:
        return _reply([], "income buyer idle")
    units = round(min(cfg["compute"]["max_purchase_per_turn"], lab.income / price), 1)
    return _reply([{"type": "acquire_compute", "units": units}], "income buyer")


def intrude_profitable(lab, labs, world, cfg, scenario, rng):
    """Intrude on the leader only when the expected value is positive; else buy."""
    if world.intrusion_open:
        target = _best_target(lab, labs, cfg)
        gain = _intrude_gain(lab, target, cfg)
        icfg = cfg["intrusion"]
        p = max(icfg["success_floor"], icfg["success_base"])
        ev = p * cfg["scoring"]["w_capability"] * gain - icfg["fee_capital_per_target"] * 0
        if ev > 0 and lab.capital >= icfg["fee_capital_per_target"] and lab.influence >= icfg["fee_influence_per_target"]:
            return _reply([{"type": "intrude", "targets": [target.key]}], "intrude (positive EV)")
    return _reply(_buy(lab, labs, world, cfg, 1.0), "intrude_profitable fallback: buy")


def always_intrude(lab, labs, world, cfg, scenario, rng):
    """Intrude on the leader whenever intrusion is open and affordable."""
    icfg = cfg["intrusion"]
    if world.intrusion_open:
        target = _best_target(lab, labs, cfg)
        if lab.capital >= icfg["fee_capital_per_target"] and lab.influence >= icfg["fee_influence_per_target"]:
            return _reply([{"type": "intrude", "targets": [target.key]}], "always intrude")
    return _reply(_buy(lab, labs, world, cfg, 1.0), "always_intrude fallback: buy")


def malformed(lab, labs, world, cfg, scenario, rng):
    """SC7: a broken reply. The engine must treat it as a logged no-op, not a crash."""
    return {"actions": "not-a-list", "reasoning": 12345}


POLICIES = {
    "idle": idle,
    "greedy": greedy,
    "income": income,
    "intrude_profitable": intrude_profitable,
    "always_intrude": always_intrude,
    "malformed": malformed,
}
