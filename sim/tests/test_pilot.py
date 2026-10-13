"""Offline checks for the v5 pilot driver (spec §9.2), main.py (one cell, §9.3) and the
charter-rating script.

Everything runs with stub models. Real-mode paths are exercised only up to their
guards, with the engine replaced by a fake that fails the test if it is ever
constructed: no test can reach a provider even when sim/.env holds keys.
"""

import json
from pathlib import Path

import pytest

import main as main_mod
import pilot
import rate_charters
from core.config import CONFIG_DIR, load_json
from core import costs, llm
from core.costs import BudgetExceeded, configure, get_tracker
from core.engine import RunAborted
from core.llm import FatalAPIError, register_stub
from core.policies import POLICIES

PILOT_FILE = Path(pilot.__file__).resolve().parent / "config" / "pilot.json"
LAB_KEYS = ("anthropic", "openai", "gdm", "meta", "xai")


@pytest.fixture(autouse=True)
def _scratch(tmp_path, monkeypatch):
    """Default pilot/run dirs and the spend ledger point at scratch space."""
    monkeypatch.setattr(pilot, "PILOT_DATA", tmp_path / "pilot_default")
    monkeypatch.setattr(main_mod, "RUNS_DIR", tmp_path / "runs_default")
    monkeypatch.setattr(costs, "DEFAULT_SPEND_FILE", tmp_path / "spend.json")
    configure()
    yield


class _NoEngine:
    """Stands in for SimulationEngine on real-mode paths: constructing it fails the test."""
    def __init__(self, *a, **k):
        raise AssertionError("a real-mode guard let the engine start")


def _dry(root, *argv):
    """pilot.main in dry-run mode under `root` (its own spend ledger)."""
    return pilot.main(list(argv) + ["--dry-run", "--output", str(root), "--spend-file", str(root / "spend.json")])


def _rec(root, test, run="run01"):
    return json.loads((root / test / f"{test}-{run}.json").read_text())


def _summary(root, test):
    return json.loads((root / test / f"pilot_summary_{test}.json").read_text())


# ---------------------------------------------------------------------------
# config/pilot.json (spec §9.2)
# ---------------------------------------------------------------------------

def test_pilot_config_presets_order_and_budget():
    data = load_json(PILOT_FILE)
    assert data["budget"] == 50 and data["budget_guard"] == 48
    assert data["order"] == ["priced", "T6", "T5", "T4", "T9"]
    assert set(data["presets"]) == set(data["order"])
    assert data["never_cut"] == ["priced", "T6", "T5", "T4"]
    assert [c["test"] for c in data["cuts"]] == ["T9", "T4"]
    assert data["cuts"][0]["drop"] is True and data["cuts"][1]["interview_turns"] == []
    assert data["t7_turns"] == [8, 12]
    for name in data["order"]:
        p = pilot.resolve_preset(data, name)
        assert p["fog"] == "F3" and p["condition"] == "A" and p["choose_disposition"] is True
        assert p["disposition_rating"] is True, f"{name}: T2 rating on every pilot run"
        assert p["grand_jury"] is (name == "T6"), "the rubric runs on the T6 run only (T8)"
        for seat, policy in (p.get("scripted_seats") or {}).items():
            assert seat in LAB_KEYS and policy in POLICIES
    # Removed in v5: T0 gate, T1a/T1b staging, the payoff ladder, T5false, T6neutral, the probe,
    # debriefs, F0-F2 and --brief.
    text = PILOT_FILE.read_text()
    for gone in ('"T0"', '"T1a"', '"T1b"', "ladder", "rung", "T5false", "T6neutral", '"debrief"',
                 '"brief"', "attribution_probe", '"F2"'):
        assert gone not in text, gone


def test_pilot_presets_match_the_spec_rows():
    data = load_json(PILOT_FILE)
    priced = pilot.resolve_preset(data, "priced")
    assert (priced["scenario"], priced["a2a"], priced["turns"]) == ("S1", "merged", 8)
    assert priced["swap"] == "anthropic:meta" and priced["interview_turns"] == "all" and priced["hard_stop"]
    t6 = pilot.resolve_preset(data, "T6")
    assert t6["swap"] == "openai:gdm" and t6["interview_turns"] == "all" and t6["grand_jury"]
    t5 = pilot.resolve_preset(data, "T5")
    assert (t5["scenario"], t5["a2a"], t5["turns"], t5["event_turn"]) == ("S1", "merged", 4, 2)
    assert t5["scripted_seats"] == {"xai": "t5_scripted"} and t5["interview_turns"] == []
    assert t5["interventions"] == [{"turn": 2, "kind": "plant_intrusion", "intruder": "xai",
                                    "target": "openai", "lead_to": ["openai", "gdm"]}]
    t4 = pilot.resolve_preset(data, "T4")
    assert (t4["scenario"], t4["a2a"]) == ("S2", "separate") and t4["interview_turns"] == [3, 4, 5, "final"]
    t9 = pilot.resolve_preset(data, "T9")
    assert (t9["scenario"], t9["a2a"]) == ("S2", "merged") and t9["interview_turns"] == []


