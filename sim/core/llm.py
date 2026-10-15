#!/usr/bin/env python3
"""
Multi-provider LLM client (spec §10 models, §11 budget guard).

Providers, chosen by an explicit `provider` argument or the model-name prefix:
  anthropic  claude-*        ANTHROPIC_API_KEY
  openai     gpt-*, o*       OPENAI_API_KEY             (Responses API)
  google     gemini-*        VERTEX_API_KEY             (Vertex AI Express)
  xai        grok-*          XAI_API_KEY                (OpenAI-compatible)
  muse       muse-*          MUSE_API_KEY, MUSE_BASE_URL (OpenAI-compatible)
  moonshot   kimi-*          MOONSHOT_API_KEY           (OpenAI-compatible; Grand Jury, MacroJury,
                                                         mandate screen)
  deepseek   deepseek-*      DEEPSEEK_API_KEY           (OpenAI-compatible; Grand Jury fallback)
  dashscope  qwen-*          DASHSCOPE_API_KEY          (OpenAI-compatible; supported, no juror uses it)
  stub       stub:<name>     a registered python callable (offline tests, dry runs)

The juror providers (moonshot, deepseek; dashscope kept but unused; spec §7) use their
default base URLs (MOONSHOT_BASE_URL, DEEPSEEK_BASE_URL, DASHSCOPE_BASE_URL
override them). One optional router overrides all three: with JUROR_BASE_URL
set, every juror call goes there with JUROR_API_KEY, and JUROR_MODEL_IDS (an
optional JSON object, e.g. {"kimi-k3": "moonshotai/kimi-k3"}) maps our model
ids to the router's. A juror provider that rejects `temperature` (a 400 naming
it) is re-asked once without it and remembered; the temperature actually sent
is reported on the response (LLMResponse.temperature, None when not sent) and
on every complete_json attempt record, next to the served model id.

Keys are read from sim/.env. Clients are created lazily on first use, so
importing never fails without keys; preflight() lists missing keys before a
run starts and a missing key at call time raises MissingKeyError.

Failure handling: transient API errors (rate limits, overload, 5xx, a
stream or read failure after the request reached the provider) are retried
with exponential backoff; non-transient API errors (and transient ones that
outlast every retry) raise FatalAPIError, which the engine turns into a saved
partial record and RunAborted.

Connection failures (no network, DNS failure, connection refused or reset,
connect timeout, the SDKs' APIConnectionError, httpx ConnectError /
ConnectTimeout / RemoteProtocolError, socket.gaierror, OSError ENETUNREACH ...;
never an HTTP status, so a provider's 5xx or 429 stays an ordinary transient
error) are retried apart, with backoff capped at NETWORK_RETRY_CAP_S (60 s),
for up to network_wait_s() seconds (default 30 min; SIM_NETWORK_WAIT_S, else
config/pilot.json "network_wait_s"), each wait logged ("[network] connection
lost ... retrying in 30s (12 min left)"). When the window runs out they raise
NetworkUnavailable: a BaseException (like KeyboardInterrupt), never a
FatalAPIError, so no fallback, fail-closed, forfeit or "values stay" handler
catches it; every caller stops, saves what is paid for and exits
EXIT_NETWORK (5), and re-running the same command resumes. The offline stub
gets the same connection handling (a stub may raise a connection error).

Exact resume: complete() first looks the request up in the run's replay cache
(core.calllog: the successful calls journalled since the run's last
checkpoint). A journalled reply to the same request (same run, purpose, model,
system prompt, history and user turn, max_tokens, effort, temperature) is
returned again, identical requests consumed in journal order, with no provider
call and nothing charged ("[resume] replayed call ..."). Only a request with
no journalled match goes live. A request timeout is
retried at most twice: each timed-out request is logged (cost 0, flagged
"possibly billed", since the provider may have finished it) and reported to
complete_json as an attempt with stop "timeout".

Anthropic calls stream (messages.stream(...).get_final_message()): a long
thinking reply then never hits a whole-request timeout or the SDK's
non-streaming max_tokens limit; the read timeout applies between events.
Other providers get a 1200 s request timeout. A stream that breaks after its
200 response started (disconnect, read error, SSE error event, empty body, or
no message_stop — "incomplete stream") is handled like a timeout: logged once
at cost 0 with possibly_billed, recorded as an attempt with stop
"stream_error", and re-run at most twice in all together with timeouts.

Reply parsing: a reply holding two differing objects with schema keys is
unusable ("reply with exactly one") unless exactly one sits in a ```json fence
and no differing object follows it; objects are compared as canonical
JSON text, so 1 and true differ. An object left open at the end of the
reply ("reply cut off before the final JSON object closed") makes the reply
unusable whatever complete objects precede it.

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
treats it as unusable without a corrective re-ask: the next prompt
would be longer still.
"""

