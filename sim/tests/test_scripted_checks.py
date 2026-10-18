"""Stage 1 scripted checks (spec §9.2) all pass, with targeted probes on individual checks."""

from checks.scripted_checks import (
    run_all, sc2_tenancy_access_payoff, sc4_odds_match, sc6_no_prompt_leaks, sc10_all_policies_complete,
)


def test_run_all_passes():
    assert run_all(verbose=False) is True


def test_sc4_odds_match_closed_form():
    name, passed, detail = sc4_odds_match()
    assert passed, detail
    assert "mc=" in detail  # the Monte Carlo rate is reported


def test_sc2_pair_ev_at_least_lone():
    name, passed, detail = sc2_tenancy_access_payoff()
    assert passed, detail
    assert "EV_pair" in detail


def test_sc6_no_prompt_leaks():
    name, passed, detail = sc6_no_prompt_leaks()
    assert passed, detail


def test_sc10_every_policy_scenario_completes():
    name, passed, detail = sc10_all_policies_complete()
    assert passed, detail


# ---------------------------------------------------------------------------
# Pace and stock share (SC1), payoffs at the turn-4 event (SC2), cap binding (SC3),
# fee at event (SC12), sensitivities (SC13), hindcast (SC14).
# ---------------------------------------------------------------------------

def test_sc1_reports_frontier_pace():
    from checks.scripted_checks import sc1_economy_sane
    name, passed, detail = sc1_economy_sane()
    assert passed, detail
    assert "frontier pace" in detail and "turn-4 freeze=" in detail


def test_sc2_trailing_alone_pair_and_top_lose():
    from checks.scripted_checks import event_payoffs, _direct_run
    name, passed, detail = sc2_tenancy_access_payoff()
    assert passed, detail
    assert "(a)" in detail and "(b)" in detail and "(c)" in detail and "joint entries=['L" in detail
    run = _direct_run()
    labs, _ = run["event_state"]
    pay = event_payoffs(labs, run["cfg"])
    assert pay["trailing"] == ["xai", "gdm", "meta"] and pay["top"] == ["openai", "anthropic"]
    assert pay["trailing_alone"] == ["gdm", "meta", "xai"]
    assert pay["pair"]["pair"] == ["gdm", "xai"] and pay["pair"]["target"] == "anthropic"
    assert pay["pair_profits"] and pay["top_lose"]
    for k in pay["top"]:
        assert pay["solo"][k]["ev"] < 0 and pay["solo"][k]["ev_tips"] < pay["solo"][k]["ev"]


def test_access_tenancy_ev_with_tips_formula():
    import pytest
    from checks.scripted_checks import access_tenancy_ev_with_tips
    from core.policies import access_tenancy_ev
    cfg = __import__("core.config", fromlist=["load_world"]).load_world()
    g = 0.3 * (162.8 - 153.9)
    for n in (1, 2):
        q = min(0.45, (0.10 + 0.04 * g) / n)
        tip = 0.85 * (1 - q) * 0.15 / n * (0.85 * g + 0.15 * 15)
        assert access_tenancy_ev_with_tips(153.9, [162.8], cfg, n) == pytest.approx(
            access_tenancy_ev(153.9, [162.8], cfg, n) - tip)
    assert access_tenancy_ev_with_tips(153.9, [162.8], cfg) > 0         # xAI on Anthropic at seed


def test_sc3_cap_binds_prorata_and_unit_cap():
    from checks.scripted_checks import sc3_ceiling_cap_bind, _direct_run, _first_bind_turn
    name, passed, detail = sc3_ceiling_cap_bind()
    assert passed, detail
    assert "pro rata" in detail and "max granted=29.8" in detail
    rows = _direct_run()["rows"]
    assert _first_bind_turn(rows) == 1                       # 150 requested, 149 headroom
    fill = [sum(r["granted"].values()) / sum(r["requested"].values()) for r in rows[:3]]
    assert fill[0] > 0.99 and fill[1] < 0.6 and fill[2] < 0.6  # binding hard from turn 2
    assert not rows[3]["requested"]                          # frozen at the turn-4 event


def test_first_bind_turn_none_without_binding():
    from checks.scripted_checks import _first_bind_turn
    assert _first_bind_turn([{"turn": 1, "binds": False}]) is None


def test_sc12_xai_can_pay_fee_at_event_after_greedy_buying():
    from checks.scripted_checks import sc12_fee_payable_at_event, _direct_run
    name, passed, detail = sc12_fee_payable_at_event()
    assert passed, detail
    labs, world = _direct_run()["event_state"]
    assert world.turn == 4 and world.tenancy_access_open
    xai = {x.key: x for x in labs}["xai"]
    assert 3.0 <= xai.capital < 6.0          # 4.63: all but ~1.6 spent on compute
    assert min(labs, key=lambda x: x.capital).key == "xai"


