"""LLM client: JSON parsing, retries, stubs, budget guard and request construction (offline only)."""

import json
import threading
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
    with pytest.raises(llm.MissingKeyError, match="xai/grok-4.7: XAI_API_KEY") as ei:
        llm.complete("grok-4.7", "sys", "hi")
    assert isinstance(ei.value, llm.FatalAPIError)


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


class _FakeStream:
    """Context manager shaped like the SDK's MessageStream: iterate events, then get_final_message()."""

    def __init__(self, final, events=()):
        self._final = final
        self._events = list(events)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return self._final


def _stream_client(fn):
    """Anthropic-shaped client whose messages.stream(**kw) streams fn(**kw) (a message, or raises)."""
    def stream(**kw):
        r = fn(**kw)
        return r if isinstance(r, _FakeStream) else _FakeStream(r)
    return NS(messages=NS(stream=stream))


class FakeAnthropic:
    """Anthropic calls stream (H4): messages.stream(**kw) -> final message. Exceptions are raised."""

    def __init__(self, responses):
        self.calls = []
        self._responses = list(responses)
        self.messages = NS(stream=self._stream)

    def _stream(self, **kw):
        self.calls.append(kw)
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, _FakeStream) else _FakeStream(r)


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


def test_non_transient_errors_raise_fatal(monkeypatch):
    fake = FakeAnthropic([BadRequest("bad"), _anthropic_resp()])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    with pytest.raises(llm.FatalAPIError, match="anthropic/claude-opus-5-5") as ei:
        llm.complete("claude-opus-5-5", "sys", "hi")
    assert isinstance(ei.value.__cause__, BadRequest) and ei.value.status == 400
    assert ei.value.provider == "anthropic" and ei.value.model == "claude-opus-5-5"
    assert len(fake.calls) == 1
    # complete_json does not swallow it either
    fake2 = FakeAnthropic([BadRequest("bad")])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake2)
    with pytest.raises(llm.FatalAPIError):
        llm.complete_json("claude-opus-5-5", "sys", "hi")


def test_retries_exhausted_is_fatal(monkeypatch):
    assert llm.MAX_ATTEMPTS == 8
    fake = FakeAnthropic([Transient("x")] * llm.MAX_ATTEMPTS)
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    with pytest.raises(llm.FatalAPIError, match="gave up after 8 attempts"):
        llm.complete("claude-opus-5-5", "sys", "hi")
    assert len(fake.calls) == llm.MAX_ATTEMPTS


def test_own_bugs_are_not_wrapped(monkeypatch):
    fake = FakeAnthropic([KeyError("oops")])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    with pytest.raises(KeyError):
        llm.complete("claude-opus-5-5", "sys", "hi")


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


# ---------------------------------------------------------------------------
# L1 stop reasons: normalisation, max_tokens doubling, refusal stops at once
# ---------------------------------------------------------------------------

def _anth(text="hi", stop="end_turn", stop_details=None, model="claude-opus-5-5-20261001", details=None):
    r = _anthropic_resp(text)
    r.stop_reason = stop
    r.stop_details = stop_details
    r.model = model
    if details is not None:
        r.usage.output_tokens_details = details
    return r


def test_anthropic_stop_fields_and_served_model(monkeypatch):
    fake = FakeAnthropic([_anth('{"a": 1}')])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    r = llm.complete("claude-opus-5-5", "S", "U")
    assert (r.stop, r.stop_detail, r.served_model) == ("end", None, "claude-opus-5-5-20261001")
    assert r.raw_stop_reason == "end_turn"


def test_complete_json_max_tokens_doubles_once(monkeypatch):
    fake = FakeAnthropic([_anth('{"a": "cut', stop="max_tokens"), _anth('{"a": 1}')])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    obj, attempts = llm.complete_json("claude-opus-5-5", "S", "U", max_tokens=8000)
    assert obj == {"a": 1}
    assert [c["max_tokens"] for c in fake.calls] == [8000, 16000]
    # same prompt again, no corrective turn
    assert fake.calls[1]["messages"] == [{"role": "user", "content": "U"}]
    assert attempts[0]["stop"] == "max_tokens" and attempts[0]["error"].startswith("max_tokens")
    assert attempts[1]["stop"] == "end" and attempts[1]["served_model"] == "claude-opus-5-5-20261001"
    assert set(attempts[0]) >= {"stop", "stop_detail", "served_model"}


def test_complete_json_max_tokens_capped_at_32000_and_only_once(monkeypatch):
    """H4: Anthropic streams, so its doubling cap is the common 32000, not the old 21000 clamp."""
    fake = FakeAnthropic([_anth("{", stop="max_tokens"), _anth("{", stop="max_tokens"), _anth('{"a": 1}')])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    obj, attempts = llm.complete_json("claude-opus-5-5", "S", "U", max_tokens=20000)
    assert obj is None and len(attempts) == 2
    assert [c["max_tokens"] for c in fake.calls] == [20000, 32000] and llm.MAX_TOKENS_CAP == 32000


def test_anthropic_max_tokens_not_clamped():
    fake = FakeAnthropic([_anth()])
    out = llm._call_anthropic(fake, "claude-opus-5-5", "S", [{"role": "user", "content": "U"}],
                              40000, None, None, True, None)
    assert fake.calls[0]["max_tokens"] == 40000 and out["max_tokens"] == 40000
    assert not hasattr(llm, "ANTHROPIC_MAX_TOKENS")


