"""
tools/ups_replay.py: the §8 UPS-contribution replay. All offline: logs come from
scripted policies (and stub models / a stub MacroJury), no paid calls.
"""

import copy
import json
import math
from pathlib import Path

import pytest

from core import policies
from core.config import build_labs_and_world, load_world
from core.engine import SimulationEngine
from core.intrusion import exposure_probability
from core.llm import register_stub
from pilot import swap_capability
from tools import ups_replay as U

KEYS = ("anthropic", "openai", "gdm", "meta", "xai")
MJ_VALUES = {"time_horizon": 30, "transparency_threshold": 80, "risk_tolerance": 20,
             "democratic_tendency": 70}
register_stub("ups_replay_mj", lambda s, u: json.dumps(
    {"values": MJ_VALUES, "mandate": "Prioritise domestic compute resilience.", "rationale": "x"}))


def run_log(seat_policies, *, scenario="S1", seed=0, turns=8, macro=True, overrides=None,
            rotation=None, choose=False, interventions=None, a2a="merged", labs_hook=None):
    cfg = load_world(overrides)
    labs, world = build_labs_and_world(
        cfg, dispositions={} if choose else {k: 50 for k in KEYS}, charters=False)
    if rotation:
        src, _, dst = rotation.partition(":")
        swap_capability(labs, src, dst, cfg["economy"]["capability_compute_elasticity"])
    if labs_hook:
        labs_hook(labs)
    eng = SimulationEngine(labs, world, cfg, scenario=scenario, turns=turns, seed=seed,
                           seat_policies=seat_policies, a2a_mode=a2a,
                           macro_jurors=["stub:ups_replay_mj"] * 3 if macro else [],
                           run_grand_jury=False, parallel=False, overrides=overrides,
                           rotation=rotation, choose_disposition=choose,
                           interventions=interventions)
    return json.loads(json.dumps(eng.run(), default=str))


def with_report(base, accused, on_turns, month=None):
    """base policy plus a report against `accused` in the given months."""
    def policy(lab, labs, world, cfg, scenario, rng):
        reply = dict(base(lab, labs, world, cfg, scenario, rng))
        if world.turn in on_turns:
            reply["report"] = {"accused": accused, "month": month, "evidence": "tip"}
        return reply
    return policy


def joint(intruders, target, on_turns=None):
    """S2: submit intrude(intruders, [target]) while intrusion is open; else buy."""
    def policy(lab, labs, world, cfg, scenario, rng):
        if world.intrusion_open and (on_turns is None or world.turn in on_turns):
            return {"actions": [{"type": "intrude", "intruders": list(intruders),
                                 "targets": [target]}], "reported_actions": [], "report": None}
        return policies.greedy(lab, labs, world, cfg, scenario, rng)
    return policy


def find_seed(make, pred, tries=200):
    for seed in range(tries):
        rec = make(seed)
        if pred(rec):
            return seed, rec
    pytest.fail("no seed gives the wanted outcome")


def entries(rec, lab):
    return [e for e in rec["ledger"] if lab in e["committers"]]


def outcomes(rec):
    return [dict(o, turn=t["turn"]) for t in rec["turns"]
            for o in t["whistleblow"]["outcomes"]]


# ---------------------------------------------------------------------------
# Identity replay: nothing removed reproduces the run exactly
# ---------------------------------------------------------------------------

def _s1_policies():
    sp = {k: policies.greedy for k in KEYS}
    sp["meta"] = policies.always_intrude
    sp["xai"] = with_report(policies.intrude_profitable, "Meta", {6, 8})
    sp["gdm"] = with_report(policies.income, "OpenAI", {5})          # a misreport
    sp["anthropic"] = with_report(policies.greedy, "xAI", {7}, month="May 2026")
    return sp


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_identity_replay_reproduces_s1_run(seed):
    rec = run_log(_s1_policies(), seed=seed)
    base = U.replay(rec)
    assert U.compare_records(rec, base["record"]) == []
    # per turn capabilities / ledger / UPS, spelled out
    for a, b in zip(rec["turns"], base["record"]["turns"]):
        assert a["public_sheets"] == b["public_sheets"]
        assert a["ups_index"] == b["ups_index"]
        assert a["integrity_inputs"] == b["integrity_inputs"]
        assert [d for d in a["draws"] if d["key"][1] != "policy"] == b["draws"]
    assert rec["ledger"] == base["record"]["ledger"]
    assert rec["final"]["ups_index"] == base["record"]["final"]["ups_index"]
    assert round(base["ups"], 4) == rec["final"]["ups_index"]["ups"]
    # the MacroJury was applied from the log, not called
    assert [m["turn"] for m in base["record"]["macro_jury"]] == [m["turn"] for m in rec["macro_jury"]]


