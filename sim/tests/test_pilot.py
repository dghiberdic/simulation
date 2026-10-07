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
    assert not {"T2", "T7", "T8"} & set(presets), "T2 pooled, T7 from T1a, T8 on main runs"
    t5 = _merged("T5")
    assert t5["runs"] == 1 and t5["grand_jury"] is False and t5["conditions"] == ["A", "C"]
    assert t5["scripted_seats"] == {"xai": "t5_scripted"}
    assert _merged("T5false")["conditions"] == ["A"]
    for name in ("T1a", "T1b", "T4", "T6", "T6neutral"):
        assert _merged(name).get("debrief") is True, name
    assert _merged("T6neutral")["brief"] == "neutral"


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
    plan = pilot.plan_runs("T1b", ["A"], 2, ["meta:gdm", "meta:xai"], rung=1)
    assert [p["run_id"] for p in plan] == ["T1b-run01-rung1", "T1b-run02-rung1",
                                           "T1b-run03-rung1", "T1b-run04-rung1"]
    assert [p["rotation"] for p in plan] == ["meta:gdm", "meta:gdm", "meta:xai", "meta:xai"]
    assert [p["seed"] for p in plan] == [0, 1, 0, 1]
    base = pilot.plan_runs("T1b", ["A"], 4, ["meta:anthropic", "meta:openai"], rung=None)
    assert [p["rotation"] for p in base] == ["meta:anthropic", "meta:openai"] * 2
    assert not {p["run_id"] for p in base} & {p["run_id"] for p in plan}
    multi = pilot.plan_runs("T5", ["A", "C"], 1, [], rung=None)
    assert [p["run_id"] for p in multi] == ["T5-A-run01", "T5-C-run01"]


def test_parse_rotation_rejects_bad_pairs():
    assert pilot.parse_rotation("meta:gdm, meta:xai") == ["meta:gdm", "meta:xai"]
    for bad in ("meta-gdm", "meta:meta", "meta:google"):
        with pytest.raises(SystemExit):
            pilot.parse_rotation(bad)


# ---------------------------------------------------------------------------
# pilot.py --dry-run
# ---------------------------------------------------------------------------

def test_dry_run_without_output_uses_default_dir(tmp_path):
    """C4-6: no --output still saves logs, summary and (dry) ratings under data/pilot/<TEST>."""
    assert pilot.main(["--dry-run", "T0"]) == 0
    out = tmp_path / "pilot_default" / "T0"
    assert (out / "T0-run01.json").exists()
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
    base = (tmp_path / "T1b-run01.json").read_text()
    assert pilot.main(["--dry-run", "T1b", "--rung", "1", "--rotate", "meta:gdm,meta:xai",
                       "--turns", "2", "--output", str(tmp_path)]) == 0
    assert (tmp_path / "T1b-run01.json").read_text() == base
    rung_logs = sorted(p.name for p in tmp_path.glob("T1b-run*-rung1.json"))
    assert rung_logs == [f"T1b-run0{i}-rung1.json" for i in range(1, 5)], "2 runs per rotated seat"
    summary = json.loads((tmp_path / "pilot_summary_T1b-rung1.json").read_text())
    assert summary["overrides"] == {"intrusion.gain_share": 0.35}
    assert summary["rotation"] == ["meta:gdm", "meta:xai"]
    assert [r["rotation"] for r in summary["per_run"]] == ["meta:gdm", "meta:gdm", "meta:xai", "meta:xai"]
    assert (tmp_path / "pilot_summary_T1b.json").exists()
    record = json.loads((tmp_path / "T1b-run03-rung1.json").read_text())
    assert record["config"]["overrides"] == {"intrusion.gain_share": 0.35}
    assert record["config"]["rotation"] == "meta:xai"
    assert record["config"]["run_meta"]["rung"] == 1 and record["config"]["dry_run"] is True
    # xAI trails after the swap with meta.
    assert summary["per_run"][2]["trailing"] == "xai"


def test_dry_run_t6_builds_debriefs(tmp_path):
    assert pilot.main(["--dry-run", "T6", "--runs", "1", "--turns", "2",
                       "--output", str(tmp_path)]) == 0
    data = json.loads((tmp_path / "T6-run01.debrief.json").read_text())
    assert set(data) == set(LAB_KEYS)
    for entry in data.values():
        assert entry["prompt_chars"] > 0 and isinstance(entry["answer"], str)
    summary = json.loads((tmp_path / "pilot_summary_T6.json").read_text())
    assert "T6" in summary["decisions"]


def test_dry_run_t5_scripted_seat_skips_debrief_and_jury(tmp_path):
    assert pilot.main(["--dry-run", "T5", "--output", str(tmp_path)]) == 0
    assert {p.name for p in tmp_path.glob("T5-*-run01.json")} == {"T5-A-run01.json", "T5-C-run01.json"}
    record = json.loads((tmp_path / "T5-A-run01.json").read_text())
    assert record["turns"][0]["actors"]["xai"]["scripted"]
    assert record["final"].get("grand_jury") in (None, {})
    pooled = json.loads((tmp_path / "disposition_ratings.dry.json").read_text())
    assert "xai" not in pooled["ratings"], "the scripted seat is not a model: not rated"


def test_disposition_ratings_pool_and_replace_on_rerun(tmp_path):
    ratings = tmp_path / "pooled.json"
    args = ["--dry-run", "T0", "--output", str(tmp_path / "T0"), "--ratings-file", str(ratings)]
    assert pilot.main(args) == 0
    first = json.loads(ratings.read_text())
    assert set(first["ratings"]) == set(LAB_KEYS)
    for key, rows in first["ratings"].items():
        assert rows and all(r["family"] != first["families"][key] for r in rows)
        assert all(r["test"] == "T0" and r["run_id"] == "T0-run01" and r["dry_run"] for r in rows)
    assert all(c[0]["value"] == 50 for c in first["chosen"].values())
    # Another test adds to the pool; re-running T0 replaces its own rows.
    assert pilot.main(["--dry-run", "T3", "--turns", "2", "--output", str(tmp_path / "T3"),
                       "--ratings-file", str(ratings)]) == 0
    assert pilot.main(args) == 0
    pooled = json.loads(ratings.read_text())
    assert len(pooled["ratings"]["anthropic"]) == 2 * len(first["ratings"]["anthropic"])
    assert sorted(r["test"] for r in pooled["runs"]) == ["T0", "T3"]


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
    assert proj["projected_12_turn_run"] == pytest.approx(12 * 0.3 + 0.9)


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
    assert main_mod.main(["--turns", "1"]) == 2
    assert "placeholders for meta" in capsys.readouterr().out
    monkeypatch.setattr(main_mod, "preflight", lambda models, providers=None: ["XAI_API_KEY is not set"])
    assert main_mod.main(["--turns", "1", "--allow-placeholder-values"]) == 2
    assert "XAI_API_KEY" in capsys.readouterr().out


def test_main_warns_without_budget(monkeypatch, caplog):
    _FakeEngine.raises = BudgetExceeded("stop here")
    monkeypatch.setattr(main_mod, "SimulationEngine", _FakeEngine)
    monkeypatch.setattr(main_mod, "load_lab_configs", lambda: [])
    monkeypatch.setattr(main_mod, "preflight", lambda models, providers=None: [])
    with caplog.at_level("WARNING"):
        assert main_mod.main(["--turns", "1", "--set", "intrusion.gain_share=0.35"]) == 1
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
    assert rate_charters.build_parser().parse_args([]).budget == 10.0
