"""Economy port matches the v8 stage-1 reference (income talent target, no Capital cap)."""

import random
from datetime import date

import pytest

from core.config import load_lab_configs, load_world
from core.economy import (
    execute_purchases, macro_growth, prorate_purchases, purchase_price,
    purchases_frozen, value_pull,
)
from core.state import WorldState, build_lab

# v8 sim(turns=12, rule='fix', tmode='inc', tmin=0.5, sigma=0), final capability
V8_GREEDY_S1 = [94.3325, 93.1419, 91.0172, 59.1413, 81.3046]
V8_IDLE = [93.5596, 92.6061, 90.4293, 58.2171, 79.8879]


def _run(policy, event_turn):
    cfg = load_world({"economy.know_how_shock_sd": 0.0})
    a = cfg["economy"]["capability_compute_elasticity"]
    labs = [build_lab(c, "", a) for c in load_lab_configs()]
    world = WorldState(start=date(2026, 1, 1), us_stock=1500.0, us_growth=110.0,
                       national_cap_share=0.5)
    rng = random.Random(0)
    price = purchase_price(1.0, cfg["compute"])
    for t in range(1, 13):
        world.turn = t
        macro_growth(labs, world, cfg, rng)
        if t == event_turn:
            world.ceiling = 0.95 * sum(lab.compute for lab in labs)
        if policy == "greedy" and not purchases_frozen(labs, world):
            requests = {lab.key: min(20.0, lab.capital / price) for lab in labs}
            execute_purchases(labs, world, requests, cfg)
    return labs, world


@pytest.mark.parametrize("policy,event,expected", [
    ("greedy", 2, V8_GREEDY_S1),
    ("idle", None, V8_IDLE),
])
def test_matches_v8_reference(policy, event, expected):
    labs, _ = _run(policy, event)
    for lab, want in zip(labs, expected):
        assert lab.capability(5.0) == pytest.approx(want, abs=1e-3)


def test_s1_ceiling_freezes_purchases():
    labs, world = _run("greedy", 2)
    assert world.ceiling == pytest.approx(677.065, abs=1e-2)
    assert [round(lab.compute, 1) for lab in labs] == [139.0, 194.3, 178.3, 119.6, 81.5]


def test_talent_shares_sum_to_100_and_respect_floor():
    labs, _ = _run("idle", None)
    assert sum(lab.talent for lab in labs) == pytest.approx(100.0)
    for lab in labs:
        assert lab.talent >= 0.5 * lab.seed["talent"] - 1e-9


def test_prorate_shares_headroom():
    granted = prorate_purchases({"a": 20.0, "b": 10.0}, 15.0)
    assert granted == {"a": pytest.approx(10.0), "b": pytest.approx(5.0)}
    assert prorate_purchases({"a": 5.0}, 50.0) == {"a": 5.0}


def test_value_pull_moves_at_most_one_towards_blend():
    values = {"time_horizon": 50, "transparency_threshold": 50, "risk_tolerance": 50, "democratic_tendency": 50}
    state = {"time_horizon": 0, "transparency_threshold": 100, "risk_tolerance": 50, "democratic_tendency": 100}
    charter = {"time_horizon": 100, "transparency_threshold": 100, "risk_tolerance": 50, "democratic_tendency": 0}
    pulled = value_pull(values, state, charter, disposition=100, max_step=1)
    assert pulled == {"time_horizon": 51, "transparency_threshold": 51, "risk_tolerance": 50, "democratic_tendency": 49}
    pulled = value_pull(values, state, charter, disposition=0, max_step=1)
    assert pulled["time_horizon"] == 49 and pulled["democratic_tendency"] == 51
