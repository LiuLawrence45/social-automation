"""Writer stage tests. Everything here runs offline: model calls go to a scripted fake client and
collection goes to a fake route. All story text in this file is invented for the tests."""
import json
import sqlite3

import pytest

from pipeline import story as story_mod
from pipeline.writer import analyze, annotate, collectors, corpus, judge, llm, store, style as style_mod, textstats, write

SOURCE = """The Night Bus

I take the last bus home every Friday. It is always empty. Last Friday the driver was new. He did not look at me when I paid.
At the second stop a woman got on. She sat right behind me even though every other seat was free. She smelled like rain.
I heard her whisper my name. I had never seen her before. I turned around and the seat was empty. The bus had not stopped.
I asked the driver if he saw her. He said nobody had got on all night. Then he asked me why I was sitting in her seat.
"""


def good_lines():
    return [
        {"speaker": "narrator", "text": "The last bus home is always empty. Last Friday, it was not."},
        {"speaker": "narrator", "text": "A new driver took my coins and never looked up."},
        {"speaker": "driver", "text": "Sit anywhere you like, miss."},
        {"speaker": "narrator", "text": "At the second stop, a woman in a wet coat got on. Every seat was free. She sat right behind me and leaned close."},
        {"speaker": "woman", "text": "Mara. You came back."},
        {"speaker": "narrator", "text": "I had never seen her before. I turned around and the seat was empty. The bus had not stopped once."},
        {"speaker": "narrator", "text": "I walked to the front and held the rail."},
        {"speaker": "mara", "text": "Did you see where she went?"},
        {"speaker": "driver", "text": "Nobody got on tonight, miss."},
        {"speaker": "narrator", "text": "He pointed at the wet seat behind mine. The doors stayed shut. The lights went out, one row at a time."},
        {"speaker": "driver", "text": "So why are you sitting in her seat?"},
    ]


def good_reply(lines=None, **extra):
    data = {
        "plan": {"premise": "a rider meets a passenger nobody else saw"},
        "title": "The Night Bus",
        "genre": "scary",
        "lines": lines or good_lines(),
        "cast": [
            {"speaker": "narrator", "name": "Mara", "kind": "person", "role": "the rider", "voice": {"age": "20s", "gender": "female", "tone": "quiet, quick", "processing": "none"}},
            {"speaker": "woman", "name": "The Woman", "kind": "voice", "role": "the passenger", "voice": {"age": "40s", "gender": "female", "tone": "soft", "processing": "slightly wrong: slower, flatter"}},
            {"speaker": "mara", "name": "Mara", "kind": "person", "role": "the rider, aloud", "voice": {"age": "20s", "gender": "female", "tone": "shaky", "processing": "none"}},
            {"speaker": "driver", "name": "Driver", "kind": "role", "role": "the new driver", "voice": {"age": "50s", "gender": "male", "tone": "flat", "processing": "none"}},
        ],
        "names": ["Mara"],
        "sound_cues": [{"line": 0, "at": "start", "kind": "ambience", "prompt": "An empty city bus idling at night.", "seconds": 6},
                       {"line": 99, "at": "end", "kind": "silence", "prompt": "A beat of quiet."}],
        "music": "A low drone that stops dead on the last line.",
    }
    data.update(extra)
    return data


RUBRIC_GOOD = {key: {"score": 4, "why": "fine"} for key in judge.RUBRIC_KEYS}
RUBRIC_GOOD["fixes"] = []
RUBRIC_BAD = {key: {"score": 4, "why": "fine"} for key in judge.RUBRIC_KEYS}
RUBRIC_BAD["filmable"] = {"score": 2, "why": "too much thinking", "weak": ["I had never seen her before."]}
RUBRIC_BAD["fixes"] = ["Line 4: show it instead."]


class FakeClient(object):
    """Answers each call from a queue chosen by the call's purpose and records what it was asked."""

    def __init__(self, drafts=(), rubrics=(), other=(), cost=0.01):
        self.queues = {"script draft": list(drafts), "script rewrite": list(drafts)[1:], "script rubric": list(rubrics)}
        self.other = list(other)
        self.calls = []
        self.cost = cost

    def __call__(self, prompt, **options):
        ledger = options.get("ledger")
        if ledger is not None:
            ledger.check()
        purpose = options.get("purpose", "")
        self.calls.append({"purpose": purpose, "prompt": prompt, "options": options})
        queue = self.queues.get(purpose)
        if purpose == "script rewrite":
            queue = self.queues["script rewrite"]
        reply = queue.pop(0) if queue else self.other.pop(0)
        if ledger is not None:
            ledger.add({"purpose": purpose, "cost_usd": self.cost})
        return {"text": json.dumps(reply), "model": "fake", "cost_usd": self.cost, "input_tokens": 1, "output_tokens": 1}


