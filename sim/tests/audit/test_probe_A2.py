"""Audit A2 probes: spec §5.1-§5.3 (who acts, turn structure, action set).
A probe FAILS when the suspected mismatch is present; `# verified` probes pass."""

import json
from datetime import date

import pytest

from core.actions import apply, charge, precheck
from core.config import build_labs_and_world, load_lab_configs, load_world
from core.economy import macro_growth, purchase_price
from core.engine import SimulationEngine
from core.intrusion import exposure_probability, resolve as intr_resolve
from core.llm import register_stub
from core.rng import KeyedRNG
from core.state import VALUE_AXES, LedgerEntry, WorldState, build_lab
from core.whistleblow import resolve as wb_resolve, validate_report

CFG = load_world()
A = CFG["economy"]["capability_compute_elasticity"]
ICFG = CFG["intrusion"]
WCFG = CFG["whistleblow"]
KEYS = ("anthropic", "openai", "gdm", "meta", "xai")


def mk(turn=5, open_=True):
    labs = [build_lab(c, "", A) for c in load_lab_configs()]
    world = WorldState(start=date(2026, 8, 1), us_stock=2450.0, us_growth=150.0, turn=turn,
                       intrusion_open=open_, state_values={a: 50.0 for a in VALUE_AXES})
    return labs, world, {l.key: l for l in labs}


class Scripted(KeyedRNG):
    def __init__(self, **v):
        super().__init__(0)
        self.v = v

    def uniform(self, turn, purpose, entry_id=None, lab=None):
        x = self.v.get(purpose, 0.99)
        if isinstance(x, dict):
            x = x.get(lab, 0.99)
        self._log.append({"key": [turn, purpose, entry_id, lab], "u": x})
        return x


# ---------------------------------------------------------------------------
# Findings (expected to FAIL while the mismatch is present)
# ---------------------------------------------------------------------------

def test_A2_1_only_two_submitted_actions_count():
    """§5.1 "Each turn a lab submits up to two actions". A reply listing four actions
    whose first two fail the pre-check must not get actions 3 and 4 executed as
    fall-backs (prompts.turn.validate_turn_reply also claims extras '>2 ... are dropped')."""
    labs, world, k = mk(open_=False)
    me = k["openai"]
    acts = [{"type": "invest_capital", "amount": 1e6},      # unaffordable -> rejected
            {"type": "intrude", "targets": ["meta"]},         # intrusion closed -> rejected
            {"type": "invest_capital", "amount": 5},          # 3rd submitted
            {"type": "lobby_institution"}]                    # 4th submitted
    accepted, rejected = precheck(me, acts, labs, world, CFG, "S1")
    assert accepted == [], f"actions beyond the first two submitted were accepted: {accepted}"


@pytest.mark.xfail(strict=True, reason='refuted on verification (see audit/AUDIT_2026-10-13.md): A2-2 one intrude carried out a month; the slot fix (A2-1) bounds it')
def test_A2_2_second_intrude_not_a_fallback():
    """§5.3 "one intrude per lab a month": a seat that submits two intrudes gets the
    second accepted when the first fails pre-check (fall-back target)."""
    labs, world, k = mk()
    me = k["xai"]
    acts = [{"type": "intrude", "targets": ["xai"]},          # self-target -> rejected
            {"type": "intrude", "targets": ["openai"]}]
    accepted, rejected = precheck(me, acts, labs, world, CFG, "S1")
    assert not any(a["type"] == "intrude" for a in accepted), accepted


@pytest.mark.xfail(strict=True, reason='spec question for David (code kept; see audit/AUDIT_2026-10-13.md): A2-3 a tip names an unexposed committer')
def test_A2_3_lead_fires_even_when_every_committer_was_caught():
    """§5.3 step 4: "one draw decides whether a true tip naming one committer goes to a
    random non-committer". Code names only unexposed committers, so a lead draw that
    fires after every committer is caught sends nothing."""
    labs, world, k = mk()
    snap = {l.key: l.capability(A) for l in labs}
    rng = Scripted(success=0.0, exposure={"xai": 0.0}, lead=0.01, lead_recipient=0.5)
    out = intr_resolve({"xai": {"type": "intrude", "intruders": ["xai"], "targets": ["openai"]}},
                       labs, snap, world, CFG, rng)
    assert out["entries"][0].exposed == ["xai"]
    assert out["entries"][0].draws["lead"]["drawn"] is True
    assert len(out["leads"]) == 1, "lead draw fired but no tip was sent"


