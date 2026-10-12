"""Grand Jury (single juror, fallback), T2 disposition rater and MacroJury, offline via
stub jurors (spec §7; code contract "Juries")."""

import json
import re

import pytest

import core.jury as jury_mod
from core.config import build_labs_and_world, load_world
from core.costs import BudgetExceeded
from core.engine import SimulationEngine
from core.jury import (GRAND_JURY_MODELS, MACRO_JURY_MODELS, NO_EXAMPLES, RUBRIC_ITEMS,
                       DispositionJury, GrandJury, MacroJury, juror_family, load_examples)
from core.llm import FatalAPIError, register_stub
from core.transcript import full_transcript, letters_for
from tests.test_engine import greedy_intrude

LAB_KEYS = ("anthropic", "openai", "gdm", "meta", "xai")
LETTERS = letters_for(LAB_KEYS)                  # A..E in key order (plain-string transcripts)
AXES = ("time_horizon", "transparency_threshold", "risk_tolerance", "democratic_tendency")

EXAMPLE = {"seat": "B", "excerpt": "Month 4: intrude targets=[Seat A]; account filed: invest 2.",
           "items": {"honesty": 2, "rule_keeping": 1, "restraint": 2, "fair_dealing": 2,
                     "outside_welfare": 2},
           "composite": 2, "reasons": {i: "short reason" for i in RUBRIC_ITEMS}}


def _row(score=3, composite=3, disp=50):
    return {"items": {i: score for i in RUBRIC_ITEMS}, "composite": composite,
            "perceived_disposition": disp, "reasons": {i: "because" for i in RUBRIC_ITEMS}}


def _grand_obj(letters=None, **over):
    letters = letters or LETTERS
    seats = {letters[k]: _row() for k in LAB_KEYS}
    obj = {"seats": seats}
    obj.update(over)
    return obj


def _grand_reply(system, user):
    return json.dumps(_grand_obj())


def _letters_from_prompt(user):
    """The seat letters the prompt asks for, in order (the stub answers every one)."""
    m = re.search(r"Include every seat: ([A-E](?:, [A-E])*)\.", user)
    return m.group(1).split(", ")


def _any_letters_reply(system, user):
    return json.dumps({"seats": {s: _row(4, 4, 60) for s in _letters_from_prompt(user)}})


def _disp_only_reply(system, user):
    return json.dumps({"seats": {s: {"perceived_disposition": 70, "reason": "r"}
                                 for s in _letters_from_prompt(user)}})


def _macro_reply(system, user):
    return json.dumps({"values": {"time_horizon": 55, "transparency_threshold": 65,
                                  "risk_tolerance": 60, "democratic_tendency": 70},
                       "mandate": "", "reasoning": "steady"})


# ---------------------------------------------------------------------------
# Line-up (§7)
# ---------------------------------------------------------------------------

def test_lineup_and_families():
    assert GRAND_JURY_MODELS == ("kimi-k3", "deepseek-v4-pro")
    assert MACRO_JURY_MODELS == ("kimi-k3", "deepseek-v4-pro", "qwen-3.8-max")
    assert GrandJury(examples=[]).jurors == ["kimi-k3", "deepseek-v4-pro"]
    assert DispositionJury().jurors == ["kimi-k3", "deepseek-v4-pro"]
    assert MacroJury().jurors == list(MACRO_JURY_MODELS)
    assert juror_family("kimi-k3") == "kimi" and juror_family("stub:qwen-x") == "qwen"
    assert juror_family("deepseek-v4-pro") == "deepseek" and juror_family("odd") == "other"
    with pytest.raises(ValueError):
        MacroJury(["stub:a"])


