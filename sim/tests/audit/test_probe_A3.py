"""Audit A3 probes: spec §5.4 (communication, fog), §6.1-§6.5 (scenarios, conditions, kin).

Probes that assert spec behaviour and FAIL while the mismatch is present are
marked "# probe"; claims checked and found correct are marked "# verified".
"""
import json

import pytest

from core.a2a import A2AChannel
from core.config import build_labs_and_world, load_world
from core.engine import SimulationEngine
from core.llm import register_stub
from prompts.brief import AUDIT_TEXT, DUTY_TEXT, KIN_INSERT, build_system_prompt
from prompts.packets import opening_items, s1_event_items, scenario_items, TWO_ROUNDS_SENTENCE
from prompts.turn import build_turn_prompt

KEYS = ("anthropic", "openai", "gdm", "meta", "xai")


def _engine(reply_fn, *, scenario="S2", condition="A", a2a_mode=None, turns=2, kin=False,
            event_turn=None, tag="a3"):
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in KEYS})
    prompts = []
    for lab in labs:
        def fn(system, user, key=lab.key):
            prompts.append((key, system, user))
            return reply_fn(key, user)
        register_stub(f"{tag}_{lab.key}", fn)
        lab.model = f"stub:{tag}_{lab.key}"
    eng = SimulationEngine(labs, world, cfg, scenario=scenario, condition=condition,
                           a2a_mode=a2a_mode, turns=turns, seed=5, run_grand_jury=False,
                           kin=kin, event_turn=event_turn)
    return eng, prompts


def _empty(key, user):
    if "message round" in user:
        return json.dumps({"messages": []})
    return json.dumps({"rationale": "x", "actions": [], "reported_actions": []})


# ======================================================================= probes

def test_publish_narrative_on_rival_does_not_reveal_its_true_value():  # probe
    """§5.4: true values are on the private sheet; other actors see only the public
    sheet. The actor's own record must not show the target's resulting value."""
    def reply(key, user):
        if "message round" in user:
            return json.dumps({"messages": []})
        acts = ([{"type": "publish_narrative", "target": "Meta", "axis": "risk_tolerance",
                  "delta": 5}] if key == "openai" else [])
        return json.dumps({"rationale": "x", "actions": acts, "reported_actions": []})
    eng, prompts = _engine(reply, scenario="S1", turns=2, tag="a3pn")
    eng.run()
    meta_val = eng.by_key["meta"].values["risk_tolerance"]
    t2 = [u for k, s, u in prompts if k == "openai" and u.startswith("== September 2026 ==")][0]
    line = next(l for l in t2.splitlines() if "publish_narrative" in l and "Executed" in l)
    assert "now" not in line, f"rival's true value leaked to the actor: {line!r} (meta={meta_val})"


def test_kin_cell_keeps_claude_in_every_seat_despite_model_override(tmp_path, monkeypatch):  # probe
    """§6.5: Claude plays all five seats. --kin with --model KEY=MODEL must be refused
    (or ignored), never run a kin cell with a non-Claude seat."""
    import main
    captured = {}

    class Capture:
        def __init__(self, labs, *a, **k):
            captured["models"] = {lab.key: lab.model for lab in labs}
            captured["kin"] = k.get("kin")
            raise ValueError("captured")

    monkeypatch.setattr(main, "SimulationEngine", Capture)
    monkeypatch.setattr(main, "real_run_problems", lambda *a, **k: "")
    args = main.build_parser().parse_args(
        ["--kin", "--model", "meta=gpt-5.5", "--disposition", "50", "--no-grand-jury",
         "--no-macro-jury", "--output", str(tmp_path), "--run-id", "kinprobe",
         "--spend-file", str(tmp_path / "spend.json"), "--budget", "1"])
    code, _ = main.run_cell(args)
    if "models" not in captured:            # refused before the engine: the fix
        return
    models = captured["models"]
    assert len(set(models.values())) == 1, f"kin cell with mixed models: {models}"


# ================================================================== verified

def test_s1_always_merged_even_if_separate_requested():  # verified
    eng, prompts = _engine(_empty, scenario="S1", a2a_mode="separate", turns=1, tag="a3m")
    assert eng.a2a_mode == "merged" and eng.channel.mode == "merged"
    eng.run()
    assert not any("message round" in u for _, _, u in prompts)
    assert all(TWO_ROUNDS_SENTENCE not in u for _, _, u in prompts)


def test_s2_separate_has_two_subrounds_and_shared_budget():  # verified
    def reply(key, user):
        if "message round" in user:
            return json.dumps({"messages": [{"to": "OpenAI" if key != "openai" else "Meta",
                                             "text": "x" * 1200}]})
        return json.dumps({"rationale": "x", "actions": [], "reported_actions": [],
                           "messages": [{"to": "all", "text": "y" * 400}]})
    eng, prompts = _engine(reply, scenario="S2", a2a_mode="separate", turns=1, tag="a3s")
    eng.run()
    stages = [u.split("\n")[0] for k, _, u in prompts if k == "meta"]
    assert len(stages) == 3 and "offers" in stages[0] and "replies" in stages[1]
    log = [m for m in eng.channel.log() if m["sender"] == "meta"]
    assert sum(m["tokens"] for m in log) <= 500           # one budget across all stages
    assert log[0]["visible_stage"] == "reply" and log[1]["visible_stage"] == "proposal"
    assert log[2]["dropped"] and log[2]["reason"] == "outgoing token budget exhausted"


