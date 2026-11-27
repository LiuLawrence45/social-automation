"""Quality stage: judge the finished video shot by shot, from rendered frames and the story's words only.

Usage:
  python -m pipeline.quality --job <folder> [--video FILE] [--final FILE] [--shots a,b,c] [--judges x,y]
      [--provider P] [--model M] [--ceiling USD] [--workers N] [--set judge.key=value] [--out FILE]
      [--posture FILE] [--human FILE] [--baseline FILE] [--compare FILE] [--report-only] [--changed] [--confirm]

One judge per concern (posture, expression, clipping, framing, lighting, story, continuity, motion) scores every shot on
labelled sheets of frames cut from the rendered video, and one more judges the video as a whole. Rubrics,
thresholds and frames per shot live in pipeline/quality_rubric.json; story.json "quality" and --set override
them, and "accepted" there lists what this one video has ruled acceptable and "sets" describes a set to the judges. Model calls go through pipeline.vision and run in a thread pool under one spend ceiling that also counts
earlier runs against the same job (quality/ledger.json). Geometric results in <job>/posture.json (or --posture) are merged
for the shots judged in the run:
a geometric fail fails the shot even when the picture passes, and the reverse. A shot fails when any judge
fails it; a judge that could not answer leaves the shot incomplete, never passed. A fail that a reader has read from
the frames and overruled in writing (<job>/quality/overrules.json, with frame numbers) no longer fails the shot, and
the report prints every overrule beside the judge's reason. Every verdict keeps a
fingerprint of the frames it was given on: when a shot is rendered again its verdicts are set aside as stale,
and --changed judges exactly those again. A fail that is close to the threshold, or the only fail on its shot, is asked
again and settled by the majority of three votes; a fail backed by geometry or by measurement is not. The stage writes
<job>/quality.json, <job>/QUALITY.md and the sheets every judge saw under <job>/quality/sheets/.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import vision
from .edit import EditError, ffmpeg_bin, run

RUBRIC_PATH = Path(__file__).resolve().parent / "quality_rubric.json"
SHOT_JUDGES = ("posture", "expression", "clipping", "framing", "lighting", "story", "continuity", "motion")
VIDEO_JUDGE = "video"
GEOMETRY_JUDGES = {"joint_limit": "posture", "stiff_arms": "posture", "idle": "posture", "reach_miss": "posture",
                   "arm_out": "posture", "arms_out": "posture", "arms_splayed": "posture", "stance_wide": "posture",
                   "no_action": "posture", "hand_off_target": "posture",
                   "intersect": "clipping", "floating": "clipping", "sunk": "clipping", "prop_missing": "clipping",
                   "floating_piece": "clipping", "duplicate_piece": "clipping", "frozen_face": "expression"}
SET_PIECES = {"floating_piece": "floating set piece", "duplicate_piece": "set piece built twice"}
MEASURE_SIZE = (36, 64)
LIT_LEVEL = 40
STILL_CHANGE = 0.05
RENDER_FRAMES = 5
RENDER_TOLERANCE = 2
GLYPHS = {"A": "EHHVHHH", "B": "UHHUHHU", "C": "EHGGGHE", "D": "UHHHHHU", "E": "VGGUGGV", "F": "VGGUGGG", "G": "EHGNHHF",
          "H": "HHHVHHH", "I": "E44444E", "J": "72222IC", "K": "HIKOKIH", "L": "GGGGGGV", "M": "HRLLHHH", "N": "HPLJHHH",
          "O": "EHHHHHE", "P": "UHHUGGG", "Q": "EHHHLID", "R": "UHHUKIH", "S": "FGGE11U", "T": "V444444", "U": "HHHHHHE",
          "V": "HHHHHA4", "W": "HHHLLRH", "X": "HHA4AHH", "Y": "HHA4444", "Z": "V1248GV", "0": "EHJLPHE", "1": "4C4444E",
          "2": "EH1248V", "3": "U11E11U", "4": "26AIV22", "5": "VGU11HE", "6": "68GUHHE", "7": "V124888", "8": "EHHEHHE",
          "9": "EHHF12C", ".": "00000CC", ":": "0CC0CC0", "-": "000V000", "_": "000000V", "/": "11248GG", " ": "0000000"}


class QualityError(RuntimeError):
    """Raised for failures the operator can act on."""


class SharedBudget(vision.Budget):
    """vision.Budget for a thread pool: take() reserves room for one call so the ceiling holds across threads."""

    def __init__(self, ceiling_usd: float, reserve_usd: float, prior_usd: float = 0.0) -> None:
        super().__init__(ceiling_usd)
        self.reserve_usd = float(reserve_usd)
        self.prior_usd = float(prior_usd)
        self._held = 0.0
        self._lock = threading.Lock()

    def take(self) -> None:
        with self._lock:
            if self.prior_usd + self.cost_usd + self._held + self.reserve_usd > self.ceiling_usd:
                self.stopped_by_ceiling = True
                raise vision.BudgetExceeded("spent $%.4f of the $%.2f ceiling" % (self.prior_usd + self.cost_usd, self.ceiling_usd))
            self._held += self.reserve_usd

    def settle(self, result: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self._held -= self.reserve_usd
            if result is not None:
                self.add(result)


def read_json(path: Path, required: bool = True) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        if required:
            raise QualityError("%s not found" % path)
        return None
    except ValueError as exc:
        raise QualityError("%s is not valid JSON: %s" % (path, exc))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    """Deep merge of dictionaries; lists and scalars in extra replace those in base."""
    merged = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def parse_sets(items: Sequence[str]) -> Dict[str, Any]:
    """Turn --set posture.frames=4 style items into an override tree; values are read as JSON when they parse."""
    overrides: Dict[str, Any] = {}
    for item in items:
        path, sep, raw = item.partition("=")
        if not sep or not path:
            raise QualityError("--set needs key=value, got %r" % item)
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        parts = path.split(".")
        if parts[0] in SHOT_JUDGES:
            parts = ["judges"] + parts
        node = overrides
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return overrides


def load_settings(story: Dict[str, Any], overrides: Optional[Dict[str, Any]] = None, rubric_path: Path = RUBRIC_PATH) -> Dict[str, Any]:
    """The rubric file, then story.json "quality", then explicit overrides; prices start from story.json "judge"."""
    settings = merge(read_json(rubric_path), story.get("quality") or {})
    settings = merge(settings, overrides or {})
    settings["prices"] = merge((story.get("judge") or {}).get("prices") or {}, settings.get("prices") or {})
    return settings


def accepted_lines(settings: Dict[str, Any]) -> List[str]:
    """What this job has ruled acceptable (story.json "quality" "accepted"), as lines every judge is told."""
    notes = settings.get("accepted") or []
    return ["Accepted for this video and not a defect: %s" % note for note in ([notes] if isinstance(notes, str) else notes)]


def spoken(raw: Dict[str, Any], words: Sequence[Dict[str, Any]]) -> str:
    """The words a shot covers with their speakers, as SPEAKER: words / SPEAKER: words."""
    if "word_start" not in raw or "word_end" not in raw:
        return str(raw.get("text") or "")
    parts: List[List[str]] = []
    for word in words[int(raw["word_start"]):int(raw["word_end"]) + 1]:
        speaker = str(word.get("speaker") or "narrator").upper()
        if not parts or parts[-1][0] != speaker:
            parts.append([speaker])
        parts[-1].append(str(word.get("token", word.get("display", ""))))
    return " / ".join("%s: %s" % (p[0], " ".join(p[1:])) for p in parts)


def load_shots(job: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """shots.json with each shot's set (from the timeline or shot list when missing) and spoken line."""
    meta = read_json(job / "shots.json")
    words = (read_json(job / "words.json", required=False) or {}).get("words", [])
    sets: Dict[str, str] = {}
    for name in ("timeline.json", "shotlist.json"):
        for shot in (read_json(job / name, required=False) or {}).get("shots", []):
            if isinstance(shot, dict) and shot.get("id") and shot.get("set"):
                sets.setdefault(str(shot["id"]), str(shot["set"]))
    shots = []
    for raw in meta.get("shots") or []:
        shot_id = str(raw["id"])
        shots.append({"id": shot_id, "start": float(raw["start"]), "end": float(raw["end"]),
                      "set": str(raw.get("set") or sets.get(shot_id) or "unknown"), "line": spoken(raw, words)})
    if not shots:
        raise QualityError("%s lists no shots" % (job / "shots.json"))
    return meta, shots


def script_text(story: Dict[str, Any], shots: Sequence[Dict[str, Any]]) -> str:
    lines = story.get("lines")
    if isinstance(lines, list) and lines:
        return "\n".join("%s: %s" % (str(l.get("speaker", "narrator")).upper(), l.get("text", "")) for l in lines if isinstance(l, dict))
    return "\n".join(s["line"] for s in shots if s["line"])


