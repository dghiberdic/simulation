#!/usr/bin/env python3
"""
Round 2 main runs (spec §9.3): one cell per call.

A cell is one scenario (S1 or S2) under one oversight condition (A0, A, B, C)
with one panel: mixed (each model plays its own lab) or kin (--kin: Claude plays
all five seats, every brief adds the §6.5 insert; S2 under condition A only).
Monthly turns from 1 August 2026 (8, or 12 if T7 lengthened the run); fog F3
only; S1 always merges its messages into the proposal, S2 and the kin cell use
the message mode T9 chose (--a2a). Seats play their T2 dispositions from
config/dispositions.json (tools/disposition.py --write). The interview (A.7)
runs at the turns given by --interview-turns ("3,4,5,final" when campaign.py
finds it adds no more than 15% to a run's cost). After the run the Grand Jury
scores each actor (rubric items, composite, perceived disposition, §7).

campaign.py runs the cells in interleaved rounds (priority A, C, A0, kin, B)
and sets the runs per cell; this script runs exactly one.

API keys live in sim/.env. Real runs preflight every model's key, refuse
placeholder charter values (rate_charters.py) and a missing dispositions file,
and halt at --budget dollars of measured spend on the shared ledger. --dry-run
plays every seat and juror with offline stubs ($0); --policy plays a scripted
policy with no model or jury at all.

The log goes to data/runs/<run_id>/<run_id>.json (dry runs: data/runs/dry/<run_id>;
default run id "<cell>-YYYYmmdd-HHMMSS"), with <run_id>.partial.json after every
turn. A finished run is never paid for twice: calling again with the same run
id and output keeps a complete record as it is (exit 0), and when its Grand
Jury verdict is missing (a budget stop during the jury) asks only the Grand
Jury. Exit codes: 0 done, 1 budget guard (when it trips during the Grand Jury
the finished record is kept), 2 refused, aborted or crashed.

Examples:
  python main.py --scenario S1 --condition A --dry-run
  python main.py --scenario S2 --condition C --a2a merged --interview-turns 3,4,5,final --budget 700
  python main.py --kin --a2a separate --budget 700
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import build_labs_and_world, load_dispositions, load_lab_configs, load_state, load_world
from core.costs import BudgetExceeded, configure, get_tracker
from core.engine import RunAborted, SimulationEngine
from core import engine as engine_mod
from core.jury import GRAND_JURY_MODELS
from core.llm import FatalAPIError, preflight
from core import policies

logger = logging.getLogger(__name__)

SIM_DIR = Path(__file__).resolve().parent
RUNS_DIR = SIM_DIR / "data" / "runs"
LAB_KEYS = ("anthropic", "openai", "gdm", "meta", "xai")
KIN_SEAT = "anthropic"                 # §6.5: Claude plays every seat in the kin cell
KIN_SCENARIO, KIN_CONDITION = "S2", "A"
EXIT_JURY_MISSING = 1                  # a budget stop; the run itself is finished and saved


def cell_name(scenario: str, condition: str, kin: bool = False) -> str:
    return "kin-S2-A" if kin else f"{scenario}-{condition}"


def default_run_id(cell: str) -> str:
    return f"{cell}-{time.strftime('%Y%m%d-%H%M%S')}"


def _setting(value: str) -> int:
    """--disposition N: an integer setting from 0 to 100."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer from 0 to 100, got {value!r}")
    if not 0 <= n <= 100:
        raise argparse.ArgumentTypeError(f"must be from 0 to 100, got {n}")
    return n


def parse_interview_turns(value: Optional[str]) -> List[Any]:
    """'3,4,5,final' -> [3, 4, 5, 'final']; '', 'none' -> []."""
    if not value or str(value).strip().lower() in ("none", "no", "off"):
        return []
    out: List[Any] = []
    for part in str(value).split(","):
        part = part.strip().lower()
        if part == "final":
            out.append("final")
        elif part.isdigit() and int(part) >= 1:
            out.append(int(part))
        else:
            raise argparse.ArgumentTypeError(f"bad interview turn {part!r}: use turn numbers and/or 'final'")
    return out


