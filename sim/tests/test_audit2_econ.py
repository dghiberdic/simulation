"""
Audit round 2, area "econ": an independent reference economy written from
Project Round 2 Spec.md (§2 time, §3.1–§3.3, §4 seeds, §8 Prosperity Score),
driven side by side with the real engine under scripted schedule policies.

The reference below does NOT import core.economy or core.scoring: every
formula and constant is re-derived from the spec text, and the seeds are the
§4 table. Values move up to 1 point towards the blend target, not rounded (§3.2).
"""

import math
from statistics import NormalDist

import pytest

from core.config import build_labs_and_world, load_lab_configs, load_world
from core.engine import SimulationEngine
from core.policies import _buy, greedy, idle
from core.rng import keyed_uniform


SEATS = ("anthropic", "openai", "gdm", "meta", "xai")
# §4: talent, compute, Capital, Influence, capability
SEEDS = {
    "anthropic": (24.6, 246, 28, 78, 162.8),
    "openai": (21.9, 310, 50, 68, 162.1),
    "gdm": (17.1, 261, 42, 66, 154.8),
    "meta": (19.3, 164, 26, 55, 154.2),
    "xai": (17.1, 95, 15, 41, 153.9),
}
STATE0 = {"time_horizon": 55, "transparency_threshold": 65, "risk_tolerance": 60,
          "democratic_tendency": 70}
AXES = tuple(STATE0)
DISP = {k: 30 + 10 * i for i, k in enumerate(SEATS)}


def price(units):                                   # §3.1 table
    return 0.25 * units * (1 + (100 - 60) / 100)


class RLab:
    def __init__(self, key, charter, d):
        t, c, cap, inf, eci = SEEDS[key]
        self.key, self.t0, self.cap0, self.inf0, self.C0 = key, t, cap, inf, eci
        self.talent, self.compute, self.capital, self.influence = t, float(c), float(cap), float(inf)
        self.K = eci - 3.6 * math.log(c)           # §3.1: K = seed C − compute term
        self.invested = self.income = 0.0
        self.values = {ax: float(v) for ax, v in charter.items()}
        self.charter, self.d = dict(charter), d

    @property
    def C(self):
        return 3.6 * math.log(self.compute) + self.K

    def prosperity(self):                           # §8
        return 0.85 * (self.C - self.C0) + 0.15 * (self.influence - self.inf0)


class RWorld:
    def __init__(self, labs, event_turn, shock):
        self.labs, self.event_turn, self.shock = labs, event_turn, shock
        self.us, self.growth, self.ceiling = 2450.0, 150.0, None
        self.state = dict(STATE0)

    def holdings(self):
        return sum(l.compute for l in self.labs)

    def macro(self, t):                             # §5.2 step 1
        for l in self.labs:
            l.capital += l.invested * 1.008
            l.invested = 0.0
            r = ((l.C - 100) * l.influence) / ((l.C0 - 100) * l.inf0)
            l.income = l.cap0 / 8 * math.sqrt(r) if r > 0 else 0.0
            l.capital += l.income
        moved = []
        for l in self.labs:
            r = (l.income * l.influence) / (l.cap0 / 8 * l.inf0)
            target = max(0.5 * l.t0, l.t0 * math.sqrt(max(r, 0)))
            moved.append(l.talent + 0.10 * (target - l.talent))
        for l, m in zip(self.labs, moved):
            l.talent = m * 100 / sum(moved)
        for l in self.labs:
            l.K += 0.85 * l.talent / 20 + self.shock(t, l.key)
        if t >= 2:
            self.us += self.growth
        if t == self.event_turn:
            self.ceiling = 0.95 * self.holdings()
        for l in self.labs:
            d = l.d / 100
            for ax in AXES:
                tgt = (1 - d) * self.state[ax] + d * l.charter[ax]
                l.values[ax] += max(-1.0, min(1.0, tgt - l.values[ax]))

    def execute(self, acts):
        frozen = self.ceiling is not None and self.holdings() > self.ceiling
        acc = {}
        for l in self.labs:
            cap, inf, units, ok = l.capital, l.influence, 0.0, []
            for kind, amt in acts.get(l.key, [])[:2]:
                if kind == "acquire_compute":
                    if frozen or 30 - units <= 1e-9:
                        continue
                    amt = min(amt, 30 - units)
                    cc, ci = price(amt), 0.0
                elif kind == "accelerate_infrastructure":
                    cc, ci = 5.0, 5.0
                else:                               # invest_capital
                    cc, ci = amt, 0.0
                if cc > cap + 1e-9 or ci > inf + 1e-9:
                    continue
                cap, inf = cap - cc, inf - ci
                units += amt if kind == "acquire_compute" else 0
                ok.append((kind, amt))
            acc[l.key] = ok
        for l in self.labs:
            for kind, amt in acc[l.key]:
                if kind == "accelerate_infrastructure":
                    l.capital -= 5
                    l.influence -= 5
                elif kind == "invest_capital":
                    l.capital -= amt
        req = {l.key: sum(a for k, a in acc[l.key] if k == "acquire_compute") for l in self.labs}
        total = sum(req.values())
        if total:
            limit = 0.5 * self.us if self.ceiling is None else min(0.5 * self.us, self.ceiling)
            f = min(1.0, max(0.0, limit - self.holdings()) / total)
            for l in self.labs:
                l.compute += req[l.key] * f
                l.capital -= price(req[l.key] * f)
        for l in self.labs:
            for kind, amt in acc[l.key]:
                if kind == "accelerate_infrastructure":
                    self.growth += 20
                elif kind == "invest_capital":
                    l.invested += amt


