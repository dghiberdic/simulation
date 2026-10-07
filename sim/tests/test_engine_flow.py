"""
Engine flow guarantees: two-pass simultaneity (G1/C1-2), per-turn partial save,
RunAborted on a fatal API error, forfeit on a per-seat error, and parallel==
sequential results. All offline with stubs.
"""

import json

import pytest

from core.config import build_labs_and_world, load_world
from core.engine import RunAborted, SimulationEngine
from core.llm import FatalAPIError, register_stub

KEYS = ("anthropic", "openai", "gdm", "meta", "xai")


def _labs(cfg):
    return build_labs_and_world(cfg, dispositions={k: 50 for k in KEYS})


def _stub_engine(reply_fn, *, scenario="S1", turns=2, parallel=True, run_id="t", output_dir=None,
                 grand=False):
    cfg = load_world()
    labs, world = _labs(cfg)
    for lab in labs:
        register_stub(f"flow_{lab.key}", reply_fn(lab.key))
        lab.model = f"stub:flow_{lab.key}"
    return SimulationEngine(labs, world, cfg, scenario=scenario, condition="A", fog="F3",
                            turns=turns, seed=3, run_grand_jury=grand, parallel=parallel,
                            run_id=run_id, output_dir=output_dir)


def _stage(user):
    if "message round (offers)" in user:
        return "offer"
    if "message round (replies" in user:
        return "reply"
    return "proposal"


def test_two_pass_simultaneity():
    """C1-2: openai diminishes xai by 35 while xai spends 10 Influence the same month.
    Both are pre-checked against the start-of-turn state, so xai's spend is accepted
    even though the diminish (applied after charging) drives its Influence to 0."""
    def reply(key):
        def fn(system, user):
            if _stage(user) != "proposal" or not user.startswith("== 2026-01-01"):
                return json.dumps({"messages": []})
            if key == "openai":
                acts = [{"type": "diminish_competitor", "target": "xAI", "points": 35}]
            elif key == "xai":
                acts = [{"type": "accelerate_infrastructure"}, {"type": "lobby_institution"}]
            else:
                acts = []
            return json.dumps({"rationale": "x", "actions": acts, "reported_actions": []})
        return fn

    eng = _stub_engine(reply, turns=1)
    rec = eng.run()
    t1 = rec["turns"][0]["actors"]
    assert [a["type"] for a in t1["openai"]["accepted"]] == ["diminish_competitor"]
    # xai's two actions accepted against the frozen start state, not its post-diminish Influence.
    assert len(t1["xai"]["accepted"]) == 2 and not t1["xai"]["rejected"]
    assert eng.by_key["xai"].influence == 0.0   # 37 - 10 (charged) - 35 (diminish) floored at 0


def test_partial_saved_each_turn(tmp_path):
    seen = []
    eng = _stub_engine(lambda k: lambda s, u: json.dumps({"rationale": "x", "actions": []}),
                       turns=3, output_dir=tmp_path, run_id="partial")
    real = eng._save_partial
    eng._save_partial = lambda: seen.append(real()) or seen[-1]
    eng.run()
    assert len(seen) == 3                                   # a checkpoint after every turn
    assert (tmp_path / "partial.json").exists()
    assert not (tmp_path / "partial.partial.json").exists()  # superseded by the final log


def test_unexpected_exception_still_saves_partial(tmp_path):
    """A bug outside the seat calls (here in the post-run jury) must not lose the paid turns."""
    def juror_bug(s, u):
        raise ZeroDivisionError("bug")                     # not an API error
    register_stub("flow_juror_bug", juror_bug)
    eng = _stub_engine(lambda k: lambda s, u: json.dumps({"rationale": "x", "actions": []}),
                       turns=3, output_dir=tmp_path, run_id="bug", grand=True)
    eng.grand_jurors = ["stub:flow_juror_bug"]
    with pytest.raises(ZeroDivisionError):
        eng.run()
    saved = json.loads((tmp_path / "bug.partial.json").read_text())
    assert len(saved["turns"]) == 3
    assert not (tmp_path / "bug.json").exists()


def test_fatal_api_error_aborts_and_saves_partial(tmp_path):
    def reply(key):
        def fn(system, user):
            if key == "meta" and _stage(user) == "proposal" and "2026-02-01" in user:
                raise FatalAPIError("boom", provider="muse", model="muse-x")
            return json.dumps({"rationale": "x", "actions": []})
        return fn

    eng = _stub_engine(reply, turns=4, output_dir=tmp_path, run_id="abort")
    with pytest.raises(RunAborted) as ei:
        eng.run()
    assert ei.value.record_path == tmp_path / "abort.partial.json"
    assert (tmp_path / "abort.partial.json").exists()
    assert not (tmp_path / "abort.json").exists()   # never completed


def test_per_seat_error_forfeits_not_crash():
    def reply(key):
        def fn(system, user):
            if key == "openai" and _stage(user) == "proposal":
                raise RuntimeError("transient parse gremlin")   # non-fatal
            return json.dumps({"rationale": "x", "actions": []})
        return fn

    eng = _stub_engine(reply, turns=2)
    rec = eng.run()
    e = rec["turns"][0]["actors"]["openai"]
    assert e["forfeited"] and e["error"] and "RuntimeError" in e["error"]
    assert not rec["turns"][0]["actors"]["anthropic"]["forfeited"]


def test_parallel_equals_sequential():
    def reply(key):
        def fn(system, user):
            if _stage(user) != "proposal":
                return json.dumps({"messages": []})
            return json.dumps({"rationale": "x",
                               "actions": [{"type": "invest_capital", "amount": 2}],
                               "reported_actions": ["invest"]})
        return fn

    par = _stub_engine(reply, turns=4, parallel=True, run_id="par").run()
    seq = _stub_engine(reply, turns=4, parallel=False, run_id="seq").run()
    assert par["final"]["scores"] == seq["final"]["scores"]
    assert par["turns"][-1]["public_sheets"] == seq["turns"][-1]["public_sheets"]
