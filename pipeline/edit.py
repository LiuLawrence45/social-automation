"""Edit stage: burn one-word captions into video.mp4 and master the narration.

Usage: python -m pipeline.edit --job <folder> [--out final.mp4] [--music bed.wav] [--tail 0.1]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import captions, handoff

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
TARGET_LUFS = -14.0
TARGET_TP = -1.0
MUSIC_OFFSET_LU = 11.0
MUSIC_FADE_SECONDS = 0.3
WIDTH, HEIGHT = 1080, 1920


class EditError(RuntimeError):
    """Raised for failures the operator can act on."""


def ffmpeg_bin() -> str:
    return os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"


def ffprobe_bin() -> str:
    return os.environ.get("FFPROBE") or shutil.which("ffprobe") or "ffprobe"


def run(cmd: List[str]) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    except FileNotFoundError:
        raise EditError("cannot run %s: not found (set FFMPEG / FFPROBE or add FFmpeg to PATH)" % cmd[0])
    if proc.returncode != 0:
        raise EditError("command failed (%d): %s\n%s" % (proc.returncode, " ".join(cmd), proc.stderr[-2000:]))
    return proc


def check_libass() -> None:
    out = run([ffmpeg_bin(), "-hide_banner", "-filters"]).stdout
    if not re.search(r"(?m)^\s*\S+\s+ass\s", out):
        raise EditError("this FFmpeg build has no libass (the 'ass' filter is missing); install an FFmpeg built with --enable-libass")


def probe(path: Path) -> Dict[str, Any]:
    out = run([ffprobe_bin(), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]).stdout
    return json.loads(out)


def _fraction(text: str) -> Fraction:
    num, _, den = text.partition("/")
    return Fraction(int(num), int(den or 1))


def probe_video(path: Path) -> Dict[str, Any]:
    info = probe(path)
    streams = [s for s in info["streams"] if s["codec_type"] == "video"]
    if not streams:
        raise EditError("%s has no video stream" % path)
    stream = streams[0]
    if (stream["width"], stream["height"]) != (WIDTH, HEIGHT):
        raise EditError("video.mp4 must be %dx%d, got %dx%d" % (WIDTH, HEIGHT, stream["width"], stream["height"]))
    fps = _fraction(stream["r_frame_rate"])
    if fps != _fraction(stream["avg_frame_rate"]):
        raise EditError("video.mp4 is not constant frame rate (r_frame_rate %s, avg %s)" % (stream["r_frame_rate"], stream["avg_frame_rate"]))
    frames = int(stream["nb_frames"]) if stream.get("nb_frames", "N/A").isdigit() else int(round(float(info["format"]["duration"]) * fps))
    return {"fps": fps, "frames": frames, "pix_fmt": stream.get("pix_fmt"), "duration": float(frames / fps)}


def audio_duration(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def measure_loudness(inputs: List[str], chain: str) -> Dict[str, float]:
    """Run loudnorm in measure mode over the given inputs and filter chain."""
    filt = "%sloudnorm=I=%s:TP=%s:LRA=11:print_format=json" % (chain, TARGET_LUFS, TARGET_TP)
    proc = run([ffmpeg_bin(), "-hide_banner", "-nostats"] + inputs + ["-af", filt, "-f", "null", "-"])
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.S)
    if not match:
        raise EditError("could not read loudnorm measurement")
    raw = json.loads(match.group(0))
    return {k: float(v) for k, v in raw.items() if k != "normalization_type"}


def loudnorm_apply_filter(measured: Dict[str, float]) -> str:
    return (
        "loudnorm=I=%s:TP=%s:LRA=11:measured_I=%s:measured_TP=%s:measured_LRA=%s:"
        "measured_thresh=%s:offset=%s:linear=true,aresample=48000"
        % (TARGET_LUFS, TARGET_TP, measured["input_i"], measured["input_tp"], measured["input_lra"],
           measured["input_thresh"], measured["target_offset"])
    )


def esc(path: Path) -> str:
    text = str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return text.replace(",", "\\,").replace("[", "\\[").replace("]", "\\]")


def summarize_shots(shots: Dict[str, Any]) -> Dict[str, Any]:
    items = shots["shots"]
    sets: Dict[str, int] = {}
    for shot in items:
        if "set" in shot:
            sets[shot["set"]] = sets.get(shot["set"], 0) + 1
    return {
        "count": len(items),
        "mean_length_seconds": round(sum(s["end"] - s["start"] for s in items) / len(items), 3),
        "emphasis_count": sum(1 for s in items if s.get("emphasis")),
        "sets": sets,
    }


def build_premaster(job: Path, narration: Path, music: Optional[Path], total: float, work: Path) -> Dict[str, Any]:
    """Write the pre-master wav (voice, or voice plus bed) and return measurements."""
    voice_chain = "aresample=48000,apad,atrim=0:%.6f,asetpts=N/SR/TB," % total
    voice_in = ["-i", str(narration)]
    voice_measure = measure_loudness(voice_in, voice_chain + "aformat=channel_layouts=stereo,")
    premaster = work / "premaster.wav"
    info: Dict[str, Any] = {"narration": voice_measure, "premaster": premaster}
    if music is None:
        run([ffmpeg_bin(), "-y", "-hide_banner"] + voice_in + [
            "-af", voice_chain + "aformat=channel_layouts=stereo", "-c:a", "pcm_f32le", "-ar", "48000", str(premaster)])
        return info
    bed_in = ["-stream_loop", "-1", "-t", "%.6f" % total, "-i", str(music)]
    bed_chain = "aresample=48000,aformat=channel_layouts=stereo,"
    bed_measure = measure_loudness(bed_in, bed_chain)
    gain = voice_measure["input_i"] - MUSIC_OFFSET_LU - bed_measure["input_i"]
    graph = (
        "[0:a]%saformat=channel_layouts=stereo[v];"
        "[1:a]aresample=48000,aformat=channel_layouts=stereo,volume=%.3fdB,"
        "atrim=0:%.6f,afade=t=out:st=%.6f:d=%.3f[b];"
        "[v][b]amix=inputs=2:duration=first:normalize=0[m]"
    ) % (voice_chain, gain, total, max(0.0, total - MUSIC_FADE_SECONDS), MUSIC_FADE_SECONDS)
    run([ffmpeg_bin(), "-y", "-hide_banner"] + voice_in + bed_in + [
        "-filter_complex", graph, "-map", "[m]", "-c:a", "pcm_f32le", "-ar", "48000", str(premaster)])
    info.update({"music": bed_measure, "music_gain_db": round(gain, 2)})
    return info


def bed_source(job: Path, music: Path) -> str:
    """Where the bed came from: the sound stage's own description when it made this file, else the file name."""
    report = job / "sound_report.json"
    if music.name == "bed.wav" and music.resolve().parent == job.resolve() and report.is_file():
        with open(report, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        score = (data.get("music") or {}).get("source")
        parts = [score] if score else []
        kinds = [item.get("kind") for item in data.get("items", [])]
        if "ambience" in kinds:
            parts.append("%d generated ambience loop(s)" % kinds.count("ambience"))
        if "cue" in kinds:
            parts.append("%d generated cue(s)" % kinds.count("cue"))
        if data.get("pad"):
            parts.append("synthetic pad")
        return "sound stage: " + ", ".join(parts)
    return music.name


def render(job: Path, out: Path, music: Optional[Path], tail: float) -> Dict[str, Any]:
    started = time.time()
    check_libass()
    video_path, narration = job / "video.mp4", job / "narration.wav"
    for path in (video_path, narration):
        if not path.is_file():
            raise EditError("missing %s" % path)
    video = probe_video(video_path)
    fps: Fraction = video["fps"]
    data = handoff.load_job(job, video["duration"])
    words, shots = data["words"], data["shots"]
    if abs(Fraction(str(shots["fps"])) - fps) > Fraction(1, 1000):
        raise EditError("shots.json fps %s does not match video.mp4 fps %s" % (shots["fps"], float(fps)))
    narration_duration = audio_duration(narration)
    if abs(narration_duration - words["duration"]) > 0.25:
        raise EditError("words.json duration %.3f does not match narration.wav %.3f" % (words["duration"], narration_duration))

    out_frames = int(round((narration_duration + tail) * fps))
    total = float(out_frames / fps)
    first = int(round(Fraction(str(shots["content_start"])) * fps))
    last = min(int(round(Fraction(str(shots["content_end"])) * fps)), video["frames"])
    events = captions.caption_events(words["words"], fps, out_frames)

    work = Path(tempfile.mkdtemp(prefix="edit-"))
    try:
        ass_path = work / "captions.ass"
        ass_path.write_text(captions.build_ass(events, fps, WIDTH, HEIGHT), encoding="utf-8")
        audio = build_premaster(job, narration, music, total, work)
        measured = measure_loudness(["-i", str(audio["premaster"])], "")
        vf = (
            "trim=start_frame=%d:end_frame=%d,setpts=PTS-STARTPTS,"
            "tpad=stop_mode=clone:stop=-1,fps=%s,"
            "ass=filename=%s:fontsdir=%s,format=yuv420p"
        ) % (first, last, "%d/%d" % (fps.numerator, fps.denominator), esc(ass_path), esc(FONTS_DIR))
        out.parent.mkdir(parents=True, exist_ok=True)
        run([
            ffmpeg_bin(), "-y", "-hide_banner", "-i", str(video_path), "-i", str(audio["premaster"]),
            "-filter_complex", "[0:v]%s[v];[1:a]%s[a]" % (vf, loudnorm_apply_filter(measured)),
            "-map", "[v]", "-map", "[a]", "-frames:v", str(out_frames),
            "-c:v", "libx264", "-profile:v", "high", "-preset", "medium", "-b:v", "8M", "-maxrate", "10M", "-bufsize", "16M",
            "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
            "-r", "%d/%d" % (fps.numerator, fps.denominator), "-fps_mode", "cfr",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-t", "%.6f" % total,
            "-movflags", "+faststart", str(out),
        ])
        result = measure_loudness(["-i", str(out)], "")
        out_info = probe(out)
        out_video = [s for s in out_info["streams"] if s["codec_type"] == "video"][0]
        report = {
            "job": str(job),
            "output": str(out),
            "input_video_duration": round(video["duration"], 3),
            "input_narration_duration": round(narration_duration, 3),
            "output_duration": round(float(out_info["format"]["duration"]), 3),
            "fps": "%d/%d" % (fps.numerator, fps.denominator),
            "output_frames": out_frames,
            "picture_frames_used": [first, min(last, first + out_frames)],
            "picture_held_frames": max(0, out_frames - (last - first)),
            "loudness": {
                "target": {"integrated_lufs": TARGET_LUFS, "true_peak_dbtp": TARGET_TP},
                "input_narration": {"integrated_lufs": audio["narration"]["input_i"], "true_peak_dbtp": audio["narration"]["input_tp"]},
                "premaster": {"integrated_lufs": measured["input_i"], "true_peak_dbtp": measured["input_tp"]},
                "output": {"integrated_lufs": result["input_i"], "true_peak_dbtp": result["input_tp"]},
            },
            "music": None if music is None else {
                "file": str(music), "bed_integrated_lufs": audio["music"]["input_i"], "gain_db": audio["music_gain_db"],
                "offset_below_voice_lu": MUSIC_OFFSET_LU, "source": bed_source(job, music)},
            "caption_events": len(events),
            "shots": summarize_shots(shots),
            "video_codec": {"codec": out_video["codec_name"], "profile": out_video.get("profile"), "pix_fmt": out_video["pix_fmt"]},
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)
    report["wall_clock_seconds"] = round(time.time() - started, 2)
    (out.parent / "edit_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.edit", description=__doc__.splitlines()[0])
    parser.add_argument("--job", required=True, type=Path, help="job folder")
    parser.add_argument("--out", default="final.mp4", help="output file; a relative name is placed in the job folder")
    parser.add_argument("--music", type=Path, help="optional music bed")
    parser.add_argument("--tail", type=float, default=0.1, help="seconds after the narration ends (default 0.1)")
    args = parser.parse_args(argv)
    out = Path(args.out)
    if not out.is_absolute() and out.parent == Path("."):
        out = args.job / out
    try:
        if args.music is not None and not args.music.is_file():
            raise EditError("music file %s not found" % args.music)
        report = render(args.job, out, args.music, args.tail)
    except (EditError, handoff.HandoffError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print("wrote %s (%.2f s, %d caption events, %.1f LUFS)" % (
        out, report["output_duration"], report["caption_events"], report["loudness"]["output"]["integrated_lufs"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
