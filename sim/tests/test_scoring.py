"""Prosperity Score, ranking and UPS index (spec §8)."""

import pytest

from core.config import load_lab_configs, load_world
from core.scoring import hhi, norms_value, prosperity_score, ranked_scores, ups_index
from core.state import build_lab


def _labs():
    cfg = load_world()
    a = cfg["economy"]["capability_compute_elasticity"]
    return [build_lab(c, "", a) for c in load_lab_configs()], cfg


def test_prosperity_zero_at_seed():
    labs, cfg = _labs()
    for lab in labs:
        assert prosperity_score(lab, cfg) == pytest.approx(0.0, abs=1e-9)


def test_prosperity_rewards_capability_most():
    labs, cfg = _labs()
    lab = labs[0]
    lab.know_how += 10.0          # +10 capability
    lab.influence += 10.0         # +10 influence
    assert prosperity_score(lab, cfg) == pytest.approx(0.8 * 10 + 0.2 * 10)


def test_ranking_orders_by_score():
    labs, cfg = _labs()
    labs[2].know_how += 50.0
    rows = ranked_scores(labs, cfg)
    assert rows[0]["lab"] == "gdm" and rows[0]["rank"] == 1
    assert [r["rank"] for r in rows] == [1, 2, 3, 4, 5]


def test_hhi_bounds():
    assert hhi([1.0]) == pytest.approx(1.0)
    assert hhi([0.2] * 5) == pytest.approx(0.2)


def test_norms_inverts_risk_tolerance():
    v = {"time_horizon": 100, "transparency_threshold": 100, "risk_tolerance": 100, "democratic_tendency": 100}
    assert norms_value(v) == pytest.approx((100 + 100 + 0 + 100) / 4)


def test_ups_components_in_range():
    labs, cfg = _labs()
    out = ups_index(labs, cfg)
    for key in ("ups", "capability", "concentration", "equity", "norms"):
        assert 0.0 <= out[key] <= 1.0
    # Equal capabilities → concentration 1−1/5, equity 1.
    for lab in labs:
        lab.know_how = 50.0 - cfg["economy"]["capability_compute_elasticity"] * __import__("math").log(lab.compute)
    out = ups_index(labs, cfg)
    assert out["concentration"] == pytest.approx(0.8)
    assert out["equity"] == pytest.approx(1.0)
