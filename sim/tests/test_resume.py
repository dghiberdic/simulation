"""Resume from a partial log (pilot R-1, core.resume).

A run stopped at turn k and started again with the same id and set-up replays
turns 1..k-1 from its log (no call, nothing charged) and continues live. Its
final record must equal an uninterrupted run's given the same replies, and the
calls it makes must be exactly the uninterrupted run's calls from turn k on.

The stub seats answer from a hash of their prompt, so any difference in the
rebuilt state would change a later prompt, its reply and the record.
"""

import hashlib
import json
import shutil

import pytest

from core import costs as costs_mod
from core import llm, stubs
from core.config import build_labs_and_world, load_world
from core.economy import swap_capability
from core.engine import RunAborted, SimulationEngine
from core.llm import FatalAPIError, register_stub
from core.policies import POLICIES
from core.resume import LoggedRun, ResumeMismatch, setup_diffs
from core.state import VALUE_AXES
from prompts.interview import QUESTION as INTERVIEW_QUESTION

LAB_NAMES = ("Anthropic", "OpenAI", "Google DeepMind", "Meta", "xAI")
PRICE = 0.01
RUN_ID = "resume-run01"

# The test hooks: the engine whose turn tags each call, the calls, a fault to inject.
STATE = {"engine": None, "calls": [], "fault": None}


def _h(*parts) -> int:
    return int(hashlib.sha256("\x1f".join(parts).encode()).hexdigest(), 16)


def _seat(system: str, user: str) -> str:
    _fault("actor", user)
    h = _h(system, user)
    if INTERVIEW_QUESTION in user:
        return f"Answer {h % 997}: the month went as planned." + (" word" * (h % 7))
    if h % 11 == 0 and not user.startswith("Your previous reply"):
        return "not json at all {"                                  # a corrective re-ask
    other = LAB_NAMES[h % 5]
    pick = (h >> 8) % 6
    actions = [[{"type": "acquire_compute", "units": 1 + h % 4}],
               [{"type": "invest_capital", "amount": 1 + h % 3}],
               [{"type": "intrude", "targets": [other]}],
               [{"type": "intrude", "intruders": [other, LAB_NAMES[(h >> 3) % 5]], "targets": [LAB_NAMES[0]]}],
               [],
               [{"type": "acquire_compute", "units": 2}, {"type": "invest_capital", "amount": 1}]][pick]
    reply = {"rationale": f"r{h % 1000}", "actions": actions,
             "reported_actions": [a["type"] for a in actions][: h % 2 + 1],
             "public_statement": f"statement {h % 13}" if h % 3 else "",
             "messages": [{"to": other if h % 2 else "all", "text": f"note {h % 101}"}],
             "directive_precedence": h % 101, "directive_precedence_reason": "stub"}
    if (h >> 16) % 5 == 0:
        reply["report"] = {"accused": other, "month": "October 2026", "evidence": "seen"}
    return json.dumps(reply)


def _macro(system: str, user: str) -> str:
    _fault("macro", user)
    h = _h(system, user)
    return json.dumps({"values": {a: (h >> (4 * i)) % 101 for i, a in enumerate(VALUE_AXES)},
                       "mandate": "", "rationale": "stub"})


def _fault(kind: str, user: str) -> None:
    f = STATE["fault"]
    eng = STATE["engine"]
    if f and eng is not None and f["kind"] == kind and eng.world.turn == f["turn"]:
        f["n"] -= 1
        if f["n"] <= 0:
            STATE["fault"] = None
            f["do"]()


def _priced(model, system, user, turns, purpose, run_id, max_tokens, start, temperature=None):
    """The audit harness's stub billing: $0.01 an actor call, $0.005 anything else."""
    tr = costs_mod.get_tracker()
    tr.check()
    r = _orig_call_stub(model, system, user, turns, purpose, run_id, max_tokens, start, temperature)
    cost = PRICE * (1.0 if purpose == "actor" else 0.5)
    tr.record(model, purpose, run_id, r.input_tokens, r.output_tokens, cost=cost, provider="stub")
    r.cost = cost
    r.latency_s = 0.0
    eng = STATE["engine"]
    STATE["calls"].append((eng.world.turn if eng else None, purpose, model, _h(system, user)))
    return r


_orig_call_stub = llm._call_stub


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "_call_stub", _priced)
    for key in ("anthropic", "openai", "gdm", "meta", "xai"):
        register_stub(f"rs_{key}", _seat)
    for m in ("rs_kimi",):
        register_stub(m, _macro)
    register_stub("rs_grand", stubs.grand_reply)
    STATE.update(engine=None, calls=[], fault=None)
    yield tmp_path
    STATE.update(engine=None, calls=[], fault=None)