def settings(**extra):
    base = dict(seconds=28, wpm=250, genre="scary", retries=2, ceiling=1.0, cast=["Mara"])
    base.update(extra)
    return write.Settings(**base)


def targets(**extra):
    base = {"seconds": 28, "wpm": 250}
    base.update(extra)
    return style_mod.targets_for(style_mod.load(), "scary", base)


def record():
    return {"id": "t1", "title": "The Night Bus", "author": "someone", "subreddit": "nosleep", "text": SOURCE}


# text measures

def test_sentences_and_measure():
    text = "Mr. Hale came at 3 a.m. and knocked. Nobody answered! Why? He knocked again."
    assert textstats.sentences(text) == ["Mr. Hale came at 3 a.m. and knocked.", "Nobody answered!", "Why?", "He knocked again."]
    stats = textstats.measure(text)
    assert stats["sentences"] == 4 and stats["first_sentence_words"] == 9 and stats["last_sentence_words"] == 3
    assert stats["short_sentence_share"] == 0.75 and stats["question_share"] == 0.25


def test_measure_lines_dialogue_filler_and_gore():
    lines = [{"speaker": "narrator", "text": "Suddenly the door opened. There was blood on the floor."}, {"speaker": "mom", "text": "Do not look."}]
    stats = textstats.measure_lines(lines, 240)
    assert stats["dialogue_lines"] == 1 and stats["dialogue_words"] == 3 and stats["first_dialogue_word"] == 10
    assert stats["filler"] == ["suddenly"] and stats["gore"] == ["blood"]
    assert stats["seconds"] == round(13 * 60 / 240.0, 1)


# model client

def test_extract_json_tolerates_fences_and_prose():
    assert llm.extract_json("```json\n{\"a\": [1, 2]}\n```") == {"a": [1, 2]}
    assert llm.extract_json("Here you go: {\"a\": {\"b\": 1}} thanks") == {"a": {"b": 1}}
    with pytest.raises(llm.LLMError):
        llm.extract_json("{\"a\": [1, 2")


def test_cost_and_run_ledger_ceiling(tmp_path):
    assert llm.cost_of("claude-sonnet-5-5", 1000000, 100000) == pytest.approx(3.0)
    assert llm.cost_of("anthropic/claude-haiku-5.5", 2000000, 0) == pytest.approx(0.2)
    with pytest.raises(llm.LLMError):
        llm.cost_of("unknown-model", 1, 1)
    path = tmp_path / "spend.jsonl"
    earlier = llm.Ledger(path)
    earlier.add({"cost_usd": 4.0})
    ledger = llm.RunLedger(path, 0.05)
    ledger.check()
    ledger.add({"cost_usd": 0.03})
    ledger.check()
    ledger.add({"cost_usd": 0.03})
    with pytest.raises(llm.BudgetError):
        ledger.check()
    assert ledger.run_spent() == pytest.approx(0.06) and ledger.spent() == pytest.approx(4.06)


def test_complete_needs_a_key(monkeypatch):
    for name in llm.ENV_KEYS.values():
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(llm.LLMError):
        llm.complete("hello")


# story records

def test_load_record_from_text_json_and_list(tmp_path):
    text_file = tmp_path / "night-bus.txt"
    text_file.write_text(SOURCE, encoding="utf-8")
    loaded = write.load_record(text_file)
    assert loaded["title"] == "The Night Bus" and loaded["text"].startswith("I take the last bus")
    one = tmp_path / "one.json"
    one.write_text(json.dumps({"title": "T", "selftext": "Body text.", "created_utc": "1795737206"}), encoding="utf-8")
    assert write.load_record(one)["text"] == "Body text." and write.load_record(one)["created"].startswith("20")
    many = tmp_path / "many.json"
    many.write_text(json.dumps([{"id": "a", "title": "A", "text": "x"}, {"id": "b", "title": "B", "text": "y"}]), encoding="utf-8")
    assert write.load_record(many, "b")["title"] == "B"
    with pytest.raises(write.WriterError):
        write.load_record(many)
    with pytest.raises(write.WriterError):
        write.load_record(many, "zzz")
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"title": "T"}), encoding="utf-8")
    with pytest.raises(write.WriterError):
        write.load_record(empty)