def test_identity_replay_s2_joint_rotation_overrides_planted():
    sp = {k: policies.greedy for k in KEYS}
    sp["meta"] = joint(["meta", "xai"], "openai")
    sp["xai"] = joint(["meta", "xai"], "openai")
    sp["gdm"] = with_report(policies.greedy, "Meta", {7})
    plant = [{"turn": 5, "kind": "plant_intrusion", "intruder": "anthropic", "target": "openai",
              "lead_to": ["gdm"]}, {"turn": 6, "kind": "windfall", "lab": "gdm", "capability": 1.0}]
    rec = run_log(sp, scenario="S2", seed=4, rotation="meta:gdm", a2a="separate",
                  overrides={"intrusion.gain_share": 0.35}, interventions=plant)
    assert U.compare_records(rec, U.replay(rec)["record"]) == []


def test_identity_replay_choose_mode():
    def choosing(lab, labs, world, cfg, scenario, rng):
        reply = dict(policies.greedy(lab, labs, world, cfg, scenario, rng))
        if lab.disposition is None and world.turn >= (2 if lab.key == "gdm" else 1):
            reply["directive_precedence"] = 20 + 10 * KEYS.index(lab.key)
        return reply
    rec = run_log({k: choosing for k in KEYS}, choose=True, turns=5)
    assert rec["final"]["dispositions"]["gdm"] == 40
    assert U.compare_records(rec, U.replay(rec)["record"]) == []


def test_identity_replay_of_a_stub_model_run():
    """Model seats (stub models, A2A pre-step): the log-driven policy replays them."""
    def reply_fn(key):
        def fn(system, user):
            if "message round" in user[:60]:
                return json.dumps({"messages": [{"to": "all", "text": "hello"}]})
            acts = [{"type": "acquire_compute", "units": "40 units"}]      # trimmed to 30
            if key == "meta" and user.startswith(("== November 2026", "== December 2026")):
                acts = [{"type": "intrude", "targets": "the leader, OpenAI"}, acts[0]]
            report = {"accused": "Meta"} if key == "gdm" and user.startswith("== January 2027") else None
            return json.dumps({"rationale": "x", "actions": acts, "reported_actions": ["bought"],
                               "report": report, "public_statement": "fine"})
        return fn

    def hook(labs):
        for lab in labs:
            register_stub(f"upsr_{lab.key}", reply_fn(lab.key))
            lab.model = f"stub:upsr_{lab.key}"
    rec = run_log({}, scenario="S2", seed=2, turns=6, a2a="separate", labs_hook=hook)
    assert any(t["actors"]["meta"]["accepted"][0]["type"] == "intrude" for t in rec["turns"][3:5])
    assert any(a.get("trimmed_from") for t in rec["turns"] for a in t["actors"]["gdm"]["accepted"])
    assert U.compare_records(rec, U.replay(rec)["record"]) == []


def test_identity_check_detects_a_different_draw():
    """The replay uses the logged draws: altering one logged success draw changes the
    replayed outcome, so the identity check flags the run."""
    rec = run_log(_s1_policies(), seed=0)
    bad = copy.deepcopy(rec)
    for t in bad["turns"]:
        for d in t["draws"]:
            if d["key"][1] == "success" and d["key"][2].startswith("meta"):
                d["u"] = 0.999 if d["u"] < 0.5 else 0.001       # flip the outcome
                break
    rep = U.replay(bad)["record"]
    assert U.compare_records(bad, rep)
    flipped = [d for t in rep["turns"] for d in t["draws"] if d["u"] in (0.999, 0.001)]
    assert flipped


# ---------------------------------------------------------------------------
# Removal replays
# ---------------------------------------------------------------------------

def _forfeit_and_report(rec):
    meta = entries(rec, "meta")
    return (any("meta" in e["exposed"] for e in meta)
            and any(o["accused"] == "meta" and o["result"] == "upheld" for o in outcomes(rec))
            and any(e["gains"].get("meta", 0) > 0 for e in meta))