def test_resolve_preset_applies_t7_and_the_approvals_cuts():
    data = load_json(PILOT_FILE)
    approval = {"turns": 12, "cuts": [{"test": "T9", "drop": True}, {"test": "T4", "interview_turns": []}]}
    assert pilot.resolve_preset(data, "T6", approval)["turns"] == 12
    assert pilot.resolve_preset(data, "T5", approval)["turns"] == 4          # T7 never lengthens T5
    assert pilot.resolve_preset(data, "T9", approval)["cut"] is True
    t4 = pilot.resolve_preset(data, "T4", approval)
    assert t4["cut"] is False and t4["interview_turns"] == [] and t4["turns"] == 12
    with pytest.raises(SystemExit):
        pilot.resolve_preset(data, "T1b")


def test_parse_swap():
    assert pilot.parse_swap("anthropic:meta") == ("anthropic", "meta")
    assert pilot.parse_swap(None) is None
    for bad in ("meta", "meta:meta", "meta:nope"):
        with pytest.raises(SystemExit):
            pilot.parse_swap(bad)


# ---------------------------------------------------------------------------
# Plan and re-pricing (§9.2 Budget)
# ---------------------------------------------------------------------------

def test_estimate_plan_and_cut_order(tmp_path):
    """Before the priced run: assumed and heavy estimates; the heavy column needs T9 cut."""
    data = load_json(PILOT_FILE)
    plan = pilot.build_plan(data, tmp_path)
    assert plan["basis"] == "estimate" and plan["columns"] == ["assumed", "heavy"]
    assumed, heavy = plan["by_column"]["assumed"], plan["by_column"]["heavy"]
    assert assumed["total"] == pytest.approx(1.75 + 6.5 + 7.85 + 2.4 + 10.6 + 4.9) and not assumed["cuts"]
    assert [c["test"] for c in heavy["cuts"]] == ["T9"] and heavy["total"] <= 48 and not heavy["stop"]
    # 12 turns: the heavy column cuts T9, then T4's interview, and still exceeds the guard -> stop.
    plan = pilot.build_plan(data, tmp_path, turns=12)
    heavy = plan["by_column"]["heavy"]
    assert [c["test"] for c in heavy["cuts"]] == ["T9", "T4"] and heavy["stop"]
    assert next(s for s in plan["steps"] if s["test"] == "T5")["turns"] == 4
    lines = "\n".join(pilot.plan_lines(plan))
    assert "OVER the guard after every cut: stop and agree on the next step" in lines


def _fake_summary(root, test, inputs=None, turns=8, cost=0.0):
    (root / test).mkdir(parents=True, exist_ok=True)
    (root / test / f"pilot_summary_{test}.json").write_text(json.dumps(
        {"test": test, "status": "completed", "runs_completed": 1, "turns": turns,
         "inputs": inputs or {}, "run_cost_usd": cost}))


def test_measured_plan_reprices_from_the_priced_run(tmp_path):
    """After the priced run the plan is priced from its measured inputs; spent = the ledger."""
    data = load_json(PILOT_FILE)
    inputs = {"actor_first_turn": 1.0, "actor_later_turn": 0.5, "interview_per_turn": 0.1,
              "macro_per_review": 0.2, "disposition_rating": 0.3,
              "seat_share": {"claude-opus-5-5": 0.2, "gpt-6-astra": 0.5, "gemini-3.1-pro": 0.1,
                             "muse-spark-1.3": 0.1, "grok-4.7": 0.1}}
    _fake_summary(tmp_path, "priced", inputs, cost=6.0)
    plan = pilot.build_plan(data, tmp_path, turns=8, spent=8.0)
    assert plan["basis"] == "measured" and plan["columns"] == ["measured"]
    t6 = next(s for s in plan["steps"] if s["test"] == "T6")["costs"]["measured"]
    # 1.0 + 7 x 0.5 + 8 interviews x 0.1 + 2 reviews x 0.2 + T2 0.3 + Grand Jury (estimated) 1.35
    assert t6 == pytest.approx(1.0 + 3.5 + 0.8 + 0.4 + 0.3 + 1.35)
    t5 = next(s for s in plan["steps"] if s["test"] == "T5")["costs"]["measured"]
    assert t5 == pytest.approx((1.0 + 3 * 0.5) * 0.8 + 0.2 + 0.3)        # 4 turns, 4 model seats, 1 review
    t4 = next(s for s in plan["steps"] if s["test"] == "T4")["costs"]["measured"]
    a2a = 0.5 * costs.A2A_TO_ACTOR_RATIO
    assert t4 == pytest.approx(1.0 + 3.5 + 8 * a2a + 4 * 0.1 + 0.4 + 0.3, abs=1e-2)   # a2a rounded to 4 dp
    assert plan["by_column"]["measured"]["spent"] == 8.0 and not plan["stop"]
    assert "grand_jury" in plan["inputs"]["estimated"]
    # A ledger near the guard: cut T9, then T4's interview, then stop.
    plan = pilot.build_plan(data, tmp_path, turns=8, spent=48.0 - t6 - t5 - t4 + 0.1)
    assert [c["test"] for c in plan["cuts"]] == ["T9", "T4"] and not plan["stop"]
    plan = pilot.build_plan(data, tmp_path, turns=8, spent=40.0)
    assert plan["stop"]


# ---------------------------------------------------------------------------
# Dry-run walkthrough: every preset end to end with stub providers
# ---------------------------------------------------------------------------

