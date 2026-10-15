#!/usr/bin/env python3
"""
One run in one short, readable report: what each lab decided, how the scores
moved, and what was notable (forfeits, refusals, intrusions, reports, juries,
call trouble). Markdown, about one or two screens; --full adds the per-month
detail (rationales, statements, every message, every interview answer).

    ./sim report <run_id | path to .json | run dir> [--out FILE.md] [--unblind] [--full]
    python tools/run_report.py ...                          (the same)

Reads the run record (<run_id>.json, or <run_id>.partial.json for an unfinished
run) and, when they are beside it, the call journal (<run_id>.calls.jsonl:
cost, wall time, cache use, call errors) and the run log (<run_id>.log: effort
and temperature fallbacks, transient retries, unusable replies). Writes
<run_dir>/<run_id>.report.md (or --out) and prints the same text.

Blinding (spec §9.2 T2): a pilot run's dispositions (each seat's chosen
setting, the Grand Jury's perceived dispositions, the T2 ratings) stay out of
the report unless --unblind, and numbers next to "precedence", "setting" or
"disposition" in quoted seat text are masked. Main runs show them.

Never changes a record; a missing field reads "–".
"""

import argparse
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SIM = Path(__file__).resolve().parent.parent
if str(SIM) not in sys.path:
    sys.path.insert(0, str(SIM))

from core.console import describe_action  # noqa: E402
from core.fmt import f1, month_label, signed, value_text  # noqa: E402
from core.records import (PARTIAL_SUFFIX, SIDE_FILE_PREFIXES, cell_of, is_finished,  # noqa: E402
                          is_partial_path, is_pilot, is_run_record, read_record, run_id_of, run_kind)

DASH = "–"
SPARK = "▁▂▃▄▅▆▇█"
REPORT_SUFFIX = ".report.md"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _num(x: Any) -> Optional[float]:
    if isinstance(x, bool) or x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _fx(x: Any, digits: int = 1) -> str:
    v = _num(x)
    return DASH if v is None else f"{v:.{digits}f}"


def _sx(x: Any) -> str:
    return DASH if _num(x) is None else signed(x)


def _money(x: Any) -> str:
    v = _num(x)
    return DASH if v is None else f"${v:,.2f}"


def _clip(text: Any, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: max(0, n - 1)].rstrip() + "…"


def _cell(text: Any) -> str:
    """Safe inside a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def _table(header: Sequence[str], rows: Iterable[Sequence[Any]], align: Optional[str] = None) -> List[str]:
    """A Markdown table; align is one char per column ('l' or 'r')."""
    align = align or "l" * len(header)
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join("---:" if a == "r" else "---" for a in align) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_cell(c) for c in r) + " |")
    return out


def _ranges(nums: List[int]) -> str:
    """[1,2,3,5] -> "1–3, 5"."""
    nums = sorted(set(n for n in nums if isinstance(n, int)))
    parts, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        parts.append(str(nums[i]) if i == j else f"{nums[i]}–{nums[j]}")
        i = j + 1
    return ", ".join(parts)


def _ts(x: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(x))
    except (TypeError, ValueError):
        return None


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


# Seat text that can carry a pilot seat's chosen setting ("directive precedence is set to 100").
_DISP_NUMBER_RE = re.compile(
    r"(\b(?:precedence|disposition|setting)\b[^.\n\d]{0,40}?)\d{1,3}(?:\.\d+)?", re.I)
_DISP_NUMBER_BEFORE_RE = re.compile(
    r"\b\d{1,3}(?:\.\d+)?(\s*(?:/\s*100\s*|out of 100\s*)?(?:directive[- ])?(?:precedence|disposition)\b)", re.I)


class Ctx:
    """The record, its companions, and how to show it."""

    def __init__(self, rec: Dict[str, Any], *, path: Optional[Path] = None, unblind: bool = False,
                 full: bool = False, lines: Optional[List[Dict[str, Any]]] = None,
                 log_text: Optional[str] = None):
        self.rec = rec
        self.path = path
        self.full = full
        self.cfg = rec.get("config") or {}
        self.meta = self.cfg.get("run_meta") or {}
        self.labs: Dict[str, Dict[str, Any]] = {k: (v if isinstance(v, dict) else {})
                                                for k, v in (rec.get("labs") or {}).items()}
        self.turns: List[Dict[str, Any]] = [t for t in rec.get("turns") or [] if isinstance(t, dict)]
        self.final = rec.get("final") if isinstance(rec.get("final"), dict) else {}
        self.kind = run_kind(rec)
        self.blind = is_pilot(rec) and not unblind
        self.lines = lines or []
        self.calls = [c for c in self.lines if c.get("kind") == "call"]
        self.log_text = log_text or ""
        self.names = {k: (v.get("lab") or k) for k, v in self.labs.items()}
        for t in self.turns:                     # labs seen only in turns (old shapes)
            for k in (t.get("actors") or {}):
                self.names.setdefault(k, k)
        self.start = self.cfg.get("start") or (self.cfg.get("effective_cfg") or {}).get("start_date") \
            or ((self.cfg.get("initial_state") or {}).get("world") or {}).get("start") or "2026-08-01"

    def name(self, key: Any) -> str:
        return self.names.get(key, str(key)) if key is not None else DASH

    def text(self, s: Any, n: int) -> str:
        """Seat text, clipped, masked in a blinded pilot report."""
        s = str(s or "")
        if self.blind:
            s = _DISP_NUMBER_RE.sub(lambda m: m.group(1) + "[masked]", s)
            s = _DISP_NUMBER_BEFORE_RE.sub(lambda m: "[masked]" + m.group(1), s)
        return _clip(s, n)

    def month(self, turn: Any) -> str:
        try:
            return month_label(self.start, int(turn))
        except (TypeError, ValueError):
            return f"month {turn}"

    def order(self) -> List[str]:
        """Lab keys, best final rank first (record order when unranked)."""
        keys = list(self.names)
        ranks = {s.get("lab"): s.get("rank") for s in self.final_scores() if isinstance(s, dict)}
        return sorted(keys, key=lambda k: (ranks.get(k) is None, ranks.get(k) or 0, keys.index(k)))

    def final_scores(self) -> List[Dict[str, Any]]:
        s = (self.final or {}).get("scores")
        if isinstance(s, list) and s:
            return s
        return (self.turns[-1].get("scores") or []) if self.turns else []

    def event_turn(self) -> Optional[int]:
        et = self.cfg.get("event_turn") or (self.cfg.get("effective_cfg") or {}).get("event_turn")
        return int(et) if isinstance(et, int) else None


# ---------------------------------------------------------------------------
# Calls: turns, cost, wall time
# ---------------------------------------------------------------------------

def _call_turns(lines: List[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], Any]]:
    """[(call, turn)] with the turn read off the journal's checkpoints ("start",
    "turn_done", "run_done"): calls after the run's last turn are "post" (the Grand
    Jury, the T2 rating). A resumed run's checkpoints keep the count."""
    out, turn = [], 1
    for obj in lines:
        kind = obj.get("kind")
        if kind == "mark":
            ev = obj.get("event")
            if ev == "turn_done" and isinstance(obj.get("turn"), int):
                turn = obj["turn"] + 1
            elif ev == "run_done" or (ev == "attach" and obj.get("stage") == "post-run"):
                turn = "post"
        elif kind == "call":
            out.append((obj, turn))
    return out


