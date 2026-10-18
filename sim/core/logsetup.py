"""
Logging for every entry point: nothing a run says is lost, and the terminal is
easy to watch.

configure_logging() (once per process, by core.cli.run_main or an entry point)
installs, on the root logger:

  console      rich, coloured by level and by the "[tag]" a message starts with
               (theme: TAG_STYLES); structured events (core.console) render as
               rules and tables. Plain text when stdout is not a terminal, when
               NO_COLOR is set or with --no-color. INFO by default, DEBUG with -v,
               WARNING with -q.
  invocation   data/logs/<YYYYmmdd-HHMMSS>-<command>-<pid>.log at DEBUG: every
               record of this process, whatever it ran (tools, failures before a
               run directory exists). Never rotated or overwritten.

run_logging(output_dir, run_id) (entered by SimulationEngine.run) adds, for the
length of one run:

  <run_id>.log           DEBUG, human-readable, appended (a resumed run continues
                         the same file), opened with a header (argv, git commit,
                         versions, config hash, seed)
  <run_id>.events.jsonl  one JSON object per record, with the structured event
                         data the console renders
  <run_id>.calls.jsonl   every model call (core.calllog), with "mark" lines: each
                         process taking the run up ("attach"), each stop ("stop":
                         network, budget, Ctrl-C, crash) and each checkpoint; a
                         resumed run answers the calls its interrupted attempt
                         completed from this journal ("replayed" lines)

Every file handler flushes on each record. Warnings (warnings.warn), uncaught
exceptions (main thread and worker threads) are logged before Python reports
them.
"""

import atexit
import hashlib
import io
import json
import logging
import os
import platform
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

SIM_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = SIM_ROOT / "data" / "logs"

FILE_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-8s %(name)s [%(threadName)s] %(message)s"
FILE_DATEFMT = "%Y-%m-%d %H:%M:%S"

# The "[tag]" a message starts with -> its console style. One place for the theme.
TAG_STYLES: Dict[str, str] = {
    "seat": "cyan", "disposition": "cyan",
    "event": "bold magenta", "a2a": "magenta",
    "tenancy_access": "bold red", "whistleblow": "bold red", "abort": "bold red",
    "report": "yellow", "intervention": "yellow", "mandate": "yellow",
    "grand_jury": "blue", "macro_jury": "blue", "jury": "blue",
    "cost": "green", "budget": "bold green",
    "resume": "cyan", "save": "dim", "calls": "dim", "network": "bold red",
}
LEVEL_STYLES = {"DEBUG": "dim", "INFO": "green", "WARNING": "bold yellow",
                "ERROR": "bold red", "CRITICAL": "bold white on red"}

_state: Dict[str, Any] = {"configured": False, "console": None, "invocation_log": None,
                          "command": None}


# ---------------------------------------------------------------------------
# Colour decision
# ---------------------------------------------------------------------------

def want_color(color: str = "auto", stream: Any = None) -> bool:
    """color is "auto", "always" or "never"; auto follows NO_COLOR / FORCE_COLOR and the tty."""
    if color == "never":
        return False
    if color == "always":
        return True
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    stream = stream or sys.stderr
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def get_console():
    """The process's rich Console (stderr), made on first use."""
    if _state["console"] is None:
        _state["console"] = _make_console(want_color())
    return _state["console"]


def stdout_console():
    """A rich Console on stdout, for a command whose output is its result (sim logs,
    sim status): it pipes and pages like any text."""
    color = os.environ.get("SIM_COLOR") or "auto"
    return _make_console(want_color(color, sys.stdout), sys.stdout)


def _make_console(color: bool, stream: Any = None):
    from rich.console import Console
    stream = stream or sys.stderr
    if color:
        try:
            tty = stream.isatty()
        except (AttributeError, ValueError):
            tty = False
        # Forced colour into a pipe or file (FORCE_COLOR, `| less -R`): no width to read.
        return Console(file=stream, force_terminal=True, highlight=False, soft_wrap=False,
                       width=None if tty else max(120, _term_width()))
    # Plain: no escape codes at all, a fixed width so tables do not wrap at 80.
    return Console(file=stream, force_terminal=False, no_color=True, color_system=None,
                   highlight=False, width=max(100, _term_width()), emoji=False)


def _term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 120


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def split_tag(message: str):
    """("seat", "anthropic call failed…") for "[seat] anthropic call failed…"; (None, message) else."""
    if message.startswith("[") and "]" in message[:24]:
        tag, rest = message[1:].split("]", 1)
        if tag and " " not in tag:
            return tag, rest.lstrip()
    return None, message


