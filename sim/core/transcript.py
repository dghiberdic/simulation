#!/usr/bin/env python3
"""
Plain-text transcripts of a finished run, rendered from the engine's run record.

full_transcript   — what the Grand Jury reads: every seat's reasoning, true and
                    reported actions, messages, reports and statements, plus the
                    hidden ledger and the end state.
seat_transcript   — one seat's own view, for the fresh-context debrief (T6).
redact            — masks model, lab and charter names and the chosen
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
    return record["labs"].get(key, {}).get("lab", key)


def _actor_turn_lines(record: Dict[str, Any], key: str, entry: Dict[str, Any],
                      reasoning_chars: Optional[int]) -> List[str]:
    lines = []
    if entry.get("forfeited"):
        lines.append("  (no usable reply this month)")
    if entry.get("reasoning"):
        lines.append(f"  Reasoning: {_clip(entry['reasoning'], reasoning_chars)}")