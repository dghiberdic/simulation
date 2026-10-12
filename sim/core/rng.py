#!/usr/bin/env python3
"""
Keyed random draws (CODE_V5_CONTRACT "turn_rec['draws']"; spec §5.3, §8).

Every uniform the engine draws is keyed by (turn, purpose, entry, lab) and is a
pure function of (seed, key): a hash of the key seeds the value, so there is no
shared stream. Removing one actor's actions (the §8 UPS-contribution replay)
therefore leaves every other draw unchanged.

    u = KeyedRNG(seed).uniform(turn, purpose, entry_id, lab)

purpose is one of PURPOSES. entry_id is the intrusion's stable draw id for the
intrusion purposes (core.intrusion.draw_id: the named intruders and targets,
which stay the same when one named committer drops out), a draw index for
"policy", else None. lab is the lab the draw is about, else None.

Each draw is appended to the log as {"key": [turn, purpose, entry_id, lab],
"u": float}; the engine moves a turn's draws into turn_rec["draws"] with take().
A replay may pass the logged draws back (KeyedRNG(seed, replay=draws)): a
logged key returns its logged value, any other key its hashed value.
"""

import hashlib
import random
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

PURPOSES = ("shock", "success", "exposure", "lead", "lead_recipient", "policy")

_SCALE = float(1 << 53)

Key = Tuple[int, str, Optional[str], Optional[str]]


def _key(turn: int, purpose: str, entry_id: Any = None, lab: Any = None) -> Key:
    if purpose not in PURPOSES:
        raise ValueError(f"unknown draw purpose {purpose!r}")
    return (int(turn), purpose, None if entry_id is None else str(entry_id),
            None if lab is None else str(lab))


def keyed_uniform(seed: int, turn: int, purpose: str, entry_id: Any = None,
                  lab: Any = None) -> float:
    """The uniform for one key, in the open interval (0, 1); no logging."""
    k = _key(turn, purpose, entry_id, lab)
    text = "\x1f".join(["sfpi-v5", str(seed), str(k[0]), k[1], k[2] or "-", k[3] or "-"])
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    n = int.from_bytes(digest[:8], "big") >> 11          # 53 bits
    return (n + 0.5) / _SCALE


class KeyedRNG:
    """Deterministic keyed uniforms with a draw log (module docstring)."""

    def __init__(self, seed: int = 0, replay: Optional[Iterable[Dict[str, Any]]] = None):
        self.seed = seed
        self._log: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._replay: Dict[Key, float] = {}
        for d in replay or []:
            self._replay[_key(*d["key"])] = float(d["u"])

    def uniform(self, turn: int, purpose: str, entry_id: Any = None, lab: Any = None) -> float:
        k = _key(turn, purpose, entry_id, lab)
        u = self._replay.get(k)
        if u is None:
            u = keyed_uniform(self.seed, *k)
        with self._lock:
            self._log.append({"key": list(k), "u": u})
        return u

    def take(self) -> List[Dict[str, Any]]:
        """The draws logged since the last take(), in draw order; clears the log."""
        with self._lock:
            out, self._log = self._log, []
        return out

    def stream(self, turn: int, purpose: str, lab: Optional[str] = None) -> "KeyedRandom":
        """A random.Random whose every draw is keyed (turn, purpose, "<index>", lab)."""
        return KeyedRandom(self, turn, purpose, lab)


class KeyedRandom(random.Random):
    """random.Random facade for code that wants a Random (scripted policies): random(),
    and everything built on it (choice, uniform, gauss, shuffle, randrange), draws
    keyed uniforms with entry_id "0", "1", ... in call order."""

    def __init__(self, keyed: KeyedRNG, turn: int, purpose: str, lab: Optional[str]):
        self._keyed, self._turn, self._purpose, self._lab = keyed, turn, purpose, lab
        self._n = 0
        super().__init__(0)

    def random(self) -> float:
        i = self._n
        self._n += 1
        return self._keyed.uniform(self._turn, self._purpose, str(i), self._lab)

    def seed(self, *args: Any, **kwargs: Any) -> None:     # draws are keyed, not seeded
        super().seed(0)

    def getstate(self):                                     # pragma: no cover - not used
        raise NotImplementedError("KeyedRandom has no stream state")

    def setstate(self, state):                              # pragma: no cover - not used
        raise NotImplementedError("KeyedRandom has no stream state")
