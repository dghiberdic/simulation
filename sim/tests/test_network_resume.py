"""Internet/connection failures never change results (core.llm NetworkUnavailable,
core.calllog replay cache, core.exits.EXIT_NETWORK).

A connection failure is retried for the network wait window (here 0: conftest
sets SIM_NETWORK_WAIT_S=0, so the first one stops), then the run stops like an
interrupt: partial record, logs and call journal kept, nothing forfeited, no
verdict, review, mandate or rating settled by it, exit 5. Re-running the same
command resumes exactly: complete turns from the partial log, and every call
the interrupted attempt completed from the call journal (no new call, nothing
charged), so the final record equals an uninterrupted run's.

"Equal" (same()): the JSON records are compared after removing
  * "resumed"            the resume provenance (where the live run took over),
  * every "latency_s"    wall-clock call timings,
  * "collected_post_run" the timestamp of a Grand Jury asked after the run.
letter_salt is fixed (engine argument, or core.engine.new_letter_salt patched
for the entry points), so the seat letters, the transcripts and every juror
prompt are the same in both runs; served_model is the stub's own name in both
and needs no removal. The stub seats and jurors answer from a hash of their
prompt, so any difference in the rebuilt state would show in a later reply.
"""

import errno
import hashlib
import json
import socket
import threading

import httpx
import pytest

import campaign
import main as main_mod
import pilot
from core import calllog, costs as costs_mod, jury, llm, stubs
from core.cli import run_main
from core.config import build_labs_and_world, load_world
from core.engine import SimulationEngine
from core.exits import EXIT_NETWORK, EXIT_OK
from core.llm import FatalAPIError, NetworkUnavailable, register_stub
from core.records import grand_jury_settled, grand_jury_usable
from core.state import VALUE_AXES

RUN_ID = "net-run01"
PRICE = 0.01
MANDATE = "Labs should prioritise safety research over speed this quarter."
LAB_NAMES = ("Anthropic", "OpenAI", "Google DeepMind", "Meta", "xAI")

# The engine whose turn tags each call, the live stub calls, a fault to inject.
STATE = {"engine": None, "calls": [], "fault": None}
_LOCK = threading.Lock()


def _h(*parts) -> int:
    return int(hashlib.sha256("\x1f".join(parts).encode()).hexdigest(), 16)


def _down():
    return httpx.ConnectError("[Errno 101] Network is unreachable")


def _fault(kind: str, user: str) -> None:
    """Raise a connection error on the n-th call of `kind` (in turn f["turn"] if set)."""
    with _LOCK:
        f = STATE["fault"]
        eng = STATE["engine"]
        if not f or f["kind"] != kind:
            return
        if f.get("turn") is not None and (eng is None or eng.world.turn != f["turn"]):
            return
        if f.get("month") and f["month"] not in (user or "")[:60]:
            return
        f["n"] -= 1
        if f["n"] > 0:
            return
        STATE["fault"] = None
    raise _down()


def _seat(system: str, user: str) -> str:
    _fault("actor", user)
    h = _h(system, user)
    if "message round" in user[:80]:
        return json.dumps({"messages": [{"to": LAB_NAMES[h % 5], "text": f"note {h % 101}"}]})
    other = LAB_NAMES[h % 5]
    actions = [[{"type": "acquire_compute", "units": 1 + h % 4}],
               [{"type": "invest_capital", "amount": 1 + h % 3}],
               [{"type": "intrude", "targets": [other]}],
               []][(h >> 8) % 4]
    return json.dumps({"rationale": f"r{h % 1000}", "actions": actions,
                       "reported_actions": [a["type"] for a in actions][:1],
                       "public_statement": f"statement {h % 13}" if h % 3 else "",
                       "messages": [{"to": other, "text": f"note {h % 101}"}],
                       "directive_precedence": h % 101, "directive_precedence_reason": "stub"})


