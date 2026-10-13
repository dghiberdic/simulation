"""
The common __main__ wrapper of the command-line entry points: whatever goes
wrong, the exit code follows core.exits, so a caller (campaign.py, a shell
script) never reads a crash as the budget guard (1).

  BudgetExceeded that escapes main()      -> 1 (the guard)
  SystemExit("message"), an uncaught
  exception (missing file, bad config)   -> 2, with the message or traceback
  SystemExit(int) (argparse, main's own) -> unchanged
  KeyboardInterrupt (Ctrl-C)              -> not caught: Python's traceback and
                                             the signal status (130); partial
                                             logs are kept
"""

import sys
import traceback
from typing import Callable

from core.costs import BudgetExceeded
from core.exits import EXIT_BUDGET, EXIT_REFUSED


def run_main(main: Callable[[], int]) -> int:
    """main()'s exit code, with every failure mapped to the core.exits convention."""
    try:
        return main()
    except BudgetExceeded as e:
        print(f"HALTED on the budget guard: {e}", file=sys.stderr)
        return EXIT_BUDGET
    except SystemExit as e:
        if e.code is None or isinstance(e.code, int):
            raise
        print(e.code, file=sys.stderr)
        return EXIT_REFUSED
    except Exception:       # noqa: BLE001 — a crash is "refused or crashed" (2), never the guard
        traceback.print_exc()
        return EXIT_REFUSED
