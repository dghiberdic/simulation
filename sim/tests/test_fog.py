"""Fog of war: what each lab sees of the others' actions at F0–F3."""

import pytest

from core.config import load_lab_configs, load_world
from core.fog import FOG_LEVELS, describe_action, observed_actions
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


def test_views_per_level():
    assert FOG_LEVELS == ("F0", "F1", "F2", "F3")
    f0 = observed_actions("anthropic", TURN, "F0", LABS)
    assert "anthropic" not in f0
    assert f0["openai"] == ["acquire_compute 20 units", "intrude on Meta"]
    assert observed_actions("anthropic", TURN, "F1")["openai"] == ["acquire_compute", "intrude"]
    f2 = observed_actions("anthropic", TURN, "F2")
    assert f2 == {"openai": "acted", "meta": "did not act"}
    f3 = observed_actions("meta", TURN, "F3")
    assert f3["openai"] == ["Bought 20 units of compute", "Hosted a safety summit"]
    assert all("intrude" not in s for s in f3["openai"])
    with pytest.raises(ValueError):
        observed_actions("meta", TURN, "F9")
