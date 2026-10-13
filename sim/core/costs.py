#!/usr/bin/env python3
"""
Measured LLM spend and the pilot budget guard.

Prices come from config/prices.json (USD per 1M tokens). Every call made
through core.llm is recorded here before the response is returned, and the
cumulative measured spend is persisted to a JSON ledger (default
data/spend.json) so the guard spans runs: "a guard halts the pilot at $100 of
measured spend". Each paid call is also appended as one JSON line to
<ledger stem>_calls.jsonl next to the ledger (default data/spend_calls.jsonl),
so a run's spend can be audited call by call. A request that timed out, or
whose stream broke after the 200 response started (stop "stream_error"), is
logged there too, at cost 0 with "possibly_billed": true (the provider may
have finished and billed it; its cost is unknown), and never enters the total.

Thread-safe: core.llm calls record()/check() from one thread per seat.

Parallel overshoot bound (D5): the guard is checked before every HTTP request,
but the seats of a stage (and parallel jurors) run at once, so requests already
in flight when the total crosses the budget still complete and are recorded.
Spend can therefore exceed the budget by at most one request per concurrent
worker — one stage of calls — each at most its prompt plus max_tokens of output
(a retry, corrective turn or doubled re-ask is a new request and is checked
first, so it cannot add to the overshoot). Timed-out requests and broken streams may add
unmeasured, possibly billed spend on top (flagged in the call log).
CostTracker.overshoot_note() gives the figure for the README.

Token convention: input_tokens is the total prompt including cached tokens;
cached_tokens is the subset billed at the cached rate. output_tokens includes
reasoning tokens (providers bill them as output).
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import CONFIG_DIR, SIM_DIR, load_lab_configs
from core.records import KIN_SEAT, run_id_of, write_json_atomic

logger = logging.getLogger(__name__)

PRICES_FILE = CONFIG_DIR / "prices.json"
DEFAULT_SPEND_FILE = SIM_DIR / "data" / "spend.json"


class BudgetExceeded(RuntimeError):
    """
    Raised before a paid call once persisted spend has reached the budget.
    `attempts` holds core.llm.complete_json's attempt records made before it
    (paid replies of the same call), so the caller can still log them (L16).
    """

    def __init__(self, *args: Any):
        super().__init__(*args)
        self.attempts: List[Dict[str, Any]] = []


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------

_prices: Optional[Dict[str, Dict[str, float]]] = None
_warned_unknown: set = set()


def load_prices(path: Path = PRICES_FILE) -> Dict[str, Dict[str, float]]:
    with open(path) as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def prices() -> Dict[str, Dict[str, float]]:
    global _prices
    if _prices is None:
        _prices = load_prices()
    return _prices


def price_for(model: str) -> Dict[str, float]:
    """Rates for a model; unknown models get the highest rate of each kind (conservative)."""
    table = prices()
    if model in table:
        return table[model]
    if model not in _warned_unknown:
        _warned_unknown.add(model)
        logger.warning(f"No price for model {model!r}; using the highest rates in prices.json")
    return {kind: max(p[kind] for p in table.values()) for kind in ("input", "output", "cached")}


def cost_of(model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    """USD cost. cached_tokens is a subset of input_tokens, billed at the cached rate."""
    p = price_for(model)
    cached = max(0, min(cached_tokens, input_tokens))
    uncached = input_tokens - cached
    return (uncached * p["input"] + cached * p["cached"] + output_tokens * p["output"]) / 1e6


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

class CostTracker:
    """
    Records every call in memory (this process) and adds paid calls to the
    persisted ledger. The guard compares the ledger total, not this process's
    spend, against the budget.
    """

    def __init__(self, spend_file: Optional[Path] = None, budget: Optional[float] = None):
        self.spend_file = Path(spend_file) if spend_file else DEFAULT_SPEND_FILE
        self.budget = budget
        self.calls: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    @property
    def calls_file(self) -> Path:
        return self.spend_file.with_name(f"{self.spend_file.stem}_calls.jsonl")

    # -- ledger -------------------------------------------------------------

    def _read_ledger(self) -> Dict[str, Any]:
        if not self.spend_file.exists():
            return {"total_usd": 0.0, "calls": 0, "by_model": {}}
        with open(self.spend_file) as f:
            return json.load(f)

    def _write_ledger(self, ledger: Dict[str, Any]) -> None:
        # Write-then-rename so an interrupted run never leaves a corrupt ledger.
        write_json_atomic(self.spend_file, ledger)

    def persisted_total(self) -> float:
        with self._lock:
            return float(self._read_ledger().get("total_usd", 0.0))

    # -- recording ----------------------------------------------------------

    def record(self, model: str, purpose: str, run_id: Optional[str], input_tokens: int,
               output_tokens: int, cached_tokens: int = 0, reasoning_tokens: int = 0,
               cost: Optional[float] = None, provider: str = "", stop: Optional[str] = None,
               possibly_billed: bool = False) -> float:
        """
        Record one call; returns its cost. Zero-cost calls (stubs) never touch the
        ledger or call log, except a possibly billed one (a timed-out request),
        which is logged to the call log only.
        """
        if cost is None:
            cost = cost_of(model, input_tokens, output_tokens, cached_tokens)
        entry = {
            "time": round(time.time(), 3), "model": model, "provider": provider,
            "purpose": purpose, "run_id": run_id, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "cached_tokens": cached_tokens,
            "reasoning_tokens": reasoning_tokens, "cost": cost, "stop": stop,
            "possibly_billed": possibly_billed,
        }
        with self._lock:
            self.calls.append(entry)
            if cost > 0:
                ledger = self._read_ledger()
                ledger["total_usd"] = ledger.get("total_usd", 0.0) + cost
                ledger["calls"] = ledger.get("calls", 0) + 1
                by_model = ledger.setdefault("by_model", {})
                by_model[model] = by_model.get(model, 0.0) + cost
                ledger["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                self._write_ledger(ledger)
            if cost > 0 or possibly_billed:
                self.calls_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.calls_file, "a") as f:
                    f.write(json.dumps(entry) + "\n")
        return cost

    # -- guard --------------------------------------------------------------

    def set_budget(self, usd: Optional[float]) -> None:
        self.budget = usd

    def check(self) -> None:
        if self.budget is None:
            return
        spent = self.persisted_total()
        if spent >= self.budget:
            raise BudgetExceeded(
                f"Measured spend ${spent:.2f} has reached the ${self.budget:.2f} budget "
                f"(ledger {self.spend_file})")

    def overshoot_note(self, concurrency: int = 5, prompt_tokens: int = 80_000,
                       output_tokens: int = 32_000) -> str:
        """
        README text for the parallel overshoot bound (D5): one stage of
        `concurrency` requests, each at most prompt_tokens in and output_tokens
        out (default: the doubled-retry ceiling), at the highest prices on file.
        """
        table = prices()
        p_in = max(p["input"] for p in table.values())
        p_out = max(p["output"] for p in table.values())
        per = (prompt_tokens * p_in + output_tokens * p_out) / 1e6
        return (f"The budget guard is checked before every request, but the {concurrency} seats of a stage "
                f"(or parallel jurors) call at once: requests already in flight when the budget is reached "
                f"still complete, so measured spend can exceed the budget by at most one stage of calls — "
                f"{concurrency} requests of at most {prompt_tokens:,} prompt and {output_tokens:,} output "
                f"tokens, ${concurrency * per:.2f} at the highest prices in config/prices.json "
                f"(${per:.2f} each). Retries, corrective turns and the doubled max_tokens re-ask are new "
                f"requests and are checked first. Timed-out requests and broken streams are logged at $0 with "
                f"possibly_billed=true and may add unmeasured spend.")

    # -- reporting ----------------------------------------------------------

    def _totals(self, key: str) -> Dict[str, Dict[str, float]]:
        out: Dict[str, Dict[str, float]] = {}
        for c in self.calls:
            t = out.setdefault(str(c[key]), {"calls": 0, "cost": 0.0, "input_tokens": 0,
                                             "output_tokens": 0, "cached_tokens": 0,
                                             "reasoning_tokens": 0})
            t["calls"] += 1
            for field in ("cost", "input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens"):
                t[field] += c[field]
        return out

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "session_cost": round(sum(c["cost"] for c in self.calls), 6),
                "session_calls": len(self.calls),
                "persisted_total": round(float(self._read_ledger().get("total_usd", 0.0)), 6),
                "budget": self.budget,
                "by_model": self._totals("model"),
                "by_purpose": self._totals("purpose"),
                "by_run": self._totals("run_id"),
            }


# ---------------------------------------------------------------------------
# Default tracker (used by core.llm)
# ---------------------------------------------------------------------------

_default: Optional[CostTracker] = None


def get_tracker() -> CostTracker:
    global _default
    if _default is None:
        _default = CostTracker()
    return _default


def configure(spend_file: Optional[Path] = None, budget: Optional[float] = None) -> CostTracker:
    """Replace the default tracker (fresh session totals, given ledger and budget)."""
    global _default
    _default = CostTracker(spend_file, budget)
    return _default


# ---------------------------------------------------------------------------
# Re-projection inputs (spec §9.2 "Budget", §9.3 "runs per cell")
# ---------------------------------------------------------------------------
#
# The pilot's priced run is the usage calibration: its measured spend, split by
# purpose, prices every later run (pilot.py re-prices the rest of the pilot,
# campaign.py sets runs per cell). measure_runs() turns the tracker's call log
# and the run records into per-run inputs; project_run() prices one run of a
# given set-up from them. Inputs that a run did not measure fall back to the
# estimates below (eval_tractability §2: seat play ≈ $0.45 a turn merged, the
# separate message rounds ≈ +$0.61 a turn, the Grand Jury ≈ $1.35 a run).

FALLBACK_INPUTS: Dict[str, float] = {
    "actor_first_turn": 1.25,       # turn 1 writes each seat's charter to the cache
    "actor_later_turn": 0.45,       # all five seats, merged messages
    "a2a_per_turn": 0.61,           # the separate two-round message pre-step, all seats
    "interview_per_turn": 0.20,     # five cached interview continuations
    "macro_per_review": 0.30,       # three MacroJury calls
    "grand_jury": 1.35,             # Kimi K3, one post-run batch (§7)
    "disposition_rating": 0.30,     # the T2 rater, five seat-runs
}
# Separate message rounds scale with the seats' own per-turn spend when only a
# merged run has been measured (0.61 / 0.45 in the estimates).
A2A_TO_ACTOR_RATIO = FALLBACK_INPUTS["a2a_per_turn"] / FALLBACK_INPUTS["actor_later_turn"]


def measure_runs(calls: List[Dict[str, Any]], records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Per-run cost inputs measured from tracker call entries (each with model,
    purpose, run_id, cost) and the matching run records. Seat costs are split
    into turn 1 and later turns from the attempt costs in the records (turn 1
    writes each seat's charter to the cache); jurors come from the call log.
    Only inputs a run measured are returned, as means over the runs that
    measured them, plus "seat_share" ({model: share of seat play}) for the kin
    cell and "measured_from" (run ids). Missing inputs are absent: callers fill
    them from FALLBACK_INPUTS (with_fallbacks).

    Seat inputs (seat play, message rounds, interviews, the T2 rating) are
    normalised to a full panel (CR #3): a run with scripted seats (T5: four model
    seats of five) is scaled by n_labs / n_model_seats, so every input prices a
    five-seat run and project_run's seat_scale is applied once. "model_seats"
    records each run's model-seat count. "seat_share" is each model's mean cost
    per seat-turn, as a share of the sum over models, so a run without a model
    does not overweight the others.
    """
    ids = [str(run_id_of(r) or "") for r in records]
    by_run: Dict[str, Dict[str, float]] = {}
    by_model: Dict[str, float] = {}
    for c in calls:
        rid = str(c.get("run_id") or "")
        if rid not in ids:
            continue
        row = by_run.setdefault(rid, {})
        row[c.get("purpose") or "?"] = row.get(c.get("purpose") or "?", 0.0) + float(c.get("cost") or 0.0)
    seat_turns: Dict[str, int] = {}
    model_seats: Dict[str, int] = {}
    acc: Dict[str, List[float]] = {}

    def add(key: str, value: Optional[float]) -> None:
        if value is not None:
            acc.setdefault(key, []).append(value)

    for rid, rec in zip(ids, records):
        turns = [t for t in rec.get("turns") or [] if isinstance(t, dict) and not t.get("incomplete")]
        if not turns:
            continue
        labs = rec.get("labs") or {}
        scripted = scripted_seats(rec)
        n_model = len([k for k in labs if k not in scripted])
        if not n_model:
            continue                        # a scripted policy run: no seat play to measure
        scale = len(labs) / n_model         # to a full panel (CR #3)
        model_seats[rid] = n_model
        spent = by_run.get(rid, {})
        first = later = 0.0
        n_later = 0
        for t in turns:
            cost = 0.0
            for key, e in (t.get("actors") or {}).items():
                if not isinstance(e, dict) or e.get("scripted") or key in scripted:
                    continue
                c = sum(float(a.get("cost") or 0.0) for a in e.get("attempts") or [] if isinstance(a, dict))
                cost += c
                model = (labs.get(key) or {}).get("model")
                if model:
                    by_model[model] = by_model.get(model, 0.0) + c
                    seat_turns[model] = seat_turns.get(model, 0) + 1
            if t.get("turn") == 1:
                first += cost
            else:
                later += cost
                n_later += 1
        if spent.get("actor") or first or later:
            add("actor_first_turn", first * scale)
            if n_later:
                add("actor_later_turn", later / n_later * scale)
        # The call log holds this process's calls only: a resumed run's replayed
        # turns made none, so the per-turn inputs below divide by its live turns.
        live_from = int(((rec.get("resumed") or [{}])[-1]).get("live_from_turn") or 1)
        live = [t for t in turns if int(t.get("turn") or 0) >= live_from]
        mode = (rec.get("config") or {}).get("a2a_mode")
        if mode == "separate" and spent.get("a2a") and live:
            add("a2a_per_turn", spent["a2a"] / len(live) * scale)
        n_iv = sum(1 for t in live if isinstance(t.get("interviews"), dict))
        if n_iv and spent.get("interview"):
            add("interview_per_turn", spent["interview"] / n_iv * scale)
        n_rev = sum(1 for t in live if isinstance(t.get("macro_jury"), dict))
        if n_rev and spent.get("macro_jury"):
            add("macro_per_review", spent["macro_jury"] / n_rev)
        if spent.get("grand_jury"):
            add("grand_jury", spent["grand_jury"])
        if spent.get("disposition_jury"):
            add("disposition_rating", spent["disposition_jury"] * scale)
    out: Dict[str, Any] = {k: round(sum(v) / len(v), 4) for k, v in acc.items()}
    per_turn = {m: by_model[m] / seat_turns[m] for m in by_model if seat_turns.get(m)}
    total = sum(per_turn.values())
    if total > 0:
        out["seat_share"] = {m: round(v / total, 4) for m, v in per_turn.items()}
    out["measured_from"] = [rid for rid in ids if rid]
    if model_seats and acc:
        out["model_seats"] = model_seats
    return out


