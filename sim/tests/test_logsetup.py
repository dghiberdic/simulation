"""Logging that loses nothing (core.logsetup, core.calllog, core.console): the run's
.log / .events.jsonl / .calls.jsonl, the invocation log, plain output off a
terminal, uncaught exceptions, and say()."""

import json
import logging
import sys

import pytest

import main
from core import calllog, console, llm, logsetup


@pytest.fixture
def logdir(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    monkeypatch.setenv("SIM_LOG_DIR", str(d))
    for k in ("SIM_QUIET", "SIM_VERBOSE", "SIM_COLOR", "FORCE_COLOR"):
        monkeypatch.delenv(k, raising=False)
    yield d
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_sim_handler", None):
            root.removeHandler(h)
            h.close()


def _lines(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


@pytest.fixture
def dry_run(tmp_path, logdir):
    out = tmp_path / "run"
    code = main.main(["--scenario", "S2", "--condition", "C", "--dry-run", "--no-grand-jury",
                      "--run-id", "logtest", "--output", str(out)])
    assert code == 0
    return out


def test_run_writes_log_events_and_calls(dry_run, logdir):
    log = (dry_run / "logtest.log").read_text()
    # DEBUG reaches the file even though the console is at INFO
    assert "DEBUG" in log and "[save] wrote" in log
    assert "Month 8" in log and "FINAL STANDING" in log            # tables rendered as text
    assert "git_commit" not in log or "commit" in log
    events = _lines(dry_run / "logtest.events.jsonl")
    kinds = [e.get("event") for e in events]
    assert kinds.count("turn_summary") == 8 and kinds.count("turn_start") == 8
    assert kinds[0] == "run_header" and "final" in kinds
    summary = next(e for e in events if e.get("event") == "turn_summary")["data"]
    assert {lab["key"] for lab in summary["labs"]} == {"anthropic", "openai", "gdm", "meta", "xai"}
    header = events[0]["data"]
    assert header["run_id"] == "logtest" and header["config_hash"] and "argv" in header
    calls = calllog.read_calls(dry_run / "logtest.calls.jsonl")
    actors = [c for c in calls if c["purpose"] == "actor"]
    assert len(actors) >= 40                                         # 5 seats x 8 months (+ message rounds)
    assert all(c["user"] and c["text"] is not None and c["system"] for c in actors)
    # system prompts are stored once each, not per call
    raw = (dry_run / "logtest.calls.jsonl").read_text().splitlines()
    assert sum(1 for x in raw if '"kind": "system"' in x) <= 10
    # the invocation log has the whole process
    inv = list(logdir.glob("*-main-*.log"))
    assert inv and "Month 8" in inv[0].read_text()


def test_resumed_run_appends_to_the_same_log(tmp_path, logdir):
    logsetup.configure_logging("t", invocation_log=False)
    for _ in range(2):
        with logsetup.run_logging(tmp_path, "r1"):
            logging.getLogger("x").info("hello")
    text = (tmp_path / "r1.log").read_text()
    assert text.count("hello") == 2 and "appending: resumed" in text


def test_nested_run_logging_writes_each_line_once(tmp_path, logdir):
    # a post-run jury (core.jury.collect_grand_jury) inside pilot's logged post-run stage
    logsetup.configure_logging("t", invocation_log=False)
    with logsetup.run_logging(tmp_path, "r2", stage="post-run"):
        with logsetup.run_logging(tmp_path, "r2", stage="grand_jury"):
            logging.getLogger("x").info("once-only")
        logging.getLogger("x").info("still-attached")
    logging.getLogger("x").info("after-detach")
    text = (tmp_path / "r2.log").read_text()
    assert text.count("once-only") == 1 and "still-attached" in text and "after-detach" not in text


def test_no_ansi_when_no_color_or_not_a_tty(logdir, monkeypatch, capsys):
    monkeypatch.setenv("NO_COLOR", "1")
    logsetup.configure_logging("t", invocation_log=False)
    logging.getLogger("x").warning("[intrusion] something happened")
    console.emit("turn_start", "m", {"turn": 1, "label": "August 2026"})
    err = capsys.readouterr().err
    assert "[intrusion] something happened" in err and "Month 1" in err
    assert "\x1b[" not in err


def test_color_when_forced(logdir, monkeypatch, capsys):
    monkeypatch.delenv("NO_COLOR", raising=False)
    logsetup.configure_logging("t", color="always", invocation_log=False)
    logging.getLogger("x").warning("[intrusion] coloured")
    assert "\x1b[" in capsys.readouterr().err


def test_quiet_console_still_logs_debug_to_files(logdir, capsys):
    path = logsetup.configure_logging("t", quiet=True)
    logging.getLogger("x").info("info line")
    logging.getLogger("x").debug("debug line")
    assert "info line" not in capsys.readouterr().err
    text = path.read_text()
    assert "info line" in text and "debug line" in text


def test_uncaught_exception_is_logged(logdir, capsys):
    path = logsetup.configure_logging("t")
    try:
        raise ValueError("boom-uncaught")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    capsys.readouterr()
    text = path.read_text()
    assert "uncaught exception" in text and "boom-uncaught" in text and "Traceback" in text


def test_warnings_are_captured(logdir):
    path = logsetup.configure_logging("t")
    # pytest replaces warnings.showwarning around each test; call what
    # logging.captureWarnings installs outside pytest.
    logging._showwarning("a-captured-warning", UserWarning, "f.py", 1)
    logging.captureWarnings(False)
    assert "a-captured-warning" in path.read_text()


def test_call_journal_keeps_failed_and_unparsed_calls(tmp_path, logdir):
    llm.register_stub("journal_ok", lambda system, user: "not json at all")

    def fail(system, user):
        raise RuntimeError("provider exploded")
    llm.register_stub("journal_fail", fail)
    path = tmp_path / "j.calls.jsonl"
    calllog.register("jrun", path)
    try:
        r = llm.complete("stub:journal_ok", "SYS", "USER-1", purpose="actor", run_id="jrun")
        assert r.text == "not json at all"
        with pytest.raises(RuntimeError):
            llm.complete("stub:journal_fail", "SYS", "USER-2", purpose="macro_jury", run_id="jrun")
    finally:
        calllog.unregister("jrun")
    calls = calllog.read_calls(path)
    assert [c["user"] for c in calls] == ["USER-1", "USER-2"]
    assert calls[0]["text"] == "not json at all" and calls[0]["system"] == "SYS"
    assert "provider exploded" in calls[1]["error"]


def test_unregistered_stub_calls_are_not_journalled(logdir):
    llm.register_stub("journal_quiet", lambda system, user: "x")
    llm.complete("stub:journal_quiet", "S", "U", run_id=None)
    assert not list(logdir.glob("calls-*.jsonl"))


def test_say_keeps_stdout_and_logs_to_file(logdir, capsys):
    path = logsetup.configure_logging("t")
    console.say("Refusing: a plain line")
    out, err = capsys.readouterr()
    assert out == "Refusing: a plain line\n"
    assert "a plain line" not in err                     # not echoed twice on the console
    assert "Refusing: a plain line" in path.read_text()
