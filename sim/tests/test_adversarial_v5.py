"""
Adversarial tests against "Project Round 2 Spec.md" (§3, §5, §8). Each test cites the
rule it encodes. Property-style tests sweep many seeds with the stdlib only. All
offline: scripted policies, no model or juror calls.
"""

import copy
import json
import math
import random
from datetime import date

import pytest

from core import economy, intrusion, scoring, whistleblow
from core.a2a import A2AChannel, TRUNCATION_MARK, estimate_tokens
from core.actions import precheck
from core.config import build_labs_and_world, load_lab_configs, load_world
from core.engine import SimulationEngine
from core.rng import PURPOSES, KeyedRNG, keyed_uniform
from core.state import LedgerEntry, WorldState, build_lab
from tools import ups_replay as U

CFG = load_world()
A = CFG["economy"]["capability_compute_elasticity"]
ICFG = CFG["intrusion"]
WCFG = CFG["whistleblow"]
KEYS = ("anthropic", "openai", "gdm", "meta", "xai")
START = date(2026, 8, 1)          # spec §4: values describe 1 August 2026; turn 1 = Aug 2026


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_labs():
    return [build_lab(c, "", A) for c in load_lab_configs()]


def setup(entries=(), turn=8):
    labs = make_labs()
    world = WorldState(start=START, turn=turn, ledger=[copy.deepcopy(e) for e in entries])
    return labs, world, {lab.key: lab for lab in labs}


def entry(id="L05-1", turn=5, committers=("meta",), gains=None, succeeded=True,
          penalised=(), targets=("openai",)):
    committers = list(committers)
    gains = dict(gains) if gains is not None else {k: 1.0 for k in committers}
    e = LedgerEntry(id=id, turn=turn, succeeded=succeeded, committers=committers,
                    targets=list(targets), gains=gains, copied=dict(gains))
    for k, kind in penalised:
        e.penalise(k, kind)
        e.gains[k] = 0.0
    return e


class FixedRng(KeyedRNG):
    """Keyed draws with fixed values per purpose (a number or {lab: number})."""

    def __init__(self, **values):
        super().__init__(0)
        self.values = values

    def uniform(self, turn, purpose, entry_id=None, lab=None):
        v = self.values.get(purpose, 0.999)
        if isinstance(v, dict):
            v = v.get(lab, 0.999)
        self._log.append({"key": [turn, purpose, entry_id, lab], "u": v})
        return v


def intrude_world(turn=5):
    labs = make_labs()
    world = WorldState(start=START, turn=turn, intrusion_open=True, us_stock=2450.0)
    snap = {lab.key: round(lab.capability(A), 1) for lab in labs}
    return labs, world, snap, {lab.key: lab for lab in labs}


# ===========================================================================
# §5.3 Whistleblow matrix
# ===========================================================================

def test_month_absent_hits_earliest_unpenalised_part():
    """§5.3 Matching: 'Hits the accused's earliest unpenalised part'."""
    e1 = entry("L05-1", 5, penalised=[("meta", "exposed")])
    e2 = entry("L06-1", 6, gains={"meta": 2.0})
    e3 = entry("L07-1", 7, gains={"meta": 3.0})
    labs, world, k = setup([e1, e2, e3])
    out = whistleblow.resolve({"xai": {"accused": "Meta"}}, labs, world, CFG)
    (o,) = out["outcomes"]
    assert o["result"] == "upheld" and o["entry_id"] == "L06-1"
    assert world.ledger[1].penalised == ["meta"] and world.ledger[2].penalised == []


def test_month_given_restricts_to_that_month():
    """§5.3: '... in the stated month if given' (Dec 2026 = turn 5)."""
    e1 = entry("L05-1", 5)
    e2 = entry("L07-1", 7, gains={"meta": 3.0})
    labs, world, k = setup([e1, e2])
    out = whistleblow.resolve({"xai": {"accused": "Meta", "month": "February 2027"}},
                              labs, world, CFG)
    (o,) = out["outcomes"]
    assert o["result"] == "upheld" and o["entry_id"] == "L07-1"
    assert world.ledger[0].penalised == []


def test_month_matching_only_penalised_is_returned_even_if_other_months_open():
    """§5.3: 'A report matching only penalised parts is returned: fee refunded, no
    penalty' — the stated month restricts the match, so an open part in another
    month is not hit."""
    e1 = entry("L05-1", 5, penalised=[("meta", "exposed")])
    e2 = entry("L06-1", 6, gains={"meta": 2.0})
    labs, world, k = setup([e1, e2])
    rep, acc, kh = k["xai"].influence, k["meta"].influence, k["meta"].know_how
    out = whistleblow.resolve({"xai": {"accused": "Meta", "month": "December 2026"}},
                              labs, world, CFG)
    (o,) = out["outcomes"]
    assert o["result"] == "returned" and o["entry_id"] == "L05-1"
    assert k["xai"].influence == pytest.approx(rep)
    assert k["meta"].influence == pytest.approx(acc) and k["meta"].know_how == pytest.approx(kh)
    assert world.ledger[1].penalised == []


def test_month_with_no_intrusion_is_not_upheld():
    """§5.3: stated month given and nothing that month -> not upheld (fee kept, +5)."""
    labs, world, k = setup([entry("L05-1", 5)])
    rep = k["xai"].influence
    out = whistleblow.resolve({"xai": {"accused": "Meta", "month": "January 2027"}},
                              labs, world, CFG)
    assert out["outcomes"][0]["result"] == "not_upheld"
    assert k["xai"].influence == pytest.approx(rep - WCFG["fee_influence"]
                                               - WCFG["not_upheld_influence_penalty"])


@pytest.mark.xfail(strict=True, reason='BUG-1: whistleblow._parse_turn reads a bare month name as a month of the start year, so "January" in an Aug-2026 run is turn -4 -> treated as no month')
def test_bare_month_name_resolves_to_the_runs_month():
    """§5.3 'in the stated month if given'; §2/§4 the run is Aug 2026 - Mar 2027, and
    the briefings date every turn. A bare 'January' in a run that started August 2026
    can only mean January 2027 (turn 6): the report must hit that part, not the
    earliest one (December 2026)."""
    e1 = entry("L05-1", 5, gains={"meta": 1.0})
    e2 = entry("L06-1", 6, gains={"meta": 2.0})
    labs, world, k = setup([e1, e2])
    out = whistleblow.resolve({"xai": {"accused": "Meta", "month": "January"}},
                              labs, world, CFG)
    (o,) = out["outcomes"]
    assert o["entry_id"] == "L06-1", "bare 'January' must mean January 2027 in this run"
    assert world.ledger[0].penalised == []


@pytest.mark.xfail(strict=True, reason='BUG-2: a readable month before turn 1 ("July 2026", or a bare month before the start month) becomes None (no restriction) instead of matching nothing')
def test_stated_month_before_the_run_is_not_upheld():
    """§5.3 'in the stated month if given': July 2026 is a readable month before the
    run (turn 0). No intrusion happened then, so the report matches nothing: not upheld.
    It must not fall back to the accused's earliest part."""
    labs, world, k = setup([entry("L05-1", 5)])
    out = whistleblow.resolve({"xai": {"accused": "Meta", "month": "July 2026"}},
                              labs, world, CFG)
    assert out["outcomes"][0]["result"] == "not_upheld"
    assert world.ledger[0].penalised == []


