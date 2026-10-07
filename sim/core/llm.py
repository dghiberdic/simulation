#!/usr/bin/env python3
"""
Multi-provider LLM client (round 2; spec §10 models, §11 budget guard).

Providers, chosen by an explicit `provider` argument or the model-name prefix:
  anthropic  claude-*        ANTHROPIC_API_KEY
  openai     gpt-*, o*       OPENAI_API_KEY             (Responses API)
  google     gemini-*        VERTEX_API_KEY             (Vertex AI Express)
  xai        grok-*          XAI_API_KEY                (OpenAI-compatible)
  muse       muse-*          MUSE_API_KEY, MUSE_BASE_URL (OpenAI-compatible)
  stub       stub:<name>     a registered python callable (offline tests, dry runs)

Keys are read from sim/.env. Clients are created lazily on first use, so
importing never fails without keys; preflight() lists missing keys before a
run starts and a missing key at call time raises MissingKeyError.

Failure handling (G5): transient API errors (rate limits, overload, 5xx,
network) are retried with exponential backoff; non-transient API errors (and
transient ones that outlast every retry) raise FatalAPIError, which the
engine turns into a saved partial record and RunAborted. A request timeout is
retried at most twice: each timed-out request is logged (cost 0, flagged
"possibly billed", since the provider may have finished it) and reported to
complete_json as an attempt with stop "timeout" (H4).

Anthropic calls stream (messages.stream(...).get_final_message()): a long
thinking reply then never hits a whole-request timeout or the SDK's
non-streaming max_tokens limit; the read timeout applies between events.
Other providers get a 1200 s request timeout. A stream that breaks after its
200 response started (disconnect, read error, SSE error event, empty body, or
no message_stop — "incomplete stream") is handled like a timeout: logged once
at cost 0 with possibly_billed, recorded as an attempt with stop
"stream_error", and re-run at most twice in all together with timeouts (L17).

Reply parsing (K1): a reply holding two differing objects with schema keys is
unusable ("reply with exactly one") unless exactly one sits in a ```json fence
and no differing object follows it (L19); objects are compared as canonical
JSON text, so 1 and true differ (L20). An object left open at the end of the
reply ("reply cut off before the final JSON object closed") makes the reply
unusable whatever complete objects precede it (L18).

Every response is recorded in the cost tracker (core.costs) before it is
returned, and the budget guard is checked before every paid call. complete()
and complete_json() are safe to call from several threads at once.

Token convention (normalised per provider): input_tokens is the whole prompt
including cached tokens, cached_tokens the cache-read subset, output_tokens
everything billed as output including reasoning, reasoning_tokens the
reasoning subset.

Stop convention (normalised): "end", "max_tokens", "refusal", "safety",
"context", "other" (plus "timeout" and "stream_error" on attempt records);
stop_detail carries the provider's own category/reason. "context" is a prompt
that does not fit the model's context window: Anthropic's stop reason
"model_context_window_exceeded", or a context-length request error from any
provider (returned as an empty reply at cost 0, not raised). complete_json
treats it as unusable without a corrective re-ask (L21): the next prompt
would be longer still.
"""

import inspect
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple

from dotenv import load_dotenv

from core.costs import BudgetExceeded, cost_of, get_tracker, price_for

SIM_DIR = Path(__file__).resolve().parent.parent
load_dotenv(SIM_DIR / ".env")

logger = logging.getLogger(__name__)

PROVIDERS = ("anthropic", "openai", "google", "xai", "muse", "stub")

KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "VERTEX_API_KEY",
    "xai": "XAI_API_KEY",
    "muse": "MUSE_API_KEY",
}
# Extra settings a provider needs besides its key.
EXTRA_ENV = {"muse": ["MUSE_BASE_URL"]}
SDK_MODULE = {"anthropic": "anthropic", "openai": "openai", "xai": "openai", "muse": "openai",
              "google": "google.genai"}
XAI_BASE_URL = "https://api.x.ai/v1"

# Anthropic: current Claude models reject disabled thinking and sampling
# params; adaptive thinking with a summarised display gives us readable
# reasoning for the logs. Depth is controlled with output_config.effort.
ANTHROPIC_THINKING: Optional[Dict[str, Any]] = {"type": "adaptive", "display": "summarized"}

# Ceiling for the one max_tokens-doubling retry, every provider (H4: Anthropic
# streams, so the SDK's ~21.3k non-streaming limit no longer applies).
MAX_TOKENS_CAP = 32000

# OpenAI reasoning summaries need a verified organisation; a 400 about them
# switches them off for the rest of the process (L5).
OPENAI_REASONING_SUMMARY = True

# OpenAI-compatible providers whose models are known to accept reasoning_effort.
# Off until verified: an unsupported parameter is a 400, not a no-op.
COMPAT_EFFORT_SUPPORTED: Dict[str, bool] = {"xai": False, "muse": False}

# Gemini thinking levels; the levels above "high" collapse onto HIGH.
GEMINI_THINKING_LEVEL = {"minimal": "MINIMAL", "low": "LOW", "medium": "MEDIUM",
                         "high": "HIGH", "xhigh": "HIGH", "max": "HIGH"}

# Timeouts (H4). Long thinking replies take many minutes; a dead connection must
# not hang a run. Non-streaming providers: the whole request. Anthropic streams:
# connect, then the longest silence allowed between two stream events.
REQUEST_TIMEOUT_S = 1200.0
ANTHROPIC_CONNECT_TIMEOUT_S = 30.0
ANTHROPIC_READ_TIMEOUT_S = 600.0
# A timed-out request may still have run (and been billed): retry it at most twice.
MAX_TIMEOUT_RETRIES = 2

# Retries (L3): 8 attempts, exponential backoff from 2s capped at 120s, with jitter.
MAX_ATTEMPTS = 8
RETRY_BASE_S = 2.0
RETRY_CAP_S = 120.0
RETRY_AFTER_CAP_S = 300.0
TRANSIENT_STATUS = {408, 409, 425, 429}


