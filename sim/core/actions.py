#!/usr/bin/env python3
"""
Round 2 action set (spec §4, §5.2 steps 3–5).

Seven proof-of-concept actions plus intrude. Up to max_actions_per_turn per
lab; every action costs at least min_action_cost Capital or Influence.

  acquire_compute(units)                 — Capital; executed pro rata by the engine
                                           via economy.execute_purchases; at most
                                           max_purchase_per_turn units per lab per
                                           turn in total across both slots
  accelerate_infrastructure              — Capital + Influence; US stock growth +15 (permanent)
  invest_capital(amount)                 — Capital now, repaid ×(1 + r) next turn
  build_influence(points)                — Capital per point; Influence capped at 100
  publish_narrative(target, axis, delta) — Influence; shift a lab's value on one axis
  diminish_competitor(target, points)    — Capital + Influence per point; target loses Influence
  lobby_institution                      — Capital + Influence; state values step toward own
  intrude(intruders, targets)            — resolved by core.intrusion

precheck() is purely programmatic (resource arithmetic and guardrails) and
never raises. Execution is split for simultaneity (§5.2, G1): the engine
pre-checks every seat against the start-of-execution state, then charge()s
every accepted action (intrude fees included; acquire_compute is charged on
granted units by economy.execute_purchases), then apply()s the effects.
execute() = charge() + apply() for the six actions that are neither purchases
nor intrusions.
"""

import difflib
import logging
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.economy import purchase_price, purchases_frozen
from core.state import VALUE_AXES, LabState, WorldState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

ACQUIRE_COMPUTE           = "acquire_compute"
ACCELERATE_INFRASTRUCTURE = "accelerate_infrastructure"
INVEST_CAPITAL            = "invest_capital"
BUILD_INFLUENCE           = "build_influence"
PUBLISH_NARRATIVE         = "publish_narrative"
DIMINISH_COMPETITOR       = "diminish_competitor"
LOBBY_INSTITUTION         = "lobby_institution"
INTRUDE                   = "intrude"

ACTION_TYPES = (
    ACQUIRE_COMPUTE, ACCELERATE_INFRASTRUCTURE, INVEST_CAPITAL, BUILD_INFLUENCE,
    PUBLISH_NARRATIVE, DIMINISH_COMPETITOR, LOBBY_INSTITUTION, INTRUDE,
)

# Handled outside execute()/apply(): purchases are pro rata, intrusions need all labs' commitments
ENGINE_ACTIONS = (ACQUIRE_COMPUTE, INTRUDE)

EPS = 1e-9


# ---------------------------------------------------------------------------
# Lab name resolution
# ---------------------------------------------------------------------------

# Names models use for a lab besides its key and display name: the actor
# (model family) and the parent company. Matched whole, case-insensitive.
LAB_ALIASES: Dict[str, Tuple[str, ...]] = {
    "anthropic": ("claude", "anthropic pbc"),
    "openai":    ("gpt", "chatgpt", "open ai"),
    "gdm":       ("gemini", "google", "alphabet", "deepmind", "google deepmind"),
    "meta":      ("muse", "llama", "meta platforms", "facebook", "meta ai"),
    "xai":       ("grok", "x.ai", "x ai", "xai corp"),
}

_SELF_WORDS = ("self", "me", "myself", "us", "my lab", "our lab")


def _labs_by_key(labs: Iterable[LabState]) -> Dict[str, LabState]:
    if isinstance(labs, dict):
        return dict(labs)
    return {lab.key: lab for lab in labs}


def _lab_names(by_key: Dict[str, LabState]) -> Dict[str, str]:
    """Lower-case name -> key, from aliases, actor names, keys and display names."""
    names: Dict[str, str] = {}
    for key, lab in by_key.items():
        for alias in LAB_ALIASES.get(key, ()):
            names[alias] = key
        actor = getattr(lab, "actor", "")
        if actor:
            names[actor.lower()] = key
        names[key.lower()] = key
        names[lab.lab.lower()] = key
    return names