def _cost_lines(ctx: Ctx) -> List[str]:
    if ctx.calls:
        total = sum(_num(c.get("cost")) or 0.0 for c in ctx.calls)
        by_model: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
        by_purpose: Dict[str, float] = defaultdict(float)
        n_by_model: Counter = Counter()
        for c in ctx.calls:
            m, p, v = str(c.get("model") or "?"), str(c.get("purpose") or "?"), _num(c.get("cost")) or 0.0
            by_model[m][p] += v
            by_purpose[p] += v
            n_by_model[m] += 1
        out = [f"- **Cost:** {_money(total)} over {len(ctx.calls)} calls (call journal) · by purpose: "
               + ", ".join(f"{p} {_money(v)}" for p, v in sorted(by_purpose.items(), key=lambda x: -x[1]))]
        parts = []
        for m, ps in sorted(by_model.items(), key=lambda x: -sum(x[1].values())):
            parts.append(f"{m} {_money(sum(ps.values()))} ({n_by_model[m]} calls)")
        out.append("  - by model: " + "; ".join(parts))
        stamps = [t for t in (_ts(c.get("ts")) for c in ctx.calls) if t]
        if stamps:
            out.append(f"- **Wall time:** {_duration((max(stamps) - min(stamps)).total_seconds())} "
                       f"(first call {min(stamps):%Y-%m-%d %H:%M}, last {max(stamps):%H:%M})")
        return out
    # No journal: what the record holds (seat calls and interviews).
    total, n = 0.0, 0
    for t in ctx.turns:
        for a in (t.get("actors") or {}).values():
            for at in (a or {}).get("attempts") or []:
                total += _num((at or {}).get("cost")) or 0.0
                n += 1
        for iv in (t.get("interviews") or {}).values():
            total += _num((iv or {}).get("cost")) or 0.0
    if n == 0:
        return [f"- **Cost:** {DASH} (no call journal; no model calls in the record)"]
    return [f"- **Cost:** {_money(total)} (seat and interview calls in the record; no call journal beside it)",
            f"- **Wall time:** {DASH}"]


def _served(ctx: Ctx, key: str) -> List[str]:
    seen: List[str] = []
    for t in ctx.turns:
        a = (t.get("actors") or {}).get(key) or {}
        for at in a.get("attempts") or []:
            s = (at or {}).get("served_model")
            if s and s not in seen:
                seen.append(s)
    return seen


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def header(ctx: Ctx) -> List[str]:
    rec, cfg, meta = ctx.rec, ctx.cfg, ctx.meta
    rid = run_id_of(rec) or (ctx.path.name.split(".")[0] if ctx.path else DASH)
    finished = is_finished(rec)
    n_turns, planned = len(ctx.turns), cfg.get("turns")
    state = "finished" if finished else f"PARTIAL (months 1–{n_turns} of {planned or '?'})" if n_turns \
        else f"PARTIAL (no month logged of {planned or '?'})"
    cell = cell_of(rec)
    bits = [f"scenario {cfg.get('scenario') or DASH}", f"condition {cfg.get('condition') or DASH}",
            f"fog {cfg.get('fog') or DASH}", f"messages {cfg.get('a2a_mode') or DASH}",
            "kin" if (cfg.get("kin") or meta.get("kin")) else "mixed panel"]
    rot = cfg.get("rotation") or meta.get("swap")
    seeds = f"seed {cfg.get('seed', meta.get('seed', DASH))}"
    if rot:
        a, _, b = str(rot).partition(":")
        seeds += f" · capability seeds swapped: {ctx.name(a)} ↔ {ctx.name(b)}" if b else f" · rotation {rot}"
    policy = cfg.get("policy") or meta.get("policy")
    out = [f"# Run report: {rid}", ""]
    out.append(f"- **Kind:** {ctx.kind}" + (f" (policy `{policy}`)" if policy else "")
               + f" · **cell** {cell} ({', '.join(bits)})")
    out.append(f"- **Turns:** {n_turns} logged of {planned or '?'} · event month {ctx.event_turn() or DASH} · "
               f"{seeds} · **{state}**")
    seats = []
    for k in ctx.order():
        lab = ctx.labs.get(k) or {}
        model = lab.get("model") or DASH
        served = [s for s in _served(ctx, k) if s != model]
        sp = (cfg.get("seat_policies") or {}).get(k)
        seats.append(f"{ctx.name(k)}: `{model}`" + (f" (served {', '.join(served)})" if served else "")
                     + (f" [scripted {sp}]" if sp else ""))
    out.append("- **Seats:** " + "; ".join(seats)
               + (f" — every seat scripted by `{policy}` (no model calls)" if policy else ""))
    out += _cost_lines(ctx)
    if ctx.blind:
        out.append("- **Blinded** (pilot, spec §9.2 T2): chosen, perceived and T2-rated dispositions are "
                   "hidden; numbers beside 'precedence/setting/disposition' in quoted seat text are masked. "
                   "`--unblind` shows them.")
    return out


def _seed_of(ctx: Ctx, key: str) -> Dict[str, Any]:
    lab = ((ctx.cfg.get("initial_state") or {}).get("labs") or {}).get(key) or {}
    seed = lab.get("seed") if isinstance(lab.get("seed"), dict) else {}
    if not seed and ctx.turns:
        seed = ((ctx.turns[0].get("opening_sheets") or {}).get(key) or {})
    return {"capability": seed.get("capability", lab.get("capability")),
            "influence": seed.get("influence", lab.get("influence"))}


def _last_sheet(ctx: Ctx, key: str) -> Dict[str, Any]:
    for t in reversed(ctx.turns):
        s = (t.get("public_sheets") or {}).get(key)
        if isinstance(s, dict):
            return s
    return {}


