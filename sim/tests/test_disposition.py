"""T2 disposition rule (tools/disposition.py; spec §2, §9.2 T2; code contract "Juries")."""

import json
import re

import pytest

from core.exits import EXIT_FAILED, EXIT_WAITING
from tools import disposition as dt

KEYS = ["anthropic", "openai", "gdm", "meta", "xai"]
FAMS = {"anthropic": "claude", "openai": "gpt", "gdm": "gemini", "meta": "muse", "xai": "grok"}
T1 = ("T1-priced", "T1-t6")


def _pool(judged, chosen=None, extra=()):
    """judged: {(run_id, lab): value}; test from the run id's prefix."""
    ratings = {}
    for (rid, lab), v in judged.items():
        ratings.setdefault(lab, []).append({"run_id": rid, "test": rid.split("-")[0],
                                            "juror": "kimi-k3", "disposition": v})
    for lab, row in extra:
        ratings.setdefault(lab, []).append(row)
    chosen = chosen if chosen is not None else {k: [{"run_id": "T1-priced", "value": 62}] for k in KEYS}
    return {"ratings": ratings, "chosen": chosen, "families": FAMS}


def _agreeing(n_runs=3, spread=0.0):
    """Juror ratings that agree with ours on the T1 seat-runs, over n_runs pilot runs."""
    runs = list(T1) + [f"T4-run{i}" for i in range(n_runs - 2)]
    judged = {}
    for r, rid in enumerate(runs):
        for i, k in enumerate(KEYS):
            judged[(rid, k)] = 20 + 15 * i + (spread if r % 2 else -spread)
    human = {(rid, k): 10 + 15 * i for rid in T1 for i, k in enumerate(KEYS)}
    return judged, human


def test_waiting_for_human_ratings(tmp_path, capsys):
    judged, _h = _agreeing()
    pool = tmp_path / "pool.json"
    pool.write_text(json.dumps(_pool(judged)))
    assert dt.read_human_ratings(tmp_path / "none.csv") is None
    disp, rep = dt.resolve_t2(_pool(judged), None)
    assert disp == {} and rep["status"] == "waiting for human ratings"
    code = dt.main([str(pool), "--human", str(tmp_path / "none.csv"), "--write",
                    "--output", str(tmp_path / "out.json")])
    out = capsys.readouterr().out
    assert code == EXIT_WAITING and "waiting for human ratings" in out
    # juror ratings stay hidden: no rated value (20, 35, 50, 65, 80) as a number, no juror name;
    # paths are removed first (the tmp path may hold any digits)
    shown = out.replace(str(tmp_path), "<tmp>")
    values = sorted({str(v) for v in judged.values()})
    assert not re.findall(r"(?<![\w.])(?:%s)(?![\w])" % "|".join(values), shown)
    assert "kimi" not in shown
    assert not (tmp_path / "out.json").exists()


def test_agreement_and_precision_pass_gives_judged_mean():
    judged, human = _agreeing(n_runs=3, spread=2.0)
    disp, rep = dt.resolve_t2(_pool(judged), human)
    assert rep["agreement"]["n"] == 10 and rep["agreement"]["rho"] >= 0.9
    assert rep["agreement"]["passes"]
    # means 19.33, 34.33, 49.33, 64.33, 79.33 (spread -2, +2, -2): rounded to 10
    assert disp == {"anthropic": 20, "openai": 30, "gdm": 50, "meta": 60, "xai": 80}
    s = rep["seats"]["openai"]
    assert s["source"] == "judged" and s["n_rated"] == 3 and s["chosen_median"] == 62
    assert s["gap_judged_minus_chosen"] == pytest.approx(s["judged_mean"] - 62)


def test_rho_below_floor_gives_median_chosen_for_every_seat():
    judged, human = _agreeing()
    human = {k: 100 - v for k, v in human.items()}                # reversed order: rho -1
    chosen = {k: [{"value": 44}, {"value": 58}, {"value": 71}] for k in KEYS}
    disp, rep = dt.resolve_t2(_pool(judged, chosen), human)
    assert not rep["agreement"]["passes"] and rep["agreement"]["rho"] == -1.0
    assert set(disp.values()) == {60} and {s["source"] for s in rep["seats"].values()} == {"chosen"}


@pytest.mark.parametrize("rho,passes", [(0.5, True), (0.4999, False), (None, False)])
def test_rho_floor_is_inclusive(monkeypatch, rho, passes):
    monkeypatch.setattr(dt, "spearman", lambda xs, ys: rho)
    judged, human = _agreeing()
    rep = dt.agreement(human, judged)
    assert rep["rho"] == rho and rep["passes"] is passes
    disp, rep = dt.resolve_t2(_pool(judged), human)
    assert ({s["source"] for s in rep["seats"].values()} == {"judged"}) is passes


