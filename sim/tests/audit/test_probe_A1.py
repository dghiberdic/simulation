"""Audit A1 probes: spec §2, §3 (3.1-3.3), §4, Appendix B starting values.

Probes that assert spec behaviour and FAIL while the mismatch is present are
marked "# probe"; claims checked and found correct are marked "# verified".
"""
import math
import random

import pytest

from core.config import build_labs_and_world, load_world
from core.economy import (capital_income, execute_purchases, macro_growth, purchase_price,
                          talent_target, value_pull)
from core.fmt import value_text
from core.actions import precheck
from core.state import VALUE_AXES

SEED = {  # spec §4 table
    "anthropic": (24.6, 246, 28, 78, 162.8),
    "openai": (21.9, 310, 50, 68, 162.1),
    "gdm": (17.1, 261, 42, 66, 154.8),
    "meta": (19.3, 164, 26, 55, 154.2),
    "xai": (17.1, 95, 15, 41, 153.9),
}


def _setup(overrides=None):
    cfg = load_world(overrides)
    labs, world = build_labs_and_world(cfg, dispositions={k: 50 for k in SEED}, charters=False)
    return cfg, labs, world


# ---------------------------------------------------------------- verified
def test_seed_table_and_t0_capability():  # verified
    cfg, labs, world = _setup()
    a = cfg["economy"]["capability_compute_elasticity"]
    assert a == 3.6
    for lab in labs:
        t, c, cap, inf, eci = SEED[lab.key]
        assert (lab.talent, lab.compute, lab.capital, lab.influence) == (t, c, cap, inf)
        assert lab.capability(a) == pytest.approx(eci, abs=1e-9)          # K = seed − a·ln(compute)
    assert sum(l.talent for l in labs) == pytest.approx(100.0)
    share = sum(l.compute for l in labs) / world.us_stock
    assert 0.435 < share < 0.445                                          # "about 44%"
    assert world.national_cap() == 1225.0


def test_income_at_seed_is_cap0_over_8():  # verified
    cfg, labs, _ = _setup()
    for lab in labs:
        assert capital_income(lab, cfg["economy"]) == pytest.approx(lab.seed["capital"] / 8)
        assert 8 * capital_income(lab, cfg["economy"]) == pytest.approx(lab.seed["capital"])
    lab = labs[0]
    lab.influence = lab.seed["influence"] * 4      # Inf ×4 -> income ×2
    assert capital_income(lab, cfg["economy"]) == pytest.approx(2 * lab.seed["capital"] / 8)


def test_talent_target_floor_and_formula():  # verified
    cfg, labs, _ = _setup()
    econ = cfg["economy"]
    lab = labs[0]
    i0 = lab.seed["capital"] / 8
    assert talent_target(lab, i0, econ) == pytest.approx(lab.seed["talent"])
    assert talent_target(lab, 0.0, econ) == pytest.approx(0.5 * lab.seed["talent"])
    assert talent_target(lab, 4 * i0, econ) == pytest.approx(2 * lab.seed["talent"])


def test_macro_growth_turn1_turn2_no_shock():  # verified
    cfg, labs, world = _setup({"economy.know_how_shock_sd": 0.0})
    k0 = {l.key: l.know_how for l in labs}
    world.turn = 1
    macro_growth(labs, world, cfg, random.Random(0))
    assert (world.us_stock, world.china_stock) == (2450.0, 260.0)   # growth only from turn 2
    for l in labs:
        assert l.know_how - k0[l.key] == pytest.approx(0.85 * l.talent / 20)  # ΔK uses talent share ÷ 20%
    assert sum(l.talent for l in labs) == pytest.approx(100.0)
    world.turn = 2
    macro_growth(labs, world, cfg, random.Random(0))
    assert (world.us_stock, world.china_stock) == (2600.0, 271.0)


def test_purchase_price_and_cap_trim():  # verified
    cfg, labs, world = _setup()
    assert purchase_price(1, cfg["compute"]) == pytest.approx(0.35)
    lab = labs[0]
    acc, rej = precheck(lab, [{"type": "acquire_compute", "units": 20},
                              {"type": "acquire_compute", "units": 20}], labs, world, cfg, "S1")
    assert sum(a["units"] for a in acc) == pytest.approx(30.0)