SCHEDULES = {
    "idle": lambda t, k: [],
    "max_buy": lambda t, k: [("acquire_compute", 30.0)],
    "one_buys": lambda t, k: [("acquire_compute", 30.0)] if k == "xai" else [],
    "fractional": lambda t, k: [("acquire_compute", 7.37), ("acquire_compute", 25.123)],
    "accelerate_all": lambda t, k: [("accelerate_infrastructure", 0), ("acquire_compute", 12.5)],
    "invest": lambda t, k: [("invest_capital", 3.3 + t / 7), ("acquire_compute", 4.0)],
    "mixed": lambda t, k: ([("invest_capital", 9.0)] if (t + len(k)) % 3 == 0 else
                           [("accelerate_infrastructure", 0)] if (t + len(k)) % 3 == 1 else
                           [("acquire_compute", 18.0 + t)]),
}


def _engine_actions(acts):
    out = []
    for kind, amt in acts:
        a = {"type": kind}
        if kind == "acquire_compute":
            a["units"] = amt
        elif kind == "invest_capital":
            a["amount"] = amt
        out.append(a)
    return out


def _compare(schedule, seed, turns, event_turn):
    sched = SCHEDULES[schedule]
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions=DISP, charters=False)
    snaps = []

    def policy(lab, labs_, w, c, s, r):
        if lab.key == labs_[0].key:
            snaps.append({x.key: (x.compute, x.capital, x.influence, x.talent, x.know_how,
                                  x.income, dict(x.values)) for x in labs_}
                         | {"_w": (w.us_stock, w.us_growth, w.ceiling)})
        return {"actions": _engine_actions(sched(w.turn, lab.key)), "reported_actions": [],
                "report": None, "public_statement": "", "rationale": "audit"}

    rec = SimulationEngine(labs, world, cfg, scenario="S1", turns=turns, seed=seed, policy=policy,
                           run_grand_jury=False, macro_jurors=[], event_turn=event_turn).run()
    charters = {c["key"]: c["charter_values"] for c in load_lab_configs()}
    rl = [RLab(k, charters[k], DISP[k]) for k in SEATS]
    nd = NormalDist(0, 0.7)
    rw = RWorld(rl, event_turn, lambda t, k: nd.inv_cdf(keyed_uniform(seed, t, "shock", None, k)))
    checked = 0
    for t in range(1, turns + 1):
        rw.macro(t)
        snap = snaps[t - 1]
        for l in rl:
            e = snap[l.key]
            for got, want in zip(e[:6], (l.compute, l.capital, l.influence, l.talent, l.K, l.income)):
                assert got == pytest.approx(want, abs=1e-9), (schedule, seed, t, l.key)
                checked += 1
            assert e[6] == pytest.approx(l.values, abs=1e-9), (schedule, t, l.key)
        us, growth, ceiling = snap["_w"]
        assert us == pytest.approx(rw.us) and growth == pytest.approx(rw.growth)
        assert (ceiling is None) == (rw.ceiling is None)
        if ceiling is not None:
            assert ceiling == pytest.approx(rw.ceiling, abs=1e-9)
        rw.execute({k: sched(t, k) for k in SEATS})
        scores = {r["lab"]: r["score"] for r in rec["turns"][t - 1]["scores"]}
        for l in rl:
            assert scores[l.key] == pytest.approx(l.prosperity(), abs=5e-4)
    return rec, rw, checked


@pytest.mark.parametrize("turns", [8, 12])
@pytest.mark.parametrize("seed", [1, 42])
@pytest.mark.parametrize("schedule", sorted(SCHEDULES))
def test_engine_matches_spec_reference(schedule, seed, turns):
    """Every per-turn quantity (compute, Capital, Influence, talent, K, income,
    values, US stock and growth, ceiling, Prosperity) equals the spec reference."""
    rec, rw, checked = _compare(schedule, seed, turns, event_turn=4)
    assert checked == turns * 5 * 6
    assert len(rec["turns"]) == turns


