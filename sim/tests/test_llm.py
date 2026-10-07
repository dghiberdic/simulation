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


def test_complete_json_max_tokens_capped_for_anthropic_and_only_once(monkeypatch):
    fake = FakeAnthropic([_anth("{", stop="max_tokens"), _anth("{", stop="max_tokens"), _anth('{"a": 1}')])
    monkeypatch.setattr(llm, "_get_client", lambda p: fake)
    obj, attempts = llm.complete_json("claude-opus-5-5", "S", "U", max_tokens=16000)
    assert obj is None and len(attempts) == 2
    assert [c["max_tokens"] for c in fake.calls] == [16000, 21000]


def test_anthropic_max_tokens_clamped():
    fake = FakeAnthropic([_anth()])
    llm._call_anthropic(fake, "claude-opus-5-5", "S", [{"role": "user", "content": "U"}],
                        40000, None, None, True, None)
    assert fake.calls[0]["max_tokens"] == llm.ANTHROPIC_MAX_TOKENS == 21000


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
    monkeypatch.setattr(llm, "_get_client", lambda p: NS(messages=NS(create=fail_and_spend)))
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
    for p in ("anthropic", "openai", "xai", "muse"):
        c = llm._get_client(p)
        assert getattr(c.timeout, "read", c.timeout) == 300.0
        assert c.max_retries == 0
    g = llm._get_client("google")
    assert g._api_client._http_options.timeout == 300_000
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
    monkeypatch.setattr(llm, "_get_client", lambda p: NS(messages=NS(create=lambda **kw: _anth())))
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