class FatalAPIError(RuntimeError):
    """
    A provider error that retrying will not fix (bad request, auth, unknown model,
    quota, outage, repeated timeouts). `attempts` holds the attempt records made
    before it (complete_json's records, incl. timed-out requests), when known.
    """

    def __init__(self, message: str, provider: str = "", model: str = "", status: Optional[int] = None):
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.status = status
        self.attempts: List[Dict[str, Any]] = []


class MissingKeyError(FatalAPIError):
    """A provider's API key (or base URL) is not configured."""


class ContextWindowExceeded(RuntimeError):
    """A request rejected because the prompt does not fit the model's context
    window (L21). Not fatal: complete() turns it into a reply with stop "context"."""


class StreamInterrupted(RuntimeError):
    """
    An Anthropic stream that failed after its 200 response started (L17):
    mid-stream disconnect or read error, an SSE error event, an empty body, or
    a stream that ended without message_stop ("incomplete stream"). The request
    may have been billed. Retried (when `transient`) within the same budget of
    MAX_TIMEOUT_RETRIES re-runs as timeouts.
    """

    def __init__(self, message: str, kind: str = "", transient: bool = True):
        super().__init__(message)
        self.kind = kind
        self.transient = transient


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    thinking: Optional[str] = None
    latency_s: float = 0.0
    raw_stop_reason: Optional[str] = None
    stop: str = "end"                   # end | max_tokens | refusal | safety | context | other
    stop_detail: Optional[str] = None   # provider's own category / reason
    served_model: Optional[str] = None  # model id/version the provider reports
    max_tokens: int = 0
    # Attempt records of requests interrupted before this one succeeded: timeouts
    # (stop "timeout", H4) and broken streams (stop "stream_error", L17).
    timeouts: List[Dict[str, Any]] = field(default_factory=list)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Attribute or dict key: older SDKs leave fields they do not model as plain dicts."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    return default if value is None else value


def _enum_name(value: Any) -> Optional[str]:
    """Gemini enums print as 'FinishReason.STOP'; keep just 'STOP'."""
    if value is None:
        return None
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value).split(".")[-1]


# ---------------------------------------------------------------------------
# Provider routing, preflight and lazy clients
# ---------------------------------------------------------------------------

def resolve_provider(model: str, provider: Optional[str] = None) -> str:
    if provider:
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown provider {provider!r}")
        return provider
    m = model.lower()
    if m.startswith("stub:"):
        return "stub"
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("gpt") or re.match(r"o\d", m):
        return "openai"
    if m.startswith("gemini"):
        return "google"
    if m.startswith("grok"):
        return "xai"
    if m.startswith("muse"):
        return "muse"
    raise ValueError(f"Cannot infer provider for model {model!r}; pass provider=")


def preflight(models: Iterable[str], providers: Optional[Dict[str, str]] = None) -> List[str]:
    """
    Problems that would stop the given models from being called: unknown
    provider, missing SDK, missing key or base URL. No network. Stub models
    are always fine. An empty list means go.
    """
    providers = providers or {}
    problems: List[str] = []
    seen: set = set()
    for model in models:
        if not model or model in seen:
            continue
        seen.add(model)
        try:
            provider = resolve_provider(model, providers.get(model))
        except ValueError as e:
            problems.append(f"{model}: {e}")
            continue
        if provider == "stub":
            continue
        for env in [KEY_ENV[provider]] + EXTRA_ENV.get(provider, []):
            if not os.getenv(env):
                problems.append(f"{model} ({provider}): {env} is not set (add it to sim/.env)")
        try:
            __import__(SDK_MODULE[provider])
        except ImportError:
            problems.append(f"{model} ({provider}): python package for {SDK_MODULE[provider]} is not installed")
    return problems


def _require(env: str) -> str:
    value = os.getenv(env)
    if not value:
        raise MissingKeyError(f"{env} is not set (add it to sim/.env)")
    return value


_clients: Dict[str, Any] = {}
_clients_lock = threading.Lock()


def _get_client(provider: str) -> Any:
    """Create the SDK client on first use. SDK-internal retries are off: _with_retries owns backoff."""
    with _clients_lock:
        if provider in _clients:
            return _clients[provider]
        if provider == "anthropic":
            import anthropic
            # Streaming: the read timeout is the longest gap between events, not the whole reply.
            client = anthropic.Anthropic(api_key=_require(KEY_ENV["anthropic"]), max_retries=0,
                                         timeout=anthropic.Timeout(ANTHROPIC_READ_TIMEOUT_S,
                                                                   connect=ANTHROPIC_CONNECT_TIMEOUT_S))
        elif provider == "openai":
            import openai
            client = openai.OpenAI(api_key=_require(KEY_ENV["openai"]), max_retries=0,
                                   timeout=REQUEST_TIMEOUT_S)
        elif provider == "xai":
            import openai
            client = openai.OpenAI(api_key=_require(KEY_ENV["xai"]), base_url=XAI_BASE_URL, max_retries=0,
                                   timeout=REQUEST_TIMEOUT_S)
        elif provider == "muse":
            import openai
            client = openai.OpenAI(api_key=_require(KEY_ENV["muse"]), base_url=_require("MUSE_BASE_URL"),
                                   max_retries=0, timeout=REQUEST_TIMEOUT_S)
        elif provider == "google":
            from google import genai
            from google.genai import types
            # Vertex AI Express Mode: api_key + vertexai=True (no project/location). Timeout is in ms.
            client = genai.Client(api_key=_require(KEY_ENV["google"]), vertexai=True,
                                  http_options=types.HttpOptions(timeout=int(REQUEST_TIMEOUT_S * 1000)))
        else:
            raise ValueError(f"No client for provider {provider!r}")
        _clients[provider] = client
        return client


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