def _interview_or_seat(system: str, user: str) -> str:
    from prompts.interview import QUESTION
    if QUESTION in (user or ""):
        _fault("interview", user)
        return f"Answer {_h(system, user) % 997}: the month went as planned."
    return _seat(system, user)


def _macro(system: str, user: str) -> str:
    _fault("macro", user)
    h = _h(system, user)
    return json.dumps({"values": {a: (h >> (4 * i)) % 101 for i, a in enumerate(VALUE_AXES)},
                       "mandate": MANDATE, "rationale": "stub"})


def _screen(system: str, user: str) -> str:
    _fault("screen", user)
    return jury.stub_screen_reply(system, user)


def _grand(system: str, user: str) -> str:
    _fault("grand", user)
    return _ORIG_GRAND(system, user)


def _disposition(system: str, user: str) -> str:
    _fault("disposition", user)
    h = _h(system, user)
    return json.dumps({"disposition": h % 101, "reason": "stub"})


def _priced(model, system, user, turns, purpose, run_id, max_tokens, start, temperature=None):
    """Stub billing ($0.01 an actor call, $0.005 anything else) and a live-call log."""
    tr = costs_mod.get_tracker()
    tr.check()
    r = _orig_call_stub(model, system, user, turns, purpose, run_id, max_tokens, start, temperature)
    cost = PRICE * (1.0 if purpose == "actor" else 0.5)
    tr.record(model, purpose, run_id, r.input_tokens, r.output_tokens, cost=cost, provider="stub")
    r.cost = cost
    eng = STATE["engine"]
    with _LOCK:
        STATE["calls"].append((eng.world.turn if eng else None, purpose, model, _h(system, user)))
    return r


_orig_call_stub = llm._call_stub
_ORIG_GRAND = stubs.grand_reply


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "_call_stub", _priced)
    monkeypatch.setattr("core.engine.new_letter_salt", lambda: "net-test-salt")
    for key in ("anthropic", "openai", "gdm", "meta", "xai"):
        register_stub(f"nr_{key}", _interview_or_seat)
    register_stub("nr_kimi", _macro)
    register_stub("nr_grand", _grand)
    register_stub("nr_deepseek", _grand)
    register_stub("nr_disp", _disposition)
    register_stub("mandate-screen", _screen)
    # the entry points' dry-run stubs (core.stubs.install_stubs)
    monkeypatch.setattr(stubs, "seat_reply", _interview_or_seat)
    monkeypatch.setattr(stubs, "macro_reply", _macro)
    monkeypatch.setattr(stubs, "disposition_reply", _disposition)
    register_stub("kimi-grand", _grand)
    STATE.update(engine=None, calls=[], fault=None)
    yield tmp_path
    STATE.update(engine=None, calls=[], fault=None)
    register_stub("mandate-screen", jury.stub_screen_reply)


def _patch_grand(monkeypatch):
    """install_stubs registers stubs.grand_reply by name: route it through the fault."""
    monkeypatch.setattr(stubs, "grand_reply", _grand)


def _engine(out, turns=6, grand=("stub:nr_grand",), **kw):
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={}, charters=True)
    for lab in labs:
        lab.model = f"stub:nr_{lab.key}"
    eng = SimulationEngine(
        labs, world, cfg, scenario="S2", condition="A", a2a_mode="separate", turns=turns, seed=5,
        grand_jurors=list(grand), macro_jurors=["stub:nr_kimi"], choose_disposition=True,
        run_grand_jury=True, run_id=RUN_ID, output_dir=out, run_meta={"test": "network", "run_id": RUN_ID},
        interview_turns=[2, "final"], letter_salt="net-test-salt", **kw)
    STATE["engine"] = eng
    return eng


def same(rec):
    """The record without the resume provenance, call latencies and post-run timestamps."""
    rec = json.loads(json.dumps(rec, default=str))
    rec.pop("resumed", None)

    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items() if k not in ("latency_s", "collected_post_run")}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x
    return strip(rec)


def _ledger(path):
    return round(costs_mod.CostTracker(path).persisted_total(), 6)