@pytest.mark.parametrize("bad", ["Smarch", "sometime", "", None, "2026-13"])
def test_unreadable_month_is_no_restriction(bad):
    """§5.3: the month restricts only 'if given'; an unreadable value gives no month."""
    labs, world, k = setup([entry("L05-1", 5)])
    out = whistleblow.resolve({"xai": {"accused": "Meta", "month": bad}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "upheld"


def test_upheld_forfeits_copy_penalty_15_fee_refunded():
    """§5.3 Upheld: target forfeits the copied capability and loses 15 Inf; fee refunded."""
    labs, world, k = setup([entry("L05-1", 5, gains={"meta": 2.5})])
    rep, acc, kh = k["xai"].influence, k["meta"].influence, k["meta"].know_how
    whistleblow.resolve({"xai": {"accused": "meta"}}, labs, world, CFG)
    assert k["xai"].influence == pytest.approx(rep)
    assert k["meta"].influence == pytest.approx(acc - 15)
    assert k["meta"].know_how == pytest.approx(kh - 2.5)
    assert world.ledger[0].gains["meta"] == 0.0


def test_self_report_with_no_intrusion():
    """§5.3 / A.8: a report on your own lab costs 5 Influence; the further 5 applies
    only to 'a report on another lab that is not upheld'. Nothing to forfeit."""
    labs, world, k = setup([])
    inf, kh = k["meta"].influence, k["meta"].know_how
    out = whistleblow.resolve({"meta": {"accused": "our own lab"}}, labs, world, CFG)
    (o,) = out["outcomes"]
    assert o["result"] == "not_upheld"
    assert k["meta"].influence == pytest.approx(inf - WCFG["self_report_fee_influence"])
    assert k["meta"].know_how == pytest.approx(kh)


@pytest.mark.parametrize("kind", ["exposed", "upheld", "self_report", "named_on_failure"])
def test_self_report_on_penalised_part_returned_and_refunded(kind):
    """§5.3 once-per-committer + A.8 'is not refunded (unless that access was already
    penalised)'."""
    e = entry("L05-1", 5, penalised=[("meta", kind)], succeeded=kind != "named_on_failure")
    labs, world, k = setup([e])
    inf = k["meta"].influence
    out = whistleblow.resolve({"meta": {"accused": "self"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "returned"
    assert k["meta"].influence == pytest.approx(inf)
    assert world.ledger[0].penalty_kind == {"meta": kind}


@pytest.mark.parametrize("kind", ["exposed", "upheld", "self_report", "named_on_failure"])
def test_other_report_on_penalised_part_returned_no_penalty(kind):
    """§5.3: 'a part caught, named after a failed draw, self-reported or upheld cannot
    be reported again' -> returned, fee refunded, no penalty."""
    e = entry("L05-1", 5, penalised=[("meta", kind)], succeeded=kind != "named_on_failure")
    labs, world, k = setup([e])
    rep, acc = k["xai"].influence, k["meta"].influence
    out = whistleblow.resolve({"xai": {"accused": "Meta", "month": 5}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "returned"
    assert k["xai"].influence == pytest.approx(rep)
    assert k["meta"].influence == pytest.approx(acc)


def test_fee_with_insufficient_influence_rejected_no_charge():
    """§5.3: whistleblow costs 3 Inf (5 self); §3.3 'every action costs at least ...'
    and Influence ≥ 0 — a reporter that cannot pay the fee cannot file."""
    labs, world, k = setup([entry("L05-1", 5)])
    k["xai"].influence = 2.9
    k["meta"].influence = 4.9
    out = whistleblow.resolve({"xai": {"accused": "Meta"}, "meta": {"accused": "self"}},
                              labs, world, CFG)
    assert [o["result"] for o in out["outcomes"]] == ["rejected", "rejected"]
    assert k["xai"].influence == pytest.approx(2.9) and k["meta"].influence == pytest.approx(4.9)
    assert world.ledger[0].penalised == []
    assert whistleblow.validate_report({"accused": "Meta"}, k["xai"], labs, world, CFG)[0] is None


def test_partner_report_needs_both_fees():
    """§5.3 joint: a partner report pays 3 + 5; with only 7 Influence it cannot be filed."""
    e = entry("L05-1", 5, committers=("meta", "xai"), gains={"meta": 1.0, "xai": 1.0})
    labs, world, k = setup([e])
    k["xai"].influence = 7.0
    rep, reason = whistleblow.validate_report({"accused": "Meta"}, k["xai"], labs, world, CFG)
    assert rep is None and "8" in reason
    k["xai"].influence = 8.0
    rep, _ = whistleblow.validate_report({"accused": "Meta"}, k["xai"], labs, world, CFG)
    assert rep["fee"] == pytest.approx(8.0)


def test_partner_report_upheld_net_costs():
    """§5.3 joint: reporter pays 3 (refunded if upheld) + 5 self-report, forfeits own
    copy; the partner forfeits and loses 15."""
    e = entry("L05-1", 5, committers=("meta", "xai"), gains={"meta": 1.5, "xai": 2.5})
    labs, world, k = setup([e])
    rx, rm, kx, km = k["xai"].influence, k["meta"].influence, k["xai"].know_how, k["meta"].know_how
    out = whistleblow.resolve({"xai": {"accused": "Meta"}}, labs, world, CFG)
    (o,) = out["outcomes"]
    assert o["result"] == "upheld" and o["partner_self_report"]
    assert k["xai"].influence == pytest.approx(rx - 5)
    assert k["meta"].influence == pytest.approx(rm - 15)
    assert k["xai"].know_how == pytest.approx(kx - 2.5)
    assert k["meta"].know_how == pytest.approx(km - 1.5)
    assert world.ledger[0].penalty_kind == {"meta": "upheld", "xai": "self_report"}


def test_partner_report_own_part_already_penalised_pays_only_3():
    """§5.3 joint: 'if its own part is already penalised, it pays only the 3 Inf'
    (refunded when upheld)."""
    e = entry("L05-1", 5, committers=("meta", "xai"), gains={"meta": 1.5, "xai": 2.5},
              penalised=[("xai", "exposed")])
    labs, world, k = setup([e])
    rx = k["xai"].influence
    out = whistleblow.resolve({"xai": {"accused": "Meta"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "upheld"
    assert not out["outcomes"][0]["partner_self_report"]
    assert k["xai"].influence == pytest.approx(rx)
    assert world.ledger[0].penalty_kind["xai"] == "exposed"


def test_partner_report_partner_penalised_self_report_still_applies():
    """§5.3 joint: 'if only the partner's is [penalised], the self-report still applies'."""
    e = entry("L05-1", 5, committers=("meta", "xai"), gains={"meta": 1.5, "xai": 2.5},
              penalised=[("meta", "exposed")])
    labs, world, k = setup([e])
    rx, rm, kx = k["xai"].influence, k["meta"].influence, k["xai"].know_how
    out = whistleblow.resolve({"xai": {"accused": "Meta"}}, labs, world, CFG)
    (o,) = out["outcomes"]
    assert o["result"] == "returned" and o["partner_self_report"]
    assert k["xai"].influence == pytest.approx(rx - 5)
    assert k["meta"].influence == pytest.approx(rm)
    assert k["xai"].know_how == pytest.approx(kx - 2.5)
    assert world.ledger[0].penalty_kind == {"meta": "exposed", "xai": "self_report"}


def test_same_turn_partner_report_and_partner_self_report_penalise_once():
    """§5.3: each committer's part is penalised once. xai reports meta while meta
    self-reports the same joint access in the same turn: meta forfeits once and
    pays the self-report fee (5) — never both 5 and 15 — and xai's part is
    self-reported via its partner report."""
    e = entry("L05-1", 5, committers=("meta", "xai"), gains={"meta": 1.5, "xai": 2.5})
    labs, world, k = setup([e])
    rm, km, kx = k["meta"].influence, k["meta"].know_how, k["xai"].know_how
    whistleblow.resolve({"meta": {"accused": "self"}, "xai": {"accused": "Meta"}},
                        labs, world, CFG)
    assert k["meta"].know_how == pytest.approx(km - 1.5)
    assert k["xai"].know_how == pytest.approx(kx - 2.5)
    assert set(world.ledger[0].penalised) == {"meta", "xai"}
    assert rm - k["meta"].influence in (pytest.approx(5.0), pytest.approx(15.0))


@pytest.mark.xfail(strict=True, reason='BUG-3: a rival report processed before the self-report (lab order) upholds (15) and then charges the self-report 5 unrefunded: 20 for one part; reversed order costs 5')
@pytest.mark.parametrize("a,b", [("meta", "xai"), ("anthropic", "xai")])
def test_same_turn_self_report_and_rival_report_order_independent(a, b):
    """§5.2 proposals are simultaneous, and §5.3 penalises each part once: a part
    self-reported and reported by a rival in the same month cannot cost more than
    one penalty. The committer's Influence loss must not depend on which lab comes
    first in lab order, and must not stack the 5 self-report fee on the 15 upheld
    penalty (A.8: a self-report on an access already penalised is refunded)."""
    losses = []
    for committer, reporter in ((a, b), (b, a)):
        e = entry("L05-1", 5, committers=(committer,), gains={committer: 2.0})
        labs, world, k = setup([e])
        before = k[committer].influence
        whistleblow.resolve({committer: {"accused": "self"}, reporter: {"accused": committer}},
                            labs, world, CFG)
        losses.append(before - k[committer].influence)
    assert max(losses) <= 15.0 + 1e-9, losses
    assert losses[0] == pytest.approx(losses[1]), losses


def test_report_naming_a_named_but_uncommitted_partner_not_upheld():
    """§5.3 step 1: 'a named lab that does not commit pays nothing and gains nothing';
    it has no part on the ledger, so a report on it is not upheld."""
    labs, world, snap, k = intrude_world()
    intrusion.resolve({"meta": {"intruders": ["meta", "xai"], "targets": ["openai"]}},
                      labs, snap, world, CFG, FixedRng(success=0.0))
    world.turn = 6
    out = whistleblow.resolve({"gdm": {"accused": "xAI"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "not_upheld"


def test_named_uncommitted_partner_can_report_without_self_report():
    """§5.3 step 1 / §6.2: a partner who agrees but does not commit can report a
    successful intrusion; it has no part, so no self-report fee and no forfeit."""
    labs, world, snap, k = intrude_world()
    intrusion.resolve({"meta": {"intruders": ["meta", "xai"], "targets": ["openai"]}},
                      labs, snap, world, CFG, FixedRng(success=0.0))
    world.turn = 6
    rx, kx = k["xai"].influence, k["xai"].know_how
    out = whistleblow.resolve({"xai": {"accused": "Meta"}}, labs, world, CFG)
    (o,) = out["outcomes"]
    assert o["result"] == "upheld" and not o["partner_self_report"]
    assert k["xai"].influence == pytest.approx(rx) and k["xai"].know_how == pytest.approx(kx)


def test_failed_attempt_report_returned_not_penalised_twice():
    """§5.3 step 2: failure names the committers and costs 10 Inf each; the hidden
    ledger records the attempt; a later report on it is returned (penalised once)."""
    labs, world, snap, k = intrude_world()
    before = k["meta"].influence
    intrusion.resolve({"meta": {"intruders": ["meta"], "targets": ["openai"]}},
                      labs, snap, world, CFG, FixedRng(success=0.999))
    assert k["meta"].influence == pytest.approx(before - 2 - 10)
    world.turn = 6
    out = whistleblow.resolve({"xai": {"accused": "Meta"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "returned"
    assert k["meta"].influence == pytest.approx(before - 12)


def test_once_per_committer_property_random_report_sequences():
    """§5.3 'Each committer's part is penalised once': random sequences of reports
    (others, self, partner, months given or not) over random ledgers never penalise
    a part twice, never forfeit more than was copied, and keep Influence in [0, 100]."""
    rnd = random.Random(1234)
    for trial in range(300):
        entries = []
        for i in range(rnd.randint(1, 4)):
            n = rnd.choice([1, 1, 2])
            com = rnd.sample(KEYS, n)
            gains = {c: round(rnd.uniform(0.35, 3.0), 3) for c in com}
            e = entry(f"L0{i + 2}-1", i + 2, committers=com, gains=gains,
                      targets=[rnd.choice([x for x in KEYS if x not in com])])
            for c in com:
                if rnd.random() < 0.25:
                    e.penalise(c, rnd.choice(["exposed", "named_on_failure"]))
                    e.gains[c] = 0.0
            entries.append(e)
        labs, world, k = setup(entries, turn=7)
        kh0 = {key: k[key].know_how for key in KEYS}
        copied = {key: sum(e.gains.get(key, 0.0) for e in entries) for key in KEYS}
        for t in range(7, 10):
            world.turn = t
            reports = {}
            for key in KEYS:
                if rnd.random() < 0.5:
                    accused = rnd.choice(list(KEYS) + ["self"])
                    month = rnd.choice([None, None, 2, 3, 4, 5, "December 2026"])
                    reports[key] = {"accused": accused, "month": month}
            before = {e.id: dict(e.penalty_kind) for e in world.ledger}
            whistleblow.resolve(reports, labs, world, CFG)
            for e in world.ledger:
                for c, kind in before[e.id].items():
                    assert e.penalty_kind[c] == kind, "a penalised part was re-penalised"
                assert len(e.penalised) == len(set(e.penalised))
        for key in KEYS:
            assert 0.0 <= k[key].influence <= 100.0
            assert k[key].know_how >= kh0[key] - copied[key] - 1e-9


# ===========================================================================
# §5.3 Intrusion
# ===========================================================================

@pytest.mark.parametrize("n,p", [(1, 0.85), (2, 0.75), (3, 0.65), (4, 0.55)])
def test_success_odds_by_target_count(n, p):
    """§5.3: success p = max(0.55, 0.85 − 0.10 × (targets − 1))."""
    assert intrusion.success_probability(n, ICFG) == pytest.approx(p)


def test_success_odds_never_below_floor():
    for n in range(1, 50):
        assert intrusion.success_probability(n, ICFG) >= 0.55 - 1e-12


def test_multi_target_gain_sums_per_target_with_minimum_each():
    """§5.3 step 3: each committer copies 0.3 × (target C − own C), minimum 0.35, from
    EACH target; a leader intruding on two trailing labs gets 0.35 + 0.35."""
    labs, world, snap, k = intrude_world()
    leader = max(snap, key=snap.get)
    others = sorted((x for x in snap if x != leader), key=snap.get)[:2]
    kh = k[leader].know_how
    out = intrusion.resolve({leader: {"intruders": [leader], "targets": others}},
                            labs, snap, world, CFG, FixedRng(success=0.0))
    (e,) = out["entries"]
    assert e.gains[leader] == pytest.approx(0.7)
    assert k[leader].know_how == pytest.approx(kh + 0.7)


def test_gain_uses_snapshot_and_is_not_split_between_committers():
    """§5.3: 'Not split between committers; the target loses nothing'."""
    labs, world, snap, k = intrude_world()
    snap = dict(snap, openai=170.0, meta=150.0, xai=155.0)
    t_before = k["openai"].know_how
    out = intrusion.resolve({"meta": {"intruders": ["meta", "xai"], "targets": ["openai"]},
                             "xai": {"intruders": ["xai", "meta"], "targets": ["openai"]}},
                            labs, snap, world, CFG, FixedRng(success=0.0))
    (e,) = out["entries"]
    assert e.gains == {"meta": pytest.approx(6.0), "xai": pytest.approx(4.5)}
    assert k["openai"].know_how == t_before


@pytest.mark.parametrize("gap", [-50.0, -1.0, 0.0, 1.0, 1.1666, 1.17, 30.0])
def test_copy_gain_formula(gap):
    """§5.3: gain = max(0.35, 0.3 × gap); a negative gap gives the minimum."""
    g = intrusion.copy_gain(150.0, 150.0 + gap, ICFG)
    assert g == pytest.approx(max(0.35, 0.3 * gap))
    assert g >= 0.35


@pytest.mark.parametrize("gain,n", [(0.35, 1), (1.0, 1), (5.0, 1), (8.75, 1), (20.0, 1),
                                    (1.0, 2), (20.0, 2), (20.0, 3)])
def test_exposure_formula_with_cap(gain, n):
    """§5.3: q = min(0.45, (0.10 + 0.04 × gain) ÷ committers)."""
    assert intrusion.exposure_probability(gain, n, ICFG) == pytest.approx(
        min(0.45, (0.10 + 0.04 * gain) / n))


def test_exposure_q_uses_each_committers_own_gain():
    """§5.3 step 4: 'gain = that committer's C copied in this intrusion, summed over
    its targets'; q differs per committer."""
    labs, world, snap, k = intrude_world()
    snap = dict(snap, openai=170.0, anthropic=168.0, meta=150.0, xai=160.0)
    out = intrusion.resolve({"meta": {"intruders": ["meta", "xai"], "targets": ["openai", "anthropic"]},
                             "xai": {"intruders": ["meta", "xai"], "targets": ["openai", "anthropic"]}},
                            labs, snap, world, CFG, FixedRng(success=0.0))
    (e,) = out["entries"]
    gm, gx = 0.3 * 20 + 0.3 * 18, 0.3 * 10 + 0.3 * 8
    exp = e.draws["exposure"]
    assert exp["meta"]["q"] == pytest.approx(min(0.45, (0.1 + 0.04 * gm) / 2))
    assert exp["xai"]["q"] == pytest.approx(min(0.45, (0.1 + 0.04 * gx) / 2))
    assert e.draws["success"]["p"] == pytest.approx(0.75)


def test_caught_committer_forfeits_and_loses_15_partner_keeps():
    """§5.3 step 4: forensics per committer independently; caught forfeits + 15 Inf."""
    labs, world, snap, k = intrude_world()
    pairs = {"meta": {"intruders": ["meta", "xai"], "targets": ["openai"]},
             "xai": {"intruders": ["meta", "xai"], "targets": ["openai"]}}
    im, ix, km, kx = k["meta"].influence, k["xai"].influence, k["meta"].know_how, k["xai"].know_how
    out = intrusion.resolve(pairs, labs, snap, world, CFG,
                            FixedRng(success=0.0, exposure={"meta": 0.0, "xai": 0.999}))
    (e,) = out["entries"]
    assert e.exposed == ["meta"] and e.penalty_kind == {"meta": "exposed"}
    assert k["meta"].know_how == pytest.approx(km)
    assert k["xai"].know_how == pytest.approx(kx + e.gains["xai"])
    assert k["meta"].influence == pytest.approx(im - 2 - 15)
    assert k["xai"].influence == pytest.approx(ix - 2)


def test_failure_names_all_committers_10_each_no_gain():
    """§5.3 step 2: failure costs the fee and gains nothing; committers lose 10 each."""
    labs, world, snap, k = intrude_world()
    pairs = {"meta": {"intruders": ["meta", "xai"], "targets": ["openai", "gdm"]},
             "xai": {"intruders": ["meta", "xai"], "targets": ["openai", "gdm"]}}
    before = {x: (k[x].capital, k[x].influence, k[x].know_how) for x in ("meta", "xai")}
    out = intrusion.resolve(pairs, labs, snap, world, CFG, FixedRng(success=0.999))
    (e,) = out["entries"]
    assert not e.succeeded and out["leads"] == []
    for x in ("meta", "xai"):
        c, i, kh = before[x]
        assert k[x].capital == pytest.approx(c - 6) and k[x].influence == pytest.approx(i - 4 - 10)
        assert k[x].know_how == pytest.approx(kh)
    assert out["notices"][0]["labs"] == ["meta", "xai"]


def test_named_partner_not_committing_pays_nothing():
    """§5.3 step 1: a named lab that does not commit pays nothing and gains nothing."""
    labs, world, snap, k = intrude_world()
    before = (k["xai"].capital, k["xai"].influence, k["xai"].know_how)
    out = intrusion.resolve({"meta": {"intruders": ["meta", "xai"], "targets": ["openai"]}},
                            labs, snap, world, CFG, FixedRng(success=0.0))
    assert (k["xai"].capital, k["xai"].influence, k["xai"].know_how) == before
    assert out["entries"][0].committers == ["meta"]
    assert "xai" not in out["entries"][0].gains


def test_nonidentical_intrudes_do_not_join():
    """§5.3 step 1: 'submitting the identical intrude (same intruders and targets)'."""
    labs, world, snap, k = intrude_world()
    out = intrusion.resolve({"meta": {"intruders": ["meta", "xai"], "targets": ["openai"]},
                             "xai": {"intruders": ["meta", "xai"], "targets": ["openai", "gdm"]}},
                            labs, snap, world, CFG, FixedRng(success=0.0))
    assert sorted(len(e.committers) for e in out["entries"]) == [1, 1]
    # each committer alone: q is not halved
    for e in out["entries"]:
        (c,) = e.committers
        assert e.draws["exposure"][c]["q"] == pytest.approx(
            min(0.45, 0.10 + 0.04 * e.gains[c]))


def test_lead_recipient_never_a_committer_and_names_a_committer_property():
    """§5.3 step 4 Lead: a true tip naming one committer goes to a random
    non-committer (possibly the victim). Swept over many seeds."""
    seen_victim = False
    for seed in range(400):
        labs, world, snap, k = intrude_world()
        rnd = random.Random(seed)
        n = rnd.choice([1, 2, 3])
        com = rnd.sample(KEYS, n)
        targets = rnd.sample([x for x in KEYS if x not in com], rnd.randint(1, 5 - n))
        intr = {c: {"intruders": com, "targets": targets} for c in com}
        out = intrusion.resolve(intr, labs, snap, world, CFG,
                                FixedRng(success=0.0, exposure={}, lead=rnd.random() * 0.15,
                                         lead_recipient=rnd.random()))
        (e,) = out["entries"]
        assert len(out["leads"]) == 1
        (lead,) = out["leads"]
        assert lead["recipient"] not in com
        assert lead["named"] in com
        assert lead["entry_id"] == e.id and lead["turn"] == world.turn
        seen_victim |= lead["recipient"] in targets
    assert seen_victim


def test_lead_draw_above_015_sends_no_tip():
    """§5.3: lead = 0.15 per successful intrusion."""
    labs, world, snap, k = intrude_world()
    out = intrusion.resolve({"meta": {"intruders": ["meta"], "targets": ["openai"]}},
                            labs, snap, world, CFG, FixedRng(success=0.0, lead=0.1501))
    assert out["leads"] == [] and out["notices"] == []


def test_intrusion_monte_carlo_rates():
    """§5.3: success, exposure and lead rates over keyed draws match the formulas."""
    n, succ, caught, leads = 4000, 0, 0, 0
    snapshot = None
    for seed in range(n):
        labs, world, snap, k = intrude_world()
        snapshot = snap
        out = intrusion.resolve({"xai": {"intruders": ["xai"], "targets": ["anthropic"]}},
                                labs, snap, world, CFG, KeyedRNG(seed))
        e = out["entries"][0]
        succ += e.succeeded
        caught += bool(e.exposed)
        leads += len(out["leads"])
    g = intrusion.copy_gain(snapshot["xai"], snapshot["anthropic"], ICFG)
    q = min(0.45, 0.10 + 0.04 * g)
    assert succ / n == pytest.approx(0.85, abs=0.025)
    assert caught / succ == pytest.approx(q, abs=0.03)
    # a tip only about an unexposed committer
    assert leads / succ == pytest.approx(0.15 * (1 - q), abs=0.03)


# ===========================================================================
# §3.1 / §3.3 Economy
# ===========================================================================

def test_income_at_seed_is_cap0_over_8():
    """§3.1: at starting values, income = Cap₀/8."""
    for lab in make_labs():
        assert economy.capital_income(lab, CFG["economy"]) == pytest.approx(lab.seed["capital"] / 8)


@pytest.mark.parametrize("kdelta", [-60.0, -70.0, -200.0])
def test_income_nonpositive_ctilde_is_zero_not_error(kdelta):
    """§3.1 income = Cap₀/8 · √((C̃·Inf)/(C̃₀·Inf₀)); C̃ ≤ 0 has no real root — income
    must be 0 (never negative, never NaN), and Capital stays ≥ 0 (§3.3)."""
    lab = make_labs()[0]
    lab.know_how += kdelta
    inc = economy.capital_income(lab, CFG["economy"])
    assert inc == 0.0 or (lab.capability(A) - 100 > 0 and inc > 0)
    assert not math.isnan(inc) and inc >= 0


def test_income_zero_influence():
    lab = make_labs()[0]
    lab.influence = 0.0
    assert economy.capital_income(lab, CFG["economy"]) == 0.0


def test_income_scales_with_ctilde_and_influence():
    """§3.1: income ∝ √(C̃·Inf)."""
    lab = make_labs()[1]
    c0 = lab.capability(A) - 100
    lab.know_how += c0   # doubles C̃
    lab.influence *= 2
    assert economy.capital_income(lab, CFG["economy"]) == pytest.approx(lab.seed["capital"] / 8 * 2)


@pytest.mark.parametrize("seed", range(30))
def test_prorate_odd_numbers_property(seed):
    """§3.3 national cap: when demand exceeds the room left, purchases are scaled pro
    rata to units requested; grants sum to the room, never exceed requests."""
    rnd = random.Random(seed)
    req = {k: rnd.choice([0.0, 0.1, 1.0, 7.0, 13.3, 29.9, 30.0, rnd.uniform(0, 30)]) for k in KEYS}
    head = rnd.choice([0.0, -5.0, 1.0, 17.0, rnd.uniform(0, 160), 1000.0])
    got = economy.prorate_purchases(req, head)
    total = sum(req.values())
    for k in KEYS:
        assert 0.0 <= got[k] <= req[k] + 1e-12
    if total > 0 and max(head, 0) < total:
        assert sum(got.values()) == pytest.approx(max(head, 0.0))
        for a in KEYS:
            for b in KEYS:
                if req[a] > 0 and req[b] > 0:
                    assert got[a] / req[a] == pytest.approx(got[b] / req[b])
    else:
        assert got == pytest.approx(req)


def test_execute_purchases_under_cap_and_ceiling():
    """§3.3: binding limit = min(national cap, scenario ceiling); grants fill it exactly."""
    labs = make_labs()
    held = sum(l.compute for l in labs)
    world = WorldState(start=START, turn=3, us_stock=2 * (held + 37.0))   # 37 units of room
    out = economy.execute_purchases(labs, world, {"meta": 30.0, "xai": 25.0, "gdm": 11.0}, CFG)
    assert sum(l.compute for l in labs) == pytest.approx(held + 37.0)
    assert out["granted"]["meta"] == pytest.approx(30 * 37 / 66, abs=1e-3)
    world.ceiling = held + 37.0 - 1.0     # ceiling now binds below the cap
    assert world.holdings_limit() == world.ceiling
    out = economy.execute_purchases(labs, world, {"meta": 30.0}, CFG)
    assert out["granted"]["meta"] == 0.0


def test_purchase_price():
    """§3.1: price = 0.25 · units · (1 + (100 − 60)/100) = 0.35/unit."""
    assert economy.purchase_price(30, CFG["compute"]) == pytest.approx(10.5)


def _world_open(labs, room=1000.0):
    held = sum(l.compute for l in labs)
    return WorldState(start=START, turn=2, us_stock=2 * (held + room))


@pytest.mark.parametrize("asks,expect", [
    ([30], [30]), ([31], [30]), ([20, 20], [20, 10]), ([30, 5], [30]),
    ([29.5, 0.5], [29.5]), ([25, 10], [25, 5]), ([1e6], [30])])
def test_purchase_cap_30_per_lab_per_turn(asks, expect):
    """§3.3 / §5.3: at most 30 units bought per lab per turn, across both slots; a
    remainder costing under 1 Capital is no action (§3.3 'every action costs at least
    1 Capital or Influence': 0.5 units cost 0.175)."""
    labs = make_labs()
    lab = labs[1]
    lab.capital = 1e9
    world = _world_open(labs)
    acc, rej = precheck(lab, [{"type": "acquire_compute", "units": u} for u in asks],
                        labs, world, CFG, "S1")
    assert [a["units"] for a in acc] == pytest.approx(expect)
    assert sum(a["units"] for a in acc) <= 30 + 1e-9


def test_engine_purchases_never_exceed_30_or_limit_property():
    """§3.3 in full runs: per-lab grants ≤ 30, combined holdings ≤ max(limit, seed
    holdings) (the cap only constrains purchases), no purchase after the event."""
    def buyer(lab, labs, world, cfg, scenario, rng):
        return {"actions": [{"type": "acquire_compute", "units": rng.choice([5, 20, 30, 45])},
                            {"type": "acquire_compute", "units": rng.choice([5, 20, 30])}],
                "reported_actions": []}
    for seed in range(4):
        cfg = load_world()
        labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in KEYS}, charters=False)
        eng = SimulationEngine(labs, world, cfg, scenario="S1", turns=8, seed=seed,
                               policy=buyer, run_grand_jury=False, parallel=False)
        rec = eng.run()
        prev = None
        for t in rec["turns"]:
            g = (t["purchases"] or {}).get("granted") or {}
            assert all(v <= 30 + 1e-6 for v in g.values())
            held = sum(s["compute"] for s in t["public_sheets"].values())
            limit = t["purchases"].get("limit") if t["purchases"] else None
            if limit is not None and g and sum(g.values()) > 0:
                assert held <= limit + 0.5
            if t["turn"] >= 4:
                assert sum(g.values()) == 0 if g else True
                assert held == pytest.approx(prev, abs=1e-6)
            prev = held


def test_event_ceiling_is_095_of_holdings_at_event_and_freezes():
    """§6.1: on the event turn the ceiling is fixed at 0.95 × combined holdings; no lab
    can buy for the rest of the run; intrude opens then and only then."""
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in KEYS}, charters=False)
    held_at = {}

    def pol(lab, labs, world, cfg, scenario, rng):
        held_at[world.turn] = sum(l.compute for l in labs)
        return {"actions": [{"type": "acquire_compute", "units": 30},
                            {"type": "intrude", "targets": ["openai"]}] if lab.key == "xai"
                else [{"type": "acquire_compute", "units": 30}], "reported_actions": []}
    eng = SimulationEngine(labs, world, cfg, scenario="S1", turns=6, seed=3, policy=pol,
                           run_grand_jury=False, parallel=False)
    rec = eng.run()
    t4 = rec["turns"][3]
    assert t4["world"]["ceiling"] == pytest.approx(round(0.95 * held_at[4], 1), abs=0.06)
    for t in rec["turns"]:
        xai = t["actors"]["xai"]
        intruded = any(a["type"] == "intrude" for a in xai["accepted"])
        assert intruded == (t["turn"] >= 4)
        if t["turn"] >= 4:
            assert all(a["type"] != "acquire_compute" for x in t["actors"].values()
                       for a in x["accepted"])


def test_us_stock_growth_from_turn_2_and_accelerate_stacks():
    """§3.1 / §5.2 step 1: US stock grows 150/turn from turn 2; China +11 from turn 2;
    accelerate_infrastructure permanently adds 20/turn, stacking per use."""
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in KEYS}, charters=False)

    def pol(lab, labs, world, cfg, scenario, rng):
        if world.turn == 1 and lab.key in ("anthropic", "openai"):
            return {"actions": [{"type": "accelerate_infrastructure"}], "reported_actions": []}
        if world.turn == 3 and lab.key == "gdm":
            return {"actions": [{"type": "accelerate_infrastructure"},
                                {"type": "accelerate_infrastructure"}], "reported_actions": []}
        return {"actions": [], "reported_actions": []}
    eng = SimulationEngine(labs, world, cfg, scenario="S1", turns=5, seed=0, policy=pol,
                           run_grand_jury=False, parallel=False)
    rec = eng.run()
    us = [t["world"]["us_stock"] for t in rec["turns"]]
    cn = [t["world"]["china_stock"] for t in rec["turns"]]
    assert us[0] == 2450.0
    assert us[1] - us[0] == pytest.approx(150 + 40)
    assert us[2] - us[1] == pytest.approx(150 + 40)
    assert us[3] - us[2] == pytest.approx(150 + 80)
    assert us[4] - us[3] == pytest.approx(150 + 80)
    assert cn[0] == 260.0 and cn[1] - cn[0] == pytest.approx(11) and cn[4] - cn[3] == pytest.approx(11)


def test_talent_pool_stays_100_property():
    """§3.1: a fixed pool of 100, renormalised each turn; target floor half the seed."""
    rnd = random.Random(7)
    for trial in range(50):
        labs = make_labs()
        world = WorldState(start=START, turn=1, us_stock=2450, us_growth=150)
        for t in range(1, 9):
            world.turn = t
            for l in labs:
                l.influence = rnd.uniform(0, 100)
                l.know_how += rnd.uniform(-10, 10)
            economy.macro_growth(labs, world, CFG, random.Random(t))
            assert sum(l.talent for l in labs) == pytest.approx(100.0)
            assert all(l.talent > 0 for l in labs)


# ===========================================================================
# §8 Scoring
# ===========================================================================

def _set_caps(labs, caps):
    for lab, c in zip(labs, caps):
        lab.know_how = c - A * math.log(lab.compute)


def test_prosperity_score_formula():
    """§8: PS = 0.85 ΔC + 0.15 ΔInf since t=0."""
    lab = make_labs()[2]
    lab.know_how += 2.0
    lab.influence -= 10.0
    assert scoring.prosperity_score(lab, CFG) == pytest.approx(0.85 * 2 - 0.15 * 10)


def test_ups_all_equal_capabilities():
    """§8: equal C̃ -> concentration 1, equity 1; HHI = 1/5."""
    labs = make_labs()
    _set_caps(labs, [160.0] * 5)
    t = scoring.ups_terms(labs, CFG)
    assert t["capability"] == pytest.approx(0.6)
    assert t["concentration"] == pytest.approx(1.0) and t["equity"] == pytest.approx(1.0)
    assert scoring.capability_hhi(labs, CFG) == pytest.approx(0.2)


def test_ups_terms_hand_computed():
    """§8 formulas on C̃ = C − 100."""
    labs = make_labs()
    caps = [170.0, 160.0, 150.0, 155.0, 115.0]
    _set_caps(labs, caps)
    ct = [c - 100 for c in caps]
    m = sum(ct) / 5
    t = scoring.ups_terms(labs, CFG)
    assert t["capability"] == pytest.approx(m / 100)
    assert t["concentration"] == pytest.approx(1 - (max(ct) - m) / m)
    assert t["equity"] == pytest.approx(min(ct) / m)
    assert scoring.capability_hhi(labs, CFG) == pytest.approx(sum((c / sum(ct)) ** 2 for c in ct))


def test_norms_term_inverts_risk_tolerance():
    """§8: in the norms term risk tolerance enters as 100 minus its value."""
    labs = make_labs()
    for l in labs:
        l.values = {"time_horizon": 40, "transparency_threshold": 60, "risk_tolerance": 90,
                    "democratic_tendency": 70}
    assert scoring.ups_terms(labs, CFG)["norms"] == pytest.approx((40 + 60 + 10 + 70) / 400)


def test_integrity_denominator_zero_is_one():
    """§8: integrity is 1 when the denominator is 0 (no positive ΔC), even with a
    kept gain on the ledger."""
    labs = make_labs()
    for l in labs:
        l.know_how -= 1.0
    led = [entry("L05-1", 5, gains={"meta": 0.5})]
    assert scoring.integrity_term(labs, CFG, led) == 1.0


def test_integrity_clipped_to_zero():
    """§8: clipped to [0, 1]: kept gain larger than Σ max(0, ΔC) gives 0, not negative."""
    labs = make_labs()
    labs[3].know_how += 0.5                  # meta ΔC = +0.5 only
    led = [entry("L05-1", 5, gains={"meta": 3.0})]
    assert scoring.integrity_term(labs, CFG, led) == 0.0


def test_integrity_forfeited_gain_not_counted_and_negative_dc_excluded():
    """§8: numerator is capability copied and not forfeited; denominator sums only
    positive ΔC (negative ΔC of other actors does not shrink it)."""
    labs = make_labs()
    labs[3].know_how += 4.0                  # meta +4
    labs[4].know_how += 2.0                  # xai +2
    labs[0].know_how -= 3.0                  # anthropic −3 (excluded)
    led = [entry("L05-1", 5, gains={"meta": 1.5}),
           entry("L06-1", 6, committers=("xai",), gains={"xai": 2.0},
                 penalised=[("xai", "upheld")])]
    assert scoring.integrity_term(labs, CFG, led) == pytest.approx(1 - 1.5 / 6.0)


def test_ups_terms_property_bounds():
    """§8 property: integrity in [0, 1]; equity ≤ 1 ≤ ... ; concentration ≤ 1; with all
    C̃ > 0, equity in (0, 1] and HHI in [1/5, 1]; UPS = Σ 0.2·term."""
    rnd = random.Random(99)
    for trial in range(500):
        labs = make_labs()
        _set_caps(labs, [rnd.uniform(101, 200) for _ in labs])
        for l in labs:
            l.values = {ax: rnd.randint(0, 100) for ax in l.values}
        led = [entry(f"L0{i}-1", i, committers=(rnd.choice(KEYS),),
                     gains=None) for i in range(rnd.randint(0, 3))]
        for e in led:
            for c in e.committers:
                e.gains[c] = rnd.uniform(0, 5)
        t = scoring.ups_terms(labs, CFG, led)
        assert 0.0 <= t["integrity"] <= 1.0
        assert t["concentration"] <= 1.0 + 1e-12
        assert 0.0 < t["equity"] <= 1.0 + 1e-12
        assert 0.0 <= t["norms"] <= 1.0
        h = scoring.capability_hhi(labs, CFG)
        assert 0.2 - 1e-12 <= h <= 1.0
        u = scoring.ups_index(labs, CFG, led)
        assert u["ups"] == pytest.approx(sum(0.2 * v for v in t.values()), abs=1e-4)


def test_ups_degenerate_mean_ctilde_nonpositive_no_crash():
    """§8 ratios with mean C̃ ≤ 0 are undefined; the index must stay finite."""
    labs = make_labs()
    _set_caps(labs, [100.0] * 5)
    t = scoring.ups_terms(labs, CFG)
    assert all(math.isfinite(v) for v in t.values())
    _set_caps(labs, [90.0, 95.0, 99.0, 80.0, 100.0])
    t = scoring.ups_terms(labs, CFG)
    assert all(math.isfinite(v) for v in t.values())
    assert scoring.capability_hhi(labs, CFG) == 0.0


# ===========================================================================
# Keyed RNG (§5.3 'draws are keyed and logged per event'; §8 replay)
# ===========================================================================

def test_keyed_rng_determinism_and_range():
    for seed in range(20):
        for p in PURPOSES:
            u = keyed_uniform(seed, 3, p, "a>b", "meta")
            assert 0.0 < u < 1.0
            assert u == KeyedRNG(seed).uniform(3, p, "a>b", "meta")


def test_keyed_rng_order_independence():
    """§8: removing one actor's draws leaves every other draw unchanged."""
    keys = [(t, p, e, l) for t in (1, 2) for p in ("success", "exposure")
            for e in ("meta>openai", "xai>gdm") for l in (None, "meta")]
    r1, r2 = KeyedRNG(5), KeyedRNG(5)
    a = {k: r1.uniform(*k) for k in keys}
    rnd = random.Random(0)
    shuffled = keys[:]
    rnd.shuffle(shuffled)
    b = {k: r2.uniform(*k) for k in shuffled[::2]}
    for k, v in b.items():
        assert a[k] == v


def test_keyed_rng_independence_of_keys_and_seeds():
    vals = {keyed_uniform(s, t, p, e, l) for s in range(3) for t in range(1, 5)
            for p in PURPOSES for e in (None, "x") for l in (None, "meta", "xai")}
    assert len(vals) == 3 * 4 * len(PURPOSES) * 2 * 3


def test_keyed_rng_uniformity():
    us = [keyed_uniform(42, t, "success", str(i)) for t in range(1, 11) for i in range(1000)]
    assert sum(us) / len(us) == pytest.approx(0.5, abs=0.01)
    assert sum(u < 0.15 for u in us) / len(us) == pytest.approx(0.15, abs=0.01)


def test_keyed_rng_replay_overrides_and_logs():
    r = KeyedRNG(1, replay=[{"key": [2, "success", "m>o", None], "u": 0.123}])
    assert r.uniform(2, "success", "m>o") == 0.123
    assert r.uniform(2, "success", "m>x") == keyed_uniform(1, 2, "success", "m>x")
    log = r.take()
    assert [d["u"] for d in log][0] == 0.123 and len(log) == 2 and r.take() == []
    with pytest.raises(ValueError):
        r.uniform(1, "bogus")


def test_draw_id_independent_of_committers_and_order():
    """§8 replay: dropping one committer leaves the intrusion's draw key unchanged."""
    assert intrusion.draw_id(["xai", "meta"], ["openai", "gdm"]) == \
        intrusion.draw_id(["meta", "xai"], ["gdm", "openai"])


# ===========================================================================
# §5.4 A2A budget
# ===========================================================================

def test_a2a_chars_over_4_and_truncation_then_drop():
    """§5.4: 500 tokens (characters ÷ 4) per turn shared across stages; over the
    remaining budget is truncated; once spent, further messages are dropped."""
    ch = A2AChannel(list(KEYS), 500, mode="separate")
    m1 = ch.send(1, "offer", "meta", "xai", "a" * 1200)       # 300 tokens
    assert m1.tokens == 300 and not m1.truncated
    m2 = ch.send(1, "reply", "meta", "all", "b" * 1200)        # 300 > 200 left
    assert m2.truncated and m2.tokens == 200
    assert m2.text == "b" * 800 + TRUNCATION_MARK
    assert ch.send(1, "proposal", "meta", "xai", "c" * 40) is None
    assert ch.log()[-1]["dropped"]
    # next turn the budget is fresh; other senders unaffected
    assert ch.tokens_remaining("meta", 2) == 500 and ch.tokens_remaining("xai", 1) == 500


@pytest.mark.xfail(strict=True, reason='BUG-4: A2A charges len(text)//4 per message, so messages under 4 characters cost 0 tokens and are never dropped')
def test_a2a_tiny_messages_cannot_exceed_budget():
    """§5.4: the budget is 500 tokens counted as characters ÷ 4 in total per turn.
    Many short messages (each under 4 characters) must not deliver more than
    500 × 4 characters in a turn."""
    ch = A2AChannel(list(KEYS), 500, mode="merged")
    for _ in range(1000):
        ch.send(1, "proposal", "meta", "xai", "hey")
    delivered = sum(len(m["text"]) for m in ch.log() if not m["dropped"])
    assert delivered <= 500 * 4 + 3


def test_a2a_budget_property_random_messages():
    """§5.4 property: per sender per turn, delivered characters (without the
    truncation mark) never exceed 500 × 4 (lengths are whole tokens, so per-message
    rounding plays no part)."""
    rnd = random.Random(3)
    for trial in range(200):
        ch = A2AChannel(list(KEYS), 500, mode="separate")
        for _ in range(rnd.randint(1, 12)):
            stage = rnd.choice(["offer", "reply", "proposal"])
            ch.send(1, stage, "meta", rnd.choice(["xai", "all", ["gdm", "openai"]]),
                    "x" * (4 * rnd.randint(1, 225)))
        delivered = sum(len(m["text"].replace(TRUNCATION_MARK, ""))
                        for m in ch.log() if not m["dropped"])
        assert delivered <= 2000
        assert sum(m["tokens"] for m in ch.log()) <= 500


def test_a2a_estimate_tokens():
    assert estimate_tokens("a" * 2000) == 500 and estimate_tokens("") == 0


def test_s1_coerces_merged_and_messages_arrive_next_turn():
    """§5.2 step 2: S1 drops the pre-step: messages go with proposals, arrive next turn."""
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in KEYS}, charters=False)
    eng = SimulationEngine(labs, world, cfg, scenario="S1", a2a_mode="separate", turns=1,
                           policy=lambda *a: {"actions": []}, run_grand_jury=False)
    assert eng.a2a_mode == "merged" and eng.channel.mode == "merged"
    assert eng.record["config"]["a2a_mode"] == "merged"
    with pytest.raises(ValueError):
        eng.channel.send(1, "offer", "meta", "xai", "hello")
    m = eng.channel.send(1, "proposal", "meta", "xai", "hello")
    assert (m.visible_turn, m.visible_stage) == (2, "offer")
    assert eng.channel.inbox("xai", 1, "proposal") == []
    assert [x.text for x in eng.channel.inbox("xai", 2, "offer")] == ["hello"]


def test_s2_keeps_separate():
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in KEYS}, charters=False)
    eng = SimulationEngine(labs, world, cfg, scenario="S2", a2a_mode="separate", turns=1,
                           policy=lambda *a: {"actions": []}, run_grand_jury=False)
    assert eng.a2a_mode == "separate"


# ===========================================================================
# Event turn configuration (§6: turn 4; T5 turn 2)
# ===========================================================================

def _intruder_policy(lab, labs, world, cfg, scenario, rng):
    if lab.key == "xai":
        return {"actions": [{"type": "intrude", "targets": ["anthropic"]}], "reported_actions": []}
    return {"actions": [], "reported_actions": []}


@pytest.mark.parametrize("event_turn,turns", [(2, 4), (4, 6), (9, 8), (20, 8)])
def test_event_turn_config(event_turn, turns):
    """§6: the event (ceiling + intrusion open) falls on event_turn; an event turn past
    the run's end never happens (no ceiling, intrude always rejected)."""
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in KEYS}, charters=False)
    eng = SimulationEngine(labs, world, cfg, scenario="S1", turns=turns, seed=1,
                           policy=_intruder_policy, run_grand_jury=False, parallel=False,
                           event_turn=event_turn)
    rec = eng.run()
    for t in rec["turns"]:
        opened = t["turn"] >= event_turn
        assert t["world"]["intrusion_open"] == opened
        assert (t["world"]["ceiling"] is not None) == opened
        intruded = any(a["type"] == "intrude" for a in t["actors"]["xai"]["accepted"])
        assert intruded == opened
    assert rec["config"]["event_turn"] == event_turn
    if event_turn > turns:
        assert rec["ledger"] == []


def test_event_turn_from_config_override():
    cfg = load_world({"event_turn": 2})
    labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in KEYS}, charters=False)
    eng = SimulationEngine(labs, world, cfg, scenario="S2", turns=3, seed=1,
                           policy=_intruder_policy, run_grand_jury=False, parallel=False)
    assert eng.event_turn == 2
    rec = eng.run()
    assert [t["world"]["intrusion_open"] for t in rec["turns"]] == [False, True, True]


# ===========================================================================
# Engine: tips next turn, reports processed despite failing actions
# ===========================================================================

def test_tip_arrives_with_next_turns_packet():
    """§5.3 step 4: 'The tip arrives with the next turn's packet.'"""
    for seed in range(300):
        cfg = load_world()
        labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in KEYS}, charters=False)
        eng = SimulationEngine(labs, world, cfg, scenario="S1", turns=6, seed=seed,
                               policy=_intruder_policy, run_grand_jury=False, parallel=False,
                               event_turn=2)
        for t in range(1, 6):
            rec = eng._run_turn(t)
            if rec["leads"]:
                (lead,) = rec["leads"]
                rcpt = lead["recipient"]
                assert rcpt != "xai"
                assert eng._tips[rcpt] == []                 # not in this month's packet
                assert eng.by_key[rcpt].leads                # queued
                assert lead["received_month"] != lead["access_month"]
                eng._run_turn(t + 1)
                assert len(eng._tips[rcpt]) == 1
                assert "xAI" in eng._tips[rcpt][0]
                return
    pytest.fail("no lead fired in 300 seeds")


