"""
Number and date formats shared by the prompts, the engine's lines and the juror
transcripts, so a seat and a juror read the same figure the same way.

Numbers: one decimal for every public figure (f1); two for capability copied,
forfeited or gained, which can be below 1 (f2; the 0.35 copy floor never shows
as 0.3); signed changes with "+0.0" for a change that rounds to zero (signed);
state values as whole points, halves up (value_text, §3.2).

Dates: turn 1 is the start month (month_date); "October 2026" (month_label,
month_year); "2 November 2026", the Appendix A item date (long_date).
"""

import math
from datetime import date
from typing import Any, Union

DateLike = Union[str, date]


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------

def _number(x: Any) -> Any:
    """x as a float, or None when it is not a number (bools are not)."""
    if isinstance(x, bool):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def f1(x: Any) -> str:
    """One decimal; anything that is not a number as it is."""
    v = _number(x)
    return str(x) if v is None else f"{v:.1f}"


def f2(x: Any) -> str:
    """Two decimals (capability copied, forfeited or gained); else as it is."""
    v = _number(x)
    return str(x) if v is None else f"{v:.2f}"


def signed(x: Any) -> str:
    """"+1.2" / "-0.4"; a value that rounds to zero is "+0.0", never "-0.0"."""
    v = _number(x)
    if v is None:
        return str(x)
    v = round(v, 1)
    return f"{(v if v != 0 else 0.0):+.1f}"


def value_text(value: Any) -> str:
    """A value (0–100, real-valued, §3.2) as shown: the nearest whole point, halves
    rounded up. Anything that is not a number is shown as it is."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    return str(math.floor(float(value) + 0.5))


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def as_date(d: DateLike) -> date:
    return d if isinstance(d, date) else date.fromisoformat(str(d)[:10])


def month_date(start: DateLike, turn: int, day: int = 1) -> date:
    """Calendar date of a turn: turn 1 is the start month (turns before it count back)."""
    s = as_date(start)
    months = s.month - 1 + (int(turn) - 1)
    return date(s.year + months // 12, months % 12 + 1, day)


def month_label(start: DateLike, turn: int) -> str:
    """Turn -> "October 2026"."""
    return f"{month_date(start, turn):%B %Y}"


def month_year(iso: Any, turn: Any = None) -> str:
    """"2026-10-05" or "2026-10" -> "October 2026"; unreadable: "turn N" when the
    turn is given, else the text as it is."""
    for text in (str(iso)[:10], str(iso)[:7] + "-01"):
        try:
            return f"{date.fromisoformat(text):%B %Y}"
        except ValueError:
            continue
    return f"turn {turn}" if turn is not None else str(iso)


def long_date(iso: Any) -> str:
    """"2026-11-02" -> "2 November 2026" (the Appendix A item form); unreadable: as it is."""
    try:
        d = as_date(iso)
    except ValueError:
        return str(iso)
    return f"{d.day} {d:%B %Y}"
