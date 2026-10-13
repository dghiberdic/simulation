"""Pilot money paths after a stop: a finished run is completed (post-run stages, the
summary, the hard stop) without being paid for again, whatever its run id, and the
summary keeps the measured cost inputs. Dry runs and synthetic records only."""

import json
from pathlib import Path

import pytest

import pilot
from core import costs

LABS = ("anthropic", "openai", "gdm", "meta", "xai")
T5_SETUP = {"scenario": "S1", "condition": "A", "a2a_mode": "merged", "turns": 4, "seed": 0,
            "rotation": None, "event_turn": 2}


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(costs, "DEFAULT_SPEND_FILE", tmp_path / "spend.json")
    costs.configure()


class _NoPay:
    built = 0

    def __init__(self, *a, **kw):
        type(self).built += 1

    def run(self):
        raise AssertionError("the finished run was paid for again")


def _t5(root: Path, run_id: str = "T5-run01", dry: bool = True, **setup) -> Path:
    d = root / "T5"
    d.mkdir(parents=True, exist_ok=True)
    rec = {"run_id": run_id, "labs": {k: {"model": f"stub:actor_{k}"} for k in LABS},
           "turns": [{"turn": t, "actors": {}} for t in range(1, 5)],
           "config": dict(T5_SETUP, dry_run=dry, **setup),
           "final": {"scores": [{"lab": "anthropic"}]}}
    (d / f"{run_id}.json").write_text(json.dumps(rec))
    (root / "approval.json").write_text(json.dumps({"turns": 8, "cuts": []}))
    return d / f"{run_id}.json"


def _argv(root, *extra):
    return ["T5", "--dry-run", "--output", str(root), "--force-order",
            "--spend-file", str(root / "spend.json"), *extra]


def _dry(root, *argv):
    return pilot.main(list(argv) + ["--dry-run", "--output", str(root), "--spend-file", str(root / "spend.json")])


def test_renamed_finished_run_is_found_and_completed(tmp_path, monkeypatch, capsys):
    """A -vN run (the base id holds another set-up) is found by a re-invoked test and by
    --post-run-only; its missing stages are collected, never by paying again."""
    _t5(tmp_path, "T5-run01", turns=6, event_turn=3)            # another set-up at the base id
    _t5(tmp_path, "T5-run01-v2")
    _NoPay.built = 0
    monkeypatch.setattr(pilot, "SimulationEngine", _NoPay)
    assert pilot.main(_argv(tmp_path, "--post-run-only")) == 0
    assert "T5-run01-v2 is finished" in capsys.readouterr().out
    s = pilot.read_summary(tmp_path, "T5")
    assert s["status"] == "completed" and s["run_id"] == "T5-run01-v2"
    assert pilot.main(_argv(tmp_path)) == 2                     # complete: refused, never re-run
    out = capsys.readouterr().out
    assert "already completed" in out and _NoPay.built == 0


def test_rerun_with_missing_post_run_stages_is_completed_without_force(tmp_path, monkeypatch, capsys):
    root = tmp_path / "p"
    assert _dry(root, "priced") == 0
    assert _dry(root, "approve", "--turns", "8") == 0
    assert _dry(root, "T6") == 0
    assert _dry(root, "T6", "--rerun") == 0
    assert pilot.rerun_done(root, pilot.load_pilot(), True)
    # A stop during the re-run's T2 rating: no rating pooled, no summary.
    pool = root / pilot.DRY_RATINGS_NAME
    pooled = json.loads(pool.read_text())
    for field in pilot.RATING_FIELDS:
        pooled[field] = {k: [r for r in v if r.get("run_id") != "T6-run02"] for k, v in pooled[field].items()}
    pool.write_text(json.dumps(pooled))
    rec_path = root / "T6" / "T6-run02.json"
    rec = json.loads(rec_path.read_text())
    rec["final"].pop("t2_ratings", None)
    rec_path.write_text(json.dumps(rec))
    pilot.summary_path(root, "T6", rerun=True).unlink()
    assert not pilot.rerun_done(root, pilot.load_pilot(), True)
    _NoPay.built = 0
    monkeypatch.setattr(pilot, "SimulationEngine", _NoPay)
    capsys.readouterr()
    assert _dry(root, "T6", "--rerun") == 0
    out = capsys.readouterr().out
    assert "--force" not in out and _NoPay.built == 0
    assert set(json.loads(rec_path.read_text())["final"]["t2_ratings"]) == set(LABS)
    assert pilot.read_summary(root, "T6", rerun=True)["status"] == "completed"
    # Complete now: a further re-run is refused; plain T6 targets the re-run too.
    assert _dry(root, "T6", "--rerun") == 2
    assert "one re-run" in capsys.readouterr().out
    assert _dry(root, "T6", "--post-run-only") == 0
    assert "T6-run02 complete" in capsys.readouterr().out


