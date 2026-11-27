"""One row per Reddit submission; missing upstream fields stay unknown."""
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def normalize(raw, subreddit, route, retrieved_at):
    key = raw["id"]
    if not re.fullmatch(r"[a-z0-9]+", key):
        raise ValueError("invalid Reddit submission id")
    body = raw.get("selftext") or ""
    removed = bool(raw.get("removed_by_category") or body.strip().lower() in ("[deleted]", "[removed]"))
    if removed:
        body = ""
    return dict(id=key, subreddit=(raw.get("subreddit") or subreddit).lower(),
                title=raw["title"], author=raw.get("author"),
                url="https://www.reddit.com/comments/" + key + "/",
                created_utc=int(raw["created_utc"]) if raw.get("created_utc") is not None else None,
                score=raw.get("score"),
                num_comments=raw.get("num_comments"), flair=raw.get("link_flair_text"),
                word_count=len(body.split()), text=body, over_18=raw.get("over_18"),
                removed=removed, retrieved_at=retrieved_at, route=route, scores=None, brief=None,
                rights="private-only", license="all-rights-reserved", license_url=None,
                attribution=f"{raw['title']} by {raw.get('author') or 'unknown'}",
                obligations=[], note="Private, internal demo only. Not for publication or redistribution.")


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path):
    path = Path(path).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=45)
    db.execute("pragma journal_mode=wal")
    db.executescript("""
        create table if not exists posts (
            id text primary key, subreddit text, created integer,
            popularity integer, record text not null
        );
        create index if not exists by_sub on posts(subreddit, popularity desc, id);
        create index if not exists by_popularity on posts(popularity desc, id);
        create table if not exists cursors (feed text primary key, value text not null);
        create table if not exists inaccessible (id text primary key);
    """)
    return db


def get(db, key):
    row = db.execute("select record from posts where id=?", (key,)).fetchone()
    if not row:
        raise ValueError("unknown story id: " + key)
    return json.loads(row[0])


def save(db, record):
    db.execute("insert or replace into posts values(?,?,?,?,?)",
               (record["id"], record["subreddit"], record["created_utc"], record["score"],
                json.dumps(record, ensure_ascii=False)))


def ingest(db, raw, subreddit, route, retrieved_at):
    record = normalize(raw, subreddit, route, retrieved_at)
    if record["removed"] or record["author"] == "[deleted]":
        forget(db, record["id"])
        return
    upsert(db, record)


def forget(db, key):
    db.execute("delete from posts where id=?", (key,))
    db.execute("insert or ignore into inaccessible values(?)", (key,))


def upsert(db, record):
    route = record["route"]
    if db.execute("select 1 from inaccessible where id=?", (record["id"],)).fetchone():
        return
    row = db.execute("select record from posts where id=?", (record["id"],)).fetchone()
    if row:
        old = json.loads(row[0])
        if old["route"] == "reddit" and route != "reddit":
            return
        if route == old["route"]:
            metadata = ("created_utc", "author", "score", "num_comments", "flair", "over_18")
            old_has_details = any(old[k] is not None for k in metadata)
            new_has_details = any(record[k] is not None for k in metadata)
            if route == "cache" and old_has_details != new_has_details:
                if old_has_details:
                    return
            elif old["retrieved_at"] > record["retrieved_at"]:
                return
        if old["removed"] and route != "reddit":
            return
        if all(old[k] == record[k] for k in ("title", "text", "author", "removed", "over_18", "flair",
                                              "rights", "license", "attribution", "obligations")):
            record.update(brief=old["brief"], scores=old["scores"])
    save(db, record)