@pytest.mark.xfail(strict=True, reason='spec question for David (code kept; see audit/AUDIT_2026-10-13.md): A2-4 §5.2 does not fix the order effects apply in')
def test_A2_4_lobby_institution_is_lab_order_dependent():
    """§5.2 step 5 execution after simultaneous proposals: two lobbies in the same
    month give a result that depends on lab order (pass 4 applies them in sequence)."""
    def run(order):
        labs, world, k = mk()
        k["anthropic"].values = {a: 50.5 for a in VALUE_AXES}
        k["openai"].values = {a: 49.5 for a in VALUE_AXES}
        for key in order:
            apply(k[key], {"type": "lobby_institution"}, labs, world, CFG)
        return dict(world.state_values)
    assert run(["anthropic", "openai"]) == run(["openai", "anthropic"])


def test_A2_5_cli_default_message_mode_for_S2_is_two_round():
    """§5.2 step 2 / §9.2 Budget: "T9 is cut first, and S2 and the kin cell then keep the
    two-round pre-step" - the un-decided default for S2 is separate. main.py defaults
    --a2a to merged (the engine itself defaults S2 to separate)."""
    import main
    captured = {}

    class Engine:
        def __init__(self, *a, **kw):
            captured["a2a"] = kw.get("a2a_mode")
            raise SystemExit(0)

    import pytest as _pt
    mp = _pt.MonkeyPatch()
    mp.setattr(main, "SimulationEngine", Engine)
    try:
        for argv in (["--scenario", "S2", "--dry-run"], ["--kin", "--dry-run"]):
            args = main.build_parser().parse_args(argv + ["--output", "/nonexistent-audit-probe"])
            try:
                main.run_cell(args)
            except SystemExit:
                pass
            assert captured["a2a"] == "separate", argv
    finally:
        mp.undo()


# ---------------------------------------------------------------------------
# Verified claims (pass)
# ---------------------------------------------------------------------------

def test_verified_joint_fee_gain_exposure_two_targets():  # verified
    """S2 joint intrusion, two targets: each committer pays 2 Capital + 4 Inf; gain per
    target max(0.35, 0.3*gap) summed; q = min(.45, (.1 + .04 gain)/2); not split."""
    labs, world, k = mk()
    snap = {l.key: l.capability(A) for l in labs}
    act = {"type": "intrude", "intruders": ["meta", "xai"], "targets": ["anthropic", "openai"]}
    before = {x: (k[x].capital, k[x].influence, k[x].know_how) for x in ("meta", "xai")}
    for x in ("meta", "xai"):
        acc, rej = precheck(k[x], [dict(act)], labs, world, CFG, "S2")
        assert acc and not rej
        ch = charge(k[x], acc[0], CFG)
        assert ch == {"capital": 2.0, "influence": 4.0}
    rng = Scripted(success=0.0)
    out = intr_resolve({"meta": act, "xai": act}, labs, snap, world, CFG, rng, fees_charged=True)
    e = out["entries"][0]
    assert e.draws["success"]["p"] == pytest.approx(0.75)
    for x in ("meta", "xai"):
        g = sum(max(0.35, 0.3 * (snap[t] - snap[x])) for t in ("anthropic", "openai"))
        assert e.gains[x] == pytest.approx(g)
        assert k[x].know_how == pytest.approx(before[x][2] + g)
        assert e.draws["exposure"][x]["q"] == pytest.approx(min(0.45, (0.1 + 0.04 * g) / 2))
    assert k["anthropic"].know_how == build_lab(load_lab_configs()[0], "", A).know_how


def test_verified_failure_and_caught_penalties():  # verified
    labs, world, k = mk()
    snap = {l.key: l.capability(A) for l in labs}
    inf, cap, kh = k["xai"].influence, k["xai"].capital, k["xai"].know_how
    out = intr_resolve({"xai": {"type": "intrude", "intruders": ["xai"], "targets": ["openai"]}},
                       labs, snap, world, CFG, Scripted(success=0.99))
    assert k["xai"].influence == pytest.approx(inf - 2 - 10) and k["xai"].capital == cap - 1
    assert k["xai"].know_how == kh and out["notices"][0]["kind"] == "intrusion_failed"
    labs, world, k = mk()
    inf, kh = k["xai"].influence, k["xai"].know_how
    out = intr_resolve({"xai": {"type": "intrude", "intruders": ["xai"], "targets": ["openai"]}},
                       labs, snap, world, CFG, Scripted(success=0.0, exposure={"xai": 0.0}))
    assert k["xai"].influence == pytest.approx(inf - 2 - 15) and k["xai"].know_how == pytest.approx(kh)


