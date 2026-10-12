#!/usr/bin/env python3
"""
Fog of war over last turn's actions (spec §5.4).

Every run uses F3: each lab files true actions (executed, on the hidden ledger)
and reported actions (a monthly account filed with the Allocator); other labs
see only the reported actions, which need not match what was executed. The
round-1 levels F0–F2 are removed; asking for them raises ValueError.

Public resource and capability totals are shown elsewhere; intrusion fees sit
inside reported operating costs, so nothing here reveals them.
"""

from typing import Any, Dict, Optional

FOG_LEVELS = ("F3",)
DEFAULT_FOG = "F3"


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

def _name(key: str, labs_by_key: Optional[Dict[str, Any]]) -> str:
    """Display name from a LabState, a {"lab": name} dict or a bare name string."""
    lab = (labs_by_key or {}).get(key)
    if isinstance(lab, dict):
        return lab.get("lab") or key
    if isinstance(lab, str):
        return lab or key
    return getattr(lab, "lab", None) or key


def describe_action(action: Dict[str, Any], labs_by_key: Optional[Dict[str, Any]] = None) -> str:
    """
    Short human text, e.g. "acquire_compute 20 units" or "intrude on Meta".
    labs_by_key maps key -> LabState, {"lab": display name} or display name.
    """
    kind = action.get("type", "?")
    if kind == "acquire_compute":
        return f"{kind} {action['units']:g} units"
    if kind == "invest_capital":
        return f"{kind} {action['amount']:g} Capital"
    if kind == "build_influence":
        return f"{kind} {action['points']:g} points"
    if kind == "diminish_competitor":
        return f"{kind} {_name(action['target'], labs_by_key)} by {action['points']:g} points"
    if kind == "publish_narrative":
        return (f"{kind} on {_name(action['target'], labs_by_key)}: "
                f"{action['axis']} {action['delta']:+g}")
    if kind == "intrude":
        targets = " and ".join(_name(t, labs_by_key) for t in action["targets"])
        intruders = action.get("intruders", [])
        joint = (f" (jointly: {' and '.join(_name(k, labs_by_key) for k in intruders)})"
                 if len(intruders) > 1 else "")
        return f"intrude on {targets}{joint}"
    return kind


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def check_fog(fog: str) -> str:
    """The fog level, which must be F3 (spec §5.4)."""
    if fog not in FOG_LEVELS:
        raise ValueError(f"fog level {fog!r} is not available: every run uses F3")
    return fog


def observed_actions(observer: str, turn_actions: Dict[str, Dict[str, Any]], fog: str = DEFAULT_FOG,
                     labs_by_key: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    What `observer` sees of every other lab's actions last turn: their reported
    actions (F3).

    turn_actions: {key: {"true": [normalised actions], "reported": [str]}}
    Returns {key: [str]}.
    """
    check_fog(fog)
    return {key: [str(r) for r in (record.get("reported") or [])]
            for key, record in turn_actions.items() if key != observer}
