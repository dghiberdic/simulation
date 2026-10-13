"""The `sim` command (cli.py): dispatch, help, exit codes, and the logs / status
views on a finished dry run."""

import logging
import os

import pytest

import cli


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                 # cli.main chdirs to sim/; monkeypatch restores
    monkeypatch.setenv("SIM_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SIM_COLOR", "never")
    for k in ("SIM_QUIET", "SIM_VERBOSE"):
        monkeypatch.delenv(k, raising=False)
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_sim_handler", None):
            root.removeHandler(h)
            h.close()


def test_help_lists_every_command(capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    for name in cli.HANDLERS:
        assert name in out


def test_no_command_and_unknown_command_exit_2(capsys):
    assert cli.main([]) == 2
    assert cli.main(["frobnicate"]) == 2
    assert "unknown command" in capsys.readouterr().err


@pytest.mark.parametrize("cmd", ["run", "pilot", "campaign", "rate-charters", "check", "test", "status", "logs"])
def test_subcommand_help(cmd, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main([cmd, "--help"])
    assert e.value.code == 0
    assert f"sim {cmd}" in capsys.readouterr().out


def test_tool_list():
    assert cli.main(["tool", "--help"]) == 0
    assert cli.main(["tool", "nope"]) == 2


def test_global_flags_set_the_environment(tmp_path):
    assert cli.main(["-q", "--no-color", "--log-dir", str(tmp_path / "L"), "status", "--dir", str(tmp_path)]) == 0
    assert os.environ["SIM_QUIET"] == "1" and os.environ["SIM_COLOR"] == "never"
    assert os.environ["SIM_LOG_DIR"] == str((tmp_path / "L").resolve())


def test_exit_code_passes_through_a_refusal(tmp_path, capsys):
    # the kin cell is S2 under condition A only: refused (2), nothing run
    code = cli.main(["run", "--kin", "--condition", "B", "--dry-run", "--output", str(tmp_path / "o")])
    assert code == 2


@pytest.fixture
def finished_run(tmp_path):
    runs = tmp_path / "runs"
    code = cli.main(["run", "--scenario", "S1", "--condition", "A", "--dry-run", "--no-grand-jury",
                     "--run-id", "clirun", "--output", str(runs / "clirun")])
    assert code == 0
    return runs


def test_logs_and_status_on_a_finished_run(finished_run, capsys):
    capsys.readouterr()
    assert cli.main(["logs", "--dir", str(finished_run)]) == 0
    out = capsys.readouterr().out
    assert "clirun" in out and "finished" in out and "S1-A" in out

    assert cli.main(["logs", "clirun", "--dir", str(finished_run)]) == 0
    out = capsys.readouterr().out
    assert "Month 1" in out and "FINAL STANDING" in out

    assert cli.main(["logs", "clirun", "--dir", str(finished_run), "--events", "--tag", "save"]) == 0
    out = capsys.readouterr().out
    assert "[save]" in out and "Month 3" not in out

    assert cli.main(["logs", "clirun", "--dir", str(finished_run), "--calls"]) == 0
    out = capsys.readouterr().out
    assert "actor" in out and "total $0.0000" in out

    assert cli.main(["status", "--dir", str(finished_run)]) == 0
    assert "clirun" in capsys.readouterr().out

    assert cli.main(["logs", "missing-run", "--dir", str(finished_run)]) == 2