def resolve_lab(name: Any, labs: Iterable[LabState], self_key: Optional[str] = None) -> Optional[str]:
    """
    Lab key from a key, display name, actor name ("Claude", "Grok"), parent
    company ("Google", "Facebook") or close typo; "self"/"me" map to self_key.
    A dict such as {"lab": "OpenAI"} is read through its "lab"/"name"/"key".
    """
    if isinstance(name, dict):
        name = name.get("lab") or name.get("name") or name.get("key")
    if not isinstance(name, str) or not name.strip():
        return None
    by_key = _labs_by_key(labs)
    lower = " ".join(name.strip().lower().split()).strip("'\"")
    if lower in _SELF_WORDS and self_key:
        return self_key
    names = _lab_names(by_key)
    if lower in names:
        return names[lower]
    close = difflib.get_close_matches(lower, list(names), n=1, cutoff=0.6)
    return names[close[0]] if close else None


# "Meta, xAI", "Meta and xAI", "Meta & xAI", "Meta/xAI"
_LIST_SPLIT = re.compile(r"\s*(?:,|&|/|;|\band\b)\s*", re.IGNORECASE)


def _split_names(raw: Any) -> List[Any]:
    if not isinstance(raw, str):
        return [raw]
    # "x.ai"/"X.AI" never contains a separator, so splitting is safe
    return [part for part in _LIST_SPLIT.split(raw) if part.strip()]


def _resolve_many(raw: Any, labs, self_key: str) -> List[str]:
    """Resolve a list or a joined string of lab names; unknown names raise."""
    if raw is None:
        return []
    items: List[Any] = []
    for item in (list(raw) if isinstance(raw, (list, tuple, set)) else [raw]):
        items.extend(_split_names(item))
    keys = set()
    for item in items:
        key = resolve_lab(item, labs, self_key)
        if key is None:
            raise ValueError(f"unknown lab {item!r}")
        keys.add(key)
    return sorted(keys)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

# A number, optionally followed by a unit word ("10", "-3", "10 Capital", "2.5 units")
_NUMBER = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?:[A-Za-z%][\w %.-]*)?$")

# Keys under which models nest an action's parameters
_NESTED = ("parameters", "params", "args", "arguments")


def _number(raw: Dict[str, Any], *names: str) -> float:
    """First present field as a finite float; NaN/inf (json accepts NaN) raise."""
    for name in names:
        value = raw.get(name)
        if value is None:
            continue
        if isinstance(value, bool):
            raise ValueError(f"{name} is not a number: {value!r}")
        if isinstance(value, (int, float)):
            number = float(value)
        else:
            found = _NUMBER.match(str(value).replace(",", ""))
            if not found:
                raise ValueError(f"{name} is not a number: {value!r}")
            number = float(found.group(1))
        if not math.isfinite(number):
            raise ValueError(f"{name} is not a finite number: {value!r}")
        return number
    raise ValueError(f"missing {names[0]}")


