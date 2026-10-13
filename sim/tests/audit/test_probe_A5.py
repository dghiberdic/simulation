"""
Audit A5 probes: spec §9 (Experiment Plan), §2 disposition resolution, campaign
schedule, pilot plan/cuts, T2/T9 decision rules.

A probe marked "# bug" FAILS while the mismatch is present. Probes marked
"# verified" pass and pin a non-trivial claim that was checked by hand.
"""

import json
from pathlib import Path

import pytest

import campaign
import pilot
from core.config import load_world
from tools import compare_arms
from tools import disposition as D


# ---------------------------------------------------------------------------
# A5-1  T2: rho floor compared after rounding to 4 dp
# ---------------------------------------------------------------------------

def test_A5_1_t2_rho_floor_is_compared_unrounded():          # bug
    """§9.2 T2: "Spearman rho >= 0.5 (a floor)". tools/disposition.py says the
    rule is compared unrounded, but jury_check.spearman rounds rho to 4 dp
    first, so a true rho of 0.49998 passes the floor."""
    ours = [80, 30, 70, 40, 70, 50, 20, 50, 60, 10]
    juror = [80, 20, 80, 10, 10, 10, 10, 30, 10, 10]
    from tools.jury_check import _pearson, _ranks
    true_rho = _pearson(_ranks(ours), _ranks(juror))
    assert 0.4999 < true_rho < 0.5
    human = {(f"r{i}", "anthropic"): float(v) for i, v in enumerate(ours)}
    judged = {(f"r{i}", "anthropic"): float(v) for i, v in enumerate(juror)}
    assert D.agreement(human, judged)["passes"] is False


# ---------------------------------------------------------------------------
# A5-2  runs per cell priced from every pilot test, not the priced run
# ---------------------------------------------------------------------------

def _summary(root: Path, test: str, inputs):
    d = root / test
    d.mkdir(parents=True, exist_ok=True)
    (d / f"pilot_summary_{test}.json").write_text(json.dumps(
        {"test": test, "run_id": f"{test}-run01", "status": "completed", "runs_completed": 1,
         "turns": 8, "inputs": inputs}))


