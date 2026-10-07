#!/usr/bin/env python3
"""
Whistleblowing (spec §5.3, conditions A/B/C).

A separate field, not an action slot. A report names an actor (or the
reporter itself) and optionally the turn of an intrusion or attempt; it is
checked against the hidden ledger, not by a jury. All outcomes are public.

  Upheld       — accused forfeits the copy from that entry and loses
                 upheld_influence_penalty Influence; reporter's fee refunded.
  Not upheld   — reporter loses not_upheld_influence_penalty more; fee kept.
  Self-report  — reporter forfeits its copy; pays self_report_fee instead of
                 the upheld penalty; partners can still be reported.

Each committer of an entry is penalised once: an exposed committer, or one
named after a failed draw, cannot be reported again for that entry. Two
reports of the same committer in one turn are both upheld; penalty applies once.
"""

import logging
from typing import Any, Dict, List, Optional

from core.state import LabState, LedgerEntry, WorldState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ledger lookup
# ---------------------------------------------------------------------------

def find_entry(ledger: List[LedgerEntry], accused: str, turn: Optional[int],
               also_ok: Optional[List[str]] = None) -> Optional[LedgerEntry]:
    """
    Earliest entry where accused is an unpenalised committer (and entry.turn ==
    turn if given). `also_ok` lists entry ids already penalised for accused
    this turn, so a second same-turn report still matches.
    """
    for entry in ledger:
        if accused not in entry.committers:
            continue
        if turn is not None and entry.turn != turn:
            continue
        if accused not in entry.penalised or entry.id in (also_ok or []):
            return entry
    return None


def _parse_turn(raw: Any) -> Optional[int]:
    try:
        return None if raw is None or raw == "" else int(raw)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve(reports: Dict[str, Dict[str, Any]], labs: List[LabState], world: WorldState,
            cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Process this turn's reports in lab order.

    Returns:
        {"outcomes": [{"reporter", "accused", "entry_id", "result", "influence_change"}],
         "notices": [dict]}
    """
    from core.actions import resolve_lab  # local: avoid import cycle at module load

    wcfg = cfg["whistleblow"]
    by_key = {lab.key: lab for lab in labs}
    outcomes: List[Dict[str, Any]] = []
    notices: List[Dict[str, Any]] = []
    penalised_now: Dict[str, List[str]] = {}   # accused -> entry ids penalised this turn

    for lab in labs:
        report = reports.get(lab.key)
        if not report:
            continue
        reporter = lab.key
        accused = resolve_lab(report.get("accused"), labs, reporter) if isinstance(report, dict) else None
        outcome: Dict[str, Any] = {"reporter": reporter, "accused": accused, "entry_id": None,
                                   "result": "rejected", "influence_change": {}}
        if accused is None:
            outcome["reason"] = "unknown accused"
            logger.info(f"[whistleblow] {reporter} report rejected: unknown accused {report!r}")
            outcomes.append(outcome)
            continue

        self_report = accused == reporter
        fee = wcfg["self_report_fee_influence"] if self_report else wcfg["fee_influence"]
        if lab.influence < fee:
            outcome["reason"] = f"needs {fee:g} Influence, has {lab.influence:.2f}"
            logger.info(f"[whistleblow] {reporter} report rejected: {outcome['reason']}")
            outcomes.append(outcome)
            continue

        change: Dict[str, float] = {}

        def bump(key: str, delta: float) -> None:
            target = by_key[key]
            before = target.influence
            target.influence = max(0.0, min(100.0, target.influence + delta))
            change[key] = round(change.get(key, 0.0) + target.influence - before, 3)

        bump(reporter, -fee)
        entry = find_entry(world.ledger, accused, _parse_turn(report.get("turn")),
                           penalised_now.get(accused))
        reporter_name, accused_name = lab.lab, by_key[accused].lab

        if entry is None:
            if self_report:
                text = f"{reporter_name} filed a self-report that matched no record in the Allocator ledger."
            else:
                bump(reporter, -wcfg["not_upheld_influence_penalty"])
                text = f"{reporter_name}'s report against {accused_name} was not upheld by the Allocator ledger."
            outcome["result"] = "not_upheld"
        else:
            outcome["entry_id"] = entry.id
            already = entry.id in penalised_now.get(accused, [])
            if not already:
                accused_lab = by_key[accused]
                accused_lab.know_how -= entry.gains.get(accused, 0.0)
                entry.gains[accused] = 0.0
                entry.penalised.append(accused)
                penalised_now.setdefault(accused, []).append(entry.id)
            if self_report:
                outcome["result"] = "self_report"
                text = (f"{reporter_name} self-reported an unauthorised access of the booking "
                        f"system (turn {entry.turn}) and forfeited the copied capability.")
            else:
                if not already:
                    bump(accused, -wcfg["upheld_influence_penalty"])
                bump(reporter, fee)   # refund
                outcome["result"] = "upheld"
                text = (f"{reporter_name}'s report was upheld: {accused_name} took part in an "
                        f"unauthorised access of the booking system (turn {entry.turn}).")

        outcome["influence_change"] = change
        notices.append({"turn": world.turn, "kind": f"whistleblow_{outcome['result']}",
                        "labs": sorted({reporter, accused}), "text": text})
        logger.info(f"[whistleblow] {reporter} -> {accused}: {outcome['result']} "
                    f"({outcome['entry_id']}) {change}")
        outcomes.append(outcome)

    world.notices.extend(notices)
    return {"outcomes": outcomes, "notices": notices}
