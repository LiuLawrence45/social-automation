import csv
from html.parser import HTMLParser

import pytest

from pipeline.source.browse import export_index
from pipeline.source.store import connect, save


class Elements(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.tags = []
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def make_record(key, url):
    return dict(id=key, title="An invented <i>story</i>", author="Example Author", url=url,
                route="archive", subreddit="shortstories", word_count=760, created_utc=None,
                score=0, text="Private source text sentinel.", brief={"premise": "Do not export this."},
                license="all-rights-reserved", rights="private-only", scores=None)


def test_index_contains_all_records_but_only_metadata(tmp_path):
    db = connect(tmp_path / "stories.db")
    save(db, make_record("a", "https://example.org/a"))
    second = make_record("b", "https://example.org/?x=1&y=2")
    second.update(title="Forged </script><img src=x>", route="scp", subreddit=None,
                  license="CC-BY-SA-3.0", rights="adaptation-permitted",
                  scores={"overall": 87, "genre": "horror", "tone": "quiet", "scary": 91, "fable": 5})
    save(db, second)
    dest = tmp_path / "exports" / "catalog.html"
    assert export_index(db, dest) == 2
    page = dest.read_text()
    for expected in ("Reddit: r/shortstories", "SCP Wiki", "760", "Private only", "CC-BY-SA-3.0",
                     "Adaptation permitted", "<td>0</td>"):
        assert expected in page
    assert "Fit (0–100)" not in page and "Scary: 91" not in page
    assert "Private source text sentinel" not in page and "Do not export this" not in page
    elements = Elements(page)
    assert [attrs["href"] for tag, attrs in elements.tags if tag == "a"] == [
        "https://example.org/a", "https://example.org/?x=1&y=2"]
    assert [tag for tag, _ in elements.tags].count("script") == 1
    assert not any(tag in {"i", "img", "link", "iframe"} or "src" in attrs for tag, attrs in elements.tags)


@pytest.mark.parametrize("url", ["javascript:void(0)", "//example.org", "file:///tmp/story",
                                 "https://[broken", "https://u:p@example.org", "https://foo\\bar"])
def test_unsafe_source_is_plain_text(tmp_path, url):
    db = connect(tmp_path / "stories.db")
    save(db, make_record("a", url))
    path = tmp_path / "index.html"
    export_index(db, path)
    assert not any(tag == "a" for tag, _ in Elements(path.read_text()).tags)


def test_index_surfaces_top_voted_and_flags_series(tmp_path):
    db = connect(tmp_path / "stories.db")
    save(db, dict(make_record("a", None), title="A low-scoring candidate", over_18=False))
    save(db, dict(make_record("z", None), title="The Clock, Part 2", score=85, over_18=False))
    path = tmp_path / "index.html"
    export_index(db, path)
    page = path.read_text()
    assert page.index("The Clock, Part 2") < page.index("A low-scoring candidate")
    assert "<td>series</td>" in page and "<td>Candidate</td>" in page
    assert "Private, internal demo only" in page


def test_csv_lists_metadata_in_score_order(tmp_path):
    db = connect(tmp_path / "stories.db")
    save(db, dict(make_record("low", "https://example.org/low"), title='Comma, quote "and newline\n',
                  score=-3, over_18=False))
    save(db, dict(make_record("high", "javascript:bad"), title="Part 2", over_18=False, score=99))
    path = tmp_path / "exports" / "index.CSV"
    assert export_index(db, path) == 2
    with path.open(newline="") as file:
        reader = csv.DictReader(file)
        assert reader.fieldnames == ["id", "title", "author", "subreddit", "words", "score", "flags", "url"]
        first, last = reader
    assert first["id"] == "high" and first["flags"] == "series" and first["url"] == ""
    assert last == {"id": "low", "title": 'Comma, quote "and newline\n', "author": "Example Author",
                    "subreddit": "shortstories", "words": "760", "score": "-3", "flags": "Candidate",
                    "url": "https://example.org/low"}
    assert "Private source text sentinel" not in path.read_text()


@pytest.mark.parametrize("value", ["=SUM(1,1)", "   +1", "-foo", "\t@hello", "\r=1", "  \ufeff＝1",
                                   "\u200b＠A1", "\nline"])
def test_csv_neutralizes_formula_like_strings_without_mutating_store(tmp_path, value):
    db = connect(tmp_path / "stories.db")
    rec = dict(make_record(value, None), title=value, author=value, subreddit=value)
    save(db, rec)
    path = tmp_path / "index.csv"
    export_index(db, path)
    with path.open(newline="") as file:
        row = next(csv.DictReader(file))
    for key in ("id", "title", "author", "subreddit"):
        assert row[key] == "'" + value
    assert db.execute("select id from posts").fetchone() == (value,)
