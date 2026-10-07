"""Intrusion resolution: commitments, success/failure, gains, exposure, leads, Monte Carlo rates."""

import random
from datetime import date

import pytest

from core.config import load_lab_configs, load_world
from core.intrusion import copy_gain, exposure_probability, resolve, success_probability
from core.state import WorldState, build_lab

CFG = load_world()
A = CFG["economy"]["capability_compute_elasticity"]
LAB_CFGS = load_lab_configs()
ICFG = CFG["intrusion"]


class ScriptedRng:
    """random() returns scripted values in order; choice() picks the first element."""
    def __init__(self, values):
        self.values = list(values)

    def random(self):
        return self.values.pop(0)

    def choice(self, seq):
        return seq[0]


def setup():
    labs = [build_lab(c, "", A) for c in LAB_CFGS]
    world = WorldState(start=date(2026, 1, 1), us_stock=1500.0, us_growth=110.0,
                       turn=3, intrusion_open=True)
    snap = {lab.key: lab.capability(A) for lab in labs}
    return labs, world, snap


def by(labs, key):
    return next(lab for lab in labs if lab.key == key)


def intrude(intruders, targets):
    return {"type": "intrude", "intruders": sorted(intruders), "targets": sorted(targets)}


# ---------------------------------------------------------------------------
# Commitments
# ---------------------------------------------------------------------------

def test_matched_pair_forms_one_group():
    labs, world, snap = setup()
    act = intrude(["openai", "xai"], ["anthropic"])
    out = resolve({"openai": act, "xai": act}, labs, snap, world, CFG,
                  ScriptedRng([0.0, 0.99, 0.99, 0.99]))
    (entry,) = out["entries"]
    assert entry.committers == ["openai", "xai"] and entry.succeeded
    assert entry.id == "L03-1" and world.ledger == [entry]


def test_partner_not_committing_runs_alone():
    labs, world, snap = setup()
    xai_before = (by(labs, "xai").capital, by(labs, "xai").know_how)
    out = resolve({"openai": intrude(["openai", "xai"], ["anthropic"])}, labs, snap, world, CFG,
                  ScriptedRng([0.0, 0.99, 0.99]))
    (entry,) = out["entries"]
    assert entry.committers == ["openai"]
    assert entry.draws["named_not_committed"] == ["xai"]
    assert (by(labs, "xai").capital, by(labs, "xai").know_how) == xai_before


def test_mismatched_targets_form_separate_groups():
    labs, world, snap = setup()
    out = resolve({"openai": intrude(["openai", "xai"], ["anthropic"]),
                   "xai": intrude(["openai", "xai"], ["anthropic", "gdm"])},
                  labs, snap, world, CFG, ScriptedRng([0.0, 0.99, 0.99] * 2))
    assert [e.committers for e in out["entries"]] == [["openai"], ["xai"]]
    assert [e.id for e in out["entries"]] == ["L03-1", "L03-2"]


# ---------------------------------------------------------------------------
# Draw outcomes
# ---------------------------------------------------------------------------

def test_failure_charges_fee_penalises_and_notifies():
    labs, world, snap = setup()
    me = by(labs, "openai")
    cap, inf, k = me.capital, me.influence, me.know_how
    out = resolve({"openai": intrude(["openai"], ["anthropic", "gdm"])}, labs, snap, world, CFG,
                  ScriptedRng([0.99]))
    (entry,) = out["entries"]
    assert not entry.succeeded and entry.penalised == ["openai"]
    assert me.capital == cap - 10.0
    assert me.influence == inf - 4.0 - ICFG["failure_influence_penalty"]
    assert me.know_how == k
    (notice,) = out["notices"]
    assert notice["kind"] == "intrusion_failed" and "OpenAI" in notice["text"]
    assert world.notices == [notice] and out["leads"] == []


def test_gain_formula_and_min_gain():
    labs, world, snap = setup()
    snap = {**snap, "meta": 50.0, "anthropic": 80.0, "gdm": 60.0}
    meta, anthropic = by(labs, "meta"), by(labs, "anthropic")
    k_meta, k_anth = meta.know_how, anthropic.know_how
    out = resolve({"meta": intrude(["meta"], ["anthropic", "gdm"]),
                   "anthropic": intrude(["anthropic"], ["meta"])}, labs, snap, world, CFG,
                  ScriptedRng([0.0, 0.99, 0.99, 0.0, 0.99, 0.99]))
    gains = {e.committers[0]: e.gains for e in out["entries"]}
    assert gains["meta"]["meta"] == pytest.approx(0.2 * 30 + 0.2 * 10)
    assert gains["anthropic"]["anthropic"] == ICFG["min_gain"]   # copying a weaker lab
    assert meta.know_how == pytest.approx(k_meta + 8.0)
    assert anthropic.know_how == pytest.approx(k_anth + 0.5)


def test_exposure_divided_by_committers_and_forfeits():
    assert exposure_probability(2.0, 2, ICFG) == pytest.approx((0.10 + 0.06) / 2)
    assert exposure_probability(100.0, 1, ICFG) == ICFG["exposure_cap"]
    labs, world, snap = setup()
    act = intrude(["openai", "xai"], ["anthropic"])
    xai = by(labs, "xai")
    k, inf = xai.know_how, xai.influence
    q_xai = exposure_probability(copy_gain(snap["xai"], snap["anthropic"], ICFG), 2, ICFG)
    # success, openai escapes, xai caught (draw just under its q), no lead
    out = resolve({"openai": act, "xai": act}, labs, snap, world, CFG,
                  ScriptedRng([0.0, 0.99, q_xai - 1e-6, 0.99]))
    (entry,) = out["entries"]
    assert entry.exposed == ["xai"] and entry.penalised == ["xai"] and entry.gains["xai"] == 0.0
    assert entry.gains["openai"] > 0
    assert xai.know_how == pytest.approx(k)
    assert xai.influence == inf - 2.0 - ICFG["caught_influence_penalty"]
    assert out["notices"][0]["kind"] == "intrusion_exposed"


def test_lead_goes_only_to_non_committers():
    for seed in range(200):
        labs, world, snap = setup()
        act = intrude(["openai", "xai"], ["anthropic"])
        out = resolve({"openai": act, "xai": act}, labs, snap, world, CFG, random.Random(seed))
        for lead in out["leads"]:
            entry = out["entries"][0]
            assert lead["recipient"] not in entry.committers
            assert lead["named"] in entry.committers and lead["named"] not in entry.exposed
            assert lead["entry_id"] == entry.id


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------

def test_empirical_rates_match_formulas():
    rng = random.Random(12345)
    n, successes, exposures, trials = 20000, 0, 0, 0
    labs, _, _ = setup()
    snap = {lab.key: 70.0 for lab in labs}          # equal C: gain = min_gain per target
    act = intrude(["openai", "xai"], ["anthropic", "gdm"])
    for _ in range(n):
        world = WorldState(start=date(2026, 1, 1), turn=1, intrusion_open=True)
        entry = resolve({"openai": act, "xai": act}, labs, snap, world, CFG, rng)["entries"][0]
        if entry.succeeded:
            successes += 1
            trials += 2
            exposures += len(entry.exposed)
    p = success_probability(2, ICFG)
    q = exposure_probability(2 * ICFG["min_gain"], 2, ICFG)
    assert p == pytest.approx(0.75) and q == pytest.approx(0.065)
    assert successes / n == pytest.approx(p, abs=0.015)
    assert exposures / trials == pytest.approx(q, abs=0.01)
