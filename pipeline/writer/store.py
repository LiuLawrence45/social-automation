"""The backlog as one queryable file: DIR/corpus.sqlite, built from the working files beside it.

Tables: shorts (one row per short with counts, performance fields and what the passes found),
snapshots (one row per short per collection date, so performance can be followed over time),
transcripts (text, timed segments, timed words, speaker turns), annotations and voices (the raw
model passes), and transcript_fts for full-text search when SQLite has FTS5.
"""
from __future__ import annotations

import csv
import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import textstats
from .corpus import read_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS shorts (
 id TEXT PRIMARY KEY, url TEXT, title TEXT, channel TEXT, channel_id TEXT, channel_handle TEXT, upload_date TEXT, duration REAL,
 language TEXT, tags TEXT, found_by TEXT, first_collected TEXT, collected TEXT,
 views INTEGER, likes INTEGER, comments INTEGER, channel_followers INTEGER,
 days_live REAL, views_per_day REAL, views_per_follower REAL, likes_per_view REAL,
 channel_listed INTEGER, channel_median_views REAL, views_over_median REAL, channel_pct REAL,
 words INTEGER, wpm REAL, is_story INTEGER, format TEXT, genre TEXT, scary INTEGER, person TEXT, tense TEXT, hook TEXT, ending TEXT,
 loops INTEGER, dialogue_share REAL, speech_lines INTEGER, voices INTEGER);
CREATE TABLE IF NOT EXISTS snapshots (id TEXT, collected TEXT, views INTEGER, likes INTEGER, comments INTEGER, channel_followers INTEGER,
 PRIMARY KEY (id, collected));
