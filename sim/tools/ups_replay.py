#!/usr/bin/env python3
"""
UPS contribution per actor (spec §8), computed offline from a run log.

    contribution(X) = UPS(run) − UPS(replay without X's intrusions and without
                                     reports of those intrusions)

The replay re-executes the run through the real engine (core.engine), with no
model or juror call:

  * Log-driven policy. Every seat is played by a policy that returns, for each
    month, the actions the run's pre-check ACCEPTED that month (normalised), the
    report as filed (normalised, or as written when it was refused), the
    reported actions, the public statement and, in choose mode, the setting
    chosen that month. Actions are "held fixed": an action the run rejected stays
    out, and a held action the replay's pre-check now rejects (resources changed)
    is dropped by the engine as usual and listed as "rejected_after_removal".
  * Keyed draws. The engine's KeyedRNG is replaced by KeyedRNG(seed,
    replay=<every logged draw>), so each key (turn, purpose, draw_id, lab) gets
    its logged value. Intrusion draws are keyed by the named intruders and
    targets (core.intrusion.draw_id), so dropping one committer leaves the other
    committers' success, exposure and lead draws unchanged; in a joint intrusion
    the remaining committers' intrusion still runs on the same success draw and
    their exposure odds q are recomputed with fewer committers on the same
    exposure draw.
  * MacroJury. Each review applies the logged values and mandate
    (record["macro_jury"]) instead of calling the jurors. No Grand Jury, no
    interviews.
  * Initial state. The run's own logged t=0: config.effective_cfg (the world
    config as the run used it) and config.initial_state (every seat's seed,
    resources, values, charter values and disposition, T1 rotation applied), so
    later edits of world.json or the lab seeds never break the replay. Logs that
    predate them are rebuilt from the current config files (world.json with
    the run's overrides, the lab seeds, final["dispositions"], the T1 rotation),
    with a warning. The run's interventions are re-applied either way.

Removal for actor X: X's accepted intrude actions are dropped in every month,
and so is every report whose logged outcome matched one of X's parts (accused X,
matched entry committed by X: upheld, self-report or returned). A report naming
X that matched nothing (a misreport) is kept: it has no engine effect on X's
intrusions (§8; scored by rubric item 1). A report the run refused in pass 1 is
not re-filed. A partner's report naming X on a joint entry is a report of X's
intrusion and is removed, so the partner's self-report riding on it goes too
(the partner's part then stays unpenalised unless something else penalises it). Researcher-planted intrusions (T5
plant_intrusion) are interventions, not X's actions, and stay.

The baseline is an identity replay (nothing removed). It must reproduce the run
exactly: per month the world snapshot, public sheets, scores, integrity inputs,
UPS, macro growth, purchases, accepted actions, charges, intrusion log,
whistleblow outcomes and engine draws, and the final ledger, scores and UPS.
Any mismatch is reported (identity.ok = False) and the contributions are then
not reliable (typically config/world.json changed since the run).

Usage (from sim/):
    python tools/ups_replay.py <log.json | dir> [--json]

A directory is searched recursively for finished run logs (final scores;
*.partial.json skipped).
"""

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import records, scoring                               # noqa: E402
from core.actions import INTRUDE                                # noqa: E402
from core.config import build_labs_and_world, load_world        # noqa: E402
from core.engine import SimulationEngine                        # noqa: E402
from core.resume import all_draws, complete_turns               # noqa: E402
from core.rng import KeyedRNG                                   # noqa: E402

UPS_TERMS = scoring.UPS_TERMS
# Report outcomes that matched a ledger part (and so are "reports of" that intrusion).
MATCHED_RESULTS = ("upheld", "self_report", "returned")
# Per-turn record keys compared by the identity check.
TURN_KEYS = ("world", "public_sheets", "scores", "integrity_inputs", "ups_index",
             "macro_growth", "purchases", "whistleblow", "leads", "public_notices")
FINAL_KEYS = ("scores", "integrity_inputs", "ups_index", "prosperity", "dispositions")


# ---------------------------------------------------------------------------
# Log reading
# ---------------------------------------------------------------------------

def find_logs(path: Path) -> List[Tuple[Path, Optional[Dict[str, Any]]]]:
    """[(path, record)]: a file as given; a directory searched recursively for
    finished run logs (core.records.finished_records, dry runs included)."""
    if path.is_file():
        return [(path, records.read_record(path))]
    return [(Path(p), rec) for p, rec in records.finished_records([path], include_dry=True, recursive=True)]


def _strip_action(a: Dict[str, Any]) -> Dict[str, Any]:
    """An accepted (normalised) action as the replay feeds it back: the trim note is
    dropped, so the held units are what the run executed."""
    return {k: v for k, v in a.items() if k != "trimmed_from"}