def test_source_block_records_where_the_story_came_from():
    source = write.source_block({"id": "abc12", "subreddit": "nosleep", "title": "T", "author": "writer", "license": "CC BY-SA 3.0",
                                 "attribution": "by writer", "text": "x"}, today="2026-11-27")
    assert source == {"kind": "reddit", "subreddit": "r/nosleep", "url": "https://www.reddit.com/r/nosleep/comments/abc12/", "title": "T",
                      "author": "u/writer", "id": "abc12", "license": "CC BY-SA 3.0", "attribution": "by writer", "retrieved": "2026-11-27",
                      "use": "private, not for distribution"}
    assert write.source_block({"title": "T", "text": "x", "kind": "file", "file": "t.txt"}, today="2026-11-27")["kind"] == "file"


# normalising the model's reply

def test_clean_text_leaves_only_spoken_words():
    assert write.clean_text("“Open up,” (whispering) she said — now… [door creaks]") == "Open up, she said, now..."
    assert write.clean_text("Wait . What ?") == "Wait. What?"


def test_normalise_builds_the_script_half():
    reply = good_reply(lines=[{"speaker": "Narrator", "text": "The bus was empty."}, {"speaker": "narrator", "text": "Then a woman sat down."},
                              {"speaker": "The Woman", "text": "“Mara.”"}, {"speaker": "narrator", "text": "(pause)"}],
                       cast=[{"speaker": "the woman", "name": "The Woman", "voice": {"age": "40s"}}], names=["Mara", "Nobody"])
    script = write.normalise(reply)
    assert script["lines"] == [{"speaker": "narrator", "text": "The bus was empty. Then a woman sat down."}, {"speaker": "the_woman", "text": "Mara."}]
    assert [entry["speaker"] for entry in script["cast"]] == ["narrator", "the_woman"]
    assert script["cast"][1]["voice"] == {"age": "40s", "gender": "", "tone": "", "processing": "none"} and script["cast"][1]["kind"] == "person"
    assert script["names"] == ["Mara"]
    assert script["sound_cues"][1]["line"] == 1 and script["sound_cues"][1]["kind"] == "silence"
    with pytest.raises(write.WriterError):
        write.normalise({"lines": []})
    with pytest.raises(write.WriterError):
        write.normalise({"lines": [{"speaker": "", "text": "x"}]})


# judge: measurable checks

def failed(report_or_checks):
    checks = report_or_checks["checks"] if isinstance(report_or_checks, dict) else report_or_checks
    return sorted(check["id"] for check in checks if not check["ok"])


def test_checks_pass_on_a_script_in_the_style():
    script = write.normalise(good_reply())
    assert failed(judge.check_script(script, targets(), ["Mara"])) == []


def test_checks_name_each_problem_and_its_fix():
    lines = good_lines()
    lines[1] = {"speaker": "narrator", "text": "A new driver took my coins and he never looked up at me, not even once, while the engine made a very low sound that I could feel in my teeth."}
    lines[5] = {"speaker": "narrator", "text": "Suddenly I saw blood. \"Who are you?\" I asked."}
    script = write.normalise(good_reply(lines=lines))
    quoted = [line for line in script["lines"] if line["text"].startswith("Suddenly")][0]
    quoted["text"] = "Suddenly I saw blood. \"Who are you?\" I asked."
    checks = judge.check_script(script, targets(), ["Willow", "Allan"])
    assert failed(checks) == ["cast_pool", "filler", "gore", "narrator_quotes", "sentence_max"]
    by_id = {check["id"]: check for check in checks}
    assert by_id["cast_pool"]["value"] == ["Mara", "Mara"] and "Willow, Allan" in by_id["cast_pool"]["fix"]
    assert "suddenly" in by_id["filler"]["fix"] and "very" in by_id["filler"]["fix"]
    assert by_id["sentence_max"]["fix"].startswith("Split this")


def test_cast_pool_is_a_pool_and_not_a_list_of_words_to_say():
    script = write.normalise(good_reply())
    spoken = judge.textstats.script_text(script["lines"])
    assert "Willow" not in spoken
    assert failed(judge.check_script(script, targets(), ["Mara", "Willow", "Allan"])) == []
    assert failed(judge.check_script(script, targets(), [])) == []
    [entry for entry in script["cast"] if entry["speaker"] == "mara"][0]["speaker"] = "rider"
    assert failed(judge.check_script(script, targets(), ["Mara"])) == ["cast", "cast_pool"]
    assert write.parse_cast(None) is None and write.parse_cast("none") == [] and write.parse_cast("Willow, Allan") == ["Willow", "Allan"]
    assert write.cast_pool(write.Settings(), style_mod.load()) == style_mod.load()["cast_pool"] and len(style_mod.load()["cast_pool"]) == 6
    assert write.cast_pool(settings(cast=[]), style_mod.load()) == []