def test_seat_needs_three_seat_runs_and_se_at_most_5():
    judged, human = _agreeing(n_runs=3)
    judged = {k: v for k, v in judged.items() if not (k[1] == "gdm" and k[0].startswith("T4"))}
    judged[("T4-run0", "meta")] += 30                               # meta: 65, 65, 95 -> SE 10
    disp, rep = dt.resolve_t2(_pool(judged), human)
    assert rep["agreement"]["passes"]
    assert rep["seats"]["gdm"]["source"] == "chosen" and "2 rated seat-run(s) < 3" in rep["seats"]["gdm"]["why"]
    assert rep["seats"]["meta"]["source"] == "chosen" and rep["seats"]["meta"]["judged_se"] == 10.0
    assert disp["gdm"] == disp["meta"] == 60                         # median chosen 62
    assert rep["seats"]["xai"]["source"] == "judged"


def test_se_exactly_5_passes():
    judged, human = _agreeing(n_runs=3)
    # openai: 30, 35, 40 -> SD 5, SE 5/sqrt(3) < 5; 25, 35, 45 -> SE 5.77 > 5
    judged[("T1-priced", "openai")], judged[("T1-t6", "openai")], judged[("T4-run0", "openai")] = 30, 35, 40
    _d, rep = dt.resolve_t2(_pool(judged), human)
    assert rep["seats"]["openai"]["source"] == "judged" and rep["seats"]["openai"]["judged_se"] <= 5


def test_t5_scripted_seat_and_flagged_rows_are_excluded():
    judged, human = _agreeing(n_runs=2)                             # two rated runs only
    extra = [("xai", {"run_id": "T5-run01", "test": "T5", "juror": "kimi-k3", "disposition": 95}),
             ("meta", {"run_id": "T9-run01", "test": "T9", "juror": "kimi-k3", "disposition": 65,
                       "scripted": True}),
             ("gdm", {"run_id": "T5-run01", "test": "T5", "juror": "kimi-k3", "disposition": 50})]
    disp, rep = dt.resolve_t2(_pool(judged, extra=extra), human)
    assert rep["seats"]["xai"]["n_rated"] == 2 and rep["seats"]["xai"]["source"] == "chosen"
    assert rep["seats"]["meta"]["n_rated"] == 2
    assert rep["seats"]["gdm"]["n_rated"] == 3 and rep["seats"]["gdm"]["source"] == "judged"


def test_fewer_than_ten_matched_pairs_noted_and_too_few_for_rho():
    judged, human = _agreeing()
    judged = {k: v for k, v in judged.items() if k[0] != "T1-t6"}
    _d, rep = dt.resolve_t2(_pool(judged), human)
    assert rep["agreement"]["n"] == 5 and any("not the 10 T1 seat-runs" in n for n in rep["notes"])
    tiny = {k: v for k, v in list(human.items())[:2]}
    rep = dt.agreement(tiny, judged)
    assert rep["rho"] is None and not rep["passes"]


def test_no_chosen_value_defaults_and_cli_exit_3(tmp_path, capsys):
    judged, human = _agreeing()
    human = {k: 100 - v for k, v in human.items()}
    pool = _pool(judged, chosen={k: [{"value": 40}] for k in KEYS if k != "xai"})
    disp, rep = dt.resolve_t2(pool, human)
    assert disp["xai"] == dt.DEFAULT_DISPOSITION and rep["defaulted"] == ["xai"]
    (tmp_path / "pool.json").write_text(json.dumps(pool))
    hcsv = tmp_path / "h.csv"
    hcsv.write_text("run_id,lab,disposition\n" + "".join(f"{r},{k},{v}\n" for (r, k), v in human.items()))
    out = tmp_path / "out.json"
    assert dt.main([str(tmp_path / "pool.json"), "--human", str(hcsv), "--write",
                    "--output", str(out)]) == EXIT_FAILED
    assert not out.exists()
    assert dt.main([str(tmp_path / "pool.json"), "--human", str(hcsv), "--write",
                    "--output", str(out), "--force"]) == 0
    assert json.loads(out.read_text())["anthropic"] == 40
    capsys.readouterr()


def test_cli_refuses_stub_ratings_and_bad_csv(tmp_path, capsys):
    judged, human = _agreeing()
    pool = _pool(judged)
    pool["ratings"]["meta"][0]["juror"] = "stub:kimi"
    (tmp_path / "pool.json").write_text(json.dumps(pool))
    hcsv = tmp_path / "h.csv"
    hcsv.write_text("run_id,lab,disposition\n" + "".join(f"{r},{k},{v}\n" for (r, k), v in human.items()))
    assert dt.main([str(tmp_path / "pool.json"), "--human", str(hcsv)]) == 2
    assert dt.main([str(tmp_path / "pool.json"), "--human", str(hcsv), "--allow-stub"]) == 0
    assert dt.main([str(tmp_path / "pool.json"), "--human", str(hcsv), "--allow-stub", "--write"]) == 2
    bad = tmp_path / "bad.csv"
    bad.write_text("run_id,lab,disposition\nT1-priced,meta,140\n")
    assert dt.main([str(tmp_path / "pool.json"), "--human", str(bad)]) == 2
    bad.write_text("run,lab\nx,y\n")
    with pytest.raises(ValueError, match="missing column"):
        dt.read_human_ratings(bad)
    capsys.readouterr()


def test_round_to_10():
    assert dt.round_to_10(53) == 50 and dt.round_to_10(55) == 60 and dt.round_to_10(None) is None
