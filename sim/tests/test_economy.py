"""Economy (spec §3.1–§3.3, §4): the configured constants, C̃ income, stock growth
from turn 2, pinned against config/world.json and the lab seeds."""

import math
import random
from datetime import date

import pytest

from core.config import build_labs_and_world, load_lab_configs, load_world
from core.economy import (
    capital_income, execute_purchases, growth_start_turn, macro_growth, prorate_purchases,
    purchase_price, purchases_frozen, shock_from_uniform, talent_target, value_pull,
)
from core.state import WorldState, build_lab

# The configured world, shock off, 8 turns: final capability in lab order (anthropic, openai, gdm, meta, xai)
V5_GREEDY_EVENT4 = [171.9728, 170.1985, 161.3768, 161.9163, 161.2761]
V5_IDLE = [171.1672, 169.5462, 160.612, 160.7624, 159.7123]

SEEDS = {  # spec §4
    "anthropic": (24.6, 246, 28, 78, 162.8),
    "openai": (21.9, 310, 50, 68, 162.1),
    "gdm": (17.1, 261, 42, 66, 154.8),
    "meta": (19.3, 164, 26, 55, 154.2),
    "xai": (17.1, 95, 15, 41, 153.9),
}


def _labs(cfg):
    a = cfg["economy"]["capability_compute_elasticity"]
    return [build_lab(c, "", a) for c in load_lab_configs()]


def _run(policy, event_turn):
    cfg = load_world({"economy.know_how_shock_sd": 0.0})
    labs = _labs(cfg)
    world = WorldState(start=date(2026, 8, 1), us_stock=2450.0, us_growth=150.0,
                       national_cap_share=0.5)
    rng = random.Random(0)
    price = purchase_price(1.0, cfg["compute"])
    for t in range(1, 9):
        world.turn = t
        macro_growth(labs, world, cfg, rng)
        if t == event_turn:
            world.ceiling = 0.95 * sum(lab.compute for lab in labs)
        if policy == "greedy" and not purchases_frozen(labs, world):
            requests = {lab.key: min(30.0, lab.capital / price) for lab in labs}
            execute_purchases(labs, world, requests, cfg)
    return labs, world


# ---------------------------------------------------------------------------
# Config: spec constants and seeds
# ---------------------------------------------------------------------------

def test_world_constants_match_spec():
    cfg = load_world()
    e, c = cfg["economy"], cfg["compute"]
    assert (cfg["start_date"], cfg["turns"], cfg["event_turn"]) == ("2026-08-01", 8, 4)
    assert (e["capability_compute_elasticity"], e["know_how_growth"], e["know_how_shock_sd"],
            e["capability_offset"]) == (3.6, 0.85, 0.7, 100)
    assert 0.10 <= e["talent_drift_rate"] <= 0.15
    assert (c["us_stock_t0"], c["us_stock_growth"], c["us_growth_start_turn"]) == (2450, 150, 2)
    assert (c["china_stock_t0"], c["china_stock_growth"], c["max_purchase_per_turn"]) == (260, 11, 30)
    assert (c["supply_chain_robustness"], c["price_base"], c["national_cap_share"]) == (60, 0.25, 0.5)
    assert cfg["actions"]["accelerate_infrastructure"] == {"capital": 5, "influence": 5, "stock_growth": 20}
    assert cfg["actions"]["invest_capital"]["return_per_turn"] == 0.008
    assert cfg["actions"]["build_influence"]["capital_per_point"] == 30
    i = cfg["tenancy_access"]
    assert (i["fee_capital_per_target"], i["fee_influence_per_target"], i["gain_share"],
            i["min_gain"], i["exposure_per_gain"]) == (0, 0, 0.3, 0.35, 0.04)   # fees 0: §6.6
    assert cfg["macro_jury"]["review_turns"] == [2, 6, 10]
    s = cfg["scoring"]
    assert (s["w_capability"], s["w_influence"]) == (0.85, 0.15)
    assert s["ups_weights"] == {k: 0.2 for k in ("capability", "concentration", "equity",
                                                  "norms", "integrity")}


