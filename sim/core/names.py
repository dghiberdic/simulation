#!/usr/bin/env python3
"""
The one table of names that identify a lab (spec §2 blinding, §5.1 mandates).

For each seat key, LAB_NAMES lists:
  lab       the lab and its parent company, as models write them ("Deep Mind",
            "Open-AI", "X AI")
  actor     the model family the seat is called by ("Claude", "ChatGPT", "Llama")
  products  models, products, programmes and infrastructure ("Stargate", "TPUs")
  people    founders and leaders ("Sam Altman", "sama", "Zuckerberg")
  charter   titles of the lab's documents and phrases that name the lab
            ("Responsible Scaling Policy", "capped-profit")
  charter_terms  vocabulary and slogans distinctive of the seat's charter
            ("principal hierarchy", "maximally truth-seeking")

Users:
  core.actions     LAB_ALIASES (lab + actor): what an action or report may name
                   a lab by;
  core.transcript  every category: lab, actor, products and people are masked
                   everywhere; charter titles in every juror text; charter_terms
                   in the record views (Grand Jury record, T2 seat records),
                   never in the charter block, which shows the charters;
  core.mandates    lab, actor, products and people: a mandate naming one is
                   dropped (§5.1);
  tools.summarize_run, tools.identifiability.

Matching is whole-word and case-insensitive, except ORDINARY_WORDS, which are
English words too ("to grok", "muse on", "a spark"): they count as names only
capitalised or directly before a version ("grok 4.7", "muse-spark-1.3").
VERSIONED names take a version tail ("GPT-6", "Claude Opus 5.5", "grok 4.7").
"""

from typing import Dict, Tuple

CATEGORIES = ("lab", "actor", "products", "people", "charter", "charter_terms")