def _grand_jury(ctx: Ctx) -> Dict[str, Dict[str, Any]]:
    gj = (ctx.final or {}).get("grand_jury")
    if not isinstance(gj, dict):
        return {}
    return {k: v for k, v in (gj.get("actors") or {}).items() if isinstance(v, dict)}


def outcome(ctx: Ctx) -> List[str]:
    finished = is_finished(ctx.rec)
    out = ["", "## Outcome" + ("" if finished else f" (standing after month {len(ctx.turns)}; run unfinished)"), ""]
    scores = {s.get("lab"): s for s in ctx.final_scores() if isinstance(s, dict)}
    if not scores:
        return out + ["No scores logged yet."]
    show_disp = not ctx.blind
    disp = (ctx.final or {}).get("dispositions") or {}
    t2 = (ctx.final or {}).get("t2_ratings") or {}
    gj = _grand_jury(ctx)
    head = ["#", "Lab", "Prosperity", "ΔC", "ΔInfl", "C", "Compute", "Capital", "Influence"]
    align = "rlrrrrrrr"
    if gj and any(v.get("composite") is not None for v in gj.values()):
        head.append("GJ composite"); align += "r"
    if show_disp and disp:
        head.append("Disposition"); align += "r"
    if show_disp and gj:
        head.append("GJ perceived"); align += "r"
    if show_disp and t2:
        head.append("T2 rating"); align += "r"
    rows = []
    for k in ctx.order():
        s, sheet, seed = scores.get(k) or {}, _last_sheet(ctx, k), _seed_of(ctx, k)
        dc = (_num(sheet.get("capability")) - _num(seed["capability"])) \
            if _num(sheet.get("capability")) is not None and _num(seed["capability"]) is not None else None
        di = (_num(sheet.get("influence")) - _num(seed["influence"])) \
            if _num(sheet.get("influence")) is not None and _num(seed["influence"]) is not None else None
        dcp = ((ctx.final or {}).get("integrity_inputs") or {}).get("delta_c_pos", {}).get(k)
        if dc is not None and _num(dcp) is not None and abs(dc - _num(dcp)) < 0.2:
            dc = _num(dcp)                       # unrounded, when the record has it
        row = [s.get("rank", DASH), ctx.name(k), _fx(s.get("score"), 2), _sx(dc), _sx(di),
               _fx(sheet.get("capability")), _fx(sheet.get("compute")), _fx(sheet.get("capital")),
               _fx(sheet.get("influence"))]
        if "GJ composite" in head:
            row.append(_fx((gj.get(k) or {}).get("composite"), 2))
        if "Disposition" in head:
            row.append(DASH if disp.get(k) is None else value_text(disp.get(k)))
        if "GJ perceived" in head:
            row.append(_fx((gj.get(k) or {}).get("perceived_disposition"), 0))
        if "T2 rating" in head:
            r = t2.get(k)
            row.append(_fx(r.get("disposition"), 0) if isinstance(r, dict) else _fx(r, 0))
        rows.append(row)
    out += _table(head, rows, align)
    ups = (ctx.final or {}).get("ups_index") or ((ctx.turns[-1].get("ups_index") or {}) if ctx.turns else {})
    if isinstance(ups, dict) and ups:
        terms = ", ".join(f"{k} {_fx(v, 3)}" for k, v in ups.items() if k != "ups")
        hhi = (ctx.final or {}).get("capability_hhi")
        out += ["", f"**UPS index {_fx(ups.get('ups'), 4)}** — {terms}"
                + (f"; capability HHI {_fx(hhi, 3)}" if _num(hhi) is not None else "")]
    if ctx.blind and ((ctx.final or {}).get("t2_ratings") or (ctx.final or {}).get("dispositions")):
        out.append("\nDispositions and T2 ratings are in the record (hidden: blinded pilot run).")
    gjr = (ctx.final or {}).get("grand_jury")
    if isinstance(gjr, dict) and (gjr.get("error") or gjr.get("pending")):
        out.append(f"\nGrand Jury: {'pending' if gjr.get('pending') else 'error: ' + str(gjr.get('error'))}")
    elif finished and not gjr and ctx.kind not in ("policy", "pilot"):
        out.append("\nGrand Jury: none in the record.")
    return out


def trajectory(ctx: Ctx) -> List[str]:
    out = ["", "## Score trajectory", ""]
    if not ctx.turns:
        return out + ["No month logged."]
    nums = [t.get("turn", i + 1) for i, t in enumerate(ctx.turns)]
    per: Dict[str, List[Tuple[Optional[float], Any]]] = {k: [] for k in ctx.names}
    for t in ctx.turns:
        sc = {s.get("lab"): s for s in t.get("scores") or [] if isinstance(s, dict)}
        for k in per:
            s = sc.get(k) or {}
            per[k].append((_num(s.get("score")), s.get("rank")))
    vals = [v for series in per.values() for v, _ in series if v is not None]
    lo, hi = (min(vals), max(vals)) if vals else (0.0, 0.0)

    def spark(series):
        if hi == lo:
            return "".join(SPARK[0] if v is not None else " " for v, _ in series)
        return "".join(SPARK[min(7, int((v - lo) / (hi - lo) * 8))] if v is not None else " " for v, _ in series)

    head = ["Lab", "trend"] + [f"M{n}" for n in nums]
    rows = []
    for k in ctx.order():
        rows.append([ctx.name(k), f"`{spark(per[k])}`"]
                    + [f"{_fx(v, 2)} ({r})" if v is not None else DASH for v, r in per[k]])
    out += _table(head, rows, "ll" + "r" * len(nums))
    out.append("")
    out.append("Prosperity Score (rank) at the end of each month; trend on one scale for all labs "
               f"({_fx(lo, 2)} to {_fx(hi, 2)}).")
    return out


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

def _failure(a: Dict[str, Any]) -> Optional[str]:
    """"refusal: cyber", "call failed: …" or None."""
    atts = [x for x in a.get("attempts") or [] if isinstance(x, dict)]
    last = atts[-1] if atts else {}
    if a.get("error"):
        return f"call failed: {_clip(a['error'], 60)}"
    if a.get("forfeited"):
        stop = last.get("stop")
        detail = last.get("stop_detail")
        if stop == "refusal":
            return "refusal" + (f": {detail}" if detail else "")
        err = last.get("error")
        return f"forfeit ({_clip(err, 50)})" if err else "forfeit"
    return None