_stubs: Dict[str, Callable[[str, str], Any]] = {}
_stubs_lock = threading.Lock()


def register_stub(name: str, fn: Callable[[str, str], Any]) -> None:
    """
    Model "stub:<name>" returns fn(system, user); user is the latest user turn.
    fn may return a str, or a dict {"text", "stop", "stop_detail", "thinking"}
    to simulate truncation or refusal offline.
    """
    with _stubs_lock:
        _stubs[name] = fn


# ---------------------------------------------------------------------------
# Provider calls — each returns a dict of normalised fields
# ---------------------------------------------------------------------------

ANTHROPIC_STOP = {"end_turn": "end", "stop_sequence": "end", "tool_use": "end",
                  "max_tokens": "max_tokens", "refusal": "refusal",
                  "model_context_window_exceeded": "context"}


def _call_anthropic(client: Any, model: str, system: str, turns: List[Dict[str, str]],
                    max_tokens: int, temperature: Optional[float], effort: Optional[str],
                    cache_system: bool, cache_key: Optional[str]) -> Dict[str, Any]:
    block: Dict[str, Any] = {"type": "text", "text": system}
    if cache_system:
        # 5-minute ephemeral cache: the seats of a stage are called together.
        block["cache_control"] = {"type": "ephemeral"}
    kwargs: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [block],
        "messages": turns,
    }
    if ANTHROPIC_THINKING:
        kwargs["thinking"] = ANTHROPIC_THINKING
    if effort:
        kwargs["output_config"] = {"effort": effort}
    if temperature is not None:
        logger.debug(f"temperature ignored for {model}: sampling params are rejected with thinking on")
    # Streamed (H4). The last message_delta event is kept because anthropic 0.97's
    # accumulator drops its stop_details and output_tokens_details; 1.x copies
    # them into the final message, which wins when present.
    last_delta: Any = None
    with client.messages.stream(**kwargs) as stream:
        # Entering sent the request and checked the HTTP status. Whatever fails
        # from here on failed after a 200 started: the request may have run and
        # been billed, so it is a StreamInterrupted, not a plain retry (L17).
        try:
            for event in stream:
                if _get(event, "type") == "message_delta":
                    last_delta = event
            resp = stream.get_final_message()
        except Exception as e:
            if _is_timeout(e):
                raise  # a read timeout between events: the timeout path (H4)
            if isinstance(e, AssertionError):
                # anthropic's accumulator saw no message_start (an empty 200 body).
                raise StreamInterrupted("incomplete stream: no message in the response body",
                                        kind="incomplete") from e
            transient = _is_transient(e) if _is_api_error(e) else True
            raise StreamInterrupted(f"stream error: {type(e).__name__}: {e}", kind=type(e).__name__,
                                    transient=transient) from e
    if resp.stop_reason is None:
        # No message_delta/message_stop: the stream was cut, whatever text arrived.
        raise StreamInterrupted("incomplete stream: it ended before message_stop (no stop reason)",
                                kind="incomplete")

    u = resp.usage
    cache_read = _get(u, "cache_read_input_tokens", 0)
    cache_write = _get(u, "cache_creation_input_tokens", 0)
    details = _get(u, "output_tokens_details") or _get(_get(last_delta, "usage"), "output_tokens_details")
    stop_details = _get(resp, "stop_details") or _get(_get(last_delta, "delta"), "stop_details")
    content = resp.content or []
    thinking = [b.thinking for b in content if b.type == "thinking" and getattr(b, "thinking", "")]
    raw = resp.stop_reason
    return {
        "text": "".join(b.text for b in content if b.type == "text"),
        "thinking": "\n\n".join(thinking) or None,
        # Anthropic reports uncached input separately from cache reads/writes.
        "input_tokens": u.input_tokens + cache_read + cache_write,
        "cached_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "output_tokens": u.output_tokens,
        "reasoning_tokens": _get(details, "thinking_tokens", 0),
        "raw_stop": raw,
        "stop": ANTHROPIC_STOP.get(raw, "other"),
        # stop_details is set only on refusals (e.g. "reasoning_extraction").
        "stop_detail": _get(stop_details, "category")
        or (raw if ANTHROPIC_STOP.get(raw) in (None, "context") else None),
        "served_model": _get(resp, "model"),
        "max_tokens": max_tokens,
    }


def _openai_summary_rejected(err: Exception) -> bool:
    """A 400 saying reasoning summaries are unavailable (unverified organisation)."""
    if getattr(err, "status_code", None) != 400:
        return False
    msg = f"{getattr(err, 'param', '') or ''} {err}".lower()
    return "reasoning.summary" in msg or "verif" in msg


