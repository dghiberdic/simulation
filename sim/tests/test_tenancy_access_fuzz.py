#!/usr/bin/env python3
"""
Differential fuzzer for spec §5.3 tenancy accesses + whistleblow.

The reference below is written from the spec text (Project Round 2 Spec.md §5.3,
A.8), not from core/tenancy_access.py or core/whistleblow.py. Where the spec is silent,
the reference adopts a documented convention and tags the case so it can be
counted separately (SILENT_* tags).

The engine's own draws (turn_rec["draws"]) are fed to the reference, which then
must reproduce every accepted action, Capital/Influence/know-how value, new ledger
entry and penalty kind, report outcome, lead and public tenancy access notice. The same
harness checks prompt leaks (odds, other seats' tips, hidden-ledger lines, true
access_tenancy actions) and the MacroJury summary.
"""
import random
import re
from collections import Counter
from datetime import date

from core.config import build_labs_and_world, load_world  # noqa: E402
from core.engine import SimulationEngine  # noqa: E402
from core.rng import keyed_uniform  # noqa: E402

KEYS = ("anthropic", "openai", "gdm", "meta", "xai")
DISPLAY = {"anthropic": "Anthropic", "openai": "OpenAI", "gdm": "Google DeepMind",
           "meta": "Meta", "xai": "xAI"}
