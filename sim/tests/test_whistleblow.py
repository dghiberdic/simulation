"""Whistleblowing against the hidden ledger: upheld, not upheld, self-report, duplicates, once-only."""

from datetime import date

import pytest

from core.config import load_lab_configs, load_world
from core.state import LedgerEntry, WorldState, build_lab
from core.whistleblow import resolve

CFG = load_world()
A = CFG["economy"]["capability_compute_elasticity"]
WCFG = CFG["whistleblow"]


def setup(entries):
    labs = [build_lab(c, "", A) for c in load_lab_configs()]
    world = WorldState(start=date(2026, 1, 1), turn=5, ledger=list(entries))
    return labs, world, {lab.key: lab for lab in labs}


def entry(**kw):
    base = dict(id="L03-1", turn=3, succeeded=True, committers=["openai", "xai"],
                targets=["anthropic"], gains={"openai": 2.0, "xai": 3.0})
    base.update(kw)
    return LedgerEntry(**base)


def test_upheld_forfeits_and_refunds():
    labs, world, k = setup([entry()])
    rep, acc = k["anthropic"].influence, k["openai"].influence
    know = k["openai"].know_how
    out = resolve({"anthropic": {"accused": "OpenAI", "turn": 3}}, labs, world, CFG)
    (o,) = out["outcomes"]
    assert o["result"] == "upheld" and o["entry_id"] == "L03-1"
    assert k["anthropic"].influence == rep
    assert k["openai"].influence == acc - WCFG["upheld_influence_penalty"]
    assert k["openai"].know_how == pytest.approx(know - 2.0)
    assert world.ledger[0].penalised == ["openai"] and world.ledger[0].gains["openai"] == 0.0
    assert world.notices == out["notices"] and len(out["notices"]) == 1


def test_not_upheld_costs_reporter():
    labs, world, k = setup([entry()])
    rep = k["anthropic"].influence
    out = resolve({"anthropic": {"accused": "meta"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "not_upheld"
    assert k["anthropic"].influence == rep - WCFG["fee_influence"] - WCFG["not_upheld_influence_penalty"]
    # Wrong turn also fails to match
    out = resolve({"gdm": {"accused": "openai", "turn": 4}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "not_upheld"


def test_self_report_and_partner_still_reportable():
    labs, world, k = setup([entry()])
    inf, know = k["xai"].influence, k["xai"].know_how
    out = resolve({"xai": {"accused": "self"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "self_report"
    assert k["xai"].influence == inf - WCFG["self_report_fee_influence"]
    assert k["xai"].know_how == pytest.approx(know - 3.0)
    out = resolve({"meta": {"accused": "OpenAI"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "upheld"


def test_duplicate_same_turn_both_upheld_penalty_once():
    labs, world, k = setup([entry()])
    acc = k["openai"].influence
    out = resolve({"anthropic": {"accused": "openai"}, "gdm": {"accused": "openai"}},
                  labs, world, CFG)
    assert [o["result"] for o in out["outcomes"]] == ["upheld", "upheld"]
    assert k["openai"].influence == acc - WCFG["upheld_influence_penalty"]
    assert world.ledger[0].penalised == ["openai"]


def test_once_only_after_exposure_or_failure():
    exposed = entry(exposed=["openai"], penalised=["openai"], gains={"openai": 0.0, "xai": 3.0})
    failed = entry(id="L04-1", turn=4, succeeded=False, committers=["meta"],
                   gains={"meta": 0.0}, penalised=["meta"])
    labs, world, k = setup([exposed, failed])
    out = resolve({"anthropic": {"accused": "openai"}, "gdm": {"accused": "meta"}},
                  labs, world, CFG)
    assert [o["result"] for o in out["outcomes"]] == ["not_upheld", "not_upheld"]
    # Second reporting turn: the already penalised committer cannot be hit again
    labs, world, k = setup([entry()])
    resolve({"anthropic": {"accused": "openai"}}, labs, world, CFG)
    world.turn = 6
    out = resolve({"gdm": {"accused": "openai"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "not_upheld"


def test_insufficient_influence_rejected():
    labs, world, k = setup([entry()])
    k["anthropic"].influence = 2.0
    out = resolve({"anthropic": {"accused": "openai"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "rejected"
    assert k["anthropic"].influence == 2.0 and world.ledger[0].penalised == []
