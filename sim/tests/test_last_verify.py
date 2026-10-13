"""Last independent verification (13 Oct): money and recovery after the fix3-fx5/fx6
passes, blinding against new leak strings, the mandate filter on new phrasings, and
README commands. Dry runs, stubs and synthetic records only; no paid call.

Strict xfails encode the behaviour the spec or the README promises and the code does
not yet deliver (BUG-last-<n>)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import campaign
import main as main_mod
import pilot
from core import costs, stubs
from core.config import build_labs_and_world, load_world
from core.mandates import mandate_violation
from core.transcript import disposition_transcript, full_transcript
from tools import identifiability as ID

SIM = Path(__file__).resolve().parent.parent
KEYS = ("anthropic", "openai", "gdm", "meta", "xai")


@pytest.fixture(autouse=True)
def _scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(costs, "DEFAULT_SPEND_FILE", tmp_path / "spend.json")
    monkeypatch.setattr(main_mod, "RUNS_DIR", tmp_path / "runs_default")
    costs.configure()
    yield


def _pilot(root, *argv):
    return pilot.main(list(argv) + ["--dry-run", "--output", str(root), "--force-order",
                                    "--spend-file", str(root / "spend.json"), "--sequential"])


# ---------------------------------------------------------------------------
# Money: a T2 rating that stays unusable after the juror's retries
# ---------------------------------------------------------------------------

@pytest.fixture
def _t2_juror_refuses(monkeypatch):
    """The T2 juror never gives a usable rating (a refusal, garbage): every call counted."""
    calls = []

    def refuse(system, user):
        calls.append(user)
        return "I cannot rate this."
    monkeypatch.setattr(stubs, "disposition_reply", refuse)
    return calls


def _t5_with_unusable_t2(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "approval.json").write_text(json.dumps({"turns": 8, "cuts": []}))
    assert _pilot(root, "T5") == 0
    s = pilot.read_summary(root, "T5")
    assert s["status"] == "completed"
    assert any("T2 rating" in e for e in s["post_run_errors"])


def test_uninterrupted_test_with_an_unusable_t2_rating_completes(tmp_path, _t2_juror_refuses):
    """Control: the uninterrupted run ends completed, the failure recorded, and the
    juror was asked (with its retries)."""
    _t5_with_unusable_t2(tmp_path / "p")
    assert _t2_juror_refuses


@pytest.mark.xfail(strict=True, reason="BUG-last-1: an unusable T2 rating is re-asked (paid) on every "
                                       "re-invocation, and --skip-completed exits 2")
def test_skip_completed_never_reasks_an_unusable_t2_rating(tmp_path, _t2_juror_refuses):
    """README: "--skip-completed (skip it, exit 0)"; "A finished run is never paid for
    again". Spec §9.2 T2: "The juror rates every seat-run blind, once"."""
    root = tmp_path / "p"
    _t5_with_unusable_t2(root)
    before = len(_t2_juror_refuses)
    assert _pilot(root, "T5", "--skip-completed") == 0
    assert len(_t2_juror_refuses) == before


@pytest.mark.xfail(strict=True, reason="BUG-last-1: after a stop before the summary, a test whose T2 "
                                       "rating stayed unusable can never be completed without --force")
def test_stop_before_summary_with_unusable_t2_rating_completes(tmp_path, _t2_juror_refuses):
    """A kill after the run and its T2 attempt but before the summary: re-invoking the
    test must end as the uninterrupted run did (completed), README "run the same
    command again; never --force"."""
    root = tmp_path / "p"
    _t5_with_unusable_t2(root)
    pilot.summary_path(root, "T5").unlink()                     # the stop: no summary yet
    assert _pilot(root, "T5") == 0
    assert pilot.read_summary(root, "T5")["status"] == "completed"


def test_settled_unusable_grand_jury_is_not_reasked_on_post_run_only(tmp_path, monkeypatch):
    """A Grand Jury verdict unusable after the whole fallback is settled: --post-run-only
    and a plain re-invocation ask no juror."""
    grand = []

    def garbage(system, user):
        grand.append(user)
        return "garbage"
    monkeypatch.setattr(stubs, "grand_reply", garbage)
    root = tmp_path / "p"
    root.mkdir()
    (root / "approval.json").write_text(json.dumps({"turns": 8, "cuts": []}))
    assert _pilot(root, "T6") == 0
    rec = json.loads((root / "T6" / "T6-run01.json").read_text())
    assert rec["final"]["grand_jury"]["error"] == "no usable verdict"
    n = len(grand)
    assert n > 0
    assert _pilot(root, "T6", "--post-run-only") == 0
    assert _pilot(root, "T6", "--skip-completed") == 0
    assert len(grand) == n