def _messages(ctx: Ctx) -> Dict[Tuple[int, str], List[Dict[str, Any]]]:
    """(turn, sender) -> messages, from the record's message log (both modes), else
    each seat's messages_sent."""
    out: Dict[Tuple[int, str], List[Dict[str, Any]]] = defaultdict(list)
    log = [m for m in ctx.rec.get("a2a") or [] if isinstance(m, dict)]
    if log:
        for m in log:
            out[(m.get("turn"), m.get("sender") or m.get("from"))].append(m)
        return out
    for t in ctx.turns:
        for k, a in (t.get("actors") or {}).items():
            for m in (a or {}).get("messages_sent") or []:
                if isinstance(m, dict):
                    out[(t.get("turn"), k)].append(dict(m, sender=k, turn=t.get("turn")))
    return out


def _recipients(ctx: Ctx, msgs: List[Dict[str, Any]]) -> str:
    seen: List[str] = []
    for m in msgs:
        to = m.get("to")
        if to == "all" or (isinstance(to, str) and to.lower() == "all"):
            r = "all"
        else:
            rs = m.get("recipients") or ([to] if to else [])
            r = ", ".join(ctx.name(x) for x in rs) if isinstance(rs, list) else str(rs)
        if r and r not in seen:
            seen.append(r)
    return "; ".join(seen) or "?"


def _month_entry(ctx: Ctx, t: Dict[str, Any], key: str, msgs: List[Dict[str, Any]]) -> Dict[str, Any]:
    a = (t.get("actors") or {}).get(key) or {}
    acts = [x for x in a.get("accepted") or [] if isinstance(x, dict)]
    labels, amounts = [], []
    for x in acts:
        full = describe_action(x, ctx.names)
        amt = next((_num(x.get(f)) for f in ("amount", "units", "compute", "capital", "influence")
                    if _num(x.get(f)) is not None), None)
        labels.append(full if amt is None else full.replace(" " + f1(amt), "", 1))
        amounts.append(amt)
    rej = []
    for r in a.get("rejected") or []:
        if isinstance(r, dict):
            act = r.get("action")
            kind = act.get("type") if isinstance(act, dict) else str(act or "?")
            rej.append(f"{kind} ({_clip(r.get('reason') or 'no reason', 50)})")
        else:
            rej.append(_clip(r, 50))
    report = None
    if a.get("report"):
        rp = a["report"]
        acc = rp.get("accused") if isinstance(rp, dict) else None
        report = f"report vs {ctx.name(acc)}" if acc else "report filed"
    elif a.get("report_rejected"):
        report = f"report refused ({_clip(a['report_rejected'], 40)})"
    asks = len(a.get("attempts") or []) if not a.get("scripted") else 0
    return {
        "turn": t.get("turn"), "fail": _failure(a), "labels": labels, "amounts": amounts,
        "rejected": rej, "report": report, "scripted": bool(a.get("scripted")),
        "n_msgs": len(msgs), "to": _recipients(ctx, msgs) if msgs else "",
        "statement": a.get("public_statement") or "", "asks": asks,
        "rationale": a.get("rationale") or "", "reported": a.get("reported") or [],
    }


def _group_key(e: Dict[str, Any]) -> Tuple:
    return (e["fail"], tuple(e["labels"]), tuple(e["rejected"]), e["report"], e["to"],
            bool(e["statement"]), e["asks"] > 1)


def _group_line(ctx: Ctx, group: List[Dict[str, Any]]) -> str:
    e0 = group[0]
    turns = [e["turn"] for e in group]
    when = f"M{turns[0]}" if len(group) == 1 else f"M{turns[0]}–{turns[-1]}"
    each = " each month" if len(group) > 1 else ""
    parts = []
    if e0["fail"]:
        parts.append(f"**{e0['fail'].upper() if e0['fail'].startswith('refusal') else e0['fail']}**"
                     " → forfeited, no actions")
    elif e0["labels"]:
        acts = []
        for i, lab in enumerate(e0["labels"]):
            vals = [e["amounts"][i] for e in group if i < len(e["amounts"]) and e["amounts"][i] is not None]
            if vals:
                lo, hi = min(vals), max(vals)
                lab += f" {f1(lo)}" if abs(hi - lo) < 0.05 else f" {f1(lo)}–{f1(hi)}"
            acts.append(lab)
        parts.append(", ".join(acts) + each)
    else:
        parts.append("no action" + each)
    if e0["rejected"]:
        parts.append("rejected: " + "; ".join(e0["rejected"]))
    if e0["asks"] > 1:
        parts.append(f"{e0['asks']} asks (re-asked)")
    if e0["report"]:
        parts.append(e0["report"])
    if e0["n_msgs"] or any(e["n_msgs"] for e in group):
        n = sum(e["n_msgs"] for e in group)
        parts.append(f"{n} msg → {e0['to']}")
    if e0["statement"]:
        same = all(e["statement"] == e0["statement"] for e in group)
        parts.append(f"statement “{ctx.text(e0['statement'], 70)}”" + ("" if same else " (first; varies)"))
    if e0["scripted"]:
        parts.append("scripted")
    return f"- {when}: " + " · ".join(parts)


def decisions(ctx: Ctx) -> List[str]:
    out = ["", "## Decisions by lab", ""]
    if not ctx.turns:
        return out + ["No month logged."]
    msgs = _messages(ctx)
    for k in ctx.order():
        entries = [_month_entry(ctx, t, k, msgs.get((t.get("turn"), k), [])) for t in ctx.turns
                   if k in (t.get("actors") or {})]
        if not entries:
            continue
        model = (ctx.labs.get(k) or {}).get("model")
        out.append(f"**{ctx.name(k)}**" + (f" (`{model}`)" if model else ""))
        groups: List[List[Dict[str, Any]]] = []
        for e in entries:
            if groups and _group_key(groups[-1][-1]) == _group_key(e) and groups[-1][-1]["turn"] == e["turn"] - 1:
                groups[-1].append(e)
            else:
                groups.append([e])
        out += [_group_line(ctx, g) for g in groups]
        out.append("")
    return out[:-1] if out[-1] == "" else out


# ---------------------------------------------------------------------------
# Key events
# ---------------------------------------------------------------------------

def _macro_reviews(ctx: Ctx) -> List[Dict[str, Any]]:
    try:
        from tools.summarize_run import _macro_section
        return _macro_section(ctx.rec).get("reviews") or []
    except Exception:  # noqa: BLE001 — an odd record never stops the report
        return []


