"""Select chronological excerpts for a private, offline first narration draft."""
import re

from pipeline.story import sentence_ids, tokenize, validate_story


TURN = re.compile(r"\b(?:but then|instead|until|that was when|only then|turns out|realized|yet)\b", re.I)


def _excerpts(sentences, limit):
    chunks = [s[start:start + limit] for s in sentences for start in range(0, len(s), limit)]
    end = len(chunks) - 1
    turn = next((i for i in range(end // 2, end) if TURN.search(" ".join(chunks[i]))), 2 * end // 3)
    anchors = (0, end // 3, turn, end)
    order = sorted(range(len(chunks)), key=lambda i: min(abs(i - anchor) for anchor in anchors))
    chosen = set()
    words = 0
    for i in order:
        size = len(chunks[i])
        if words + size <= 230:
            chosen.add(i)
            words += size
    if words < 200:
        return []
    passages = []
    for i in sorted(chosen):
        if i - 1 not in chosen:
            passages.append([])
        passages[-1].extend(chunks[i])
    return [{"speaker": "narrator", "text": " ".join(words)} for words in passages]


def draft(record):
    """Return story.json without inventing dialogue or expanding a short source."""
    text = record.get("text") or ""
    if record.get("removed") or record.get("author") == "[deleted]":
        raise ValueError("cannot draft a removed or deleted post")
    text = re.sub(r"!?\[([^\]]*)\]\(https?://[^)\s]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    words = [word for word in tokenize(text) if any(ch.isalnum() for ch in word)]
    if len(words) < 200:
        raise ValueError(f"insufficient source: found {len(words)} spoken words; need at least 200")

    sentences = []
    for word, index in zip(words, sentence_ids(words)):
        if index == len(sentences):
            sentences.append([])
        sentences[-1].append(word)
    lines = _excerpts(sentences, 55) or _excerpts(sentences, 30)
    kind = "reddit" if record.get("subreddit") else record.get("route", "original")
    source = {k: record[k] for k in ("id", "title", "author", "url", "retrieved_at", "route",
                                      "subreddit", "license", "license_url", "attribution",
                                      "obligations", "rights", "permission") if k in record}
    source.update(kind=kind, private_use_only=True, note="Private, internal demo only. Not for publication.")
    slug = re.sub(r"[^a-z0-9]+", "-", (kind + "-" + record["id"]).lower()).strip("-")
    return validate_story({
        "version": 1, "slug": slug, "source": source, "title": record["title"],
        "script": " ".join(line["text"] for line in lines), "lines": lines, "names": [],
        "voice": {"provider": "say", "speakers": {"narrator": {}}},
    })