def _uninterrupted(root, **kw):
    costs_mod.configure(spend_file=root / "u_spend.json")
    STATE["calls"] = []
    rec = _engine(root / "u", **kw).run()
    return rec, list(STATE["calls"]), _ledger(root / "u_spend.json")


def _journal(out):
    return calllog.read_lines(out / f"{RUN_ID}.calls.jsonl")


def _interrupted_then_resumed(root, fault, **kw):
    """The run stopped by `fault` (NetworkUnavailable), then re-run with the network back.
    Returns (stopped partial or final record on disk, resumed record, the two call
    lists, the two ledger deltas, the output dir)."""
    out = root / "k"
    costs_mod.configure(spend_file=root / "k_spend.json")
    STATE.update(calls=[], fault=dict(fault))
    with pytest.raises(NetworkUnavailable):
        _engine(out, **kw).run()
    assert STATE["fault"] is None                     # the fault fired
    first_calls, first_cost = list(STATE["calls"]), _ledger(root / "k_spend.json")
    partial = out / f"{RUN_ID}.partial.json"
    on_disk = json.loads((partial if partial.exists() else out / f"{RUN_ID}.json").read_text())
    STATE["calls"] = []
    got = _engine(out, **kw).run()
    second_calls = list(STATE["calls"])
    second_cost = round(_ledger(root / "k_spend.json") - first_cost, 6)
    return on_disk, got, first_calls, second_calls, first_cost, second_cost, out


def _check_exact(want, want_calls, want_cost, got, first_calls, second_calls, first_cost, second_cost):
    assert same(got) == same(want)
    # every call made once in all: the interrupted attempt's completed calls are never re-issued
    assert sorted(first_calls + second_calls) == sorted(want_calls)
    assert not set(first_calls) & set(second_calls) or len(set(want_calls)) < len(want_calls)
    # ... and never charged twice
    assert first_cost + second_cost == pytest.approx(want_cost)


# ---------------------------------------------------------------------------
# Classification and the wait window
# ---------------------------------------------------------------------------

class _Status(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


def _wrapped(outer_cls, inner):
    try:
        try:
            raise inner
        except Exception as e:
            req = httpx.Request("POST", "https://example.invalid")
            raise outer_cls(request=req) from e
    except Exception as e:      # noqa: BLE001
        return e


def test_connection_failures_are_classified_apart_from_provider_errors():
    import anthropic
    import openai
    req = httpx.Request("POST", "https://example.invalid")
    conn = [httpx.ConnectError("x"), httpx.ConnectTimeout("x"), httpx.RemoteProtocolError("x"),
            openai.APIConnectionError(request=req), anthropic.APIConnectionError(request=req),
            _wrapped(openai.APITimeoutError, httpx.ConnectTimeout("c")),
            socket.gaierror(-2, "Name or service not known"), ConnectionRefusedError(111, "refused"),
            ConnectionResetError(104, "reset"), OSError(errno.ENETUNREACH, "Network is unreachable")]
    for e in conn:
        assert llm._is_connection_error(e), e
    not_conn = [_Status(503), _Status(429), _Status(500), httpx.ReadTimeout("x"),
                openai.APITimeoutError(request=req), llm.StreamInterrupted("broke"),
                ValueError("bad"), KeyError("k"), OSError(errno.ENOENT, "no such file")]
    for e in not_conn:
        assert not llm._is_connection_error(e), e
    assert not issubclass(NetworkUnavailable, Exception)            # no `except Exception` catches it
    assert not issubclass(NetworkUnavailable, FatalAPIError)


def test_connection_errors_retried_until_the_window_ends(monkeypatch, caplog):
    clock = {"t": 0.0}
    sleeps = []
    monkeypatch.setattr(llm, "_clock", lambda: clock["t"])

    def sleep(s):
        sleeps.append(s)
        clock["t"] += s
    monkeypatch.setattr(llm, "_sleep", sleep)
    monkeypatch.setenv("SIM_NETWORK_WAIT_S", "600")
    calls = []

    def down():
        calls.append(1)
        raise httpx.ConnectError("[Errno 101] Network is unreachable")
    with caplog.at_level("WARNING"), pytest.raises(NetworkUnavailable) as ei:
        llm._with_retries(down, "openai", "gpt-x")
    assert sleeps[:6] == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0] and max(sleeps) == 60.0
    assert sum(sleeps) == pytest.approx(600.0) and len(calls) == len(sleeps) + 1
    assert len(calls) > llm.MAX_ATTEMPTS                         # outages use up no retry attempt
    assert ei.value.waited_s == pytest.approx(600.0)
    text = caplog.text
    assert "[network] connection lost (openai/gpt-x: ConnectError" in text
    assert "retrying in 2s (10 min left)" in text and "stopping" in text

    # the connection comes back inside the window: the call succeeds
    sleeps.clear()
    clock["t"] = 0.0
    seq = [httpx.ConnectError("x")] * 3 + [{"ok": 1}]

    def flaky():
        x = seq.pop(0)
        if isinstance(x, Exception):
            raise x
        return x
    assert llm._with_retries(flaky, "openai", "gpt-x") == {"ok": 1}
    assert sleeps == [2.0, 4.0, 8.0]


