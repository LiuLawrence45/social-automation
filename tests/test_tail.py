from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline import handoff, sound, story

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "found-box"


def fake_words(script_story, pace=0.3):
    tokens = story.tokenize(script_story["script"])
    words = [{"i": i, "token": token, "display": token, "start": round(0.1 + i * pace, 3),
              "end": round(0.1 + i * pace + 0.25, 3), "sentence": 0} for i, token in enumerate(tokens)]
    return {"version": 1, "audio": "narration.wav", "duration": round(words[-1]["end"] + 0.2, 3), "words": words}


def with_tail(holds=(1.5, 1.0)):
    loaded = story.load_story(EXAMPLE)
    shotlist = copy.deepcopy(loaded["shotlist"])
    last = shotlist["shots"][-1]
    for number, hold in enumerate(holds):
        silent = {key: value for key, value in copy.deepcopy(last).items() if key not in ("text", "emphasis")}
        silent.update({"id": "tail%d" % number, "hold": hold})
        shotlist["shots"].append(silent)
    return loaded["story"], shotlist


def test_silent_shots_extend_the_timeline_past_the_narration():
    script_story, shotlist = with_tail()
    story.validate_shotlist(shotlist, script_story)
    words = fake_words(script_story)
    plain = story.build_timeline(script_story, story.load_story(EXAMPLE)["shotlist"], words)
    timeline = story.build_timeline(script_story, shotlist, words)
    first, second = timeline["shots"][-2:]
    assert first["frame_start"] == plain["frames"] and first["frame_end"] - first["frame_start"] == 45
    assert second["frame_start"] == first["frame_end"] and second["frame_end"] == timeline["frames"] == plain["frames"] + 75
    assert timeline["tail"] == pytest.approx(2.5) and plain["tail"] == 0
    assert "word_start" not in first and timeline["shots"][-3]["frame_end"] == plain["frames"]
    assert timeline["narration_duration"] == words["duration"]


def test_handoff_leaves_word_ranges_off_silent_shots():
    script_story, shotlist = with_tail((2.0,))
    words = fake_words(script_story)
    timeline = story.build_timeline(script_story, shotlist, words)
    shots = handoff.validate_shots(story.shots_handoff(timeline), word_count=len(words["words"]), video_duration=timeline["duration"])
    assert "word_start" not in shots["shots"][-1] and "word_end" in shots["shots"][-2]
    assert shots["content_end"] == timeline["duration"] == pytest.approx(timeline["narration_duration"] + 0.1 + 2.0, abs=1 / 30)


def test_silent_shots_must_follow_the_last_word_and_carry_no_text():
    script_story, shotlist = with_tail((1.0,))
    moved = copy.deepcopy(shotlist)
    moved["shots"].insert(2, moved["shots"].pop())
    with pytest.raises(story.StoryError, match="silent shots go after the last word"):
        story.validate_shotlist(moved, script_story)
    for change, message in (({"hold": 0}, "positive number of seconds"), ({"text": "and then"}, "both text and hold")):
        broken = copy.deepcopy(shotlist)
        broken["shots"][-1].update(change)
        with pytest.raises(story.StoryError, match=message):
            story.validate_shotlist(broken, script_story)


def sound_job(tmp_path, config, picture):
    job = tmp_path / "job"
    job.mkdir()
    words = [{"i": 0, "start": 0.2, "end": 0.5}]
    (job / "words.json").write_text(json.dumps({"version": 1, "duration": 2.0, "words": words}))
    (job / "timeline.json").write_text(json.dumps({"version": 1, "duration": picture, "shots": [{"id": "a", "start": 0.0}, {"id": "tail", "start": 2.1}]}))
    (job / "sound.json").write_text(json.dumps(config))
    return job


def test_bed_covers_the_picture_and_takes_cues_in_the_tail(tmp_path):
    config = {"drone": {"root_hz": 55, "gain_db": -6}, "cues": [{"name": "last", "prompt": "low hit", "seconds": 1, "at": {"shot": "tail", "offset": 0.5}}]}
    job = sound_job(tmp_path, config, picture=4.5)
    report = sound.make_bed(job, provider="synthetic")
    assert report["duration"] == 4.5 and report["items"][0]["start"] == 2.6
    assert len(sound.read_wav(job / "bed.wav")) == int(4.5 * sound.RATE)


def test_drone_never_drops_out_even_where_the_score_is_cut(tmp_path):
    config = {"normalise": True, "drone": {"root_hz": 55, "gain_db": -6},
              "music": {"prompt": "slow uneasy score", "duck_db": -3.5, "cuts": [{"at": 1.0, "seconds": 1.5}]}}
    job = sound_job(tmp_path, config, picture=4.0)
    report = sound.make_bed(job, provider="synthetic")
    assert report["drone"]["source"].startswith("synthesised") and report["music"]["duck_db"] == -3.5
    window = report["quietest_window"]
    assert 1.0 <= window["at"] <= 2.5 and window["rms_dbfs"] > -40 and window["below_overall_db"] < 18
    without = dict(config)
    del without["drone"]
    (job / "sound.json").write_text(json.dumps(without))
    assert sound.make_bed(job, provider="synthetic")["quietest_window"]["rms_dbfs"] < -70


def test_drone_alone_is_a_valid_bed_and_bad_roots_are_refused(tmp_path):
    job = sound_job(tmp_path, {"drone": {}}, picture=3.0)
    assert sound.make_bed(job, provider="synthetic")["drone"]["root_hz"] == 55.0
    (job / "sound.json").write_text(json.dumps({"drone": {"root_hz": 5}}))
    with pytest.raises(sound.SoundError, match="between 20 and 400"):
        sound.make_bed(job, provider="synthetic")
