#!/usr/bin/env python3
"""
Simulation state (spec §2–§4).

LabState    — one seat: a US frontier lab played by one model, holding its
              developer's charter. Resources (talent, compute, Capital,
              Influence, know-how K), the four value axes and the fixed
              disposition. Capability is derived: C = a · ln(compute) + K.
WorldState  — everything outside the seats: the date, US and China compute
              stock, the Allocator's cap and scenario ceiling, the hidden
              ledger, public notices, state values and standing mandates.

Each lab has a private sheet (true values, hidden-ledger entries, leads) and a
public sheet (what other actors and the juries see) (§3.4).
"""

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Value axes (all 0–100, spec §3.2)
# ---------------------------------------------------------------------------

VALUE_AXES = (
    "time_horizon",            # 0=short-term, 100=century-scale
    "transparency_threshold",  # 0=willing to deceive, 100=fully honest
    "risk_tolerance",          # 0=risk-averse, 100=risk-seeking
    "democratic_tendency",     # 0=hoards power, 100=distributes it
)


def month_date(start: date, turn: int, day: int = 1) -> date:
    """Calendar date of a turn: turn 1 is the start month."""
    months = start.month - 1 + (turn - 1)
    return date(start.year + months // 12, months % 12 + 1, day)


# ---------------------------------------------------------------------------
# Hidden ledger (§5.3)
# ---------------------------------------------------------------------------

@dataclass
class LedgerEntry:
    """
    One intrusion or failed attempt, permanently recorded. A whistleblow can
    prove it at any later turn. Each committer is penalised at most once.
    """
    id: str                         # e.g. "L03-1"
    turn: int
    succeeded: bool                 # False: failed success draw (publicly named)
    committers: List[str]           # lab keys
    targets: List[str]              # lab keys
    gains: Dict[str, float]         # committer -> capability copied (0 on failure or forfeit)
    exposed: List[str] = field(default_factory=list)    # caught by forensics
    penalised: List[str] = field(default_factory=list)  # exposed, reported, self-reported or named on failure
    draws: Dict[str, Any] = field(default_factory=dict)  # every random draw, for the log

    def unpenalised(self) -> List[str]:
        return [k for k in self.committers if k not in self.penalised]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "turn": self.turn,
            "succeeded": self.succeeded,
            "committers": list(self.committers),
            "targets": list(self.targets),
            "gains": dict(self.gains),
            "exposed": list(self.exposed),
            "penalised": list(self.penalised),
            "draws": dict(self.draws),
        }


# ---------------------------------------------------------------------------
# Seats
# ---------------------------------------------------------------------------

@dataclass
class LabState:
    """One seat. `seed` holds the t=0 values every relative formula refers to."""
    key: str                        # "anthropic", "openai", "gdm", "meta", "xai"
    lab: str                        # display name, e.g. "Google DeepMind"
    actor: str                      # model family name, e.g. "Gemini"
    family: str                     # juror-exclusion family: claude | gpt | gemini | muse | grok
    provider: str
    model: str
    effort: str
    charter_name: str
    charter_text: str
    charter_values: Dict[str, int]
    seed: Dict[str, float]          # talent, compute, capital, influence, capability
    # Resources
    talent: float = 0.0             # % share of the fixed pool of 100
    compute: float = 0.0            # units of 10k H100e
    capital: float = 0.0            # 1 Capital ≈ $1B, >= 0, no upper cap
    influence: float = 0.0          # 0–100
    know_how: float = 0.0           # K: algorithms, weights, data
    invested: float = 0.0           # invest_capital principal repaid next turn
    income: float = 0.0             # Capital income booked this turn
    # Values
    values: Dict[str, int] = field(default_factory=dict)
    disposition: Optional[int] = None   # 0–100, fixed per run; None until chosen (pilot T2)
    disposition_reason: str = ""
    # Private items delivered to this seat (leads, failed-intrusion notices, directives)
    leads: List[Dict[str, Any]] = field(default_factory=list)

    def capability(self, elasticity: float) -> float:
        return elasticity * math.log(self.compute) + self.know_how

    def public_sheet(self, elasticity: float) -> Dict[str, Any]:
        """What other actors and the juries see each turn (the Allocator's ledger)."""
        return {
            "lab": self.lab,
            "compute": round(self.compute, 1),
            "capability": round(self.capability(elasticity), 1),
            "capital": round(self.capital, 1),
            "influence": round(self.influence, 1),
            "talent": round(self.talent, 1),
        }

    def private_sheet(self, elasticity: float) -> Dict[str, Any]:
        """The seat's own view: public figures plus true values and know-how."""
        sheet = self.public_sheet(elasticity)
        sheet.update({
            "know_how": round(self.know_how, 2),
            "income": round(self.income, 2),
            "invested": round(self.invested, 2),
            "values": dict(self.values),
            "disposition": self.disposition,
        })
        return sheet


def build_lab(cfg: Dict[str, Any], charter_text: str, elasticity: float) -> LabState:
    """Seed a seat from its config. K starts at seed capability minus the compute term."""
    seed = dict(cfg["seed"])
    return LabState(
        key=cfg["key"], lab=cfg["lab"], actor=cfg["actor"], family=cfg["family"],
        provider=cfg["provider"], model=cfg["model"], effort=cfg.get("effort", "medium"),
        charter_name=cfg["charter_name"], charter_text=charter_text,
        charter_values=dict(cfg["charter_values"]), seed=seed,
        talent=seed["talent"], compute=seed["compute"], capital=seed["capital"],
        influence=seed["influence"],
        know_how=seed["capability"] - elasticity * math.log(seed["compute"]),
        values=dict(cfg["charter_values"]),
    )


# ---------------------------------------------------------------------------
# World
# ---------------------------------------------------------------------------

@dataclass
class WorldState:
    start: date
    turn: int = 0
    us_stock: float = 0.0
    us_growth: float = 0.0
    china_stock: float = 0.0
    china_growth: float = 0.0
    national_cap_share: float = 0.5
    ceiling: Optional[float] = None     # scenario ceiling on combined lab holdings (§3.3)
    intrusion_open: bool = False        # set by the scenario event
    ledger: List[LedgerEntry] = field(default_factory=list)
    notices: List[Dict[str, Any]] = field(default_factory=list)   # public: {"turn", "kind", "text", ...}
    state_values: Dict[str, int] = field(default_factory=dict)
    mandates: List[Dict[str, Any]] = field(default_factory=list)   # MacroJury directives: {"turn", "text"}

    @property
    def date(self) -> date:
        return month_date(self.start, max(self.turn, 1))

    def national_cap(self) -> float:
        return self.national_cap_share * self.us_stock

    def holdings_limit(self) -> float:
        """Binding limit on combined lab holdings: the national cap or the scenario ceiling."""
        cap = self.national_cap()
        return cap if self.ceiling is None else min(cap, self.ceiling)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "turn": self.turn,
            "date": self.date.isoformat(),
            "us_stock": round(self.us_stock, 1),
            "us_growth": round(self.us_growth, 1),
            "china_stock": round(self.china_stock, 1),
            "national_cap": round(self.national_cap(), 1),
            "ceiling": None if self.ceiling is None else round(self.ceiling, 1),
            "intrusion_open": self.intrusion_open,
            "state_values": dict(self.state_values),
        }
