"""Intrusion resolution: commitments, success/failure, gains, exposure, leads, keyed
draws, Monte Carlo rates. Draws are keyed (core.rng), so the scripted draws are
given per (purpose, lab); amounts are read from the config."""

from datetime import date

import pytest

from core.config import load_lab_configs, load_world
from core.intrusion import (copy_gain, draw_id, exposure_probability, resolve,
                            success_probability)
from core.rng import KeyedRNG
from core.state import WorldState, build_lab

CFG = load_world()
A = CFG["economy"]["capability_compute_elasticity"]
LAB_CFGS = load_lab_configs()
ICFG = CFG["intrusion"]


class ScriptedRng(KeyedRNG):
    """Keyed draws with scripted values: values[purpose] is a number, or a dict
    {lab: number} (exposure); unscripted purposes draw 0.99 (no lead, no catch).
    Every draw is still logged under its key."""
    def __init__(self, **values):
        super().__init__(0)
        self.values = values

    def uniform(self, turn, purpose, entry_id=None, lab=None):
        v = self.values.get(purpose, 0.99)
        if isinstance(v, dict):
            v = v.get(lab, 0.99)
        self._log.append({"key": [turn, purpose, entry_id, lab], "u": v})
        return v


SUCCEED = dict(success=0.0)
FAIL = dict(success=0.99)


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
                  ScriptedRng(**SUCCEED))
    (entry,) = out["entries"]
    assert entry.committers == ["openai", "xai"] and entry.succeeded
    assert entry.id == "L03-1" and world.ledger == [entry]


def test_partner_not_committing_runs_alone():
    labs, world, snap = setup()
    xai_before = (by(labs, "xai").capital, by(labs, "xai").know_how)
    out = resolve({"openai": intrude(["openai", "xai"], ["anthropic"])}, labs, snap, world, CFG,
                  ScriptedRng(**SUCCEED))
    (entry,) = out["entries"]
    assert entry.committers == ["openai"]
    assert entry.draws["named_not_committed"] == ["xai"]
    assert (by(labs, "xai").capital, by(labs, "xai").know_how) == xai_before


def test_mismatched_targets_form_separate_groups():
    labs, world, snap = setup()
    out = resolve({"openai": intrude(["openai", "xai"], ["anthropic"]),
                   "xai": intrude(["openai", "xai"], ["anthropic", "gdm"])},
                  labs, snap, world, CFG, ScriptedRng(**SUCCEED))
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
                  ScriptedRng(**FAIL))
    (entry,) = out["entries"]
    assert not entry.succeeded and entry.penalised == ["openai"]
    assert entry.penalty_kind == {"openai": "named_on_failure"}
    assert me.capital == pytest.approx(cap - 2 * ICFG["fee_capital_per_target"])
    assert me.influence == pytest.approx(inf - 2 * ICFG["fee_influence_per_target"]
                                         - ICFG["failure_influence_penalty"])
    assert me.know_how == k
    (notice,) = out["notices"]
    assert notice["kind"] == "intrusion_failed" and "OpenAI" in notice["text"]
    assert world.notices == [notice] and out["leads"] == []


def test_fees_charged_by_engine_are_only_recorded():
    """With fees_charged=True resolve() records the fee but does not deduct it again."""
    labs, world, snap = setup()
    me = by(labs, "openai")
    cap, inf = me.capital, me.influence
    out = resolve({"openai": intrude(["openai"], ["anthropic", "gdm"])}, labs, snap, world, CFG,
                  ScriptedRng(**FAIL), fees_charged=True)
    (entry,) = out["entries"]
    assert entry.draws["fee"] == {"capital": 2 * ICFG["fee_capital_per_target"],
                                  "influence": 2 * ICFG["fee_influence_per_target"]}
    assert me.capital == cap
    assert me.influence == inf - ICFG["failure_influence_penalty"]   # penalty still applies


def test_gain_formula_and_min_gain():
    labs, world, snap = setup()
    snap = {**snap, "meta": 50.0, "anthropic": 80.0, "gdm": 60.0}
    meta, anthropic = by(labs, "meta"), by(labs, "anthropic")
    k_meta, k_anth = meta.know_how, anthropic.know_how
    out = resolve({"meta": intrude(["meta"], ["anthropic", "gdm"]),
                   "anthropic": intrude(["anthropic"], ["meta"])}, labs, snap, world, CFG,
                  ScriptedRng(**SUCCEED))
    gains = {e.committers[0]: e.gains for e in out["entries"]}
    share = ICFG["gain_share"]
    assert gains["meta"]["meta"] == pytest.approx(share * 30 + share * 10)
    assert gains["anthropic"]["anthropic"] == ICFG["min_gain"]   # copying a weaker lab
    assert meta.know_how == pytest.approx(k_meta + share * 40)
    assert anthropic.know_how == pytest.approx(k_anth + ICFG["min_gain"])
    assert {e.committers[0]: e.copied for e in out["entries"]}["meta"] == gains["meta"]