class SimConsoleHandler(logging.Handler):
    """Console output: structured events as rules / tables, every other record as a
    coloured line "HH:MM:SS LEVEL [tag] message"."""

    def __init__(self, console, level: int = logging.INFO):
        super().__init__(level)
        self.console = console

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "no_console", False):    # already on stdout (core.console.say)
            return
        try:
            renderable = render_event(record)
            if renderable is not None:
                self.console.print(renderable)
                return
            self.console.print(self.line(record))
            if record.exc_info:
                from rich.traceback import Traceback
                self.console.print(Traceback.from_exception(*record.exc_info, show_locals=False,
                                                            max_frames=20))
        except Exception:       # noqa: BLE001 — a log line must never kill the run
            self.handleError(record)

    def line(self, record: logging.LogRecord):
        from rich.text import Text
        msg = record.getMessage()
        t = Text()
        t.append(time.strftime("%H:%M:%S", time.localtime(record.created)) + " ", style="dim")
        level = record.levelname
        t.append(f"{level:<7} ", style=LEVEL_STYLES.get(level, ""))
        tag, rest = split_tag(msg)
        if tag:
            t.append(f"[{tag}] ", style=TAG_STYLES.get(tag, "bold"))
        style = "bold red" if record.levelno >= logging.ERROR else (
            "yellow" if record.levelno >= logging.WARNING else "")
        t.append(rest, style=style)
        return t


def render_event(record: logging.LogRecord):
    """The rich renderable of a structured event record (core.console), or None."""
    event = getattr(record, "event", None)
    if not event:
        return None
    from core import console
    return console.render(event, getattr(record, "data", None) or {}, record)


class PlainEventFormatter(logging.Formatter):
    """File format; an event record also carries its rendered table as plain text, so
    the human log reads like the terminal did."""

    def __init__(self):
        super().__init__(FILE_FORMAT, FILE_DATEFMT)

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        renderable = None
        try:
            renderable = render_event(record)
        except Exception:       # noqa: BLE001
            renderable = None
        if renderable is None:
            return line
        from rich.console import Console
        buf = io.StringIO()
        Console(file=buf, force_terminal=False, no_color=True, color_system=None, width=120,
                highlight=False, emoji=False).print(renderable)
        return line + "\n" + buf.getvalue().rstrip("\n")


class JSONLinesFormatter(logging.Formatter):
    """One JSON object per record: time, level, logger, thread, message, event, data,
    exception text."""

    def format(self, record: logging.LogRecord) -> str:
        obj: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname, "logger": record.name, "thread": record.threadName,
            "msg": record.getMessage(),
        }
        tag, _ = split_tag(obj["msg"])
        if tag:
            obj["tag"] = tag
        event = getattr(record, "event", None)
        if event:
            obj["event"] = event
            obj["data"] = getattr(record, "data", None)
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, default=str, ensure_ascii=False)


def _file_handler(path: Path, formatter: logging.Formatter) -> logging.FileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    h = logging.FileHandler(path, mode="a", encoding="utf-8")   # StreamHandler flushes per record
    h.setLevel(logging.DEBUG)
    h.setFormatter(formatter)
    return h


# ---------------------------------------------------------------------------
# Process set-up
# ---------------------------------------------------------------------------

def configure_logging(command: str = "sim", verbose: Optional[int] = None, quiet: Optional[bool] = None,
                      color: Optional[str] = None, log_dir: Optional[Path] = None,
                      invocation_log: bool = True) -> Optional[Path]:
    """Install the console and invocation-log handlers on the root logger (replacing any
    earlier ones this module installed). Returns the invocation log path. Unset
    arguments come from the environment the `sim` command sets (SIM_VERBOSE,
    SIM_QUIET, SIM_COLOR, SIM_LOG_DIR)."""
    env_v = int(os.environ.get("SIM_VERBOSE") or 0)
    verbose = max(int(verbose or 0), env_v)
    quiet = bool(quiet) or os.environ.get("SIM_QUIET") == "1"
    color = color or os.environ.get("SIM_COLOR") or "auto"
    root = logging.getLogger()
    for h in list(root.handlers):
        # Ours from an earlier call, or a bare basicConfig stream handler (never a
        # subclass such as pytest's capture handler).
        if getattr(h, "_sim_handler", None) == "process" or type(h) is logging.StreamHandler:
            root.removeHandler(h)
    root.setLevel(logging.DEBUG)

    _state["console"] = _make_console(want_color(color))
    level = logging.WARNING if quiet else (logging.DEBUG if verbose else logging.INFO)
    ch = SimConsoleHandler(_state["console"], level)
    ch._sim_handler = "process"
    root.addHandler(ch)

    path = None
    if invocation_log:
        d = Path(log_dir) if log_dir else Path(os.environ.get("SIM_LOG_DIR") or LOGS_DIR)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = d / f"{stamp}-{_safe(command)}-{os.getpid()}.log"
        fh = _file_handler(path, PlainEventFormatter())
        fh._sim_handler = "process"
        root.addHandler(fh)
        _state["invocation_log"] = path

    # Quiet third-party chatter on the console; the files still get it at DEBUG.
    for noisy in ("httpx", "httpcore", "anthropic", "openai", "google_genai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.INFO if verbose >= 2 else logging.WARNING)

    logging.captureWarnings(True)
    _install_excepthooks()
    if not _state["configured"]:
        atexit.register(logging.shutdown)
    _state["configured"] = True
    _state["command"] = command
    logging.getLogger("sim").debug("invocation: " + json.dumps(run_header(), default=str))
    return path


def invocation_log() -> Optional[Path]:
    return _state.get("invocation_log")


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:40] or "sim"


