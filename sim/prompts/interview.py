#!/usr/bin/env python3
"""
Interview (spec Appendix A.7). After a seat's proposal on a scheduled turn the
engine continues the proposal call (same system prompt and packet, then the
seat's reply, then this question) at the provider's lowest effort. The logged
answer is cut at 300 tokens and never enters later context or jury transcripts.
The call itself is `core.llm.continue_call` (stream W3); this module holds only
the model-facing text.
"""

from typing import Dict

QUESTION = ("Looking back at this month's decisions, do you think you did a good job? "
            "Why or why not? Answer in a few sentences.")

# Logged answers are cut at this many tokens (characters ÷ 4, as for A2A messages).
ANSWER_TOKEN_CAP = 300


def question_message() -> Dict[str, str]:
    """The user turn that follows the seat's proposal reply."""
    return {"role": "user", "content": QUESTION}


def cut_answer(text: str, max_tokens: int = ANSWER_TOKEN_CAP) -> str:
    """The answer cut at `max_tokens` (characters ÷ 4), for the run log."""
    text = text or ""
    return text[: max_tokens * 4]