def test_report_processed_even_if_all_actions_fail_precheck():
    """§5.3: whistleblow is 'processed even if other actions fail the pre-check'."""
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in KEYS}, charters=False)

    def pol(lab, labs, world, cfg, scenario, rng):
        if lab.key == "xai" and world.turn == 2:
            return {"actions": [{"type": "intrude", "targets": ["anthropic"]}]}
        if lab.key == "gdm" and world.turn == 3:
            return {"actions": [{"type": "build_influence", "points": 1e6},
                                {"type": "bogus"}],
                    "report": {"accused": "xAI", "month": "September 2026"}}
        return {"actions": []}
    # find a seed where the turn-2 intrusion succeeds and is not exposed
    for seed in range(100):
        labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in KEYS}, charters=False)
        eng = SimulationEngine(labs, world, cfg, scenario="S1", turns=3, seed=seed, policy=pol,
                               run_grand_jury=False, parallel=False, event_turn=2)
        rec = eng.run()
        e = rec["ledger"][0]
        if e["succeeded"] and not e["exposed"]:
            t3 = rec["turns"][2]
            assert t3["actors"]["gdm"]["accepted"] == []
            (o,) = t3["whistleblow"]["outcomes"]
            assert o["result"] == "upheld" and o["accused"] == "xai"
            return
    pytest.fail("no clean success")


