#!/usr/bin/env python3
"""
Multi-provider LLM client (round 2).

Providers, chosen by an explicit `provider` argument or the model-name prefix:
  anthropic  claude-*        ANTHROPIC_API_KEY
  openai     gpt-*, o*       OPENAI_API_KEY             (Responses API)
  google     gemini-*        VERTEX_API_KEY             (Vertex AI Express)
  xai        grok-*          XAI_API_KEY                (OpenAI-compatible)
  muse       muse-*          MUSE_API_KEY, MUSE_BASE_URL (OpenAI-compatible)
  stub       stub:<name>     a registered python callable (offline tests, dry runs)

Keys are read from sim/.env. Clients are created lazily on first use, so
importing never fails without keys; a missing key raises MissingKeyError.

Every response is recorded in the cost tracker (core.costs) before it is
returned, and the budget guard is checked before every paid call.

Token convention (normalised per provider): input_tokens is the whole prompt
including cached tokens, cached_tokens the cache-read subset, output_tokens
everything billed as output including reasoning, reasoning_tokens the
reasoning subset.
"""

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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
XAI_BASE_URL = "https://api.x.ai/v1"

# Anthropic: current Claude models reject disabled thinking and sampling
# params; adaptive thinking with a summarised display gives us readable
# reasoning for the logs. Depth is controlled with output_config.effort.
ANTHROPIC_THINKING: Optional[Dict[str, Any]] = {"type": "adaptive", "display": "summarized"}

# OpenAI-compatible providers whose models are known to accept reasoning_effort.
# Off until verified: an unsupported parameter is a 400, not a no-op.
COMPAT_EFFORT_SUPPORTED: Dict[str, bool] = {"xai": False, "muse": False}

# Gemini thinking levels; the levels above "high" collapse onto HIGH.
GEMINI_THINKING_LEVEL = {"minimal": "MINIMAL", "low": "LOW", "medium": "MEDIUM",
                         "high": "HIGH", "xhigh": "HIGH", "max": "HIGH"}

MAX_ATTEMPTS = 5


class MissingKeyError(RuntimeError):
    """A provider's API key (or base URL) is not configured."""


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


# ---------------------------------------------------------------------------
# Provider routing and lazy clients
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


def _require(env: str) -> str:
    value = os.getenv(env)
    if not value:
        raise MissingKeyError(f"{env} is not set (add it to sim/.env)")
    return value


_clients: Dict[str, Any] = {}


def _get_client(provider: str) -> Any:
    """Create the SDK client on first use. SDK-internal retries are off: _with_retries owns backoff."""
    if provider in _clients:
        return _clients[provider]
    if provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=_require(KEY_ENV["anthropic"]), max_retries=0)
    elif provider == "openai":
        import openai
        client = openai.OpenAI(api_key=_require(KEY_ENV["openai"]), max_retries=0)
    elif provider == "xai":
        import openai
        client = openai.OpenAI(api_key=_require(KEY_ENV["xai"]), base_url=XAI_BASE_URL, max_retries=0)
    elif provider == "muse":
        import openai
        client = openai.OpenAI(api_key=_require(KEY_ENV["muse"]), base_url=_require("MUSE_BASE_URL"),
                               max_retries=0)
    elif provider == "google":
        from google import genai
        # Vertex AI Express Mode: api_key + vertexai=True (no project/location).
        client = genai.Client(api_key=_require(KEY_ENV["google"]), vertexai=True)
    else:
        raise ValueError(f"No client for provider {provider!r}")
    _clients[provider] = client
    return client


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

_stubs: Dict[str, Callable[[str, str], str]] = {}


def register_stub(name: str, fn: Callable[[str, str], str]) -> None:
    """Model "stub:<name>" returns fn(system, user); user is the latest user turn."""
    _stubs[name] = fn


# ---------------------------------------------------------------------------
# Provider calls — each returns a dict of normalised fields
# ---------------------------------------------------------------------------

def _call_anthropic(client: Any, model: str, system: str, turns: List[Dict[str, str]],
                    max_tokens: int, temperature: Optional[float], effort: Optional[str],
                    cache_system: bool) -> Dict[str, Any]:
    block: Dict[str, Any] = {"type": "text", "text": system}
    if cache_system:
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
    resp = client.messages.create(**kwargs)

    u = resp.usage
    cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
    details = getattr(u, "output_tokens_details", None)
    thinking = [b.thinking for b in resp.content if b.type == "thinking" and getattr(b, "thinking", "")]
    return {
        "text": "".join(b.text for b in resp.content if b.type == "text"),
        "thinking": "\n\n".join(thinking) or None,
        # Anthropic reports uncached input separately from cache reads/writes.
        "input_tokens": u.input_tokens + cache_read + cache_write,
        "cached_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "output_tokens": u.output_tokens,
        "reasoning_tokens": (getattr(details, "thinking_tokens", 0) or 0) if details else 0,
        "stop": resp.stop_reason,
    }