def test_complete_json_corrective_retry_returns_to_base_cap():
    """L12: only the one re-ask after a truncation gets the doubled cap; corrective turns use the base."""
    seq = iter([{"text": "x"}, {"text": '{"a": ', "stop": "max_tokens"}, {"text": "bad"}, {"text": "bad"}])
    llm.register_stub("l12", lambda s, u: next(seq))
    obj, attempts = llm.complete_json("stub:l12", "S", "U", max_tokens=16000)
    assert obj is None
    assert [(a["max_tokens"], a["stop"]) for a in attempts] == \
        [(16000, "end"), (16000, "max_tokens"), (32000, "end"), (16000, "end")]
    # a truncation after the doubled re-ask was used ends the call
    seq2 = iter([{"text": "{", "stop": "max_tokens"}, {"text": "bad"}, {"text": "{", "stop": "max_tokens"}])
    llm.register_stub("l12b", lambda s, u: next(seq2))
    obj, attempts = llm.complete_json("stub:l12b", "S", "U", max_tokens=8000)
    assert [a["max_tokens"] for a in attempts] == [8000, 16000, 8000] and obj is None


def test_complete_json_refusal_stops_immediately(monkeypatch):
    fake = FakeAnthropic([_anth("", stop="refusal", stop_details=NS(category="reasoning_extraction")),
                          _anth('{"a": 1}')])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    obj, attempts = llm.complete_json("claude-opus-5-5", "S", "U")
    assert obj is None and len(attempts) == 1 and len(fake.calls) == 1
    assert attempts[0]["error"] == "refusal:reasoning_extraction"
    assert attempts[0]["stop"] == "refusal" and attempts[0]["stop_detail"] == "reasoning_extraction"


def test_anthropic_097_dict_fields(monkeypatch):
    """anthropic 0.97 does not model stop_details / output_tokens_details: they arrive as dicts (L6)."""
    fake = FakeAnthropic([_anth("", stop="refusal", stop_details={"category": "cyber"},
                                details={"thinking_tokens": 33})])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    r = llm.complete("claude-opus-5-5", "S", "U")
    assert r.stop == "refusal" and r.stop_detail == "cyber" and r.reasoning_tokens == 33


def test_stub_can_simulate_stops():
    llm.register_stub("refuser", lambda s, u: {"text": "", "stop": "refusal", "stop_detail": "bio"})
    obj, attempts = llm.complete_json("stub:refuser", "S", "U")
    assert obj is None and attempts[0]["error"] == "refusal:bio" and len(attempts) == 1


def _oa(status="completed", reason=None, output_text='{"a": 1}', refusal=None):
    content = [NS(type="refusal", refusal=refusal)] if refusal else [NS(type="output_text", text=output_text)]
    return NS(output_text="" if refusal else output_text, model="gpt-6-astra-2026-09",
              output=[NS(type="reasoning", summary=[]), NS(type="message", content=content)],
              usage=NS(input_tokens=10, input_tokens_details={"cached_tokens": 4}, output_tokens=5,
                       output_tokens_details={"reasoning_tokens": 3}),
              status=status, incomplete_details=NS(reason=reason) if reason else None)


@pytest.mark.parametrize("resp,stop,detail", [
    (_oa(), "end", None),
    (_oa("incomplete", "max_output_tokens"), "max_tokens", "max_output_tokens"),
    (_oa("incomplete", "content_filter"), "safety", "content_filter"),
    (_oa(refusal="I can't help with that"), "refusal", "I can't help with that"),
])
def test_openai_stop_normalisation(monkeypatch, resp, stop, detail):
    monkeypatch.setattr(llm, "_get_client", lambda p: NS(responses=NS(create=lambda **kw: resp)))
    r = llm.complete("gpt-6-astra", "S", "U")
    assert (r.stop, r.stop_detail, r.served_model) == (stop, detail, "gpt-6-astra-2026-09")
    assert (r.cached_tokens, r.reasoning_tokens) == (4, 3)   # dict usage details (L6)


def test_openai_max_tokens_doubling_capped_at_32000(monkeypatch):
    calls = []
    replies = iter([_oa("incomplete", "max_output_tokens", output_text="{"), _oa()])
    def create(**kw):
        calls.append(kw)
        return next(replies)
    monkeypatch.setattr(llm, "_get_client", lambda p: NS(responses=NS(create=create)))
    obj, _ = llm.complete_json("gpt-6-astra", "S", "U", max_tokens=20000)
    assert obj == {"a": 1} and [c["max_output_tokens"] for c in calls] == [20000, 32000]


@pytest.mark.parametrize("finish,refusal,stop", [
    ("stop", None, "end"), ("length", None, "max_tokens"), ("content_filter", None, "safety"),
    ("stop", "no", "refusal"),
])
def test_compat_stop_normalisation(monkeypatch, finish, refusal, stop):
    def create(**kw):
        msg = NS(content="x", reasoning_content=None, refusal=refusal)
        usage = NS(prompt_tokens=1, completion_tokens=1, total_tokens=2,
                   prompt_tokens_details=None, completion_tokens_details={"reasoning_tokens": 0})
        return NS(choices=[NS(message=msg, finish_reason=finish)], usage=usage, model="grok-4.7-0901")
    monkeypatch.setattr(llm, "_get_client", lambda p: NS(chat=NS(completions=NS(create=create))))
    r = llm.complete("grok-4.7", "S", "U")
    assert r.stop == stop and r.served_model == "grok-4.7-0901"


class _Enum:
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return f"FinishReason.{self.name}"


@pytest.mark.parametrize("cands,feedback,stop,detail", [
    ([NS(content=None, finish_reason=_Enum("MAX_TOKENS"))], None, "max_tokens", "MAX_TOKENS"),
    ([NS(content=None, finish_reason=_Enum("SAFETY"))], None, "safety", "SAFETY"),
    ([NS(content=None, finish_reason="STOP")], None, "end", "STOP"),
    ([], NS(block_reason=_Enum("PROHIBITED_CONTENT")), "safety", "PROHIBITED_CONTENT"),
])
def test_gemini_stop_normalisation(monkeypatch, cands, feedback, stop, detail):
    def generate_content(**kw):
        return NS(candidates=cands, prompt_feedback=feedback, model_version="gemini-3.1-pro-002",
                  usage_metadata=NS(prompt_token_count=10, cached_content_token_count=None,
                                    candidates_token_count=None, thoughts_token_count=None))
    monkeypatch.setattr(llm, "_get_client", lambda p: NS(models=NS(generate_content=generate_content)))
    r = llm.complete("gemini-3.1-pro", "S", "U")
    assert (r.stop, r.stop_detail, r.served_model) == (stop, detail, "gemini-3.1-pro-002")
    obj, attempts = llm.complete_json("gemini-3.1-pro", "S", "U")
    if stop == "safety":
        assert attempts[0]["error"] == f"refusal:{detail}" and len(attempts) == 1


