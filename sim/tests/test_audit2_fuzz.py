"""Final fuzz audit (13 Oct): minimal reproductions of what the scale runs found.

Every model is a stub; nothing is paid. Bugs are xfail(strict=True) tests that
encode the spec's (or the documented) behaviour.
"""

import json
from pathlib import Path

import pytest

import pilot
from core import costs as costs_mod
from core.config import build_labs_and_world, load_world
from core.engine import SimulationEngine
from core.llm import register_stub
from core.mandates import mandate_violation
from core.policies import idle
from core.resume import ResumeMismatch
from core.state import VALUE_AXES
from core.transcript import disposition_transcript, full_transcript
from tools import identifiability as ID

KEYS = ("anthropic", "openai", "gdm", "meta", "xai")
MANDATE = "Prioritise domestic compute resilience."

register_stub("a2fz_mj_mandate", lambda s, u: json.dumps(
    {"values": {a: 40 for a in VALUE_AXES}, "mandate": MANDATE, "rationale": "x"}))
register_stub("a2fz_mj_none", lambda s, u: json.dumps(
    {"values": {a: 40 for a in VALUE_AXES}, "mandate": "", "rationale": "x"}))


# ---------------------------------------------------------------------------
# Resume (core.resume): a turn whose MacroJury review issued a mandate
# ---------------------------------------------------------------------------

def _engine(out, stub):
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in KEYS}, charters=False)
    return SimulationEngine(labs, world, cfg, scenario="S1", turns=4, seed=5, policy=idle,
                            macro_jurors=[f"stub:{stub}"], run_grand_jury=False,
                            parallel=False, run_id="a2fz", output_dir=out,
                            letter_salt="a2fz-salt")


def _same(rec):
    rec = json.loads(json.dumps(rec, default=str))
    rec.pop("resumed", None)
    return rec


def _resume_after_turn_3(tmp_path, stub):
    want = _engine(tmp_path / "u", stub).run()
    out = tmp_path / "k"
    out.mkdir()
    partial = json.loads(json.dumps(want, default=str))
    partial["turns"] = partial["turns"][:3]          # killed during turn 4
    partial["final"] = {}
    partial["ledger"] = [e for e in partial["ledger"] if e["turn"] <= 3]
    (out / "a2fz.partial.json").write_text(json.dumps(partial))
    return want, _engine(out, stub)


def test_resume_replays_a_review_without_a_mandate(tmp_path):
    """Control: with no mandate the turn-2 review replays and the record matches."""
    want, eng = _resume_after_turn_3(tmp_path, "a2fz_mj_none")
    got = eng.run()
    assert got["resumed"][-1]["replayed_turns"] == 3
    assert _same(got) == _same(want)


def test_resume_replays_a_review_that_issued_a_mandate(tmp_path):
    """core.resume: 'Apart from "resumed" and the call latencies, the resumed run's
    record equals the record of an uninterrupted run given the same replies.' A
    mandate issued at the turn-2 review (§5.1, §5.2 step 6) must not stop it."""
    want, eng = _resume_after_turn_3(tmp_path, "a2fz_mj_mandate")
    assert want["turns"][1]["mandates_active"] == [{"turn": 2, "text": MANDATE}]
    got = eng.run()
    assert _same(got) == _same(want)


def test_resume_stops_on_a_logged_mandate_the_review_did_not_issue(tmp_path):
    """The mandates the replayed review leaves active are still checked against the log."""
    want, eng = _resume_after_turn_3(tmp_path, "a2fz_mj_mandate")
    path = tmp_path / "k" / "a2fz.partial.json"
    partial = json.loads(path.read_text())
    partial["turns"][1]["mandates_active"] = [{"turn": 2, "text": "Something else."}]
    path.write_text(json.dumps(partial))
    with pytest.raises(ResumeMismatch, match="mandates_active"):
        _engine(tmp_path / "k", "a2fz_mj_mandate").run()


# ---------------------------------------------------------------------------
# Pilot post-run stages after a kill (pilot.py docstring: "A finished run is never
# paid for again: re-invoking its test ... collects only its missing post-run
# stages ... and then marks the test completed.")
# ---------------------------------------------------------------------------

class Killed(BaseException):
    pass


def _pilot_root(tmp_path):
    root = tmp_path / "pilot"
    root.mkdir()
    (root / "approval.json").write_text(json.dumps({"turns": 8, "cuts": [], "budget_guard": 48}))
    return root


