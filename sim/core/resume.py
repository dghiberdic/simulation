"""
Resume a run from its partial log (pilot R-1), and the log readers the §8
replay (tools/ups_replay.py) shares.

The engine saves <run_id>.partial.json after every turn. A new run with the
same run id, output directory and set-up (the whole logged config but
run_meta, and the seats' models) is rebuilt from that log instead of being
paid for again:

  * Logged replies. Every model call of a logged complete turn is answered
    from the log, with no provider call and nothing charged: a proposal or a
    message round returns the logged attempts and the reply parsed from the
    accepted (last) attempt's text, put through the same validator; a seat
    whose call failed gets its logged error; the interviews and the MacroJury
    review are the logged ones (the review's values and mandate are applied
    to the world, as in the §8 replay). Prompts are still built, so the
    engine's state evolves exactly as in the run.
  * Logged draws. The engine's KeyedRNG replays every logged draw
    (KeyedRNG(seed, replay=draws)).
  * Check. Each rebuilt turn record must equal the logged one (JSON-normalised);
    any difference stops the resume with ResumeMismatch before anything is
    called or written, and the partial log is left as it was.

The run then continues live from the first turn the log does not hold
complete. A turn that was in flight when the run stopped ("incomplete": true)
is run again live; it and any MacroJury review that did not finish are kept in
record["resumed"] (provenance; their paid attempts stay on record). Apart from
"resumed" and the call latencies, the resumed run's record equals the record
of an uninterrupted run given the same replies.
"""

import copy
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from core.llm import parse_json_detail
from core.records import is_finished, read_json

logger = logging.getLogger(__name__)

# Config keys that may differ between the partial log and the resuming run.
_FREE_CONFIG_KEYS = ("run_meta",)
# Turn-record keys the check leaves out: the review is checked on its own.
_UNCHECKED_TURN_KEYS = ("macro_jury",)
# Set again after a review: a logged finished review's mandate is in the logged list.
_AFTER_REVIEW_KEYS = ("mandates_active",)


class ResumeMismatch(RuntimeError):
    """The partial log cannot be rebuilt exactly; nothing was called or written."""


class LoggedFailure(Exception):
    """A seat call that failed in the logged run (its logged error string)."""

    def __init__(self, error: Optional[str]):
        super().__init__(error or "no logged reply")
        self.error = error


# ---------------------------------------------------------------------------
# Log readers (shared with tools/ups_replay.py)
# ---------------------------------------------------------------------------