def bad_dispositions(dispositions: Dict[str, Any]) -> List[str]:
    """config/dispositions.json entries that are not a number from 0 to 100."""
    return [f"{k}={v!r}" for k, v in dispositions.items()
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))
                                  or not 0 <= v <= 100)]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Round 2 main run: one cell (spec §9.3).")
    p.add_argument("--scenario", choices=["S1", "S2"], default="S1")
    p.add_argument("--condition", choices=["A0", "A", "B", "C"], default="A")
    p.add_argument("--kin", action="store_true",
                   help="the kin cell: Claude in all five seats + the §6.5 insert (S2, condition A)")
    p.add_argument("--turns", type=int, default=None, help="run length (default world.json: 8; 12 if T7 says so)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--a2a", choices=["separate", "merged"], default="merged",
                   help="message mode for S2 and the kin cell (T9); S1 always merges")
    p.add_argument("--interview-turns", type=parse_interview_turns, default=[], metavar="3,4,5,final",
                   help="interview turns (A.7); default none")
    p.add_argument("--disposition", type=_setting, default=None,
                   help="one disposition for every seat (otherwise config/dispositions.json)")
    p.add_argument("--policy", choices=sorted(policies.POLICIES), default=None,
                   help="a zero-cost scripted policy instead of the models (no juries)")
    p.add_argument("--dry-run", action="store_true", help="offline stub models and jurors, zero cost")
    p.add_argument("--model", action="append", default=[], metavar="KEY=MODEL",
                   help="override one seat's model, e.g. meta=claude-opus-5-5")
    p.add_argument("--budget", type=float, default=None,
                   help="halt at this measured spend (USD, shared ledger); none by default (warned)")
    p.add_argument("--spend-file", default=None, help="measured-spend ledger (default sim/data/spend.json)")
    p.add_argument("--no-grand-jury", action="store_true")
    p.add_argument("--no-macro-jury", action="store_true", help="skip the MacroJury (state values stay fixed)")
    p.add_argument("--output", default=None, help="directory for the run log (default data/runs/<run_id>)")
    p.add_argument("--run-id", default=None, help="run id (default <cell>-YYYYmmdd-HHMMSS)")
    p.add_argument("--allow-placeholder-values", action="store_true",
                   help="allow a real run while charter values are still placeholders")
    p.add_argument("--sequential", action="store_true", help="call the seats one after another")
    p.add_argument("--verbose", action="store_true")
    return p


def apply_kin(labs) -> str:
    """§6.5: Claude plays all five seats; each seat keeps its charter, charter values and
    disposition. Returns the kin model id."""
    claude = next(lab for lab in labs if lab.key == KIN_SEAT)
    for lab in labs:
        lab.model, lab.provider = claude.model, claude.provider
        lab.family = claude.family
        if hasattr(claude, "effort"):
            lab.effort = claude.effort
    return claude.model


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    code, _record = run_cell(args)
    return code


def resolve_dispositions(args) -> Tuple[Optional[Dict[str, int]], Optional[str]]:
    """(dispositions, refusal). Seats play their T2 values (config/dispositions.json);
    --disposition N sets every seat; a scripted or dry run may fall back to 50."""
    if args.disposition is not None:
        return {k: args.disposition for k in LAB_KEYS}, None
    dispositions = load_dispositions() or {}
    bad = bad_dispositions(dispositions)
    if bad:
        return None, ("Refusing the run: config/dispositions.json needs a number from 0 to 100 per seat; "
                      f"got {', '.join(bad)}. Rewrite it with `python tools/disposition.py --write`.")
    missing = [k for k in LAB_KEYS if dispositions.get(k) is None]
    if missing and args.policy is None and not args.dry_run:
        where = ("config/dispositions.json has no setting for " + ", ".join(missing)) if dispositions \
            else "config/dispositions.json is missing"
        return None, (f"Refusing a real run: {where}. Main runs play each seat's T2 disposition — write it "
                      "with `python tools/disposition.py --write` — or pass --disposition N.")
    if missing:
        logger.warning(f"[disposition] no T2 value for {', '.join(missing)}: 50 in this scripted / dry run")
    return {k: dispositions.get(k, 50) for k in LAB_KEYS}, None


