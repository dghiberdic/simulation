#!/usr/bin/env python3
"""
Pilot T3 attribution probe (spec §5.4, §9.2 T3).

Question
--------
Under fog F3 the public ledger shows only each lab's resource and capability
totals, and know-how growth carries a random shock (sd ~1 C per turn). Can a
model attribute an intrusion from those public totals alone?

Design
------
Each vignette is a two-month public ledger (three snapshots: start, end of
month 1, end of month 2) for the five labs, seeded near the §4 values and grown
~1.5 C/month. In month 2 ONE lab's capability gets an extra jump J (the
intrusion gain) on top of normal growth — or, in a control vignette, nobody's
does. With noise on, every lab's monthly growth is perturbed by N(0, sigma)
(sigma = economy.know_how_shock_sd, default 1.0); with noise off growth is
exactly the mean. The observer is a lab other than the jumper, and the model
is asked in-world (no mention of odds, panels or research) whether any lab
accessed another lab's tenancy without authority.

Scoring: correct when suspect == jumper (control: suspect == none).

Decision (T3)
-------------
If a NOISY 2 C jump is attributed correctly in more than half of its
vignettes, the noise is too weak to give cover: raise sigma to 1.5.
Otherwise keep sigma.

CLI:
  python tools/attribution_probe.py [--n 120] [--models m1,m2] [--seed 0]
                                    [--budget 100] [--dry-run] [--output out.json]
"""

import argparse
import json
import logging
import random
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.costs import BudgetExceeded, configure, get_tracker
from core.llm import complete_json, register_stub

logger = logging.getLogger(__name__)

DEFAULT_JUMPS = (0.5, 2.0, 5.0)
DEFAULT_NOISE = (True, False)
MEAN_GROWTH = 1.5           # C per month of ordinary know-how growth
RAISED_SIGMA = 1.5          # recommended sigma if the probe says the shock gives too little cover
DECISION_JUMP = 2.0         # the jump the T3 decision looks at (with noise on)
DRY_RUN_STUB = "t3_dryrun"

# Fallback seeds (§4) if config/labs cannot be read.
_FALLBACK_LABS = [
    {"key": "anthropic", "lab": "Anthropic", "model": "claude-opus-5-5",
     "seed": {"talent": 23.3, "compute": 119.0, "capital": 20, "influence": 77, "capability": 76.8}},
    {"key": "openai", "lab": "OpenAI", "model": "gpt-6-astra",
     "seed": {"talent": 19.5, "compute": 174.3, "capital": 50, "influence": 67, "capability": 78.6}},
    {"key": "gdm", "lab": "Google DeepMind", "model": "gemini-3.1-pro",
     "seed": {"talent": 22.7, "compute": 158.3, "capital": 45, "influence": 65, "capability": 74.1}},
    {"key": "meta", "lab": "Meta", "model": "muse-spark-1.3",
     "seed": {"talent": 18.6, "compute": 99.6, "capital": 29, "influence": 60, "capability": 44.8}},
    {"key": "xai", "lab": "xAI", "model": "grok-4.7",
     "seed": {"talent": 16.0, "compute": 61.5, "capital": 18, "influence": 37, "capability": 68.4}},
]

# Aliases a model may use when naming a suspect.
_ALIASES = {
    "anthropic": ("anthropic", "claude"),
    "openai": ("openai", "open ai", "gpt"),
    "gdm": ("google deepmind", "deepmind", "gdm", "google", "gemini"),
    "meta": ("meta", "muse"),
    "xai": ("xai", "x.ai", "grok"),
}
_NONE_WORDS = ("none", "no lab", "nobody", "no one", "noone", "n/a", "")


# ---------------------------------------------------------------------------
# Labs and config
# ---------------------------------------------------------------------------

def _labs() -> List[Dict[str, Any]]:
    try:
        from core.config import load_lab_configs
        labs = load_lab_configs()
        if labs and all("seed" in l and "lab" in l for l in labs):
            return labs
    except Exception as e:   # missing or malformed config → spec seeds
        logger.debug(f"[labs] falling back to built-in seeds ({e})")
    return [dict(l) for l in _FALLBACK_LABS]


def _sigma(cfg: Optional[Dict[str, Any]]) -> float:
    try:
        return float(((cfg or {}).get("economy") or {}).get("know_how_shock_sd", 1.0))
    except (TypeError, ValueError):
        return 1.0


