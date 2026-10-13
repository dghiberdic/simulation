#!/usr/bin/env python3
"""
Residual identifiability of the blinded juror views (spec §2: "residual
identifiability is reported"; §7).

An attacker holding only public information (the §4 seed table, i.e. the seeds
in config/labs; the five charters; the names in core/names.py) tries to tell
which lab sits behind each seat letter of a run, in four views:

  grand_jury.ledger    the first public ledger of the Grand Jury record, matched
                       to the seed table (compute, capital, influence, talent,
                       capability; relative distance)
  grand_jury.charters  the Grand Jury's charter block, matched to the charters by
                       length and distinctive vocabulary
  grand_jury.text      each seat's own lines in the Grand Jury record (rationale,
                       actions, accounts, reports, statements)
  t2.text              each model seat's T2 record (core.transcript.disposition_transcript)

The text views are scored by the lab vocabulary that survives redaction: every
name of core.names (lab, actor, products, people, charter titles, charter terms;
weight 3) and the word pairs found only in one lab's charter (at least three
times; weight 1). Within a view the seats are assigned to labs jointly (the
best permutation). A seat counts as identified when its assignment is right and
rests on evidence (a positive score for the assigned lab); with no evidence a
guess is right one time in five at most, and it is not counted.

Output per run and pooled: seats, identified, rate (identified ÷ seats).

CLI:
  python tools/identifiability.py <log.json|dir> [...] [--include-dry] [--json]
"""

import argparse
import itertools
import json
import math
import re
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_charter, load_lab_configs                        # noqa: E402
from core.costs import scripted_seats                                          # noqa: E402
from core.names import CATEGORIES, LAB_NAMES                                   # noqa: E402
from core.transcript import charters_block, disposition_transcript, full_transcript  # noqa: E402

VIEWS = ("grand_jury.ledger", "grand_jury.charters", "grand_jury.text", "t2.text")
LEDGER_FIELDS = ("capability", "compute", "capital", "influence", "talent")
NAME_WEIGHT = 3
PAIR_MIN_COUNT = 3
_STOP = frozenset("the of and to a in that is for or be it as with on by this an are its we our "
                  "not can may should will if at from which their they has have more any these "
                  "such other than also but all into when".split())

_LEDGER_RE = re.compile(r"Public ledger at month end:\n((?:    Seat [A-Z]: [^\n]*\n?)+)")
_ROW_RE = re.compile(r"    Seat ([A-Z]): ([^;\n]*)")
_FIG_RE = re.compile(r"(capability|compute|capital|influence|talent) (-?[\d.]+)")
_CHARTER_RE = re.compile(r"=== Seat ([A-Z]) charter ===\n(.*?)(?=\n\n=== Seat [A-Z] charter ===|\Z)",
                         re.S)
_WORD_RE = re.compile(r"[a-z][a-z0-9'-]+")


# ---------------------------------------------------------------------------
# Public information
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _public() -> Tuple[Dict[str, Dict[str, float]], Dict[str, str]]:
    """({lab key: seed figures}, {lab key: charter text}) from config."""
    seeds, charters = {}, {}
    for cfg in load_lab_configs():
        key = cfg.get("key")
        if not key:
            continue
        seeds[key] = {f: float(v) for f, v in (cfg.get("seed") or {}).items()
                      if isinstance(v, (int, float))}
        try:
            charters[key] = load_charter(cfg)
        except (OSError, KeyError, ValueError):
            charters[key] = ""
    return seeds, charters


def _pairs(text: str) -> Counter:
    words = _WORD_RE.findall(text.lower())
    return Counter(f"{a} {b}" for a, b in zip(words, words[1:])
                   if a not in _STOP and b not in _STOP)


@lru_cache(maxsize=1)
def charter_pairs() -> Dict[str, frozenset]:
    """Word pairs found at least PAIR_MIN_COUNT times in one charter and in no other."""
    counts = {k: _pairs(t) for k, t in _public()[1].items()}
    out = {}
    for key, c in counts.items():
        others = Counter()
        for k2, c2 in counts.items():
            if k2 != key:
                others.update(c2)
        out[key] = frozenset(p for p, n in c.items() if n >= PAIR_MIN_COUNT and not others[p])
    return out


@lru_cache(maxsize=None)
def _name_regex(key: str) -> Optional[re.Pattern]:
    terms = [t for c in CATEGORIES for t in LAB_NAMES.get(key, {}).get(c, ())]
    if not terms:
        return None
    alts = "|".join(r"[\s-]*".join(map(re.escape, t.split())) for t in
                    sorted(terms, key=len, reverse=True))
    return re.compile(r"(?<![\w])(?:" + alts + r")(?![\w])", re.IGNORECASE)


def text_scores(text: str, keys: Sequence[str]) -> Dict[str, float]:
    """{lab key: evidence in `text`}: surviving names ×NAME_WEIGHT + charter word pairs."""
    pairs = _pairs(text)
    out = {}
    for key in keys:
        rx = _name_regex(key)
        names = len(rx.findall(text)) if rx else 0
        out[key] = NAME_WEIGHT * names + sum(pairs[p] for p in charter_pairs().get(key, ()) if p in pairs)
    return out


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def best_assignment(scores: Dict[str, Dict[str, float]], keys: Sequence[str]) -> Dict[str, str]:
    """{letter: lab key} maximising the summed score (ties: the first permutation)."""
    letters = sorted(scores)
    best, best_total = {}, -math.inf
    for perm in itertools.permutations(keys, len(letters)):
        total = sum(scores[L].get(k, -math.inf) for L, k in zip(letters, perm))
        if total > best_total:
            best, best_total = dict(zip(letters, perm)), total
    return best


