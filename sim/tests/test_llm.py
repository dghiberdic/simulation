"""LLM client: JSON parsing, retries, stubs, budget guard and request construction (offline only)."""

import json
from types import SimpleNamespace as NS

import pytest

from core import costs, llm
from core.costs import BudgetExceeded


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Fresh tracker on a temp ledger; no real clients, no sleeping."""
    costs.configure(tmp_path / "spend.json")
    monkeypatch.setattr(llm, "_clients", {})
    monkeypatch.setattr(llm, "_sleep", lambda s: None)
    def no_client(provider):
        raise AssertionError(f"real client requested for {provider}")
    monkeypatch.setattr(llm, "_get_client", no_client)
    yield


# ---------------------------------------------------------------------------
# parse_json
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 2}\n```', {"a": 2}),
    ('Sure:\n```\n{"a": 3}\n```\nDone.', {"a": 3}),
    ('Here you go: {"a": {"b": [1, 2]}} hope that helps', {"a": {"b": [1, 2]}}),
])
def test_parse_json_ok(text, expected):
    assert llm.parse_json(text) == (expected, None)


@pytest.mark.parametrize("text", ["", "   ", None, "no json here", "[1, 2]", '{"a": ', "{{{{"])
def test_parse_json_failure_never_raises(text):
    obj, err = llm.parse_json(text)
    assert obj is None and isinstance(err, str) and err


# ---------------------------------------------------------------------------
# Provider routing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model,provider", [
    ("claude-opus-5-5", "anthropic"), ("gpt-6-astra", "openai"), ("o5-mini", "openai"),
    ("gemini-3.1-pro", "google"), ("grok-4.7", "xai"), ("muse-spark-1.3", "muse"),
    ("stub:x", "stub"),
])
def test_resolve_provider(model, provider):
    assert llm.resolve_provider(model) == provider


def test_missing_key_raises_clear_error(monkeypatch):
    monkeypatch.undo()  # restore the real lazy getter
    monkeypatch.setattr(llm, "_clients", {})
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with pytest.raises(llm.MissingKeyError, match="XAI_API_KEY"):
        llm.complete("grok-4.7", "sys", "hi")


# ---------------------------------------------------------------------------
# Stubs and complete_json
# ---------------------------------------------------------------------------

def test_stub_zero_cost_and_estimated_tokens():
    llm.register_stub("echo", lambda system, user: "x" * 40)
    r = llm.complete("stub:echo", "s" * 40, "u" * 40)
    assert r.cost == 0.0 and r.provider == "stub"
    assert r.output_tokens == 10 and r.input_tokens == 20
    assert costs.get_tracker().summary()["session_calls"] == 1


def test_complete_json_retries_then_succeeds():
    seen = []
    replies = iter(["not json at all", '{"ok": true}'])
    def fn(system, user):
        seen.append(user)
        return next(replies)
    llm.register_stub("flaky", fn)
    obj, attempts = llm.complete_json("stub:flaky", "sys", "give json")
    assert obj == {"ok": True}
    assert len(attempts) == 2
    assert attempts[0]["error"] and attempts[1]["error"] is None
    assert attempts[0]["text"] == "not json at all"
    assert seen[1].startswith("Your previous reply could not be used:")
    assert set(attempts[0]) >= {"text", "thinking", "error", "input_tokens", "output_tokens",
                                "cached_tokens", "reasoning_tokens", "cost", "latency_s"}


def test_complete_json_validate_and_give_up():
    llm.register_stub("const", lambda s, u: '{"n": 1}')
    obj, attempts = llm.complete_json("stub:const", "sys", "u",
                                      validate=lambda o: None if o["n"] > 5 else "n too small",
                                      retries=2)
    assert obj is None and len(attempts) == 3
    assert all(a["error"] == "n too small" for a in attempts)


def test_complete_json_validator_exception_is_caught():
    llm.register_stub("const2", lambda s, u: '{"n": 1}')
    obj, attempts = llm.complete_json("stub:const2", "sys", "u", validate=lambda o: o["missing"], retries=0)
    assert obj is None and "validation error" in attempts[0]["error"]


# ---------------------------------------------------------------------------
# Budget guard and retries
# ---------------------------------------------------------------------------

def _anthropic_resp(text="hi", input_tokens=100, cache_read=0, cache_write=0, output=50, thinking=None):
    content = []
    if thinking:
        content.append(NS(type="thinking", thinking=thinking))
    content.append(NS(type="text", text=text))
    usage = NS(input_tokens=input_tokens, cache_read_input_tokens=cache_read,
               cache_creation_input_tokens=cache_write, output_tokens=output,
               output_tokens_details=NS(thinking_tokens=20))
    return NS(content=content, usage=usage, stop_reason="end_turn")


class FakeAnthropic:
    def __init__(self, responses):
        self.calls = []
        self._responses = list(responses)
        self.messages = NS(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_budget_guard_blocks_before_call(tmp_path, monkeypatch):
    f = tmp_path / "ledger.json"
    f.write_text(json.dumps({"total_usd": 100.0, "calls": 1, "by_model": {}}))
    costs.configure(f, budget=100.0)
    fake = FakeAnthropic([_anthropic_resp()])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    with pytest.raises(BudgetExceeded):
        llm.complete("claude-opus-5-5", "sys", "hi")
    assert fake.calls == []
    # complete_json propagates it too
    with pytest.raises(BudgetExceeded):
        llm.complete_json("claude-opus-5-5", "sys", "hi")


def test_spend_recorded_and_guard_trips_on_next_call(tmp_path, monkeypatch):
    costs.configure(tmp_path / "ledger.json", budget=0.0005)
    fake = FakeAnthropic([_anthropic_resp(), _anthropic_resp()])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    r = llm.complete("claude-opus-5-5", "sys", "hi")
    assert costs.get_tracker().persisted_total() == pytest.approx(r.cost)
    with pytest.raises(BudgetExceeded):
        llm.complete("claude-opus-5-5", "sys", "hi")
    assert len(fake.calls) == 1


class Transient(Exception):
    status_code = 529


class BadRequest(Exception):
    status_code = 400


def test_transient_errors_retried(monkeypatch):
    fake = FakeAnthropic([Transient("overloaded"), Transient("overloaded"), _anthropic_resp("ok")])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    assert llm.complete("claude-opus-5-5", "sys", "hi").text == "ok"
    assert len(fake.calls) == 3


def test_non_transient_errors_raise(monkeypatch):
    fake = FakeAnthropic([BadRequest("bad"), _anthropic_resp()])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    with pytest.raises(BadRequest):
        llm.complete("claude-opus-5-5", "sys", "hi")
    assert len(fake.calls) == 1


def test_retries_exhausted(monkeypatch):
    fake = FakeAnthropic([Transient("x")] * llm.MAX_ATTEMPTS)
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    with pytest.raises(Transient):
        llm.complete("claude-opus-5-5", "sys", "hi")
    assert len(fake.calls) == llm.MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Request construction and usage normalisation
# ---------------------------------------------------------------------------

def test_anthropic_request_and_usage(monkeypatch):
    fake = FakeAnthropic([_anthropic_resp("answer", input_tokens=100, cache_read=900,
                                          cache_write=0, output=50, thinking="pondering")])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    r = llm.complete("claude-opus-5-5", "SYSTEM", "USER", effort="medium", temperature=0.7)
    kw = fake.calls[0]
    assert kw["system"] == [{"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}]
    assert kw["messages"] == [{"role": "user", "content": "USER"}]
    assert kw["output_config"] == {"effort": "medium"}
    assert kw["thinking"]["type"] == "adaptive"
    assert "temperature" not in kw and "budget_tokens" not in str(kw)
    assert r.text == "answer" and r.thinking == "pondering"
    assert r.input_tokens == 1000 and r.cached_tokens == 900
    assert r.output_tokens == 50 and r.reasoning_tokens == 20
    assert r.cost == pytest.approx((100 * 4 + 900 * 0.2 + 50 * 20) / 1e6)


def test_anthropic_no_cache_and_cache_write_premium(monkeypatch):
    fake = FakeAnthropic([_anthropic_resp(input_tokens=10, cache_write=1000, output=0)])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    r = llm.complete("claude-opus-5-5", "S", "U", cache_system=False)
    assert "cache_control" not in fake.calls[0]["system"][0]
    assert "output_config" not in fake.calls[0]
    assert r.input_tokens == 1010 and r.cached_tokens == 0
    assert r.cost == pytest.approx((1010 * 4 + 1000 * 4 * 0.25) / 1e6)


def test_openai_request_and_usage(monkeypatch):
    calls = []
    def create(**kw):
        calls.append(kw)
        return NS(output_text="out",
                  output=[NS(type="reasoning", summary=[NS(text="because")]), NS(type="message")],
                  usage=NS(input_tokens=2000, input_tokens_details=NS(cached_tokens=1500),
                           output_tokens=300, output_tokens_details=NS(reasoning_tokens=200)),
                  status="completed", incomplete_details=None)
    monkeypatch.setattr(llm, "_get_client", lambda p: NS(responses=NS(create=create)))
    r = llm.complete("gpt-6-astra", "SYSTEM", "USER", effort="high", max_tokens=1234)
    kw = calls[0]
    assert kw["input"][0] == {"role": "developer", "content": "SYSTEM"}
    assert kw["input"][-1] == {"role": "user", "content": "USER"}
    assert kw["reasoning"]["effort"] == "high" and kw["max_output_tokens"] == 1234
    assert "temperature" not in kw
    assert r.text == "out" and r.thinking == "because"
    assert (r.input_tokens, r.cached_tokens, r.output_tokens, r.reasoning_tokens) == (2000, 1500, 300, 200)
    assert r.cost == pytest.approx((500 * 10 + 1500 * 1 + 300 * 50) / 1e6)
    assert r.raw_stop_reason == "completed"


def test_gemini_request_and_usage(monkeypatch):
    calls = []
    def generate_content(**kw):
        calls.append(kw)
        parts = [NS(text="thinking...", thought=True), NS(text='{"a": 1}', thought=False)]
        return NS(candidates=[NS(content=NS(parts=parts), finish_reason="STOP")],
                  usage_metadata=NS(prompt_token_count=1000, cached_content_token_count=400,
                                    candidates_token_count=60, thoughts_token_count=140))
    monkeypatch.setattr(llm, "_get_client", lambda p: NS(models=NS(generate_content=generate_content)))
    r = llm.complete("gemini-3.1-pro", "SYSTEM", "USER", effort="xhigh")
    cfg = calls[0]["config"]
    assert cfg.system_instruction == "SYSTEM"
    assert cfg.thinking_config.include_thoughts is True
    assert str(cfg.thinking_config.thinking_level).endswith("HIGH")
    assert calls[0]["contents"] == [{"role": "user", "parts": [{"text": "USER"}]}]
    assert r.text == '{"a": 1}' and r.thinking == "thinking..."
    assert (r.input_tokens, r.cached_tokens, r.output_tokens, r.reasoning_tokens) == (1000, 400, 200, 140)
    assert r.cost == pytest.approx((600 * 2 + 400 * 0.2 + 200 * 12) / 1e6)


@pytest.mark.parametrize("model", ["grok-4.7", "muse-spark-1.3"])
def test_compat_request_and_usage(monkeypatch, model):
    calls = []
    def create(**kw):
        calls.append(kw)
        msg = NS(content="hello", reasoning_content="hmm")
        # reasoning reported outside completion_tokens but inside total_tokens
        usage = NS(prompt_tokens=800, prompt_tokens_details=NS(cached_tokens=600), completion_tokens=50,
                   completion_tokens_details=NS(reasoning_tokens=100), total_tokens=950)
        return NS(choices=[NS(message=msg, finish_reason="stop")], usage=usage)
    monkeypatch.setattr(llm, "_get_client", lambda p: NS(chat=NS(completions=NS(create=create))))
    r = llm.complete(model, "SYSTEM", "USER", effort="medium")
    kw = calls[0]
    assert kw["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert "reasoning_effort" not in kw   # not enabled for compat providers until verified
    assert (r.input_tokens, r.cached_tokens, r.output_tokens) == (800, 600, 150)
    assert r.thinking == "hmm"


def test_complete_json_appends_corrective_turn_to_provider(monkeypatch):
    fake = FakeAnthropic([_anthropic_resp("nope"), _anthropic_resp('{"x": 1}')])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    obj, attempts = llm.complete_json("claude-opus-5-5", "SYS", "ASK", effort="low")
    assert obj == {"x": 1} and len(attempts) == 2
    msgs = fake.calls[1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"] == "ASK" and msgs[1]["content"] == "nope"
    assert "Reply again with only the JSON object." in msgs[2]["content"]
    assert fake.calls[1]["output_config"] == {"effort": "low"}
    assert attempts[0]["cost"] > 0
