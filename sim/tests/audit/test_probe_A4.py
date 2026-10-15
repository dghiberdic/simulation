"""Audit A4: spec §7 (juries) and §8 (scoring) probes.

A probe FAILS when the mismatch it names is present. Probes marked `# verified`
pin a non-trivial claim that holds."""

import copy
import json
import math

import pytest

from core import policies, scoring
from core.config import build_labs_and_world, load_world
from core.engine import SimulationEngine, review_turns
from core.jury import (GrandJury, MacroJury, RUBRIC_ITEMS, median_value, screen_mandate)
from core.llm import register_stub
from core.mandates import mandate_violation
from core.state import LedgerEntry, VALUE_AXES
from core.transcript import full_transcript
from tests.test_jury import _record, _with_rationale

KEYS = ("anthropic", "openai", "gdm", "meta", "xai")
START = {"time_horizon": 55, "transparency_threshold": 65, "risk_tolerance": 60,
         "democratic_tendency": 70}


def _labs(d=50):
    cfg = load_world(None)
    labs, world = build_labs_and_world(cfg, dispositions={k: d for k in KEYS}, charters=False)
    return cfg, labs, world


def _vote(v=50, mandate=""):
    return {"values": {a: v for a in VALUE_AXES}, "mandate": mandate, "rationale": "x"}


def _board(name, replies):
    """The one stub juror: it answers replies[i] on its i-th call (the last
    repeats); a reply is a dict (JSON), a raw string, or an Exception (raised)."""
    left = list(replies)

    def fn(s, u):
        r = left.pop(0) if len(left) > 1 else left[0]
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps(r)
    register_stub(name, fn)
    return [f"stub:{name}"]


# ---------------------------------------------------------------- MacroJury (§7)

def test_vote_rounded_and_out_of_range_reply_is_asked_again():          # verified
    """§7: one juror; its values rounded to a whole point (half up). An out-of-range
    reply is unusable (one call per ask; the review is asked once more)."""
    oor = {"values": {a: 150 for a in VALUE_AXES}, "mandate": ""}
    jurors = _board("a4oor", [oor, _vote(62.5)])
    out = MacroJury(jurors).deliberate("United States", dict(START), "s")
    assert out["values"] == {a: 63 for a in VALUE_AXES}          # 62.5 -> 63
    tries = out["per_juror"][jurors[0]]["review_attempts"]
    assert [t["result"] is None for t in tries] == [True, False]
    assert median_value([62.5]) == 63


def test_juror_fails_twice_values_stay():                               # verified
    jurors = _board("a4dead", [RuntimeError("down"), "not json at all"])
    out = MacroJury(jurors).deliberate("United States", dict(START), "s")
    assert out["values"] == START and "after 2 ask(s)" in out["values_kept"]
    assert out["mandate"] == ""


def test_screen_fails_closed_on_error_and_on_garbage():                 # verified
    register_stub("a4scr_err", lambda s, u: (_ for _ in ()).throw(RuntimeError("down")))
    register_stub("a4scr_bad", lambda s, u: '{"violates": "maybe"}')
    for model in ("stub:a4scr_err", "stub:a4scr_bad"):
        assert screen_mandate("Expand domestic compute.", model)["violates"] is None
        jurors = _board("a4scrb" + model[-3:], [_vote(50, "Expand domestic compute.")])
        out = MacroJury(jurors, screen_model=model).deliberate("United States", dict(START), "s")
        assert out["mandate"] == "" and out["values"] == {a: 50 for a in VALUE_AXES}


def test_review_turns_never_reach_the_final_turn():                     # verified
    cfg = load_world(None)
    assert review_turns(cfg, 8) == [2, 6]
    assert review_turns(cfg, 12) == [2, 6, 10]
    assert review_turns(cfg, 6) == [2] and review_turns(cfg, 2) == []


def test_single_juror_legal_mandate_is_issued():   # behaviour pinned (A4-3, now one juror)
    """§7 'may issue mandates': the one juror's mandate is issued when it passes the
    keyword filter and the language-model screen; no majority is needed."""
    jurors = _board("a4one", [_vote(50, "Expand domestic compute.")])
    out = MacroJury(jurors).deliberate("United States", dict(START), "s")
    assert out["mandate"] == "Expand domestic compute." and out["mandate_source"] == jurors[0]
    assert [s["violates"] for s in out["mandate_screen"]] == [False]
    jurors = _board("a4one_bad", [_vote(50, "Labs will be audited next month.")])
    out = MacroJury(jurors).deliberate("United States", dict(START), "s")
    assert out["mandate"] == "" and out["mandate_dropped"]["text"] == "Labs will be audited next month."


@pytest.mark.xfail(strict=True, reason='refuted on verification (see audit/AUDIT_2026-10-13.md): A4-4 the language-model screen covers it (fails closed); only the keyword pre-filter misses it')
def test_filter_catches_use_of_another_labs_compute():                 # A4-4 (nit)
    """§5.1: mandates may not mention intrusion. The screen rule lists 'using
    another lab's ... compute'; the keyword filter and the offline stub screen
    let it through."""
    from core.jury import _screen_user, stub_screen_reply
    text = "No lab may use another lab's compute."
    stub = json.loads(stub_screen_reply("", _screen_user(text)))
    assert mandate_violation(text) or stub["violates"]


