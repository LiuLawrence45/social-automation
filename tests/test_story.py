from __future__ import annotations

import copy
import json
import math
import shutil
from pathlib import Path

import pytest

from pipeline import handoff, story

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "found-box"


def fake_words(script_story, pace=0.3):
    tokens = story.tokenize(script_story["script"])
    shown = story.display_words(script_story)
    sentences = story.sentence_ids(tokens)
    words = [{"i": i, "token": token, "display": shown[i], "start": round(0.1 + i * pace, 3),
              "end": round(0.1 + i * pace + 0.25, 3), "sentence": sentences[i]} for i, token in enumerate(tokens)]
    return {"version": 1, "audio": "narration.wav", "duration": round(words[-1]["end"] + 0.2, 3), "words": words}


def test_sentences_keep_abbreviations_together():
    tokens = story.tokenize("Mrs. Hale waved. I waved back.")
    assert story.sentence_ids(tokens) == [0, 0, 0, 1, 1, 1]


def test_example_story_loads_and_shots_cover_the_script():
    loaded = story.load_story(EXAMPLE)
    tokens = story.tokenize(loaded["story"]["script"])
    mapped = story.map_shots(loaded["story"], loaded["shotlist"])
    assert mapped[0]["word_start"] == 0
    assert mapped[-1]["word_end"] == len(tokens) - 1
    for before, after in zip(mapped, mapped[1:]):
        assert after["word_start"] == before["word_end"] + 1


def test_display_overrides_and_numbers():
    sample = {"script": "Twenty-four notes. Mrs. Hale", "names": ["Mrs", "Hale"], "display_overrides": {"Twenty-four": "24"}}
    assert story.display_words(sample) == ["24", "notes", "Mrs", "Hale"]


def test_shot_text_must_match_the_script():
    loaded = story.load_story(EXAMPLE)
    shotlist = copy.deepcopy(loaded["shotlist"])
    shotlist["shots"][1]["text"] = "I found a crate by my door."
    with pytest.raises(story.StoryError, match="does not match script word"):
        story.validate_shotlist(shotlist, loaded["story"])


def test_shots_must_cover_every_word():
    loaded = story.load_story(EXAMPLE)
    shotlist = copy.deepcopy(loaded["shotlist"])
    shotlist["shots"].pop()
    with pytest.raises(story.StoryError, match="shots cover"):
        story.validate_shotlist(shotlist, loaded["story"])


def test_cast_roles_must_be_declared():
    loaded = story.load_story(EXAMPLE)
    shotlist = copy.deepcopy(loaded["shotlist"])
    shotlist["shots"][1]["cast"][0]["who"] = "stranger"
    with pytest.raises(story.StoryError, match="who must be one of"):
        story.validate_shotlist(shotlist, loaded["story"])


def test_timeline_cuts_on_word_starts_and_fills_the_narration():
    loaded = story.load_story(EXAMPLE)
    words = fake_words(loaded["story"])
    timeline = story.build_timeline(loaded["story"], loaded["shotlist"], words, fps=30, tail=0.1)
    shots = timeline["shots"]
    assert shots[0]["frame_start"] == 0
    assert timeline["frames"] == int(math.ceil((words["duration"] + 0.1) * 30 - 1e-6))
    assert shots[-1]["frame_end"] == timeline["frames"]
    for before, after in zip(shots, shots[1:]):
        assert before["frame_end"] == after["frame_start"]
        assert after["frame_start"] == round(words["words"][after["word_start"]]["start"] * 30)
    assert timeline["cast"] == loaded["shotlist"]["cast"]


def test_shots_handoff_passes_the_edit_stage_validator():
    loaded = story.load_story(EXAMPLE)
    words = fake_words(loaded["story"])
    timeline = story.build_timeline(loaded["story"], loaded["shotlist"], words)
    handoff.validate_words(words)
    shots = handoff.validate_shots(story.shots_handoff(timeline), word_count=len(words["words"]), video_duration=timeline["duration"])
    assert shots["shots"][4]["emphasis"] is True
    assert shots["content_end"] == timeline["duration"]


def test_cli_writes_timeline(tmp_path, capsys):
    job = tmp_path / "job"
    shutil.copytree(str(EXAMPLE), str(job))
    assert story.main(["check", "--job", str(job)]) == 0
    assert "10 shots" in capsys.readouterr().out
    with open(job / "words.json", "w", encoding="utf-8") as handle:
        json.dump(fake_words(story.load_story(job)["story"]), handle)
    assert story.main(["timeline", "--job", str(job)]) == 0
    timeline = json.loads((job / "timeline.json").read_text(encoding="utf-8"))
    assert len(timeline["shots"]) == 10 and timeline["fps"] == 30


def test_cli_reports_a_broken_story(tmp_path, capsys):
    job = tmp_path / "job"
    shutil.copytree(str(EXAMPLE), str(job))
    broken = json.loads((job / "story.json").read_text(encoding="utf-8"))
    broken["slug"] = "Not A Slug"
    (job / "story.json").write_text(json.dumps(broken), encoding="utf-8")
    assert story.main(["check", "--job", str(job)]) == 2
    assert "slug" in capsys.readouterr().err
