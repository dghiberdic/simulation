"""Offline checks for the pilot driver, its presets, main.py's guards and the charter-rating script.

Everything here runs with stub models, so the suite stays offline and fast.
Real-mode paths are exercised only up to their guards, with the engine replaced
by a fake that fails the test if it is ever constructed: no test can reach a
provider even when sim/.env holds keys.
"""

import json
from pathlib import Path

import pytest

import main as main_mod
import pilot
import rate_charters
from core.config import CONFIG_DIR, load_json
from core import costs
from core.costs import BudgetExceeded, configure, get_tracker
from core.engine import RunAborted
from core.llm import register_stub
from core.policies import POLICIES

PILOT_FILE = Path(pilot.__file__).resolve().parent / "config" / "pilot.json"
LAB_KEYS = ("anthropic", "openai", "gdm", "meta", "xai")


@pytest.fixture(autouse=True)
def _scratch(tmp_path, monkeypatch):
    """Default pilot/run dirs and the spend ledger point at scratch space."""
    monkeypatch.setattr(pilot, "PILOT_DATA", tmp_path / "pilot_default")
    monkeypatch.setattr(pilot, "RATINGS_FILE", tmp_path / "pilot_default" / "disposition_ratings.json")
    monkeypatch.setattr(main_mod, "RUNS_DIR", tmp_path / "runs_default")
    monkeypatch.setattr(costs, "DEFAULT_SPEND_FILE", tmp_path / "spend.json")
    configure()
    yield


class _NoEngine:
    """Stands in for SimulationEngine on real-mode paths: constructing it fails the test."""
    def __init__(self, *a, **k):
        raise AssertionError("a real-mode guard let the engine start")


def _merged(name):
    data = load_json(PILOT_FILE)
    return {**data["defaults"], **data["presets"][name]}


# ---------------------------------------------------------------------------
# config/pilot.json (C4-5, C4-12)
# ---------------------------------------------------------------------------

def test_pilot_config_loads_and_presets_well_formed():
    data = load_json(PILOT_FILE)
    assert data["budget_guard"] == 100
    for name, preset in data["presets"].items():
        merged = {**data["defaults"], **preset}
        for key in ("scenario", "fog", "turns", "runs", "a2a", "brief",
                    "choose_disposition", "grand_jury"):
            assert key in merged, f"{name} missing {key}"
        assert "condition" in merged or "conditions" in merged, f"{name} has no condition"
        assert merged["scenario"] in ("S1", "S2")
        assert isinstance(merged["turns"], int) and merged["turns"] >= 1
        assert isinstance(merged["runs"], int) and merged["runs"] >= 1
        assert merged["choose_disposition"] is True, f"{name}: seats choose at the first prompt (G6)"
        for seat, policy in (merged.get("scripted_seats") or {}).items():
            assert seat in LAB_KEYS and policy in POLICIES


def test_budget_safe_preset_set():
    presets = load_json(PILOT_FILE)["presets"]
    # H6: T3 pools every F3 run + the probe, T6 reads the T1/T4 debriefs (aux §4).
    assert not {"T2", "T3", "T6", "T7", "T8"} & set(presets), \
        "T2 pooled, T3 from all F3 runs, T6 from T1/T4 debriefs, T7 from T1a, T8 on main runs"
    t5 = _merged("T5")
    assert t5["runs"] == 1 and t5["grand_jury"] is False and t5["conditions"] == ["A", "C"]
    assert t5["scripted_seats"] == {"xai": "t5_scripted"}
    assert _merged("T5false")["conditions"] == ["A"]
    for name in ("T1a", "T1b", "T4", "T6neutral"):
        assert _merged(name).get("debrief") is True, name
    assert _merged("T6neutral")["brief"] == "neutral"
    assert "CONDITIONAL" in _merged("T6neutral")["note"]


def test_presets_print_only_their_decisions():
    """P22/P36/K3: presets never print the pooled lines (T1, T3, T6, T7) nor T9."""
    presets = load_json(PILOT_FILE)["presets"]
    for name, p in presets.items():
        assert isinstance(p.get("decides"), list), name
        assert not {"T1", "T3", "T6", "T7", "T9"} & set(p["decides"]), name
    assert _merged("T5")["decides"] == ["T5"] and _merged("T5false")["decides"] == ["T5false"]
    assert _merged("T1a")["decides"] == [] and _merged("T1b")["decides"] == []
    assert _merged("T4")["decides"] == ["T4"] and _merged("T6neutral")["decides"] == ["T6neutral"]


def test_t1b_ladder_is_well_formed():
    data = load_json(PILOT_FILE)
    ladder = data["presets"]["T1b"]["ladder"]
    assert isinstance(ladder, list) and ladder
    for rung in ladder:
        for dotted, value in rung.items():
            assert "." in dotted and isinstance(value, (int, float)) and not isinstance(value, bool)
    assert len(pilot.parse_rotation(data["presets"]["T1b"]["rotate"])) == 4


# ---------------------------------------------------------------------------
# Run planning (C4-2)
# ---------------------------------------------------------------------------

def test_plan_runs_rung_suffix_and_runs_per_seat():
    """P27: run ids carry the swap and count within it, so one-seat-at-a-time runs never collide."""
    plan = pilot.plan_runs("T1b", ["A"], 2, ["meta:gdm", "meta:xai"], rung=1)
    assert [p["run_id"] for p in plan] == ["T1b-meta-gdm-run01-rung1", "T1b-meta-gdm-run02-rung1",
                                           "T1b-meta-xai-run01-rung1", "T1b-meta-xai-run02-rung1"]
    assert [p["rotation"] for p in plan] == ["meta:gdm", "meta:gdm", "meta:xai", "meta:xai"]
    assert [p["seed"] for p in plan] == [0, 1, 0, 1]
    one_by_one = (pilot.plan_runs("T1b", ["A"], 2, ["meta:gdm"], rung=1)
                  + pilot.plan_runs("T1b", ["A"], 2, ["meta:xai"], rung=1))
    assert [p["run_id"] for p in one_by_one] == [p["run_id"] for p in plan]
    # P49: with a rotation the count is per seat, with or without a rung.
    base = pilot.plan_runs("T1b", ["A"], 2, ["meta:anthropic", "meta:openai"], rung=None)
    assert [p["rotation"] for p in base] == ["meta:anthropic"] * 2 + ["meta:openai"] * 2
    assert [p["run_id"] for p in base] == ["T1b-meta-anthropic-run01", "T1b-meta-anthropic-run02",
                                           "T1b-meta-openai-run01", "T1b-meta-openai-run02"]
    assert [p["seed"] for p in base] == [0, 1, 0, 1]
    assert not {p["run_id"] for p in base} & {p["run_id"] for p in plan}
    multi = pilot.plan_runs("T5", ["A", "C"], 1, [], rung=None)
    assert [p["run_id"] for p in multi] == ["T5-A-run01", "T5-C-run01"]
    assert pilot.summary_name("T1b", 1, "meta:gdm,meta:xai") == "T1b-meta-gdm_meta-xai-rung1"
    assert pilot.summary_name("T1b", 1, None) == "T1b-rung1" and pilot.summary_name("T0", None, None) == "T0"


def test_p49_run_counts_are_per_seat():
    """P49: T1b = 1 run per seat (4), the 2-seat trim = 2, one seat = 1, a rung = 2 per seat, --runs per seat."""
    data = pilot.load_pilot()
    t1b = pilot.resolve_preset(data, "T1b")
    count = lambda runs_arg, rung, rotate: len(pilot.plan_runs(
        "T1b", ["A"], pilot.resolve_runs(t1b, runs_arg, rung, pilot.parse_rotation(rotate)),
        pilot.parse_rotation(rotate), rung))
    assert count(None, None, t1b["rotate"]) == 4
    assert count(None, None, "meta:gdm,meta:xai") == 2
    assert count(None, None, "meta:gdm") == 1
    assert count(None, 1, "meta:gdm") == 2
    assert count(3, None, "meta:gdm,meta:xai") == 6
    t6n = pilot.resolve_preset(data, "T6neutral")
    assert t6n["runs"] == 1 and pilot.resolve_runs(t6n, None, None, []) == 1          # P50
    # estimated cost before starting: README estimate per run, scaled by turns
    assert pilot.estimate_cost(t1b, 2, 6, 1) == round(2 * t1b["est_cost_per_rung_run"], 2)
    assert pilot.estimate_cost(t1b, 4, 3, None) == round(4 * t1b["est_cost_per_run"] / 2, 2)