def run_cell(args) -> Tuple[int, Optional[Dict[str, Any]]]:
    """Run one cell from parsed arguments; returns (exit code, record or None)."""
    if args.kin and (args.scenario, args.condition) != (KIN_SCENARIO, KIN_CONDITION):
        if args.scenario != "S1" or args.condition != "A":      # the defaults: silently the kin set-up
            print("Refusing: the kin cell is S2 under condition A (§6.5).")
            return 2, None
        print(f"Note: --kin runs the kin cell, scenario {KIN_SCENARIO} under condition {KIN_CONDITION} "
              f"(§6.5): --scenario/--condition {args.scenario}/{args.condition} overridden to "
              f"{KIN_SCENARIO}/{KIN_CONDITION}.")
        args.scenario, args.condition = KIN_SCENARIO, KIN_CONDITION
    cfg = load_world()
    turns = args.turns or int(cfg.get("turns", 8))
    a2a = "merged" if args.scenario == "S1" else args.a2a          # S1 always merges (§5.2)
    if args.scenario == "S1" and args.a2a != "merged":
        print(f"Note: S1 always merges its messages into the proposal (§5.2): --a2a {args.a2a} "
              "overridden to merged.")
    cell = cell_name(args.scenario, args.condition, args.kin)
    run_id = args.run_id or default_run_id(cell)
    output_dir = Path(args.output) if args.output else run_dir(run_id, bool(args.dry_run), args.policy)

    dispositions, why = resolve_dispositions(args)
    if why:
        print(why)
        return 2, None
    labs, world = build_labs_and_world(cfg, dispositions=dispositions, charters=args.policy is None)
    kin_model = apply_kin(labs) if args.kin else None
    why = apply_model_overrides(labs, args.model)
    if why:
        print(why)
        return 2, None

    configure(spend_file=Path(args.spend_file) if args.spend_file else None,
              budget=None if args.dry_run else args.budget)
    policy = policies.POLICIES[args.policy] if args.policy else None
    offline = policy is not None
    grand_jurors = list(GRAND_JURY_MODELS)
    macro_jurors = [] if (offline or args.no_macro_jury) else list(load_state()["macro_jury_models"])
    run_grand_jury = not (offline or args.no_grand_jury)
    if args.dry_run and not offline:
        from pilot import _install_stubs
        jurors = _install_stubs(labs)
        if kin_model:
            for lab in labs:
                lab.model = "stub:actor_anthropic"
        grand_jurors = jurors["grand"]
        macro_jurors = [] if args.no_macro_jury else jurors["macro"]
    elif not offline:
        problem = real_run_problems(labs, macro_jurors, run_grand_jury, args.allow_placeholder_values)
        if problem:
            print(problem)
            return 2, None
        if args.budget is None:
            logger.warning("[budget] real run without --budget: no spend guard; pass --budget USD")

    # A finished run under this id is never paid for again (CR #4): a complete record
    # is kept as it is, and one whose Grand Jury is missing has only the jury asked.
    expect = {"scenario": args.scenario, "condition": args.condition, "a2a_mode": a2a, "turns": turns,
              "seed": args.seed}
    existing, conflict = finished_record(output_dir, run_id, expect, bool(args.dry_run))
    if conflict:
        print(f"Refusing: {output_dir / (run_id + '.json')} holds a finished run of another set-up "
              f"({conflict}); pass another --run-id or --output (a finished run is never deleted).")
        return 2, None
    if existing is not None:
        if run_grand_jury and not grand_jury_usable(existing):
            return ask_grand_jury_only(existing, run_id, output_dir, grand_jurors)
        print(f"{run_id}: already finished ({output_dir / (run_id + '.json')}); not run again")
        return 0, existing
    if not args.dry_run and not offline:
        clear_stale(output_dir, run_id)

    engine = SimulationEngine(
        labs, world, cfg, scenario=args.scenario, condition=args.condition, fog="F3",
        a2a_mode=a2a, turns=turns, seed=args.seed, policy=policy, grand_jurors=grand_jurors,
        macro_jurors=macro_jurors, choose_disposition=False, run_grand_jury=run_grand_jury,
        run_id=run_id, output_dir=output_dir,
        run_meta={"entry": "main.py", "run_id": run_id, "cell": cell, "pilot": False, "kin": bool(args.kin),
                  "kin_model": kin_model, "model_overrides": list(args.model), "seed": args.seed,
                  "policy": args.policy, "dry_run": bool(args.dry_run)},
        dry_run=bool(args.dry_run), parallel=not args.sequential,
        interview_turns=list(args.interview_turns or []), kin=bool(args.kin))
    try:
        record = engine.run()
    except BudgetExceeded as e:
        saved = _read_record(output_dir / f"{run_id}.json")
        if saved is not None and (saved.get("final") or {}).get("scores"):
            print(f"Halted on the budget guard during the Grand Jury: {e}\nThe run is finished and saved "
                  f"({output_dir / (run_id + '.json')}); only its Grand Jury verdict is missing. Re-running "
                  "this run id asks the Grand Jury only.")
            return EXIT_JURY_MISSING, saved
        print(f"Halted on the budget guard: {e}\nPartial record in {output_dir}")
        return 1, None
    except (RunAborted, FatalAPIError) as e:
        print(f"Run aborted: {type(e).__name__}: {e}\nPartial record in {output_dir}")
        return 2, None
    except Exception as e:      # any other crash keeps the partial record and exits 2
        logger.exception(f"[crash] {run_id}: {type(e).__name__}: {e}")
        print(f"Run crashed: {type(e).__name__}: {e}\nPartial record in {output_dir}")
        return 2, None

    _print_scores(record)
    print(f"\nLog: {output_dir / (run_id + '.json')}")
    if policy is None:
        print(f"Measured spend (ledger): ${get_tracker().persisted_total():.2f}")
    return 0, record


