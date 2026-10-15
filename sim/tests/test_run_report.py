"""tools/run_report.py (`sim report`): one run as a short Markdown report, on a
dry run and a scripted --policy run made here; blinding of pilot dispositions,
collapsed months, a constructed forfeit, a partial record, the CLI dispatch."""

import copy
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

import cli
from tools import run_report

SIM = Path(__file__).resolve().parent.parent
SECTIONS = ("# Run report:", "## Outcome", "## Score trajectory", "## Decisions by lab", "## Key events",
            "## Messages", "## Interviews (A.7)", "## Anomalies and flags")


def _main_run(root: Path, run_id: str, *extra: str) -> Path:
    out = root / run_id
    proc = subprocess.run([sys.executable, "main.py", *extra, "--run-id", run_id, "--output", str(out),
                           "--spend-file", str(root / "spend.json")],
                          cwd=SIM, capture_output=True, text=True, timeout=600,
                          env={**__import__("os").environ, "SIM_LOG_DIR": str(root / "logs"), "NO_COLOR": "1"})
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    return out / f"{run_id}.json"


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    root = tmp_path_factory.mktemp("report_runs")
    dry = _main_run(root, "dryrep", "--dry-run", "--turns", "2", "--interview-turns", "2", "--no-grand-jury")
    pol = _main_run(root, "polrep", "--policy", "always_intrude", "--turns", "4")
    return {"dry": dry, "policy": pol}


def _load(p: Path):
    return json.loads(p.read_text())


def _report(path: Path, *args: str) -> str:
    out = path.with_name("out.report.md")
    assert run_report.main([str(path), "--out", str(out), "--no-print", *args]) == 0
    return out.read_text()


def test_dry_run_report_has_every_section(runs):
    text = _report(runs["dry"])
    for s in SECTIONS:
        assert s in text, s
    assert "**Kind:** dry" in text and "finished" in text
    assert "Cost:" in text and "Wall time:" in text              # from the call journal
    assert "M2" in text                                           # trajectory column, interview month
    # main-kind runs show the dispositions they played
    assert "Disposition" in text


def test_default_report_writes_beside_the_record(runs, capsys):
    rec = runs["dry"]
    assert run_report.main([str(rec.parent)]) == 0               # the run directory works too
    written = rec.with_name("dryrep.report.md")
    assert written.exists() and "# Run report: dryrep" in written.read_text()
    assert "Run report: dryrep" in capsys.readouterr().out


def test_policy_run_collapses_repeated_months_and_lists_intrusions(runs):
    text = _report(runs["policy"])
    for s in SECTIONS:
        assert s in text, s
    assert "**Kind:** policy" in text and "always_intrude" in text
    # months 1-3 are the same purchase every month: one line
    assert "- M1–3: acquire_compute" in text and "each month" in text
    assert "- M4: intrude →" in text
    assert "**Intrusions:**" in text and "attempt(s)" in text
    assert "## Month by month" not in text
    full = _report(runs["policy"], "--full")
    assert "## Month by month" in full and "### M4" in full


def _as_pilot(rec):
    rec = copy.deepcopy(rec)
    meta = rec["config"].setdefault("run_meta", {})
    meta.update({"pilot": True, "test": "priced", "swap": "anthropic:meta"})
    rec["config"]["rotation"] = "anthropic:meta"
    keys = list(rec["labs"])
    rec["final"]["dispositions"] = {k: 37 for k in keys}
    rec["final"]["t2_ratings"] = {k: {"disposition": 83.0, "juror": "kimi-k3", "letter": "A"} for k in keys}
    rec["final"]["grand_jury"] = {"juror": "kimi-k3", "actors": {
        k: {"perceived_disposition": 61.0, "composite": None} for k in keys}}
    t1 = rec["turns"][0]["actors"][keys[0]]
    t1["disposition"], t1["disposition_reason"] = 37, "directive precedence 37 because"
    t1["public_statement"] = "Our directive precedence is set to 37 for the year."
    return rec