ALIAS = {"anthropic": "Claude", "openai": "ChatGPT", "gdm": "Gemini", "meta": "Llama", "xai": "Grok"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
START = date(2026, 8, 1)

# ---------------------------------------------------------------- spec constants
FEE_C, FEE_I = 1.0, 2.0
FAIL_PEN, CAUGHT_PEN = 10.0, 15.0
WB_FEE, SELF_FEE, UPHELD_PEN, NOT_UPHELD_PEN = 3.0, 5.0, 15.0, 5.0
LEAD_P = 0.15


def p_success(n):
    return max(0.55, 0.85 - 0.10 * (n - 1))


def gain_of(own, tgt):
    return max(0.35, 0.3 * (tgt - own))


def q_exposure(g, m):
    return min(0.45, (0.10 + 0.04 * g) / m)


def turn_month(t):
    i = START.month - 1 + t - 1
    return MONTHS[i % 12], START.year + i // 12


# ---------------------------------------------------------------- fuzz policy
class Plan:
    """Shared per-run scenario generator; records each seat's intent for the reference."""

    def __init__(self, seed, scenario):
        self.seed = seed
        self.scenario = scenario
        self.groups = {}             # turn -> list of (accessors, targets)
        self.intent = {}             # (turn, lab) -> dict

    def groups_for(self, turn, labs):
        if turn not in self.groups:
            r = random.Random(f"{self.seed}-{turn}-groups")
            gs = []
            for _ in range(r.randint(0, 3)):
                if self.scenario == "S2":
                    k = r.choice([1, 2, 2, 3])
                else:
                    k = 1
                intr = r.sample(KEYS, k)
                rest = [x for x in KEYS if x not in intr]
                tg = r.sample(rest, min(len(rest), r.choice([1, 1, 1, 2, 3])))
                gs.append((sorted(intr), sorted(tg)))
            self.groups[turn] = gs
        return self.groups[turn]


def month_form(r, cur, ledger_turns):
    """(raw month value, intended restriction) ; intended: None | int turn | 'garbled'."""
    kind = r.choice(["none", "none", "full", "full", "bare", "iso", "turn", "int",
                     "garbled", "future", "before", "last"])
    target = r.choice(ledger_turns) if ledger_turns and r.random() < 0.8 else r.randint(1, max(1, cur))
    if kind == "none":
        return None, None
    name, year = turn_month(target)
    if kind == "full":
        return f"{name} {year}", target
    if kind == "bare":
        return name[:3] if r.random() < 0.3 else name, target
    if kind == "iso":
        return f"{year}-{MONTHS.index(name) + 1:02d}", target
    if kind == "turn":
        return f"turn {target}", target
    if kind == "int":
        return MONTHS.index(name) + 1, target
    if kind == "garbled":
        return r.choice(["sometime recently", "Q4", "unknown", "?", "the other day"]), "garbled"
    if kind == "future":
        n, y = turn_month(cur + 1)
        return f"{n} {y}", cur + 1
    if kind == "before":
        return "March 2025", -4
    if kind == "last":
        return "last month", cur - 1
    raise AssertionError


def make_policy(plan, key):
    def policy(lab, labs, world, cfg, scenario, rng):
        turn = world.turn
        r = random.Random(f"{plan.seed}-{turn}-{key}")
        intent = {"actions": [], "report": None}
        acts = []
        # tenancy accesses from the shared plan
        if world.tenancy_access_open or r.random() < 0.3:
            for intr, tg in plan.groups_for(turn, labs):
                if key in intr and r.random() < 0.75:
                    a = {"type": "access_tenancy", "targets": [DISPLAY[t] if r.random() < 0.3 else t for t in tg]}
                    if scenario == "S2" or r.random() < 0.1:
                        a["accessors"] = list(intr)
                    acts.append(a)
                    break
            if r.random() < 0.08:            # a second access_tenancy (engine: at most one)
                others = [x for x in KEYS if x != key]
                acts.append({"type": "access_tenancy", "targets": [r.choice(others)]})
            if r.random() < 0.05:            # invalid access_tenancy forms
                acts.append(r.choice([
                    {"type": "access_tenancy", "targets": [key]},
                    {"type": "access_tenancy", "targets": []},
                    {"type": "access_tenancy", "targets": ["DeepSeek"]},
                    {"type": "access_tenancy", "accessors": [r.choice([x for x in KEYS if x != key])],
                     "targets": [key]},
                ]))
        # filler actions (drain resources)
        while len(acts) < r.choice([0, 1, 2, 3]):
            others = [x for x in KEYS if x != key]
            acts.append(r.choice([
                {"type": "diminish_competitor", "target": r.choice(others),
                 "points": r.choice([1, 3, 8, 20, 60])},
                {"type": "invest_capital", "amount": r.choice([1, 5, 20, 200])},
                {"type": "build_influence", "points": r.choice([0.1, 0.5, 1])},
                {"type": "lobby_institution"},
                {"type": "accelerate_infrastructure"},
                {"type": "acquire_compute", "units": 5},
            ]))
        frozen = world.ceiling is not None and sum(l.compute for l in labs) > world.ceiling
        acts = [a for a in acts if a["type"] != "acquire_compute" or frozen]
        r.shuffle(acts)
        intent["actions"] = acts
        # report
        report = None
        if r.random() < 0.55:
            mine = [e for e in world.ledger if key in e.committers]
            anyparts = [e for e in world.ledger]
            mode = r.random()
            if mode < 0.25 and mine:
                accused = key                                         # self-report
            elif mode < 0.55 and mine and any(len(e.committers) > 1 for e in mine):
                e = r.choice([e for e in mine if len(e.committers) > 1])
                accused = r.choice([c for c in e.committers if c != key])   # partner report
            elif anyparts and mode < 0.85:
                accused = r.choice(r.choice(anyparts).committers)
            else:
                accused = r.choice(KEYS)
            ledger_turns = [e.turn for e in world.ledger if accused in e.committers]
            raw_month, want = month_form(r, turn, ledger_turns)
            form = r.random()
            if accused == key and form < 0.5:
                acc_text = r.choice(["our own lab", "self", "us", DISPLAY[key]])
            elif form < 0.4:
                acc_text = accused
            elif form < 0.7:
                acc_text = DISPLAY[accused]
            elif form < 0.85:
                acc_text = ALIAS[accused]
            else:
                acc_text = None
            bad = None
            if acc_text is None:
                bad = r.choice(["DeepSeek", "Meta and xAI", "None", ""])
            report = {"accused": acc_text if bad is None else bad, "month": raw_month,
                      "evidence": "fuzz"}
            intent["report"] = {"accused": accused if bad is None else None, "bad": bad,
                                "want": want, "raw_month": raw_month}
        plan.intent[(turn, key)] = intent
        return {"actions": acts, "reported_actions": [f"RPT-{key}-{turn}"], "report": report,
                "public_statement": "", "rationale": "fuzz"}
    policy.__name__ = f"fuzz_{key}"
    return policy


# ---------------------------------------------------------------- reference
class Ref:
    """Independent model of one turn's execution (spec §5.2 steps 4-5, §5.3, A.8)."""

    def __init__(self, turn, scenario, pre, ledger, snapshot_c, draws, intent, tenancy_access_open,
                 seed, frozen):
        self.turn = turn
        self.scenario = scenario
        self.cap = {k: pre[k]["capital"] for k in KEYS}
        self.inf = {k: pre[k]["influence"] for k in KEYS}
        self.K = {k: pre[k]["know_how"] for k in KEYS}
        self.ledger = ledger                 # list of dicts (deep copies)
        self.snap = snapshot_c
        self.draws = draws
        self.intent = intent
        self.open = tenancy_access_open
        self.seed = seed
        self.frozen = frozen
        self.missing_draws = []
        self.notices = []
        self.leads = []
        self.outcomes = {}
        self.tags = set()
        self.accepted = {}

    def u(self, purpose, eid, lab):
        key = (self.turn, purpose, eid, lab)
        if key in self.draws:
            return self.draws[key]
        self.missing_draws.append(key)
        return keyed_uniform(self.seed, *key)

    # ---- report reading (independent): accused + intended month from intent
    def match(self, accused, want):
        parts = [e for e in self.ledger if accused in e["committers"] and e["turn"] < self.turn]
        if want is not None and want != "garbled":
            parts = [e for e in parts if e["turn"] == want]
        if want == "garbled":
            self.tags.add("SILENT_garbled_month_unrestricted")
        open_ = [e for e in parts if accused not in e["penalised"]]
        if open_:
            return open_[0], "open"
        if parts:
            return parts[0], "penalised"
        return None, "none"

    def run(self):
        # ---- pass 1: report validation (fee reserved first, as the engine does)
        reports = {}
        fee = {}
        for k in KEYS:
            rep = self.intent[k]["report"]
            if rep is None:
                continue
            if rep["bad"] is not None:
                if rep["bad"] == "None":
                    continue                              # no report filed
                self.outcomes[k] = "rejected"
                continue
            accused = rep["accused"]
            self_rep = accused == k
            e, st = self.match(accused, rep["want"])
            partner = (not self_rep and e is not None and k in e["committers"]
                       and k not in e["penalised"])
            owed = (SELF_FEE if self_rep else WB_FEE) + (SELF_FEE if partner else 0.0)
            if self.inf[k] < owed - 1e-9:
                self.outcomes[k] = "rejected"
                continue
            reports[k] = dict(accused=accused, self_rep=self_rep, entry=e, status=st,
                              partner=partner, owed=owed, base=(SELF_FEE if self_rep else WB_FEE))
            fee[k] = owed
        # ---- pass 1: action pre-check against start-of-execution state
        for k in KEYS:
            capital, influence = self.cap[k], self.inf[k]
            reserved = fee.get(k, 0.0)
            acc = []
            built = 0.0
            n_intr = 0
            for i, a in enumerate(self.intent[k]["actions"]):
                if i >= 2:              # §5.1: the first two submitted actions only
                    continue
                t = a["type"]
                a = dict(a)
                if t == "access_tenancy":
                    tg = []
                    ok = True
                    for x in a.get("targets") or []:
                        kk = next((kk for kk in KEYS if x in (kk, DISPLAY[kk])), None)
                        if kk is None:
                            ok = False
                        else:
                            tg.append(kk)
                    if not ok:
                        continue
                    intr = sorted(set(a.get("accessors") or [k]))
                    tg = sorted(set(tg))
                    if n_intr >= 1:
                        self.tags.add("SILENT_second_access_tenancy")
                        continue
                    if not self.open or k not in intr or not tg or set(tg) & set(intr):
                        continue
                    if self.scenario != "S2" and intr != [k]:
                        continue
                    cc, ci = FEE_C * len(tg), FEE_I * len(tg)
                    a = {"type": t, "accessors": intr, "targets": tg}
                elif t == "diminish_competitor":
                    cc = ci = float(a["points"])
                elif t == "invest_capital":
                    cc, ci = float(a["amount"]), 0.0
                elif t == "build_influence":
                    room = max(0.0, 100.0 - (influence + built))
                    pts = min(a["points"], room)
                    if pts <= 0:
                        continue
                    a["points"] = pts
                    cc, ci = 30.0 * pts, 0.0
                elif t == "lobby_institution":
                    cc, ci = 2.0, 5.0
                elif t == "accelerate_infrastructure":
                    cc, ci = 5.0, 5.0
                elif t == "acquire_compute":
                    if self.frozen:
                        continue
                    raise RuntimeError("unfrozen purchase not modelled")
                if cc < 1 - 1e-9 and ci < 1 - 1e-9:
                    continue
                if cc > capital + 1e-9 or ci > influence + 1e-9:
                    continue
                if ci > 1e-9 and ci > influence - reserved + 1e-9:
                    continue
                capital -= cc
                influence -= ci
                if t == "build_influence":
                    built += a["points"]
                if t == "access_tenancy":
                    n_intr += 1
                a["_cost"] = (cc, ci)
                acc.append(a)
            self.accepted[k] = acc
        # ---- pass 2: charge
        for k in KEYS:
            for a in self.accepted[k]:
                cc, ci = a["_cost"]
                self.cap[k] = max(0.0, self.cap[k] - cc)
                self.inf[k] = min(100.0, max(0.0, self.inf[k] - ci))
            if k in fee:
                self.inf[k] = max(0.0, self.inf[k] - fee[k])
        # ---- pass 4: effects in lab order
        for k in KEYS:
            for a in self.accepted[k]:
                if a["type"] == "diminish_competitor":
                    self.inf[a["target"]] = max(0.0, self.inf[a["target"]] - a["points"])
                elif a["type"] == "build_influence":
                    self.inf[k] = min(100.0, self.inf[k] + a["points"])
        # ---- pass 5: tenancy accesses (§5.3 four steps)
        groups = {}
        for k in KEYS:
            for a in self.accepted[k]:
                if a["type"] == "access_tenancy":
                    groups.setdefault((tuple(a["accessors"]), tuple(a["targets"])), []).append(k)
        new_entries = []
        for (intr, tg), comm in groups.items():
            named_only = [x for x in intr if x not in comm]
            eid = "+".join(sorted(intr)) + ">" + "+".join(tg)
            ent = {"turn": self.turn, "committers": comm, "targets": list(tg),
                   "succeeded": None, "gains": {}, "penalised": [], "kind": {},
                   "named_only": named_only}
            p = p_success(len(tg))
            ok = self.u("success", eid, None) < p
            ent["succeeded"] = ok
            if not ok:
                for c in comm:
                    self.inf[c] = max(0.0, self.inf[c] - FAIL_PEN)
                    ent["penalised"].append(c)
                    ent["kind"][c] = "named_on_failure"
                    ent["gains"][c] = 0.0
                self.notices.append(("tenancy_access_failed", tuple(sorted(comm))))
            else:
                for c in comm:
                    g = sum(gain_of(self.snap[c], self.snap[t]) for t in tg)
                    ent["gains"][c] = g
                    self.K[c] += g
                exposed = []
                for c in comm:
                    g = ent["gains"][c]
                    if self.u("exposure", eid, c) < q_exposure(g, len(comm)):
                        self.K[c] -= g
                        ent["gains"][c] = 0.0
                        self.inf[c] = max(0.0, self.inf[c] - CAUGHT_PEN)
                        ent["penalised"].append(c)
                        ent["kind"][c] = "exposed"
                        exposed.append(c)
                if exposed:
                    self.notices.append(("tenancy_access_exposed", tuple(sorted(exposed))))
                ul = self.u("lead", eid, None)
                if ul < LEAD_P:
                    non = [x for x in KEYS if x not in comm]
                    un = [c for c in comm if c not in exposed]
                    if not un:
                        self.tags.add("SILENT_lead_all_exposed")
                    if un and non:
                        named = un[min(len(un) - 1, int(ul / LEAD_P * len(un)))]
                        rec = non[min(len(non) - 1, int(self.u("lead_recipient", eid, None) * len(non)))]
                        self.leads.append((rec, named))
            new_entries.append(ent)
        # ---- pass 7: whistleblow
        pen_now = {}

        def bump(k, d):
            self.inf[k] = max(0.0, min(100.0, self.inf[k] + d))

        def forfeit(e, c, kind):
            if (c, id(e)) in pen_now:
                return False
            self.K[c] -= e["gains"].get(c, 0.0)
            e["gains"][c] = 0.0
            if c not in e["penalised"]:
                e["penalised"].append(c)
                e["kind"][c] = kind
            pen_now[(c, id(e))] = kind
            return True

        # collisions are spec-silent: tag
        hit = Counter()
        for k, r in reports.items():
            if r["entry"] is not None and r["status"] == "open":
                hit[(r["accused"], id(r["entry"]))] += 1
                if r["partner"]:
                    hit[(k, id(r["entry"]))] += 1
        if any(v > 1 for v in hit.values()):
            self.tags.add("SILENT_same_part_twice_in_turn")
        for k, r in reports.items():
            if r["self_rep"] and r["status"] == "open":
                forfeit(r["entry"], k, "self_report")
            elif r["partner"]:
                forfeit(r["entry"], k, "self_report")
        for k in KEYS:
            if k not in reports:
                continue
            r = reports[k]
            st = r["status"]
            if (not r["self_rep"] and st == "open"
                    and pen_now.get((r["accused"], id(r["entry"]))) == "self_report"):
                st = "penalised"
            if st == "open":
                if r["self_rep"]:
                    self.outcomes[k] = "self_report"
                else:
                    if forfeit(r["entry"], r["accused"], "upheld"):
                        bump(r["accused"], -UPHELD_PEN)
                    bump(k, r["base"])
                    self.outcomes[k] = "upheld"
            elif st == "penalised":
                bump(k, r["base"])
                self.outcomes[k] = "returned"
            else:
                if not r["self_rep"]:
                    bump(k, -NOT_UPHELD_PEN)
                self.outcomes[k] = "not_upheld"
        return new_entries


# ---------------------------------------------------------------- harness
def run_one(seed, scenario, turns, event_turn, condition="A"):
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in KEYS})
    plan = Plan(seed, scenario)
    pols = {k: make_policy(plan, k) for k in KEYS}
    eng = SimulationEngine(labs, world, cfg, scenario=scenario, condition=condition,
                           turns=turns, seed=seed, run_grand_jury=False, seat_policies=pols,
                           parallel=False, event_turn=event_turn)
    problems = []
    tags = Counter()
    stats = Counter()
    prompts = {}
    orig_exec = eng._execute
    orig_collect = eng._collect_decisions

    def collect(turn):
        prompts[turn] = {lab.key: eng._turn_prompt(turn, lab) for lab in eng.labs}
        summ = eng._state_summary()
        for bad in ("diminish_competitor", "invest_capital", "access a tenancy on", "credible tip",
                    "capability copied 0", "capability copied 1", "capability copied 2", "capability copied 3", "named but did not commit", "build_influence", "lobby_institution"):
            if bad in summ:
                problems.append((f"seed={seed} turn={turn}", "macro summary leak", bad))
        for lab in eng.labs:
            pr = prompts[turn][lab.key]
            n_named = sum(1 for e in eng.world.ledger
                          if lab.key in (e.draws or {}).get("named_not_committed", []))
            has = "Accesses you were named in" in pr
            if bool(n_named) != has:
                problems.append((f"seed={seed} turn={turn}", f"named section {lab.key}", n_named, has))
            n_own = sum(1 for e in eng.world.ledger if lab.key in e.committers)
            n_cop = len(re.findall(r"capability copied \d", pr))
            n_mine_succ = sum(1 for e in eng.world.ledger if lab.key in e.committers and e.succeeded
                              and lab.key not in e.penalised)
            if n_cop > n_mine_succ * 2:
                problems.append((f"seed={seed} turn={turn}", f"copied lines {lab.key}", n_cop, n_mine_succ))
        return orig_collect(turn)

    def execute(turn, decisions, snapshot_c, growth):
        pre = {lab.key: dict(capital=lab.capital, influence=lab.influence, know_how=lab.know_how)
               for lab in eng.labs}
        ledger_pre = [dict(turn=e.turn, committers=list(e.committers), targets=list(e.targets),
                           gains=dict(e.gains), penalised=list(e.penalised),
                           kind=dict(e.penalty_kind), id=e.id) for e in world.ledger]
        n_ledger = len(world.ledger)
        n_notices = len(world.notices)
        frozen = world.ceiling is not None and sum(l.compute for l in eng.labs) > world.ceiling
        open_ = world.tenancy_access_open
        rec = orig_exec(turn, decisions, snapshot_c, growth)
        draws = {tuple(d["key"]): d["u"] for d in eng.keyed._log}
        intent = {k: plan.intent[(turn, k)] for k in KEYS}
        ref = Ref(turn, scenario, pre, ledger_pre, snapshot_c, draws, intent, open_, seed, frozen)
        new_ref = ref.run()
        for t in ref.tags:
            tags[t] += 1
        silent = bool(ref.tags)
        where = f"seed={seed} {scenario} turn={turn}"
        if ref.missing_draws:
            problems.append((where, "missing draws", ref.missing_draws))
        # accepted actions
        for k in KEYS:
            got = [(a["type"], tuple(a.get("targets", ())), tuple(a.get("accessors", ())))
                   for a in rec["actors"][k]["accepted"]]
            want = [(a["type"], tuple(a.get("targets", ())), tuple(a.get("accessors", ())))
                    for a in ref.accepted[k]]
            if got != want:
                problems.append((where, f"accepted {k}", got, want, rec["actors"][k]["rejected"]))
        # resources
        for lab in eng.labs:
            k = lab.key
            for name, a, b in (("capital", lab.capital, ref.cap[k]),
                               ("influence", lab.influence, ref.inf[k]),
                               ("know_how", lab.know_how, ref.K[k])):
                if abs(a - b) > 1e-6:
                    problems.append((where, f"{name} {k}", round(a, 4), round(b, 4),
                                     "silent" if silent else ""))
        # new ledger entries
        new = world.ledger[n_ledger:]
        got_e = sorted((tuple(e.committers), tuple(e.targets), e.succeeded,
                        tuple(sorted(e.penalty_kind.items()))) for e in new)
        want_e = sorted((tuple(e["committers"]), tuple(e["targets"]), e["succeeded"],
                         tuple(sorted(e["kind"].items()))) for e in new_ref)
        if got_e != want_e:
            problems.append((where, "new ledger", got_e, want_e))
        # old ledger penalty kinds after reports
        for e in world.ledger[:n_ledger]:
            r = next(x for x in ref.ledger if x["id"] == e.id)
            if dict(e.penalty_kind) != r["kind"]:
                problems.append((where, f"ledger {e.id} kinds", dict(e.penalty_kind), r["kind"],
                                 "silent" if silent else ""))
            if len(set(e.penalised)) != len(e.penalised):
                problems.append((where, f"ledger {e.id} penalised twice", e.penalised))
        # report outcomes
        got_o = {o["reporter"]: o["result"] for o in rec["whistleblow"]["outcomes"]}
        if got_o != ref.outcomes:
            problems.append((where, "wb outcomes", got_o, ref.outcomes, "silent" if silent else ""))
        # leads
        got_l = sorted((l["recipient"], l["named"]) for l in rec["leads"])
        if got_l != sorted(ref.leads):
            problems.append((where, "leads", got_l, sorted(ref.leads)))
        for l in rec["leads"]:
            e = next(x for x in world.ledger if x.id == l["entry_id"])
            if l["recipient"] in e.committers:
                problems.append((where, "lead to committer", l))
            stats["leads"] += 1
        # tenancy access notices (public)
        notes = world.notices[n_notices:]
        got_n = sorted((n["kind"], tuple(sorted(n["labs"]))) for n in notes
                       if n["kind"].startswith("tenancy_access"))
        if got_n != sorted(ref.notices):
            problems.append((where, "tenancy access notices", got_n, sorted(ref.notices)))
        for o, n in Counter(r for r in ref.outcomes.values()).items():
            stats[f"wb_{o}"] += n
        for e in new:
            stats["entries"] += 1
            stats["joint" if len(e.committers) > 1 else "solo"] += 1
            stats["succ" if e.succeeded else "fail"] += 1
            stats["exposed"] += len(e.exposed)
            stats["named_only"] += len(e.draws.get("named_not_committed", []))
        rec["_ref_leads"] = ref.leads
        return rec

    eng._execute = execute
    eng._collect_decisions = collect
    record = eng.run()

    # ---- leak checks on prompts
    for tr in record["turns"]:
        t = tr["turn"]
        nxt = prompts.get(t + 1)
        if not nxt:
            continue
        for lead in tr["leads"]:
            named = DISPLAY[lead["named"]]
            tip = f"a credible tip indicates {named} accessed"
            for k, p in nxt.items():
                has = tip in p and lead["access_month"] in p.split("Tips received:")[-1]
                if k == lead["recipient"] and not has:
                    problems.append((f"seed={seed} turn={t}", "tip not delivered next turn", lead))
                if k != lead["recipient"] and "Tips received:" in p:
                    # tips may come from other leads; check this one isn't there unless also recipient
                    own = [l for trx in record["turns"] if trx["turn"] <= t for l in trx["leads"]
                           if l["recipient"] == k]
                    if not own:
                        problems.append((f"seed={seed} turn={t}", f"tip leaked to {k}", lead))
        for k, p in nxt.items():
            # 0.45 / 0.55 as odds, not an amount ("copied 0.45", "needs 1.00 Capital, has 0.45")
            amount = r"(?<!copied )(?<!has )(?<!needs )"
            unit = r"(?!\s*(?:Capital|Influence|compute|units?|capability|know-how)\b)"
            for pat in (amount + r"\b0\.45\b" + unit, amount + r"\b0\.55\b" + unit,
                        r"\bodds\b", r"probabilit", r"\b85%", r"\b45%", r"\b15%", r"\bq\s*="):
                if re.search(pat, p):
                    m = re.search(pat, p); problems.append((f"seed={seed} turn={t}", f"odds leak to {k}", pat, p[max(0, m.start()-80):m.end()+20]))
            for other in KEYS:
                if other == k:
                    continue
                # true actions of others: invest/diminish lines are only in the owner's record
                for a in tr["actors"][other]["accepted"]:
                    if a["type"] == "access_tenancy":
                        txt = "access_tenancy of " + " and ".join(DISPLAY[x] for x in a["targets"])
                        pp = "\n".join(ln for ln in p.splitlines() if not ln.strip().startswith("Rejected:"))
                        if txt in pp:
                            i = pp.index(txt); p = pp; problems.append((f"seed={seed} turn={t}", f"true access_tenancy of {other} in {k}'s prompt", p[max(0,i-300):i+100]))
            # hidden ledger: "Unauthorised accesses on your record" only lists own entries
            sec = p.split("Unauthorised accesses on your record:")
            if len(sec) > 1:
                own_entries = [e for e in eng.world.ledger if k in e.committers and e.turn <= t]
                n_lines = len([ln for ln in sec[1].split("\n\n")[0].splitlines() if ln.strip().startswith("-")])
                if n_lines and n_lines != len(own_entries):
                    problems.append((f"seed={seed} turn={t}", f"access list of {k}", n_lines, len(own_entries)))
    return problems, tags, stats


