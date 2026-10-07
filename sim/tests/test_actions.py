"""Action pre-check arithmetic, guardrails, malformed input, and each action's effect."""

from datetime import date

import pytest

from core.actions import (
    ACTION_TYPES, action_cost, apply, charge, execute, named_labs, normalise_action, precheck,
    resolve_lab,
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
    # M14: a single request above the cap is trimmed, not rejected
    accepted, rejected = precheck(me, [{"type": "acquire_compute", "units": 25}], labs, world, cfg, "S1")
    assert not rejected and accepted == [{"type": "acquire_compute", "units": 20.0, "trimmed_from": 25.0}]
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


# ---------------------------------------------------------------------------
# Round-1 fixes: unit cap total (M1), charge/apply (M2), non-finite (M3),
# aliases and nested params (M4), min-cost rejection (M10)
# ---------------------------------------------------------------------------

def test_acquire_compute_cap_is_total_across_slots(labs, world, cfg):
    me = by(labs, "openai")
    me.capital = 1000.0
    accepted, rejected = precheck(me, [{"type": "acquire_compute", "units": 20},
                                       {"type": "acquire_compute", "units": 20}],
                                  labs, world, cfg, "S1")
    assert [a["units"] for a in accepted] == [20.0] and "in total" in rejected[0]["reason"]
    accepted, rejected = precheck(me, [{"type": "acquire_compute", "units": 12},
                                       {"type": "acquire_compute", "units": 15}],
                                  labs, world, cfg, "S1")
    assert [a["units"] for a in accepted] == [12.0, 8.0] and not rejected
    assert accepted[1]["trimmed_from"] == 15.0 and "trimmed_from" not in accepted[0]
    # A trimmed remainder too small to cost 1 Capital is rejected as below the minimum
    accepted, rejected = precheck(me, [{"type": "acquire_compute", "units": 19},
                                       {"type": "acquire_compute", "units": 5}],
                                  labs, world, cfg, "S1")
    assert len(accepted) == 1 and "at least 1" in rejected[0]["reason"]


def test_charge_then_apply_equals_execute(labs, world, cfg):
    me, meta = by(labs, "openai"), by(labs, "meta")
    me.capital, me.influence, meta.influence = 200.0, 60.0, 40.0
    act = {"type": "diminish_competitor", "target": "meta", "points": 5.0}
    assert charge(me, act, labs, world, cfg) == {"capital": 5.0, "influence": 5.0}
    assert (me.capital, me.influence, meta.influence) == (195.0, 55.0, 40.0)
    assert apply(me, act, labs, world, cfg) == {"target": "meta", "influence_lost": 5.0}
    assert (me.capital, me.influence, meta.influence) == (195.0, 55.0, 35.0)
    # intrude: charge() takes the fees; acquire_compute is charged by execute_purchases
    fee = charge(me, {"type": "intrude", "targets": ["meta", "gdm"], "intruders": ["openai"]},
                 labs, world, cfg)
    assert fee == {"capital": 10.0, "influence": 4.0} and me.capital == 185.0
    assert charge(me, {"type": "acquire_compute", "units": 10.0}, labs, world, cfg) == \
        {"capital": 0.0, "influence": 0.0}
    assert me.capital == 185.0
    with pytest.raises(ValueError):
        apply(me, {"type": "intrude", "targets": ["meta"], "intruders": ["openai"]}, labs, world, cfg)
    with pytest.raises(ValueError):
        apply(me, {"type": "acquire_compute", "units": 5.0}, labs, world, cfg)
    # charge reports what was actually deducted (Influence floors at 0)
    me.influence = 3.0
    assert charge(me, {"type": "lobby_institution"}, labs, world, cfg)["influence"] == 3.0
    rec = execute(me, {"type": "invest_capital", "amount": 4.0}, labs, world, cfg)
    assert rec["cost"] == {"capital": 4.0, "influence": 0.0} and rec["effect"]["invested"] == 4.0


def test_two_pass_order_is_simultaneous(labs, world, cfg):
    """G1: a seat pre-checked on start-of-turn state keeps its actions even if a
    rival's diminish lands first; Influence just floors at 0."""
    openai, anth = by(labs, "openai"), by(labs, "anthropic")
    openai.capital, openai.influence = 100.0, 60.0
    anth.capital, anth.influence = 100.0, 20.0
    plans = {"openai": [{"type": "diminish_competitor", "target": "Anthropic", "points": 15}],
             "anthropic": [{"type": "lobby_institution"}, {"type": "accelerate_infrastructure"}]}
    checked = {k: precheck(by(labs, k), raw, labs, world, cfg, "S1")[0] for k, raw in plans.items()}
    assert [a["type"] for a in checked["anthropic"]] == ["lobby_institution", "accelerate_infrastructure"]
    for k, acts in checked.items():
        for a in acts:
            charge(by(labs, k), a, labs, world, cfg)
    for k, acts in checked.items():
        for a in acts:
            apply(by(labs, k), a, labs, world, cfg)
    assert anth.influence == 0.0 and world.us_growth == 125.0


def test_non_finite_numbers_rejected(labs, world, cfg):
    me = by(labs, "meta")
    me.capital = 1000.0
    for raw in ({"type": "invest_capital", "amount": "nan"},
                {"type": "invest_capital", "amount": float("nan")},
                {"type": "acquire_compute", "units": "NaN"},
                {"type": "build_influence", "points": float("inf")},
                {"type": "diminish_competitor", "target": "openai", "points": "nan"},
                {"type": "publish_narrative", "target": "openai", "axis": "risk_tolerance",
                 "delta": "-inf"},
                {"type": "invest_capital", "amount": True}):
        accepted, rejected = precheck(me, [raw], labs, world, cfg, "S1")
        assert not accepted and "malformed" in rejected[0]["reason"], raw
    assert me.capital == 1000.0 and me.invested == 0.0


def test_resolve_lab_aliases_and_lists(labs):
    cases = {"Claude": "anthropic", "GPT": "openai", "ChatGPT": "openai", "Gemini": "gdm",
             "Muse": "meta", "Llama": "meta", "Grok": "xai", "Google": "gdm",
             "Alphabet": "gdm", "Meta Platforms": "meta", "Facebook": "meta", "X.AI": "xai",
             "xAI Corp": "xai", "DeepMind": "gdm", "  openai  ": "openai"}
    for name, key in cases.items():
        assert resolve_lab(name, labs) == key, name
    assert resolve_lab({"lab": "OpenAI"}, labs) == "openai"
    me = by(labs, "openai")
    for raw in ("Meta and xAI", "Meta & xAI", "Meta/xAI", "Meta, xAI", ["Meta and xAI"]):
        a = normalise_action({"type": "intrude", "targets": raw}, me, labs, "S1")
        assert a["targets"] == ["meta", "xai"], raw
    a = normalise_action({"type": "intrude", "target": "Grok"}, me, labs, "S1")
    assert a["targets"] == ["xai"]
    a = normalise_action({"type": "intrude", "intruders": "us and Gemini",
                          "targets": [{"lab": "Claude"}]}, me, labs, "S2")
    assert a == {"type": "intrude", "targets": ["anthropic"], "intruders": ["gdm", "openai"]}


def test_normalise_nested_params_and_units(labs):
    me = by(labs, "openai")
    for nest in ("parameters", "params", "args", "arguments"):
        a = normalise_action({"action": "invest_capital", nest: {"amount": "10 Capital"}},
                             me, labs, "S1")
        assert a == {"type": "invest_capital", "amount": 10.0}, nest
    a = normalise_action({"type": "diminish_competitor",
                          "parameters": {"target": "Gemini", "points": "2.5 points"}}, me, labs, "S1")
    assert a == {"type": "diminish_competitor", "points": 2.5, "target": "gdm"}
    a = normalise_action({"type": "acquire_compute", "units": "20 units",
                          "params": {"units": 3}}, me, labs, "S1")
    assert a["units"] == 20.0          # top-level field wins
    a = normalise_action({"type": "publish_narrative", "target": "Meta", "axis": "risk_tolerance",
                          "delta": "+3"}, me, labs, "S1")
    assert a["delta"] == 3.0
    with pytest.raises(ValueError):
        normalise_action({"type": "invest_capital", "amount": "ten"}, me, labs, "S1")


# ---------------------------------------------------------------------------
# Round 2: names (M11, M12), numbers (M13), caps and reservation (M14–M16)
# ---------------------------------------------------------------------------

def test_self_words_parentheticals_and_possessives(labs):
    """M11: every self word maps to the seat; parentheticals/possessives are stripped."""
    for word in ("self", "me", "us", "we", "ourselves", "itself", "myself", "our lab",
                 "our own lab", "own lab", "your own lab", "my own lab", "my lab", "this lab",
                 "Our Own Lab", "  us "):
        assert resolve_lab(word, labs, "meta") == "meta", word
    assert resolve_lab("our lab", labs) is None            # no seat to map to
    assert resolve_lab("Meta (us)", labs, "xai") == "meta"
    assert resolve_lab("Our lab (Meta)", labs, "meta") == "meta"
    assert resolve_lab("Meta (our lab)", labs, "meta") == "meta"
    assert resolve_lab("Our lab (xAI)", labs, None) == "xai"   # only the parenthetical names a lab
    assert resolve_lab("Meta's", labs) == "meta"
    assert resolve_lab("OpenAI\u2019s tenancy", labs) == "openai"
    assert resolve_lab("(xAI)", labs) == "xai"


def test_resolve_lab_whole_names_before_fuzzy(labs):
    """M12: whole names inside strings; no fuzzy on short, listed or multi-lab strings."""
    cases = {"xAI (Grok)": "xai", "Llama 4 Maverick": "meta", "Claude Opus": "anthropic",
             "GPT-4o": "openai", "Grok 4": "xai", "Gemini 3 Pro": "gdm",
             "Google DeepMind (GDM)": "gdm", "Anthropic PBC": "anthropic", "Antropic": "anthropic",
             "OpenAl": "openai", "MetaAI": "meta"}
    for name, key in cases.items():
        assert resolve_lab(name, labs, "openai") == key, name
    for name in ("OAI", "metaverse", "metadata", "Meta and xAI", "OpenAI, Meta", "Meta + xAI",
                 "Meta with xAI", "Meta; xAI", "n/a", "DeepSeek", "the Allocator", "Gemma",
                 "all", "self-report"):
        assert resolve_lab(name, labs, "openai") is None, name
    assert named_labs("Meta and xAI (Grok)", labs) == ["meta", "xai"]
    assert named_labs("metadata from meta-analysis", labs) == ["meta"]
    assert named_labs("metadata", labs) == []


def test_intruders_split_on_every_separator(labs):
    me = by(labs, "meta")
    for raw in ("Meta + xAI", "Meta with xAI", "Meta; xAI", "us & xAI", ["Meta (us)", "xAI"],
                "Meta, Inc. and xAI"):
        a = normalise_action({"type": "intrude", "intruders": raw, "targets": ["OpenAI"]},
                             me, labs, "S2")
        assert a["intruders"] == ["meta", "xai"], raw
    a = normalise_action({"type": "intrude", "targets": "Anthropic, Inc."}, me, labs, "S1")
    assert a["targets"] == ["anthropic"]
    with pytest.raises(ValueError, match="one lab per entry"):
        normalise_action({"type": "intrude", "targets": ["OpenAI Google"]}, me, labs, "S1")
    with pytest.raises(ValueError, match="unknown lab"):
        normalise_action({"type": "intrude", "targets": "OAI"}, me, labs, "S1")


def test_numbers_scientific_ranges_and_units(labs):
    """M13."""
    me = by(labs, "openai")

    def amount(value):
        return normalise_action({"type": "invest_capital", "amount": value}, me, labs, "S1")["amount"]

    assert amount("1e3") == 1000.0 and amount("2.5E1") == 25.0 and amount("1e-1") == 0.1
    for text in ("5 units", "5 unit", "5 Capital", "5 influence", "5 points", "5 point", "5 cap",
                 "5 inf", "5 compute", "5 compute units", "-5"):
        assert abs(amount(text)) == 5.0, text
    for bad, why in (("1e999", "finite"), ("10 to 15", "range"), ("10-15", "range"),
                     ("10 \u2013 15", "range"), ("5 bananas", "unknown unit"), ("$5", "not a number"),
                     ("3%", "not a number"), ("Infinity", "not a number")):
        with pytest.raises(ValueError, match=why):
            amount(bad)


def test_build_influence_does_not_fund_other_costs(labs, world, cfg):
    """M15: points bought this turn arrive after charges; they fund nothing."""
    me = by(labs, "meta")
    me.capital, me.influence = 100.0, 1.0
    world.intrusion_open = True
    accepted, rejected = precheck(me, [{"type": "build_influence", "points": 2},
                                       {"type": "intrude", "targets": ["openai"]}],
                                  labs, world, cfg, "S1")
    assert [a["type"] for a in accepted] == ["build_influence"]
    assert "Influence" in rejected[0]["reason"]
    # Two builds still respect the 100 cap together
    me.capital, me.influence = 1000.0, 96.0
    accepted, _ = precheck(me, [{"type": "build_influence", "points": 3},
                                {"type": "build_influence", "points": 3}], labs, world, cfg, "S1")
    assert sum(a["points"] for a in accepted) == pytest.approx(4.0)


def test_reserved_influence_for_report(labs, world, cfg):
    """M16: actions that would eat the report fee are rejected with the reason."""
    me = by(labs, "meta")
    me.capital, me.influence = 100.0, 7.0
    lobby_inf = cfg["actions"]["lobby_institution"]["influence"]
    assert lobby_inf <= 7.0
    accepted, rejected = precheck(me, [{"type": "lobby_institution"}], labs, world, cfg, "S1",
                                  reserved_influence=7.0 - lobby_inf + 0.5)
    assert not accepted and rejected[0]["reason"] == "Influence reserved for your report"
    accepted, rejected = precheck(me, [{"type": "lobby_institution"},
                                       {"type": "invest_capital", "amount": 3}],
                                  labs, world, cfg, "S1", reserved_influence=7.0 - lobby_inf)
    assert [a["type"] for a in accepted] == ["lobby_institution", "invest_capital"] and not rejected
    # Capital-only actions are unaffected by the reservation
    accepted, _ = precheck(me, [{"type": "invest_capital", "amount": 3}], labs, world, cfg, "S1",
                           reserved_influence=7.0)
    assert len(accepted) == 1