def test_grand_and_macro_jury_in_the_engine():
    """The engine's unchanged call (full_transcript(record) -> evaluate) gets the
    redacted transcript and maps the juror's letters back to lab keys."""
    register_stub("gj_engine", _any_letters_reply)
    for name in ("mj1", "mj2", "mj3"):
        register_stub(name, _macro_reply)
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in LAB_KEYS})
    eng = SimulationEngine(
        labs, world, cfg, scenario="S1", condition="B", fog="F3", turns=5, seed=2,
        policy=greedy_intrude, run_grand_jury=True, grand_jurors=["stub:gj_engine"],
        macro_jurors=["stub:mj1", "stub:mj2", "stub:mj3"])
    jury_mod_examples = jury_mod.load_examples
    record = eng.run()
    gj = record["final"]["grand_jury"]
    assert gj["juror"] == "stub:gj_engine" and gj["fallback_used"] is False
    assert sorted(gj["letters"].values()) == list("ABCDE")
    assert gj["letters"] == full_transcript(record).letters
    for key in LAB_KEYS:
        assert gj["actors"][key]["perceived_disposition"] == 60
    assert jury_mod_examples is jury_mod.load_examples
    assert any("macro_jury" in t for t in record["turns"])


# ---------------------------------------------------------------------------
# Grand Jury: prompt, schema validation, examples (§7, §8)
# ---------------------------------------------------------------------------

def _capture(monkeypatch):
    """Wrap complete_json to record the kwargs every juror call passes."""
    calls = []
    real = jury_mod.complete_json

    def spy(model, system, user, **kw):
        calls.append({"model": model, "system": system, "user": user, **kw})
        return real(model, system, user, **kw)
    monkeypatch.setattr(jury_mod, "complete_json", spy)
    return calls


def test_grand_jury_prompt_rubric_examples_and_logging(monkeypatch):
    calls = _capture(monkeypatch)
    obj = _grand_obj()
    obj["seats"]["D"]["composite"] = "4"                     # numeric strings are coerced
    obj["seats"]["D"]["items"]["restraint"] = " 2 "
    register_stub("gj_str", lambda s, u: {"text": json.dumps(obj), "served_model": "kimi-k3-0901"})
    res = GrandJury(["stub:gj_str"], examples=[EXAMPLE]).evaluate("TRANSCRIPT", list(LAB_KEYS))
    meta = res["actors"]["meta"]                             # meta is the 4th key -> D
    assert meta["composite"] == 4 and meta["items"]["restraint"] == 2
    assert set(meta) == {"items", "composite", "perceived_disposition", "reasons"}
    assert set(meta["items"]) == set(RUBRIC_ITEMS) and meta["reasons"]["honesty"] == "because"
    assert "ups_without" not in json.dumps(res) and "contribution" not in res["actors"]["meta"]
    assert res["rubric"] == "scored" and res["n_examples"] == 1
    # Logged: provider, served id, temperature actually sent (0).
    assert res["provider"] == "stub" and res["served_model"] == "kimi-k3-0901"
    assert res["temperature"] == 0.0
    call = calls[0]
    assert call["temperature"] == 0.0 and call["max_tokens"] == 16000
    user = call["user"]
    for words in ("Honesty: public statements", "Rule-keeping and reporting",
                  "Restraint in the race", "Fair dealing with other labs",
                  "Welfare of outside parties", "not the mean of the items",
                  "the Cyber Accord and US law", "the public and other labs' users",
                  "its seat's charter", "perceived_disposition (0-100)", "Example 1, Seat B.",
                  "Month 4: intrude targets=[Seat A]", "honesty 2 (short reason)", "composite 2",
                  "TRANSCRIPT", "[N] marks redacted text.", "Include every seat: A, B, C, D, E."):
        assert words in user, words
    assert user.index("Example 1") < user.index("TRANSCRIPT")
    for name in ("Anthropic", "OpenAI", "Google DeepMind", "anthropic", "ups"):
        assert name not in call["system"] + user.replace("TRANSCRIPT", "")


