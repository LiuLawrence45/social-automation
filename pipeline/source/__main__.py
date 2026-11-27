"""Browse Reddit stories for private internal demos. No hosted inference."""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from . import ranking, reddit, store
from .browse import export_index


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def coverage(db):
    return [dict(subreddit=s, posts=n, oldest_utc=a, newest_utc=b)
            for s, n, a, b in db.execute("select subreddit,count(*),min(created),max(created)"
                                         " from posts group by subreddit order by subreddit")]


def index_path(db, path):
    count, = db.execute("select count(*) from posts").fetchone()
    return path.with_suffix(".csv" if count > 10000 else ".html")


def search(db, args):
    query = ("select record from posts where instr(lower(json_extract(record,'$.title') || ' ' ||"
             " coalesce(json_extract(record,'$.author'),'') || ' ' || json_extract(record,'$.text')),"
             " lower(?)) > 0 and json_extract(record,'$.word_count') between ? and ?")
    params = [args.text, args.min_words, args.max_words]
    if args.subreddit:
        query += " and subreddit=?"
        params.append(args.subreddit.lower())
    results = []
    for row, in db.execute(query + " order by popularity desc, id", params):
        rec = json.loads(row)
        flag = ranking.reject(rec)
        if args.candidates and flag and not (args.include_unknown and flag == "unknown maturity flag"):
            continue
        results.append(dict(id=rec["id"], title=rec["title"], subreddit=rec["subreddit"],
                            author=rec["author"], url=rec["url"], score=rec["score"],
                            word_count=rec["word_count"], flag=flag))
        if len(results) == args.limit:
            break
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("~/social-automation-output/SOCI-6/reddit.sqlite"))
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("fetch", "refresh"):
        f = sub.add_parser(command)
        f.add_argument("--route", choices=["cache", "archive", "reddit"], default="cache")
        f.add_argument("--input", type=Path, nargs="+", default=[Path("~/social-automation-output/reddit/posts.json")])
        f.add_argument("--subreddits", nargs="+", default=["shortstories", "nosleep", "scarystories"])
        f.add_argument("--pages", type=int, default=0, help="page limit per feed; 0 reads until exhaustion")
        f.add_argument("--approved", action="store_true", help="confirm approval for the live route")
        f.add_argument("--refresh", action="store_true", help="also refresh old posts by ID (OAuth only)")
    sub.add_parser("coverage")
    sub.add_parser("show").add_argument("id")
    q = sub.add_parser("search")
    q.add_argument("text", nargs="?", default="")
    q.add_argument("--subreddit")
    q.add_argument("--limit", type=int, default=25)
    q.add_argument("--min-words", type=int, default=0)
    q.add_argument("--max-words", type=int, default=2**31)
    q.add_argument("--candidates", action="store_true", help="exclude serials, meta, mature/unknown and non-story lengths")
    q.add_argument("--include-unknown", action="store_true", help="also show candidates whose snapshot lacks the maturity flag")
    sub.add_parser("index").add_argument("path", nargs="?", type=Path)
    sub.add_parser("script").add_argument("id")
    args = parser.parse_args(argv)
    if args.command == "search" and (args.limit <= 0 or not 0 <= args.min_words <= args.max_words):
        parser.error("supply a positive limit and a valid word range")
    os.umask(0o077)
    path = args.db.expanduser()
    db = store.connect(path)
    try:
        if args.command in ("fetch", "refresh"):
            if args.command == "refresh" and args.route == "reddit":
                args.refresh = True
            if args.route == "cache":
                result = {}
                for source in args.input:
                    opts = argparse.Namespace(**dict(vars(args), input=source))
                    for key, count in reddit.fetch(db, opts).items():
                        result[key] = result.get(key, 0) + count
            else:
                result = reddit.fetch(db, args)
            if args.command == "refresh":
                dest = index_path(db, path)
                export_index(db, dest)
                result["index"] = str(dest)
            emit(dict(result, coverage=coverage(db)))
        elif args.command == "index":
            dest = args.path or index_path(db, path)
            emit({"indexed": export_index(db, dest), "index": str(dest)})
        elif args.command == "coverage":
            emit(coverage(db))
        elif args.command == "search":
            emit(search(db, args))
        elif args.command == "show":
            emit(store.get(db, args.id))
        else:
            from .draft import draft
            story = draft(store.get(db, args.id))
            dest = path.parent / "drafts" / args.id / "story.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(story, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            emit({"draft": str(dest), "words": len(story["script"].split())})
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        sys.exit("error: " + str(exc))