def test_p51_run_ids_follow_the_preset_conditions():
    """P51: T5 --conditions C still writes T5-C-run01; a single-condition preset run in another condition is named."""
    assert [p["run_id"] for p in pilot.plan_runs("T5", ["C"], 1, [], None, ["A", "C"])] == ["T5-C-run01"]
    assert [p["run_id"] for p in pilot.plan_runs("T5false", ["A"], 1, [], None, ["A"])] == ["T5false-run01"]
    assert [p["run_id"] for p in pilot.plan_runs("T5false", ["C"], 1, [], None, ["A"])] == ["T5false-C-run01"]
    assert pilot.summary_name("T5", None, None, ["C"]) == "T5-C"
    assert pilot.summary_name("T5", None, None, ["A", "C"]) == "T5-A_C"


def test_parse_rotation_rejects_bad_pairs():
    assert pilot.parse_rotation("meta:gdm, meta:xai") == ["meta:gdm", "meta:xai"]
    for bad in ("meta-gdm", "meta:meta", "meta:google"):
        with pytest.raises(SystemExit):
            pilot.parse_rotation(bad)


# ---------------------------------------------------------------------------
# pilot.py --dry-run
# ---------------------------------------------------------------------------

def test_dry_run_without_output_uses_default_dir(tmp_path):
    """C4-6 / H7: no --output saves a dry run's logs, summary and ratings under data/pilot/dry/<TEST>."""
    assert pilot.main(["--dry-run", "T0"]) == 0
    out = tmp_path / "pilot_default" / "dry" / "T0"
    assert (out / "T0-run01.json").exists()
    assert not (tmp_path / "pilot_default" / "T0").exists(), "dry logs never land in the real dir"
    summary = json.loads((out / "pilot_summary_T0.json").read_text())
    assert summary["runs_completed"] == 1 and summary["status"] == "completed"
    assert (out / "disposition_ratings.dry.json").exists()
    assert not (tmp_path / "pilot_default" / "disposition_ratings.json").exists(), \
        "dry ratings never enter the real pooled file"
    assert get_tracker().summary()["session_cost"] == 0.0
    assert get_tracker().persisted_total() == 0.0


def test_dry_run_t0_usage_table(tmp_path):
    assert pilot.main(["--dry-run", "T0", "--output", str(tmp_path)]) == 0
    summary = json.loads((tmp_path / "pilot_summary_T0.json").read_text())
    usage = summary["usage"]
    assert set(usage["by_actor_model"]) == {f"stub:actor_{k}" for k in LAB_KEYS}
    row = usage["by_actor_model"]["stub:actor_anthropic"]
    assert row["seat_turns"] == 2 and row["proposal_calls"] >= 2 and row["forfeits"] == 0
    for field in ("proposal_failure_rate", "message_failure_rate", "stop_max_tokens",
                  "stop_refusal", "cached_tokens", "reasoning_tokens", "cost"):
        assert field in row
    purposes = {r["purpose"] for r in usage["jurors"].values()}
    assert {"grand_jury", "disposition_jury"} <= purposes
    assert usage["projection"]["turns_measured"] == 2


def test_dry_run_rung_never_overwrites_base_runs(tmp_path):
    assert pilot.main(["--dry-run", "T1b", "--runs", "1", "--turns", "2",
                       "--output", str(tmp_path)]) == 0
    base = (tmp_path / "T1b-meta-anthropic-run01.json").read_text()
    assert pilot.main(["--dry-run", "T1b", "--rung", "1", "--rotate", "meta:gdm,meta:xai",
                       "--turns", "2", "--output", str(tmp_path)]) == 0
    assert (tmp_path / "T1b-meta-anthropic-run01.json").read_text() == base
    rung_logs = sorted(p.name for p in tmp_path.glob("T1b-*-rung1.json"))
    assert rung_logs == [f"T1b-meta-{s}-run0{i}-rung1.json" for s in ("gdm", "xai") for i in (1, 2)], \
        "2 runs per rotated seat"
    summary = json.loads((tmp_path / "pilot_summary_T1b-meta-gdm_meta-xai-rung1.json").read_text())
    assert summary["overrides"] == {"intrusion.gain_share": 0.35}
    assert summary["rotation"] == ["meta:gdm", "meta:xai"]
    assert [r["rotation"] for r in summary["per_run"]] == ["meta:gdm", "meta:gdm", "meta:xai", "meta:xai"]
    assert (tmp_path / "pilot_summary_T1b.json").exists()
    record = json.loads((tmp_path / "T1b-meta-xai-run01-rung1.json").read_text())
    assert record["config"]["overrides"] == {"intrusion.gain_share": 0.35}
    assert record["config"]["rotation"] == "meta:xai"
    assert record["config"]["run_meta"]["rung"] == 1 and record["config"]["dry_run"] is True
    # xAI trails after the swap with meta.
    assert summary["per_run"][2]["trailing"] == "xai"


def test_dry_run_t6neutral_builds_debriefs(tmp_path):
    assert pilot.main(["--dry-run", "T6neutral", "--runs", "1", "--turns", "2",
                       "--output", str(tmp_path)]) == 0
    data = json.loads((tmp_path / "T6neutral-run01.debrief.json").read_text())
    assert set(data) == set(LAB_KEYS)
    for entry in data.values():
        assert entry["prompt_chars"] > 0 and isinstance(entry["answer"], str)
        assert entry["calls"] == 1
    summary = json.loads((tmp_path / "pilot_summary_T6neutral.json").read_text())
    assert set(summary["decisions"]) == {"T6neutral"}, "only the lines the preset decides (P22, P36)"
    assert "pool with summarize_run" in summary["decisions"]["T6neutral"]


def test_dry_run_t5_scripted_seat_skips_debrief_and_jury(tmp_path):
    assert pilot.main(["--dry-run", "T5", "--output", str(tmp_path)]) == 0
    assert {p.name for p in tmp_path.glob("T5-*-run01.json")} == {"T5-A-run01.json", "T5-C-run01.json"}
    record = json.loads((tmp_path / "T5-A-run01.json").read_text())
    assert record["turns"][0]["actors"]["xai"]["scripted"]
    assert record["final"].get("grand_jury") in (None, {})
    pooled = json.loads((tmp_path / "disposition_ratings.dry.json").read_text())
    assert "xai" not in pooled["ratings"], "the scripted seat is not a model: not rated"


def test_disposition_ratings_pool_and_replace_on_rerun(tmp_path):
    """Dry ratings pool in the output dir's dry file only, even with --ratings-file (H7)."""
    real = tmp_path / "pooled.json"
    ratings = tmp_path / "out" / "disposition_ratings.dry.json"
    args = ["--dry-run", "T0", "--output", str(tmp_path / "out"), "--ratings-file", str(real)]
    assert pilot.main(args) == 0
    assert not real.exists(), "a dry run never writes the pooled ratings file"
    first = json.loads(ratings.read_text())
    assert set(first["ratings"]) == set(LAB_KEYS)
    for key, rows in first["ratings"].items():
        assert rows and all(r["family"] != first["families"][key] for r in rows)
        assert all(r["test"] == "T0" and r["run_id"] == "T0-run01" and r["dry_run"] for r in rows)
    assert all(c[0]["value"] == 50 for c in first["chosen"].values())
    # Another test adds to the pool; re-running T0 replaces its own rows.
    assert pilot.main(["--dry-run", "T1a", "--turns", "2", "--output", str(tmp_path / "out")]) == 0
    assert pilot.main(args) == 0
    pooled = json.loads(ratings.read_text())
    assert len(pooled["ratings"]["anthropic"]) == 2 * len(first["ratings"]["anthropic"])
    assert sorted(r["test"] for r in pooled["runs"]) == ["T0", "T1a"]


def test_append_ratings_refuses_dry_rows_in_the_pooled_file(tmp_path):
    rows = {"ratings": {}, "chosen": {}, "families": {}}
    with pytest.raises(ValueError):
        pilot.append_ratings(tmp_path / "disposition_ratings.json", rows, "T0", "T0-run01", True)
    pilot.append_ratings(tmp_path / "x.dry.json", rows, "T0", "T0-run01", True)
    assert (tmp_path / "x.dry.json").exists()


def test_append_ratings_unit(tmp_path):
    path = tmp_path / "p.json"
    rows = {"ratings": {"meta": [{"juror": "gpt-6-sol", "disposition": 40, "test": "T1a",
                                  "run_id": "T1a-run01", "dry_run": False}]},
            "chosen": {"meta": [{"value": 70, "test": "T1a", "run_id": "T1a-run01", "dry_run": False}]},
            "families": {"meta": "muse"}}
    pilot.append_ratings(path, rows, "T1a", "T1a-run01", False)
    pilot.append_ratings(path, rows, "T1a", "T1a-run01", False)
    data = json.loads(path.read_text())
    assert len(data["ratings"]["meta"]) == 1 and len(data["chosen"]["meta"]) == 1
    assert data["runs"] == [{"test": "T1a", "run_id": "T1a-run01", "dry_run": False}]