def test_value_pull_rule():  # verified
    vals = {a: 50.0 for a in VALUE_AXES}
    state = {a: 70 for a in VALUE_AXES}
    charter = {a: 30 for a in VALUE_AXES}
    out = value_pull(vals, state, charter, 50, 1)   # target 50 -> no move
    assert out == vals
    out = value_pull(vals, state, charter, 0, 1)    # target 70 -> +1 (unrounded)
    assert all(v == 51.0 for v in out.values())
    out = value_pull({a: 69.6 for a in VALUE_AXES}, state, charter, 0, 1)
    assert all(v == pytest.approx(70.0) for v in out.values())
    assert value_text(64.5) == "65" and value_text(64.49) == "64"


# ---------------------------------------------------------------- probes
@pytest.mark.xfail(strict=True, reason='spec question for David (code kept; see audit/AUDIT_2026-10-13.md): A1-1 §3.1/§5.3 sanction pro-rata charging; the minimum applies to the action as submitted')
def test_A1_1_prorated_purchase_costs_at_least_one():  # probe
    """§3.3 'every action costs at least 1 Capital or Influence'. A pro-rated (or
    zero-headroom) acquire_compute is charged on granted units only, so an accepted
    action can cost < 1 Capital, or 0 when the cap leaves no room."""
    cfg, labs, world = _setup()
    world.us_stock = 2 * sum(l.compute for l in labs)        # headroom 0
    out = execute_purchases(labs, world, {"xai": 30.0}, cfg)
    assert out["cost"].get("xai", 0.0) >= 1.0


@pytest.mark.xfail(strict=True, reason='refuted on verification (see audit/AUDIT_2026-10-13.md): A1-2 the reply schema asks for an integer; the played integer is rounded to 10 once')
def test_A1_2_chosen_disposition_double_rounding():  # probe
    """§2: 'Otherwise it plays at its median chosen value, rounded to 10.' The pilot
    stores the chosen value int-rounded (as_disposition), and tools/disposition.py
    rounds that to 10 again: a chosen 44.6 becomes 45 -> 50, not 40."""
    from prompts.turn import as_disposition
    from tools.disposition import _median_chosen, round_to_10
    raw = 44.6
    stored = as_disposition(raw)                      # what pilot.py writes as "chosen"
    assert round_to_10(_median_chosen([{"value": stored}])) == round_to_10(raw)


@pytest.mark.xfail(strict=True, reason="spec question for David (code kept; see audit/AUDIT_2026-10-13.md): A1-3 the cap can bind on turn 1 if every lab buys its maximum; §3.3 says 'by turns 2-3'")
def test_A1_3_cap_not_binding_on_turn1():  # probe
    """§3.3: national cap 'Not binding at t=0 ... binds by turns 2–3'. Headroom at
    turn 1 is 1225 − 1076 = 149 < 5 × 30 = 150, so if every lab buys its max the cap
    binds already on turn 1."""
    cfg, labs, world = _setup()
    headroom = world.holdings_limit() - sum(l.compute for l in labs)
    assert headroom >= len(labs) * cfg["compute"]["max_purchase_per_turn"]


@pytest.mark.xfail(strict=True, reason='spec question for David (code kept; see audit/AUDIT_2026-10-13.md): A1-4 the value is set at the first decision prompt (pinned by test_engine_flow)')
def test_A1_4_first_prompt_collects_disposition_in_separate_mode():  # probe
    """§2 step 1: 'each seat picks its value at its first prompt'. In the pilot's S2
    separate-message run (T4) the first prompt is the offers round, whose reply
    schema has no directive_precedence field; the value is only taken at the
    third prompt (decisions)."""
    from prompts.turn import build_message_prompt
    p = build_message_prompt(stage="offer", turn_date="2026-08-01", new_items=[], ledger={},
                             own={}, observed={}, notices=[], inbox=[], statements={})
    assert "directive_precedence" in p


@pytest.mark.xfail(strict=True, reason='refuted on verification (see audit/AUDIT_2026-10-13.md): A1-5 dispositions.json is written rounded by tools/disposition.py; a hand edit is operator error')
def test_A1_5_main_run_disposition_must_be_rounded_to_10():  # probe
    """§2 step 2: main runs play a value 'rounded to 10'. main.bad_dispositions only
    range-checks config/dispositions.json, so 47 or 55.5 run unchanged."""
    from main import bad_dispositions
    assert bad_dispositions({"anthropic": 47, "openai": 55.5}) != []