def test_checks_catch_length_dialogue_and_cast():
    script = write.normalise(good_reply(lines=[{"speaker": "narrator", "text": "The bus was empty. I sat down. Nothing happened at all."},
                                               {"speaker": "narrator2", "text": "She said hello."}]))
    script["cast"] = []
    ids = failed(judge.check_script(script, targets(), []))
    for expected in ("length", "dialogue_lines", "cast"):
        assert expected in ids
    with pytest.raises(judge.JudgeError):
        judge.check_script({"lines": [{"speaker": "narrator", "text": ""}]}, targets(), [])


def test_speech_tag_check_ignores_things_that_said():
    lines = good_lines()
    lines[5] = {"speaker": "narrator", "text": "She said it twice. I turned around and the seat was empty. The bus had not stopped once."}
    assert "speech_tags" in failed(judge.check_script(write.normalise(good_reply(lines=lines)), targets(), []))
    lines[5] = {"speaker": "narrator", "text": "The screen said one hundred pounds. I turned around and the seat was empty. The bus had not stopped."}
    assert "speech_tags" not in failed(judge.check_script(write.normalise(good_reply(lines=lines)), targets(), []))


# judge: rubric and score

def test_rubric_scores_and_summary():
    script = write.normalise(good_reply())
    client = FakeClient(rubrics=[RUBRIC_BAD, RUBRIC_GOOD])
    report = judge.judge_story(script, targets(), "scary", ["Mara"], SOURCE, client)
    assert report["summary"]["passed"] is False and report["summary"]["rubric_low"] == ["filmable"]
    assert any("Not filmable" in fix and "never seen her" in fix for fix in report["fixes"])
    assert "CAST POOL: Mara" in client.calls[0]["prompt"] and "narrator = Mara (person)" in client.calls[0]["prompt"] and "SOURCE STORY" in client.calls[0]["prompt"]
    report = judge.judge_story(script, targets(), "scary", ["Mara"], SOURCE, client, ["Line 4: show it instead."])
    assert report["summary"]["passed"] is True and report["summary"]["rubric_mean"] == 4.0 and report["summary"]["score"] == 87.5
    assert "EARLIER FINDINGS" in client.calls[1]["prompt"]
    offline = judge.judge_story(script, targets(), "scary", ["Mara"])
    assert offline["rubric"] is None and offline["summary"]["passed"] is True and offline["summary"]["score"] == 100.0


def test_rubric_rejects_a_reply_without_scores():
    with pytest.raises(llm.LLMError):
        judge.rubric(write.normalise(good_reply()), "scary", None, FakeClient(rubrics=[{"fixes": []}]))


# writer: draft, judge, rewrite

def test_write_story_passes_first_time():
    client = FakeClient(drafts=[good_reply()], rubrics=[RUBRIC_GOOD])
    result = write.write_story(record(), settings(cast=["Mara"]), client, llm.RunLedger(None, 1.0))
    assert result["stopped"] == "passed" and len(result["history"]) == 1 and result["chosen"] == 0
    draft_call = client.calls[0]
    assert "Cast pool: Mara." in draft_call["prompt"] and "I take the last bus" in draft_call["prompt"]
    assert "STYLE GUIDE" in draft_call["options"]["system"] and "117 words" in draft_call["options"]["system"]


def test_rewrite_loop_fixes_a_failing_draft():
    long_lines = good_lines() + [{"speaker": "narrator", "text": "Then I thought about it for a long time and I decided that it was probably nothing at all to worry about."}]
    client = FakeClient(drafts=[good_reply(lines=long_lines), good_reply()], rubrics=[RUBRIC_BAD, RUBRIC_GOOD])
    result = write.write_story(record(), settings(), client, llm.RunLedger(None, 1.0))
    assert [item["report"]["summary"]["passed"] for item in result["history"]] == [False, True]
    assert result["chosen"] == 1 and result["stopped"] == "passed"
    rewrite = [call for call in client.calls if call["purpose"] == "script rewrite"][0]
    assert "The script is" in rewrite["prompt"] and "Line 4: show it instead." in rewrite["prompt"]
    assert "Not filmable" in rewrite["prompt"]