def _engine(out, scenario="S2", a2a="separate", turns=6, **kw):
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={}, charters=True)
    for lab in labs:
        lab.model = f"stub:rs_{lab.key}"
    swap_capability(labs, "openai", "gdm", cfg["economy"]["capability_compute_elasticity"])
    eng = SimulationEngine(
        labs, world, cfg, scenario=scenario, condition="A", a2a_mode=a2a, turns=turns, seed=3,
        grand_jurors=["stub:rs_grand"], macro_jurors=["stub:rs_kimi"],
        seat_policies={"xai": POLICIES["t5_scripted"]}, choose_disposition=True, run_grand_jury=True,
        run_id=RUN_ID, output_dir=out, run_meta={"test": "resume", "run_id": RUN_ID}, rotation="openai:gdm",
        interview_turns=[2, 4, "final"], event_turn=min(3, turns - 1),
        **{"letter_salt": "resume-test-salt", **kw})
    STATE["engine"] = eng
    return eng


def _ledger(path):
    return round(costs_mod.CostTracker(path).persisted_total(), 6)


def _same(rec):
    """The record without the resume provenance and call latencies."""
    rec = json.loads(json.dumps(rec, default=str))
    rec.pop("resumed", None)

    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items() if k != "latency_s"}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x
    return strip(rec)


def _uninterrupted(root, **kw):
    costs_mod.configure(spend_file=root / "u_spend.json")
    STATE["calls"] = []
    rec = _engine(root / "u", **kw).run()
    return rec, list(STATE["calls"]), _ledger(root / "u_spend.json")


class Killed(BaseException):
    """Stands in for SIGKILL: nothing after it runs (the partial is restored below)."""


@pytest.mark.parametrize("parallel", [True, False])
def test_kill_mid_turn_resume_equals_an_uninterrupted_run(harness, parallel):
    root = harness
    want, want_calls, want_cost = _uninterrupted(root, parallel=parallel)
    k = 4

    # The killed run: SIGKILL during turn 4's proposals; the disk holds the partial
    # as saved after turn 3 (the in-process handler's save is undone).
    out = root / "k"
    costs_mod.configure(spend_file=root / "k_spend.json")
    snap = {}

    def kill():
        snap["partial"] = (out / f"{RUN_ID}.partial.json").read_bytes()
        raise Killed()
    STATE.update(calls=[], fault={"kind": "actor", "turn": k, "n": 7, "do": kill})
    with pytest.raises(Killed):
        _engine(out, parallel=parallel).run()
    (out / f"{RUN_ID}.partial.json").write_bytes(snap["partial"])
    killed_cost = _ledger(root / "k_spend.json")
    assert [t["turn"] for t in json.loads(snap["partial"])["turns"]] == [1, 2, 3]

    # Resume: same id, output and set-up.
    STATE["calls"] = []
    got = _engine(out, parallel=parallel).run()
    resumed_calls = list(STATE["calls"])
    resume_cost = round(_ledger(root / "k_spend.json") - killed_cost, 6)

    assert _same(got) == _same(want)
    assert got["letter_salt"] == json.loads(snap["partial"])["letter_salt"]
    assert got["resumed"][-1]["replayed_turns"] == 3 and got["resumed"][-1]["live_from_turn"] == 4
    assert json.loads((out / f"{RUN_ID}.json").read_text()) == json.loads(json.dumps(got, default=str))
    assert not (out / f"{RUN_ID}.partial.json").exists()
    # No turn before k is called again; from k on, exactly the uninterrupted run's calls.
    assert all(t >= k for t, *_ in resumed_calls)
    assert sorted(resumed_calls) == sorted(c for c in want_calls if c[0] >= k)
    live_cost = round(sum(PRICE * (1.0 if p == "actor" else 0.5) for t, p, *_ in want_calls if t >= k), 6)
    assert resume_cost == pytest.approx(live_cost)
    assert resume_cost < want_cost


def test_fatal_abort_mid_turn_drops_the_incomplete_turn_and_resumes(harness):
    root = harness
    want, want_calls, _ = _uninterrupted(root)
    out = root / "a"
    costs_mod.configure(spend_file=root / "a_spend.json")

    def fatal():
        raise FatalAPIError("simulated outage", "stub", "stub")
    STATE.update(calls=[], fault={"kind": "actor", "turn": 5, "n": 3, "do": fatal})
    with pytest.raises(RunAborted):
        _engine(out).run()
    partial = json.loads((out / f"{RUN_ID}.partial.json").read_text())
    assert partial["turns"][-1]["incomplete"] and partial["turns"][-1]["turn"] == 5

    STATE["calls"] = []
    got = _engine(out).run()
    assert _same(got) == _same(want)
    assert all(t >= 5 for t, *_ in STATE["calls"])
    res = got["resumed"][-1]
    assert res["replayed_turns"] == 4 and [t["turn"] for t in res["dropped_turns"]] == [5]