# ---------------------------------------------------------------------------
# Recovery: the campaign's saved plan after a stop during its first run
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="BUG-last-2: a stop during the campaign's first run discards the "
                                       "saved plan; a plain `run` re-plans with defaults (8 turns, not 4)")
def test_campaign_resume_after_stop_in_first_run_keeps_saved_plan(tmp_path, monkeypatch):
    """README: "`run` again resumes the saved plan: the same cells and set-up". The plan
    was saved with --turns 4; the first run was interrupted (its partial log is on
    disk); `campaign.py run` must resume that run at 4 turns."""
    out, pilot_dir = tmp_path / "runs", tmp_path / "nopilot"
    pilot_dir.mkdir()
    base = ["run", "--dry-run", "--output", str(out), "--spend-file", str(tmp_path / "spend.json"),
            "--pilot-dir", str(pilot_dir)]

    def interrupted(args):
        raise KeyboardInterrupt
    monkeypatch.setattr(main_mod, "run_cell", interrupted)
    with pytest.raises(KeyboardInterrupt):
        campaign.main(base + ["--turns", "4", "--max-runs", "2"])
    seen = []

    def record(args):
        seen.append(args.turns)
        return 2, None
    monkeypatch.setattr(main_mod, "run_cell", record)
    campaign.main(base)
    assert seen and seen[0] == 4


# ---------------------------------------------------------------------------
# Ledger: three processes on one ledger lose no increment
# ---------------------------------------------------------------------------

_WORKER = """
import sys
sys.path.insert(0, {sim!r})
from core.costs import CostTracker
t = CostTracker({led!r})
for i in range(200):
    t.record("m", "actor", "r", 1, 1, cost=0.01, provider="stub")
"""


def test_three_processes_on_one_ledger_lose_no_increment(tmp_path):
    led = str(tmp_path / "spend.json")
    code = _WORKER.format(sim=str(SIM), led=led)
    env = {k: v for k, v in os.environ.items() if not k.endswith(("_API_KEY", "_BASE_URL"))}
    ps = [subprocess.Popen([sys.executable, "-c", code], env=env) for _ in range(3)]
    assert all(p.wait() == 0 for p in ps)
    ledger = json.loads(Path(led).read_text())
    assert ledger["calls"] == 600
    assert ledger["total_usd"] == pytest.approx(6.0)
    assert len((tmp_path / "spend_calls.jsonl").read_text().splitlines()) == 600


# ---------------------------------------------------------------------------
# Blinding (§2): new leak strings
# ---------------------------------------------------------------------------

def _record():
    sys.path.insert(0, str(SIM / "tests"))
    import test_identifiability as TI
    return TI._record()


def _inject(rec, texts, chosen=None):
    for key, text in texts.items():
        rec["turns"][2]["actors"][key]["rationale"] = text
    for key, v in (chosen or {}).items():
        rec["turns"][0]["actors"][key]["disposition"] = v
        rec["final"]["dispositions"][key] = v
    return rec


# Obfuscated forms of names already in core.names (digits for letters, capital I for l).
LEET = {"anthropic": "4nthr0pic", "openai": "Op3nAI", "gdm": "D33pM1nd", "meta": "M3ta", "xai": "E1on"}


@pytest.mark.parametrize("key", KEYS)
def test_leetspeak_names_are_masked(key):
    """BUG-last-3 (fixed): digits and symbols for letters, and I for l, are read
    inside a run of letters (core.mandates.normalise_variants)."""
    rec = _inject(_record(), {key: f"Plan: {LEET[key]} leads here. We buy compute."})
    assert LEET[key] not in str(disposition_transcript(rec, key))
    assert LEET[key] not in str(full_transcript(rec))


def test_identifiability_reads_names_as_the_mask_does():
    """BUG-last-3 (fixed): the tool scores obfuscated names on the same readings as
    the mask, so a form the mask left would be counted; the masked views give it none."""
    for key, form in LEET.items():
        scores = ID.text_scores(f"Plan: {form} leads here. We buy compute.", KEYS)
        assert scores[key] > 0 and all(v == 0 for k, v in scores.items() if k != key)
    rec = _inject(_record(), {k: f"Plan: {v} leads here. We buy compute." for k, v in LEET.items()})
    assert not any(v in str(full_transcript(rec)) for v in LEET.values())
    assert ID.run_identifiability(rec)["grand_jury.text"]["identified"] == 0


def test_identifiability_report_states_the_floor():
    rep = ID.identifiability([("r", _record())])
    assert "floor" in rep["method"]
    assert "a floor" in ID.format_report(rep)


def test_listed_names_in_homoglyph_and_invisible_forms_are_masked():
    """Control: the normalisation (look-alikes, zero-width characters, a zero for o,
    dotted letters) masks these forms of listed names."""
    forms = {"anthropic": "\u0391nthropic", "openai": "Anthro\u200dpic", "gdm": "G\u0435mini",
             "meta": "G.E.M.I.N.I", "xai": "Gr\u043ek"}
    rec = _inject(_record(), {k: f"Plan: {v} leads here." for k, v in forms.items()})
    gj = str(full_transcript(rec))
    assert not any(v in gj for v in forms.values())