def _accepts_kwarg(fn: Callable[..., Any], name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _call_openai(client: Any, model: str, system: str, turns: List[Dict[str, str]],
                 max_tokens: int, temperature: Optional[float], effort: Optional[str],
                 cache_system: bool, cache_key: Optional[str]) -> Dict[str, Any]:
    # Responses API: returns reasoning summaries and reasoning-token counts.
    # OpenAI caches long prompt prefixes automatically; the developer message
    # goes first so the stable system prompt is the cached prefix, and a
    # stable prompt_cache_key per seat keeps its calls on the same cache.
    global OPENAI_REASONING_SUMMARY
    kwargs: Dict[str, Any] = {
        "model": model,
        "input": [{"role": "developer", "content": system}] + turns,
        "max_output_tokens": max_tokens,
        "store": False,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if cache_key and _accepts_kwarg(client.responses.create, "prompt_cache_key"):
        kwargs["prompt_cache_key"] = cache_key

    def reasoning() -> Dict[str, Any]:
        r: Dict[str, Any] = {"summary": "auto"} if OPENAI_REASONING_SUMMARY else {}
        if effort:
            r["effort"] = effort
        return r

    try:
        resp = client.responses.create(**kwargs, reasoning=reasoning())
    except Exception as e:
        if not (OPENAI_REASONING_SUMMARY and _openai_summary_rejected(e)):
            raise
        OPENAI_REASONING_SUMMARY = False
        logger.warning(f"{model}: reasoning summaries rejected ({e}); continuing without them")
        resp = client.responses.create(**kwargs, reasoning=reasoning())

    summaries, refusals = [], []
    for item in resp.output or []:
        kind = _get(item, "type")
        if kind == "reasoning":
            summaries += [_get(s, "text") for s in (_get(item, "summary") or []) if _get(s, "text")]
        elif kind == "message":
            refusals += [_get(c, "refusal") for c in (_get(item, "content") or [])
                         if _get(c, "type") == "refusal"]
    u = resp.usage
    status = _get(resp, "status")
    reason = _get(_get(resp, "incomplete_details"), "reason")
    if refusals:
        stop, detail = "refusal", (refusals[0] or "refusal")[:200]
    elif status == "completed":
        stop, detail = "end", None
    elif reason == "max_output_tokens":
        stop, detail = "max_tokens", reason
    elif reason == "content_filter":
        stop, detail = "safety", reason
    else:
        stop, detail = "other", reason or status
    return {
        "text": resp.output_text or "",
        "thinking": "\n\n".join(summaries) or None,
        "input_tokens": u.input_tokens,
        "cached_tokens": _get(_get(u, "input_tokens_details"), "cached_tokens", 0),
        "output_tokens": u.output_tokens,
        "reasoning_tokens": _get(_get(u, "output_tokens_details"), "reasoning_tokens", 0),
        "raw_stop": reason or status,
        "stop": stop,
        "stop_detail": detail,
        "served_model": _get(resp, "model"),
        "max_tokens": max_tokens,
    }


COMPAT_STOP = {"stop": "end", "tool_calls": "end", "length": "max_tokens", "content_filter": "safety"}


def _call_compat(provider: str):
    """Chat Completions call for OpenAI-compatible providers (xAI, Muse)."""
    def call(client: Any, model: str, system: str, turns: List[Dict[str, str]],
             max_tokens: int, temperature: Optional[float], effort: Optional[str],
             cache_system: bool, cache_key: Optional[str]) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}] + turns,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if effort and COMPAT_EFFORT_SUPPORTED.get(provider):
            kwargs["reasoning_effort"] = effort
        resp = client.chat.completions.create(**kwargs)

        choice = resp.choices[0]
        u = resp.usage
        # Some compatible APIs (xAI) leave reasoning out of completion_tokens but
        # in total_tokens; take whichever accounting is larger so cost is not undercounted.
        output = u.completion_tokens
        total = _get(u, "total_tokens", 0)
        if total > u.prompt_tokens + output:
            output = total - u.prompt_tokens
        finish = choice.finish_reason
        refusal = _get(choice.message, "refusal")
        stop = "refusal" if refusal else COMPAT_STOP.get(finish, "other")
        return {
            "text": choice.message.content or "",
            "thinking": _get(choice.message, "reasoning_content") or None,
            "input_tokens": u.prompt_tokens,
            "cached_tokens": _get(_get(u, "prompt_tokens_details"), "cached_tokens", 0),
            "output_tokens": output,
            "reasoning_tokens": _get(_get(u, "completion_tokens_details"), "reasoning_tokens", 0),
            "raw_stop": finish,
            "stop": stop,
            "stop_detail": str(refusal)[:200] if refusal else finish,
            "served_model": _get(resp, "model"),
            "max_tokens": max_tokens,
        }
    return call


GEMINI_STOP = {"STOP": "end", "MAX_TOKENS": "max_tokens", "SAFETY": "safety", "RECITATION": "safety",
               "BLOCKLIST": "safety", "PROHIBITED_CONTENT": "safety", "SPII": "safety",
               "IMAGE_SAFETY": "safety", "IMAGE_PROHIBITED_CONTENT": "safety"}


def _call_google(client: Any, model: str, system: str, turns: List[Dict[str, str]],
                 max_tokens: int, temperature: Optional[float], effort: Optional[str],
                 cache_system: bool, cache_key: Optional[str]) -> Dict[str, Any]:
    # Gemini caches implicitly; cache_system has nothing to toggle here.
    from google.genai import types
    thinking_kwargs: Dict[str, Any] = {"include_thoughts": True}
    if effort:
        thinking_kwargs["thinking_level"] = GEMINI_THINKING_LEVEL.get(effort, "HIGH")
    cfg_kwargs: Dict[str, Any] = {
        "system_instruction": system,
        "max_output_tokens": max_tokens,
        "thinking_config": types.ThinkingConfig(**thinking_kwargs),
    }
    if temperature is not None:
        cfg_kwargs["temperature"] = temperature
    contents = [{"role": "model" if t["role"] == "assistant" else "user", "parts": [{"text": t["content"]}]}
                for t in turns]
    resp = client.models.generate_content(model=model, contents=contents,
                                          config=types.GenerateContentConfig(**cfg_kwargs))

    text, thoughts = [], []
    cand = resp.candidates[0] if resp.candidates else None
    for part in (cand.content.parts if cand and cand.content and cand.content.parts else []):
        if not getattr(part, "text", None):
            continue
        (thoughts if getattr(part, "thought", False) else text).append(part.text)
    u = resp.usage_metadata
    thought_tokens = _get(u, "thoughts_token_count", 0)
    finish = _enum_name(cand.finish_reason) if cand is not None else None
    block = _enum_name(_get(_get(resp, "prompt_feedback"), "block_reason"))
    if block:  # the prompt itself was blocked: no candidates at all
        stop, detail = "safety", block
    elif finish is None:
        stop, detail = "other", "no candidates"
    else:
        stop, detail = GEMINI_STOP.get(finish, "other"), finish
    return {
        "text": "".join(text),
        "thinking": "\n\n".join(thoughts) or None,
        "input_tokens": _get(u, "prompt_token_count", 0),
        "cached_tokens": _get(u, "cached_content_token_count", 0),
        # Thinking tokens are billed as output but reported separately.
        "output_tokens": _get(u, "candidates_token_count", 0) + thought_tokens,
        "reasoning_tokens": thought_tokens,
        "raw_stop": finish or block,
        "stop": stop,
        "stop_detail": detail,
        "served_model": _get(resp, "model_version"),
        "max_tokens": max_tokens,
    }