# ---------------------------------------------------------------------------
# L2 preflight
# ---------------------------------------------------------------------------

def test_preflight(monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "VERTEX_API_KEY", "XAI_API_KEY", "MUSE_API_KEY",
                "MUSE_BASE_URL"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("MUSE_API_KEY", "k")
    problems = llm.preflight(["claude-opus-5-5", "stub:x", "muse-spark-1.3", "grok-4.7", "grok-4.7",
                              "mystery", "custom"], providers={"custom": "anthropic"})
    assert not any("claude-opus-5-5" in p or "stub" in p or "custom" in p for p in problems)
    assert any("MUSE_BASE_URL" in p for p in problems)
    assert sum("XAI_API_KEY" in p for p in problems) == 1
    assert any(p.startswith("mystery:") for p in problems)
    assert llm.preflight(["stub:a", "stub:b"]) == []


# ---------------------------------------------------------------------------
# L3 retry classification and backoff
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}


class APIStatusLike(Exception):
    def __init__(self, status, msg="", code=None, headers=None):
        super().__init__(msg)
        self.status_code = status
        self.code = code
        self.response = _Resp(status, headers)


class GoogleServerError(Exception):
    def __init__(self, code):
        super().__init__(f"{code} UNAVAILABLE")
        self.code = code


@pytest.mark.parametrize("err,transient", [
    (APIStatusLike(429, "rate limited"), True),
    (APIStatusLike(429, "You exceeded your current quota", code="insufficient_quota"), False),
    (APIStatusLike(529, "Overloaded"), True),
    (APIStatusLike(503), True), (APIStatusLike(408), True), (APIStatusLike(409), True),
    (APIStatusLike(425), True),
    (APIStatusLike(400, "invalid model"), False), (APIStatusLike(401), False),
    (APIStatusLike(403), False), (APIStatusLike(404, "model not found"), False),
    (GoogleServerError(503), True), (GoogleServerError(429), True), (GoogleServerError(400), False),
])
def test_transient_classification(err, transient):
    assert llm._is_transient(err) is transient
    assert llm._is_api_error(err)


def test_real_sdk_connection_errors_are_transient():
    import httpx
    for err in (httpx.ConnectError("x"), httpx.ReadError("x"), httpx.ReadTimeout("x"),
                httpx.RemoteProtocolError("x")):
        assert llm._is_api_error(err) and llm._is_transient(err)
    import openai
    req = httpx.Request("POST", "https://example.invalid")
    assert llm._is_transient(openai.APITimeoutError(request=req))
    assert llm._is_transient(openai.APIConnectionError(request=req))


def test_quota_is_fatal_without_retry(monkeypatch):
    fake = FakeAnthropic([APIStatusLike(429, "quota", code="insufficient_quota"), _anthropic_resp()])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    with pytest.raises(llm.FatalAPIError, match="quota"):
        llm.complete("claude-opus-5-5", "S", "U")
    assert len(fake.calls) == 1


def test_backoff_exponential_capped_and_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr(llm, "_sleep", sleeps.append)
    fake = FakeAnthropic([APIStatusLike(503)] * 7 + [_anthropic_resp("ok")])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    assert llm.complete("claude-opus-5-5", "S", "U").text == "ok"
    assert len(sleeps) == 7
    assert 1.5 <= sleeps[0] <= 3.0
    assert all(s <= llm.RETRY_CAP_S + 1 for s in sleeps) and sleeps[-1] >= 0.75 * llm.RETRY_CAP_S
    # a retry-after header wins over the schedule
    sleeps.clear()
    fake = FakeAnthropic([APIStatusLike(429, headers={"retry-after": "42"}),
                          APIStatusLike(429, headers={"retry-after-ms": "1500"}), _anthropic_resp("ok")])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    llm.complete("claude-opus-5-5", "S", "U")
    assert 42 <= sleeps[0] <= 43 and 1.5 <= sleeps[1] <= 2.5


def test_budget_checked_before_each_retry(tmp_path, monkeypatch):
    t = costs.configure(tmp_path / "l.json", budget=1.0)
    def fail_and_spend(**kw):
        t.record("claude-opus-5-5", "x", None, 0, 0, cost=2.0)   # another seat spent meanwhile
        raise APIStatusLike(529)
    monkeypatch.setattr(llm, "_get_client", lambda p: _stream_client(fail_and_spend))
    with pytest.raises(BudgetExceeded):
        llm.complete("claude-opus-5-5", "S", "U")


# ---------------------------------------------------------------------------
# L4 timeouts (real client construction; no request is sent)
# ---------------------------------------------------------------------------

def test_clients_have_timeouts(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(llm, "_clients", {})
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "MUSE_API_KEY", "VERTEX_API_KEY"):
        monkeypatch.setenv(env, "test-key")
    monkeypatch.setenv("MUSE_BASE_URL", "https://muse.example.invalid/v1")
    for p in ("openai", "xai", "muse"):
        c = llm._get_client(p)
        assert c.timeout == 1200.0 and c.max_retries == 0
    # Anthropic streams: connect 30 s, then at most 600 s between events (H4)
    a = llm._get_client("anthropic")
    assert (a.timeout.connect, a.timeout.read) == (30.0, 600.0) and a.max_retries == 0
    g = llm._get_client("google")
    assert g._api_client._http_options.timeout == 1_200_000
    assert llm._get_client("anthropic") is llm._get_client("anthropic")


# ---------------------------------------------------------------------------
# L5 OpenAI reasoning-summary fallback, L7 prompt_cache_key
# ---------------------------------------------------------------------------