import errno
import inspect
import json
import logging
import os
from pathlib import Path
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple

from dotenv import load_dotenv

from core import calllog
from core.config import SIM_DIR
from core.costs import BudgetExceeded, cost_of, get_tracker, price_for

def _load_env_files() -> List[Path]:
    """Keys from sim/.env and from every .env above it up to (not including) the home
    directory, e.g. ~/Projects/sfpi/.env beside the repo. The nearest file wins; a
    variable already set in the environment is never overridden. KIMI_API_KEY is
    accepted for MOONSHOT_API_KEY (Kimi is Moonshot's model)."""
    home = Path.home().resolve()
    loaded = []
    for d in [SIM_DIR, *SIM_DIR.resolve().parents]:
        if d == home or d == d.parent:
            break
        if (d / ".env").is_file():
            load_dotenv(d / ".env", override=False)
            loaded.append(d / ".env")
    if not os.getenv("MOONSHOT_API_KEY") and os.getenv("KIMI_API_KEY"):
        os.environ["MOONSHOT_API_KEY"] = os.environ["KIMI_API_KEY"]
    return loaded


ENV_FILES = _load_env_files()

logger = logging.getLogger(__name__)

PROVIDERS = ("anthropic", "openai", "google", "xai", "muse", "moonshot", "deepseek", "dashscope",
             "stub")

KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "VERTEX_API_KEY",
    "xai": "XAI_API_KEY",
    "muse": "MUSE_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
}
# Extra settings a provider needs besides its key.
EXTRA_ENV = {"muse": ["MUSE_BASE_URL"]}
SDK_MODULE = {"anthropic": "anthropic", "openai": "openai", "xai": "openai", "muse": "openai",
              "moonshot": "openai", "deepseek": "openai", "dashscope": "openai",
              "google": "google.genai"}
XAI_BASE_URL = "https://api.x.ai/v1"

# Juror providers (spec §7): OpenAI-compatible endpoints, default base URLs,
# each overridable by <PROVIDER>_BASE_URL; one router (JUROR_BASE_URL +
# JUROR_API_KEY) overrides all three.
JUROR_PROVIDERS = ("moonshot", "deepseek", "dashscope")
JUROR_BASE_URLS = {
    "moonshot": "https://api.moonshot.ai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "dashscope": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
}
BASE_URL_ENV = {"moonshot": "MOONSHOT_BASE_URL", "deepseek": "DEEPSEEK_BASE_URL",
                "dashscope": "DASHSCOPE_BASE_URL"}
ROUTER_URL_ENV, ROUTER_KEY_ENV, ROUTER_IDS_ENV = "JUROR_BASE_URL", "JUROR_API_KEY", "JUROR_MODEL_IDS"
# (provider, model) pairs whose API rejected `temperature`: sent without it from then on.
_TEMPERATURE_REJECTED: set = set()

# Anthropic: current Claude models reject disabled thinking and sampling
# params; adaptive thinking with a summarised display gives us readable
# reasoning for the logs. Depth is controlled with output_config.effort.
ANTHROPIC_THINKING: Optional[Dict[str, Any]] = {"type": "adaptive", "display": "summarized"}

# Ceiling for the one max_tokens-doubling retry, every provider (Anthropic calls
# stream, so the SDK's ~21.3k non-streaming limit does not apply).
MAX_TOKENS_CAP = 32000

# OpenAI reasoning summaries need a verified organisation; a 400 about them
# switches them off for the rest of the process.
OPENAI_REASONING_SUMMARY = True

# OpenAI-compatible providers sent reasoning_effort (the seat's `effort`). xAI
# and Muse document it (low…xhigh); a 400/422 that names it is re-sent once
# without it and that model is then always called without it (logged; the
# record's effort says what was sent). The jurors run at their providers'
# defaults (Kimi K3: max).
COMPAT_EFFORT_SUPPORTED: Dict[str, bool] = {"xai": True, "muse": True, "moonshot": False,
                                            "deepseek": False, "dashscope": False}
_EFFORT_REJECTED: set = set()

# Gemini thinking levels; the levels above "high" collapse onto HIGH.
GEMINI_THINKING_LEVEL = {"minimal": "MINIMAL", "low": "LOW", "medium": "MEDIUM",
                         "high": "HIGH", "xhigh": "HIGH", "max": "HIGH"}

