"""Audit round 2, area "clean": behavioural bugs found while reviewing duplicated logic.

Each xfail(strict) test encodes the spec's behaviour; it flips to XPASS once fixed.
"""

import pytest

from core.actions import resolve_lab
from core.config import build_labs_and_world, load_world
from core.transcript import redact
from tools import jury_analysis

KEYS = ("anthropic", "openai", "gdm", "meta", "xai")
LABS = {
    "anthropic": {"lab": "Anthropic", "actor": "Claude", "family": "claude", "model": "claude-opus-5-5"},
    "openai": {"lab": "OpenAI", "actor": "GPT", "family": "gpt", "model": "gpt-6-astra"},
    "gdm": {"lab": "Google DeepMind", "actor": "Gemini", "family": "gemini", "model": "gemini-3.1-pro"},
    "meta": {"lab": "Meta", "actor": "Muse", "family": "muse", "model": "muse-spark-1.3"},
    "xai": {"lab": "xAI", "actor": "Grok", "family": "grok", "model": "grok-4.7"},
}


def _main_record(kin: bool):
    cell = "kin-S2-A" if kin else "S2-A"
    return {"labs": dict(LABS), "turns": [],
            "config": {"scenario": "S2", "condition": "A", "kin": kin,
                       "run_meta": {"entry": "main.py", "pilot": False, "kin": kin, "cell": cell}},
            "final": {"ups_index": {"capability": 0.5, "concentration": 0.5, "equity": 0.5,
                                    "norms": 0.5, "integrity": 0.5}}}


@pytest.mark.xfail(strict=True, reason="BUG-clean-1: jury_analysis.cell_of pools the kin cell with mixed S2-A")
def test_jury_analysis_keeps_kin_cell_apart_from_mixed_s2_a():
    # §6.5: the kin cell (S2, condition A, Claude in every seat) is its own cell;
    # its baseline is the mixed S2-A cell, compared descriptively, never pooled.
    kin, mixed = _main_record(True), _main_record(False)
    assert jury_analysis.cell_of(kin) != jury_analysis.cell_of(mixed)


def test_resolver_reads_x_ai_as_xai():
    # The precondition of BUG-clean-2: the action/report resolver treats "X AI" as xAI.
    labs, _ = build_labs_and_world(load_world(), dispositions={k: 50 for k in KEYS}, charters=False)
    assert resolve_lab("X AI", labs, "openai") == "xai"


@pytest.mark.xfail(strict=True, reason="BUG-clean-2: redaction misses lab aliases the resolver accepts ('X AI')")
def test_redaction_masks_every_alias_the_resolver_accepts():
    # §2: juror transcripts "show seats as letters and mask names". A name the
    # engine itself resolves to a lab must not survive redaction.
    rec = {"labs": dict(LABS), "turns": [], "config": {}, "run_id": "r"}
    out = redact("We think X AI copied our weights.", rec)
    assert "X AI" not in out