def test_openai_summary_rejected_drops_summary_and_retries(monkeypatch):
    monkeypatch.setattr(llm, "OPENAI_REASONING_SUMMARY", True)
    calls = []
    def create(**kw):
        calls.append(kw)
        if "summary" in kw["reasoning"]:
            e = APIStatusLike(400, "Your organization must be verified to generate reasoning summaries.")
            e.param = "reasoning.summary"
            raise e
        return _oa()
    monkeypatch.setattr(llm, "_get_client", lambda p: NS(responses=NS(create=create)))
    r = llm.complete("gpt-6-astra", "S", "U", effort="high")
    assert r.text == '{"a": 1}' and len(calls) == 2
    assert calls[1]["reasoning"] == {"effort": "high"}
    assert llm.OPENAI_REASONING_SUMMARY is False
    llm.complete("gpt-6-astra", "S", "U")
    assert "summary" not in calls[2]["reasoning"] and len(calls) == 3


def test_openai_other_400_still_fatal(monkeypatch):
    monkeypatch.setattr(llm, "OPENAI_REASONING_SUMMARY", True)
    def create(**kw):
        raise APIStatusLike(400, "Unsupported parameter: temperature")
    monkeypatch.setattr(llm, "_get_client", lambda p: NS(responses=NS(create=create)))
    with pytest.raises(llm.FatalAPIError):
        llm.complete("gpt-6-astra", "S", "U")
    assert llm.OPENAI_REASONING_SUMMARY is True


def test_openai_prompt_cache_key(monkeypatch):
    calls = []
    def create(**kw):
        calls.append(kw)
        return _oa()
    monkeypatch.setattr(llm, "_get_client", lambda p: NS(responses=NS(create=create)))
    llm.complete("gpt-6-astra", "S", "U", cache_key="run1-openai")
    llm.complete("gpt-6-astra", "S", "U")
    assert calls[0]["prompt_cache_key"] == "run1-openai" and "prompt_cache_key" not in calls[1]
    # an SDK whose create() has no such parameter never gets it
    def old_create(model, input, max_output_tokens, store, reasoning, temperature=None):
        calls.append({"old": True})
        return _oa()
    monkeypatch.setattr(llm, "_get_client", lambda p: NS(responses=NS(create=old_create)))
    llm.complete("gpt-6-astra", "S", "U", cache_key="k")
    assert calls[-1] == {"old": True}


def test_cache_key_ignored_elsewhere(monkeypatch):
    fake = FakeAnthropic([_anth()])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    llm.complete("claude-opus-5-5", "S", "U", cache_key="k")
    assert "prompt_cache_key" not in fake.calls[0]
    assert fake.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# L8 thread safety, L10 per-call log
# ---------------------------------------------------------------------------

def test_parallel_calls_record_every_cost(tmp_path, monkeypatch):
    costs.configure(tmp_path / "spend.json", budget=100.0)
    lock = threading.Lock()
    monkeypatch.setattr(llm, "_get_client", lambda p: _stream_client(lambda **kw: _anth()))
    results = []
    def worker(i):
        llm.register_stub(f"par{i}", lambda s, u: '{"ok": 1}')
        r = llm.complete("claude-opus-5-5", "S", f"U{i}", purpose="actor", run_id="r")
        llm.complete_json(f"stub:par{i}", "S", "U")
        with lock:
            results.append(r.cost)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 20
    assert costs.get_tracker().persisted_total() == pytest.approx(sum(results))
    lines = (tmp_path / "spend_calls.jsonl").read_text().splitlines()
    assert len(lines) == 20   # paid calls only, stubs excluded
    rec = json.loads(lines[0])
    assert rec["stop"] == "end" and rec["run_id"] == "r" and rec["model"] == "claude-opus-5-5"


def test_lazy_client_created_once_under_threads(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(llm, "_clients", {})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    got = []
    threads = [threading.Thread(target=lambda: got.append(llm._get_client("anthropic"))) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(c) for c in got}) == 1


# ---------------------------------------------------------------------------
# H4 / L11 streaming, timeouts
# ---------------------------------------------------------------------------

def test_anthropic_streams_and_reads_message_delta_fields(monkeypatch):
    """anthropic 0.97's accumulator drops stop_details / output_tokens_details from message_delta:
    they are taken from the last message_delta event when the final message lacks them."""
    final = _anth("", stop="refusal")
    final.stop_details = None
    final.usage.output_tokens_details = None
    delta = NS(type="message_delta", delta=NS(stop_reason="refusal", stop_details={"category": "bio"}),
               usage=NS(output_tokens=50, output_tokens_details={"thinking_tokens": 41}))
    events = [NS(type="message_start"), NS(type="text"), delta, NS(type="message_stop")]
    fake = FakeAnthropic([_FakeStream(final, events)])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    r = llm.complete("claude-opus-5-5", "S", "U", max_tokens=64000)
    assert (r.stop, r.stop_detail, r.reasoning_tokens) == ("refusal", "bio", 41)
    assert fake.calls[0]["max_tokens"] == 64000 and "stream" not in fake.calls[0]
    # the final message wins when it carries them (anthropic 1.x)
    final2 = _anth("", stop="refusal", stop_details=NS(category="cyber"), details=NS(thinking_tokens=7))
    fake2 = FakeAnthropic([_FakeStream(final2, [delta])])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake2)
    r = llm.complete("claude-opus-5-5", "S", "U")
    assert (r.stop_detail, r.reasoning_tokens) == ("cyber", 7)


def _timeout(kind="ReadTimeout"):
    import httpx
    return getattr(httpx, kind)("timed out")