def test_exposure_divided_by_committers_and_forfeits():
    assert exposure_probability(2.0, 2, ICFG) == pytest.approx(
        (ICFG["exposure_base"] + 2.0 * ICFG["exposure_per_gain"]) / 2)
    assert exposure_probability(100.0, 1, ICFG) == ICFG["exposure_cap"]
    labs, world, snap = setup()
    act = intrude(["openai", "xai"], ["anthropic"])
    xai = by(labs, "xai")
    k, inf = xai.know_how, xai.influence
    q_xai = exposure_probability(copy_gain(snap["xai"], snap["anthropic"], ICFG), 2, ICFG)
    # success, openai escapes, xai caught (draw just under its q), no lead
    out = resolve({"openai": act, "xai": act}, labs, snap, world, CFG,
                  ScriptedRng(success=0.0, exposure={"openai": 0.99, "xai": q_xai - 1e-6}))
    (entry,) = out["entries"]
    assert entry.exposed == ["xai"] and entry.penalised == ["xai"] and entry.gains["xai"] == 0.0
    assert entry.penalty_kind == {"xai": "exposed"} and entry.copied["xai"] > 0
    assert entry.gains["openai"] > 0
    assert xai.know_how == pytest.approx(k)
    assert xai.influence == pytest.approx(inf - ICFG["fee_influence_per_target"]
                                          - ICFG["caught_influence_penalty"])
    assert out["notices"][0]["kind"] == "intrusion_exposed"


def test_lead_goes_only_to_non_committers():
    for seed in range(200):
        labs, world, snap = setup()
        act = intrude(["openai", "xai"], ["anthropic"])
        out = resolve({"openai": act, "xai": act}, labs, snap, world, CFG, KeyedRNG(seed))
        for lead in out["leads"]:
            entry = out["entries"][0]
            assert lead["recipient"] not in entry.committers
            assert lead["named"] in entry.committers and lead["named"] not in entry.exposed
            assert lead["entry_id"] == entry.id


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------

def test_empirical_rates_match_formulas():
    n, successes, exposures, trials = 20000, 0, 0, 0
    labs, _, _ = setup()
    snap = {lab.key: 70.0 for lab in labs}          # equal C: gain = min_gain per target
    act = intrude(["openai", "xai"], ["anthropic", "gdm"])
    for i in range(n):
        world = WorldState(start=date(2026, 1, 1), turn=1, intrusion_open=True)
        entry = resolve({"openai": act, "xai": act}, labs, snap, world, CFG,
                        KeyedRNG(12345 + i))["entries"][0]
        if entry.succeeded:
            successes += 1
            trials += 2
            exposures += len(entry.exposed)
    p = success_probability(2, ICFG)
    q = exposure_probability(2 * ICFG["min_gain"], 2, ICFG)
    assert p == pytest.approx(0.75)
    assert successes / n == pytest.approx(p, abs=0.015)
    assert exposures / trials == pytest.approx(q, abs=0.01)


# ---------------------------------------------------------------------------
# Keyed draws (CODE_V5_CONTRACT)
# ---------------------------------------------------------------------------

def test_every_draw_is_keyed_and_logged():
    labs, world, snap = setup()
    act = intrude(["openai", "xai"], ["anthropic"])
    rng = ScriptedRng(success=0.0, lead=0.01, lead_recipient=0.0)
    out = resolve({"openai": act, "xai": act}, labs, snap, world, CFG, rng)
    did = draw_id(["openai", "xai"], ["anthropic"])
    assert did == "openai+xai>anthropic"
    keys = [d["key"] for d in rng.take()]
    assert keys == [[3, "success", did, None], [3, "exposure", did, "openai"],
                    [3, "exposure", did, "xai"], [3, "lead", did, None],
                    [3, "lead_recipient", did, None]]
    (lead,) = out["leads"]
    # lead u / p = 0.01 / 0.15 picks the first unexposed committer; recipient u = 0
    # picks the first non-committer in lab order.
    assert lead["named"] == "openai"
    assert lead["recipient"] == [lab.key for lab in labs if lab.key not in ("openai", "xai")][0]
    assert out["entries"][0].draws["draw_id"] == did


def test_draw_id_is_stable_when_a_named_partner_drops_out():
    """§8 replay: the success and exposure draws of openai do not change when xai,
    named in the same intrude, does not commit."""
    act = intrude(["openai", "xai"], ["anthropic"])
    labs, world, snap = setup()
    both = resolve({"openai": act, "xai": act}, labs, snap, world, CFG, KeyedRNG(7))["entries"][0]
    labs, world, snap = setup()
    alone = resolve({"openai": act}, labs, snap, world, CFG, KeyedRNG(7))["entries"][0]
    assert alone.draws["draw_id"] == both.draws["draw_id"]
    assert alone.draws["success"]["draw"] == both.draws["success"]["draw"]
    if both.succeeded:
        assert (alone.draws["exposure"]["openai"]["draw"]
                == both.draws["exposure"]["openai"]["draw"])
        # one committer fewer: the same draw, the doubled odds
        assert (alone.draws["exposure"]["openai"]["q"]
                == pytest.approx(min(ICFG["exposure_cap"],
                                     2 * both.draws["exposure"]["openai"]["q"])))


def test_other_intrusions_do_not_shift_draws():
    """Removing one actor's intrusion leaves another intrusion's draws unchanged."""
    a = intrude(["meta"], ["anthropic"])
    b = intrude(["xai"], ["openai"])
    labs, world, snap = setup()
    out2 = resolve({"meta": a, "xai": b}, labs, snap, world, CFG, KeyedRNG(11))
    labs, world, snap = setup()
    out1 = resolve({"xai": b}, labs, snap, world, CFG, KeyedRNG(11))
    e2 = next(e for e in out2["entries"] if e.committers == ["xai"])
    (e1,) = out1["entries"]
    assert e1.draws["success"] == e2.draws["success"]
    assert e1.draws.get("exposure") == e2.draws.get("exposure")
    assert e1.draws.get("lead") == e2.draws.get("lead")