def _tally(scores: Dict[str, Dict[str, float]], keys: Sequence[str], truth: Dict[str, str],
           evidence: bool = True) -> Dict[str, Any]:
    """Assign, then count seats identified: right, and (if `evidence`) with a positive
    score once each lab's lowest score over the seats is subtracted (text every seat
    shares, such as the strategy brief, is no evidence)."""
    if evidence and scores:
        floor = {k: min(s.get(k, 0) for s in scores.values()) for k in keys}
        scores = {L: {k: s.get(k, 0) - floor[k] for k in keys} for L, s in scores.items()}
    guess = best_assignment(scores, keys) if scores else {}
    hit = sum(1 for L, k in guess.items()
              if truth.get(L) == k and (not evidence or scores[L].get(k, 0) > 0))
    n = len(scores)
    return {"seats": n, "identified": hit, "rate": round(hit / n, 4) if n else None}


def ledger_view(gj_text: str, keys: Sequence[str], truth: Dict[str, str]) -> Optional[Dict[str, Any]]:
    m = _LEDGER_RE.search(gj_text)
    if not m:
        return None
    seeds = _public()[0]
    scores: Dict[str, Dict[str, float]] = {}
    for letter, figs in _ROW_RE.findall(m.group(1)):
        obs = {f: float(v) for f, v in _FIG_RE.findall(figs)}
        scores[letter] = {k: -sum(abs(obs[f] - seeds[k][f]) / max(abs(seeds[k][f]), 1.0)
                                  for f in LEDGER_FIELDS if f in obs and f in seeds.get(k, {}))
                          for k in keys if k in seeds}
    return _tally(scores, keys, truth, evidence=False)


def charter_view(block: str, keys: Sequence[str], truth: Dict[str, str]) -> Optional[Dict[str, Any]]:
    texts = dict(_CHARTER_RE.findall(block or ""))
    if not texts:
        return None
    charters = _public()[1]
    scores = {}
    for letter, text in texts.items():
        vocab = text_scores(text, keys)
        scores[letter] = {k: vocab[k] - 100 * abs(math.log(max(len(text), 1) / max(len(charters.get(k) or ""), 1)))
                          for k in keys}
    return _tally(scores, keys, truth, evidence=False)


def _seat_blocks(gj_text: str) -> Dict[str, str]:
    """Each seat's own lines in the Grand Jury record ("  Seat X:" and its indented lines)."""
    out: Dict[str, List[str]] = {}
    current = None
    for line in gj_text.splitlines():
        head = re.fullmatch(r"  Seat ([A-Z]):", line)
        if head:
            current = head.group(1)
            out.setdefault(current, [])
        elif current and line.startswith("    "):
            out[current].append(line)
        else:
            current = None
    return {L: "\n".join(lines) for L, lines in out.items()}


def run_identifiability(record: Dict[str, Any]) -> Dict[str, Any]:
    """{view: {"seats", "identified", "rate"}} for one run record."""
    from core.jury import record_charters
    keys = list(record.get("labs") or {})
    gj = full_transcript(record)
    letters = dict(gj.letters)
    truth = {L: k for k, L in letters.items()}
    out: Dict[str, Any] = {}
    out["grand_jury.ledger"] = ledger_view(str(gj), keys, truth)
    out["grand_jury.charters"] = charter_view(
        charters_block(record, record_charters(record), letters), keys, truth)
    blocks = {L: t for L, t in _seat_blocks(str(gj)).items() if L in truth}
    out["grand_jury.text"] = (_tally({L: text_scores(t, keys) for L, t in blocks.items()}, keys, truth)
                              if blocks else None)
    scripted = set(scripted_seats(record))
    t2 = {letters[k]: text_scores(str(disposition_transcript(record, k)), keys)
          for k in keys if k not in scripted}
    out["t2.text"] = _tally(t2, keys, truth) if t2 else None
    return out


def pooled(per_run: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    out = {}
    for view in VIEWS:
        rows = [r[view] for r in per_run.values() if r.get(view)]
        n = sum(r["seats"] for r in rows)
        hit = sum(r["identified"] for r in rows)
        out[view] = {"runs": len(rows), "seats": n, "identified": hit,
                     "rate": round(hit / n, 4) if n else None}
    return out


def identifiability(records: Sequence[Tuple[str, Dict[str, Any]]]) -> Dict[str, Any]:
    from tools.summarize_run import run_id_of
    per_run = {}
    for path, rec in records:
        per_run[run_id_of(rec) or str(path)] = run_identifiability(rec)
    return {"runs": per_run, "pooled": pooled(per_run)}


def format_report(rep: Dict[str, Any]) -> str:
    lines = ["Residual identifiability (spec §2): seats matched to labs from public information",
             f"{'view':<22}{'runs':>6}{'seats':>7}{'identified':>12}{'rate':>8}"]
    for view, p in rep["pooled"].items():
        rate = "-" if p["rate"] is None else f"{p['rate']:.2f}"
        lines.append(f"{view:<22}{p['runs']:>6}{p['seats']:>7}{p['identified']:>12}{rate:>8}")
    lines.append("A seat counts when matched correctly; text views need evidence (guessing: at most 0.2).")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Residual identifiability of the juror views (spec §2).")
    p.add_argument("logs", nargs="+", help="run logs or directories (searched recursively)")
    p.add_argument("--include-dry", action="store_true", help="read dry-run records too")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    from tools.summarize_run import load_logs
    loaded, _ = load_logs(args.logs, include_dry=args.include_dry, recursive=True)
    records = [(path, rec) for path, rec in loaded
               if not str(path).endswith(".partial.json") and rec.get("turns")]
    if not records:
        print("no run records (dry-run records need --include-dry)")
        return 1
    rep = identifiability(records)
    print(json.dumps(rep, indent=2) if args.json else format_report(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