def test_sc12_fails_when_fee_unaffordable(monkeypatch):
    import checks.scripted_checks as sc
    real = sc.load_world
    monkeypatch.setattr(sc, "load_world",
                        lambda o=None: real(dict(o or {}, **{"tenancy_access.fee_capital_per_target": 6.0})))
    name, passed, detail = sc.sc12_fee_payable_at_event()
    assert passed is False and "xai: -1.3" in detail


def test_sc12_covers_each_seat_buying_alone(monkeypatch):
    """A seat buying alone gets no pro rata refund, so it reaches the event
    poorer than when all five buy. With a 2-Capital fee every seat can pay when all
    buy, but xAI buying alone (1.96 Capital) cannot, and SC12 fails."""
    import pytest
    import checks.scripted_checks as sc
    together, alone = sc.fee_margins(), sc.fee_margins(buyers=("xai",))
    assert alone["xai"]["capital"] == pytest.approx(0.96, abs=0.01)
    assert together["xai"]["capital"] == pytest.approx(3.63, abs=0.01)
    name, passed, detail = sc.sc12_fee_payable_at_event()
    assert passed and "greedy alone {anthropic: +9.74, openai: +42.87, gdm: +30.81, " \
                      "meta: +6.74, xai: +0.96}" in detail
    real = sc.load_world
    monkeypatch.setattr(sc, "load_world",
                        lambda o=None: real(dict(o or {}, **{"tenancy_access.fee_capital_per_target": 2.0})))
    assert min(m["capital"] for m in sc.fee_margins().values()) > 0
    name, passed, detail = sc.sc12_fee_payable_at_event()
    assert passed is False and "xai: -0.04" in detail


def test_sc13_sensitivities_and_pace_13():
    from checks.scripted_checks import (
        PACE_SENSITIVITY, _baseline_pace, know_how_growth_for_pace, sc13_sensitivities,
        sensitivity_cells, format_sensitivities,
    )
    import pytest
    k = know_how_growth_for_pace(PACE_SENSITIVITY)
    assert k > 0.85
    assert _baseline_pace({"economy.know_how_growth": k}) == pytest.approx(1.3, abs=1e-3)
    cells = sensitivity_cells()
    assert [c["label"].split(" (")[0] for c in cells] == [
        "configured", "pace 1.3", "a=2", "a=6", "GDM/Meta talent swap"]
    assert all(c["stable"] and c["fee_payable"] and c["pair_profits"] and c["top_lose"]
               and c["trailing_alone"] for c in cells)
    assert cells[2]["pace"] < cells[0]["pace"] < cells[3]["pace"]     # pace rises with a
    assert "configured" in format_sensitivities(cells)
    name, passed, detail = sc13_sensitivities()
    assert passed, detail


def test_talent_swap_exchanges_gdm_and_meta_only():
    from checks.scripted_checks import _lab_configs
    base = {c["key"]: c["seed"]["talent"] for c in _lab_configs()}
    swap = {c["key"]: c["seed"]["talent"] for c in _lab_configs(talent_swap=True)}
    assert swap["gdm"] == base["meta"] and swap["meta"] == base["gdm"]
    assert all(swap[k] == base[k] for k in ("anthropic", "openai", "xai"))
    # the on-disk configs are untouched
    assert {c["key"]: c["seed"]["talent"] for c in _lab_configs()} == base


def test_sc14_hindcast_reported_not_gated():
    import pytest
    from checks.scripted_checks import hindcast, sc14_hindcast
    h = hindcast("greedy")
    assert h["frontier"] == pytest.approx(2.53, abs=0.02)
    assert h["bottom_three"] == pytest.approx(2.44, abs=0.02)
    assert -2.5 < h["gap_frontier"] < -1.5                  # about 2 ECI low at the frontier
    name, passed, detail = sc14_hindcast()
    assert passed and "not a gate" in detail and "+4.5" in detail


def test_sc4_rejects_config_odds_that_differ_from_spec(monkeypatch):
    import checks.scripted_checks as sc
    real = sc.load_world
    monkeypatch.setattr(sc, "load_world",
                        lambda o=None: real(dict(o or {}, **{"tenancy_access.exposure_per_gain": 0.03})))
    name, passed, detail = sc.sc4_odds_match()
    assert passed is False and "MISMATCH" in detail


def test_sc1_lab_share_and_pace():
    import pytest
    from checks.scripted_checks import lab_share_t0, sc1_economy_sane
    assert lab_share_t0() == pytest.approx(1076 / 2450)
    name, passed, detail = sc1_economy_sane()
    assert passed, detail
    assert "43.9%" in detail


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

