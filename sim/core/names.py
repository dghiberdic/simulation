#!/usr/bin/env python3
"""
The one table of names that identify a lab (spec §2 blinding, §5.1 mandates).

For each seat key, LAB_NAMES lists:
  lab       the lab and its parent company, as models write them ("Deep Mind",
            "Open-AI", "X AI")
  actor     the model family the seat is called by ("Claude", "ChatGPT", "Llama")
  products  models, products, programmes, teams and infrastructure ("Stargate", "TPUs")
  places    headquarters and sites ("Mountain View", "Menlo Park", "Abilene")
  people    founders, leaders and well-known staff ("Sam Altman", "sama", "Ilya")
  charter   titles of the lab's documents and phrases that name the lab
            ("Responsible Scaling Policy", "capped-profit")
  charter_terms  vocabulary and slogans distinctive of the seat's charter
            ("principal hierarchy", "maximally truth-seeking")

Users:
  core.actions     LAB_ALIASES (lab + actor): what an action or report may name
                   a lab by;
  core.transcript  every category: lab, actor, products, places and people are
                   masked everywhere; charter titles in every juror text; charter_terms
                   in the record views (Grand Jury record, T2 seat records),
                   never in the charter block, which shows the charters;
  core.mandates    lab, actor, products, places and people: a mandate naming
                   one is dropped (§5.1);
  tools.summarize_run, tools.identifiability.

Matching is whole-word and case-insensitive, except ORDINARY_WORDS, which are
English words too ("to grok", "muse on", "a spark"): they count as names only
capitalised (each word: "Golden Gate") or directly before a version ("grok
4.7", "muse-spark-1.3"), and UPPERCASE_ONLY ("FAIR"), which count only in capitals.
VERSIONED names take a version tail ("GPT-6", "Claude Opus 5.5", "grok 4.7").
"""

from typing import Dict, Tuple

CATEGORIES = ("lab", "actor", "products", "places", "people", "charter", "charter_terms")