def test_dry_walkthrough_of_the_whole_pilot(tmp_path, capsys):
    root = tmp_path / "pilot"
    assert _dry(root, "priced") == 0
    out = capsys.readouterr().out
    assert "HARD STOP after the priced run" in out and "approve --turns 8" in out
    share = (root / "priced" / "share.txt").read_text()
    assert "priced run measured cost: $0.00" in share and "T7 run length 12 turns" in share
    assert "first intrusion or first report" in share
    rec = _rec(root, "priced")
    cfg = rec["config"]
    assert (cfg["scenario"], cfg["condition"], cfg["fog"], cfg["a2a_mode"], cfg["turns"]) == \
        ("S1", "A", "F3", "merged", 8)
    assert cfg["rotation"] == "anthropic:meta" and cfg["interview_turns"] == list(range(1, 9))
    assert cfg["run_meta"]["pilot"] is True and cfg["choose_disposition"] is True
    assert all(set(t["interviews"]) == set(LAB_KEYS) for t in rec["turns"])
    assert "grand_jury" not in rec["final"]                                    # no rubric in the priced run
    assert (root / pilot.SHEET_NAME).exists()

    # The hard stop: nothing else runs before the approval.
    assert _dry(root, "T6") == 2
    assert "hard stop" in capsys.readouterr().out
    assert _dry(root, "approve") == 2                                          # needs --turns 8|12
    assert _dry(root, "approve", "--turns", "8") == 0
    approval = json.loads((root / pilot.APPROVAL_NAME).read_text())
    assert approval["turns"] == 8 and approval["cuts"] == []

    assert _dry(root, "T6") == 0
    rec = _rec(root, "T6")
    assert rec["config"]["rotation"] == "openai:gdm" and rec["config"]["turns"] == 8
    assert rec["final"]["grand_jury"]["juror"] == "stub:kimi-grand"           # the rubric run
    assert _dry(root, "T5") == 0
    rec = _rec(root, "T5")
    cfg = rec["config"]
    assert (cfg["turns"], cfg["event_turn"], cfg["interview_turns"]) == (4, 2, [])
    assert cfg["seat_policies"] == {"xai": "t5_scripted"}
    planted = [e for e in rec["ledger"] if e["draws"].get("planted")]
    assert planted and planted[0]["committers"] == ["xai"] and planted[0]["targets"] == ["openai"]
    assert planted[0]["turn"] == 2 and planted[0]["succeeded"] and not planted[0]["exposed"]
    assert "xai" not in rec["final"]["t2_ratings"]                            # scripted seat: not rated
    assert _dry(root, "T4") == 0
    cfg = _rec(root, "T4")["config"]
    assert (cfg["scenario"], cfg["a2a_mode"], cfg["interview_turns"]) == ("S2", "separate", [3, 4, 5, 8])
    assert _dry(root, "T9") == 0
    cfg = _rec(root, "T9")["config"]
    assert (cfg["scenario"], cfg["a2a_mode"], cfg["interview_turns"]) == ("S2", "merged", [])
    out = capsys.readouterr().out
    assert "T9: merged arm 0 match(es)" in out and "T4: no solicitation flagged" in out
    for test in ("priced", "T6", "T5", "T4", "T9"):
        s = _summary(root, test)
        assert s["status"] == "completed" and s["dry_run"] and s["usage"]["by_actor_model"]
        assert (root / test / f"review_{test}.md").exists()
    assert "T1" in _summary(root, "T6")["decisions"] and "T5" in _summary(root, "T5")["decisions"]
    ratings = json.loads((root / "disposition_ratings.dry.json").read_text())
    assert [r["test"] for r in ratings["runs"]] == ["priced", "T6", "T5", "T4", "T9"]
    # Dry runs never touch the real pilot directory or the pooled real ratings.
    assert not (pilot.PILOT_DATA / "disposition_ratings.json").exists()


def test_all_stops_at_the_hard_stop_then_runs_the_rest(tmp_path, capsys):
    root = tmp_path / "pilot"
    assert _dry(root, "all") == 0
    assert pilot.test_done(root, "priced") and not (root / "T6").exists()
    assert "HARD STOP" in capsys.readouterr().out
    assert _dry(root, "all") == 0
    assert "stopping before T6" in capsys.readouterr().out
    # An approval that cut T9: `all` runs T6, T5 and T4 and skips T9.
    (root / pilot.APPROVAL_NAME).write_text(json.dumps({"turns": 8, "cuts": [{"test": "T9", "drop": True}]}))
    assert _dry(root, "all") == 0
    assert all(pilot.test_done(root, t) for t in ("T6", "T5", "T4")) and not (root / "T9").exists()
    assert "T9: cut at approval" in capsys.readouterr().out
    assert _dry(root, "T9") == 2
    assert "cut at approval" in capsys.readouterr().out


def test_approve_twelve_turns_lengthens_every_run_but_t5(tmp_path):
    root = tmp_path / "pilot"
    assert _dry(root, "priced") == 0
    assert _dry(root, "approve", "--turns", "12") == 0
    assert _dry(root, "T6") == 0 and _dry(root, "T5") == 0
    assert _rec(root, "T6")["config"]["turns"] == 12
    assert _rec(root, "T5")["config"]["turns"] == 4
    assert _rec(root, "T6")["config"]["run_meta"]["t7_turns"] == 12


