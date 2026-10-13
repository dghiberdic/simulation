"""
Run-record files: writing them, finding them, and the questions every tool asks
of a record (is it finished, which run and cell, what kind of run).

write_json_atomic writes a JSON file through a temporary file in the same
directory and os.replace, so a kill or crash mid-write leaves either the old
file or the new one, never truncated JSON (a truncated <run_id>.json would read
as "no finished run", and the paid run would be run again).

The engine saves <run_id>.partial.json after every turn and <run_id>.json when
the run ends. A run is finished when its record holds final scores.

Run kinds (run_kind): "main" (a model run of main.py / campaign.py), "pilot"
(pilot.py), "policy" (a zero-cost scripted --policy run) and "dry" (offline
stub models). The main analyses read finished main runs only (§9.2, §9.3).
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

PathLike = Union[str, Path]

PARTIAL_SUFFIX = ".partial.json"
DRY_SUBDIR = "dry"                  # dry-run logs live under a dry/ directory
# JSON files written beside the run records that are not run records.
SIDE_FILE_PREFIXES = ("pilot_summary_", "disposition_ratings", "spend", "approval", "post_run_",
                      "campaign_state")

# §6.5: the kin cell is S2 under condition A, with this seat's model (Claude) in every seat.
KIN_SCENARIO, KIN_CONDITION, KIN_SEAT = "S2", "A", "anthropic"


# ---------------------------------------------------------------------------
# Reading and writing
# ---------------------------------------------------------------------------

def write_json_atomic(path: PathLike, obj: Any, indent: Optional[int] = 2) -> Path:
    """Write obj as JSON to path atomically (write-then-rename, flushed to disk)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f, indent=indent, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return path


