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
