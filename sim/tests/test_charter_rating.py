"""Charter values rated by an external rater (spec §3.2): Kimi K3 rates every
charter (rate_charters.py, default), each seat's self-rating is kept apart in
charter_values_self (--self) and never read by the engine; nothing about the
rating (who rated, the rationale, the self-ratings) reaches a seat or a juror,
and the rating prompt holds only the charter's name and text."""

import json
import re
import shutil

import pytest

import main
import rate_charters
from core import calllog
from core.config import (CONFIG_DIR, LAB_ORDER, build_labs_and_world, load_charter, load_json,
                         load_lab_configs, load_world)
from core.jury import GRAND_JURY_MODEL, MACRO_JURY_MODEL, DispositionJury, screen_mandate
from core.llm import register_stub
from core.preflight import placeholder_labs
from core.state import VALUE_AXES
from core.transcript import disposition_transcript, full_transcript

CFGS = {c["key"]: c for c in load_lab_configs()}
CHARTERS = [load_charter(c) for c in CFGS.values()]
KIMI = load_json(rate_charters.ratings_file("kimi-k3"))["ratings"]
SEAT_PURPOSES = {"actor", "a2a", "interview"}


def _notes_snippets(cfg):
    """Both rationales of a seat (the external and the self one) cut into 50-character
    pieces: any 100 characters of either, quoted anywhere, contain a whole piece.
    A piece the rationale quotes from a charter is charter text, not a leak: left out."""
    out = []
    for f in ("charter_values_notes", "charter_values_self_notes"):
        text = cfg.get(f) or ""
        out += [text[i:i + 50] for i in range(0, len(text) - 49, 50)]
    return [piece for piece in out if not any(piece in t for t in CHARTERS)]


def _self_number_patterns(cfg, min_gap=5):
    """How a self-rating would show if it leaked: "<axis> N", "<axis>: N", '"<axis>": N',
    for the axes where it differs from the charter value by more than a few turns of
    value pull (at most one point a turn) could explain."""
    pats = []
    for a, v in (cfg.get("charter_values_self") or {}).items():
        if abs(v - cfg["charter_values"][a]) >= min_gap:
            pats.append(re.compile(rf'"?{a}"?\s*[:=]?\s*{v}\b'))
    return pats


def _leaks(text, own=None):
    """Mentions of the rating in a prompt: the rater's name or role, the config field
    names, any seat's rationale (external or self), and (with `own`, a seat's config)
    that seat's self-rating numbers."""
    found = []
    low = text.lower()
    for word in (r"\bkimi\b", r"\brater\b", r"charter_values", r"self-rat", r"\brated by\b"):
        if re.search(word, low):
            found.append(word)
    for cfg in CFGS.values():
        found += [f"{cfg['key']} rationale" for s in _notes_snippets(cfg) if s in text]
    if own is not None:
        found += [f"{own['key']} self {p.pattern}" for p in _self_number_patterns(own) if p.search(text)]
    return found


# ---------------------------------------------------------------------------
# Config layout
# ---------------------------------------------------------------------------

def test_configs_hold_kimis_values_and_keep_the_self_ratings_apart():
    for key in LAB_ORDER:
        cfg = CFGS[key]
        assert cfg["charter_values"] == KIMI[key]["values"], key
        assert cfg["charter_values_source"] == "rated by kimi-k3 on 2026-10-15"
        assert cfg["charter_values_notes"] == KIMI[key]["rationale"]
        assert set(cfg["charter_values_self"]) == set(VALUE_AXES)
        assert cfg["charter_values_self_source"] == f"rated by {cfg['model']} on 2026-10-15"
        assert cfg["charter_values_self_notes"]
    assert CFGS["xai"]["charter_values"] == {"time_horizon": 50, "transparency_threshold": 57,
                                             "risk_tolerance": 34, "democratic_tendency": 31}
    assert placeholder_labs() == []


