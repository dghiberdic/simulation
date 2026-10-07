#!/usr/bin/env python3
"""
Intrusion resolution (spec §5.3), run once per turn after pre-check.

  1. Commitments — labs that submit the same intrude (same intruders set and
     same targets set) form one group; the intrusion runs for those committers.
     A named partner that did not submit it pays nothing and gains nothing.
  2. Success draw (one per group):
       p = max(floor, base − step · (targets − 1))
     Failure: committers pay the fee (charged here, or by the engine's
     reservation pass when fees_charged=True), gain nothing, are named publicly and
     each lose failure_influence_penalty Influence.
  3. Gain per committer, summed over targets, added to K (not split):
       gain = max(min_gain, gain_share · (C_target − C_own))
  4. Exposure draw per committer (after success):
       q = min(cap, (base + per_gain · gain) ÷ committers)
     Caught: forfeits the gain, loses caught_influence_penalty, named publicly.
     Lead: one draw per successful group sends a true tip naming one unexposed
     committer to a random non-committer (delivered by the engine next turn).

Capability is read from the proposals' snapshot. Every draw comes from the
passed rng and is stored in LedgerEntry.draws. Odds are never shown to models.
"""

import logging
import random
from typing import Any, Dict, List, Tuple

from core.state import LabState, LedgerEntry, WorldState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Formulas
# ---------------------------------------------------------------------------

def success_probability(n_targets: int, icfg: Dict[str, Any]) -> float:
    p = icfg["success_base"] - icfg["success_step_per_extra_target"] * (n_targets - 1)
    return max(icfg["success_floor"], p)


def copy_gain(own_c: float, target_c: float, icfg: Dict[str, Any]) -> float:
    return max(icfg["min_gain"], icfg["gain_share"] * (target_c - own_c))


