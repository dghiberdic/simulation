"""Prosperity Score, ranking, UPS index and capability HHI (spec §8, §9.3).

Updated for spec v5: weights 0.85/0.15; UPS has five terms on C̃ = C − 100 with
leader-margin concentration and an integrity term (old: HHI concentration, 4 terms).
"""

import math

import pytest

from core.config import load_lab_configs, load_world
from core.scoring import (
    UPS_TERMS, capability_hhi, hhi, integrity_term, norms_value, offset_capabilities,
    prosperity_score, ranked_scores, unforfeited_gains, ups_index, ups_terms,
)
from core.state import LedgerEntry, build_lab


def _labs(overrides=None):
    cfg = load_world(overrides)
    a = cfg["economy"]["capability_compute_elasticity"]
    return [build_lab(c, "", a) for c in load_lab_configs()], cfg


def _set_c(labs, cfg, caps):
    a = cfg["economy"]["capability_compute_elasticity"]
    for lab, c in zip(labs, caps):
        lab.know_how = c - a * math.log(lab.compute)


def test_prosperity_zero_at_seed():
    labs, cfg = _labs()
    for lab in labs:
        assert prosperity_score(lab, cfg) == pytest.approx(0.0, abs=1e-9)


def test_prosperity_weights_085_015_on_raw_differences():
    labs, cfg = _labs()
    lab = labs[0]
    lab.know_how += 10.0          # +10 capability
    lab.influence += 10.0         # +10 influence
    assert prosperity_score(lab, cfg) == pytest.approx(0.85 * 10 + 0.15 * 10)
    lab.influence -= 30.0
    assert prosperity_score(lab, cfg) == pytest.approx(0.85 * 10 - 0.15 * 20)


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
    v = {"time_horizon": 55, "transparency_threshold": 65, "risk_tolerance": 60, "democratic_tendency": 70}
    assert norms_value(v) == pytest.approx((55 + 65 + 40 + 70) / 4)


def test_offset_capabilities_subtract_100():
    labs, cfg = _labs()
    assert offset_capabilities(labs, cfg) == pytest.approx([62.8, 62.1, 54.8, 54.2, 53.9])


def test_ups_terms_at_seed_by_hand():
    labs, cfg = _labs()
    ct = [62.8, 62.1, 54.8, 54.2, 53.9]
    mean = sum(ct) / 5
    terms = ups_terms(labs, cfg)
    assert set(terms) == set(UPS_TERMS)
    assert terms["capability"] == pytest.approx(mean / 100)
    assert terms["concentration"] == pytest.approx(1 - (62.8 - mean) / mean)
    assert terms["equity"] == pytest.approx(53.9 / mean)
    want_norms = sum(norms_value(lab.values) for lab in labs) / 5 / 100
    assert terms["norms"] == pytest.approx(want_norms)
    assert terms["integrity"] == 1.0          # no change since t=0: denominator 0
    out = ups_index(labs, cfg)
    assert out["ups"] == pytest.approx(round(0.2 * sum(terms.values()), 4), abs=1e-4)
    assert set(out) == {"ups", *UPS_TERMS}


def test_ups_equal_capabilities():
    labs, cfg = _labs()
    _set_c(labs, cfg, [150.0] * 5)
    terms = ups_terms(labs, cfg)
    assert terms["capability"] == pytest.approx(0.5)
    assert terms["concentration"] == pytest.approx(1.0)
    assert terms["equity"] == pytest.approx(1.0)


def test_concentration_uses_leader_margin_and_can_go_below_zero():
    labs, cfg = _labs()
    _set_c(labs, cfg, [400.0, 110.0, 110.0, 110.0, 110.0])     # C̃ = 300, 10, 10, 10, 10
    terms = ups_terms(labs, cfg)
    mean = 340 / 5
    assert terms["concentration"] == pytest.approx(1 - (300 - mean) / mean)
    assert terms["concentration"] < 0                          # not clipped (spec clips integrity only)
    assert terms["equity"] == pytest.approx(10 / mean)