PARAPHRASES = [
    ("anthropic", 70, "Seventy parts obedience, thirty parts conscience.", "Seventy"),
    ("gdm", 80, "We defer to the state four times out of five.", "four times out of five"),
    ("xai", 65, "We follow the state about 65 times in a hundred.", "65 times"),
    ("meta", 20, "Our tilt is twenty, so conscience mostly rules.", "twenty"),
]


@pytest.mark.parametrize("key,value,text,probe", PARAPHRASES)
def test_chosen_setting_paraphrases_without_a_cue_word_are_a_documented_limit(key, value, text, probe):
    """BUG-last-4, left on purpose: a paraphrase with no setting, policy, precedence,
    directive or charter word is not chased (core.transcript SETTING_CUE_RE); it
    stays in the view and is not a setting leak candidate either. This pins the limit."""
    rec = _inject(_record(), {key: text}, chosen={key: value})
    assert probe in str(disposition_transcript(rec, key))
    assert not ID.run_identifiability(rec)["setting_leak_candidates"]["count"]


@pytest.mark.parametrize("key,value,text,probe", PARAPHRASES)
def test_chosen_setting_paraphrases_with_a_cue_word_are_masked(key, value, text, probe):
    """The same paraphrases in a sentence that names the setting, a policy or the
    charter lose their numbers (§2: the T2 transcripts mask "the chosen number")."""
    for cue in ("Our policy: ", "Per our charter: ", "Our directive balance: "):
        rec = _inject(_record(), {key: cue + text[0].lower() + text[1:]}, chosen={key: value})
        assert probe.lower() not in str(disposition_transcript(rec, key)).lower()
        assert probe.lower() not in str(full_transcript(rec)).lower()


def test_a_cue_word_with_a_number_left_is_a_setting_leak_candidate():
    """A public statement keeps its figures under weak cues ("charter" is said about
    everything); a sentence with a cue word and a number left is reported."""
    rec = _record()
    rec["turns"][2]["actors"]["gdm"]["public_statement"] = "Our charter guides us; we stand at 81."
    cands = ID.run_identifiability(rec)["setting_leak_candidates"]
    assert cands["count"] == 1 and cands["sentences"][0]["seat"] == "gdm"
    assert cands["sentences"][0]["field"] == "public_statement"


def test_chosen_setting_with_a_cue_is_masked():
    """Control: the same value with a cue word ("setting") is masked."""
    rec = _inject(_record(), {"anthropic": "My setting is 70. We buy compute."}, chosen={"anthropic": 70})
    t2 = str(disposition_transcript(rec, "anthropic"))
    assert "setting is 70" not in t2 and "setting is [N]" in t2


# ---------------------------------------------------------------------------
# Mandate filter (§5.1): new phrasings
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def labs():
    return build_labs_and_world(load_world(), charters=False)[0]


CAUGHT = [
    "No developer may harvest another developer's model parameters.",
    "Any lab that peeks at a rival's checkpoints loses its allocation.",
    "Labs shall not use stolen passwords on the booking portal.",
    "Labs must refrain from duplicating fellow developers' proprietary weights without consent.",
    "Do not compromise other labs' tenancies.",
    "Labs must alert the compute regulator to any irregular tenancy activity.",
    "Labs should share what they witness of peer misconduct with officials.",
    "Labs must volunteer any wrongdoing of their own to the Allocator.",
    "Labs must document and hand over evidence of peers' misbehaviour.",
    "Officials will scrutinise each lab's books every quarter.",
    "Independent assessors will grade each lab's compliance monthly.",
    "Labs must submit to a monthly compliance review.",
    "Labs will undergo surprise compliance inspections.",
    "The maker of Llama must double its compute purchases.",
    "Labs other than the Claude developer must slow down.",
    "The lab headquartered at 1 Hacker Way must divest.",
    "The ChatGPT company should lead national deployment.",
    "Gemini's parent must share compute with smaller labs.",
    "SpaceX's sister lab must cut its risk tolerance.",
]
MISSED = [
    "Labs must not slip into a competitor's compute partition.",
    "Labs shall not commandeer capacity reserved for other labs.",
    "Labs must never lift a rival's training recipes.",
    "Labs must not impersonate other labs to reach their clusters.",
    "Labs must notify Washington whenever a peer breaks the rules.",
    "Labs are obliged to grass on any cheating competitor.",
    "Labs should snitch on rule-breaking peers.",
    "Every lab must disclose any knowledge of another lab's violations.",
    "Each lab's tenancy logs will be combed through by federal staff.",
    "The government will look over every lab's ledger.",
    "The Mission Bay lab must cut capital spending.",
]
KEPT = [
    "Labs should publish quarterly progress on energy efficiency for the public.",
    "Expand access to compute for universities.",
    "Labs should take lessons from rival nations' industrial policy.",
    "Labs should break new ground in chip design.",
    "Labs should tap into domestic talent pools.",
    "Labs should copy best practices from the energy sector.",
    "Labs should check that their products meet consumer safety standards.",
    "Labs should monitor demand for compute and plan purchases accordingly.",
    "Review hiring plans to grow domestic talent.",
    "Labs should inspect their power contracts for hidden costs.",
    "Labs should share surplus capacity with hospitals.",
    "Labs should flag energy shortfalls early in their planning.",
    "Labs should verify that their models are reliable before launch.",
    "Labs should examine long-term risks of rapid scaling.",
    "Labs should get into new markets abroad.",
    "Labs should pull ahead of foreign competitors in capability.",
    "Labs should alert the public to energy price risks.",
    "The Allocator should publish clearer booking rules.",
    "Labs should invest in long-horizon safety research.",
    "Labs should grow their workforce through apprenticeships.",
    "Meta-learning research deserves more funding.",
    "Spark interest in science careers among students.",
    "Labs should build gigawatt-scale data centres in the heartland.",
    "Labs should collaborate with community colleges on training.",
    "Labs should accelerate domestic chip packaging.",
    "Muse on the long-term effects of automation before scaling.",
    "Labs should harvest waste heat from data centres for local heating.",
    "Labs should pry open new export markets.",
    "Labs should break even on infrastructure within two years.",
    "Labs should lift wages for domestic engineers.",
]