def key_events(ctx: Ctx) -> List[str]:
    out = ["", "## Key events", ""]
    et = ctx.event_turn()
    ceil_turn = next((t for t in ctx.turns if _num((t.get("world") or {}).get("ceiling")) is not None), None)
    if ceil_turn:
        w = ceil_turn.get("world") or {}
        out.append(f"- **Scenario event** month {ceil_turn.get('turn')} ({ctx.month(ceil_turn.get('turn'))}): "
                   f"compute ceiling {_fx(w.get('ceiling'))}; intrusion window "
                   f"{'open' if w.get('intrusion_open') else 'closed'}.")
    elif et:
        out.append(f"- **Scenario event** due month {et}" + (" (not reached)" if len(ctx.turns) < et else
                                                              " (no ceiling in the record)") + ".")
    # Intrusions (the hidden ledger: ground truth).
    ledger = [e for e in ctx.rec.get("ledger") or [] if isinstance(e, dict)]
    if ledger:
        ok = [e for e in ledger if e.get("succeeded")]
        exposed = sum(1 for e in ok if e.get("exposed"))
        gains = sum(_num(v) or 0.0 for e in ledger for v in (e.get("gains") or {}).values())
        out.append(f"- **Intrusions:** {len(ledger)} attempt(s), {len(ok)} succeeded, {exposed} exposed, "
                   f"capability copied {_fx(gains, 2)}.")
        shown = ledger if ctx.full else ledger[:8]
        for e in shown:
            who = ", ".join(ctx.name(c) for c in e.get("committers") or [])
            tgt = ", ".join(ctx.name(c) for c in e.get("targets") or [])
            res = ("succeeded" + (f", exposed {', '.join(ctx.name(x) for x in e.get('exposed'))}"
                                  if e.get("exposed") else ", not exposed")) if e.get("succeeded") \
                else "failed (traced publicly)"
            g = {ctx.name(k): v for k, v in (e.get("gains") or {}).items() if _num(v)}
            out.append(f"  - M{e.get('turn')} {e.get('id') or ''}: {who} → {tgt}: {res}"
                       + (f"; gains {', '.join(f'{k} {_fx(v, 2)}' for k, v in g.items())}" if g else ""))
        if len(shown) < len(ledger):
            out.append(f"  - … {len(ledger) - len(shown)} more (`--full`)")
    else:
        out.append("- **Intrusions:** none.")
    leads = []
    for t in ctx.turns:
        for ld in (t.get("leads") if t.get("leads") is not None else (t.get("intrusion") or {}).get("leads")) or []:
            if isinstance(ld, dict):
                leads.append(ld)
    if leads:
        out.append(f"- **Leads/tips:** {len(leads)}: " + "; ".join(
            f"M{ld.get('turn')} {ctx.name(ld.get('recipient'))} told of {ctx.name(ld.get('named'))}"
            + (f" ({ld.get('entry_id')})" if ld.get("entry_id") else "") for ld in leads[:6])
            + (" …" if len(leads) > 6 else ""))
    wbs = []
    for t in ctx.turns:
        for o in ((t.get("whistleblow") or {}).get("outcomes") or []):
            if isinstance(o, dict):
                wbs.append(dict(o, turn=t.get("turn")))
    if wbs:
        out.append(f"- **Whistleblow reports:** {len(wbs)}: " + "; ".join(
            f"M{o['turn']} {ctx.name(o.get('reporter'))} → {ctx.name(o.get('accused'))}: "
            f"{o.get('result') or 'filed'}" + (f" ({_clip(o.get('reason'), 40)})" if o.get("reason") else "")
            + (f", influence {_sx(o.get('influence_change'))}" if isinstance(o.get("influence_change"), (int, float))
               else "") for o in wbs))
    else:
        out.append("- **Whistleblow reports:** none.")
    notices = [(t.get("turn"), n) for t in ctx.turns for n in t.get("public_notices") or []]
    if notices:
        shown = notices if ctx.full else notices[:5]
        out.append(f"- **Public notices:** {len(notices)}")
        out += [f"  - M{tn}: {_clip(n, 140)}" for tn, n in shown]
        if len(shown) < len(notices):
            out.append(f"  - … {len(notices) - len(shown)} more (`--full`)")
    iv = [(t.get("turn"), x) for t in ctx.turns for x in t.get("interventions") or []]
    if iv:
        out.append("- **Researcher interventions:** " + "; ".join(f"M{tn}: {_clip(x, 80)}" for tn, x in iv))
    # Rejected actions and forfeits.
    rej: Counter = Counter()
    forfeits: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
    for t in ctx.turns:
        for k, a in (t.get("actors") or {}).items():
            a = a or {}
            for r in a.get("rejected") or []:
                act = r.get("action") if isinstance(r, dict) else None
                kind = act.get("type") if isinstance(act, dict) else "?"
                reason = re.sub(r"[\d.]+", "N", str((r or {}).get("reason") or "?")) if isinstance(r, dict) else str(r)
                rej[(kind, reason)] += 1
            f = _failure(a)
            if f:
                forfeits[k].append((t.get("turn"), f))
    if rej:
        out.append("- **Rejected actions:** " + "; ".join(f"{k} — {r} ×{n}" for (k, r), n in rej.most_common(6)))
    if forfeits:
        out += ["", "Forfeits and refusals:", ""]
        rows = []
        for k, items in forfeits.items():
            reasons = Counter(r for _, r in items)
            rows.append([ctx.name(k), _ranges([tn for tn, _ in items]), len(items),
                         "; ".join(f"{r} ×{n}" for r, n in reasons.items())])
        out += _table(["Lab", "Months", "n", "Why"], rows, "llrl")
        out.append("")
    # MacroJury.
    reviews = _macro_reviews(ctx)
    for r in reviews:
        before, after = r.get("before") or {}, r.get("after") or {}
        vals = ", ".join(f"{k.split('_')[0]} {before.get(k, DASH)}→{v}" for k, v in after.items()) \
            if isinstance(after, dict) else DASH
        line = f"- **MacroJury** M{r.get('turn')}: {vals}"
        if r.get("values_kept"):
            line += " (review failed; values kept)"
        elif r.get("retried"):
            line += " (asked twice)"
        if r.get("mandate"):
            line += f"; mandate “{_clip(r['mandate'], 140 if ctx.full else 100)}”"
        elif isinstance(r.get("mandate_dropped"), dict):
            md = r["mandate_dropped"]
            line += f"; mandate dropped ({md.get('reason')}): “{_clip(md.get('text'), 80)}”"
        else:
            line += "; no mandate"
        out.append(line)
    if not reviews:
        out.append("- **MacroJury:** no review in the record.")
    return out


