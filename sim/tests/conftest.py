import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Invocation logs and unregistered call journals (core.logsetup, core.calllog) of
# the entry points the tests call go to a throwaway directory, never sim/data/logs.
os.environ.setdefault("SIM_LOG_DIR", tempfile.mkdtemp(prefix="sim-test-logs-"))