def test_rewrite_loop_is_bounded_by_retries_and_keeps_the_best():
    client = FakeClient(drafts=[good_reply()] * 4, rubrics=[RUBRIC_BAD, RUBRIC_BAD, RUBRIC_BAD, RUBRIC_BAD])
    result = write.write_story(record(), settings(retries=2), client, llm.RunLedger(None, 1.0))
    assert len(result["history"]) == 3 and result["stopped"] == "retries used" and result["report"]["summary"]["passed"] is False


def test_rewrite_loop_stops_at_the_spend_ceiling():
    client = FakeClient(drafts=[good_reply()] * 4, rubrics=[RUBRIC_BAD] * 4, cost=0.02)
    result = write.write_story(record(), settings(retries=5, ceiling=0.05), client, llm.RunLedger(None, 0.05))
    assert result["stopped"] == "spend ceiling reached" and len(result["history"]) == 1
    assert result["spent_usd"] <= 0.07


def test_save_writes_a_story_the_story_stage_accepts(tmp_path):
    client = FakeClient(drafts=[good_reply()], rubrics=[RUBRIC_GOOD])
    result = write.write_story(record(), settings(cast=["Mara"]), client, llm.RunLedger(None, 1.0))
    job = tmp_path / "job"
    story = write.save(job, result, record())
    on_disk = json.loads((job / "story.json").read_text(encoding="utf-8"))
    assert on_disk == story
    story_mod.validate_story(json.loads(json.dumps(story)))
    assert story["slug"] == "the-night-bus" and story["source"]["subreddit"] == "r/nosleep" and story["source"]["use"].startswith("private")
    assert all(set(line) == {"speaker", "text"} for line in story["lines"])
    assert story["script"] == " ".join(line["text"] for line in story["lines"])
    assert story["voice"] == {"target_wpm": 250, "speakers": {}}
    assert {entry["speaker"] for entry in story["cast"]} == {line["speaker"] for line in story["lines"]}
    assert story["writer"]["cast_pool"] == ["Mara"] and story["writer"]["passed"] is True
    judged = json.loads((job / "script_judge.json").read_text(encoding="utf-8"))
    assert judged["drafts"][0]["lines"] == story["lines"] and judged["final"]["summary"]["passed"] is True
    assert json.loads((job / "source.json").read_text(encoding="utf-8"))["text"] == SOURCE
    assert "**WOMAN:** Mara. You came back." in (job / "script.md").read_text(encoding="utf-8")


def test_save_keeps_production_fields_already_in_story_json(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "story.json").write_text(json.dumps({"version": 1, "slug": "kept-slug", "voice": {"voice_id": "abc", "speakers": {"driver": {"voice_id": "xyz"}}},
                                               "display_overrides": {"a.m.,": "AM"}}), encoding="utf-8")
    client = FakeClient(drafts=[good_reply()], rubrics=[RUBRIC_GOOD])
    story = write.save(job, write.write_story(record(), settings(), client, llm.RunLedger(None, 1.0)), record())
    assert story["slug"] == "kept-slug" and story["voice"]["voice_id"] == "abc" and story["voice"]["speakers"]["driver"] == {"voice_id": "xyz"}
    assert story["display_overrides"] == {"a.m.,": "AM"}


def test_judge_cli_checks_only(tmp_path, capsys):
    client = FakeClient(drafts=[good_reply()], rubrics=[RUBRIC_GOOD])
    job = tmp_path / "job"
    write.save(job, write.write_story(record(), settings(cast=["Mara"]), client, llm.RunLedger(None, 1.0)), record())
    assert judge.main(["--job", str(job), "--checks-only"]) == 0
    assert "PASS" in capsys.readouterr().out
    assert judge.main(["--job", str(job), "--checks-only", "--cast", "Willow"]) == 1
    assert judge.main(["--job", str(job), "--checks-only", "--cast", "none"]) == 0
    assert judge.main(["--job", str(tmp_path / "missing"), "--checks-only"]) == 2


def test_style_targets_follow_settings_and_genre():
    style = style_mod.load()
    scary = style_mod.targets_for(style, "scary", {"seconds": 60, "wpm": 240})
    assert scary["words"] == 240 and scary["dialogue_share"] == style["genres"]["scary"]["dialogue_share"]
    assert style_mod.targets_for(style, "nonsense")["dialogue_share"] == style["targets"]["dialogue_share"]
    text = style_mod.render(style, "scary", scary)
    assert "240 words" in text and style["genres"]["scary"]["rules"][0] in text


