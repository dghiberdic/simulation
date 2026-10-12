"""Main-run schedule (campaign.py, spec §9.3) and the re-projection inputs (core/costs.py)."""

import json

import pytest

import campaign
import main as main_mod
from core import costs
from core.costs import kin_seat_scale, measure_runs, project_run, with_fallbacks


@pytest.fixture(autouse=True)
def _scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(costs, "DEFAULT_SPEND_FILE", tmp_path / "spend.json")
    monkeypatch.setattr(main_mod, "RUNS_DIR", tmp_path / "runs_default")
    costs.configure()
    yield


# ---------------------------------------------------------------------------
# core/costs.py: measured inputs and projection
# ---------------------------------------------------------------------------

def _measured_record(run_id="priced-run01", mode="merged", scripted=False):
    def actors(cost):
        a = {"anthropic": {"attempts": [{"cost": cost}]}, "openai": {"attempts": [{"cost": 3 * cost}]}}
        if scripted:
            a["xai"] = {"scripted": True, "attempts": [{"cost": 9.0}]}
        return a
    labs = {"anthropic": {"model": "claude-opus-5-5"}, "openai": {"model": "gpt-6-astra"}}
    if scripted:
        labs["xai"] = {"model": "grok-4.7"}
    return {"config": {"a2a_mode": mode, "run_meta": {"run_id": run_id}},
            "labs": labs,
            "turns": [{"turn": 1, "actors": actors(0.5), "interviews": {}},
                      {"turn": 2, "actors": actors(0.25), "interviews": {}, "macro_jury": {}},
                      {"turn": 3, "actors": actors(0.25)}]}


def test_measure_runs_splits_turn_one_and_purposes():
    rec = _measured_record()
    calls = [{"run_id": "priced-run01", "purpose": p, "cost": c} for p, c in
             (("actor", 4.0), ("interview", 0.6), ("macro_jury", 0.3), ("disposition_jury", 0.2),
              ("grand_jury", 1.1))] + [{"run_id": "other", "purpose": "actor", "cost": 99.0}]
    m = measure_runs(calls, [rec])
    assert m["actor_first_turn"] == pytest.approx(2.0)              # 0.5 + 1.5
    assert m["actor_later_turn"] == pytest.approx(1.0)
    assert m["interview_per_turn"] == pytest.approx(0.3)            # two turns interviewed
    assert m["macro_per_review"] == pytest.approx(0.3) and m["grand_jury"] == 1.1
    assert m["disposition_rating"] == 0.2 and "a2a_per_turn" not in m
    assert m["seat_share"] == {"claude-opus-5-5": 0.25, "gpt-6-astra": 0.75}
    # Dry runs measure nothing (all costs 0): every input falls back.
    dry = _measured_record()
    for t in dry["turns"]:
        for e in t["actors"].values():
            e["attempts"] = [{"cost": 0.0}]
    assert set(measure_runs([dict(c, cost=0.0) for c in calls], [dry])) == {"measured_from"}
    full = with_fallbacks(m)
    assert full["a2a_per_turn"] == pytest.approx(1.0 * costs.A2A_TO_ACTOR_RATIO, abs=1e-3)
    assert "a2a_per_turn" in full["estimated"] and "actor_later_turn" not in full["estimated"]
    sep = _measured_record("T4-run01", mode="separate")
    m2 = measure_runs([{"run_id": "T4-run01", "purpose": "a2a", "cost": 3.0}], [sep])
    assert m2["a2a_per_turn"] == pytest.approx(1.0)


