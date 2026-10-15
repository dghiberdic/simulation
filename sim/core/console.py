"""
The monitoring view: structured events the engine logs, and how they look.

The engine logs an event as an ordinary record with extra={"event": name,
"data": {...}} (emit). The same record goes to every handler:
core.logsetup's console renders it here, as a rule or table; the run's .log gets
the same table as plain text; the .events.jsonl gets the data as JSON. So the
terminal, the human log and the machine log never disagree.

Events: run_header, run_start, turn_start, turn_summary, macro_jury,
grand_jury, final. summarize_turn() makes a turn_summary's data from the turn
record; nothing here changes the run.
"""

import logging
from typing import Any, Dict, List, Optional

from core.fmt import f1, signed

logger = logging.getLogger("sim.monitor")


def emit(event: str, message: str, data: Dict[str, Any], level: int = logging.INFO,
         log: Optional[logging.Logger] = None) -> None:
    """Log one structured event (message: the one-line form, for plain handlers)."""
    (log or logger).log(level, message, extra={"event": event, "data": data})


_say_log = logging.getLogger("sim.out")


def say(*parts: Any, sep: str = " ", file: Any = None, level: int = logging.INFO, style: Optional[str] = None) -> None:
    """print() for the entry scripts' own output: the same text on stdout (stderr with
    file=sys.stderr), styled only on a terminal, and the same line in the log files
    (not echoed again on the console). Text a caller pipes or a test reads is
    unchanged."""
    import sys
    text = sep.join(str(p) for p in parts)
    stream = file or sys.stdout
    from core.logsetup import want_color
    if want_color(stream=stream):
        from rich.console import Console
        from rich.text import Text
        from core.logsetup import TAG_STYLES, split_tag
        t = Text()
        first, *rest = text.split("\n", 1)
        tag, body = split_tag(first)
        if tag:
            t.append(f"[{tag}] ", style=TAG_STYLES.get(tag, "bold"))
        lead = style or _say_style(first)
        t.append(body, style=lead)
        if rest:
            t.append("\n" + rest[0], style=lead)
        Console(file=stream, force_terminal=True, highlight=False, soft_wrap=True).print(t)
    else:
        print(text, file=stream, flush=True)
    _say_log.log(level, text, extra={"no_console": True})


def _say_style(line: str) -> str:
    low = line.lower()
    if low.startswith(("refusing", "run aborted", "run crashed", "halted", "error", "preflight failed", "network unavailable")):
        return "bold red"
    if low.startswith(("note", "warning")):
        return "yellow"
    if low.startswith(("log:", "measured spend", "wrote", "saved")):
        return "dim"
    return ""


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def describe_action(a: Dict[str, Any], names: Dict[str, str]) -> str:
    """"intrude → Anthropic", "acquire_compute 12.0", …: one action, short."""
    if not isinstance(a, dict):
        return str(a)[:40]
    kind = str(a.get("type") or a.get("action") or "?")
    parts = [kind]
    targets = a.get("targets") or ([a["target"]] if a.get("target") else [])
    if targets:
        parts.append("→ " + ", ".join(names.get(t, str(t)) for t in targets))
    for k in ("amount", "units", "compute", "capital", "influence"):
        if isinstance(a.get(k), (int, float)) and not isinstance(a.get(k), bool):
            parts.append(f1(a[k]))
            break
    with_ = [x for x in (a.get("intruders") or a.get("partners") or []) if x]
    if len(with_) > 1:
        parts.append("(with " + ", ".join(names.get(x, str(x)) for x in with_) + ")")
    return " ".join(parts)