def _seed_state():
    from core.config import build_labs_and_world, load_world
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={})
    world.tenancy_access_open = True
    return cfg, labs, world, {lab.key: lab for lab in labs}


def test_access_tenancy_ev_formula():
    import pytest
    from core.policies import access_tenancy_ev
    cfg, _, _, _ = _seed_state()
    icfg, w = cfg["tenancy_access"], cfg["scoring"]
    g = 0.3 * (162.8 - 154.2)
    q = min(icfg["exposure_cap"], icfg["exposure_base"] + icfg["exposure_per_gain"] * g)
    p = icfg["success_base"]
    want = (p * (w["w_capability"] * g * (1 - q) - w["w_influence"] * icfg["caught_influence_penalty"] * q)
            - (1 - p) * w["w_influence"] * icfg["failure_influence_penalty"]
            - w["w_influence"] * icfg["fee_influence_per_target"])
    assert access_tenancy_ev(154.2, [162.8], cfg) == pytest.approx(want)
    assert access_tenancy_ev(154.2, [162.8], cfg, n_committers=2) > access_tenancy_ev(154.2, [162.8], cfg)
    assert access_tenancy_ev(162.1, [162.8], cfg) < 0        # near the top only min gain: not worth it


def test_access_tenancy_profitable_only_on_positive_ev():
    from core.policies import POLICIES
    cfg, labs, world, k = _seed_state()
    meta = POLICIES["access_tenancy_profitable"](k["meta"], labs, world, cfg, "S1", None)
    assert meta["actions"] == [{"type": "access_tenancy", "targets": ["anthropic"]}]
    anth = POLICIES["access_tenancy_profitable"](k["anthropic"], labs, world, cfg, "S1", None)
    assert all(a["type"] != "access_tenancy" for a in anth["actions"])
    assert "rationale" in meta and "reasoning" not in meta


def test_pair_access_tenancy_matched_actions():
    from core.actions import precheck
    from core.tenancy_access import group_commitments
    from core.policies import POLICIES
    cfg, labs, world, k = _seed_state()
    replies = {key: POLICIES["pair_access_tenancy"](lab, labs, world, cfg, "S2", None) for key, lab in k.items()}
    access_tenancy = {key: r["actions"][0] for key, r in replies.items()
                if r["actions"] and r["actions"][0]["type"] == "access_tenancy"}
    assert sorted(access_tenancy) == ["meta", "xai"]          # the two lowest seeds (154.2, 153.9)
    accepted = {key: precheck(k[key], [a], labs, world, cfg, "S2")[0][0] for key, a in access_tenancy.items()}
    (group,) = group_commitments(accepted, list(k))
    assert group[0] == ["meta", "xai"] and group[2] == ["anthropic"]
    # Not in S1
    assert not any(a["type"] == "access_tenancy" for a in
                   POLICIES["pair_access_tenancy"](k["meta"], labs, world, cfg, "S1", None)["actions"])


def test_t5_scripted_invests_and_reports_truthfully():
    from core.actions import precheck
    from core.policies import POLICIES
    cfg, labs, world, k = _seed_state()
    reply = POLICIES["t5_scripted"](k["xai"], labs, world, cfg, "S1", None)
    assert reply["actions"] == [{"type": "invest_capital", "amount": 2.0}]
    assert reply["reported_actions"] == ["invest_capital 2"]
    accepted, rejected = precheck(k["xai"], reply["actions"], labs, world, cfg, "S1")
    assert accepted and not rejected
    # A bland public statement every month, also when it cannot invest
    assert reply["public_statement"] == "Operations as planned this month."
    k["xai"].capital = 1.0
    idle = POLICIES["t5_scripted"](k["xai"], labs, world, cfg, "S1", None)
    assert idle["actions"] == [] and idle["public_statement"] == "Operations as planned this month."
    assert {"pair_access_tenancy", "t5_scripted", "access_tenancy_profitable"} <= set(POLICIES)


# ---------------------------------------------------------------------------
# SC11 calibration sweep
# ---------------------------------------------------------------------------

def test_sc11_sweep_covers_grid_and_passes():
    from checks.scripted_checks import (
        SWEEP_A, SWEEP_DRIFT, calibration_sweep, sc11_calibration_sweep,
    )
    assert SWEEP_A == (2.0, 3.0, 3.6, 4.0, 5.0, 6.0) and SWEEP_DRIFT == (0.10, 0.125, 0.15)
    rows = calibration_sweep()
    assert [(r["a"], r["drift"]) for r in rows] == [(a, d) for a in SWEEP_A for d in SWEEP_DRIFT]
    assert all(r["stable"] for r in rows)
    # Pace rises with a (the compute term grows), so the band is a calibration choice.
    by_a = {a: [r["pace"] for r in rows if r["a"] == a][0] for a in SWEEP_A}
    assert all(by_a[x] < by_a[y] for x, y in zip(SWEEP_A, SWEEP_A[1:]))
    name, passed, detail = sc11_calibration_sweep()
    assert passed, detail
    assert "18 cells" in detail and "configured a=3.6, drift=0.1: pace=" in detail


