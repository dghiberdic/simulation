"""core.cli.run_main: every entry point's failures follow core.exits (never 1 unless
the budget guard)."""

import subprocess
import sys
from pathlib import Path

import pytest

from core.cli import run_main
from core.costs import BudgetExceeded
from core.exits import EXIT_BUDGET, EXIT_REFUSED

SIM = Path(__file__).resolve().parent.parent


def _raise(exc):
    def main():
        raise exc
    return main


def test_failures_map_to_the_exit_convention(capsys):
    assert run_main(_raise(SystemExit("bad swap 'x'"))) == EXIT_REFUSED
    assert "bad swap" in capsys.readouterr().err
    assert run_main(_raise(FileNotFoundError("missing.csv"))) == EXIT_REFUSED
    assert run_main(_raise(BudgetExceeded("guard"))) == EXIT_BUDGET
    assert run_main(lambda: 4) == 4
    with pytest.raises(SystemExit) as e:          # argparse's own codes pass through
        run_main(_raise(SystemExit(2)))
    assert e.value.code == 2
    with pytest.raises(KeyboardInterrupt):        # Ctrl-C keeps Python's signal status
        run_main(_raise(KeyboardInterrupt()))


@pytest.mark.parametrize("argv", [
    ["tools/summarize_run.py", "--t6-tally", "/nonexistent/sheet.csv"],
    ["pilot.py", "approve", "--dry-run", "--output", "/nonexistent/pilot"],
])
def test_entry_points_never_exit_1_on_bad_input(argv):
    out = subprocess.run([sys.executable, *argv], cwd=SIM, capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin"})
    assert out.returncode == EXIT_REFUSED, out.stdout + out.stderr