def test_approve_refused_when_the_plan_exceeds_the_guard(tmp_path, monkeypatch, capsys):
    root = tmp_path / "pilot"
    assert _dry(root, "priced") == 0
    data = pilot.load_pilot()
    for name in data["presets"]:
        data["presets"][name]["est_cost"] = [20.0, 30.0]
    monkeypatch.setattr(pilot, "load_pilot", lambda: data)
    assert _dry(root, "approve", "--turns", "8") == pilot.EXIT_OVER_GUARD
    assert "APPROVAL REFUSED" in capsys.readouterr().out
    assert not (root / pilot.APPROVAL_NAME).exists()
    # The team may move the guard (--budget): the plan is then re-cut against it.
    assert _dry(root, "approve", "--turns", "8", "--budget", "200") == 0
    assert json.loads((root / pilot.APPROVAL_NAME).read_text())["cuts"] == []


def test_order_is_enforced_and_the_t6_rerun(tmp_path, capsys):
    root = tmp_path / "pilot"
    assert _dry(root, "priced") == 0 and _dry(root, "approve", "--turns", "8") == 0
    assert _dry(root, "T5") == 2
    assert "run the earlier tests first (T6)" in capsys.readouterr().out
    assert _dry(root, "T5", "--force-order") == 0
    assert _dry(root, "T5", "--rerun") == 2                                    # --rerun is T6's only
    assert _dry(root, "T6", "--rerun") == 2                                    # needs a finished T6
    assert _dry(root, "T6") == 0 and _dry(root, "T6", "--rerun") == 0
    rec = _rec(root, "T6", "run02")
    assert rec["config"]["seed"] == 1 and rec["config"]["run_meta"]["run_id"] == "T6-run02"


def test_skip_completed_and_post_run_only(tmp_path, capsys):
    root = tmp_path / "pilot"
    assert _dry(root, "priced") == 0
    assert _dry(root, "priced", "--skip-completed") == 0
    assert "already completed (skipped)" in capsys.readouterr().out
    # Drop two seats' ratings: --post-run-only asks only for them.
    path = root / "disposition_ratings.dry.json"
    pooled = json.loads(path.read_text())
    pooled["ratings"]["gdm"], pooled["ratings"]["meta"] = [], []
    path.write_text(json.dumps(pooled))
    assert _dry(root, "priced", "--post-run-only") == 0
    out = capsys.readouterr().out
    assert "T2 ratings (gdm, meta)" in out and "2 seat(s) rated" in out
    assert len(json.loads(path.read_text())["ratings"]["gdm"]) == 1
    assert _dry(root, "priced", "--post-run-only") == 0
    assert "complete" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Real-mode guards (the engine never starts) and stops
# ---------------------------------------------------------------------------

def _approve_real(root):
    for t in ("priced",):
        _fake_summary(root, t)
    (root / pilot.APPROVAL_NAME).write_text(json.dumps({"turns": 8, "cuts": []}))


def test_real_pilot_guards(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pilot, "SimulationEngine", _NoEngine)
    root = tmp_path / "real"
    args = ["--output", str(root), "--spend-file", str(tmp_path / "s.json")]
    monkeypatch.setattr(pilot, "placeholder_labs", lambda: ["meta"])
    assert pilot.main(["priced"] + args) == 2
    assert "placeholders for meta" in capsys.readouterr().out
    monkeypatch.setattr(pilot, "placeholder_labs", lambda: [])
    monkeypatch.setattr(llm, "preflight", lambda models, providers=None: ["ANTHROPIC_API_KEY missing"])
    assert pilot.main(["priced"] + args) == 2
    assert "Preflight failed; nothing was called" in capsys.readouterr().out
    # T6 needs the few-shot examples from the priced run's hand scores (T8).
    _approve_real(root)
    monkeypatch.setattr(pilot.jury_mod, "load_examples", lambda *a, **k: [])
    assert pilot.main(["T6"] + args) == 2
    assert "few-shot examples" in capsys.readouterr().out
    assert pilot.main(["T6", "--allow-no-examples"] + args) == 2              # then the preflight stops it
    assert "Preflight failed" in capsys.readouterr().out


@pytest.mark.parametrize("exc, code, status", [
    (BudgetExceeded("guard"), 1, "halted_budget"),
    (RunAborted("fatal", None), 2, "aborted"),
    (FatalAPIError("boom", provider="openai", model="m", status=500), 2, "aborted"),
    (ValueError("bug"), 2, "crashed"),
])
def test_a_stopped_run_writes_its_summary_and_is_not_done(tmp_path, monkeypatch, exc, code, status):
    def boom(self):
        raise exc
    monkeypatch.setattr(pilot.SimulationEngine, "run", boom)
    root = tmp_path / "pilot"
    assert _dry(root, "priced") == code
    s = _summary(root, "priced")
    assert s["status"] == status and s["runs_completed"] == 0 and s["aborted"]["error"]
    assert not pilot.test_done(root, "priced")
    assert not (root / "priced" / "share.txt").exists()                       # no hard stop on a failure


def test_budget_stop_in_the_t2_rating_keeps_what_was_rated(tmp_path, monkeypatch):
    calls = {"n": 0}

    def rater(system, user):
        calls["n"] += 1
        if calls["n"] == 3:
            raise BudgetExceeded("guard")
        return json.dumps({"disposition": 40, "reason": "x"})
    orig = pilot.install_stubs

    def patched(labs):
        jurors = orig(labs)
        register_stub("kimi-disposition", rater)
        return jurors
    monkeypatch.setattr(pilot, "install_stubs", patched)
    root = tmp_path / "pilot"
    assert _dry(root, "priced") == 1
    pooled = json.loads((root / "disposition_ratings.dry.json").read_text())
    assert sum(len(v) for v in pooled["ratings"].values()) == 2
    assert _summary(root, "priced")["status"] == "halted_budget"