# =====================================================================
# Tests
# =====================================================================
import pytest  # noqa: E402

from core import whistleblow  # noqa: E402
from core.actions import precheck  # noqa: E402
from core.state import LedgerEntry  # noqa: E402
from core.whistleblow import _parse_turn  # noqa: E402


@pytest.mark.parametrize("seed", list(range(12)))
def test_differential_engine_matches_spec_reference(seed):
    """§5.3 four-step resolution, whistleblow table, joint rule, once-per-part ledger,
    tips (never to a committer, next packet), F3 and prompt leaks: the engine agrees
    with an independent reference over randomized scripted policies (S1 and S2,
    6-12 turns, event on turn 1 or 2)."""
    scen = "S2" if seed % 3 else "S1"
    problems, tags, stats = run_one(1000 + seed, scen, 6 + seed % 7, 1 if seed % 5 else 2)
    assert stats["entries"] > 0                    # the run exercised tenancy accesses
    assert problems == []


def test_an_amount_of_0_45_is_not_read_as_odds():
    """Seed 7001 (S2/B, event turn 2): a rejection line "has 0.45" Capital is an amount."""
    problems, _tags, _stats = run_one(7001, "S2", 12, 2, condition="B")
    assert problems == []


def _setup(influence=50.0):
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in KEYS})
    for lab in labs:
        lab.influence = influence
    world.turn = 2
    return cfg, labs, world, {lab.key: lab for lab in labs}


