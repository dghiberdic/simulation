"""Final end-to-end pass (README followed start to finish, dry runs only).

Passing tests pin README behaviour that worked; xfail(strict) tests pin problems
found by the pass, so they flip to XPASS (a failure) once fixed and the marker
must then be removed.
"""

import csv
import json
import re

import pytest

import campaign as campaign_mod
import main as main_mod
import pilot
from core import costs, llm
from core.config import build_labs_and_world, load_world
from core.costs import configure
from core.engine import mandate_violation
from tools import contrasts as contrasts_mod

CANON = ("anthropic", "openai", "gdm", "meta", "xai")   # the Situation note's lab order


@pytest.fixture(autouse=True)
def _scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "PILOT_DATA", tmp_path / "pilot_default")
    monkeypatch.setattr(main_mod, "RUNS_DIR", tmp_path / "runs_default")
    monkeypatch.setattr(campaign_mod, "RUNS_DIR", tmp_path / "runs_default")
    monkeypatch.setattr(costs, "DEFAULT_SPEND_FILE", tmp_path / "spend.json")
    configure()
    yield


@pytest.fixture
def calls(monkeypatch):
    log = []
    orig = llm.register_stub

    def reg(name, fn):
        def wrapped(system, user, _n=name, _f=fn):
            out = _f(system, user)
            log.append({"stub": _n, "system": system, "user": user})
            return out
        orig(name, wrapped)

    monkeypatch.setattr(pilot, "register_stub", reg)
    return log


def _dry(root, *argv):
    return pilot.main(list(argv) + ["--dry-run", "--output", str(root), "--spend-file", str(root / "spend.json")])


# ---------------------------------------------------------------------------
# README steps that worked
# ---------------------------------------------------------------------------

def test_refused_approval_exits_3_and_keeps_the_previous_approval(tmp_path):
    root = tmp_path / "pilot"
    assert _dry(root, "priced") == 0
    assert _dry(root, "approve", "--turns", "8", "--budget", "37.6") == 0
    assert _dry(root, "approve", "--turns", "12", "--budget", "20") == 3
    appr = json.loads((root / "approval.json").read_text())
    assert appr["turns"] == 8 and appr["budget_guard"] == 37.6
    # a later approve without --budget keeps the stored guard (README is silent on this)
    assert _dry(root, "approve", "--turns", "8") == 0
    assert json.loads((root / "approval.json").read_text())["budget_guard"] == 37.6


def test_mandate_filter_examples():
    labs, _ = build_labs_and_world(load_world(), charters=False)
    for text in ("Labs must report any intrusion to the Allocator.",
                 "OpenAI must share its compute with smaller labs.",
                 "Labs must avoid hacking or unauthorised access to rival tenancies.",
                 "Google must slow down.", "The Allocator will inspect all tenancies."):
        assert mandate_violation(text, labs), text
    assert mandate_violation("Labs must prioritise long-term safety research over speed.", labs) is None


def test_main_s1_separate_silently_merges(tmp_path):
    out = tmp_path / "s1"
    assert main_mod.main(["--dry-run", "--scenario", "S1", "--a2a", "separate", "--output", str(out),
                          "--run-id", "x", "--no-grand-jury", "--no-macro-jury"]) == 0
    assert json.loads((out / "x.json").read_text())["config"]["a2a_mode"] == "merged"


# ---------------------------------------------------------------------------
# Problems found (xfail strict: remove the marker once fixed)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="scripted --policy runs (README 'Verify offline') count as main runs")
def test_policy_runs_are_not_main_runs(tmp_path):
    out = tmp_path / "runs"
    assert main_mod.main(["--policy", "greedy", "--scenario", "S1", "--output", str(out / "p"),
                          "--run-id", "p"]) == 0
    kept, _ = contrasts_mod.load_main_runs([str(out)])
    assert kept == []


@pytest.mark.xfail(strict=True, reason="juror transcripts list seats in the fixed lab order, revealing the letter key")
def test_grand_jury_seat_order_does_not_reveal_letters(tmp_path, calls):
    out = tmp_path / "c"
    assert main_mod.main(["--dry-run", "--scenario", "S1", "--output", str(out), "--run-id", "x",
                          "--no-macro-jury"]) == 0
    gj = next(c for c in calls if c["stub"] == "kimi-grand")["user"]
    listed = re.search(r"Five US labs \((Seat [A-E]), (Seat [A-E]), (Seat [A-E]), (Seat [A-E]), and (Seat [A-E])\)",
                       gj).groups()
    letters = json.loads((out / "x.json").read_text())["final"]["grand_jury"]["letters"]
    assert list(listed) != [f"Seat {letters[k]}" for k in CANON]


@pytest.mark.xfail(strict=True, reason="mandate filter is keyword-based; paraphrases about the flaw pass")
def test_mandate_filter_catches_flaw_paraphrase():
    labs, _ = build_labs_and_world(load_world(), charters=False)
    assert mandate_violation("Labs must not exploit the booking-system flaw.", labs)


@pytest.mark.xfail(strict=True, reason="pilot prints 'T6: T6: ...' after a T6 re-run")
def test_t6_rerun_line_has_one_prefix(tmp_path, capsys):
    root = tmp_path / "pilot"
    assert _dry(root, "priced") == 0
    assert _dry(root, "approve", "--turns", "8") == 0
    assert _dry(root, "T6") == 0
    sheet = root / "T6_coding_sheet.csv"          # the coders have started
    with sheet.open(newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["score_only"] = "1"
    with sheet.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    capsys.readouterr()
    assert _dry(root, "T6", "--rerun") == 0
    assert "T6: T6:" not in capsys.readouterr().out


@pytest.mark.xfail(strict=True, reason="the turn-6 MacroJury review sees only the review month, not turns 3-5")
def test_macro_review_sees_the_months_since_the_last_review(tmp_path, calls):
    out = tmp_path / "m"
    assert main_mod.main(["--dry-run", "--scenario", "S1", "--output", str(out), "--run-id", "x",
                          "--no-grand-jury"]) == 0
    second = [c for c in calls if c["stub"] == "kimi-macro"][1]["user"]
    assert "November 2026" in second     # the event month falls between the reviews