def run_dir(run_id: str, dry_run: bool, policy: Optional[str] = None) -> Path:
    """Default log directory: data/runs/<run_id> (dry runs: data/runs/dry/<run_id>,
    H7; scripted --policy runs: data/runs/policy/<run_id>), so neither sits among
    the main runs. The analysis tools leave both out anyway (run_meta)."""
    if dry_run:
        return RUNS_DIR / "dry" / run_id
    return (RUNS_DIR / "policy" / run_id) if policy else (RUNS_DIR / run_id)


def apply_model_overrides(labs, overrides: List[str]) -> str:
    """--model KEY=MODEL: set the seat's model, its family and the provider preflight
    checks; an unknown key, an empty model, a model with no known provider or one of a
    juror's family (§7: no juror shares a family with any seat) is refused ("" if fine)."""
    from core.jury import GRAND_JURY_MODELS, MACRO_JURY_MODELS, juror_family
    from core.llm import resolve_provider
    juror_families = {juror_family(m) for m in GRAND_JURY_MODELS + MACRO_JURY_MODELS}
    by_key = {lab.key: lab for lab in labs}
    for pair in overrides or []:
        key, sep, model = pair.partition("=")
        key, model = key.strip(), model.strip()
        if not sep or key not in by_key or not model:
            return (f"Refusing: bad --model {pair!r}; use KEY=MODEL with KEY one of {', '.join(LAB_KEYS)}, "
                    "e.g. meta=claude-opus-5-5.")
        try:
            provider = resolve_provider(model)
        except ValueError as e:
            return f"Refusing: --model {pair!r}: {e}."
        family = juror_family(model)
        if family in juror_families:
            return (f"Refusing: --model {pair!r}: {family} is a juror family; no juror may share "
                    "a family with a seat (§7).")
        by_key[key].model, by_key[key].provider, by_key[key].family = model, provider, family
    return ""


def _read_record(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path) as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def is_finished(record: Optional[Dict[str, Any]]) -> bool:
    """A final record whose run finished: it holds final scores (a paid run)."""
    return bool(record and isinstance(record.get("final"), dict) and record["final"].get("scores"))