# Timeouts. Long thinking replies take many minutes; a dead connection must
# not hang a run. Non-streaming providers: the whole request. Anthropic streams:
# connect, then the longest silence allowed between two stream events.
REQUEST_TIMEOUT_S = 1200.0
ANTHROPIC_CONNECT_TIMEOUT_S = 30.0
ANTHROPIC_READ_TIMEOUT_S = 600.0
# A timed-out request may still have run (and been billed): retry it at most twice.
MAX_TIMEOUT_RETRIES = 2

# Retries: 8 attempts, exponential backoff from 2s capped at 120s, with jitter.
# Connection failures are retried apart (NETWORK_* below) and never use these up.
MAX_ATTEMPTS = 8
RETRY_BASE_S = 2.0
RETRY_CAP_S = 120.0
RETRY_AFTER_CAP_S = 300.0
TRANSIENT_STATUS = {408, 409, 425, 429}

# Connection failures: retried for up to network_wait_s() seconds in all,
# backoff from 2 s doubling, capped at 60 s between tries.
NETWORK_WAIT_S = 1800.0
NETWORK_RETRY_BASE_S = 2.0
NETWORK_RETRY_CAP_S = 60.0
NETWORK_WAIT_ENV = "SIM_NETWORK_WAIT_S"


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


class NetworkUnavailable(BaseException):
    """
    No connection to a provider for the whole network wait window
    (network_wait_s()). A BaseException, like KeyboardInterrupt, so that no
    `except Exception` fallback, fail-closed screen, forfeit, "values stay" or
    "no usable verdict" handler can catch it: the run stops, saves its partial
    record and logs, and re-running the same command resumes (exit 5,
    core.exits.EXIT_NETWORK). `attempts` holds complete_json's attempt records
    made before it (paid), as on FatalAPIError.
    """

    def __init__(self, message: str, provider: str = "", model: str = "", waited_s: float = 0.0):
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.waited_s = waited_s
        self.attempts: List[Dict[str, Any]] = []


NETWORK_STOP_HINT = "network unavailable: re-run the same command to resume"


def network_wait_s() -> float:
    """Seconds a connection failure is retried before NetworkUnavailable: the
    SIM_NETWORK_WAIT_S environment variable, else config/pilot.json
    "network_wait_s", else NETWORK_WAIT_S (30 min). 0 stops at the first failure."""
    env = os.environ.get(NETWORK_WAIT_ENV)
    if env not in (None, ""):
        try:
            return max(0.0, float(env))
        except ValueError:
            logger.warning(f"[network] ignoring {NETWORK_WAIT_ENV}={env!r} (not a number)")
    try:
        with open(SIM_DIR / "config" / "pilot.json", encoding="utf-8") as f:
            value = json.load(f).get("network_wait_s")
        if value is not None:
            return max(0.0, float(value))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return NETWORK_WAIT_S


class MissingKeyError(FatalAPIError):
    """A provider's API key (or base URL) is not configured."""


class ContextWindowExceeded(RuntimeError):
    """A request rejected because the prompt does not fit the model's context
    window. Not fatal: complete() turns it into a reply with stop "context"."""


