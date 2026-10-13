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
