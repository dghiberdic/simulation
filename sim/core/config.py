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


def load_charter(lab_cfg: Dict[str, Any]) -> str:
    path = CONFIG_DIR / lab_cfg["charter_file"]
    if not path.exists():
        raise FileNotFoundError(f"Charter missing for {lab_cfg['lab']}: {path}")
    return path.read_text()
