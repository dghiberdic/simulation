"""
The model-call journal: one JSON line per call to core.llm.complete, written the
moment the call returns or fails (flushed and fsynced: a crash, a kill or a
power loss right after a call keeps it), so a reply that does not parse still
leaves the raw text on disk.

A call goes to the journal of its run (<run_id>.calls.jsonl, registered by
core.logsetup.run_logging); a call with no registered run (rate_charters.py, a
tool) goes to data/logs/calls-<YYYYmmdd>.jsonl. Never rotated.

Lines:
  {"kind": "system", "sha": ..., "text": ...}   a system prompt, once per file
  {"kind": "call", "ts", "run_id", "purpose", "provider", "model", "served_model",
   "system_sha", "history", "user", "text", "thinking", "stop", "stop_detail",
   "input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "cost",
   "latency_s", "timeouts", "request_key", "error"}
  {"kind": "mark", "ts", "event", ...}          the run's progress: "attach" (a process
                                                took the run up; "replayable" calls),
                                                "stop" (why it stopped), and the
                                                checkpoints "start", "turn_done",
                                                "run_done", "done" ("fence": true)
  {"kind": "replayed", "ts", "purpose", "model", "request_key", "of_ts"}
                                                a call a resumed run answered from
                                                this journal (no provider call)
System prompts (charter included, tens of thousands of tokens) are stored once by
sha256 and referred to by "system_sha"; everything else is in full.

Replay cache (exact resume). register() loads, for the run, every successful
call journalled after the last checkpoint ("fence" mark): the calls of the
stage that was in flight when the run stopped (a network outage, the budget
guard, Ctrl-C, a crash). core.llm.complete asks take_replay() first: a request
with the same request_key (purpose, model, sha of the system prompt, sha of
history + user turn, max_tokens, effort, temperature) gets the journalled reply
back, identical keys consumed in journal order (a legitimately repeated
request gets the next journalled reply); the first request without a match
goes live. A failed call is never replayed. A checkpoint (mark(fence=True))
empties the cache: what came before it is in the saved record.
"""

import hashlib
import json
import logging
import os
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from core.logsetup import LOGS_DIR

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_runs: Dict[str, Path] = {}
_seen: Dict[Path, set] = {}
_replay: Dict[str, Dict[str, Deque[Dict[str, Any]]]] = {}


def register(run_id: str, path: Path) -> int:
    """Journal run_id's calls to path and load its replay cache (the successful calls
    journalled since the last checkpoint). Returns the number of replayable calls."""
    path = Path(path)
    cache = _load_replay(path)
    with _lock:
        _runs[run_id] = path
        _replay[run_id] = cache
    return sum(len(q) for q in cache.values())


def unregister(run_id: str) -> None:
    with _lock:
        _runs.pop(run_id, None)
        left = _replay.pop(run_id, None) or {}
    n = sum(len(q) for q in left.values())
    if n:
        logger.debug(f"[resume] {run_id}: {n} journalled call(s) were not asked again (not replayed)")


def default_path() -> Path:
    d = Path(os.environ.get("SIM_LOG_DIR") or LOGS_DIR)
    return d / f"calls-{datetime.now():%Y%m%d}.jsonl"