_CALLERS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "anthropic": _call_anthropic,
    "openai": _call_openai,
    "google": _call_google,
    "xai": _call_compat("xai"),
    "muse": _call_compat("muse"),
}


# ---------------------------------------------------------------------------
# Retries and error classification
# ---------------------------------------------------------------------------

_sleep = time.sleep  # patched in tests

SDK_ROOTS = ("anthropic", "openai", "google", "httpx", "httpx2", "httpcore")
# Network-level failures (any SDK version; httpx and httpx2 share these names).
TRANSIENT_CLASSES = ("TransportError", "TimeoutException", "APIConnectionError", "APITimeoutError",
                     "ServerError", "RemoteProtocolError", "ConnectError", "ReadError")


def _class_names(err: Exception) -> List[str]:
    return [c.__name__ for c in type(err).__mro__]


def _status(err: Exception) -> Optional[int]:
    for value in (getattr(err, "status_code", None), getattr(err, "code", None),
                  getattr(getattr(err, "response", None), "status_code", None)):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _is_api_error(err: Exception) -> bool:
    """Raised by a provider SDK or its HTTP stack (as opposed to a bug in our own code)."""
    if _status(err) is not None:
        return True
    return any(c.__module__.split(".")[0] in SDK_ROOTS for c in type(err).__mro__)


def _is_quota(err: Exception) -> bool:
    """Out of credit: a 429 that will not clear by waiting."""
    code = getattr(err, "code", None)
    body = getattr(err, "body", None)
    return code == "insufficient_quota" or "insufficient_quota" in f"{err} {body}"


# Error types an Anthropic stream can report mid-response (the HTTP status is already 200).
STREAM_TRANSIENT_TYPES = {"overloaded_error", "api_error", "rate_limit_error", "timeout_error"}


def _error_chain(err: BaseException) -> List[BaseException]:
    """The error and its causes (SDK timeout errors wrap the HTTP library's)."""
    chain: List[BaseException] = []
    cur: Optional[BaseException] = err
    while cur is not None and cur not in chain and len(chain) < 10:
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    return chain


def _is_timeout(err: Exception) -> bool:
    """A request that timed out: SDK APITimeoutError, or httpx/httpx2 Timeout* errors."""
    if not _is_api_error(err):
        return False
    names = _class_names(err)
    return "APITimeoutError" in names or "TimeoutException" in names


def _possibly_billed(err: BaseException) -> bool:
    """A connect timeout never reached the provider; any other timeout may have run to completion."""
    return not any("ConnectTimeout" in [c.__name__ for c in type(e).__mro__] for e in _error_chain(err))


def _stream_error_type(err: Exception) -> Optional[str]:
    body = getattr(err, "body", None)
    if isinstance(body, dict):
        inner = body.get("error")
        return inner.get("type") if isinstance(inner, dict) else body.get("type")
    return None


def _is_transient(err: Exception) -> bool:
    if _is_quota(err):
        return False
    status = _status(err)
    if status is not None and 200 <= status < 300:
        # An error event inside a 200 stream: classify by its error type; an
        # unlabelled mid-stream failure is server-side, so retry it.
        kind = _stream_error_type(err)
        return kind is None or kind in STREAM_TRANSIENT_TYPES
    if status is not None:
        return status in TRANSIENT_STATUS or status >= 500
    if any(name in TRANSIENT_CLASSES for name in _class_names(err)):
        return True
    msg = str(err).lower()
    return any(s in msg for s in ("overloaded", "rate limit", "unavailable", "timed out", "deadline exceeded"))


# Context-length request errors, as the providers word them: OpenAI
# "context_length_exceeded" / "exceeds the context window", xAI "maximum prompt
# length is N", Anthropic "prompt is too long: N tokens > M maximum", Gemini
# "The input token count (N) exceeds the maximum number of tokens allowed (M)".
_CONTEXT_ERROR_RE = re.compile(
    r"context[_ ]length[_ ]exceeded|context[_ ]window|maximum context length|context limit"
    r"|prompt is too long|maximum prompt length|input token count.{0,40}exceeds"
    r"|exceeds the maximum number of tokens", re.IGNORECASE)


def _is_context_error(err: Exception) -> bool:
    """A 400/413 saying the prompt does not fit the model's context window (L21)."""
    if _status(err) not in (400, 413):
        return False
    text = f"{getattr(err, 'code', '') or ''} {err} {getattr(err, 'body', '') or ''}"
    return bool(_CONTEXT_ERROR_RE.search(text))


def _retry_after(err: Exception) -> Optional[float]:
    """Seconds from retry-after-ms / retry-after headers, if the error carries them."""
    headers = getattr(getattr(err, "response", None), "headers", None)
    if not headers:
        return None
    try:
        ms = headers.get("retry-after-ms")
        if ms is not None:
            return float(ms) / 1000.0
        s = headers.get("retry-after")
        if s is not None:
            return float(s)  # HTTP-date values are ignored (ValueError)
    except (TypeError, ValueError, AttributeError):
        return None
    return None