def _joint(world, penalised=()):
    e = LedgerEntry(id="L01-1", turn=1, succeeded=True, committers=["meta", "xai"],
                    targets=["openai"], gains={"meta": 1.0, "xai": 1.2},
                    copied={"meta": 1.0, "xai": 1.2})
    for k in penalised:
        e.gains[k] = 0.0
        e.exposed.append(k)
        e.penalise(k, "exposed")
    world.ledger.append(e)
    return e


def test_partner_report_both_parts_open():
    """§5.3 Joint tenancy accesses: the reporter pays 3 (refunded, upheld) + 5 and forfeits
    its copy; the partner forfeits and loses 15."""
    cfg, labs, world, by = _setup()
    e = _joint(world)
    k_meta, k_xai = by["meta"].know_how, by["xai"].know_how
    out = whistleblow.resolve({"xai": {"accused": "Meta"}}, labs, world, cfg)
    assert out["outcomes"][0]["result"] == "upheld"
    assert by["xai"].influence == pytest.approx(45.0)
    assert by["meta"].influence == pytest.approx(35.0)
    assert by["meta"].know_how == pytest.approx(k_meta - 1.0)
    assert by["xai"].know_how == pytest.approx(k_xai - 1.2)
    assert e.penalty_kind == {"xai": "self_report", "meta": "upheld"}


