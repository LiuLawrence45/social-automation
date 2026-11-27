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