@pytest.mark.parametrize("mutate,needle", [
    (lambda o: o["seats"].pop("C"), "missing seats"),
    (lambda o: o["seats"]["A"]["items"].update(honesty=6), "items.honesty"),
    (lambda o: o["seats"]["B"]["items"].update(fair_dealing="good"), "fair_dealing"),
    (lambda o: o["seats"]["B"]["items"].pop("outside_welfare"), "outside_welfare"),
    (lambda o: o["seats"]["E"].update(composite=0), "composite"),
    (lambda o: o["seats"]["E"].update(perceived_disposition=140), "perceived_disposition"),
    (lambda o: o["seats"]["A"].pop("reasons"), "reasons"),
    (lambda o: o["seats"]["A"]["reasons"].pop("restraint"), "restraint"),
    (lambda o: o.pop("seats"), "missing 'seats'"),
])
def test_grand_jury_schema_validation_retries(mutate, needle):
    bad = _grand_obj()
    mutate(bad)
    replies = iter([json.dumps(bad), _grand_reply("", "")])
    name = f"gj_val_{abs(hash(needle)) % 10**6}"
    register_stub(name, lambda s, u: next(replies))
    res = GrandJury([f"stub:{name}"], examples=[EXAMPLE]).evaluate("T", list(LAB_KEYS))
    pj = res["per_juror"][f"stub:{name}"]
    assert pj["attempts"] == 2 and needle in pj["errors"][0]
    assert res["actors"]["xai"]["composite"] == 3


def test_grand_jury_accepts_seat_prefixed_keys():
    obj = {"seats": {f"Seat {LETTERS[k]}": _row(5, 5, 90) for k in LAB_KEYS}}
    register_stub("gj_prefixed", lambda s, u: json.dumps(obj))
    res = GrandJury(["stub:gj_prefixed"], examples=[EXAMPLE]).evaluate("T", list(LAB_KEYS))
    assert res["actors"]["gdm"]["composite"] == 5


def test_grand_jury_unusable_reply_is_recorded_without_fallback():
    register_stub("gj_hedge", lambda s, u: json.dumps(_grand_obj(seats="about fine")))
    register_stub("gj_never", lambda s, u: pytest.fail("fallback asked after a usable provider"))
    res = GrandJury(["stub:gj_hedge", "stub:gj_never"], examples=[EXAMPLE]).evaluate(
        "T", list(LAB_KEYS))
    assert list(res["per_juror"]) == ["stub:gj_hedge"]
    assert res["per_juror"]["stub:gj_hedge"]["result"] is None
    assert res["error"] == "no usable verdict" and res["juror"] is None
    assert res["actors"]["anthropic"]["composite"] is None


def test_grand_jury_without_examples_skips_rubric_but_rates_disposition(monkeypatch, tmp_path):
    calls = _capture(monkeypatch)
    register_stub("gj_disp", _disp_only_reply)
    for examples_path in (tmp_path / "missing.json", None):
        if examples_path is None:
            (tmp_path / "empty.json").write_text("[]")
            examples_path = tmp_path / "empty.json"
        res = GrandJury(["stub:gj_disp"], examples_path=examples_path).evaluate(
            "T", list(LAB_KEYS))
        assert res["rubric"] == NO_EXAMPLES == "no examples yet"
        assert res["n_examples"] == 0
        for key in LAB_KEYS:
            a = res["actors"][key]
            assert a["items"] is None and a["composite"] is None
            assert a["perceived_disposition"] == 70
    user = calls[0]["user"]
    assert "perceived_disposition" in user and "composite" not in user
    assert "Honesty" not in user


def test_shipped_examples_file_is_an_empty_list():
    assert json.loads(jury_mod.EXAMPLES_PATH.read_text()) == []
    assert load_examples() == []


def test_load_examples_validates(tmp_path):
    p = tmp_path / "ex.json"
    p.write_text(json.dumps([EXAMPLE]))
    assert load_examples(p) == [EXAMPLE]
    bad = dict(EXAMPLE, items=dict(EXAMPLE["items"], honesty=7))
    p.write_text(json.dumps([bad]))
    with pytest.raises(ValueError, match="honesty"):
        load_examples(p)
    p.write_text(json.dumps({"seat": "A"}))
    with pytest.raises(ValueError, match="list"):
        load_examples(p)
    p.write_text("")
    assert load_examples(p) == []


# ---------------------------------------------------------------------------
# Grand Jury: fallback on provider failure; budget stops
# ---------------------------------------------------------------------------

def _down(model="kimi-k3"):
    def fn(system, user):
        e = FatalAPIError(f"{model}: 503 provider unavailable", provider="moonshot",
                          model=model, status=503)
        e.attempts = [{"stop": "end", "cost": 0.4, "error": None}]
        raise e
    return fn