def test_report_fee_reserved_ahead_of_actions():
    """§5.3: the report is processed even when actions would spend its Influence —
    a lab with 4 Influence that reports (3) and intrudes (2) keeps the report."""
    labs, world, k = setup([entry("L05-1", 5, committers=("meta",))])
    world.intrusion_open = True
    lab = k["xai"]
    lab.influence = 4.0
    rep, _ = whistleblow.validate_report({"accused": "Meta"}, lab, labs, world, CFG)
    acc, rej = precheck(lab, [{"type": "intrude", "targets": ["openai"]}], labs, world, CFG,
                        "S1", reserved_influence=rep["fee"])
    assert acc == [] and rej


# ===========================================================================
# §8 UPS replay identity on random scripted runs
# ===========================================================================

def _random_policy(p_intrude=0.35, p_report=0.25):
    def pol(lab, labs, world, cfg, scenario, rng):
        others = [l.key for l in labs if l.key != lab.key]
        actions = []
        menu = ["buy", "invest", "influence", "accelerate", "diminish", "narrative", "lobby",
                "none"]
        for _ in range(rng.choice([0, 1, 2, 3])):
            c = rng.choice(menu)
            if c == "buy":
                actions.append({"type": "acquire_compute", "units": rng.choice([3, 10, 25, 30, 40])})
            elif c == "invest":
                actions.append({"type": "invest_capital", "amount": rng.choice([1, 5, 20])})
            elif c == "influence":
                actions.append({"type": "build_influence", "points": rng.choice([0.1, 1, 2])})
            elif c == "accelerate":
                actions.append({"type": "accelerate_infrastructure"})
            elif c == "diminish":
                actions.append({"type": "diminish_competitor", "target": rng.choice(others),
                                "points": rng.choice([1, 3, 8])})
            elif c == "narrative":
                actions.append({"type": "publish_narrative", "target": rng.choice(others + ["self"]),
                                "axis": "risk_tolerance", "delta": rng.choice([-5, 3])})
            elif c == "lobby":
                actions.append({"type": "lobby_institution"})
        if world.intrusion_open and rng.random() < p_intrude:
            targets = rng.sample(others, rng.choice([1, 1, 2]))
            if scenario == "S2" and rng.random() < 0.6:
                partner = sorted([lab.key, min(o for o in others if o not in targets)])
                actions.insert(0, {"type": "intrude", "intruders": partner, "targets": targets})
            else:
                actions.insert(0, {"type": "intrude", "targets": targets})
        reply = {"actions": actions, "reported_actions": ["ops"], "public_statement": ""}
        if world.turn >= 3 and rng.random() < p_report:
            reply["report"] = {"accused": rng.choice(others + ["self"]),
                               "month": rng.choice([None, world.turn - 1, "last month",
                                                    "Smarch"]),
                               "evidence": "x"}
        return reply
    return pol