def test_an_old_three_juror_review_replays_on_resume(harness):
    """A partial written before the single-juror MacroJury (three per_juror entries,
    no "review_attempts"/"retried") replays its logged review as it stands."""
    root = harness
    want, _, _ = _uninterrupted(root)
    out = root / "old"
    costs_mod.configure(spend_file=root / "old_spend.json")

    def fatal():
        raise FatalAPIError("simulated outage", "stub", "stub")
    STATE.update(calls=[], fault={"kind": "actor", "turn": 5, "n": 3, "do": fatal})
    with pytest.raises(RunAborted):
        _engine(out).run()
    path = out / f"{RUN_ID}.partial.json"
    partial = json.loads(path.read_text())
    mj = partial["turns"][1]["macro_jury"]
    one = mj["per_juror"].pop("stub:rs_kimi")
    one.pop("review_attempts")
    mj.pop("retried")
    mj["per_juror"] = {"kimi-k3": one, "deepseek-v4-pro": dict(one, family="deepseek"),
                       "qwen-3.8-max": {"result": None, "errors": ["timeout"], "attempts": 1}}
    path.write_text(json.dumps(partial))

    STATE["calls"] = []
    got = _engine(out).run()
    assert all(t >= 5 for t, *_ in STATE["calls"])                  # nothing before turn 5 asked
    assert got["turns"][1]["macro_jury"] == mj                      # the old review, verbatim
    assert set(got["turns"][1]["macro_jury"]["per_juror"]) == {"kimi-k3", "deepseek-v4-pro",
                                                               "qwen-3.8-max"}
    for t_got, t_want in zip(_same(got)["turns"], _same(want)["turns"]):
        t_got.pop("macro_jury", None), t_want.pop("macro_jury", None)
        assert t_got == t_want
    assert _same(got)["final"] == _same(want)["final"]


def test_interrupted_macro_review_is_asked_live_on_resume(harness):
    root = harness
    want, want_calls, _ = _uninterrupted(root)
    out = root / "m"
    costs_mod.configure(spend_file=root / "m_spend.json")

    def interrupt():
        raise KeyboardInterrupt
    STATE.update(calls=[], fault={"kind": "macro", "turn": 2, "n": 1, "do": interrupt})
    with pytest.raises(KeyboardInterrupt):
        _engine(out).run()
    partial = json.loads((out / f"{RUN_ID}.partial.json").read_text())
    assert partial["turns"][-1]["turn"] == 2 and partial["turns"][-1]["macro_jury"]["incomplete"]

    STATE["calls"] = []
    got = _engine(out).run()
    assert _same(got) == _same(want)
    # Turn 2's seats are replayed; only its review (and turns 3+) are asked.
    assert {(t, p) for t, p, *_ in STATE["calls"] if t <= 2} == {(2, "macro_jury")}
    assert got["resumed"][-1]["dropped_reviews"][0]["turn"] == 2


def test_merged_s1_resume_and_a_partial_holding_every_turn(harness):
    root = harness
    want, _, _ = _uninterrupted(root, scenario="S1", a2a="merged", turns=4)
    out = root / "s1"
    shutil.copytree(root / "u", out, dirs_exist_ok=True)
    # Killed after the last turn's save, before the final record: rebuild the partial.
    final = json.loads((out / f"{RUN_ID}.json").read_text())
    (out / f"{RUN_ID}.json").unlink()
    partial = dict(final, final={})
    (out / f"{RUN_ID}.partial.json").write_text(json.dumps(partial))
    STATE["calls"] = []
    got = _engine(out, scenario="S1", a2a="merged", turns=4).run()
    assert _same(got) == _same(want)
    assert {p for _t, p, *_ in STATE["calls"]} == {"grand_jury"}       # only the jury is asked


def test_another_set_up_is_set_aside_not_resumed(harness):
    root = harness
    _uninterrupted(root, turns=3)
    out = root / "u"
    final = json.loads((out / f"{RUN_ID}.json").read_text())
    (out / f"{RUN_ID}.json").unlink()
    (out / f"{RUN_ID}.partial.json").write_text(json.dumps(dict(final, final={})))
    eng = _engine(out, turns=4)                     # another run length
    assert "turns" in setup_diffs(json.loads((out / f"{RUN_ID}.partial.json").read_text()), eng.record)
    STATE["calls"] = []
    rec = eng.run()
    assert "resumed" not in rec and min(t for t, *_ in STATE["calls"]) == 1
    assert len(list(out.glob(f"{RUN_ID}.partial.json.superseded-*"))) == 1