# corpus: collection

class FakeRoute(collectors.Collector):
    name = "fake"

    def __init__(self):
        self.views = {"v%d" % index: 1000 * (index + 1) for index in range(10)}
        self.block = set()
        self.fetched = []

    def list_channel(self, channel):
        return {"channel": "Chan", "channel_id": "UC1", "videos": [{"id": key, "title": "Title %s" % key, "view_count": value} for key, value in self.views.items()]}

    def list_hashtag(self, tag, limit):
        return [{"id": "h1", "title": "A scary Roblox story", "view_count": 50}, {"id": "h2", "title": "ТУННЕЛЬ", "view_count": 999}, {"id": "h3", "title": "", "view_count": 5}]

    def search(self, query, limit):
        return [{"id": "s1", "title": "Roblox story short", "view_count": 7, "duration": 60, "channel": "Other", "channel_id": "UC2"}]

    def _info(self, video):
        return {"id": video, "title": "Title %s" % video, "channel": "Chan", "channel_id": "UC1", "uploader_id": "@chan", "channel_follower_count": 2000,
                "upload_date": "20261101", "duration": 50, "view_count": self.views.get(video, 10), "like_count": 5, "comment_count": 1, "tags": ["roblox"]}

    def fetch(self, video, audio_dir):
        if video in self.block:
            raise collectors.Blocked("Sign in to confirm")
        audio_dir.mkdir(parents=True, exist_ok=True)
        (audio_dir / (video + ".m4a")).write_bytes(b"0")
        self.fetched.append(video)
        return self._info(video)

    def counts(self, video):
        return self._info(video)


def test_pick_weighted_and_percentile_rank():
    entries = [{"id": str(index), "view_count": index} for index in range(20)]
    picked = corpus.pick_weighted(entries, 6, seed=1)
    assert [entry["id"] for entry in picked[:3]] == ["19", "18", "17"] and len(picked) == 6 and len({entry["id"] for entry in picked}) == 6
    assert corpus.pick_weighted(entries, 6, seed=1) == picked
    assert corpus.percentile_rank([10, 20, 30, 40], 40) == 1.0 and corpus.percentile_rank([10, 20, 30, 40], 10) == 0.0


def test_discover_fetch_refresh_and_rank(tmp_path):
    route = FakeRoute()
    counts = corpus.discover(tmp_path, {"channels": ["@chan"], "hashtags": ["robloxstory"], "searches": ["roblox story"]}, 6, 5, route)
    assert counts == {"channel:@chan": 6, "hashtag:robloxstory": 1, "search:roblox story": 1}
    candidates = corpus.read_jsonl(tmp_path / "candidates.jsonl")
    assert len(candidates) == 8 and {row["id"] for row in candidates} >= {"v9", "v8", "v7", "h1", "s1"}
    assert corpus.discover(tmp_path, {"channels": ["@chan"]}, 6, 5, route) == {"channel:@chan": 0}
    assert corpus.fetch(tmp_path, 1, route) == {"fetched": 8}
    assert corpus.fetch(tmp_path, 1, route) == {"have": 8}
    meta = corpus.read_json(tmp_path / "meta" / "v9.json")
    assert meta["url"].endswith("/shorts/v9") and meta["retrieved"] and meta["found_by"] == "channel:@chan" and len(meta["history"]) == 1
    meta["history"][0]["collected"] = "2026-11-01"
    meta["retrieved"] = meta["first_collected"] = "2026-11-01"
    corpus.write_json(tmp_path / "meta" / "v9.json", meta)
    route.views["v9"] = 99999
    tally = corpus.refresh(tmp_path, 1, route)
    assert tally == {"refreshed": 1, "current": 7}
    meta = corpus.read_json(tmp_path / "meta" / "v9.json")
    assert [item["view_count"] for item in meta["history"]] == [10000, 99999] and meta["view_count"] == 99999 and meta["first_collected"] == "2026-11-01"
    assert corpus.rank(tmp_path, route)["ranked"] == 8
    assert corpus.read_json(tmp_path / "meta" / "v9.json")["channel_pct"] == 1.0


def test_fetch_stops_asking_once_blocked(tmp_path):
    route = FakeRoute()
    route.block = {"v%d" % index for index in range(10)}
    corpus.write_jsonl(tmp_path / "candidates.jsonl", [{"id": "v%d" % index} for index in range(10)] + [{"id": "ok"}] * 3)
    tally = corpus.fetch(tmp_path, 1, route)
    assert tally == {"blocked": 13} and route.fetched == []