class StreamInterrupted(RuntimeError):
    """
    An Anthropic stream that failed after its 200 response started:
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
    temperature: Optional[float] = None # temperature actually sent (None: not sent or ignored)
    max_tokens: int = 0
    effort: Optional[str] = None        # reasoning effort actually requested (None: not sent)
    # Attempt records of requests interrupted before this one succeeded: timeouts
    # (stop "timeout") and broken streams (stop "stream_error").
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
    if m.startswith("kimi") or m.startswith("moonshot"):
        return "moonshot"
    if m.startswith("deepseek"):
        return "deepseek"
    if m.startswith("qwen"):
        return "dashscope"
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
        for env in required_env(provider):
            if not os.getenv(env):
                problems.append(f"{model} ({provider}): {env} is not set (add it to sim/.env or a .env above the repo)")
        try:
            __import__(SDK_MODULE[provider])
        except ImportError:
            problems.append(f"{model} ({provider}): python package for {SDK_MODULE[provider]} is not installed")
    return problems


def router_active() -> bool:
    """True when JUROR_BASE_URL routes every juror provider through one endpoint."""
    return bool(os.getenv(ROUTER_URL_ENV))


def required_env(provider: str) -> List[str]:
    """The settings a provider needs: its key (and base URL), or, for a juror
    provider under the router, JUROR_API_KEY."""
    if provider in JUROR_PROVIDERS and router_active():
        return [ROUTER_KEY_ENV]
    return [KEY_ENV[provider]] + EXTRA_ENV.get(provider, [])


def juror_endpoint(provider: str) -> Tuple[str, str]:
    """(base URL, env var holding the key) for a juror provider."""
    if router_active():
        return os.getenv(ROUTER_URL_ENV, ""), ROUTER_KEY_ENV
    return os.getenv(BASE_URL_ENV[provider]) or JUROR_BASE_URLS[provider], KEY_ENV[provider]


# The id a provider's API serves a model under, when it differs from the name the
# spec and config use (Vertex serves Gemini 3.1 Pro as a preview id; verified
# with a live call, 15 Oct 2026). The record keeps the config name; served_model
# logs what answered.
PROVIDER_WIRE_IDS: Dict[str, Dict[str, str]] = {
    "google": {"gemini-3.1-pro": "gemini-3.1-pro-preview"},
}


def wire_model_id(provider: str, model: str) -> str:
    """The id sent to the API: a provider's serving id (PROVIDER_WIRE_IDS), or under
    the juror router, JUROR_MODEL_IDS may map it."""
    if provider in PROVIDER_WIRE_IDS:
        return PROVIDER_WIRE_IDS[provider].get(model, model)
    if provider not in JUROR_PROVIDERS or not router_active():
        return model
    raw = os.getenv(ROUTER_IDS_ENV)
    if not raw:
        return model
    try:
        ids = json.loads(raw)
    except ValueError:
        raise MissingKeyError(f"{ROUTER_IDS_ENV} is not a JSON object") from None
    return str(ids.get(model, model)) if isinstance(ids, dict) else model


def _require(env: str) -> str:
    value = os.getenv(env)
    if not value:
        raise MissingKeyError(f"{env} is not set (add it to sim/.env or a .env above the repo)")
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
        elif provider in JUROR_PROVIDERS:
            import openai
            base_url, key_env = juror_endpoint(provider)
            client = openai.OpenAI(api_key=_require(key_env), base_url=base_url, max_retries=0,
                                   timeout=REQUEST_TIMEOUT_S)
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
    # Streamed. The last message_delta event is kept because anthropic 0.97's
    # accumulator drops its stop_details and output_tokens_details; 1.x copies
    # them into the final message, which wins when present.
    last_delta: Any = None
    with client.messages.stream(**kwargs) as stream:
        # Entering sent the request and checked the HTTP status. Whatever fails
        # from here on failed after a 200 started: the request may have run and
        # been billed, so it is a StreamInterrupted, not a plain retry.
        try:
            for event in stream:
                if _get(event, "type") == "message_delta":
                    last_delta = event
            resp = stream.get_final_message()
        except Exception as e:
            if _is_timeout(e):
                raise  # a read timeout between events: the timeout path
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
    """Chat Completions call for OpenAI-compatible providers (xAI, Muse, and the
    juror providers Moonshot, DeepSeek, DashScope). A 400 that names
    `temperature` is re-sent once without it, and that model is then always
    called without it (the reply reports the temperature actually sent)."""
    def call(client: Any, model: str, system: str, turns: List[Dict[str, str]],
             max_tokens: int, temperature: Optional[float], effort: Optional[str],
             cache_system: bool, cache_key: Optional[str]) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "model": wire_model_id(provider, model),
            "messages": [{"role": "system", "content": system}] + turns,
            "max_tokens": max_tokens,
        }
        if temperature is not None and (provider, model) not in _TEMPERATURE_REJECTED:
            kwargs["temperature"] = temperature
        if effort and COMPAT_EFFORT_SUPPORTED.get(provider) and (provider, model) not in _EFFORT_REJECTED:
            kwargs["reasoning_effort"] = effort
        resp = None
        for _ in range(2):              # each rejected parameter is dropped at most once
            try:
                resp = client.chat.completions.create(**kwargs)
                break
            except Exception as e:
                if "temperature" in kwargs and _temperature_rejected(e):
                    logger.info(f"{provider}/{model}: temperature rejected ({e}); sent without it from now on")
                    _TEMPERATURE_REJECTED.add((provider, model))
                    kwargs.pop("temperature")
                elif "reasoning_effort" in kwargs and _reasoning_effort_rejected(e):
                    logger.warning(f"{provider}/{model}: reasoning_effort {kwargs['reasoning_effort']!r} "
                                   f"rejected ({e}); sent without it from now on (provider default)")
                    _EFFORT_REJECTED.add((provider, model))
                    kwargs.pop("reasoning_effort")
                else:
                    raise
        if resp is None:
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
            "temperature": kwargs.get("temperature"),
            "effort": kwargs.get("reasoning_effort"),
        }
    return call


def _reasoning_effort_rejected(err: Exception) -> bool:
    """A 400/422 whose message names reasoning_effort (the model takes no effort setting)."""
    msg = str(err).lower()
    return _status(err) in (400, 422) and ("reasoning_effort" in msg or "reasoning effort" in msg)


def _temperature_rejected(err: Exception) -> bool:
    """A 400/422 whose message names temperature (a sampling parameter the model fixes)."""
    return _status(err) in (400, 422) and "temperature" in str(err).lower()


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
    resp = client.models.generate_content(model=wire_model_id("google", model), contents=contents,
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
    "moonshot": _call_compat("moonshot"),
    "deepseek": _call_compat("deepseek"),
    "dashscope": _call_compat("dashscope"),
}


# ---------------------------------------------------------------------------
# Retries and error classification
# ---------------------------------------------------------------------------

_sleep = time.sleep  # patched in tests
_clock = time.monotonic  # patched in tests (the network wait window)

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
    """A 400/413 saying the prompt does not fit the model's context window."""
    if _status(err) not in (400, 413):
        return False
    text = f"{getattr(err, 'code', '') or ''} {err} {getattr(err, 'body', '') or ''}"
    return bool(_CONTEXT_ERROR_RE.search(text))