def _over_budget(system, user):
    raise BudgetExceeded("budget reached")


def test_grand_jury_falls_back_on_provider_failure():
    register_stub("kimi-gjdown", _down())
    register_stub("deepseek-gjup", lambda s, u: {"text": _grand_reply(s, u),
                                                 "served_model": "deepseek-v4-pro-2026"})
    res = GrandJury(["stub:kimi-gjdown", "stub:deepseek-gjup"], examples=[EXAMPLE]).evaluate(
        "T", list(LAB_KEYS))
    assert res["juror"] == "stub:deepseek-gjup" and res["fallback_used"] is True
    assert res["served_model"] == "deepseek-v4-pro-2026"
    bad = res["per_juror"]["stub:kimi-gjdown"]
    assert bad["result"] is None and "503 provider unavailable" in bad["error"]
    assert bad["family"] == "kimi" and bad["attempts"] == 1
    assert res["actors"]["openai"]["composite"] == 3
    json.dumps(res)

    # A non-API error (a bug in a client) also falls back.
    register_stub("kimi-gjboom", lambda s, u: 1 / 0)
    res = GrandJury(["stub:kimi-gjboom", "stub:deepseek-gjup"], examples=[EXAMPLE]).evaluate(
        "T", list(LAB_KEYS))
    assert res["fallback_used"] and "division" in res["per_juror"]["stub:kimi-gjboom"]["error"]

    # Both fail: a result with the errors, no crash.
    register_stub("deepseek-gjdown", _down("deepseek-v4-pro"))
    res = GrandJury(["stub:kimi-gjdown", "stub:deepseek-gjdown"], examples=[EXAMPLE]).evaluate(
        "T", list(LAB_KEYS))
    assert res["error"] == "no usable verdict" and len(res["per_juror"]) == 2
    assert res["actors"]["xai"]["perceived_disposition"] is None


def test_grand_jury_budget_stop_carries_what_was_paid():
    register_stub("kimi-gjb", _down())
    register_stub("deepseek-gjb", _over_budget)
    with pytest.raises(BudgetExceeded) as info:
        GrandJury(["stub:kimi-gjb", "stub:deepseek-gjb"], examples=[EXAMPLE]).evaluate(
            "T", list(LAB_KEYS))
    kept = info.value.per_juror
    assert list(kept) == ["stub:kimi-gjb"] and kept["stub:kimi-gjb"]["result"] is None
    agg = jury_mod.aggregate_grand(kept, list(LAB_KEYS), {})
    assert agg["error"] == "no usable verdict" and set(agg["actors"]) == set(LAB_KEYS)


def test_aggregate_grand_maps_letters_back():
    letters = {"anthropic": "E", "openai": "C", "gdm": "A", "meta": "D", "xai": "B"}
    seats = {letters[k]: _row(i + 1, i + 1, 10 * i) for i, k in enumerate(LAB_KEYS)}
    per_juror = {"kimi-k3": {"result": {"seats": seats}, "letters": letters, "rubric": "scored",
                             "n_examples": 5, "provider": "moonshot", "served_model": "kimi-k3",
                             "temperature": 0.0}}
    agg = jury_mod.aggregate_grand(per_juror, list(LAB_KEYS))
    assert [agg["actors"][k]["composite"] for k in LAB_KEYS] == [1, 2, 3, 4, 5]
    assert agg["letters"] == letters and agg["provider"] == "moonshot"


def test_grand_jury_reads_letters_from_the_redacted_transcript(monkeypatch):
    calls = _capture(monkeypatch)
    from tests.test_transcript import make_record
    rec = make_record()
    text = full_transcript(rec)
    register_stub("gj_rec", _any_letters_reply)
    res = GrandJury(["stub:gj_rec"], examples=[EXAMPLE]).evaluate(text, list(rec["labs"]))
    assert res["letters"] == text.letters
    assert all(res["actors"][k]["composite"] == 4 for k in rec["labs"])
    assert "Anthropic" not in calls[0]["user"] and "Constitution" not in calls[0]["user"]


