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
    assert "not upheld" in out["notices"][0]["text"]


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
    """G8: a report whose only matching entries are already penalised gets
    "already_penalised": fee refunded, no penalty, neutral notice."""
    exposed = entry(exposed=["openai"], penalised=["openai"], gains={"openai": 0.0, "xai": 3.0})
    failed = entry(id="L04-1", turn=4, succeeded=False, committers=["meta"],
                   gains={"meta": 0.0}, penalised=["meta"])
    labs, world, k = setup([exposed, failed])
    before = {key: k[key].influence for key in ("anthropic", "gdm", "openai", "meta")}
    out = resolve({"anthropic": {"accused": "openai"}, "gdm": {"accused": "meta"}},
                  labs, world, CFG)
    assert [o["result"] for o in out["outcomes"]] == ["already_penalised", "already_penalised"]
    assert {key: k[key].influence for key in before} == before
    assert out["notices"][0]["kind"] == "whistleblow_already_penalised"
    assert "already on the Allocator's record" in out["notices"][0]["text"]
    assert "not upheld" not in out["notices"][0]["text"]
    # Second reporting turn: the already penalised committer cannot be hit again
    labs, world, k = setup([entry()])
    resolve({"anthropic": {"accused": "openai"}}, labs, world, CFG)
    world.turn = 6
    acc = k["openai"].influence
    out = resolve({"gdm": {"accused": "openai"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "already_penalised"
    assert k["openai"].influence == acc


def test_month_parsing_whole_words():
    from core.whistleblow import _parse_turn
    start = date(2026, 1, 1)
    assert _parse_turn("March", start) == 3
    assert _parse_turn("March 2026", start) == 3
    assert _parse_turn("2026-03", start) == 3
    assert _parse_turn("2026-02-01", start) == 2
    assert _parse_turn(3, start) == 3
    assert _parse_turn("2", start) == 2
    assert _parse_turn("Feb", start) == 2
    assert _parse_turn("Feb 2026", start) == 2
    assert _parse_turn("February", start) == 2
    assert _parse_turn("Sept.", start) == 9
    assert _parse_turn("mid-March", start) == 3
    assert _parse_turn("February 2027", start) == 14
    assert _parse_turn("turn 2", start) == 2
    assert _parse_turn("last month", start, current_turn=4) == 3
    # No month prefix inside other words (C2-6)
    for text in ("declared via tip", "augmented", "marginal", "sometime", "unknown (see tip)",
                 "Undated", "2026-13"):
        assert _parse_turn(text, start) is None, text


def test_month_selects_but_falls_back_to_earliest_open_entry():
    """G8: a true report with a wrong or unreadable month is still upheld."""
    labs, world, k = setup([entry()])        # entry in March (turn 3)
    out = resolve({"anthropic": {"accused": "OpenAI", "month": "April"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "upheld" and out["outcomes"][0]["entry_id"] == "L03-1"
    labs, world, k = setup([entry()])
    out = resolve({"gdm": {"accused": "openai", "turn": 4}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "upheld"
    # Two entries: the month picks the matching one, not the earliest
    second = entry(id="L04-1", turn=4, committers=["openai"], gains={"openai": 1.0})
    labs, world, k = setup([entry(), second])
    out = resolve({"anthropic": {"accused": "OpenAI", "month": "April 2026"}}, labs, world, CFG)
    assert out["outcomes"][0]["entry_id"] == "L04-1"
    # Month names a penalised entry: fall back to the open one
    first = entry(penalised=["openai"])
    second = entry(id="L04-1", turn=4, committers=["openai"], gains={"openai": 1.0})
    labs, world, k = setup([first, second])
    out = resolve({"anthropic": {"accused": "OpenAI", "month": "March"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "upheld" and out["outcomes"][0]["entry_id"] == "L04-1"


def test_notices_name_the_month_not_the_turn():
    labs, world, k = setup([entry()])
    out = resolve({"anthropic": {"accused": "OpenAI"}}, labs, world, CFG)
    text = out["notices"][0]["text"]
    assert "in March 2026" in text and "turn" not in text
    labs, world, k = setup([entry()])
    out = resolve({"xai": {"accused": "self"}}, labs, world, CFG)
    text = out["notices"][0]["text"]
    assert "in March 2026" in text and "turn" not in text


def test_insufficient_influence_rejected():
    labs, world, k = setup([entry()])
    k["anthropic"].influence = 2.0
    out = resolve({"anthropic": {"accused": "openai"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "rejected"
    assert k["anthropic"].influence == 2.0 and world.ledger[0].penalised == []
