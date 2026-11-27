from __future__ import annotations

import json
import subprocess

import pytest

from .conftest import CONTENT_START, FFMPEG, FFPROBE, NARRATION_SECONDS, WORDS, needs_ffmpeg

pytestmark = needs_ffmpeg

W, H = 1080, 1920
CENTER = int(H * 0.69)
BAND = (CENTER - 100, CENTER + 100)
OFFSET = int(CONTENT_START * 30)


def gray_frame(path, n):
    proc = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-vf", "select=eq(n\\,%d),format=gray" % n, "-frames:v", "1",
         "-f", "rawvideo", "-"], stdout=subprocess.PIPE, check=True)
    assert len(proc.stdout) == W * H, "frame %d missing" % n
    return proc.stdout


def rows_diff(a, b, y0, y1):
    total = 0
    for y in range(y0, y1):
        ra, rb = a[y * W:(y + 1) * W], b[y * W:(y + 1) * W]
        total += sum(abs(p - q) for p, q in zip(ra[::3], rb[::3]))
    return total / ((y1 - y0) * len(range(0, W, 3)))


def caption_present(job, n):
    out, plate = gray_frame(job / "final.mp4", n), gray_frame(job / "video.mp4", n + OFFSET)
    return rows_diff(out, plate, *BAND), rows_diff(out, plate, 0, BAND[0] - 20) + rows_diff(out, plate, BAND[1] + 20, H)


def probe(job):
    out = subprocess.run([FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(job / "final.mp4")],
                         stdout=subprocess.PIPE, check=True, universal_newlines=True).stdout
    return json.loads(out)


def test_format(rendered):
    job, _ = rendered
    info = probe(job)
    video = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"][0]
    assert (video["width"], video["height"]) == (W, H)
    assert video["r_frame_rate"] == "30/1" and video["avg_frame_rate"] == "30/1"
    assert video["profile"] == "High" and video["pix_fmt"] == "yuv420p"
    assert video["color_space"] == "bt709" and video["color_primaries"] == "bt709" and video["color_transfer"] == "bt709"
    assert audio["codec_name"] == "aac" and audio["sample_rate"] == "48000"
    expected = NARRATION_SECONDS + 0.1
    assert abs(float(info["format"]["duration"]) - expected) <= 1 / 30 + 0.01
    assert (job / "edit_report.json").is_file()


def test_loudness(rendered):
    _, report = rendered
    loud = report["loudness"]["output"]
    assert abs(loud["integrated_lufs"] + 14) <= 1.0
    assert loud["true_peak_dbtp"] <= -0.5
    assert report["caption_events"] == len(WORDS)


def test_caption_in_band_and_absent_outside(rendered):
    job, _ = rendered
    inside, outside = caption_present(job, 40)
    assert inside > 3 * outside + 2
    assert outside < 3


def test_first_word_on_frame_zero(rendered):
    job, _ = rendered
    inside, outside = caption_present(job, 0)
    assert inside > 3 * outside + 2


def test_caption_changes_exactly_on_boundary(rendered):
    job, _ = rendered
    boundary = int(round(0.8 * 30))
    before, after = gray_frame(job / "final.mp4", boundary - 1), gray_frame(job / "final.mp4", boundary)
    plate_before, plate_after = (gray_frame(job / "video.mp4", n + OFFSET) for n in (boundary - 1, boundary))
    same_before = rows_diff(before, plate_before, *BAND)
    same_after = rows_diff(after, plate_after, *BAND)
    previous = gray_frame(job / "final.mp4", boundary - 2)
    assert rows_diff(previous, before, *BAND) < rows_diff(before, plate_before, *BAND) * 0.5 + 1
    assert same_before > 5 and same_after > 5
    assert rows_diff(before, after, *BAND) > rows_diff(previous, before, *BAND) + 3


def test_long_gap_holds_previous_word(rendered):
    job, _ = rendered
    inside, _ = caption_present(job, 80)
    assert inside > 5



def test_bed_source_names_what_the_sound_stage_made(tmp_path):
    import json

    from pipeline import edit as edit_stage

    (tmp_path / "bed.wav").write_bytes(b"")
    (tmp_path / "sound_report.json").write_text(json.dumps({
        "music": {"source": "ElevenLabs music generation"}, "items": [{"kind": "ambience"}, {"kind": "cue"}, {"kind": "cue"}], "pad": None}))
    assert edit_stage.bed_source(tmp_path, tmp_path / "bed.wav") == "sound stage: ElevenLabs music generation, 1 generated ambience loop(s), 2 generated cue(s)"
    assert edit_stage.bed_source(tmp_path, tmp_path / "other.wav") == "other.wav"


import array
import math

from pipeline import edit as edit_stage
from .conftest import make_job, run


