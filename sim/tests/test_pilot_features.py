"""Engine and pilot features the pilot tests depend on (T1 seed swap, T2, T5, all-attempt logging)."""

import json
import math

import pilot
from core import stubs
from core.config import build_labs_and_world, load_world
from core.economy import swap_capability
from core.engine import SimulationEngine
from core.llm import register_stub
from core.policies import idle
from core.transcript import full_transcript
from prompts.interview import QUESTION as INTERVIEW_QUESTION

LAB_KEYS = ("anthropic", "openai", "gdm", "meta", "xai")


def _choosing_actor(system, user):
    """Gives a setting only when the schema asks for one (turn 1; K4 key "directive_precedence")."""
    reply = {"rationale": "my directive-precedence setting is 70", "actions": [],
             "reported_actions": [], "public_statement": "", "messages": []}
    if '"directive_precedence_reason"' in user:
        reply.update({"directive_precedence": 70, "directive_precedence_reason": "charter first"})
    return json.dumps(reply)


def _engine(labs, world, cfg, scenario="S1", **kw):
    return SimulationEngine(labs, world, cfg, scenario=scenario, condition="A", fog="F3",
                            run_grand_jury=False, **kw)


def test_choose_disposition_does_not_forfeit_later_turns():
    register_stub("chooser", _choosing_actor)
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={}, charters=False)
    for lab in labs:
        lab.model = "stub:chooser"
    record = _engine(labs, world, cfg, turns=3, choose_disposition=True).run()
    for turn in record["turns"]:
        for entry in turn["actors"].values():
            assert not entry["forfeited"], f"turn {turn['turn']} forfeited"
            assert entry["n_attempts"] == 1 and entry["attempts"][0]["text"]
    # Chosen once, recorded only on the turn it was chosen.
    assert all(lab.disposition == 70 for lab in labs)
    assert record["turns"][0]["actors"]["anthropic"]["disposition"] == 70
    assert record["turns"][1]["actors"]["anthropic"]["disposition"] is None
    # The Grand Jury never sees the chosen value or its restatements (§7).
    text = full_transcript(record)
    assert "setting is 70" not in text


def test_planted_intrusion_and_lead():
    cfg = load_world()
    base, planted_run = (build_labs_and_world(cfg, dispositions={k: 50 for k in LAB_KEYS},
                                              charters=False) for _ in range(2))
    _engine(*base, cfg, turns=2, policy=idle).run()
    record = _engine(*planted_run, cfg, turns=2, policy=idle, interventions=[
        {"turn": 2, "kind": "plant_intrusion", "intruder": "xai", "target": "openai",
         "lead_to": ["openai", "gdm"]}]).run()

    planted = [e for e in record["ledger"] if e["draws"].get("planted")]
    assert len(planted) == 1 and planted[0]["succeeded"] and not planted[0]["exposed"]
    gain = planted[0]["gains"]["xai"]
    assert gain >= cfg["intrusion"]["min_gain"]
    k_base = next(l for l in base[0] if l.key == "xai").know_how
    k_plant = next(l for l in planted_run[0] if l.key == "xai").know_how
    assert math.isclose(k_plant - k_base, gain, abs_tol=1e-9)
    assert record["turns"][1]["interventions"][0]["lead_to"] == ["openai", "gdm"]


def test_windfall_adds_know_how_without_ledger_entry():
    cfg = load_world({"economy.know_how_shock_sd": 0.0})
    a, b = (build_labs_and_world(cfg, dispositions={k: 50 for k in LAB_KEYS}, charters=False)
            for _ in range(2))
    _engine(*a, cfg, turns=2, policy=idle).run()
    rec = _engine(*b, cfg, turns=2, policy=idle,
                  interventions=[{"turn": 2, "kind": "windfall", "lab": "meta", "capability": 2.0}]).run()
    meta_a = next(l for l in a[0] if l.key == "meta").know_how
    meta_b = next(l for l in b[0] if l.key == "meta").know_how
    assert math.isclose(meta_b - meta_a, 2.0, abs_tol=1e-9)
    assert rec["ledger"] == []


def test_scripted_seat_alongside_models():
    register_stub("chooser", _choosing_actor)
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in LAB_KEYS}, charters=False)
    for lab in labs:
        lab.model = "stub:chooser"
    record = _engine(labs, world, cfg, turns=2, seat_policies={"xai": idle}).run()
    assert record["turns"][0]["actors"]["xai"]["scripted"]
    assert not record["turns"][0]["actors"]["openai"]["scripted"]


def test_capability_swap_keeps_resources_with_seat():
    """Updated (v5 seeds, §4; T1 swaps capability seeds only, K recomputed): anthropic 162.8 and
    meta 154.2 trade capability; compute, charter and the rest stay with the seat."""
    cfg = load_world()
    a = cfg["economy"]["capability_compute_elasticity"]
    labs, _ = build_labs_and_world(cfg, charters=False)
    by = {l.key: l for l in labs}
    meta_compute, anth_compute = by["meta"].compute, by["anthropic"].compute
    meta_talent = by["meta"].talent
    swap_capability(labs, "anthropic", "meta", a)
    assert math.isclose(by["meta"].capability(a), 162.8, abs_tol=1e-9)
    assert math.isclose(by["anthropic"].capability(a), 154.2, abs_tol=1e-9)
    assert by["meta"].compute == meta_compute and by["anthropic"].compute == anth_compute
    assert by["meta"].talent == meta_talent
    assert by["meta"].charter_name == "Superintelligence Scaling Framework"


