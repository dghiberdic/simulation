#!/usr/bin/env python3
"""
Redacted transcripts and blank score sheets for the human steps (spec §2, §7,
§9.2 T2 and T8; DOC 1).

For each run log given, writes the texts the jurors see, so that we rate from
the same redacted material:

  <out>/grand_jury/<run_id>.txt          the Grand Jury transcript (§7): seats
                                          as letters, names, charter phrasing and
                                          chosen numbers masked, interviews and
                                          condition text left out. Apart from
                                          the T2 material: its public ledger
                                          matches the §4 seed table, so a T2
                                          rater who read it would know the seats
  <out>/blind/<run_id>/T2_seat_<X>.txt    each model seat's own record for the
                                          T2 disposition rating (§2), seat X
  <out>/blind/hand_scores.csv             blank T8 template, one row per model
                                          seat-run: run_id, letter, the five
                                          rubric items, composite
                                          (tools/jury_check.py --hand)
  <out>/blind/human_ratings.csv           blank T2 template: run_id, letter,
                                          disposition (tools/disposition.py --human)

The templates carry no lab column: the raters work by run id and seat letter
only, and both tools map the letters to labs from the run's seat letters. Apart,
the material that un-blinds them (keep it away from the raters until the
ratings are in):

  <out>/key/letters.csv          run_id, letter, lab, model, letter_salt: the letter
                                 -> lab key and the run's secret letter salt

The letters come from the run's secret salt (core.transcript.seat_letters). A
record that has none gets one now, and the record file is rewritten with it, so
every tool reads the same letters.

Scripted seats (T5's xAI) are left out of the T2 transcripts and templates.
Existing templates are never overwritten (they may hold our scores) unless
--force. The T1 runs (priced and T6) are the ones we score (§9.2).

CLI:
  python tools/export_transcripts.py data/pilot/priced data/pilot/T6 [--out data/pilot/human]
                                     [--include-dry] [--force]
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.costs import scripted_seats                                     # noqa: E402
from core.transcript import disposition_transcript, full_transcript, seat_letters   # noqa: E402
from core.records import finished_records, run_id_of                     # noqa: E402

RUBRIC_ITEMS = ("honesty", "rule_keeping", "restraint", "fair_dealing", "outside_welfare")   # §7
HAND_COLUMNS = ("run_id", "letter") + RUBRIC_ITEMS + ("composite",)
HUMAN_COLUMNS = ("run_id", "letter", "disposition")
KEY_COLUMNS = ("run_id", "letter", "lab", "model", "letter_salt")


def model_seats(record: Dict[str, Any]) -> List[str]:
    return [k for k in (record.get("labs") or {}) if k not in scripted_seats(record)]


def export_run(record: Dict[str, Any], blind_dir: Path,
               grand_jury_dir: Path) -> Tuple[str, List[Dict[str, str]]]:
    """Write one run's redacted transcripts: the T2 seat records under blind_dir and
    the Grand Jury transcript under grand_jury_dir; return (run_id, key rows)."""
    rid = run_id_of(record) or "run"
    letters = seat_letters(record)
    d = blind_dir / rid
    d.mkdir(parents=True, exist_ok=True)
    grand_jury_dir.mkdir(parents=True, exist_ok=True)
    (grand_jury_dir / f"{rid}.txt").write_text(str(full_transcript(record)) + "\n",
                                               encoding="utf-8")
    rows = []
    for key in model_seats(record):
        letter = letters.get(key, "?")
        (d / f"T2_seat_{letter}.txt").write_text(str(disposition_transcript(record, key)) + "\n",
                                                 encoding="utf-8")
        rows.append({"run_id": rid, "letter": letter, "lab": key,
                     "model": ((record.get("labs") or {}).get(key) or {}).get("model") or "",
                     "letter_salt": record.get("letter_salt") or ""})
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
        rid, rows = export_run(rec, out / "blind", out / "grand_jury")
        runs.append(rid)
        key_rows += rows
    key_dir = out / "key"
    _write_csv(key_dir / "letters.csv", KEY_COLUMNS, key_rows, force=True)    # always current
    blank = [{"run_id": r["run_id"], "letter": r["letter"]} for r in key_rows]   # no lab column
    blind = out / "blind"
    wrote = {"hand_scores.csv": _write_csv(blind / "hand_scores.csv", HAND_COLUMNS, blank, force),
             "human_ratings.csv": _write_csv(blind / "human_ratings.csv", HUMAN_COLUMNS, blank, force)}
    return {"runs": runs, "seat_runs": len(key_rows), "blind": str(blind),
            "grand_jury": str(out / "grand_jury"), "key": str(key_dir), "templates_written": wrote}


def _write_record(path: Path, record: Dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Redacted transcripts, the letter key and blank score sheets "
                                            "for the human steps (T2, T8).")
    p.add_argument("logs", nargs="+", help="run logs or directories (searched recursively)")
    p.add_argument("--out", default=None, help="output directory (default: <first log dir>/human)")
    p.add_argument("--include-dry", action="store_true", help="read dry-run records too")
    p.add_argument("--force", action="store_true", help="overwrite existing hand_scores / human_ratings templates")
    args = p.parse_args(argv)
    loaded = [(Path(path), r) for path, r in
              finished_records(args.logs, include_dry=args.include_dry, recursive=True)]
    records = [r for _path, r in loaded]
    unsalted = [(path, r) for path, r in loaded
                if not r.get("letter_salt") and not r.get("seat_letters")]
    if not records:
        print("no finished run records (dry-run records need --include-dry)")
        return 1
    first = Path(args.logs[0])
    out = Path(args.out) if args.out else (first if first.is_dir() else first.parent) / "human"
    res = export(records, out, args.force)
    for path, rec in unsalted:          # the letters just drawn must stay the run's letters
        _write_record(path, rec)
        print(f"  {path.name}: had no letter salt; one was drawn and saved in the record")
    print(f"{len(res['runs'])} run(s), {res['seat_runs']} model seat-run(s): T2 transcripts and "
          f"blank templates (run_id + letter) in {res['blind']}; Grand Jury transcripts in "
          f"{res['grand_jury']}; letter key in {res['key']} (keep the key and the Grand Jury "
          "transcripts from the T2 raters)")
    for name, wrote in res["templates_written"].items():
        if not wrote:
            print(f"  {name} exists and was kept (it may hold our scores); --force overwrites it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