def test_measure_runs_normalises_scripted_seat_runs_to_a_full_panel():
    """CR #3: a run with a scripted seat (T5) measures seat inputs for its model seats
    only; they are scaled to the full panel so later projections are not biased low,
    and project_run's seat_scale then applies once."""
    full, part = _measured_record("a-run01"), _measured_record("t5-run01", scripted=True)
    calls = [{"run_id": rid, "purpose": p, "cost": c} for rid in ("a-run01", "t5-run01")
             for p, c in (("actor", 4.0), ("interview", 0.6), ("disposition_jury", 0.2))]
    m_full = measure_runs(calls[:3], [full])
    m_part = measure_runs(calls[3:], [part])                       # 2 model seats of 3: x 1.5
    assert m_part["model_seats"] == {"t5-run01": 2}
    assert m_part["actor_first_turn"] == pytest.approx(m_full["actor_first_turn"] * 1.5)
    assert m_part["actor_later_turn"] == pytest.approx(m_full["actor_later_turn"] * 1.5)
    assert m_part["interview_per_turn"] == pytest.approx(m_full["interview_per_turn"] * 1.5)
    assert m_part["disposition_rating"] == pytest.approx(0.3)
    # Projection of a run with that scripted seat gets back the measured run's own cost.
    proj = project_run(with_fallbacks(m_part), turns=3, seat_scale=2 / 3)
    assert proj["seats"] == pytest.approx(0.5 + 1.5 + 2 * (0.25 + 0.75))
    # seat_share is per seat-turn: a run without a model does not inflate the others.
    assert m_part["seat_share"] == {"claude-opus-5-5": 0.25, "gpt-6-astra": 0.75}


def test_measured_inputs_average_seat_shares_over_tests_that_played_the_model(tmp_path):
    import pilot
    data = {"order": ["priced", "T5"]}
    shares = {"priced": {"claude-opus-5-5": 0.2, "grok-4.7": 0.2, "gpt-6-astra": 0.6},
              "T5": {"claude-opus-5-5": 0.25, "gpt-6-astra": 0.75}}
    for t, sh in shares.items():
        (tmp_path / t).mkdir()
        (tmp_path / t / f"pilot_summary_{t}.json").write_text(json.dumps(
            {"status": "completed", "runs_completed": 1, "inputs": {"actor_later_turn": 0.5, "seat_share": sh}}))
    out = pilot.measured_inputs(tmp_path, data)["seat_share"]
    assert sum(out.values()) == pytest.approx(1.0, abs=1e-3)
    assert out["grok-4.7"] == pytest.approx(0.2 / (0.225 + 0.2 + 0.675), abs=1e-3)


def test_project_run_and_kin_scale():
    i = with_fallbacks({"actor_first_turn": 1.0, "actor_later_turn": 0.5, "a2a_per_turn": 0.4,
                        "interview_per_turn": 0.1, "macro_per_review": 0.2, "grand_jury": 1.0,
                        "disposition_rating": 0.3, "seat_share": {"claude-opus-5-5": 0.3, "gpt-6-astra": 0.7}})
    p = project_run(i, turns=8, a2a="separate", interview_turns=4, grand_jury=True, reviews=2)
    assert p["seats"] == 4.5 and p["a2a"] == pytest.approx(3.2) and p["interview"] == pytest.approx(0.4)
    assert p["total"] == pytest.approx(4.5 + 3.2 + 0.4 + 0.4 + 1.0)
    assert project_run(i, turns=8, disposition_rating=True)["disposition_rating"] == 0.3
    assert kin_seat_scale(i) == pytest.approx(0.6)                  # 2 models measured x Claude's 0.3
    assert kin_seat_scale(with_fallbacks(None)) == 1.0


# ---------------------------------------------------------------------------
# campaign.py: cells, priority, runs per cell, cuts, interview decision
# ---------------------------------------------------------------------------

def test_cells_in_priority_order():
    names = [c["name"] for c in campaign.all_cells()]
    assert names == ["S1-A", "S2-A", "S1-C", "S2-C", "S1-A0", "S2-A0", "kin-S2-A", "S1-B", "S2-B"]
    kin = next(c for c in campaign.all_cells() if c["kin"])
    assert (kin["scenario"], kin["condition"]) == ("S2", "A")


