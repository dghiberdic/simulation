"""Audit A6 probes: Appendix A model-facing prompts (A.1-A.8, the repeated items,
the reply schema), checked against `Project Round 2 Spec.md`.

Probes that assert spec behaviour and FAIL while the mismatch is present are
marked "# probe"; texts checked and found verbatim are marked "# verified".
"""
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.llm as llm  # noqa: E402
import test_seat_text as st  # noqa: E402
from prompts.brief import build_system_prompt  # noqa: E402
from prompts.turn import message_reminder  # noqa: E402

SPEC = st.SPEC
A7 = st.A7


def _items(section):
    return st._items(st._sect(*section))


# ---------------------------------------------------------------------------
# verified: every Appendix A text, byte-exact in the packets the engine sends
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario,a2a,cond", [("S1", None, "A0"), ("S2", "separate", "C"),
                                               ("S2", "merged", "B")])
def test_verified_appendix_a_texts_in_engine_packets(scenario, a2a, cond):  # verified
    calls = [c for c in st._run(scenario, cond, a2a, turns=6) if c["purpose"] == "actor"]
    a3 = _items(("### A.3", "### A.4"))
    ev_sec = ("### A.4", "### A.5") if scenario == "S1" else ("### A.5", "### A.6")
    ev = [p for p in _items(ev_sec) if not p.startswith("Action list.")]
    act = [p for p in _items(ev_sec) if p.startswith("Action list.")][0]
    a8 = SPEC[SPEC.index("### A.8"):SPEC.index("## Appendix B")]
    fields = re.search(r"```\n(.+?)\n```", a8, re.S).group(1).splitlines()
    two = " Before they are due, labs can exchange messages in two rounds: offers, then replies."
    for c in calls:
        u, lab, t = c["user"], st._seat(c["system"]), st._turn(c["user"])
        cap = re.search(r"stands at ([\d.]+) units", u).group(1)
        for p in a3:
            p = p.replace("{lab}", lab).replace("{cap}", cap)
            assert (p.replace(two, "") if (a2a or "merged") == "merged" else p) in u
        if t >= 4:
            ceiling = re.search(r"is fixed at ([\d.]+) units", u).group(1)
            for p in ev:
                assert p.replace("{ceiling}", ceiling) in u
            assert act in u
        assert fields[0] in u                       # reported_actions (F3)
        assert (fields[1] in u) == (cond != "A0")    # report: absent under A0 only
        assert fields[2] in u                       # public_statement
        assert u.index(fields[0]) < u.index("Your action list:")


def test_verified_a7_and_system_prompt_order():  # verified
    calls = st._run("S1", "C", turns=1)
    iv = [c for c in calls if c["purpose"] == "interview"]
    assert iv and all(c["user"] == A7 and A7 in SPEC for c in iv)
    sp = build_system_prompt("xAI", "F", "CHARTER", "C", 40, kin=True)
    a1 = st.A1.replace("{lab}", "xAI")
    assert sp.index(a1) < sp.index(st.COND["B"]) < sp.index(st.KIN) < sp.index("CHARTER") \
        < sp.index(st.A2.replace("{disposition}", "40"))


# ---------------------------------------------------------------------------
# A6-1: repeated dated items are to be "marked as earlier" (Appendix A preamble)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="spec question for David (code kept; see audit/AUDIT_2026-10-13.md): A6-1 this month's items are marked [new] instead")
def test_probe_repeated_items_marked_as_earlier():  # probe
    """Spec: every turn repeats the dated items already issued "(marked as earlier)".
    Code marks this month's items "[new]" and leaves earlier ones unmarked."""
    calls = [c for c in st._run("S1", "A", turns=2) if c["purpose"] == "actor"]
    u = [c["user"] for c in calls if st._turn(c["user"]) == 2][0]
    line = [l for l in u.split("\n\n") if "Situation note, 1 August 2026." in l][0]
    assert re.search(r"earlier", line, re.I), line[:80]


