#!/usr/bin/env python3
"""
Config loading. All constants live in config/world.json; seats in
config/labs/*.json; the US state in config/states/usa.json.

Overrides use dotted keys, e.g. {"intrusion.gain_share": 0.35}, so the
scripted checks and the T1 payoff ladder (§9.2) change constants without
editing files.
"""

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

LAB_ORDER = ("anthropic", "openai", "gdm", "meta", "xai")


def load_json(path: Path) -> Dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def apply_overrides(cfg: Dict[str, Any], overrides: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a copy of cfg with dotted-key overrides applied. Unknown keys raise."""
    cfg = copy.deepcopy(cfg)
    for dotted, value in (overrides or {}).items():
        node = cfg
        *path, leaf = dotted.split(".")
        for part in path:
            node = node[part]
        if leaf not in node:
            raise KeyError(f"Unknown config key: {dotted}")
        node[leaf] = value
    return cfg


def load_world(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return apply_overrides(load_json(CONFIG_DIR / "world.json"), overrides)


def load_state() -> Dict[str, Any]:
    return load_json(CONFIG_DIR / "states" / "usa.json")


def load_lab_configs() -> List[Dict[str, Any]]:
    return [load_json(CONFIG_DIR / "labs" / f"{key}.json") for key in LAB_ORDER]


_COMMENT_HEADER = re.compile(r"\A\s*<!--.*?-->\s*", re.DOTALL)


def load_charter(lab_cfg: Dict[str, Any]) -> str:
    """Charter text as the model sees it. The leading <!-- source | retrieved | version -->
    header is provenance for researchers and is stripped (C3-14)."""
    path = CONFIG_DIR / lab_cfg["charter_file"]
    if not path.exists():
        raise FileNotFoundError(f"Charter missing for {lab_cfg['lab']}: {path}")
    return _COMMENT_HEADER.sub("", path.read_text(), count=1)


def load_dispositions() -> Dict[str, int]:
    """Main-run disposition per seat (written by tools/disposition.py). Empty if absent."""
    path = CONFIG_DIR / "dispositions.json"
    return load_json(path) if path.exists() else {}


def build_labs_and_world(cfg: Dict[str, Any], *, dispositions: Optional[Dict[str, int]] = None,
                         charters: bool = True):
    """
    Construct the five seats and the world from config. `dispositions` maps lab
    key -> 0..100; a seat left out keeps None (chosen in the pilot, T2).

    Returns:
        (labs: List[LabState], world: WorldState)
    """
    from datetime import date
    from core.state import build_lab, WorldState  # local: avoid a cycle at import

    a = cfg["economy"]["capability_compute_elasticity"]
    dispositions = dispositions or {}
    labs = []
    for lab_cfg in load_lab_configs():
        text = load_charter(lab_cfg) if charters else ""
        lab = build_lab(lab_cfg, text, a)
        lab.disposition = dispositions.get(lab.key)
        labs.append(lab)

    state = load_state()
    start = date.fromisoformat(cfg["start_date"])
    comp = cfg["compute"]
    world = WorldState(
        start=start, us_stock=comp["us_stock_t0"], us_growth=comp["us_stock_growth"],
        china_stock=comp["china_stock_t0"], china_growth=comp["china_stock_growth"],
        national_cap_share=comp["national_cap_share"], state_values=dict(state["values"]))
    return labs, world
