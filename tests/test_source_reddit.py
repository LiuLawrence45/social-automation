import json
import os
from types import SimpleNamespace

import pytest

from pipeline.source import reddit, store


def args(route="cache", **changes):
    defaults = dict(route=route, pages=0, input=None, approved=False, refresh=False,
                    subreddits=["shortstories", "nosleep"])
    return SimpleNamespace(**dict(defaults, **changes))


def post(key, created, **fields):
    return dict(id=key, title="An invented title", selftext="An invented story.",
                created_utc=created, **fields)


def test_cache_paging_and_original_timestamp(tmp_path):
    db = store.connect(tmp_path / "db")
    src = tmp_path / "posts.json"
    src.write_text(json.dumps([post(str(i), i) for i in range(100)]
                              + [post("aa", 101, subreddit="nosleep")]))
    os.utime(src, (1609459200, 1609459200))
    opt = args(input=src, pages=1, subreddits=["nosleep"])
    assert reddit.fetch(db, opt)["processed"] == 0
    assert reddit.fetch(db, opt)["processed"] == 1
    assert reddit.fetch(db, opt)["pages"] == 0
    assert store.get(db, "aa")["retrieved_at"] == "2021-01-01T00:00:00+00:00"
    assert reddit.fetch(db, args(input=src, subreddits=["shortstories"]))["processed"] == 100
    assert store.get(db, "1")["subreddit"] == "shortstories"
    assert db.execute("select count(*) from posts").fetchone() == (101,)


def test_nosleep_csv_snapshot_import(tmp_path):
    db = store.connect(tmp_path / "db")
    src = tmp_path / "posts.csv"
    src.write_text('title,score,id,subreddit,url,num_comments,body,created\n'
                   'Invented,0,ab,nosleep,,7,"A line, then\nanother.",1666800000.0\n'
                   'Also invented,,ac,nosleep,,,,\n')
    assert reddit.fetch(db, args(input=src))["processed"] == 2
    r = store.get(db, "ab")
    assert (r["text"], r["score"], r["num_comments"]) == ("A line, then\nanother.", 0, 7)
    assert (r["created_utc"], r["flair"], r["author"], r["over_18"]) == (1666800000, None, None, None)
    assert r["rights"] == "private-only"
    assert r["route"] == "cache"
    missing = store.get(db, "ac")
    assert missing["score"] is missing["num_comments"] is missing["created_utc"] is None
    assert reddit.fetch(db, args(input=src))["processed"] == 0


def test_link_csv_resumes_filters_and_preserves_richer_snapshot(tmp_path):
    db = store.connect(tmp_path / "db")
    src = tmp_path / "large.csv"
    src.write_text(",title,post,link\n" + "".join(
        f'{i},Invented,"Line ā\nline",https://www.reddit.com/r/NoSleep/comments/{i}/slug/\n'
        for i in range(101)) + "101,Mismatch,text,https://example.net/r/nosleep/comments/no/\n")
    os.utime(src, (1700000000, 1700000000))
    store.ingest(db, post("0", 1, score=2), "nosleep", "cache", "2020-01-01T00:00:00+00:00")
    opt = args(input=src, pages=1)
    assert reddit.fetch(db, opt)["processed"] == 100
    assert reddit.fetch(db, opt)["processed"] == 1
    assert reddit.fetch(db, opt)["processed"] == 0
    assert db.execute("select count(*) from posts").fetchone() == (101,)
    assert store.get(db, "0")["score"] == 2
    assert store.get(db, "100")["text"] == "Line ā\nline"
    assert store.get(db, "100")["created_utc"] is None
    store.ingest(db, post("100", 5, score=9), "nosleep", "cache", "2020-01-01T00:00:00+00:00")
    assert store.get(db, "100")["score"] == 9


@pytest.mark.parametrize("replacement", ["", "[removed]", "[deleted]"])
def test_cache_replacement_invalidates_story_and_derivatives(tmp_path, replacement):
    db = store.connect(tmp_path / "db")
    src = tmp_path / "posts.json"
    raw = post("a", 10)
    src.write_text(json.dumps([raw]))
    os.utime(src, (1609459200, 1609459200))
    opt = args(input=src)
    reddit.fetch(db, opt)
    record = store.get(db, "a")
    store.save(db, dict(record, scores={"overall": 42}, brief={"premise": "Invented."}))
    raw["selftext"] = replacement
    src.write_text(json.dumps([raw]))
    os.utime(src, (1609459201, 1609459201))
    assert reddit.fetch(db, opt)["processed"] == 1
    if replacement:
        with pytest.raises(ValueError, match="unknown"):
            store.get(db, "a")
    else:
        updated = store.get(db, "a")
        assert (updated["text"], updated["scores"], updated["brief"]) == ("", None, None)


def test_archive_resumes_and_checks_new_posts(tmp_path, monkeypatch):
    db = store.connect(tmp_path / "db")
    opt = args("archive", approved=True, pages=1, subreddits=["nosleep"])
    calls = []
    def page(sub, cursor):
        calls.append(cursor)
        return {None: ([post("a", 10)], "11"), "11": ([post("b", 12)], None),
                "12": ([post("b", 12), post("c", 13)], None)}[cursor]
    monkeypatch.setattr(reddit.collect, "archive_page", page)
    for _ in range(3):
        reddit.fetch(db, opt)
    assert calls == [None, "11", "12"]
    assert db.execute("select count(*) from posts").fetchone() == (3,)


def test_oauth_page_watermark_and_refresh(tmp_path, monkeypatch):
    db = store.connect(tmp_path / "db")
    opt = args("reddit", approved=True, pages=1, subreddits=["nosleep"])
    calls = []
    class Client:
        def new_page(self, sub, cursor, since):
            calls.append((cursor, since))
            return [([post("d", 10)], "t3_d"), ([post("b", 5)], None),
                    ([post("e", 12)], None)][len(calls) - 1]
        def refresh(self, ids):
            assert set(ids) == {"a", "b", "d", "e"}
            return [post("e", 12, score=7), post("b", 5), post("d", 10)]
    monkeypatch.setattr(reddit.collect.RedditClient, "from_env", Client)
    store.ingest(db, post("a", 1), "shortstories", "cache", store.utcnow())
    for _ in range(2):
        reddit.fetch(db, opt)
    opt.refresh = True
    assert reddit.fetch(db, opt)["missing"] == 1
    assert calls == [(None, 0), ("t3_d", 0), (None, 10)]
    assert store.get(db, "e")["score"] == 7
    assert db.execute("select id from inaccessible").fetchall() == [("a",)]
    store.ingest(db, post("a", 1), "shortstories", "cache", store.utcnow())
    with pytest.raises(ValueError, match="unknown"):
        store.get(db, "a")


@pytest.mark.parametrize("route", ["reddit", "archive"])
def test_requires_approval_before_network(tmp_path, route):
    with pytest.raises(ValueError, match="approval"):
        reddit.fetch(store.connect(tmp_path / "db"), args(route))


def test_bad_page_rolls_back_rows_and_cursor(tmp_path, monkeypatch):
    db = store.connect(tmp_path / "db")
    monkeypatch.setattr(reddit.collect, "archive_page",
                        lambda *a: ([post("a", 1), post("b", 2, subreddit="other")], "3"))
    with pytest.raises(ValueError, match="different subreddit"):
        reddit.fetch(db, args("archive", approved=True))
    assert db.execute("select count(*) from posts").fetchone() == (0,)
    assert db.execute("select count(*) from cursors").fetchone() == (0,)