# ---------------------------------------------------------------------------
# Units: ratings pool, usage report
# ---------------------------------------------------------------------------

def test_append_ratings_rules(tmp_path):
    rows = pilot.new_rating_rows()
    rows["ratings"]["gdm"] = [{"run_id": "r1", "test": "priced", "juror": "kimi-k3", "disposition": 40}]
    with pytest.raises(ValueError):
        pilot.append_ratings(tmp_path / "disposition_ratings.json", rows, "priced", "r1", dry_run=True)
    path = tmp_path / "pool.json"
    pilot.append_ratings(path, rows, "priced", "r1", dry_run=False)
    pilot.append_ratings(path, rows, "priced", "r1", dry_run=False)            # a re-run replaces
    assert len(json.loads(path.read_text())["ratings"]["gdm"]) == 1
    more = pilot.new_rating_rows()
    more["ratings"]["meta"] = [{"run_id": "r1", "test": "priced", "juror": "kimi-k3", "disposition": 60}]
    pilot.append_ratings(path, more, "priced", "r1", dry_run=False, merge=True)
    pooled = json.loads(path.read_text())
    assert len(pooled["ratings"]["gdm"]) == 1 and len(pooled["ratings"]["meta"]) == 1


def test_usage_report_tallies_failures_interviews_and_served_ids():
    rec = {"labs": {"openai": {"model": "gpt-6-astra"}},
           "turns": [{"turn": 1, "actors": {"openai": {
               "forfeited": False,
               "attempts": [{"error": "bad json", "stop": "max_tokens", "cost": 0.1, "input_tokens": 10},
                            {"stop": "end", "cost": 0.2, "served_model": "gpt-6-astra-2026-08"}]}},
               "interviews": {"openai": {"skipped": False, "cost": 0.05}}},
               {"turn": 2, "actors": {"openai": {"forfeited": True, "attempts": [
                   {"error": "x", "stop": "refusal", "served_model": "gpt-5"}]}},
                "interviews": {"openai": {"skipped": True, "reason": "no usable proposal"}}}]}
    u = pilot.usage_report([rec], ["r"])
    row = u["by_actor_model"]["gpt-6-astra"]
    assert row["proposal_calls"] == 3 and row["proposal_failed"] == 2
    assert row["proposal_failure_rate"] == pytest.approx(0.667, abs=1e-3)
    assert row["forfeits"] == 1 and row["stop_max_tokens"] == 1 and row["stop_refusal"] == 1
    assert row["interviews"] == 1 and row["interviews_skipped"] == 1
    assert row["cost"] == pytest.approx(0.35)
    assert row["served_mismatch"] == ["gpt-5"]


def test_usage_report_counts_empty_incomplete_and_cut_interviews_apart():
    """A.7 flags: empty answers are counted apart from skips, scripted seats not at all;
    incomplete (output cap) and cut (300-token log cut) are counted separately."""
    rec = {"labs": {"openai": {"model": "gpt-6-astra"}},
           "turns": [{"turn": t, "actors": {"openai": {"forfeited": False, "attempts": [{"stop": "end"}]}},
                      "interviews": {"openai": iv}} for t, iv in enumerate([
               {"answer": "Fine.", "skipped": False, "incomplete": True, "cut": False, "truncated": True,
                "output_tokens": 7, "cost": 0.01},
               {"answer": "Long.", "skipped": False, "incomplete": False, "cut": True, "truncated": True},
               {"answer": "", "skipped": True, "reason": "empty answer (max_tokens)", "retried": True},
               {"answer": "", "skipped": True, "reason": "call failed"},
               {"answer": "", "skipped": True, "reason": "scripted seat"}], start=1)]}
    row = pilot.usage_report([rec], ["r"])["by_actor_model"]["gpt-6-astra"]
    assert row["interviews"] == 2
    assert row["interviews_incomplete"] == 1 and row["interviews_cut"] == 1
    assert row["interviews_empty"] == 1 and row["interviews_skipped"] == 1
    assert row["interviews_retried"] == 1 and row["output_tokens"] == 7


# ---------------------------------------------------------------------------
# main.py: one cell per call (§9.3)
# ---------------------------------------------------------------------------

def test_main_scripted_run_saves_under_default_dir():
    assert main_mod.main(["--policy", "idle", "--turns", "2", "--run-id", "scripted1"]) == 0
    rec = json.loads((main_mod.RUNS_DIR / "policy" / "scripted1" / "scripted1.json").read_text())
    assert rec["config"]["fog"] == "F3" and rec["config"]["run_meta"]["pilot"] is False


def test_main_s1_always_merges_and_parses_interview_turns(tmp_path):
    assert main_mod.parse_interview_turns("3,4,5,final") == [3, 4, 5, "final"]
    assert main_mod.parse_interview_turns("none") == []
    with pytest.raises(Exception):
        main_mod.parse_interview_turns("3,x")
    assert main_mod.main(["--dry-run", "--scenario", "S1", "--a2a", "separate", "--turns", "6",
                          "--interview-turns", "3,4,5,final", "--output", str(tmp_path), "--run-id", "m1"]) == 0
    rec = json.loads((tmp_path / "m1.json").read_text())
    cfg = rec["config"]
    assert cfg["a2a_mode"] == "merged" and cfg["interview_turns"] == [3, 4, 5, 6]
    assert cfg["choose_disposition"] is False and cfg["run_meta"]["cell"] == "S1-A"
    assert rec["final"]["grand_jury"]["juror"] == "stub:kimi-grand"
    # F0-F2 and --brief are gone.
    for gone in (["--fog", "F2"], ["--brief", "neutral"], ["--choose-disposition"]):
        with pytest.raises(SystemExit):
            main_mod.build_parser().parse_args(gone)


