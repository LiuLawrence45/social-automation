"""Corpus study: measure every story short, then compare genres and the top quarter against the rest.

Usage:
  python -m pipeline.writer analyze --corpus DIR [--out stats.json] [--features features.jsonl] [--examples examples.md]

Reads DIR/meta, transcripts, annotations and voices. Writes the derived statistics (no transcript
text) to --out; these are what the style guide's numbers come from and are safe to commit.
--features writes one row of measures per short, and --examples writes quoted hooks, spoken lines
and last lines for the person writing the style guide; both hold other people's words and stay out
of the repository.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import store, textstats
from .corpus import read_json

NUMERIC = ["seconds", "words", "wpm", "sentences", "sentence_mean", "sentence_median", "sentence_p90", "sentence_max", "short_sentence_share",
           "first_sentence_words", "premise_words", "premise_seconds", "last_sentence_words", "grade", "long_word_share", "letters_per_word",
           "question_share", "beats", "turn_position", "dialogue_share", "speech_lines", "speech_per_100_words", "speech_line_words",
           "speech_line_max", "first_speech_position", "speakers", "named", "voices", "other_voice_share", "title_words"]
CATEGORICAL = ["genre", "format", "person", "tense", "hook", "ending", "opener", "last_line_spoken", "loops", "viewer_address", "scary",
               "has_speech", "multi_voice", "narrator_synthetic", "music", "sound_effects", "gore", "title_emoji", "title_opener"]
STOP = set("a an the and or but so to of in on at for with from by is was were be been it its i me my we you he she they them his her our your this that "
           "as not no do did had has have just then there when what who all one out up if would could".split())
TOP_SHARE = 0.75


def _cumulative_words(sentences: Sequence[str], upto: int) -> int:
    return sum(len(textstats.words(sentence)) for sentence in sentences[:upto + 1])


def features(meta: Dict[str, Any], transcript: Dict[str, Any], annotation: Dict[str, Any], voices: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Every measure for one story short."""
    text = transcript["text"]
    sentences = textstats.sentences(text)
    timed = store.sentence_times(transcript)
    stats = textstats.measure(text)
    total = stats["words"]
    words = transcript.get("words") or []
    spoken_seconds = (words[-1]["end"] - words[0]["start"]) if words else 0.0
    row = dict(stats)
    row.update({"id": meta["id"], "channel": meta.get("channel"), "title": meta.get("title"), "seconds": meta.get("duration"),
                "wpm": round(total * 60.0 / spoken_seconds, 1) if spoken_seconds > 5 else None,
                "views": meta.get("view_count"), "channel_pct": meta.get("channel_pct"), "views_over_median": meta.get("views_over_median"),
                "upload_date": meta.get("upload_date")})
    for key in ("genre", "format", "person", "tense", "hook", "ending"):
        row[key] = annotation.get(key)
    for key in ("scary", "loops", "viewer_address"):
        row[key] = bool(annotation.get(key))
    row["gore"] = annotation.get("gore")
    premise = annotation.get("premise_sentence")
    if isinstance(premise, int) and 0 <= premise < len(sentences):
        row["premise_words"] = _cumulative_words(sentences, premise)
        row["premise_seconds"] = timed[premise]["end"]
    turn = annotation.get("turn_sentence")
    if isinstance(turn, int) and 0 < turn < len(sentences) and total:
        row["turn_position"] = round(_cumulative_words(sentences, turn - 1) / float(total), 3)
    beats = [beat for beat in annotation.get("beats") or [] if isinstance(beat, dict) and beat.get("label")]
    row["beats"] = len(beats) or None
    row["beat_labels"] = [beat["label"] for beat in beats]
    speech = [item for item in annotation.get("speech") or [] if isinstance(item, dict) and str(item.get("words") or "").strip()]
    lengths = [len(textstats.words(str(item["words"]))) for item in speech]
    row["has_speech"] = bool(speech)
    row["speech_lines"] = len(speech)
    row["dialogue_share"] = round(min(1.0, sum(lengths) / float(total)), 3) if total else 0.0
    row["speech_per_100_words"] = round(100.0 * len(speech) / total, 2) if total else 0.0
    row["speech_line_words"] = round(sum(lengths) / float(len(lengths)), 2) if lengths else None
    row["speech_line_max"] = max(lengths) if lengths else None
    row["speech_line_lengths"] = lengths
    row["speech_intros"] = [item.get("intro") for item in speech if item.get("intro") in ("before", "after", "none")]
    row["speakers"] = len({str(item.get("speaker")) for item in speech}) if speech else 0
    first = min((item["s"] for item in speech if isinstance(item.get("s"), int)), default=None)
    if first is not None and 0 <= first < len(sentences) and total:
        row["first_speech_position"] = round(_cumulative_words(sentences, first - 1) / float(total), 3) if first else 0.0
    last_index = len(sentences) - 1
    row["last_line_spoken"] = any(item.get("s") == last_index for item in speech)
    row["named"] = len(annotation.get("named") or [])
    row["devices"] = [str(device).lower() for device in annotation.get("devices") or []][:4]
    opener = " ".join(word.lower() for word in textstats.words(text)[:3])
    row["opener"] = opener if opener in ("in this world", "in this town", "in this city", "in this school", "in my town", "in our town") else (
        "in this/my ..." if opener.startswith(("in this", "in my", "in our")) else "other")
    title = re.sub(r"#\S+", "", meta.get("title") or "").strip()
    title_words = textstats.words(title)
    row["title_words"] = len(title_words)
    row["title_emoji"] = bool(re.search(r"[\U0001F000-\U0001FAFF☀-➿]", meta.get("title") or ""))
    row["title_opener"] = " ".join(word.lower() for word in title_words[:2]) if len(title_words) >= 2 else "other"
    if voices:
        heard = voices.get("voices") or []
        turns = [item for item in voices.get("turns") or [] if isinstance(item, dict)]
        row["voices"] = len(heard)
        row["multi_voice"] = len(heard) > 1
        row["other_voice_share"] = round(len(turns) / float(len(sentences)), 3) if sentences else 0.0
        row["narrator_synthetic"] = bool(heard[0].get("synthetic")) if heard else None
        row["music"] = bool(voices.get("music"))
        row["sound_effects"] = voices.get("sound_effects")
        spoken_sentences = {item["s"] for item in speech if isinstance(item.get("s"), int)}
        voiced = {item.get("s") for item in turns}
        row["speech_in_other_voice"] = round(len(spoken_sentences & voiced) / float(len(spoken_sentences)), 3) if spoken_sentences else None
    return row