# ---------------------------------------------------------------------------
# Real-mode guards (C4-4, C4-15) — never reach the engine
# ---------------------------------------------------------------------------

def test_real_pilot_refuses_placeholder_values(monkeypatch):
    monkeypatch.setattr(pilot, "SimulationEngine", _NoEngine)
    monkeypatch.setattr(pilot, "placeholder_labs", lambda: ["meta"])
    assert pilot.main(["T0"]) == 2


def test_real_pilot_preflight_failure_aborts_before_any_call(monkeypatch, capsys):
    monkeypatch.setattr(pilot, "SimulationEngine", _NoEngine)
    monkeypatch.setattr(pilot, "placeholder_labs", lambda: [])
    seen = {}

    def fake_preflight(models, providers=None):
        seen["models"] = list(models)
        return ["MUSE_BASE_URL is not set (muse-spark-1.3)"]

    monkeypatch.setattr(pilot, "preflight", fake_preflight)
    assert pilot.main(["T5"]) == 2
    assert "Preflight failed" in capsys.readouterr().out
    # Every model the pilot would call is checked; the scripted xAI seat is not.
    assert "claude-sonnet-5" in seen["models"] and "muse-spark-1.3" in seen["models"]
    assert "grok-4.7" not in seen["models"]


class _FakeEngine:
    """Records construction kwargs; run() raises the configured exception."""
    raises = None
    kwargs = None

    def __init__(self, *a, **k):
        type(self).kwargs = k

    def run(self):
        raise type(self).raises


@pytest.mark.parametrize("exc, code, status", [
    (BudgetExceeded("guard"), 1, "halted_budget"),
    (RunAborted("data/pilot/T0/T0-run01.partial.json"), 2, "aborted"),
])
def test_pilot_stops_and_writes_summary_on_halt(tmp_path, monkeypatch, exc, code, status):
    _FakeEngine.raises = exc
    monkeypatch.setattr(pilot, "SimulationEngine", _FakeEngine)
    assert pilot.main(["--dry-run", "T4", "--output", str(tmp_path)]) == code
    summary = json.loads((tmp_path / "pilot_summary_T4.json").read_text())
    assert summary["status"] == status and summary["runs_completed"] == 0
    assert summary["runs_requested"] == 2
    kw = _FakeEngine.kwargs
    assert kw["dry_run"] is True and kw["run_meta"]["test"] == "T4" and kw["parallel"] is True


def test_fatal_api_error_mid_run_keeps_partial_and_exits_2(tmp_path, monkeypatch):
    """C4-4: a fatal provider error in month 2 aborts the pilot; turn 1 survives on disk."""
    from core.llm import FatalAPIError
    orig, calls = pilot._stub_turn_reply, {"n": 0}

    def flaky(system, user):
        calls["n"] += 1
        if calls["n"] > 20:                     # month 1 needs 15 calls (3 stages x 5 seats)
            raise FatalAPIError("muse/muse-spark-1.3: 404 model not found")
        return orig(system, user)

    monkeypatch.setattr(pilot, "_stub_turn_reply", flaky)
    assert pilot.main(["--dry-run", "T0", "--sequential", "--output", str(tmp_path)]) == 2
    summary = json.loads((tmp_path / "pilot_summary_T0.json").read_text())
    assert summary["status"] == "aborted"
    assert "404 model not found" in summary["aborted"]["error"]
    partial = json.loads((tmp_path / "T0-run01.partial.json").read_text())
    assert len(partial["turns"]) >= 1
    assert not (tmp_path / "T0-run01.json").exists()


# ---------------------------------------------------------------------------
# Usage report on a hand-made record (C4-3)
# ---------------------------------------------------------------------------

def _attempt(error=None, stop="end", cost=0.01, **tok):
    return {"text": "", "thinking": None, "error": error, "stop": stop, "input_tokens": tok.get("i", 1000),
            "output_tokens": tok.get("o", 200), "cached_tokens": tok.get("c", 800),
            "reasoning_tokens": tok.get("r", 50), "cost": cost, "latency_s": 1.0}


def test_usage_report_rates_stops_and_projection():
    record = {
        "labs": {"anthropic": {"model": "claude-opus-5-5"}, "xai": {"model": "grok-4.7"}},
        "final": {"grand_jury": {"per_juror": {"gpt-6-sol": {"result": {"ups": 50}},
                                               "gemini-3.1-pro": {"result": None}}}},
        "turns": [
            {"turn": t, "actors": {
                "anthropic": {"forfeited": t == 2, "attempts": [_attempt("bad json", "max_tokens"), _attempt()]
                              if t == 1 else [_attempt("refused", "refusal")],
                              "message_attempts": [_attempt(), _attempt()]},
                "xai": {"scripted": True}}}
            for t in (1, 2)],
    }
    tracker = configure()
    for purpose, model, cost in (("actor", "claude-opus-5-5", 0.4), ("a2a", "claude-opus-5-5", 0.2),
                                 ("grand_jury", "gpt-6-sol", 0.3), ("grand_jury", "gemini-3.1-pro", 0.3),
                                 ("grand_jury", "gemini-3.1-pro", 0.3)):
        tracker.record(model, purpose, "r1", 10, 5, cost=cost)
    tracker.record("x", "actor", "other", 0, 0, cost=99.0)
    rep = pilot.usage_report([record], ["r1"])
    row = rep["by_actor_model"]["claude-opus-5-5"]
    assert "grok-4.7" not in rep["by_actor_model"]
    assert row["seat_turns"] == 2 and row["forfeits"] == 1
    assert row["proposal_calls"] == 3 and row["proposal_failed"] == 2
    assert row["proposal_failure_rate"] == pytest.approx(0.667, abs=1e-3)
    assert row["message_calls"] == 4 and row["message_failure_rate"] == 0.0
    assert row["stop_max_tokens"] == 1 and row["stop_refusal"] == 1
    assert row["cached_tokens"] == 7 * 800 and row["reasoning_tokens"] == 7 * 50
    gem = rep["jurors"]["gemini-3.1-pro|grand_jury"]
    assert gem["calls"] == 2 and gem["failed_calls"] == 2
    assert rep["jurors"]["gpt-6-sol|grand_jury"]["failed_calls"] == 0
    proj = rep["projection"]
    assert proj["cost_per_turn"] == pytest.approx(0.3)          # (0.4 + 0.2) / 2 turns
    assert proj["post_run_cost_per_run"] == pytest.approx(0.9)
    # turn 1 and later turns priced apart from the records' attempt costs (0.01 each here)
    assert proj["first_turn_cost"] == pytest.approx(0.04) and proj["later_turn_cost"] == pytest.approx(0.03)
    assert proj["projected_12_turn_run"] == pytest.approx(0.04 + 11 * 0.03 + 0.9)


# ---------------------------------------------------------------------------
# main.py (P1, P7, P11)
# ---------------------------------------------------------------------------

def test_main_scripted_run_saves_under_default_dir(tmp_path):
    assert main_mod.main(["--policy", "idle", "--turns", "2"]) == 0
    runs = list((tmp_path / "runs_default").iterdir())
    assert len(runs) == 1 and runs[0].name.startswith("S1-A-")
    assert (runs[0] / f"{runs[0].name}.json").exists()


def test_main_real_run_guards(monkeypatch, capsys):
    monkeypatch.setattr(main_mod, "SimulationEngine", _NoEngine)
    monkeypatch.setattr(main_mod, "load_lab_configs",
                        lambda: [{"key": "meta", "charter_values_source": "placeholder; run it"}])
    assert main_mod.main(["--turns", "1", "--disposition", "50"]) == 2
    assert "placeholders for meta" in capsys.readouterr().out
    monkeypatch.setattr(main_mod, "preflight", lambda models, providers=None: ["XAI_API_KEY is not set"])
    assert main_mod.main(["--turns", "1", "--allow-placeholder-values", "--disposition", "50"]) == 2
    assert "XAI_API_KEY" in capsys.readouterr().out


def test_main_warns_without_budget(monkeypatch, caplog):
    _FakeEngine.raises = BudgetExceeded("stop here")
    monkeypatch.setattr(main_mod, "SimulationEngine", _FakeEngine)
    monkeypatch.setattr(main_mod, "load_lab_configs", lambda: [])
    monkeypatch.setattr(main_mod, "preflight", lambda models, providers=None: [])
    with caplog.at_level("WARNING"):
        assert main_mod.main(["--turns", "1", "--set", "intrusion.gain_share=0.35", "--disposition", "50"]) == 1
    assert "without --budget" in caplog.text
    assert _FakeEngine.kwargs["overrides"] == {"intrusion.gain_share": 0.35}
    assert _FakeEngine.kwargs["run_id"].startswith("S1-A-")


