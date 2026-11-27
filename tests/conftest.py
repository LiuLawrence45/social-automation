from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

needs_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE and "libass" in subprocess.run(
        [FFMPEG or "ffmpeg", "-hide_banner", "-buildconf"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True).stdout) if FFMPEG else True,
    reason="FFmpeg with libass is not available",
)

CONTENT_START = 0.5
VIDEO_SECONDS = 6.0
NARRATION_SECONDS = 5.0

WORDS = [
    ("Nine", "9", 0.12, 0.38), ("people", "people", 0.40, 0.80), ("went", "went", 0.80, 1.00),
    ("in", "in", 1.00, 1.06), ("Mara's", "Mara's", 1.06, 1.50), ("house", "house", 1.50, 1.90),
    ("and", "and", 1.90, 2.10), ("nobody", "nobody", 3.30, 3.80), ("came", "came", 3.80, 4.10),
    ("out", "out", 4.10, 4.40), ("alive", "alive", 4.40, 4.80), ("tonight", "tonight", 4.80, 5.00),
]


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def make_job(root: Path) -> Path:
    job = root / "job"
    job.mkdir()
    run([FFMPEG, "-y", "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=%s" % VIDEO_SECONDS,
         "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", "-an", str(job / "video.mp4")])
    run([FFMPEG, "-y", "-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.2:sample_rate=48000:duration=%s" % NARRATION_SECONDS,
         "-ac", "1", "-c:a", "pcm_s16le", str(job / "narration.wav")])
    words = [{"i": i, "token": t, "display": d, "start": s, "end": e, "sentence": 0} for i, (t, d, s, e) in enumerate(WORDS)]
    (job / "words.json").write_text(json.dumps(
        {"version": 1, "audio": "narration.wav", "duration": NARRATION_SECONDS, "words": words}))
    (job / "shots.json").write_text(json.dumps({
        "version": 1, "fps": 30, "width": 1080, "height": 1920,
        "content_start": CONTENT_START, "content_end": VIDEO_SECONDS,
        "shots": [{"id": "s01", "start": 0.0, "end": 3.0, "word_start": 0, "word_end": 6, "set": "street", "emphasis": False},
                  {"id": "s02", "start": 3.0, "end": 6.0, "word_start": 6, "word_end": 12}]}))
    return job


@pytest.fixture(scope="session")
def rendered(tmp_path_factory):
    if not (FFMPEG and FFPROBE):
        pytest.skip("FFmpeg is not available")
    from pipeline import edit
    job = make_job(tmp_path_factory.mktemp("edit"))
    report = edit.render(job, job / "final.mp4", None, 0.1)
    return job, report
