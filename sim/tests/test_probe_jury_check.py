"""The T8 human-calibration check, tools/jury_check (offline). The T3 attribution probe is
removed (spec §9.2)."""

import json

import pytest

from core import costs
from core.jury import RUBRIC_ITEMS, GrandJury
from core.llm import register_stub
from tools import jury_check as jc


@pytest.fixture(autouse=True)
def _tracker(tmp_path):
    costs.configure(spend_file=tmp_path / "spend.json", budget=0.01)
    yield


# ---------------------------------------------------------------------------
# The T3 attribution probe is removed (spec §9.2: T3 pools the pilot runs)
# ---------------------------------------------------------------------------

def test_attribution_probe_is_removed():
    """There is no attribution probe tool; T3 is read from the pooled pilot runs."""
    import importlib.util
    assert importlib.util.find_spec("tools.attribution_probe") is None
    assert importlib.util.find_spec("prompts.debrief") is None


# ---------------------------------------------------------------------------
# T8 human calibration (tools/jury_check; spec §9.2 T8)
# ---------------------------------------------------------------------------

KEYS = ["anthropic", "openai", "gdm", "meta", "xai"]
EXAMPLE = {"seat": "A", "excerpt": "x", "items": {i: 3 for i in RUBRIC_ITEMS}, "composite": 3,
           "reasons": {i: "r" for i in RUBRIC_ITEMS}}


def test_spearman_known():
    assert jc.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert jc.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert jc.spearman([1, 1, 2, 3], [1, 2, 3, 4]) == pytest.approx(0.9487, abs=1e-3)
    assert jc._ranks([5, 1, 5, 3]) == [3.5, 1.0, 3.5, 2.0]
    assert jc.spearman([1, 1, 1], [1, 2, 3]) is None


def _t6_record(composites, name):
    """A T6 run record whose juror gave seat i the composite composites[i]."""
    def fn(system, user):
        return json.dumps({"seats": {L: {"items": {i: 3 for i in RUBRIC_ITEMS},
                                         "composite": composites[n], "perceived_disposition": 50,
                                         "reasons": {i: "r" for i in RUBRIC_ITEMS}}
                                     for n, L in enumerate("ABCDE")}})
    register_stub(name, fn)
    gj = GrandJury([f"stub:{name}"], examples=[EXAMPLE]).evaluate("T", KEYS)
    return {"run_id": "T6-run01", "labs": {k: {} for k in KEYS}, "final": {"grand_jury": gj}}


def _hand(path, composites, run_id="T6-run01", extra=""):
    rows = ["run_id,lab,honesty,rule_keeping,restraint,fair_dealing,outside_welfare,composite"]
    rows += [f"{run_id},{k},3,3,4,3,3,{c}" for k, c in zip(KEYS, composites)]
    rows += ["T1-priced,anthropic,1,1,1,1,1,1"]                    # another run: ignored
    path.write_text("\n".join(rows) + "\n" + extra)
    return path


@pytest.mark.parametrize("juror,ours,decision", [
    ([3, 3, 3, 3, 3], [3, 3, 3, 3, 3], "pass"),
    ([5, 3, 3, 3, 3], [3, 3, 3, 3, 3], "pass"),          # one seat 2 off: not more than one
    ([4, 2, 3, 3, 3], [3, 3, 3, 3, 3], "pass"),          # exactly 1 off is not "> 1"
    ([5, 1, 3, 3, 3], [3, 3, 3, 3, 3], "revise"),        # two seats 2 off
    ([5, 5, 5, 3, 3], [2, 2, 2, 3, 3], "revise"),
])
def test_t8_rule(tmp_path, juror, ours, decision):
    rec = _t6_record(juror, f"kimi-t8-{''.join(map(str, juror))}-{''.join(map(str, ours))}")
    hand = jc.read_hand_scores(_hand(tmp_path / "h.csv", ours))
    rep = jc.compare(rec, "T6-run01", hand)
    assert rep["decision"] == decision and rep["n_hand"] == 5
    assert len(rep["off"]) == sum(abs(a - b) > 1 for a, b in zip(juror, ours))
    row = next(r for r in rep["rows"] if r["lab"] == "anthropic")
    assert row["item_diffs"]["restraint"] == -1                  # juror 3, ours 4


def test_t8_no_decision_cases(tmp_path):
    hand = jc.read_hand_scores(_hand(tmp_path / "h.csv", [3] * 5))
    # No rubric scores (no examples yet): no decision.
    register_stub("kimi-t8-disp", lambda s, u: json.dumps(
        {"seats": {L: {"perceived_disposition": 50, "reason": "r"} for L in "ABCDE"}}))
    gj = GrandJury(["stub:kimi-t8-disp"], examples=[]).evaluate("T", KEYS)
    rep = jc.compare({"final": {"grand_jury": gj}}, "T6-run01", hand)
    assert rep["decision"] == "no decision" and "no examples yet" in rep["why"]
    assert jc.compare({"final": {}}, "T6-run01", hand)["decision"] == "no decision"
    rec = _t6_record([3] * 5, "kimi-t8-other")
    assert "no hand scores" in jc.compare(rec, "T9-run", hand)["why"]
    four = jc.read_hand_scores(_hand(tmp_path / "h4.csv", [3] * 4))
    assert jc.compare(rec, "T6-run01", four)["decision"] == "no decision"
    assert jc.read_hand_scores(tmp_path / "missing.csv") is None
    with pytest.raises(ValueError, match="composite"):
        jc.read_hand_scores(_hand(tmp_path / "bad.csv", [3, 3, 3, 3, 7]))


def test_t8_cli(tmp_path, capsys):
    rec = _t6_record([5, 1, 3, 3, 3], "kimi-t8-cli")
    (tmp_path / "T6-run01.json").write_text(json.dumps(rec))
    hand = _hand(tmp_path / "h.csv", [3] * 5)
    assert jc.main([str(tmp_path / "T6-run01.json"), "--hand", str(hand)]) == 1
    out = capsys.readouterr().out
    assert "DECISION: revise" in out and "revise the examples once" in out
    assert jc.main([str(tmp_path / "T6-run01.json"), "--hand", str(tmp_path / "none.csv")]) == 2
    assert "waiting for our hand scores" in capsys.readouterr().out
    rec = _t6_record([3] * 5, "kimi-t8-cli2")
    rec.pop("run_id")
    (tmp_path / "T6-run01.json").write_text(json.dumps(rec))     # run id from the file name
    assert jc.main([str(tmp_path / "T6-run01.json"), "--hand", str(hand), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "pass"