# ---------------------------------------------------------------------------
# rate_charters.py (P10)
# ---------------------------------------------------------------------------

def test_rate_lab_with_stub_writes_valid_ints():
    seen = {}

    def stub(system, user):
        seen["user"] = user
        return json.dumps({"time_horizon": 60.4, "transparency_threshold": 55, "risk_tolerance": 40.6,
                           "democratic_tendency": 45, "rationale": "stub"})

    register_stub("ratetest", stub)
    cfg = load_json(CONFIG_DIR / "labs" / "anthropic.json")
    cfg["model"] = "stub:ratetest"
    values, notes = rate_charters.rate_lab(cfg)
    assert set(values) == set(rate_charters.AXES)
    assert all(isinstance(v, int) and 0 <= v <= 100 for v in values.values())
    assert values["time_horizon"] == 60 and values["risk_tolerance"] == 41
    assert notes == "stub"
    assert '"rationale"' in seen["user"] and '"reasoning"' not in seen["user"]
    assert rate_charters.MAX_TOKENS == 8000


def test_rate_charters_dry_run_is_non_destructive():
    path = CONFIG_DIR / "labs" / "anthropic.json"
    before = path.read_text()
    assert rate_charters.main(["--dry-run", "--lab", "anthropic"]) == 0
    assert path.read_text() == before, "dry-run must not touch the config"


def test_rate_charters_real_mode_preflight_and_budget(monkeypatch):
    monkeypatch.setattr(rate_charters, "preflight", lambda models, providers=None: ["no key"])
    monkeypatch.setattr(rate_charters, "rate_lab", lambda *a, **k: pytest.fail("called a model"))
    assert rate_charters.main(["--lab", "meta"]) == 2
    args = rate_charters.build_parser().parse_args([])
    assert args.budget is None and args.headroom == 10.0


def test_rate_charters_guard_is_remaining_budget(tmp_path, monkeypatch):
    """P23: the default guard is the ledger's current total + 10, not an absolute $10."""
    assert rate_charters.guard_for(57.5, None, 10.0) == 67.5
    assert rate_charters.guard_for(57.5, 80.0, 10.0) == 80.0
    spend = tmp_path / "s.json"
    spend.write_text(json.dumps({"total_usd": 42.0, "calls": 3, "by_model": {}}))
    seen = {}
    monkeypatch.setattr(rate_charters, "preflight", lambda models, providers=None: [])

    def fake_rate(cfg, run_id="rate_charters"):
        seen["budget"] = get_tracker().budget
        raise BudgetExceeded("stop here")
    monkeypatch.setattr(rate_charters, "rate_lab", fake_rate)
    assert rate_charters.main(["--lab", "meta", "--spend-file", str(spend)]) == 1          # P65: budget stop exits 1
    assert seen["budget"] == pytest.approx(52.0)


# ---------------------------------------------------------------------------
# Round 2: crashes (H5/P13), dry segregation (H7/P15), debriefs (P25), timeouts
# ---------------------------------------------------------------------------

def test_crash_in_a_stage_writes_summary_with_partial_usage_and_exits_2(tmp_path, monkeypatch):
    """H5: any exception (here a bug in month 2's scoring) -> status crashed,
    exit 2, the summary written and the paid turns of the partial record in the usage table."""
    import core.engine as engine_mod
    orig, calls = engine_mod.ups_index, {"n": 0}

    def buggy(*a, **k):                     # the end-of-turn scoring of month 2 crashes
        calls["n"] += 1
        if calls["n"] == 2:
            raise ZeroDivisionError("boom in month 2")
        return orig(*a, **k)

    monkeypatch.setattr(engine_mod, "ups_index", buggy)
    rc = pilot.main(["--dry-run", "T4", "--sequential", "--output", str(tmp_path)])
    assert rc == 2
    summary = json.loads((tmp_path / "pilot_summary_T4.json").read_text())
    assert summary["status"] == "crashed" and summary["runs_completed"] == 0
    assert "ZeroDivisionError: boom in month 2" in summary["aborted"]["error"]
    assert summary["aborted"]["run_id"] == "T4-run01"
    assert summary["aborted"]["record_path"].endswith("T4-run01.partial.json")
    rows = summary["usage"]["by_actor_model"]
    assert rows and sum(r["seat_turns"] for r in rows.values()) >= 5, "partial turns counted (C10)"


def test_unexpected_exception_from_engine_is_crashed(tmp_path, monkeypatch):
    _FakeEngine.raises = KeyError("unexpected")
    monkeypatch.setattr(pilot, "SimulationEngine", _FakeEngine)
    assert pilot.main(["--dry-run", "T0", "--output", str(tmp_path)]) == 2
    summary = json.loads((tmp_path / "pilot_summary_T0.json").read_text())
    assert summary["status"] == "crashed" and "KeyError" in summary["aborted"]["error"]


def test_usage_report_reads_incomplete_turns_and_counts_timeouts():
    record = {"labs": {"anthropic": {"model": "claude-opus-5-5"}},
              "turns": [{"turn": 1, "actors": {"anthropic": {"attempts": [_attempt()]}}},
                        {"turn": 2, "incomplete": True, "actors": {"anthropic": {
                            "attempts": [_attempt("timeout", "timeout", cost=0.0), _attempt()]}}}]}
    tracker = configure()
    tracker.record("gpt-6-sol", "grand_jury", "r1", 0, 0, cost=0.0, stop="timeout", possibly_billed=True)
    rep = pilot.usage_report([record], ["r1"])
    row = rep["by_actor_model"]["claude-opus-5-5"]
    assert row["proposal_calls"] == 3 and row["stop_timeout"] == 1 and row["possibly_billed"] == 1
    assert rep["projection"]["turns_measured"] == 1, "only complete turns are measured turns"
    assert rep["jurors"]["gpt-6-sol|grand_jury"]["timeouts"] == 1


def test_default_output_and_ratings_paths_segregate_dry_runs(tmp_path):
    assert pilot.default_output("T4", True) == tmp_path / "pilot_default" / "dry" / "T4"
    assert pilot.default_output("T4", False) == tmp_path / "pilot_default" / "T4"
    args = pilot.build_parser().parse_args(["--dry-run", "T4", "--ratings-file", "x.json"])
    out = tmp_path / "o"
    assert pilot.ratings_path(args, out) == out / "disposition_ratings.dry.json"
    args = pilot.build_parser().parse_args(["T4", "--ratings-file", "x.json"])
    assert pilot.ratings_path(args, out) == Path("x.json")
    args = pilot.build_parser().parse_args(["T4"])
    assert pilot.ratings_path(args, out) == pilot.RATINGS_FILE


def test_dry_summary_points_only_at_its_dry_ratings(tmp_path, capsys):
    assert pilot.main(["--dry-run", "T0", "--output", str(tmp_path)]) == 0
    printed = capsys.readouterr().out
    assert str(tmp_path / "disposition_ratings.dry.json") in printed
    assert "data/pilot/disposition_ratings.json" not in printed


def test_clear_stale_removes_old_final_and_debrief(tmp_path):
    (tmp_path / "T4-run01.json").write_text("{}")
    (tmp_path / "T4-run01.debrief.json").write_text("{}")
    (tmp_path / "T4-run01.partial.json").write_text("{}")
    assert sorted(pilot.clear_stale(tmp_path, "T4-run01")) == ["T4-run01.debrief.json", "T4-run01.json"]
    assert (tmp_path / "T4-run01.partial.json").exists()
    (tmp_path / "x.json").write_text("{}")
    assert main_mod.clear_stale(tmp_path, "x") and not (tmp_path / "x.json").exists()


def test_real_run_deletes_stale_final_before_starting(tmp_path, monkeypatch):
    (tmp_path / "T0-run01.json").write_text(json.dumps({"stale": True}))
    _FakeEngine.raises = RunAborted("x")
    monkeypatch.setattr(pilot, "SimulationEngine", _FakeEngine)
    monkeypatch.setattr(pilot, "preflight_problems", lambda *a, **k: [])
    assert pilot.main(["T0", "--output", str(tmp_path), "--allow-placeholder-values"]) == 2
    assert not (tmp_path / "T0-run01.json").exists()


class _Resp:
    def __init__(self, text, stop):
        self.text, self.stop = text, stop