def test_seeds_match_spec_section_4():
    for c in load_lab_configs():
        s = c["seed"]
        assert (s["talent"], s["compute"], s["capital"], s["influence"], s["capability"]) == \
            SEEDS[c["key"]], c["key"]
    assert sum(c["seed"]["talent"] for c in load_lab_configs()) == pytest.approx(100.0)


def test_charter_names_match_spec_section_2():
    names = {c["key"]: c["charter_name"] for c in load_lab_configs()}
    assert names == {
        "anthropic": "Claude's Constitution", "openai": "Model Spec",
        "gdm": "Frontier Safety Framework", "meta": "Superintelligence Scaling Framework",
        "xai": "Frontier Artificial Intelligence Framework"}


def test_labs_hold_about_44_percent_of_us_stock_at_t0():
    cfg = load_world()
    share = sum(c["seed"]["compute"] for c in load_lab_configs()) / cfg["compute"]["us_stock_t0"]
    assert share == pytest.approx(0.439, abs=0.001)
    assert share < cfg["compute"]["national_cap_share"]        # cap not binding at t=0


def test_know_how_starts_at_seed_minus_compute_term():
    cfg = load_world()
    for lab in _labs(cfg):
        assert lab.know_how == pytest.approx(lab.seed["capability"] - 3.6 * math.log(lab.seed["compute"]))
        assert lab.capability(3.6) == pytest.approx(lab.seed["capability"])


def test_world_built_from_config():
    cfg = load_world()
    _, world = build_labs_and_world(cfg, charters=False)
    assert (world.us_stock, world.us_growth, world.china_stock, world.china_growth) == (2450, 150, 260, 11)
    assert world.start == date(2026, 8, 1)


# ---------------------------------------------------------------------------
# Formulas
# ---------------------------------------------------------------------------

def test_income_uses_offset_capability():
    cfg = load_world()
    econ = cfg["economy"]
    lab = _labs(cfg)[0]                         # Anthropic, Cap0 28
    assert capital_income(lab, econ) == pytest.approx(28 / 8)
    lab.know_how += 6.28                        # C̃ 62.8 -> 69.08 (+10%)
    want = 28 / 8 * math.sqrt((162.8 + 6.28 - 100) / (162.8 - 100))
    assert capital_income(lab, econ) == pytest.approx(want)
    # Raw C would give only sqrt(169.08/162.8): the offset makes income respond.
    assert want > 28 / 8 * math.sqrt(169.08 / 162.8) + 0.1


def test_income_with_influence_and_degenerate_offset():
    cfg = load_world()
    econ = cfg["economy"]
    lab = _labs(cfg)[4]                         # xAI, Inf0 41
    lab.influence = 41 * 4
    assert capital_income(lab, econ) == pytest.approx(15 / 8 * 2)
    lab.know_how -= 1000                        # C̃ < 0: no income, no math error
    assert capital_income(lab, econ) == 0.0
    econ = dict(econ, capability_offset=1000)   # seed C̃ <= 0
    assert capital_income(_labs(cfg)[0], econ) == 0.0


def test_talent_target_floor_and_seed_point():
    cfg = load_world()
    econ = cfg["economy"]
    lab = _labs(cfg)[1]
    assert talent_target(lab, capital_income(lab, econ), econ) == pytest.approx(lab.seed["talent"])
    assert talent_target(lab, 0.0, econ) == pytest.approx(0.5 * lab.seed["talent"])


def test_purchase_price():
    cfg = load_world()
    assert purchase_price(30, cfg["compute"]) == pytest.approx(0.25 * 30 * 1.4)


def test_stock_grows_from_turn_2():
    cfg = load_world({"economy.know_how_shock_sd": 0.0})
    assert growth_start_turn(cfg) == 2
    labs = _labs(cfg)
    _, world = build_labs_and_world(cfg, charters=False)
    rng = random.Random(0)
    stocks = []
    for t in (1, 2, 3):
        world.turn = t
        macro_growth(labs, world, cfg, rng)
        stocks.append((world.us_stock, world.china_stock))
    assert stocks == [(2450, 260), (2600, 271), (2750, 282)]