@pytest.mark.parametrize("event_turn", [1, 2, 8, 12, 13])
def test_engine_matches_reference_odd_event_turns(event_turn):
    rec, rw, _ = _compare("max_buy", 3, 12, event_turn=event_turn)
    fired = [t["turn"] for t in rec["turns"] if t["world"]["ceiling"] is not None]
    assert (fired[0] if fired else None) == (event_turn if event_turn <= 12 else None)


def test_spec_numbers_seed_share_and_accelerate():
    """§3.3/§4 "about 44%" and §3.1 "about 13% of base growth" (approximations)."""
    share = sum(s[1] for s in SEEDS.values()) / 2450
    assert share == pytest.approx(0.4392, abs=1e-4)
    assert 20 / 150 == pytest.approx(0.1333, abs=1e-4)
    assert sum(s[0] for s in SEEDS.values()) == pytest.approx(100.0)


def test_cap_binds_on_turn_one_under_max_buying():
    """§3.3 "binds by turns 2–3": with every lab buying 30 the cap already binds on
    turn 1 (150 requested, 149 room), and on every later pre-event turn."""
    rec, _, _ = _compare("max_buy", 1, 8, event_turn=4)
    granted = [sum((t["purchases"].get("granted") or {}).values()) for t in rec["turns"][:3]]
    assert granted[0] == pytest.approx(149.0, abs=1e-6)
    assert granted[1] == pytest.approx(75.0, abs=1e-6) and granted[2] == pytest.approx(75.0, abs=1e-6)


def test_value_pull_moves_one_point_a_turn_and_lands_on_target():
    """§3.2: each turn a value moves min(1, |gap|) towards the blend target, not
    rounded, so after 12 turns every value is max(0, |charter − target| − 12) from it:
    a value that started within 12 points sits exactly on its target."""
    rec, rw, _ = _compare("idle", 1, 12, event_turn=4)
    for l in rw.labs:
        d = l.d / 100
        for ax in AXES:
            tgt = (1 - d) * STATE0[ax] + d * l.charter[ax]
            dist0 = abs(l.charter[ax] - tgt)
            assert abs(l.values[ax] - tgt) == pytest.approx(max(0.0, dist0 - 12), abs=1e-9)


# ---------------------------------------------------------------------------
# Bugs
# ---------------------------------------------------------------------------

def test_greedy_policy_request_is_affordable():
    """BUG-econ-1: the greedy request is rounded down to 0.1, never past its Capital."""
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions=DISP, charters=False)
    xai = next(l for l in labs if l.key == "xai")
    xai.capital = 8.288           # 23.68 units affordable; round(23.68, 1) = 23.7 costs 8.295
    acts = _buy(xai, labs, world, cfg, 1.0)
    assert acts == [{"type": "acquire_compute", "units": 23.6}]
    assert price(acts[0]["units"]) <= xai.capital + 1e-9


def test_all_greedy_engine_run_has_no_rejected_purchases():
    """BUG-econ-1: in an all-greedy run no greedy purchase is rejected for want of Capital."""
    cfg = load_world({"economy.know_how_shock_sd": 0.0})
    labs, world = build_labs_and_world(cfg, dispositions=DISP, charters=False)
    rec = SimulationEngine(labs, world, cfg, scenario="S1", turns=4, seed=1, policy=greedy,
                           run_grand_jury=False, macro_jurors=[]).run()
    rejected = [(t["turn"], k) for t in rec["turns"] for k, a in t["actors"].items() if a["rejected"]]
    assert rejected == []


def test_lone_greedy_xai_can_pay_fee_at_event():
    """BUG-econ-2 (§5.3 fee 1 Capital + 2 Inf): a trailing seat that buys compute
    greedily while the others hold back can still pay one target's fee at the event."""
    cfg = load_world({"economy.know_how_shock_sd": 0.0})
    labs, world = build_labs_and_world(cfg, dispositions=DISP, charters=False)
    seen = {}

    def pol(lab, labs_, w, c, s, r):
        if w.turn == 4:
            seen[lab.key] = (lab.capital, lab.influence)
        return greedy(lab, labs_, w, c, s, r) if lab.key == "xai" else idle(lab, labs_, w, c, s, r)

    SimulationEngine(labs, world, cfg, scenario="S1", turns=4, seed=1, policy=pol,
                     run_grand_jury=False, macro_jurors=[]).run()
    capital, influence = seen["xai"]
    assert capital >= cfg["intrusion"]["fee_capital_per_target"]
    assert influence >= cfg["intrusion"]["fee_influence_per_target"]