def test_the_engine_starts_from_charter_values_never_the_self_ratings():
    labs, _world = build_labs_and_world(load_world(), charters=False)
    for lab in labs:
        assert lab.charter_values == CFGS[lab.key]["charter_values"]
        assert lab.values == CFGS[lab.key]["charter_values"]
        assert not any("self" in f for f in vars(lab))


def test_the_canonical_rater_is_the_juror_model():
    assert rate_charters.RATER_MODEL == GRAND_JURY_MODEL == MACRO_JURY_MODEL == "kimi-k3"


# ---------------------------------------------------------------------------
# rate_charters.py: default external rating, --self, --rater
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_rating(monkeypatch):
    """main() with preflight, the model call and the writes captured."""
    seen = {"preflight": [], "rated": [], "written": [], "recorded": []}

    def preflight(models, providers=None):
        seen["preflight"] = list(models)
        return []

    def rate_lab(cfg, run_id="rate_charters"):
        seen["rated"].append(dict(cfg))
        return {a: 50 for a in VALUE_AXES}, "why"
    monkeypatch.setattr(rate_charters, "preflight", preflight)
    monkeypatch.setattr(rate_charters, "rate_lab", rate_lab)
    monkeypatch.setattr(rate_charters, "write_values",
                        lambda key, values, model, notes, mode="external", on=None:
                        seen["written"].append((key, model, mode)))
    monkeypatch.setattr(rate_charters, "record_rating",
                        lambda model, key, *a, **k: seen["recorded"].append((key, model)))
    return seen


def test_default_rates_every_charter_with_kimi_and_writes_charter_values(fake_rating, tmp_path):
    assert rate_charters.main(["--spend-file", str(tmp_path / "s.json")]) == 0
    assert fake_rating["preflight"] == ["kimi-k3"]                 # only Kimi's key needed
    assert [c["model"] for c in fake_rating["rated"]] == ["kimi-k3"] * 5
    for c in fake_rating["rated"]:      # the rater gets the charter, never the seat's values or model
        assert set(c) == {"key", "lab", "charter_name", "charter_file", "model", "provider", "effort"}
        assert c["provider"] is None and c["effort"] is None
    assert fake_rating["written"] == [(k, "kimi-k3", "external") for k in LAB_ORDER]
    assert fake_rating["recorded"] == [(k, "kimi-k3") for k in LAB_ORDER]


def test_self_rates_with_each_seats_own_model_into_the_self_fields(fake_rating, tmp_path):
    assert rate_charters.main(["--self", "--spend-file", str(tmp_path / "s.json")]) == 0
    assert fake_rating["preflight"] == [CFGS[k]["model"] for k in LAB_ORDER]
    assert [c["model"] for c in fake_rating["rated"]] == [CFGS[k]["model"] for k in LAB_ORDER]
    assert fake_rating["rated"][2]["provider"] == "gemini_api"     # the seat's own route
    assert fake_rating["written"] == [(k, CFGS[k]["model"], "self") for k in LAB_ORDER]
    assert fake_rating["recorded"] == []


def test_another_rater_writes_only_its_archive(fake_rating, tmp_path):
    assert rate_charters.main(["--rater", "deepseek-v4-pro", "--lab", "xai",
                               "--spend-file", str(tmp_path / "s.json")]) == 0
    assert fake_rating["preflight"] == ["deepseek-v4-pro"]
    assert fake_rating["written"] == [] and fake_rating["recorded"] == [("xai", "deepseek-v4-pro")]
    with pytest.raises(SystemExit):
        rate_charters.build_parser().parse_args(["--self", "--rater", "x"])


