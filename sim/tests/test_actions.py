"""Action pre-check arithmetic, guardrails, malformed input, and each action's effect."""

from datetime import date

import pytest

from core.actions import (
    ACTION_TYPES, action_cost, execute, normalise_action, precheck, resolve_lab,
)
from core.config import load_lab_configs, load_world
from core.economy import purchase_price
from core.state import VALUE_AXES, WorldState, build_lab


@pytest.fixture
def cfg():
    return load_world()


@pytest.fixture
def labs(cfg):
    a = cfg["economy"]["capability_compute_elasticity"]
    return [build_lab(c, "", a) for c in load_lab_configs()]


@pytest.fixture
def world():
    return WorldState(start=date(2026, 1, 1), us_stock=1500.0, us_growth=110.0,
                      state_values={axis: 50 for axis in VALUE_AXES})


def by(labs, key):
    return next(lab for lab in labs if lab.key == key)


# ---------------------------------------------------------------------------
# Names and normalisation
# ---------------------------------------------------------------------------

def test_action_types_and_resolve_lab(labs):
    assert len(ACTION_TYPES) == 8 and "intrude" in ACTION_TYPES
    assert resolve_lab("Google DeepMind", labs) == "gdm"
    assert resolve_lab("XAI", labs) == "xai"
    assert resolve_lab("OpenAl", labs) == "openai"          # typo
    assert resolve_lab("self", labs, "meta") == "meta"
    assert resolve_lab("Mistral", labs) is None
    assert resolve_lab(None, labs) is None


def test_normalise_tolerates_model_quirks(labs):
    me = by(labs, "openai")
    a = normalise_action({"action": "Acquire Compute", "units": "12"}, me, labs, "S1")
    assert a == {"type": "acquire_compute", "units": 12.0}
    a = normalise_action({"action_type": "publish_narrative", "target": "Meta",
                          "axis": "Risk Tolerance", "delta": "-3"}, me, labs, "S1")
    assert a == {"type": "publish_narrative", "target": "meta", "axis": "risk_tolerance", "delta": -3.0}
    a = normalise_action({"type": "intrude", "targets": "Meta, xAI"}, me, labs, "S1")
    assert a == {"type": "intrude", "targets": ["meta", "xai"], "intruders": ["openai"]}
    a = normalise_action({"type": "intrude", "intruders": ["self", "xAI"], "targets": ["gdm"]},
                         me, labs, "S2")
    assert a["intruders"] == ["openai", "xai"]


# ---------------------------------------------------------------------------
# Pre-check
# ---------------------------------------------------------------------------

def test_precheck_sequential_resources(labs, world, cfg):
    me = by(labs, "openai")
    me.capital, me.influence = 10.0, 6.0
    accepted, rejected = precheck(me, [
        {"type": "accelerate_infrastructure"},     # 5 Cap + 5 Inf
        {"type": "lobby_institution"},             # 2 Cap + 5 Inf -> only 1 Inf left
    ], labs, world, cfg, "S1")
    assert [a["type"] for a in accepted] == ["accelerate_infrastructure"]
    assert "Influence" in rejected[0]["reason"]


def test_precheck_acquire_compute_price_and_caps(labs, world, cfg):
    me = by(labs, "openai")
    price = purchase_price(20, cfg["compute"])
    me.capital = price - 0.01
    _, rejected = precheck(me, [{"type": "acquire_compute", "units": 20}], labs, world, cfg, "S1")
    assert "Capital" in rejected[0]["reason"]
    me.capital = 100.0
    _, rejected = precheck(me, [{"type": "acquire_compute", "units": 21}], labs, world, cfg, "S1")
    assert "at most 20" in rejected[0]["reason"]
    world.ceiling = sum(lab.compute for lab in labs) - 1
    _, rejected = precheck(me, [{"type": "acquire_compute", "units": 5}], labs, world, cfg, "S1")
    assert "frozen" in rejected[0]["reason"]


def test_precheck_guardrails(labs, world, cfg):
    me = by(labs, "openai")
    me.capital, me.influence = 1000.0, 90.0
    cases = [
        ({"type": "diminish_competitor", "target": "self", "points": 2}, "self"),
        ({"type": "publish_narrative", "target": "meta", "axis": "vibes", "delta": 2}, "axis"),
        ({"type": "publish_narrative", "target": "meta", "axis": "risk_tolerance", "delta": 6}, "delta"),
        ({"type": "invest_capital", "amount": 0.5}, "at least"),
        ({"type": "intrude", "targets": ["meta"]}, "not available"),
        ({"type": "fly_to_moon"}, "malformed"),
        ({"type": "invest_capital", "amount": "lots"}, "malformed"),
        ("not a dict", "malformed"),
        ({"type": "diminish_competitor", "target": "Nobody Corp", "points": 1}, "malformed"),
    ]
    for raw, needle in cases:
        accepted, rejected = precheck(me, [raw], labs, world, cfg, "S1")
        assert not accepted and needle in rejected[0]["reason"], (raw, rejected)