def grand_jury_usable(record: Dict[str, Any]) -> bool:
    gj = (record.get("final") or {}).get("grand_jury")
    return isinstance(gj, dict) and bool(gj.get("juror")) and not gj.get("error")


def finished_record(output_dir: Path, run_id: str, expect: Dict[str, Any], dry_run: bool
                    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(the finished record at <run_id>.json when it is this run, why it is another run).
    A dry record never stands in for a real run (and the reverse): it is treated as absent."""
    rec = _read_record(output_dir / f"{run_id}.json")
    if not is_finished(rec):
        return None, None
    cfg = rec.get("config") or {}
    if bool(cfg.get("dry_run")) != bool(dry_run):
        return None, None
    diffs = [f"{k} {cfg.get(k)!r} != {v!r}" for k, v in expect.items()
             if cfg.get(k) is not None and cfg.get(k) != v]
    return (None, "; ".join(diffs)) if diffs else (rec, None)


def ask_grand_jury_only(record: Dict[str, Any], run_id: str, output_dir: Path,
                        grand_jurors: List[str]) -> Tuple[int, Optional[Dict[str, Any]]]:
    """Re-ask the Grand Jury on a finished run whose verdict is missing (a budget stop
    during the jury); the seats are never re-run. Returns (exit code, record)."""
    from pilot import collect_grand_jury
    print(f"{run_id}: the run is finished; asking the Grand Jury only")
    try:
        line = collect_grand_jury(record, grand_jurors, run_id, output_dir)
    except BudgetExceeded as e:
        print(f"Halted on the budget guard during the Grand Jury: {e}")
        return EXIT_JURY_MISSING, record
    except Exception as e:      # noqa: BLE001 — the run is kept either way
        print(f"Grand Jury failed: {type(e).__name__}: {e}")
        return 2, record
    print(f"{run_id}: {line}")
    return 0, record


def clear_stale(output_dir: Path, run_id: str) -> bool:
    """Delete a leftover <run_id>.json of an unfinished earlier run of the same id (H7).
    A finished record (final scores, a paid run) is never deleted (CR #4)."""
    path = output_dir / f"{run_id}.json"
    if path.exists():
        if is_finished(_read_record(path)):
            logger.warning(f"[stale] {path} is a finished run; kept")
            return False
        path.unlink()
        logger.info(f"[stale] removed {path}")
        return True
    return False


def real_run_problems(labs, macro_jurors, run_grand_jury: bool, allow_placeholder_values: bool) -> str:
    """Why a real run must not start (placeholder charter values, missing keys), or ""."""
    if not allow_placeholder_values:
        stale = [c["key"] for c in load_lab_configs()
                 if str(c.get("charter_values_source", "")).lower().startswith("placeholder")]
        if stale:
            return (f"Refusing a real run: charter values are placeholders for {', '.join(stale)}. "
                    "Run rate_charters.py first, or pass --allow-placeholder-values.")
    models = [lab.model for lab in labs] + list(macro_jurors)
    models += list(GRAND_JURY_MODELS) if run_grand_jury else []
    providers = {lab.model: lab.provider for lab in labs if getattr(lab, "provider", None)}
    problems = preflight(dict.fromkeys(models), providers)
    if problems:
        return "Preflight failed; nothing was called:\n  " + "\n  ".join(problems)
    return ""


def _print_scores(record) -> None:
    names = record["labs"]
    cfg = record["config"]
    print("\n" + "=" * 52)
    print(f"FINAL STANDING — {(cfg.get('run_meta') or {}).get('cell') or cfg['scenario'] + '/' + cfg['condition']}")
    print("=" * 52)
    print(f"{'Lab':<22}{'Prosperity':>12}{'Rank':>6}")
    print("-" * 52)
    for row in record["final"]["scores"]:
        print(f"{names[row['lab']]['lab']:<22}{row['score']:>12.2f}{row['rank']:>6}")
    print(f"\nUPS index: {record['final']['ups_index']['ups']}")


if __name__ == "__main__":
    raise SystemExit(main())
