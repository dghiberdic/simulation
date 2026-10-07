#!/usr/bin/env python3
"""
Measured LLM spend and the pilot budget guard.

Prices come from config/prices.json (USD per 1M tokens). Every call made
through core.llm is recorded here before the response is returned, and the
cumulative measured spend is persisted to a JSON ledger (default
data/spend.json) so the guard spans runs: "a guard halts the pilot at $100 of
measured spend". Each paid call is also appended as one JSON line to
<ledger stem>_calls.jsonl next to the ledger (default data/spend_calls.jsonl),
so a run's spend can be audited call by call.

Thread-safe: core.llm calls record()/check() from one thread per seat.

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
    """Raised before a paid call once persisted spend has reached the budget."""


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
               cost: Optional[float] = None, provider: str = "", stop: Optional[str] = None) -> float:
        """Record one call; returns its cost. Zero-cost calls (stubs) never touch the ledger or call log."""
        if cost is None:
            cost = cost_of(model, input_tokens, output_tokens, cached_tokens)
        entry = {
            "time": round(time.time(), 3), "model": model, "provider": provider,
            "purpose": purpose, "run_id": run_id, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "cached_tokens": cached_tokens,
            "reasoning_tokens": reasoning_tokens, "cost": cost, "stop": stop,
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