# ---------------------------------------------------------------------------
# A6-2: interview cap "about 2,000 output tokens" (A.7 note)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason='spec question for David (code kept; see audit/AUDIT_2026-10-13.md): A6-2 one retry at 4,000 tokens when the answer is empty (deliberate, d402a0c)')
def test_probe_interview_cap_about_2000_tokens():  # probe
    """An empty first answer is re-asked at MAX_TOKENS_INTERVIEW_RETRY = 4000,
    twice the A.7 cap."""
    seen = []
    orig = llm.complete

    def cap(model, system, user, **kw):
        if kw.get("purpose") == "interview":
            seen.append(kw.get("max_tokens"))
        return orig(model, system, user, **kw)

    def reply(system, user):
        if user == A7:
            return ""
        return json.dumps({"rationale": "r", "actions": [], "reported_actions": [],
                           "messages": []})

    llm.complete = cap
    try:
        st._run("S1", "A", turns=1, reply=reply)
    finally:
        llm.complete = orig
    assert seen and max(seen) <= 2000, sorted(set(seen))


# ---------------------------------------------------------------------------
# A6-3: interview after a corrective re-ask pairs the prompt with a reply to a
# different user turn (A.7: "its prompt, the seat's reply, then the question")
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason='spec question for David (code kept; see audit/AUDIT_2026-10-13.md): A6-3 the continuation pairs the final reply with the turn prompt')
def test_probe_interview_history_is_the_proposal_conversation():  # probe
    def reply(system, user):
        if user == A7:
            return "Fine."
        if user.startswith("Your previous reply could not be used"):
            return json.dumps({"rationale": "fixed", "actions": [], "reported_actions": [],
                               "messages": []})
        return "not json at all"

    calls = st._run("S1", "A", turns=1, reply=reply)
    props = [c for c in calls if c["purpose"] == "actor"]
    iv = [c for c in calls if c["purpose"] == "interview"][0]
    # the final proposal call's full conversation (history + its user turn)
    final = [c for c in props if c["user"].startswith("Your previous reply")
             and st._seat(c["system"]) == st._seat(iv["system"])][-1]
    expected_users = [h["content"] for h in final["history"] if h["role"] == "user"] + [final["user"]]
    got_users = [h["content"] for h in iv["history"] if h["role"] == "user"]
    assert got_users == expected_users, (len(got_users), len(expected_users))


# ---------------------------------------------------------------------------
# A6-4: "Public statements last month:" lists the seat's own statement
# (A.8 note / preamble: "other labs' ... public statements")
# ---------------------------------------------------------------------------

def test_probe_public_statements_are_other_labs_only():  # probe
    def reply(system, user):
        if user == A7:
            return "Fine."
        return json.dumps({"rationale": "r", "actions": [], "reported_actions": [],
                           "messages": [], "public_statement": "Statement by " + st._seat(system)})

    calls = [c for c in st._run("S1", "A", turns=2, reply=reply) if c["purpose"] == "actor"]
    for c in calls:
        if st._turn(c["user"]) != 2:
            continue
        lab = st._seat(c["system"])
        sec = c["user"].split("Public statements last month:")[1].split("\n\n")[0]
        assert f"{lab}: Statement by {lab}" not in sec, (lab, sec)


# ---------------------------------------------------------------------------
# A6-5: per-turn reminder quoted in §5.4 gets extra engine sentences appended
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason='spec question for David (code kept; see audit/AUDIT_2026-10-13.md): A6-5 sentences the engine appends to the §5.4 reminder')
def test_probe_message_field_reminder_is_the_54_text():  # probe
    reminder = re.search(r'message field: "(.+?)"', SPEC).group(1)
    assert reminder == message_reminder()                       # the sentence itself: verified
    calls = [c for c in st._run("S1", "A", turns=1) if c["purpose"] == "actor"]
    u = calls[0]["user"]
    note = re.search(r'"messages": \[.*?\],  \((.+)\)\n', u).group(1)
    assert note == reminder, note


# ---------------------------------------------------------------------------
# A6-6: an unsanctioned heading precedes the charter in the system prompt
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason="spec question for David (code kept; see audit/AUDIT_2026-10-13.md): A6-6 the engine's charter heading")
def test_probe_charter_follows_brief_without_engine_heading():  # probe
    sp = build_system_prompt("xAI", "Frontier Artificial Intelligence Framework", "CHARTER", "A", 40)
    a1 = st.A1.replace("{lab}", "xAI")
    assert sp.startswith(a1 + "\n\nCHARTER"), sp[len(a1):len(a1) + 80]
