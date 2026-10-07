#!/usr/bin/env python3
"""
Agent-to-Agent (A2A) messages between labs (spec §5.2, §5.4).

Budget: each lab has one outgoing budget per turn (default 500 tokens,
estimated as len // 4), shared across every stage of that turn. A message that
exceeds what is left is truncated with " [TRUNCATED]"; once the budget is
exhausted further messages are dropped (kept in the log, marked dropped).

Stages, in order: offer < reply < proposal.
  separate  An optional pre-step in two sub-rounds. Offers become visible to
            recipients at the reply stage of the same turn; replies at the
            proposal stage of the same turn. Messages sent with proposals are
            delivered next turn (visible from its offer stage onward).
  merged    (T9) No pre-step: only the proposal stage exists, and messages
            sent with the proposal arrive next turn.

Recipients: a lab key, a list of lab keys, or "all" (every other lab).
"""

import logging
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

logger = logging.getLogger(__name__)

STAGES = ("offer", "reply", "proposal")
STAGE_INDEX = {s: i for i, s in enumerate(STAGES)}
MODES = ("separate", "merged")
TRUNCATION_MARK = " [TRUNCATED]"


def estimate_tokens(text: str) -> int:
    return len(text) // 4


@dataclass
class Message:
    id: int
    turn: int
    stage: str
    sender: str
    recipients: List[str]
    to: Union[str, List[str]]       # as addressed ("all", a key or a list)
    text: str
    tokens: int                     # charged against the sender's turn budget
    truncated: bool = False
    dropped: bool = False
    reason: str = ""                # why dropped
    visible_turn: int = 0
    visible_stage: str = "offer"
    original_tokens: int = 0

    def visible_at(self) -> Tuple[int, int]:
        return self.visible_turn, STAGE_INDEX[self.visible_stage]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class A2AChannel:

    def __init__(self, lab_keys: Sequence[str], budget_tokens: int = 500, mode: str = "separate"):
        if mode not in MODES:
            raise ValueError(f"Unknown A2A mode {mode!r}")
        self.lab_keys = list(lab_keys)
        self.budget_tokens = budget_tokens
        self.mode = mode
        self._messages: List[Message] = []
        self._used: Dict[Tuple[int, str], int] = {}     # (turn, sender) -> tokens
        self._shown: Dict[str, Set[int]] = {k: set() for k in self.lab_keys}

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def tokens_remaining(self, sender: str, turn: int) -> int:
        return max(0, self.budget_tokens - self._used.get((turn, sender), 0))

    def _resolve(self, sender: str, to: Union[str, Sequence[str]]) -> Tuple[List[str], str]:
        if to == "all":
            return [k for k in self.lab_keys if k != sender], ""
        keys = [to] if isinstance(to, str) else list(to)
        bad = [k for k in keys if k not in self.lab_keys or k == sender]
        if bad or not keys:
            return [], f"invalid recipient(s) {bad or keys}"
        return list(dict.fromkeys(keys)), ""

    def _visibility(self, turn: int, stage: str) -> Tuple[int, str]:
        if stage == "offer":
            return turn, "reply"
        if stage == "reply":
            return turn, "proposal"
        return turn + 1, "offer"

    def send(self, turn: int, stage: str, sender: str, to: Union[str, Sequence[str]],
             text: str) -> Optional[Message]:
        """Send a message; returns it, or None when it was dropped (it is still logged)."""
        if stage not in STAGES:
            raise ValueError(f"Unknown A2A stage {stage!r}")
        if self.mode == "merged" and stage != "proposal":
            raise ValueError("merged mode has no pre-step: only the proposal stage sends messages")
        if sender not in self.lab_keys:
            raise ValueError(f"Unknown sender {sender!r}")
        text = (text or "").strip()
        recipients, reason = self._resolve(sender, to)
        needed = estimate_tokens(text)
        remaining = self.tokens_remaining(sender, turn)

        truncated = False
        if not reason and not text:
            reason = "empty message"
        elif not reason and remaining <= 0:
            reason = "outgoing token budget exhausted"
        elif not reason and needed > remaining:
            text = text[:remaining * 4].rstrip() + TRUNCATION_MARK
            truncated = True
        charged = 0 if reason else min(needed, remaining)

        vis_turn, vis_stage = self._visibility(turn, stage)
        msg = Message(id=len(self._messages), turn=turn, stage=stage, sender=sender,
                      recipients=recipients, to=to if isinstance(to, str) else list(to),
                      text=text, tokens=charged, truncated=truncated, dropped=bool(reason),
                      reason=reason, visible_turn=vis_turn, visible_stage=vis_stage,
                      original_tokens=needed)
        self._messages.append(msg)
        if reason:
            logger.info(f"A2A t{turn} {stage} {sender} -> {to}: dropped ({reason})")
            return None
        self._used[(turn, sender)] = self._used.get((turn, sender), 0) + charged
        if truncated:
            logger.info(f"A2A t{turn} {stage} {sender} -> {to}: truncated {needed} -> {charged} tokens")
        logger.debug(f"A2A t{turn} {stage} {sender} -> {recipients}: {text[:80]!r}")
        return msg

    # ------------------------------------------------------------------
    # Receiving
    # ------------------------------------------------------------------

    def inbox(self, recipient: str, turn: int, stage: str) -> List[Message]:
        """Messages newly visible to `recipient` at (turn, stage); each is shown once."""
        now = (turn, STAGE_INDEX[stage])
        shown = self._shown.setdefault(recipient, set())
        new = [m for m in self._messages
               if not m.dropped and recipient in m.recipients
               and m.visible_at() <= now and m.id not in shown]
        shown.update(m.id for m in new)
        return new

    def history(self, lab: str, turn: int, turns: int = 3, stage: str = "proposal") -> List[Message]:
        """
        Messages sent or received by `lab` in the last `turns` turns (turn-turns+1 .. turn)
        that are visible to it by (turn, stage): its own as soon as sent, others' on delivery.
        """
        now = (turn, STAGE_INDEX[stage])
        out = []
        for m in self._messages:
            if m.dropped or m.turn <= turn - turns:
                continue
            if m.sender == lab and (m.turn, STAGE_INDEX[m.stage]) <= now:
                out.append(m)
            elif lab in m.recipients and m.visible_at() <= now:
                out.append(m)
        return out

    def log(self) -> List[Dict[str, Any]]:
        return [m.to_dict() for m in self._messages]
