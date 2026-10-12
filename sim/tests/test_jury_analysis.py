"""tools/jury_analysis: per-seat Grand Jury item and composite means (one juror, §7, §8) and
the UPS-weight sensitivity of the index."""

import json

import pytest

from core.jury import RUBRIC_ITEMS, GrandJury
from core.llm import register_stub
from tools import jury_analysis as ja

KEYS = ["anthropic", "openai", "gdm", "meta", "xai"]
EXAMPLE = {"seat": "A", "excerpt": "x", "items": {i: 3 for i in RUBRIC_ITEMS}, "composite": 3,
           "reasons": {i: "r" for i in RUBRIC_ITEMS}}


def _stub(run):
    """Seat i scores i+1 on every item; composite i+1 in even runs, i+2 capped in odd runs."""
    def fn(system, user):
        seats = {}
        for i, letter in enumerate("ABCDE"):
            comp = min(5, i + 1 + run % 2)
            seats[letter] = {"items": {it: i + 1 for it in RUBRIC_ITEMS}, "composite": comp,
                             "perceived_disposition": 10 * (i + 1) + run,
                             "reasons": {it: "r" for it in RUBRIC_ITEMS}}
        return json.dumps({"seats": seats})
    return fn


def _records(n=3):
    recs = []
    for i in range(n):
        register_stub(f"kimi-ja{i}", _stub(i))
        gj = GrandJury([f"stub:kimi-ja{i}"], examples=[EXAMPLE]).evaluate("T", KEYS)
        recs.append({"run_id": f"r{i}", "config": {"scenario": "S1", "condition": "ABC"[i % 3]},
                     "labs": {k: {"model": f"m-{k}"} for k in KEYS}, "turns": [],
                     "final": {"grand_jury": gj,
                               "ups_index": {"ups": 0.6, "capability": 0.5 + 0.1 * i,
                                             "concentration": 0.8 - 0.05 * i, "equity": 0.6,
                                             "norms": 0.5 + 0.02 * i, "integrity": 1.0 - 0.1 * i}}})
    return recs


def test_per_seat_item_and_composite_means():
    rep = ja.analyse(_records(), samples=0)
    m = rep["measures"]
    assert set(m) == {f"items.{i}" for i in RUBRIC_ITEMS} | {"composite", "perceived_disposition"}
    assert m["items.honesty"]["anthropic"] == {"n_runs": 3, "mean": 1.0, "sd": 0.0}
    assert m["items.outside_welfare"]["xai"]["mean"] == 5.0
    # composite: runs 0, 2 give i+1, run 1 gives i+2 (capped at 5)
    assert m["composite"]["anthropic"]["mean"] == pytest.approx(round(4 / 3, 2))
    assert m["composite"]["xai"]["mean"] == 5.0
    assert m["perceived_disposition"]["openai"]["mean"] == 21.0
    assert "ups_contribution" not in json.dumps(rep) and "adjusted" not in json.dumps(rep)
    assert rep["runs"]["r0"]["juror"] == "stub:kimi-ja0" and rep["runs"]["r0"]["rubric"] == "scored"


def test_no_examples_runs_give_disposition_only_and_missing_jury_noted():
    register_stub("kimi-ja-disp", lambda s, u: json.dumps(
        {"seats": {L: {"perceived_disposition": 40, "reason": "r"} for L in "ABCDE"}}))
    rec = {"run_id": "nd", "labs": {k: {} for k in KEYS},
           "final": {"grand_jury": GrandJury(["stub:kimi-ja-disp"], examples=[]).evaluate("T", KEYS)}}
    bare = {"run_id": "none", "labs": {k: {} for k in KEYS}, "final": {}}
    rep = ja.analyse([rec, bare], samples=0)
    assert set(rep["measures"]) == {"perceived_disposition"}
    assert rep["measures"]["perceived_disposition"]["gdm"]["mean"] == 40.0
    assert any("nd: rubric not scored (no examples yet)" in n for n in rep["notes"])
    assert any("none: no Grand Jury verdict" in n for n in rep["notes"])


def test_ups_weight_sensitivity_is_seeded_and_reports_rank_stability():
    recs = _records()
    a = ja.ups_sensitivity([(r["run_id"], r) for r in recs], samples=50, seed=3)
    b = ja.ups_sensitivity([(r["run_id"], r) for r in recs], samples=50, seed=3)
    assert a == b
    runs = a["runs"]
    assert len(runs["perturbations"]) == 10                         # five weights, ±0.1
    assert all(abs(sum(w.values()) - 1) < 1e-9 for _l, w in ja.perturbations(ja.base_weights()))
    assert set(ja.base_weights()) == set(ja.UPS_COMPONENTS)
    d = runs["dirichlet"]
    assert d["samples"] == 50 and 0 <= d["share_same_order"] <= 1 and set(d["rank_range"]) == {"r0", "r1", "r2"}
    assert ja.ups_sensitivity([(recs[0]["run_id"], recs[0])], samples=5)["runs"] is None


def test_cli_and_duplicate_run_ids(tmp_path, capsys):
    for r in _records():
        (tmp_path / f"{r['run_id']}.json").write_text(json.dumps(r))
    assert ja.main([str(tmp_path), "--samples", "10"]) == 0
    out = capsys.readouterr().out
    assert "one juror" in out and "composite" in out and "Dirichlet (10 draws)" in out
    assert ja.main([str(tmp_path), "--json", "--samples", "0"]) == 0
    assert json.loads(capsys.readouterr().out)["n_runs"] == 3
    recs = _records(2)
    recs[1]["run_id"] = "r0"
    rep = ja.analyse(recs, samples=0, paths=["A/r0.json", "B/r0.json"])
    assert set(rep["runs"]) == {"r0 @ A/r0.json", "r0 @ B/r0.json"}
    assert any(n.startswith("WARNING: run id r0 appears 2 times") for n in rep["notes"])