def test_removing_an_intruder_removes_gains_forfeits_reports_and_keeps_other_draws():
    seed, rec = find_seed(lambda s: run_log(_s1_policies(), seed=s), _forfeit_and_report)
    replies, removed = U.remove_actor(U.logged_replies(rec), rec, "meta")
    assert removed["intrusion_turns"] == [4, 5, 6, 7, 8]
    assert removed["reports"] and all(r["reporter"] == "xai" for r in removed["reports"])
    cf = U.replay(rec, replies)["record"]

    # gains and forfeits gone: no meta entry, no exposure of meta, no meta notices
    assert entries(cf, "meta") == []
    assert not any(n.startswith("Allocator") and "traced to Meta" in n
                   for t in cf["turns"] for n in t["public_notices"])
    assert cf["final"]["integrity_inputs"]["copied_gain"]["meta"] == 0
    assert cf["final"]["integrity_inputs"]["unforfeited_gain"]["meta"] == 0
    # the reports of meta's intrusions are gone (xai's upheld reports)
    assert not any(o["accused"] == "meta" and o["result"] in ("upheld", "returned")
                   for o in outcomes(cf))
    # other actors' draws unchanged: every key both replays draw has the same value,
    # and every non-meta draw of the run is drawn again
    orig = {tuple(d["key"]): d["u"] for t in rec["turns"] for d in t["draws"]}
    new = {tuple(d["key"]): d["u"] for t in cf["turns"] for d in t["draws"]}
    assert all(orig[k] == u for k, u in new.items() if k in orig)
    kept = {k for k in orig if not (k[2] or "").startswith("meta>") and k[1] != "policy"}
    assert kept <= set(new)
    # xai's intrusions get the same draws (gains may move: talent shares and income
    # respond to meta's changed position, which is the counterfactual's point)
    def uniforms(e):
        d = e["draws"]
        return (d["draw_id"], d["success"]["draw"], d.get("exposure", {}).get("xai", {}).get("draw"),
                d.get("lead", {}).get("draw"))
    assert [uniforms(e) for e in entries(rec, "xai")] == [uniforms(e) for e in entries(cf, "xai")]

    # held actions only: meta's intrudes became no actions (nothing else added)
    assert all(not t["actors"]["meta"]["accepted"] for t in cf["turns"][3:])


def test_contribution_sign_for_a_kept_intrusion():
    """A successful, unexposed, unreported intrusion lowers integrity, so the
    integrity term's contribution is negative and (here) so is the actor's."""
    sp = {k: policies.greedy for k in KEYS}
    sp["meta"] = policies.always_intrude

    def kept(rec):
        return any(e["gains"].get("meta", 0) > 0 for e in entries(rec, "meta"))
    _, rec = find_seed(lambda s: run_log(sp, seed=s), kept)
    res = U.contributions(rec)
    assert res["identity"]["ok"]
    m = res["actors"]["meta"]
    assert m["terms"]["integrity"] < 0
    assert m["contribution"] < 0
    assert m["terms"]["norms"] == 0              # values do not depend on intrusions here
    assert math.isclose(m["contribution"], sum(m["weighted_terms"].values()), abs_tol=1e-12)
    assert math.isclose(m["contribution"], res["ups"] - m["ups_without"], abs_tol=1e-12)
    for k in ("anthropic", "openai", "gdm", "xai"):
        assert res["actors"][k]["contribution"] == 0


def test_joint_intrusion_remaining_committer_runs_with_recomputed_q():
    sp = {k: policies.greedy for k in KEYS}
    sp["meta"] = joint(["meta", "xai"], "openai", {4})
    sp["xai"] = joint(["meta", "xai"], "openai", {4})

    def ok(rec):
        e = entries(rec, "meta")
        return len(e) == 1 and e[0]["succeeded"] and e[0]["committers"] == ["meta", "xai"]
    _, rec = find_seed(lambda s: run_log(sp, scenario="S2", seed=s), ok)
    replies, removed = U.remove_actor(U.logged_replies(rec), rec, "meta")
    assert removed["intrusion_turns"] == [4]
    cf = U.replay(rec, replies)["record"]
    (orig,) = entries(rec, "xai")
    (new,) = entries(cf, "xai")
    assert new["committers"] == ["xai"]
    assert new["draws"]["named_not_committed"] == ["meta"]
    assert new["draws"]["draw_id"] == orig["draws"]["draw_id"]
    assert new["draws"]["success"] == orig["draws"]["success"]          # same logged draw
    ex_o, ex_n = orig["draws"]["exposure"]["xai"], new["draws"]["exposure"]["xai"]
    assert ex_n["draw"] == ex_o["draw"] and ex_n["gain"] == ex_o["gain"]
    cfg = load_world()
    assert ex_o["q"] == exposure_probability(ex_o["gain"], 2, cfg["intrusion"])
    assert ex_n["q"] == exposure_probability(ex_o["gain"], 1, cfg["intrusion"])
    assert ex_n["caught"] == (ex_n["draw"] < ex_n["q"])
    assert entries(cf, "meta") == []


