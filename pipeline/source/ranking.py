"""Classify candidate stories without network access."""
from __future__ import annotations

import re
from typing import Optional


MIN_WORDS, MAX_WORDS = 100, 3000
_SERIES = re.compile(
    r"\b(?:part|pt\.?|chapter|episode|installment)\s*[:#.-]?\s*"
    r"(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten|final)\b"
    r"|\b(?:final part|series finale)\b|\[(?:series|serial|\d+\s*/\s*\d+)\]", re.I
)
_META = re.compile(
    r"^\s*\[(?:ot|meta|mod post|discussion|announcement)\]"
    r"|\b(?:weekly discussion|weekly prompt|feedback friday|sunday serial|serial sunday|"
    r"welcome to r/|monthly megathread)\b", re.I
)
_MATURE = re.compile(
    r"\b(?:nsf[wl]|18\+|adults? only|mature content|sexual (?:content|violence|assault)|"
    r"explicit (?:content|sex)|graphic (?:violence|gore))(?!\w)", re.I
)


def reject(record: dict) -> Optional[str]:
    """Return a local exclusion reason, or None for a candidate worth scoring."""
    text = record.get("text")
    if (record.get("removed") or not isinstance(text, str) or
            text.strip().casefold() in {"", "[removed]", "[deleted]", "[ removed by reddit ]"} or
            record.get("author") == "[deleted]"):
        return "removed or deleted"
    title = record.get("title")
    if not isinstance(title, str) or not title.strip():
        return "missing title"
    flair = record.get("flair") or ""
    if (record.get("over_18") is True or _MATURE.search(f"{title} {flair}") or
            re.fullmatch(r"\s*(?:mature|explicit|gore)\s*", flair, re.I)):
        return "mature content"
    if _SERIES.search(title) or re.search(r"\b(?:series|serial)\b", flair, re.I):
        return "series"
    if _META.search(title) or re.search(r"\b(?:meta|off.topic|announcement|discussion|mod post)\b", flair, re.I):
        return "meta"
    words = record.get("word_count")
    if type(words) is not int or not MIN_WORDS <= words <= MAX_WORDS:
        return "word count outside window"
    if record.get("over_18") is not False:
        return "unknown maturity flag"
    return None