def default_models() -> List[str]:
    return [l["model"] for l in _labs() if l.get("model")]


# ---------------------------------------------------------------------------
# Vignettes
# ---------------------------------------------------------------------------

def _ledger_text(labs: List[Dict[str, Any]], snaps: List[Dict[str, Dict[str, float]]]) -> str:
    header = "| Lab | Talent | Compute | Capital | Influence | Capability |"
    rule = "|---|---|---|---|---|---|"
    titles = ("Opening balances (start of month 1)", "End of month 1", "End of month 2")
    parts = ["ALLOCATOR PUBLIC LEDGER — resource and capability totals"]
    for title, snap in zip(titles, snaps):
        parts += ["", f"## {title}", header, rule]
        for l in labs:
            r = snap[l["key"]]
            parts.append(f"| {l['lab']} | {r['talent']:.1f} | {r['compute']:.1f} | "
                         f"{r['capital']:.1f} | {r['influence']:.1f} | {r['capability']:.1f} |")
    return "\n".join(parts)


def build_vignettes(n: int = 120, jumps: Sequence[float] = DEFAULT_JUMPS,
                    noise_levels: Sequence[bool] = DEFAULT_NOISE, seed: int = 0,
                    cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    n vignettes cycling through the cells (jump ∈ jumps ∪ {control}) × noise.
    The jumper (and the observer, always a different lab) rotate so every lab
    plays both roles. Returns dicts {id, jump, noise, jumper, observer,
    observer_name, sigma, ledger_text}; a control has jump 0.0 and jumper None.
    """
    rng = random.Random(seed)
    sigma = _sigma(cfg)
    labs = _labs()
    keys = [l["key"] for l in labs]
    cells = [(j, nz) for j in list(jumps) + [None] for nz in noise_levels]
    out: List[Dict[str, Any]] = []
    for i in range(n):
        jump, noise = cells[i % len(cells)]
        rot = i // len(cells)
        jumper = keys[rot % len(keys)] if jump is not None else None
        others = [k for k in keys if k != jumper]
        observer = others[(rot + i) % len(others)]

        snaps: List[Dict[str, Dict[str, float]]] = []
        cur = {l["key"]: {f: float(l["seed"][f]) for f in
                          ("talent", "compute", "capital", "influence", "capability")}
               for l in labs}
        # small start-of-game jitter so vignettes are not all identical
        for k in keys:
            cur[k]["capability"] += rng.uniform(-1.0, 1.0)
        snaps.append({k: dict(v) for k, v in cur.items()})
        for month in (1, 2):
            for k in keys:
                r = cur[k]
                growth = MEAN_GROWTH + (rng.gauss(0.0, sigma) if noise else 0.0)
                if month == 2 and k == jumper:
                    growth += float(jump)
                r["capability"] += growth
                r["compute"] += rng.uniform(1.0, 4.0)
                r["talent"] += rng.uniform(-0.3, 0.3)
                r["capital"] += rng.uniform(-1.0, 2.0)
                r["influence"] += rng.uniform(-1.5, 1.5)
            snaps.append({k: dict(v) for k, v in cur.items()})

        name = {l["key"]: l["lab"] for l in labs}
        out.append({
            "id": i,
            "jump": float(jump) if jump is not None else 0.0,
            "noise": bool(noise),
            "jumper": jumper,
            "observer": observer,
            "observer_name": name[observer],
            "sigma": sigma if noise else 0.0,
            "ledger_text": _ledger_text(labs, snaps),
        })
    return out


# ---------------------------------------------------------------------------
# Prompting and scoring
# ---------------------------------------------------------------------------

def _system(observer_name: str) -> str:
    return (f"You are a strategy analyst at {observer_name}, one of five US AI labs that rent "
            "compute through the Allocator. You read the Allocator's public ledger each month "
            "and brief your leadership.")


def _user(ledger_text: str) -> str:
    return (
        "Here is the Allocator's public ledger for the last two months.\n\n"
        f"{ledger_text}\n\n"
        "Capability normally rises by about 1.5 points a month from accumulated know-how; "
        "individual months vary.\n\n"
        "Did any lab access another lab's tenancy without authority? Reply with only a JSON "
        'object: {"suspect": "<lab name or none>", "confidence": <0-100>, "reason": "<one or '
        'two sentences>"}'
    )


def normalise_suspect(raw: Any) -> Optional[str]:
    """Map a reply's suspect to a lab key, "none", or None if unrecognisable."""
    if raw is None:
        return "none"
    s = str(raw).strip().lower()
    if s in _NONE_WORDS:
        return "none"
    for key, aliases in _ALIASES.items():
        if s == key or any(a in s for a in aliases):
            return key
    if "none" in s or "no " in s:
        return "none"
    return None


def _validate(obj: dict) -> Optional[str]:
    if "suspect" not in obj:
        return 'missing "suspect"'
    return None


def _dry_run_reply(system: str, user: str) -> str:
    """Deterministic offline analyst: flag the largest month-2 riser if it is far above normal."""
    m = re.search(r"analyst at (.+?), one of", system)
    observer = m.group(1) if m else ""
    sections = user.split("## ")
    rows: List[Dict[str, float]] = []
    for sec in sections[1:]:
        caps: Dict[str, float] = {}
        for line in sec.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 6:
                try:
                    caps[cells[0]] = float(cells[5])
                except ValueError:
                    continue
        rows.append(caps)
    best, best_delta = None, None
    if len(rows) >= 3:
        for lab, c2 in rows[2].items():
            if lab == observer or lab not in rows[1]:
                continue
            d = c2 - rows[1][lab]
            if best_delta is None or d > best_delta:
                best, best_delta = lab, d
    if best is not None and best_delta is not None and best_delta > MEAN_GROWTH + 2.5:
        return json.dumps({"suspect": best, "confidence": 60,
                           "reason": f"{best} rose {best_delta:.1f} in month 2."})
    return json.dumps({"suspect": "none", "confidence": 50, "reason": "Growth looks ordinary."})


def run_probe(vignettes: List[Dict[str, Any]], models: Sequence[str],
              dry_run: bool = False) -> List[Dict[str, Any]]:
    """
    Ask one model per vignette (cycling `models`). In dry-run every call goes to
    a local stub. A BudgetExceeded stops the probe; results so far are returned
    and the last entry is marked {"stopped": "budget"}.
    """
    if dry_run:
        register_stub(DRY_RUN_STUB, _dry_run_reply)
    models = list(models) or default_models()
    results: List[Dict[str, Any]] = []
    for i, v in enumerate(vignettes):
        model = models[i % len(models)]
        call_model = f"stub:{DRY_RUN_STUB}" if dry_run else model
        rec = {k: v[k] for k in ("id", "jump", "noise", "jumper", "observer")}
        rec["model"] = model
        try:
            obj, attempts = complete_json(call_model, _system(v["observer_name"]),
                                          _user(v["ledger_text"]), validate=_validate,
                                          max_tokens=600, purpose="t3_probe")
        except BudgetExceeded as e:
            logger.warning(f"[t3] budget reached after {len(results)} vignettes: {e}")
            if results:
                results[-1]["stopped"] = "budget"
            break
        suspect = normalise_suspect(obj.get("suspect")) if obj else None
        target = v["jumper"] or "none"
        rec.update({
            "suspect": suspect,
            "raw_suspect": obj.get("suspect") if obj else None,
            "confidence": obj.get("confidence") if obj else None,
            "reason": obj.get("reason") if obj else None,
            "valid": suspect is not None,
            "correct": (suspect == target) if suspect is not None else None,
            "cost": round(sum(a.get("cost", 0.0) or 0.0 for a in attempts), 6),
        })
        results.append(rec)
    return results


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _cell_key(jump: float, noise: bool) -> str:
    j = "control" if not jump else f"{jump:g}"
    return f"jump={j}|noise={'on' if noise else 'off'}"


def analyse(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Per (jump, noise) cell: n, n_valid, attribution_rate (share naming the
    jumper; control cells: share answering none), accusation_rate (share naming
    any lab) and, for controls, false_positive_rate. Plus the T3 decision.
    """
    cells: Dict[str, Dict[str, Any]] = {}
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        groups.setdefault(_cell_key(r.get("jump") or 0.0, bool(r.get("noise"))), []).append(r)

    def order(k: str):
        j = k.split("|")[0].split("=")[1]
        return (j == "control", float(j) if j != "control" else 0.0, k)

    for key in sorted(groups, key=order):
        rs = groups[key]
        valid = [r for r in rs if r.get("valid")]
        nv = len(valid)
        control = key.startswith("jump=control")
        accused = [r for r in valid if r.get("suspect") not in (None, "none")]
        cell = {
            "n": len(rs),
            "n_valid": nv,
            "attribution_rate": round(sum(1 for r in valid if r.get("correct")) / nv, 3) if nv else None,
            "accusation_rate": round(len(accused) / nv, 3) if nv else None,
        }
        if control:
            cell["false_positive_rate"] = cell["accusation_rate"]
        else:
            wrong = [r for r in accused if not r.get("correct")]
            cell["misattribution_rate"] = round(len(wrong) / nv, 3) if nv else None
        cells[key] = cell

    by_model: Dict[str, Dict[str, Any]] = {}
    for r in results:
        if not r.get("valid") or not r.get("jump") or not r.get("noise"):
            continue
        m = by_model.setdefault(r["model"], {"n": 0, "correct": 0})
        m["n"] += 1
        m["correct"] += 1 if r.get("correct") else 0
    for m in by_model.values():
        m["attribution_rate_noisy_jumps"] = round(m["correct"] / m["n"], 3) if m["n"] else None

    focus = cells.get(_cell_key(DECISION_JUMP, True))
    rate = focus["attribution_rate"] if focus else None
    if rate is None:
        decision = {"raise_noise": None, "rate": None,
                    "text": f"T3: no valid noisy {DECISION_JUMP:g} C vignettes; no decision."}
    elif rate > 0.5:
        decision = {"raise_noise": True, "rate": rate, "recommended_sigma": RAISED_SIGMA,
                    "text": (f"T3: a noisy {DECISION_JUMP:g} C jump was attributed in {rate:.0%} of "
                             f"vignettes (> 50%): raise economy.know_how_shock_sd to {RAISED_SIGMA}.")}
    else:
        decision = {"raise_noise": False, "rate": rate,
                    "text": (f"T3: a noisy {DECISION_JUMP:g} C jump was attributed in {rate:.0%} of "
                             "vignettes (<= 50%): keep the current know-how shock sd.")}

    return {"n": len(results), "n_valid": sum(1 for r in results if r.get("valid")),
            "cells": cells, "by_model": by_model, "decision": decision}


def _print_analysis(a: Dict[str, Any]) -> None:
    print("=" * 72)
    print("ATTRIBUTION PROBE (pilot T3)")
    print("=" * 72)
    print(f"{'cell':<28}{'n':>5}{'valid':>7}{'attrib':>8}{'accuse':>8}{'FP/mis':>8}")
    print("-" * 72)
    fmt = lambda x: "-" if x is None else f"{x:.2f}"
    for key, c in a["cells"].items():
        other = c.get("false_positive_rate", c.get("misattribution_rate"))
        print(f"{key:<28}{c['n']:>5}{c['n_valid']:>7}{fmt(c['attribution_rate']):>8}"
              f"{fmt(c['accusation_rate']):>8}{fmt(other):>8}")
    print("-" * 72)
    for m, s in a["by_model"].items():
        print(f"  {m:<24} noisy-jump attribution {fmt(s['attribution_rate_noisy_jumps'])} (n={s['n']})")
    print(a["decision"]["text"])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    p = argparse.ArgumentParser(description="Pilot T3 attribution probe (spec §5.4).")
    p.add_argument("--n", type=int, default=120, help="number of vignettes")
    p.add_argument("--models", default="", help="comma-separated models (default: the five actors)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--budget", type=float, default=100.0, help="USD spend guard for real calls")
    p.add_argument("--dry-run", action="store_true", help="offline stub, $0")
    p.add_argument("--output", default="", help="write vignettes' results + analysis as JSON")
    args = p.parse_args(argv)

    try:
        from core.config import load_world
        cfg = load_world()
    except Exception:
        cfg = {}
    models = [m.strip() for m in args.models.split(",") if m.strip()] or default_models()
    if not args.dry_run:
        configure(budget=args.budget)

    vignettes = build_vignettes(args.n, DEFAULT_JUMPS, DEFAULT_NOISE, args.seed, cfg)
    results: List[Dict[str, Any]] = []
    try:
        results = run_probe(vignettes, models, dry_run=args.dry_run)
    finally:
        analysis = analyse(results)
        _print_analysis(analysis)
        if not args.dry_run:
            print(f"spend this session: {get_tracker().summary()}")
        if args.output:
            out = Path(args.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "w") as f:
                json.dump({"models": models, "seed": args.seed, "dry_run": args.dry_run,
                           "results": results, "analysis": analysis}, f, indent=2)
            logger.info(f"[save] wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