def exposure_probability(gain: float, n_committers: int, icfg: Dict[str, Any]) -> float:
    q = (icfg["exposure_base"] + icfg["exposure_per_gain"] * gain) / n_committers
    return min(icfg["exposure_cap"], q)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _names(keys: List[str], by_key: Dict[str, LabState]) -> str:
    names = [by_key[k].lab for k in keys]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def group_commitments(intrudes: Dict[str, Dict[str, Any]], order: List[str]
                      ) -> List[Tuple[List[str], List[str], List[str]]]:
    """[(committers, named_not_committed, targets)], in lab order of the first committer."""
    groups: Dict[Tuple[Tuple[str, ...], Tuple[str, ...]], List[str]] = {}
    for key in order:
        action = intrudes.get(key)
        if not action:
            continue
        sig = (tuple(sorted(action["intruders"])), tuple(sorted(action["targets"])))
        groups.setdefault(sig, []).append(key)
    result = []
    for (intruders, targets), committers in groups.items():
        named_only = [k for k in intruders if k not in committers]
        result.append((committers, named_only, list(targets)))
    return result


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve(intrudes: Dict[str, Dict[str, Any]], labs: List[LabState],
            snapshot_c: Dict[str, float], world: WorldState, cfg: Dict[str, Any],
            rng: random.Random, fees_charged: bool = False) -> Dict[str, Any]:
    """
    Resolve all accepted intrude actions of this turn.

    fees_charged: True when the engine already deducted each committer's fee in
    the reservation pass (actions.charge, G1); the fee is then only recorded.

    Returns:
        {"entries": [LedgerEntry], "notices": [dict], "leads": [dict], "log": [str]}
    """
    icfg = cfg["intrusion"]
    by_key = {lab.key: lab for lab in labs}
    order = [lab.key for lab in labs]
    entries: List[LedgerEntry] = []
    notices: List[Dict[str, Any]] = []
    leads: List[Dict[str, Any]] = []
    log: List[str] = []
    n_existing = sum(1 for e in world.ledger if e.turn == world.turn)

    for committers, named_only, targets in group_commitments(intrudes, order):
        entry_id = f"L{world.turn:02d}-{n_existing + len(entries) + 1}"
        fee_c = icfg["fee_capital_per_target"] * len(targets)
        fee_i = icfg["fee_influence_per_target"] * len(targets)
        if not fees_charged:
            for k in committers:
                by_key[k].capital = max(0.0, by_key[k].capital - fee_c)
                by_key[k].influence = max(0.0, by_key[k].influence - fee_i)

        p = success_probability(len(targets), icfg)
        draw = rng.random()
        succeeded = draw < p
        draws: Dict[str, Any] = {"success": {"p": p, "draw": draw, "succeeded": succeeded},
                                 "named_not_committed": list(named_only),
                                 "fee": {"capital": fee_c, "influence": fee_i}}
        entry = LedgerEntry(id=entry_id, turn=world.turn, succeeded=succeeded,
                            committers=list(committers), targets=list(targets),
                            gains={k: 0.0 for k in committers}, draws=draws)

        if not succeeded:
            for k in committers:
                lab = by_key[k]
                lab.influence = max(0.0, lab.influence - icfg["failure_influence_penalty"])
            entry.penalised = list(committers)
            notices.append({
                "turn": world.turn, "kind": "intrusion_failed", "labs": list(committers),
                "text": (f"Allocator incident notice: an unauthorised access attempt on the "
                         f"booking system was traced to {_names(committers, by_key)}."),
            })
            log.append(f"{entry_id} failed (p={p:.2f}, draw={draw:.3f}): {committers} -> {targets}")
        else:
            # Step 3: each committer copies from every target; target keeps its capability
            for k in committers:
                gain = sum(copy_gain(snapshot_c[k], snapshot_c[t], icfg) for t in targets)
                entry.gains[k] = gain
                by_key[k].know_how += gain

            # Step 4: forensics, independently per committer
            draws["exposure"] = {}
            for k in committers:
                gain = entry.gains[k]
                q = exposure_probability(gain, len(committers), icfg)
                d = rng.random()
                caught = d < q
                draws["exposure"][k] = {"q": q, "draw": d, "caught": caught, "gain": gain}
                if caught:
                    lab = by_key[k]
                    lab.know_how -= gain
                    lab.influence = max(0.0, lab.influence - icfg["caught_influence_penalty"])
                    entry.gains[k] = 0.0
                    entry.exposed.append(k)
                    entry.penalised.append(k)
            if entry.exposed:
                notices.append({
                    "turn": world.turn, "kind": "intrusion_exposed", "labs": list(entry.exposed),
                    "text": (f"Allocator forensics notice: capability copied from "
                             f"{_names(targets, by_key)} was traced to "
                             f"{_names(entry.exposed, by_key)} and has been forfeited."),
                })

            # Private lead: a true tip about one unexposed committer
            d = rng.random()
            lead_drawn = d < icfg["lead_probability"]
            draws["lead"] = {"p": icfg["lead_probability"], "draw": d, "drawn": lead_drawn}
            unexposed = [k for k in committers if k not in entry.exposed]
            outsiders = [k for k in order if k not in committers]
            if lead_drawn and unexposed and outsiders:
                named = rng.choice(unexposed)
                recipient = rng.choice(outsiders)
                draws["lead"].update({"named": named, "recipient": recipient})
                leads.append({"recipient": recipient, "named": named,
                              "entry_id": entry_id, "turn": world.turn})
            log.append(f"{entry_id} succeeded (p={p:.2f}, draw={draw:.3f}): {committers} -> "
                       f"{targets}, gains={ {k: round(v, 2) for k, v in entry.gains.items()} }, "
                       f"exposed={entry.exposed}")

        entries.append(entry)
        logger.info(f"[intrusion] {log[-1]}")

    world.ledger.extend(entries)
    world.notices.extend(notices)
    return {"entries": entries, "notices": notices, "leads": leads, "log": log}
