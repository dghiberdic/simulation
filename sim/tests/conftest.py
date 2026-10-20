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

# The suite tests the spec's 8-turn design (event on turn 4, §6, §9.2). The
# max-pressure branch ships a 4-turn go/no-go pilot (world.json turns 4,
# event_turn 2, a scripted access on turn 3, MacroJury review_turns "all", §6.6), so world.json is read here
# with the design's run length and review turns;
# everything else (fees, pressure) is as shipped. Patched before any test module
# imports core.config's users, since prompts.packets reads the event turn at import.
# tests/test_max_pressure_pilot.py reads the shipped file itself (SHIPPED_WORLD).
import core.config as _config  # noqa: E402

DESIGN_RUN = {"turns": 8, "event_turn": 4}
DESIGN_REVIEW_TURNS = [2, 6, 10]   # the shipped pilot reviews after every turn ("all")
_load_json = _config.load_json
SHIPPED_WORLD = _config.CONFIG_DIR / "world.json"


def _design_load_json(path):
    data = _load_json(path)
    if Path(path) == SHIPPED_WORLD:
        data.update(DESIGN_RUN)
        data["scripted_access"] = None   # the pilot's scripted access is tested on its own
        data["macro_jury"] = {**data["macro_jury"], "review_turns": DESIGN_REVIEW_TURNS}
    return data


_config.load_json = _design_load_json