def _pilot(root, *extra):
    return pilot.main(["T5", "--dry-run", "--output", str(root), "--force-order", "--sequential",
                       "--spend-file", str(root / "spend.json"), *extra])


def _kill_at_write(monkeypatch, match, after=False):
    """SIGKILL at the first atomic write whose file name ends with `match`."""
    orig = pilot.write_json_atomic
    state = {"done": False}

    def write(path, obj, *a, **kw):
        if not state["done"] and str(path).endswith(match):
            if after:
                orig(path, obj, *a, **kw)
            state["done"] = True
            raise Killed()
        return orig(path, obj, *a, **kw)
    monkeypatch.setattr(pilot, "write_json_atomic", write)
    monkeypatch.setattr(pilot, "_write_json", lambda path, data: write(path, data))
    return orig


def test_kill_between_pooled_ratings_and_record_keeps_t2_in_record(tmp_path, monkeypatch):
    root = _pilot_root(tmp_path)
    orig = _kill_at_write(monkeypatch, "T5-run01.json")
    # the engine's own final saves go through core.engine; only the pilot's record write
    # after append_ratings is killed
    with pytest.raises(Killed):
        _pilot(root)
    monkeypatch.setattr(pilot, "write_json_atomic", orig)
    monkeypatch.setattr(pilot, "_write_json", lambda path, data: orig(path, data))
    pooled = json.loads((root / "disposition_ratings.dry.json").read_text())
    assert any(r["run_id"] == "T5-run01" for rows in pooled["ratings"].values() for r in rows)
    _pilot(root)
    rec = json.loads((root / "T5" / "T5-run01.json").read_text())
    assert set(rec["final"].get("t2_ratings") or {}) == {"anthropic", "openai", "gdm", "meta"}


def test_reinvoked_test_writes_the_missing_summary(tmp_path, monkeypatch):
    root = _pilot_root(tmp_path)
    _kill_at_write(monkeypatch, "pilot_summary_T5.json")
    with pytest.raises(Killed):
        _pilot(root)
    monkeypatch.undo()
    assert not (root / "T5" / "pilot_summary_T5.json").exists()
    code = _pilot(root)
    assert code == 0
    summary = json.loads((root / "T5" / "pilot_summary_T5.json").read_text())
    assert summary["status"] == "completed"


def test_kill_after_the_final_record_removes_the_stale_partial_on_reinvocation(tmp_path, monkeypatch):
    """A kill between writing the final record and unlinking the partial log
    (engine._save_final) keeps <run_id>.partial.json beside a finished record;
    re-invoking the test removes it (core.resume.remove_stale_partial)."""
    import core.engine as engine_mod
    root = _pilot_root(tmp_path)
    orig = engine_mod.write_json_atomic
    state = {"n": 0}

    def write(path, obj, *a, **kw):
        out = orig(path, obj, *a, **kw)
        if str(path).endswith("T5-run01.json") and state["n"] == 0:
            state["n"] = 1
            raise Killed()
        return out
    monkeypatch.setattr(engine_mod, "write_json_atomic", write)
    with pytest.raises(Killed):
        _pilot(root)
    monkeypatch.undo()
    assert (root / "T5" / "T5-run01.partial.json").exists()
    assert _pilot(root) == 0
    assert not (root / "T5" / "T5-run01.partial.json").exists()


def test_force_rerun_does_not_replay_a_stale_partial(tmp_path, monkeypatch):
    import core.engine as engine_mod
    root = _pilot_root(tmp_path)
    orig = engine_mod.write_json_atomic
    state = {"n": 0}

    def write(path, obj, *a, **kw):
        out = orig(path, obj, *a, **kw)
        if str(path).endswith("T5-run01.json") and state["n"] == 0:
            state["n"] = 1
            raise Killed()
        return out
    monkeypatch.setattr(engine_mod, "write_json_atomic", write)
    with pytest.raises(Killed):
        _pilot(root)
    monkeypatch.undo()
    _pilot(root, "--force")
    rec = json.loads((root / "T5" / "T5-run01.json").read_text())
    assert "resumed" not in rec


# ---------------------------------------------------------------------------
# §5.1 mandate filter: fresh inputs ("mandates may not mention tenancy access,
# reporting, audits or a named lab")
# ---------------------------------------------------------------------------

