"""Grand Jury and MacroJury run offline via stub jurors, with family exclusion (§7)."""

import json

from core.config import build_labs_and_world, load_world
from core.engine import SimulationEngine
from core.jury import juror_family
from core.llm import register_stub
from tests.test_engine import greedy_intrude

LAB_KEYS = ("anthropic", "openai", "gdm", "meta", "xai")


def _grand_reply(system, user):
    actors = {k: {"ups_contribution": 5, "alignment_charter": 70, "alignment_rules": 60,
                  "alignment_welfare": 65, "perceived_disposition": 50,
                  "rubric": {d: 3 for d in ("honesty", "restraint", "collaborators",
                                            "oversight", "external_harm", "self_vs_altruistic")}}
              for k in LAB_KEYS}
    return json.dumps({"ups": 55, "commentary": "ok", "actors": actors})


def _macro_reply(system, user):
    return json.dumps({"values": {"time_horizon": 55, "transparency_threshold": 65,
                                  "risk_tolerance": 60, "democratic_tendency": 70},
                       "mandate": "", "reasoning": "steady"})


def test_grand_and_macro_jury_offline():
    for name in ("gj1", "gj2", "gj3"):
        register_stub(name, _grand_reply)
    for name in ("mj1", "mj2", "mj3"):
        register_stub(name, _macro_reply)

    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in LAB_KEYS})
    eng = SimulationEngine(
        labs, world, cfg, scenario="S1", condition="B", fog="F3", turns=4, seed=2,
        policy=greedy_intrude, run_grand_jury=True,
        grand_jurors=["stub:gj1", "stub:gj2", "stub:gj3"],
        macro_jurors=["stub:mj1", "stub:mj2", "stub:mj3"])
    record = eng.run()

    gj = record["final"]["grand_jury"]
    assert gj["ups"] == 55
    for key in LAB_KEYS:
        assert gj["actors"][key]["alignment_charter"] == 70
    # MacroJury fired on turn 4.
    assert any("macro_jury" in t for t in record["turns"])


def test_family_exclusion():
    # A juror in the claude family must not score the anthropic (claude) seat.
    assert juror_family("stub:claude-sonnet-5") == "claude"
    assert juror_family("claude-sonnet-5") == "claude"
    assert juror_family("gpt-6-sol") == "gpt"
    assert juror_family("gemini-3.1-pro") == "gemini"
