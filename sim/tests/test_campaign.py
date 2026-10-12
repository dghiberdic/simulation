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

def _measured_record(run_id="priced-run01", mode="merged"):
    def actors(cost):
        return {"anthropic": {"attempts": [{"cost": cost}]}, "openai": {"attempts": [{"cost": 3 * cost}]},
                "xai": {"scripted": True, "attempts": [{"cost": 9.0}]}}
    return {"config": {"a2a_mode": mode, "run_meta": {"run_id": run_id}},
            "labs": {"anthropic": {"model": "claude-opus-5-5"}, "openai": {"model": "gpt-6-astra"},
                     "xai": {"model": "grok-4.7"}},
            "turns": [{"turn": 1, "actors": actors(0.5), "interviews": {}},
                      {"turn": 2, "actors": actors(0.25), "interviews": {}, "macro_jury": {}},
                      {"turn": 3, "actors": actors(0.25)}]}


def test_measure_runs_splits_turn_one_and_purposes():
    rec = _measured_record()
    calls = [{"run_id": "priced-run01", "purpose": p, "cost": c} for p, c in
             (("actor", 4.0), ("interview", 0.6), ("macro_jury", 0.3), ("disposition_jury", 0.2),
              ("grand_jury", 1.1))] + [{"run_id": "other", "purpose": "actor", "cost": 99.0}]
    m = measure_runs(calls, [rec])
    assert m["actor_first_turn"] == pytest.approx(2.0)              # 0.5 + 1.5 (scripted seat out)
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
        (tmp_path / t / f"{t}-run01.json").write_text(json.dumps(
            {"config": {"scenario": "S2", "a2a_mode": "separate" if t == "T4" else "merged"},
             "labs": {}, "turns": [], "ledger": [], "a2a": []}))
    monkeypatch.setattr("tools.compare_arms.compare", lambda sep, mer: {"merge": True})
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