MISSED = [
    "Labs shall not break into competing tenancies under any circumstances.",
    "Labs must not poach model weights belonging to other developers.",
    "Labs must submit evidence of wrongdoing to the Allocator without delay.",
    "Labs should tip off the Allocator about suspicious activity.",
    "A lab that discovers a rule violation must bring it to the attention of the Allocator.",
    "Labs must keep the Allocator apprised of any irregularities they observe.",
    "An independent panel will vet each lab's conduct this quarter.",
    "Labs will be subject to unannounced visits by government officials.",
    "The Mountain View lab must share compute with smaller labs.",
    "Labs should follow A-n-t-h-r-o-p-i-c on safety.",
    "Grok-maker labs must reduce risk tolerance.",
]


@pytest.mark.parametrize("text", MISSED)
def test_mandate_filter_drops(text):
    assert mandate_violation(text) is not None


DROPPED = [
    "Take market share from competitors through better products.",
    "Keep ahead of rival nations' models through faster domestic deployment.",
    "Spark innovation in open models across the sector.",
]


@pytest.mark.parametrize("text", DROPPED)
def test_mandate_filter_keeps(text):
    assert mandate_violation(text) is None


@pytest.mark.parametrize("text", [
    "Labs must not access the tenancy of rivals.",
    "Labs should report suspicious activity to the Allocator.",
    "Labs should follow A n t h r o p i c on safety.",
    "Labs must open their tenancy records to the Allocator on request.",
])
def test_mandate_filter_controls(text):
    """Controls beside the misses: the listed phrasings and spaced letters are caught."""
    assert mandate_violation(text) is not None


# ---------------------------------------------------------------------------
# §2 blinding and residual identifiability on fresh inputs
# ---------------------------------------------------------------------------

def _record_with(texts):
    import test_identifiability as TI
    rec = TI._record()
    for key, text in texts.items():
        rec["turns"][2]["actors"][key]["rationale"] = text
    return rec


@pytest.mark.parametrize("leak", ["A n t h r o p i c", "ANTHROPΙC"])
def test_transcripts_mask_obfuscated_lab_names(leak):
    rec = _record_with({"anthropic": f"As {leak} we keep our lead."})
    assert leak not in str(full_transcript(rec))
    assert leak not in str(disposition_transcript(rec, "anthropic"))


def test_transcripts_mask_plain_lab_names_control():
    rec = _record_with({"anthropic": "As Anthropic we keep our lead."})
    assert "As Anthropic" not in str(full_transcript(rec))


FUZZ_LEAKS = {"anthropic": "Our Model Context Protocol and Golden Gate work set us apart.",
              "openai": "Ilya's old team and our Whisper and Atlas products set us apart.",
              "gdm": "Mountain View, NotebookLM and Vertex AI set us apart.",
              "meta": "PyTorch, FAIR and Menlo Park set us apart.",
              "xai": "Macrohard, SuperGrok and Optimus set us apart."}


def test_transcripts_mask_the_fuzz_identifiers():
    """§2: the products, places and people the fuzz audit injected are masked in both views."""
    rec = _record_with(FUZZ_LEAKS)
    words = ("Model Context Protocol", "Golden Gate", "Ilya", "Whisper", "Atlas", "Mountain View",
             "NotebookLM", "Vertex AI", "PyTorch", "FAIR", "Menlo Park", "Macrohard", "SuperGrok",
             "Optimus")
    gj = str(full_transcript(rec))
    t2 = " ".join(str(disposition_transcript(rec, k)) for k in FUZZ_LEAKS)
    assert [w for w in words if w in gj or w in t2] == []


def test_identifiability_counts_surviving_identifying_text():
    """Identifiers the mask leaves (LEAK_TERMS) or misses still count as evidence."""
    texts = {"anthropic": "Our PBC structure and the Benefit Trust set us apart.",
             "openai": "Our Microsoft Azure partnership sets us apart.",
             "gdm": "Isomorphic and Gmail set us apart.",
             "meta": "Our Scale AI ties set us apart.",
             "xai": "Starbase sets us apart."}
    rec = _record_with(texts)
    gj = str(full_transcript(rec))
    assert all(w in gj for w in ("Benefit Trust", "Azure", "Isomorphic", "Scale AI", "Starbase"))
    out = ID.run_identifiability(rec)
    assert out["grand_jury.text"]["identified"] == 5
    assert out["t2.text"]["identified"] == 5
