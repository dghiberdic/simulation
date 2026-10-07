"""Pricing math, conservative unknown-model price, persisted ledger and budget guard."""

import pytest

from core import costs
from core.costs import BudgetExceeded, CostTracker, cost_of


def test_cost_of_plain():
    # claude-opus-5-5: $4 in / $20 out per 1M
    assert cost_of("claude-opus-5-5", 1_000_000, 0) == pytest.approx(4.0)
    assert cost_of("claude-opus-5-5", 1000, 500) == pytest.approx((1000 * 4 + 500 * 20) / 1e6)


def test_cost_of_cached_is_subset_of_input():
    # gpt-6-astra 10/50/1: 10k prompt of which 8k cached, 2k output
    expected = (2000 * 10 + 8000 * 1.0 + 2000 * 50) / 1e6
    assert cost_of("gpt-6-astra", 10_000, 2000, 8000) == pytest.approx(expected)
    # cached can never exceed input
    assert cost_of("gpt-6-astra", 100, 0, 500) == pytest.approx(100 * 1.0 / 1e6)


def test_unknown_model_priced_at_highest_rates(caplog):
    p = costs.price_for("mystery-model-9")
    assert p == {"input": 10.0, "output": 50.0, "cached": 1.0}
    assert cost_of("mystery-model-9", 1_000_000, 1_000_000) == pytest.approx(60.0)


def test_tracker_persists_across_instances(tmp_path):
    f = tmp_path / "spend.json"
    a = CostTracker(f)
    a.record("claude-opus-5-5", "actor", "r1", 1_000_000, 0)
    b = CostTracker(f)
    assert b.persisted_total() == pytest.approx(4.0)
    b.record("gemini-3.1-pro", "jury", "r2", 0, 1_000_000)
    assert CostTracker(f).persisted_total() == pytest.approx(16.0)
    s = b.summary()
    assert s["session_calls"] == 1 and s["by_purpose"]["jury"]["cost"] == pytest.approx(12.0)


def test_zero_cost_calls_do_not_touch_ledger(tmp_path):
    f = tmp_path / "spend.json"
    t = CostTracker(f)
    t.record("stub:x", "actor", None, 100, 100, cost=0.0)
    assert not f.exists()
    assert t.summary()["session_calls"] == 1


def test_per_call_log_for_paid_calls_only(tmp_path):
    import json
    f = tmp_path / "T0_spend.json"
    t = CostTracker(f)
    t.record("stub:x", "actor", "r", 100, 100, cost=0.0, stop="end")
    assert not t.calls_file.exists()
    t.record("claude-opus-5-5", "grand_jury", "r1", 1000, 500, 200, 100, provider="anthropic", stop="max_tokens")
    assert t.calls_file == tmp_path / "T0_spend_calls.jsonl"
    rec = json.loads(t.calls_file.read_text().splitlines()[0])
    assert (rec["model"], rec["purpose"], rec["run_id"], rec["stop"]) == ("claude-opus-5-5", "grand_jury", "r1",
                                                                          "max_tokens")
    assert (rec["input_tokens"], rec["output_tokens"], rec["cached_tokens"], rec["reasoning_tokens"]) == \
        (1000, 500, 200, 100)
    assert rec["cost"] == pytest.approx(cost_of("claude-opus-5-5", 1000, 500, 200))
    assert CostTracker().calls_file.name == "spend_calls.jsonl"


def test_budget_guard(tmp_path):
    f = tmp_path / "spend.json"
    t = CostTracker(f, budget=5.0)
    t.check()
    t.record("claude-opus-5-5", "actor", None, 1_000_000, 0)   # $4
    t.check()
    t.record("claude-opus-5-5", "actor", None, 250_000, 0)     # +$1 -> $5
    with pytest.raises(BudgetExceeded):
        t.check()
    # a new process with the same ledger is halted too
    with pytest.raises(BudgetExceeded):
        CostTracker(f, budget=5.0).check()
    t.set_budget(None)
    t.check()


def test_possibly_billed_timeout_logged_but_not_counted(tmp_path):
    """H4: a timed-out request is logged at cost 0 with a flag; the ledger total is untouched."""
    import json
    f = tmp_path / "spend.json"
    t = CostTracker(f)
    t.record("claude-opus-5-5", "actor", "r", 0, 0, cost=0.0, provider="anthropic", stop="timeout",
             possibly_billed=True)
    assert not f.exists()   # nothing measured, nothing added to the guard's total
    rec = json.loads(t.calls_file.read_text().splitlines()[0])
    assert rec["stop"] == "timeout" and rec["cost"] == 0.0 and rec["possibly_billed"] is True
    t.record("claude-opus-5-5", "actor", "r", 1000, 0)
    lines = [json.loads(x) for x in t.calls_file.read_text().splitlines()]
    assert [x["possibly_billed"] for x in lines] == [True, False]
    assert t.persisted_total() == pytest.approx(0.004)


def test_overshoot_note_states_one_stage_bound():
    """D5: README text for the parallel overshoot bound, priced at the highest rates on file."""
    note = CostTracker().overshoot_note(concurrency=5, prompt_tokens=50_000, output_tokens=32_000)
    per = (50_000 * 10.0 + 32_000 * 50.0) / 1e6
    assert f"${5 * per:.2f}" in note and f"${per:.2f} each" in note
    assert "one stage of calls" in note and "possibly_billed" in note
    assert "checked first" in note and "broken streams" in note


def test_budget_exceeded_carries_attempts_list():
    """L16: core.llm attaches complete_json's attempts to the error; default empty."""
    e = BudgetExceeded("guard")
    assert e.attempts == [] and str(e) == "guard"
    e.attempts = [{"stop": "end"}]
    assert BudgetExceeded("other").attempts == []
