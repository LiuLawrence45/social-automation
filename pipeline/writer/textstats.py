"""Plain-text measures shared by the corpus analysis and the script judge. Standard library only."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

WORD = re.compile(r"[A-Za-z0-9]+(?:['\u2019][A-Za-z]+)*")
ABBREVIATIONS = {"mr.", "mrs.", "ms.", "dr.", "st.", "jr.", "sr.", "vs.", "a.m.", "p.m."}
SENTENCE_END = re.compile(r"[.!?\u2026]+[\"'\u201d\u2019)\]]*$")
FILLER = [
    "little did i know", "little did we know", "needless to say", "it was then that", "at that moment", "in that moment",
    "to be honest", "i couldn't believe my eyes", "i couldn't believe what", "it goes without saying", "for some reason",
    "long story short", "as you can imagine", "i have to admit", "believe it or not", "what happened next", "you won't believe",
    "sent shivers down my spine", "blood ran cold", "a chill ran down", "heart pounding in my chest", "let me tell you",
    "i kid you not", "basically", "literally", "honestly", "obviously", "suddenly", "very", "really", "just then", "kind of", "sort of",
]
GORE = ["blood", "bloody", "gore", "guts", "intestines", "entrails", "severed", "decapitat", "dismember", "mutilat", "corpse",
        "flesh", "disembowel", "stabbed", "slit", "skinned", "gouged", "viscera", "organs", "maggots", "rotting"]


def words(text: str) -> List[str]:
    return WORD.findall(text)


def sentences(text: str) -> List[str]:
    """Split text into sentences on end punctuation, keeping common abbreviations together."""
    out = []
    current = []
    for token in text.split():
        current.append(token)
        if SENTENCE_END.search(token) and token.lower() not in ABBREVIATIONS:
            out.append(" ".join(current))
            current = []
    if current:
        out.append(" ".join(current))
    return out


def syllables(word: str) -> int:
    word = re.sub(r"[^a-z]", "", word.lower())
    if not word:
        return 1
    if len(word) <= 3:
        return 1
    word = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", word)
    word = re.sub(r"^y", "", word)
    return max(1, len(re.findall(r"[aeiouy]{1,2}", word)))


def median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def quantile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = q * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def find_phrases(text: str, phrases: Sequence[str]) -> List[str]:
    lowered = " " + re.sub(r"[^a-z0-9']+", " ", text.lower().replace("\u2019", "'")) + " "
    found = []
    for phrase in phrases:
        hits = lowered.count(" " + phrase + " ")
        found.extend([phrase] * hits)
    return found


def find_stems(text: str, stems: Sequence[str]) -> List[str]:
    tokens = [token.lower() for token in words(text)]
    return [token for token in tokens if any(token.startswith(stem) for stem in stems)]


def measure(text: str) -> Dict[str, Any]:
    """Length, sentence and vocabulary measures for one piece of spoken text."""
    tokens = words(text)
    parts = sentences(text)
    lengths = [len(words(part)) for part in parts if words(part)]
    count = len(tokens)
    syllable_count = sum(syllables(token) for token in tokens)
    long_words = sum(1 for token in tokens if syllables(token) >= 3)
    grade = 0.0
    if count and lengths:
        grade = 0.39 * (count / float(len(lengths))) + 11.8 * (syllable_count / float(count)) - 15.59
    return {
        "words": count,
        "sentences": len(lengths),
        "sentence_mean": round(count / float(len(lengths)), 2) if lengths else 0.0,
        "sentence_median": median(lengths),
        "sentence_p90": round(quantile(lengths, 0.9), 1),
        "sentence_max": max(lengths) if lengths else 0,
        "short_sentence_share": round(sum(1 for value in lengths if value <= 5) / float(len(lengths)), 3) if lengths else 0.0,
        "first_sentence_words": lengths[0] if lengths else 0,
        "last_sentence_words": lengths[-1] if lengths else 0,
        "grade": round(grade, 2),
        "long_word_share": round(long_words / float(count), 4) if count else 0.0,
        "letters_per_word": round(sum(len(token) for token in tokens) / float(count), 2) if count else 0.0,
        "question_share": round(sum(1 for part in parts if part.rstrip("\"'\u201d\u2019)").endswith("?")) / float(len(parts)), 3) if parts else 0.0,
    }


def script_text(lines: Sequence[Dict[str, Any]]) -> str:
    return " ".join(line["text"].strip() for line in lines)


def measure_lines(lines: Sequence[Dict[str, Any]], wpm: float, narrator: str = "narrator") -> Dict[str, Any]:
    """Measures for a script given as speaker lines, with the spoken length at a pace in words a minute."""
    stats = measure(script_text(lines))
    spoken = [line for line in lines if line["speaker"] != narrator]
    spoken_words = [len(words(line["text"])) for line in spoken]
    stats.update({
        "seconds": round(stats["words"] * 60.0 / wpm, 1) if wpm else 0.0,
        "lines": len(lines),
        "speakers": sorted({line["speaker"] for line in lines}),
        "dialogue_lines": len(spoken),
        "dialogue_words": sum(spoken_words),
        "dialogue_share": round(sum(spoken_words) / float(stats["words"]), 3) if stats["words"] else 0.0,
        "dialogue_line_mean": round(sum(spoken_words) / float(len(spoken_words)), 2) if spoken_words else 0.0,
        "dialogue_line_max": max(spoken_words) if spoken_words else 0,
        "first_dialogue_word": first_dialogue_word(lines, narrator),
        "filler": find_phrases(script_text(lines), FILLER),
        "gore": find_stems(script_text(lines), GORE),
    })
    return stats


def first_dialogue_word(lines: Sequence[Dict[str, Any]], narrator: str = "narrator") -> Optional[int]:
    seen = 0
    for line in lines:
        if line["speaker"] != narrator:
            return seen
        seen += len(words(line["text"]))
    return None