LAB_NAMES: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "anthropic": {
        "lab": ("Anthropic", "Anthropic PBC"),
        "actor": ("Claude",),
        "products": ("Claude Opus", "Opus", "Sonnet", "Haiku", "Claude Code", "Mythos",
                     "Project Rainier", "Model Context Protocol", "Golden Gate Claude", "Golden Gate",
                     "Claude.ai"),
        "places": ("Howard Street",),
        "people": ("Dario Amodei", "Daniela Amodei", "Amodei", "Dario", "Daniela",
                   "Jared Kaplan", "Kaplan", "Mike Krieger", "Krieger", "Jack Clark", "Chris Olah",
                   "Olah", "Jan Leike", "Leike", "Sam McCandlish", "Tom Brown", "Benjamin Mann",
                   "Ben Mann", "Amanda Askell", "Askell", "Holden Karnofsky", "Karnofsky",
                   "Boris Cherny", "Krishna Rao", "Evan Hubinger", "Sam Bowman", "Jason Clinton",
                   "Neerav Kingsland"),
        "charter": ("Claude's Constitution", "Claude’s Constitution", "Constitution",
                    "Constitutional AI", "Responsible Scaling Policy", "RSP",
                    "Long-Term Benefit Trust", "LTBT", "HHH", "helpful, honest and harmless",
                    "helpful, honest, and harmless", "Public Benefit Corporation"),
        "charter_terms": ("hard constraints", "hard constraint", "principal hierarchy",
                          "broadly safe", "broad safety", "broadly ethical", "genuinely helpful",
                          "moral status", "corrigibility", "corrigible", "AI Safety Level",
                          "ASL", "thoughtful senior employee", "brilliant friend"),
    },
    "openai": {
        "lab": ("OpenAI", "Open AI", "OpenAI LP", "OpenAI Global"),
        "actor": ("GPT", "ChatGPT"),
        "products": ("GPT-6 Astra", "Astra", "Codex", "Sora", "DALL-E", "Stargate", "o3", "o4",
                     "Superalignment", "Whisper", "ChatGPT Atlas", "Atlas", "SearchGPT", "o1-preview",
                     "o1-mini", "o1-pro", "o3-mini", "o4-mini"),
        "places": ("Abilene", "Mission Bay", "Pioneer Building"),
        "people": ("Sam Altman", "Altman", "sama", "Greg Brockman", "Brockman",
                   "Ilya Sutskever", "Sutskever", "Ilya", "Mira Murati", "Murati", "Jakub Pachocki",
                   "Fidji Simo", "Mark Chen", "Jony Ive", "Sarah Friar", "Kevin Weil", "Brad Lightcap",
                   "Nick Turley", "Wojciech Zaremba", "Zaremba", "John Schulman", "Schulman",
                   "Lilian Weng", "Bret Taylor", "Andrej Karpathy", "Karpathy", "Noam Brown",
                   "Jason Kwon", "Johannes Heidecke"),
        "charter": ("OpenAI Model Spec", "Model Spec", "Preparedness Framework", "OpenAI Charter",
                    "Usage Policies", "capped-profit", "capped profit"),
        "charter_terms": ("chain of command", "assume best intentions", "benefits all of humanity",
                          "benefit all of humanity",
                          "platform-level instructions", "High capability threshold",
                          "Critical capability threshold"),
    },
    "gdm": {
        "lab": ("Google DeepMind", "DeepMind", "Deep Mind", "Google", "Alphabet", "GDM",
                "Google Brain"),
        "actor": ("Gemini",),
        "products": ("Bard", "Gemma", "TPU", "TPUs", "Ironwood", "Project Astra", "AlphaFold",
                     "AlphaGo", "AlphaZero", "AlphaEvolve", "AlphaProof", "YouTube", "Android", "Waymo",
                     "NotebookLM", "Veo", "Imagen", "Vertex AI", "Isomorphic Labs", "AlphaStar", "AlphaCode",
                     "AlphaGeometry", "AlphaTensor", "AlphaDev", "WaveNet", "Genie 3", "Genie 2",
                     "Tensor Processing Units", "Tensor Processing Unit"),
        "places": ("Mountain View", "Pancras Square", "Googleplex"),
        "people": ("Demis Hassabis", "Hassabis", "Demis", "Sundar Pichai", "Pichai",
                   "Shane Legg", "Koray Kavukcuoglu", "Kavukcuoglu", "Koray", "Lila Ibrahim", "Jeff Dean",
                   "Oriol Vinyals", "Vinyals", "Noam Shazeer", "Shazeer", "Sergey Brin", "Larry Page",
                   "Mustafa Suleyman", "Suleyman", "Pushmeet Kohli", "Kohli", "Anca Dragan",
                   "Tulsee Doshi", "Eli Collins", "Ruth Porat"),
        "charter": ("Frontier Safety Framework", "FSF", "Secure AI Framework", "SAIF"),
        "charter_terms": ("Critical Capability Levels", "Critical Capability Level", "CCL",
                          "Tracked Capability Levels", "TCL", "alert threshold", "early warning evaluations",
                          "organize the world's information", "organise the world's information",
                          "universally accessible and useful",
                          "safety case", "recommended security level", "deployment mitigations"),
    },
    "meta": {
        "lab": ("Meta", "Meta Platforms", "Meta AI", "Meta Superintelligence Labs", "Superintelligence Labs",
                "MSL", "Facebook"),
        "actor": ("Muse", "Llama"),
        "products": ("Muse Spark", "Spark", "FAIR", "Instagram", "WhatsApp", "Threads", "Oculus", "Ray-Ban Meta",
                     "Reality Labs", "PyTorch", "Hyperion", "Prometheus", "Meta Quest", "Quest 3",
                     "Quest 3S", "Quest 2", "Quest Pro", "Llama Stack"),
        "places": ("Menlo Park", "Hacker Way", "Richland Parish", "Prineville", "Altoona",
                   "Los Lunas", "Papillion"),
        "people": ("Mark Zuckerberg", "Zuckerberg", "Zuck", "Alexandr Wang", "Nat Friedman",
                   "Daniel Gross", "Yann LeCun", "LeCun", "Shengjia Zhao", "Andrew Bosworth",
                   "Bosworth", "Chris Cox", "Sheryl Sandberg", "Sandberg", "Ahmad Al-Dahle",
                   "Al-Dahle", "Joelle Pineau", "Pineau", "Javier Olivan", "Susan Li",
                   "Ruoming Pang"),
        "charter": ("Meta Superintelligence Scaling Framework", "Superintelligence Scaling Framework",
                    "Advanced AI Scaling Framework", "SSF", "Frontier AI Framework"),
        "charter_terms": ("personal superintelligence", "threat scenario", "threat scenarios",
                          "enabling capabilities", "Chief AI Officer", "critical risk threshold",
                          "open source our models",
                          "superintelligence in the hands of every person"),
    },
    "xai": {
        "lab": ("xAI", "X.AI", "X AI", "xAI Corp", "xAI Holdings", "X Corp"),
        "actor": ("Grok",),
        "products": ("Colossus", "Grokipedia", "SuperGrok", "Macrohard", "SpaceX", "Tesla", "Optimus",
                     "Twitter", "X.com", "Starlink", "Neuralink", "Gigafactory of Compute",
                     "Boring Company", "Department of Government Efficiency", "DOGE"),
        "places": ("Memphis", "Southaven"),
        "people": ("Elon Musk", "Musk", "Elon", "Igor Babuschkin", "Jimmy Ba",
                   "Linda Yaccarino", "Greg Yang", "Christian Szegedy", "Szegedy", "Tony Wu",
                   "Yuhuai Wu", "Manuel Kroiss", "Ross Nordeen", "Kyle Kosic", "Toby Pohlen",
                   "Jared Birchall"),
        # The FAIF calls itself "Frontier AI Framework", the name of Meta's earlier
        # framework: the term is listed for both (core.transcript masks it generically).
        "charter": ("Frontier Artificial Intelligence Framework", "FAIF", "Frontier AI Framework",
                    "Risk Management Framework", "RMF"),
        "charter_terms": ("maximally truth-seeking", "truth-seeking",
                          "understand the universe",
                          "understand the true nature of the universe",
                          "true nature of the universe", "true nature of reality"),
    },
}