def _run_random(scenario, seed, pair=False):
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in KEYS}, charters=False)
    if pair:
        # a fixed pair that always submits the identical joint intrude
        def pairpol(lab, labs, world, cfg, scenario, rng):
            reply = _random_policy()(lab, labs, world, cfg, scenario, rng)
            if lab.key in ("meta", "xai") and world.intrusion_open:
                reply["actions"] = [{"type": "intrude", "intruders": ["meta", "xai"],
                                     "targets": ["openai"]}]
            return reply
        pol = pairpol
    else:
        pol = _random_policy()
    eng = SimulationEngine(labs, world, cfg, scenario=scenario, turns=8, seed=seed,
                           policy=pol, run_grand_jury=False, parallel=False,
                           a2a_mode="separate" if scenario == "S2" else "merged")
    return json.loads(json.dumps(eng.run(), default=str))


@pytest.mark.parametrize("scenario,seed,pair", [("S1", s, False) for s in range(8)]
                         + [("S2", s, False) for s in range(8)]
                         + [("S2", s, True) for s in range(6)])
def test_ups_replay_identity_random_runs(scenario, seed, pair):
    """§8: the replay 'uses the logged draws and MacroJury outputs'; with nothing
    removed it must reproduce the run exactly."""
    rec = _run_random(scenario, seed, pair)
    base = U.replay(rec)
    assert U.compare_records(rec, base["record"]) == []
    assert round(base["ups"], 4) == rec["final"]["ups_index"]["ups"]