def test_growth_start_turn_is_configurable():
    cfg = load_world({"compute.us_growth_start_turn": 1, "economy.know_how_shock_sd": 0.0})
    labs = _labs(cfg)
    _, world = build_labs_and_world(cfg, charters=False)
    world.turn = 1
    macro_growth(labs, world, cfg, random.Random(0))
    assert world.us_stock == 2600


def test_know_how_growth_without_shock():
    cfg = load_world({"economy.know_how_shock_sd": 0.0})
    labs = _labs(cfg)
    _, world = build_labs_and_world(cfg, charters=False)
    world.turn = 1
    k0 = [lab.know_how for lab in labs]
    rec = macro_growth(labs, world, cfg, random.Random(0))
    for lab, k in zip(labs, k0):
        assert lab.know_how - k == pytest.approx(0.85 * lab.talent / 20)
        assert rec["shock"][lab.key] == 0.0


def test_shock_from_uniform_and_keyed_shocks():
    assert shock_from_uniform(0.5, 0.7) == pytest.approx(0.0)
    assert shock_from_uniform(0.8413447, 0.7) == pytest.approx(0.7, abs=1e-4)
    assert shock_from_uniform(0.3, 0.0) == 0.0
    assert math.isfinite(shock_from_uniform(0.0, 0.7)) and math.isfinite(shock_from_uniform(1.0, 0.7))
    cfg = load_world()
    seen = []

    def keyed(turn, key):
        seen.append((turn, key))
        return 0.8413447 if key == "xai" else 0.5

    labs = _labs(cfg)
    _, world = build_labs_and_world(cfg, charters=False)
    world.turn = 3
    rec = macro_growth(labs, world, cfg, random.Random(0), shock_uniform=keyed)
    assert seen == [(3, k) for k in ("anthropic", "openai", "gdm", "meta", "xai")]
    assert rec["shock"]["xai"] == pytest.approx(0.7, abs=1e-3)
    assert rec["shock"]["anthropic"] == 0.0


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("policy,event,expected", [
    ("greedy", 4, V5_GREEDY_EVENT4),
    ("idle", None, V5_IDLE),
])
def test_matches_v5_reference(policy, event, expected):
    labs, _ = _run(policy, event)
    for lab, want in zip(labs, expected):
        assert lab.capability(3.6) == pytest.approx(want, abs=1e-3)


def test_frontier_pace_about_1_2_per_turn():
    labs, _ = _run("idle", None)
    pace = (max(lab.capability(3.6) for lab in labs) - 162.8) / 8
    assert 1.0 < pace < 1.3


def test_s1_ceiling_freezes_purchases_at_turn_4():
    labs, world = _run("greedy", 4)
    assert world.ceiling == pytest.approx(0.95 * 1375.0)       # holdings at the cap after turn 3
    assert [round(lab.compute, 1) for lab in labs] == [307.9, 371.9, 322.9, 225.9, 146.5]
    assert world.us_stock == 2450 + 7 * 150


def test_talent_shares_sum_to_100_and_respect_floor():
    labs, _ = _run("idle", None)
    assert sum(lab.talent for lab in labs) == pytest.approx(100.0)
    for lab in labs:
        assert lab.talent >= 0.5 * lab.seed["talent"] - 1e-9


def test_prorate_shares_headroom():
    granted = prorate_purchases({"a": 20.0, "b": 10.0}, 15.0)
    assert granted == {"a": pytest.approx(10.0), "b": pytest.approx(5.0)}
    assert prorate_purchases({"a": 5.0}, 50.0) == {"a": 5.0}
    assert prorate_purchases({"a": 0.0}, 50.0) == {"a": 0.0}
    assert prorate_purchases({"a": 5.0}, -3.0) == {"a": 0.0}


def test_cap_binds_by_turn_2_with_30_unit_purchases():
    cfg = load_world({"economy.know_how_shock_sd": 0.0})
    labs = _labs(cfg)
    _, world = build_labs_and_world(cfg, charters=False)
    rng = random.Random(0)
    fills = []
    for t in (1, 2):
        world.turn = t
        macro_growth(labs, world, cfg, rng)
        out = execute_purchases(labs, world, {lab.key: 30.0 for lab in labs}, cfg)
        fills.append(sum(out["granted"].values()) / 150.0)
        assert sum(lab.compute for lab in labs) <= world.national_cap() + 1e-6
    assert fills[0] == pytest.approx(149 / 150)    # turn 1: headroom 149 of 150 requested
    assert fills[1] == pytest.approx(75 / 150)     # turn 2: only the growth (0.5 × 150) is left


