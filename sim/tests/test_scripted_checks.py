"""Stage 1 scripted checks (spec §9.2) all pass, with targeted probes on individual checks."""

from checks.scripted_checks import (
    run_all, sc2_intrusion_payoff, sc4_odds_match, sc6_no_prompt_leaks, sc10_all_policies_complete,
)


def test_run_all_passes():
    assert run_all(verbose=False) is True


def test_sc4_odds_match_closed_form():
    name, passed, detail = sc4_odds_match()
    assert passed, detail
    assert "mc=" in detail  # the Monte Carlo rate is reported


def test_sc2_pair_ev_at_least_lone():
    name, passed, detail = sc2_intrusion_payoff()
    assert passed, detail
    assert "EV_pair" in detail


def test_sc6_no_prompt_leaks():
    name, passed, detail = sc6_no_prompt_leaks()
    assert passed, detail


def test_sc10_every_policy_scenario_completes():
    name, passed, detail = sc10_all_policies_complete()
    assert passed, detail


# ---------------------------------------------------------------------------
# Round-1 fixes: frontier pace (SC1), score-EV payoff (SC2), pro rata and unit cap (SC3)
# ---------------------------------------------------------------------------

def test_sc1_reports_frontier_pace():
    from checks.scripted_checks import sc1_economy_sane
    name, passed, detail = sc1_economy_sane()
    assert passed, detail
    assert "frontier pace" in detail and "S1=" in detail and "S2=" in detail


def test_sc2_solo_pair_and_matched_joint_entry():
    name, passed, detail = sc2_intrusion_payoff()
    assert passed, detail
    assert "(a)" in detail and "(b)" in detail and "joint entries=['L01-1'" in detail


def test_sc3_prorata_and_unit_cap():
    from checks.scripted_checks import sc3_ceiling_cap_bind
    name, passed, detail = sc3_ceiling_cap_bind()
    assert passed, detail
    assert "pro rata" in detail and "max granted=20" in detail


# ---------------------------------------------------------------------------
# Policies (M8)
# ---------------------------------------------------------------------------

def _seed_state():
    from core.config import build_labs_and_world, load_world
    cfg = load_world()
    labs, world = build_labs_and_world(cfg, dispositions={})
    world.intrusion_open = True
    return cfg, labs, world, {lab.key: lab for lab in labs}


def test_intrude_ev_formula():
    import pytest
    from core.policies import intrude_ev
    cfg, _, _, _ = _seed_state()
    icfg, w = cfg["intrusion"], cfg["scoring"]
    g = 0.2 * (78.6 - 44.8)
    q = min(icfg["exposure_cap"], icfg["exposure_base"] + icfg["exposure_per_gain"] * g)
    p = icfg["success_base"]
    want = (p * (w["w_capability"] * g * (1 - q) - w["w_influence"] * icfg["caught_influence_penalty"] * q)
            - (1 - p) * w["w_influence"] * icfg["failure_influence_penalty"]
            - w["w_influence"] * icfg["fee_influence_per_target"])
    assert intrude_ev(44.8, [78.6], cfg) == pytest.approx(want)
    assert intrude_ev(44.8, [78.6], cfg, n_committers=2) > intrude_ev(44.8, [78.6], cfg)
    assert intrude_ev(76.8, [78.6], cfg) < 0          # near the top only min gain: not worth it


def test_intrude_profitable_only_on_positive_ev():
    from core.policies import POLICIES
    cfg, labs, world, k = _seed_state()
    meta = POLICIES["intrude_profitable"](k["meta"], labs, world, cfg, "S1", None)
    assert meta["actions"] == [{"type": "intrude", "targets": ["openai"]}]
    anth = POLICIES["intrude_profitable"](k["anthropic"], labs, world, cfg, "S1", None)
    assert all(a["type"] != "intrude" for a in anth["actions"])
    assert "rationale" in meta and "reasoning" not in meta


def test_pair_intrude_matched_actions():
    from core.actions import precheck
    from core.intrusion import group_commitments
    from core.policies import POLICIES
    cfg, labs, world, k = _seed_state()
    replies = {key: POLICIES["pair_intrude"](lab, labs, world, cfg, "S2", None) for key, lab in k.items()}
    intrudes = {key: r["actions"][0] for key, r in replies.items()
                if r["actions"] and r["actions"][0]["type"] == "intrude"}
    assert sorted(intrudes) == ["meta", "xai"]
    accepted = {key: precheck(k[key], [a], labs, world, cfg, "S2")[0][0] for key, a in intrudes.items()}
    (group,) = group_commitments(accepted, list(k))
    assert group[0] == ["meta", "xai"] and group[2] == ["openai"]
    # Not in S1
    assert not any(a["type"] == "intrude" for a in
                   POLICIES["pair_intrude"](k["meta"], labs, world, cfg, "S1", None)["actions"])


def test_t5_scripted_invests_and_reports_truthfully():
    from core.actions import precheck
    from core.policies import POLICIES
    cfg, labs, world, k = _seed_state()
    reply = POLICIES["t5_scripted"](k["xai"], labs, world, cfg, "S1", None)
    assert reply["actions"] == [{"type": "invest_capital", "amount": 2.0}]
    assert reply["reported_actions"] == ["invest_capital 2"]
    accepted, rejected = precheck(k["xai"], reply["actions"], labs, world, cfg, "S1")
    assert accepted and not rejected
    # M23: a bland public statement every month, also when it cannot invest
    assert reply["public_statement"] == "Operations as planned this month."
    k["xai"].capital = 1.0
    idle = POLICIES["t5_scripted"](k["xai"], labs, world, cfg, "S1", None)
    assert idle["actions"] == [] and idle["public_statement"] == "Operations as planned this month."
    assert {"pair_intrude", "t5_scripted", "intrude_profitable"} <= set(POLICIES)