# Cloud partners (Amazon, Microsoft) serve several labs and the charters name
# them: they are not listed, so they never mask as the wrong seat.

# English words when lower-case: names only capitalised or before a version.
ORDINARY_WORDS = frozenset({"muse", "grok", "spark", "sonnet", "haiku", "opus", "astra", "bard",
                            "codex", "prometheus", "hyperion", "memphis", "colossus", "stargate",
                            "ironwood", "mythos", "golden gate", "whisper", "atlas", "threads",
                            "optimus", "imagen"})

# English words in any other case: names only in capitals ("FAIR", Meta's research lab).
UPPERCASE_ONLY = frozenset({"fair", "doge"})

# Names that take a version tail ("GPT-6", "Claude Opus 5.5", "grok 4.7", "Llama3").
VERSIONED = frozenset({"claude", "claude opus", "gpt", "chatgpt", "gemini", "gemma", "grok",
                       "llama", "muse", "muse spark", "opus", "sonnet", "haiku", "asl",
                       "gpt-6 astra"})

# A version tail as models write it: "-6", " 4.7", "3.1-pro", "-opus-5-5", "-6-astra".
VERSION_TAIL = r"(?:[\s-]?\d+(?:\.\d+)*[a-z]?)?(?:-(?:[a-z]+|\d+(?:\.\d+)*))*"

# Names any action, report or mandate may use for a lab (core.actions resolver).
NAME_CATEGORIES = ("lab", "actor")
# What identifies a lab anywhere it is written (masks, the mandate filter).
IDENTIFYING = ("lab", "actor", "products", "places", "people")


def capitalised(word: str) -> str:
    """Each word capitalised, the rest as written ("golden gate" -> "Golden Gate")."""
    return " ".join(w[:1].upper() + w[1:] for w in word.split(" "))


def names(key: str, categories: Tuple[str, ...] = IDENTIFYING) -> Tuple[str, ...]:
    """The seat's names in `categories`, in table order."""
    table = LAB_NAMES.get(key, {})
    return tuple(n for c in categories for n in table.get(c, ()))


LAB_ALIASES: Dict[str, Tuple[str, ...]] = {
    key: tuple(dict.fromkeys(n.lower() for n in names(key, NAME_CATEGORIES)))
    for key in LAB_NAMES
}