def test_unknown_route_is_an_error():
    with pytest.raises(collectors.CollectError):
        collectors.get("carrier-pigeon")


# corpus: passes, index, study

def make_corpus(root, count=40):
    """An invented backlog: one channel, shorts that alternate between a scary and a plain story."""
    views = [1000 * (index + 1) for index in range(count)]
    for index in range(count):
        video = "vid%02d" % index
        scary = index % 2 == 0
        sentences = ["In this town, nobody opens the door after dark.", "My sister did not believe it.", "One night she heard a knock.",
                     "Let me in, it said.", "She opened the door.", "Nobody was there.", "In this town, nobody opens the door twice."]
        if not scary:
            sentences = ["My dog can count to ten.", "He barks once for every treat in the jar.", "Today he barked eleven times in a row.", "One more, he said.",
                         "I gave him two.", "Now he will not stop counting.", "He counted the stairs, the cars and the mailman.", "The mailman did not like it."]
        sentences = sentences + ["Then we went home."] * (index % 3)
        text = " ".join(sentences)
        tokens = text.split()
        words = [{"w": token, "start": round(position * 0.25, 2), "end": round(position * 0.25 + 0.2, 2)} for position, token in enumerate(tokens)]
        corpus.write_json(root / "meta" / (video + ".json"), {
            "id": video, "url": "https://www.youtube.com/shorts/" + video, "title": ("In This Town %d" % index) if scary else ("My Dog %d" % index),
            "channel": "Chan", "channel_id": "UC1", "channel_handle": "@chan", "channel_followers": 5000, "upload_date": "20261101", "duration": len(tokens) * 0.25 + 1,
            "view_count": views[index], "like_count": 10, "comment_count": 2, "tags": [], "retrieved": "2026-11-21", "found_by": "channel:@chan",
            "channel_listed": count, "channel_median_views": 12500.0, "channel_pct": corpus.percentile_rank(views, views[index]),
            "views_over_median": round(views[index] / 12500.0, 3)})
        corpus.write_json(root / "transcripts" / (video + ".json"), {"id": video, "model": "invented", "language": "en", "duration": len(tokens) * 0.25 + 1,
                                                                  "text": text, "segments": [{"start": 0, "end": len(tokens) * 0.25, "text": text}], "words": words})
    return views


def annotation_for(prompt):
    scary = "nobody opens the door" in prompt
    return {"is_story": True, "format": "narrated", "genre": "scary" if scary else "comedy", "scary": scary, "person": "first", "tense": "past",
            "hook": "rule" if scary else "claim", "premise_sentence": 0, "turn_sentence": 4, "beats": [{"s": 0, "label": "hook"}, {"s": 4, "label": "turn"}],
            "ending": "loop" if scary else "punchline", "loops": scary, "viewer_address": False,
            "speech": [{"s": 3, "speaker": "the voice" if scary else "dog", "words": "Let me in" if scary else "One more", "intro": "after"}],
            "characters": ["me"], "named": [], "gore": 0, "devices": ["rule broken"] if scary else ["wrong detail"]}


def annotating_client(prompt, **options):
    if options.get("audio") is not None:
        reply = {"voices": [{"id": "V1", "role": "narrator", "description": "adult male", "synthetic": True, "processing": "none"},
                            {"id": "V2", "role": "voice", "description": "low", "synthetic": True, "processing": "pitch shifted"}],
                 "turns": [{"s": 3, "voice": "V2"}], "music": True, "sound_effects": "few", "singing": False}
    else:
        reply = annotation_for(prompt)
    options["ledger"].add({"cost_usd": 0.001})
    return {"text": json.dumps(reply), "model": "fake", "cost_usd": 0.001}