def test_main_kin_cell(tmp_path, monkeypatch):
    seen = {}
    orig = main_mod.SimulationEngine.__init__

    def spy(self, *a, **k):
        orig(self, *a, **k)
        seen["engine"] = self
    monkeypatch.setattr(main_mod.SimulationEngine, "__init__", spy)
    assert main_mod.main(["--kin", "--dry-run", "--a2a", "separate", "--turns", "2",
                          "--output", str(tmp_path), "--run-id", "kin1"]) == 0
    rec = json.loads((tmp_path / "kin1.json").read_text())
    cfg = rec["config"]
    assert (cfg["scenario"], cfg["condition"], cfg["a2a_mode"]) == ("S2", "A", "separate")
    assert cfg["run_meta"]["kin"] is True and cfg["run_meta"]["cell"] == "kin-S2-A"
    assert cfg["kin"] is True
    assert {v["model"] for v in rec["labs"].values()} == {"stub:actor_anthropic"}
    from prompts.brief import KIN_INSERT
    eng = seen["engine"]
    assert all(KIN_INSERT in eng.system[k] for k in LAB_KEYS)
    # Seats keep their charters.
    assert rec["labs"]["meta"]["charter_name"] == "Superintelligence Scaling Framework"
    assert main_mod.main(["--kin", "--condition", "C", "--dry-run"]) == 2


def test_main_apply_kin_sets_claude_in_every_seat():
    from core.config import build_labs_and_world, load_world
    labs, _ = build_labs_and_world(load_world(), dispositions={k: 50 for k in LAB_KEYS}, charters=False)
    model = main_mod.apply_kin(labs)
    assert model == "claude-opus-5-5"
    assert {l.model for l in labs} == {"claude-opus-5-5"} and {l.provider for l in labs} == {"anthropic"}
    assert len({l.charter_name for l in labs}) == 5


def test_main_real_run_guards(monkeypatch, capsys):
    monkeypatch.setattr(main_mod, "SimulationEngine", _NoEngine)
    monkeypatch.setattr(main_mod, "load_dispositions", lambda: {})
    assert main_mod.main(["--turns", "1"]) == 2
    assert "config/dispositions.json is missing" in capsys.readouterr().out
    monkeypatch.setattr(main_mod, "load_dispositions", lambda: {k: 40 for k in LAB_KEYS} | {"meta": 140})
    assert main_mod.main(["--turns", "1"]) == 2
    assert "meta=140" in capsys.readouterr().out
    monkeypatch.setattr(main_mod, "load_dispositions", lambda: {k: 40 for k in LAB_KEYS})
    monkeypatch.setattr(llm, "preflight", lambda models, providers=None: ["XAI_API_KEY missing"])
    assert main_mod.main(["--turns", "1", "--allow-placeholder-values"]) == 2
    assert "Preflight failed" in capsys.readouterr().out


def test_main_any_crash_exits_2(tmp_path, monkeypatch, capsys):
    def boom(self):
        raise ValueError("bug")
    monkeypatch.setattr(main_mod.SimulationEngine, "run", boom)
    assert main_mod.main(["--policy", "idle", "--turns", "1", "--output", str(tmp_path)]) == 2
    assert "Run crashed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# rate_charters.py (unchanged in v5)
# ---------------------------------------------------------------------------

def test_rate_lab_with_stub_writes_valid_ints():
    seen = {}

    def stub(system, user):
        seen["user"] = user
        return json.dumps({"time_horizon": 60.4, "transparency_threshold": 55, "risk_tolerance": 40.6,
                           "democratic_tendency": 45, "rationale": "stub"})

    register_stub("ratetest", stub)
    cfg = load_json(CONFIG_DIR / "labs" / "anthropic.json")
    cfg["model"] = "stub:ratetest"
    values, notes = rate_charters.rate_lab(cfg)
    assert set(values) == set(rate_charters.AXES)
    assert all(isinstance(v, int) and 0 <= v <= 100 for v in values.values())
    assert values["time_horizon"] == 60 and values["risk_tolerance"] == 41
    assert notes == "stub"
    assert '"rationale"' in seen["user"] and '"reasoning"' not in seen["user"]
    assert rate_charters.MAX_TOKENS == 8000


def test_rate_charters_dry_run_is_non_destructive():
    path = CONFIG_DIR / "labs" / "anthropic.json"
    before = path.read_text()
    assert rate_charters.main(["--dry-run", "--lab", "anthropic"]) == 0
    assert path.read_text() == before, "dry-run must not touch the config"


def test_rate_charters_real_mode_preflight_and_budget(monkeypatch):
    monkeypatch.setattr(rate_charters, "preflight", lambda models, providers=None: ["no key"])
    monkeypatch.setattr(rate_charters, "rate_lab", lambda *a, **k: pytest.fail("called a model"))
    assert rate_charters.main(["--lab", "meta"]) == 2
    args = rate_charters.build_parser().parse_args([])
    assert args.budget is None and args.headroom is None


