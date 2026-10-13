"""core.records.write_json_atomic: a crash mid-write never truncates a record (clean P2-5)."""

import json

import pytest

from core import records


def test_write_is_atomic_when_the_dump_fails(tmp_path, monkeypatch):
    path = tmp_path / "run.json"
    records.write_json_atomic(path, {"final": {"scores": [1]}})

    def broken(obj, f, **kw):
        f.write('{"final": {"sco')            # half a record, then the process "dies"
        raise KeyboardInterrupt

    monkeypatch.setattr(records.json, "dump", broken)
    with pytest.raises(KeyboardInterrupt):
        records.write_json_atomic(path, {"final": {}})
    assert json.loads(path.read_text()) == {"final": {"scores": [1]}}
    assert [p.name for p in tmp_path.iterdir()] == ["run.json"]     # no temp file left


def test_engine_writes_through_the_atomic_writer():
    import inspect
    from core import engine
    assert "write_json_atomic" in inspect.getsource(engine.SimulationEngine._write)


def _rec(final=None, **meta):
    return {"labs": {}, "turns": [], "final": final or {},
            "config": {"scenario": "S2", "condition": "A", "run_meta": meta}}


def test_is_finished_needs_final_scores():
    assert records.is_finished(_rec({"scores": [{"lab": "a"}]}))
    assert not records.is_finished(_rec({"ups_index": {}}))
    assert not records.is_finished(None)


def test_cell_of_keeps_kin_apart():
    assert records.cell_of(_rec(kin=True)) == "kin-S2-A" == records.cell_name("S2", "A", kin=True)
    assert records.cell_of(_rec()) == "S2-A"
    assert records.cell_of(_rec(pilot=True, test="T4")) == "pilot T4"


def test_finished_records_drops_partials_side_files_and_dry_dirs(tmp_path):
    done = _rec({"scores": [1]}, pilot=False)
    (tmp_path / "S1-A-run01.json").write_text(json.dumps(done))
    (tmp_path / "S1-A-run02.partial.json").write_text(json.dumps(_rec()))
    (tmp_path / "campaign_state.json").write_text(json.dumps({"runs": {}}))
    (tmp_path / "dry").mkdir()
    (tmp_path / "dry" / "d.json").write_text(json.dumps(done))
    assert [r["run_id"] for _p, r in records.load_records([tmp_path])] == ["S1-A-run01", "S1-A-run02"]
    assert [r["run_id"] for _p, r in records.finished_records([tmp_path], recursive=True)] == ["S1-A-run01"]
    assert len(records.finished_records([tmp_path], recursive=True, include_dry=True)) == 2


def test_world_setting_falls_back_to_the_shipped_world():
    from core.config import load_world, world_setting
    assert world_setting({"event_turn": 2}, "event_turn") == 2
    assert world_setting({}, "event_turn") == load_world()["event_turn"]
    assert world_setting(None, "macro_jury", "review_turns") == load_world()["macro_jury"]["review_turns"]