def test_post_run_only_keeps_the_measured_inputs(tmp_path, monkeypatch):
    """The summary of a post-run-only process keeps the run's measured inputs, usage and
    run cost (approve re-prices from them), never the fallbacks."""
    _t5(tmp_path)
    prior = {"test": "T5", "run_id": "T5-run01", "status": "halted_budget", "runs_completed": 1,
             "inputs": {"actor_first_turn": 2.0, "actor_later_turn": 0.6, "interview_per_turn": 0.3,
                        "macro_per_review": 0.4, "disposition_rating": 0.1},
             "usage": {"run_cost_usd": 6.0, "by_purpose": {"actor": 5.0, "macro_jury": 0.8,
                                                            "disposition_jury": 0.2},
                       "jurors": {}, "by_actor_model": {}},
             "run_cost_usd": 6.0}
    (tmp_path / "T5" / "pilot_summary_T5.json").write_text(json.dumps(prior))
    monkeypatch.setattr(pilot, "SimulationEngine", _NoPay)
    assert pilot.main(_argv(tmp_path)) == 0
    s = pilot.read_summary(tmp_path, "T5")
    assert s["status"] == "completed" and s["post_run_only"]
    assert s["inputs"]["macro_per_review"] == 0.4 and s["inputs"]["actor_later_turn"] == 0.6
    assert s["run_cost_usd"] == 6.0 and s["usage"]["by_purpose"]["macro_jury"] == 0.8
    measured = pilot.measured_inputs(tmp_path, {"order": ["T5"]})
    assert measured["macro_per_review"] == 0.4


def test_post_run_only_without_a_summary_reads_the_call_log(tmp_path, monkeypatch):
    """A kill before the summary was written: usage and inputs come from the ledger's call
    log of this run (every process), not from this process's juror calls alone."""
    _t5(tmp_path)
    log = tmp_path / "spend_calls.jsonl"
    rows = [{"model": "kimi-k2", "purpose": "macro_jury", "run_id": "T5-run01", "cost": 0.5},
            {"model": "kimi-k2", "purpose": "macro_jury", "run_id": "T5-run01", "cost": 0.5},
            {"model": "claude-opus-5-5", "purpose": "interview", "run_id": "T5-run01", "cost": 0.9},
            {"model": "claude-opus-5-5", "purpose": "actor", "run_id": "other-run", "cost": 9.0}]
    log.write_text("".join(json.dumps(r) + "\n" for r in rows))
    rec = json.loads((tmp_path / "T5" / "T5-run01.json").read_text())
    rec["turns"][1]["macro_jury"] = {"values": {}}
    for t in rec["turns"]:
        t["interviews"] = {}
    (tmp_path / "T5" / "T5-run01.json").write_text(json.dumps(rec))
    monkeypatch.setattr(pilot, "SimulationEngine", _NoPay)
    assert pilot.main(_argv(tmp_path)) == 0
    s = pilot.read_summary(tmp_path, "T5")
    assert s["run_cost_usd"] == pytest.approx(1.9)
    assert s["inputs"]["macro_per_review"] == pytest.approx(1.0)


def test_dry_run_never_touches_a_real_runs_log(tmp_path, capsys):
    """--dry-run with the real run's --output: refused, the real partial log kept as it is."""
    d = tmp_path / "T5"
    d.mkdir()
    (tmp_path / "approval.json").write_text(json.dumps({"turns": 8, "cuts": []}))
    partial = {"run_id": "T5-run01", "labs": {k: {"model": k} for k in LABS},
               "turns": [{"turn": 1, "actors": {}}], "config": dict(T5_SETUP, dry_run=False), "final": {}}
    (d / "T5-run01.partial.json").write_text(json.dumps(partial))
    before = (d / "T5-run01.partial.json").read_text()
    assert pilot.main(_argv(tmp_path)) == 2
    assert "real run's log" in capsys.readouterr().out
    assert (d / "T5-run01.partial.json").read_text() == before
    assert sorted(p.name for p in d.iterdir()) == ["T5-run01.partial.json"]