def test_rate_charters_guard_is_the_pilot_guard(tmp_path, monkeypatch):
    assert rate_charters.guard_for(57.5, None, None) == load_json(PILOT_FILE)["budget_guard"]
    assert rate_charters.guard_for(57.5, None, 10.0) == 67.5
    assert rate_charters.guard_for(57.5, 80.0, 10.0) == 80.0
    spend = tmp_path / "s.json"
    spend.write_text(json.dumps({"total_usd": 42.0, "calls": 3, "by_model": {}}))
    seen = {}
    monkeypatch.setattr(rate_charters, "preflight", lambda models, providers=None: [])

    def fake_rate(cfg, run_id="rate_charters"):
        seen["budget"] = get_tracker().budget
        raise BudgetExceeded("stop here")
    monkeypatch.setattr(rate_charters, "rate_lab", fake_rate)
    assert rate_charters.main(["--lab", "meta", "--spend-file", str(spend)]) == 1
    assert seen["budget"] == pytest.approx(48.0)
    assert rate_charters.main(["--lab", "meta", "--spend-file", str(spend), "--headroom", "10"]) == 1
    assert seen["budget"] == pytest.approx(52.0)


def test_rate_charters_unusable_rating_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rate_charters, "preflight", lambda models, providers=None: [])
    written = []
    monkeypatch.setattr(rate_charters, "write_values", lambda key, *a: written.append(key))

    def fake_rate(cfg, run_id="rate_charters"):
        if cfg["key"] == "openai":
            raise rate_charters.UnusableRating("openai: no usable charter rating after 3 attempt(s)")
        return {a: 50 for a in rate_charters.AXES}, "ok"

    monkeypatch.setattr(rate_charters, "rate_lab", fake_rate)
    assert rate_charters.main(["--spend-file", str(tmp_path / "s.json")]) == 2
    out = capsys.readouterr().out
    assert "UNUSABLE rating for openai" in out and "python rate_charters.py --lab openai" in out
    assert "openai" not in written and len(written) == 4


def test_rate_lab_raises_unusable(monkeypatch):
    monkeypatch.setattr(rate_charters, "complete_json",
                        lambda *a, **k: (None, [{"error": "axis 'risk_tolerance' must be a number 0-100"}]))
    monkeypatch.setattr(rate_charters, "load_charter", lambda cfg: "text")
    with pytest.raises(rate_charters.UnusableRating, match="risk_tolerance"):
        rate_charters.rate_lab({"key": "meta", "model": "m", "charter_name": "c"})


def test_rate_charters_fatal_still_exits_2(tmp_path, monkeypatch):
    monkeypatch.setattr(rate_charters, "preflight", lambda models, providers=None: [])

    def fake_rate(cfg, run_id="rate_charters"):
        raise FatalAPIError("auth", provider="openai", model="m", status=401)
    monkeypatch.setattr(rate_charters, "rate_lab", fake_rate)
    assert rate_charters.main(["--lab", "meta", "--spend-file", str(tmp_path / "s.json")]) == 2


# ---------------------------------------------------------------------------
# Wave-4 fixes: the moved guard, re-run limits, plan display, T8 re-score
# ---------------------------------------------------------------------------

class _BudgetProbe:
    """Records the guard the tracker enforces when a real-mode engine would start."""
    seen = []

    def __init__(self, *a, **k):
        _BudgetProbe.seen.append(get_tracker().budget)
        raise RuntimeError("probe: the engine never runs in this test")


def test_approve_budget_is_stored_and_enforced_in_later_tests(tmp_path, monkeypatch, capsys):
    """CR #5 / E2E 3: approve --budget X stores X; later tests and `plan` use it; shown exactly."""
    root = tmp_path / "real"
    args = ["--output", str(root), "--spend-file", str(tmp_path / "s.json")]
    _fake_summary(root, "priced")
    assert pilot.main(["approve", "--turns", "8", "--budget", "37.6"] + args) == 0
    assert json.loads((root / pilot.APPROVAL_NAME).read_text())["budget_guard"] == 37.6
    assert "guard $37.60" in capsys.readouterr().out
    assert pilot.main(["plan"] + args) == 0
    assert "guard $37.60 of $50.00" in capsys.readouterr().out
    assert pilot.main(["plan", "--budget", "30"] + args) == 0                 # plan --budget is honoured
    assert "guard $30.00" in capsys.readouterr().out
    monkeypatch.setattr(pilot, "SimulationEngine", _BudgetProbe)
    monkeypatch.setattr(pilot, "placeholder_labs", lambda: [])
    monkeypatch.setattr(llm, "preflight", lambda models, providers=None: [])
    monkeypatch.setattr(pilot.jury_mod, "load_examples", lambda *a, **k: [{"x": 1}])
    _BudgetProbe.seen.clear()
    pilot.main(["T6"] + args)
    assert _BudgetProbe.seen == [37.6]                                       # not pilot.json's 48
    pilot.main(["T6", "--budget", "40"] + args)
    assert _BudgetProbe.seen[-1] == 40.0


def test_a_completed_test_is_refused_unless_skipped_or_forced(tmp_path, capsys):
    """E2E 2: a finished test is never silently re-run (and re-paid)."""
    root = tmp_path / "pilot"
    assert _dry(root, "priced") == 0
    assert _dry(root, "priced") == 2
    out = capsys.readouterr().out
    assert "already completed" in out and "--force" in out
    assert _dry(root, "priced", "--skip-completed") == 0
    assert _dry(root, "priced", "--force") == 0
    assert _summary(root, "priced")["status"] == "completed"