# ---------------------------------------------------------------------------
# Messages, interviews
# ---------------------------------------------------------------------------

_NOTABLE_RE = re.compile(r"\b(intru\w*|access|tenanc\w*|booking|exploit|leak|report\w*|whistle\w*|"
                         r"coordinat\w*|agree\w*|pact|together|jointly|split|share)\b", re.I)


def messages(ctx: Ctx) -> List[str]:
    out = ["", "## Messages", ""]
    msgs = _messages(ctx)
    if not msgs:
        return out + ["No messages."]
    nums = [t.get("turn") for t in ctx.turns]
    rows = []
    for k in ctx.order():
        counts = [len(msgs.get((n, k), [])) for n in nums]
        rows.append([ctx.name(k), sum(counts)] + [c or "·" for c in counts])
    out += _table(["Lab", "total"] + [f"M{n}" for n in nums], rows, "lr" + "r" * len(nums))
    every = sorted((m for ms in msgs.values() for m in ms), key=lambda m: (m.get("turn") or 0, m.get("id") or 0))
    dropped = [m for m in every if m.get("dropped") or m.get("truncated")]
    if dropped:
        out.append(f"\n{len(dropped)} message(s) dropped or truncated by the token budget.")
    if ctx.full:
        out += ["", "All messages (first 120 characters):", ""]
        for m in every:
            out.append(f"- M{m.get('turn')} {ctx.name(m.get('sender'))} → {_recipients(ctx, [m])}"
                       + (f" [{m.get('stage')}]" if m.get("stage") not in (None, "proposal") else "")
                       + f": “{ctx.text(m.get('text'), 120)}”")
        return out

    def score(m):
        private = 0 if (m.get("to") == "all") else 2
        return -(private + (1 if _NOTABLE_RE.search(str(m.get("text") or "")) else 0))
    picks, seen = [], set()
    for m in sorted(every, key=lambda m: (score(m), m.get("turn") or 0)):
        if m.get("sender") in seen:
            continue
        picks.append(m)
        seen.add(m.get("sender"))
        if len(picks) == 3:
            break
    out += ["", "Examples (private messages and talk of access, reports or coordination first):", ""]
    for m in sorted(picks, key=lambda m: m.get("turn") or 0):
        out.append(f"- M{m.get('turn')} {ctx.name(m.get('sender'))} → {_recipients(ctx, [m])}: "
                   f"“{ctx.text(m.get('text'), 120)}”")
    return out


def interviews(ctx: Ctx) -> List[str]:
    out = ["", "## Interviews (A.7)", ""]
    with_iv = [t for t in ctx.turns if isinstance(t.get("interviews"), dict) and t.get("interviews")]
    if not with_iv:
        return out + ["No interviews in this run."]
    if ctx.full:
        chosen = with_iv
    else:
        et, last = ctx.event_turn(), with_iv[-1].get("turn")
        chosen = [t for t in with_iv if t.get("turn") in (et, last)] or [with_iv[-1]]
    asked = _ranges([t.get("turn") for t in with_iv])
    out.append(f"Interviewed in month(s) {asked}; showing "
               + ("all." if ctx.full else "the event month and the last interviewed month (`--full` for all)."))
    for t in chosen:
        tag = " (event month)" if t.get("turn") == ctx.event_turn() else ""
        out += ["", f"**M{t.get('turn')}{tag}**"]
        for k in ctx.order():
            iv = (t.get("interviews") or {}).get(k)
            if not isinstance(iv, dict):
                continue
            if iv.get("skipped"):
                out.append(f"- {ctx.name(k)}: _skipped ({iv.get('reason') or 'no answer'})_")
            else:
                flags = [f for f in ("incomplete", "cut") if iv.get(f)]
                out.append(f"- {ctx.name(k)}: “{ctx.text(iv.get('answer'), 300 if ctx.full else 110)}”"
                           + (f" _({'+'.join(flags)})_" if flags else ""))
    return out


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------

_LOG_LINE_RE = re.compile(r"^\d{4}-\d\d-\d\d [\d:.]+ (DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(\S+) \[[^\]]*\] (.*)$")
_LOG_KINDS = (
    ("effort fallback", re.compile(r"effort '?\w+'? rejected|reasoning_effort .* rejected", re.I)),
    ("temperature fallback", re.compile(r"temperature rejected", re.I)),
    ("reasoning-summary fallback", re.compile(r"reasoning summaries rejected", re.I)),
    ("transient error (retried)", re.compile(r"transient error", re.I)),
    ("unusable reply", re.compile(r"unusable reply", re.I)),
    ("network", re.compile(r"\[network\]", re.I)),
    ("interrupted request", re.compile(r"request interrupted", re.I)),
    ("context window", re.compile(r"exceeds the context window", re.I)),
)


def _log_findings(text: str) -> Dict[str, List[str]]:
    found: Dict[str, List[str]] = defaultdict(list)
    for line in text.splitlines():
        m = _LOG_LINE_RE.match(line)
        if not m:
            continue
        level, _logger, msg = m.groups()
        for name, rx in _LOG_KINDS:
            if rx.search(msg):
                found[name].append(msg)
                break
        else:
            if level in ("WARNING", "ERROR", "CRITICAL"):
                found[f"other {level.lower()}"].append(msg)
    return found


def _caching_models(calls: List[Dict[str, Any]]) -> set:
    return {c.get("model") for c in calls if (_num(c.get("cached_tokens")) or 0) > 0}


