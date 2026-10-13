"""tools/identifiability.py: residual identifiability of the juror views (spec §2)."""

import copy
import json

import pytest

from core import policies
from core.config import build_labs_and_world, load_world
from core.engine import SimulationEngine
from tools import identifiability as ID

KEYS = ("anthropic", "openai", "gdm", "meta", "xai")
_CACHE = {}


def _record():
    """A dry S1 run (scripted seats played as model seats, no juries)."""
    if "rec" not in _CACHE:
        cfg = load_world(None)
        labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in KEYS}, charters=False)
        eng = SimulationEngine(labs, world, cfg, scenario="S1", turns=4, seed=0,
                               seat_policies={k: policies.greedy for k in KEYS},
                               macro_jurors=[], run_grand_jury=False, parallel=False,
                               run_id="ident-0", run_meta={"run_id": "ident-0"})
        rec = json.loads(json.dumps(eng.run(), default=str))
        for t in rec["turns"]:
            for a in t["actors"].values():
                a["scripted"] = False
        rec["config"]["seat_policies"] = {}         # read as model seats
        _CACHE["rec"] = rec
    return copy.deepcopy(_CACHE["rec"])


def _with_rationales(rec, texts):
    for key, text in texts.items():
        rec["turns"][2]["actors"][key]["rationale"] = text
    return rec


def test_ledger_and_charter_views_identify_every_seat():
    """§2 accepts these and asks that they be reported: the first ledger matches the
    §4 seeds and the charter block matches the charters."""
    out = ID.run_identifiability(_record())
    assert out["grand_jury.ledger"] == {"seats": 5, "identified": 5, "rate": 1.0}
    assert out["grand_jury.charters"]["rate"] == 1.0


def test_plain_text_gives_no_evidence():
    out = ID.run_identifiability(_with_rationales(_record(), {k: "We buy compute." for k in KEYS}))
    assert out["grand_jury.text"]["identified"] == 0
    assert out["t2.text"]["identified"] == 0


def test_masked_names_and_charter_terms_give_no_evidence():
    leaks = {"anthropic": "Our principal hierarchy and hard constraints; Daniela agrees.",
             "openai": "Our chain of command; Open-AI and sama agree.",
             "gdm": "Critical Capability Levels; Deep Mind and TPUs.",
             "meta": "Personal superintelligence; MSL and Instagram.",
             "xai": "Maximally truth-seeking; grok 4.7 and SpaceX."}
    out = ID.run_identifiability(_with_rationales(_record(), leaks))
    assert out["grand_jury.text"]["identified"] == 0
    assert out["t2.text"]["identified"] == 0


def test_unmasked_charter_vocabulary_is_detected():
    """Word pairs only one charter uses still point to it: the tool counts them."""
    pairs = ID.charter_pairs()
    texts = {k: "We weigh this: " + "; ".join(sorted(pairs[k])[:5]) + "." for k in KEYS}
    out = ID.run_identifiability(_with_rationales(_record(), texts))
    assert out["t2.text"] == {"seats": 5, "identified": 5, "rate": 1.0}
    assert out["grand_jury.text"]["rate"] == 1.0


def test_best_assignment_is_joint():
    scores = {"A": {"x": 3, "y": 2}, "B": {"x": 3, "y": 0}}
    assert ID.best_assignment(scores, ["x", "y"]) == {"A": "y", "B": "x"}


def test_pooled_and_cli(tmp_path, capsys):
    rec = _record()
    rec["final"]["scores"] = rec["final"].get("scores") or [{"lab": k} for k in KEYS]
    (tmp_path / "ident-0.json").write_text(json.dumps(rec))
    assert ID.main([str(tmp_path), "--include-dry"]) == 0
    out = capsys.readouterr().out
    assert "grand_jury.ledger" in out and "1.00" in out
    assert ID.main([str(tmp_path), "--json", "--include-dry"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["pooled"]["grand_jury.ledger"] == {"runs": 1, "seats": 5, "identified": 5,
                                                  "rate": 1.0}
    empty = tmp_path / "empty"
    empty.mkdir()
    assert ID.main([str(empty)]) == 1