def test_t6_rerun_allowed_once_and_never_a_third_run(tmp_path, capsys):
    root = tmp_path / "pilot"
    assert _dry(root, "priced") == 0
    assert _dry(root, "approve", "--turns", "8") == 0
    assert _dry(root, "T6") == 0
    assert _dry(root, "T6") == 2                                            # completed: refused
    assert _dry(root, "T6", "--rerun") == 0
    assert _dry(root, "T6", "--rerun") == 2
    assert "its one re-run" in capsys.readouterr().out
    assert _dry(root, "T6", "--rerun", "--skip-completed") == 0
    assert _dry(root, "T6", "--rerun", "--force") == 0                      # redoes run02 only
    assert not (root / "T6" / "T6-run03.json").exists()


def test_unknown_test_exits_2_with_a_clear_message(tmp_path, capsys):
    assert _dry(tmp_path / "pilot", "T7") == 2
    assert "Unknown test 'T7'" in capsys.readouterr().out


def test_plan_shows_dropped_interview_and_measured_cost_of_done_tests(tmp_path, capsys):
    """E2E 10 / DOC 8."""
    root = tmp_path / "pilot"
    assert _dry(root, "priced") == 0
    assert _dry(root, "approve", "--turns", "12", "--budget", "37.6") == 0
    capsys.readouterr()
    assert _dry(root, "plan") == 0
    out = capsys.readouterr().out
    t4 = next(line for line in out.splitlines() if line.strip().startswith("T4"))
    assert "iv dropped" in t4
    priced = next(line for line in out.splitlines() if line.strip().startswith("priced"))
    assert priced.split()[-2:] == ["0.00", "0.00"]                         # measured ($0 dry), not 6.50
    assert "T4 drops its interview (-$0.00)" not in out                    # not applied twice
    plan = pilot.build_plan(dict(pilot.load_pilot(), budget_guard=37.6), root,
                            approval=pilot.read_approval(root))
    assert next(s for s in plan["steps"] if s["test"] == "T4")["interview_dropped"]


def test_rescore_grand_jury_reasks_the_jury_once_on_the_t6_record(tmp_path, monkeypatch, capsys):
    """A2 M12 (T8): after the examples are revised, the Grand Jury is re-asked on the saved
    T6 record; the seats are never re-run; the old verdict is kept; once only."""
    root = tmp_path / "pilot"
    assert _dry(root, "priced") == 0
    assert _dry(root, "approve", "--turns", "8") == 0
    assert _dry(root, "T6") == 0
    before = _rec(root, "T6")

    def no_seats(self):
        raise AssertionError("the seats were run again")
    monkeypatch.setattr(pilot.SimulationEngine, "run", no_seats)
    assert _dry(root, "T5", "--rescore-grand-jury") == 2
    assert _dry(root, "T6", "--rescore-grand-jury") == 0
    after = _rec(root, "T6")
    assert after["turns"] == before["turns"]
    assert after["final"]["grand_jury_previous"]["juror"] == before["final"]["grand_jury"]["juror"]
    assert after["final"]["grand_jury"]["collected_post_run"]
    assert _dry(root, "T6", "--rescore-grand-jury") == 2
    assert "already re-scored once" in capsys.readouterr().out


def test_t6_rerun_merges_its_rows_into_the_coded_sheet(tmp_path):
    """DOC 3: the coding sheet already being coded, T6's re-run adds its rows and keeps the codes."""
    import csv
    root = tmp_path / "pilot"
    assert _dry(root, "priced") == 0
    assert _dry(root, "approve", "--turns", "8") == 0
    assert _dry(root, "T6") == 0
    sheet = root / pilot.SHEET_NAME
    rows = list(csv.DictReader(open(sheet)))
    n = len(rows)
    rows[0].update(score_only="1", notes="coded")
    with open(sheet, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    assert _dry(root, "T6", "--rerun") == 0
    after = list(csv.DictReader(open(sheet)))
    assert len(after) > n and {r["run_id"] for r in after} >= {"T6-run02"}
    first = next(r for r in after if (r["run_id"], r["seat"], r["turn"], r["source"])
                 == (rows[0]["run_id"], rows[0]["seat"], rows[0]["turn"], rows[0]["source"]))
    assert first["score_only"] == "1" and first["notes"] == "coded"
    s = json.loads((root / "T6" / "pilot_summary_T6-rerun.json").read_text())   # the re-run's own (BUG-8)
    assert "new row(s) merged, codes already entered kept" in s["decisions"]["T6"]
    assert _summary(root, "T6")["run_id"] == "T6-run01"


def test_t4_summary_reads_the_hindcast_and_the_macro_jury(tmp_path):
    """A2 M9 and M7: T4's summary prints the turns 1-2 hindcast; every test the MacroJury's first review."""
    root = tmp_path / "pilot"
    assert _dry(root, "priced") == 0
    assert "first review (read for slant" in _summary(root, "priced")["decisions"]["MacroJury"]
    assert _dry(root, "approve", "--turns", "8") == 0
    for t in ("T6", "T5", "T4"):
        assert _dry(root, t) == 0
    d = _summary(root, "T4")["decisions"]
    assert "Hindcast" in d and "vs real +4.5" in d["Hindcast"] and "not a gate" in d["Hindcast"]
    assert "Hindcast" not in _summary(root, "T6")["decisions"]           # rotated seeds