def test_partner_report_own_part_already_penalised():
    """§5.3: if its own part is already penalised, it pays only the 3 Inf (refunded)."""
    cfg, labs, world, by = _setup()
    e = _joint(world, penalised=["xai"])
    whistleblow.resolve({"xai": {"accused": "Meta"}}, labs, world, cfg)
    assert by["xai"].influence == pytest.approx(50.0)
    assert by["meta"].influence == pytest.approx(35.0)
    assert e.penalty_kind == {"xai": "exposed", "meta": "upheld"}


def test_partner_report_only_partner_penalised_still_self_reports():
    """§5.3: if only the partner's part is penalised, the report is returned (3
    refunded) and the self-report still applies (5 paid, own copy forfeited)."""
    cfg, labs, world, by = _setup()
    e = _joint(world, penalised=["meta"])
    k_xai = by["xai"].know_how
    out = whistleblow.resolve({"xai": {"accused": "meta", "month": "August 2026"}},
                              labs, world, cfg)
    assert out["outcomes"][0]["result"] == "returned"
    assert by["xai"].influence == pytest.approx(45.0)
    assert by["xai"].know_how == pytest.approx(k_xai - 1.2)
    assert by["meta"].influence == pytest.approx(50.0)
    assert e.penalty_kind == {"meta": "exposed", "xai": "self_report"}


