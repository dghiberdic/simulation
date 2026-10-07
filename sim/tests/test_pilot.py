"""Offline checks for the pilot driver, its presets and the charter-rating script.

Everything here runs with stub models, so the suite stays offline and fast and
never touches the network or config/.env.
"""

import json
from pathlib import Path

import pilot
import rate_charters
from core.config import CONFIG_DIR, load_json
from core.costs import get_tracker
from core.llm import register_stub

PILOT_FILE = Path(pilot.__file__).resolve().parent / "config" / "pilot.json"


# ---------------------------------------------------------------------------
# config/pilot.json
# ---------------------------------------------------------------------------

def test_pilot_config_loads_and_presets_well_formed():
    data = load_json(PILOT_FILE)
    assert data["budget_guard"] == 100
    assert "defaults" in data and "presets" in data

    defaults = data["defaults"]
    presets = data["presets"]
    assert presets, "at least one preset"

    for name, preset in presets.items():
        merged = {**defaults, **preset}
        for key in ("scenario", "fog", "turns", "runs", "a2a", "brief",
                    "choose_disposition", "grand_jury"):
            assert key in merged, f"{name} missing {key}"
        assert "condition" in merged or "conditions" in merged, f"{name} has no condition"
        assert merged["scenario"] in ("S1", "S2")
        assert isinstance(merged["turns"], int) and merged["turns"] >= 1
        assert isinstance(merged["runs"], int) and merged["runs"] >= 1


def test_t1b_ladder_is_well_formed():
    data = load_json(PILOT_FILE)
    ladder = data["presets"]["T1b"]["ladder"]
    assert isinstance(ladder, list) and ladder
    for rung in ladder:
        assert isinstance(rung, dict) and rung
        for dotted, value in rung.items():
            assert "." in dotted, f"override key {dotted!r} is not dotted"
            assert isinstance(value, (int, float)) and not isinstance(value, bool)
    # The rotation covers four seats (meta trails in T1a).
    assert len(data["presets"]["T1b"]["rotate"]) == 4


# ---------------------------------------------------------------------------
# pilot.py --dry-run
# ---------------------------------------------------------------------------

def test_dry_run_t0_offline_zero_cost(tmp_path):
    rc = pilot.main(["--dry-run", "T0", "--output", str(tmp_path)])
    assert rc == 0
    logs = list(tmp_path.glob("T0-run*.json"))
    assert logs, "a run log was written"
    summary = list(tmp_path.glob("pilot_summary_T0.json"))
    assert summary and json.loads(summary[0].read_text())["runs_completed"] == 1
    assert get_tracker().summary()["session_cost"] == 0.0
    assert get_tracker().persisted_total() == 0.0


def test_dry_run_t1b_completes_and_applies_rung(tmp_path):
    rc = pilot.main(["--dry-run", "T1b", "--runs", "1", "--turns", "2", "--output", str(tmp_path)])
    assert rc == 0
    assert list(tmp_path.glob("T1b-run*.json"))
    assert get_tracker().summary()["session_cost"] == 0.0

    # A rung from the ladder applies cleanly via load_world overrides.
    rc = pilot.main(["--dry-run", "T1b", "--runs", "1", "--turns", "2",
                     "--rung", "1", "--output", str(tmp_path)])
    assert rc == 0


def test_dry_run_t6_builds_debriefs(tmp_path):
    rc = pilot.main(["--dry-run", "T6", "--runs", "1", "--turns", "2", "--output", str(tmp_path)])
    assert rc == 0
    debriefs = list(tmp_path.glob("T6-run*.debrief.json"))
    assert debriefs, "a debrief file was written"
    data = json.loads(debriefs[0].read_text())
    assert set(data) == {"anthropic", "openai", "gdm", "meta", "xai"}
    for entry in data.values():
        assert entry["prompt_chars"] > 0
        assert entry["answer"] is None  # dry-run only builds the prompt


# ---------------------------------------------------------------------------
# rate_charters.py
# ---------------------------------------------------------------------------

def test_rate_lab_with_stub_writes_valid_ints():
    register_stub("ratetest", lambda s, u: json.dumps(
        {"time_horizon": 60.4, "transparency_threshold": 55, "risk_tolerance": 40.6,
         "democratic_tendency": 45, "notes": "stub"}))
    cfg = load_json(CONFIG_DIR / "labs" / "anthropic.json")
    cfg["model"] = "stub:ratetest"
    values, notes = rate_charters.rate_lab(cfg)
    assert set(values) == set(rate_charters.AXES)
    for v in values.values():
        assert isinstance(v, int) and 0 <= v <= 100
    assert values["time_horizon"] == 60 and values["risk_tolerance"] == 41


def test_rate_charters_dry_run_is_non_destructive():
    path = CONFIG_DIR / "labs" / "anthropic.json"
    before = path.read_text()
    rc = rate_charters.main(["--dry-run", "--lab", "anthropic"])
    assert rc == 0
    assert path.read_text() == before, "dry-run must not touch the config"