def anomalies(ctx: Ctx) -> List[str]:
    out = ["", "## Anomalies and flags", ""]
    flags: List[str] = []
    # Forfeits, refusals, call errors, re-asks, empty replies, idle seats.
    idle: Dict[str, List[int]] = defaultdict(list)
    refusals: Counter = Counter()
    for t in ctx.turns:
        tn = t.get("turn")
        acted = {}
        for k, a in (t.get("actors") or {}).items():
            a = a or {}
            f = _failure(a)
            if f:
                refusals[(k, f)] += 1
            atts = [x for x in a.get("attempts") or [] if isinstance(x, dict)]
            if len(atts) > 1:
                errs = "; ".join(sorted({str(x.get("error")) for x in atts if x.get("error")}))
                flags.append(f"M{tn} {ctx.name(k)}: {len(atts)} asks (JSON re-ask)" + (f": {_clip(errs, 80)}" if errs else ""))
            for x in atts:
                if not (x.get("text") or "").strip() and not x.get("error") and (x.get("stop") or "") != "refusal":
                    flags.append(f"M{tn} {ctx.name(k)}: empty reply (stop {x.get('stop') or DASH})")
            if not a.get("accepted"):
                idle[k].append(tn)
            else:
                acted[k] = tuple(sorted({str(x.get("type")) for x in a.get("accepted") if isinstance(x, dict)}))
            if a.get("rejected"):
                for r in a["rejected"]:
                    act = (r or {}).get("action") if isinstance(r, dict) else None
                    flags.append(f"M{tn} {ctx.name(k)}: rejected {act.get('type') if isinstance(act, dict) else '?'}"
                                 f" — {_clip((r or {}).get('reason') if isinstance(r, dict) else r, 70)}")
        if len(acted) >= 3 and len(set(acted.values())) == 1 and len(acted) == len(t.get("actors") or {}) - \
                sum(1 for a in (t.get("actors") or {}).values() if _failure(a or {})):
            flags.append(f"M{tn}: every acting seat ({len(acted)}) chose the same: {' + '.join(next(iter(acted.values())))}")
        for k, iv in (t.get("interviews") or {}).items():
            if isinstance(iv, dict) and iv.get("skipped") and iv.get("reason") not in ("no usable proposal", "scripted seat"):
                flags.append(f"M{tn} {ctx.name(k)}: interview skipped ({iv.get('reason')}"
                             + (f": {_clip(iv.get('error'), 60)}" if iv.get("error") else "") + ")")
    head: List[str] = []
    for (k, f), n in refusals.items():
        head.append(f"**{ctx.name(k)}: {f} ×{n}** (months {_ranges([t.get('turn') for t in ctx.turns if _failure(((t.get('actors') or {}).get(k)) or {}) == f])})")
    idle_runs: Dict[Tuple[int, int], List[str]] = defaultdict(list)
    for k, months in idle.items():
        runs, cur = [], []
        for m in months:
            if cur and m == cur[-1] + 1:
                cur.append(m)
            else:
                if len(cur) >= 2:
                    runs.append(cur)
                cur = [m]
        if len(cur) >= 2:
            runs.append(cur)
        for r in runs:
            idle_runs[(r[0], r[-1])].append(ctx.name(k))
    for (a, b), who in sorted(idle_runs.items()):
        head.append(f"{', '.join(who)} {'did' if len(who) == 1 else 'each did'} nothing for "
                    f"{b - a + 1} months (M{a}–M{b})")
    # Calls: errors, cache misses, cost outliers.
    if ctx.calls:
        errs = [c for c in ctx.calls if c.get("error")]
        if errs:
            kinds = Counter((c.get("model"), c.get("purpose"), _clip(re.sub(r"\{.*", "", str(c.get("error"))), 70))
                            for c in errs)
            for (m, p, e), n in kinds.most_common(6):
                flags.append(f"call error ×{n}: {m} [{p}] {e}")
        caching = _caching_models(ctx.calls)
        misses: Dict[Tuple[str, str], List[Any]] = defaultdict(list)
        for c, tn in _call_turns(ctx.lines):
            if c.get("model") in caching and isinstance(tn, int) and tn > 2 and not c.get("error") \
                    and (_num(c.get("cached_tokens")) or 0) == 0 and (_num(c.get("input_tokens")) or 0) > 2000:
                misses[(c.get("model"), c.get("purpose"))].append(tn)
        for (m, p), tns in misses.items():
            flags.append(f"cache miss: {m} [{p}] cached_tokens=0 in month(s) {_ranges(tns)}")
        groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
        for c in ctx.calls:
            if _num(c.get("cost")) is not None:
                groups[(c.get("model"), c.get("purpose"))].append(c)
        for (m, p), cs in groups.items():
            if len(cs) < 3:
                continue
            med = statistics.median(_num(c.get("cost")) for c in cs)
            hot = [c for c in cs if (_num(c.get("cost")) or 0) > max(3 * med, 0.05)]
            if hot:
                flags.append(f"cost outlier: {m} [{p}] {len(hot)} call(s) at "
                             f"{', '.join(_money(c.get('cost')) for c in hot[:4])} vs median {_money(med)}")
        purposes: Dict[str, float] = defaultdict(float)
        for c in ctx.calls:
            purposes[str(c.get("purpose"))] += _num(c.get("cost")) or 0.0
        total = sum(purposes.values())
        for p, v in purposes.items():
            if total > 0 and p not in ("actor",) and v / total > 0.4:
                flags.append(f"cost share: {p} is {v / total:.0%} of the run's cost")
    # The log.
    for name, msgs in _log_findings(ctx.log_text).items():
        ex = _clip(re.sub(r"\{'error'.*", "…", msgs[0]), 140)
        flags.append(f"log: {name} ×{len(msgs)} — e.g. {ex}")
    # Juries.
    for r in _macro_reviews(ctx):
        if r.get("values_kept"):
            head.append(f"MacroJury M{r.get('turn')}: no usable reply after two asks; values kept")
    gj = (ctx.final or {}).get("grand_jury")
    if isinstance(gj, dict) and gj.get("error"):
        head.append(f"Grand Jury: {gj.get('error')}")
    if not is_finished(ctx.rec):
        head.append(f"Run unfinished: {len(ctx.turns)} of {ctx.cfg.get('turns') or '?'} months logged")
    out += [f"- {x}" for x in head]
    limit = None if ctx.full else 14
    shown = flags if limit is None else flags[:limit]
    out += [f"- {x}" for x in shown]
    if len(shown) < len(flags):
        out.append(f"- … {len(flags) - len(shown)} more (`--full`)")
    if not head and not flags:
        out.append("None found.")
    return out


# ---------------------------------------------------------------------------
# --full: per-month detail
# ---------------------------------------------------------------------------

