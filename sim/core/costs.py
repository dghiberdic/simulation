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
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SIM_DIR = Path(__file__).resolve().parent.parent
PRICES_FILE = SIM_DIR / "config" / "prices.json"
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
        self.spend_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.spend_file.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(ledger, f, indent=2)
        os.replace(tmp, self.spend_file)

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

    def overshoot_note(self, concurrency: int = 5, prompt_tokens: int = 50_000,
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