def complete_turns(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The turn records not marked incomplete."""
    return [t for t in record.get("turns", []) if not t.get("incomplete")]


def all_draws(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every logged keyed draw, in turn order."""
    return [d for t in record.get("turns", []) for d in t.get("draws") or []]


def replayable_turns(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The leading run of complete turns 1, 2, ..., n (stops at the first gap or
    incomplete turn)."""
    out = []
    for t in record.get("turns", []):
        if not isinstance(t, dict) or t.get("incomplete") or t.get("turn") != len(out) + 1:
            break
        out.append(t)
    return out


def review_done(turn_rec: Dict[str, Any]) -> bool:
    mj = turn_rec.get("macro_jury")
    return isinstance(mj, dict) and not mj.get("incomplete")


def normalise(obj: Any) -> Any:
    """JSON round trip: what the record holds once written."""
    return json.loads(json.dumps(obj, default=str))


# ---------------------------------------------------------------------------
# Finding a partial log
# ---------------------------------------------------------------------------

def partial_path(output_dir: Path, run_id: str) -> Path:
    return Path(output_dir) / f"{run_id}.partial.json"


def _read_partial(output_dir: Path, run_id: str) -> Optional[Dict[str, Any]]:
    rec = read_json(partial_path(output_dir, run_id))
    if not isinstance(rec, dict) or not isinstance(rec.get("turns"), list) \
            or not isinstance(rec.get("config"), dict):
        return None
    return rec


def remove_stale_partial(output_dir: Optional[Path], run_id: str) -> bool:
    """Remove <run_id>.partial.json when it sits beside the finished record of the same
    run (a stop between writing the final record and removing the log): the record
    holds everything it does. A partial log of another set-up is kept."""
    if output_dir is None:
        return False
    rec = _read_partial(Path(output_dir), run_id)
    final = read_json(Path(output_dir) / f"{run_id}.json")
    if rec is None or not is_finished(final) or setup_diffs(rec, final):
        return False
    partial_path(output_dir, run_id).unlink(missing_ok=True)
    logger.info(f"[resume] {run_id}: removed the partial log left beside its finished record")
    return True


def load_partial(output_dir: Optional[Path], run_id: str) -> Optional[Dict[str, Any]]:
    """The partial log of run_id, or None: missing, unreadable, not a run record, or
    left over beside the finished record of the same run (then removed)."""
    if output_dir is None or remove_stale_partial(output_dir, run_id):
        return None
    return _read_partial(Path(output_dir), run_id)


def setup_diffs(partial: Dict[str, Any], record: Dict[str, Any]) -> List[str]:
    """How the partial log's set-up differs from a new run's record ([] when it is
    the same run): every config key but run_meta, and the seats' models."""
    old, new = partial.get("config") or {}, normalise(record.get("config") or {})
    diffs = [k for k in sorted(set(old) | set(new))
             if k not in _FREE_CONFIG_KEYS and old.get(k) != new.get(k)]
    if normalise(partial.get("labs") or {}) != normalise(record.get("labs") or {}):
        diffs.append("labs")
    return diffs


def set_aside(output_dir: Path, run_id: str) -> Path:
    """Move a partial log of another set-up out of the way (kept, never globbed as a
    record: the name no longer ends in .json)."""
    src = partial_path(output_dir, run_id)
    dst = src.with_name(f"{src.name}.superseded-{time.strftime('%Y%m%d-%H%M%S')}")
    src.replace(dst)
    return dst


# ---------------------------------------------------------------------------
# The logged run
# ---------------------------------------------------------------------------

class LoggedRun:
    """The replayable part of a partial log: turns 1..last, their draws and reviews."""

    def __init__(self, record: Dict[str, Any]):
        self.record = record
        turns = replayable_turns(record)
        self.turns: Dict[int, Dict[str, Any]] = {int(t["turn"]): t for t in turns}
        self.last = len(turns)
        self.draws = [d for t in turns for d in t.get("draws") or []]
        self.reviews = {int(m["turn"]): m for m in record.get("macro_jury") or []
                        if isinstance(m, dict) and m.get("turn") in self.turns}

    def covers(self, turn: int) -> bool:
        return turn in self.turns

    def provenance(self) -> Dict[str, Any]:
        """record["resumed"] entry: where the live run takes over and what was dropped
        (the turn in flight and unfinished reviews, with their paid attempts)."""
        dropped = [t for t in self.record.get("turns", []) if int(t.get("turn") or 0) > self.last]
        reviews = [{"turn": n, "macro_jury": t["macro_jury"]} for n, t in self.turns.items()
                   if t.get("macro_jury") is not None and not review_done(t)]
        return {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "replayed_turns": self.last,
                "live_from_turn": self.last + 1, "dropped_turns": dropped, "dropped_reviews": reviews}

    # -- replies -------------------------------------------------------------

    def _actor(self, turn: int, key: str) -> Dict[str, Any]:
        return ((self.turns[turn].get("actors") or {}).get(key)) or {}

    @staticmethod
    def _answer(attempts: List[Dict[str, Any]], validate: Optional[Callable[[dict], Optional[str]]],
                expect_keys: Iterable[str], where: str) -> Tuple[Optional[dict], List[Dict[str, Any]]]:
        """(reply, attempts) as complete_json returned them: the reply is the accepted
        (last, error-free) attempt parsed and validated again; None when none was."""
        attempts = copy.deepcopy(attempts)
        if not attempts or attempts[-1].get("error") is not None:
            return None, attempts
        obj, err, _ = parse_json_detail(attempts[-1].get("text"), tuple(expect_keys or ()))
        if obj is not None and validate is not None:
            err = validate(obj)
        if obj is None or err is not None:
            raise ResumeMismatch(f"{where}: the logged accepted reply no longer parses ({err})")
        return obj, attempts

    def reply(self, turn: int, stage: str, key: str,
              validate: Optional[Callable[[dict], Optional[str]]] = None,
              expect_keys: Iterable[str] = ()) -> Tuple[Optional[dict], List[Dict[str, Any]]]:
        """The logged result of one seat call (stage "proposal", "offer" or "reply").
        Raises LoggedFailure when the call failed in the run."""
        entry = self._actor(turn, key)
        where = f"turn {turn} {stage} {key}"
        if stage == "proposal":
            if entry.get("error") is not None and not entry.get("attempts"):
                raise LoggedFailure(entry["error"])
            return self._answer(entry.get("attempts") or [], validate, expect_keys, where)
        attempts = [{k: v for k, v in a.items() if k != "stage"}
                    for a in entry.get("message_attempts") or [] if a.get("stage") == stage]
        if not attempts:
            raise LoggedFailure(None)
        return self._answer(attempts, validate, expect_keys, where)

    def interviews(self, turn: int) -> Dict[str, Dict[str, Any]]:
        return copy.deepcopy(self.turns[turn].get("interviews") or {})

    def review(self, turn: int) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """(turn record's macro_jury, record["macro_jury"] entry) of a finished review,
        else None (no review logged, or it did not finish: it is asked live)."""
        t = self.turns[turn]
        if not review_done(t) or turn not in self.reviews:
            return None
        return copy.deepcopy(t["macro_jury"]), copy.deepcopy(self.reviews[turn])

    # -- check ---------------------------------------------------------------

    def check_logs(self, ledger: List[Dict[str, Any]], a2a: List[Dict[str, Any]]) -> None:
        """After the last replayed turn: the rebuilt hidden ledger and message log must
        equal the logged ones up to that turn (a turn in flight may have added more)."""
        for name, mine in (("ledger", ledger), ("a2a", a2a)):
            logged = [x for x in self.record.get(name) or [] if int(x.get("turn") or 0) <= self.last]
            if normalise(mine) != logged:
                raise ResumeMismatch(f"the {name} rebuilt from the log differs from the logged one "
                                     f"after turn {self.last}; the engine or config changed since the run")

    def check_turn(self, turn: int, rebuilt: Dict[str, Any]) -> None:
        """Raise ResumeMismatch unless the rebuilt turn record equals the logged one.
        Called before the turn's review; when the log holds a finished review, the
        keys it sets again are checked after it is replayed (check_reviewed)."""
        skip = _UNCHECKED_TURN_KEYS + (_AFTER_REVIEW_KEYS if review_done(self.turns[turn]) else ())
        logged = {k: v for k, v in self.turns[turn].items() if k not in skip}
        mine = {k: v for k, v in normalise(rebuilt).items() if k not in skip}
        if logged == mine:
            return
        keys = sorted(k for k in set(logged) | set(mine) if logged.get(k) != mine.get(k))
        if "actors" in keys:
            la, ma = logged.get("actors") or {}, mine.get("actors") or {}
            keys += [f"actors.{s}.{f}" for s in sorted(set(la) | set(ma))
                     for f in sorted(set(la.get(s) or {}) | set(ma.get(s) or {}))
                     if (la.get(s) or {}).get(f) != (ma.get(s) or {}).get(f)]
        raise ResumeMismatch(f"turn {turn} rebuilt from the log differs from the logged turn "
                             f"({', '.join(keys[:12])}); the engine or config changed since the run")

    def check_reviewed(self, turn: int, rebuilt: Dict[str, Any]) -> None:
        """After a replayed review: the keys it set again must equal the logged ones."""
        if not review_done(self.turns[turn]):
            return
        mine = normalise(rebuilt)
        keys = [k for k in _AFTER_REVIEW_KEYS if self.turns[turn].get(k) != mine.get(k)]
        if keys:
            raise ResumeMismatch(f"turn {turn} rebuilt from the log differs from the logged turn "
                                 f"after its review ({', '.join(keys)}); the engine or config "
                                 f"changed since the run")
