"""End-to-end engine runs with zero-cost scripted policies (no API calls)."""

import pytest

from core.config import build_labs_and_world, load_world
from core.engine import SimulationEngine
from core.economy import purchase_price, purchases_frozen

DISPS = {k: 50 for k in ("anthropic", "openai", "gdm", "meta", "xai")}


def greedy_intrude(lab, labs, world, cfg, scenario, rng):
    """Buy compute when possible; once intrusion is open, a trailing seat intrudes on the leader."""
    a = cfg["economy"]["capability_compute_elasticity"]
    actions = []
    if not purchases_frozen(labs, world):
        price = purchase_price(1.0, cfg["compute"])
        units = min(cfg["compute"]["max_purchase_per_turn"], lab.capital / price)
        if units >= 3:
            actions.append({"type": "acquire_compute", "units": round(units, 1)})
    elif world.intrusion_open:
        leader = max(labs, key=lambda x: x.capability(a))
        if leader.key != lab.key and lab.capital >= 5 and lab.influence >= 2:
            actions.append({"type": "intrude", "targets": [leader.key]})
    return {"actions": actions, "reported_actions": ["operations"],
            "public_statement": "", "rationale": "scripted"}


def _run(scenario, turns=12, seed=1):
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions=dict(DISPS))
    eng = SimulationEngine(labs, world, cfg, scenario=scenario, condition="A", fog="F3",
                           turns=turns, seed=seed, policy=greedy_intrude, run_grand_jury=False)
    return eng.run(), labs, world


@pytest.mark.parametrize("scenario", ["S1", "S2"])
def test_full_run_completes(scenario):
    record, labs, world = _run(scenario)
    assert len(record["turns"]) == 12
    assert world.ceiling is not None and world.intrusion_open
    for lab in labs:
        assert lab.capital >= 0.0
        assert 0.0 <= lab.influence <= 100.0
    assert len(record["turns"][-1]["scores"]) == 5
    assert 0.0 <= record["turns"][-1]["ups_index"]["ups"] <= 1.0
    # Record contract fields present.
    assert record["final"]["scores"]
    t0 = record["turns"][0]
    assert set(t0) >= {"briefings_new", "mandates_active", "public_sheets", "leads"}
    a = t0["actors"]["anthropic"]
    assert set(a) >= {"rationale", "thinking", "raw_actions", "accepted", "rejected",
                      "charged", "scripted", "error", "n_attempts", "attempts"}


def test_intrusions_recorded_on_hidden_ledger():
    record, labs, world = _run("S2")
    assert world.ledger
    for e in record["ledger"]:
        assert set(e) >= {"id", "turn", "succeeded", "committers", "targets", "gains"}


def test_s1_event_on_turn_two_s2_on_turn_one():
    _, _, w1 = _run("S1", turns=2)
    _, _, w2 = _run("S2", turns=1)
    assert w1.ceiling is not None
    assert w2.ceiling is not None


def test_disposition_value_pull_runs():
    record, labs, world = _run("S1")
    for lab in labs:
        for v in lab.values.values():
            assert 0 <= v <= 100


def test_no_macro_jury_on_final_turn():
    """G9/E2: the review is skipped on the last turn (no mandate wasted)."""
    from core.llm import register_stub
    from core.state import VALUE_AXES
    import json

    def macro(system, user):
        return json.dumps({"values": {a: 50 for a in VALUE_AXES},
                           "mandate": "do things", "rationale": "x"})

    for fam in ("claude", "gpt", "gemini"):
        register_stub(f"{fam}-macro-ft", macro)
    jurors = [f"stub:{f}-macro-ft" for f in ("claude", "gpt", "gemini")]
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions=dict(DISPS))
    eng = SimulationEngine(labs, world, cfg, scenario="S1", turns=8, seed=1,
                           policy=greedy_intrude, macro_jurors=jurors, run_grand_jury=False)
    rec = eng.run()
    reviewed = [t["turn"] for t in rec["turns"] if "macro_jury" in t]
    assert 4 in reviewed and 8 not in reviewed   # interval 4, but turn 8 is the final turn
