import json

from pipeline.source.__main__ import main


def test_local_demo_round_trip(tmp_path, capsys):
    src = tmp_path / "stories.json"
    raw = dict(id="abc", title="The missing hands", subreddit="nosleep", author="Invented",
               selftext="A clock ticks behind the closed curtain. " * 50, score=9, created_utc=10,
               over_18=False, num_comments=2)
    src.write_text(json.dumps([raw, dict(raw, id="def", title="Part 2", score=10)]))
    prefix = ["--db", str(tmp_path / "stories.sqlite")]

    def run(*args):
        main(prefix + list(args))
        return json.loads(capsys.readouterr().out)

    assert run("fetch", "--input", str(src))["processed"] == 2
    assert run("refresh", "--input", str(src))["processed"] == 0
    assert run("search", "curtain")[0]["id"] == "def"
    assert [r["id"] for r in run("search", "--candidates", "--max-words", "500")] == ["abc"]
    assert run("show", "abc")["text"] == raw["selftext"]
    assert 200 <= run("script", "abc")["words"] <= 230
    assert (tmp_path / "drafts/abc/story.json").is_file()
