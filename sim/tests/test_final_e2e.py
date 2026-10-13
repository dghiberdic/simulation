"""Final end-to-end verification (README followed literally, dry run only).

Each xfail(strict) test encodes the behaviour the spec or the README promises
and that the merged code does not yet deliver; the passing tests pin the README
walkthrough steps that worked. Offline: stubs, scratch dirs, no keys.
"""

import csv
import json

import pytest

import campaign
import main as main_mod
import pilot
from core import costs
from core.costs import configure
from core.exits import EXIT_REFUSED, EXIT_WAITING
from tools import compare_arms
from tools import disposition as dt
from tools import jury_check as jc
from tools import summarize_run as SR

KEYS = ["anthropic", "openai", "gdm", "meta", "xai"]
FAMS = {"anthropic": "claude", "openai": "gpt", "gdm": "gemini", "meta": "muse", "xai": "grok"}
ITEMS = ["honesty", "rule_keeping", "restraint", "fair_dealing", "outside_welfare"]


@pytest.fixture(autouse=True)
def _scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "PILOT_DIR", tmp_path / "pilot_default")
    monkeypatch.setattr(main_mod, "RUNS_DIR", tmp_path / "runs_default")
    monkeypatch.setattr(campaign, "RUNS_DIR", tmp_path / "runs_default")
    monkeypatch.setattr(costs, "DEFAULT_SPEND_FILE", tmp_path / "spend.json")
    configure()
    yield


def _dry(root, *argv):
    return pilot.main(list(argv) + ["--dry-run", "--output", str(root), "--spend-file", str(root / "spend.json")])


def _blank_template(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r + [""] * (len(header) - len(r)))


# ---------------------------------------------------------------------------
# T2: the exported, still blank human_ratings.csv template (README "Human steps")
# ---------------------------------------------------------------------------

def _real_pool():
    """A non-stub pooled file: juror ratings of two T1 runs, letters A-E."""
    ratings, chosen = {}, {}
    for rid, test in (("priced-run01", "priced"), ("T6-run01", "T6")):
        for i, k in enumerate(KEYS):
            ratings.setdefault(k, []).append({"run_id": rid, "test": test, "juror": "kimi-k3",
                                              "disposition": 20 + 15 * i, "letter": "ABCDE"[i]})
            chosen.setdefault(k, []).append({"run_id": rid, "test": test, "value": 60})
    return {"ratings": ratings, "chosen": chosen, "families": FAMS}


@pytest.mark.xfail(strict=True, reason="BUG-e2e-1: disposition.py treats the exported blank human_ratings.csv "
                                       "as our ratings: it prints the juror's judged values and --write writes "
                                       "config/dispositions.json with no human rating")
def test_blank_human_template_is_waiting_not_resolved(tmp_path, capsys):
    pool = tmp_path / "pool.json"
    pool.write_text(json.dumps(_real_pool()))
    human = tmp_path / "human_ratings.csv"
    _blank_template(human, ["run_id", "letter", "disposition"],
                    [[rid, L] for rid in ("priced-run01", "T6-run01") for L in "ABCDE"])
    out = tmp_path / "dispositions.json"
    rc = dt.main([str(pool), "--human", str(human), "--write", "--output", str(out)])
    text = capsys.readouterr().out
    assert rc == EXIT_WAITING                         # README: exit 4 "waiting for human ratings"
    assert not out.exists()                           # nothing written without our ratings
    assert "judged" not in text                       # §9.2 T2: ours before seeing the juror's


def test_filled_human_template_resolves_by_letter(tmp_path, capsys):
    """The README path that works: ratings keyed by run id + letter map through the pooled letters."""
    pool = tmp_path / "pool.json"
    pool.write_text(json.dumps(_real_pool()))
    human = tmp_path / "human_ratings.csv"
    _blank_template(human, ["run_id", "letter", "disposition"],
                    [[rid, L, str(10 + 15 * i)] for rid in ("priced-run01", "T6-run01")
                     for i, L in enumerate("ABCDE")])
    out = tmp_path / "dispositions.json"
    assert dt.main([str(pool), "--human", str(human), "--write", "--output", str(out)]) == 0
    assert "rho 1.0" in capsys.readouterr().out
    # SE of two identical ratings is 0 but n = 2 < 3 rated seat-runs -> the chosen median
    assert json.loads(out.read_text()) == {k: 60 for k in KEYS}


# ---------------------------------------------------------------------------
# T8: the exported, still blank hand_scores.csv template
# ---------------------------------------------------------------------------