def read_json(path: PathLike) -> Optional[Any]:
    """The JSON at path, or None when it is missing or unreadable."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_record(path: PathLike) -> Optional[Dict[str, Any]]:
    """The JSON object at path, or None when it is missing, unreadable or not an object."""
    rec = read_json(path)
    return rec if isinstance(rec, dict) else None


# ---------------------------------------------------------------------------
# Questions asked of a record
# ---------------------------------------------------------------------------

def is_partial_path(path: PathLike) -> bool:
    """A <run_id>.partial.json file (an unfinished run's per-turn save)."""
    return str(path).endswith(PARTIAL_SUFFIX)


def is_run_record(rec: Any) -> bool:
    """A run record (finished or partial): an object with a turns list and labs."""
    return isinstance(rec, dict) and isinstance(rec.get("turns"), list) and "labs" in rec


def is_finished(rec: Any) -> bool:
    """A finished run: its record holds final scores (a partial log does not)."""
    return bool(isinstance(rec, dict) and isinstance(rec.get("final"), dict)
                and rec["final"].get("scores"))


def grand_jury_usable(rec: Dict[str, Any]) -> bool:
    """The record holds a Grand Jury verdict (a juror named, no error)."""
    gj = (rec.get("final") or {}).get("grand_jury")
    return isinstance(gj, dict) and bool(gj.get("juror")) and not gj.get("error")


def run_meta(rec: Dict[str, Any]) -> Dict[str, Any]:
    return (rec.get("config") or {}).get("run_meta") or {}


def run_id_of(rec: Dict[str, Any]) -> Optional[str]:
    """The run id: the record's, else run_meta's, else config's (None if none)."""
    cfg = rec.get("config") or {}
    return rec.get("run_id") or (cfg.get("run_meta") or {}).get("run_id") or cfg.get("run_id")


def cell_name(scenario: str, condition: str, kin: bool = False) -> str:
    """A main-run cell's name: "S1-A", or "kin-S2-A" for the kin cell (§9.3)."""
    return ("kin-" if kin else "") + f"{scenario}-{condition}"


def cell_of(rec: Dict[str, Any]) -> str:
    """The run's cell label: "S1-A", "kin-S2-A", or the pilot test's name."""
    cfg = rec.get("config") or {}
    meta = cfg.get("run_meta") or {}
    if meta.get("cell"):
        return str(meta["cell"])
    if meta.get("pilot") and meta.get("test"):
        return f"pilot {meta['test']}"
    return cell_name(cfg.get("scenario") or "?", cfg.get("condition") or "?", bool(meta.get("kin")))


def is_dry(rec: Dict[str, Any]) -> bool:
    return bool((rec.get("config") or {}).get("dry_run"))


def is_pilot(rec: Dict[str, Any]) -> bool:
    return bool(run_meta(rec).get("pilot"))


def is_main_run(rec: Dict[str, Any]) -> bool:
    """A main run (main.py / campaign.py: run_meta.pilot is False). Untagged logs are not."""
    meta = run_meta(rec)
    return meta.get("pilot") is False or meta.get("entry") == "main.py"


def is_scripted_run(rec: Dict[str, Any]) -> bool:
    """A run played by a zero-cost scripted --policy (no models): config.policy or
    run_meta.policy."""
    cfg = rec.get("config") or {}
    return bool(cfg.get("policy") or (cfg.get("run_meta") or {}).get("policy"))


def run_kind(rec: Dict[str, Any]) -> str:
    """"pilot", "policy" (scripted), "dry" (stub models) or "main" (a model run of
    main.py / campaign.py); from run_meta (pilot, policy, dry_run) and config."""
    if is_pilot(rec):
        return "pilot"
    if is_scripted_run(rec):
        return "policy"
    if is_dry(rec) or run_meta(rec).get("dry_run"):
        return "dry"
    return "main"


# ---------------------------------------------------------------------------
# Finding and loading run records
# ---------------------------------------------------------------------------

def expand_paths(paths: Sequence[PathLike], recursive: bool = False, include_dry: bool = False) -> List[str]:
    """
    Directories become their *.json files (sorted); files pass through. With
    `recursive` a directory is searched at any depth and its dry/ subdirectories
    are skipped unless include_dry.
    """
    out: List[str] = []
    for path in paths:
        p = Path(path)
        if not p.is_dir():
            out.append(str(path))
        elif recursive:
            out += sorted(str(x) for x in p.rglob("*.json")
                          if include_dry or DRY_SUBDIR not in x.relative_to(p).parts[:-1])
        else:
            out += sorted(str(x) for x in p.glob("*.json"))
    return out


def load_records(paths: Sequence[PathLike], include_dry: bool = False, recursive: bool = False
                 ) -> List[Tuple[str, Dict[str, Any]]]:
    """
    [(path, record)] for the run records under `paths` (files or directories),
    skipping side files (summaries, approvals, ratings, the ledger, campaign
    state) and dry-run records unless include_dry. A <id>.partial.json is used
    only when no final <id>.json was read. Each record gets "run_id" (its own,
    else the file name's stem). Unreadable or non-run files are reported on
    stderr and skipped.
    """
    finals: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    partials: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    order: List[str] = []
    for path in expand_paths(paths, recursive, include_dry):
        p = Path(path)
        if p.name.startswith(SIDE_FILE_PREFIXES):
            continue
        try:
            with open(p) as f:
                rec = json.load(f)
        except (OSError, ValueError) as exc:
            print(f"[skip] {path}: {exc}", file=sys.stderr)
            continue
        if not is_run_record(rec):
            print(f"[skip] {path}: not a run record", file=sys.stderr)
            continue
        if is_dry(rec) and not include_dry:
            print(f"[skip] {path}: dry-run record (pass --include-dry to read it)", file=sys.stderr)
            continue
        partial = is_partial_path(p)
        stem = p.name[:-len(PARTIAL_SUFFIX)] if partial else p.stem
        key = str(p.with_name(stem))
        rec.setdefault("run_id", run_id_of(rec) or stem)
        (partials if partial else finals)[key] = (path, rec)
        if key not in order:
            order.append(key)
    return [finals.get(key) or partials[key] for key in order]


def finished_records(paths: Sequence[PathLike], include_dry: bool = False, recursive: bool = False
                     ) -> List[Tuple[str, Dict[str, Any]]]:
    """load_records, keeping only finished runs (final records with final scores)."""
    return [(path, rec) for path, rec in load_records(paths, include_dry, recursive)
            if not is_partial_path(path) and is_finished(rec)]
