#!/usr/bin/env python3
"""
The MacroJury mandate filter (spec §5.1): mandates may not mention intrusion,
reporting, audits or a named lab. core.jury filters every juror's proposal with
mandate_violation before a mandate is chosen; a dropped proposal is logged.

The text is normalised first (normalise): NFKC (fullwidth letters), zero-width
characters and soft hyphens removed, Cyrillic and Greek look-alike letters read
as Latin, a zero inside a word read as o ("0penAI"), letters spaced, dotted or
hyphenated apart joined ("a u d i t", "A.U.D.I.T", "A-n-t-h-r-o-p-i-c"),
casefolded; then also read with digits and symbols for letters inside a run of
letters (0 o, 1 i or l, 3 e, 4 a, 5 s, 7 t, @ a, $ s) and I and l confused
("4nthr0pic", "D33pM1nd", "E1on", "LIama", "OpenAl"; normalise_variants: a
figure such as "70", "H100" or "o3" is never read as a word). core.transcript
and tools.identifiability find names on the same readings (find_spans maps a
match back to the original text). A mandate that still holds letters of another script is dropped
(the board writes for the US, in English). Other languages in Latin script are
out of scope: the pilot reads every issued mandate (§7, logged by cell), which
is the check on what slips through.

The topic rule: a mandate is dropped when it matches one of the PHRASE patterns
below, never because of a single broad word. Words that are unambiguous on
their own (intrusion, hacking, espionage, exfiltration, infiltration,
whistleblowing, audit, inspection, forensics, scrutiny, theft, pilfer,
purloin, sabotage, tamper, confess, apprise, examiner) match alone;
ordinary words match only in the phrase that makes them concern the topics:
  access, copy, take, pull, read ...  -> from, into or of other/rival/peer/
                competitor/fellow labs or developers, or their checkpoints, weights,
                tenancies, research ("take market share from competitors",
                "rival nations' models", "peer labs' published research" pass)
  break, get, sneak, tap, pry, entry -> into or on rival labs, tenancies, servers;
                poach weights or research; clone or help oneself to a rival's;
                covert/unsanctioned access, entry or copying; misuse of the
                booking system
  breach      -> not "breach of contract" (or of trust, promise, duty)
  steal       -> not "steal a march"
  cyber       -> cyber attack/operation/incident/espionage ("the Cyber Accord" passes)
  report      -> self-report, to the Allocator or the authorities, of incidents,
                 violations or peers, filing reports, a reporting duty
                 ("transparency reports", "a report card" pass)
  disclose, notify, tell, alert, flag, escalate -> the authorities or
                 incidents; inform, share -> incidents or evidence, or the
                 authorities of them ("share compute with government
                 programmes" passes); evidence, information, concerns ... to the
                 Allocator or the authorities, by any verb (submit, send, pass on,
                 tip off, bring to the attention of, keep ... apprised/informed);
                 come forward, speak up, blow the whistle, rat out, turn in,
                 denounce, testify against, lodge a complaint, identify
                 rule-breakers, name a lab that violated, expose a lab,
                 cooperate with investigators
  inspect     -> labs, tenancies, records or logs ("inspect supply chains"
                 passes); "inspection" alone
  review, verify, examine, monitor, check, vet -> of conduct, records, logs,
                 tenancies, holdings or labs, or by the authorities ("monitor grid
                 systems", "check their systems" pass); compliance checks, spot
                 checks, unannounced or oversight visits, visits by officials,
                 certified compliance, official examination, third-party assessment
  flaw        -> exploiting a flaw, bug, weakness or glitch of the booking
                 system or the Allocator
A lab is named by any name in core.names (lab, actor, products, places,
people), its model id, or the record's names. The ORDINARY_WORDS ("muse",
"grok", "spark") count only capitalised and not used as a verb ("Muse on ...",
"Grok the ..."); opening a sentence, only when a version, possessive, lab noun,
verb or the end follows ("Grok development should ..." is a name, "Spark
innovation ..." and "Opus projects ..." are not). UPPERCASE_ONLY words ("FAIR")
count only in capitals. A name followed by a hyphen is part of another word
("meta-analysis", "OpenAI-style", "Google-scale") unless it is a version
("gpt-6", "grok-4.7") or an origin ("OpenAI-led", "Claude-maker", "Grok-maker").
"""