CREATE TABLE IF NOT EXISTS transcripts (id TEXT PRIMARY KEY, model TEXT, language TEXT, duration REAL, text TEXT, segments TEXT, words TEXT, turns TEXT);
CREATE TABLE IF NOT EXISTS annotations (id TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS voices (id TEXT PRIMARY KEY, data TEXT);
"""
SHORT_COLUMNS = ["id", "url", "title", "channel", "channel_id", "channel_handle", "upload_date", "duration", "language", "tags", "found_by",
                 "first_collected", "collected", "views", "likes", "comments", "channel_followers", "days_live", "views_per_day",
                 "views_per_follower", "likes_per_view", "channel_listed", "channel_median_views", "views_over_median", "channel_pct",
                 "words", "wpm", "is_story", "format", "genre", "scary", "person", "tense", "hook", "ending", "loops", "dialogue_share",
                 "speech_lines", "voices"]
SORTS = {"views": "views", "views_per_day": "views_per_day", "views_over_median": "views_over_median",
         "views_per_follower": "views_per_follower", "upload_date": "upload_date"}


def sentence_times(transcript: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every sentence of the transcript with its start and end time, from the word timings."""
    words = transcript.get("words") or []
    out = []
    cursor = 0
    for index, sentence in enumerate(textstats.sentences(transcript.get("text") or "")):
        count = len(sentence.split())
        span = words[cursor:cursor + count]
        out.append({"s": index, "text": sentence, "start": span[0]["start"] if span else None, "end": span[-1]["end"] if span else None,
                    "words": len(textstats.words(sentence))})
        cursor += count
    return out


def speech_words(annotation: Optional[Dict[str, Any]]) -> int:
    return sum(len(textstats.words(str(item.get("words") or ""))) for item in (annotation or {}).get("speech") or [] if isinstance(item, dict))


def speaker_turns(transcript: Dict[str, Any], annotation: Optional[Dict[str, Any]], voices: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Speaker turns: sentences grouped by who speaks them. The speaker comes from the text pass (direct speech
    and its speaker), the voice id from the audio pass when it ran; a sentence with no direct speech is the narrator's."""
    speech = {}
    for item in (annotation or {}).get("speech") or []:
        if isinstance(item, dict) and isinstance(item.get("s"), int):
            speech.setdefault(item["s"], []).append(item)
    voice_of = {}
    for item in (voices or {}).get("turns") or []:
        if isinstance(item, dict) and isinstance(item.get("s"), int):
            voice_of[item["s"]] = str(item.get("voice"))
    turns = []
    for sentence in sentence_times(transcript):
        said = speech.get(sentence["s"], [])
        speaker = str(said[0].get("speaker") or "character") if said else "narrator"
        voice = voice_of.get(sentence["s"], "V1") if voices else None
        if turns and turns[-1]["speaker"] == speaker and turns[-1]["voice"] == voice:
            turns[-1]["text"] += " " + sentence["text"]
            turns[-1]["end"] = sentence["end"]
        else:
            turns.append({"speaker": speaker, "voice": voice, "start": sentence["start"], "end": sentence["end"], "text": sentence["text"]})
        if said:
            turns[-1].setdefault("spoken", []).extend(str(item.get("words") or "") for item in said)
    return turns


def _days(upload: Optional[str], collected: Optional[str]) -> Optional[float]:
    try:
        first = datetime.datetime.strptime(str(upload), "%Y%m%d")
        last = datetime.datetime.strptime(str(collected), "%Y-%m-%d")
    except ValueError:
        return None
    return max(1.0, (last - first).days + 0.0)


def _ratio(top: Any, bottom: Any, digits: int = 4) -> Optional[float]:
    if top is None or not bottom:
        return None
    return round(float(top) / float(bottom), digits)


def short_row(meta: Dict[str, Any], transcript: Optional[Dict[str, Any]], annotation: Optional[Dict[str, Any]],
              voices: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """One shorts row: the counts, the fields that make shorts comparable, and what the passes found."""
    annotation = annotation or {}
    days = _days(meta.get("upload_date"), meta.get("retrieved"))
    words = len(textstats.words(transcript["text"])) if transcript else None
    spoken = None
    if transcript and transcript.get("words"):
        spoken = transcript["words"][-1]["end"] - transcript["words"][0]["start"]
    said = speech_words(annotation) if annotation.get("is_story") else None
    return {
        "id": meta["id"], "url": meta.get("url"), "title": meta.get("title"), "channel": meta.get("channel"), "channel_id": meta.get("channel_id"),
        "channel_handle": meta.get("channel_handle"), "upload_date": meta.get("upload_date"), "duration": meta.get("duration"),
        "language": (transcript or {}).get("language") or meta.get("language"), "tags": json.dumps(meta.get("tags") or [], ensure_ascii=False),
        "found_by": meta.get("found_by"), "first_collected": meta.get("first_collected") or meta.get("retrieved"), "collected": meta.get("retrieved"),
        "views": meta.get("view_count"), "likes": meta.get("like_count"), "comments": meta.get("comment_count"),
        "channel_followers": meta.get("channel_followers"), "days_live": days, "views_per_day": _ratio(meta.get("view_count"), days, 1),
        "views_per_follower": _ratio(meta.get("view_count"), meta.get("channel_followers"), 3),
        "likes_per_view": _ratio(meta.get("like_count"), meta.get("view_count"), 5),
        "channel_listed": meta.get("channel_listed"), "channel_median_views": meta.get("channel_median_views"),
        "views_over_median": meta.get("views_over_median"), "channel_pct": meta.get("channel_pct"),
        "words": words, "wpm": round(words * 60.0 / spoken, 1) if words and spoken and spoken > 5 else None,
        "is_story": None if not annotation else int(bool(annotation.get("is_story"))), "format": annotation.get("format"),
        "genre": annotation.get("genre"), "scary": None if annotation.get("scary") is None else int(bool(annotation.get("scary"))),
        "person": annotation.get("person"), "tense": annotation.get("tense"), "hook": annotation.get("hook"), "ending": annotation.get("ending"),
        "loops": None if annotation.get("loops") is None else int(bool(annotation.get("loops"))),
        "dialogue_share": _ratio(said, words, 3) if said is not None else None,
        "speech_lines": len(annotation.get("speech") or []) if annotation.get("is_story") else None,
        "voices": len(voices.get("voices") or []) if voices else None,
    }


def _load(path: Path) -> Optional[Dict[str, Any]]:
    return read_json(path) if path.exists() else None


def connect(corpus: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(corpus / "corpus.sqlite"))
    connection.row_factory = sqlite3.Row
    return connection


def build(corpus: Path) -> Dict[str, int]:
    """Rebuild corpus.sqlite from the working files. Snapshots already in the file are kept."""
    connection = connect(corpus)
    connection.executescript(SCHEMA)
    for table in ("shorts", "transcripts", "annotations", "voices"):
        connection.execute("DELETE FROM %s" % table)
    try:
        connection.execute("DROP TABLE IF EXISTS transcript_fts")
        connection.execute("CREATE VIRTUAL TABLE transcript_fts USING fts5(id UNINDEXED, title, text)")
        fts = True
    except sqlite3.OperationalError:
        fts = False
    counts = {"shorts": 0, "transcripts": 0, "annotated": 0, "stories": 0, "voices": 0}
    for path in sorted((corpus / "meta").glob("*.json")):
        meta = read_json(path)
        transcript = _load(corpus / "transcripts" / path.name)
        annotation = _load(corpus / "annotations" / path.name)
        voices = _load(corpus / "voices" / path.name)
        row = short_row(meta, transcript, annotation, voices)
        connection.execute("INSERT INTO shorts (%s) VALUES (%s)" % (", ".join(SHORT_COLUMNS), ", ".join("?" for _ in SHORT_COLUMNS)),
                           [row[column] for column in SHORT_COLUMNS])
        counts["shorts"] += 1
        history = meta.get("history") or [{"collected": meta.get("retrieved"), "view_count": meta.get("view_count"), "like_count": meta.get("like_count"),
                                           "comment_count": meta.get("comment_count"), "channel_followers": meta.get("channel_followers")}]
        for item in history:
            connection.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?, ?)",
                               (meta["id"], item.get("collected"), item.get("view_count"), item.get("like_count"), item.get("comment_count"),
                                item.get("channel_followers")))
        if transcript:
            connection.execute("INSERT INTO transcripts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (meta["id"], transcript.get("model"), transcript.get("language"), transcript.get("duration"), transcript.get("text"),
                                json.dumps(transcript.get("segments") or [], ensure_ascii=False), json.dumps(transcript.get("words") or [], ensure_ascii=False),
                                json.dumps(speaker_turns(transcript, annotation, voices), ensure_ascii=False)))
            counts["transcripts"] += 1
            if fts:
                connection.execute("INSERT INTO transcript_fts VALUES (?, ?, ?)", (meta["id"], meta.get("title") or "", transcript.get("text") or ""))
        if annotation:
            connection.execute("INSERT INTO annotations VALUES (?, ?)", (meta["id"], json.dumps(annotation, ensure_ascii=False)))
            counts["annotated"] += 1
            counts["stories"] += int(bool(annotation.get("is_story")))
        if voices:
            connection.execute("INSERT INTO voices VALUES (?, ?)", (meta["id"], json.dumps(voices, ensure_ascii=False)))
            counts["voices"] += 1
    counts["snapshots"] = connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    connection.commit()
    connection.close()
    return counts