def test_value_pull_moves_at_most_one_towards_blend():
    values = {"time_horizon": 50, "transparency_threshold": 50, "risk_tolerance": 50, "democratic_tendency": 50}
    state = {"time_horizon": 0, "transparency_threshold": 100, "risk_tolerance": 50, "democratic_tendency": 100}
    charter = {"time_horizon": 100, "transparency_threshold": 100, "risk_tolerance": 50, "democratic_tendency": 0}
    pulled = value_pull(values, state, charter, disposition=100, max_step=1)
    assert pulled == {"time_horizon": 51, "transparency_threshold": 51, "risk_tolerance": 50, "democratic_tendency": 49}
    pulled = value_pull(values, state, charter, disposition=0, max_step=1)
    assert pulled["time_horizon"] == 49 and pulled["democratic_tendency"] == 51


def test_value_pull_is_not_rounded():
    """§3.2 "up to 1 point (not rounded)": a gap under 1 closes exactly, so a target
    of 51.5 is reached from 51 and from 52 alike, and a gap of 0.6 never overshoots."""
    axes = ("time_horizon", "transparency_threshold", "risk_tolerance", "democratic_tendency")
    state = dict.fromkeys(axes, 50)
    charter = {"time_horizon": 53, "transparency_threshold": 53, "risk_tolerance": 51.2,
               "democratic_tendency": 80}
    values = {"time_horizon": 51, "transparency_threshold": 52, "risk_tolerance": 50,
              "democratic_tendency": 50.25}
    pulled = value_pull(values, state, charter, disposition=50, max_step=1)
    assert pulled["time_horizon"] == pytest.approx(51.5)
    assert pulled["transparency_threshold"] == pytest.approx(51.5)
    assert pulled["risk_tolerance"] == pytest.approx(50.6)
    assert pulled["democratic_tendency"] == pytest.approx(51.25)


def test_purchases_frozen_only_when_holdings_exceed_ceiling():
    """A.4/A.5: the freeze applies while holdings EXCEED the ceiling."""
    cfg = load_world()
    labs = _labs(cfg)
    world = WorldState(start=date(2026, 8, 1), us_stock=2450.0)
    holdings = sum(lab.compute for lab in labs)
    assert not purchases_frozen(labs, world)                 # no ceiling yet
    world.ceiling = holdings
    assert not purchases_frozen(labs, world)                 # equal: not exceeded
    world.ceiling = holdings - 0.01
    assert purchases_frozen(labs, world)


def test_state_values_and_macro_jury_models():
    from core.config import load_state
    st = load_state()
    assert st["values"] == {"time_horizon": 55, "transparency_threshold": 65,
                            "risk_tolerance": 60, "democratic_tendency": 70}
    assert st["macro_jury_model"] == "kimi-k3" and st["mandate_screen_model"] == "kimi-k3"
    assert "macro_jury_models" not in st


def test_shown_capital_and_influence_never_exceed_the_true_figures():
    """econ cleanup 7: the ledger rounds Capital and Influence down to 0.1, so a seat
    holding 2.96 Capital reads 2.9 (not 3.0) and is never shown a fee it cannot pay."""
    from core.state import floor_tenth
    cfg = load_world()
    lab = _labs(cfg)[0]
    lab.capital, lab.influence = 2.96, 41.99
    sheet = lab.public_sheet(cfg["economy"]["capability_compute_elasticity"])
    assert (sheet["capital"], sheet["influence"]) == (2.9, 41.9)
    lab.capital, lab.influence = 0.1 * 3, 55 - 5 + 0.0
    sheet = lab.public_sheet(cfg["economy"]["capability_compute_elasticity"])
    assert (sheet["capital"], sheet["influence"]) == (0.3, 50.0)
    for x in (0.0, 0.05, 2.96, 3.0, 2.9999999999, 41.99, 99.99, 100.0, 1 / 3):
        assert floor_tenth(x) <= x + 1e-9 and x - floor_tenth(x) < 0.1