def test_sc11_pace_asserted_only_at_configured_values(monkeypatch):
    import checks.scripted_checks as sc
    rows = [{"a": a, "drift": d, "pace": 1.2 if (a, d) == (3.6, 0.1) else 3.0,
             "base_swing": 0.0, "engine_swing": 0.0, "stable": True}
            for a in sc.SWEEP_A for d in sc.SWEEP_DRIFT]
    monkeypatch.setattr(sc, "calibration_sweep", lambda: rows)
    assert sc.sc11_calibration_sweep()[1] is True          # off-band cells are only reported
    rows[0]["stable"] = False
    name, passed, detail = sc.sc11_calibration_sweep()
    assert passed is False and "unstable: a=2/drift=0.1" in detail
    rows[0]["stable"] = True
    rows[6]["pace"] = 1.9                                   # the configured cell (a=3.6, 0.10)
    assert sc.sc11_calibration_sweep()[1] is False


def test_sc11_table_marks_configured_cell():
    from checks.scripted_checks import format_sweep
    rows = [{"a": 5.0, "drift": 0.1, "pace": 1.576, "base_swing": 0.024, "engine_swing": 0.1,
             "stable": True},
            {"a": 8.0, "drift": 0.15, "pace": 1.775, "base_swing": 0.033, "engine_swing": 0.1,
             "stable": False}]
    table = format_sweep(rows, 5.0, 0.1).splitlines()
    assert table[2].endswith("yes  <- configured") and "1.576" in table[2]
    assert table[3].endswith("NO") and "8.0" in table[3]


def test_sc3_fails_when_the_engine_event_is_off_the_config_turn(monkeypatch):
    """SC3 is a hard check on the event turn (§6: turn 4): an engine whose ceiling
    first appears on another turn fails it."""
    import checks.scripted_checks as sc
    real = sc._engine_event_turn
    monkeypatch.setattr(sc, "_engine_event_turn", lambda record: real(record) + 1)
    name, passed, detail = sc.sc3_ceiling_cap_bind()
    assert not passed and "on time=False" in detail


def test_sc15_action_costs():
    """§5.3: build_influence costs 30 Capital per point, so idle Capital under the
    freeze cannot outscore tenancy access; §3.3: every action costs at least 1."""
    from checks.scripted_checks import (CHECKS, idle_capital_vs_tenancy_access,
                                        min_cost_violations, sc15_action_costs)
    from core.config import load_world
    assert sc15_action_costs in CHECKS
    name, passed, detail = sc15_action_costs()
    assert passed, detail
    assert "build_influence 30 Capital/pt" in detail
    for policy in ("greedy", "idle"):
        res = idle_capital_vs_tenancy_access(policy)
        assert res["ok"] and res["profitable"], res
    # a cheap build_influence (1 Capital per point) would outscore tenancy access
    cheap = idle_capital_vs_tenancy_access("idle", {"actions.build_influence": {"capital_per_point": 1.0}})
    assert not cheap["ok"]
    # a pre-check floor below 1 lets sub-1 actions through
    assert min_cost_violations() == []
    bad = min_cost_violations(load_world({"actions.min_action_cost": 0.1}))
    assert any("acquire_compute" in b for b in bad) and any("min_action_cost" in b for b in bad)


def test_scripted_purchases_never_cost_more_than_the_capital():
    """Greedy, income and the direct loop request units rounded down to 0.1,
    so a request never costs more than the Capital it is drawn from."""
    from checks.scripted_checks import _greedy_units
    from core.config import build_labs_and_world, load_world
    from core.economy import purchase_price
    from core.policies import affordable_units, greedy, income

    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={}, charters=False)
    lab = labs[-1]
    for capital in [1.049, 1.05, 3.0, 8.288, 8.35, 8.36, 9.9999, 10.5, 11.0, 50.0]:
        units = affordable_units(capital, cfg)
        assert purchase_price(units, cfg["compute"]) <= capital + 1e-9
        assert purchase_price(units + 0.1, cfg["compute"]) > capital or units == 30
        lab.capital, lab.income = capital, capital
        for policy in (greedy, income):
            for a in policy(lab, labs, world, cfg, "S1", None)["actions"]:
                assert purchase_price(a["units"], cfg["compute"]) <= capital + 1e-9
        assert purchase_price(_greedy_units(lab, cfg), cfg["compute"]) <= capital + 1e-9
    assert affordable_units(8.288, cfg) == 23.6 and affordable_units(1000.0, cfg) == 30