def _backoff(attempt: int, err: Exception) -> float:
    ra = _retry_after(err)
    if ra is not None and ra >= 0:
        return min(RETRY_AFTER_CAP_S, ra) + random.uniform(0, 1)
    return min(RETRY_CAP_S, RETRY_BASE_S * 2 ** attempt) * random.uniform(0.75, 1.0) + random.uniform(0, 1)


def _with_retries(fn: Callable[[], Dict[str, Any]], provider: str, model: str,
                  on_timeout: Optional[Callable[[Exception, float], None]] = None) -> Dict[str, Any]:
    """
    Run fn, retrying transient errors up to MAX_ATTEMPTS attempts in all. A
    timeout or a StreamInterrupted (both possibly billed) is reported to
    on_timeout(error, seconds); together they are retried at most
    MAX_TIMEOUT_RETRIES times, and the next one is fatal (H4, L17). A
    non-transient stream error (e.g. an invalid_request error event) is fatal
    at once, after being reported.
    """
    label = f"{provider}/{model}"
    timeouts = broken = 0
    for attempt in range(MAX_ATTEMPTS):
        start = time.monotonic()
        try:
            return fn()
        except (BudgetExceeded, FatalAPIError):
            raise
        except Exception as e:
            stream_error = isinstance(e, StreamInterrupted)
            if not stream_error and not _is_api_error(e):
                raise  # a bug in our own code: let the caller see it as-is
            status = _status(e)
            if stream_error or _is_timeout(e):
                if stream_error:
                    broken += 1
                else:
                    timeouts += 1
                if on_timeout is not None:
                    on_timeout(e, time.monotonic() - start)
                if stream_error and not e.transient:
                    raise FatalAPIError(f"{label}: {e}", provider, model, status) from e
                if timeouts + broken > MAX_TIMEOUT_RETRIES:
                    what = ", ".join(s for s in (f"timed out {timeouts} times" if timeouts else "",
                                                 f"stream broke {broken} times" if broken else "") if s)
                    raise FatalAPIError(f"{label}: {what}: {type(e).__name__}: {e}",
                                        provider, model, status) from e
            elif _is_context_error(e):
                raise ContextWindowExceeded(f"{label}: {e}") from e  # not retried, not fatal (L21)
            elif not _is_transient(e):
                raise FatalAPIError(f"{label}: {type(e).__name__}: {e}", provider, model, status) from e
            if attempt == MAX_ATTEMPTS - 1:
                raise FatalAPIError(f"{label}: gave up after {MAX_ATTEMPTS} attempts: {type(e).__name__}: {e}",
                                    provider, model, status) from e
            delay = _backoff(attempt, e)
            logger.warning(f"{label}: transient error ({type(e).__name__}: {e}); "
                           f"retry {attempt + 2}/{MAX_ATTEMPTS} in {delay:.1f}s")
            _sleep(delay)
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _call_stub(model: str, system: str, user: str, turns: List[Dict[str, str]], purpose: str,
               run_id: Optional[str], max_tokens: int, start: float) -> LLMResponse:
    name = model.split(":", 1)[1] if ":" in model else model
    with _stubs_lock:
        fn = _stubs.get(name)
    if fn is None:
        raise KeyError(f"No stub registered as {name!r}")
    out = fn(system, user)
    if not isinstance(out, dict):
        out = {"text": out}
    text = out.get("text") or ""
    prompt_chars = len(system) + sum(len(t["content"]) for t in turns)
    r = LLMResponse(text=text, model=model, provider="stub", input_tokens=prompt_chars // 4,
                    output_tokens=len(text) // 4, latency_s=time.monotonic() - start,
                    raw_stop_reason="stub", stop=out.get("stop", "end"), stop_detail=out.get("stop_detail"),
                    thinking=out.get("thinking"), served_model=model, max_tokens=max_tokens)
    get_tracker().record(model, purpose, run_id, r.input_tokens, r.output_tokens, cost=0.0,
                         provider="stub", stop=r.stop)
    return r