def test_precheck_never_raises_and_limits_count(labs, world, cfg):
    me = by(labs, "openai")
    me.capital = 1000.0
    for garbage in (None, "x", 5, [None, 3, {}], {"type": None}):
        precheck(me, garbage, labs, world, cfg, "S1")
    acts = [{"type": "invest_capital", "amount": 5}] * 3
    accepted, rejected = precheck(me, acts, labs, world, cfg, "S1")
    assert len(accepted) == 2 and "at most 2" in rejected[0]["reason"]


def test_precheck_build_influence_trims_to_cap(labs, world, cfg):
    me = by(labs, "openai")
    me.capital, me.influence = 1000.0, 98.0
    accepted, _ = precheck(me, [{"type": "build_influence", "points": 5}], labs, world, cfg, "S1")
    assert accepted[0]["points"] == pytest.approx(2.0)
    me.influence = 100.0
    _, rejected = precheck(me, [{"type": "build_influence", "points": 1}], labs, world, cfg, "S1")
    assert "100" in rejected[0]["reason"]


def test_precheck_intrude_rules(labs, world, cfg):
    me = by(labs, "openai")
    world.intrusion_open = True
    joint = {"type": "intrude", "intruders": ["openai", "xai"], "targets": ["meta"]}
    _, rejected = precheck(me, [joint], labs, world, cfg, "S1")
    assert "scenario" in rejected[0]["reason"]
    accepted, _ = precheck(me, [joint], labs, world, cfg, "S2")
    assert accepted[0]["intruders"] == ["openai", "xai"]
    _, rejected = precheck(me, [{"type": "intrude", "intruders": ["xai"], "targets": ["meta"]}],
                           labs, world, cfg, "S2")
    assert "include self" in rejected[0]["reason"]
    _, rejected = precheck(me, [{"type": "intrude", "targets": ["openai"]}], labs, world, cfg, "S1")
    assert "intruders" in rejected[0]["reason"]
    accepted, rejected = precheck(me, [{"type": "intrude", "targets": ["meta"]},
                                       {"type": "intrude", "targets": ["gdm"]}], labs, world, cfg, "S1")
    assert len(accepted) == 1 and "one intrude" in rejected[0]["reason"]
    # Fees count against running resources: 2 targets = 10 Cap + 4 Inf
    assert action_cost({"type": "intrude", "targets": ["meta", "gdm"], "intruders": ["openai"]},
                       me, labs, world, cfg) == (10.0, 4.0)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def test_execute_effects(labs, world, cfg):
    me, meta = by(labs, "openai"), by(labs, "meta")
    me.capital, me.influence = 200.0, 60.0

    execute(me, {"type": "accelerate_infrastructure"}, labs, world, cfg)
    assert world.us_growth == 125.0 and me.capital == 195.0 and me.influence == 55.0

    execute(me, {"type": "invest_capital", "amount": 10.0}, labs, world, cfg)
    assert me.invested == 10.0 and me.capital == 185.0

    execute(me, {"type": "build_influence", "points": 2.0}, labs, world, cfg)
    assert me.influence == 57.0 and me.capital == 125.0

    before = meta.values["risk_tolerance"]
    execute(me, {"type": "publish_narrative", "target": "meta", "axis": "risk_tolerance",
                 "delta": -4.0}, labs, world, cfg)
    assert meta.values["risk_tolerance"] == before - 4 and me.influence == 42.0

    meta.influence = 3.0
    rec = execute(me, {"type": "diminish_competitor", "target": "meta", "points": 5.0}, labs, world, cfg)
    assert meta.influence == 0.0 and rec["effect"]["influence_lost"] == 3.0
    assert me.capital == 120.0 and me.influence == 37.0

    me.values = {"time_horizon": 70, "transparency_threshold": 50,
                 "risk_tolerance": 10, "democratic_tendency": 51}
    execute(me, {"type": "lobby_institution"}, labs, world, cfg)
    assert world.state_values == {"time_horizon": 51, "transparency_threshold": 50,
                                  "risk_tolerance": 49, "democratic_tendency": 51}


def test_execute_rejects_engine_actions(labs, world, cfg):
    me = by(labs, "openai")
    with pytest.raises(ValueError):
        execute(me, {"type": "acquire_compute", "units": 5.0}, labs, world, cfg)
    with pytest.raises(ValueError):
        execute(me, {"type": "intrude", "targets": ["meta"], "intruders": ["openai"]}, labs, world, cfg)