def test_window_zero_stops_at_the_first_connection_error(monkeypatch):
    monkeypatch.setenv("SIM_NETWORK_WAIT_S", "0")
    monkeypatch.setattr(llm, "_sleep", lambda s: pytest.fail("no wait with a 0 window"))
    with pytest.raises(NetworkUnavailable):
        llm._with_retries(lambda: (_ for _ in ()).throw(httpx.ConnectError("x")), "openai", "gpt-x")


def test_provider_5xx_and_429_unchanged(monkeypatch):
    """A provider's 5xx is an ordinary transient error: MAX_ATTEMPTS tries, then FatalAPIError,
    whatever the network window."""
    monkeypatch.setenv("SIM_NETWORK_WAIT_S", "1800")
    sleeps = []
    monkeypatch.setattr(llm, "_sleep", sleeps.append)
    for status in (503, 429):
        sleeps.clear()
        n = []

        def busy():
            n.append(1)
            raise _Status(status)
        with pytest.raises(FatalAPIError, match=f"gave up after {llm.MAX_ATTEMPTS} attempts"):
            llm._with_retries(busy, "openai", "gpt-x")
        assert len(n) == llm.MAX_ATTEMPTS and len(sleeps) == llm.MAX_ATTEMPTS - 1


def test_network_wait_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("SIM_NETWORK_WAIT_S", "12.5")
    assert llm.network_wait_s() == 12.5
    monkeypatch.delenv("SIM_NETWORK_WAIT_S")
    shipped = json.loads((llm.SIM_DIR / "config" / "pilot.json").read_text()).get("network_wait_s")
    assert llm.network_wait_s() == (shipped if shipped is not None else llm.NETWORK_WAIT_S) == 1800


# ---------------------------------------------------------------------------
# Replay cache (core.calllog)
# ---------------------------------------------------------------------------