def _where(channel: Optional[str], genre: Optional[str], stories: bool, min_views: Optional[int]) -> Any:
    clauses, values = [], []
    if channel:
        clauses.append("(shorts.channel LIKE ? OR shorts.channel_handle LIKE ? OR shorts.channel_id = ?)")
        values += ["%" + channel + "%", "%" + channel.lstrip("@") + "%", channel]
    if genre:
        clauses.append("(shorts.genre = ? OR (? = 'scary' AND shorts.scary = 1))")
        values += [genre, genre]
    if stories:
        clauses.append("shorts.is_story = 1")
    if min_views:
        clauses.append("shorts.views >= ?")
        values.append(min_views)
    return clauses, values


def search(corpus: Path, words: str = "", channel: Optional[str] = None, genre: Optional[str] = None, stories: bool = False,
           min_views: Optional[int] = None, sort: str = "views", limit: int = 20) -> List[Dict[str, Any]]:
    """Shorts matching the words (in the title or the transcript) and the filters, best first by the sort field."""
    connection = connect(corpus)
    clauses, values = _where(channel, genre, stories, min_views)
    join = "LEFT JOIN transcripts ON transcripts.id = shorts.id"
    if words.strip():
        has_fts = connection.execute("SELECT name FROM sqlite_master WHERE name = 'transcript_fts'").fetchone() is not None
        if has_fts:
            clauses.append("shorts.id IN (SELECT id FROM transcript_fts WHERE transcript_fts MATCH ?)")
            values.append(" ".join('"%s"' % word.replace('"', "") for word in words.split()))
        else:
            for word in words.split():
                clauses.append("(shorts.title LIKE ? OR transcripts.text LIKE ?)")
                values += ["%" + word + "%", "%" + word + "%"]
    query = "SELECT shorts.*, transcripts.text AS transcript FROM shorts %s %s ORDER BY shorts.%s DESC LIMIT ?" % (
        join, ("WHERE " + " AND ".join(clauses)) if clauses else "", SORTS.get(sort, "views"))
    rows = [dict(row) for row in connection.execute(query, values + [limit])]
    connection.close()
    return rows