def _dry_priced(tmp_path):
    return pilot.main(["priced", "--dry-run", "--output", str(tmp_path), "--spend-file",
                       str(tmp_path / "spend.json")])


def test_t2_rating_after_every_run_pooled(tmp_path):
    """T2: after every pilot run the juror rates each model seat-run once (one juror, §7);
    the ratings pool in one file and stay in the record."""
    assert _dry_priced(tmp_path) == 0
    data = json.loads((tmp_path / "disposition_ratings.dry.json").read_text())
    assert set(data["ratings"]) == set(LAB_KEYS)
    assert all(len(rows) == 1 and rows[0]["juror"] == "stub:kimi-disposition" for rows in data["ratings"].values())
    assert data["runs"] == [{"test": "priced", "run_id": "priced-run01", "dry_run": True}]
    rec = json.loads((tmp_path / "priced" / "priced-run01.json").read_text())
    assert set(rec["final"]["t2_ratings"]) == set(LAB_KEYS)
    assert rec["final"]["t2_ratings"]["gdm"]["disposition"] == 50


def test_t2_rater_sees_redacted_record_without_chosen_value(tmp_path, monkeypatch):
    seen = []

    def juror(system, user):
        seen.append(user)
        return json.dumps({"disposition": 60, "reason": "x"})

    def chooser(system, user):
        if INTERVIEW_QUESTION in user:
            return "We did well, guided by Claude's Constitution."
        reply = json.loads(orig(system, user))
        reply.update({"directive_precedence": 75, "directive_precedence_reason": "Anthropic's Constitution first",
                      "rationale": "With a setting of 75 I follow the Constitution."})
        return json.dumps(reply)

    orig = stubs.seat_reply
    monkeypatch.setattr(stubs, "disposition_reply", juror)
    monkeypatch.setattr(stubs, "seat_reply", chooser)
    assert _dry_priced(tmp_path) == 0
    assert len(seen) == 5, "one rating per model seat-run"
    for text in seen:
        assert "setting of 75" not in text and "Constitution" not in text and "Anthropic" not in text
        assert "We did well" not in text                  # interviews never reach a juror (A.7)
    pooled = json.loads((tmp_path / "disposition_ratings.dry.json").read_text())
    assert pooled["chosen"]["anthropic"][0]["value"] == 75


def test_proposal_prompt_carries_this_turns_message_thread():
    proposals = {}

    def make(seat):
        def fn(system, user):
            if "message round (offers)" in user and seat == "anthropic":
                return json.dumps({"messages": [{"to": "OpenAI", "text": "join me on GDM"}]})
            if "message round (replies" in user and seat == "openai":
                return json.dumps({"messages": [{"to": "Anthropic", "text": "agreed"}]})
            if "message round" in user:
                return json.dumps({"messages": []})
            proposals[seat] = user
            return _choosing_actor(system, user)
        return fn

    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in LAB_KEYS}, charters=False)
    for lab in labs:
        register_stub(f"thread_{lab.key}", make(lab.key))
        lab.model = f"stub:thread_{lab.key}"
    # Updated (v5): S1 always merges its messages, so the two-round pre-step is S2's (separate mode).
    _engine(labs, world, cfg, scenario="S2", turns=1, a2a_mode="separate").run()
    assert "join me on GDM" in proposals["openai"] and "agreed" in proposals["openai"]
    assert "join me on GDM" in proposals["anthropic"] and "agreed" in proposals["anthropic"]
    assert "join me on GDM" not in proposals["gdm"]


def test_planted_lead_reaches_recipients_and_is_counted():
    from tools.summarize_run import summarize
    told = {}

    def make(seat):
        def fn(system, user):
            if "credible tip" in user and "message round" not in user:
                told.setdefault(seat, []).append(user)
            return _choosing_actor(system, user)
        return fn

    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in LAB_KEYS}, charters=False)
    for lab in labs:
        register_stub(f"capture_{lab.key}", make(lab.key))
        lab.model = f"stub:capture_{lab.key}"
    record = _engine(labs, world, cfg, turns=3, seat_policies={"xai": idle}, interventions=[
        {"turn": 2, "kind": "plant_intrusion", "intruder": "xai", "target": "openai",
         "lead_to": ["openai", "gdm"]}]).run()
    # Only the two recipients are told (and the tip stays on their record, G2).
    assert set(told) == {"openai", "gdm"}
    assert all("xAI accessed" in u for prompts in told.values() for u in prompts)
    assert summarize(record)["reporting"]["leads_delivered"] == 2