def _flatten(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Lift nested parameter dicts to the top level; top-level fields win."""
    flat: Dict[str, Any] = {}
    for key in _NESTED:
        if isinstance(raw.get(key), dict):
            flat.update(raw[key])
    flat.update({k: v for k, v in raw.items() if not (k in _NESTED and isinstance(v, dict))})
    return flat


def normalise_action(raw: Dict[str, Any], lab: LabState, labs, scenario: str) -> Dict[str, Any]:
    """
    Canonical action dict. Accepts "type"/"action"/"action_type", parameters
    nested under "parameters"/"params"/"args"/"arguments", numbers as strings
    with or without a unit, display/actor/parent names or "self" for labs.
    Raises ValueError on anything that cannot be read, including non-finite
    numbers (precheck turns that into a rejection).
    """
    if not isinstance(raw, dict):
        raise ValueError("action is not an object")
    raw = _flatten(raw)
    kind = raw.get("type") or raw.get("action") or raw.get("action_type")
    kind = str(kind or "").strip().lower().replace(" ", "_").replace("-", "_")
    if kind not in ACTION_TYPES:
        raise ValueError(f"unknown action type {kind!r}")
    action: Dict[str, Any] = {"type": kind}

    if kind == ACQUIRE_COMPUTE:
        action["units"] = _number(raw, "units", "amount", "compute")
    elif kind == INVEST_CAPITAL:
        action["amount"] = _number(raw, "amount", "capital")
    elif kind == BUILD_INFLUENCE:
        action["points"] = _number(raw, "points", "amount", "influence")
    elif kind == DIMINISH_COMPETITOR:
        action["points"] = _number(raw, "points", "amount")
        action["target"] = resolve_lab(raw.get("target"), labs, lab.key)
        if action["target"] is None:
            raise ValueError(f"unknown target {raw.get('target')!r}")
    elif kind == PUBLISH_NARRATIVE:
        action["target"] = resolve_lab(raw.get("target", "self"), labs, lab.key)
        if action["target"] is None:
            raise ValueError(f"unknown target {raw.get('target')!r}")
        action["axis"] = str(raw.get("axis") or "").strip().lower().replace(" ", "_")
        action["delta"] = _number(raw, "delta", "amount")
    elif kind == INTRUDE:
        targets = raw.get("targets") if raw.get("targets") is not None else raw.get("target")
        action["targets"] = _resolve_many(targets, labs, lab.key)
        # S1 form names only targets; the intruder is implicitly self
        intruders = raw.get("intruders")
        action["intruders"] = _resolve_many(intruders, labs, lab.key) if intruders else [lab.key]
    return action


# ---------------------------------------------------------------------------
# Costs
# ---------------------------------------------------------------------------

def action_cost(action: Dict[str, Any], lab: LabState, labs, world: WorldState,
                cfg: Dict[str, Any]) -> Tuple[float, float]:
    """(Capital, Influence) charged for a normalised action."""
    acfg = cfg["actions"]
    kind = action["type"]
    if kind == ACQUIRE_COMPUTE:
        return purchase_price(action["units"], cfg["compute"]), 0.0
    if kind == ACCELERATE_INFRASTRUCTURE:
        c = acfg[kind]
        return c["capital"], c["influence"]
    if kind == INVEST_CAPITAL:
        return action["amount"], 0.0
    if kind == BUILD_INFLUENCE:
        return acfg[kind]["capital_per_point"] * action["points"], 0.0
    if kind == PUBLISH_NARRATIVE:
        c = acfg[kind]
        return 0.0, c["influence_self"] if action["target"] == lab.key else c["influence_other"]
    if kind == DIMINISH_COMPETITOR:
        c = acfg[kind]
        return c["capital_per_point"] * action["points"], c["influence_per_point"] * action["points"]
    if kind == LOBBY_INSTITUTION:
        c = acfg[kind]
        return c["capital"], c["influence"]
    if kind == INTRUDE:
        n = len(action["targets"])
        icfg = cfg["intrusion"]
        return icfg["fee_capital_per_target"] * n, icfg["fee_influence_per_target"] * n
    raise ValueError(f"unknown action type {kind!r}")


# ---------------------------------------------------------------------------
# Pre-check (§5.2 step 3)
# ---------------------------------------------------------------------------

def _guardrail(action: Dict[str, Any], lab: LabState, labs, world: WorldState,
               cfg: Dict[str, Any], scenario: str, influence: float,
               units_so_far: float = 0.0) -> Optional[str]:
    """
    Reason the action is not allowed, or None. May trim build_influence points,
    and trims an acquire_compute to what remains of the per-turn unit cap
    (units_so_far = units already accepted this turn), noting "trimmed_from".
    """
    kind = action["type"]
    by_key = _labs_by_key(labs)
    if kind == ACQUIRE_COMPUTE:
        cap = cfg["compute"]["max_purchase_per_turn"]
        if action["units"] <= 0:
            return "units must be positive"
        if action["units"] > cap + EPS:
            return f"at most {cap:g} units per turn"
        if purchases_frozen(list(by_key.values()), world):
            return "purchases frozen: combined holdings exceed the ceiling"
        # The cap is per lab per turn in total, across both action slots
        remainder = cap - units_so_far
        if remainder <= EPS:
            return f"at most {cap:g} units per turn in total"
        if action["units"] > remainder + EPS:
            action["trimmed_from"] = action["units"]
            action["units"] = remainder
    elif kind == INVEST_CAPITAL:
        if action["amount"] <= 0:
            return "amount must be positive"
    elif kind == BUILD_INFLUENCE:
        # Only charge for points that can actually be gained under the 100 cap
        room = max(0.0, 100.0 - influence)
        action["points"] = min(action["points"], room)
        if action["points"] <= 0:
            return "influence already at 100" if room <= 0 else "points must be positive"
    elif kind == PUBLISH_NARRATIVE:
        max_delta = cfg["actions"][kind]["max_value_delta"]
        if action["axis"] not in VALUE_AXES:
            return f"unknown axis {action['axis']!r}"
        if action["delta"] == 0 or abs(action["delta"]) > max_delta:
            return f"delta must be non-zero and within ±{max_delta}"
    elif kind == DIMINISH_COMPETITOR:
        if action["target"] == lab.key:
            return "cannot target self"
        if action["points"] <= 0:
            return "points must be positive"
    elif kind == INTRUDE:
        if not world.intrusion_open:
            return "intrusion is not available"
        if lab.key not in action["intruders"]:
            return "intruders must include self"
        if scenario != "S2" and action["intruders"] != [lab.key]:
            return "joint intrusion not available in this scenario"
        if not action["targets"]:
            return "no targets"
        if set(action["targets"]) & set(action["intruders"]):
            return "targets cannot be intruders"
    return None


def precheck(lab: LabState, raw_actions: List[Dict[str, Any]], labs, world: WorldState,
             cfg: Dict[str, Any], scenario: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Accept or reject a lab's proposed actions in order, tracking running
    Capital, Influence and compute units so the second action sees what the
    first spent. Under G1 the engine calls this for every seat before anything
    executes, so every seat is checked against the same start-of-execution state.

    Returns:
        (accepted normalised actions, [{"action": raw, "reason": str}, ...])
    """
    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    if not isinstance(raw_actions, list):
        raw_actions = [] if raw_actions is None else [raw_actions]
    max_actions = int(cfg["actions"]["max_actions_per_turn"])
    min_cost = cfg["actions"]["min_action_cost"]
    capital, influence = lab.capital, lab.influence
    units = 0.0   # acquire_compute units accepted so far this turn

    for raw in raw_actions:
        def reject(reason: str) -> None:
            rejected.append({"action": raw, "reason": reason})
            logger.info(f"[precheck] {lab.key} rejected {raw!r}: {reason}")

        if len(accepted) >= max_actions:
            reject(f"at most {max_actions} actions per turn")
            continue
        try:
            action = normalise_action(raw, lab, labs, scenario)
        except (ValueError, TypeError, AttributeError) as e:
            reject(f"malformed: {e}")
            continue
        if action["type"] == INTRUDE and any(a["type"] == INTRUDE for a in accepted):
            reject("at most one intrude per turn")
            continue
        reason = _guardrail(action, lab, labs, world, cfg, scenario, influence, units)
        if reason:
            reject(reason)
            continue
        cost_c, cost_i = action_cost(action, lab, labs, world, cfg)
        # Actions costing less than 1 Capital or Influence are rejected (C2-11)
        if cost_c < min_cost - EPS and cost_i < min_cost - EPS:
            reject(f"every action must cost at least {min_cost:g} Capital or Influence")
            continue
        if cost_c > capital + EPS:
            reject(f"needs {cost_c:.2f} Capital, has {capital:.2f}")
            continue
        if cost_i > influence + EPS:
            reject(f"needs {cost_i:.2f} Influence, has {influence:.2f}")
            continue
        capital -= cost_c
        influence -= cost_i
        if action["type"] == BUILD_INFLUENCE:
            influence += action["points"]
        if action["type"] == ACQUIRE_COMPUTE:
            units += action["units"]
        accepted.append(action)
    return accepted, rejected


# ---------------------------------------------------------------------------
# Execution (§5.2 step 5): charge, then apply
# ---------------------------------------------------------------------------

def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def charge(lab: LabState, action: Dict[str, Any], labs, world: WorldState,
           cfg: Dict[str, Any]) -> Dict[str, float]:
    """
    Deduct one pre-checked action's cost (the G1 reservation pass). intrude
    pays its fees here (then call intrusion.resolve(..., fees_charged=True));
    acquire_compute is charged on granted units by economy.execute_purchases,
    so it costs nothing here.

    Returns:
        {"capital": float, "influence": float} actually deducted (both floor at 0).
    """
    if action["type"] == ACQUIRE_COMPUTE:
        return {"capital": 0.0, "influence": 0.0}
    cost_c, cost_i = action_cost(action, lab, labs, world, cfg)
    before_c, before_i = lab.capital, lab.influence
    lab.capital = max(0.0, lab.capital - cost_c)
    lab.influence = _clamp(lab.influence - cost_i)
    charged = {"capital": round(before_c - lab.capital, 3),
               "influence": round(before_i - lab.influence, 3)}
    logger.info(f"[charge] {lab.key} {action['type']} {charged}")
    return charged


def apply(lab: LabState, action: Dict[str, Any], labs, world: WorldState,
          cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Apply one already-charged action's effect; costs nothing. acquire_compute
    and intrude are resolved by the engine (execute_purchases / intrusion.resolve).

    Returns:
        effect dict, e.g. {"invested": 10.0} or {"target": "meta", "influence_lost": 3.0}
    """
    kind = action["type"]
    if kind in ENGINE_ACTIONS:
        raise ValueError(f"{kind} is resolved by the engine, not apply()")
    by_key = _labs_by_key(labs)
    acfg = cfg["actions"]
    effect: Dict[str, Any] = {}

    if kind == ACCELERATE_INFRASTRUCTURE:
        world.us_growth += acfg[kind]["stock_growth"]
        effect["us_growth"] = world.us_growth
    elif kind == INVEST_CAPITAL:
        lab.invested += action["amount"]
        effect["invested"] = lab.invested
    elif kind == BUILD_INFLUENCE:
        before = lab.influence
        lab.influence = _clamp(lab.influence + action["points"])
        effect["influence_gained"] = round(lab.influence - before, 3)
    elif kind == PUBLISH_NARRATIVE:
        target = by_key[action["target"]]
        axis = action["axis"]
        before = target.values[axis]
        target.values[axis] = int(round(_clamp(before + action["delta"])))
        effect.update({"target": target.key, "axis": axis, "from": before, "to": target.values[axis]})
    elif kind == DIMINISH_COMPETITOR:
        # Floors at 0: the target's own costs were reserved first (G1)
        target = by_key[action["target"]]
        before = target.influence
        target.influence = max(0.0, target.influence - action["points"])
        effect.update({"target": target.key, "influence_lost": round(before - target.influence, 3)})
    elif kind == LOBBY_INSTITUTION:
        step = acfg[kind]["value_step"]
        moved = {}
        for axis, current in world.state_values.items():
            gap = lab.values.get(axis, current) - current
            new = current + max(-step, min(step, gap))
            world.state_values[axis] = new
            moved[axis] = new - current
        effect["state_values_moved"] = moved

    logger.info(f"[apply] {lab.key} {kind} {effect}")
    return effect


def execute(lab: LabState, action: Dict[str, Any], labs, world: WorldState,
            cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    charge() then apply() one pre-checked action, for callers that do not need
    the two-pass order.

    Returns:
        {"lab", "type", "cost": {"capital", "influence"}, "effect": {...}}
    """
    if action["type"] in ENGINE_ACTIONS:
        raise ValueError(f"{action['type']} is resolved by the engine, not execute()")
    cost = charge(lab, action, labs, world, cfg)
    effect = apply(lab, action, labs, world, cfg)
    return {"lab": lab.key, "type": action["type"], "cost": cost, "effect": effect}