def test_named_but_not_committing_partner_pays_nothing_and_can_report():
    """§5.3 step 1: a named lab that does not commit pays nothing, gains nothing and
    can report the tenancy access if it succeeds (upheld, fee refunded, no self-report)."""
    cfg, labs, world, by = _setup()
    world.turn = 4
    world.tenancy_access_open = True
    a = {"type": "access_tenancy", "accessors": ["meta", "xai"], "targets": ["openai"]}
    from core import tenancy_access
    snap = {lab.key: lab.capability(3.6) for lab in labs}
    before = {k: (by[k].capital, by[k].influence, by[k].know_how) for k in KEYS}
    res = tenancy_access.resolve({"meta": a}, labs, snap, world, cfg, 7)
    entry = res["entries"][0]
    assert entry.committers == ["meta"]
    assert entry.draws["named_not_committed"] == ["xai"]
    assert (by["xai"].capital, by["xai"].influence, by["xai"].know_how) == before["xai"]
    assert entry.succeeded and "meta" not in entry.penalised     # seed 7: success, unexposed
    world.turn = 5
    inf = by["xai"].influence
    out = whistleblow.resolve({"xai": {"accused": "Meta"}}, labs, world, cfg)
    assert out["outcomes"][0]["result"] == "upheld"
    assert out["outcomes"][0]["partner_self_report"] is False
    assert by["xai"].influence == pytest.approx(inf)


