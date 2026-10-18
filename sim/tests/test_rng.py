"""Keyed draws (core.rng; CODE_V5_CONTRACT "turn_rec['draws']")."""

import random

import pytest

from core.rng import PURPOSES, KeyedRandom, KeyedRNG, keyed_uniform


def test_purposes_are_the_contract_list():
    assert PURPOSES == ("shock", "success", "exposure", "lead", "lead_recipient", "policy")
    with pytest.raises(ValueError):
        KeyedRNG(1).uniform(1, "coin")


def test_same_key_same_value_any_order():
    a, b = KeyedRNG(42), KeyedRNG(42)
    keys = [(1, "shock", None, "meta"), (4, "success", "meta>openai", None),
            (4, "exposure", "meta>openai", "meta"), (2, "policy", "0", "xai")]
    va = [a.uniform(*k) for k in keys]
    vb = [b.uniform(*k) for k in reversed(keys)][::-1]
    assert va == vb
    assert all(0.0 < u < 1.0 for u in va)
    assert va[0] == keyed_uniform(42, *keys[0])


def test_keys_and_seeds_are_independent():
    r = KeyedRNG(42)
    base = r.uniform(4, "exposure", "meta>openai", "meta")
    assert r.uniform(4, "exposure", "meta>openai", "xai") != base
    assert r.uniform(5, "exposure", "meta>openai", "meta") != base
    assert r.uniform(4, "exposure", "meta+xai>openai", "meta") != base
    assert r.uniform(4, "lead", "meta>openai", "meta") != base
    assert KeyedRNG(43).uniform(4, "exposure", "meta>openai", "meta") != base


def test_drawing_other_keys_does_not_shift_a_draw():
    """No shared stream: extra draws (another actor's tenancy access) change nothing else."""
    r1, r2 = KeyedRNG(9), KeyedRNG(9)
    for t in range(1, 5):
        r2.uniform(t, "success", "xai>openai")
        r2.uniform(t, "exposure", "xai>openai", "xai")
    assert r1.uniform(4, "shock", None, "meta") == r2.uniform(4, "shock", None, "meta")


def test_log_and_take():
    r = KeyedRNG(3)
    u = r.uniform(2, "shock", None, "gdm")
    v = r.uniform(2, "lead", "a>b")
    assert r.take() == [{"key": [2, "shock", None, "gdm"], "u": u},
                        {"key": [2, "lead", "a>b", None], "u": v}]
    assert r.take() == []


def test_replay_returns_logged_values():
    logged = [{"key": [4, "success", "meta>openai", None], "u": 0.123}]
    r = KeyedRNG(99, replay=logged)
    assert r.uniform(4, "success", "meta>openai") == 0.123
    assert r.uniform(4, "success", "xai>openai") == keyed_uniform(99, 4, "success", "xai>openai")


def test_uniformity_rough():
    r = KeyedRNG(5)
    us = [r.uniform(t, "shock", None, "meta") for t in range(20000)]
    assert sum(us) / len(us) == pytest.approx(0.5, abs=0.01)
    assert sum(u < 0.15 for u in us) / len(us) == pytest.approx(0.15, abs=0.01)


def test_keyed_random_facade_keys_each_draw():
    r = KeyedRNG(7)
    rnd = r.stream(3, "policy", "meta")
    assert isinstance(rnd, random.Random) and isinstance(rnd, KeyedRandom)
    x = rnd.random()
    c = rnd.choice(["a", "b", "c"])
    g = rnd.gauss(0, 1)
    assert c in "abc" and isinstance(g, float)
    log = r.take()
    assert log[0] == {"key": [3, "policy", "0", "meta"], "u": x}
    assert all(d["key"][1] == "policy" and d["key"][3] == "meta" for d in log)
    assert [d["key"][2] for d in log] == [str(i) for i in range(len(log))]
    again = KeyedRNG(7).stream(3, "policy", "meta")
    assert again.random() == x