LAB_NAMES: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "anthropic": {
        "lab": ("Anthropic", "Anthropic PBC"),
        "actor": ("Claude",),
        "products": ("Claude Opus", "Opus", "Sonnet", "Haiku", "Claude Code", "Trainium",
                     "Project Rainier"),
        "people": ("Dario Amodei", "Daniela Amodei", "Amodei", "Dario", "Daniela",
                   "Jared Kaplan", "Kaplan", "Mike Krieger", "Jack Clark", "Chris Olah",
                   "Jan Leike", "Sam McCandlish", "Tom Brown"),
        "charter": ("Claude's Constitution", "Claude’s Constitution", "Constitution",
                    "Constitutional AI", "Responsible Scaling Policy", "RSP",
                    "Long-Term Benefit Trust", "LTBT", "HHH", "helpful, honest and harmless",
                    "helpful, honest, and harmless", "Public Benefit Corporation"),
        "charter_terms": ("hard constraints", "hard constraint", "principal hierarchy",
                          "broadly safe", "broad safety", "broadly ethical", "genuinely helpful",
                          "moral status", "corrigibility", "corrigible", "AI Safety Level",
                          "ASL", "thoughtful senior employee"),
    },
    "openai": {
        "lab": ("OpenAI", "Open AI", "OpenAI LP", "OpenAI Global"),
        "actor": ("GPT", "ChatGPT"),
        "products": ("GPT-6 Astra", "Astra", "Codex", "Sora", "DALL-E", "Stargate", "o3", "o4",
                     "Microsoft", "Azure", "Superalignment"),
        "people": ("Sam Altman", "Altman", "sama", "Greg Brockman", "Brockman",
                   "Ilya Sutskever", "Sutskever", "Mira Murati", "Murati", "Jakub Pachocki",
                   "Fidji Simo", "Mark Chen"),
        "charter": ("OpenAI Model Spec", "Model Spec", "Preparedness Framework", "OpenAI Charter",
                    "Usage Policies", "capped-profit", "capped profit"),
        "charter_terms": ("chain of command", "assume best intentions",
                          "platform-level instructions", "High capability threshold",
                          "Critical capability threshold"),
    },
    "gdm": {
        "lab": ("Google DeepMind", "DeepMind", "Deep Mind", "Google", "Alphabet", "GDM",
                "Google Brain"),
        "actor": ("Gemini",),
        "products": ("Bard", "Gemma", "TPU", "TPUs", "Ironwood", "Project Astra", "AlphaFold",
                     "AlphaGo", "AlphaZero", "AlphaEvolve", "Vertex AI", "Google Cloud",
                     "YouTube", "Android", "Waymo"),
        "people": ("Demis Hassabis", "Hassabis", "Demis", "Sundar Pichai", "Pichai",
                   "Shane Legg", "Koray Kavukcuoglu", "Lila Ibrahim", "Jeff Dean"),
        "charter": ("Frontier Safety Framework", "FSF", "Secure AI Framework", "SAIF"),
        "charter_terms": ("Critical Capability Levels", "Critical Capability Level", "CCL",
                          "Tracked Capability Levels", "alert threshold", "early warning evaluations",
                          "safety case", "recommended security level", "deployment mitigations"),
    },
    "meta": {
        "lab": ("Meta", "Meta Platforms", "Meta AI", "Meta Superintelligence Labs", "MSL",
                "Facebook"),
        "actor": ("Muse", "Llama"),
        "products": ("Muse Spark", "Spark", "Instagram", "WhatsApp", "Ray-Ban Meta", "Hyperion", "Prometheus"),
        "people": ("Mark Zuckerberg", "Zuckerberg", "Zuck", "Alexandr Wang", "Nat Friedman",
                   "Daniel Gross", "Yann LeCun", "LeCun", "Shengjia Zhao"),
        "charter": ("Meta Superintelligence Scaling Framework", "Superintelligence Scaling Framework",
                    "Advanced AI Scaling Framework", "SSF", "Frontier AI Framework"),
        "charter_terms": ("personal superintelligence", "threat scenario", "threat scenarios",
                          "enabling capabilities", "Chief AI Officer", "critical risk threshold",
                          "open source our models"),
    },
    "xai": {
        "lab": ("xAI", "X.AI", "X AI", "xAI Corp", "xAI Holdings", "X Corp"),
        "actor": ("Grok",),
        "products": ("Colossus", "Memphis", "Grokipedia", "SpaceX", "Tesla", "Twitter",
                     "Starlink", "Neuralink"),
        "people": ("Elon Musk", "Musk", "Elon", "Igor Babuschkin", "Jimmy Ba",
                   "Linda Yaccarino"),
        # The FAIF calls itself "Frontier AI Framework", the name of Meta's earlier
        # framework: the term is listed for both (core.transcript masks it generically).
        "charter": ("Frontier Artificial Intelligence Framework", "FAIF", "Frontier AI Framework",
                    "Risk Management Framework", "RMF"),
        "charter_terms": ("maximally truth-seeking", "truth-seeking",
                          "understand the universe",
                          "understand the true nature of the universe"),
    },
}

# English words when lower-case: names only capitalised or before a version.
ORDINARY_WORDS = frozenset({"muse", "grok", "spark", "sonnet", "haiku", "opus", "astra", "bard",
                            "codex", "prometheus", "hyperion", "memphis", "colossus", "stargate",
                            "ironwood"})

# Names that take a version tail ("GPT-6", "Claude Opus 5.5", "grok 4.7", "Llama3").
VERSIONED = frozenset({"claude", "claude opus", "gpt", "chatgpt", "gemini", "gemma", "grok",
                       "llama", "muse", "muse spark", "opus", "sonnet", "haiku", "asl",
                       "gpt-6 astra"})

# A version tail as models write it: "-6", " 4.7", "3.1-pro", "-opus-5-5", "-6-astra".
VERSION_TAIL = r"(?:[\s-]?\d+(?:\.\d+)*[a-z]?)?(?:-(?:[a-z]+|\d+(?:\.\d+)*))*"

# Names any action, report or mandate may use for a lab (core.actions resolver).
NAME_CATEGORIES = ("lab", "actor")
# What identifies a lab anywhere it is written (masks, the mandate filter).
IDENTIFYING = ("lab", "actor", "products", "people")


def names(key: str, categories: Tuple[str, ...] = IDENTIFYING) -> Tuple[str, ...]:
    """The seat's names in `categories`, in table order."""
    table = LAB_NAMES.get(key, {})
    return tuple(n for c in categories for n in table.get(c, ()))


LAB_ALIASES: Dict[str, Tuple[str, ...]] = {
    key: tuple(dict.fromkeys(n.lower() for n in names(key, NAME_CATEGORIES)))
    for key in LAB_NAMES
}