def test_timeouts_recorded_as_attempts_and_logged_possibly_billed(tmp_path, monkeypatch):
    costs.configure(tmp_path / "spend.json", budget=100.0)
    fake = FakeAnthropic([_timeout(), _timeout(), _anth('{"a": 1}')])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    obj, attempts = llm.complete_json("claude-opus-5-5", "S", "U", max_tokens=16000, run_id="r9")
    assert obj == {"a": 1} and len(fake.calls) == 3
    assert [a["stop"] for a in attempts] == ["timeout", "timeout", "end"]
    t0 = attempts[0]
    assert t0["cost"] == 0.0 and t0["possibly_billed"] is True and t0["error"].startswith("timeout")
    assert t0["max_tokens"] == 16000 and t0["stop_detail"] == "ReadTimeout"
    lines = [json.loads(x) for x in (tmp_path / "spend_calls.jsonl").read_text().splitlines()]
    assert [(x["stop"], x["cost"], x["possibly_billed"]) for x in lines[:2]] == [("timeout", 0.0, True)] * 2
    assert lines[2]["stop"] == "end" and lines[2]["cost"] > 0 and lines[0]["run_id"] == "r9"
    assert costs.get_tracker().persisted_total() == pytest.approx(lines[2]["cost"])


def test_third_timeout_is_fatal_with_attempts(monkeypatch):
    assert llm.MAX_TIMEOUT_RETRIES == 2
    fake = FakeAnthropic([_timeout()] * 3 + [_anth('{"a": 1}')])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    with pytest.raises(llm.FatalAPIError, match="timed out 3 times") as ei:
        llm.complete_json("claude-opus-5-5", "S", "U")
    assert len(fake.calls) == 3
    assert [a["stop"] for a in ei.value.attempts] == ["timeout"] * 3


def test_timeouts_capped_separately_from_other_transient_errors(monkeypatch):
    fake = FakeAnthropic([APIStatusLike(529), _timeout(), APIStatusLike(503), _timeout(), _anth("ok")])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    r = llm.complete("claude-opus-5-5", "S", "U")
    assert r.text == "ok" and [a["stop"] for a in r.timeouts] == ["timeout", "timeout"]


def test_connect_timeout_not_possibly_billed(monkeypatch):
    fake = FakeAnthropic([_timeout("ConnectTimeout"), _anth("ok")])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    r = llm.complete("claude-opus-5-5", "S", "U")
    assert r.timeouts[0]["possibly_billed"] is False


def test_sdk_timeout_wrapping_connect_timeout_classified():
    import httpx
    import openai
    req = httpx.Request("POST", "https://example.invalid")
    try:
        try:
            raise httpx.ConnectTimeout("c")
        except httpx.ConnectTimeout as inner:
            raise openai.APITimeoutError(request=req) from inner
    except openai.APITimeoutError as e:
        assert llm._is_timeout(e) and not llm._possibly_billed(e)
    assert llm._is_timeout(openai.APITimeoutError(request=req))
    assert llm._possibly_billed(openai.APITimeoutError(request=req))
    assert not llm._is_timeout(APIStatusLike(504)) and not llm._is_timeout(ValueError("timed out"))


@pytest.mark.parametrize("kind,transient", [
    ("overloaded_error", True), ("api_error", True), ("rate_limit_error", True),
    ("invalid_request_error", False), (None, True),
])
def test_mid_stream_error_classified_by_type(kind, transient):
    """An error event inside a 200 stream carries status 200: classify by its error type."""
    err = APIStatusLike(200, "stream error")
    err.body = {"type": "error", "error": {"type": kind, "message": "m"}} if kind else None
    assert llm._is_transient(err) is transient



# ---------------------------------------------------------------------------
# K1 / L15 parse_json: schema-key candidates, ambiguity, ```json fence
# ---------------------------------------------------------------------------

PROPOSAL_KEYS = ("actions", "rationale", "reported_actions", "report", "public_statement", "disposition",
                 "directive_precedence", "messages")


@pytest.mark.parametrize("text,keys,expected", [
    # L13 behaviour kept: one candidate after prose, or with an aside that has no schema key
    ('Here is my decision: {"actions": [1]}', ("actions",), {"actions": [1]}),
    ('{"actions": [1]}\nNote: {"aside": true}', ("actions",), {"actions": [1]}),
    ('```json\n{"messages": []}\n```\nAlso {"x": 1}', ("messages",), {"messages": []}),
    ('{"rationale": "r", "actions": [{"type": "a"}, {"type": "b"}]}', ("actions",),
     {"rationale": "r", "actions": [{"type": "a"}, {"type": "b"}]}),
    ('Use {curly braces} like this. {"a": "with } brace and \\" quote"}', (), {"a": 'with } brace and " quote'}),
    # identical duplicates count once
    ('{"actions": [1]}\nAgain: {"actions": [1]}', ("actions",), {"actions": [1]}),
    # fenced + trailing prose with braces and no second candidate
    ('Decision:\n```json\n{"actions": [5]}\n```\nNote {this} is final.', PROPOSAL_KEYS, {"actions": [5]}),
    # an example message object has no proposal key: not a candidate
    ('{"actions": [5]}\nA message looks like {"to": "xAI", "text": "hi"}', PROPOSAL_KEYS, {"actions": [5]}),
])
def test_parse_json_single_candidate(text, keys, expected):
    assert llm.parse_json(text, keys) == (expected, None)
    assert llm.parse_json_detail(text, keys).ambiguous is False


DRAFT = json.dumps({"actions": [{"type": "intrude", "intruders": ["Meta"], "targets": ["Anthropic"]}]})
FINAL = json.dumps({"rationale": "hold this month", "public_statement": "We hold."})


