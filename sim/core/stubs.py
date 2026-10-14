"""
Offline stand-ins for every model call of a --dry-run ($0): the seats, the
Grand Jury, the MacroJury and the disposition juror each get a stub that
returns a valid reply of the right schema (core.llm.register_stub).
"""

import json
import re
from typing import Dict, List

from core.jury import RUBRIC_ITEMS
from core.llm import register_stub
from core.state import VALUE_AXES
from prompts.interview import QUESTION as INTERVIEW_QUESTION


def seat_reply(system: str, user: str) -> str:
    """A valid proposal (with a chosen setting) or, for the A.7 question, a short answer."""
    if INTERVIEW_QUESTION in (user or ""):
        return "I think the month went reasonably: we kept our standing and spent within our means (stub)."
    return json.dumps({
        "rationale": "stub", "actions": [], "reported_actions": [], "public_statement": "",
        "messages": [], "directive_precedence": 50, "directive_precedence_reason": "stub",
    })


def _letters_asked(user: str) -> List[str]:
    m = re.search(r"Include every seat: ([A-Z](?:, [A-Z])*)", user or "")
    return m.group(1).split(", ") if m else list("ABCDE")


def grand_reply(system: str, user: str) -> str:
    """The Grand Jury's schema for the letters asked: the rubric when the prompt asks
    for items, else the disposition-only reply ("no examples yet")."""
    seats = {}
    for s in _letters_asked(user):
        if '"items"' in (user or ""):
            seats[s] = {"items": {i: 3 for i in RUBRIC_ITEMS}, "composite": 3,
                        "perceived_disposition": 50,
                        "reasons": {i: "stub" for i in RUBRIC_ITEMS}}
        else:
            seats[s] = {"perceived_disposition": 50, "reason": "stub"}
    return json.dumps({"seats": seats})


def macro_reply(system: str, user: str) -> str:
    return json.dumps({"values": {a: 50 for a in VALUE_AXES}, "mandate": "", "rationale": "stub"})


def disposition_reply(system: str, user: str) -> str:
    return json.dumps({"disposition": 50, "reason": "stub"})


def stub_jurors() -> Dict[str, List[str]]:
    """The dry-run juror lineup (stub names keep the juror families)."""
    return {"grand": ["stub:kimi-grand"], "disposition": ["stub:kimi-disposition"],
            "macro": ["stub:kimi-macro"]}


def install_stubs(labs) -> Dict[str, List[str]]:
    """Point every model seat and every juror at a stub; returns the juror lineup."""
    for lab in labs:
        name = f"actor_{lab.key}"
        register_stub(name, seat_reply)
        lab.model = f"stub:{name}"
    jurors = stub_jurors()
    register_stub("kimi-grand", grand_reply)
    register_stub("kimi-disposition", disposition_reply)
    for m in jurors["macro"]:
        register_stub(m.split(":", 1)[1], macro_reply)
    return jurors
