import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Invocation logs and unregistered call journals (core.logsetup, core.calllog) of
# the entry points the tests call go to a throwaway directory, never sim/data/logs.
os.environ.setdefault("SIM_LOG_DIR", tempfile.mkdtemp(prefix="sim-test-logs-"))

# A connection failure stops at once in the tests (core.llm.network_wait_s): no
# test ever waits out the 30-minute network window. Tests of the window set it.
os.environ.setdefault("SIM_NETWORK_WAIT_S", "0")
