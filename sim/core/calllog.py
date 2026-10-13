"""
The model-call journal: one JSON line per call to core.llm.complete, written the
moment the call returns or fails, so a crash, a kill or a reply that does not
parse still leaves the raw text on disk.

A call goes to the journal of its run (<run_id>.calls.jsonl, registered by
core.logsetup.run_logging); a call with no registered run (rate_charters.py, a
post-run jury, a tool) goes to data/logs/calls-<YYYYmmdd>.jsonl. Never rotated.

Lines:
  {"kind": "system", "sha": ..., "text": ...}   a system prompt, once per file
  {"kind": "call", "ts", "run_id", "purpose", "provider", "model", "served_model",
   "system_sha", "history", "user", "text", "thinking", "stop", "stop_detail",
   "input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "cost",
   "latency_s", "timeouts", "error"}
System prompts (charter included, tens of thousands of tokens) are stored once by
sha256 and referred to by "system_sha"; everything else is in full.
"""

import hashlib
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logsetup import LOGS_DIR

_lock = threading.Lock()
_runs: Dict[str, Path] = {}
_seen: Dict[Path, set] = {}


def register(run_id: str, path: Path) -> None:
    with _lock:
        _runs[run_id] = Path(path)


def unregister(run_id: str) -> None:
    with _lock:
        _runs.pop(run_id, None)


def default_path() -> Path:
    d = Path(os.environ.get("SIM_LOG_DIR") or LOGS_DIR)
    return d / f"calls-{datetime.now():%Y%m%d}.jsonl"


def path_for(run_id: Optional[str]) -> Path:
    with _lock:
        return _runs.get(run_id or "", None) or default_path()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


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


def record(*, run_id: Optional[str], purpose: str, provider: str, model: str, system: str, user: str,
           history: Optional[List[Dict[str, str]]] = None, response: Any = None,
           error: Optional[BaseException] = None, latency_s: Optional[float] = None) -> None:
    """Append one call. Never raises: the journal must not break a run."""
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
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            seen = _known_shas(path)
            with open(path, "a", encoding="utf-8") as f:
                if sha not in seen:
                    f.write(json.dumps({"kind": "system", "sha": sha, "text": system},
                                       ensure_ascii=False) + "\n")
                    seen.add(sha)
                f.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
                f.flush()
    except Exception as e:      # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(f"[calls] could not journal a {purpose} call: {e}")


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