def path_for(run_id: Optional[str]) -> Path:
    with _lock:
        return _runs.get(run_id or "", None) or default_path()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def request_key(*, purpose: str, model: str, system: str, history: Optional[List[Dict[str, str]]],
                user: str, max_tokens: Any, effort: Any, temperature: Any) -> str:
    """The replay key of one request (the run id scopes the cache)."""
    convo = json.dumps([[m.get("role"), m.get("content")] for m in history or []] + [["user", user]],
                       ensure_ascii=False, default=str)
    body = json.dumps([purpose, model, _sha(system or ""), hashlib.sha256(convo.encode("utf-8", "replace"))
                       .hexdigest(), max_tokens, effort, temperature], default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


def _known_shas(path: Path) -> set:
    """The system prompts already in the file (a resumed run appends to it)."""
    seen = _seen.get(path)
    if seen is None:
        seen = set()
        if path.exists():
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"kind": "system"' in line[:40]:
                        try:
                            seen.add(json.loads(line)["sha"])
                        except (ValueError, KeyError):
                            pass
        _seen[path] = seen
    return seen


def _append(path: Path, lines: List[str], durable: bool = True) -> None:
    """Append lines, flushed and (durable) fsynced. Called with _lock held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
        f.flush()
        if durable:
            os.fsync(f.fileno())


def record(*, run_id: Optional[str], purpose: str, provider: str, model: str, system: str, user: str,
           history: Optional[List[Dict[str, str]]] = None, response: Any = None,
           error: Optional[BaseException] = None, latency_s: Optional[float] = None,
           request_key: Optional[str] = None) -> None:
    """Append one call. Never raises: the journal must not break a run. A paid call is
    fsynced before it returns (an offline stub's only flushed: it costs nothing)."""
    try:
        with _lock:
            registered = run_id in _runs if run_id else False
        if provider == "stub" and not registered:
            return      # offline stubs outside a logged run (unit tests): nothing to keep
        path = path_for(run_id)
        sha = _sha(system or "")
        entry: Dict[str, Any] = {
            "kind": "call", "ts": datetime.now().isoformat(timespec="milliseconds"),
            "run_id": run_id, "purpose": purpose, "provider": provider, "model": model,
            "system_sha": sha, "history": history or [], "user": user,
        }
        if response is not None:
            for k in ("served_model", "text", "thinking", "stop", "stop_detail", "raw_stop_reason",
                      "input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "cost",
                      "latency_s", "max_tokens", "temperature", "effort", "timeouts"):
                entry[k] = getattr(response, k, None)
        if error is not None:
            entry["error"] = f"{type(error).__name__}: {error}"
            entry["attempts"] = getattr(error, "attempts", None)
            if latency_s is not None:
                entry["latency_s"] = round(latency_s, 3)
        if request_key:
            entry["request_key"] = request_key
        with _lock:
            seen = _known_shas(path)
            lines = []
            if sha not in seen:
                lines.append(json.dumps({"kind": "system", "sha": sha, "text": system}, ensure_ascii=False))
                seen.add(sha)
            lines.append(json.dumps(entry, default=str, ensure_ascii=False))
            _append(path, lines, durable=provider != "stub")
    except Exception as e:      # noqa: BLE001
        logger.warning(f"[calls] could not journal a {purpose} call: {e}")


def mark(run_id: Optional[str], event: str, *, fence: bool = False, **data: Any) -> None:
    """A progress line in the run's journal (registered runs only). fence=True is a
    checkpoint: everything before it is in the saved record, so the replay cache is
    emptied. Never raises."""
    if not run_id:
        return
    try:
        with _lock:
            path = _runs.get(run_id)
            if path is None:
                return
            if fence:
                _replay[run_id] = {}
            entry = {"kind": "mark", "ts": datetime.now().isoformat(timespec="milliseconds"),
                     "run_id": run_id, "event": event, **({"fence": True} if fence else {}), **data}
            _append(path, [json.dumps(entry, default=str, ensure_ascii=False)])
    except Exception as e:      # noqa: BLE001
        logger.warning(f"[calls] could not journal the {event} mark: {e}")


def take_replay(run_id: Optional[str], key: str) -> Optional[Dict[str, Any]]:
    """The next journalled successful reply to this request in the run's replay cache
    (consumed), or None: the request goes live."""
    if not run_id:
        return None
    with _lock:
        q = (_replay.get(run_id) or {}).get(key)
        return q.popleft() if q else None


def note_replayed(run_id: Optional[str], hit: Dict[str, Any], key: str) -> None:
    """Journal that a call was answered from the journal (no provider call)."""
    try:
        with _lock:
            path = _runs.get(run_id or "")
            if path is None:
                return
            entry = {"kind": "replayed", "ts": datetime.now().isoformat(timespec="milliseconds"),
                     "run_id": run_id, "purpose": hit.get("purpose"), "model": hit.get("model"),
                     "request_key": key, "of_ts": hit.get("ts")}
            _append(path, [json.dumps(entry, default=str, ensure_ascii=False)],
                    durable=hit.get("provider") != "stub")
    except Exception as e:      # noqa: BLE001
        logger.warning(f"[calls] could not journal a replayed call: {e}")


def replayable(run_id: str) -> int:
    with _lock:
        return sum(len(q) for q in (_replay.get(run_id) or {}).values())


def _load_replay(path: Path) -> Dict[str, Deque[Dict[str, Any]]]:
    """The successful calls (with a request_key) after the journal's last checkpoint,
    by request key, in journal order. A torn last line (a kill mid-write) is skipped."""
    tail: List[Dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"kind": "mark"' in line[:40] and '"fence": true' in line:
                    tail = []
                    continue
                if '"kind": "call"' not in line[:40]:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("error") is None and obj.get("request_key"):
                    tail.append(obj)
    except OSError:
        return {}
    cache: Dict[str, Deque[Dict[str, Any]]] = {}
    for obj in tail:
        cache.setdefault(obj["request_key"], deque()).append(obj)
    return cache


def read_calls(path: Path) -> List[Dict[str, Any]]:
    """The call lines of a journal with their system prompt text restored."""
    systems: Dict[str, str] = {}
    calls: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            if obj.get("kind") == "system":
                systems[obj["sha"]] = obj["text"]
            elif obj.get("kind") == "call":
                obj["system"] = systems.get(obj.get("system_sha"))
                calls.append(obj)
    return calls


def read_lines(path: Path) -> List[Dict[str, Any]]:
    """Every journal line (calls, marks, replayed calls), in order."""
    out: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out