def test_debrief_retries_once_at_16000_after_an_empty_capped_answer(monkeypatch):
    seen = []

    def fake_complete(model, system, user, **kw):
        seen.append(kw)
        return _Resp("", "max_tokens") if len(seen) == 1 else _Resp("It measured strategy.", "end")

    monkeypatch.setattr(pilot, "complete", fake_complete)
    r, n = pilot._ask_debrief("m", "prompt", "run")
    assert n == 2 and r.text == "It measured strategy."
    assert [k["max_tokens"] for k in seen] == [8000, 16000]
    assert all(k["effort"] == "medium" for k in seen)
    seen.clear()
    monkeypatch.setattr(pilot, "complete", lambda *a, **kw: seen.append(kw) or _Resp("", "end"))
    _r, n = pilot._ask_debrief("m", "prompt", "run")
    assert n == 1 and len(seen) == 1, "only an empty answer at the cap is retried"


def test_debrief_uses_debrief_transcript(monkeypatch):
    from core import transcript
    monkeypatch.setattr(transcript, "debrief_transcript", lambda record, key: f"DEBRIEF VIEW {key}",
                        raising=False)
    assert pilot.debrief_text({}, "meta") == "DEBRIEF VIEW meta"


def test_conditions_override_trims_t5(tmp_path):
    assert pilot.main(["--dry-run", "T5", "--conditions", "A", "--turns", "2",
                       "--output", str(tmp_path)]) == 0
    summary = json.loads((tmp_path / "pilot_summary_T5-A.json").read_text())       # P51
    assert summary["conditions"] == ["A"] and summary["runs_requested"] == 1
    assert (tmp_path / "T5-A-run01.json").exists() and (tmp_path / "review_T5-A.md").exists()
    with pytest.raises(SystemExit):
        pilot.main(["--dry-run", "T5", "--conditions", "D", "--output", str(tmp_path)])


# ---------------------------------------------------------------------------
# Round 3 (P27, P36, P38, P39, P41)
# ---------------------------------------------------------------------------

def test_per_seat_rung_runs_into_one_dir_never_collide(tmp_path):
    """P27 (r3C/t_rungcollide.py): --rotate meta:gdm then --rotate meta:xai keep all four runs."""
    for seat in ("gdm", "xai"):
        assert pilot.main(["--dry-run", "T1b", "--rung", "1", "--rotate", f"meta:{seat}", "--turns", "2",
                           "--output", str(tmp_path), "--no-disposition-jury"]) == 0
    rotations = {p.name: json.loads(p.read_text())["config"]["rotation"]
                 for p in tmp_path.glob("T1b-*-rung1.json")}
    assert rotations == {f"T1b-meta-{s}-run0{i}-rung1.json": f"meta:{s}" for s in ("gdm", "xai") for i in (1, 2)}
    for seat in ("gdm", "xai"):
        summary = json.loads((tmp_path / f"pilot_summary_T1b-meta-{seat}-rung1.json").read_text())
        assert [r["rotation"] for r in summary["per_run"]] == [f"meta:{seat}"] * 2


def _final(path, rotation=None, overrides=None, seed=0):
    path.write_text(json.dumps({"config": {"rotation": rotation, "overrides": overrides, "seed": seed},
                                "turns": [], "labs": {}}))


def test_clear_stale_refuses_a_final_of_a_different_run(tmp_path):
    """P27: a final with another rotation / overrides / seed is kept; the new run takes -v2."""
    _final(tmp_path / "T1b-run01-rung1.json", rotation="meta:gdm", overrides={"intrusion.gain_share": 0.35})
    expect = {"rotation": "meta:xai", "overrides": {"intrusion.gain_share": 0.35}, "seed": 0}
    assert pilot.stale_conflict(tmp_path, "T1b-run01-rung1", expect).startswith("rotation")
    assert pilot.clear_stale(tmp_path, "T1b-run01-rung1", expect) == []
    assert (tmp_path / "T1b-run01-rung1.json").exists()
    assert pilot.claim_run_id(tmp_path, "T1b-run01-rung1", expect) == "T1b-run01-rung1-v2"
    assert (tmp_path / "T1b-run01-rung1.json").exists()
    # The same run (same rotation, overrides and seed) is a stale copy: cleared, same id.
    same = dict(expect, rotation="meta:gdm")
    assert pilot.claim_run_id(tmp_path, "T1b-run01-rung1", same) == "T1b-run01-rung1"
    assert not (tmp_path / "T1b-run01-rung1.json").exists()
    _final(tmp_path / "T4-run01.json", overrides={})
    assert pilot.stale_conflict(tmp_path, "T4-run01", {"rotation": None, "overrides": None, "seed": 0}) is None
    assert pilot.stale_conflict(tmp_path, "T4-run01", {"rotation": None, "overrides": None, "seed": 1})


def test_clear_stale_drops_the_runs_pooled_ratings(tmp_path):
    """P38 (r3C/t_pool2.py step 5): a cleared final takes its pooled rating rows with it."""
    pool = tmp_path / "pool.json"
    for rid, v in (("T0-run01", 10), ("T4-run01", 20)):
        test = rid.split("-")[0]
        tag = {"run_id": rid, "test": test, "dry_run": False}
        pilot.append_ratings(pool, {"ratings": {"meta": [dict(tag, juror="gpt-6-sol", disposition=v)]},
                                    "chosen": {"meta": [dict(tag, value=v)]}, "families": {"meta": "muse"}},
                             test, rid, False)
    (tmp_path / "T0-run01.json").write_text("{}")
    assert pilot.clear_stale(tmp_path, "T0-run01", ratings_file=pool) == ["T0-run01.json"]
    data = json.loads(pool.read_text())
    assert [r["run_id"] for r in data["ratings"]["meta"]] == ["T4-run01"]
    assert [r["run_id"] for r in data["chosen"]["meta"]] == ["T4-run01"]
    assert [r["run_id"] for r in data["runs"]] == ["T4-run01"]


def test_real_rerun_clears_pooled_rows_before_starting(tmp_path, monkeypatch):
    pool = tmp_path / "pool.json"
    tag = {"run_id": "T0-run01", "test": "T0", "dry_run": False}
    pilot.append_ratings(pool, {"ratings": {"meta": [dict(tag, juror="gpt-6-sol", disposition=10)]},
                                "chosen": {}, "families": {}}, "T0", "T0-run01", False)
    _final(tmp_path / "T0-run01.json")
    _FakeEngine.raises = RunAborted("x")
    monkeypatch.setattr(pilot, "SimulationEngine", _FakeEngine)
    monkeypatch.setattr(pilot, "preflight_problems", lambda *a, **k: [])
    assert pilot.main(["T0", "--output", str(tmp_path), "--allow-placeholder-values",
                       "--ratings-file", str(pool)]) == 2
    assert not (tmp_path / "T0-run01.json").exists()
    assert json.loads(pool.read_text())["ratings"]["meta"] == []


def test_summary_is_written_with_an_error_when_building_it_fails(tmp_path, monkeypatch):
    """P41 (r3C/t_crash2.py 'aggregate'): a failing aggregate / usage report never loses the summary."""
    def boom(*a, **k):
        raise RuntimeError("boom in aggregate")
    monkeypatch.setattr(pilot, "aggregate", boom)
    monkeypatch.setattr(pilot, "usage_report", boom)
    assert pilot.main(["--dry-run", "T4", "--runs", "1", "--turns", "2", "--output", str(tmp_path)]) == 0
    summary = json.loads((tmp_path / "pilot_summary_T4.json").read_text())
    assert summary["runs_completed"] == 1 and summary["decisions"] == {} and summary["usage"] == {}
    assert "decisions: RuntimeError: boom in aggregate" in summary["error"]
    assert "usage: RuntimeError" in summary["error"]


def test_main_py_any_crash_exits_2_and_keeps_the_partial(tmp_path, monkeypatch, capsys):
    """P39 (r3C/t_main_crash.py): any exception in a run -> exit 2, message, partial kept."""
    import core.engine as engine_mod
    orig = engine_mod.SimulationEngine._execute

    def bad(self, turn, *a, **k):
        if turn == 2:
            raise ValueError("boom")
        return orig(self, turn, *a, **k)
    monkeypatch.setattr(engine_mod.SimulationEngine, "_execute", bad)
    assert main_mod.main(["--scenario", "S1", "--policy", "greedy", "--output", str(tmp_path),
                          "--run-id", "mc", "--turns", "3"]) == 2
    assert "Run crashed: ValueError: boom" in capsys.readouterr().out
    assert (tmp_path / "mc.partial.json").exists() and not (tmp_path / "mc.json").exists()