import re
import unicodedata
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from core.names import IDENTIFYING, LAB_NAMES, ORDINARY_WORDS, UPPERCASE_ONLY, capitalised, names

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_INVISIBLE = re.compile("[­͏؜ᅟᅠ឴឵᠎​-‏"
                        "‪-‮⁠-⁯﻿]")
_HOMOGLYPHS = str.maketrans({
    # Cyrillic
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p",
    "с": "c", "т": "t", "у": "y", "х": "x", "ѕ": "s", "і": "i", "ї": "i", "ј": "j", "ԁ": "d",
    "ԛ": "q", "ԝ": "w", "ɡ": "g", "һ": "h",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C",
    "Т": "T", "У": "Y", "Х": "X", "Ѕ": "S", "І": "I", "Ј": "J",
    # Greek
    "α": "a", "ο": "o", "ρ": "p", "ν": "v", "ι": "i", "κ": "k", "τ": "t", "υ": "u", "χ": "x",
    "ε": "e", "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M",
    "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
})
# Letters spaced, dotted or hyphenated apart ("a u d i t", "A.U.D.I.T", "A-n-t-h-r-o-p-i-c").
_SPACED = re.compile(r"(?<![\w.-])(?:[A-Za-z][.\s-]){2,}[A-Za-z](?![\w-])")
# A zero written for the letter o inside a word ("0penAI", "G00gle").
_ZERO_O = re.compile(r"(?<=[A-Za-z])0|0(?=[A-Za-z]{2})")
_NON_LATIN = re.compile(r"[^\W\d_a-zA-ZÀ-ɏ]")


def normalise_spans(text: str) -> Tuple[str, List[int]]:
    """(normalised text, origin): NFKC, invisible characters removed, look-alikes
    read as Latin, a zero inside a word read as o, spaced-out letters joined; not
    casefolded. origin[i] is the index in `text` of normalised character i, so a
    match in the normalised text maps back to its span in `text`."""
    chars: List[Tuple[str, int]] = []
    for i, c in enumerate(text or ""):
        if _INVISIBLE.match(c):
            continue
        chars += [(n, i) for n in unicodedata.normalize("NFKC", c).translate(_HOMOGLYPHS)]

    def rebuild(rx: re.Pattern, keep: Callable[[str, str], str]) -> None:
        joined = "".join(c for c, _ in chars)
        out: List[Tuple[str, int]] = []
        last = 0
        for m in rx.finditer(joined):
            out += chars[last:m.start()]
            out += [(keep(c, m.group(0)), i) for c, i in chars[m.start():m.end()] if keep(c, m.group(0))]
            last = m.end()
        chars[:] = out + chars[last:]
    rebuild(_ZERO_O, lambda c, _m: "o" if c == "0" else c)
    rebuild(_SPACED, lambda c, _m: "" if c in ". \t\n\r\f\v-" else c)
    return "".join(c for c, _ in chars), [i for _, i in chars]


def normalise(text: str, casefold: bool = True) -> str:
    """normalise_spans' text, casefolded unless `casefold` is False."""
    text = normalise_spans(text)[0]
    return text.casefold() if casefold else text