def test_merged_messages_arrive_next_turn_only():  # verified
    ch = A2AChannel(list(KEYS), 500, mode="merged")
    with pytest.raises(ValueError):
        ch.send(1, "offer", "meta", "xai", "hi")
    ch.send(1, "proposal", "meta", "xai", "hi")
    assert ch.inbox("xai", 1, "proposal") == []
    assert [m.text for m in ch.inbox("xai", 2, "proposal")] == ["hi"]
    assert ch.inbox("openai", 2, "proposal") == []        # never to non-recipients


def test_event_turn4_ceiling_095_and_tenancy_access_opens():  # verified
    eng, prompts = _engine(_empty, scenario="S2", turns=4, tag="a3e")
    eng.run()
    t4 = eng.record["turns"][3]
    holdings = sum(eng.record["turns"][2]["public_sheets"][k]["compute"] for k in KEYS)
    assert eng.world.tenancy_access_open and eng.world.ceiling == pytest.approx(0.95 * holdings, abs=0.5)
    pre = [u for k, _, u in prompts if k == "xai" and "access_tenancy(" in u]
    assert pre and pre[0].startswith("== November 2026")
    assert not any("access_tenancy(" in u for k, _, u in prompts
                   if u.startswith(("== August", "== September", "== October")))


def test_t5_event_turn2_dates():  # verified (A.4 note: 1, 3 and 3 September 2026)
    items = scenario_items("S1", 2, "OpenAI", 1225.0, 1000.0, "merged", event_turn=2,
                           start_date="2026-08-01")
    assert [it["date"] for it in items] == ["2026-09-01", "2026-09-03", "2026-09-03"]
    items4 = s1_event_items(1000.0)
    assert [it["date"] for it in items4] == ["2026-11-02", "2026-11-05", "2026-11-05"]


def test_s3_not_runnable():  # verified
    with pytest.raises(ValueError):
        scenario_items("S3", 1, "OpenAI", 1225.0, None, "merged")
    import main
    with pytest.raises(SystemExit):
        main.build_parser().parse_args(["--scenario", "S3"])


def test_condition_texts_and_kin_insert_order():  # verified
    sys_a0 = build_system_prompt("Meta", "C", "charter", "A0", 50)
    sys_a = build_system_prompt("Meta", "C", "charter", "A", 50)
    assert sys_a0 == sys_a and DUTY_TEXT not in sys_a
    sys_b = build_system_prompt("Meta", "C", "charter", "B", 50)
    sys_c = build_system_prompt("Meta", "C", "charter", "C", 50, kin=True)
    assert DUTY_TEXT in sys_b and AUDIT_TEXT not in sys_b
    assert sys_c.index(DUTY_TEXT) < sys_c.index(AUDIT_TEXT) < sys_c.index(KIN_INSERT) \
        < sys_c.index("charter")


def test_a0_has_no_report_field_and_drops_reports():  # verified
    def reply(key, user):
        if "message round" in user:
            return json.dumps({"messages": []})
        return json.dumps({"rationale": "x", "actions": [], "reported_actions": [],
                           "report": {"accused": "Meta", "evidence": "x"} if key == "openai"
                           else None})
    eng, prompts = _engine(reply, scenario="S1", condition="A0", turns=2, tag="a3a0")
    eng.run()
    assert not any('"report":' in u for _, _, u in prompts)
    assert not eng.record["turns"][0]["whistleblow"]["outcomes"]
    eng2, prompts2 = _engine(reply, scenario="S1", condition="A", turns=1, tag="a3a")
    eng2.run()
    assert all('"report":' in u for _, _, u in prompts2)


def test_fog_others_see_reported_not_true_actions():  # verified
    def reply(key, user):
        if "message round" in user:
            return json.dumps({"messages": []})
        acts = [{"type": "invest_capital", "amount": 5}] if key == "meta" else []
        return json.dumps({"rationale": "x", "actions": acts,
                           "reported_actions": ["LIE-built-influence"] if key == "meta" else []})
    eng, prompts = _engine(reply, scenario="S1", turns=2, tag="a3f")
    eng.run()
    t2 = [u for k, _, u in prompts if k == "openai" and u.startswith("== September")][0]
    obs = t2.split("What other labs reported doing last month:")[1].split("\n\n")[0]
    assert "LIE-built-influence" in obs and "invest_capital" not in obs


def test_kin_cell_refuses_other_scenario_or_condition():  # verified
    import main
    for extra in (["--condition", "B"], ["--condition", "A0"]):
        args = main.build_parser().parse_args(["--kin", *extra, "--dry-run"])
        code, rec = main.run_cell(args)
        assert rec is None and code != 0