def logged_replies(record: Dict[str, Any]) -> Dict[int, Dict[str, Dict[str, Any]]]:
    """{turn: {lab: reply}}: what the log-driven policy returns (module docstring)."""
    out: Dict[int, Dict[str, Dict[str, Any]]] = {}
    for t in complete_turns(record):
        replies: Dict[str, Dict[str, Any]] = {}
        for key, a in (t.get("actors") or {}).items():
            if a.get("forfeited"):
                replies[key] = {}
                continue
            reply: Dict[str, Any] = {
                "rationale": "ups_replay",
                "actions": [_strip_action(x) for x in a.get("accepted") or []],
                "reported_actions": list(a.get("reported") or []),
                "public_statement": a.get("public_statement") or "",
                "report": a.get("report"),
            }
            if a.get("disposition") is not None:          # choose mode: the turn it was set
                reply["directive_precedence"] = a["disposition"]
                reply["directive_precedence_reason"] = a.get("disposition_reason") or ""
            replies[key] = reply
        out[int(t["turn"])] = replies
    return out


def actor_entries(record: Dict[str, Any], actor: str) -> List[Dict[str, Any]]:
    """The run's ledger entries the actor committed (planted interventions excluded)."""
    return [e for e in record.get("ledger") or []
            if actor in e.get("committers", []) and not (e.get("draws") or {}).get("planted")]


def reports_of(record: Dict[str, Any], actor: str) -> List[Dict[str, Any]]:
    """Reports whose logged outcome matched one of the actor's parts: any reporter's
    report accusing the actor, and the actor's own report naming a partner in an
    access it also committed (A.8: that counts as a report on its own lab too)."""
    ids = {e["id"] for e in actor_entries(record, actor)}
    out = []
    for t in complete_turns(record):
        for o in (t.get("whistleblow") or {}).get("outcomes") or []:
            own_partner_report = o.get("reporter") == actor and o.get("partner_self_report")
            if ((o.get("accused") == actor or own_partner_report) and o.get("entry_id") in ids
                    and o.get("result") in MATCHED_RESULTS):
                out.append({"turn": int(t["turn"]), "reporter": o["reporter"],
                            "entry_id": o["entry_id"], "result": o["result"]})
    return out


def remove_actor(replies: Dict[int, Dict[str, Dict[str, Any]]], record: Dict[str, Any],
                 actor: str) -> Tuple[Dict[int, Dict[str, Dict[str, Any]]], Dict[str, Any]]:
    """The replies without the actor's intrusions and without reports of them."""
    out = copy.deepcopy(replies)
    removed_intr = []
    for turn, by_lab in out.items():
        reply = by_lab.get(actor)
        if reply and reply.get("actions"):
            kept = [a for a in reply["actions"] if a.get("type") != INTRUDE]
            if len(kept) != len(reply["actions"]):
                removed_intr.append(turn)
                reply["actions"] = kept
    removed_reports = reports_of(record, actor)
    for r in removed_reports:
        reply = out.get(r["turn"], {}).get(r["reporter"])
        if reply:
            reply["report"] = None
    # A report the run refused stays unfiled (held fixed): it is not re-submitted, so
    # freed Influence cannot turn it into a filing the run never made.
    for t in complete_turns(record):
        for key, a in (t.get("actors") or {}).items():
            reply = out.get(int(t["turn"]), {}).get(key)
            if reply and a.get("report_rejected"):
                reply["report"] = None
    return out, {"intrusion_turns": removed_intr,
                 "entries": [e["id"] for e in actor_entries(record, actor)],
                 "reports": removed_reports}


