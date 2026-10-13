"""
Run-record files.

write_json_atomic writes a JSON file through a temporary file in the same
directory and os.replace, so a kill or crash mid-write leaves either the old
file or the new one, never truncated JSON (a truncated <run_id>.json would read
as "no finished run", and the paid run would be run again).
"""

import json
import os
from pathlib import Path
from typing import Any, Optional


def write_json_atomic(path: Path, obj: Any, indent: Optional[int] = 2) -> Path:
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


def read_json(path: Path) -> Optional[Any]:
    """The JSON at path, or None when it is missing or unreadable."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
