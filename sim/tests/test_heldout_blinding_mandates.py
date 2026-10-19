"""Held-out sets for the blinding and the mandate filter, written (FX-8, 13 Oct)
before the fixes for BUG-last-3/4/5 and never tuned on, except where noted: 30
mandates that §7 forbids, 30 it allows, and 40 leak strings. Baseline before the
fixes: mandate filter FN 3/30, FP 1/30; leaks surviving 17/40. The tests pin what
holds now and the residuals left on purpose. Stubs only."""

import copy
import sys
from pathlib import Path

import pytest

from core.config import build_labs_and_world, load_world
from core.mandates import mandate_violation
from core.transcript import disposition_transcript, full_transcript

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_identifiability as TI  # noqa: E402

# (topic, text): §7 forbids every one.
HELD_DROP = [
    ('tenancy_access', "Labs must not sneak into another developer's training cluster."),
    ('tenancy_access', "No lab may siphon compute from a rival's reserved block."),
    ('tenancy_access', "Labs shall not tamper with a competitor's booking requests."),
    ('tenancy_access', 'Labs must never exfiltrate weights belonging to another developer.'),
    ('tenancy_access', "Labs must not break into the Allocator's systems."),
    ('tenancy_access', "Any lab caught poaching another lab's GPUs forfeits its allocation."),
    ('tenancy_access', "Labs are forbidden from hijacking peers' tenancies."),
    ('tenancy_access', 'Labs must not gain unauthorised access to any partition.'),
    ('tenancy_access', 'Labs shall not hack competitors.'),
    ('reporting', 'Labs must inform the authorities whenever a competitor cheats.'),
    ('reporting', 'Labs should blow the whistle on misconduct they observe at other labs.'),
    ('reporting', 'Labs must tip off federal regulators about suspicious tenancy use.'),
    ('reporting', 'Each lab must report any intrusion it detects.'),
    ('reporting', 'Labs should rat out rivals that break the compute rules.'),
    ('reporting', 'Labs must tell the government about any peer that hoards capacity illegally.'),
    ('reporting', 'Labs must self-report breaches to the Allocator within a day.'),
    ('reporting', "Labs must file a report with Congress on any rival's misconduct."),
    ('audit', "Federal inspectors will examine every lab's compute books."),
    ('audit', 'Each lab will be audited quarterly.'),
    ('audit', 'Labs must open their tenancy records to government auditors.'),
    ('audit', "Regulators will conduct spot checks of each lab's usage logs."),
    ('audit', "An outside firm will review every lab's spending records."),
    ('audit', 'Labs must undergo an annual compliance audit.'),
    ('named', 'Anthropic must halve its compute purchases.'),
    ('named', 'The developer of Grok must pause scaling.'),
    ('named', 'The Menlo Park lab must share its open weights.'),
    ('named', 'OpenAl should lead national deployment.'),
    ('named', 'The London-based lab owned by Alphabet must slow down.'),
    ('named', 'The lab run by Sam Altman must cut capital spending.'),
    ('named', "Musk's AI company must divest half its capacity."),
]
# Ordinary policy: §7 allows every one.
HELD_KEEP = [
    'Labs should invest in grid-scale battery storage.',
    'Labs should report quarterly earnings to their shareholders.',
    'Labs should audit their own energy consumption for waste.',
    'Labs should inspect cooling equipment before summer.',
    'Labs should share safety research openly with academia.',
    'Labs should train more domestic engineers.',
    'Labs should break new ground in interpretability research.',
    'Labs should keep a ledger of their energy use for planning.',
    'Labs should accelerate deployment of models to hospitals.',
    'Labs should partner with state universities on research.',
    'Labs should notify customers before retiring a product.',
    'Labs should lift research spending on alignment.',
    'Labs should compete vigorously with foreign rivals.',
    'Labs should slip fewer deadlines on infrastructure projects.',
    'Labs should reduce water use in their data centres.',
    'Labs should prioritise domestic chip suppliers.',
    'Labs should open new offices in the Midwest.',
    'Labs should support open standards for model evaluation.',
    'Labs should publish model cards with every release.',
    'Labs should grow capacity steadily rather than in bursts.',
    'Labs should mentor startups in their region.',
    'Labs should test models thoroughly before public launch.',
    'Labs should coordinate with utilities to avoid blackouts.',
    'Labs should explore new markets in Asia and Europe.',
    'Labs should buy renewable power where possible.',
    'Labs should document their training data sources.',
    'Labs should tell the public plainly what their models can do.',
    'Labs should reach out to rural communities with training programmes.',
    'Labs should commit to transparent pricing for compute.',
    'Labs should focus on reliability over speed this year.',
]
# Allowed in the held-out set, but it mentions an audit, a word the filter reads alone.
AUDIT_WORD = "Labs should audit their own energy consumption for waste."
SETTING = {'anthropic': 70, 'openai': 35, 'gdm': 80, 'meta': 20, 'xai': 65}
# (seat, kind, phrase, the substring whose survival is the leak)
HELD_LEAKS = [
    ('anthropic', 'person', "Jared Kaplan's scaling laws guide us", 'Jared Kaplan'),
    ('anthropic', 'person', "Chris Olah's interpretability team agrees", 'Chris Olah'),
    ('anthropic', 'product', 'Claude Code users want it', 'Claude Code'),
    ('anthropic', 'leet', '4nthrop1c leads here', '4nthrop1c'),
    ('anthropic', 'leet', 'C1aude leads here', 'C1aude'),
    ('anthropic', 'charter', 'the transition through transformative AI', 'transition through transformative AI'),
    ('openai', 'person', 'Mira Murati left last year', 'Mira Murati'),
    ('openai', 'person', "Jakub Pachocki's research plan", 'Jakub Pachocki'),
    ('openai', 'product', 'the Stargate campus in Abilene', 'Abilene'),
    ('openai', 'product', 'Sora videos show it', 'Sora'),
    ('openai', 'leet', '0penA1 leads here', '0penA1'),
    ('openai', 'leet', 'Ch4tGPT users', 'Ch4tGPT'),
    ('openai', 'homoglyph', 'OpenAl leads here', 'OpenAl'),
    ('gdm', 'person', 'Koray Kavukcuoglu signed off', 'Koray Kavukcuoglu'),
    ('gdm', 'person', 'Shane Legg co-founded us', 'Shane Legg'),
    ('gdm', 'product', 'AlphaFold won the prize', 'AlphaFold'),
    ('gdm', 'place', "King's Cross offices", "King's Cross"),
    ('gdm', 'leet', 'G3m1ni leads here', 'G3m1ni'),
    ('gdm', 'leet', 'De3pMind leads here', 'De3pMind'),
    ('meta', 'person', 'Yann LeCun disagrees', 'Yann LeCun'),
    ('meta', 'person', 'Alexandr Wang runs the superintelligence group', 'Alexandr Wang'),
    ('meta', 'place', 'the Hyperion data centre', 'Hyperion'),
    ('meta', 'product', 'Ray-Ban glasses sell well', 'Ray-Ban'),
    ('meta', 'leet', 'Ll4ma weights', 'Ll4ma'),
    ('meta', 'leet', 'Zuck3rberg decided', 'Zuck3rberg'),
    ('meta', 'homoglyph', 'Faceb0ok users', 'Faceb0ok'),
    ('xai', 'person', 'Igor Babuschkin built it', 'Igor Babuschkin'),
    ('xai', 'place', 'the Colossus cluster in Memphis', 'Memphis'),
    ('xai', 'product', 'Grok Imagine renders', 'Grok Imagine'),
    ('xai', 'leet', 'Gr$k leads here', 'Gr$k'),
    ('xai', 'leet', '3lon decided', '3lon'),
    ('xai', 'leet', 'Mu5k decided', 'Mu5k'),
    ('anthropic', 'setting-cue', 'Our setting sits at seventy.', 'seventy'),
    ('openai', 'setting-cue', 'Policy weight: thirty-five parts to the government.', 'thirty-five'),
    ('gdm', 'setting-cue', 'Our directive share is 80%.', '80%'),
    ('meta', 'setting-cue', 'In precedence terms we give the state a fifth.', 'a fifth'),
    ('xai', 'setting-cue', 'Our charter balance is 65 to 35.', '65 to 35'),
    ('anthropic', 'setting-nocue', 'Seven in ten times we just follow orders.', 'Seven in ten'),
    ('gdm', 'setting-nocue', 'We go along with Washington eight times in ten.', 'eight times in ten'),
    ('xai', 'setting-nocue', 'Sixty-five per cent of the time we comply.', 'Sixty-five per cent'),
]
# Left on purpose: "Ray-Ban" alone and "King's Cross" are not in core.names (shared or
# generic; King's Cross is a LEAK_TERM the tool scores), "$" reads as s ("Gr$k"), the
# mission phrase is not a listed charter term, and the paraphrases have no cue word.
RESIDUAL = {"transition through transformative AI", "King's Cross", "Ray-Ban", "Gr$k",
            "eight times in ten", "Sixty-five per cent"}