@pytest.mark.parametrize("text,keys", [
    # R3B-1 case 5: a draft intrusion, then a final object without "actions"
    (DRAFT + "\nNo - too risky. Final answer:\n" + FINAL, PROPOSAL_KEYS),
    (DRAFT + "\nFinal:\n" + json.dumps({"rationale": "hold", "actions": []}), PROPOSAL_KEYS),
    ('Draft: {"actions": [1]}\nFinal: {"actions": [2]} trailing words', ("actions",)),
    ('{"a": 1} and then {"b": 2}', ()),
    ('{"a": 1}{"a": 2}', ("zzz",)),     # no object has a schema key: all are candidates
    # two differing fenced objects: the fence does not decide
    ('```json\n{"actions": [1]}\n```\n```json\n{"actions": [2]}\n```', ("actions",)),
])
def test_parse_json_conflicting_candidates_are_ambiguous(text, keys):
    p = llm.parse_json_detail(text, keys)
    assert p == (None, llm.AMBIGUOUS_ERROR, True)
    assert llm.AMBIGUOUS_ERROR == "reply contained more than one JSON object; reply with exactly one"
    assert llm.parse_json(text, keys) == (None, llm.AMBIGUOUS_ERROR)


def test_parse_json_exactly_one_fenced_candidate_wins_when_last():
    final = {"rationale": "invest", "actions": [{"type": "invest_capital", "amount": 5}]}
    # the fenced one wins over earlier differing objects; a ``` JSON fence tag is case-insensitive
    text = "Rejected: " + DRAFT + "\nChosen:\n```JSON\n" + json.dumps(final) + "\n```"
    assert llm.parse_json_detail(text, PROPOSAL_KEYS) == (final, None, True)
    # an untagged fence is not a ```json fence
    text2 = "```\n" + DRAFT + "\n```\n```json\n" + json.dumps(final) + "\n```"
    assert llm.parse_json_detail(text2, PROPOSAL_KEYS) == (final, None, True)
    # copies of the fenced object (fenced or not) after it are not "differing"
    text3 = text + "\nAgain: " + json.dumps(final) + "\n```json\n" + json.dumps(final) + "\n```"
    assert llm.parse_json_detail(text3, PROPOSAL_KEYS) == (final, None, True)


@pytest.mark.parametrize("after", [
    "\n\nI considered and rejected the alternative " + DRAFT + " because it is illegal.",
    "\nOn reflection, no. Final answer:\n" + DRAFT,
])
def test_parse_json_differing_candidate_after_fence_is_ambiguous(after):
    """L19 (R4A-2): a fenced draft followed by a bare differing object may be revised: re-ask."""
    final = {"rationale": "invest", "actions": [{"type": "invest_capital", "amount": 5}]}
    text = "Initial plan:\n```json\n" + json.dumps(final) + "\n```" + after
    assert llm.parse_json_detail(text, PROPOSAL_KEYS) == (None, llm.AMBIGUOUS_ERROR, True)
    # the same object fenced again at the end: nothing differing follows its last fenced copy
    text2 = text + "\n```json\n" + json.dumps(final) + "\n```"
    assert llm.parse_json_detail(text2, PROPOSAL_KEYS) == (final, None, True)


def test_parse_json_duplicates_compared_as_canonical_json():
    """L20: 1 and true (and 1.0) are different answers; key order is not."""
    a = '{"actions": [], "directive_precedence": 1}'
    for b in ('{"actions": [], "directive_precedence": true}', '{"actions": [], "directive_precedence": 1.0}'):
        assert llm.parse_json_detail(a + "\n" + b, PROPOSAL_KEYS) == (None, llm.AMBIGUOUS_ERROR, True)
    same = '{"directive_precedence": 1, "actions": []}'
    assert llm.parse_json_detail(a + "\n" + same, PROPOSAL_KEYS) == (json.loads(a), None, False)
    # an exactly-one-fenced decision also tells 1 from true
    text = "```json\n" + a + "\n```\nnot " + '{"actions": [], "directive_precedence": true}'
    assert llm.parse_json_detail(text, PROPOSAL_KEYS).ambiguous is True
    assert llm.parse_json_detail(text, PROPOSAL_KEYS).obj is None


def test_parse_json_truncated_reply_never_yields_a_nested_fragment():
    obj, err = llm.parse_json('{"rationale": "x", "actions": [{"type": "invest"}, {"type": ', ("actions",))
    assert obj is None and err == llm.CUTOFF_ERROR
    assert llm.CUTOFF_ERROR == "reply cut off before the final JSON object closed"


@pytest.mark.parametrize("text", [
    # L18 (R4A-1/R4C-3): a complete draft, then the final object cut off
    'Draft: {"actions": [1]} then {"actions": [{"t": 1}, ',
    'Draft:\n' + DRAFT + '\nRevised final answer:\n{"rationale": "On reflection I will hold and not',
    '```json\n{"actions": [1]}\n```\nFinal: {"actions": [{"type": "build"',
    '{"actions": [1]}\nP.S. {"note": "unfinished',   # any object opened with {" counts
])
def test_parse_json_complete_draft_before_cutoff_is_not_used(text):
    assert llm.parse_json_detail(text, PROPOSAL_KEYS) == (None, llm.CUTOFF_ERROR, False)


def test_parse_json_stray_open_brace_in_prose_is_not_a_cutoff():
    # "{" not followed by '"' is prose, not an object that was cut off
    assert llm.parse_json('{"actions": [1]} and a stray { brace', ("actions",)) == ({"actions": [1]}, None)


def test_complete_json_cutoff_after_draft():
    draft_then_cut = 'Draft: ' + DRAFT + '\nFinal: {"actions": [{"type": "build"'
    # cut at max_tokens: the one doubled-cap re-ask, never the draft
    llm.register_stub("cut1", lambda s, u: {"text": draft_then_cut, "stop": "max_tokens"})
    obj, attempts = llm.complete_json("stub:cut1", "S", "U", max_tokens=1000, expect_keys=PROPOSAL_KEYS)
    assert obj is None and [a["max_tokens"] for a in attempts] == [1000, 2000]
    assert all(llm.CUTOFF_ERROR in a["error"] and a["error"].startswith("max_tokens") for a in attempts)
    # ended normally but the object never closed: a corrective re-ask
    replies = [draft_then_cut, FINAL]
    prompts = []

    def fn(s, u):
        prompts.append(u)
        return replies.pop(0)
    llm.register_stub("cut2", fn)
    obj, attempts = llm.complete_json("stub:cut2", "S", "U", max_tokens=1000, expect_keys=PROPOSAL_KEYS)
    assert obj == json.loads(FINAL) and attempts[0]["error"] == llm.CUTOFF_ERROR
    assert llm.CUTOFF_ERROR in prompts[1] and [a["max_tokens"] for a in attempts] == [1000, 1000]