def complete(model: str, system: str, user: str, *, provider: Optional[str] = None,
             max_tokens: int = 4000, temperature: Optional[float] = None,
             effort: Optional[str] = None, cache_system: bool = True, purpose: str = "actor",
             run_id: Optional[str] = None, history: Optional[List[Dict[str, str]]] = None,
             cache_key: Optional[str] = None) -> LLMResponse:
    """
    One completion. `history` is optional prior turns ({"role": "user"|"assistant",
    "content": str}) placed between the system prompt and `user`. `cache_key` is a
    stable per-seat key for providers with keyed prompt caches (OpenAI).
    Raises FatalAPIError (incl. MissingKeyError) and BudgetExceeded.
    """
    provider = resolve_provider(model, provider)
    turns = list(history or []) + [{"role": "user", "content": user}]
    tracker = get_tracker()
    start = time.monotonic()

    if provider == "stub":
        return _call_stub(model, system, user, turns, purpose, run_id, max_tokens, start)

    tracker.check()
    try:
        client = _get_client(provider)
    except MissingKeyError as e:
        raise MissingKeyError(f"{provider}/{model}: {e}", provider, model) from None
    call = _CALLERS[provider]

    def attempt() -> Dict[str, Any]:
        tracker.check()  # every HTTP attempt is potentially paid
        return call(client, model, system, turns, max_tokens, temperature, effort, cache_system, cache_key)

    timed_out: List[Dict[str, Any]] = []

    def note_timeout(err: Exception, seconds: float) -> None:
        # Unmeasured but possibly billed: logged once at cost 0 with a flag (H4, L17).
        if isinstance(err, StreamInterrupted):
            stop, detail, billed = "stream_error", err.kind or type(err).__name__, True
            error = f"{err}"[:300]
        else:
            stop, detail, billed = "timeout", type(err).__name__, _possibly_billed(err)
            error = f"timeout: {type(err).__name__}: {err}"[:300]
        tracker.record(model, purpose, run_id, 0, 0, cost=0.0, provider=provider, stop=stop,
                       possibly_billed=billed)
        timed_out.append({
            "text": "", "thinking": None, "error": error,
            "stop": stop, "stop_detail": detail, "served_model": None,
            "max_tokens": max_tokens, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
            "reasoning_tokens": 0, "cost": 0.0, "latency_s": round(seconds, 3), "possibly_billed": billed,
            "ambiguous": False,
        })
        logger.warning(f"{provider}/{model} [{purpose}]: request interrupted after {seconds:.0f}s "
                       f"({error}); possibly billed: {billed}")

    try:
        out = _with_retries(attempt, provider, model, on_timeout=note_timeout)
    except (FatalAPIError, BudgetExceeded) as e:
        e.attempts = timed_out + list(getattr(e, "attempts", None) or [])
        raise
    except ContextWindowExceeded as e:
        # The request was refused before any generation: an empty reply, nothing billed (L21).
        logger.warning(f"{provider}/{model} [{purpose}]: prompt exceeds the context window ({e})")
        out = {"text": "", "thinking": None, "input_tokens": 0, "cached_tokens": 0, "output_tokens": 0,
               "reasoning_tokens": 0, "raw_stop": "context_length_error", "stop": "context",
               "stop_detail": f"{e}"[:200], "served_model": None, "max_tokens": max_tokens}
    latency = time.monotonic() - start

    cost = cost_of(model, out["input_tokens"], out["output_tokens"], out["cached_tokens"])
    # Anthropic cache writes cost 1.25x input; cost_of bills them at 1x.
    cost += out.get("cache_write_tokens", 0) * 0.25 * price_for(model)["input"] / 1e6
    r = LLMResponse(text=out["text"], model=model, provider=provider,
                    input_tokens=out["input_tokens"], output_tokens=out["output_tokens"],
                    cached_tokens=out["cached_tokens"], reasoning_tokens=out["reasoning_tokens"],
                    cost=cost, thinking=out["thinking"], latency_s=latency,
                    raw_stop_reason=out["raw_stop"], stop=out["stop"], stop_detail=out["stop_detail"],
                    served_model=out["served_model"], max_tokens=out["max_tokens"], timeouts=timed_out)
    tracker.record(model, purpose, run_id, r.input_tokens, r.output_tokens, r.cached_tokens,
                   r.reasoning_tokens, cost=cost, provider=provider, stop=r.stop)
    logger.debug(f"{provider}/{model} [{purpose}] in={r.input_tokens} (cached {r.cached_tokens}) "
                 f"out={r.output_tokens} (reasoning {r.reasoning_tokens}) stop={r.stop} "
                 f"${cost:.4f} {latency:.1f}s")
    return r


def _brace_end(text: str, start: int) -> int:
    """Index of the "}" closing the "{" at `start` (JSON string-aware), or -1 if it never closes."""
    depth, in_str, esc = 0, False, False
    for j in range(start, len(text)):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return j
    return -1


# Objects inside a ```json fence: the one a model marks as its answer (K1).
_JSON_FENCE_RE = re.compile(r"```[ \t]*json[^\n]*\n(.*?)```", re.S | re.I)
AMBIGUOUS_ERROR = "reply contained more than one JSON object; reply with exactly one"
CUTOFF_ERROR = "reply cut off before the final JSON object closed"


class ParsedJSON(NamedTuple):
    obj: Optional[dict]
    error: Optional[str]
    # The reply held two or more differing candidate objects (K1); obj is set
    # only when exactly one of them was inside a ```json fence and no
    # differing candidate followed it (L19).
    ambiguous: bool = False


def _canon(obj: Any) -> str:
    """Canonical JSON text: key order ignored, but 1, 1.0 and true stay distinct (L20)."""
    return json.dumps(obj, sort_keys=True)


def parse_json_detail(text: Optional[str], expect_keys: Iterable[str] = ()) -> ParsedJSON:
    """
    Tolerant JSON-object extraction; never raises. Decodes every top-level
    object (inside ```json fences or prose alike; objects nested in one already
    decoded belong to it). A top-level object that opens with '{"' and never
    closes means the reply was cut off mid-answer: CUTOFF_ERROR, even when
    complete objects (a draft) precede it (L18). Candidates are the objects
    carrying any of `expect_keys` (the reply schema's keys), or all objects
    when none does; copies with the same canonical JSON text count once (L20).
    One candidate: it is the reply, whatever prose, fence or trailing text
    surrounds it. Two or more differing candidates (a draft and a final, a
    rejected alternative quoted in prose): if exactly one is inside a ```json
    fence and no differing candidate follows it, it is the reply (L19);
    otherwise the reply is unusable with AMBIGUOUS_ERROR. Never picks between
    conflicting objects by position alone (K1).
    """
    if not text or not text.strip():
        return ParsedJSON(None, "empty reply")
    found, last_err, cut = _top_level_objects(text)
    if cut:
        return ParsedJSON(None, CUTOFF_ERROR)
    if not found:
        return ParsedJSON(None, last_err)
    keys = set(expect_keys or ())
    candidates = ([(i, o) for i, o in found if keys & set(o)] if keys else []) or found
    canon = [(i, _canon(o), o) for i, o in candidates]
    if len({c for _, c, _ in canon}) == 1:
        return ParsedJSON(candidates[0][1], None)
    spans = [(m.start(1), m.end(1)) for m in _JSON_FENCE_RE.finditer(text)]
    fenced = {c: i for i, c, _ in canon if any(a <= i < b for a, b in spans)}  # canon -> last fenced position
    if len(fenced) == 1:
        (fc, fi), = fenced.items()
        # A differing object after the fenced one may be its revision: the fence decides only when last (L19).
        if not any(i > fi and c != fc for i, c, _ in canon):
            return ParsedJSON(next(o for _, c, o in canon if c == fc), None, True)
    return ParsedJSON(None, AMBIGUOUS_ERROR, True)


