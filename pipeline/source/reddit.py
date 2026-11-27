"""Import a local Reddit snapshot or resume an authorized feed."""
import csv
import json
import os
import re
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

from . import collect, store


def _load(db, feed):
    row = db.execute("select value from cursors where feed=?", (feed,)).fetchone()
    return json.loads(row[0]) if row else {}


def _save(db, feed, state):
    db.execute("insert or replace into cursors values (?,?)", (feed, json.dumps(state)))


_NOSLEEP_LINK = re.compile(r"https?://(?:(?:www|old)\.)?reddit\.com/r/nosleep/comments/"
                          r"([a-z0-9]+)(?:/[^/?#]*)?/?(?:[?#].*)?", re.I)


def _csv_rows(reader):
    if reader.fieldnames == ["", "title", "post", "link"]:
        for row in reader:
            match = _NOSLEEP_LINK.fullmatch(row["link"] or "")
            yield (dict(id=match[1].lower(), title=row["title"], selftext=row["post"],
                        subreddit="nosleep") if match else None)
        return
    required = {"title", "score", "id", "subreddit", "num_comments", "body", "created"}
    if not required.issubset(reader.fieldnames or ()):
        raise ValueError("Unexpected Reddit CSV fields")
    for row in reader:
        if not row["subreddit"]:
            raise ValueError("Missing subreddit in CSV")
        yield dict(id=row["id"], title=row["title"], subreddit=row["subreddit"],
                   selftext=row["body"], score=int(row["score"]) if row["score"] else None,
                   num_comments=int(row["num_comments"]) if row["num_comments"] else None,
                   created_utc=int(float(row["created"])) if row["created"] else None)


def fetch(db, args):
    """A page budget applies per subreddit, or to batches of 100 cache records."""
    route = args.route
    if route not in {"archive", "reddit", "cache"} or args.pages < 0:
        raise ValueError("Invalid Reddit route or page budget")
    if route == "reddit" and not args.approved:
        raise ValueError("Live Reddit collection requires Reddit approval for the proposed use")
    if route == "archive" and not args.approved:
        raise ValueError("Live archive collection requires approval for this third-party source and use")
    if args.refresh and route != "reddit":
        raise ValueError("Score refresh requires the approved Reddit OAuth route")
    subs = sorted({collect._subreddit(sub).lower() for sub in args.subreddits})
    if not subs:
        raise ValueError("Select at least one subreddit")
    report = dict(pages=0, processed=0, refreshed=0, missing=0)
    if route == "cache":
        if not args.input:
            raise ValueError("Provide the path to a Reddit cache")
        path = Path(args.input).expanduser().resolve()
        with path.open(encoding="utf-8-sig", newline="") as file:
            stat = os.fstat(file.fileno())
            stamp = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds")
            feed = "cache:" + json.dumps([str(path), subs])
            state = _load(db, feed)
            version = [stat.st_size, stat.st_mtime_ns]
            if state.get("version") != version:
                state = {}
            offset = state.get("offset", 0)
            if path.suffix.lower() == ".csv":
                csv.field_size_limit(4 * 1024 * 1024)
                reader = csv.DictReader(iter(file.readline, ""))
                if not reader.fieldnames:
                    raise ValueError("Missing CSV header")
                rows = _csv_rows(reader)
                if "position" in state:
                    file.seek(state["position"])
                else:
                    for _ in islice(rows, offset):
                        pass
            else:
                data = json.load(file)
                if not isinstance(data, list):
                    raise ValueError("Expected a JSON array of Reddit posts")
                rows = islice(data, offset, None)
            while not args.pages or report["pages"] < args.pages:
                batch = list(islice(rows, 100))
                if not batch:
                    break
                with db:
                    for row in batch:
                        if row is None:
                            continue
                        sub = (row.get("subreddit") or "shortstories").lower()
                        if sub in subs:
                            store.ingest(db, row, sub, route, stamp)
                            report["processed"] += 1
                    offset += len(batch)
                    _save(db, feed, {"offset": offset, "position": file.tell(), "version": version})
                report["pages"] += 1
        return report

    client = collect.RedditClient.from_env() if route == "reddit" else None
    for sub in subs:
        feed = f"{route}:{sub}"
        state = _load(db, feed)
        cursor = state.get("cursor")
        since = state.get("since", 0)
        newest = state.get("newest", since)
        pages = 0
        while not args.pages or pages < args.pages:
            if client is None:
                rows, after = collect.archive_page(sub, cursor)
            else:
                rows, after = client.new_page(sub, cursor, since)
            stamp = store.utcnow()
            with db:
                for row in rows:
                    if (row.get("subreddit") or sub).lower() != sub:
                        raise ValueError("Feed returned a different subreddit")
                    store.ingest(db, row, sub, route, stamp)
                    newest = max(newest, int(row["created_utc"]))
                cursor = after
                if after is None:
                    if client is None:
                        cursor = str(newest) if newest else None
                    else:
                        since = newest
                _save(db, feed, dict(cursor=cursor, since=since, newest=newest))
            report["pages"] += 1
            report["processed"] += len(rows)
            pages += 1
            if after is None:
                break

    if args.refresh:
        last = ""
        while True:
            batch = db.execute("""select id, subreddit from posts
                where subreddit is not null and id > ? order by id limit 100""", (last,)).fetchall()
            if not batch:
                break
            ids = dict(batch)
            rows = client.refresh(ids)
            present = {row["id"] for row in rows}
            stamp = store.utcnow()
            with db:
                for row in rows:
                    store.ingest(db, row, ids[row["id"]], "reddit", stamp)
                for key in ids.keys() - present:
                    store.forget(db, key)
            report["refreshed"] += len(present)
            report["missing"] += len(ids) - len(present)
            last = batch[-1][0]
    return report