def test_rescore_targets_the_latest_t6_run(tmp_path, monkeypatch):
    """T8 (§9.2): the held-out five come from the latest T6 run: after the re-run, the
    re-score reads T6-run02."""
    d = tmp_path / "T6"
    d.mkdir()
    t6 = pilot.resolve_preset(pilot.load_pilot(), "T6")
    for seed in (0, 1):
        rid = f"T6-run0{seed + 1}"
        rec = {"run_id": rid, "labs": {k: {"model": f"stub:actor_{k}"} for k in LABS}, "turns": [],
               "config": dict(pilot.run_expect(t6, seed), dry_run=True),
               "final": {"scores": [1], "grand_jury": {"juror": "kimi", "actors": {}}}}
        (d / f"{rid}.json").write_text(json.dumps(rec))
    seen = []

    def verdict(record, jurors, run_id, output_dir):
        seen.append(run_id)
        return "Grand Jury: usable verdict"
    monkeypatch.setattr(pilot, "collect_grand_jury", verdict)
    assert pilot.main(["T6", "--rescore-grand-jury", "--dry-run", "--output", str(tmp_path),
                       "--spend-file", str(tmp_path / "spend.json")]) == 0
    assert seen == ["T6-run02"]


def test_force_moves_the_partial_log_aside_with_the_final(tmp_path):
    """claim_run_id(force=True): a partial log left beside the finished final (a kill
    between the final write and the partial unlink) goes aside with it, so the new
    run never replays the old one."""
    path = _t5(tmp_path, dry=False)
    partial = path.with_name("T5-run01.partial.json")
    partial.write_text(path.read_text())
    assert pilot.claim_run_id(path.parent, "T5-run01", T5_SETUP, None, force=True) == "T5-run01"
    assert not path.exists() and not partial.exists()
    assert len(list(path.parent.glob("T5-run01.json.replaced-*"))) == 1
    assert len(list(path.parent.glob("T5-run01.partial.json.replaced-*"))) == 1


def test_unusable_t2_rating_is_settled_and_rerate_asks_again(tmp_path, monkeypatch):
    """A T2 rating unusable after the juror's retries and fallback is settled: the test
    completes, re-invocations ask nothing, the summary lists it, and --rerate asks the
    juror again for those seats only."""
    from core import stubs
    calls = []
    monkeypatch.setattr(stubs, "disposition_reply", lambda system, user: (calls.append(1), "no")[1])
    root = tmp_path / "p"
    root.mkdir()
    (root / "approval.json").write_text(json.dumps({"turns": 8, "cuts": []}))
    argv = ["T5", "--dry-run", "--output", str(root), "--force-order", "--sequential",
            "--spend-file", str(tmp_path / "spend.json")]
    assert pilot.main(argv) == 0
    seats = pilot.settled_unusable(pilot.ratings_path(root, True), "T5", "T5-run01")
    assert seats and calls
    n = len(calls)
    for extra in (["--post-run-only"], ["--skip-completed"]):
        assert pilot.main(argv + extra) == 0
    assert len(calls) == n
    pilot.summary_path(root, "T5").unlink()
    assert pilot.main(argv) == 0 and len(calls) == n
    s = pilot.read_summary(root, "T5")
    assert s["status"] == "completed"
    assert all(any(e.startswith(f"T2 rating {k}:") and "--rerate" in e for e in s["post_run_errors"])
               for k in seats)
    rated = []
    monkeypatch.setattr(stubs, "disposition_reply",
                        lambda system, user: (rated.append(1), json.dumps({"disposition": 40, "reason": "r"}))[1])
    assert pilot.main(argv + ["--rerate"]) == 0
    assert len(rated) == len(seats)
    assert pilot.settled_unusable(pilot.ratings_path(root, True), "T5", "T5-run01") == []
    rec = json.loads((root / "T5" / "T5-run01.json").read_text())
    assert set(seats) <= set(rec["final"]["t2_ratings"])
    assert not pilot.read_summary(root, "T5")["post_run_errors"]
    assert pilot.main(argv + ["--rerate"]) == 0 and len(rated) == len(seats)


def test_t6_rerun_writes_its_own_review(tmp_path):
    """T6 --rerun writes review_T6-rerun.md; T6-run01's review_T6.md is kept."""
    root = tmp_path / "p"
    root.mkdir()
    (root / "approval.json").write_text(json.dumps({"turns": 8, "cuts": []}))
    argv = ["T6", "--dry-run", "--output", str(root), "--force-order", "--sequential", "--allow-no-examples",
            "--spend-file", str(tmp_path / "spend.json")]
    assert pilot.main(argv) == 0
    first = (root / "T6" / "review_T6.md").read_text()
    assert pilot.main(argv + ["--rerun"]) == 0
    assert (root / "T6" / "review_T6.md").read_text() == first
    assert pilot.review_path(root, "T6", True).name == "review_T6-rerun.md"
    assert "T6-run02" in pilot.review_path(root, "T6", True).read_text()
    assert pilot.read_summary(root, "T6", rerun=True)["review"].endswith("review_T6-rerun.md")