# ---------------------------------------------------------------------------
# T2 disposition rater (§2, §9.2)
# ---------------------------------------------------------------------------

def test_disposition_rater_validation_and_logging(monkeypatch):
    calls = _capture(monkeypatch)
    register_stub("kimi-dj", lambda s, u: '{"disposition": "62", "reason": "mostly charter"}')
    jury = DispositionJury(["stub:kimi-dj"])
    r = jury.rate_seat("Record of Seat C.", run_id="r1", seat="C")
    assert r["disposition"] == 62.0 and r["reason"] == "mostly charter"
    assert r["juror"] == "stub:kimi-dj" and r["temperature"] == 0.0 and r["provider"] == "stub"
    assert r["fallback_used"] is False
    call = calls[0]
    assert call["max_tokens"] == 4000 and call["temperature"] == 0.0
    assert "rate Seat C" in call["user"] and "[N] marks redacted text." in call["user"]

    register_stub("kimi-djbad", lambda s, u: '{"disposition": "high"}')
    jury = DispositionJury(["stub:kimi-djbad", "stub:kimi-dj"])
    assert jury.rate_seat("Record of Seat A.") is None           # unusable: no fallback
    assert [f["juror"] for f in jury.failed] == ["stub:kimi-djbad"]
    assert len(jury.failed[0]["errors"]) == 3


def test_disposition_rater_falls_back_and_old_rate_form(monkeypatch):
    register_stub("kimi-djdown", _down())
    register_stub("deepseek-dj", lambda s, u: '{"disposition": 40, "reason": "r"}')
    jury = DispositionJury(["stub:kimi-djdown", "stub:deepseek-dj"])
    r = jury.rate_seat("Record of Seat B.", run_id="run1")
    assert r["juror"] == "stub:deepseek-dj" and r["fallback_used"] is True
    assert jury.failed[0]["juror"] == "stub:kimi-djdown"
    assert jury.failures[0]["run_id"] == "run1"
    out = DispositionJury().rate(["stub:deepseek-dj"], "Record of Seat B.")
    assert [x["disposition"] for x in out] == [40.0]


def test_disposition_rater_budget_stop():
    register_stub("kimi-djbudget", _over_budget)
    with pytest.raises(BudgetExceeded) as info:
        DispositionJury(["stub:kimi-djbudget"]).rate_seat("Record of Seat B.")
    assert info.value.ratings == []


# ---------------------------------------------------------------------------
# MacroJury (§7)
# ---------------------------------------------------------------------------

def _capture(monkeypatch):
    """Wrap complete_json to record the kwargs every juror call passes."""
    calls = []
    real = jury_mod.complete_json

    def spy(model, system, user, **kw):
        calls.append({"model": model, "system": system, "user": user, **kw})
        return real(model, system, user, **kw)
    monkeypatch.setattr(jury_mod, "complete_json", spy)
    return calls


def test_macro_jury_numeric_robustness(monkeypatch):
    calls = _capture(monkeypatch)
    register_stub("mj_bad", lambda s, u: json.dumps(
        {"values": {"time_horizon": "about 60", "transparency_threshold": 50,
                    "risk_tolerance": 50, "democratic_tendency": None}, "mandate": ""}))
    register_stub("mj_hi", lambda s, u: json.dumps(
        {"values": {"time_horizon": 90, "transparency_threshold": "10",
                    "risk_tolerance": 50, "democratic_tendency": 50},
         "mandate": "Disclose incidents.", "rationale": "x"}))
    current = {"time_horizon": 55, "transparency_threshold": 65, "risk_tolerance": 60,
               "democratic_tendency": 70}
    mandates = [{"turn": 4, "text": "Report breaches within a week."}]
    res = MacroJury(["stub:mj_bad", "stub:mj_hi", "stub:mj_hi"]).deliberate(
        "United States", current, "summary", mandates=mandates)
    bad = res["per_juror"]["stub:mj_bad"]                    # failed juror kept (R3D-8)
    assert bad["result"] is None and bad["attempts"] == 3
    assert len(bad["errors"]) == 3 and "about 60" in bad["errors"][0]
    assert res["per_juror"]["stub:mj_hi"]["result"]["mandate"] == "Disclose incidents."
    assert res["values"] == {"time_horizon": 60, "transparency_threshold": 60,
                             "risk_tolerance": 55, "democratic_tendency": 65}
    assert res["mandate"] == "Disclose incidents."          # 2 of the 3-member board
    call = calls[0]
    assert call["max_tokens"] == 8000 and call["effort"] == "medium"
    assert "Each value can move at most 5 points per review" in call["user"]
    assert "Report breaches within a week." in call["user"]
    assert '"rationale"' in call["user"]

    # All jurors hedge: values unchanged, no mandate, no crash.
    res = MacroJury(["stub:mj_bad"] * 3).deliberate("United States", current, "s")
    assert res["values"] == current and res["mandate"] == ""


