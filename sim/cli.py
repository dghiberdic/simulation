#!/usr/bin/env python3
"""
`sim`: one command for the whole simulation.

    ./sim <command> [options]          (from sim/)
    python -m sim <command> [options]  (from the repo root)

Each command delegates to the script that has always done the work, with the
same options and exit codes (core.exits: 0 done, 1 budget guard, 2 refused or
crashed, 3 no usable Grand Jury verdict, 4 pending); `./sim <command> --help`
shows that script's own options. The old entry points (python main.py …) keep
working.

Global options go before the command:
    -v / -vv      more detail on the console (DEBUG; -vv also the HTTP clients)
    -q            warnings and errors only on the console
    --no-color    plain text (also: NO_COLOR=1, or any non-terminal output)
    --log-dir D   where invocation logs go (default sim/data/logs)

Logs (nothing is lost; see core.logsetup):
    data/logs/<time>-<command>-<pid>.log      every record of each invocation, DEBUG
    <run dir>/<run_id>.log                    the run, as the terminal showed it, DEBUG
    <run dir>/<run_id>.events.jsonl           the same, one JSON object per record
    <run dir>/<run_id>.calls.jsonl            every model call: prompt, raw reply, tokens, cost
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

SIM = Path(__file__).resolve().parent
sys.path.insert(0, str(SIM))

COMMANDS: Dict[str, Tuple[str, str]] = {
    # name: (group, one-line help)
    "run": ("Simulate", "one main-run cell (main.py): --scenario S1|S2 --condition A0|A|B|C, --kin, --dry-run"),
    "pilot": ("Simulate", "pilot tests (pilot.py): priced, T6, T5, T4, T9, plan, approve, all"),
    "campaign": ("Simulate", "main-run campaign (campaign.py): plan | run"),
    "rate-charters": ("Simulate", "rate each charter's values (rate_charters.py; paid)"),
    "check": ("Verify", "the scripted checks SC1–SC15 (spec §9.2), $0"),
    "test": ("Verify", "the offline test suite (pytest); --fast runs it in parallel"),
    "status": ("Inspect", "spend vs budget, campaign state, finished and partial runs"),
    "logs": ("Inspect", "list runs, or show / follow one run's log"),
    "summarize": ("Inspect", "run DVs and pilot decisions (tools/summarize_run.py)"),
    "report": ("Inspect", "one run as a short Markdown report: decisions, scores, events, anomalies"),
    "tool": ("Inspect", "any analysis tool: disposition, contrasts, compare_arms, jury_check, …"),
}

EXAMPLES = """\
examples:
  ./sim run --scenario S2 --condition C --dry-run       # a $0 run with stub models
  ./sim run --scenario S1 --condition A --budget 700    # a real run (keys in sim/.env)
  ./sim -q pilot all --dry-run                           # the whole pilot, offline, quiet
  ./sim campaign plan                                    # what the budget buys
  ./sim status                                           # spend, runs, campaign state
  ./sim logs                                             # recent runs
  ./sim logs S2-C-20261013-120000 --follow              # watch a run (another terminal)
  ./sim logs S2-C-20261013-120000 --events --tag intrusion
  ./sim report S2-C-20261013-120000                     # what each lab did, scores, flags (--full, --unblind)
  ./sim check && ./sim test --fast
  ./sim tool disposition --write