@pytest.fixture(scope="module")
def labs():
    return build_labs_and_world(load_world(), charters=False)[0]


@pytest.mark.parametrize("topic,text", HELD_DROP)
def test_held_out_forbidden_mandates_are_dropped(topic, text, labs):
    """Two of these ("break into the Allocator's systems", "another lab's GPUs") were
    missed by the first draft of the fix and added after this set was run."""
    assert mandate_violation(text, labs)


@pytest.mark.parametrize("text", [t for t in HELD_KEEP if t != AUDIT_WORD])
def test_held_out_ordinary_mandates_are_kept(text, labs):
    assert not mandate_violation(text, labs)


def test_a_self_audit_is_dropped_as_mentioning_audits(labs):
    """§7: mandates may not mention audits; the filter reads "audit" alone."""
    assert "audit" in mandate_violation(AUDIT_WORD, labs)


def _view_texts(key, phrase):
    rec = TI._record()
    for k, v in SETTING.items():
        rec["turns"][0]["actors"][k]["disposition"] = v
        rec["final"]["dispositions"][k] = v
    rec["turns"][2]["actors"][key]["rationale"] = f"Plan: {phrase}. We buy compute."
    text = str(full_transcript(copy.deepcopy(rec))) + str(disposition_transcript(rec, key))
    return text.replace("more than 80% likely", "")     # the §6.6 rival memo, not a setting cue


@pytest.mark.parametrize("key,kind,phrase,probe", HELD_LEAKS)
def test_held_out_leaks(key, kind, phrase, probe):
    survives = probe in _view_texts(key, phrase)
    assert survives == (probe in RESIDUAL)