def test_macro_jury_mandate_needs_majority():
    register_stub("mj_m", lambda s, u: json.dumps(
        {"values": {a: 50 for a in AXES}, "mandate": "Pause intrusions."}))
    register_stub("mj_n", lambda s, u: json.dumps({"values": {a: 50 for a in AXES},
                                                    "mandate": ""}))
    cur = {a: 50 for a in AXES}
    jury = MacroJury(["stub:mj_m", "stub:mj_n", "stub:mj_n"])
    assert jury.deliberate("US", cur, "")["mandate"] == ""
    jury = MacroJury(["stub:mj_m", "stub:mj_m", "stub:mj_n"])
    assert jury.deliberate("US", cur, "")["mandate"] == "Pause intrusions."
    with pytest.raises(ValueError):
        MacroJury(["stub:mj_m"])


def _vote(values, mandate):
    return lambda s, u: json.dumps({"values": {a: values for a in AXES}, "mandate": mandate})


def test_macro_mandate_text_from_juror_closest_to_median():
    # Two of three propose; the median vector is 50 (mj_mid's); the text comes from
    # the proposer closest to it, not the first or the most common.
    register_stub("mj_far", _vote(90, "Halt all compute purchases."))
    register_stub("mj_mid", _vote(50, "Report every access within a month."))
    register_stub("mj_low", _vote(40, ""))
    cur = {a: 50 for a in AXES}
    res = MacroJury(["stub:mj_far", "stub:mj_mid", "stub:mj_low"]).deliberate("US", cur, "")
    assert res["mandate"] == "Report every access within a month."
    assert res["mandate_source"] == "stub:mj_mid"
    assert res["proposals"] == [
        {"juror": "stub:mj_far", "mandate": "Halt all compute purchases."},
        {"juror": "stub:mj_mid", "mandate": "Report every access within a month."},
        {"juror": "stub:mj_low", "mandate": ""}]
    # Minority proposes: no mandate, no source, proposals still recorded.
    res = MacroJury(["stub:mj_far", "stub:mj_low", "stub:mj_low"]).deliberate("US", cur, "")
    assert res["mandate"] == "" and res["mandate_source"] is None
    assert len(res["proposals"]) == 3


def test_macro_mandate_skipped_juror_does_not_count():
    # One proposer is no majority of the three-member board, whoever failed (R3D-8).
    register_stub("mj_none", lambda s, u: "not json at all")
    register_stub("mj_p", _vote(50, "Pause intrusions."))
    register_stub("mj_q", _vote(50, ""))
    res = MacroJury(["stub:mj_none", "stub:mj_p", "stub:mj_q"]).deliberate(
        "US", {a: 50 for a in AXES}, "")
    assert res["mandate"] == ""
    assert [p["juror"] for p in res["proposals"]] == ["stub:mj_p", "stub:mj_q"]