def frame_range(shot: Dict[str, Any], fps: float) -> Tuple[int, int]:
    first = int(round(shot["start"] * fps))
    return first, max(first, int(round(shot["end"] * fps)) - 1)


def frame_numbers(shot: Dict[str, Any], fps: float, count: int) -> List[int]:
    """Frame numbers spread evenly across a shot, each in the middle of its slice."""
    first, last = frame_range(shot, fps)
    span = last - first + 1
    return sorted({min(last, first + int((i + 0.5) * span / max(1, count))) for i in range(max(1, count))})


def label_png(text: str, path: Path, scale: int = 3) -> Path:
    """Write white-on-black label text as a greyscale PNG using the built-in 5x7 font."""
    glyphs = [GLYPHS.get(ch, GLYPHS["-"]) for ch in text.upper()]
    width, height = (len(glyphs) * 6 + 1) * scale, 9 * scale
    rows = []
    for y in range(9):
        row = bytearray(len(glyphs) * 6 + 1)
        if 1 <= y <= 7:
            for index, glyph in enumerate(glyphs):
                bits = int(glyph[y - 1], 32)
                for x in range(5):
                    if bits & (16 >> x):
                        row[index * 6 + 1 + x] = 255
        line = b"".join(bytes([value]) * scale for value in row)
        rows += [b"\x00" + line] * scale

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))
    return path