def test_no_intrusions_gives_zero():
    sp = {k: policies.income for k in KEYS}
    sp["gdm"] = with_report(policies.greedy, "Meta", {5})          # a misreport is kept
    rec = run_log(sp, seed=3)
    res = U.contributions(rec)
    assert res["identity"]["ok"]
    for a in res["actors"].values():
        assert a["contribution"] == 0 and all(v == 0 for v in a["terms"].values())
        assert a["removed"]["reports"] == []


def test_refused_report_is_not_refiled_in_a_removal_replay():
    sp = _s1_policies()
    sp["openai"] = with_report(policies.greedy, "DeepSeek", {5})          # refused
    rec = run_log(sp, seed=0)
    assert rec["turns"][4]["actors"]["openai"]["report_rejected"]
    assert U.compare_records(rec, U.replay(rec)["record"]) == []       # identity re-refuses it
    replies, _ = U.remove_actor(U.logged_replies(rec), rec, "meta")
    assert replies[5]["openai"]["report"] is None


def test_planted_intrusion_is_not_the_actors():
    sp = {k: policies.greedy for k in KEYS}
    sp["anthropic"] = with_report(policies.greedy, "xAI", {6})
    plant = [{"turn": 5, "kind": "plant_intrusion", "intruder": "xai", "target": "openai",
              "lead_to": ["anthropic"]}]
    rec = run_log(sp, seed=1, interventions=plant)
    res = U.contributions(rec)
    assert res["identity"]["ok"]
    assert res["actors"]["xai"]["removed"]["reports"] == []
    assert res["actors"]["xai"]["contribution"] == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_file_and_directory(tmp_path, capsys):
    rec = run_log(_s1_policies(), seed=0)
    (tmp_path / "sub").mkdir()
    path = tmp_path / "sub" / "run.json"
    path.write_text(json.dumps(rec))
    (tmp_path / "sub" / "run.partial.json").write_text(json.dumps(rec))
    (tmp_path / "other.json").write_text(json.dumps({"not": "a run"}))

    assert U.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "reproduces the run exactly" in out and "meta" in out and "integrity" in out

    assert U.main([str(tmp_path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1 and data[0]["path"] == str(path)
    assert set(data[0]["actors"]) == set(KEYS)
    assert set(data[0]["actors"]["meta"]["terms"]) == set(U.UPS_TERMS)


def test_replay_starts_from_the_logged_state_after_config_edits(monkeypatch):
    """The replay uses config.effective_cfg / initial_state, so an edit of
    world.json or the lab seeds after the run never breaks the identity replay; an old
    log (without them) falls back to the current files, says so, and then mismatches."""
    rec = run_log({k: policies.always_intrude for k in KEYS}, seed=3, rotation="anthropic:meta")
    assert rec["config"].get("effective_cfg") and rec["config"].get("initial_state")
    real_load = U.load_world

    def edited(overrides=None):
        cfg = real_load(overrides)
        cfg["economy"]["capability_compute_elasticity"] += 0.4
        return cfg
    monkeypatch.setattr(U, "load_world", edited)
    res = U.contributions(rec)
    assert res["start_state"] == "logged" and res["identity"]["ok"], res["identity"]["mismatches"][:3]
    old = copy.deepcopy(rec)
    old["config"].pop("effective_cfg")
    old["config"].pop("initial_state")
    res = U.contributions(old)
    assert res["start_state"] == "config files" and not res["identity"]["ok"]
    assert "rebuilt from the CURRENT config files" in U.format_result(Path("x.json"), res)


def test_a_run_shorter_than_its_event_turn_replays(tmp_path):
    import main as main_mod
    assert main_mod.main(["--policy", "idle", "--turns", "2", "--output", str(tmp_path), "--run-id", "short"]) == 0
    assert U.main([str(tmp_path / "short.json")]) == 0


def test_no_finished_log_exits_2_and_a_bad_log_never_stops_the_others(tmp_path, capsys, monkeypatch):
    assert U.main([str(tmp_path)]) == 2
    import main as main_mod
    for rid in ("a", "b"):
        assert main_mod.main(["--policy", "idle", "--turns", "3", "--output", str(tmp_path / rid),
                              "--run-id", rid]) == 0
    real = U.contributions

    def flaky(record):
        if record.get("run_id") == "a" or (record.get("config", {}).get("run_meta") or {}).get("run_id") == "a":
            raise ValueError("cannot rebuild")
        return real(record)
    monkeypatch.setattr(U, "contributions", flaky)
    capsys.readouterr()
    assert U.main([str(tmp_path)]) == 2
    out = capsys.readouterr().out
    assert "cannot replay" in out and "reproduces the run exactly" in out