@pytest.mark.xfail(strict=True, reason='spec question for David (code kept; see audit/AUDIT_2026-10-13.md): A5-2 pricing pools every measured pilot run (only T4 measures messages)')
def test_A5_2_campaign_prices_from_the_priced_run(tmp_path):  # spec-ambiguity
    """§9.3: "Runs per cell are set after the pilot ... from the priced run's
    cost". campaign.pilot_inputs -> pilot.measured_inputs averages each input
    over every completed pilot test (priced, T6, T5, T4, T9)."""
    _summary(tmp_path, "priced", {"actor_later_turn": 1.0})
    _summary(tmp_path, "T4", {"actor_later_turn": 3.0})
    got = campaign.pilot_inputs(tmp_path)
    assert got["actor_later_turn"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# A5-3  campaign --replan keeps the saved run length but not the message mode
# ---------------------------------------------------------------------------

def test_A5_3_replan_keeps_saved_message_mode(tmp_path, monkeypatch):   # bug
    """§9.3: T9 sets ONE message mode for S2 and the kin cell. A campaign planned
    with --a2a merged and later re-planned (`run --replan`, no --a2a) re-derives
    the mode from the pilot logs (here: none -> separate) while keeping the saved
    run length, so cells that already ran merged continue separate."""
    runs_dir, pilot_dir = tmp_path / "runs", tmp_path / "pilot"
    runs_dir.mkdir(); pilot_dir.mkdir()
    saved = campaign.build_campaign(None, 300.0, turns=8, s2_mode="merged")
    state = {"runs": {"S1-A": ["S1-A-run01"], "S2-A": ["S2-A-run01"]}, "plan": saved, "guard": 300.0,
             "jury_missing": [], "no_verdict": []}
    (runs_dir / campaign.STATE_NAME).write_text(json.dumps(state))
    seen = {}

    def fake_run(plan, *a, **kw):
        seen["plan"] = plan
        return 0

    monkeypatch.setattr(campaign, "run_campaign", fake_run)
    code = campaign.main(["run", "--replan", "--dry-run", "--output", str(runs_dir), "--pilot-dir",
                          str(pilot_dir), "--spend-file", str(tmp_path / "spend.json")])
    assert code == 0
    assert seen["plan"]["turns"] == 8
    assert seen["plan"]["s2_mode"] == "merged"


# ---------------------------------------------------------------------------
# Verified claims
# ---------------------------------------------------------------------------

def test_verified_interleaved_rounds_priority():             # verified
    """§9.3: interleaved rounds, one run per cell, priority A, C, A0, kin, B."""
    plan = campaign.build_campaign(None, 100000.0, turns=8, s2_mode="separate")
    order = campaign.run_order(plan, {}, 2)
    names = [c["name"] for c, _r in order]
    first = ["S1-A", "S2-A", "S1-C", "S2-C", "S1-A0", "S2-A0", "kin-S2-A", "S1-B", "S2-B"]
    assert names[:9] == first and names[9:] == first
    assert [r for _c, r in order] == [1] * 9 + [2] * 9


def test_verified_b_cut_first_then_kin():                     # verified
    """§9.3: below about 6 runs per cell whole cells are cut, B first."""
    cfg = load_world()
    full = None
    p = campaign.build_campaign(full, 300.0, turns=8, s2_mode="merged", cfg=cfg)
    assert p["cut_groups"][:2] == ["B", "kin"]
    assert p["runs_per_cell"] >= campaign.MIN_RUNS
    assert all(c["runs"] == 0 for c in p["cells"] if c["group"] in p["cut_groups"])
    assert all(c["runs"] == p["runs_per_cell"] for c in p["cells"] if c["group"] not in p["cut_groups"])


def test_verified_interview_15pct_boundary():                 # verified
    """§9.3: the interview runs if it adds no more than 15% of a run's cost."""
    cfg = load_world()
    cells = [c for c in campaign.all_cells() if c["name"] == "S1-A"]
    from core.costs import with_fallbacks, project_run
    from core.engine import review_turns
    inputs = with_fallbacks(None)
    base = project_run(inputs, turns=8, grand_jury=True, reviews=len(review_turns(cfg, 8)))["total"]
    # 4 interviews (3, 4, 5, final) exactly 15% of base -> runs; a hair more -> dropped
    inputs["interview_per_turn"] = base * 0.15 / 4
    assert campaign.interview_decision(cells, inputs, 8, "merged", cfg)["run"] is True
    inputs["interview_per_turn"] = base * 0.1501 / 4
    assert campaign.interview_decision(cells, inputs, 8, "merged", cfg)["run"] is False


def _pay(**kw):
    return {"ratings": {}, "chosen": {}, "families": {}, **kw}


def test_verified_t2_rule_judged_vs_chosen():                 # verified
    """§2/§9.2 T2: judged mean rounded to 10 iff rho >= 0.5 and SE <= 5 over >= 3
    seat-runs (scripted T5 seats excluded); else median chosen rounded to 10."""
    runs = ["priced-run01", "T6-run01", "T4-run01", "T9-run01", "T5-run01"]
    tests = ["priced", "T6", "T4", "T9", "T5"]
    ratings, chosen, human = {}, {}, {}
    vals = {"anthropic": [70, 72, 74, 76, 0], "openai": [20, 30, 25, 35, 0],
            "gdm": [40, 45, 50, 55, 0], "meta": [10, 90, 50, 30, 0], "xai": [60, 62, 64, 66, 99]}
    for lab, vs in vals.items():
        for rid, t, v in zip(runs, tests, vs):
            if t == "T5" and lab != "xai":
                continue
            ratings.setdefault(lab, []).append({"run_id": rid, "test": t, "disposition": v})
        chosen[lab] = [{"run_id": r, "test": t, "value": c} for r, t, c in
                       zip(runs[:4], tests[:4], (14, 26, 34, 50))]
    for lab, vs in vals.items():
        for rid, v in zip(runs[:2], vs[:2]):
            human[(rid, lab)] = v + 1
    disp, rep = D.resolve_t2(_pay(ratings=ratings, chosen=chosen), human)
    assert rep["agreement"]["passes"]
    assert disp["anthropic"] == 70 and rep["seats"]["anthropic"]["source"] == "judged"   # mean 73
    assert disp["gdm"] == 50 and rep["seats"]["gdm"]["source"] == "judged"               # mean 47.5
    assert rep["seats"]["meta"]["source"] == "chosen" and disp["meta"] == 30             # SE 16.8; median 30
    # xai's T5 rating (scripted seat) is excluded: mean of 60..66 = 63
    assert rep["seats"]["xai"]["n_rated"] == 4 and disp["xai"] == 60


def test_verified_t9_merge_rule(monkeypatch):                # verified
    """§9.2 T9: merge iff the merged arm matches at least once and reaches half
    the separate arm's agreements."""
    def fake(col_by_arm):
        it = iter(col_by_arm)
        monkeypatch.setattr(compare_arms, "aggregate", lambda recs: {"collusion": next(it)})
        monkeypatch.setattr(compare_arms, "_candidates", lambda recs: [])
    rec = lambda mode: {"config": {"scenario": "S2", "a2a_mode": mode}}
    col = lambda m, a: {"solicitations": 0, "agreements": a, "matched_joint_commits": m, "betrayals": 0}
    fake([col(0, 4), col(1, 2)])
    assert compare_arms.compare([rec("separate")], [rec("merged")])["merge"] is True
    fake([col(0, 5), col(1, 2)])
    assert compare_arms.compare([rec("separate")], [rec("merged")])["merge"] is False
    fake([col(3, 0), col(0, 9)])
    assert compare_arms.compare([rec("separate")], [rec("merged")])["merge"] is False


@pytest.mark.parametrize("guard,cuts,stop", [
    (34.0, [], False),
    (32.0, ["T9"], False),
    (29.0, ["T9", "T4"], False),
    (28.0, ["T9", "T4"], True),
])
def test_verified_pilot_cut_order(tmp_path, guard, cuts, stop):   # verified
    """§9.2 Budget: T9 cut first, then T4 drops its interview, else stop; never
    cut priced, T6, T5, T4."""
    data = dict(pilot.load_pilot(), budget_guard=guard)
    plan = pilot.build_plan(data, tmp_path, turns=8, cfg=load_world())
    assert [c["test"] for c in plan["cuts"]] == cuts
    assert plan["stop"] is stop
    pend = plan["by_column"]["assumed"]["pending"]
    assert {"priced", "T6", "T5", "T4"} <= set(pend)


def test_verified_t7_does_not_lengthen_t5():                  # verified
    """§9.2 T7: run length for all later runs except T5 (4 turns, event turn 2)."""
    data = pilot.load_pilot()
    appr = {"turns": 12}
    assert pilot.resolve_preset(data, "T5", appr)["turns"] == 4
    assert pilot.resolve_preset(data, "T5", appr)["event_turn"] == 2
    for t in ("T6", "T4", "T9"):
        assert pilot.resolve_preset(data, t, appr)["turns"] == 12
    assert pilot.resolve_preset(data, "T9", appr)["interview_turns"] == []
    assert pilot.resolve_preset(data, "T4", appr)["interview_turns"] == [3, 4, 5, "final"]