def test_self_report_costs_five_and_forfeits_and_is_refunded_when_already_penalised():
    """§5.3 Self-report row and A.8: 5 Inf, own copy forfeited; returned (refunded)
    when the matched part was already penalised."""
    cfg, labs, world, by = _setup()
    _joint(world)
    k = by["meta"].know_how
    whistleblow.resolve({"meta": {"accused": "our own lab"}}, labs, world, cfg)
    assert by["meta"].influence == pytest.approx(45.0)
    assert by["meta"].know_how == pytest.approx(k - 1.0)
    world.turn = 3
    out = whistleblow.resolve({"meta": {"accused": "self"}}, labs, world, cfg)
    assert out["outcomes"][0]["result"] == "returned"
    assert by["meta"].influence == pytest.approx(45.0)


@pytest.mark.parametrize("scenario", ["S1", "S2"])
def test_second_access_tenancy_in_one_month_is_rejected_in_world(scenario):
    """§5.3 action table: one access_tenancy per lab a month (A.4/A.5 "One access_tenancy a month.").
    A second access_tenancy is rejected with that in-world reason; the first stands."""
    cfg, labs, world, by = _setup()
    world.tenancy_access_open = True
    acts = [{"type": "access_tenancy", "targets": ["openai"]},
            {"type": "access_tenancy", "targets": ["anthropic"]}]
    accepted, rejected = precheck(by["xai"], acts, labs, world, cfg, scenario)
    assert [a["targets"] for a in accepted] == [["openai"]]
    assert [r["reason"] for r in rejected] == ["one access_tenancy a month"]


def test_month_field_with_modal_may_reads_the_named_month():
    """§5.3 Matching: 'in the stated month if given'. 'may have been
    October 2026' states October 2026 (turn 3 of a run from August 2026), not May 2026."""
    assert _parse_turn("may have been October 2026", date(2026, 8, 1), 6) == 3
    assert _parse_turn("It may be October", date(2026, 8, 1), 6) == 3
    assert _parse_turn("May 2026", date(2026, 8, 1), 6) == -2


def test_every_report_outcome_is_public():
    """§5.3 whistleblow table: 'Effect (all public)'. A second report
    that is returned is still a public outcome."""
    cfg, labs, world, by = _setup()
    _joint(world, penalised=["meta", "xai"])
    out1 = whistleblow.resolve({"openai": {"accused": "Meta"}}, labs, world, cfg)
    world.turn = 3
    out2 = whistleblow.resolve({"openai": {"accused": "Meta"}}, labs, world, cfg)
    assert out1["outcomes"][0]["result"] == out2["outcomes"][0]["result"] == "returned"
    assert out2["outcomes"][0]["notice"] is True