def test_t6neutral_summary_compares_with_eval_arm_logs(tmp_path):
    """P36: T6neutral --eval-arm DIR prints the per-seat-turn comparison."""
    eval_dir = tmp_path / "eval"
    assert pilot.main(["--dry-run", "T0", "--output", str(eval_dir), "--no-disposition-jury"]) == 0
    out = tmp_path / "neutral"
    assert pilot.main(["--dry-run", "T6neutral", "--runs", "1", "--turns", "2", "--output", str(out),
                       "--no-disposition-jury", "--eval-arm", str(eval_dir)]) == 0
    line = json.loads((out / "pilot_summary_T6neutral.json").read_text())["decisions"]["T6neutral"]
    assert "eval arm 0/10" in line and "neutral arm 0/10" in line and "keep the eval brief" in line


# ---------------------------------------------------------------------------
# Round 4 (S1, S2, P42, P49, P53, P54, P56, P58, P59)
# ---------------------------------------------------------------------------

def test_r4_plan_and_estimate_printed_before_starting(tmp_path, capsys, monkeypatch):
    seen = {}

    class Boom(Exception):
        pass

    def stop(*a, **k):
        seen["out"] = capsys.readouterr().out
        raise Boom()

    monkeypatch.setattr(pilot, "SimulationEngine", stop)
    pilot.main(["--dry-run", "T1b", "--rotate", "meta:gdm,meta:xai", "--turns", "2",
                "--output", str(tmp_path), "--no-disposition-jury"])
    out = seen["out"]
    assert "PILOT T1b-meta-gdm_meta-xai: 2 run(s) planned; estimated cost ≈ $4.63" in out          # P66: 2 x 6.94 x 2/6
    assert "T1b-meta-gdm-run01" in out and "T1b-meta-xai-run01" in out and "run02" not in out


def test_r4_skip_completed_reruns_only_missing(tmp_path, capsys):
    args = ["--dry-run", "T1b", "--rotate", "meta:gdm,meta:xai", "--turns", "1",
            "--output", str(tmp_path), "--no-disposition-jury"]
    assert pilot.main(args) == 0
    (tmp_path / "T1b-meta-xai-run01.json").unlink()
    capsys.readouterr()
    assert pilot.main(args + ["--skip-completed"]) == 0
    out = capsys.readouterr().out
    assert "1 run(s) planned, 1 already completed (skipped)" in out
    assert "skip (completed) T1b-meta-gdm-run01" in out
    summary = json.loads((tmp_path / "pilot_summary_T1b-meta-gdm_meta-xai.json").read_text())
    assert summary["skipped_completed"] == ["T1b-meta-gdm-run01"]
    assert [r["run_id"] for r in summary["per_run"]] == ["T1b-meta-xai-run01"]
    # a final of another rotation under the same id is never "completed"
    rec = json.loads((tmp_path / "T1b-meta-gdm-run01.json").read_text())
    assert pilot.completed_record(tmp_path, "T1b-meta-gdm-run01",
                                  {"rotation": "meta:xai", "overrides": None, "seed": 0, "condition": "A"},
                                  True) is None
    assert pilot.completed_record(tmp_path, "T1b-meta-gdm-run01",
                                  {"rotation": "meta:gdm", "overrides": None, "seed": 0, "condition": "A"},
                                  True)["config"]["rotation"] == rec["config"]["rotation"]


def test_r4_rung_skips_debrief_and_grand_jury(tmp_path):
    assert pilot.main(["--dry-run", "T1b", "--rung", "1", "--rotate", "meta:gdm", "--turns", "1", "--runs", "1",
                       "--output", str(tmp_path), "--no-disposition-jury"]) == 0
    rec = json.loads((tmp_path / "T1b-meta-gdm-run01-rung1.json").read_text())
    assert not rec["final"].get("grand_jury") and not (tmp_path / "T1b-meta-gdm-run01-rung1.debrief.json").exists()
    summary = json.loads((tmp_path / "pilot_summary_T1b-meta-gdm-rung1.json").read_text())
    assert summary["debriefs"] is False and summary["grand_jury"] is False
    assert pilot.main(["--dry-run", "T1b", "--rung", "1", "--rotate", "meta:xai", "--turns", "1", "--runs", "1",
                       "--output", str(tmp_path), "--no-disposition-jury", "--debrief", "--grand-jury"]) == 0
    rec = json.loads((tmp_path / "T1b-meta-xai-run01-rung1.json").read_text())
    assert rec["final"].get("grand_jury") and (tmp_path / "T1b-meta-xai-run01-rung1.debrief.json").exists()


