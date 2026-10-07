"""A2A channel: per-turn budget, truncation, drops, stage visibility, broadcast and history."""

import pytest

from core.a2a import A2AChannel, TRUNCATION_MARK

LABS = ["anthropic", "openai", "gdm", "meta", "xai"]


def words(tokens: int) -> str:
    return "abcd" * tokens   # len // 4 == tokens


def test_budget_shared_across_stages_truncates_then_drops():
    ch = A2AChannel(LABS, budget_tokens=500)
    assert ch.send(1, "offer", "openai", "gdm", words(300)).tokens == 300
    m = ch.send(1, "reply", "openai", "meta", words(300))
    assert m.truncated and m.tokens == 200 and m.text.endswith(TRUNCATION_MARK)
    assert ch.tokens_remaining("openai", 1) == 0
    assert ch.send(1, "proposal", "openai", "xai", "hello there") is None
    dropped = ch.log()[-1]
    assert dropped["dropped"] and "budget" in dropped["reason"]
    # dropped message is never delivered; budget resets next turn
    assert ch.inbox("xai", 2, "offer") == []
    assert ch.tokens_remaining("openai", 2) == 500


def test_separate_mode_visibility():
    ch = A2AChannel(LABS, 500, mode="separate")
    ch.send(1, "offer", "anthropic", "openai", "offer A")
    assert ch.inbox("openai", 1, "offer") == []
    got = ch.inbox("openai", 1, "reply")
    assert [m.text for m in got] == ["offer A"]
    assert ch.inbox("openai", 1, "reply") == []          # shown once
    ch.send(1, "reply", "openai", "anthropic", "reply B")
    assert ch.inbox("anthropic", 1, "reply") == []
    assert [m.text for m in ch.inbox("anthropic", 1, "proposal")] == ["reply B"]
    ch.send(1, "proposal", "anthropic", "openai", "with proposal C")
    assert ch.inbox("openai", 1, "proposal") == []
    assert [m.text for m in ch.inbox("openai", 2, "offer")] == ["with proposal C"]


def test_inbox_catches_up_on_skipped_stages():
    ch = A2AChannel(LABS, 500)
    ch.send(1, "offer", "anthropic", "openai", "early")
    assert [m.text for m in ch.inbox("openai", 1, "proposal")] == ["early"]


def test_merged_mode():
    ch = A2AChannel(LABS, 500, mode="merged")
    with pytest.raises(ValueError):
        ch.send(1, "offer", "anthropic", "openai", "x")
    ch.send(1, "proposal", "anthropic", "openai", "hi")
    assert ch.inbox("openai", 1, "proposal") == []
    assert [m.text for m in ch.inbox("openai", 2, "proposal")] == ["hi"]


def test_broadcast_all():
    ch = A2AChannel(LABS, 500)
    m = ch.send(1, "proposal", "xai", "all", "to everyone")
    assert sorted(m.recipients) == sorted(k for k in LABS if k != "xai")
    for k in LABS:
        got = ch.inbox(k, 2, "offer")
        assert len(got) == (0 if k == "xai" else 1)


def test_invalid_recipient_dropped():
    ch = A2AChannel(LABS, 500)
    assert ch.send(1, "offer", "gdm", "china", "hi") is None
    assert ch.send(1, "offer", "gdm", "gdm", "self") is None
    assert all(e["dropped"] for e in ch.log())
    assert ch.tokens_remaining("gdm", 1) == 500


def test_history_window_and_visibility():
    ch = A2AChannel(LABS, 500)
    for t in range(1, 6):
        ch.send(t, "proposal", "meta", "anthropic", f"t{t}")
    ch.send(5, "offer", "openai", "anthropic", "offer5")
    ch.send(5, "offer", "openai", "gdm", "not anthropic's")
    hist = [m.text for m in ch.history("anthropic", 5, turns=3, stage="offer")]
    # turns 3..5; the turn-5 proposal message and the turn-5 offer are not yet visible
    assert hist == ["t3", "t4"]
    hist = [m.text for m in ch.history("anthropic", 5, turns=3, stage="reply")]
    assert hist == ["t3", "t4", "offer5"]
    # the sender sees its own messages as soon as sent
    assert [m.text for m in ch.history("meta", 5, turns=2)] == ["t4", "t5"]


def test_log_metadata():
    ch = A2AChannel(LABS, 500)
    ch.send(3, "reply", "gdm", ["openai", "meta"], "hey")
    e = ch.log()[0]
    for key in ("turn", "stage", "sender", "recipients", "text", "tokens", "truncated", "dropped"):
        assert key in e
    assert e["recipients"] == ["openai", "meta"] and e["turn"] == 3 and e["stage"] == "reply"
