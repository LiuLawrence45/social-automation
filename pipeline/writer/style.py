"""The machine-readable style guide: load it, resolve targets for a genre, render it for a prompt."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent


def load(path: Optional[str] = None) -> Dict[str, Any]:
    with open(Path(path) if path else HERE / "style.json", "r", encoding="utf-8") as handle:
        return json.load(handle)


def targets_for(style: Dict[str, Any], genre: str, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Targets with the genre's and the caller's overrides applied."""
    targets = copy.deepcopy(style["targets"])
    extra = style.get("genres", {}).get(genre) or style.get("genres", {}).get("default") or {}
    for key, value in extra.items():
        if key != "rules":
            targets[key] = value
    for key, value in (overrides or {}).items():
        if value is not None:
            targets[key] = value
    targets["words"] = int(round(targets["seconds"] * targets["wpm"] / 60.0))
    return targets


def rules_for(style: Dict[str, Any], genre: str) -> List[str]:
    extra = style.get("genres", {}).get(genre) or style.get("genres", {}).get("default") or {}
    return list(style["rules"]) + list(extra.get("rules", []))


def render(style: Dict[str, Any], genre: str, targets: Dict[str, Any]) -> str:
    """The style guide as the text the writer model reads."""
    low = int(round(targets["words"] * (1 - targets["length_tolerance"])))
    high = int(round(targets["words"] * (1 + targets["length_tolerance"])))
    parts = ["STYLE GUIDE (measured on successful Roblox story shorts)", ""]
    parts += ["%d. %s" % (index + 1, rule) for index, rule in enumerate(rules_for(style, genre))]
    parts += ["", "TARGETS",
              "- Length: %d words (%d to %d), which is %d seconds at %d words a minute." % (targets["words"], low, high, targets["seconds"], targets["wpm"]),
              "- Mean sentence length %s to %s words; at least %d%% of sentences are 5 words or fewer."
              % (targets["sentence_mean_words"][0], targets["sentence_mean_words"][1], int(targets["short_sentence_share_min"] * 100)),
              "- Spoken dialogue is %d%% to %d%% of the words, in at least %d separate spoken lines, none over %d words."
              % (int(targets["dialogue_share"][0] * 100), int(targets["dialogue_share"][1] * 100), targets["dialogue_lines_min"], targets["dialogue_line_max_words"]),
              "- At most %d speakers including the narrator." % targets["speakers_max"]]
    for example in style.get("examples", [])[:1]:
        parts += ["", "EXAMPLE OF THE FORM (%s Do not reuse its content.)" % example["note"]]
        parts += ["%s: %s" % (line["speaker"], line["text"]) for line in example["lines"]]
    return "\n".join(parts)