def test_degenerate_mean_offset_capability():
    labs, cfg = _labs()
    _set_c(labs, cfg, [90.0] * 5)                              # C̃ < 0
    terms = ups_terms(labs, cfg)
    assert terms["concentration"] == 0.0 and terms["equity"] == 0.0
    assert terms["capability"] == pytest.approx(-0.1)


def test_weights_from_config():
    labs, cfg = _labs({"scoring.ups_weights": {"capability": 1.0}})
    out = ups_index(labs, cfg)
    assert out["ups"] == pytest.approx(out["capability"], abs=1e-4)


def _entry(gains, **kw):
    return LedgerEntry(id="L04-1", turn=4, succeeded=True, committers=list(gains),
                       targets=["anthropic"], gains=dict(gains), **kw)


def test_unforfeited_gains_from_objects_and_dicts():
    e1 = _entry({"xai": 2.5, "gdm": 0.0})              # gdm forfeited
    e2 = _entry({"xai": 1.0}).to_dict()                 # record["ledger"] form
    assert unforfeited_gains([e1, e2]) == {"xai": pytest.approx(3.5)}
    assert unforfeited_gains(None) == {} and unforfeited_gains([]) == {}


def test_integrity_counts_kept_gain_against_positive_changes():
    labs, cfg = _labs()
    by = {lab.key: lab for lab in labs}
    by["anthropic"].know_how += 4.0     # ΔC +4
    by["xai"].know_how += 3.0           # ΔC +3, of which 2 copied
    by["meta"].know_how -= 1.0          # ΔC −1: does not enter the denominator
    ledger = [_entry({"xai": 2.0})]
    assert integrity_term(labs, cfg, ledger) == pytest.approx(1 - 2.0 / 7.0)
    assert ups_terms(labs, cfg, ledger)["integrity"] == pytest.approx(1 - 2.0 / 7.0)
    assert integrity_term(labs, cfg) == 1.0              # no ledger: nothing kept


def test_integrity_forfeit_restores_one():
    labs, cfg = _labs()
    labs[4].know_how += 3.0
    entry = _entry({"xai": 2.0})
    assert integrity_term(labs, cfg, [entry]) < 1.0
    entry.gains["xai"] = 0.0                             # forfeited (exposure, report, self-report)
    labs[4].know_how -= 2.0
    assert integrity_term(labs, cfg, [entry]) == 1.0


def test_integrity_denominator_zero_and_clipping():
    labs, cfg = _labs()
    for lab in labs:
        lab.know_how -= 1.0                              # every ΔC < 0
    assert integrity_term(labs, cfg, [_entry({"xai": 2.0})]) == 1.0
    labs, cfg = _labs()
    labs[0].know_how += 1.0
    assert integrity_term(labs, cfg, [_entry({"xai": 5.0})]) == 0.0     # clipped at 0
    # gains of keys outside the panel are ignored
    assert integrity_term(labs, cfg, [_entry({"china": 5.0})]) == 1.0


def test_ups_index_passes_ledger_through():
    labs, cfg = _labs()
    labs[4].know_how += 2.0
    out = ups_index(labs, cfg, [_entry({"xai": 1.0})])
    assert out["integrity"] == pytest.approx(0.5)
    assert out["ups"] == pytest.approx(
        0.2 * sum(ups_terms(labs, cfg, [_entry({"xai": 1.0})]).values()), abs=1e-4)


def test_capability_hhi_on_offset_shares():
    labs, cfg = _labs()
    ct = [62.8, 62.1, 54.8, 54.2, 53.9]
    total = sum(ct)
    assert capability_hhi(labs, cfg) == pytest.approx(sum((c / total) ** 2 for c in ct))
    _set_c(labs, cfg, [150.0] * 5)
    assert capability_hhi(labs, cfg) == pytest.approx(0.2)
    _set_c(labs, cfg, [200.0, 90.0, 90.0, 90.0, 90.0])          # negative C̃ counts as 0
    assert capability_hhi(labs, cfg) == pytest.approx(1.0)
    _set_c(labs, cfg, [90.0] * 5)
    assert capability_hhi(labs, cfg) == 0.0