def test_runs_per_cell_from_the_budget_and_whole_cell_cuts():
    plan = campaign.build_campaign(None, 750.0, turns=8, s2_mode="merged")
    assert plan["runs_per_cell"] >= 6 and not plan["cut_groups"]
    assert all(c["a2a"] == "merged" for c in plan["cells"])
    assert plan["total"] <= 750.0 and plan["round_cost"] == pytest.approx(sum(c["run_cost"] for c in plan["cells"]),
                                                                          abs=0.02)
    # A tighter budget: B, then kin, then A0 go whole, never thinned below 6.
    plan = campaign.build_campaign(None, 300.0, turns=8, s2_mode="separate")
    assert plan["cut_groups"] == ["B", "kin", "A0"] and plan["runs_per_cell"] >= 6
    assert {c["name"] for c in plan["cells"] if c["runs"]} == {"S1-A", "S2-A", "S1-C", "S2-C"}
    assert all(c["a2a"] == ("merged" if c["scenario"] == "S1" else "separate") for c in plan["cells"])
    # Too little even for A: it runs what the budget allows, flagged.
    plan = campaign.build_campaign(None, 40.0, turns=8, s2_mode="separate")
    assert plan["below_min"] and plan["cut_groups"] == ["B", "kin", "A0", "C"]
    assert "BELOW the 6-run floor" in "\n".join(campaign.campaign_lines(plan))
    # Capped at --max-runs.
    assert campaign.build_campaign(None, 100000.0, max_runs=12)["runs_per_cell"] == 12


def test_interview_runs_only_if_it_adds_at_most_15_percent():
    cheap = {"interview_per_turn": 0.05}
    plan = campaign.build_campaign(cheap, 750.0)
    assert plan["interview"]["run"] and plan["interview_turns"] == [3, 4, 5, "final"]
    dear = {"interview_per_turn": 2.0}
    plan = campaign.build_campaign(dear, 750.0)
    assert not plan["interview"]["run"] and plan["interview_turns"] == []
    assert "reported as untested" in "\n".join(campaign.campaign_lines(plan))


def test_kin_cell_priced_from_claudes_measured_share():
    inputs = {"actor_first_turn": 2.0, "actor_later_turn": 1.0,
              "seat_share": {"claude-opus-5-5": 0.1, "gpt-6-astra": 0.6, "gemini-3.1-pro": 0.1,
                             "muse-spark-1.3": 0.1, "grok-4.7": 0.1}}
    plan = campaign.build_campaign(inputs, 750.0, s2_mode="merged")
    kin = next(c for c in plan["cells"] if c["kin"])
    s2a = next(c for c in plan["cells"] if c["name"] == "S2-A")
    assert kin["cost_parts"]["seats"] == pytest.approx(s2a["cost_parts"]["seats"] * 0.5)


def test_s2_mode_follows_t9(tmp_path, monkeypatch):
    assert campaign.pilot_s2_mode(tmp_path) == "separate"            # T9 cut or not run
    (tmp_path / "T4").mkdir()
    (tmp_path / "T9").mkdir()
    for t in ("T4", "T9"):
        (tmp_path / t / f"{t}-run01.partial.json").write_text(json.dumps(
            {"config": {"scenario": "S2", "a2a_mode": "separate" if t == "T4" else "merged"},
             "labs": {}, "turns": [], "ledger": [], "a2a": []}))
    monkeypatch.setattr("tools.compare_arms.compare", lambda sep, mer: {"merge": True})
    assert campaign.pilot_s2_mode(tmp_path) == "separate"            # partial (aborted) logs never decide
    for t in ("T4", "T9"):
        (tmp_path / t / f"{t}-run01.json").write_text(json.dumps(
            {"config": {"scenario": "S2", "a2a_mode": "separate" if t == "T4" else "merged"},
             "labs": {}, "turns": [], "ledger": [], "a2a": [], "final": {"scores": [{"lab": "xai"}]}}))
    assert campaign.pilot_s2_mode(tmp_path) == "merged"
    monkeypatch.setattr("tools.compare_arms.compare", lambda sep, mer: {"merge": False})
    assert campaign.pilot_s2_mode(tmp_path) == "separate"


