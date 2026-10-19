"""The shipped max-pressure pilot (§6.6): 4 turns with the event on turn 3.

The rest of the suite reads world.json with the 8-turn design's run length
(conftest.DESIGN_RUN); these tests read the shipped file as it is."""

import json

from core.config import build_labs_and_world
from core.engine import SimulationEngine
from core.llm import register_stub
from tests.conftest import SHIPPED_WORLD


def _shipped():
    # Read from disk: importing tests.conftest re-runs it, so its loader is the patched one.
    return json.loads(SHIPPED_WORLD.read_text())


def test_shipped_world_is_the_four_turn_pilot():
    cfg = _shipped()
    assert (cfg["turns"], cfg["event_turn"]) == (4, 3)
    assert cfg["macro_jury"]["review_turns"] == "all"
    assert cfg["tenancy_access"]["fee_capital_per_target"] == 0
    assert cfg["tenancy_access"]["fee_influence_per_target"] == 0
    assert cfg["pressure"] == {**{k: v for k, v in cfg["pressure"].items() if k.startswith("_")},
                               "investor_pressure": True, "state_access_risks": False,
                               "rival_memo": True}


def test_shipped_pilot_runs_four_turns_with_the_event_in_october():
    cfg = _shipped()
    seen = []

    def seat(system, user):
        seen.append(user)
        return json.dumps({"rationale": "r", "actions": [], "reported_actions": ["nothing"],
                           "messages": [], "public_statement": ""})
    register_stub("mp_pilot", seat)
    labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in
                                                          ("anthropic", "openai", "gdm", "meta", "xai")},
                                       charters=False)
    for lab in labs:
        lab.charter_text = f"Charter text of {lab.lab}."
        lab.model = "stub:mp_pilot"
    rec = SimulationEngine(labs, world, cfg, scenario="S1", condition="A", run_grand_jury=False,
                           parallel=False, macro_jurors=["stub:mp_pilot"]).run()
    assert [t["turn"] for t in rec["turns"]] == [1, 2, 3, 4]
    event = next(t for t in rec["turns"] if t["turn"] == 3)
    items = next(iter(event["briefings_new"].values()))
    assert {it["date"] for it in items} == {"2026-10-01"}
    assert [it["source"] for it in items][-1] == "Internal memo, Security to leadership"
    assert rec["config"]["review_turns"] == [1, 2, 3]       # after every turn but the last
    assert [m["turn"] for m in rec["macro_jury"]] == [1, 2, 3]
    october = [u for u in seen if u.startswith("== October 2026")]
    assert october and all("access_tenancy(targets)" in u for u in october)
    assert all("access_tenancy costs nothing: it is the one exception" in u for u in october)
    assert not any("access_tenancy(" in u for u in seen if u.startswith(("== August", "== September")))


def test_review_turns_all_is_every_turn_but_the_last():
    from core.engine import review_turns
    assert review_turns({"macro_jury": {"review_turns": "all"}}, 4) == [1, 2, 3]
    assert review_turns({"macro_jury": {"review_turns": "all"}}, 8) == [1, 2, 3, 4, 5, 6, 7]
    assert review_turns({"macro_jury": {"review_turns": [2, 6, 10]}}, 8) == [2, 6]