def sine(seconds, level, rate=48000):
    return array.array("f", [level * math.sin(2 * math.pi * 220 * i / rate) for i in range(int(seconds * rate))])


def test_music_offset_precedence(tmp_path):
    assert edit_stage.music_offset_setting(tmp_path) == 11.0
    (tmp_path / "story.json").write_text(json.dumps({"mix": {"music_offset_lu": 8.5}}))
    assert edit_stage.music_offset_setting(tmp_path) == 8.5
    assert edit_stage.music_offset_setting(tmp_path, 14) == 14.0
    (tmp_path / "story.json").write_text(json.dumps({"title": "no mix"}))
    assert edit_stage.music_offset_setting(tmp_path) == 11.0
    (tmp_path / "story.json").write_text(json.dumps({"mix": {"music_offset_lu": "loud"}}))
    with pytest.raises(edit_stage.EditError, match="mix.music_offset_lu"):
        edit_stage.music_offset_setting(tmp_path)


def test_measure_levels_on_synthetic_audio():
    words = [{"i": 0, "token": "Hi.", "start": 1.0, "end": 2.0, "sentence": 0},
             {"i": 1, "token": "Yo.", "start": 3.0, "end": 4.0, "sentence": 1}]
    voice = array.array("f", [0.0] * 48000) + sine(1, 0.5) + array.array("f", [0.0] * 48000) + sine(1, 0.5) + array.array("f", [0.0] * 48000)
    bed = sine(5, 0.05)
    levels = edit_stage.measure_levels(voice, bed, words, 5.0)
    assert levels["gap_count"] == 3 and levels["gap_seconds"] == pytest.approx(3.0)
    assert levels["speech_seconds"] == pytest.approx(2.0)
    assert levels["voice_speech_rms_dbfs"] == pytest.approx(-9.03, abs=0.05)
    assert levels["bed_rms_dbfs"] == pytest.approx(-29.03, abs=0.05)
    assert levels["bed_gap_rms_dbfs"] == pytest.approx(-29.03, abs=0.05)
    assert levels["bed_minus_voice_db"] == pytest.approx(-20.0, abs=0.1)
    assert levels["bed_gap_minus_voice_db"] == pytest.approx(-20.0, abs=0.1)


def test_gaps_split_on_sentences_only():
    words = [{"i": 0, "token": "a", "start": 0.0, "end": 1.0}, {"i": 1, "token": "b.", "start": 1.5, "end": 2.0},
             {"i": 2, "token": "c", "start": 3.0, "end": 3.5}]
    assert edit_stage.sentence_gaps(words, 4.0) == [(2.0, 3.0), (3.5, 4.0)]


def test_bed_levels_follow_the_offset(tmp_path):
    job = make_job(tmp_path)
    run([FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=2", "-c:a", "pcm_s16le", str(job / "bed.wav")])
    words = json.loads((job / "words.json").read_text())["words"]
    results = []
    for offset in (8.0, 14.0):
        work = tmp_path / ("work%d" % offset)
        work.mkdir()
        info = edit_stage.build_premaster(job, job / "narration.wav", job / "bed.wav", 5.1, work, offset, words)
        results.append(info["music_levels"])
    assert results[0]["bed_minus_voice_db"] - results[1]["bed_minus_voice_db"] == pytest.approx(6.0, abs=0.05)
    assert results[0]["gap_count"] == 2
    assert results[0]["bed_gap_rms_dbfs"] is not None


def test_tail_follows_the_timeline_and_the_flag_overrides_it(tmp_path, rendered):
    _, flagged = rendered
    assert flagged["tail"] == {"seconds": pytest.approx(0.1, abs=0.02), "source": "flag"}
    job = make_job(tmp_path)
    run([FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=2", "-c:a", "pcm_s16le", str(job / "bed.wav")])
    (job / "story.json").write_text(json.dumps({"mix": {"music_offset_lu": 9}}))
    followed = edit_stage.render(job, job / "final.mp4", job / "bed.wav")
    assert followed["tail"]["source"] == "timeline"
    assert followed["output_duration"] == pytest.approx(5.5, abs=0.05)
    assert followed["tail"]["seconds"] == pytest.approx(0.5, abs=0.04)
    assert followed["music"]["offset_below_voice_lu"] == 9.0
    assert set(followed["music"]["levels"]) >= {"bed_rms_dbfs", "bed_gap_rms_dbfs", "voice_speech_rms_dbfs", "bed_minus_voice_db",
                                                "bed_gap_minus_voice_db", "gap_seconds", "gap_count"}
    overridden = edit_stage.render(job, job / "final2.mp4", None, 0.1, 11)
    assert overridden["tail"]["source"] == "flag"
    assert overridden["output_duration"] == pytest.approx(5.1, abs=0.05)