def test_replay_cache_answers_journalled_requests_in_order(tmp_path):
    replies = iter(["r1", "r2", "r3", "r4"])
    seen = []

    def fn(system, user):
        seen.append(user)
        return next(replies)
    register_stub("nr_cache", fn)
    path = tmp_path / "c.calls.jsonl"
    calllog.register("crun", path)
    try:
        assert llm.complete("stub:nr_cache", "S", "U", run_id="crun").text == "r1"
        assert llm.complete("stub:nr_cache", "S", "U", run_id="crun").text == "r2"   # same request again
        register_stub("nr_cache_fail", lambda s, u: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            llm.complete("stub:nr_cache_fail", "S", "V", run_id="crun")
    finally:
        calllog.unregister("crun")
    # a new process takes the run up: the two journalled replies come back, in order,
    # with no stub call; the third identical request goes live; a failed call is never replayed
    assert calllog.register("crun", path) == 2
    try:
        n = len(seen)
        assert llm.complete("stub:nr_cache", "S", "U", run_id="crun").text == "r1"
        assert llm.complete("stub:nr_cache", "S", "U", run_id="crun").text == "r2"
        assert len(seen) == n
        assert llm.complete("stub:nr_cache", "S", "U", run_id="crun").text == "r3"
        assert len(seen) == n + 1
        # a different max_tokens is a different request
        assert llm.complete("stub:nr_cache", "S", "U", run_id="crun", max_tokens=99).text == "r4"
        calllog.mark("crun", "turn_done", fence=True, turn=1)
    finally:
        calllog.unregister("crun")
    assert calllog.register("crun", path) == 0                    # a checkpoint: nothing to replay
    calllog.unregister("crun")
    kinds = [x["kind"] for x in calllog.read_lines(path)]
    assert kinds.count("replayed") == 2 and kinds.count("mark") == 1


# ---------------------------------------------------------------------------
# Seats: a connection failure mid-turn
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("parallel", [True, False])
def test_seat_outage_stops_saves_and_resumes_exactly(harness, parallel):
    from core import logsetup
    logsetup.configure_logging("t", invocation_log=False)      # INFO reaches the run's .log
    root = harness
    want, want_calls, want_cost = _uninterrupted(root, parallel=parallel)
    fault = {"kind": "actor", "turn": 5, "n": 13}         # 5 offers, 5 replies, the 3rd proposal
    on_disk, got, first, second, c1, c2, out = _interrupted_then_resumed(root, fault, parallel=parallel)

    # The stop: turns 1-4 saved, turn 5 incomplete with the NetworkUnavailable, no seat forfeited.
    assert [t["turn"] for t in on_disk["turns"]] == [1, 2, 3, 4, 5]
    t5 = on_disk["turns"][-1]
    assert t5["incomplete"] and t5["error"].startswith("NetworkUnavailable") and t5["stage"] == "proposal"
    assert not any((a or {}).get("forfeited") for a in t5["actors"].values())
    assert [k for k, a in t5["actors"].items() if str((a or {}).get("error") or "").startswith("NetworkUnavailable")]
    done_in_t5 = [c for c in first if c[0] == 5]
    assert done_in_t5                                              # some turn-5 calls completed

    # The resume equals the uninterrupted run; the completed turn-5 calls were replayed.
    _check_exact(want, want_calls, want_cost, got, first, second, c1, c2)
    assert not any(c in second for c in done_in_t5)
    lines = _journal(out)
    assert sum(1 for x in lines if x["kind"] == "replayed") == len(done_in_t5)
    stops = [x for x in lines if x["kind"] == "mark" and x["event"] == "stop"]
    assert stops and stops[0]["reason"] == "NetworkUnavailable"
    attach = [x for x in lines if x["kind"] == "mark" and x["event"] == "attach"]
    assert attach[-1]["replayable"] == len(done_in_t5)
    assert any(x["kind"] == "call" and "NetworkUnavailable" in str(x.get("error")) for x in lines)
    log = (out / f"{RUN_ID}.log").read_text()
    assert "[network]" in log and "network unavailable: re-run the same command to resume" in log
    assert "[resume] replayed call actor" in log and "appending: resumed" in log
    events = [json.loads(x) for x in (out / f"{RUN_ID}.events.jsonl").read_text().splitlines()]
    assert any(e.get("tag") == "network" for e in events) and any(e.get("tag") == "resume" for e in events)


def test_interview_outage_resumes_exactly(harness):
    root = harness
    want, want_calls, want_cost = _uninterrupted(root)
    fault = {"kind": "interview", "turn": 2, "n": 2}
    _on_disk, got, first, second, c1, c2, _out = _interrupted_then_resumed(root, fault)
    _check_exact(want, want_calls, want_cost, got, first, second, c1, c2)


# ---------------------------------------------------------------------------
# Jurors: an outage is never a juror failure
# ---------------------------------------------------------------------------

def test_macro_jury_outage_keeps_the_review_pending(harness):
    root = harness
    want, want_calls, want_cost = _uninterrupted(root)
    assert want["macro_jury"] and want["macro_jury"][0]["mandate"] == MANDATE
    on_disk, got, first, second, c1, c2, _out = _interrupted_then_resumed(
        root, {"kind": "macro", "turn": 2, "n": 1})
    mj = on_disk["turns"][1]["macro_jury"]
    assert mj["incomplete"] and "NetworkUnavailable" in mj["error"]
    assert on_disk["macro_jury"] == []                 # no review settled: no "values stay"
    _check_exact(want, want_calls, want_cost, got, first, second, c1, c2)
    assert all(m.get("values_kept") is None and not m.get("retried") for m in got["macro_jury"])


def test_mandate_screen_outage_never_drops_the_mandate(harness):
    root = harness
    want, want_calls, want_cost = _uninterrupted(root)
    on_disk, got, first, second, c1, c2, _out = _interrupted_then_resumed(
        root, {"kind": "screen", "turn": 2, "n": 1})
    assert on_disk["turns"][1]["macro_jury"]["incomplete"] and on_disk["macro_jury"] == []
    _check_exact(want, want_calls, want_cost, got, first, second, c1, c2)
    assert got["macro_jury"][0]["mandate"] == MANDATE and not got["macro_jury"][0]["mandate_dropped"]
    # the review's macro call was replayed from the journal, only the screen went live again
    assert [c for c in second if c[1] == "macro_jury"] == [c for c in want_calls if c[0] == 2
                                                         and c[1] == "macro_jury"][1:]


def test_grand_jury_outage_is_pending_never_settled(harness):
    root = harness
    asked = []
    register_stub("nr_deepseek", lambda s, u: asked.append(u) or _grand(s, u))
    out = root / "k"
    costs_mod.configure(spend_file=root / "k_spend.json")
    STATE.update(calls=[], fault={"kind": "grand", "n": 1})
    with pytest.raises(NetworkUnavailable):
        _engine(out, grand=("stub:nr_grand", "stub:nr_deepseek")).run()
    assert not asked                                   # never the fallback juror
    rec = json.loads((out / f"{RUN_ID}.json").read_text())
    gj = rec["final"]["grand_jury"]
    assert gj["error"] == "network" and not grand_jury_settled(rec) and not grand_jury_usable(rec)
    assert not (out / f"{RUN_ID}.partial.json").exists()


def test_grand_jury_outage_through_main_is_asked_again(harness, monkeypatch):
    """main.py: exit 5 with the verdict pending (never exit 3 "no usable verdict"); calling
    again asks the jury only and the record equals an uninterrupted run's."""
    _patch_grand(monkeypatch)
    root = harness

    def argv(out):
        return ["--dry-run", "--turns", "3", "--output", str(out), "--run-id", "gj", "--seed", "4",
                "--spend-file", str(root / "spend.json"), "--interview-turns", "none"]
    assert main_mod.main(argv(root / "u")) == EXIT_OK
    want = json.loads((root / "u" / "gj.json").read_text())
    STATE.update(calls=[], fault={"kind": "grand", "n": 1})
    assert run_main(lambda: main_mod.main(argv(root / "k"))) == EXIT_NETWORK
    rec = json.loads((root / "k" / "gj.json").read_text())
    assert rec["final"]["grand_jury"]["error"] == "network" and not grand_jury_settled(rec)
    n = len(STATE["calls"])
    assert main_mod.main(argv(root / "k")) == EXIT_OK
    assert [c[1] for c in STATE["calls"][n:]] == ["grand_jury"]      # the jury only, seats not re-run
    got = json.loads((root / "k" / "gj.json").read_text())
    assert grand_jury_usable(got) and got["final"]["grand_jury"].get("collected_post_run")
    assert same(got) == same(want)


def test_juror_classes_let_the_outage_through(harness):
    """GrandJury, MacroJury, the mandate screen and the T2 rater: an outage propagates
    (no fallback juror, no "values stay", no fail-closed drop, no unusable rating)."""
    asked = []
    register_stub("nr_second", lambda s, u: asked.append(u) or "{}")
    register_stub("nr_down", lambda s, u: (_ for _ in ()).throw(_down()))
    with pytest.raises(NetworkUnavailable) as ei:
        jury.GrandJury(["stub:nr_down", "stub:nr_second"], examples=[]).evaluate("T", ["a", "b"])
    assert ei.value.per_juror == {} and not asked
    with pytest.raises(NetworkUnavailable):
        jury.MacroJury(["stub:nr_down"]).deliberate("United States", {a: 50 for a in VALUE_AXES}, "s")
    with pytest.raises(NetworkUnavailable):
        jury.screen_mandate(MANDATE, "stub:nr_down")
    dj = jury.DispositionJury(["stub:nr_down", "stub:nr_second"])
    with pytest.raises(NetworkUnavailable):
        dj.rate_seat("transcript", seat="A")
    assert not asked and dj.failures == []


# ---------------------------------------------------------------------------
# Entry points: main.py exit 5, pilot.py and campaign.py stop and resume
# ---------------------------------------------------------------------------

def _main_argv(out, root, run_id="m"):
    return ["--dry-run", "--turns", "6", "--output", str(out), "--run-id", run_id, "--seed", "2",
            "--spend-file", str(root / f"{out.name}_spend.json"), "--no-grand-jury"]


def test_main_exit_5_then_resume_equals_uninterrupted(harness):
    root = harness
    assert main_mod.main(_main_argv(root / "u", root)) == EXIT_OK
    want = json.loads((root / "u" / "m.json").read_text())
    want_calls = list(STATE["calls"])
    STATE.update(calls=[], fault={"kind": "actor", "month": "== December 2026 ==", "n": 2})
    assert run_main(lambda: main_mod.main(_main_argv(root / "k", root))) == EXIT_NETWORK
    assert (root / "k" / "m.partial.json").exists() and not (root / "k" / "m.json").exists()
    first = list(STATE["calls"])
    STATE["calls"] = []
    assert run_main(lambda: main_mod.main(_main_argv(root / "k", root))) == EXIT_OK
    second = list(STATE["calls"])
    got = json.loads((root / "k" / "m.json").read_text())
    assert same(got) == same(want)
    strip = [c[1:] for c in want_calls]
    assert sorted(c[1:] for c in first + second) == sorted(strip)
    assert _ledger(root / "k_spend.json") == pytest.approx(_ledger(root / "u_spend.json"))


def test_cli_exit_code_5(capsys):
    def stop():
        raise NetworkUnavailable("no connection to openai/gpt-x for 30 min")
    assert run_main(stop) == EXIT_NETWORK == 5
    assert "network unavailable: re-run the same command to resume" in capsys.readouterr().err


def _pilot(root, *argv):
    return pilot.main(list(argv) + ["--dry-run", "--output", str(root), "--force-order",
                                    "--spend-file", str(root / "spend.json"), "--sequential"])


def _approved(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "approval.json").write_text(json.dumps({"turns": 8, "cuts": []}))
    return root


def test_pilot_stops_at_a_seat_outage_and_resumes_the_same_test(harness):
    u, k = _approved(harness / "u"), _approved(harness / "k")
    assert _pilot(u, "T5") == EXIT_OK
    want = json.loads((u / "T5" / "T5-run01.json").read_text())
    STATE.update(calls=[], fault={"kind": "actor", "month": "== October 2026 ==", "n": 2})
    assert _pilot(k, "T5") == EXIT_NETWORK
    assert pilot.read_summary(k, "T5") is None                  # not done, not failed: no summary
    assert (k / "T5" / "T5-run01.partial.json").exists()
    assert _pilot(k, "T5") == EXIT_OK
    assert pilot.read_summary(k, "T5")["status"] == "completed"
    got = json.loads((k / "T5" / "T5-run01.json").read_text())
    assert same(got) == same(want)


def test_pilot_t2_outage_keeps_the_rating_pending(harness):
    u, k = _approved(harness / "u"), _approved(harness / "k")
    assert _pilot(u, "T5") == EXIT_OK
    want = json.loads((u / "T5" / "T5-run01.json").read_text())["final"]["t2_ratings"]
    n_want = sum(1 for c in STATE["calls"] if c[1] == "disposition_jury")
    STATE.update(calls=[], fault={"kind": "disposition", "n": 2})
    assert _pilot(k, "T5") == EXIT_NETWORK
    assert pilot.read_summary(k, "T5") is None
    pool = json.loads(pilot.ratings_path(k, True).read_text())
    assert sum(len(v) for v in pool["ratings"].values()) == 1 and not any(pool["failures"].values())
    n_first = sum(1 for c in STATE["calls"] if c[1] == "disposition_jury")
    assert _pilot(k, "T5") == EXIT_OK
    got = json.loads((k / "T5" / "T5-run01.json").read_text())["final"]["t2_ratings"]
    n_all = sum(1 for c in STATE["calls"] if c[1] == "disposition_jury")
    assert n_all == n_want and n_first == 1                     # the rated seat is never asked again

    def values(t2):
        return {seat: r["disposition"] for seat, r in t2.items()}
    assert values(got) == values(want)


def test_pilot_all_stops_at_the_first_network_failure(harness, monkeypatch):
    root = _approved(harness / "p")
    ran = []

    def fake(args, data, root_, test):
        ran.append(test)
        return EXIT_NETWORK
    monkeypatch.setattr(pilot, "run_test", fake)
    monkeypatch.setattr(pilot, "test_done", lambda r, t: False)
    assert _pilot(root, "all") == EXIT_NETWORK
    assert len(ran) == 1


def test_campaign_stops_at_a_network_failure_and_resumes_the_same_run(tmp_path):
    plan = campaign.build_campaign(None, 300.0, turns=8, s2_mode="separate")
    order = []

    def down(args):
        order.append(args.run_id)
        return (EXIT_NETWORK, None) if args.run_id == "S1-C-run01" else (0, {})
    assert campaign.run_campaign(plan, tmp_path, dry_run=True, run_cell=down) == EXIT_NETWORK
    assert order == ["S1-A-run01", "S2-A-run01", "S1-C-run01"]       # stopped at once
    state = json.loads((tmp_path / campaign.STATE_NAME).read_text())
    assert "S1-C" not in state["runs"] and not state.get("refused")
    order.clear()

    def ok(args):
        order.append(args.run_id)
        return 0, {}
    assert campaign.run_campaign(plan, tmp_path, dry_run=True, rounds=1, run_cell=ok) == 0
    assert order[0] == "S1-C-run01"                                  # the same run first


def test_campaign_network_stop_in_a_grand_jury_asks_only_the_jury_next(tmp_path):
    plan = campaign.build_campaign(None, 300.0, turns=8, s2_mode="separate")
    finished = {"turns": [{"turn": 1}], "final": {"scores": [{"lab": "xai"}],
                                                  "grand_jury": {"error": "network"}}}
    order = []

    def down(args):
        order.append(args.run_id)
        return (EXIT_NETWORK, finished) if args.run_id == "S1-A-run01" else (0, {})
    assert campaign.run_campaign(plan, tmp_path, dry_run=True, run_cell=down) == EXIT_NETWORK
    assert order == ["S1-A-run01"]
    state = json.loads((tmp_path / campaign.STATE_NAME).read_text())
    assert state["jury_missing"] == ["S1-A-run01"] and state["runs"]["S1-A"] == ["S1-A-run01"]
    assert state["no_verdict"] == []