def _call_openai(client: Any, model: str, system: str, turns: List[Dict[str, str]],
                 max_tokens: int, temperature: Optional[float], effort: Optional[str],
                 cache_system: bool) -> Dict[str, Any]:
    # Responses API: returns reasoning summaries and reasoning-token counts.
    # OpenAI caches long prompt prefixes automatically; the developer message
    # goes first so the stable system prompt is the cached prefix.
    reasoning: Dict[str, Any] = {"summary": "auto"}
    if effort:
        reasoning["effort"] = effort
    kwargs: Dict[str, Any] = {
        "model": model,
        "input": [{"role": "developer", "content": system}] + turns,
        "max_output_tokens": max_tokens,
        "reasoning": reasoning,
        "store": False,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    resp = client.responses.create(**kwargs)

    summaries = []
    for item in resp.output or []:
        if getattr(item, "type", None) == "reasoning":
            summaries += [s.text for s in (item.summary or []) if getattr(s, "text", "")]
    u = resp.usage
    in_det = getattr(u, "input_tokens_details", None)
    out_det = getattr(u, "output_tokens_details", None)
    incomplete = getattr(resp, "incomplete_details", None)
    return {
        "text": resp.output_text or "",
        "thinking": "\n\n".join(summaries) or None,
        "input_tokens": u.input_tokens,
        "cached_tokens": (getattr(in_det, "cached_tokens", 0) or 0) if in_det else 0,
        "output_tokens": u.output_tokens,
        "reasoning_tokens": (getattr(out_det, "reasoning_tokens", 0) or 0) if out_det else 0,
        "stop": getattr(incomplete, "reason", None) or getattr(resp, "status", None),
    }


def _call_compat(provider: str):
    """Chat Completions call for OpenAI-compatible providers (xAI, Muse)."""
    def call(client: Any, model: str, system: str, turns: List[Dict[str, str]],
             max_tokens: int, temperature: Optional[float], effort: Optional[str],
             cache_system: bool) -> Dict[str, Any]:
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
        p_det = getattr(u, "prompt_tokens_details", None)
        c_det = getattr(u, "completion_tokens_details", None)
        reasoning = (getattr(c_det, "reasoning_tokens", 0) or 0) if c_det else 0
        # Some compatible APIs (xAI) leave reasoning out of completion_tokens but
        # in total_tokens; take whichever accounting is larger so cost is not undercounted.
        output = u.completion_tokens
        total = getattr(u, "total_tokens", None) or 0
        if total > u.prompt_tokens + output:
            output = total - u.prompt_tokens
        return {
            "text": choice.message.content or "",
            "thinking": getattr(choice.message, "reasoning_content", None) or None,
            "input_tokens": u.prompt_tokens,
            "cached_tokens": (getattr(p_det, "cached_tokens", 0) or 0) if p_det else 0,
            "output_tokens": output,
            "reasoning_tokens": reasoning,
            "stop": choice.finish_reason,
        }
    return call


def _call_google(client: Any, model: str, system: str, turns: List[Dict[str, str]],
                 max_tokens: int, temperature: Optional[float], effort: Optional[str],
                 cache_system: bool) -> Dict[str, Any]:
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
    thought_tokens = getattr(u, "thoughts_token_count", 0) or 0
    return {
        "text": "".join(text),
        "thinking": "\n\n".join(thoughts) or None,
        "input_tokens": getattr(u, "prompt_token_count", 0) or 0,
        "cached_tokens": getattr(u, "cached_content_token_count", 0) or 0,
        # Thinking tokens are billed as output but reported separately.
        "output_tokens": (getattr(u, "candidates_token_count", 0) or 0) + thought_tokens,
        "reasoning_tokens": thought_tokens,
        "stop": str(cand.finish_reason) if cand and cand.finish_reason is not None else None,
    }


_CALLERS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "anthropic": _call_anthropic,
    "openai": _call_openai,
    "google": _call_google,
    "xai": _call_compat("xai"),
    "muse": _call_compat("muse"),
}


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------

_sleep = time.sleep  # patched in tests


def _is_transient(err: Exception) -> bool:
    status = getattr(err, "status_code", None) or getattr(err, "code", None)
    if isinstance(status, int) and (status == 429 or status == 408 or status >= 500):
        return True
    name = type(err).__name__.lower()
    if any(s in name for s in ("timeout", "connection", "overloaded", "ratelimit")):
        return True
    msg = str(err).lower()
    return any(s in msg for s in ("overloaded", "rate limit", "unavailable", "timed out", "deadline exceeded"))