def _t6_record():
    actors = {k: {"items": {i: 3 for i in ITEMS}, "composite": 3, "perceived_disposition": 50}
              for k in KEYS}
    return {"run_id": "T6-run01", "config": {"run_meta": {"pilot": "T6", "run_id": "T6-run01"}},
            "labs": {k: {"model": f"m-{k}"} for k in KEYS},
            "final": {"grand_jury": {"juror": "kimi-k3", "rubric": "scored", "actors": actors,
                                     "letters": dict(zip(KEYS, "ABCDE"))}}}


@pytest.mark.xfail(strict=True, reason="BUG-e2e-2: jury_check.py on the exported blank hand_scores.csv exits 2 "
                                       "('no decision') instead of 4 (README: 4 hand scores not there yet)")
def test_blank_hand_template_is_waiting(tmp_path):
    rec = tmp_path / "T6-run01.json"
    rec.write_text(json.dumps(_t6_record()))
    hand = tmp_path / "hand_scores.csv"
    _blank_template(hand, ["run_id", "letter"] + ITEMS + ["composite"],
                    [["T6-run01", L] for L in "ABCDE"])
    assert jc.main([str(rec), "--hand", str(hand)]) == EXIT_WAITING


# ---------------------------------------------------------------------------
# pilot.py T6 --post-run-only after the one re-run
# ---------------------------------------------------------------------------

def test_post_run_only_collects_the_rerun_grand_jury(tmp_path, capsys):
    root = tmp_path / "p"
    assert _dry(root, "priced") == 0
    assert _dry(root, "approve", "--turns", "8") == 0
    assert _dry(root, "T6") == 0
    assert _dry(root, "T6", "--rerun") == 0
    p = root / "T6" / "T6-run02.json"
    rec = json.loads(p.read_text())
    assert rec["final"].get("grand_jury")
    del rec["final"]["grand_jury"]                 # e.g. the guard tripped during the re-run's jury
    p.write_text(json.dumps(rec))
    _dry(root, "T6", "--post-run-only")
    assert json.loads(p.read_text())["final"].get("grand_jury")


# ---------------------------------------------------------------------------
# campaign.py: priorities after --replan; an explicit guard on a resume
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="BUG-e2e-4: after run --replan keeps a formerly cut cell, that cell's "
                                       "catch-up runs (run01..) go before the priority cells' next run, so a "
                                       "binding guard cuts A and C before A0/kin (§9.3 priority)")
def test_replan_keeps_priority_order_when_a_cut_cell_returns(tmp_path):
    done = {"S1-A": 4, "S2-A": 4, "S1-C": 4, "S2-C": 4}
    plan = campaign.build_campaign(None, 300.0, turns=8, s2_mode="separate", done=done)
    assert any(c["runs"] for c in plan["cells"] if c["group"] == "A0")      # the re-plan keeps A0
    state = {"runs": {k: [f"{k}-run{i:02d}" for i in range(1, n + 1)] for k, n in done.items()}}
    (tmp_path / campaign.STATE_NAME).write_text(json.dumps(state))
    order = []

    def fake(args):
        order.append(args.run_id)
        return 0, {}
    assert campaign.run_campaign(plan, tmp_path, dry_run=True, run_cell=fake) == 0
    assert order[0] == "S1-A-run05"


def _ledger(path, total):
    path.write_text(json.dumps({"total_usd": total, "calls": 0, "by_model": {}}))


@pytest.mark.xfail(strict=True, reason="BUG-e2e-5: a first campaign `run` refused before any run saves its "
                                       "$750 guard; a later `run --headroom 10` silently keeps $750")
def test_explicit_guard_on_resume_is_not_silently_ignored(tmp_path, monkeypatch, capsys):
    _ledger(tmp_path / "spend.json", 0.0)
    argv = ["run", "--spend-file", str(tmp_path / "spend.json"), "--output", str(tmp_path / "runs"),
            "--pilot-dir", str(tmp_path / "nopilot")]
    monkeypatch.setattr(main_mod, "run_cell", lambda a: (EXIT_REFUSED, None))   # e.g. no dispositions.json
    assert campaign.main(argv) == EXIT_REFUSED
    budgets = []

    def fake(a):
        budgets.append(a.budget)
        return 0, {"final": {"scores": [{"lab": "xai"}]}}
    monkeypatch.setattr(main_mod, "run_cell", fake)
    capsys.readouterr()
    rc = campaign.main(argv + ["--headroom", "10", "--rounds", "1"])
    out = capsys.readouterr().out
    # Either the new guard applies, or the flag is refused / named as ignored.
    assert all(b is not None and b <= 10.0 + 1e-9 for b in budgets) or \
        (rc == EXIT_REFUSED and not budgets) or "headroom" in out.lower()