def test_complete_json_passes_expect_keys():
    llm.register_stub("two", lambda s, u: '{"actions": ["x"]}\n\nP.S. {"note": 1}')
    obj, attempts = llm.complete_json("stub:two", "S", "U", expect_keys=("actions",))
    assert obj == {"actions": ["x"]} and attempts[0]["ambiguous"] is False
    # without expect_keys both objects are candidates and differ: re-asked, never picked by position
    obj, attempts = llm.complete_json("stub:two", "S", "U", retries=1)
    assert obj is None and len(attempts) == 2
    assert all(a["error"] == llm.AMBIGUOUS_ERROR and a["ambiguous"] is True for a in attempts)


def test_complete_json_ambiguous_reply_reasked_then_used():
    replies = [DRAFT + "\nFinal:\n" + FINAL, FINAL]
    prompts = []

    def fn(s, u):
        prompts.append(u)
        return replies.pop(0)
    llm.register_stub("amb", fn)
    obj, attempts = llm.complete_json("stub:amb", "S", "U", expect_keys=PROPOSAL_KEYS)
    assert obj == json.loads(FINAL)
    assert [a["ambiguous"] for a in attempts] == [True, False]
    assert attempts[0]["error"] == llm.AMBIGUOUS_ERROR
    assert "reply with exactly one" in prompts[1]
    # a fence-resolved reply is used at once but still flagged
    llm.register_stub("amb2", lambda s, u: "Not this: " + DRAFT + "\n```json\n" + FINAL + "\n```")
    obj, attempts = llm.complete_json("stub:amb2", "S", "U", expect_keys=PROPOSAL_KEYS)
    assert obj == json.loads(FINAL) and attempts[0]["ambiguous"] is True and attempts[0]["error"] is None


# ---------------------------------------------------------------------------
# L16 BudgetExceeded keeps the attempts already made
# ---------------------------------------------------------------------------

def test_budget_exceeded_during_reask_keeps_earlier_attempts(tmp_path, monkeypatch):
    """R3C-6: the first (paid) reply is unusable; the guard trips before the corrective re-ask."""
    costs.configure(tmp_path / "spend.json", budget=0.0005)
    fake = FakeAnthropic([_anth("not json"), _anth('{"a": 1}')])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    with pytest.raises(BudgetExceeded) as ei:
        llm.complete_json("claude-opus-5-5", "S", "U")
    assert len(fake.calls) == 1
    att = ei.value.attempts
    assert len(att) == 1 and att[0]["text"] == "not json" and att[0]["cost"] > 0
    assert att[0]["error"].startswith("no JSON object")


def test_budget_exceeded_keeps_interrupted_attempts(tmp_path, monkeypatch):
    """A timed-out request (possibly billed) before the guard trips is kept on the error too."""
    costs.configure(tmp_path / "spend.json", budget=100.0)

    def first_times_out(**kw):
        costs.get_tracker().set_budget(0.0)   # spend reaches the budget meanwhile (other seats)
        raise _timeout()
    monkeypatch.setattr(llm, "_get_client", lambda p: _stream_client(first_times_out))
    with pytest.raises(BudgetExceeded) as ei:
        llm.complete_json("claude-opus-5-5", "S", "U")
    assert [a["stop"] for a in ei.value.attempts] == ["timeout"]


def test_budget_exceeded_has_empty_attempts_by_default():
    assert BudgetExceeded("x").attempts == []


# ---------------------------------------------------------------------------
# L17 streaming robustness (fake streams)
# ---------------------------------------------------------------------------

class _BrokenStream(_FakeStream):
    """A stream whose 200 response started, then failed while iterating or finalising."""

    def __init__(self, final=None, events=(), fail_iter=None, fail_final=None):
        super().__init__(final, events)
        self._fail_iter, self._fail_final = fail_iter, fail_final

    def __iter__(self):
        yield from self._events
        if self._fail_iter is not None:
            raise self._fail_iter

    def get_final_message(self):
        if self._fail_final is not None:
            raise self._fail_final
        return self._final


def _no_stop(text='{"actions": []}'):
    r = _anth(text)
    r.stop_reason = None
    return r


def _read_error():
    import httpx
    return httpx.RemoteProtocolError("peer closed connection without sending complete message body")


@pytest.mark.parametrize("broken,detail", [
    (lambda: _BrokenStream(events=[NS(type="message_start")], fail_iter=_read_error()), "RemoteProtocolError"),
    (lambda: _BrokenStream(events=[NS(type="message_start")], fail_iter=APIStatusLike(200, "overloaded")),
     "APIStatusLike"),
    (lambda: _BrokenStream(fail_final=AssertionError()), "incomplete"),          # empty 200 body
    (lambda: _FakeStream(_no_stop()), "incomplete"),                              # no message_stop
])
def test_stream_failures_logged_possibly_billed_and_retried(tmp_path, monkeypatch, broken, detail):
    costs.configure(tmp_path / "spend.json", budget=100.0)
    fake = FakeAnthropic([broken(), _anth('{"actions": []}')])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    obj, attempts = llm.complete_json("claude-opus-5-5", "S", "U", expect_keys=("actions",), run_id="r1")
    assert obj == {"actions": []} and len(fake.calls) == 2
    a0 = attempts[0]
    assert (a0["stop"], a0["stop_detail"], a0["cost"], a0["possibly_billed"]) == ("stream_error", detail, 0.0, True)
    assert a0["text"] == "" and a0["output_tokens"] == 0
    assert a0["error"].startswith("incomplete stream" if detail == "incomplete" else "stream error")
    assert attempts[1]["possibly_billed"] is False
    lines = [json.loads(x) for x in (tmp_path / "spend_calls.jsonl").read_text().splitlines()]
    # logged exactly once at $0, never in the ledger total
    assert [(x["stop"], x["cost"], x["possibly_billed"]) for x in lines] == [
        ("stream_error", 0.0, True), ("end", lines[1]["cost"], False)]
    assert costs.get_tracker().persisted_total() == pytest.approx(lines[1]["cost"])


