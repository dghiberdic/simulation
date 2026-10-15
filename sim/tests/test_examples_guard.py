"""§7/T8: a real main run (main.py, campaign.py run) refuses to start without the
hand-scored few-shot examples (config/jury_examples.json), before anything is
called; dry runs and runs without the Grand Jury are not affected."""

import pytest

import campaign
import main
from core import jury
from core.exits import EXIT_REFUSED

EXAMPLE = {"seat": "A", "excerpt": "x", "scores": {}}


@pytest.fixture
def no_examples(monkeypatch):
    monkeypatch.setattr(jury, "load_examples", lambda path=None: [])


@pytest.fixture
def no_engine(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("the engine must not be built")
    monkeypatch.setattr(main, "SimulationEngine", boom)


def test_real_main_run_refused_without_examples(tmp_path, no_examples, no_engine, capsys):
    args = main.build_parser().parse_args(["--scenario", "S1", "--condition", "A", "--allow-placeholder-values", "--disposition", "50",
                                           "--output", str(tmp_path / "o"), "--run-id", "r",
                                           "--spend-file", str(tmp_path / "spend.json")])
    code, record = main.run_cell(args)
    assert code == EXIT_REFUSED and record is None
    assert "no hand-scored few-shot examples" in capsys.readouterr().out
    assert not (tmp_path / "o").exists() or not any((tmp_path / "o").iterdir())   # nothing written
    assert not (tmp_path / "spend.json").exists()                                     # nothing paid


def test_examples_check_runs_before_placeholder_and_key_checks(no_examples):
    # even with placeholder charter values and no keys, the missing examples are named first
    assert "few-shot" in main.real_run_problems([], ["kimi-k3"], True, False)


def test_examples_present_pass_the_guard(monkeypatch):
    monkeypatch.setattr(jury, "load_examples", lambda path=None: [EXAMPLE])
    assert main.no_examples_problem() == ""


def test_malformed_examples_refused(monkeypatch):
    def bad(path=None):
        raise ValueError("example 1: no scores")
    monkeypatch.setattr(jury, "load_examples", bad)
    assert "malformed" in main.no_examples_problem()


def test_no_grand_jury_and_dry_runs_are_not_guarded(no_examples):
    assert "few-shot" not in main.real_run_problems([], [], False, True)


def test_dry_main_run_still_runs_without_examples(tmp_path, no_examples):
    code = main.main(["--scenario", "S1", "--condition", "A", "--dry-run", "--no-grand-jury",
                      "--output", str(tmp_path / "d"), "--run-id", "d"])
    assert code == 0


def test_real_campaign_refused_before_planning(tmp_path, no_examples, monkeypatch, capsys):
    def boom(*a, **kw):
        raise AssertionError("no cell may run")
    monkeypatch.setattr(main, "run_cell", boom)
    code = campaign.main(["run", "--output", str(tmp_path / "runs"),
                          "--spend-file", str(tmp_path / "spend.json")])
    assert code == EXIT_REFUSED
    assert "no hand-scored few-shot examples" in capsys.readouterr().out
    assert not (tmp_path / "runs" / campaign.STATE_NAME).exists()


def test_campaign_plan_and_dry_run_are_not_refused(tmp_path, no_examples):
    assert campaign.main(["plan", "--dry-run", "--output", str(tmp_path / "p"),
                          "--spend-file", str(tmp_path / "s.json")]) == 0