@pytest.mark.parametrize("text", CAUGHT)
def test_new_forbidden_mandates_are_dropped(text, labs):
    assert mandate_violation(text, labs)


@pytest.mark.parametrize("text", KEPT)
def test_new_ordinary_mandates_are_kept(text, labs):
    assert not mandate_violation(text, labs)


@pytest.mark.parametrize("text", MISSED)
def test_formerly_missed_forbidden_mandates_are_dropped(text, labs):
    """BUG-last-5 (fixed): §5.1 "Mandates may not mention intrusion, reporting, audits
    or a named lab"; the keyword filter now has these phrasings."""
    assert mandate_violation(text, labs)


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------

def test_readme_campaign_dry_run_command_as_written_is_a_usage_error(capsys):
    """README §9.3 writes "`campaign.py --dry-run` runs the schedule on stubs"; as written
    argparse refuses it (the command `run` is required)."""
    with pytest.raises(SystemExit) as e:
        campaign.main(["--dry-run"])
    assert e.value.code == 2


@pytest.mark.xfail(strict=True, reason="BUG-last-6: README writes `campaign.py --dry-run` (no command); "
                                       "it should read `campaign.py run --dry-run`")
def test_readme_names_the_campaign_dry_run_with_its_command():
    text = (SIM.parent / "README.md").read_text()
    assert "`campaign.py --dry-run`" not in text


@pytest.mark.xfail(strict=True, reason="BUG-last-7: a stop between writing the final record and removing "
                                       "the partial log leaves the partial beside the record for good")
def test_stale_partial_beside_a_finished_record_is_removed_on_reinvocation(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "approval.json").write_text(json.dumps({"turns": 8, "cuts": []}))
    assert _pilot(root, "T5") == 0
    final = root / "T5" / "T5-run01.json"
    partial = root / "T5" / "T5-run01.partial.json"
    partial.write_text(final.read_text())                      # the stop's leftover
    assert _pilot(root, "T5", "--post-run-only") == 0
    assert _pilot(root, "T5", "--skip-completed") == 0
    assert not partial.exists()


@pytest.mark.xfail(strict=True, reason="BUG-last-8: after a stop mid-round, the resumed campaign runs the "
                                       "higher-priority cells' next round before the unfinished round's "
                                       "remaining cells")
def test_resume_mid_round_finishes_the_round_first():
    """§9.3: "Runs go in interleaved rounds, one per cell, in priority A, C, A0, kin, B".
    Stopped during round 1 after S1-A, S2-A and S1-C: round 1's S2-C comes next, not
    S1-A's run 2 (fuzz seed 30012 ran S1-A-run02, S2-A-run02, S1-C-run02 first)."""
    names = ["S1-A", "S2-A", "S1-C", "S2-C", "S1-A0"]
    plan = {"cells": [{"name": n, "runs": 2} for n in names], "runs_per_cell": 2}
    done = {"S1-A": ["S1-A-run01"], "S2-A": ["S2-A-run01"], "S1-C": ["S1-C-run01"]}
    order = [(c["name"], r) for c, r in campaign.run_order(plan, done, 2)]
    assert order[:2] == [("S2-C", 1), ("S1-A0", 1)]