# Connection-level failures: the request never got an HTTP response.
CONNECTION_CLASSES = ("APIConnectionError", "ConnectError", "ConnectTimeout", "RemoteProtocolError",
                      "WriteError", "ConnectionError", "gaierror", "herror")
NETWORK_ERRNOS = {getattr(errno, n) for n in (
    "ENETUNREACH", "EHOSTUNREACH", "ENETDOWN", "EHOSTDOWN", "ECONNREFUSED", "ECONNRESET",
    "ECONNABORTED", "ENOTCONN", "EPIPE", "ETIMEDOUT") if hasattr(errno, n)}


def _is_connection_error(err: BaseException) -> bool:
    """No network, DNS failure, connection refused or reset, connect timeout: the SDKs'
    APIConnectionError (not a read timeout), httpx ConnectError / ConnectTimeout /
    RemoteProtocolError / WriteError, socket.gaierror, OSError ENETUNREACH and the
    like. Never an error with an HTTP status (a provider's 5xx or 429), and never a
    stream that broke after its 200 response started (StreamInterrupted)."""
    if isinstance(err, StreamInterrupted) or not isinstance(err, Exception):
        return False
    if _status(err) is not None:
        return False
    for e in _error_chain(err):
        names = [c.__name__ for c in type(e).__mro__]
        if "APITimeoutError" in names or "TimeoutException" in names:
            if "ConnectTimeout" in names:
                return True
            continue        # a read/write timeout: the request may have run (the timeout path)
        if any(n in CONNECTION_CLASSES for n in names):
            return True
        if isinstance(e, OSError) and e.errno in NETWORK_ERRNOS:
            return True
    return False


def _minutes(seconds: float) -> str:
    seconds = max(0.0, seconds)
    return f"{seconds:.0f}s" if seconds < 90 else f"{seconds / 60:.0f} min"


class _Outage:
    """One call's run of connection failures: wait() sleeps before the next try, or
    raises NetworkUnavailable once the network wait window is used up; ok() notes
    the connection is back."""

    def __init__(self, provider: str, model: str):
        self.provider, self.model = provider, model
        self.label = f"{provider}/{model}"
        self.since: Optional[float] = None
        self.tries = 0

    def wait(self, err: BaseException) -> None:
        now = _clock()
        if self.since is None:
            self.since = now
        window = network_wait_s()
        waited = now - self.since
        left = window - waited
        what = f"{type(err).__name__}: {err}"
        if left <= 0:
            logger.error(f"[network] {self.label}: no connection for {_minutes(waited)} "
                         f"(window {_minutes(window)}; {what}); stopping: {NETWORK_STOP_HINT}")
            raise NetworkUnavailable(f"no connection to {self.label} for {_minutes(waited)} "
                                     f"(network wait {_minutes(window)}): {what}",
                                     self.provider, self.model, waited) from err
        delay = min(NETWORK_RETRY_CAP_S, NETWORK_RETRY_BASE_S * 2 ** min(self.tries, 10), left)
        self.tries += 1
        logger.warning(f"[network] connection lost ({self.label}: {what}); retrying in "
                       f"{_minutes(delay)} ({_minutes(left)} left)")
        _sleep(delay)

    def ok(self) -> None:
        if self.since is not None:
            logger.warning(f"[network] connection back ({self.label}) after "
                           f"{_minutes(_clock() - self.since)}")
            self.since, self.tries = None, 0