def test_campaign_resume_after_a_kill_finishes_the_interrupted_run(tmp_path):
    """README: `run` again resumes the saved plan; the interrupted run is redone first."""
    plan = campaign.build_campaign(None, 300.0, turns=8, s2_mode="separate")
    order = []

    def stop(args):
        order.append(args.run_id)
        return (2, None) if args.run_id == "S2-A-run03" else (0, {})
    assert campaign.run_campaign(plan, tmp_path, dry_run=True, rounds=3, run_cell=stop) == 2
    order.clear()

    def ok(args):
        order.append(args.run_id)
        return 0, {}
    assert campaign.run_campaign(plan, tmp_path, dry_run=True, rounds=3, run_cell=ok) == 0
    assert order == ["S2-A-run03", "S1-C-run03", "S2-C-run03"]


# ---------------------------------------------------------------------------
# Exit codes (README table: 2 = missing or unreadable input)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="BUG-e2e-6: summarize_run.py --t6-tally on a missing sheet raises "
                                       "FileNotFoundError (exit 1, the budget-guard code) instead of exit 2")
def test_t6_tally_missing_sheet_exits_2(tmp_path):
    assert SR.main(["--t6-tally", str(tmp_path / "nope.csv")]) == EXIT_REFUSED


@pytest.mark.xfail(strict=True, reason="BUG-e2e-7: summarize_run.py --t1 with no record exits 0 (README: 2)")
def test_t1_with_no_records_exits_2(tmp_path):
    assert SR.main(["--t1", str(tmp_path / "nope")]) == EXIT_REFUSED


@pytest.mark.xfail(strict=True, reason="BUG-e2e-7: summarize_run.py --t7 with no record exits 0 (README: 2)")
def test_t7_with_no_records_exits_2(tmp_path):
    assert SR.main(["--t7", str(tmp_path / "nope")]) == EXIT_REFUSED


@pytest.mark.xfail(strict=True, reason="BUG-e2e-8: summarize_run.py --t6-sheet over missing dirs writes an "
                                       "empty sheet and exits 0 (README: 2)")
def test_t6_sheet_with_no_records_exits_2_and_writes_nothing(tmp_path):
    sheet = tmp_path / "T6_coding_sheet.csv"
    rc = SR.main([str(tmp_path / "priced"), str(tmp_path / "T6"), "--t6-sheet", str(sheet)])
    assert rc == EXIT_REFUSED and not sheet.exists()


@pytest.mark.xfail(strict=True, reason="BUG-e2e-9: compare_arms.py with no logs in either arm exits 0 and "
                                       "writes review_T9-compare.md beside the missing dir (README: 2)")
def test_compare_arms_with_no_logs_exits_2(tmp_path):
    rc = compare_arms.main(["--separate", str(tmp_path / "x"), "--merged", str(tmp_path / "y")])
    assert rc == EXIT_REFUSED and not (tmp_path / "review_T9-compare.md").exists()


# ---------------------------------------------------------------------------
# README walkthrough steps that work (pinned)
# ---------------------------------------------------------------------------

def test_approve_budget_is_stored_kept_and_reset(tmp_path):
    """README step 5: --budget is stored, kept by a later approve, reset by --budget 48;
    a guard the plan cannot fit is refused (exit 3) and the earlier approval kept."""
    root = tmp_path / "p"
    assert _dry(root, "priced") == 0
    assert _dry(root, "approve", "--turns", "8", "--budget", "30") == 0
    a = json.loads((root / "approval.json").read_text())
    assert a["budget_guard"] == 30.0 and [c["test"] for c in a["cuts"]] == ["T9"]
    assert _dry(root, "approve", "--turns", "8") == 0
    assert json.loads((root / "approval.json").read_text())["budget_guard"] == 30.0
    assert _dry(root, "approve", "--turns", "8", "--budget", "10") == 3
    assert json.loads((root / "approval.json").read_text())["budget_guard"] == 30.0
    assert _dry(root, "approve", "--turns", "8", "--budget", "48") == 0
    a = json.loads((root / "approval.json").read_text())
    assert a["budget_guard"] == 48.0 and a["cuts"] == []