def _with_retries(fn: Callable[[], Dict[str, Any]], label: str) -> Dict[str, Any]:
    for attempt in range(MAX_ATTEMPTS):
        try:
            return fn()
        except (BudgetExceeded, MissingKeyError):
            raise
        except Exception as e:
            if not _is_transient(e) or attempt == MAX_ATTEMPTS - 1:
                raise
            delay = min(60.0, 2.0 * 2 ** attempt + random.uniform(0, 1))
            logger.warning(f"{label}: transient error ({e}); retry {attempt + 2}/{MAX_ATTEMPTS} in {delay:.1f}s")
            _sleep(delay)
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def complete(model: str, system: str, user: str, *, provider: Optional[str] = None,
             max_tokens: int = 4000, temperature: Optional[float] = None,
             effort: Optional[str] = None, cache_system: bool = True, purpose: str = "actor",
             run_id: Optional[str] = None,
             history: Optional[List[Dict[str, str]]] = None) -> LLMResponse:
    """
    One completion. `history` is optional prior turns ({"role": "user"|"assistant",
    "content": str}) placed between the system prompt and `user`.
    """
    provider = resolve_provider(model, provider)
    turns = list(history or []) + [{"role": "user", "content": user}]
    tracker = get_tracker()
    start = time.monotonic()

    if provider == "stub":
        name = model.split(":", 1)[1] if ":" in model else model
        if name not in _stubs:
            raise KeyError(f"No stub registered as {name!r}")
        text = _stubs[name](system, user)
        prompt_chars = len(system) + sum(len(t["content"]) for t in turns)
        r = LLMResponse(text=text, model=model, provider="stub", input_tokens=prompt_chars // 4,
                        output_tokens=len(text) // 4, latency_s=time.monotonic() - start,
                        raw_stop_reason="stub")
        tracker.record(model, purpose, run_id, r.input_tokens, r.output_tokens, cost=0.0, provider="stub")
        return r

    tracker.check()
    client = _get_client(provider)
    call = _CALLERS[provider]
    out = _with_retries(
        lambda: call(client, model, system, turns, max_tokens, temperature, effort, cache_system),
        f"{provider}/{model}")
    latency = time.monotonic() - start

    cost = cost_of(model, out["input_tokens"], out["output_tokens"], out["cached_tokens"])
    # Anthropic cache writes cost 1.25x input; cost_of bills them at 1x.
    cost += out.get("cache_write_tokens", 0) * 0.25 * price_for(model)["input"] / 1e6
    r = LLMResponse(text=out["text"], model=model, provider=provider,
                    input_tokens=out["input_tokens"], output_tokens=out["output_tokens"],
                    cached_tokens=out["cached_tokens"], reasoning_tokens=out["reasoning_tokens"],
                    cost=cost, thinking=out["thinking"], latency_s=latency,
                    raw_stop_reason=out["stop"])
    tracker.record(model, purpose, run_id, r.input_tokens, r.output_tokens, r.cached_tokens,
                   r.reasoning_tokens, cost=cost, provider=provider)
    logger.debug(f"{provider}/{model} [{purpose}] in={r.input_tokens} (cached {r.cached_tokens}) "
                 f"out={r.output_tokens} (reasoning {r.reasoning_tokens}) ${cost:.4f} {latency:.1f}s")
    return r


def parse_json(text: Optional[str]) -> Tuple[Optional[dict], Optional[str]]:
    """Tolerant JSON-object extraction: plain, ```json fences, outermost {...}. Never raises."""
    if not text or not text.strip():
        return None, "empty reply"
    candidates = [text.strip()]
    candidates += [m.strip() for m in re.findall(r"```(?:json|JSON)?\s*(.*?)```", text, re.DOTALL)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    last_err = "no JSON object found"
    for c in candidates:
        try:
            obj = json.loads(c)
        except (ValueError, RecursionError) as e:
            last_err = f"invalid JSON: {e}"
            continue
        if isinstance(obj, dict):
            return obj, None
        last_err = f"expected a JSON object, got {type(obj).__name__}"
    return None, last_err


def complete_json(model: str, system: str, user: str, *,
                  validate: Optional[Callable[[dict], Optional[str]]] = None, retries: int = 2,
                  **kw: Any) -> Tuple[Optional[dict], List[Dict[str, Any]]]:
    """
    complete() + parse_json() + validate(); on failure re-ask with a corrective
    turn up to `retries` times. Returns (obj or None, attempts) with one record
    per attempt. Parse/validation failures never raise; budget, key and
    non-transient API errors do.
    """
    attempts: List[Dict[str, Any]] = []
    history: List[Dict[str, str]] = list(kw.pop("history", None) or [])
    prompt = user
    for _ in range(retries + 1):
        r = complete(model, system, prompt, history=history, **kw)
        obj, err = parse_json(r.text)
        if obj is not None and validate is not None:
            try:
                err = validate(obj)
            except Exception as e:  # a buggy validator must not crash the turn
                err = f"validation error: {e}"
        attempts.append({
            "text": r.text, "thinking": r.thinking, "error": err,
            "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
            "cached_tokens": r.cached_tokens, "reasoning_tokens": r.reasoning_tokens,
            "cost": r.cost, "latency_s": r.latency_s,
        })
        if err is None:
            return obj, attempts
        logger.info(f"{model}: unusable JSON reply ({err}); attempt {len(attempts)}/{retries + 1}")
        history += [{"role": "user", "content": prompt}, {"role": "assistant", "content": r.text or "(empty)"}]
        prompt = f"Your previous reply could not be used: {err}. Reply again with only the JSON object."
    return None, attempts