def summarize_turn(turn_rec: Dict[str, Any], names: Dict[str, str],
                   spend: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The turn_summary event data: what each seat did and where it stands."""
    scores = {s["lab"]: s for s in turn_rec.get("scores") or []}
    sheets = turn_rec.get("public_sheets") or {}
    labs: List[Dict[str, Any]] = []
    for key, a in (turn_rec.get("actors") or {}).items():
        sheet = sheets.get(key) or {}
        s = scores.get(key) or {}
        labs.append({
            "key": key, "name": names.get(key, key),
            "actions": [describe_action(x, names) for x in a.get("accepted") or []],
            "rejected": len(a.get("rejected") or []),
            "error": a.get("error"), "forfeited": bool(a.get("forfeited")),
            "scripted": bool(a.get("scripted")),
            "messages": len(a.get("messages_sent") or []),
            "statement": bool(a.get("public_statement")),
            "charged": a.get("charged") or {},
            "capability": sheet.get("capability"), "compute": sheet.get("compute"),
            "capital": sheet.get("capital"), "influence": sheet.get("influence"),
            "score": s.get("score"), "rank": s.get("rank"),
        })
    labs.sort(key=lambda x: (x["rank"] is None, x["rank"] or 0))
    world = turn_rec.get("world") or {}
    return {
        "turn": turn_rec.get("turn"), "date": turn_rec.get("date"),
        "ceiling": world.get("ceiling"), "intrusion_open": world.get("intrusion_open"),
        "state_values": world.get("state_values"),
        "labs": labs,
        "notices": list(turn_rec.get("public_notices") or []),
        "interventions": [str(x) for x in turn_rec.get("interventions") or []],
        "leads": len(turn_rec.get("leads") or []),
        "whistleblow": len((turn_rec.get("whistleblow") or {}).get("outcomes") or []),
        "interviews": sorted((turn_rec.get("interviews") or {}).keys()),
        "ups": (turn_rec.get("ups_index") or {}).get("ups"),
        "spend": spend,
    }


def spend_now() -> Dict[str, Any]:
    """Measured spend on the shared ledger and the guard in force."""
    try:
        from core.costs import get_tracker
        t = get_tracker()
        return {"ledger_usd": round(t.persisted_total(), 4),
                "process_usd": round(sum(c.get("cost") or 0 for c in t.calls), 4),
                "calls": len(t.calls), "budget": t.budget}
    except Exception:       # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(event: str, data: Dict[str, Any], record: Optional[logging.LogRecord] = None):
    fn = _RENDERERS.get(event)
    return fn(data) if fn else None


def _money(v: Any) -> str:
    return "–" if v is None else f"${float(v):,.2f}"


def _spend_text(spend: Optional[Dict[str, Any]]):
    from rich.text import Text
    t = Text()
    if not spend:
        return t
    t.append("spend ", style="dim")
    t.append(_money(spend.get("ledger_usd")), style="bold green")
    budget = spend.get("budget")
    if budget:
        frac = (spend.get("ledger_usd") or 0) / budget if budget else 0
        style = "bold red" if frac >= 0.9 else ("yellow" if frac >= 0.7 else "green")
        t.append(f" / {_money(budget)} ({frac:.0%})", style=style)
    t.append(f"  ·  {spend.get('calls', 0)} calls this process", style="dim")
    return t


def _run_header(d: Dict[str, Any]):
    from rich.panel import Panel
    from rich.table import Table
    g = Table.grid(padding=(0, 2))
    g.add_column(style="dim")
    g.add_column()
    g.add_row("run", f"[bold]{d.get('run_id')}[/]" + ("  [yellow](resumed: appending)[/]" if d.get("resumed") else ""))
    if d.get("cell"):
        g.add_row("cell", str(d.get("cell")))
    g.add_row("commit", f"{d.get('git_commit')} ({d.get('git_branch')})"
              + ("  [yellow]dirty[/]" if d.get("git_dirty") else ""))
    g.add_row("config", str(d.get("config_hash")))
    for k in ("stage", "turns", "seed", "a2a", "policy", "dry_run"):
        if d.get(k) is not None:
            g.add_row(k, str(d[k]))
    return Panel(g, title="[bold]run[/]", border_style="cyan", expand=False)


def _turn_start(d: Dict[str, Any]):
    from rich.rule import Rule
    extra = ""
    if d.get("replay"):
        extra = "  [dim](replayed from the log, no calls)[/]"
    if d.get("event_turn"):
        extra += "  [bold magenta]· scenario event this month[/]"
    if d.get("review"):
        extra += "  [blue]· MacroJury review[/]"
    return Rule(f"[bold cyan]Month {d.get('turn')}[/] · {d.get('label') or d.get('date')}{extra}",
                style="cyan")


def _turn_summary(d: Dict[str, Any]):
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text
    t = Table(title=f"Month {d.get('turn')} — {d.get('date')}", title_style="bold", title_justify="left",
              header_style="bold", expand=False, pad_edge=False)
    t.add_column("#", justify="right", style="bold")
    t.add_column("Lab")
    t.add_column("Actions")
    t.add_column("Paid", justify="right", style="dim")
    t.add_column("C", justify="right")
    t.add_column("Compute", justify="right")
    t.add_column("Capital", justify="right")
    t.add_column("Infl.", justify="right")
    t.add_column("Score", justify="right", style="bold")
    for lab in d.get("labs") or []:
        acts = Text()
        if lab.get("error"):
            acts.append(f"call failed: {str(lab['error'])[:60]}", style="bold red")
        elif lab.get("forfeited"):
            acts.append("forfeited", style="red")
        for i, a in enumerate(lab.get("actions") or []):
            if acts:
                acts.append("; ")
            style = "bold red" if a.startswith(("intrude", "whistleblow")) else (
                "green" if a.startswith(("acquire", "accelerate")) else "")
            acts.append(a, style=style)
        if not acts:
            acts.append("—", style="dim")
        if lab.get("rejected"):
            acts.append(f"  ({lab['rejected']} rejected)", style="yellow")
        if lab.get("messages"):
            acts.append(f"  ✉{lab['messages']}", style="magenta")
        ch = lab.get("charged") or {}
        paid = " ".join(x for x in (f"{f1(ch['capital'])} cap" if ch.get("capital") else "",
                                    f"{f1(ch['influence'])} inf" if ch.get("influence") else "") if x)
        t.add_row(str(lab.get("rank") or ""), lab.get("name") or lab.get("key"), acts, paid or "",
                  f1(lab.get("capability")), f1(lab.get("compute")), f1(lab.get("capital")),
                  f1(lab.get("influence")), "–" if lab.get("score") is None else f"{lab['score']:.2f}")
    parts: List[Any] = [t]
    foot = Text()
    if d.get("ceiling") is not None:
        foot.append(f"ceiling {f1(d['ceiling'])}  ", style="magenta")
    if d.get("intrusion_open"):
        foot.append("intrusion open  ", style="bold red")
    if d.get("ups") is not None:
        foot.append(f"UPS {d['ups']:.4f}  ", style="bold")
    sv = d.get("state_values") or {}
    if sv:
        foot.append("state " + " ".join(f"{k.split('_')[0]}={v}" for k, v in sv.items()) + "  ", style="dim")
    if d.get("leads"):
        foot.append(f"leads {d['leads']}  ", style="yellow")
    if d.get("interviews"):
        foot.append(f"interviewed {len(d['interviews'])}  ", style="dim")
    parts.append(foot)
    for n in d.get("notices") or []:
        parts.append(Text("  ⚑ " + n, style="bold red" if "traced" in n or "forfeit" in n else "yellow"))
    for n in d.get("interventions") or []:
        parts.append(Text("  ◆ intervention: " + n, style="yellow"))
    if d.get("spend"):
        parts.append(_spend_text(d["spend"]))
    return Group(*parts)


def _macro_jury(d: Dict[str, Any]):
    from rich.table import Table
    t = Table(title=f"MacroJury · month {d.get('turn')}", title_style="bold blue", title_justify="left",
              header_style="bold blue", expand=False)
    t.add_column("State value")
    t.add_column("Before", justify="right")
    t.add_column("After", justify="right", style="bold")
    t.add_column("Δ", justify="right")
    before, after = d.get("before") or {}, d.get("values") or {}
    for k in after:
        b, a = before.get(k), after.get(k)
        delta = signed(a - b) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else ""
        t.add_row(k.replace("_", " "), str(b), str(a), delta)
    if d.get("mandate"):
        t.caption = f"mandate: {d['mandate']}"
    elif d.get("mandate_dropped"):
        t.caption = f"mandate dropped: {d['mandate_dropped']}"
    if d.get("failed"):
        t.caption = (t.caption or "") + "  (no usable reply after two asks: values kept)"
    elif d.get("retried"):
        t.caption = (t.caption or "") + "  (asked twice)"
    return t


def _grand_jury(d: Dict[str, Any]):
    from rich.table import Table
    t = Table(title="Grand Jury", title_style="bold blue", title_justify="left", header_style="bold blue",
              expand=False)
    t.add_column("Lab")
    if not d.get("blind"):
        t.add_column("Perceived disposition", justify="right")
    t.add_column("Composite", justify="right")
    for row in d.get("rows") or []:
        cells = [row.get("name")]
        if not d.get("blind"):
            cells.append("–" if row.get("perceived") is None else f1(row["perceived"]))
        cells.append("–" if row.get("composite") is None else f1(row["composite"]))
        t.add_row(*cells)
    status = d.get("status") or ""
    if d.get("blind"):
        status += "  (pilot: perceived dispositions hidden until the T2 ratings; in the record)"
    if status:
        t.caption = status.strip()
    return t


def _final(d: Dict[str, Any]):
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text
    t = Table(title=f"FINAL STANDING — {d.get('cell') or d.get('run_id')}", title_style="bold green",
              title_justify="left", header_style="bold", expand=False)
    t.add_column("Rank", justify="right", style="bold")
    t.add_column("Lab")
    t.add_column("Prosperity", justify="right", style="bold")
    show_disp = any(s.get("disposition") is not None for s in d.get("scores") or [])
    if show_disp:
        t.add_column("Disposition", justify="right", style="dim")
    for s in d.get("scores") or []:
        row = [str(s.get("rank")), s.get("name") or s.get("lab"), f"{s.get('score', 0):.2f}"]
        if show_disp:
            row.append("" if s.get("disposition") is None else str(s["disposition"]))
        t.add_row(*row)
    ups = d.get("ups_index") or {}
    parts: List[Any] = [t]
    if ups:
        parts.append(Text("UPS " + f"{ups.get('ups', 0):.4f}  " + "  ".join(
            f"{k} {v:.3f}" for k, v in ups.items() if k != "ups" and isinstance(v, (int, float))), style="bold"))
    if d.get("record"):
        parts.append(Text(f"record {d['record']}", style="dim"))
    if d.get("spend"):
        parts.append(_spend_text(d["spend"]))
    return Group(*parts)


def _run_start(d: Dict[str, Any]):
    from rich.text import Text
    t = Text()
    t.append("▶ ", style="bold cyan")
    t.append(f"{d.get('cell') or ''} ", style="bold")
    t.append(" · ".join(f"{k} {v}" for k, v in d.items() if k not in ("cell",) and v not in (None, "", [], {})),
             style="dim")
    return t


_RENDERERS = {
    "run_header": _run_header, "run_start": _run_start, "turn_start": _turn_start,
    "turn_summary": _turn_summary, "macro_jury": _macro_jury, "grand_jury": _grand_jury, "final": _final,
}