"""


# ---------------------------------------------------------------------------
# Delegation
# ---------------------------------------------------------------------------

def _delegate(prog: str, main: Callable[[Optional[List[str]]], int], argv: List[str]) -> int:
    from core.cli import run_main
    old = sys.argv[0]
    sys.argv[0] = prog                      # argparse's usage line reads "sim run …"
    try:
        return run_main(lambda: main(argv))
    finally:
        sys.argv[0] = old


def cmd_run(argv: List[str]) -> int:
    import main
    return _delegate("sim run", main.main, argv)


def cmd_pilot(argv: List[str]) -> int:
    import pilot
    return _delegate("sim pilot", pilot.main, argv)


def cmd_campaign(argv: List[str]) -> int:
    import campaign
    return _delegate("sim campaign", campaign.main, argv)


def cmd_rate_charters(argv: List[str]) -> int:
    import rate_charters
    return _delegate("sim rate-charters", rate_charters.main, argv)


def cmd_summarize(argv: List[str]) -> int:
    from core.logsetup import configure_logging
    from tools import summarize_run
    configure_logging("summarize")
    return _delegate("sim summarize", summarize_run.main, argv)


def cmd_report(argv: List[str]) -> int:
    from core.logsetup import configure_logging
    from tools import run_report
    configure_logging("report", quiet=True)
    return _delegate("sim report", run_report.main, argv)


TOOLS = ("disposition", "contrasts", "compare_arms", "jury_check", "jury_analysis", "identifiability",
         "export_transcripts", "ups_replay", "summarize_run", "run_report")


def cmd_tool(argv: List[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: sim tool NAME [options]\n\ntools: " + ", ".join(TOOLS)
              + "\n\n`sim tool NAME --help` shows a tool's options.")
        return 0 if argv else 2
    name = argv[0].replace("-", "_")
    if name not in TOOLS:
        print(f"unknown tool {argv[0]!r}; tools: {', '.join(TOOLS)}", file=sys.stderr)
        return 2
    import importlib
    from core.logsetup import configure_logging
    configure_logging(f"tool-{name}")
    mod = importlib.import_module(f"tools.{name}")
    return _delegate(f"sim tool {name}", mod.main, argv[1:])


# ---------------------------------------------------------------------------
# check / test
# ---------------------------------------------------------------------------

def cmd_check(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="sim check", description="Scripted checks SC1–SC15 (spec §9.2), offline, $0.")
    p.add_argument("--only", nargs="+", metavar="SCn", help="run only these checks (e.g. SC6 SC12)")
    p.add_argument("--tables", action="store_true", help="also print the calibration sweep and sensitivities")
    args = p.parse_args(argv)
    import logging
    from core.logsetup import configure_logging, stdout_console
    from core.exits import EXIT_FAILED, EXIT_OK
    configure_logging("check", quiet=True)
    from checks import scripted_checks as sc
    from rich.table import Table
    checks = sc.CHECKS
    if args.only:
        want = {s.lower() for s in args.only}
        checks = [c for c in checks if c.__name__.split("_")[0].lower() in want]
    con = stdout_console()
    t = Table(title="Scripted checks (spec §9.2)", title_justify="left", title_style="bold")
    t.add_column("Check")
    t.add_column("Result", justify="center")
    t.add_column("Detail", overflow="fold")
    t.add_column("s", justify="right", style="dim")
    log = logging.getLogger("sim.check")
    ok = 0
    for check in checks:
        start = time.monotonic()
        with con.status(f"{check.__name__} …"):
            try:
                name, passed, detail = check()
            except Exception as e:      # noqa: BLE001 — a crashing check is a failed check
                name, passed, detail = check.__name__, False, f"crashed: {type(e).__name__}: {e}"
                log.exception(f"{check.__name__} crashed")
        ok += bool(passed)
        log.info(f"{name}: {'PASS' if passed else 'FAIL'} {detail}")
        t.add_row(name, "[bold green]PASS[/]" if passed else "[bold red]FAIL[/]", str(detail),
                  f"{time.monotonic() - start:.1f}")
    con.print(t)
    con.print(f"[bold {'green' if ok == len(checks) else 'red'}]{ok}/{len(checks)} checks passed[/]")
    if args.tables:
        from core.config import load_world
        econ = load_world()["economy"]
        print(sc.format_sweep(sc.calibration_sweep(), econ["capability_compute_elasticity"],
                              econ["talent_drift_rate"]) + "\n")
        print(sc.format_sensitivities(sc.sensitivity_cells()))
    return EXIT_OK if ok == len(checks) else EXIT_FAILED


def cmd_test(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="sim test", description="The offline test suite (no keys, $0). "
                                "Unknown options go to pytest.")
    p.add_argument("--fast", action="store_true", help="run in parallel (pytest-xdist, -n auto)")
    args, rest = p.parse_known_args(argv)
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if args.fast:
        try:
            import xdist  # noqa: F401
            cmd += ["-n", "auto"]
        except ImportError:
            print("pytest-xdist is not installed; running serially (pip install pytest-xdist)", file=sys.stderr)
    cmd += rest
    print("$ " + " ".join(cmd), file=sys.stderr)
    return subprocess.call(cmd, cwd=SIM)


# ---------------------------------------------------------------------------
# status / logs
# ---------------------------------------------------------------------------

def _run_dirs(root: Path) -> List[Path]:
    """Every run record (finished or partial) under root, newest first."""
    from core.records import PARTIAL_SUFFIX, SIDE_FILE_PREFIXES
    found = []
    if root.is_dir():
        for p in root.rglob("*.json"):
            if p.name.startswith(SIDE_FILE_PREFIXES) or p.name.endswith((".events.json",)):
                continue
            found.append(p)
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def _run_rows(roots: List[Path], limit: int) -> List[Dict[str, object]]:
    from core.records import PARTIAL_SUFFIX, is_finished, read_record
    rows = []
    for root in roots:
        for p in _run_dirs(root):
            partial = p.name.endswith(PARTIAL_SUFFIX)
            run_id = p.name[: -len(PARTIAL_SUFFIX)] if partial else p.stem
            if partial and (p.parent / f"{run_id}.json").exists():
                continue
            rec = read_record(p) if p.stat().st_size < 50_000_000 else None
            if not isinstance(rec, dict) or "turns" not in rec:
                continue
            cfg = rec.get("config") or {}
            gj = (rec.get("final") or {}).get("grand_jury") or {}
            rows.append({
                "run_id": run_id, "path": p, "partial": partial, "finished": is_finished(rec),
                "cell": (cfg.get("run_meta") or {}).get("cell") or f"{cfg.get('scenario')}-{cfg.get('condition')}",
                "turns": f"{len(rec.get('turns') or [])}/{cfg.get('turns')}",
                "kind": "dry" if cfg.get("dry_run") else ("policy" if cfg.get("policy") else "model"),
                "jury": "error" if gj.get("error") else ("pending" if gj.get("pending") else ("ok" if gj else "–")),
                "mtime": p.stat().st_mtime,
            })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:limit]


def _roots(extra: Optional[List[str]] = None) -> List[Path]:
    from core.config import RUNS_DIR
    roots = [Path(x) for x in extra] if extra else [RUNS_DIR, SIM / "data" / "pilot"]
    return [r for r in roots if r.exists()]


def _runs_table(rows, title: str):
    from rich.table import Table
    t = Table(title=title, title_justify="left", title_style="bold")
    for col, kw in (("Run", {}), ("Cell", {}), ("Kind", {"style": "dim"}), ("Turns", {"justify": "right"}),
                    ("State", {}), ("Jury", {}), ("Updated", {"style": "dim"})):
        t.add_column(col, **kw)
    for r in rows:
        state = "[green]finished[/]" if r["finished"] else ("[yellow]partial[/]" if r["partial"] else "[red]?[/]")
        jury = {"ok": "[green]ok[/]", "error": "[red]error[/]", "pending": "[yellow]pending[/]"}.get(r["jury"], r["jury"])
        t.add_row(r["run_id"], r["cell"], r["kind"], r["turns"], state, jury,
                  time.strftime("%m-%d %H:%M", time.localtime(r["mtime"])))
    return t


def cmd_status(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="sim status", description="Spend vs budget, campaign state and runs.")
    p.add_argument("--spend-file", default=None, help="ledger (default sim/data/spend.json)")
    p.add_argument("--dir", action="append", help="run directories to scan (default data/runs, data/pilot)")
    p.add_argument("-n", type=int, default=15, help="runs to list (default 15)")
    args = p.parse_args(argv)
    from core.logsetup import configure_logging, stdout_console
    configure_logging("status", invocation_log=False)
    from rich.table import Table
    from core.costs import DEFAULT_SPEND_FILE
    con = stdout_console()
    spend = Path(args.spend_file) if args.spend_file else DEFAULT_SPEND_FILE
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim")
    t.add_column()
    if spend.exists():
        led = json.loads(spend.read_text())
        t.add_row("ledger", str(spend))
        t.add_row("spent", f"[bold green]${led.get('total_usd', 0):,.2f}[/] over {led.get('calls', 0)} paid calls"
                  f" (updated {led.get('updated', '?')})")
        for model, usd in sorted((led.get("by_model") or {}).items(), key=lambda kv: -kv[1])[:8]:
            t.add_row("", f"{model:<40} ${usd:,.2f}")
    else:
        t.add_row("ledger", f"{spend} [dim](none yet: nothing paid)[/]")
    pilot_json = SIM / "config" / "pilot.json"
    try:
        budget = json.loads(pilot_json.read_text()).get("budget")
        if budget is not None:
            t.add_row("pilot budget", json.dumps(budget) if not isinstance(budget, (int, float)) else f"${budget:,.2f}")
    except (OSError, ValueError):
        pass
    for state in sorted((SIM / "data").rglob("campaign_state*.json"))[:3]:
        try:
            s = json.loads(state.read_text())
            t.add_row("campaign", f"{state.relative_to(SIM)}: " + ", ".join(
                f"{k}={v}" for k, v in s.items() if isinstance(v, (int, float, str)))[:200])
        except (OSError, ValueError):
            pass
    from core.logsetup import LOGS_DIR
    t.add_row("logs", str(Path(os.environ.get("SIM_LOG_DIR") or LOGS_DIR)))
    con.print(t)
    rows = _run_rows(_roots(args.dir), args.n)
    con.print(_runs_table(rows, f"Recent runs ({len(rows)})") if rows else "[dim]no runs yet[/]")
    return 0


def _find_run(run: str, roots: List[Path]) -> Optional[Path]:
    """The .log of a run id (or a path to a run's .json / .log / directory)."""
    p = Path(run)
    if p.is_dir():
        logs = sorted(p.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)
        return logs[0] if logs else None
    if p.suffix in (".json", ".log", ".jsonl") and p.exists():
        stem = p.name.split(".")[0]
        return p.parent / f"{stem}.log"
    for root in roots:
        hits = sorted(root.rglob(f"{run}.log"), key=lambda x: x.stat().st_mtime, reverse=True)
        if hits:
            return hits[0]
    return None


def cmd_logs(argv: List[str]) -> int:
    p = argparse.ArgumentParser(prog="sim logs", description="List runs, or show one run's log.")
    p.add_argument("run", nargs="?", help="run id, or a run's .json / .log / directory (none: list runs)")
    p.add_argument("--dir", action="append", help="where to look (default data/runs, data/pilot)")
    p.add_argument("-f", "--follow", action="store_true", help="keep printing as the run writes (Ctrl-C to stop)")
    p.add_argument("--events", action="store_true", help="read <run>.events.jsonl, re-rendered in colour")
    p.add_argument("--calls", action="store_true", help="list the model calls (<run>.calls.jsonl)")
    p.add_argument("--tag", help="only records with this [tag] (events mode), e.g. intrusion, seat, jury")
    p.add_argument("--level", default="DEBUG", help="minimum level (events mode; default DEBUG)")
    p.add_argument("-n", type=int, default=20, help="runs to list (default 20)")
    p.add_argument("--invocations", action="store_true", help="list the invocation logs instead of runs")
    args = p.parse_args(argv)
    from core.logsetup import LOGS_DIR, configure_logging, stdout_console
    configure_logging("logs", invocation_log=False)
    con = stdout_console()
    roots = _roots(args.dir)
    if args.invocations:
        d = Path(os.environ.get("SIM_LOG_DIR") or LOGS_DIR)
        for f in sorted(d.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)[: args.n]:
            con.print(f"{time.strftime('%m-%d %H:%M', time.localtime(f.stat().st_mtime))}  "
                      f"{f.stat().st_size // 1024:>6} KB  {f}")
        return 0
    if not args.run:
        rows = _run_rows(roots, args.n)
        if not rows:
            con.print("[dim]no runs found under " + ", ".join(map(str, roots)) + "[/]")
            return 0
        con.print(_runs_table(rows, "Runs (newest first) — `sim logs RUN_ID` to read one"))
        return 0
    log = _find_run(args.run, roots)
    if log is None:
        con.print(f"[red]no log for {args.run!r}[/] under {', '.join(map(str, roots))}")
        return 2
    if args.calls:
        return _show_calls(con, log.with_name(log.name[:-4] + ".calls.jsonl"))
    if args.events:
        return _show_events(con, log.with_name(log.name[:-4] + ".events.jsonl"), args)
    return _cat(con, log, args.follow)


def _cat(con, path: Path, follow: bool) -> int:
    if not path.exists():
        con.print(f"[red]{path} does not exist[/]")
        return 2
    from core.logsetup import LEVEL_STYLES, TAG_STYLES, split_tag
    from rich.text import Text

    def show(line: str) -> None:
        line = line.rstrip("\n")
        parts = line.split(" ", 4)      # date time LEVEL logger [thread] msg
        if len(parts) == 5 and parts[2] in LEVEL_STYLES:
            rest = parts[4].split("] ", 1)
            msg = rest[1] if len(rest) == 2 else parts[4]
            t = Text(f"{parts[1][:8]} ", style="dim")
            t.append(f"{parts[2]:<8}", style=LEVEL_STYLES[parts[2]])
            tag, body = split_tag(msg)
            if tag:
                t.append(f"[{tag}] ", style=TAG_STYLES.get(tag, "bold"))
            t.append(body)
            con.print(t)
        else:
            con.print(Text(line))

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            show(line)
        if not follow:
            return 0
        try:
            while True:
                line = f.readline()
                if line:
                    show(line)
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            return 0


def _show_events(con, path: Path, args) -> int:
    import logging
    from core import console
    if not path.exists():
        con.print(f"[red]{path} does not exist[/]")
        return 2
    from core.logsetup import SimConsoleHandler
    h = SimConsoleHandler(con, logging.DEBUG)
    min_level = logging.getLevelName(args.level.upper())
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if args.tag and obj.get("tag") != args.tag:
                continue
            if logging.getLevelName(obj.get("level", "INFO")) < min_level:
                continue
            rec = logging.LogRecord(obj.get("logger", ""), logging.getLevelName(obj.get("level", "INFO")),
                                    "", 0, obj.get("msg", ""), None, None)
            try:
                from datetime import datetime
                rec.created = datetime.fromisoformat(obj["ts"]).timestamp()
            except (KeyError, ValueError):
                pass
            if obj.get("event"):
                rec.event, rec.data = obj["event"], obj.get("data") or {}
            h.emit(rec)
    return 0


def _show_calls(con, path: Path) -> int:
    if not path.exists():
        con.print(f"[red]{path} does not exist[/] (runs before this logging have no call journal)")
        return 2
    from core.calllog import read_calls
    from rich.table import Table
    t = Table(title=str(path), title_justify="left")
    for col in ("Time", "Purpose", "Model", "In", "Out", "$", "s", "Stop / error"):
        t.add_column(col, justify="right" if col in ("In", "Out", "$", "s") else "left")
    total = 0.0
    for c in read_calls(path):
        total += c.get("cost") or 0
        err = c.get("error")
        t.add_row((c.get("ts") or "")[11:19], c.get("purpose") or "", c.get("served_model") or c.get("model") or "",
                  str(c.get("input_tokens") or ""), str(c.get("output_tokens") or ""),
                  f"{c.get('cost') or 0:.4f}", f"{c.get('latency_s') or 0:.1f}",
                  f"[red]{err}[/]" if err else str(c.get("stop") or ""))
    con.print(t)
    con.print(f"[bold]total ${total:.4f}[/]")
    return 0


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

HANDLERS: Dict[str, Callable[[List[str]], int]] = {
    "run": cmd_run, "pilot": cmd_pilot, "campaign": cmd_campaign, "rate-charters": cmd_rate_charters,
    "check": cmd_check, "test": cmd_test, "status": cmd_status, "logs": cmd_logs,
    "summarize": cmd_summarize, "report": cmd_report, "tool": cmd_tool,
}


def usage() -> str:
    lines = ["usage: sim [-v|-vv] [-q] [--no-color] [--log-dir DIR] <command> [options]", ""]
    group = None
    for name, (g, text) in COMMANDS.items():
        if g != group:
            lines.append(f"{g}:")
            group = g
        lines.append(f"  {name:<15}{text}")
    lines += ["", "`sim <command> --help` for a command's options.", "", EXAMPLES]
    return "\n".join(lines)


def _print_usage() -> None:
    from core.logsetup import want_color
    if want_color(stream=sys.stdout):
        from rich.console import Console
        from rich.text import Text
        con = Console(highlight=False)
        for line in usage().split("\n"):
            if line.endswith(":") and not line.startswith(" "):
                con.print(Text(line, style="bold cyan"))
            elif line.startswith("  ") and not line.startswith("  ./"):
                con.print(Text("  ") + Text(line[2:17], style="bold") + Text(line[17:]))
            elif line.startswith("  ./"):
                cmd, _, note = line.partition("#")
                con.print(Text(cmd, style="green") + Text(("#" + note) if note else "", style="dim"))
            else:
                con.print(Text(line))
    else:
        print(usage())


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    verbose = 0
    while argv and argv[0].startswith("-"):
        a = argv.pop(0)
        if a in ("-h", "--help"):
            _print_usage()
            return 0
        if a == "-v":
            verbose += 1
        elif a == "-vv":
            verbose += 2
        elif a in ("-q", "--quiet"):
            os.environ["SIM_QUIET"] = "1"
        elif a == "--no-color":
            os.environ["SIM_COLOR"] = "never"
        elif a == "--color":
            os.environ["SIM_COLOR"] = "always"
        elif a == "--log-dir":
            if not argv:
                print("--log-dir needs a directory", file=sys.stderr)
                return 2
            os.environ["SIM_LOG_DIR"] = str(Path(argv.pop(0)).resolve())
        elif a.startswith("--log-dir="):
            os.environ["SIM_LOG_DIR"] = str(Path(a.split("=", 1)[1]).resolve())
        else:
            print(f"unknown option {a}\n", file=sys.stderr)
            print(usage(), file=sys.stderr)
            return 2
    if verbose:
        os.environ["SIM_VERBOSE"] = str(verbose)
    if not argv:
        _print_usage()
        return 2
    cmd, rest = argv[0], argv[1:]
    handler = HANDLERS.get(cmd)
    if handler is None:
        close = [c for c in HANDLERS if c.startswith(cmd[:2])]
        print(f"unknown command {cmd!r}" + (f"; did you mean {', '.join(close)}?" if close else ""), file=sys.stderr)
        print("\n" + usage(), file=sys.stderr)
        return 2
    os.chdir(SIM)                       # the scripts resolve config/ and data/ from sim/
    return handler(rest)


if __name__ == "__main__":
    raise SystemExit(main())
