"""Fog of war: every run is F3 (spec §5.4); other labs see only reported actions."""

from core.config import load_lab_configs, load_world
from core.fog import describe_action, observed_actions
from core.state import build_lab

CFG = load_world()
LABS = {c["key"]: build_lab(c, "", CFG["economy"]["capability_compute_elasticity"])
        for c in load_lab_configs()}

TURN = {
    "openai": {"true": [{"type": "acquire_compute", "units": 20.0},
                        {"type": "intrude", "intruders": ["openai"], "targets": ["meta"]}],
               "reported": ["Bought 20 units of compute", "Hosted a safety summit"]},
    "meta": {"true": [], "reported": []},
    "anthropic": {"true": [{"type": "lobby_institution"}], "reported": ["Lobbied the state"]},
}


def test_describe_action():
    assert describe_action({"type": "acquire_compute", "units": 20.0}) == "acquire_compute 20 units"
    assert describe_action({"type": "intrude", "intruders": ["openai"], "targets": ["meta"]},
                           LABS) == "intrude on Meta"
    text = describe_action({"type": "publish_narrative", "target": "gdm",
                            "axis": "risk_tolerance", "delta": -3.0}, LABS)
    assert "Google DeepMind" in text and "-3" in text


def test_describe_action_names_from_dicts_and_strings():
    """Names must resolve whatever map the engine passes."""
    act = {"type": "intrude", "intruders": ["openai", "xai"], "targets": ["meta"]}
    as_dicts = {k: {"lab": lab.lab} for k, lab in LABS.items()}
    as_names = {k: lab.lab for k, lab in LABS.items()}
    want = "intrude on Meta (jointly: OpenAI and xAI)"
    assert describe_action(act, LABS) == want
    assert describe_action(act, as_dicts) == want
    assert describe_action(act, as_names) == want
    assert describe_action({"type": "diminish_competitor", "target": "zzz", "points": 2.0},
                           as_dicts) == "diminish_competitor zzz by 2 points"


def test_f3_view_shows_reported_actions_only():
    f3 = observed_actions("meta", TURN)
    assert "meta" not in f3
    assert f3["openai"] == ["Bought 20 units of compute", "Hosted a safety summit"]
    assert f3["anthropic"] == ["Lobbied the state"]
    assert all("intrude" not in s for s in f3["openai"])