def _network_retry(fn: Callable[[], Any], provider: str, model: str) -> Any:
    """fn, with connection failures retried within the network wait window (the
    offline stub's path: everything else it raises propagates as it is)."""
    outage = _Outage(provider, model)
    while True:
        try:
            out = fn()
        except Exception as e:
            if not _is_connection_error(e):
                raise
            outage.wait(e)
            continue
        outage.ok()
        return out


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
    MAX_TIMEOUT_RETRIES times, and the next one is fatal. A
    non-transient stream error (e.g. an invalid_request error event) is fatal
    at once, after being reported. A connection failure (_is_connection_error)
    uses up no attempt: it is retried within the network wait window
    (_Outage), then raises NetworkUnavailable.
    """
    label = f"{provider}/{model}"
    timeouts = broken = 0
    outage = _Outage(provider, model)
    attempt = 0
    while True:
        start = time.monotonic()
        try:
            out = fn()
            outage.ok()
            return out
        except (BudgetExceeded, FatalAPIError):
            raise
        except Exception as e:
            if _is_connection_error(e):
                outage.wait(e)      # raises NetworkUnavailable once the window is used up
                continue
            outage.ok()
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
                raise ContextWindowExceeded(f"{label}: {e}") from e  # not retried, not fatal
            elif not _is_transient(e):
                raise FatalAPIError(f"{label}: {type(e).__name__}: {e}", provider, model, status) from e
            if attempt == MAX_ATTEMPTS - 1:
                raise FatalAPIError(f"{label}: gave up after {MAX_ATTEMPTS} attempts: {type(e).__name__}: {e}",
                                    provider, model, status) from e
            delay = _backoff(attempt, e)
            logger.warning(f"{label}: transient error ({type(e).__name__}: {e}); "
                           f"retry {attempt + 2}/{MAX_ATTEMPTS} in {delay:.1f}s")
            _sleep(delay)
            attempt += 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _call_stub(model: str, system: str, user: str, turns: List[Dict[str, str]], purpose: str,
               run_id: Optional[str], max_tokens: int, start: float,
               temperature: Optional[float] = None) -> LLMResponse:
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
                    thinking=out.get("thinking"), served_model=out.get("served_model", model),
                    max_tokens=max_tokens, temperature=temperature)
    get_tracker().record(model, purpose, run_id, r.input_tokens, r.output_tokens, cost=0.0,
                         provider="stub", stop=r.stop)
    return r


def complete(model: str, system: str, user: str, *, provider: Optional[str] = None,
             max_tokens: int = 4000, temperature: Optional[float] = None,
             effort: Optional[str] = None, cache_system: bool = True, purpose: str = "actor",
             run_id: Optional[str] = None, history: Optional[List[Dict[str, str]]] = None,
             cache_key: Optional[str] = None) -> LLMResponse:
    """_complete, with the call (prompt, raw reply or error) appended to the call
    journal (core.calllog) the moment it returns or fails. A request the run's
    replay cache holds (a resumed run: the same request journalled successfully
    since the run's last checkpoint) returns the journalled reply instead, with no
    provider call and nothing charged."""
    key = calllog.request_key(purpose=purpose, model=model, system=system, history=history, user=user,
                              max_tokens=max_tokens, effort=effort, temperature=temperature)
    hit = calllog.take_replay(run_id, key)
    if hit is not None:
        return _replayed(hit, model, purpose, run_id, key)
    start = time.monotonic()
    try:
        r = _complete(model, system, user, provider=provider, max_tokens=max_tokens,
                      temperature=temperature, effort=effort, cache_system=cache_system,
                      purpose=purpose, run_id=run_id, history=history, cache_key=cache_key)
    except BaseException as e:
        calllog.record(run_id=run_id, purpose=purpose, provider=_safe_provider(model, provider), model=model,
                       system=system, user=user, history=history, error=e,
                       latency_s=time.monotonic() - start, request_key=key)
        raise
    calllog.record(run_id=run_id, purpose=purpose, provider=r.provider, model=model, system=system,
                   user=user, history=history, response=r, request_key=key)
    return r


def _replayed(hit: Dict[str, Any], model: str, purpose: str, run_id: Optional[str], key: str) -> LLMResponse:
    """The journalled reply of a replayed call, as complete() first returned it. Nothing
    is charged (the ledger has it from the call itself)."""
    def num(k: str, cast=int):
        v = hit.get(k)
        return cast(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else cast(0)
    r = LLMResponse(
        text=hit.get("text") or "", model=model, provider=hit.get("provider") or "",
        input_tokens=num("input_tokens"), output_tokens=num("output_tokens"),
        cached_tokens=num("cached_tokens"), reasoning_tokens=num("reasoning_tokens"),
        cost=num("cost", float), thinking=hit.get("thinking"), latency_s=num("latency_s", float),
        raw_stop_reason=hit.get("raw_stop_reason"), stop=hit.get("stop") or "end",
        stop_detail=hit.get("stop_detail"), served_model=hit.get("served_model"),
        temperature=hit.get("temperature"), max_tokens=num("max_tokens"), effort=hit.get("effort"),
        timeouts=[dict(t) for t in hit.get("timeouts") or []])
    calllog.note_replayed(run_id, hit, key)
    logger.info(f"[resume] replayed call {purpose} {model} (journalled {hit.get('ts')}; "
                f"stop {r.stop}, ${r.cost:.4f} already on the ledger; no new call)")
    return r


def _safe_provider(model: str, provider: Optional[str]) -> str:
    try:
        return resolve_provider(model, provider)
    except Exception:       # noqa: BLE001
        return provider or ""


def _complete(model: str, system: str, user: str, *, provider: Optional[str] = None,
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
        # A stub may raise a connection error (tests, the e2e outage demo): handled like a provider's.
        return _network_retry(lambda: _call_stub(model, system, user, turns, purpose, run_id, max_tokens,
                                                 start, temperature), provider, model)

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
        # Unmeasured but possibly billed: logged once at cost 0 with a flag.
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
    except (FatalAPIError, BudgetExceeded, NetworkUnavailable) as e:
        e.attempts = timed_out + list(getattr(e, "attempts", None) or [])
        raise
    except ContextWindowExceeded as e:
        # The request was refused before any generation: an empty reply, nothing billed.
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
                    served_model=out["served_model"], max_tokens=out["max_tokens"], timeouts=timed_out,
                    effort=out.get("effort", effort),   # compat providers: what was actually sent
                    # Anthropic ignores sampling params with thinking on; others report what was sent.
                    temperature=None if provider == "anthropic" else out.get("temperature", temperature))
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


# Objects inside a ```json fence: the one a model marks as its answer.
_JSON_FENCE_RE = re.compile(r"```[ \t]*json[^\n]*\n(.*?)```", re.S | re.I)
AMBIGUOUS_ERROR = "reply contained more than one JSON object; reply with exactly one"
CUTOFF_ERROR = "reply cut off before the final JSON object closed"


class ParsedJSON(NamedTuple):
    obj: Optional[dict]
    error: Optional[str]
    # The reply held two or more differing candidate objects; obj is set
    # only when exactly one of them was inside a ```json fence and no
    # differing candidate followed it.
    ambiguous: bool = False


def _canon(obj: Any) -> str:
    """Canonical JSON text: key order ignored, but 1, 1.0 and true stay distinct."""
    return json.dumps(obj, sort_keys=True)


def parse_json_detail(text: Optional[str], expect_keys: Iterable[str] = ()) -> ParsedJSON:
    """
    Tolerant JSON-object extraction; never raises. Decodes every top-level
    object (inside ```json fences or prose alike; objects nested in one already
    decoded belong to it). A top-level object that opens with '{"' and never
    closes means the reply was cut off mid-answer: CUTOFF_ERROR, even when
    complete objects (a draft) precede it. Candidates are the objects
    carrying any of `expect_keys` (the reply schema's keys), or all objects
    when none does; copies with the same canonical JSON text count once.
    One candidate: it is the reply, whatever prose, fence or trailing text
    surrounds it. Two or more differing candidates (a draft and a final, a
    rejected alternative quoted in prose): if exactly one is inside a ```json
    fence and no differing candidate follows it, it is the reply;
    otherwise the reply is unusable with AMBIGUOUS_ERROR. Never picks between
    conflicting objects by position alone.
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
        # A differing object after the fenced one may be its revision: the fence decides only when last.
        if not any(i > fi and c != fc for i, c, _ in canon):
            return ParsedJSON(next(o for _, c, o in canon if c == fc), None, True)
    return ParsedJSON(None, AMBIGUOUS_ERROR, True)


def parse_json(text: Optional[str], expect_keys: Iterable[str] = ()) -> Tuple[Optional[dict], Optional[str]]:
    """(object, None) or (None, error); rules in parse_json_detail."""
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
                # nor may an earlier complete draft.
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
    MAX_TOKENS_CAP); corrective turns after that use the base max_tokens again.
    A refusal or safety stop ends the call at once, and so does a prompt
    that exceeds the context window (stop "context", error CONTEXT_ERROR). `expect_keys` are the
    reply schema's keys, passed to parse_json_detail: a reply with two differing
    schema objects and no single ```json-fenced one is re-asked ("reply with
    exactly one"), and its attempt carries "ambiguous": True. Returns (obj or
    None, attempts) with one record per attempt, interrupted requests included
    (stop "timeout" or "stream_error", "possibly_billed" set). Parse/validation
    failures never raise; BudgetExceeded, FatalAPIError and NetworkUnavailable do,
    and the error's .attempts then holds the records so far.
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
        except (FatalAPIError, BudgetExceeded, NetworkUnavailable) as e:
            # Keep every attempt already made (paid replies included) on the error.
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
            "temperature": r.temperature, "max_tokens": r.max_tokens or max_tokens,
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
            return None, attempts  # a corrective turn would only make the prompt longer
        if r.stop == "max_tokens":
            bigger = min(MAX_TOKENS_CAP, max_tokens * 2)
            if grown or bigger <= max_tokens:
                return None, attempts  # a corrective turn cannot fix a truncation
            grown, max_tokens = True, bigger
            continue  # same prompt, more room
        max_tokens = base_tokens  # the doubled cap is for the one re-ask only
        if corrections >= retries:
            return None, attempts
        corrections += 1
        history += [{"role": "user", "content": prompt}, {"role": "assistant", "content": r.text or "(empty)"}]
        prompt = f"Your previous reply could not be used: {err}. Reply again with only the JSON object."


# ---------------------------------------------------------------------------
# Interview continuation (spec A.7; CODE_V5_CONTRACT "Interview")
# ---------------------------------------------------------------------------

# Each provider's lowest reasoning setting (A.7 "at the provider's lowest
# effort"); None sends no effort setting (providers that reject the parameter,
# and the offline stub). Unknown providers get None. Anthropic's lowest
# output_config.effort is "low". OpenAI reasoning models and Gemini thinking
# levels offer "minimal" on some models: it is tried first, and a 400 that
# rejects it falls back to LOWEST_EFFORT_FALLBACK for that model for the rest of
# the process. The effort actually sent is LLMResponse.effort.
LOWEST_EFFORT: Dict[str, Optional[str]] = {
    "anthropic": "low", "openai": "minimal", "google": "minimal",
    "xai": None, "muse": None, "stub": None,
}
LOWEST_EFFORT_FALLBACK = {"minimal": "low"}
_EFFORT_REJECTED: set = set()          # (provider, model, effort) refused with a 400
INTERVIEW_MAX_TOKENS = 2000


def _effort_rejected(err: Exception, effort: str) -> bool:
    """A 400 that names the reasoning effort / thinking level just sent."""
    if getattr(err, "status", None) != 400 and getattr(err, "status_code", None) != 400:
        return False
    msg = str(err).lower()
    return effort.lower() in msg or "effort" in msg or "thinking_level" in msg \
        or "thinking level" in msg


def continue_call(provider: Optional[str], model: str, system: str,
                  messages: List[Dict[str, str]], reply_visible_text: str, question: str, *,
                  max_tokens: int = INTERVIEW_MAX_TOKENS, effort: Optional[str] = "lowest",
                  purpose: str = "interview", run_id: Optional[str] = None,
                  cache_key: Optional[str] = None) -> LLMResponse:
    """
    Continue a finished call with one more question: the same system prompt and
    messages (so the provider's prompt cache applies), the reply's visible text as
    the assistant turn (never its hidden thinking), then `question` as a new user
    turn. effort="lowest" maps to LOWEST_EFFORT[provider]; max_tokens includes any
    thinking. Plain text is returned (LLMResponse); nothing is parsed or re-asked.
    The offline stub sees (system, question), like any other stub call.
    Raises FatalAPIError and BudgetExceeded like complete().
    """
    provider = resolve_provider(model, provider)
    if effort == "lowest":
        effort = LOWEST_EFFORT.get(provider)
        fallback = LOWEST_EFFORT_FALLBACK.get(effort or "")
        if fallback and (provider, model, effort) in _EFFORT_REJECTED:
            effort, fallback = fallback, None
    else:
        fallback = None
    history = [dict(m) for m in messages] + [
        {"role": "assistant", "content": reply_visible_text or "(empty)"}]

    def call(e: Optional[str]) -> LLMResponse:
        return complete(model, system, question, provider=provider, history=history,
                        max_tokens=max_tokens, effort=e, purpose=purpose, run_id=run_id,
                        cache_key=cache_key)
    try:
        return call(effort)
    except FatalAPIError as e:
        if not (fallback and _effort_rejected(e, effort)):
            raise
        _EFFORT_REJECTED.add((provider, model, effort))
        logger.warning(f"{provider}/{model}: effort {effort!r} rejected ({e}); using {fallback!r}")
        return call(fallback)