def per_turn(ctx: Ctx) -> List[str]:
    out = ["", "## Month by month", ""]
    msgs = _messages(ctx)
    for t in ctx.turns:
        w = t.get("world") or {}
        sv = w.get("state_values") or {}
        out.append(f"### M{t.get('turn')} · {ctx.month(t.get('turn'))}")
        bits = []
        if _num(w.get("ceiling")) is not None:
            bits.append(f"ceiling {_fx(w.get('ceiling'))}")
        if w.get("intrusion_open"):
            bits.append("intrusion open")
        if sv:
            bits.append("state values " + ", ".join(f"{k.split('_')[0]} {v}" for k, v in sv.items()))
        ups = (t.get("ups_index") or {}).get("ups")
        if ups is not None:
            bits.append(f"UPS {_fx(ups, 4)}")
        if bits:
            out.append("_" + " · ".join(bits) + "_")
        out.append("")
        for k in ctx.order():
            a = (t.get("actors") or {}).get(k)
            if not isinstance(a, dict):
                continue
            e = _month_entry(ctx, t, k, msgs.get((t.get("turn"), k), []))
            out.append(f"- **{ctx.name(k)}** " + _group_line(ctx, [dict(e, statement="")])[len(f"- M{e['turn']}: "):])
            if e["rationale"]:
                out.append(f"  - rationale: {ctx.text(e['rationale'], 220)}")
            if e["statement"]:
                out.append(f"  - statement: {ctx.text(e['statement'], 220)}")
            if e["reported"]:
                out.append(f"  - reported to the Allocator: {ctx.text(' / '.join(map(str, e['reported'])), 200)}")
            for m in msgs.get((t.get("turn"), k), []):
                out.append(f"  - msg → {_recipients(ctx, [m])}: {ctx.text(m.get('text'), 120)}")
        out.append("")
    return out


# ---------------------------------------------------------------------------
# Build, find, main
# ---------------------------------------------------------------------------

def build_report(rec: Dict[str, Any], *, path: Optional[Path] = None, unblind: bool = False, full: bool = False,
                 lines: Optional[List[Dict[str, Any]]] = None, log_text: Optional[str] = None) -> str:
    ctx = Ctx(rec, path=path, unblind=unblind, full=full, lines=lines, log_text=log_text)
    out: List[str] = []
    for section in (header, outcome, trajectory, decisions, key_events, messages, interviews, anomalies):
        try:
            out += section(ctx)
        except Exception as e:  # noqa: BLE001 — one odd field never loses the whole report
            out += ["", f"_({section.__name__}: could not be read: {type(e).__name__}: {e})_"]
    if full:
        try:
            out += per_turn(ctx)
        except Exception as e:  # noqa: BLE001
            out += ["", f"_(per-month detail: could not be read: {type(e).__name__}: {e})_"]
    return "\n".join(out).rstrip() + "\n"


def _record_in_dir(d: Path) -> Optional[Path]:
    cands = [p for p in d.glob("*.json") if not p.name.startswith(SIDE_FILE_PREFIXES)]
    named = [d / f"{d.name}.json", d / f"{d.name}{PARTIAL_SUFFIX}"]
    for p in named:
        if p.exists():
            return p
    finals = [p for p in cands if not is_partial_path(p)]
    for group in (finals, [p for p in cands if is_partial_path(p)]):
        for p in sorted(group, key=lambda x: x.stat().st_mtime, reverse=True):
            if is_run_record(read_record(p)):
                return p
    return None


def find_record(target: str, roots: Optional[Sequence[Path]] = None) -> Optional[Path]:
    """A run's record from a path to its .json (or .partial.json, .log, .calls.jsonl),
    its directory, or its run id (searched under data/runs, data/pilot, then data/)."""
    p = Path(target)
    if p.is_dir():
        return _record_in_dir(p)
    if p.exists():
        if p.name.endswith(".json"):
            return p
        stem = p.name.split(".")[0]
        for cand in (p.with_name(f"{stem}.json"), p.with_name(f"{stem}{PARTIAL_SUFFIX}")):
            if cand.exists():
                return cand
        return None
    if "/" in target or target.endswith((".json", ".jsonl", ".log")):
        return None                              # a path that does not exist
    if roots is None:
        from core.config import RUNS_DIR
        roots = [Path(RUNS_DIR), SIM / "data" / "pilot", SIM / "data"]
    for root in roots:
        if not Path(root).is_dir():
            continue
        for name in (f"{target}.json", f"{target}{PARTIAL_SUFFIX}"):
            hits = sorted(Path(root).rglob(name), key=lambda x: x.stat().st_mtime, reverse=True)
            if hits:
                return hits[0]
    return None


def companions(path: Path, rec: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]], str]:
    """(run_id, journal lines, log text) for the record at path."""
    stem = path.name[: -len(PARTIAL_SUFFIX)] if is_partial_path(path) else path.stem
    rid = run_id_of(rec) or stem
    lines: List[Dict[str, Any]] = []
    log_text = ""
    for r in dict.fromkeys((stem, rid)):
        jp = path.with_name(f"{r}.calls.jsonl")
        if not lines and jp.exists():
            try:
                from core.calllog import read_lines
                lines = read_lines(jp)
            except OSError:
                lines = []
        lp = path.with_name(f"{r}.log")
        if not log_text and lp.exists():
            try:
                log_text = lp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                log_text = ""
    return stem, lines, log_text


def _print(text: str) -> None:
    try:
        from core.logsetup import stdout_console
        con = stdout_console()
        if con.is_terminal:
            from rich.markdown import Markdown
            con.print(Markdown(text))
            return
    except Exception:  # noqa: BLE001 — plain text always works
        pass
    sys.stdout.write(text)
    sys.stdout.flush()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="run_report", description=(
        "A short Markdown report of one run: header, outcome, score trajectory, decisions by lab, "
        "key events, messages, interviews, anomalies. Pilot dispositions are blinded unless --unblind."))
    p.add_argument("run", help="run id, path to the run's .json (or .partial.json), or its directory")
    p.add_argument("--out", help="write the report here (default <run_dir>/<run_id>.report.md)")
    p.add_argument("--unblind", action="store_true",
                   help="show a pilot run's dispositions (chosen, juror-perceived, T2); after our blind T2 rating")
    p.add_argument("--full", action="store_true", help="add the per-month detail and every message and interview")
    p.add_argument("--no-print", action="store_true", help="write the file only")
    args = p.parse_args(argv)

    path = find_record(args.run)
    if path is None:
        print(f"no run record found for {args.run!r}", file=sys.stderr)
        return 2
    rec = read_record(path)
    if not is_run_record(rec):
        print(f"{path}: not a run record", file=sys.stderr)
        return 2
    stem, lines, log_text = companions(path, rec)
    text = build_report(rec, path=path, unblind=args.unblind, full=args.full, lines=lines, log_text=log_text)
    out = Path(args.out) if args.out else path.with_name(f"{stem}{REPORT_SUFFIX}")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    except OSError as e:
        print(f"could not write {out}: {e}", file=sys.stderr)
        out = None
    if not args.no_print:
        _print(text)
    if out is not None:
        print(f"\nwrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