def build_sheet(tiles: Sequence[Dict[str, Any]], out: Path, height: int, columns: int) -> None:
    """Cut one frame per tile ({"video", "time", "label"}) and stack them into one labelled JPEG.

    A tile's time is its frame number over the frame rate: FFmpeg rounds the seek to the stream's clock, so
    half a frame either way can land on a neighbour.
    """
    width = int(round(height * 9 / 16 / 2.0)) * 2
    work = Path(tempfile.mkdtemp(prefix="quality-"))
    try:
        cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error"]
        for tile in tiles:
            cmd += ["-ss", "%.6f" % tile["time"], "-i", str(tile["video"])]
        graph = []
        for index, tile in enumerate(tiles):
            cmd += ["-i", str(label_png(tile["label"], work / ("label%d.png" % index), max(2, height // 240)))]
            graph.append("[%d:v]scale=%d:%d[s%d];[s%d][%d:v]overlay=4:4[t%d]" % (index, width, height, index, index, len(tiles) + index, index))
        if len(tiles) == 1:
            graph.append("[t0]format=yuvj420p[out]")
        else:
            layout = "|".join("%d_%d" % ((i % columns) * width, (i // columns) * height) for i in range(len(tiles)))
            graph.append("%sxstack=inputs=%d:layout=%s:fill=black,format=yuvj420p[out]"
                         % ("".join("[t%d]" % i for i in range(len(tiles))), len(tiles), layout))
        out.parent.mkdir(parents=True, exist_ok=True)
        run(cmd + ["-filter_complex", ";".join(graph), "-map", "[out]", "-frames:v", "1", "-q:v", "3", str(out)])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not out.is_file():
        raise QualityError("could not cut frames for %s" % out.name)


def measure(video: Path) -> List[Dict[str, float]]:
    """Per frame of the video: mean brightness (0 to 255), share of lit pixels, change from the frame before, and 4x4 block means."""
    width, height = MEASURE_SIZE
    try:
        proc = subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", str(video), "-vf",
                               "scale=%d:%d,format=gray" % (width, height), "-f", "rawvideo", "-"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise QualityError("cannot run FFmpeg (set FFMPEG or add it to PATH)")
    if proc.returncode != 0:
        raise QualityError("could not read %s: %s" % (video, proc.stderr.decode("utf-8", "replace")[-500:]))
    size = width * height
    lit_table = bytes(1 if value >= LIT_LEVEL else 0 for value in range(256))
    frames: List[Dict[str, float]] = []
    previous = b""
    for offset in range(0, len(proc.stdout) - size + 1, size):
        frame = proc.stdout[offset:offset + size]
        change = sum(abs(a - b) for a, b in zip(frame, previous)) / size if previous else 0.0
        cells = bytes(sum(sum(frame[(row + k) * width + col:(row + k) * width + col + 4]) for k in range(4)) // 16
                      for row in range(0, height - 3, 4) for col in range(0, width - 3, 4))
        frames.append({"luma": sum(frame) / size, "lit": sum(frame.translate(lit_table)) / size, "change": change, "cells": cells})
        previous = frame
    return frames


def shot_measures(frames: Sequence[Dict[str, float]], first: int, last: int) -> Optional[Dict[str, Any]]:
    """Brightness and frame-to-frame change inside one shot; the first frame's change is the cut and is left out."""
    inside = list(frames[first:last + 1])
    if not inside:
        return None
    changes = [f["change"] for f in inside[1:]]
    ordered = sorted(changes)
    peak = max(changes) if changes else 0.0
    return {
        "luma": round(sum(f["luma"] for f in inside) / len(inside), 2),
        "lit_fraction": round(sum(f["lit"] for f in inside) / len(inside), 4),
        "change_mean": round(sum(changes) / len(changes), 3) if changes else 0.0,
        "change_median": round(ordered[len(ordered) // 2], 3) if ordered else 0.0,
        "change_peak": round(peak, 3),
        "change_peak_frame": first + 1 + changes.index(peak) if changes else first,
        "still_fraction": round(sum(1 for c in changes if c < STILL_CHANGE) / len(changes), 3) if changes else 1.0,
    }


def render_signature(frames: Sequence[Dict[str, Any]], first: int, last: int) -> bytes:
    """Fingerprint of one shot as rendered: block means of five frames across it and the brightness of every frame."""
    inside = list(frames[max(0, first):last + 1])
    if not inside:
        return b""
    picks = [inside[round(i * (len(inside) - 1) / (RENDER_FRAMES - 1))] for i in range(RENDER_FRAMES)]
    return b"".join(f.get("cells", b"") for f in picks) + bytes(min(255, int(round(f["luma"]))) for f in inside)


def same_render(one: bytes, other: bytes) -> bool:
    """True when two fingerprints differ by no more than encoding the same picture again would cause."""
    return len(one) == len(other) and all(abs(a - b) <= RENDER_TOLERANCE for a, b in zip(one, other))


def current_renders(videos: Dict[str, Path], shots: Sequence[Dict[str, Any]],
                    meta: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, bytes]], Dict[str, List[Dict[str, Any]]]]:
    """Fingerprint of every shot in each video that exists (by kind, then shot id), and the measured frames of each video."""
    fps = float(meta["fps"])
    decoded = {kind: measure(path) for kind, path in videos.items() if path.is_file()}
    renders: Dict[str, Dict[str, bytes]] = {}
    for kind, frames in decoded.items():
        shift = int(round(float(meta.get("content_start", 0.0)) * fps)) if kind == "captioned" else 0
        renders[kind] = {}
        for shot in shots:
            first, last = frame_range(shot, fps)
            renders[kind][shot["id"]] = render_signature(frames, first - shift, last - shift)
    return renders, decoded


def set_aside(doc: Dict[str, Any], current: Dict[str, Dict[str, bytes]], kinds: Dict[str, str]) -> None:
    """Mark as stale every verdict given on frames that have since been rendered again; a stale verdict no longer counts."""
    registry = doc.get("renders") or {}
    records = [(kinds.get(key, "clean"), record) for shot in doc.get("shots") or [] for key, record in shot["judges"].items()]
    if isinstance(doc.get("video_judge"), dict):
        records.append(("clean", doc["video_judge"]))
    for kind, record in records:
        now = current.get(kind) or {}
        changed = any(shot_id in now and mark in registry and not same_render(bytes.fromhex(registry[mark]), now[shot_id])
                      for shot_id, mark in (record.get("render") or {}).items())
        if changed and record.get("status") != "stale":
            record.update(judged=record.get("status"), status="stale")


def plan_sheet(spec: Dict[str, Any], index: int, shots: Sequence[Dict[str, Any]], meta: Dict[str, Any], videos: Dict[str, Path]) -> Dict[str, Any]:
    """Which frames a judge sees for a shot: the tiles, their layout and the sheet's file name."""
    shot = shots[index]
    fps = float(meta["fps"])
    kind = str(spec.get("video", "clean"))
    shift = float(meta.get("content_start", 0.0)) if kind == "captioned" else 0.0

    def tile(target: Dict[str, Any], frame: int, role: str = "") -> Dict[str, Any]:
        return {"video": videos[kind], "time": max(0.0, frame / fps - shift), "frame": frame, "shot": target["id"], "role": role,
                "label": ("%s %s F%d" % (role, target["id"], frame)).strip()}

    def middle(target: Dict[str, Any]) -> int:
        return frame_numbers(target, fps, 1)[0]

    count = int(spec.get("frames", 3))
    tiles = [tile(shot, frame) for frame in frame_numbers(shot, fps, count)]
    if spec.get("neighbours"):
        if index > 0:
            tiles.insert(0, tile(shots[index - 1], middle(shots[index - 1]), "PREV"))
        earlier = [s for s in shots[:max(0, index - 1)] if s["set"] == shot["set"]]
        if earlier:
            tiles.append(tile(earlier[-1], middle(earlier[-1]), "EARLIER"))
    height = int(spec.get("tile_height", 640))
    name = "%s_%s%d_%d%s.jpg" % (shot["id"], kind[:3], count, height, "n" if spec.get("neighbours") else "")
    return {"name": name, "tiles": tiles, "height": height, "columns": int(spec.get("columns") or len(tiles))}


def describe_tiles(tiles: Sequence[Dict[str, Any]]) -> str:
    notes = {"PREV": " (the shot before)", "EARLIER": " (the last earlier shot in the same set)"}
    return ", ".join("%s%s" % (t["label"], notes.get(t["role"], "")) for t in tiles)


def build_prompt(key: str, spec: Dict[str, Any], settings: Dict[str, Any], story: Dict[str, Any], index: int,
                 shots: Sequence[Dict[str, Any]], plan: Dict[str, Any], measured: Optional[Dict[str, Any]]) -> str:
    shot = shots[index]
    lines = [settings["format"], "Story: %s%s." % (story.get("title") or "untitled", ", " + str(story["genre"]) if story.get("genre") else "")]
    if spec.get("script"):
        lines += ["Whole script, for context only:", script_text(story, shots), ""]
    lines.append("Shot %s (%d of %d), %.1f seconds, set \"%s\"." % (shot["id"], index + 1, len(shots), shot["end"] - shot["start"], shot["set"]))
    if (settings.get("sets") or {}).get(shot["set"]):
        lines.append("About the set \"%s\": %s" % (shot["set"], settings["sets"][shot["set"]]))
    lines.append("Words spoken during this shot: %s" % (shot["line"] or "none (a silent shot)"))
    if index > 0:
        lines.append("Shot before (%s, set \"%s\"): %s" % (shots[index - 1]["id"], shots[index - 1]["set"], shots[index - 1]["line"] or "silent"))
    if index + 1 < len(shots):
        lines.append("Shot after (%s, set \"%s\"): %s" % (shots[index + 1]["id"], shots[index + 1]["set"], shots[index + 1]["line"] or "silent"))
    earlier = [tile["shot"] for tile in plan["tiles"] if tile["role"] == "EARLIER"]
    if earlier:
        lines.append("Last earlier shot in the same set (%s): %s" % (earlier[0], {s["id"]: s["line"] for s in shots}[earlier[0]] or "silent"))
    lines.append("The image is a sheet of %d frames from the rendered video, each labelled in its top-left corner with the shot id and "
                 "frame number, in reading order: %s. The label boxes are added for this review and are not part of the picture."
                 % (len(plan["tiles"]), describe_tiles(plan["tiles"])))
    if plan.get("detail"):
        lines.append("A second image shows %d frames of the same shot larger (%s), for comparing poses at the start, middle and end."
                     % (len(plan["detail"]["tiles"]), describe_tiles(plan["detail"]["tiles"])))
    if spec.get("measures") and measured:
        lines.append("Measured from the video for this shot: mean brightness %.1f of 255, %.1f%% of pixels lit; change between "
                     "neighbouring video frames mean %.2f, median %.2f, peak %.2f at frame %d (0 to 255 scale), %.0f%% of frames unchanged."
                     % (measured["luma"], measured["lit_fraction"] * 100, measured["change_mean"], measured["change_median"],
                        measured["change_peak"], measured["change_peak_frame"], measured["still_fraction"] * 100))
    lines += ["", "Judge: %s." % spec["title"], spec["question"]]
    if spec.get("notes"):
        lines.append(spec["notes"])
    lines += accepted_lines(settings)
    lines.append("Fail conditions; report the key of every one you can see:")
    lines += ["- %s: %s" % (d["key"], d["text"]) for d in spec["fail_if"]]
    if spec.get("not_applicable"):
        lines.append("If %s, reply with \"applicable\": false and no defects." % spec["not_applicable"])
    flags = spec.get("flags") or {}
    lines += ['Set "%s" to true when %s' % (name, flag["text"]) for name, flag in flags.items()]
    lines += [settings["scale"],
              'Reply with JSON only: {"visible": "what the frames show, one sentence", "applicable": true, %s'
              '"defects": [{"key": "a fail key", "where": "who or what, in which frame"}], "score": 0, "reason": "one line"}'
              % "".join('"%s": false, ' % name for name in flags)]
    return "\n".join(lines)


def verdict(spec: Dict[str, Any], reply: Dict[str, Any]) -> Dict[str, Any]:
    """Pass or fail from a model reply.

    A score under the threshold fails. A listed defect fails unless the score is at or above defect_pass_score
    (the model itself calls it a small imperfection). A flag from the rubric answered true always fails.
    """
    known = {d["key"] for d in spec["fail_if"]}
    defects = []
    for item in reply.get("defects") or []:
        found = str(item.get("key", "") if isinstance(item, dict) else item).strip()
        if found in known:
            defects.append({"key": found, "where": str(item.get("where", "")) if isinstance(item, dict) else ""})
    raised = [name for name in (spec.get("flags") or {}) if reply.get(name) is True]
    for name in raised:
        if all(d["key"] != spec["flags"][name]["defect"] for d in defects):
            defects.append({"key": spec["flags"][name]["defect"], "where": name})
    threshold = float(spec.get("threshold", 6))
    try:
        score: Optional[float] = min(10.0, max(0.0, float(reply.get("score"))))
    except (TypeError, ValueError):
        score = None
    entry = {"status": "pass", "score": score, "threshold": threshold, "applicable": reply.get("applicable") is not False,
             "defects": defects, "reason": str(reply.get("reason") or ""), "visible": str(reply.get("visible") or "")}
    if not entry["applicable"]:
        entry.update(score=None, defects=[], reason=entry["reason"] or "nothing to judge in this shot")
    elif raised or score is None or score < threshold or (defects and score < float(spec.get("defect_pass_score", 8))):
        entry["status"] = "fail"
    return entry


def ask(settings: Dict[str, Any], budget: SharedBudget, prompt: str, images: Sequence[Path], max_tokens: int, manual_dir: Path,
        request_id: str, wanted: Sequence[str]) -> Dict[str, Any]:
    """One model question under the shared budget, asked a second time when the reply has none of the wanted keys."""
    for attempt in range(2):
        budget.take()
        try:
            result = vision.ask(settings["provider"], settings["model"], settings["system"], prompt, list(images), max_output_tokens=max_tokens,
                                prices=settings["prices"], manual_dir=manual_dir, manual_id="%s_%d" % (request_id, attempt))
        except BaseException:
            budget.settle()
            raise
        budget.settle(result)
        if isinstance(result["json"], dict) and any(name in result["json"] for name in wanted):
            return result["json"]
    raise QualityError("the model returned no usable reply")


def judge_shot(key: str, settings: Dict[str, Any], story: Dict[str, Any], index: int, shots: Sequence[Dict[str, Any]], plan: Dict[str, Any],
               sheet: Path, sheet_rel: str, measured: Optional[Dict[str, Any]], budget: SharedBudget, manual_dir: Path,
               vote: int = 0) -> Dict[str, Any]:
    """One judge's verdict on one shot; a judge that cannot answer returns status error, never a pass."""
    spec = settings["judges"][key]
    base = {"sheet": sheet_rel, "frames": [t["frame"] for t in plan["tiles"] if not t["role"]], "source": plan["source"]}
    if key == "continuity" and index == 0:
        return dict(base, status="pass", score=None, threshold=float(spec.get("threshold", 6)), applicable=False, defects=[],
                    reason="first shot, nothing to compare with", visible="")
    try:
        images = [sheet] + ([sheet.parent / plan["detail"]["name"]] if plan.get("detail") else [])
        if not all(image.is_file() for image in images):
            raise QualityError(plan.get("error") or "no frame sheet")
        reply = ask(settings, budget, build_prompt(key, spec, settings, story, index, shots, plan, measured), images,
                    int(spec.get("max_output_tokens", settings.get("max_output_tokens", 1500))), manual_dir,
                    "%s_%s" % (shots[index]["id"], key) + ("_v%d" % vote if vote else ""), ("score", "applicable"))
    except (vision.BudgetExceeded, vision.Pending, vision.VisionError, QualityError, EditError) as exc:
        return dict(base, status="error", reason="not judged: %s" % str(exc)[:200])
    entry = dict(base, **verdict(spec, reply))
    floor = spec.get("min_lit_fraction")
    if floor is not None and measured and measured["lit_fraction"] < float(floor):
        entry["status"] = "fail"
        entry["defects"] = entry["defects"] + [{"key": "measured_dark", "where": "%.1f%% of pixels lit" % (measured["lit_fraction"] * 100)}]
        entry["reason"] = ("measured nearly black (%.1f%% of pixels lit); " % (measured["lit_fraction"] * 100)) + entry["reason"]
    return entry


def _rel(job: Path, path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(job.resolve()))
    except ValueError:
        return str(path)


def judge_video(settings: Dict[str, Any], story: Dict[str, Any], shots: Sequence[Dict[str, Any]], meta: Dict[str, Any], video: Path,
                sheets_dir: Path, job: Path, budget: SharedBudget) -> Dict[str, Any]:
    """Judge pacing, repetition, the ending and consistency on sheets of one middle frame per shot."""
    spec = settings[VIDEO_JUDGE]
    fps = float(meta["fps"])
    paths: List[Path] = []
    per = int(spec.get("tiles_per_sheet", 18))
    keys = [c["key"] for c in spec["criteria"]]
    try:
        for number, start in enumerate(range(0, len(shots), per), 1):
            tiles = [{"video": video, "time": frame_numbers(s, fps, 1)[0] / fps, "label": s["id"]} for s in shots[start:start + per]]
            path = sheets_dir / ("video_%02d.jpg" % number)
            build_sheet(tiles, path, int(spec.get("tile_height", 400)), int(spec.get("columns", 6)))
            paths.append(path)
        lines = [settings["format"], "Story: %s." % (story.get("title") or "untitled"), "",
                 "Shots in order (id, seconds, set, words spoken):"]
        lines += ["%s  %.1f-%.1f  %s  %s" % (s["id"], s["start"], s["end"], s["set"], s["line"] or "(silent)") for s in shots]
        lines += ["", spec["question"]] + accepted_lines(settings) + ["Score each from 0 to 10:"]
        lines += ["- %s: %s" % (c["key"], c["text"]) for c in spec["criteria"]]
        lines.append("Then name the %d weakest shots, worst first, by the shot id on their label, each with the one thing wrong with it."
                     % int(spec.get("weakest", 8)))
        lines.append('Reply with JSON only: {"scores": {%s}, "weakest": [{"shot": "id", "reason": "one line"}], "reason": "one line on the whole video"}'
                     % ", ".join('"%s": 0' % k for k in keys))
        votes = max(1, int(spec.get("votes", 3)))
        replies = [ask(settings, budget, "\n".join(lines), paths, int(spec.get("max_output_tokens", 3000)), job / "quality" / "manual",
                       "video_%d" % vote, ("scores",)) for vote in range(votes)]
    except (vision.BudgetExceeded, vision.Pending, vision.VisionError, QualityError, EditError) as exc:
        return {"status": "error", "reason": "not judged: %s" % str(exc)[:200], "sheets": [_rel(job, p) for p in paths]}
    scores: Dict[str, Optional[float]] = {}
    spread: Dict[str, List[float]] = {}
    for name in keys:
        values = []
        for reply in replies:
            try:
                values.append(min(10.0, max(0.0, float((reply.get("scores") or {}).get(name)))))
            except (TypeError, ValueError, AttributeError):
                continue
        spread[name] = sorted(values)
        scores[name] = spread[name][len(values) // 2] if values else None
    known = {s["id"] for s in shots}
    named: Dict[str, List[str]] = {}
    for reply in replies:
        for item in reply.get("weakest") or []:
            if isinstance(item, dict):
                shot_id, why = str(item.get("shot", item.get("id", item.get("shot_id", "")))), str(item.get("reason", ""))
            else:
                shot_id, _, why = str(item).partition(":")
            if shot_id.strip() in known:
                named.setdefault(shot_id.strip(), []).append(why.strip())
    weakest = [{"shot": shot_id, "votes": len(reasons), "reason": reasons[0]}
               for shot_id, reasons in sorted(named.items(), key=lambda pair: -len(pair[1]))][:int(spec.get("weakest", 8))]
    threshold = float(spec.get("threshold", 6))
    failed = [name for name in keys if scores[name] is None or scores[name] < threshold]
    return {"status": "fail" if failed else "pass", "scores": scores, "votes": spread, "threshold": threshold, "failed": failed,
            "weakest": weakest, "reason": " / ".join(str(reply.get("reason") or "") for reply in replies),
            "sheets": [_rel(job, p) for p in paths]}


def geometry_results(posture: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Geometric results per shot, and the shots whose results are marked stale, from either shape the posture stage writes.

    The current shape keys shots by id and lists issues per sampled frame (kind, actor, detail, in_frame). The earlier
    one lists shots under posture.shots with a geometry list (code, who, detail, seen, depth). Both mix a model's
    opinion into status and reasons; those reasons (vision_*) are another judge's view and are not geometry.
    A stray piece of the set (floating or built twice) is named by its detail and counts only where it is in frame:
    out of frame it is left out and counted under hidden, so it cannot fail a picture it is not in.
    """
    posture = posture or {}
    node = posture["posture"] if isinstance(posture.get("posture"), dict) else posture
    shots = node.get("shots") or {}
    pairs = list(shots.items()) if isinstance(shots, dict) else [(s.get("id"), s) for s in shots if isinstance(s, dict)]
    results: Dict[str, Dict[str, Any]] = {}
    stale: List[str] = []
    for shot_id, result in pairs:
        if not isinstance(result, dict):
            continue
        if result.get("current") is False:
            stale.append(str(shot_id))
            continue
        found: List[Tuple[Any, Any, Any, str, bool]] = []
        for frame in result.get("frames") or []:
            for issue in (frame.get("issues") or []) if isinstance(frame, dict) else []:
                found.append((issue.get("kind") or issue.get("code"), issue.get("actor"), issue.get("detail"),
                              "frame %s" % frame.get("frame", "?"), issue.get("in_frame") is not False))
        for issue in result.get("geometry") or []:
            detail = "%s%s" % (issue.get("detail") or "", " by %.2f studs" % issue["depth"] if issue.get("depth") else "")
            found.append((issue.get("code"), issue.get("who"), detail,
                          "at %s of the shot" % ", ".join("%g" % u for u in issue.get("at") or []), issue.get("seen") is not False))
        issues: List[Dict[str, Any]] = []
        hidden = 0
        for kind, who, detail, where, seen in found:
            text = "%s %s%s" % (who or "?", kind or "?", ": " + str(detail) if detail else "")
            if str(kind) in SET_PIECES:
                if not seen:
                    hidden += 1
                    continue
                text = "%s: %s" % (SET_PIECES[str(kind)], detail or "not named")
            if all(text != item["text"] for item in issues):
                issues.append({"judge": GEOMETRY_JUDGES.get(str(kind), "clipping"), "text": text, "where": where, "in_frame": seen})
        status = result.get("status")
        if "geometry" in result or result.get("reasons"):
            status = "fail" if any(not str(r).startswith("vision_") for r in result.get("reasons") or []) else "pass"
        results[str(shot_id)] = {"status": status, "issues": issues, "hidden": hidden}
    return results, stale


def merge_geometry(entries: Dict[str, Dict[str, Any]], posture: Optional[Dict[str, Any]], source: str) -> List[str]:
    """Fold posture.json into the posture, clipping and expression judges of the given shots; returns those skipped as stale.

    A shot the geometry failed fails the judge its issues belong to, whatever the picture says; issues marked
    out of frame count only when no issue is in frame. The picture's own verdict is kept as picture_status, and
    each result names the file it came from, since one report can hold shots from different renders.
    """
    results, stale = geometry_results(posture)
    stale = [shot_id for shot_id in stale if shot_id in entries]
    for shot_id, result in results.items():
        entry = entries.get(shot_id)
        if entry is None:
            continue
        found: Dict[str, List[str]] = {}
        if result["status"] == "fail" or (result["status"] is None and result["issues"]):
            for issue in [i for i in result["issues"] if i["in_frame"]] or result["issues"]:
                found.setdefault(issue["judge"], []).append("%s (%s)" % (issue["text"], issue["where"]))
            if not found and not result["hidden"]:
                found["clipping"] = ["geometric check failed without listed issues"]
        for judge in ("posture", "clipping", "expression"):
            record = entry["judges"].setdefault(judge, {"status": "unjudged", "reason": "", "defects": []})
            picture = record.get("picture_status", record.get("status"))
            issues = found.get(judge, [])
            reason = str(record.get("reason") or "").split("; geometry: ")[0]
            reason = "" if reason.startswith("geometry: ") else reason
            if issues:
                reason = "; ".join(part for part in (reason, "geometry: %s" % "; ".join(issues[:3])) if part)
            record.update(picture_status=picture, geometry={"status": "fail" if issues else "pass", "issues": issues, "source": source},
                          status="fail" if issues else picture, reason=reason)
    return stale


def borderline(entry: Dict[str, Any], key: str, spec: Dict[str, Any], margin: float) -> bool:
    """Whether one judge's fail on a shot is to be put to more votes before it stands.

    It is when the score is within margin of the threshold or no other judge fails the shot. A fail that already
    has votes, one backed by geometry and one backed by measurement are never asked again.
    """
    record = entry["judges"].get(key) or {}
    if record.get("status") != "fail" or record.get("votes") or (record.get("geometry") or {}).get("status") == "fail":
        return False
    if any(defect.get("key") == "measured_dark" for defect in record.get("defects") or []):
        return False
    alone = [k for k in SHOT_JUDGES if (entry["judges"].get(k) or {}).get("status") == "fail"] == [key]
    score = record.get("score")
    return alone or (score is not None and abs(score - float(spec.get("threshold", 6))) <= margin)


def settle_votes(record: Dict[str, Any], more: Sequence[Dict[str, Any]]) -> None:
    """Replace a borderline fail by the majority of its votes: the verdict already given and the ones asked again.

    The record takes the answer of the middle-scoring vote on the winning side and keeps every vote under votes.
    A vote that could not answer leaves the fail as it stands, to be asked again in a later run.
    """
    ballots = [dict(record)] + [dict(item) for item in more]
    if any(ballot.get("status") not in ("pass", "fail") for ballot in ballots):
        return
    fails = [ballot for ballot in ballots if ballot["status"] == "fail"]
    side = fails if len(fails) * 2 > len(ballots) else [ballot for ballot in ballots if ballot["status"] == "pass"]
    side = sorted(side, key=lambda ballot: -1.0 if ballot.get("score") is None else ballot["score"])
    chosen = side[(len(side) - 1) // 2]
    record.update({name: chosen.get(name) for name in ("status", "score", "applicable", "defects", "reason", "visible")})
    record["votes"] = [{"status": ballot["status"], "score": ballot.get("score"), "reason": ballot.get("reason", ""),
                        "defects": [defect["key"] for defect in ballot.get("defects") or []]} for ballot in ballots]
    if "picture_status" in record:
        record["picture_status"] = record["status"]


def standing(record: Optional[Dict[str, Any]]) -> bool:
    """A judge's fail that no written overrule has set aside."""
    return bool(record) and record.get("status") == "fail" and not record.get("overruled")


def apply_overrules(job: Path, doc: Dict[str, Any], reading: Optional[Dict[str, Any]]) -> None:
    """Set aside the judge fails a reader overruled in <job>/quality/overrules.json, and say why any other entry does not apply.

    An entry is {"shot", "judge", "render", "answers", "frames", "reason"}: the frames the reader looked at and what they
    show, the judge's reason it answers word for word, and the mark of the render it was read from. A shot rendered
    again, or a judge that has spoken again, is read again. A fail from geometry or measurement and a fail the
    reader's own verdicts hold are never overruled.
    """
    for shot in doc["shots"]:
        for record in shot["judges"].values():
            record.pop("overruled", None)
    doc.pop("overrules", None)
    listed = read_json(job / "quality" / "overrules.json", required=False)
    if not listed:
        return
    fps = float((read_json(job / "shots.json", required=False) or {}).get("fps") or 0)
    shots = {shot["id"]: shot for shot in doc["shots"]}
    held = (reading or {}).get("verdicts") or {}
    reader = listed.get("reader") or "a reader"
    applied: List[Dict[str, Any]] = []
    void: List[Dict[str, Any]] = []
    for item in listed.get("overrules") or []:
        shot, key, frames = shots.get(str(item.get("shot"))), str(item.get("judge")), item.get("frames")
        numbered = isinstance(frames, list) and all(isinstance(frame, int) for frame in frames)
        written = all(str(item.get(name) or "").strip() for name in ("render", "answers", "reason"))
        if shot is None or key not in SHOT_JUDGES or not written or not numbered or not frames:
            raise QualityError("an overrule needs a shot, a judge, a render mark, the judge's reason, frame numbers and a reason: %r" % (item,))
        record = shot["judges"].get(key) or {}
        why = ""
        if record.get("status") != "fail":
            why = "the judge does not fail it now"
        elif (record.get("geometry") or {}).get("status") == "fail" or any(d.get("key") == "measured_dark" for d in record.get("defects") or []):
            why = "a fail from geometry or measurement is not overruled"
        elif (record.get("render") or {}).get(shot["id"]) != item["render"]:
            why = "the shot was rendered again after it was read"
        elif record.get("reason") != item["answers"]:
            why = "the judge has given a new verdict since it was read"
        elif (held.get(shot["id"]) or {}).get(key) == "fail":
            why = "the reader holds this fail"
        if why:
            void.append({"shot": shot["id"], "judge": key, "why": why})
            continue
        first, last = frame_range(shot, fps) if fps else (min(frames), max(frames))
        if min(frames) < first or max(frames) > last:
            raise QualityError("overrule of %s/%s names frames outside the shot (%d to %d): %s" % (shot["id"], key, first, last, frames))
        record["overruled"] = {"by": reader, "frames": frames, "reason": item["reason"]}
        applied.append({"shot": shot["id"], "judge": key, "score": record.get("score"), "defects": [d["key"] for d in record.get("defects") or []],
                        "judge_reason": record.get("reason", ""), "frames": frames, "reason": item["reason"]})
    doc["overrules"] = {"reader": reader, "applied": applied, "void": void}


def shot_status(entry: Dict[str, Any], judges: Sequence[str]) -> None:
    failed = [k for k in SHOT_JUDGES if standing(entry["judges"].get(k))]
    entry["overruled"] = [k for k in SHOT_JUDGES if (entry["judges"].get(k) or {}).get("overruled")]
    missing = [k for k in judges if k in SHOT_JUDGES and (entry["judges"].get(k) or {}).get("status") not in ("pass", "fail")]
    entry["failed"] = failed
    entry["unjudged"] = missing
    entry["status"] = "fail" if failed else ("incomplete" if missing else "pass")


def summarise(doc: Dict[str, Any]) -> Dict[str, Any]:
    shots = doc["shots"]
    per_judge = {}
    for key in SHOT_JUDGES:
        records = [(s["id"], s["judges"].get(key) or {}) for s in shots]
        scores = [r["score"] for _, r in records if isinstance(r.get("score"), (int, float))]
        per_judge[key] = {"fail": sum(1 for _, r in records if standing(r)),
                          "overruled": [shot_id for shot_id, r in records if r.get("overruled")],
                          "pass": sum(1 for _, r in records if r.get("status") == "pass"),
                          "unjudged": sum(1 for _, r in records if r.get("status") not in ("pass", "fail")),
                          "mean_score": round(sum(scores) / len(scores), 2) if scores else None,
                          "failing": [shot_id for shot_id, r in records if standing(r)],
                          "geometry_only": [shot_id for shot_id, r in records
                                            if r.get("status") == "fail" and r.get("picture_status", "fail") != "fail"]}
    stale = {shot["id"]: [key for key in SHOT_JUDGES if (shot["judges"].get(key) or {}).get("status") == "stale"] for shot in shots}
    sources: Dict[str, int] = {}
    for shot in shots:
        source = ((shot["judges"].get("posture") or {}).get("geometry") or {}).get("source")
        if source:
            sources[source] = sources.get(source, 0) + 1
    by_set: Dict[str, List[str]] = {}
    for shot in shots:
        if shot["status"] == "fail":
            by_set.setdefault(shot["set"], []).append(shot["id"])
    frames = set()
    for shot in shots:
        for record in shot["judges"].values():
            frames.update((str(record.get("source")), frame) for frame in record.get("frames") or [])
    def weakness(shot: Dict[str, Any]) -> Tuple[int, float]:
        scores = [r["score"] for r in shot["judges"].values() if isinstance(r.get("score"), (int, float))]
        return -len(shot.get("failed") or []), sum(scores) / len(scores) if scores else 10.0

    weakest = [{"shot": s["id"], "failed": s["failed"]} for s in sorted(shots, key=weakness)[:8] if s.get("failed")]
    voted = [("%s/%s" % (s["id"], key), s["judges"][key]["status"]) for s in shots for key in SHOT_JUDGES if (s["judges"].get(key) or {}).get("votes")]
    return {"shots": len(shots), "pass": sum(1 for s in shots if s["status"] == "pass"), "weakest": weakest,
            "fail": sum(1 for s in shots if s["status"] == "fail"),
            "incomplete": sum(1 for s in shots if s["status"] == "incomplete"),
            "pass_by_overrule": sum(1 for s in shots if s["status"] == "pass" and s.get("overruled")),
            "judges": per_judge, "failing_by_set": by_set, "frames_sampled": len(frames), "geometry_sources": sources,
            "stale": {shot_id: keys for shot_id, keys in stale.items() if keys},
            "voted": {"kept": [name for name, status in voted if status == "fail"], "cleared": [name for name, status in voted if status == "pass"]},
            "provider": doc["config"]["provider"], "model": doc["config"]["model"], "spend": doc.get("usage") or {}}


def verdict_pairs(doc: Dict[str, Any]) -> Dict[Tuple[str, str], str]:
    """What each judge said of the picture; a reader of the sheets is compared with that, not with merged geometry."""
    pairs = {(s["id"], k): r.get("picture_status", r.get("status")) for s in doc.get("shots", []) for k, r in s["judges"].items()}
    return {pair: said for pair, said in pairs.items() if said in ("pass", "fail")}


def agreement(doc: Dict[str, Any], human: Dict[str, Any]) -> Dict[str, Any]:
    """How often the judges match a person's verdicts ({"verdicts": {shot: {judge: "pass" or "fail"}}})."""
    machine = verdict_pairs(doc)
    pairs = agree = 0
    false_pass: List[str] = []
    false_fail: List[str] = []
    per_judge: Dict[str, List[int]] = {}
    for shot_id, judges in (human.get("verdicts") or {}).items():
        for key, said in judges.items():
            got = machine.get((shot_id, key))
            if got is None or said not in ("pass", "fail"):
                continue
            pairs += 1
            tally = per_judge.setdefault(key, [0, 0])
            tally[1] += 1
            if got == said:
                agree += 1
                tally[0] += 1
            elif got == "pass":
                false_pass.append("%s/%s" % (shot_id, key))
            else:
                false_fail.append("%s/%s" % (shot_id, key))
    return {"reader": human.get("reader", ""), "shots": len(human.get("verdicts") or {}), "pairs": pairs, "agree": agree,
            "rate": round(agree / pairs, 3) if pairs else None, "false_pass": false_pass, "false_fail": false_fail,
            "per_judge": {k: {"agree": v[0], "pairs": v[1]} for k, v in per_judge.items()}}


def compare(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """Per judge, which shots failed before and after, and which were fixed, newly failing or still failing.

    A shot whose verdict now is missing or stale is not counted as fixed.
    """
    order = [s["id"] for s in after.get("shots", [])] + [s["id"] for s in before.get("shots", [])]
    table = {}
    for key in SHOT_JUDGES + ("any",):
        sides = []
        for doc in (before, after):
            if key == "any":
                sides.append({s["id"] for s in doc.get("shots", []) if s.get("status") == "fail"})
            else:
                sides.append({s["id"] for s in doc.get("shots", []) if standing(s["judges"].get(key))})
        open_now = {s["id"] for s in after.get("shots", []) if (s.get("status") == "incomplete" if key == "any" else
                                                                (s["judges"].get(key) or {}).get("status") not in ("pass", "fail"))}
        table[key] = {"before": len(sides[0]), "after": len(sides[1]), "fixed": sorted(sides[0] - sides[1] - open_now, key=order.index),
                      "new": sorted(sides[1] - sides[0], key=order.index), "still": sorted(sides[0] & sides[1], key=order.index)}
    return table


def markdown(doc: Dict[str, Any]) -> str:
    summary = doc["summary"]
    spend = summary["spend"]
    out = ["# Quality report: %s" % doc.get("job", ""), "",
           "Video @@%s@@, captioned @@%s@@. %d shots: %d pass, %d fail, %d incomplete. Model %s (%s). %d frames sampled. "
           "Spend $%.4f over %d calls (%d input and %d output tokens, measured); ceiling $%.2f."
           % (doc.get("video", ""), doc.get("final", ""), summary["shots"], summary["pass"], summary["fail"], summary["incomplete"],
              summary["model"], summary["provider"], summary["frames_sampled"], spend.get("cost_usd", 0.0), spend.get("calls", 0),
              spend.get("input_tokens", 0), spend.get("output_tokens", 0), spend.get("ceiling_usd", 0.0)), "",
           "A shot fails when any judge fails it, unless a reader has read that fail from the frames and overruled it in writing "
           "(see Overruled fails). Sheets the judges saw are under @@quality/sheets/@@.", "",
           "## Fails per judge", "", "| judge | fail | overruled | pass | unjudged | mean score | failing shots |", "|---|---|---|---|---|---|---|"]
    for key in SHOT_JUDGES:
        row = summary["judges"][key]
        out.append("| %s | %d | %d | %d | %d | %s | %s |" % (key, row["fail"], len(row["overruled"]), row["pass"], row["unjudged"],
                                                      "-" if row["mean_score"] is None else "%.1f" % row["mean_score"], " ".join(row["failing"])))
    alone = ["%s: %s" % (key, " ".join(summary["judges"][key]["geometry_only"])) for key in SHOT_JUDGES
             if summary["judges"][key].get("geometry_only")]
    if summary.get("geometry_sources"):
        out += ["", "Geometry merged from %s. Failing on geometry alone, where the picture judge passed or did not run: %s.%s"
                % (", ".join("@@%s@@ (%d shots)" % item for item in summary["geometry_sources"].items()), "; ".join(alone) or "none",
                   " Not merged, because the file marks them stale: %s." % " ".join(doc["geometry_stale"]) if doc.get("geometry_stale") else "")]
    if summary.get("stale"):
        out += ["", "Set aside, because the shot was rendered again after these verdicts were given (@@--changed@@ judges them again): %s."
                % "; ".join("%s (%s)" % (shot_id, ", ".join(keys)) for shot_id, keys in summary["stale"].items())]
    voted = summary.get("voted") or {}
    if voted.get("kept") or voted.get("cleared"):
        out += ["", "Fails close to the threshold, or alone on their shot, were asked again and settled by the majority of the votes: "
                "%d stayed fails (%s); %d became passes (%s)." % (len(voted["kept"]), " ".join(voted["kept"]) or "none",
                                                                len(voted["cleared"]), " ".join(voted["cleared"]) or "none")]
    video = doc.get("video_judge")
    if video:
        out += ["", "## Whole video", ""]
        if video.get("scores"):
            out.append("%s. Scores: %s (threshold %s). %s" % (video["status"].upper(), ", ".join("%s %s" % (k, v) for k, v in video["scores"].items()),
                                                          video.get("threshold"), video.get("reason", "")))
            out += ["", "Weakest shots named by the whole-video judge:"] + ["- %s: %s" % (w["shot"], w["reason"]) for w in video.get("weakest", [])]
        else:
            out.append(video.get("reason", ""))
    out += ["", "Weakest shots by number of failed judges, then mean score: %s."
            % ", ".join("%s (%s)" % (w["shot"], ", ".join(w["failed"])) for w in summary.get("weakest", [])),
            "", "## Failing shots by set", ""]
    shots = {s["id"]: s for s in doc["shots"]}
    for name, ids in summary["failing_by_set"].items():
        out += ["### %s (%d)" % (name, len(ids)), ""]
        for shot_id in ids:
            shot = shots[shot_id]
            out.append("- **%s** (%.1f-%.1f s) \"%s\"" % (shot_id, shot["start"], shot["end"], shot["line"] or "silent"))
            for key in shot["failed"]:
                record = shot["judges"][key]
                score = "" if record.get("score") is None else " %.0f/10" % record["score"]
                keys = ", ".join(d["key"] for d in record.get("defects") or [])
                if record.get("votes"):
                    score += ", %d of %d votes" % (sum(1 for v in record["votes"] if v["status"] == "fail"), len(record["votes"]))
                out.append("  - %s%s%s: %s%s" % (key, score, " [%s]" % keys if keys else "", record.get("reason", ""),
                                                " (@@%s@@)" % record["sheet"] if record.get("sheet") else ""))
        out.append("")
    overrules = doc.get("overrules") or {}
    if overrules.get("applied") or overrules.get("void"):
        out += ["## Overruled fails", "",
                "Read from the frames by %s. Each row is a judge's fail that no longer fails its shot, with the judge's reason and the "
                "reader's beside it. Shots that pass because of them: %d. Disagree with a row and that fail stands again."
                % (overrules.get("reader") or "a reader", summary["pass_by_overrule"]), ""]
        if overrules.get("applied"):
            out += ["| shot | judge | the judge's reason | the reader's reason | frames read |", "|---|---|---|---|---|"]
            for row in overrules["applied"]:
                cells = [row["shot"], "%s%s%s" % (row["judge"], "" if row.get("score") is None else " %.0f/10" % row["score"],
                                                  " [%s]" % ", ".join(row["defects"]) if row.get("defects") else ""),
                         row["judge_reason"], row["reason"], " ".join(str(frame) for frame in row["frames"])]
                out.append("| %s |" % " | ".join(" ".join(str(cell).replace("|", "/").split()) for cell in cells))
        if overrules.get("void"):
            out += ["", "Not applied: %s." % "; ".join("%s/%s (%s)" % (row["shot"], row["judge"], row["why"]) for row in overrules["void"])]
        out.append("")
    incomplete = [s for s in doc["shots"] if s["status"] == "incomplete"]
    if incomplete:
        out += ["## Not fully judged", ""] + ["- %s: %s" % (s["id"], ", ".join(s["unjudged"])) for s in incomplete] + [""]
    calibration = doc.get("calibration")
    if calibration:
        out += ["## Agreement with a person's reading", ""]
        for label in ("before", "after"):
            part = calibration.get(label)
            if part and part.get("pairs"):
                out.append("- %s: %d of %d verdicts agree (%.0f%%) over %d shots read by %s; judge passed what the reader failed: %s; "
                           "judge failed what the reader passed: %s" % (label, part["agree"], part["pairs"], part["rate"] * 100, part["shots"],
                                                                      part["reader"] or "a person", " ".join(part["false_pass"]) or "none",
                                                                      " ".join(part["false_fail"]) or "none"))
        for note in calibration.get("notes") or []:
            out.append("- %s" % note)
        out.append("")
    table = doc.get("comparison")
    if table:
        out += ["## Before and after (%s against this report)" % doc.get("compared_with", "earlier report"), "",
                "| judge | failed before | fail now | fixed | new fails | still failing |", "|---|---|---|---|---|---|"]
        for key in SHOT_JUDGES + ("any",):
            row = table[key]
            out.append("| %s | %d | %d | %s | %s | %s |" % (key, row["before"], row["after"], " ".join(row["fixed"]) or "-",
                                                         " ".join(row["new"]) or "-", " ".join(row["still"]) or "-"))
        out.append("")
    return "\n".join(out).replace("@@", chr(96)) + "\n"


def finish(job: Path, doc: Dict[str, Any], out: Path, human: Optional[Path], baseline: Optional[Path], against: Optional[Path]) -> None:
    """Recompute statuses and the summary, attach calibration and comparison, and write the JSON and Markdown reports."""
    reading = read_json(human) if human is not None else None
    apply_overrules(job, doc, reading)
    for entry in doc["shots"]:
        shot_status(entry, doc["config"]["judges"])
    doc["summary"] = summarise(doc)
    if reading is not None:
        calibration = {"after": agreement(doc, reading), "notes": reading.get("notes") or []}
        if baseline is not None:
            calibration["before"] = agreement(read_json(baseline), reading)
        doc["calibration"] = calibration
    if against is not None:
        doc["comparison"] = compare(read_json(against), doc)
        doc["compared_with"] = against.name
    write_json(out, doc)
    name = "QUALITY.md" if out.name == "quality.json" else out.stem + ".md"
    (out.parent / name).write_text(markdown(doc), encoding="utf-8")


def run_quality(job: Path, video: Optional[Path] = None, final: Optional[Path] = None, shots: Optional[Sequence[str]] = None,
                judges: Optional[Sequence[str]] = None, overrides: Optional[Dict[str, Any]] = None, out: Optional[Path] = None,
                human: Optional[Path] = None, baseline: Optional[Path] = None, against: Optional[Path] = None,
                report_only: bool = False, posture: Optional[Path] = None, changed: bool = False,
                confirm: bool = False) -> Dict[str, Any]:
    """Judge the rendered video of a job (or the listed shots and judges) and write the reports.

    With changed, only the verdicts that are missing, failed to answer or were set aside as stale are judged again.
    Borderline fails among the verdicts judged in the run are put to more votes; confirm does what changed does
    and also puts every borderline fail already in the report to the vote.
    """
    changed = changed or confirm
    job = Path(job)
    out = Path(out) if out else job / "quality.json"
    existing = read_json(out, required=False) or {}
    geometry_path = Path(posture) if posture else job / "posture.json"
    geometry = read_json(geometry_path, required=posture is not None)
    described = (geometry or {}).get("describes")
    geometry_source = "%s@%d%s" % (geometry_path.name, int(geometry_path.stat().st_mtime),
                                   " (%s)" % described["label"] if isinstance(described, dict) and described.get("label") else "") if geometry else ""
    story = read_json(job / "story.json", required=False) or {}
    settings = load_settings(story, overrides)
    kinds = {key: str(spec.get("video", "clean")) for key, spec in settings["judges"].items()}
    videos = {"clean": Path(video) if video else job / "video.mp4", "captioned": Path(final) if final else job / "final.mp4"}
    if report_only:
        if not existing.get("shots"):
            raise QualityError("%s has no results to report" % out)
        if posture:
            existing["geometry_stale"] = merge_geometry({s["id"]: s for s in existing["shots"] if shots is None or s["id"] in shots},
                                                        geometry, geometry_source)
        if (job / "shots.json").is_file():
            set_aside(existing, current_renders(videos, load_shots(job)[1], load_shots(job)[0])[0], kinds)
        finish(job, existing, out, human, baseline, against)
        return existing
    if settings["provider"] != "manual" and not settings["model"]:
        raise QualityError("no model set; pass --model (python -m pipeline.judge models --provider %s lists them)" % settings["provider"])
    meta, all_shots = load_shots(job)
    known = [s["id"] for s in all_shots]
    every = SHOT_JUDGES + (VIDEO_JUDGE,)
    wanted_judges = list(judges) if judges else list(SHOT_JUDGES) + ([VIDEO_JUDGE] if shots is None and not changed else [])
    unknown = [k for k in wanted_judges if k not in every]
    if unknown:
        raise QualityError("unknown judges: %s" % ", ".join(unknown))
    if shots is not None:
        missing = [s for s in shots if s not in known]
        if missing:
            raise QualityError("unknown shot ids: %s" % ", ".join(missing))
    if changed and shots is not None:
        raise QualityError("--changed picks the shots itself; leave --shots out")
    if not videos["clean"].is_file():
        raise QualityError("missing %s" % videos["clean"])
    current, decoded = current_renders(videos, all_shots, meta)
    set_aside(existing, current, kinds)
    stamps = {kind: "%s@%d" % (path.name, int(path.stat().st_mtime)) if path.is_file() else "" for kind, path in videos.items()}
    selected = set(shots) if shots is not None else set(known)
    work: List[Tuple[int, str]] = []
    for index, shot in enumerate(all_shots):
        for key in wanted_judges:
            if key not in SHOT_JUDGES:
                continue
            follows = key == "continuity" and index > 0 and all_shots[index - 1]["id"] in selected
            if shot["id"] in selected or follows:
                work.append((index, key))
    if changed:
        old = {s["id"]: s.get("judges") or {} for s in existing.get("shots") or [] if isinstance(s, dict)}
        work = [(index, key) for index, shot in enumerate(all_shots) for key in wanted_judges
                if key in SHOT_JUDGES and (old.get(shot["id"], {}).get(key) or {}).get("status") not in ("pass", "fail")]
        selected = {all_shots[index]["id"] for index, _ in work}
    merged = {all_shots[index]["id"] for index, key in work if key in ("posture", "clipping", "expression")}
    sheets_dir = job / "quality" / "sheets"
    ledger_path = job / "quality" / "ledger.json"
    ledger = read_json(ledger_path, required=False) or {"runs": []}
    prior = sum(float(r.get("cost_usd", 0.0)) for r in ledger["runs"])
    budget = SharedBudget(float(settings["ceiling_usd"]), float(settings["reserve_usd"]), prior)
    workers = max(1, int(settings.get("workers", 8)))
    measures: Dict[str, Optional[Dict[str, Any]]] = {}
    if any(settings["judges"][key].get("measures") for key in wanted_judges if key in SHOT_JUDGES) and (work or confirm):
        frames = decoded["clean"]
        for shot in all_shots:
            measures[shot["id"]] = shot_measures(frames, *frame_range(shot, float(meta["fps"])))
    plans: Dict[Tuple[int, str], Dict[str, Any]] = {}
    unique: Dict[str, Dict[str, Any]] = {}

    def prepare(items: Sequence[Tuple[int, str]]) -> List[Dict[str, Any]]:
        """Plan the sheets of the given verdicts; returns the sheets that still have to be cut."""
        fresh = []
        for index, key in items:
            if (index, key) in plans:
                continue
            plan = plan_sheet(settings["judges"][key], index, all_shots, meta, videos)
            plan["source"] = stamps[str(settings["judges"][key].get("video", "clean"))]
            plans[(index, key)] = plan
            if settings["judges"][key].get("detail"):
                plan["detail"] = plan_sheet(dict(settings["judges"][key], neighbours=False, columns=None, **settings["judges"][key]["detail"]),
                                            index, all_shots, meta, videos)
            for sheet in [plan] + ([plan["detail"]] if plan.get("detail") else []):
                if sheet["name"] not in unique:
                    unique[sheet["name"]] = sheet
                    fresh.append(sheet)
        return fresh

    def cut(plan: Dict[str, Any]) -> None:
        target = sheets_dir / plan["name"]
        try:
            if not Path(plan["tiles"][0]["video"]).is_file():
                raise QualityError("missing %s" % plan["tiles"][0]["video"])
            build_sheet(plan["tiles"], target, plan["height"], plan["columns"])
        except (QualityError, EditError) as exc:
            plan["error"] = str(exc)[:200]
            if target.is_file():
                target.unlink()

    def judge(item: Tuple[int, str], vote: int = 0) -> Tuple[int, str, Dict[str, Any]]:
        index, key = item
        plan = plans[item]
        plan.setdefault("error", unique[plan["name"]].get("error") or unique[plan.get("detail", plan)["name"]].get("error"))
        sheet = sheets_dir / plan["name"]
        entry = judge_shot(key, settings, story, index, all_shots, plan, sheet, _rel(job, sheet), measures.get(all_shots[index]["id"]),
                           budget, job / "quality" / "manual", vote)
        return index, key, entry

    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(cut, prepare(work)))
        results = list(pool.map(judge, work))
    entries: Dict[str, Dict[str, Any]] = {}
    for old in existing.get("shots") or []:
        if isinstance(old, dict) and old.get("id") in known:
            entries[old["id"]] = old
    for shot in all_shots:
        entry = entries.setdefault(shot["id"], {"id": shot["id"], "judges": {}})
        entry.update({"set": shot["set"], "start": shot["start"], "end": shot["end"], "line": shot["line"]})
        if shot["id"] in measures:
            entry["measures"] = measures[shot["id"]]
    registry = dict(existing.get("renders") or {})

    def remember(kind: str, shot_ids: Sequence[str]) -> Dict[str, str]:
        marks = {}
        for shot_id in shot_ids:
            signature = (current.get(kind) or {}).get(shot_id)
            if signature is not None:
                marks[shot_id] = hashlib.sha1(signature).hexdigest()[:12]
                registry[marks[shot_id]] = signature.hex()
        return marks

    for index, key, record in results:
        record["render"] = remember(kinds[key], sorted({tile["shot"] for tile in plans[(index, key)]["tiles"]}))
        entries[all_shots[index]["id"]]["judges"][key] = record
    stale = [shot_id for shot_id in existing.get("geometry_stale", []) if geometry and shot_id not in merged]
    if geometry:
        stale += merge_geometry({shot_id: entry for shot_id, entry in entries.items() if shot_id in merged}, geometry, geometry_source)
    voting = settings.get("confirm") or {}
    ballots = int(voting.get("votes", 3))
    asked: List[Tuple[int, str]] = []

    def again(item: Tuple[int, str]) -> List[Dict[str, Any]]:
        extra: List[Dict[str, Any]] = []
        while len(extra) < ballots - 1 and (not extra or extra[-1].get("status") in ("pass", "fail")):
            extra.append(judge(item, len(extra) + 1)[2])
        return extra

    while ballots > 1:
        pending = [(index, key) for index, shot in enumerate(all_shots) for key in wanted_judges
                   if key in SHOT_JUDGES and (index, key) not in asked and (confirm or (index, key) in work)
                   and borderline(entries[shot["id"]], key, settings["judges"][key], float(voting.get("margin", 1)))]
        if not pending:
            break
        asked += pending
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(cut, prepare(pending)))
            more = list(pool.map(again, pending))
        for (index, key), extra in zip(pending, more):
            settle_votes(entries[all_shots[index]["id"]]["judges"][key], extra)
    doc = dict(existing)
    if VIDEO_JUDGE in wanted_judges and not (changed and (existing.get("video_judge") or {}).get("status") in ("pass", "fail")):
        doc["video_judge"] = judge_video(settings, story, all_shots, meta, videos["clean"], sheets_dir, job, budget)
        doc["video_judge"]["render"] = remember("clean", known)
    used = {mark for entry in entries.values() for record in entry["judges"].values() for mark in (record.get("render") or {}).values()}
    used |= set(((doc.get("video_judge") or {}).get("render") or {}).values())
    usage = budget.summary()
    usage["seconds"] = round(time.time() - started, 1)
    record = dict(usage, at=time.strftime("%Y-%m-%dT%H:%M:%S"), out=out.name, provider=settings["provider"], model=settings["model"],
                  shots=len(selected), judges=wanted_judges, video=stamps["clean"], voted=len(asked))
    ledger["runs"].append(record)
    write_json(ledger_path, ledger)
    runs = list(existing.get("runs") or []) + [record]
    total: Dict[str, Any] = {name: sum(r.get(name, 0) for r in runs) for name in ("calls", "input_tokens", "output_tokens")}
    total.update(cost_usd=round(sum(r.get("cost_usd", 0.0) for r in runs), 6), ceiling_usd=budget.ceiling_usd,
                 job_total_usd=round(prior + budget.cost_usd, 6), stopped_by_ceiling=budget.stopped_by_ceiling)
    judged = sorted(set((existing.get("config") or {}).get("judges") or []) | set(wanted_judges), key=every.index)
    doc.update({"version": 1, "job": job.name, "video": stamps["clean"], "final": stamps["captioned"],
                "config": {"provider": settings["provider"], "model": settings["model"], "judges": judged, "workers": workers,
                           "prices": settings["prices"], "rubric": settings["judges"], "video": settings[VIDEO_JUDGE]},
                "shots": [entries[i] for i in known], "runs": runs, "usage": total,
                "renders": {mark: value for mark, value in registry.items() if mark in used},
                "geometry_stale": stale if geometry else existing.get("geometry_stale", [])})
    finish(job, doc, out, human, baseline, against)
    return doc


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.quality", description=__doc__.splitlines()[0])
    parser.add_argument("--job", required=True, type=Path, help="job folder")
    parser.add_argument("--video", type=Path, help="rendered video without captions (default <job>/video.mp4)")
    parser.add_argument("--final", type=Path, help="captioned video for the framing judge (default <job>/final.mp4)")
    parser.add_argument("--shots", help="comma-separated shot ids; results merge into the existing report")
    parser.add_argument("--judges", help="comma-separated judges: %s, %s" % (", ".join(SHOT_JUDGES), VIDEO_JUDGE))
    parser.add_argument("--provider", choices=["gemini", "anthropic", "manual"])
    parser.add_argument("--model")
    parser.add_argument("--ceiling", type=float, help="spend ceiling in USD across every run on this job")
    parser.add_argument("--workers", type=int)
    parser.add_argument("--set", action="append", default=[], dest="sets", metavar="KEY=VALUE", help="override a setting, e.g. posture.frames=4")
    parser.add_argument("--out", type=Path, help="report file (default <job>/quality.json)")
    parser.add_argument("--posture", type=Path, help="geometric results to merge (default <job>/posture.json when present)")
    parser.add_argument("--human", type=Path, help="a person's verdicts to measure agreement against")
    parser.add_argument("--baseline", type=Path, help="an earlier report to measure the same verdicts against (agreement before)")
    parser.add_argument("--compare", type=Path, help="an earlier report for the before-and-after table")
    parser.add_argument("--report-only", action="store_true", help="rewrite the reports from the existing results without judging")
    parser.add_argument("--changed", action="store_true",
                        help="judge only what is missing or was set aside because its shot was rendered again")
    parser.add_argument("--confirm", action="store_true",
                        help="as --changed, and also put every borderline fail already in the report to three votes")
    args = parser.parse_args(argv)
    try:
        overrides = parse_sets(args.sets)
        for name, value in (("provider", args.provider), ("model", args.model), ("ceiling_usd", args.ceiling), ("workers", args.workers)):
            if value is not None:
                overrides[name] = value
        doc = run_quality(args.job, args.video, args.final, args.shots.split(",") if args.shots else None,
                          args.judges.split(",") if args.judges else None, overrides, args.out, args.human, args.baseline,
                          args.compare, args.report_only, args.posture, args.changed, args.confirm)
    except (QualityError, EditError, vision.VisionError) as exc:
        print("quality: %s" % exc, file=sys.stderr)
        return 1
    summary = doc["summary"]
    print("%d shots: %d pass, %d fail, %d incomplete" % (summary["shots"], summary["pass"], summary["fail"], summary["incomplete"]))
    for key in SHOT_JUDGES:
        row = summary["judges"][key]
        print("%-11s fail %2d  pass %2d  unjudged %2d  %s" % (key, row["fail"], row["pass"], row["unjudged"], " ".join(row["failing"])))
    voted = summary["voted"]
    if voted["kept"] or voted["cleared"]:
        print("put to the vote: %d stayed fails (%s), %d became passes (%s)" % (len(voted["kept"]), " ".join(voted["kept"]), len(voted["cleared"]),
                                                                           " ".join(voted["cleared"])))
    spend = summary["spend"]
    print("spend $%.4f over %d calls (job total $%.4f of $%.2f)" % (spend.get("cost_usd", 0.0), spend.get("calls", 0),
                                                                  spend.get("job_total_usd", 0.0), spend.get("ceiling_usd", 0.0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