def print_rows(rows: List[Dict[str, Any]], with_text: bool = False) -> None:
    for row in rows:
        print("%s  %-14s %10s views  x%-6s of channel median  %5s/day  %3ss  %-10s %s"
              % (row["id"], (row["channel"] or "")[:14], row["views"], row["views_over_median"], int(row["views_per_day"] or 0),
                 int(row["duration"] or 0), row["genre"] or "-", (row["title"] or "")[:60]))
        if with_text and row.get("transcript"):
            print("    " + row["transcript"][:400])
    print("%d shorts" % len(rows))


def export(corpus: Path, out: Path, channel: Optional[str] = None, genre: Optional[str] = None, stories: bool = False,
           min_views: Optional[int] = None, transcripts: bool = True) -> int:
    """Write the matching shorts to a .jsonl file (with transcripts, turns, annotations and snapshots) or a .csv (the shorts table)."""
    connection = connect(corpus)
    clauses, values = _where(channel, genre, stories, min_views)
    rows = [dict(row) for row in connection.execute("SELECT * FROM shorts %s ORDER BY views DESC" % (("WHERE " + " AND ".join(clauses)) if clauses else ""), values)]
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() == ".csv":
        with open(out, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SHORT_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
    else:
        with open(out, "w", encoding="utf-8") as handle:
            for row in rows:
                row["tags"] = json.loads(row["tags"] or "[]")
                row["snapshots"] = [dict(item) for item in connection.execute(
                    "SELECT collected, views, likes, comments, channel_followers FROM snapshots WHERE id = ? ORDER BY collected", (row["id"],))]
                annotation = connection.execute("SELECT data FROM annotations WHERE id = ?", (row["id"],)).fetchone()
                row["annotation"] = json.loads(annotation[0]) if annotation else None
                voices = connection.execute("SELECT data FROM voices WHERE id = ?", (row["id"],)).fetchone()
                row["voice_pass"] = json.loads(voices[0]) if voices else None
                if transcripts:
                    item = connection.execute("SELECT text, segments, turns FROM transcripts WHERE id = ?", (row["id"],)).fetchone()
                    row["transcript"] = {"text": item[0], "segments": json.loads(item[1]), "turns": json.loads(item[2])} if item else None
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    connection.close()
    return len(rows)