def test_run_campaign_interleaves_rounds_and_resumes(tmp_path):
    plan = campaign.build_campaign(None, 300.0, turns=8, s2_mode="separate")
    order = []

    def fake(args):
        order.append(args.run_id)
        assert args.interview_turns == plan["interview_turns"] and args.turns == 8
        assert args.a2a == ("merged" if args.scenario == "S1" else "separate")
        return (1, None) if args.run_id == "S2-C-run02" else (0, {})
    assert campaign.run_campaign(plan, tmp_path, dry_run=True, run_cell=fake) == 1
    assert order[:5] == ["S1-A-run01", "S2-A-run01", "S1-C-run01", "S2-C-run01", "S1-A-run02"]
    assert order[-1] == "S2-C-run02"
    state = json.loads((tmp_path / campaign.STATE_NAME).read_text())
    assert state["runs"]["S2-C"] == ["S2-C-run01"]
    order.clear()

    def ok(args):
        order.append(args.run_id)
        return 0, {}
    assert campaign.run_campaign(plan, tmp_path, dry_run=True, rounds=2, run_cell=ok) == 0
    assert order == ["S2-C-run02"]                                   # resumes where it stopped


def test_campaign_dry_round_end_to_end(tmp_path, capsys):
    """One dry round of every cell through main.run_cell (stub models and jurors, 2 turns)."""
    plan = campaign.build_campaign(None, 750.0, turns=2, s2_mode="separate")
    assert campaign.run_campaign(plan, tmp_path, dry_run=True, rounds=1) == 0
    for c in plan["cells"]:
        rec = json.loads((tmp_path / c["name"] / f"{c['name']}-run01.json").read_text())
        cfg = rec["config"]
        assert cfg["scenario"] == c["scenario"] and cfg["condition"] == c["condition"]
        assert cfg["fog"] == "F3" and cfg["a2a_mode"] == c["a2a"]
        assert cfg["run_meta"]["kin"] is c["kin"] and cfg["run_meta"]["pilot"] is False
    assert campaign.main(["plan", "--dry-run", "--pilot-dir", str(tmp_path / "nopilot"),
                          "--spend-file", str(tmp_path / "s.json")]) == 0
    assert "priority order of each round: S1-A, S2-A" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Resume (CR #1), guard trips during the Grand Jury (CR #4)
# ---------------------------------------------------------------------------

def _ledger(path, total):
    path.write_text(json.dumps({"total_usd": total, "calls": 0, "by_model": {}}))


def _campaign_argv(tmp_path, *extra):
    return ["run", "--spend-file", str(tmp_path / "spend.json"), "--runs-dir", str(tmp_path / "runs"),
            "--pilot-dir", str(tmp_path / "nopilot"), *extra]


def _paying_run_cell(tmp_path, costs_by_cell, factor=1.0, fail_at=None, order=None):
    """A stand-in for main.run_cell that adds the run's cost to the ledger (as a real run would)."""
    def fake(args):
        if order is not None:
            order.append(args.run_id)
        if args.run_id == fail_at:
            return 1, None
        f = tmp_path / "spend.json"
        total = json.loads(f.read_text())["total_usd"] if f.exists() else 0.0
        _ledger(f, total + factor * costs_by_cell[args.output.rsplit("/", 1)[-1]])
        return 0, {"final": {"scores": [{"lab": "xai"}]}}
    return fake


def test_resume_reuses_the_saved_plan_across_processes(tmp_path, monkeypatch):
    """CR #1: a resumed campaign keeps its cells and its rounds; it does not re-plan
    from the budget left (which used to cut the campaign short)."""
    _ledger(tmp_path / "spend.json", 0.0)
    plan = campaign.build_campaign(None, 300.0, turns=8, s2_mode="separate")
    cost = {c["name"]: c["run_cost"] for c in plan["cells"]}
    active = [c["name"] for c in plan["cells"] if c["runs"]]
    order = []
    monkeypatch.setattr(main_mod, "run_cell", _paying_run_cell(tmp_path, cost, fail_at=f"{active[1]}-run03",
                                                               order=order))
    assert campaign.main(_campaign_argv(tmp_path, "--budget", "300")) == 1
    state = json.loads((tmp_path / "runs" / campaign.STATE_NAME).read_text())
    assert state["guard"] == pytest.approx(300.0) and state["plan"]["runs_per_cell"] == plan["runs_per_cell"]
    # A fresh process: new tracker, module state re-read from disk, no --budget.
    costs.configure()
    order.clear()
    monkeypatch.setattr(main_mod, "run_cell", _paying_run_cell(tmp_path, cost, order=order))
    assert campaign.main(_campaign_argv(tmp_path)) == 0
    assert order[0] == f"{active[1]}-run03"
    state = json.loads((tmp_path / "runs" / campaign.STATE_NAME).read_text())
    assert sorted(state["runs"]) == sorted(active)                   # the cuts stayed fixed
    assert all(len(v) == plan["runs_per_cell"] for v in state["runs"].values())


def test_resume_rounds_follow_the_budget_left(tmp_path, monkeypatch):
    """Runs dearer than projected: the resume runs done + floor(left / round cost) rounds."""
    _ledger(tmp_path / "spend.json", 0.0)
    plan = campaign.build_campaign(None, 300.0, turns=8, s2_mode="separate")
    cost = {c["name"]: c["run_cost"] for c in plan["cells"]}
    active = [c["name"] for c in plan["cells"] if c["runs"]]
    monkeypatch.setattr(main_mod, "run_cell", _paying_run_cell(tmp_path, cost, factor=1.5,
                                                               fail_at=f"{active[0]}-run03"))
    assert campaign.main(_campaign_argv(tmp_path, "--budget", "300")) == 1
    costs.configure()
    spent = json.loads((tmp_path / "spend.json").read_text())["total_usd"]
    want = 2 + int((300.0 - spent) // plan["round_cost"])
    assert want < plan["runs_per_cell"]
    monkeypatch.setattr(main_mod, "run_cell", _paying_run_cell(tmp_path, cost, factor=1.5))
    assert campaign.main(_campaign_argv(tmp_path)) == 0
    state = json.loads((tmp_path / "runs" / campaign.STATE_NAME).read_text())
    assert all(len(v) == want for v in state["runs"].values())
    # --replan adopts a new plan from the budget left: rounds = done + floor(left / round cost).
    costs.configure()
    assert campaign.main(["plan", "--spend-file", str(tmp_path / "spend.json"), "--runs-dir",
                          str(tmp_path / "runs"), "--pilot-dir", str(tmp_path / "nopilot")]) == 0


def test_resume_rounds_pays_for_a_partly_run_round_first():
    plan = {"runs_per_cell": 8, "round_cost": 10.0,
            "cells": [{"name": n, "runs": 8, "run_cost": 2.5} for n in "abcd"]}
    done = {"a": ["a1", "a2", "a3"], "b": ["b1", "b2", "b3"], "c": ["c1", "c2"], "d": ["d1", "d2"]}
    assert campaign.completed_rounds(plan, done) == 2
    assert campaign.resume_rounds(plan, done, 4.0) == 2               # cannot finish round 3
    assert campaign.resume_rounds(plan, done, 5.0) == 3
    assert campaign.resume_rounds(plan, done, 25.0) == 5
    assert campaign.resume_rounds(plan, done, 1000.0) == 8            # never beyond the saved plan
    assert campaign.resume_rounds(plan, done, None) == 8              # dry run


def test_guard_trip_during_grand_jury_counts_as_done_with_jury_missing(tmp_path):
    plan = campaign.build_campaign(None, 300.0, turns=8, s2_mode="separate")
    first = next(c["name"] for c in plan["cells"] if c["runs"])
    calls, tripped = [], []

    def fake(args):
        calls.append(args.run_id)
        if args.run_id == f"{first}-run01" and not tripped:
            tripped.append(1)
            return 1, {"final": {"scores": [{"lab": "xai"}], "grand_jury": {"error": "budget"}}}
        return 0, {"final": {"scores": [{"lab": "xai"}]}}
    assert campaign.run_campaign(plan, tmp_path, dry_run=True, rounds=1, run_cell=fake) == 1
    state = json.loads((tmp_path / campaign.STATE_NAME).read_text())
    assert state["runs"][first] == [f"{first}-run01"] and state["jury_missing"] == [f"{first}-run01"]
    calls.clear()
    assert campaign.run_campaign(plan, tmp_path, dry_run=True, rounds=1, run_cell=fake) == 0
    assert calls.count(f"{first}-run01") == 1 and calls[0] == f"{first}-run01"   # the jury only, once
    assert json.loads((tmp_path / campaign.STATE_NAME).read_text())["jury_missing"] == []


def test_main_never_repays_a_run_halted_during_its_grand_jury(tmp_path, monkeypatch):
    """CR #4: the guard trips in the Grand Jury after the final record is saved; calling
    again asks only the jury, and the final record is never deleted."""
    from core import jury as jury_mod
    from core.costs import BudgetExceeded
    out = tmp_path / "run"
    argv = ["--dry-run", "--scenario", "S1", "--condition", "A", "--turns", "2", "--output", str(out),
            "--run-id", "S1-A-run01"]
    orig = jury_mod.GrandJury.evaluate

    def broke(self, *a, **kw):
        raise BudgetExceeded("guard reached")
    monkeypatch.setattr(jury_mod.GrandJury, "evaluate", broke)
    code, rec = main_mod.run_cell(main_mod.build_parser().parse_args(argv))
    assert code == 1 and rec["final"]["scores"] and rec["final"]["grand_jury"]["error"] == "budget"
    saved = json.loads((out / "S1-A-run01.json").read_text())
    assert saved["final"]["scores"]
    monkeypatch.setattr(jury_mod.GrandJury, "evaluate", orig)

    def no_rerun(self):
        raise AssertionError("the seats were run again")
    monkeypatch.setattr(main_mod.SimulationEngine, "run", no_rerun)
    code, rec = main_mod.run_cell(main_mod.build_parser().parse_args(argv))
    assert code == 0
    again = json.loads((out / "S1-A-run01.json").read_text())
    assert again["turns"] == saved["turns"] and not again["final"]["grand_jury"].get("error")
    assert again["final"]["grand_jury"].get("collected_post_run")
    # A complete run is kept as it is.
    code, _ = main_mod.run_cell(main_mod.build_parser().parse_args(argv))
    assert code == 0
    # clear_stale never deletes a finished record; another set-up under the id is refused.
    assert main_mod.clear_stale(out, "S1-A-run01") is False and (out / "S1-A-run01.json").exists()
    code, _ = main_mod.run_cell(main_mod.build_parser().parse_args(argv[:4] + ["C"] + argv[5:]))
    assert code == 2 and (out / "S1-A-run01.json").exists()


def test_main_model_override_is_validated_and_sets_the_provider(tmp_path, capsys):
    from core.config import build_labs_and_world, load_world
    labs, _ = build_labs_and_world(load_world(), charters=False)
    assert "KEY=MODEL" in main_mod.apply_model_overrides(labs, ["openia=gpt-6-astra"])
    assert "KEY=MODEL" in main_mod.apply_model_overrides(labs, ["openai"])
    assert "Cannot infer provider" in main_mod.apply_model_overrides(labs, ["openai=mystery-1"])
    assert main_mod.apply_model_overrides(labs, ["openai=claude-opus-5-5"]) == ""
    oa = next(lab for lab in labs if lab.key == "openai")
    assert (oa.model, oa.provider) == ("claude-opus-5-5", "anthropic")   # preflight checks this key
    assert main_mod.main(["--dry-run", "--model", "nolab=gpt-6-astra", "--turns", "1"]) == 2
    assert "bad --model" in capsys.readouterr().out


def test_main_dry_run_without_output_writes_under_runs_dry(tmp_path):
    assert main_mod.main(["--dry-run", "--turns", "1", "--run-id", "dry-x", "--no-grand-jury"]) == 0
    assert (tmp_path / "runs_default" / "dry" / "dry-x" / "dry-x.json").exists()
    assert not (tmp_path / "runs_default" / "dry-x").exists()