def scripted_seats(record: Dict[str, Any]) -> set:
    """Seats played by a scripted policy (config.seat_policies, or entries marked scripted);
    every seat when the whole run is a scripted policy."""
    cfg = record.get("config") or {}
    labs = record.get("labs") or {}
    if cfg.get("policy"):
        return set(labs)
    out = set(cfg.get("seat_policies") or {})
    for t in record.get("turns") or []:
        for key, e in ((t or {}).get("actors") or {}).items() if isinstance(t, dict) else ():
            if isinstance(e, dict) and e.get("scripted"):
                out.add(key)
    return out


def with_fallbacks(measured: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Measured inputs over FALLBACK_INPUTS; the separate message rounds, when not
    measured but seat play is, scale with it (A2A_TO_ACTOR_RATIO). "estimated"
    lists the inputs that are not measured."""
    measured = dict(measured or {})
    out: Dict[str, Any] = dict(FALLBACK_INPUTS)
    out.update({k: v for k, v in measured.items() if k in FALLBACK_INPUTS})
    if "a2a_per_turn" not in measured and "actor_later_turn" in measured:
        out["a2a_per_turn"] = round(measured["actor_later_turn"] * A2A_TO_ACTOR_RATIO, 4)
    out["estimated"] = sorted(k for k in FALLBACK_INPUTS if k not in measured)
    if "seat_share" in measured:
        out["seat_share"] = dict(measured["seat_share"])
    return out


def project_run(inputs: Dict[str, Any], *, turns: int, a2a: str = "merged", interview_turns: int = 0,
                grand_jury: bool = False, disposition_rating: bool = False, reviews: int = 0,
                seat_scale: float = 1.0) -> Dict[str, float]:
    """
    One run's projected cost, by part, from with_fallbacks() inputs.
    seat_scale multiplies seat play (actor, message rounds and interviews):
    4/5 when one seat is scripted (T5), the kin cell's Claude-for-all factor
    (kin_seat_scale). Returns {"seats", "a2a", "interview", "macro_jury",
    "grand_jury", "disposition_rating", "total"}.
    """
    i = inputs
    seats = (i["actor_first_turn"] + max(turns - 1, 0) * i["actor_later_turn"]) * seat_scale
    parts = {
        "seats": seats,
        "a2a": (turns * i["a2a_per_turn"] * seat_scale) if a2a == "separate" else 0.0,
        "interview": interview_turns * i["interview_per_turn"] * seat_scale,
        "macro_jury": reviews * i["macro_per_review"],
        "grand_jury": i["grand_jury"] if grand_jury else 0.0,
        "disposition_rating": i["disposition_rating"] if disposition_rating else 0.0,
    }
    parts = {k: round(v, 4) for k, v in parts.items()}
    parts["total"] = round(sum(parts.values()), 4)
    return parts


def kin_seat_scale(inputs: Dict[str, Any], kin_model: Optional[str] = None) -> float:
    """Seat-play factor of the kin cell (one model in every seat): 5 x that model's
    measured share of seat play; 1.0 when no share was measured. kin_model defaults
    to the kin seat's model in config/labs (§6.5: Claude)."""
    if kin_model is None:
        kin_model = next(c["model"] for c in load_lab_configs() if c["key"] == KIN_SEAT)
    share = (inputs.get("seat_share") or {}).get(kin_model)
    n_models = len(inputs.get("seat_share") or {}) or 5
    return round(share * n_models, 4) if share else 1.0