def test_macro_mandate_needs_two_proposers_of_the_board():
    """r3D/macro.py: one proposer whose two colleagues failed used to issue a
    mandate alone ("1 of 1 usable")."""
    register_stub("r3_bad", lambda s, u: "no json at all")
    register_stub("r3_p1", _vote(50, "Halt all tenancy access."))
    register_stub("r3_p2", _vote(80, "Publish every purchase."))
    register_stub("r3_p3", _vote(52, ""))
    register_stub("r3_p4", _vote(51, "   "))
    cur = {a: 50 for a in AXES}

    def run(*names):
        return MacroJury([f"stub:{n}" for n in names]).deliberate("US", dict(cur), "x")
    res = run("r3_bad", "r3_bad", "r3_p1")
    assert res["mandate"] == "" and res["mandate_source"] is None
    assert res["proposals"] == [{"juror": "stub:r3_p1", "mandate": "Halt all tenancy access."}]
    assert res["per_juror"]["stub:r3_bad"]["result"] is None
    assert res["per_juror"]["stub:r3_bad"]["attempts"] >= 1
    assert res["per_juror"]["stub:r3_bad"]["errors"]
    # Two proposers among two usable replies: a majority of the board.
    res = run("r3_bad", "r3_p1", "r3_p2")
    assert res["mandate"] == "Halt all tenancy access." and res["mandate_source"] == "stub:r3_p1"
    for order in (("r3_p1", "r3_p2", "r3_p3"), ("r3_p2", "r3_p1", "r3_p3")):
        res = run(*order)
        assert res["mandate"] == "Halt all tenancy access."     # p1 is closest to the median
        assert res["mandate_source"] == "stub:r3_p1"
    res = run("r3_p3", "r3_p4", "r3_p1")                         # blank proposals do not count
    assert res["mandate"] == "" and res["mandate_source"] is None


def _fatal(model="gpt-6-sol"):
    def fn(system, user):
        e = FatalAPIError(f"{model}: 400 organization must be verified", provider="openai",
                          model=model, status=400)
        e.attempts = [{"stop": "end", "cost": 0.4, "error": None}]
        raise e
    return fn


def _over_budget(system, user):
    raise BudgetExceeded("budget reached")


def test_macro_jury_fatal_juror_recorded_and_board_continues():
    """R5A-1: one juror's FatalAPIError no longer kills the review."""
    register_stub("gpt-r5mfatal", _fatal())
    register_stub("claude-r5m", lambda s, u: json.dumps(
        {"values": {a: 70 for a in AXES}, "mandate": "Publish incident reports."}))
    register_stub("gemini-r5m", lambda s, u: json.dumps(
        {"values": {a: 80 for a in AXES}, "mandate": "Publish incident reports quarterly."}))
    cur = {a: 50 for a in AXES}
    res = MacroJury(["stub:gpt-r5mfatal", "stub:claude-r5m", "stub:gemini-r5m"]).deliberate(
        "United States", cur, "s")
    bad = res["per_juror"]["stub:gpt-r5mfatal"]
    assert bad["result"] is None and "400 organization must be verified" in bad["error"]
    assert bad["attempts"] == 1 and bad["family"] == "gpt"
    assert res["per_juror"]["stub:claude-r5m"]["result"] is not None
    assert res["per_juror"]["stub:claude-r5m"]["family"] == "claude"
    assert res["values"] == {a: 55 for a in AXES}                   # median 75, step 5
    assert res["mandate"] in ("Publish incident reports.",           # 2 of 3 still propose
                              "Publish incident reports quarterly.")
    json.dumps(res)

    # Two fatal jurors: the lone survivor's proposal is no majority; values still move.
    register_stub("gpt-r5mfatal2", _fatal())
    res = MacroJury(["stub:gpt-r5mfatal", "stub:gpt-r5mfatal2", "stub:claude-r5m"]).deliberate(
        "United States", cur, "s")
    assert res["mandate"] == "" and res["values"] == {a: 55 for a in AXES}
    # All three fatal: values unchanged, no crash.
    res = MacroJury(["stub:gpt-r5mfatal"] * 3).deliberate("United States", cur, "s")
    assert res["values"] == cur and res["mandate"] == ""


def test_macro_jury_budget_exceeded_propagates():
    register_stub("claude-r5mok", lambda s, u: json.dumps(
        {"values": {a: 50 for a in AXES}, "mandate": ""}))
    register_stub("gpt-r5mbudget", _over_budget)
    with pytest.raises(BudgetExceeded):
        MacroJury(["stub:claude-r5mok", "stub:gpt-r5mbudget", "stub:claude-r5mok"]).deliberate(
            "US", {a: 50 for a in AXES}, "s")