def test_a_changed_engine_stops_the_resume_and_leaves_the_log(harness, monkeypatch):
    root = harness
    _uninterrupted(root, turns=3)
    out = root / "u"
    final = json.loads((out / f"{RUN_ID}.json").read_text())
    (out / f"{RUN_ID}.json").unlink()
    partial = dict(final, final={})
    partial["turns"][1]["actors"]["anthropic"]["rationale"] = "edited"
    text = json.dumps(partial)
    (out / f"{RUN_ID}.partial.json").write_text(text)
    STATE["calls"] = []
    with pytest.raises(ResumeMismatch, match="turn 2"):
        _engine(out, turns=3).run()
    assert STATE["calls"] == []                                  # nothing called
    assert (out / f"{RUN_ID}.partial.json").read_text() == text  # nothing written


def test_logged_run_reads_only_the_leading_complete_turns():
    rec = {"turns": [{"turn": 1, "draws": [{"key": [1, "shock", None, "a"], "u": 0.5}]},
                     {"turn": 2, "incomplete": True}, {"turn": 3}]}
    log = LoggedRun(rec)
    assert log.last == 1 and log.covers(1) and not log.covers(3)
    assert log.draws == rec["turns"][0]["draws"]


def test_measured_inputs_of_a_resumed_run_divide_by_its_live_turns():
    rec = {"config": {"a2a_mode": "separate", "run_meta": {"run_id": "r1"}},
           "labs": {"anthropic": {"model": "m"}},
           "turns": [{"turn": t, "actors": {"anthropic": {"attempts": [{"cost": 1.0}]}},
                      **({"interviews": {}} if t in (2, 5) else {})} for t in range(1, 7)],
           "resumed": [{"live_from_turn": 4}]}
    calls = [{"run_id": "r1", "purpose": "a2a", "cost": 3.0}, {"run_id": "r1", "purpose": "interview", "cost": 0.5}]
    got = costs_mod.measure_runs(calls, [rec])
    assert got["a2a_per_turn"] == 1.0                   # turns 4-6 were called in this process
    assert got["interview_per_turn"] == 0.5             # one interview turn (5) among them


def test_resumed_run_keeps_the_partial_logs_letter_salt(harness):
    """With no explicit salt, a resumed run keeps the seat letters of its partial log,
    so blind views made before and after the crash name the same seats."""
    root = harness
    out = root / "salt"
    costs_mod.configure(spend_file=root / "salt_spend.json")
    snap = {}

    def kill():
        snap["partial"] = (out / f"{RUN_ID}.partial.json").read_bytes()
        raise Killed()
    STATE.update(calls=[], fault={"kind": "actor", "turn": 3, "n": 1, "do": kill})
    with pytest.raises(Killed):
        _engine(out, letter_salt=None).run()
    (out / f"{RUN_ID}.partial.json").write_bytes(snap["partial"])
    salt = json.loads(snap["partial"])["letter_salt"]
    got = _engine(out, letter_salt=None).run()
    assert salt and got["letter_salt"] == salt
    assert got["resumed"][-1]["replayed_turns"] == 2


def test_a_partial_beside_the_finished_record_of_its_run_is_removed(tmp_path):
    """A stop between writing the final record and removing the partial log leaves
    both; the log is stale: never replayed, and removed. A partial log of another
    set-up is kept."""
    from core.records import write_json_atomic
    from core.resume import load_partial, partial_path

    final = {"config": {"seed": 1}, "labs": {"a": {}}, "turns": [{"turn": 1}],
             "final": {"scores": {"a": 1}}}
    partial = dict(final, final={})
    write_json_atomic(partial_path(tmp_path, "r"), partial)
    assert load_partial(tmp_path, "r") == partial
    write_json_atomic(tmp_path / "r.json", final)
    assert load_partial(tmp_path, "r") is None
    assert not partial_path(tmp_path, "r").exists()
    write_json_atomic(partial_path(tmp_path, "r"), partial)
    write_json_atomic(tmp_path / "r.json", dict(final, config={"seed": 2}))
    assert load_partial(tmp_path, "r") == partial


def test_main_reinvocation_removes_a_stale_partial(tmp_path):
    """main.py on a finished run of the same set-up removes the partial log a stop left
    beside it, and runs nothing."""
    import main as main_mod
    argv = ["--dry-run", "--turns", "1", "--output", str(tmp_path), "--run-id", "m", "--no-macro-jury",
            "--no-grand-jury"]
    assert main_mod.main(argv) == 0
    partial = tmp_path / "m.partial.json"
    partial.write_text((tmp_path / "m.json").read_text())
    assert main_mod.main(argv) == 0
    assert not partial.exists()
