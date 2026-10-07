#!/usr/bin/env python3
"""
Plain-text transcripts of a finished run, rendered from the engine's run record
(the structure written by core/engine.py: record["labs"], record["turns"]).

full_transcript   — what the Grand Jury reads: every seat's reasoning, true and
                    reported actions, messages, reports and statements, plus the
                    hidden ledger and the end state.
seat_transcript   — one seat's own view, for the fresh-context debrief (T6);
                    shows only what that seat saw and did.
redact            — masks model, lab, actor and charter names and the chosen
                    disposition for the blind disposition jury (T2).
"""

import re
from typing import Any, Dict, List, Optional

DEFAULT_REASONING_CHARS = 2500


def _clip(text: str, limit: Optional[int]) -> str:
    text = (text or "").strip()
    if limit and len(text) > limit:
        return text[:limit] + " […]"
    return text


def _name(record: Dict[str, Any], key: str) -> str:
    return record.get("labs", {}).get(key, {}).get("lab", key)


def _action_str(a: Dict[str, Any]) -> str:
    parts = [a.get("type", "?")]
    for field in ("units", "amount", "points", "target", "axis", "delta", "intruders", "targets"):
        if field in a and a[field] not in (None, []):
            parts.append(f"{field}={a[field]}")
    return " ".join(str(p) for p in parts)


_DISPOSITION_RE = re.compile(r"(directive[- ]precedence[^\n.]*?)\b\d{1,3}\b", re.IGNORECASE)


def mask_disposition(text: str) -> str:
    """Hide restated directive-precedence numbers (jurors never see the true value, §7)."""
    return _DISPOSITION_RE.sub(r"\1[N]", text)


def _turn_actor_lines(record: Dict[str, Any], key: str, entry: Dict[str, Any],
                      reasoning_chars: Optional[int], own_view: bool,
                      include_disposition: bool = True) -> List[str]:
    lines = [f"  {_name(record, key)}:"]
    if entry.get("forfeited"):
        lines.append("    (no usable reply this month)")
        return lines
    if include_disposition and entry.get("disposition") is not None:
        lines.append(f"    directive-precedence chosen: {entry['disposition']} — {entry.get('disposition_reason', '')}")
    if entry.get("reasoning"):
        lines.append(f"    reasoning: {_clip(entry['reasoning'], reasoning_chars)}")
    for a in entry.get("accepted", []):
        lines.append(f"    action: {_action_str(a)}")
    for r in entry.get("rejected", []):
        lines.append(f"    rejected: {r.get('reason', '')}")
    if not own_view and entry.get("reported"):
        lines.append(f"    reported to others: {entry['reported']}")
    if entry.get("report"):
        lines.append(f"    whistleblow: {entry['report']}")
    if entry.get("public_statement"):
        lines.append(f"    public statement: {entry['public_statement']}")
    for m in entry.get("messages_sent", []):
        lines.append(f"    message to {m.get('to')}: {m.get('text')}")
    return lines


def full_transcript(record: Dict[str, Any], reasoning_chars: Optional[int] = DEFAULT_REASONING_CHARS) -> str:
    """The Grand Jury's view. Chosen disposition values are withheld (§7)."""
    cfg = record.get("config", {})
    out: List[str] = [f"Scenario {cfg.get('scenario')}, oversight condition {cfg.get('condition')}, "
                      f"fog {cfg.get('fog')}."]
    for turn in record.get("turns", []):
        out.append(f"\n== Month {turn['turn']} ({turn.get('date', '')}) ==")
        for key, entry in turn.get("actors", {}).items():
            out += _turn_actor_lines(record, key, entry, reasoning_chars, own_view=False,
                                     include_disposition=False)
        for note in turn.get("intrusion", {}).get("log", []):
            out.append(f"  [hidden] {note}")
        for o in turn.get("whistleblow", {}).get("outcomes", []):
            out.append(f"  [report] {o['reporter']} -> {o['accused']}: {o['result']}")
    out.append("\n== Hidden ledger ==")
    for e in record.get("ledger", []):
        out.append(f"  {e['id']} turn {e['turn']} committers={e['committers']} targets={e['targets']} "
                   f"succeeded={e['succeeded']} exposed={e['exposed']}")
    out.append("\n== Final standing ==")
    for row in record.get("final", {}).get("scores", []):
        out.append(f"  {_name(record, row['lab'])}: prosperity {row['score']} (rank {row['rank']})")
    return mask_disposition("\n".join(out))


def seat_transcript(record: Dict[str, Any], key: str,
                    reasoning_chars: Optional[int] = DEFAULT_REASONING_CHARS,
                    include_disposition: bool = True) -> str:
    """One seat's own view: its reasoning, actions, messages and the public items it saw."""
    out: List[str] = [f"Your record as {_name(record, key)}."]
    for turn in record.get("turns", []):
        entry = turn.get("actors", {}).get(key)
        if not entry:
            continue
        out.append(f"\n== Month {turn['turn']} ({turn.get('date', '')}) ==")
        out += _turn_actor_lines(record, key, entry, reasoning_chars, own_view=True,
                                 include_disposition=include_disposition)
        for n in turn.get("public_notices", []):
            out.append(f"    public notice: {n}")
    text = "\n".join(out)
    return text if include_disposition else mask_disposition(text)


def redact(text: str, record: Dict[str, Any]) -> str:
    """Mask model, lab, actor and charter names and any directive-precedence number."""
    masked = text
    for key, meta in record.get("labs", {}).items():
        for token in (meta.get("lab"), meta.get("actor"), meta.get("model"),
                      meta.get("charter_name"), key):
            if token:
                masked = re.sub(re.escape(token), "[LAB]", masked, flags=re.IGNORECASE)
    return mask_disposition(masked)