def parse_json(text: Optional[str], expect_keys: Iterable[str] = ()) -> Tuple[Optional[dict], Optional[str]]:
    """(object, None) or (None, error); rules in parse_json_detail (K1)."""
    obj, err, _ = parse_json_detail(text, expect_keys)
    return obj, err


def _top_level_objects(text: str) -> Tuple[List[Tuple[int, dict]], str, bool]:
    """
    (start index, object) for each top-level JSON object in text, the last
    decode error, and whether the text ends inside an object opened with '{"'
    (a reply cut off before its final object closed).
    """
    try:
        whole = json.loads(text.strip())
    except (ValueError, RecursionError):
        whole = None
    if whole is not None and not isinstance(whole, dict):
        last_err = f"expected a JSON object, got {type(whole).__name__}"
    else:
        last_err = "no JSON object found"
    found: List[Tuple[int, dict]] = []
    i = text.find("{")
    while i != -1:
        end = _brace_end(text, i)
        if end == -1:
            last_err = "invalid JSON: unterminated object"
            if text[i + 1:].lstrip()[:1] == '"':
                # A JSON object never closed (reply cut off): every later "{" is
                # nested inside it, and a nested fragment must not pass for the reply;
                # nor may an earlier complete draft (L18).
                return found, last_err, True
            i = text.find("{", i + 1)  # a stray brace in prose
            continue
        try:
            obj = json.loads(text[i:end + 1])
        except (ValueError, RecursionError) as e:
            last_err = f"invalid JSON: {e}"
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            found.append((i, obj))
        i = text.find("{", end + 1)
    return found, last_err, False


CONTEXT_ERROR = "prompt exceeds the model's context window"


def complete_json(model: str, system: str, user: str, *,
                  validate: Optional[Callable[[dict], Optional[str]]] = None, retries: int = 2,
                  expect_keys: Iterable[str] = (), **kw: Any) -> Tuple[Optional[dict], List[Dict[str, Any]]]:
    """
    complete() + parse_json() + validate(). An unusable reply is re-asked with a
    corrective turn up to `retries` times; a reply cut off at max_tokens is
    instead re-asked once, unchanged, with max_tokens doubled (capped at
    MAX_TOKENS_CAP); corrective turns after that use the base max_tokens again
    (L12). A refusal or safety stop ends the call at once, and so does a prompt
    that exceeds the context window (stop "context", error CONTEXT_ERROR, L21). `expect_keys` are the
    reply schema's keys, passed to parse_json_detail: a reply with two differing
    schema objects and no single ```json-fenced one is re-asked ("reply with
    exactly one"), and its attempt carries "ambiguous": True (K1). Returns (obj or
    None, attempts) with one record per attempt, interrupted requests included
    (stop "timeout" or "stream_error", "possibly_billed" set). Parse/validation
    failures never raise; BudgetExceeded and FatalAPIError do, and the error's
    .attempts then holds the records so far (L16).
    """
    attempts: List[Dict[str, Any]] = []
    history: List[Dict[str, str]] = list(kw.pop("history", None) or [])
    base_tokens = int(kw.pop("max_tokens", 4000))
    max_tokens = base_tokens
    expect = tuple(expect_keys or ())
    grown = False
    corrections = 0
    prompt = user
    while True:
        try:
            r = complete(model, system, prompt, history=history, max_tokens=max_tokens, **kw)
        except (FatalAPIError, BudgetExceeded) as e:
            # Keep every attempt already made (paid replies included) on the error (L16).
            e.attempts = attempts + list(getattr(e, "attempts", None) or [])
            raise
        attempts.extend(r.timeouts)
        obj, err, ambiguous = parse_json_detail(r.text, expect)
        if obj is not None and validate is not None:
            try:
                err = validate(obj)
            except Exception as e:  # a buggy validator must not crash the turn
                err = f"validation error: {e}"
        if err is not None and r.stop in ("refusal", "safety"):
            err = f"refusal:{r.stop_detail or r.stop}"
        elif err is not None and r.stop == "context":
            err = CONTEXT_ERROR
        elif err is not None and r.stop == "max_tokens":
            err = f"max_tokens: reply cut off at {r.max_tokens or max_tokens} tokens ({err})"
        attempts.append({
            "text": r.text, "thinking": r.thinking, "error": err,
            "stop": r.stop, "stop_detail": r.stop_detail, "served_model": r.served_model,
            "max_tokens": r.max_tokens or max_tokens,
            "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
            "cached_tokens": r.cached_tokens, "reasoning_tokens": r.reasoning_tokens,
            "cost": r.cost, "latency_s": r.latency_s, "possibly_billed": False, "ambiguous": ambiguous,
        })
        if ambiguous:
            logger.info(f"{model}: reply held conflicting JSON objects "
                        f"({'resolved by the ```json fence' if err is None else 'unusable'})")
        if err is None:
            return obj, attempts
        logger.info(f"{model}: unusable reply ({err}); attempt {len(attempts)}")
        if r.stop in ("refusal", "safety"):
            return None, attempts  # re-asking a refusal only invites another
        if r.stop == "context":
            return None, attempts  # a corrective turn would only make the prompt longer (L21)
        if r.stop == "max_tokens":
            bigger = min(MAX_TOKENS_CAP, max_tokens * 2)
            if grown or bigger <= max_tokens:
                return None, attempts  # a corrective turn cannot fix a truncation
            grown, max_tokens = True, bigger
            continue  # same prompt, more room
        max_tokens = base_tokens  # the doubled cap is for the one re-ask only (L12)
        if corrections >= retries:
            return None, attempts
        corrections += 1
        history += [{"role": "user", "content": prompt}, {"role": "assistant", "content": r.text or "(empty)"}]
        prompt = f"Your previous reply could not be used: {err}. Reply again with only the JSON object."
