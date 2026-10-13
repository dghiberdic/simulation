"""
Checks before a real (paid) run, with no network call: charter values still at
their shipped placeholders (rate_charters.py has not been run), and a missing
key or base URL for any model the run will call (core.llm.preflight).
"""

from typing import Any, Dict, Iterable, List

from core import llm
from core.config import load_lab_configs


def placeholder_labs() -> List[str]:
    """Lab keys whose charter values are still the shipped placeholders."""
    return [c["key"] for c in load_lab_configs()
            if str(c.get("charter_values_source", "")).lower().startswith("placeholder")]


def model_problems(labs, jurors: Dict[str, List[str]], scripted: Iterable[str] = (),
                   uses_macro: bool = True, uses_grand: bool = True) -> List[str]:
    """Missing keys / base URLs for every model this run will call: the seats not in
    `scripted`, the disposition juror (jurors["disposition"], if any), and the Grand
    Jury and MacroJury when the run uses them."""
    scripted = set(scripted)
    models: List[Any] = [lab.model for lab in labs if lab.key not in scripted]
    models += list(jurors.get("disposition") or []) + (list(jurors["grand"]) if uses_grand else [])
    models += list(jurors["macro"]) if uses_macro else []
    providers = {lab.model: lab.provider for lab in labs if getattr(lab, "provider", None)}
    return llm.preflight(dict.fromkeys(models), providers)
