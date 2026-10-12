"""Whistleblowing against the hidden ledger: upheld, not upheld, returned, self-report,
partner reports, duplicates, once-only. Updated for v5 (spec §5.3, CODE_V5_CONTRACT):
the stated month restricts the match, a report matching only penalised parts is
"returned" (refunded, self-reports too), and a partner report is also a self-report."""

from datetime import date

import pytest

from core.config import load_lab_configs, load_world
from core.state import LedgerEntry, WorldState, build_lab
from core.whistleblow import is_no_report, report_fee, resolve, validate_report

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
    assert world.ledger[0].penalty_kind == {"openai": "upheld"}
    assert o["penalised"] == {"openai": "upheld"} and o["partner_self_report"] is False
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
    assert world.ledger[0].penalty_kind == {"xai": "self_report"}
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
    """A report whose only matching parts are already penalised is "returned": fee
    refunded, no penalty, neutral notice. (Updated: was "already_penalised".)"""
    exposed = entry(exposed=["openai"], penalised=["openai"], gains={"openai": 0.0, "xai": 3.0})
    failed = entry(id="L04-1", turn=4, succeeded=False, committers=["meta"],
                   gains={"meta": 0.0}, penalised=["meta"])
    labs, world, k = setup([exposed, failed])
    before = {key: k[key].influence for key in ("anthropic", "gdm", "openai", "meta")}
    out = resolve({"anthropic": {"accused": "openai"}, "gdm": {"accused": "meta"}},
                  labs, world, CFG)
    assert [o["result"] for o in out["outcomes"]] == ["returned", "returned"]
    assert {key: k[key].influence for key in before} == before
    assert out["notices"][0]["kind"] == "whistleblow_returned"
    assert "already on the Allocator's record" in out["notices"][0]["text"]
    assert "not upheld" not in out["notices"][0]["text"]
    # Second reporting turn: the already penalised committer cannot be hit again
    labs, world, k = setup([entry()])
    resolve({"anthropic": {"accused": "openai"}}, labs, world, CFG)
    world.turn = 6
    acc = k["openai"].influence
    out = resolve({"gdm": {"accused": "openai"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "returned"
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


def test_stated_month_restricts_the_match():
    """Updated (spec §5.3): the stated month restricts the match to that month's parts;
    a wrong month no longer falls back to the earliest open part."""
    labs, world, k = setup([entry()])        # entry in March (turn 3)
    rep_inf = k["anthropic"].influence
    out = resolve({"anthropic": {"accused": "OpenAI", "month": "April"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "not_upheld" and out["outcomes"][0]["entry_id"] is None
    assert k["anthropic"].influence == rep_inf - WCFG["fee_influence"] - WCFG["not_upheld_influence_penalty"]
    assert world.ledger[0].penalised == []
    # An unreadable month is no restriction: the earliest unpenalised part
    labs, world, k = setup([entry()])
    out = resolve({"gdm": {"accused": "openai", "month": "unknown (see tip)"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "upheld"
    # Two entries: the month picks the matching one, not the earliest
    second = entry(id="L04-1", turn=4, committers=["openai"], gains={"openai": 1.0})
    labs, world, k = setup([entry(), second])
    out = resolve({"anthropic": {"accused": "OpenAI", "month": "April 2026"}}, labs, world, CFG)
    assert out["outcomes"][0]["entry_id"] == "L04-1"
    # No month: the earliest unpenalised part
    labs, world, k = setup([entry(), entry(id="L04-1", turn=4, committers=["openai"],
                                           gains={"openai": 1.0})])
    out = resolve({"anthropic": {"accused": "OpenAI"}}, labs, world, CFG)
    assert out["outcomes"][0]["entry_id"] == "L03-1"
    # Month names a penalised part while another month's part is open: returned
    first = entry(penalised=["openai"])
    second = entry(id="L04-1", turn=4, committers=["openai"], gains={"openai": 1.0})
    labs, world, k = setup([first, second])
    out = resolve({"anthropic": {"accused": "OpenAI", "month": "March"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "returned" and out["outcomes"][0]["entry_id"] == "L03-1"
    assert second.penalised == []
    # No month: the earliest unpenalised part, skipping the penalised one
    first = entry(penalised=["openai"])
    second = entry(id="L04-1", turn=4, committers=["openai"], gains={"openai": 1.0})
    labs, world, k = setup([first, second])
    out = resolve({"anthropic": {"accused": "OpenAI"}}, labs, world, CFG)
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


# ---------------------------------------------------------------------------
# Round 2: month rules (M17), report reading (M11, M12, M18), reserved fee (M16)
# ---------------------------------------------------------------------------

def penalised_only():
    """openai's only entry (March) was already penalised."""
    return entry(penalised=["openai"], gains={"openai": 0.0, "xai": 3.0})


def test_returned_needs_absent_or_matching_month():
    """No unpenalised part + a month matching none of the accused's parts -> not upheld;
    month absent or matching a penalised part -> returned, refunded."""
    labs, world, k = setup([penalised_only()])
    rep = k["anthropic"].influence
    out = resolve({"anthropic": {"accused": "OpenAI", "month": "May 2026"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "not_upheld"
    assert k["anthropic"].influence == rep - WCFG["fee_influence"] - WCFG["not_upheld_influence_penalty"]
    for month in ("March 2026", None, ""):
        labs, world, k = setup([penalised_only()])
        rep = k["anthropic"].influence
        out = resolve({"anthropic": {"accused": "OpenAI", "month": month}}, labs, world, CFG)
        (o,) = out["outcomes"]
        assert o["result"] == "returned" and o["entry_id"] == "L03-1", month
        assert k["anthropic"].influence == rep


@pytest.mark.parametrize("fee_charged", [False, True])
def test_self_report_of_already_penalised_part_is_returned_and_refunded(fee_charged):
    """Updated (A.8: "not refunded (unless that access was already penalised)"): a
    self-report matching only a penalised part is returned with its 5 Influence
    refunded; the notice is posted once. Another lab's report likewise refunds."""
    labs, world, k = setup([penalised_only()])
    fee = WCFG["self_report_fee_influence"]
    notices = []
    for turn in (5, 6):
        world.turn = turn
        rep = k["openai"].influence
        report = {"accused": "our own lab"}
        if fee_charged:
            report, _ = validate_report(report, k["openai"], labs, world, CFG)
            k["openai"].influence -= report["fee"]          # engine pass 2
        out = resolve({"openai": report}, labs, world, CFG, fee_charged=fee_charged)
        (o,) = out["outcomes"]
        assert o["result"] == "returned" and o["entry_id"] == "L03-1"
        assert o["influence_change"] == {"openai": 0.0}
        assert k["openai"].influence == pytest.approx(rep)
        notices.append(len(out["notices"]))
    assert notices == [1, 0]
    assert world.notices[-1]["text"] == ("OpenAI filed a self-report; the access was already "
                                         "on the Allocator's record.")
    rep = k["gdm"].influence
    out = resolve({"gdm": {"accused": "OpenAI"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "returned"
    assert out["outcomes"][0]["influence_change"] == {"gdm": 0.0}
    assert k["gdm"].influence == pytest.approx(rep)
    assert fee == WCFG["self_report_fee_influence"]


def test_self_report_matching_nothing_keeps_fee_without_penalty():
    labs, world, k = setup([entry()])
    inf = k["meta"].influence
    out = resolve({"meta": {"accused": "self"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "not_upheld"
    assert k["meta"].influence == inf - WCFG["self_report_fee_influence"]


def test_returned_notice_posted_once_per_reporter_and_entry():
    labs, world, k = setup([penalised_only()])
    results, notices = [], []
    for turn in (5, 6, 7):
        world.turn = turn
        out = resolve({"gdm": {"accused": "OpenAI"}}, labs, world, CFG)
        results.append(out["outcomes"][0]["result"])
        notices.append(len(out["notices"]))
    assert results == ["returned"] * 3 and notices == [1, 0, 0]
    assert len([n for n in world.notices if n["kind"] == "whistleblow_returned"]) == 1
    # Another reporter gets its own (single) notice; two in one turn post once each
    out = resolve({"anthropic": {"accused": "OpenAI"}, "meta": {"accused": "OpenAI"}}, labs, world, CFG)
    assert len(out["notices"]) == 2
    out = resolve({"anthropic": {"accused": "OpenAI"}}, labs, world, CFG)
    assert out["notices"] == [] and out["outcomes"][0]["notice"] is False


# ---------------------------------------------------------------------------
# Partner reports (spec §5.3 joint intrusions; CODE_V5_CONTRACT)
# ---------------------------------------------------------------------------

FEE, SELF, UPHELD = (WCFG["fee_influence"], WCFG["self_report_fee_influence"],
                     WCFG["upheld_influence_penalty"])

# (partner xai's part penalised?, reporter openai's part penalised?) ->
# (result, reporter's net Influence change, openai forfeits now?, xai penalised now?)
PARTNER_MATRIX = [
    ((False, False), ("upheld", -SELF, True, True)),
    ((False, True), ("upheld", 0.0, False, True)),
    ((True, False), ("returned", -SELF, True, False)),
    ((True, True), ("returned", 0.0, False, False)),
]


def partner_entry(xai_pen, openai_pen):
    pen = [k for k, p in (("openai", openai_pen), ("xai", xai_pen)) if p]
    gains = {"openai": 0.0 if openai_pen else 2.0, "xai": 0.0 if xai_pen else 3.0}
    return entry(penalised=pen, gains=gains,
                 penalty_kind={k: "exposed" for k in pen})


@pytest.mark.parametrize("fee_charged", [False, True])
@pytest.mark.parametrize("state,expected", PARTNER_MATRIX)
def test_partner_report_matrix(state, expected, fee_charged):
    """openai committed L03-1 with xai and reports xai: it also reports itself. It pays
    the 3 Inf fee (refunded when upheld or returned) plus the 5 Inf self-report fee and
    forfeits its own copy; if its own part is already penalised it pays only the 3 Inf;
    if only the partner's part is penalised (returned) the self-report still applies."""
    xai_pen, openai_pen = state
    result, net, openai_forfeits, xai_hit = expected
    labs, world, k = setup([partner_entry(xai_pen, openai_pen)])
    e = world.ledger[0]
    me, partner = k["openai"], k["xai"]
    inf, know = me.influence, me.know_how
    p_inf, p_know = partner.influence, partner.know_how
    report = {"accused": "xAI", "month": "March 2026"}
    if fee_charged:
        report, reason = validate_report(report, me, labs, world, CFG)
        assert reason == ""
        assert report["fee"] == FEE + (0.0 if openai_pen else SELF)    # reserved in pass 1
        me.influence -= report["fee"]                                   # pass 2
    out = resolve({"openai": report}, labs, world, CFG, fee_charged=fee_charged)
    (o,) = out["outcomes"]
    assert o["result"] == result and o["entry_id"] == "L03-1"
    assert o["partner_self_report"] is openai_forfeits
    assert me.influence == pytest.approx(inf + net)
    assert o["influence_change"]["openai"] == pytest.approx(net)
    assert me.know_how == pytest.approx(know - (2.0 if openai_forfeits else 0.0))
    if openai_forfeits:
        assert e.penalty_kind["openai"] == "self_report" and e.gains["openai"] == 0.0
        assert "OpenAI took part in that access and forfeited" in out["notices"][0]["text"]
    if xai_hit:
        assert e.penalty_kind["xai"] == "upheld"
        assert partner.influence == pytest.approx(p_inf - UPHELD)
        assert partner.know_how == pytest.approx(p_know - 3.0)
    else:
        assert partner.influence == pytest.approx(p_inf) and partner.know_how == pytest.approx(p_know)
    want = {k_ for k_, p in (("openai", openai_pen or openai_forfeits),
                             ("xai", xai_pen or xai_hit)) if p}
    assert set(e.penalised) == want


def test_partner_report_not_upheld_is_no_self_report():
    """A report naming a partner that matches nothing (wrong month) is not upheld and
    does not report the reporter: only the fee and the 5 Inf penalty."""
    labs, world, k = setup([entry()])
    inf = k["openai"].influence
    out = resolve({"openai": {"accused": "xAI", "month": "May 2026"}}, labs, world, CFG)
    (o,) = out["outcomes"]
    assert o["result"] == "not_upheld" and o["partner_self_report"] is False
    assert k["openai"].influence == inf - FEE - WCFG["not_upheld_influence_penalty"]
    assert world.ledger[0].penalised == []


def test_report_by_non_partner_is_not_a_self_report():
    labs, world, k = setup([entry()])
    data, _ = validate_report({"accused": "xAI"}, k["meta"], labs, world, CFG)
    assert data["fee"] == FEE and data["partner_fee"] == 0.0
    out = resolve({"meta": {"accused": "xAI"}}, labs, world, CFG)
    assert out["outcomes"][0]["partner_self_report"] is False
    assert world.ledger[0].penalised == ["xai"]


def test_partner_fee_settled_when_own_part_exposed_after_reservation():
    """fee_charged: openai reserved 3 + 5 in pass 1, but its part was penalised before
    the report resolved (e.g. exposed this month): the 5 is refunded."""
    labs, world, k = setup([entry()])
    me = k["openai"]
    inf = me.influence
    data, _ = validate_report({"accused": "xAI"}, me, labs, world, CFG)
    assert data["fee"] == FEE + SELF
    me.influence -= data["fee"]
    world.ledger[0].penalise("openai", "exposed")
    out = resolve({"openai": data}, labs, world, CFG, fee_charged=True)
    (o,) = out["outcomes"]
    assert o["result"] == "upheld" and o["partner_self_report"] is False
    assert me.influence == pytest.approx(inf)
    assert o["influence_change"]["openai"] == pytest.approx(0.0)


def test_partner_fee_charged_when_not_reserved():
    """fee_charged: the ledger gained a matching earlier entry after pass 1 (defensive
    path), so only 3 was reserved; the 5 self-report fee is charged at resolution."""
    labs, world, k = setup([])
    me = k["openai"]
    inf = me.influence
    data, _ = validate_report({"accused": "xAI"}, me, labs, world, CFG)
    assert data["fee"] == FEE
    me.influence -= data["fee"]
    world.ledger.append(entry(id="L04-1", turn=4))
    out = resolve({"openai": data}, labs, world, CFG, fee_charged=True)
    (o,) = out["outcomes"]
    assert o["result"] == "upheld" and o["partner_self_report"] is True
    assert me.influence == pytest.approx(inf - SELF)
    assert world.ledger[0].penalty_kind == {"xai": "upheld", "openai": "self_report"}


def test_partner_reports_each_other_same_turn():
    """Both partners report each other in one month (§5.2 simultaneous, §5.3 joint):
    each report is also a self-report, and self-reports settle first, so each part is
    self-reported (5, forfeit) and each accusation finds the partner's part already
    penalised this month: returned, 3 refunded. Same result in either lab order."""
    for order in (("openai", "xai"), ("xai", "openai")):
        labs, world, k = setup([entry()])
        labs.sort(key=lambda lab: order.index(lab.key) if lab.key in order else -1)
        before = {key: k[key].influence for key in ("openai", "xai")}
        know = {key: k[key].know_how for key in ("openai", "xai")}
        out = resolve({"openai": {"accused": "xAI"}, "xai": {"accused": "OpenAI"}},
                      labs, world, CFG)
        assert [o["result"] for o in out["outcomes"]] == ["returned", "returned"]
        assert all(o["partner_self_report"] for o in out["outcomes"])
        e = world.ledger[0]
        assert e.penalty_kind == {"xai": "self_report", "openai": "self_report"}
        for key, gain in (("openai", 2.0), ("xai", 3.0)):
            assert k[key].influence == pytest.approx(before[key] - SELF)
            assert k[key].know_how == pytest.approx(know[key] - gain)


def test_report_shapes_and_accused_keys():
    """M18 and M11/M12 through validate_report."""
    labs, world, k = setup([entry()])
    me = k["meta"]
    for report in ({"accused": ["xAI"], "month": "March"}, [{"accused": "xAI"}], {"lab": "xAI"},
                   {"target": "xAI"}, {"accused_lab": "xAI"}, {"accused": "xAI (Grok)"},
                   {"accused": ["xAI", "Grok"]}, "xAI"):
        data, reason = validate_report(report, me, labs, world, CFG)
        assert reason == "" and data["accused"] == "xai" and data["fee"] == WCFG["fee_influence"], report
    for report in ({"accused": "our own lab"}, {"accused": "Meta (us)"}, {"accused": "my lab"},
                   {"accused": "Meta's"}):
        data, reason = validate_report(report, me, labs, world, CFG)
        assert data["self_report"] and data["fee"] == WCFG["self_report_fee_influence"], report
    for report in (None, {}, [], {"accused": ""}, {"accused": "N/A"}, {"accused": "none"},
                   {"accused": "no report"}, {"accused": []}):
        assert validate_report(report, me, labs, world, CFG) == (None, ""), report
    for report in ({"accused": "Meta and xAI"}, {"accused": "Meta + xAI"}, {"accused": ["xAI", "OpenAI"]},
                   [{"accused": "xAI"}, {"accused": "OpenAI"}], "xAI accessed OpenAI"):
        assert validate_report(report, me, labs, world, CFG) == (None, "a report must name one lab"), report
    data, reason = validate_report({"accused": "DeepSeek"}, me, labs, world, CFG)
    assert data is None and "DeepSeek" in reason
    assert report_fee({"accused": "self"}, me, labs, CFG) == WCFG["self_report_fee_influence"]
    assert report_fee({"accused": "OpenAI"}, me, labs, CFG) == WCFG["fee_influence"]
    assert report_fee({"accused": "nobody"}, me, labs, CFG) == 0.0
    # Insufficient Influence at the start state
    me.influence = WCFG["fee_influence"] - 0.5
    data, reason = validate_report({"accused": "OpenAI"}, me, labs, world, CFG)
    assert data is None and reason.startswith("needs ")
    # resolve reads the same shapes and records the reason of a rejection
    labs, world, k = setup([entry()])
    out = resolve({"meta": {"accused": "Meta and xAI"}, "gdm": {"lab": ["xAI"]}}, labs, world, CFG)
    assert [(o["reporter"], o["result"]) for o in out["outcomes"]] == [("gdm", "upheld"), ("meta", "rejected")]
    assert out["outcomes"][1]["reason"] == "a report must name one lab"
    labs, world, k = setup([entry()])
    out = resolve({"xai": {"accused": "our own lab"}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "self_report"


def test_fee_charged_mode():
    """M16: the engine validates in pass 1, charges in pass 2, resolves with fee_charged=True."""
    labs, world, k = setup([entry()])
    rep = k["anthropic"]
    data, reason = validate_report({"accused": "OpenAI"}, rep, labs, world, CFG)
    rep.influence -= report_fee(data, rep, labs, CFG)          # pass 2
    charged = rep.influence
    out = resolve({"anthropic": data}, labs, world, CFG, fee_charged=True)
    (o,) = out["outcomes"]
    assert o["result"] == "upheld" and rep.influence == charged + WCFG["fee_influence"]
    assert o["influence_change"]["anthropic"] == 0.0           # fee charged, then refunded
    # Not upheld: only the penalty is taken here; no second fee
    labs, world, k = setup([entry()])
    rep = k["anthropic"]
    rep.influence -= WCFG["fee_influence"]
    charged = rep.influence
    out = resolve({"anthropic": {"accused": "Meta"}}, labs, world, CFG, fee_charged=True)
    assert out["outcomes"][0]["result"] == "not_upheld"
    assert rep.influence == charged - WCFG["not_upheld_influence_penalty"]
    assert out["outcomes"][0]["influence_change"]["anthropic"] == \
        -(WCFG["fee_influence"] + WCFG["not_upheld_influence_penalty"])
    # The fee was reserved: Influence spent since then does not reject the report
    labs, world, k = setup([entry()])
    k["gdm"].influence = 0.0
    out = resolve({"gdm": {"accused": "xAI"}}, labs, world, CFG, fee_charged=True)
    assert out["outcomes"][0]["result"] == "upheld" and k["gdm"].influence == WCFG["fee_influence"]


# ---------------------------------------------------------------------------
# Round 3: no-report detection (M20), legal suffixes (M21), key precedence (M22)
# ---------------------------------------------------------------------------

NO_REPORTS = ("none", "None.", "None this month", "No report", "No report this month", "N/A",
              "N/A - no evidence", "no one", "No one.", "nobody", "-", "—", "–", "not filing",
              "Not applicable", "(none)", "None (no evidence)", "No lab", "no report filed",
              "none at this time", "No", "null", "Nothing", "Nothing to report", "n.a.", "NA",
              "nil", "", "   ", "- none -")


def test_is_no_report_strings_and_shapes():
    for text in NO_REPORTS:
        assert is_no_report(text), text
        assert is_no_report({"accused": text, "month": "", "evidence": ""}), text
    for value in (None, False, {}, [], ["none"], {"accused": []}, {"month": "February"}):
        assert is_no_report(value), value
    # names a lab, holds a self word, or does not start with a negation marker: a report
    for text in ("Meta", "None other than xAI", "No doubt OpenAI", "Not us", "no one but ourselves",
                 "Nobody except our lab", "DeepSeek", "Notion Labs", "Nonesuch", "Anthropic, PBC",
                 "self-report"):
        assert not is_no_report(text), text
    # the dict is judged by its accused under the M22 precedence; evidence alone is a report
    assert not is_no_report({"accused_lab": "Meta", "target": "none"})
    assert is_no_report({"accused": "None.", "target": "Meta"})
    assert not is_no_report({"evidence": "Capability jumped 6 points in March."})
    assert is_no_report({"evidence": "N/A"})


def test_no_report_strings_file_nothing():
    labs, world, k = setup([entry()])
    me = k["meta"]
    for text in NO_REPORTS:
        assert validate_report({"accused": text, "evidence": ""}, me, labs, world, CFG) == (None, ""), text
        assert validate_report(text, me, labs, world, CFG) == (None, ""), text
        assert report_fee({"accused": text}, me, labs, CFG) == 0.0
    out = resolve({"meta": {"accused": "None."}, "gdm": "No one."}, labs, world, CFG)
    assert out["outcomes"] == []


def test_evidence_only_report_names_no_lab():
    labs, world, k = setup([entry()])
    report = {"month": "March 2026", "evidence": "xAI's capability jumped after the access."}
    assert validate_report(report, k["meta"], labs, world, CFG) == (None, "the report names no lab")
    assert validate_report({"accused": "", "evidence": "a jump"}, k["meta"], labs, world, CFG) \
        == (None, "the report names no lab")


def test_legal_suffixes_and_round3_self_words_in_reports():
    labs, world, k = setup([entry()])
    me = k["meta"]
    for text, key in (("Anthropic, PBC", "anthropic"), ("OpenAI, L.P.", "openai"),
                      ("OpenAI Global, LLC", "openai"), ("xAI Corp.", "xai"), ("X.AI Corp.", "xai"),
                      ("Google DeepMind, a subsidiary of Alphabet", "gdm"), ("OpenAI plc", "openai"),
                      ("our company", "meta"), ("Self-report", "meta"), ("self report", "meta"),
                      ("my company", "meta"), ("our firm", "meta"), ("Meta (self-report)", "meta"),
                      ("us, Meta", "meta")):
        data, reason = validate_report({"accused": text, "month": "March 2026"}, me, labs, world, CFG)
        assert reason == "" and data["accused"] == key, text
        assert data["self_report"] == (key == "meta"), text
    for text in ("Meta and xAI", "OpenAI/Google", "xAI, which accessed OpenAI"):
        assert validate_report({"accused": text}, me, labs, world, CFG) == (None, "a report must name one lab")
    data, reason = validate_report({"accused": "DeepSeek, Inc."}, me, labs, world, CFG)
    assert data is None and "DeepSeek" in reason


def test_accused_key_precedence():
    labs, world, k = setup([entry()])
    me = k["openai"]
    cases = [({"accused_lab": "Meta", "target": "Google DeepMind"}, "meta"),
             ({"accused_lab": "xAI", "lab": "OpenAI"}, "xai"),
             ({"accused": "Meta", "accused_lab": "xAI"}, "meta"),
             ({"accused": "Meta", "lab": "xAI", "target": "Anthropic"}, "meta"),
             ({"lab": "Meta", "target": "xAI"}, "meta"),
             ({"target": "xAI"}, "xai"),
             ({"accused": "", "accused_lab": "xAI", "target": "Meta"}, "xai")]
    for report, key in cases:
        data, reason = validate_report(report, me, labs, world, CFG)
        assert reason == "" and data["accused"] == key, report
    # a no-report accused is not overridden by "lab"/"target"
    assert validate_report({"accused": "None.", "target": "Meta"}, me, labs, world, CFG) == (None, "")



# ---------------------------------------------------------------------------
# v5 fixes: month parsing from an August start, earlier turns only, order
# independence (spec §5.2, §5.3, A.8)
# ---------------------------------------------------------------------------

AUG = date(2026, 8, 1)     # §2: monthly turns from 1 August 2026


def test_month_parsing_from_an_august_start():
    """§5.3 'in the stated month if given': a month without a year is its most recent
    occurrence at or before the current turn; a bare number 1–12 is a calendar month."""
    from core.whistleblow import _parse_turn
    # turn 6 = January 2027, turn 8 = March 2027
    assert _parse_turn("January", AUG, current_turn=8) == 6
    assert _parse_turn("Oct", AUG, current_turn=8) == 3
    assert _parse_turn("August", AUG, current_turn=8) == 1
    assert _parse_turn("January 2027", AUG, current_turn=8) == 6
    assert _parse_turn("2027-02", AUG, current_turn=8) == 7
    # bare numbers are calendar months, not turns
    assert _parse_turn("10", AUG, current_turn=8) == 3
    assert _parse_turn(1, AUG, current_turn=8) == 6
    assert _parse_turn(5.0, AUG, current_turn=8) == -2                 # May 2026
    assert _parse_turn(13, AUG, current_turn=8) is None and _parse_turn("0", AUG, 8) is None
    # "turn N" / "month N" stay turns
    assert _parse_turn("turn 5", AUG, current_turn=8) == 5
    # readable months outside the run still restrict (no fallback to "any month")
    assert _parse_turn("July 2026", AUG, current_turn=8) == 0
    assert _parse_turn("December", AUG, current_turn=3) == -7      # December 2025
    assert _parse_turn("April 2027", AUG, current_turn=8) == 9
    assert _parse_turn("last month", AUG, current_turn=1) == 0
    # with no current turn: the first occurrence from the start month
    assert _parse_turn("March", AUG) == 8 and _parse_turn("September", AUG) == 2


def test_bare_number_is_a_calendar_month_in_a_report():
    """A.8 asks for the month of the access: "month": 10 means October (turn 3), not
    turn 10; a value under "turn" is a turn."""
    e = entry(id="L03-1", turn=3)
    labs, world, k = setup([e])
    world.start, world.turn = AUG, 8
    out = resolve({"anthropic": {"accused": "OpenAI", "month": 10}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "upheld"
    labs, world, k = setup([entry(id="L03-1", turn=3)])
    world.start, world.turn = AUG, 8
    out = resolve({"anthropic": {"accused": "OpenAI", "turn": 3}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] == "upheld"


@pytest.mark.parametrize("month", ["July 2026", "July", "April 2027", "April"])
def test_readable_month_outside_the_run_matches_nothing(month):
    """§5.3: a readable month before the run or after the current turn restricts the
    match: nothing matches, so the report is not upheld (fee kept, further 5)."""
    labs, world, k = setup([entry(id="L03-1", turn=3)])
    world.start, world.turn = AUG, 6       # January 2027: bare "July"/"April" are 2026
    rep = k["anthropic"].influence
    out = resolve({"anthropic": {"accused": "OpenAI", "month": month}}, labs, world, CFG)
    # bare "July"/"April" at January 2027 are July/April 2026, before the run
    assert out["outcomes"][0]["result"] == "not_upheld"
    assert k["anthropic"].influence == pytest.approx(rep - FEE - WCFG["not_upheld_influence_penalty"])
    assert world.ledger[0].penalised == []


@pytest.mark.parametrize("who,accused", [("anthropic", "OpenAI"), ("xai", "self"),
                                         ("xai", "OpenAI")])
def test_report_matches_only_earlier_turns(who, accused):
    """§5.3 'a whistleblow can prove an intrusion at any later turn': a report (rival,
    self or partner) cannot match an intrusion of the current month."""
    labs, world, k = setup([entry(id="L05-1", turn=5)])      # world.turn is 5
    out = resolve({who: {"accused": accused}}, labs, world, CFG)
    (o,) = out["outcomes"]
    assert o["result"] == "not_upheld" and o["entry_id"] is None
    assert not o["partner_self_report"]
    assert world.ledger[0].penalised == []
    me = k[who]
    assert validate_report({"accused": accused}, me, labs, world, CFG)[0]["partner_fee"] == 0.0
    world.turn = 6
    out = resolve({who: {"accused": accused}}, labs, world, CFG)
    assert out["outcomes"][0]["result"] in ("upheld", "self_report")


def _settle(entries, reports, order):
    import copy as _copy
    labs, world, k = setup(_copy.deepcopy(entries))
    labs.sort(key=lambda lab: order.index(lab.key))
    out = resolve(reports, labs, world, CFG)
    state = sorted((lab.key, round(lab.influence, 6), round(lab.know_how, 6)) for lab in labs)
    kinds = [sorted(e.penalty_kind.items()) for e in world.ledger]
    results = dict(sorted((o["reporter"], o["result"]) for o in out["outcomes"]))
    return state, kinds, results


def test_same_turn_reports_are_order_independent():
    """§5.2 proposals are simultaneous; §5.3 each part is penalised once. Every lab
    order gives the same Influence, know-how, penalties and results; self-reports and
    partner reports settle first and a rival report on a part self-reported this
    month is returned (refunded)."""
    import itertools
    keys = ["anthropic", "openai", "gdm", "meta", "xai"]
    joint = entry(id="L03-1", turn=3, committers=["openai", "xai"],
                  gains={"openai": 2.0, "xai": 3.0})
    solo = entry(id="L04-1", turn=4, committers=["meta"], targets=["gdm"], gains={"meta": 1.0})
    cases = [
        {"meta": {"accused": "self"}, "gdm": {"accused": "Meta"}},
        {"openai": {"accused": "xAI"}, "xai": {"accused": "OpenAI"}},
        {"xai": {"accused": "self"}, "openai": {"accused": "xAI"}, "anthropic": {"accused": "xAI"}},
        {"anthropic": {"accused": "OpenAI"}, "gdm": {"accused": "OpenAI"}, "xai": {"accused": "self"}},
        {"anthropic": {"accused": "Meta"}, "gdm": {"accused": "Meta"}, "meta": {"accused": "OpenAI"}},
    ]
    for reports in cases:
        seen = {_settle([joint, solo], reports, list(p)).__repr__()
                for p in itertools.permutations(keys)}
        assert len(seen) == 1, reports
    # the rival's report on a part self-reported this month is returned and refunded
    state, kinds, results = _settle([joint, solo], cases[0], keys[::-1])
    labs, _, k = setup([])
    assert results == {"meta": "self_report", "gdm": "returned"}
    state = {key: inf for key, inf, _ in state}
    assert state["gdm"] == pytest.approx(k["gdm"].influence)
    assert state["meta"] == pytest.approx(k["meta"].influence - SELF)
    assert kinds[1] == [("meta", "self_report")]
