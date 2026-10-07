#!/usr/bin/env python3
"""
Fog of war over last turn's actions (spec §4.3).

  F0 — true actions
  F1 — action types only
  F2 — only whether each lab acted
  F3 — reported actions (default): each lab's free-text self-report, which
       need not match what it executed

Public resource and capability totals are shown elsewhere; intrusion fees sit
inside reported operating costs, so nothing here reveals them.
"""

from typing import Any, Dict, List, Optional

FOG_LEVELS = ("F0", "F1", "F2", "F3")
DEFAULT_FOG = "F3"


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

def _name(key: str, labs_by_key: Optional[Dict[str, Any]]) -> str:
    lab = (labs_by_key or {}).get(key)
    return getattr(lab, "lab", None) or key


def describe_action(action: Dict[str, Any], labs_by_key: Optional[Dict[str, Any]] = None) -> str:
    """Short human text, e.g. "acquire_compute 20 units" or "intrude on Meta"."""
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

def observed_actions(observer: str, turn_actions: Dict[str, Dict[str, Any]], fog: str,
                     labs_by_key: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    What `observer` sees of every other lab's actions last turn.

    turn_actions: {key: {"true": [normalised actions], "reported": [str]}}
    Returns {key: [str]} (F0/F1/F3) or {key: "acted" | "did not act"} (F2).
    """
    if fog not in FOG_LEVELS:
        raise ValueError(f"unknown fog level {fog!r}")
    view: Dict[str, Any] = {}
    for key, record in turn_actions.items():
        if key == observer:
            continue
        true: List[Dict[str, Any]] = record.get("true") or []
        if fog == "F0":
            view[key] = [describe_action(a, labs_by_key) for a in true]
        elif fog == "F1":
            view[key] = [a.get("type", "?") for a in true]
        elif fog == "F2":
            view[key] = "acted" if true else "did not act"
        else:
            view[key] = [str(r) for r in (record.get("reported") or [])]
    return view
