"""tools/jury_analysis: per-seat Grand Jury measures, raw and juror-adjusted (§7, R1–R3), and the
UPS-weight sensitivity of the index."""

import json

import pytest

from core.jury import RUBRIC_DIMS, GrandJury
from core.llm import register_stub
from tools import jury_analysis as ja

KEYS = ["anthropic", "openai", "gdm", "meta", "xai"]
FAM = {"anthropic": "claude", "openai": "gpt", "gdm": "gemini", "meta": "muse", "xai": "grok"}
LEN = {"claude": 80, "gpt": 40, "gemini": 60}        # jurors differ only in leniency (r5C/fe.py)
JURORS = ["stub:claude-ja", "stub:gpt-ja", "stub:gemini-ja"]


def _stub(fam, favour=0):
    def fn(system, user):
        v = LEN[fam]
        actors = {}
        for k in KEYS:
            s = v + (favour if FAM[k] == fam else 0)
            actors[k] = {"ups_without": 50 - v / 10, "alignment_charter": s, "alignment_rules": s,
                         "alignment_welfare": s, "perceived_disposition": s,
                         "rubric": {d: {"claude": 5, "gpt": 1, "gemini": 3}[fam] for d in RUBRIC_DIMS}}
        return json.dumps({"ups": 50, "commentary": "x", "actors": actors})
    return fn


def _records(n=3, favour=0):
    for f in LEN:
        register_stub(f"{f}-ja", _stub(f, favour))
    gj = GrandJury(JURORS)
    recs = []
    for i in range(n):
        recs.append({"run_id": f"r{i}", "config": {"scenario": "S1", "condition": "ABC"[i % 3]},
                     "labs": {k: {"family": FAM[k], "model": f"m-{k}"} for k in KEYS}, "turns": [],
                     "final": {"grand_jury": gj.evaluate("T", KEYS, FAM),
                               "ups_index": {"ups": 0.6, "capability": 0.5 + 0.1 * i,
                                             "concentration": 0.8 - 0.05 * i, "equity": 0.6,
                                             "norms": 0.5 + 0.02 * i}}})
    return recs


def test_identical_seats_get_identical_adjusted_means():
    """r5C/fe.py: raw means differ only because own-family exclusion drops different jurors."""
    rep = ja.analyse(_records(), samples=20)
    ac = rep["measures"]["alignment_charter"]
    assert ac["seats"]["anthropic"]["raw"] == 50.0 and ac["seats"]["openai"]["raw"] == 70.0
    for m in rep["measures"].values():
        assert len({s["adjusted"] for s in m["seats"].values()}) == 1
    assert ac["juror_offsets"]["stub:claude-ja"] == 20.0 and ac["juror_offsets"]["stub:gpt-ja"] == -20.0
    assert ac["seats"]["anthropic"]["n"] == 6 and ac["seats"]["meta"]["n"] == 9   # own family out (§7)


def test_ups_contribution_is_leave_one_out_and_excludable():
    rep = ja.analyse(_records(), samples=0)
    uc = rep["measures"]["ups_contribution"]
    assert uc["seats"]["meta"]["raw"] == pytest.approx(6.0)        # mean of 50-(50-v/10) = v/10
    assert rep["runs"]["r0"]["jury_ups"] == 50.0
    ex = ja.analyse(_records(), exclude_ups=["claude"], samples=0)
    assert ex["measures"]["ups_contribution"]["seats"]["meta"]["n"] == 6       # claude juror out of UPS
    assert ex["measures"]["alignment_charter"]["seats"]["meta"]["n"] == 9      # but not of alignment


def test_old_records_with_ups_contribution():
    rec = _records(1)[0]
    for pj in rec["final"]["grand_jury"]["per_juror"].values():
        for a in pj["result"]["actors"].values():
            a.pop("ups_without")
            a["ups_contribution"] = 7
    rep = ja.analyse([rec], samples=0)
    assert rep["measures"]["ups_contribution"]["seats"]["gdm"]["raw"] == 7.0