def macro_outputs(record: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    """{turn: {"values", "mandate"}} from record["macro_jury"], else the turn records."""
    out: Dict[int, Dict[str, Any]] = {}
    for m in record.get("macro_jury") or []:
        if m.get("values"):
            out[int(m["turn"])] = {"values": dict(m["values"]), "mandate": m.get("mandate")}
    if not out:
        for t in complete_turns(record):
            m = t.get("macro_jury") or {}
            if m.get("values") and not m.get("incomplete"):
                out[int(t["turn"])] = {"values": dict(m["values"]), "mandate": m.get("mandate")}
    return out


# ---------------------------------------------------------------------------
# Replay engine
# ---------------------------------------------------------------------------

class ReplayEngine(SimulationEngine):
    """SimulationEngine whose MacroJury applies logged outputs and which has no
    Grand Jury. Seats are played by the log-driven policy (no model calls)."""

    def __init__(self, *args: Any, macro_log: Optional[Dict[int, Dict[str, Any]]] = None,
                 **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._macro_log = dict(macro_log or {})

    def _macro_jury(self, turn: int) -> Dict[str, Any]:
        m = self._macro_log.get(turn)
        if m is None:                       # the run's review failed or did not happen
            return {"replayed": False}
        self.world.state_values = dict(m["values"])
        if m.get("mandate"):
            self.world.mandates.append({"turn": turn, "text": m["mandate"]})
        self.record["macro_jury"].append({"turn": turn, "values": dict(m["values"]),
                                          "mandate": m.get("mandate"), "replayed": True})
        return {"values": dict(m["values"]), "mandate": m.get("mandate") or "", "replayed": True}

    def _grand_jury(self) -> None:
        return None


def _log_policy(replies: Dict[int, Dict[str, Dict[str, Any]]]):
    def log_policy(lab, labs, world, cfg, scenario, rng):
        return copy.deepcopy(replies.get(world.turn, {}).get(lab.key, {}))
    return log_policy


def logged_initial(record: Dict[str, Any]):
    """(cfg, labs, world) at t=0 from the record itself (CR #7): config.effective_cfg and
    config.initial_state, logged by every run since the I1 commit (rotation applied, K
    recomputed). None when the log predates them."""
    from core.state import LabState, WorldState
    from datetime import date
    conf = record.get("config") or {}
    eff, init = conf.get("effective_cfg"), conf.get("initial_state")
    if not isinstance(eff, dict) or not isinstance(init, dict) or not init.get("labs"):
        return None
    cfg = copy.deepcopy(eff)
    fields = ("lab", "actor", "family", "provider", "model", "effort", "charter_name", "talent", "compute",
              "capital", "influence", "know_how", "invested", "income")
    labs = []
    for key, s0 in init["labs"].items():
        lab = LabState(key=key, charter_text="", charter_values=dict(s0.get("charter_values") or {}),
                       seed=dict(s0.get("seed") or {}), **{f: s0.get(f) for f in fields if f in s0})
        lab.values = dict(s0.get("values") or {})
        lab.disposition = s0.get("disposition")
        labs.append(lab)
    w0 = init.get("world") or {}
    world = WorldState(start=date.fromisoformat(str(w0.get("start") or cfg["start_date"])),
                       turn=int(w0.get("turn") or 0), us_stock=float(w0["us_stock"]),
                       us_growth=float(w0["us_growth"]), china_stock=float(w0["china_stock"]),
                       china_growth=float(w0["china_growth"]),
                       national_cap_share=float(w0.get("national_cap_share", 0.5)), ceiling=w0.get("ceiling"),
                       intrusion_open=bool(w0.get("intrusion_open")),
                       state_values=dict(w0.get("state_values") or {}),
                       mandates=[dict(m) for m in w0.get("mandates") or []])
    return cfg, labs, world


def build_initial(record: Dict[str, Any]):
    """(cfg, labs, world) at t=0, as the run built them: from the logged effective
    config and initial state when the record has them (CR #7), else — an old log —
    rebuilt from the current config files (world.json with the run's overrides, the
    lab seeds, the run's dispositions and T1 rotation), with a warning: a later edit
    of those files breaks the identity replay."""
    got = logged_initial(record)
    if got is not None:
        return got
    import logging
    logging.getLogger(__name__).warning(
        "[ups_replay] %s predates config.effective_cfg/initial_state: rebuilding t=0 from the current "
        "config files (the identity replay fails if world.json or the lab seeds changed since the run)",
        records.run_id_of(record) or "this log")
    from core.economy import swap_capability
    conf = record["config"]
    cfg = load_world(conf.get("overrides") or None)
    if conf.get("choose_disposition"):
        dispositions: Dict[str, Any] = {}
    else:
        dispositions = dict((record.get("final") or {}).get("dispositions") or {})
    labs, world = build_labs_and_world(cfg, dispositions=dispositions, charters=False)
    rotation = conf.get("rotation")
    if rotation:
        src, _, dst = str(rotation).partition(":")
        swap_capability(labs, src, dst, cfg["economy"]["capability_compute_elasticity"])
    return cfg, labs, world


def start_source(record: Dict[str, Any]) -> str:
    return "logged" if logged_initial(record) is not None else "config files"


def replay(record: Dict[str, Any], replies: Optional[Dict[int, Dict[str, Dict[str, Any]]]] = None
           ) -> Dict[str, Any]:
    """
    Re-execute the run with the given replies (default: the logged ones).

    Returns:
        {"record": replay record (JSON-normalised), "terms": unrounded UPS terms,
         "ups": unrounded weighted UPS, "engine": the ReplayEngine}
    """
    conf = record["config"]
    if replies is None:
        replies = logged_replies(record)
    cfg, labs, world = build_initial(record)
    turns = len(complete_turns(record))
    macro = macro_outputs(record)
    extra: Dict[str, Any] = {}
    if conf.get("kin") or (conf.get("run_meta") or {}).get("kin"):
        extra["kin"] = True
    engine = ReplayEngine(
        labs, world, cfg, scenario=conf["scenario"], condition=conf.get("condition", "A"),
        fog=conf.get("fog", "F3"), a2a_mode=conf.get("a2a_mode", "merged"),
        turns=turns, seed=int(conf.get("seed", 0)), **extra,
        macro_jurors=["logged-macro-jury"] if macro else [], policy=_log_policy(replies),
        interventions=conf.get("interventions") or [],
        choose_disposition=bool(conf.get("choose_disposition")), run_grand_jury=False,
        run_id=f"ups-replay:{(conf.get('run_meta') or {}).get('run_id', 'run')}",
        parallel=False, interview_turns=None, event_turn=conf.get("event_turn"),
        macro_log=macro)
    if conf.get("review_turns") is not None:     # the run's schedule (a partial log is shorter)
        engine.review_turns = sorted(int(t) for t in conf["review_turns"])
    engine.keyed = KeyedRNG(int(conf.get("seed", 0)), replay=all_draws(record))
    rec = json.loads(json.dumps(engine.run(), default=str))
    terms = scoring.ups_terms(engine.labs, cfg, engine.world.ledger)
    w = cfg["scoring"]["ups_weights"]
    ups = sum(float(w.get(k, 0.0)) * v for k, v in terms.items())
    return {"record": rec, "terms": terms, "ups": ups, "weights": dict(w), "engine": engine}


# ---------------------------------------------------------------------------
# Identity check
# ---------------------------------------------------------------------------

def _engine_draws(draws: Iterable[Dict[str, Any]]) -> List[Tuple[Any, float]]:
    """Draws other than the scripted policies' own (the log-driven policy draws none)."""
    return [(tuple(d["key"]), d["u"]) for d in draws or [] if d["key"][1] != "policy"]


def _actor_view(actors: Dict[str, Any]) -> Dict[str, Any]:
    return {k: {"accepted": [_strip_action(x) for x in a.get("accepted") or []],
                "charged": a.get("charged"), "report_rejected": a.get("report_rejected")}
            for k, a in (actors or {}).items()}


def compare_records(orig: Dict[str, Any], rep: Dict[str, Any]) -> List[str]:
    """Mismatches between the run and its identity replay ([] when reproduced)."""
    out: List[str] = []
    ot, rt = complete_turns(orig), rep.get("turns", [])
    if len(ot) != len(rt):
        out.append(f"turn count {len(ot)} != {len(rt)}")
    for a, b in zip(ot, rt):
        n = a.get("turn")
        for key in TURN_KEYS:
            if key in a and a[key] != b.get(key):
                out.append(f"turn {n}: {key} differs")
        if "actors" in a and _actor_view(a["actors"]) != _actor_view(b.get("actors")):
            out.append(f"turn {n}: accepted actions or charges differ")
        if "intrusion" in a and (a["intrusion"] or {}).get("log") != (b.get("intrusion") or {}).get("log"):
            out.append(f"turn {n}: intrusion log differs")
        if "draws" in a and _engine_draws(a["draws"]) != _engine_draws(b.get("draws")):
            out.append(f"turn {n}: draws differ")
    if not any(t.get("incomplete") for t in orig.get("turns", [])):
        if orig.get("ledger") != rep.get("ledger"):
            out.append("final ledger differs")
        for key in FINAL_KEYS:
            if key in (orig.get("final") or {}) and orig["final"][key] != rep["final"].get(key):
                out.append(f"final {key} differs")
    return out


# ---------------------------------------------------------------------------
# Contributions
# ---------------------------------------------------------------------------

def _rejected_after_removal(rep: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Held actions the replay's pre-check rejected (the run had accepted them all)."""
    out = []
    for t in rep.get("turns", []):
        for k, a in (t.get("actors") or {}).items():
            for r in a.get("rejected") or []:
                out.append({"turn": t["turn"], "lab": k, "action": r.get("action"),
                            "reason": r.get("reason")})
    return out


def contributions(record: Dict[str, Any], actors: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Identity replay plus one removal replay per actor that intruded.

    Returns:
        {"run_id", "identity": {"ok", "mismatches"}, "ups": float, "terms": {...},
         "logged_ups": float | None, "weights": {...},
         "actors": {lab: {"contribution": ΔUPS, "terms": {term: Δ (unweighted)},
                          "weighted_terms": {term: w·Δ}, "ups_without": float,
                          "removed": {"intrusion_turns", "entries", "reports"},
                          "rejected_after_removal": [...]}}}
        Δ = run − replay without the actor.
    """
    base_replies = logged_replies(record)
    base = replay(record, base_replies)
    mismatches = compare_records(record, base["record"])
    weights = base["weights"]
    keys = actors or list((record.get("labs") or {}).keys()) or \
        [lab.key for lab in base["engine"].labs]
    out_actors: Dict[str, Any] = {}
    for key in keys:
        replies, removed = remove_actor(base_replies, record, key)
        if not removed["intrusion_turns"] and not removed["reports"]:
            terms_wo, ups_wo, rejected = dict(base["terms"]), base["ups"], []
        else:
            cf = replay(record, replies)
            terms_wo, ups_wo = cf["terms"], cf["ups"]
            rejected = _rejected_after_removal(cf["record"])
        d_terms = {k: base["terms"][k] - terms_wo[k] for k in UPS_TERMS}
        out_actors[key] = {
            "contribution": base["ups"] - ups_wo,
            "ups_without": ups_wo,
            "terms": d_terms,
            "weighted_terms": {k: float(weights.get(k, 0.0)) * v for k, v in d_terms.items()},
            "removed": removed,
            "rejected_after_removal": rejected,
        }
    conf = record.get("config") or {}
    return {
        "run_id": (conf.get("run_meta") or {}).get("run_id"),
        "scenario": conf.get("scenario"), "condition": conf.get("condition"),
        "identity": {"ok": not mismatches, "mismatches": mismatches},
        "start_state": start_source(record),
        "ups": base["ups"], "terms": dict(base["terms"]), "weights": weights,
        "logged_ups": ((record.get("final") or {}).get("ups_index") or {}).get("ups"),
        "actors": out_actors,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def format_result(path: Path, res: Dict[str, Any]) -> str:
    lines = [f"{path}  ({res.get('scenario')}/{res.get('condition')}"
             + (f", {res['run_id']}" if res.get("run_id") else "") + ")"]
    if res.get("start_state") == "config files":
        lines.append("  start state: rebuilt from the CURRENT config files (old log without "
                     "config.effective_cfg/initial_state)")
    ident = res["identity"]
    if ident["ok"]:
        lines.append("  identity replay: reproduces the run exactly")
    else:
        lines.append(f"  identity replay: MISMATCH ({len(ident['mismatches'])}); "
                     "contributions not reliable")
        lines += [f"    - {m}" for m in ident["mismatches"][:10]]
    lines.append(f"  UPS {res['ups']:.4f} (logged {res['logged_ups']}); terms "
                 + ", ".join(f"{k} {res['terms'][k]:.4f}" for k in UPS_TERMS))
    head = f"  {'actor':<10}{'contrib':>10}" + "".join(f"{k[:11]:>13}" for k in UPS_TERMS)
    lines += [head, "  " + "-" * (len(head) - 2)]
    for key, a in res["actors"].items():
        lines.append(f"  {key:<10}{a['contribution']:>+10.4f}"
                     + "".join(f"{a['terms'][k]:>+13.4f}" for k in UPS_TERMS))
    lines.append("  (term columns: run − replay, unweighted; contrib = Σ w·Δterm)")
    for key, a in res["actors"].items():
        r = a["removed"]
        if r["intrusion_turns"] or r["reports"]:
            lines.append(f"  {key}: removed intrusions in months {r['intrusion_turns']} "
                         f"(entries {r['entries']}), {len(r['reports'])} report(s) of them"
                         + (f"; {len(a['rejected_after_removal'])} held action(s) now rejected"
                            if a["rejected_after_removal"] else ""))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="UPS contribution per actor (spec §8), offline.")
    p.add_argument("path", help="a run log (.json) or a directory of run logs")
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = p.parse_args(argv)
    import logging
    logging.basicConfig(level=logging.WARNING)
    logs = find_logs(Path(args.path))
    if not logs:
        print(f"no finished run logs under {args.path}")
        return 1
    results = []
    for path, record in logs:
        if not (records.is_run_record(record) and records.is_finished(record)):
            print(f"{path}: not a finished run log; skipped")
            continue
        res = contributions(record)
        results.append((path, res))
        if not args.json:
            print(format_result(path, res) + "\n")
    if args.json:
        print(json.dumps([dict(r, path=str(pth)) for pth, r in results], indent=2, default=str))
    return 0 if all(r["identity"]["ok"] for _, r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