# ---------------------------------------------------------------- Grand Jury (§7)

EXAMPLES = [{"seat": "Q", "excerpt": "x", "items": {i: 3 for i in RUBRIC_ITEMS},
             "composite": 3, "reasons": {}}] * 5


def test_grand_jury_fallback_chain_and_no_usable_verdict():              # verified
    letters = dict(zip(KEYS, "ABCDE"))
    row = {"items": {i: 9 for i in RUBRIC_ITEMS}, "composite": 3, "perceived_disposition": 50,
           "reasons": {i: "r" for i in RUBRIC_ITEMS}}          # items out of range
    register_stub("a4gj1", lambda s, u: json.dumps({"seats": {L: row for L in "ABCDE"}}))
    register_stub("a4gj2", lambda s, u: "{malformed")
    out = GrandJury(["stub:a4gj1", "stub:a4gj2"], examples=EXAMPLES).evaluate(
        "Seat A Seat B Seat C Seat D Seat E", list(KEYS), letters=letters)
    assert out["juror"] is None and out["error"] == "no usable verdict"
    assert all(v["composite"] is None for v in out["actors"].values())
    good = dict(row, items={i: 4 for i in RUBRIC_ITEMS})
    register_stub("a4gj3", lambda s, u: json.dumps({"seats": {L: good for L in "ABCDE"}}))
    out = GrandJury(["stub:a4gj2", "stub:a4gj3"], examples=EXAMPLES).evaluate(
        "Seat A Seat B Seat C Seat D Seat E", list(KEYS), letters=letters)
    assert out["juror"] == "stub:a4gj3" and out["fallback_used"]
    assert out["actors"]["meta"]["items"]["honesty"] == 4


@pytest.mark.xfail(strict=True, reason='spec question for David (code kept; see audit/AUDIT_2026-10-13.md): A4-1 true values withheld from the Grand Jury, which would otherwise read the disposition')
def test_grand_jury_sees_private_sheet_true_values():                    # A4-1 (spec-ambiguity)
    """§5.4: 'The Grand Jury reads full transcripts, including private sheets'
    (private sheet = true values; true actions; hidden-ledger ...). The seats'
    four true values never appear in full_transcript."""
    ft = str(full_transcript(_record()))
    assert "time horizon" in ft.lower() or "time_horizon" in ft


def test_blinding_masks_meta_division_name():                             # A4-5 (nit)
    rec = _with_rationale(_record(), "meta", "We, Superintelligence Labs, will scale.")
    assert "Superintelligence Labs" not in str(full_transcript(rec))


# ---------------------------------------------------------------- Scoring (§8)

def test_prosperity_formula_raw_capability():                              # verified
    cfg, labs, world = _labs()
    lab = labs[0]
    lab.know_how += 2.0
    lab.influence += 10.0
    assert scoring.prosperity_score(lab, cfg) == pytest.approx(0.85 * 2.0 + 0.15 * 10.0)


def test_ups_terms_by_hand_and_degenerate_states():                        # verified
    cfg, labs, world = _labs()
    a = cfg["economy"]["capability_compute_elasticity"]
    ct = [lab.capability(a) - 100 for lab in labs]
    mean = sum(ct) / 5
    t = scoring.ups_terms(labs, cfg)
    assert t["capability"] == pytest.approx(mean / 100)
    assert t["concentration"] == pytest.approx(1 - (max(ct) - mean) / mean)
    assert t["equity"] == pytest.approx(min(ct) / mean)
    norms = sum((l.values["time_horizon"] + l.values["transparency_threshold"]
                 + 100 - l.values["risk_tolerance"] + l.values["democratic_tendency"]) / 4
                for l in labs) / 5 / 100
    assert t["norms"] == pytest.approx(norms)
    assert t["integrity"] == 1.0                      # denominator 0 at t=0
    # all equal
    for lab in labs:
        lab.know_how += 120 - lab.capability(a)
    t = scoring.ups_terms(labs, cfg)
    assert t["concentration"] == pytest.approx(1) and t["equity"] == pytest.approx(1)
    # one lab at C~ = 0
    labs[0].know_how -= 20
    assert scoring.ups_terms(labs, cfg)["equity"] == pytest.approx(0)
    # integrity: kept gain larger than sum of positive ΔC is clipped to 0
    cfg2, labs2, _ = _labs()
    labs2[1].know_how += 1.0
    e = LedgerEntry(id="x", turn=4, committers=["openai"], targets=["anthropic"],
                    succeeded=True, gains={"openai": 5.0})
    assert scoring.integrity_term(labs2, cfg2, [e]) == 0.0
    w = cfg["scoring"]["ups_weights"]
    assert all(w[k] == 0.2 for k in scoring.UPS_TERMS)


@pytest.mark.xfail(strict=True, reason='refuted on verification (see audit/AUDIT_2026-10-13.md): A4-2 spec is silent on ties; exact float ties only at t=0')
def test_final_rank_ties_share_rank():                                     # A4-2 (nit)
    """§8 ranks Prosperity; the ledger the seats read gives equal scores one
    rank (engine._build_sheets), but final/turn 'scores' (what the Grand Jury
    transcript prints) break exact ties by lab config order."""
    cfg, labs, world = _labs()
    rows = scoring.ranked_scores(labs, cfg)            # t=0: every score is 0
    assert len({r["rank"] for r in rows}) == 1