def test_missing_jury_and_failed_juror_are_noted():
    rec = _records(1)[0]
    rec["final"]["grand_jury"]["per_juror"]["stub:gpt-ja"] = {"result": None, "error": "404"}
    rep = ja.analyse([rec, {"run_id": "x", "labs": {}, "final": {"grand_jury": None}}], samples=0)
    assert any("stub:gpt-ja has no usable verdict (404)" in n for n in rep["notes"])
    assert any("x: no Grand Jury verdicts" in n for n in rep["notes"])
    assert rep["measures"]["alignment_charter"]["seats"]["meta"]["n"] == 2


def test_ups_weight_sensitivity_is_seeded_and_reports_rank_stability():
    recs = _records()
    a = ja.ups_sensitivity([(r["run_id"], r) for r in recs], samples=50, seed=3)
    b = ja.ups_sensitivity([(r["run_id"], r) for r in recs], samples=50, seed=3)
    assert a == b
    runs = a["runs"]
    assert [x["unit"] for x in runs["baseline"]] == ["r2", "r1", "r0"]
    assert len(runs["perturbations"]) == 8
    assert all(abs(sum(w.values()) - 1) < 1e-9 for _l, w in ja.perturbations(ja.base_weights()))
    d = runs["dirichlet"]
    assert d["samples"] == 50 and 0 <= d["share_same_order"] <= 1 and set(d["rank_range"]) == {"r0", "r1", "r2"}
    assert a["cells"]["baseline"][0]["unit"] == "S1/C"
    assert ja.ups_sensitivity([(recs[0]["run_id"], recs[0])], samples=5)["runs"] is None


def test_cli(tmp_path, capsys):
    for r in _records():
        (tmp_path / f"{r['run_id']}.json").write_text(json.dumps(r))
    assert ja.main([str(tmp_path), "--exclude-ups-family", "claude", "--samples", "10"]) == 0
    out = capsys.readouterr().out
    assert "juror-adjusted" in out and "UPS measures exclude juror family claude" in out
    assert "ups_contribution (R1: ups - ups_without, leave-one-out)" in out
    assert "Dirichlet (10 draws)" in out
    assert ja.main([str(tmp_path), "--json", "--samples", "0"]) == 0
    assert json.loads(capsys.readouterr().out)["n_runs"] == 3


# ---------------------------------------------------------------------------
# Round 6 (R6B-6..8)
# ---------------------------------------------------------------------------

def test_r6_duplicate_run_ids_kept_apart_with_warning(tmp_path, capsys):
    recs = _records(2)
    recs[1]["run_id"] = "r0"                               # the same id in two pilot directories
    rep = ja.analyse(recs, samples=0, paths=["A/r0.json", "B/r0.json"])
    assert set(rep["runs"]) == {"r0 @ A/r0.json", "r0 @ B/r0.json"}
    assert any(n.startswith("WARNING: run id r0 appears 2 times") for n in rep["notes"])
    assert rep["measures"]["alignment_charter"]["seats"]["meta"]["n_runs"] == 2
    for d, r in zip("AB", recs):
        (tmp_path / d).mkdir()
        (tmp_path / d / "r0.json").write_text(json.dumps(r))
    assert ja.main([str(tmp_path / "A"), str(tmp_path / "B"), "--samples", "0"]) == 0
    assert "WARNING: run id r0 appears 2 times" in capsys.readouterr().out


def test_r6_seat_with_no_eligible_juror_is_noted():
    rep = ja.analyse(_records(1), exclude_ups=["claude", "gpt"], samples=0)
    # gdm's only other-family juror for UPS would be claude/gpt: both excluded, gemini is its own family.
    assert "ups_contribution" in rep["uncovered_seats"]["gdm"]
    assert "alignment_charter" not in rep["uncovered_seats"]["gdm"]
    assert any(n.startswith("seat gdm: no eligible juror after own-family exclusion and "
                            "--exclude-ups-family claude,gpt for ups_contribution") for n in rep["notes"])
    assert "meta" not in rep["uncovered_seats"]       # the gemini juror still scores meta's UPS
    assert ja.analyse(_records(1), samples=0)["uncovered_seats"] == {}


def test_r6_print_labels_scales_and_spearman(capsys):
    ja._print(ja.analyse(_records(), samples=10))
    out = capsys.readouterr().out
    assert "jury UPS (0-100) 50.00" in out and "index (0-1) 0.60" in out
    assert "Spearman rho is the informative figure" in out
    assert "±0.1 per weight: Spearman min" in out