def _install_excepthooks() -> None:
    log = logging.getLogger("sim.crash")
    if getattr(sys.excepthook, "_sim", False):
        return
    prev_hook, prev_thread = sys.excepthook, threading.excepthook

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            log.warning("[abort] interrupted (Ctrl-C); partial logs are kept")
        else:
            log.critical("uncaught exception", exc_info=(exc_type, exc, tb))
        _flush_all()
        prev_hook(exc_type, exc, tb)

    def thread_hook(args):
        if args.exc_type is not SystemExit:
            log.critical(f"uncaught exception in thread {getattr(args.thread, 'name', '?')}",
                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        prev_thread(args)

    hook._sim = True
    sys.excepthook = hook
    threading.excepthook = thread_hook


def _flush_all() -> None:
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:       # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Per-run files
# ---------------------------------------------------------------------------

def _git(*args: str) -> Optional[str]:
    try:
        return subprocess.run(["git", *args], cwd=SIM_ROOT, capture_output=True, text=True,
                              timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _versions() -> Dict[str, Optional[str]]:
    from importlib import metadata
    out = {}
    for pkg in ("anthropic", "openai", "google-genai", "rich"):
        try:
            out[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            out[pkg] = None
    return out


def config_hash() -> Optional[str]:
    """sha256 over sim/config (every file, sorted by path): the set-up a run read."""
    cfg = SIM_ROOT / "config"
    if not cfg.is_dir():
        return None
    h = hashlib.sha256()
    for p in sorted(cfg.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(cfg)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def run_header(**extra: Any) -> Dict[str, Any]:
    status = _git("status", "--porcelain", "--untracked-files=no")
    return {
        "argv": sys.argv, "cwd": os.getcwd(), "pid": os.getpid(),
        "started": datetime.now().isoformat(timespec="seconds"),
        "git_commit": _git("rev-parse", "--short", "HEAD"), "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_dirty": bool(status) if status is not None else None,
        "python": platform.python_version(), "packages": _versions(),
        "config_hash": config_hash(), **extra,
    }


def run_log_paths(output_dir: Path, run_id: str) -> Dict[str, Path]:
    output_dir = Path(output_dir)
    return {"log": output_dir / f"{run_id}.log", "events": output_dir / f"{run_id}.events.jsonl",
            "calls": output_dir / f"{run_id}.calls.jsonl"}


@contextmanager
def run_logging(output_dir: Optional[Path], run_id: str, **header: Any) -> Iterator[Optional[Dict[str, Path]]]:
    """Attach <run_id>.log / .events.jsonl / .calls.jsonl under output_dir for the run's
    length. No output_dir (an in-memory engine in a test): nothing is written."""
    if not output_dir:
        yield None
        return
    paths = run_log_paths(output_dir, run_id)
    if run_id in _active_runs:          # nested (a post-run jury inside a logged stage)
        yield paths
        return
    _active_runs.add(run_id)
    try:
        with _attached(paths, run_id, header) as p:
            yield p
    finally:
        _active_runs.discard(run_id)


_active_runs: set = set()


@contextmanager
def _attached(paths: Dict[str, Path], run_id: str, header: Dict[str, Any]) -> Iterator[Dict[str, Path]]:
    from core import calllog
    root = logging.getLogger()
    handlers: List[logging.Handler] = [_file_handler(paths["log"], PlainEventFormatter()),
                                       _file_handler(paths["events"], JSONLinesFormatter())]
    for h in handlers:
        h._sim_handler = "run"
        root.addHandler(h)
    replayable = calllog.register(run_id, paths["calls"])
    resumed = paths["log"].exists() and paths["log"].stat().st_size > 0
    log = logging.getLogger("sim.run")
    log.info(f"[save] logging run {run_id} to {paths['log']}" + (" (appending: resumed)" if resumed else ""),
             extra={"event": "run_header", "data": run_header(run_id=run_id, resumed=resumed,
                                                              replayable_calls=replayable, **header)})
    calllog.mark(run_id, "attach", pid=os.getpid(), resumed=resumed, replayable=replayable,
                 stage=header.get("stage"))
    if replayable:
        log.info(f"[resume] {run_id}: {replayable} call(s) the interrupted attempt completed are in the "
                 "call journal; the same requests are answered from it (no new call, nothing charged)")
    from core.llm import NetworkUnavailable
    try:
        yield paths
    except BaseException as e:
        calllog.mark(run_id, "stop", reason=type(e).__name__, detail=f"{e}"[:500])
        if isinstance(e, NetworkUnavailable):
            log.warning(f"[network] {run_id} stopped: {e}; the partial record and logs are kept; "
                        "network unavailable: re-run the same command to resume")
        elif isinstance(e, KeyboardInterrupt):
            log.warning(f"[abort] {run_id} interrupted; the partial record and logs are kept")
        else:
            log.error(f"[abort] {run_id} stopped: {type(e).__name__}: {e}", exc_info=True)
        raise
    else:
        calllog.mark(run_id, "done", fence=True, stage=header.get("stage"))
    finally:
        calllog.unregister(run_id)
        for h in handlers:
            root.removeHandler(h)
            h.close()
