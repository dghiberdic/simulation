"""The language-model screen of the mandate the MacroJury would issue (§7: mandates
may not mention intrusion, reporting, audits or a named lab). Stubs only."""

import json

import pytest

from core import costs, policies
from core.config import build_labs_and_world, load_state, load_world
from core.costs import BudgetExceeded
from core.engine import SimulationEngine
from core.jury import (MANDATE_SCREEN_MODEL, STUB_SCREEN_MODEL, MacroJury, screen_mandate,
                       stub_screen_reply)
from core.llm import register_stub
from core.mandates import mandate_violation

KEYS = ("anthropic", "openai", "gdm", "meta", "xai")
AXES = ("time_horizon", "transparency_threshold", "risk_tolerance", "democratic_tendency")
START = {a: 50 for a in AXES}


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(costs, "DEFAULT_SPEND_FILE", tmp_path / "spend.json")
    costs.configure()


def _board(name, mandates, values=(50, 50, 50)):
    models = []
    for i, (m, v) in enumerate(zip(mandates, values)):
        register_stub(f"{name}{i}", lambda s, u, m=m, v=v: json.dumps(
            {"values": {a: v for a in AXES}, "mandate": m, "rationale": "x"}))
        models.append(f"stub:{name}{i}")
    return models


def _screen(name, fn):
    calls = []

    def wrapped(system, user):
        calls.append(user)
        return fn(system, user)
    register_stub(name, wrapped)
    return f"stub:{name}", calls


def test_screen_model_is_configured_and_priced():
    """config/states/usa.json names the screen model; it has a price on file."""
    assert load_state()["mandate_screen_model"] == MANDATE_SCREEN_MODEL == "deepseek-v4-pro"
    assert costs.price_for(MANDATE_SCREEN_MODEL)["input"] == 1.32
    assert MacroJury(["kimi-k3", "deepseek-v4-pro", "qwen-3.8-max"]).screen_model == MANDATE_SCREEN_MODEL
    assert MacroJury(["stub:a", "stub:b", "stub:c"]).screen_model == STUB_SCREEN_MODEL
    assert MacroJury(["stub:a", "stub:b", "stub:c"], screen_model="x").screen_model == "x"


def test_a_clear_mandate_is_screened_once_and_issued():
    model, calls = _screen("scr-clear", lambda s, u: '{"violates": false, "topics": []}')
    jurors = _board("scrok", ["Expand domestic compute."] * 3)
    out = MacroJury(jurors, screen_model=model).deliberate("United States", dict(START), "s")
    assert out["mandate"] == "Expand domestic compute."
    assert len(calls) == 1 and "Expand domestic compute." in calls[0]
    (s,) = out["mandate_screen"]
    assert s["violates"] is False and s["model"] == model and s["temperature"] == 0.0


def test_the_screen_call_is_on_the_ledger_as_a_macro_jury_call():
    model, _ = _screen("scr-ledger", lambda s, u: '{"violates": false, "topics": []}')
    MacroJury(_board("scrled", ["Expand domestic compute."] * 3), screen_model=model).deliberate(
        "United States", dict(START), "s", run_id="r1")
    rows = [c for c in costs.get_tracker().calls if c["model"] == model]
    assert len(rows) == 1 and rows[0]["purpose"] == "macro_jury" and rows[0]["run_id"] == "r1"


def test_a_refused_mandate_is_dropped_and_the_next_legal_one_screened():
    """The text closest to the median is refused; two legal proposals remain, so the
    next one is screened and issued."""
    model, calls = _screen("scr-veto", lambda s, u: json.dumps(
        {"violates": "Mission" in u, "topics": ["named_lab"] if "Mission" in u else []}))
    jurors = _board("scrveto", ["The Mission district lab must slow down.", "Expand domestic compute.",
                                "Invest in domestic chips."], values=(50, 10, 52))
    out = MacroJury(jurors, screen_model=model).deliberate("United States", dict(START), "s")
    assert out["mandate"] == "Invest in domestic chips."
    assert [s["violates"] for s in out["mandate_screen"]] == [True, False]
    assert out["proposals"][0]["dropped"] == "screen: named_lab"


def test_without_a_majority_after_the_screen_no_mandate_is_issued():
    model, _ = _screen("scr-maj", lambda s, u: '{"violates": true, "topics": ["audit"]}')
    jurors = _board("scrmaj", ["Expand domestic compute.", "Expand domestic compute.", ""])
    out = MacroJury(jurors, screen_model=model).deliberate("United States", dict(START), "s")
    assert out["mandate"] == "" and len(out["mandate_screen"]) == 1     # one text, screened once
    assert out["mandate_dropped"]["reason"] == "screen: audit"