def _numbers(rows: Sequence[Dict[str, Any]], key: str) -> List[float]:
    return [float(row[key]) for row in rows if isinstance(row.get(key), (int, float)) and not isinstance(row.get(key), bool)]


def describe(values: Sequence[float]) -> Optional[Dict[str, Any]]:
    if not values:
        return None
    return {"n": len(values), "median": round(textstats.median(values), 3), "mean": round(sum(values) / len(values), 3),
            "p25": round(textstats.quantile(values, 0.25), 3), "p75": round(textstats.quantile(values, 0.75), 3),
            "p10": round(textstats.quantile(values, 0.1), 3), "p90": round(textstats.quantile(values, 0.9), 3)}


def shares(rows: Sequence[Dict[str, Any]], key: str, limit: int = 12) -> Dict[str, Any]:
    values = [row.get(key) for row in rows if row.get(key) is not None]
    counts = collections.Counter(str(value) for value in values)
    return {"n": len(values), "values": {name: {"count": count, "share": round(count / float(len(values)), 3)} for name, count in counts.most_common(limit)}}


def summarise(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    out = {"n": len(rows), "numeric": {}, "categorical": {}}
    for key in NUMERIC:
        summary = describe(_numbers(rows, key))
        if summary:
            out["numeric"][key] = summary
    for key in CATEGORICAL:
        out["categorical"][key] = shares(rows, key)
    lengths = [value for row in rows for value in row.get("speech_line_lengths") or []]
    out["speech_line_length"] = describe([float(value) for value in lengths])
    intros = collections.Counter(value for row in rows for value in row.get("speech_intros") or [])
    out["speech_intro"] = {name: round(count / float(sum(intros.values())), 3) for name, count in intros.most_common()} if intros else {}
    devices = collections.Counter(device for row in rows for device in row.get("devices") or [])
    out["devices"] = dict(devices.most_common(15))
    sequences = collections.Counter(" > ".join(row.get("beat_labels") or []) for row in rows if row.get("beat_labels"))
    out["beat_sequences"] = dict(sequences.most_common(8))
    beat_use = collections.Counter(label for row in rows for label in set(row.get("beat_labels") or []))
    out["beat_use"] = {label: round(count / float(len(rows)), 3) for label, count in beat_use.most_common()} if rows else {}
    voiced = [row for row in rows if row.get("speech_in_other_voice") is not None]
    out["speech_in_other_voice"] = describe([row["speech_in_other_voice"] for row in voiced])
    return out


def rank_test(first: Sequence[float], second: Sequence[float]) -> Optional[Dict[str, Any]]:
    """Mann-Whitney comparison: auc is the chance a value from the first group beats one from the second; p is two-sided (normal approximation)."""
    if len(first) < 8 or len(second) < 8:
        return None
    pooled = sorted([(value, 0) for value in first] + [(value, 1) for value in second])
    ranks = [0.0] * len(pooled)
    index = 0
    ties = 0.0
    while index < len(pooled):
        end = index
        while end + 1 < len(pooled) and pooled[end + 1][0] == pooled[index][0]:
            end += 1
        for position in range(index, end + 1):
            ranks[position] = (index + end) / 2.0 + 1
        size = end - index + 1
        ties += size ** 3 - size
        index = end + 1
    n1, n2 = len(first), len(second)
    rank_sum = sum(rank for rank, item in zip(ranks, pooled) if item[1] == 0)
    u = rank_sum - n1 * (n1 + 1) / 2.0
    total = n1 + n2
    variance = n1 * n2 / 12.0 * ((total + 1) - ties / float(total * (total - 1)))
    z = (u - n1 * n2 / 2.0) / math.sqrt(variance) if variance > 0 else 0.0
    return {"auc": round(u / (n1 * n2), 3), "p": round(math.erfc(abs(z) / math.sqrt(2)), 4)}


def share_test(hits_a: int, n_a: int, hits_b: int, n_b: int) -> Optional[float]:
    if n_a < 8 or n_b < 8:
        return None
    pooled = (hits_a + hits_b) / float(n_a + n_b)
    error = math.sqrt(pooled * (1 - pooled) * (1.0 / n_a + 1.0 / n_b))
    if error == 0:
        return 1.0
    return round(math.erfc(abs(hits_a / float(n_a) - hits_b / float(n_b)) / error / math.sqrt(2)), 4)


def compare(top: Sequence[Dict[str, Any]], rest: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Top quarter against the rest, measure by measure, with a p-value for each difference."""
    out = {"n_top": len(top), "n_rest": len(rest), "numeric": {}, "categorical": {}}
    for key in NUMERIC:
        a, b = _numbers(top, key), _numbers(rest, key)
        test = rank_test(a, b)
        if test:
            out["numeric"][key] = {"top_median": round(textstats.median(a), 3), "rest_median": round(textstats.median(b), 3), "auc": test["auc"], "p": test["p"]}
    for key in CATEGORICAL:
        a = [str(row.get(key)) for row in top if row.get(key) is not None]
        b = [str(row.get(key)) for row in rest if row.get(key) is not None]
        for value, count in collections.Counter(a + b).most_common(6):
            if count < 8:
                continue
            p = share_test(a.count(value), len(a), b.count(value), len(b))
            if p is not None:
                out["categorical"]["%s=%s" % (key, value)] = {"top_share": round(a.count(value) / float(len(a)), 3),
                                                              "rest_share": round(b.count(value) / float(len(b)), 3), "p": p}
    out["differs"] = sorted([key for key, value in out["numeric"].items() if value["p"] < 0.05] +
                            [key for key, value in out["categorical"].items() if value["p"] < 0.05])
    return out


def phrases(texts: Sequence[str], sizes: Sequence[int] = (2, 3, 4), limit: int = 40) -> List[Dict[str, Any]]:
    """Word sequences that recur across many scripts (counted once per script), most widespread first."""
    spread = collections.Counter()
    for text in texts:
        tokens = [token.lower().replace("’", "'") for token in textstats.words(text)]
        seen = set()
        for size in sizes:
            for index in range(len(tokens) - size + 1):
                gram = tokens[index:index + size]
                if sum(1 for token in gram if token not in STOP) >= max(1, size - 2) and gram[0] not in ("the", "a", "of") and gram[-1] not in ("the", "a", "of", "and", "to"):
                    seen.add(" ".join(gram))
        spread.update(seen)
    floor = max(3, int(len(texts) * 0.04))
    kept = [(gram, count) for gram, count in spread.most_common(400) if count >= floor]
    out = []
    for gram, count in kept:
        if any(gram in other and gram != other and other_count >= count * 0.8 for other, other_count in kept):
            continue
        out.append({"phrase": gram, "scripts": count, "share": round(count / float(len(texts)), 3)})
    return out[:limit]


def load_rows(corpus: Path) -> Dict[str, Any]:
    rows, texts = [], {}
    tally = collections.Counter()
    for path in sorted((corpus / "meta").glob("*.json")):
        tally["shorts"] += 1
        transcript_path = corpus / "transcripts" / path.name
        annotation_path = corpus / "annotations" / path.name
        if not transcript_path.exists():
            continue
        tally["transcribed"] += 1
        if not annotation_path.exists():
            continue
        annotation = read_json(annotation_path)
        tally["annotated"] += 1
        if not annotation.get("is_story"):
            continue
        transcript = read_json(transcript_path)
        voices_path = corpus / "voices" / path.name
        row = features(read_json(path), transcript, annotation, read_json(voices_path) if voices_path.exists() else None)
        rows.append(row)
        texts[row["id"]] = transcript["text"]
    tally["stories"] = len(rows)
    return {"rows": rows, "texts": texts, "tally": dict(tally)}


def study(rows: Sequence[Dict[str, Any]], texts: Dict[str, str], tally: Dict[str, int]) -> Dict[str, Any]:
    ranked = [row for row in rows if row.get("channel_pct") is not None]
    top = [row for row in ranked if row["channel_pct"] >= TOP_SHARE]
    rest = [row for row in ranked if row["channel_pct"] < TOP_SHARE]
    scary = [row for row in rows if row["scary"]]
    out = {
        "version": 1,
        "note": "Derived statistics from the shorts backlog. No transcript text. Top quarter means a short's views beat at least 75% of its own channel's listed shorts.",
        "corpus": dict(tally, channels=len({row["channel"] for row in rows}), ranked=len(ranked)),
        "all": summarise(rows),
        "by_genre": {genre: summarise([row for row in rows if row["genre"] == genre])
                     for genre, count in collections.Counter(row["genre"] for row in rows).most_common() if count >= 8},
        "scary": summarise(scary),
        "not_scary": summarise([row for row in rows if not row["scary"]]),
        "top_quarter": summarise(top),
        "rest": summarise(rest),
        "top_vs_rest": compare(top, rest),
        "top_vs_rest_scary": compare([row for row in top if row["scary"]], [row for row in rest if row["scary"]]),
        "scary_vs_not": compare(scary, [row for row in rows if not row["scary"]]),
        "by_channel": {channel: {"n": count, "median_words": textstats.median(_numbers([row for row in rows if row["channel"] == channel], "words")),
                                 "median_seconds": textstats.median(_numbers([row for row in rows if row["channel"] == channel], "seconds")),
                                 "median_dialogue_share": textstats.median(_numbers([row for row in rows if row["channel"] == channel], "dialogue_share")),
                                 "scary_share": round(sum(1 for row in rows if row["channel"] == channel and row["scary"]) / float(count), 3)}
                       for channel, count in collections.Counter(row["channel"] for row in rows).most_common(20) if count >= 5},
        "recurring_phrases": phrases([texts[row["id"]] for row in rows]),
        "recurring_phrases_scary": phrases([texts[row["id"]] for row in scary]),
        "recurring_openers": {opener: count for opener, count in collections.Counter(
            " ".join(word.lower() for word in textstats.words(texts[row["id"]])[:3]) for row in rows).most_common(15) if count >= 3},
        "recurring_title_openers": {opener: count for opener, count in collections.Counter(row["title_opener"] for row in rows).most_common(15) if count >= 3},
    }
    return out


def write_examples(rows: Sequence[Dict[str, Any]], corpus: Path, out: Path) -> None:
    """Quoted hooks, spoken lines and last lines of the best-ranked shorts, for the style guide's author. Job data."""
    lines = ["# Examples from the backlog (other people's words; not for the repository)", ""]
    ordered = sorted([row for row in rows if row.get("channel_pct") is not None], key=lambda row: -(row.get("views") or 0))
    for label, group in (("Scary", [row for row in ordered if row["scary"]][:25]), ("Not scary", [row for row in ordered if not row["scary"]][:25])):
        lines += ["## %s" % label, ""]
        for row in group:
            transcript = read_json(corpus / "transcripts" / ("%s.json" % row["id"]))
            annotation = read_json(corpus / "annotations" / ("%s.json" % row["id"]))
            sentences = textstats.sentences(transcript["text"])
            lines.append("### %s, %s (%s views, x%s of channel median, %s words, %ss, hook %s, ending %s)"
                         % (row["channel"], row["title"], row["views"], row["views_over_median"], row["words"], row["seconds"], row["hook"], row["ending"]))
            lines.append("- First: " + " ".join(sentences[:3]))
            for item in (annotation.get("speech") or [])[:6]:
                lines.append("- Spoken (%s, intro %s): %s" % (item.get("speaker"), item.get("intro"), item.get("words")))
            lines.append("- Last: " + " ".join(sentences[-2:]))
            lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.writer analyze", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--out")
    parser.add_argument("--features")
    parser.add_argument("--examples")
    args = parser.parse_args(argv)
    corpus = Path(args.corpus)
    loaded = load_rows(corpus)
    if not loaded["rows"]:
        print("error: no annotated story shorts in %s" % corpus, file=sys.stderr)
        return 2
    result = study(loaded["rows"], loaded["texts"], loaded["tally"])
    out = Path(args.out) if args.out else corpus / "style_stats.json"
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=1)
    if args.features:
        with open(args.features, "w", encoding="utf-8") as handle:
            for row in loaded["rows"]:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    if args.examples:
        write_examples(loaded["rows"], corpus, Path(args.examples))
    numeric = result["all"]["numeric"]
    print("%d story shorts of %d collected (%d scary); %d ranked within channel, %d in the top quarter"
          % (len(loaded["rows"]), loaded["tally"]["shorts"], result["scary"]["n"], result["corpus"]["ranked"], result["top_vs_rest"]["n_top"]))
    for key in ("seconds", "words", "wpm", "sentence_mean", "first_sentence_words", "premise_words", "turn_position", "dialogue_share", "speech_lines", "speech_line_words"):
        if key in numeric:
            print("  %-22s median %-8s (p25 %s, p75 %s)" % (key, numeric[key]["median"], numeric[key]["p25"], numeric[key]["p75"]))
    print("  top quarter differs on: %s" % (", ".join(result["top_vs_rest"]["differs"]) or "nothing at p < 0.05"))
    print("wrote %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