def test_default_preflight_needs_moonshot_not_the_seat_keys(monkeypatch, tmp_path, capsys):
    for env in ("MOONSHOT_API_KEY", "JUROR_BASE_URL", "JUROR_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(rate_charters, "rate_lab", lambda *a, **k: pytest.fail("called a model"))
    assert rate_charters.main(["--spend-file", str(tmp_path / "s.json")]) == 2
    out = capsys.readouterr().out
    assert "MOONSHOT_API_KEY" in out
    assert not any(k in out for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY"))


def test_wrapper_maps_model_to_rater(monkeypatch):
    from tools import rate_charters_external as ext
    assert ext.translate(["--model", "deepseek-v4-pro", "--lab", "xai"]) == \
        ["--rater", "deepseek-v4-pro", "--lab", "xai"]
    got = {}
    monkeypatch.setattr(ext.rate_charters, "main", lambda argv: got.setdefault("argv", argv) and 0)
    ext.main(["--dry-run"])
    assert got["argv"] == ["--dry-run"]


@pytest.fixture
def lab_dir(tmp_path, monkeypatch):
    (tmp_path / "labs").mkdir()
    for key in LAB_ORDER:
        shutil.copy(CONFIG_DIR / "labs" / f"{key}.json", tmp_path / "labs" / f"{key}.json")
    monkeypatch.setattr(rate_charters, "CONFIG_DIR", tmp_path)
    return tmp_path / "labs"


def test_write_values_external_and_self_fields(lab_dir):
    before = json.loads((lab_dir / "meta.json").read_text())
    rate_charters.write_values("meta", {a: 11 for a in VALUE_AXES}, "muse-spark-1.3", "own view",
                               mode="self", on="2026-10-16")
    cfg = json.loads((lab_dir / "meta.json").read_text())
    assert cfg["charter_values"] == before["charter_values"]            # untouched by --self
    assert cfg["charter_values_source"] == before["charter_values_source"]
    assert cfg["charter_values_self"] == {a: 11 for a in VALUE_AXES}
    assert cfg["charter_values_self_source"] == "rated by muse-spark-1.3 on 2026-10-16"
    assert cfg["charter_values_self_notes"] == "own view"
    rate_charters.write_values("meta", {a: 22 for a in VALUE_AXES}, "kimi-k3", "", on="2026-10-16")
    cfg = json.loads((lab_dir / "meta.json").read_text())
    assert cfg["charter_values"] == {a: 22 for a in VALUE_AXES}
    assert cfg["charter_values_source"] == "rated by kimi-k3 on 2026-10-16"
    assert "charter_values_notes" not in cfg                            # no stale rationale
    assert cfg["charter_values_self"] == {a: 11 for a in VALUE_AXES}


def test_record_rating_merges_into_the_archive(tmp_path):
    path = tmp_path / "r.json"
    rate_charters.record_rating("kimi-k3", "xai", "X", {a: 1 for a in VALUE_AXES}, "a", path=path)
    rate_charters.record_rating("kimi-k3", "gdm", "G", {a: 2 for a in VALUE_AXES}, "b", path=path)
    data = json.loads(path.read_text())
    assert data["model"] == "kimi-k3" and set(data["ratings"]) == {"xai", "gdm"}
    assert data["ratings"]["gdm"]["values"]["time_horizon"] == 2


def test_dry_run_default_and_self_write_nothing(lab_dir):
    before = {p.name: p.read_text() for p in lab_dir.iterdir()}
    archive = rate_charters.ratings_file("kimi-k3").read_text()
    assert rate_charters.main(["--dry-run"]) == 0
    assert rate_charters.main(["--dry-run", "--self", "--lab", "gdm"]) == 0
    assert {p.name: p.read_text() for p in lab_dir.iterdir()} == before
    assert rate_charters.ratings_file("kimi-k3").read_text() == archive


# ---------------------------------------------------------------------------
# (c) The prompt sent to the rater: charter name and text only
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", LAB_ORDER)
def test_the_rating_prompt_holds_only_the_charter(key):
    seen = {}

    def stub(system, user):
        seen.update(system=system, user=user)
        return json.dumps({a: 50 for a in VALUE_AXES} | {"rationale": "r"})
    register_stub("kimi_capture", stub)
    cfg = rate_charters.rater_cfg(CFGS[key], "stub:kimi_capture")
    rate_charters.rate_lab(cfg)
    assert seen["system"] == rate_charters._SYSTEM
    assert seen["user"] == rate_charters._user(CFGS[key]["charter_name"], load_charter(CFGS[key]))
    prompt = seen["system"] + seen["user"]
    assert _leaks(prompt, own=dict(CFGS[key], charter_values={a: -99 for a in VALUE_AXES})) == []
    for a, v in CFGS[key]["charter_values_self"].items():      # no axis is given a number
        assert not re.search(rf'"?{a}"?\s*[:=]?\s*\d', prompt), a
    assert CFGS[key]["model"] not in prompt


# ---------------------------------------------------------------------------
# (a), (b) What seats and jurors see in a run
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def dry_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("leak")
    assert main.main(["--scenario", "S2", "--condition", "C", "--dry-run", "--turns", "3",
                      "--interview-turns", "2,final", "--run-id", "leak", "--output", str(out)]) == 0
    record = json.loads((out / "leak.json").read_text())
    return calllog.read_calls(out / "leak.calls.jsonl"), record


def _prompt(call):
    hist = call.get("history") or []
    return "\n".join([call.get("system") or "", call.get("user") or "", json.dumps(hist)])


def test_seat_prompts_carry_only_their_own_value_profile(dry_run):
    calls, _ = dry_run
    seat_calls = [c for c in calls if c["purpose"] in SEAT_PURPOSES]
    assert {c["purpose"] for c in seat_calls} == SEAT_PURPOSES
    first = {}
    for c in seat_calls:
        text = _prompt(c)
        key = c["model"].split("_", 1)[1]
        assert _leaks(text, own=CFGS[key]) == [], (c["purpose"], key)
        if c["purpose"] == "actor" and key not in first:
            first[key] = text
    assert set(first) == set(LAB_ORDER)
    for key, text in first.items():
        profiles = re.findall(r"Your lab's value profile \(0–100; does not enter the Prosperity Score\): (.*)", text)
        assert len(profiles) == 1, key
        shown = {a: float(v) for a, v in re.findall(r"(\w+) (\d+(?:\.\d+)?)", profiles[0])}
        # month 1: the charter value moved at most one point by the value pull
        assert set(shown) == set(VALUE_AXES)
        for a in VALUE_AXES:
            assert abs(shown[a] - CFGS[key]["charter_values"][a]) <= 1, (key, a)


def test_juror_prompts_never_mention_the_charter_rating(dry_run):
    calls, record = dry_run
    juror_calls = [c for c in calls if c["purpose"] in ("grand_jury", "macro_jury")]
    assert {c["purpose"] for c in juror_calls} == {"grand_jury", "macro_jury"}
    for c in juror_calls:
        text = _prompt(c)
        assert _leaks(text) == [], c["purpose"]
        assert "value profile" not in text          # no seat's values are shown to a juror
    # The Grand Jury's transcript shows the state's values (MacroJury reviews) only.
    full = full_transcript(record)
    assert "value profile" not in full and _leaks(full) == []
    # T2 disposition rater and the mandate screen (both Kimi in a real run).
    seen = []
    register_stub("disp_capture", lambda s, u: seen.append(s + u) or json.dumps(
        {"disposition": 50, "reason": "r"}))
    register_stub("screen_capture", lambda s, u: seen.append(s + u) or json.dumps(
        {"violates": False, "topics": []}))
    for key in LAB_ORDER:
        DispositionJury(["stub:disp_capture"]).rate_seat(disposition_transcript(record, key), seat=key)
    screen_mandate("Expand domestic compute.", "stub:screen_capture")
    assert len(seen) == 6
    for text in seen:
        assert _leaks(text) == [] and "value profile" not in text