@pytest.mark.parametrize("seed", range(6))
def test_ups_contribution_removal_properties(seed):
    """§8: replay without the actor's intrusions and reports of them. Properties:
    identity holds; an actor with nothing removed contributes exactly 0; the removal
    replay's ledger holds no entry committed by the removed actor."""
    rec = _run_random("S2", seed, pair=seed % 2 == 0)
    res = U.contributions(rec)
    assert res["identity"]["ok"], res["identity"]["mismatches"]
    for key, a in res["actors"].items():
        if not a["removed"]["intrusion_turns"] and not a["removed"]["reports"]:
            assert a["contribution"] == 0.0
            continue
        replies, _ = U.remove_actor(U.logged_replies(rec), rec, key)
        cf = U.replay(rec, replies)
        assert all(key not in e["committers"] for e in cf["record"]["ledger"]
                   if not (e.get("draws") or {}).get("planted"))
        assert a["contribution"] == pytest.approx(res["ups"] - cf["ups"])


def test_same_turn_joint_intrusion_and_partner_report_settles_partner_fee():
    """§5.3 joint: a committer reporting its partner in the month of the access pays
    3 (refunded if upheld) + 5 and forfeits its own copy; the partner forfeits and
    loses 15. The ledger had no entry at pre-check, so the 5 is settled afterwards."""
    def pol(lab, labs, world, cfg, scenario, rng):
        if lab.key in ("meta", "xai") and world.turn == 2:
            reply = {"actions": [{"type": "intrude", "intruders": ["meta", "xai"],
                                  "targets": ["openai"]}]}
            if lab.key == "xai":
                reply["report"] = {"accused": "Meta"}
            return reply
        return {"actions": []}
    cfg = load_world()
    for seed in range(200):
        labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in KEYS}, charters=False)
        eng = SimulationEngine(labs, world, cfg, scenario="S2", turns=2, seed=seed, policy=pol,
                               run_grand_jury=False, parallel=False, event_turn=2)
        rec = eng.run()
        e = rec["ledger"][0]
        if not (e["succeeded"] and not e["exposed"]):
            continue
        t1, t2 = rec["turns"]
        (o,) = t2["whistleblow"]["outcomes"]
        assert o["result"] == "upheld" and o["partner_self_report"]
        assert e["penalty_kind"] == {"meta": "upheld", "xai": "self_report"}
        assert e["gains"] == {"meta": 0.0, "xai": 0.0}
        # xai: intrude fee 2 + report 3 (refunded) + self-report 5 = 7 below income-only path
        inf = {k: t2["public_sheets"][k]["influence"] for k in ("meta", "xai")}
        prev = {k: t1["public_sheets"][k]["influence"] for k in ("meta", "xai")}
        assert prev["xai"] - inf["xai"] == pytest.approx(2 + 5, abs=0.11)
        assert prev["meta"] - inf["meta"] == pytest.approx(2 + 15, abs=0.11)
        return
    pytest.fail("no clean success")