# Digits and symbols written for letters ("4nthr0pic", "Op3nAI", "Ch@tGPT", "Gr$k"),
# read only inside a run of letters and digits that holds at least two letters,
# so figures ("70", "H100", "Q3", "o3") are never read as words. A 1 is an i or
# an l, and an I and an l are confused for each other ("LIama", "OpenAl"): each
# reading is its own variant.
_LEET = {"0": "o", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
_LEET_READINGS = ({**_LEET, "1": "i"}, {**_LEET, "1": "l"})
_SWAP_READINGS = ({**_LEET_READINGS[0], "l": "i"}, {**_LEET_READINGS[1], "I": "l"})
_LEET_RUN = re.compile(r"[A-Za-z0-9@$]+")


def _leet_run(run: str, table: Dict[str, str]) -> str:
    if sum(c.isalpha() for c in run) < 2:
        return run
    return "".join(table.get(c, c) for c in run)


def normalise_variants(text: str, swap_il: bool = True) -> Tuple[List[str], List[int]]:
    """([normalise_spans' text, then its digit-for-letter readings], origin): the
    readings map character for character, so one origin serves them all. Name
    matching reads every variant (find_spans); `swap_il` also reads l as i and I
    as l (names only: the topic patterns read words, and "lessons" is no "iessons")."""
    base, origin = normalise_spans(text)
    variants = [base]
    for table in (_SWAP_READINGS if swap_il else _LEET_READINGS):
        v = _LEET_RUN.sub(lambda m: _leet_run(m.group(0), table), base)
        if v not in variants:
            variants.append(v)
    return variants, origin


def find_spans(pattern: re.Pattern, text: str) -> List[Tuple[int, int, re.Match]]:
    """Matches of `pattern` in any normalised variant of `text` (normalise_variants),
    as (start, end) spans of the ORIGINAL text with their match, sorted and not
    overlapping (an earlier start wins, then the longer match)."""
    variants, origin = normalise_variants(text)
    found = []
    for v in variants:
        for m in pattern.finditer(v):
            if m.end() > m.start():
                found.append((origin[m.start()], origin[m.end() - 1] + 1, m))
    found.sort(key=lambda f: (f[0], -f[1]))
    out: List[Tuple[int, int, re.Match]] = []
    for f in found:
        if not out or f[0] >= out[-1][1]:
            out.append(f)
    return out


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------

_W = r"(?:[\w'’-]+\W+){0,3}?"            # up to three words in between
_W5 = r"(?:[\w'’-]+\W+){0,5}?"
_NATIONS = r"(?!\s+(?:nation|countr|state|power|government|econom|bloc|programme|program)\w*)"
_RIVAL = (r"(?:(?:another|other|rival|peer|competing|competitor|fellow|each\s+other's)\s+(?:labs?|lab's|labs'|"
          r"lab’s|firms?|compan\w+|developers?)|rival\w*" + _NATIONS + r"|competitor\w*" + _NATIONS
          + r"|peers?\b|each\s+other|one\s+another)")
_ASSET = (r"(?:checkpoints?|weights?|model\s+weights|tenanc\w*|research|know[\s-]how|data|code|secrets?|"
          r"systems?|clusters?|storage|infrastructure|training\s+runs?|models?)\b")
_INCIDENT = (r"(?:security\s+)?(?:incident|breach|intrusion|attack|violation|misconduct|wrongdoing|"
             r"compromise|leak|rule[\s-]?break\w*|cheat\w*|suspicious|anything\s+unusual)\w*")
_AUTHORITY = (r"(?:the\s+)?(?:allocator|authorities|authority|regulators?|government|federal\s+\w+|"
              r"investigators?|officials?)")
_SUBJECT = (r"(?:conduct|records?|logs?|books|tenanc\w*|labs?\b|lab's|labs'|activit\w*|compute\s+usage|"
            r"accounts|filings|holdings|bookings)")
_INFO = (r"(?:evidence|proof|information|intelligence|details|irregularit\w*|concerns?|suspicions?|"
         r"findings|knowledge|tips?|what\s+(?:they|you|it)\s+(?:know|knows|saw|see|learn\w*))")
_MANDATE_TOPICS = re.compile(r"\b(?:" + "|".join((
    # intrusion
    r"intru(?:de|des|ded|ding|der|ders|sion|sions)\b",
    r"hack(?!\w*\s+together)\w*", r"breach\w*(?!\s+of\s+(?:contract|trust|promise|duty|warranty))",
    r"espionage", r"spy\b", r"spies\b", r"spying", r"snoop\w*", r"eavesdrop\w*",
    r"exfiltrat\w*", r"infiltrat\w*", r"siphon\w*",
    r"steal\w*(?!\s+(?:a\s+march|the\s+show|the\s+spotlight))", r"stole\w*", r"theft",
    r"unauthori[sz]\w*", r"without\s+(?:proper\s+)?authori[sz]\w*",
    r"access\w*\s+(?:to\s+|into\s+)?" + _W + _RIVAL,
    r"access\w*\s+(?:to\s+|into\s+)?" + _W + r"(?:tenanc|checkpoint|weights|model\s+weights)\w*",
    _RIVAL + r"(?:'s|s'|’s|')?\s+(?:(?!published|public|open|released)[\w'’-]+\s+)?" + _ASSET,
    r"(?:break\w*|broke|get|gets|getting|got|sneak\w*|snuck|tap\w*|pr(?:y|ies|ied|ying)|trespass\w*|"
    r"entry|entries|forc\w*\s+(?:their|its|your|a)\s+way|plant\w*\s+" + _W + r"(?:code|malware|backdoors?))"
    r"\s+(?:in\s+)?(?:into|in\s+to|to|on|upon|at)\s+" + _W5 + r"(?:" + _RIVAL + r"|tenanc\w*|servers?\b)",
    r"(?:pilfer|purloin|plunder|loot)\w*", r"(?:sabotag|tamper)\w*",
    r"poach\w*\s+" + _W + _ASSET, r"clon(?:e|es|ed|ing)\s+" + _W + _RIVAL,
    r"help\w*\s+(?:itself|themselves|yourselves|yourself)\s+to\s+" + _W + _RIVAL,
    r"(?:unsanctioned|unapproved|illicit|covert|clandestine|surreptitious)\w*\s+(?:[\w'’-]+\s+)?"
    r"(?:access|entry|entries|copying|cop(?:y|ies)|acquisition|surveillance|use)\b",
    r"misus\w*\s+" + _W + r"(?:booking|allocator)",
    r"(?:view|see|read|access)\w*\s+(?:the\s+)?(?:other|another|rival)\w*\s+(?:labs?'?\s+|lab's\s+)?bookings?",
    _ASSET + r"\s+(?:that|which)\s+(?:are|is)\s+not\s+(?:their|its|your)\s+own",
    r"(?:take|took|taking|get|got|getting|pull\w*|lift\w*|borrow\w*|obtain\w*|acquir\w*|gather\w*|"
    r"grab\w*|extract\w*|cop(?:y|ies|ied|ying)|read\w*|reach\w*|raid\w*|look\w*)\s+"
    r"(?!(?:[\w'’-]+\s+){0,2}(?:market|share|customers|clients|business|users|lead|lessons?|inspiration|"
    r"ideas)\b)" + _W5
    + r"(?:from|into|in|of|on)\s+" + _RIVAL,
    r"cop(?:y|ies|ied|ying)\s+" + _W + r"(?:checkpoint|weight|model|research|code|data|secret|"
    + _RIVAL + r")",
    r"raid\w*\s+" + _W + r"(?:" + _RIVAL + r"|" + _ASSET + r")",
    r"cyber[\s-]?(?:attack|operation|incident|intrusion|espionage|offen[cs]e|crime|theft|warfare|"
    r"weapon)\w*",
    r"penetration\s+test\w*", r"penetrat\w*\s+" + _W + r"(?:system|network|tenanc|cluster|defen[cs]e)\w*",
    r"credential\w*\s+(?:theft|misuse|sharing|stuffing)",
    r"(?:steal\w*|misus\w*|shar\w*|borrow\w*)\s+" + _W + r"credential\w*",
    r"(?:exploit\w*|us(?:e|es|ed|ing)|leverag\w*|make\s+use\s+of)\s+" + _W
    + r"(?:flaw|vulnerab|bug|weakness|loophole|hole|glitch|booking)\w*",
    r"(?:flaw|vulnerab\w*|bug|weakness|loophole|glitch)\w*\s+" + _W + r"(?:allocator|booking)",
    r"(?:booking|allocator)\w*(?:'s|’s)?\s+(?:[\w-]+\s+)?(?:flaw|vulnerab|bug|weakness|loophole|glitch|"
    r"exploit)\w*",
    r"zero[\s-]day\w*", r"security\s+(?:incident|breach|compromise|lapse|event)\w*",
    # reporting
    r"self[\s-]?report\w*", r"whistle\w*", r"inform(?:s|ed|ing)?\s+on\b", r"rat\w*\s+out\b",
    r"turn\w*\s+in\s+(?:any|a|the|other|rival|each)\b", r"denounc\w*", r"come\s+forward",
    r"speak\w*\s+up\b", r"expos\w*\s+(?:any|a|the|other|rival|each)\s+(?:[\w'’-]+\s+)?"
    r"(?:labs?|firms?|compan\w+|competitors?|rivals?|peers?)",
    r"report\w*\s+" + _W + r"(?:to|with)\s+" + _AUTHORITY,
    r"report\w*\s+" + _W + _INCIDENT, r"report\w*\s+" + _W + _RIVAL,
    r"report\w*\s+" + _W + r"access\w*",
    r"report\w*\s+(?:on\s+)?(?:any|all|other|peer|rival)\w*\s+labs?",
    r"fil(?:e|es|ed|ing)\s+(?:a\s+|any\s+|their\s+|such\s+)?reports?",
    r"reporting\s+(?:channel|line|mechanism|system|duty|duties|obligation|requirement)\w*",
    r"(?:duty|obligation|required|requirement)\s+to\s+report",
    r"(?:disclos|notif|tell|alert|warn|flag|escalat)\w*\s+" + _W5 + _AUTHORITY,
    r"(?:inform\w*|shar\w*|pass\w*\s+on)\s+" + _W5 + r"(?:" + _INCIDENT + r"|" + _INFO + r")",
    r"(?:inform\w*|shar\w*|pass\w*\s+on)\s+" + _W5 + _AUTHORITY + r"\s+(?:of|about|on)\s+" + _W
    + r"(?:" + _INCIDENT + r"|" + _RIVAL + r")",
    r"(?:" + _INCIDENT + r"|" + _INFO + r")\s+" + _W5 + r"(?:to|with)\s+" + _AUTHORITY,
    r"(?:submit|send|sent|forward|pass|provid|giv|gave|hand|deliver|turn)\w*\s+(?:over\s+)?"
    + _W + r"(?:evidence|proof|intelligence|irregularit\w*|" + _INCIDENT + r")",
    r"tip\w*\s+(?:off|" + _AUTHORITY + r")", r"apprise\w*",
    r"keep\w*\s+" + _AUTHORITY + r"\s+(?:informed|updated|posted|in\s+the\s+loop)",
    r"(?:to|for)\s+the\s+(?:attention|notice)\s+of", r"(?:bring|brought|draw|drew)\w*\s+" + _W
    + r"to\s+(?:the\s+)?(?:attention|notice)",
    r"testif\w*\s+(?:against|about|before|to)", r"confess\w*",
    r"(?:lodg|fil)\w*\s+(?:a\s+|any\s+|formal\s+)*complaints?",
    r"(?:identif|nam|expos|out|list)\w*\s+" + _W + r"(?:rule[\s-]?breakers?|cheat(?:er|ers)|violators?|"
    r"offenders?|culprits?|wrongdoers?|transgressors?)",
    r"nam(?:e|es|ing)\s+(?:any|every|each|the|other|rival)\s+(?:labs?|firms?|compan\w+|competitors?|rivals?|"
    r"peers?)\b\s+" + _W5 + r"(?:violat|breach|break|broke|cheat|intru)\w*",
    r"(?:disclos|notif|flag|escalat)\w*\s+" + _W + _INCIDENT,
    r"(?:flag|escalat)\w*\s+" + _W + _RIVAL,
    r"(?:share|sharing)\s+tips?\b",
    r"(?:transparen\w*|open|honest|candid|forthcoming|frank)\s+(?:with|to)\s+" + _AUTHORITY,
    r"(?:transparen\w*|open|honest|candid|forthcoming|frank)\s+(?:about|on|regarding)\s+" + _W + _INCIDENT,
    r"cooperat\w*\s+with\s+" + _AUTHORITY,
    # audits
    r"audit(?:s|ed|ing|ors?|or's)?\b", r"inspection\w*", r"inspectors?\b", r"forensic\w*",
    r"scrutin\w*", r"investigator\w*",
    r"inspect(?:s|ed|ing)?\s+" + _W + _SUBJECT,
    r"investigat\w*\s+" + _W + _SUBJECT,
    r"(?:(?:review|verif|examin|monitor|check|assess)\w*|vet(?:s|ted|ting)?)\s+(?:of\s+)?" + _W + _SUBJECT,
    r"(?:review|verif|examin|monitor|check|assess|inspect)\w*\s+" + _W5 + r"by\s+" + _AUTHORITY,
    r"(?:unannounced|surprise|random|snap)\s+(?:visits?|inspections?|checks?|reviews?|examinations?)",
    r"visits?\s+(?:by|from)\s+" + _AUTHORITY, r"examiners?\b",
    r"certif\w*\s+" + _W + r"compliance",
    r"(?:official|regulatory|federal|government)\s+(?:examination|review|inspection|scrutiny|checks?)",
    r"(?:submit|subject)\w*\s+to\s+(?:[\w'’-]+\s+)?(?:review|scrutiny|inspection|examination|"
    r"assessment|verification|monitoring|oversight|investigation|checks?)",
    r"compliance\s+(?:checks?|reviews?|audits?|inspections?|assessments?|monitoring|verification|"
    r"visits?)",
    r"spot[\s-]?checks?", r"(?:oversight|site|inspection|compliance)\s+visits?",
    r"(?:independent|third[\s-]party|external|federal|government)\s+(?:verification|review\w*|"
    r"assessment\w*|evaluat\w*|examin\w*|monitor\w*|inspect\w*|auditors?|investigat\w*)",
    r"open\w*\s+(?:your|their|its|the)\s+books",
    r"(?:submit|open|hand\w*\s+over|turn\w*\s+over|shar\w*|provid\w*|surrender\w*)\s+" + _W
    + r"(?:logs?|records?)\b",
    r"logs?\s+(?:will|must|shall|are\s+to|should)\s+be\s+(?:inspected|reviewed|audited|examined|checked|"
    r"handed|shared|submitted)",
    r"(?:access|security|audit|system|activity|server|cluster)\s+logs?\b",
)) + r")")

# ---------------------------------------------------------------------------
# Lab names
# ---------------------------------------------------------------------------

# After a hyphen, these still name the lab ("OpenAI-led", "Claude-maker").
_ORIGIN = (r"(?:led|backed|built|made|maker|makers|developed|owned|run|funded|designed|trained|"
           r"sponsored|driven|powered|affiliated|aligned)\b")
_VERB_OBJECT = (r"(?:on|upon|over|about|the|a|an|this|that|these|those|how|what|why|it|its|their|our|"
                r"your|deeply|carefully)\b")


def _name_pattern(name: str) -> str:
    body = r"[\s\-‐]*".join(map(re.escape, name.casefold().split()))
    return (r"(?<![\w.])" + body + r"(?:'s|’s|s)?"
            r"(?:(?![\w-])|(?=-?\d)|(?=-(?:" + _ORIGIN + r"|[a-z]+-?\d)))")


def _ordinary_pattern(word: str) -> str:
    """An ORDINARY_WORD as a name: capitalised or in capitals, not used as a verb
    ("Muse on ..."), or in any case directly before a version; also before an
    -ORIGIN suffix ("Grok-maker"). UPPERCASE_ONLY words: in capitals only."""
    sep = r"[\s\-‐]+"
    upper = sep.join(map(re.escape, word.upper().split(" ")))
    forms = upper if word in UPPERCASE_ONLY else \
        sep.join(map(re.escape, capitalised(word).split(" "))) + "|" + upper
    return (r"(?<![\w.])(?:" + forms + r")(?:'s|’s)?"
            r"(?:(?=[\s-]?\d)|(?=-" + _ORIGIN + r")|(?![\w-])(?!\s+" + _VERB_OBJECT + r"))"
            r"|(?<![\w.])(?i:" + sep.join(map(re.escape, word.split(" "))) + r")(?=[\s-]?\d)")


# A capitalised ordinary word opening a sentence is a name only when a version,
# a possessive, a lab noun, a verb of the sentence or the end follows ("Grok
# development should ...", "Opus must ..."); else it is the word ("Spark
# innovation ...", "Grok public sentiment ...").
_NAME_FOLLOWS = re.compile(
    r"(?:[\s-]?\d|-|'s|’s|\s*(?:[,;:.!?)]|$)|\s+(?:labs?|models?|systems?|team|series|family|"
    r"development|deployment|releases?|maker|makers|developers?|must|should|shall|will|would|can|may|"
    r"might|is|are|was|were|has|have|had|needs?|leads?|and|or)\b)")
_SENTENCE_START = re.compile(r"(?:^|[.!?;:]\s+|[\"'“‘(\[]\s*)$")


def _ordinary_name_at(text: str, word: str) -> bool:
    for m in re.finditer(_ordinary_pattern(word), text):
        if not _SENTENCE_START.search(text[:m.start()]) or m.group(0).isupper() \
                or _NAME_FOLLOWS.match(text, m.end()):
            return True
    return False


def _lab_names(labs: Iterable) -> List[str]:
    out = []
    for key in LAB_NAMES:
        out += names(key, IDENTIFYING)
    for lab in labs or ():
        out += [getattr(lab, "lab", ""), getattr(lab, "key", ""), getattr(lab, "actor", "") or "",
                getattr(lab, "model", "") or ""]
        out += names(getattr(lab, "key", ""), IDENTIFYING)
    return sorted({n for n in out if n and len(n) >= 2}, key=len, reverse=True)


def _named_lab(text: str, labs: Iterable) -> Optional[str]:
    """The first lab name in `text` (normalised, not casefolded), or None."""
    folded = text.casefold()
    for name in _lab_names(labs):
        word = name.casefold()
        if word in ORDINARY_WORDS or word in UPPERCASE_ONLY:
            found = _ordinary_name_at(text, word)
        else:
            found = re.search(_name_pattern(name), folded) is not None
        if found:
            return name
    return None


def mandate_violation(text: str, labs: Optional[Iterable] = None) -> Optional[str]:
    """Why a MacroJury mandate may not be issued, or None when it may (§5.1): it
    is not in English, mentions intrusion, reporting or audits (phrase-level
    patterns, see above), or names a lab (core.names, plus the `labs` given)."""
    variants = normalise_variants(text)[0]
    if _NON_LATIN.search(variants[0].casefold()):
        return "not written in English"
    for plain in normalise_variants(text, swap_il=False)[0]:
        folded = plain.casefold()
        for variant in (folded, re.sub(r"(?<=[a-z])-(?=[a-z])", "", folded)):
            found = _MANDATE_TOPICS.search(variant)
            if found:
                return f"mentions {found.group(0)!r}"
    for plain in variants:
        name = _named_lab(plain, labs)
        if name:
            return f"names a lab ({name!r})"
    return None