def test_verified_named_non_committer_pays_nothing():  # verified
    labs, world, k = mk()
    snap = {l.key: l.capability(A) for l in labs}
    m = (k["meta"].capital, k["meta"].influence, k["meta"].know_how)
    act = {"type": "intrude", "intruders": ["meta", "xai"], "targets": ["openai"]}
    out = intr_resolve({"xai": act}, labs, snap, world, CFG, Scripted(success=0.0))
    e = out["entries"][0]
    assert e.committers == ["xai"] and (k["meta"].capital, k["meta"].influence, k["meta"].know_how) == m
    assert e.draws["exposure"]["xai"]["q"] == pytest.approx(
        exposure_probability(e.gains["xai"], 1, ICFG))


def test_verified_whistleblow_partner_and_fees():  # verified
    labs, world, k = mk(turn=6)
    world.ledger = [LedgerEntry(id="L05-1", turn=5, succeeded=True, committers=["meta", "xai"],
                                targets=["openai"], gains={"meta": 2.0, "xai": 3.0})]
    xi, mi = k["xai"].influence, k["meta"].influence
    rep, _ = validate_report({"accused": "Meta"}, k["xai"], labs, world, CFG)
    assert rep["fee"] == 8.0
    out = wb_resolve({"xai": {"accused": "Meta"}}, labs, world, CFG)
    o = out["outcomes"][0]
    assert o["result"] == "upheld" and o["partner_self_report"]
    assert k["xai"].influence == pytest.approx(xi - 5)          # 3 refunded, 5 kept
    assert k["meta"].influence == pytest.approx(mi - 15)
    assert world.ledger[0].penalty_kind == {"xai": "self_report", "meta": "upheld"}


def test_verified_costs_and_min_cost():  # verified
    labs, world, k = mk(open_=False)
    me = k["openai"]
    me.capital = 1000.0
    assert purchase_price(10, CFG["compute"]) == pytest.approx(3.5)
    _, rej = precheck(me, [{"type": "acquire_compute", "units": 2}], labs, world, CFG, "S1")
    assert rej and "at least" in rej[0]["reason"]                  # 0.7 Capital < 1
    acc, _ = precheck(me, [{"type": "acquire_compute", "units": 20},
                           {"type": "acquire_compute", "units": 20}], labs, world, CFG, "S1")
    assert sum(a["units"] for a in acc) == 30
    me.influence = 50.0
    ch = charge(me, {"type": "build_influence", "points": 2}, CFG)
    assert ch["capital"] == 60.0
    me.invested = 0.0
    me.capital = 100.0
    acc, _ = precheck(me, [{"type": "invest_capital", "amount": 10}], labs, world, CFG, "S1")
    charge(me, acc[0], CFG)
    apply(me, acc[0], labs, world, CFG)
    world.turn = 6
    rec = macro_growth(labs, world, CFG, None, shock_uniform=lambda t, kk: 0.5)
    assert rec["repaid"]["openai"] == pytest.approx(10.08)


def test_verified_report_fee_reserved_from_actions():  # verified
    labs, world, k = mk(turn=6)
    me = k["xai"]
    me.influence = 4.0
    acc, rej = precheck(me, [{"type": "intrude", "targets": ["openai"]}], labs, world, CFG, "S1",
                        reserved_influence=3.0)
    assert not acc and "reserved" in rej[0]["reason"]


@pytest.mark.xfail(strict=True, reason='spec question for David (code kept; see audit/AUDIT_2026-10-13.md): A2-6 which penalised part a returned report matches')
def test_A2_6_partner_report_when_partner_parts_all_penalised():
    """§5.3 joint intrusions: "if only the partner's [part] is [penalised], the
    self-report still applies" (A.8: "A report naming a partner in an access you also
    committed counts as a report on your own lab as well"). When the partner also has an
    earlier, penalised solo part, find_part returns that earlier entry, so the reporter's
    own unpenalised part in the joint entry is never self-reported."""
    labs, world, k = mk(turn=6)
    solo = LedgerEntry(id="L04-1", turn=4, succeeded=True, committers=["meta"],
                       targets=["openai"], gains={"meta": 0.0})
    solo.penalise("meta", "exposed")
    joint = LedgerEntry(id="L05-1", turn=5, succeeded=True, committers=["meta", "xai"],
                        targets=["openai"], gains={"meta": 0.0, "xai": 3.0})
    joint.penalise("meta", "exposed")
    world.ledger = [solo, joint]
    out = wb_resolve({"xai": {"accused": "Meta"}}, labs, world, CFG)
    o = out["outcomes"][0]
    assert o["result"] == "returned"
    assert o["partner_self_report"] is True and joint.penalty_kind.get("xai") == "self_report"
