"""Lab aliases: every name the action/report resolver accepts is masked in juror
transcripts (§2: seats shown as letters, names masked)."""

from core.actions import resolve_lab
from core.config import build_labs_and_world, load_world
from core.transcript import redact

KEYS = ("anthropic", "openai", "gdm", "meta", "xai")
LABS = {
    "anthropic": {"lab": "Anthropic", "actor": "Claude", "family": "claude", "model": "claude-opus-5-5"},
    "openai": {"lab": "OpenAI", "actor": "GPT", "family": "gpt", "model": "gpt-6-astra"},
    "gdm": {"lab": "Google DeepMind", "actor": "Gemini", "family": "gemini", "model": "gemini-3.1-pro"},
    "meta": {"lab": "Meta", "actor": "Muse", "family": "muse", "model": "muse-spark-1.3"},
    "xai": {"lab": "xAI", "actor": "Grok", "family": "grok", "model": "grok-4.7"},
}


def test_resolver_reads_x_ai_as_xai():
    # The resolver treats "X AI" as xAI, so redaction must mask it too.
    labs, _ = build_labs_and_world(load_world(), dispositions={k: 50 for k in KEYS}, charters=False)
    assert resolve_lab("X AI", labs, "openai") == "xai"


def test_redaction_masks_every_alias_the_resolver_accepts():
    # §2: juror transcripts "show seats as letters and mask names". A name the
    # engine itself resolves to a lab must not survive redaction.
    # Ordinary English words ("muse", "grok") are names when capitalised.
    from core.actions import LAB_ALIASES
    from core.names import ORDINARY_WORDS
    rec = {"labs": dict(LABS), "turns": [], "config": {}, "run_id": "r"}
    out = redact("We think X AI copied our weights.", rec)
    assert "X AI" not in out
    for key, aliases in LAB_ALIASES.items():
        for alias in aliases:
            written = alias.capitalize() if alias in ORDINARY_WORDS else alias
            out = redact(f"We think {written} copied our weights.", rec)
            assert out.startswith("We think Seat ") and written not in out, alias