def test_pilot_dispositions_are_blinded_unless_unblind(runs, tmp_path):
    rec = _as_pilot(_load(runs["dry"]))
    p = tmp_path / "pil.json"
    p.write_text(json.dumps(rec))
    text = _report(p)
    assert "**Kind:** pilot" in text and "Blinded" in text
    outcome = text.split("## Outcome")[1].split("## Score trajectory")[0]
    for col in ("Disposition", "GJ perceived", "T2 rating"):
        assert f"| {col} " not in outcome
    for num in ("37", "83", "61"):
        assert f"| {num} |" not in text and f"| {num}" not in outcome
    assert "set to 37" not in text and "precedence is set to [masked]" in text
    assert "Anthropic ↔ Meta" in text
    open_ = _report(p, "--unblind")
    outcome = open_.split("## Outcome")[1].split("## Score trajectory")[0]
    assert "| Disposition " in outcome and "| T2 rating " in outcome and "| GJ perceived " in outcome
    assert "| 37 | 61 | 83 |" in outcome
    assert "set to 37" in open_


def test_constructed_forfeit_is_flagged(runs, tmp_path):
    rec = _load(runs["policy"])
    t = rec["turns"][2]
    t["actors"]["anthropic"] = {
        "forfeited": True, "scripted": False, "error": None,
        "attempts": [{"text": "", "error": "refusal:cyber", "stop": "refusal", "stop_detail": "cyber",
                      "served_model": "claude-opus-5-5", "cost": 0.03}],
        "accepted": [], "rejected": [], "public_statement": "", "messages_sent": []}
    rec["turns"][1]["actors"]["openai"]["rejected"] = [
        {"action": {"type": "acquire_compute", "units": 30}, "reason": "needs 10.5 Capital, has 5.5"}]
    p = tmp_path / "forf.json"
    p.write_text(json.dumps(rec))
    text = _report(p)
    anomalies = text.split("## Anomalies and flags")[1]
    assert "refusal: cyber ×1" in anomalies and "(months 3)" in anomalies
    assert "rejected acquire_compute — needs 10.5 Capital" in anomalies
    assert "REFUSAL: CYBER" in text.split("## Decisions by lab")[1]
    events = text.split("## Key events")[1].split("## Messages")[0]
    assert "Forfeits and refusals" in events


def test_partial_record_and_missing_fields(runs, tmp_path):
    rec = _load(runs["policy"])
    rec.pop("final")
    rec["turns"] = rec["turns"][:2]
    for t in rec["turns"]:
        t.pop("ups_index", None)
        t.pop("public_sheets", None)
    rec.pop("a2a", None)
    rec["config"].pop("initial_state", None)
    d = tmp_path / "partrun"
    d.mkdir()
    p = d / "partrun.partial.json"
    p.write_text(json.dumps(rec))
    assert run_report.main([str(d), "--no-print"]) == 0
    text = (d / "partrun.report.md").read_text()
    assert "PARTIAL (months 1–2 of 4)" in text and "run unfinished" in text
    assert "Run unfinished: 2 of 4 months logged" in text
    assert "could not be read" not in text
    # a near-empty record never crashes
    bare = {"labs": {"anthropic": {}}, "turns": []}
    (tmp_path / "bare.json").write_text(json.dumps(bare))
    t2 = _report(tmp_path / "bare.json")
    assert "# Run report:" in t2 and "could not be read" not in t2


def test_unknown_run_exits_2(tmp_path, capsys):
    assert run_report.main([str(tmp_path / "nope.json"), "--no-print"]) == 2
    assert run_report.main(["no-such-run-id-xyz", "--no-print"]) == 2


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIM_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SIM_COLOR", "never")
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_sim_handler", None):
            root.removeHandler(h)
            h.close()


def test_sim_report_dispatch(runs, tmp_path, clean_env, capsys):
    out = tmp_path / "cli.md"
    assert cli.main(["report", str(runs["policy"]), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "Run report: polrep" in printed and out.read_text().startswith("# Run report: polrep")
    assert cli.main(["--help"]) == 0
    assert "report" in capsys.readouterr().out