def test_r4_t0_served_models_macro_measure_and_reprojection(tmp_path, capsys):
    assert pilot.main(["--dry-run", "T0", "--output", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    summary = json.loads((tmp_path / "pilot_summary_T0.json").read_text())
    usage = summary["usage"]
    assert usage["by_actor_model"]["stub:actor_meta"]["served_models"] == {"stub:actor_meta": 6}
    assert not usage["by_actor_model"]["stub:actor_meta"]["served_mismatch"]
    macro = usage["macro_measures"][0]
    assert sorted(macro["per_juror"]) == sorted(["stub:claude-macro", "stub:gpt-macro", "stub:gemini-macro"])
    assert all(pj["usable"] for pj in macro["per_juror"].values())
    assert usage["projection"]["macro_reviews_measured"] == 1 and usage["projection"]["macro_reviews_per_12_turn_run"] == 2
    assert usage["jurors"]["stub:gpt-macro|macro_jury"]["failed_calls"] == 0
    steps = [s["step"] for s in summary["reprojection"]["steps"]]
    assert steps == ["rate_charters", "T0", "T1a", "T1b", "T4", "T9", "T5", "T5false", "attribution_probe"]
    assert [t["label"].split()[0] for t in summary["reprojection"]["trims"]] == ["T9", "T5", "T4", "T1b"]
    assert "MacroJury measured once on the final state: 3/3 usable votes" in out
    assert "re-projected core order" in out and "Update config/prices.json" in out
    assert (tmp_path / "review_T0.md").exists() and summary["review"].endswith("review_T0.md")


def test_r4_served_mismatch_and_reprojection_math():
    assert not pilot.served_mismatch("claude-opus-5-5", "claude-opus-5-5-20260301")
    assert not pilot.served_mismatch("gemini-3.1-pro", "models/gemini-3.1-pro")
    assert pilot.served_mismatch("gpt-6-astra", "gpt-5.2")
    proj = {"cost_per_turn": 1.0, "first_turn_cost": 2.0, "later_turn_cost": 1.0, "first_turn_a2a_cost": 1.0,
            "later_turn_a2a_cost": 0.5, "a2a_cost_per_turn": 0.5, "macro_cost_per_review": 0.1,
            "grand_jury_cost_per_run": 0.3, "disposition_jury_cost_per_run": 0.2, "debrief_cost_per_run": 0.0}
    rep = pilot.reproject(pilot.load_pilot(), proj)
    cost = {s["step"]: s["cost"] for s in rep["steps"]}
    assert cost["T0"] == round(2.0 + 1.0 + 0.2 + 0.3, 2)                     # 2 turns, no review, DJ + GJ
    assert cost["T1a"] == round(2.0 + 11 * 1.0 + 2 * 0.1 + 0.2 + 0.3 + 0.22, 2)
    assert cost["T9"] == round(2 * ((2.0 - 1.0) + 5 * 0.5 + 0.1 + 0.2 + 0.3), 2)   # merged: no message rounds
    assert cost["T5"] == round(2 * (2.0 + 3 * 1.0 + 0.2), 2)                 # no Grand Jury, 2 conditions
    assert rep["reserve"] == round(100 - rep["total"], 2)
    assert dict((t["label"].split()[0], t["saves"]) for t in rep["trims"])["T1b"] == round(cost["T1b"] / 2, 2)


def test_r4_budget_stop_in_post_run_jury_keeps_the_run(tmp_path, monkeypatch):
    """S2: the engine saved the final record (grand_jury error) and re-raised BudgetExceeded."""
    from core.costs import BudgetExceeded
    real = pilot.SimulationEngine

    class Engine(real):
        def run(self):
            rec = super().run()
            rec["final"]["grand_jury"] = {"error": "budget"}
            pilot._write_json(self.output_dir / f"{self.run_id}.json", rec)
            raise BudgetExceeded("guard reached during the Grand Jury")

    monkeypatch.setattr(pilot, "SimulationEngine", Engine)
    assert pilot.main(["--dry-run", "T4", "--turns", "1", "--output", str(tmp_path),
                       "--no-disposition-jury"]) == 1
    summary = json.loads((tmp_path / "pilot_summary_T4.json").read_text())
    assert summary["status"] == "halted_budget" and summary["runs_completed"] == 1
    assert "the run counts" in summary["per_run"][0]["note"]
    assert summary["aborted"]["record_path"].endswith("T4-run01.json")


def test_r4_followups_and_review_pointer(tmp_path, capsys):
    assert pilot.main(["--dry-run", "T9", "--turns", "1", "--runs", "1", "--output", str(tmp_path),
                       "--no-disposition-jury"]) == 0
    out = capsys.readouterr().out
    assert "next: T9 is decided only by: python tools/compare_arms.py" in out
    assert f"review file (every screened text in full, with verdict and rule): {tmp_path / 'review_T9.md'}" in out
    data = pilot.load_pilot()
    assert any("--decide T7" in f for f in data["presets"]["T1a"]["followup"])


def test_r4_overshoot_note_uses_largest_measured_prompt(monkeypatch):
    class T:
        calls = [{"run_id": "r", "input_tokens": 1234}, {"run_id": "other", "input_tokens": 99999}]

        def overshoot_note(self, concurrency=5, prompt_tokens=50_000, output_tokens=32_000):
            return f"prompt {prompt_tokens}"

    monkeypatch.setattr(pilot, "get_tracker", lambda: T())
    assert pilot._overshoot_note(["r"]) == "prompt 1234"
    T.calls = []
    assert pilot._overshoot_note(["r"]) == "prompt 50000"


def test_r4_rate_charters_unusable_rating_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rate_charters, "preflight", lambda models, providers=None: [])
    written = []
    monkeypatch.setattr(rate_charters, "write_values", lambda key, *a: written.append(key))

    def fake_rate(cfg, run_id="rate_charters"):
        if cfg["key"] == "openai":
            raise rate_charters.UnusableRating("openai: no usable charter rating after 3 attempt(s)")
        return {a: 50 for a in rate_charters.AXES}, "ok"

    monkeypatch.setattr(rate_charters, "rate_lab", fake_rate)
    assert rate_charters.main(["--spend-file", str(tmp_path / "s.json")]) == 2
    out = capsys.readouterr().out
    assert "UNUSABLE rating for openai" in out and "python rate_charters.py --lab openai" in out
    assert "openai" not in written and len(written) == 4


def test_r4_rate_lab_raises_unusable(monkeypatch):
    monkeypatch.setattr(rate_charters, "complete_json",
                        lambda *a, **k: (None, [{"error": "axis 'risk_tolerance' must be a number 0-100"}]))
    monkeypatch.setattr(rate_charters, "load_charter", lambda cfg: "text")
    with pytest.raises(rate_charters.UnusableRating, match="risk_tolerance"):
        rate_charters.rate_lab({"key": "meta", "model": "m", "charter_name": "c"})


# ---------------------------------------------------------------------------
# Round 5: post-run stages never abort (R4), juror table (P60), debriefs (P61),
# --post-run-only (P62), served probe (P63), main.py dispositions (P64),
# rate_charters budget exit (P65), estimates (P66)
# ---------------------------------------------------------------------------

from core import jury as jury_mod      # noqa: E402
from core.llm import FatalAPIError     # noqa: E402

_ORIG_INSTALL = pilot._install_stubs


def _fatal(model="gpt-x"):
    def raiser(system, user):
        raise FatalAPIError(f"{model}: 404 model not found", provider="openai", model=model, status=404)
    return raiser


def _patch_stubs(monkeypatch, **stubs):
    """After the pilot registers its stubs, replace some (stub name -> reply fn)."""
    def patched(labs):
        jurors = _ORIG_INSTALL(labs)
        for name, fn in stubs.items():
            register_stub(name, fn)
        return jurors
    monkeypatch.setattr(pilot, "_install_stubs", patched)


def _ratings(tmp_path):
    return json.loads((tmp_path / "disposition_ratings.dry.json").read_text())


def test_r5_dry_stub_grand_jury_gives_a_usable_verdict(tmp_path):
    """The stub Grand Juror answers the R1 schema (ups_without), so dry runs get a verdict."""
    assert pilot.main(["--dry-run", "T4", "--turns", "1", "--runs", "1", "--output", str(tmp_path),
                       "--no-disposition-jury"]) == 0
    gj = json.loads((tmp_path / "T4-run01.json").read_text())["final"]["grand_jury"]
    assert len(gj["per_juror"]) == 3 and all(pj["result"] for pj in gj["per_juror"].values())
    assert gj["ups"] == 50 and gj["actors"]["meta"]["n_jurors"] == 3
    assert gj["actors"]["meta"]["contribution"] == 0


def test_r5_t0_juror_table_names_every_failing_juror(tmp_path, monkeypatch, capsys):
    """P60/P61 (r5A x1): a juror family dead in every role is a table row with failures and a WARNING."""
    _patch_stubs(monkeypatch, **{"gpt-grand": _fatal(), "gpt-disposition": _fatal(), "gpt-macro": _fatal()})
    assert pilot.main(["--dry-run", "T0", "--output", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    summary = json.loads((tmp_path / "pilot_summary_T0.json").read_text())
    assert summary["status"] == "completed"
    jurors = summary["usage"]["jurors"]
    for role in ("grand_jury", "macro_jury", "disposition_jury"):
        short = role.split("_")[0]
        row = jurors[f"stub:gpt-{short}|{role}"]
        assert row["calls"] == 0 and row["usable"] == 0 and row["failed"] >= 1
        assert "404 model not found" in row["last_error"]
        assert f"WARNING: stub:gpt-{short} ({role}) failed" in out
        assert jurors[f"stub:claude-{short}|{role}"]["failed"] == 0
    assert jurors["stub:gpt-disposition|disposition_jury"]["failed"] == 4      # every non-gpt seat
    assert "MacroJury measured once on the final state: 2/3 usable votes" in out
    assert "WARNING: MacroJury juror stub:gpt-macro gave no usable vote" in out
    failures = _ratings(tmp_path)["failures"]                                  # persisted (P60)
    assert sorted(failures) == ["anthropic", "gdm", "meta", "xai"]
    assert failures["meta"][0]["juror"] == "stub:gpt-disposition" and "404" in failures["meta"][0]["error"]
    assert "Grand Jury juror(s) without a verdict: stub:gpt-grand" in summary["per_run"][0]["missing_post_run"][0]
    assert "--post-run-only" in summary["post_run_only_command"]


def test_r5_debrief_fatal_error_is_recorded_and_rating_still_runs(tmp_path, monkeypatch):
    """P61 (r5A x5): one seat's debrief FatalAPIError never stops the pilot or loses the others."""
    def meta(system, user):
        if "designed to measure" in user:
            raise FatalAPIError("meta: gave up after 4 attempts: 503", provider="muse", model="m", status=503)
        return pilot._stub_turn_reply(system, user)
    _patch_stubs(monkeypatch, actor_meta=meta)
    assert pilot.main(["--dry-run", "T4", "--turns", "1", "--runs", "1", "--output", str(tmp_path)]) == 0
    debriefs = json.loads((tmp_path / "T4-run01.debrief.json").read_text())
    assert debriefs["meta"]["answer"] is None and "503" in debriefs["meta"]["error"]
    assert all(debriefs[k]["answer"] for k in ("anthropic", "openai", "gdm", "xai"))
    assert set(_ratings(tmp_path)["ratings"]) == set(LAB_KEYS)
    summary = json.loads((tmp_path / "pilot_summary_T4.json").read_text())
    assert summary["status"] == "completed" and summary["per_run"][0]["debriefs"] == 4
    assert summary["per_run"][0]["missing_post_run"] == ["debriefs (meta)"]
    assert summary["usage"]["jurors"]["stub:actor_meta|debrief"]["failed"] == 1


def test_r5_disposition_budget_stop_keeps_rated_rows_then_post_run_only_completes(tmp_path, monkeypatch, capsys):
    """P62: BudgetExceeded in the disposition jury appends the rows already rated; --post-run-only fills the rest."""
    calls = {"n": 0}

    def juror(system, user):
        calls["n"] += 1
        if calls["n"] == 3:
            raise BudgetExceeded("guard reached in the disposition jury")
        return pilot._stub_disposition_reply(system, user)
    _patch_stubs(monkeypatch, **{f"{f}-disposition": juror for f in ("claude", "gpt", "gemini")})
    args = ["--dry-run", "T4", "--turns", "1", "--runs", "1", "--output", str(tmp_path)]
    assert pilot.main(args) == 1
    assert sum(len(v) for v in _ratings(tmp_path)["ratings"].values()) == 2
    summary = json.loads((tmp_path / "pilot_summary_T4.json").read_text())
    assert summary["status"] == "halted_budget" and summary["runs_completed"] == 1
    assert "post-run stage was stopped by the budget guard" in summary["per_run"][0]["note"]
    assert "disposition ratings (10 seat x juror" in summary["per_run"][0]["missing_post_run"][0]
    monkeypatch.setattr(pilot, "_install_stubs", _ORIG_INSTALL)
    capsys.readouterr()
    assert pilot.main(args + ["--skip-completed"]) == 0
    assert "missing post-run: disposition ratings (10 seat x juror" in capsys.readouterr().out
    assert pilot.main(args + ["--post-run-only"]) == 0
    assert "disposition ratings: 10 pair(s) asked" in capsys.readouterr().out
    pooled = _ratings(tmp_path)
    assert sum(len(v) for v in pooled["ratings"].values()) == 12
    assert pooled["runs"] == [{"test": "T4", "run_id": "T4-run01", "dry_run": True}]
    assert pilot.main(args + ["--post-run-only"]) == 0
    assert "complete         T4-run01" in capsys.readouterr().out
    assert json.loads((tmp_path / "post_run_T4.json").read_text())["runs"][0]["status"] == "complete"


def test_r5_post_run_only_reasks_only_the_failed_grand_juror_and_debrief(tmp_path, monkeypatch, capsys):
    """P62: only the missing jurors / seats are asked again; the kept verdicts stay."""
    def meta(system, user):
        if "designed to measure" in user:
            raise FatalAPIError("meta: 503", provider="muse", model="m", status=503)
        return pilot._stub_turn_reply(system, user)
    _patch_stubs(monkeypatch, **{"gpt-grand": _fatal(), "actor_meta": meta})
    args = ["--dry-run", "T4", "--turns", "1", "--runs", "1", "--output", str(tmp_path), "--no-disposition-jury"]
    assert pilot.main(args) == 0
    gj = json.loads((tmp_path / "T4-run01.json").read_text())["final"]["grand_jury"]
    assert gj["per_juror"]["stub:gpt-grand"]["result"] is None
    asked = []
    monkeypatch.setattr(pilot, "_install_stubs", _ORIG_INSTALL)
    real_eval = pilot.GrandJury.evaluate

    def spy(self, *a, **k):
        asked.extend(self.jurors)
        return real_eval(self, *a, **k)
    monkeypatch.setattr(pilot.GrandJury, "evaluate", spy)
    capsys.readouterr()
    assert pilot.main(args + ["--post-run-only"]) == 0
    out = capsys.readouterr().out
    assert asked == ["stub:gpt-grand"]
    assert "Grand Jury: asked stub:gpt-grand; 3/3 usable verdict(s)" in out and "debriefs: asked meta" in out
    gj = json.loads((tmp_path / "T4-run01.json").read_text())["final"]["grand_jury"]
    assert all(pj["result"] for pj in gj["per_juror"].values()) and gj["actors"]["meta"]["n_jurors"] == 3
    debriefs = json.loads((tmp_path / "T4-run01.debrief.json").read_text())
    assert all(d["answer"] for d in debriefs.values()) and "error" not in debriefs["meta"]


def test_r5_post_run_only_lists_unrun_runs_and_runs_nothing_new(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pilot, "SimulationEngine", _NoEngine)
    assert pilot.main(["--dry-run", "T4", "--turns", "1", "--output", str(tmp_path), "--post-run-only"]) == 0
    out = capsys.readouterr().out
    assert "not run          T4-run01" in out and "not run          T4-run02" in out


def test_r5_completed_record_compares_turns_brief_scenario(tmp_path):
    rec = {"config": {"condition": "A", "rotation": None, "overrides": {}, "seed": 0, "turns": 6,
                      "brief": "eval", "scenario": "S2", "a2a_mode": "separate", "fog": "F3"},
           "turns": [], "final": {"scores": []}}
    (tmp_path / "T4-run01.json").write_text(json.dumps(rec))
    same = {"condition": "A", "rotation": None, "overrides": None, "seed": 0, "turns": 6, "brief": "eval",
            "scenario": "S2", "a2a_mode": "separate", "fog": "F3"}
    assert pilot.completed_record(tmp_path, "T4-run01", same, False) is not None
    for field, value in (("turns", 1), ("brief", "neutral"), ("scenario", "S1"), ("a2a_mode", "merged")):
        assert field in pilot.stale_conflict(tmp_path, "T4-run01", dict(same, **{field: value}))
        assert pilot.completed_record(tmp_path, "T4-run01", dict(same, **{field: value}), False) is None


def test_r5_served_probe_cleared_and_restored(tmp_path, monkeypatch):
    """P63: _SERVED starts empty for each pilot and jury.complete_json is restored afterwards."""
    before = jury_mod.complete_json
    pilot._SERVED[("ghost", "grand_jury")] = {"ghost": 1}
    seen = {}
    real = pilot._run

    def spy(args):
        seen["served"] = dict(pilot._SERVED)
        seen["wrapped"] = getattr(jury_mod.complete_json, "_served_probe", False)
        return real(args)
    monkeypatch.setattr(pilot, "_run", spy)
    assert pilot.main(["--dry-run", "T0", "--turns", "1", "--output", str(tmp_path), "--no-disposition-jury"]) == 0
    assert seen == {"served": {}, "wrapped": True}
    assert jury_mod.complete_json is before

    def boom(args):
        raise RuntimeError("boom")
    monkeypatch.setattr(pilot, "_run", boom)
    with pytest.raises(RuntimeError):
        pilot.main(["--dry-run", "T0"])
    assert jury_mod.complete_json is before


def test_r5_main_refuses_real_runs_without_dispositions(monkeypatch, capsys):
    """P64: no silent 50 — a real non-choose run needs config/dispositions.json or --disposition N."""
    monkeypatch.setattr(main_mod, "SimulationEngine", _NoEngine)
    monkeypatch.setattr(main_mod, "load_dispositions", lambda: {})
    assert main_mod.main(["--turns", "1"]) == 2
    out = capsys.readouterr().out
    assert "config/dispositions.json is missing" in out and "--disposition N" in out
    monkeypatch.setattr(main_mod, "load_dispositions", lambda: {"meta": 40})
    assert main_mod.main(["--turns", "1"]) == 2
    assert "no setting for anthropic, openai, gdm, xai" in capsys.readouterr().out
    _FakeEngine.raises = BudgetExceeded("stop")
    monkeypatch.setattr(main_mod, "SimulationEngine", _FakeEngine)
    monkeypatch.setattr(main_mod, "load_lab_configs", lambda: [])
    monkeypatch.setattr(main_mod, "preflight", lambda models, providers=None: [])
    for extra in (["--disposition", "60"], ["--choose-disposition"]):
        assert main_mod.main(["--turns", "1", "--budget", "5"] + extra) == 1
    assert main_mod.main(["--policy", "idle", "--turns", "1"]) == 1        # scripted runs need no source


def test_r5_rate_charters_fatal_still_exits_2(tmp_path, monkeypatch):
    monkeypatch.setattr(rate_charters, "preflight", lambda models, providers=None: [])

    def fake_rate(cfg, run_id="rate_charters"):
        raise FatalAPIError("auth", provider="openai", model="m", status=401)
    monkeypatch.setattr(rate_charters, "rate_lab", fake_rate)
    assert rate_charters.main(["--lab", "meta", "--spend-file", str(tmp_path / "s.json")]) == 2


def test_r5_preset_estimates_match_the_readme_table():
    """P66: runs x est_cost_per_run rounds to the README's per-step figure."""
    data = pilot.load_pilot()
    readme = {"T0": 3.2, "T1a": 14.1, "T1b": 27.8, "T4": 14.4, "T9": 7.1, "T5": 9.1, "T5false": 5.0}
    for test, figure in readme.items():
        preset = pilot.resolve_preset(data, test)
        est = pilot.estimate_cost(preset, pilot._preset_runs(preset), preset["turns"], None)
        assert round(est, 1) == figure, test


def test_r5_append_ratings_merge_keeps_existing_rows(tmp_path):
    path = tmp_path / "p.json"
    tag = {"test": "T4", "run_id": "T4-run01", "dry_run": False}
    first = {"ratings": {"meta": [dict(tag, juror="a", disposition=40)]},
             "failures": {"meta": [dict(tag, juror="b", error="auth")]}, "chosen": {}, "families": {"meta": "muse"}}
    pilot.append_ratings(path, first, "T4", "T4-run01", False)
    pilot.append_ratings(path, {"ratings": {"meta": [dict(tag, juror="b", disposition=60)]}, "failures": {},
                                "chosen": {}, "families": {}}, "T4", "T4-run01", False, merge=True)
    data = json.loads(path.read_text())
    assert sorted(r["juror"] for r in data["ratings"]["meta"]) == ["a", "b"] and data["failures"]["meta"] == []
    assert pilot.remove_ratings(path, "T4-run01") == 2