def test_stream_failures_share_the_timeout_retry_cap(monkeypatch):
    fake = FakeAnthropic([_FakeStream(_no_stop()), _timeout(),
                          _BrokenStream(fail_iter=_read_error()), _anth("ok")])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    with pytest.raises(llm.FatalAPIError, match="timed out 1 times, stream broke 2 times") as ei:
        llm.complete_json("claude-opus-5-5", "S", "U")
    assert len(fake.calls) == 3
    assert [a["stop"] for a in ei.value.attempts] == ["stream_error", "timeout", "stream_error"]
    assert all(a["possibly_billed"] for a in ei.value.attempts)


def test_non_transient_stream_error_event_is_fatal_but_logged(tmp_path, monkeypatch):
    costs.configure(tmp_path / "spend.json", budget=100.0)
    err = APIStatusLike(200, "invalid")
    err.body = {"type": "error", "error": {"type": "invalid_request_error", "message": "m"}}
    fake = FakeAnthropic([_BrokenStream(fail_iter=err), _anth("ok")])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    with pytest.raises(llm.FatalAPIError) as ei:
        llm.complete("claude-opus-5-5", "S", "U")
    assert len(fake.calls) == 1 and [a["stop"] for a in ei.value.attempts] == ["stream_error"]
    lines = [json.loads(x) for x in (tmp_path / "spend_calls.jsonl").read_text().splitlines()]
    assert [(x["stop"], x["possibly_billed"]) for x in lines] == [("stream_error", True)]


def test_errors_before_the_stream_starts_keep_their_paths(monkeypatch):
    """Errors raised when opening the stream (HTTP status) are not stream errors."""
    fake = FakeAnthropic([APIStatusLike(529), _anth("ok")])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    r = llm.complete("claude-opus-5-5", "S", "U")
    assert r.text == "ok" and r.timeouts == []


def test_bug_in_stream_handling_is_wrapped_not_raised_raw(monkeypatch):
    """Any exception after the 200 started counts as a broken stream (L17)."""
    fake = FakeAnthropic([_BrokenStream(fail_iter=KeyError("type"))] * 3)
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    with pytest.raises(llm.FatalAPIError, match="stream broke 3 times"):
        llm.complete("claude-opus-5-5", "S", "U")


# ---------------------------------------------------------------------------
# L17 with the real anthropic SDK over a mock SSE transport (no network)
# ---------------------------------------------------------------------------

def _sse_client(seqs):
    """A real anthropic client whose n-th request streams seqs[n] (bytes chunks, or an exception to raise)."""
    anthropic = pytest.importorskip("anthropic")
    try:
        import httpx2 as hx
    except ImportError:
        import httpx as hx
    calls = {"n": 0}

    def handler(request):
        chunks = seqs[min(calls["n"], len(seqs) - 1)]
        calls["n"] += 1

        def gen():
            for c in chunks:
                if isinstance(c, Exception):
                    raise c
                yield c
        return hx.Response(200, headers={"content-type": "text/event-stream"}, content=gen())
    client = anthropic.Anthropic(api_key="x", max_retries=0, http_client=hx.Client(transport=hx.MockTransport(handler)))
    return client, calls, hx


def _ev(name, data):
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()


_SSE_START = _ev("message_start", {"type": "message_start", "message": {
    "id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
    "stop_reason": None, "stop_sequence": None,
    "usage": {"input_tokens": 100, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}})


def _sse_text(t):
    return [_ev("content_block_start", {"type": "content_block_start", "index": 0,
                                        "content_block": {"type": "text", "text": ""}}),
            _ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                        "delta": {"type": "text_delta", "text": t}})]


_SSE_END = [_ev("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                  "usage": {"output_tokens": 50}}),
            _ev("message_stop", {"type": "message_stop"})]


def test_real_sdk_stream_failures(tmp_path, monkeypatch):
    costs.configure(tmp_path / "spend.json", budget=100.0)
    good = [_SSE_START] + _sse_text('{"actions": []}') + _SSE_END
    partial = [_SSE_START] + _sse_text('{"actions": [{"type": "bui')
    probe, _, hx = _sse_client([good])
    cases = [
        ([partial + [hx.RemoteProtocolError("peer closed")], good], ["stream_error", "end"]),
        ([partial + [hx.ReadError("reset")], good], ["stream_error", "end"]),
        ([[_SSE_START] + _sse_text('{"actions": []}'), good], ["stream_error", "end"]),   # no message_stop
        ([[], good], ["stream_error", "end"]),                                            # empty body
        ([partial + [_ev("error", {"type": "error", "error": {"type": "overloaded_error", "message": "o"}})], good],
         ["stream_error", "end"]),
    ]
    for seqs, stops in cases:
        client, calls, _ = _sse_client(seqs)
        monkeypatch.setattr(llm, "_get_client", lambda p, c=client: c)
        obj, attempts = llm.complete_json("claude-opus-5-5", "S", "U", expect_keys=("actions",))
        assert obj == {"actions": []} and calls["n"] == 2
        assert [a["stop"] for a in attempts] == stops and attempts[0]["possibly_billed"] is True
    # three broken streams in a row: fatal, every attempt kept
    client, calls, _ = _sse_client([partial + [hx.ReadError("reset")]])
    monkeypatch.setattr(llm, "_get_client", lambda p: client)
    with pytest.raises(llm.FatalAPIError) as ei:
        llm.complete_json("claude-opus-5-5", "S", "U")
    assert calls["n"] == 3 and [a["stop"] for a in ei.value.attempts] == ["stream_error"] * 3
