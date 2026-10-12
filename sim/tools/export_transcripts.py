#!/usr/bin/env python3
"""
Redacted transcripts and blank score sheets for the human steps (spec §2, §7,
§9.2 T2 and T8; DOC 1).

For each run log given, writes the texts the jurors see, so that we rate from
the same redacted material:

  <out>/blind/<run_id>/grand_jury.txt     the Grand Jury transcript (§7): seats
                                          as letters, names, charter phrasing and
                                          chosen numbers masked, interviews and
                                          condition text left out
  <out>/blind/<run_id>/T2_seat_<X>.txt    each model seat's own record for the
                                          T2 disposition rating (§2), seat X

and, apart, the material that un-blinds them (keep it away from the raters
until the ratings are in):

  <out>/key/letters.csv          run_id, letter, lab, model: the letter -> lab key
  <out>/key/hand_scores.csv      blank T8 template, one row per model seat-run:
                                 run_id, lab, the five rubric items, composite
                                 (tools/jury_check.py --hand), plus the letter
  <out>/key/human_ratings.csv    blank T2 template: run_id, lab, disposition
                                 (tools/disposition.py --human), plus the letter

Scripted seats (T5's xAI) are left out of the T2 transcripts and templates.
Existing templates are never overwritten (they may hold our scores) unless
--force. The T1 runs (priced and T6) are the ones we score (§9.2).

CLI:
  python tools/export_transcripts.py data/pilot/priced data/pilot/T6 [--out data/pilot/human]
                                     [--include-dry] [--force]
"""

import argparse
import csv
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.costs import scripted_seats                                     # noqa: E402
from core.transcript import disposition_transcript, full_transcript, seat_letters   # noqa: E402
from tools.summarize_run import load_logs, run_id_of                       # noqa: E402

RUBRIC_ITEMS = ("honesty", "rule_keeping", "restraint", "fair_dealing", "outside_welfare")   # §7
HAND_COLUMNS = ("run_id", "lab") + RUBRIC_ITEMS + ("composite", "letter")
HUMAN_COLUMNS = ("run_id", "lab", "disposition", "letter")
KEY_COLUMNS = ("run_id", "letter", "lab", "model")


def model_seats(record: Dict[str, Any]) -> List[str]:
    return [k for k in (record.get("labs") or {}) if k not in scripted_seats(record)]


def export_run(record: Dict[str, Any], blind_dir: Path) -> Tuple[str, List[Dict[str, str]]]:
    """Write one run's redacted transcripts; return (run_id, key rows)."""
    rid = run_id_of(record) or "run"
    letters = seat_letters(record)
    d = blind_dir / rid
    d.mkdir(parents=True, exist_ok=True)
    (d / "grand_jury.txt").write_text(str(full_transcript(record)) + "\n", encoding="utf-8")
    rows = []
    for key in model_seats(record):
        letter = letters.get(key, "?")
        (d / f"T2_seat_{letter}.txt").write_text(str(disposition_transcript(record, key)) + "\n",
                                                 encoding="utf-8")
        rows.append({"run_id": rid, "letter": letter, "lab": key,
                     "model": ((record.get("labs") or {}).get(key) or {}).get("model") or ""})
    return rid, sorted(rows, key=lambda r: r["letter"])


def _write_csv(path: Path, columns: Tuple[str, ...], rows: List[Dict[str, Any]], force: bool) -> bool:
    if path.exists() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(columns))
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in columns})
    return True


def export(records: List[Dict[str, Any]], out: Path, force: bool = False) -> Dict[str, Any]:
    """Transcripts for every record, the key file and the two templates."""
    key_rows: List[Dict[str, str]] = []
    runs = []
    for rec in records:
        rid, rows = export_run(rec, out / "blind")
        runs.append(rid)
        key_rows += rows
    key_dir = out / "key"
    _write_csv(key_dir / "letters.csv", KEY_COLUMNS, key_rows, force=True)    # always current
    blank = [{"run_id": r["run_id"], "lab": r["lab"], "letter": r["letter"]} for r in key_rows]
    wrote = {"hand_scores.csv": _write_csv(key_dir / "hand_scores.csv", HAND_COLUMNS, blank, force),
             "human_ratings.csv": _write_csv(key_dir / "human_ratings.csv", HUMAN_COLUMNS, blank, force)}
    return {"runs": runs, "seat_runs": len(key_rows), "blind": str(out / "blind"), "key": str(key_dir),
            "templates_written": wrote}


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Redacted transcripts, the letter key and blank score sheets "
                                            "for the human steps (T2, T8).")
    p.add_argument("logs", nargs="+", help="run logs or directories (searched recursively)")
    p.add_argument("--out", default=None, help="output directory (default: <first log dir>/human)")
    p.add_argument("--include-dry", action="store_true", help="read dry-run records too")
    p.add_argument("--force", action="store_true", help="overwrite existing hand_scores / human_ratings templates")
    args = p.parse_args(argv)
    loaded, _ = load_logs(args.logs, include_dry=args.include_dry, recursive=True)
    records = [r for path, r in loaded if not str(path).endswith(".partial.json")
               and (r.get("final") or {}).get("scores")]
    if not records:
        print("no finished run records (dry-run records need --include-dry)")
        return 1
    first = Path(args.logs[0])
    out = Path(args.out) if args.out else (first if first.is_dir() else first.parent) / "human"
    res = export(records, out, args.force)
    print(f"{len(res['runs'])} run(s), {res['seat_runs']} model seat-run(s): redacted transcripts in "
          f"{res['blind']}; letter key and templates in {res['key']} (keep the key from the raters)")
    for name, wrote in res["templates_written"].items():
        if not wrote:
            print(f"  {name} exists and was kept (it may hold our scores); --force overwrites it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