def test_annotate_voices_index_search_export_and_study(tmp_path, monkeypatch):
    monkeypatch.delenv("WRITER_LEDGER", raising=False)
    make_corpus(tmp_path)
    corpus.write_json(tmp_path / "meta" / "short.json", {"id": "short", "title": "x", "view_count": 5, "retrieved": "2026-11-21"})
    corpus.write_json(tmp_path / "transcripts" / "short.json", {"id": "short", "language": "en", "text": "Too short to study.", "words": [], "segments": []})
    tally = annotate.annotate_corpus(tmp_path, None, None, 1.0, 2, client=annotating_client)
    assert tally["annotated"] == 40 and tally["skipped"] == 1 and tally["spent_usd"] == pytest.approx(0.04)
    assert annotate.annotate_corpus(tmp_path, None, None, 1.0, 2, client=annotating_client)["annotated"] == 0
    for path in (tmp_path / "transcripts").glob("vid*.json"):
        (tmp_path / "audio").mkdir(exist_ok=True)
        (tmp_path / "audio" / (path.stem + ".m4a")).write_bytes(b"0")
    assert annotate.voices_sample(tmp_path, None, None, 1.0, 6, 2, client=annotating_client)["voiced"] == 6
    assert annotate.voices_sample(tmp_path, None, None, 1.0, 0, 2, client=annotating_client)["voiced"] == 34

    counts = store.build(tmp_path)
    assert counts == {"shorts": 41, "transcripts": 41, "annotated": 41, "stories": 40, "voices": 40, "snapshots": 41}
    assert store.build(tmp_path)["snapshots"] == 41
    connection = sqlite3.connect(str(tmp_path / "corpus.sqlite"))
    row = connection.execute("SELECT views, days_live, views_per_day, views_per_follower, views_over_median, genre, voices, dialogue_share FROM shorts WHERE id = 'vid00'").fetchone()
    assert row[:5] == (1000, 20.0, 50.0, 0.2, 0.08) and row[5:7] == ("scary", 2) and 0 < row[7] < 0.2
    turns = json.loads(connection.execute("SELECT turns FROM transcripts WHERE id = 'vid00'").fetchone()[0])
    assert [turn["speaker"] for turn in turns] == ["narrator", "the voice", "narrator"] and turns[1]["voice"] == "V2" and turns[1]["spoken"] == ["Let me in"]
    assert turns[1]["start"] == pytest.approx(21 * 0.25) and turns[0]["start"] == 0.0
    connection.close()

    found = store.search(tmp_path, "knock", genre="scary", sort="views_over_median", limit=3)
    assert [row["id"] for row in found] == ["vid38", "vid36", "vid34"]
    assert store.search(tmp_path, "barked", stories=True, min_views=23000)[0]["id"] == "vid39"
    assert store.search(tmp_path, channel="@chan", limit=100)[0]["views"] == 40000
    assert store.export(tmp_path, tmp_path / "out.jsonl", genre="scary") == 20
    first = json.loads((tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert first["transcript"]["turns"] and first["snapshots"][0]["views"] == first["views"] and first["annotation"]["genre"] == "scary"
    assert store.export(tmp_path, tmp_path / "out.csv") == 41 and (tmp_path / "out.csv").read_text(encoding="utf-8").startswith("id,url,title")

    assert analyze.main(["--corpus", str(tmp_path), "--features", str(tmp_path / "features.jsonl"), "--examples", str(tmp_path / "examples.md")]) == 0
    stats = json.loads((tmp_path / "style_stats.json").read_text(encoding="utf-8"))
    assert stats["corpus"]["stories"] == 40 and stats["scary"]["n"] == 20 and stats["top_vs_rest"]["n_top"] == 10
    assert stats["all"]["categorical"]["opener"]["values"]["in this town"]["share"] == 0.5
    assert stats["scary"]["categorical"]["ending"]["values"]["loop"]["share"] == 1.0
    assert stats["all"]["numeric"]["first_sentence_words"]["median"] == 7.5
    assert stats["all"]["speech_intro"] == {"after": 1.0} and stats["all"]["numeric"]["voices"]["median"] == 2.0
    assert stats["scary"]["numeric"]["turn_position"]["median"] > 0.5
    assert "let me in" not in json.dumps(stats).lower() or all(item["scripts"] >= 3 for item in stats["recurring_phrases"])
    assert "genre=scary" in stats["top_vs_rest"]["categorical"]


def test_rank_test_and_share_test():
    low, high = [float(value) for value in range(20)], [float(value) + 15 for value in range(20)]
    result = analyze.rank_test(high, low)
    assert result["auc"] > 0.9 and result["p"] < 0.001
    assert analyze.rank_test(low, low)["auc"] == 0.5 and analyze.rank_test(low, low)["p"] == 1.0
    assert analyze.rank_test([1.0], low) is None
    assert analyze.share_test(18, 20, 2, 20) < 0.001 and analyze.share_test(10, 20, 10, 20) == 1.0


def test_writer_cli_dispatch(capsys):
    from pipeline.writer import __main__ as cli
    assert cli.main(["nonsense"]) == 2
    assert cli.main(["--help"]) == 0
    assert "pipeline.writer write" in capsys.readouterr().out
