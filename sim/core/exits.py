"""
Exit codes of every command-line entry point (main.py, pilot.py, campaign.py,
rate_charters.py, checks/scripted_checks.py and tools/*.py).

  0  done
  1  halted on the budget guard (the spend is on the ledger; partial records kept)
  2  refused, usage error, missing or unreadable input, or an aborted or crashed run
  3  the plan or check did not pass: pilot approve's re-priced plan exceeds the
     guard, a scripted check failed, the UPS identity replay does not reproduce
     the run, the juror's scores need revised examples (T8), a disposition seat
     defaulted (T2), or a finished run has no usable Grand Jury verdict after the
     whole fallback chain (main.py, campaign.py)
  4  waiting for human input (our hand scores or human ratings are not there yet)
"""

EXIT_OK = 0
EXIT_BUDGET = 1
EXIT_REFUSED = 2
EXIT_FAILED = 3
EXIT_WAITING = 4