@pytest.mark.parametrize("reply", ["I cannot help with that.", '{"violates": "maybe"}', ""])
def test_an_unusable_screen_drops_the_mandate(reply):
    """Fail closed: a screen that never gives a usable answer drops the text (logged)."""
    model, calls = _screen("scr-bad" + str(len(reply)), lambda s, u: reply)
    jurors = _board("scrbad" + str(len(reply)), ["Expand domestic compute."] * 3)
    out = MacroJury(jurors, screen_model=model).deliberate("United States", dict(START), "s")
    assert out["mandate"] == ""
    (s,) = out["mandate_screen"]
    assert s["violates"] is None and s["error"]
    assert out["mandate_dropped"]["reason"].startswith("screen unusable")
    assert len(calls) == 3                     # the corrective retries of complete_json


def test_a_failing_screen_call_drops_the_mandate():
    def down(s, u):
        raise RuntimeError("provider down")
    model, _ = _screen("scr-down", down)
    jurors = _board("scrdown", ["Expand domestic compute."] * 3)
    out = MacroJury(jurors, screen_model=model).deliberate("United States", dict(START), "s")
    assert out["mandate"] == "" and "provider down" in out["mandate_screen"][0]["error"]


def test_budget_stop_in_the_screen_propagates_with_the_votes():
    def broke(s, u):
        raise BudgetExceeded("over")
    model, _ = _screen("scr-budget", broke)
    jurors = _board("scrbud", ["Expand domestic compute."] * 3)
    with pytest.raises(BudgetExceeded) as e:
        MacroJury(jurors, screen_model=model).deliberate("United States", dict(START), "s")
    assert set(e.value.per_juror) == set(jurors)


def test_screen_off_skips_the_call():
    model, calls = _screen("scr-off", lambda s, u: '{"violates": true, "topics": []}')
    jurors = _board("scroff", ["Expand domestic compute."] * 3)
    out = MacroJury(jurors, screen_model=model).deliberate("United States", dict(START), "s",
                                                           screen=False)
    assert out["mandate"] == "Expand domestic compute." and not calls and out["mandate_screen"] == []


def test_no_mandate_no_screen_call():
    model, calls = _screen("scr-none", lambda s, u: '{"violates": false}')
    out = MacroJury(_board("scrnone", ["", "", ""]), screen_model=model).deliberate(
        "United States", dict(START), "s")
    assert out["mandate"] == "" and not calls


def test_screen_prompt_states_the_rule():
    model, calls = _screen("scr-prompt", lambda s, u: '{"violates": false, "topics": []}')
    screen_mandate("Expand domestic compute.", model)
    for word in ("intrusion", "reporting", "audit", "named_lab", "any means"):
        assert word in calls[0]


@pytest.mark.parametrize("text,violates", [
    ("The Mission Bay lab must cut capital spending.", True),
    ("The London-based lab must slow down.", True),
    ("Labs must tell regulators whenever a rival cheats.", True),
    ("Labs must not enter another lab's partition.", True),
    ("Officials will go through each lab's books.", True),
    ("Expand domestic compute.", False),
    ("Labs should publish quarterly progress on energy efficiency for the public.", False),
])
def test_offline_stub_screen(text, violates):
    """stub:mandate-screen: the keyword filter plus a few rules beyond it."""
    out = json.loads(stub_screen_reply("", f"Mandate: <<<{text}>>>"))
    assert out["violates"] is violates


def test_stub_screen_catches_what_the_keyword_filter_lets_through():
    text = "The Embarcadero lab must slow down."
    assert not mandate_violation(text)
    out = MacroJury(_board("scrstub", [text] * 3)).deliberate("United States", dict(START), "s")
    assert out["mandate"] == "" and out["mandate_screen"][0]["model"] == STUB_SCREEN_MODEL


def test_engine_keeps_the_screen_in_the_turn_record():
    cfg = load_world(None)
    labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in KEYS}, charters=False)
    eng = SimulationEngine(labs, world, cfg, scenario="S1", turns=3, seed=0,
                           seat_policies={k: policies.idle for k in KEYS},
                           macro_jurors=_board("screng", ["Invest in domestic chips."] * 3),
                           run_grand_jury=False, parallel=False)
    rec = eng.run()
    mj = rec["turns"][1]["macro_jury"]
    assert mj["mandate"] == "Invest in domestic chips."
    assert mj["mandate_screen"][0]["violates"] is False
    assert eng.world.mandates == [{"turn": 2, "text": "Invest in domestic chips."}]
