"""Judge stage: pick the camera for every shot by looking at candidate stills, then audit the finished video.

Usage:
  python -m pipeline.judge models --provider gemini|anthropic
  python -m pipeline.judge shots --job <folder> [--shots s01,s02] [--provider P] [--model M] [--ceiling USD] [--retries N] [--max-candidates N]
  python -m pipeline.judge video --job <folder> [--references DIR] [--provider P] [--model M]
  python -m pipeline.judge show --job <folder>

For each shot the stage proposes several camera setups, drops the ones that fail the geometric checks done
inside Studio (head in frame, camera inside the room, caption band clear, not a repeat of the previous shot),
renders one still per survivor, tiles them into a labelled contact sheet and asks a vision model to score
every tile against studio/library/rubric.json. Totals are computed here from the rubric weights, never taken
from the model. When the best total is under the threshold the shot is restaged with new candidates, up to
"retries" times, and the best candidate over all rounds wins. The Studio side is a stager object with
geometry(shot_id, cameras, previous) and stills(shot_id, cameras, out_dir); this module never talks to Studio.
Spending stops at a USD ceiling, after which shots keep their best geometric candidate. The result is
<job>/judge.json; chosen_cameras() reads it back for the timeline builder. judge_video() scores one mid-shot
frame per shot from <job>/video.mp4 against the same rubric plus a production-quality criterion and lists
the weakest shots. Settings come from defaults, story.json "judge", and the command line, in that order.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import vision
from .edit import FONTS_DIR, EditError, esc, ffmpeg_bin, run

REPO = Path(__file__).resolve().parent.parent
RUBRIC_PATH = REPO / "studio" / "library" / "rubric.json"
FONT = FONTS_DIR / "Montserrat-ExtraBold.ttf"
DEFAULTS: Dict[str, Any] = {
    "provider": "gemini",
    "model": "",
    "max_candidates": 6,
    "retries": 1,
    "threshold": None,
    "ceiling_usd": 2.0,
    "still_height": 480,
    "scout_below": 3,
    "prices": {},
}
FRAMING_KEYS = ("on", "frame", "yaw", "pitch", "head", "dutch", "ots", "pov", "move", "amount", "from", "pos", "locked")
SHOT_TYPES = ("wide", "medium", "close", "extreme_close", "over_shoulder", "low_angle", "high_angle", "tilted", "pov", "insert")
FRAME_TYPES = {"extreme": "extreme_close", "detail": "extreme_close", "close": "close", "medium": "medium", "full": "medium", "wide": "wide", "vast": "wide"}
MOVE_KINDS = {"static": "static", "push": "push_in", "creep": "push_in", "pull": "pull_out", "pan_left": "pan", "pan_right": "pan",
              "tilt_up": "pan", "tilt_down": "pan", "drift_left": "track", "drift_right": "track", "rise": "track", "drop": "track",
              "handheld": "handheld", "orbit_left": "orbit", "orbit_right": "orbit"}
LOW_PITCH, HIGH_PITCH, TILT_DEGREES = -12.0, 20.0, 6.0
FRONT_YAW, BACK_YAW = 25.0, 155.0
TYPE_PITCHES = {"low_angle": (-16, -24), "high_angle": (26, 36)}
VARIETY_DEFAULTS = {"cap": 0.2, "slack": 0.02, "min_moving": 0.5}
CAPTION_TOP, CAPTION_HEIGHT = 0.60, 0.18
SCOUT_FRAMES = {"extreme": ("close",), "close": ("medium",), "medium": ("close", "full"), "full": ("medium", "wide"), "wide": ("full",), "vast": ("wide",)}
SCOUT_PITCHES = (6, 22, -12)
SCOUT_STEPS = 12
FACE_ON_YAW = 70.0
SHEET_COLUMNS = 6
SHEET_SIZE = 12
WEAKEST_COUNT = 8
MAX_REFERENCES = 4
QUALITY_WEIGHT = 0.2
VIDEO_TOKENS = 5000
BOX_COLORS = ["red", "green", "blue", "yellow", "magenta", "cyan", "orange", "purple"]
SYSTEM_PROMPT = (
    "You are a cinematographer and film editor judging camera choices for a vertical 9:16 short video "
    "that tells a narrated story with animated characters. Judge only what is visible. Be strict and use the "
    "full 0 to 10 range. Reply with one JSON object and nothing else."
)


class JudgeError(RuntimeError):
    """Raised for failures the operator can act on."""


def load_rubric(path: Path = RUBRIC_PATH) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def total_score(scores: Dict[str, Any], criteria: Sequence[Dict[str, Any]]) -> float:
    """Weighted sum of 0 to 10 scores; a missing or non-numeric score counts as 0."""
    total = 0.0
    for criterion in criteria:
        try:
            value = float(scores.get(criterion["key"], 0))
        except (TypeError, ValueError):
            value = 0.0
        total += criterion["weight"] * min(10.0, max(0.0, value))
    return round(total, 3)


def resolve_config(story: Dict[str, Any], config: Optional[Dict[str, Any]], rubric: Dict[str, Any]) -> Dict[str, Any]:
    """Defaults, then story.json "judge", then explicit settings; None values never override."""
    merged = copy.deepcopy(DEFAULTS)
    merged.update(story.get("judge") or {})
    merged.update({key: value for key, value in (config or {}).items() if value is not None})
    if merged["threshold"] is None:
        merged["threshold"] = rubric["threshold"]
    return merged


def require_model(config: Dict[str, Any]) -> None:
    if config["provider"] != "none" and not config["model"]:
        raise JudgeError("no model set; run python -m pipeline.judge models --provider %s and pass one with --model" % config["provider"])


def read_json(path: Path, required: bool = True) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        if required:
            raise JudgeError("%s not found" % path)
        return None
    except ValueError as exc:
        raise JudgeError("%s is not valid JSON: %s" % (path, exc))


def read_job(job: Path) -> Dict[str, Any]:
    story = read_json(job / "story.json", required=False) or {}
    words = read_json(job / "words.json", required=False) or {}
    return {"timeline": read_json(job / "timeline.json"), "story": story, "words": words.get("words", [])}


def shot_text(shot: Dict[str, Any], words: Sequence[Dict[str, Any]]) -> str:
    """The words spoken during a shot, rebuilt from words.json when the shot carries no text."""
    if shot.get("text"):
        return str(shot["text"])
    if "word_start" in shot and "word_end" in shot:
        picked = words[int(shot["word_start"]):int(shot["word_end"]) + 1]
        return " ".join(str(word.get("display", word.get("token", ""))) for word in picked)
    return ""


def _subject(shot: Dict[str, Any]) -> Optional[str]:
    camera = shot.get("camera") or {}
    if camera.get("on"):
        return str(camera["on"])
    cast = shot.get("cast") or []
    return str(cast[0]["who"]) if cast else None


def is_locked(shot: Dict[str, Any]) -> bool:
    """A fixed or explicitly locked camera is never restaged."""
    camera = shot.get("camera") or {}
    return bool(camera.get("locked")) or "from" in camera or "pos" in camera or _subject(shot) is None


def _fixed(camera: Dict[str, Any]) -> bool:
    return camera.get("from") is not None or "pos" in camera or not camera.get("on")


def geometric_type(camera: Dict[str, Any]) -> Optional[str]:
    """The shot type a subject camera's own numbers give it; None for a fixed camera, which has to be tagged."""
    if camera.get("pov"):
        return "pov"
    if camera.get("ots"):
        return "over_shoulder"
    if _fixed(camera):
        return None
    pitch = float(camera.get("pitch") or 0)
    if abs(float(camera.get("dutch") or 0)) >= TILT_DEGREES:
        return "tilted"
    if pitch <= LOW_PITCH:
        return "low_angle"
    if pitch >= HIGH_PITCH:
        return "high_angle"
    return FRAME_TYPES.get(str(camera.get("frame") or "medium"), "medium")


def camera_type(camera: Dict[str, Any]) -> str:
    """One of SHOT_TYPES: the camera's "type" tag when it has one, else what its geometry says; an untagged fixed camera is an insert."""
    return str(camera.get("type") or geometric_type(camera) or "insert")


def camera_side(camera: Dict[str, Any]) -> str:
    """Which side of the subject the camera is on (front, left, right, back), or the fixed position it shoots from."""
    if camera.get("pov"):
        return "pov:%s" % camera["pov"]
    if _fixed(camera):
        place = camera.get("from") if camera.get("from") is not None else camera.get("pos")
        if isinstance(place, dict):
            place = place.get("pos") or place.get("on")
        return "fixed:%s" % json.dumps(place)
    yaw = (float(camera.get("yaw") or 0) + 180.0) % 360.0 - 180.0
    if abs(yaw) <= FRONT_YAW:
        return "front"
    if abs(yaw) >= BACK_YAW:
        return "back"
    return "left" if yaw < 0 else "right"


def camera_move(camera: Dict[str, Any]) -> str:
    return MOVE_KINDS.get(str(camera.get("move") or "push"), "push_in")


def repeats(camera: Dict[str, Any], previous: Optional[Dict[str, Any]]) -> bool:
    """True when a camera has both the type and the side of the previous shot's camera."""
    return previous is not None and camera_type(camera) == camera_type(previous) and camera_side(camera) == camera_side(previous)


def variety(shots: Sequence[Dict[str, Any]], cameras: Optional[Dict[str, Dict[str, Any]]] = None,
            settings: Optional[Dict[str, Any]] = None, locked_neighbours_only: bool = False) -> Dict[str, Any]:
    """The shot-type and camera-move distribution of a timeline, with every break of the variety rules listed.

    Shares are of screen time. The rules: no type above cap (plus slack), neighbouring shots never share both type
    and side, at least min_moving of the time on a moving camera, and a "type" tag never contradicts the camera.
    """
    rules = dict(VARIETY_DEFAULTS, **(settings or {}))
    rows = []
    for shot in shots:
        camera = (cameras or {}).get(shot["id"]) or shot.get("camera") or {}
        seconds = float(shot.get("end", 0)) - float(shot.get("start", 0)) if "end" in shot else 1.0
        rows.append({"id": shot["id"], "seconds": seconds, "type": camera_type(camera), "side": camera_side(camera),
                     "move": camera_move(camera), "tag": camera.get("type"), "geometry": geometric_type(camera), "locked": is_locked(shot)})
    total = sum(row["seconds"] for row in rows) or 1.0

    def table(key: str) -> Dict[str, Dict[str, Any]]:
        found: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            item = found.setdefault(row[key], {"shots": 0, "seconds": 0.0})
            item["shots"] += 1
            item["seconds"] += row["seconds"]
        for item in found.values():
            item["share"] = round(item["seconds"] / total, 4)
            item["seconds"] = round(item["seconds"], 3)
        return dict(sorted(found.items(), key=lambda pair: -pair[1]["share"]))

    types, moves = table("type"), table("move")
    moving = round(1.0 - moves.get("static", {"share": 0.0})["share"], 4)
    problems = []
    for name, item in types.items():
        if name not in SHOT_TYPES:
            problems.append("type %r is not one of %s" % (name, ", ".join(SHOT_TYPES)))
        if item["share"] > rules["cap"] + rules["slack"]:
            problems.append("%s takes %.0f%% of the video (cap %.0f%%)" % (name, 100 * item["share"], 100 * rules["cap"]))
    for before, after in zip(rows, rows[1:]):
        if before["type"] == after["type"] and before["side"] == after["side"] and (
                not locked_neighbours_only or (before["locked"] and after["locked"])):
            problems.append("%s and %s are both %s from %s" % (before["id"], after["id"], after["type"], after["side"]))
    for row in rows:
        if row["tag"] and row["geometry"] and row["tag"] != row["geometry"]:
            problems.append("%s is tagged %s but its camera is %s" % (row["id"], row["tag"], row["geometry"]))
    if moving < rules["min_moving"]:
        problems.append("only %.0f%% of the video is on a moving camera (minimum %.0f%%)" % (100 * moving, 100 * rules["min_moving"]))
    return {"rules": rules, "shots": len(rows), "seconds": round(total, 3), "types": types, "moves": moves, "moving_share": moving,
            "problems": problems, "sequence": [{"id": row["id"], "type": row["type"], "side": row["side"], "move": row["move"]} for row in rows]}


def describe_variety(report: Dict[str, Any]) -> str:
    types = ", ".join("%s %.0f%%" % (name, 100 * item["share"]) for name, item in report["types"].items())
    moves = ", ".join("%s %.0f%%" % (name, 100 * item["share"]) for name, item in report["moves"].items())
    return "types: %s\nmoves: %s (moving %.0f%%)" % (types, moves, 100 * report["moving_share"])


def _typed_specs(camera: Dict[str, Any], round_index: int) -> List[Tuple[str, Dict[str, Any]]]:
    """Variations that keep a tagged camera's shot type: the same framing from other sides of the subject."""
    if camera.get("pov") or camera.get("ots"):
        return []
    base = {key: camera[key] for key in ("on", "frame", "pitch", "head", "dutch", "move", "amount") if key in camera}
    yaw = float(camera.get("yaw") or 0)
    turns = (-2 * yaw, 35, -35, 70, -70) if round_index == 0 else (20, -20, 55, -55, 110, -110)
    specs = []
    for turn in turns:
        if abs(turn) < 15:
            continue
        specs.append(("%s_turn%+d" % (camera["type"], turn), dict(base, yaw=round((yaw + turn + 180.0) % 360.0 - 180.0, 1))))
    return specs


def propose(shot: Dict[str, Any], round_index: int, max_candidates: int) -> List[Dict[str, Any]]:
    """Candidate cameras for one shot; round 0 starts with the author's own camera, later rounds restage.

    A camera tagged with a "type" only gets candidates of that type, so the judge chooses the side and
    never changes what kind of shot it is.
    """
    camera = shot.get("camera") or {}
    author = {"name": "author", "camera": copy.deepcopy(camera)}
    if is_locked(shot):
        return [author]
    subject = _subject(shot)
    others = [str(a["who"]) for a in shot.get("cast") or [] if str(a["who"]) != subject]
    other = others[0] if others else None
    if camera.get("type"):
        specs = _typed_specs(camera, round_index)
    elif round_index == 0:
        specs = [
            ("close_three_quarter", {"on": subject, "frame": "close", "yaw": 30, "pitch": 4, "move": "push"}),
            ("medium_other_side", {"on": subject, "frame": "medium", "yaw": -35, "move": "drift_left"}),
            ("low_angle", {"on": subject, "frame": "medium", "yaw": 20, "pitch": -18, "head": 0.3, "move": "push"}),
            ("high_angle", {"on": subject, "frame": "full", "yaw": -15, "pitch": 28, "move": "creep"}),
            ("over_shoulder", {"on": subject, "ots": other, "move": "creep"}) if other
            else ("dutch_close", {"on": subject, "frame": "close", "yaw": -25, "dutch": 12, "move": "handheld"}),
        ]
    else:
        specs = [
            ("wide_establishing", {"on": subject, "frame": "wide", "yaw": 25, "pitch": 10, "move": "push"}),
            ("extreme_close", {"on": subject, "frame": "extreme", "yaw": 12, "move": "creep"}),
            ("profile", {"on": subject, "frame": "medium", "yaw": 80, "move": "drift_right"}),
            ("dutch_low", {"on": subject, "frame": "medium", "yaw": -30, "pitch": -14, "dutch": -10, "move": "push"}),
            ("back_view", {"on": subject, "frame": "full", "yaw": 160, "pitch": 8, "move": "creep"}),
        ]
        if other:
            specs.append(("pov", {"on": subject, "pov": other, "move": "handheld"}))
        if round_index > 1:
            specs = [("%s_r%d" % (name, round_index), dict(spec, yaw=spec["yaw"] + (20 if (i + round_index) % 2 == 0 else -20)))
                     for i, (name, spec) in enumerate(specs) if "yaw" in spec]
    kept = {key: value for key, value in camera.items() if key not in FRAMING_KEYS}
    if camera.get("face_on"):
        specs = [(name, spec) for name, spec in specs if "yaw" in spec and abs(spec["yaw"]) <= FACE_ON_YAW]
    candidates = [{"name": name, "camera": dict(kept, **spec)} for name, spec in specs]
    if round_index == 0 or (camera.get("type") and not candidates):
        candidates.insert(0, author)
    return candidates[:max(1, max_candidates)]


def _turn(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _spread(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    return _turn(a["yaw"], b["yaw"]) + 2.0 * abs(a["pitch"] - b["pitch"]) + (50.0 if a["frame"] != b["frame"] else 0.0)


def scout(shot: Dict[str, Any], stager: Any, previous: Optional[Dict[str, Any]], limit: int, round_index: int = 0) -> List[Dict[str, Any]]:
    """Sweep the camera around the subject and keep setups that pass the geometric checks.

    The first one kept is the valid setup nearest the author's camera; the rest are chosen to differ
    from those already kept. Used when too few of the proposed cameras survive, which is what happens
    when a character stands close to a wall or a door. A camera marked "face_on" only gets setups
    within FACE_ON_YAW degrees of the subject's front.
    """
    camera = shot.get("camera") or {}
    subject = _subject(shot)
    if limit <= 0 or subject is None:
        return []
    kept = {key: value for key, value in camera.items() if key not in FRAMING_KEYS}
    wish = {"frame": camera.get("frame") or "medium", "yaw": float(camera.get("yaw") or 0), "pitch": float(camera.get("pitch") or 6)}
    frames = [wish["frame"]] + [name for name in SCOUT_FRAMES.get(wish["frame"], ()) if name != wish["frame"]]
    pitches = [wish["pitch"]] + [pitch for pitch in TYPE_PITCHES.get(str(camera.get("type")), SCOUT_PITCHES) if abs(pitch - wish["pitch"]) > 5]
    extra = {key: camera[key] for key in ("dutch", "head", "amount") if key in camera and camera.get("type")}
    specs = []
    for frame in frames:
        for pitch in pitches:
            for step in range(SCOUT_STEPS):
                yaw = (wish["yaw"] + 15.0 * (round_index % 2) + step * 360.0 / SCOUT_STEPS + 180.0) % 360.0 - 180.0
                if camera.get("face_on") and abs(yaw) > FACE_ON_YAW:
                    continue
                spec = dict(extra, on=subject, frame=frame, yaw=round(yaw, 1), pitch=pitch, move=camera.get("move") or "push")
                if not camera.get("type") or geometric_type(spec) == camera["type"]:
                    specs.append(spec)
    if not specs:
        return []
    checks = stager.geometry(shot["id"], [dict(kept, **spec) for spec in specs], previous)
    valid = [(spec, check) for spec, check in zip(specs, checks) if check.get("ok")]
    picked: List[Any] = []
    while valid and len(picked) < limit:
        if picked:
            best = max(valid, key=lambda item: min(_spread(item[0], other[0]) for other in picked) - 60.0 * len(item[1].get("issues") or []))
        else:
            best = min(valid, key=lambda item: _spread(item[0], wish) + 60.0 * len(item[1].get("issues") or []))
        valid.remove(best)
        picked.append(best)
    return [{"name": "scout_%s_y%d_p%d" % (spec["frame"], spec["yaw"], spec["pitch"]), "camera": dict(kept, **spec), "geometry": check}
            for spec, check in picked]


@lru_cache(maxsize=1)
def drawtext_available() -> bool:
    filters = run([ffmpeg_bin(), "-hide_banner", "-filters"]).stdout
    return re.search(r"(?m)^\s*\S+\s+drawtext\s", filters) is not None and FONT.is_file()


def _tile_chain(index: int, tile: Dict[str, Any], width: int, height: int, mode: str, work: Path) -> str:
    chain = [
        "scale=%d:%d" % (width, height),
        "drawbox=x=0:y=ih*%s:w=iw:h=ih*%s:color=black@0.6:t=fill" % (CAPTION_TOP, CAPTION_HEIGHT),
    ]
    if mode == "boxes":
        side = max(8, int(height * 0.08))
        chain.append("drawbox=x=6:y=6:w=%d:h=%d:color=%s:t=fill" % (side, side, tile["color"]))
        return "[%d:v]%s[t%d]" % (index, ",".join(chain), index)
    font = "fontfile=%s:expansion=none:fontcolor=white" % esc(FONT)
    if tile.get("word"):
        word_file = work / ("word%d.txt" % index)
        word_file.write_text(tile["word"], encoding="utf-8")
        size = max(10, min(int(height * 0.05), int(width * 1.4 / len(tile["word"]))))
        chain.append("drawtext=%s:textfile=%s:fontsize=%d:x=(w-text_w)/2:y=h*%s-text_h/2"
                     % (font, esc(word_file), size, CAPTION_TOP + CAPTION_HEIGHT / 2))
    label_file = work / ("label%d.txt" % index)
    label_file.write_text(tile["label"], encoding="utf-8")
    chain.append("drawtext=%s:textfile=%s:fontsize=%d:x=8:y=8:box=1:boxcolor=black@0.75:boxborderw=6"
                 % (font, esc(label_file), max(12, int(height * 0.07))))
    return "[%d:v]%s[t%d]" % (index, ",".join(chain), index)


def contact_sheet(tiles: Sequence[Dict[str, Any]], out: Path, height: int, columns: int, mode: str) -> None:
    """Tile images into one labelled JPEG; each tile is {"path", "label", "word", "color"}."""
    width = int(round(height * 1080 / 1920 / 2.0)) * 2
    work = Path(tempfile.mkdtemp(prefix="judge-"))
    try:
        graph = [_tile_chain(i, tile, width, height, mode, work) for i, tile in enumerate(tiles)]
        if len(tiles) == 1:
            graph.append("[t0]format=yuvj420p[out]")
        else:
            layout = "|".join("%d_%d" % ((i % columns) * width, (i // columns) * height) for i in range(len(tiles)))
            graph.append("%sxstack=inputs=%d:layout=%s:fill=black,format=yuvj420p[out]"
                         % ("".join("[t%d]" % i for i in range(len(tiles))), len(tiles), layout))
        cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error"]
        for tile in tiles:
            cmd += ["-i", str(tile["path"])]
        out.parent.mkdir(parents=True, exist_ok=True)
        run(cmd + ["-filter_complex", ";".join(graph), "-map", "[out]", "-frames:v", "1", "-q:v", "3", str(out)])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _rubric_lines(criteria: Sequence[Dict[str, Any]]) -> str:
    return "\n".join("- %s (weight %.2f): %s" % (c["key"], c["weight"], c["question"]) for c in criteria)


def _legend(labels: Sequence[str], mode: str) -> str:
    if mode == "text":
        return "Each tile has its letter in the top-left corner."
    parts = ["%s = %s corner box" % (label, BOX_COLORS[i % len(BOX_COLORS)]) for i, label in enumerate(labels)]
    return "Tiles are marked by a coloured box in the top-left corner (%s; PREV = no box)." % ", ".join(parts)


def shots_prompt(criteria: Sequence[Dict[str, Any]], story: Dict[str, Any], text: str, candidates: Sequence[Dict[str, Any]],
                 has_prev: bool, mode: str, intent: str = "") -> str:
    lines = ["Story genre: %s." % story.get("genre", "story")]
    if story.get("title"):
        lines.append("Title: %s." % story["title"])
    lines.append('The narrator says during this shot: "%s"' % text)
    if intent:
        lines.append("The director wants this shot to show: %s. Count that under the first criterion." % intent)
    lines.append("")
    lines.append("The image is a contact sheet of candidate camera setups for this one shot. " + _legend([c["label"] for c in candidates], mode))
    lines.append("A dark translucent band on every tile marks where the caption will be drawn; it holds the first word of the sentence.")
    if has_prev:
        lines.append("The last tile, PREV, is the previous shot's chosen frame; it is not a candidate.")
    lines.append("")
    lines.append("Candidates:")
    for c in candidates:
        issues = c["geometry"].get("issues") or []
        lines.append("%s: %s (geometry notes: %s)" % (c["label"], c["name"], "; ".join(issues) if issues else "none"))
    lines += ["", "Score every candidate from 0 to 10 on each criterion:", _rubric_lines(criteria), "",
              'Reply with JSON only: {"candidates": [{"label": "A", "scores": {%s}, "reason": "one line"}]}'
              % ", ".join('"%s": 0' % c["key"] for c in criteria)]
    return "\n".join(lines)


def parse_scores(data: Optional[Dict[str, Any]], labels: Sequence[str], criteria: Sequence[Dict[str, Any]], key: str = "label",
                 listing: str = "candidates") -> Dict[str, Dict[str, Any]]:
    """Map each label to {"scores", "total", "reason"} from a model reply; unknown labels are ignored."""
    found: Dict[str, Dict[str, Any]] = {}
    items = (data or {}).get(listing)
    if not isinstance(items, list):
        return found
    for item in items:
        if not isinstance(item, dict) or str(item.get(key, "")).strip() not in labels:
            continue
        raw = item.get("scores") if isinstance(item.get("scores"), dict) else {}
        scores = {}
        for criterion in criteria:
            try:
                scores[criterion["key"]] = min(10.0, max(0.0, float(raw.get(criterion["key"], 0))))
            except (TypeError, ValueError):
                scores[criterion["key"]] = 0.0
        found[str(item[key]).strip()] = {"scores": scores, "total": total_score(scores, criteria), "reason": str(item.get("reason", ""))}
    return found


def _rel(job: Path, path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(job.resolve()))
    except ValueError:
        return str(path)


def _ask_scores(job: Path, cfg: Dict[str, Any], budget: vision.Budget, images: Sequence[Path], prompt: str, labels: Sequence[str],
                criteria: Sequence[Dict[str, Any]], request_id: str, max_tokens: int, key: str, listing: str) -> Dict[str, Dict[str, Any]]:
    """Ask the model, retrying once when the reply holds no usable scores; raises BudgetExceeded when out of budget."""
    for attempt in range(2):
        budget.check()
        result = vision.ask(cfg["provider"], cfg["model"], SYSTEM_PROMPT, prompt, list(images), max_output_tokens=max_tokens,
                            prices=cfg["prices"], manual_dir=job / "judge" / "manual", manual_id="%s_%d" % (request_id, attempt))
        budget.add(result)
        found = parse_scores(result["json"], labels, criteria, key, listing)
        if found:
            return found
    raise JudgeError("the model returned no usable scores for %s" % request_id)


def _chosen_record(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {"label": candidate["label"], "round": candidate["round"], "name": candidate["name"], "camera": candidate["camera"],
            "total": candidate.get("total"), "reason": candidate.get("reason", ""),
            "metrics": (candidate.get("geometry") or {}).get("metrics") or {}}


def _chosen_still(job: Path, entry: Dict[str, Any]) -> Optional[Path]:
    chosen = entry.get("chosen") or {}
    for record in entry.get("rounds", []):
        if record["round"] != chosen.get("round"):
            continue
        for candidate in record["candidates"]:
            if candidate["label"] == chosen.get("label") and candidate.get("still"):
                path = job / candidate["still"]
                return path if path.is_file() else None
    return None


def _merge_usage(base: Optional[Dict[str, Any]], current: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(current)
    for key in ("calls", "input_tokens", "output_tokens"):
        merged[key] = int((base or {}).get(key, 0)) + int(current[key])
    merged["cost_usd"] = round(float((base or {}).get("cost_usd", 0.0)) + float(current["cost_usd"]), 6)
    return merged


def _write_judge(job: Path, doc: Dict[str, Any]) -> None:
    (job / "judge.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _load_judge(job: Path) -> Dict[str, Any]:
    try:
        data = json.loads((job / "judge.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _geometry_pick(candidates: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    for candidate in candidates:
        if candidate["name"] == "author":
            return candidate
    return candidates[0]


def _judge_shot(job: Path, shot: Dict[str, Any], text: str, previous: Optional[Dict[str, Any]], prev_still: Optional[Path],
                stager: Any, cfg: Dict[str, Any], rubric: Dict[str, Any], story: Dict[str, Any], budget: vision.Budget) -> Dict[str, Any]:
    shot_id = shot["id"]
    entry: Dict[str, Any] = {"id": shot_id, "text": text, "status": "", "rounds": [], "chosen": None}
    if is_locked(shot):
        camera = propose(shot, 0, 1)[0]["camera"]
        entry["status"] = "locked"
        entry["chosen"] = {"label": "A", "round": 0, "name": "author", "camera": camera, "total": None, "reason": "fixed camera, not judged"}
        return entry
    use_model = cfg["provider"] != "none"
    stopped = use_model and budget.stopped_by_ceiling
    scored: List[Dict[str, Any]] = []
    usable: List[Dict[str, Any]] = []
    fallback: Optional[Dict[str, Any]] = None
    first_word = text.split()[0] if text.split() else ""
    for round_index in range(cfg["retries"] + 1):
        proposals = propose(shot, round_index, cfg["max_candidates"])
        checks = stager.geometry(shot_id, [p["camera"] for p in proposals], previous)
        if len(checks) != len(proposals):
            raise JudgeError("stager.geometry returned %d results for %d cameras of %s" % (len(checks), len(proposals), shot_id))
        for proposal, check in zip(proposals, checks):
            if check.get("ok") and previous and repeats(proposal["camera"], previous.get("camera")):
                check = dict(check, ok=False, issues=list(check.get("issues") or []) + ["same shot type and side as the previous shot"])
            proposal.update({"round": round_index, "geometry": check, "still": None, "scores": None, "total": None, "reason": ""})
        good = [p for p in proposals if p["geometry"].get("ok")]
        bad = [p for p in proposals if not p["geometry"].get("ok")]
        if len(good) < cfg["scout_below"]:
            found = scout(shot, stager, previous, cfg["max_candidates"] - len(good), round_index)
            found = [p for p in found if not (previous and repeats(p["camera"], previous.get("camera")))]
            for proposal in found:
                proposal.update({"round": round_index, "still": None, "scores": None, "total": None, "reason": ""})
            proposals += found
            good += found
        if not good:
            least = min(bad, key=lambda p: len(p["geometry"].get("issues") or []))
            if fallback is None or len(least["geometry"].get("issues") or []) < len(fallback["geometry"].get("issues") or []):
                fallback = least
        survivors = good or [min(bad, key=lambda p: len(p["geometry"].get("issues") or []))]
        for label, candidate in zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", survivors):
            candidate["label"] = label
        record: Dict[str, Any] = {"round": round_index, "sheet": None, "candidates": survivors,
                                  "dropped": [{"name": p["name"], "camera": p["camera"], "geometry": p["geometry"]} for p in bad if good]}
        entry["rounds"].append(record)
        usable += good
        if stopped:
            break
        if not use_model:
            if good:
                break
            continue
        if not good:
            continue
        try:
            budget.check()
            out_dir = job / "judge" / ("%s_r%d" % (shot_id, round_index))
            stills = stager.stills(shot_id, [c["camera"] for c in good], out_dir)
            if len(stills) != len(good):
                raise JudgeError("stager.stills returned %d images for %d cameras of %s" % (len(stills), len(good), shot_id))
            for candidate, still in zip(good, stills):
                candidate["still"] = _rel(job, Path(still))
            tiles = [{"path": Path(s), "label": c["label"], "word": first_word, "color": BOX_COLORS[i % len(BOX_COLORS)]}
                     for i, (c, s) in enumerate(zip(good, stills))]
            if prev_still is not None:
                tiles.append({"path": prev_still, "label": "PREV", "word": first_word, "color": "white"})
            sheet = job / "judge" / ("%s_r%d.jpg" % (shot_id, round_index))
            contact_sheet(tiles, sheet, cfg["still_height"], SHEET_COLUMNS + 1, cfg["labels"])
            record["sheet"] = _rel(job, sheet)
            prompt = shots_prompt(rubric["criteria"], story, text, good, prev_still is not None, cfg["labels"],
                                  str((shot.get("camera") or {}).get("intent") or ""))
            try:
                found = _ask_scores(job, cfg, budget, [sheet], prompt, [c["label"] for c in good], rubric["criteria"],
                                    "%s_r%d" % (shot_id, round_index), 1500, "label", "candidates")
            except JudgeError as exc:
                record["error"] = str(exc)
                continue
        except vision.BudgetExceeded:
            stopped = True
            break
        for candidate in good:
            result = found.get(candidate["label"], {"scores": None, "total": 0.0, "reason": "not scored by the model"})
            candidate.update(scores=result["scores"], total=result["total"], reason=result["reason"])
            scored.append(candidate)
        best = max(scored, key=lambda c: c["total"])
        if best["total"] >= cfg["threshold"]:
            break
    if scored:
        best = max(scored, key=lambda c: (c["total"], c["name"] == "author"))
        entry["status"] = "budget_stopped" if stopped else ("picked" if best["total"] >= cfg["threshold"] else "below_threshold")
    elif usable:
        best = _geometry_pick(usable)
        entry["status"] = "budget_stopped" if stopped else "geometry_only"
    else:
        best = fallback  # type: ignore[assignment]
        entry["status"] = "geometry_failed"
    entry["chosen"] = _chosen_record(best)
    return entry


def judge_shots(job: Path, stager: Any, config: Optional[Dict[str, Any]] = None, shots: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Choose a camera for every shot (or the listed ones) and write judge.json, merging with any earlier run."""
    job = Path(job)
    data = read_job(job)
    rubric = load_rubric()
    cfg = resolve_config(data["story"], config, rubric)
    require_model(cfg)
    timeline_shots = data["timeline"]["shots"]
    known = [s["id"] for s in timeline_shots]
    if shots is not None:
        missing = [s for s in shots if s not in known]
        if missing:
            raise JudgeError("unknown shot ids: %s" % ", ".join(missing))
    if cfg["provider"] != "none" and "labels" not in cfg:
        cfg["labels"] = "text" if drawtext_available() else "boxes"
    rubric["threshold"] = cfg["threshold"]
    if cfg.get("variety") is not None:
        authored = variety(timeline_shots, None, cfg["variety"], locked_neighbours_only=True)
        if authored["problems"]:
            raise JudgeError("the shot list breaks the variety rules before any camera is tried: %s" % "; ".join(authored["problems"]))
    existing = _load_judge(job)
    entries = {e["id"]: e for e in existing.get("shots", []) if isinstance(e, dict) and "id" in e}
    budget = vision.Budget(cfg["ceiling_usd"])
    doc = dict(existing)

    def save() -> None:
        ordered = [entries[i] for i in known if i in entries] + [e for i, e in entries.items() if i not in known]
        cameras = {i: e["chosen"]["camera"] for i, e in entries.items() if e.get("chosen")}
        doc.update({"version": 1, "config": cfg, "rubric": rubric, "shots": ordered, "variety": variety(timeline_shots, cameras, cfg.get("variety")),
                    "usage": _merge_usage(existing.get("usage"), budget.summary())})
        _write_judge(job, doc)

    wanted = set(shots) if shots is not None else None
    previous: Optional[Dict[str, Any]] = None
    prev_still: Optional[Path] = None
    for shot in timeline_shots:
        if wanted is not None and shot["id"] not in wanted:
            previous_entry = entries.get(shot["id"])
            previous = previous_entry["chosen"] if previous_entry else None
            prev_still = _chosen_still(job, previous_entry) if previous_entry else None
            continue
        entry = _judge_shot(job, shot, shot_text(shot, data["words"]), previous, prev_still, stager, cfg, rubric, data["story"], budget)
        entries[shot["id"]] = entry
        previous, prev_still = entry["chosen"], _chosen_still(job, entry)
        chosen = entry["chosen"]
        total = "-" if chosen["total"] is None else "%.2f" % chosen["total"]
        print("%s %-15s %-22s %5s  %s" % (shot["id"], entry["status"], chosen["name"], total, chosen["reason"][:70]), flush=True)
        save()
    save()
    return doc


def chosen_cameras(job: Path) -> Dict[str, Dict[str, Any]]:
    """Shot id to chosen camera from judge.json; empty when the file is missing.

    A shot whose camera is fixed or locked in the timeline keeps its own camera, whatever an earlier run chose.
    """
    doc = _load_judge(Path(job))
    timeline = read_json(Path(job) / "timeline.json", required=False) or {}
    own = {shot["id"]: shot.get("camera") for shot in timeline.get("shots", []) if is_locked(shot)}
    return {e["id"]: own.get(e["id"], e["chosen"]["camera"]) for e in doc.get("shots", []) if e.get("chosen")}


def video_rubric(rubric: Dict[str, Any]) -> Dict[str, Any]:
    criteria = [dict(c, weight=round(c["weight"] * (1 - QUALITY_WEIGHT), 6)) for c in rubric["criteria"]]
    criteria.append({"key": "quality", "weight": QUALITY_WEIGHT, "question":
                     "Overall production quality compared with the reference frames (what a finished video in this format looks like)."})
    return dict(rubric, criteria=criteria)


def _reference_files(references: Any) -> List[Path]:
    if references is None:
        return []
    if isinstance(references, (str, Path)) and Path(references).is_dir():
        paths = sorted(p for p in Path(references).iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    else:
        paths = [Path(p) for p in ([references] if isinstance(references, (str, Path)) else references)]
    for path in paths:
        if not path.is_file():
            raise JudgeError("reference image %s not found" % path)
    return paths[:MAX_REFERENCES]


def extract_frame(video: Path, seconds: float, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    run([ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-ss", "%.3f" % seconds, "-i", str(video),
         "-frames:v", "1", "-q:v", "2", str(out)])
    if not out.is_file():
        raise JudgeError("no frame at %.2f s in %s" % (seconds, video))


def judge_video(job: Path, config: Optional[Dict[str, Any]] = None, references: Any = None) -> Dict[str, Any]:
    """Score one mid-shot frame per shot from video.mp4 and record the weakest shots under video_pass in judge.json."""
    job = Path(job)
    data = read_job(job)
    base = load_rubric()
    cfg = resolve_config(data["story"], config, base)
    if cfg["provider"] == "none":
        raise JudgeError("the video pass needs a model; pass --provider gemini or anthropic")
    require_model(cfg)
    video = job / "video.mp4"
    if not video.is_file():
        raise JudgeError("missing %s" % video)
    if "labels" not in cfg:
        cfg["labels"] = "text" if drawtext_available() else "boxes"
    rubric = video_rubric(base)
    refs = _reference_files(references)
    shots_file = read_json(job / "shots.json", required=False) or {}
    offset = float(shots_file.get("content_start", 0.0))
    budget = vision.Budget(cfg["ceiling_usd"])
    frames: List[Dict[str, Any]] = []
    unscored: List[str] = []
    sheets: List[str] = []
    timeline_shots = data["timeline"]["shots"]
    for number, start in enumerate(range(0, len(timeline_shots), SHEET_SIZE), 1):
        group = timeline_shots[start:start + SHEET_SIZE]
        tiles, times = [], {}
        for i, shot in enumerate(group):
            middle = (float(shot["start"]) + float(shot["end"])) / 2.0
            times[shot["id"]] = middle
            frame = job / "judge" / "video" / "frames" / ("%s.jpg" % shot["id"])
            extract_frame(video, offset + middle, frame)
            word = shot_text(shot, data["words"]).split()
            tiles.append({"path": frame, "label": shot["id"], "word": word[0] if word else "", "color": BOX_COLORS[i % len(BOX_COLORS)]})
        sheet = job / "judge" / "video" / ("sheet_%02d.jpg" % number)
        contact_sheet(tiles, sheet, cfg["still_height"], SHEET_COLUMNS, cfg["labels"])
        sheets.append(_rel(job, sheet))
        ids = [t["label"] for t in tiles]
        prompt = video_prompt(rubric["criteria"], data["story"], ids, len(refs))
        try:
            found = _ask_scores(job, cfg, budget, refs + [sheet], prompt, ids, rubric["criteria"], "video_%02d" % number, VIDEO_TOKENS, "shot", "frames")
        except vision.BudgetExceeded:
            unscored += ids
            break
        except JudgeError:
            unscored += ids
            continue
        for shot_id in ids:
            if shot_id not in found:
                unscored.append(shot_id)
                continue
            frames.append({"shot": shot_id, "time": round(times[shot_id], 3), "scores": found[shot_id]["scores"],
                           "total": found[shot_id]["total"], "reason": found[shot_id]["reason"]})
    unscored += [s["id"] for s in timeline_shots[len(sheets) * SHEET_SIZE:]]
    weakest = [f["shot"] for f in sorted(frames, key=lambda f: f["total"])[:WEAKEST_COUNT]]
    video_pass: Dict[str, Any] = {"sheets": sheets, "frames": frames, "weakest": weakest, "references": [_rel(job, r) for r in refs]}
    if unscored:
        video_pass["unscored"] = unscored
    existing = _load_judge(job)
    doc = dict(existing)
    doc.setdefault("version", 1)
    doc.setdefault("config", cfg)
    doc.setdefault("rubric", base)
    doc.setdefault("shots", [])
    doc["video_pass"] = video_pass
    doc["usage"] = _merge_usage(existing.get("usage"), budget.summary())
    _write_judge(job, doc)
    return doc


def video_prompt(criteria: Sequence[Dict[str, Any]], story: Dict[str, Any], ids: Sequence[str], reference_count: int) -> str:
    lines = ["Story genre: %s." % story.get("genre", "story")]
    if story.get("title"):
        lines.append("Title: %s." % story["title"])
    if reference_count:
        lines.append("The first %d image(s) are reference frames: what a finished video in this format looks like. "
                     "Use them as the bar for the quality criterion." % reference_count)
    lines.append("The last image is a contact sheet with one frame per shot, labelled with the shot id in the top-left corner. "
                 "Tiles in reading order, row by row, left to right: %s." % ", ".join(ids))
    lines.append("A dark translucent band on every tile marks where the caption will be drawn. "
                 "Judge variety against the neighbouring tiles on the sheet.")
    lines += ["", "Score every frame from 0 to 10 on each criterion:", _rubric_lines(criteria), "",
              'Reply with JSON only: {"frames": [{"shot": "%s", "scores": {%s}, "reason": "one line"}]}'
              % (ids[0], ", ".join('"%s": 0' % c["key"] for c in criteria))]
    return "\n".join(lines)


def make_stager(job: Path) -> Any:
    """Build the Studio-backed stager for a job."""
    from .render import JudgeStager
    return JudgeStager(Path(job))


def run_shots(job: Path, stager: Any, argv_config: Dict[str, Any]) -> Dict[str, Any]:
    """Run the shots pass from command-line style settings; "shots" in argv_config limits it to those ids."""
    overrides = dict(argv_config)
    shots = overrides.pop("shots", None)
    return judge_shots(job, stager, overrides, shots)


def show(job: Path) -> str:
    doc = _load_judge(Path(job))
    if not doc.get("shots"):
        raise JudgeError("%s has no judge.json with shots; run the shots pass first" % job)
    rows = [("shot", "status", "chosen", "total", "reason")]
    for entry in doc["shots"]:
        chosen = entry.get("chosen") or {}
        total = chosen.get("total")
        rows.append((entry["id"], entry["status"], chosen.get("name", "-"), "-" if total is None else "%.2f" % total, chosen.get("reason", "")))
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row[:4])) + "  " + row[4] for row in rows]
    video_pass = doc.get("video_pass")
    if video_pass:
        lines.append("")
        lines.append("video pass weakest: %s" % ", ".join(video_pass.get("weakest", [])))
    usage = doc.get("usage") or {}
    lines.append("")
    lines.append("usage: %s calls, $%.4f of $%.2f" % (usage.get("calls", 0), usage.get("cost_usd", 0.0), usage.get("ceiling_usd", 0.0)))
    return "\n".join(lines)


def _add_model_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", choices=["gemini", "anthropic", "manual", "none"], help="vision provider (default gemini)")
    parser.add_argument("--model", help="model id; list them with the models command")
    parser.add_argument("--ceiling", type=float, help="spend ceiling in USD (default 2.0)")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.judge", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    models = sub.add_parser("models", help="list the model ids a provider offers")
    models.add_argument("--provider", required=True, choices=["gemini", "anthropic"])
    shots = sub.add_parser("shots", help="choose a camera for every shot")
    shots.add_argument("--job", required=True, type=Path)
    shots.add_argument("--shots", help="comma separated shot ids to (re)judge")
    shots.add_argument("--retries", type=int, help="restage rounds after the first (default 1)")
    shots.add_argument("--max-candidates", type=int, help="candidates per round (default 6)")
    _add_model_flags(shots)
    video = sub.add_parser("video", help="score the rendered video")
    video.add_argument("--job", required=True, type=Path)
    video.add_argument("--references", type=Path, help="folder of up to 4 reference frames")
    _add_model_flags(video)
    sub.add_parser("show", help="print the chosen cameras").add_argument("--job", required=True, type=Path)
    spread = sub.add_parser("variety", help="print the shot-type and move distribution and any broken variety rule")
    spread.add_argument("--job", required=True, type=Path)
    spread.add_argument("--authored", action="store_true", help="use the shot list's own cameras, ignoring judge.json")
    pose = sub.add_parser("posture", help="check every shot for clipping, unnatural posture and characters with nothing to do")
    pose.add_argument("--job", required=True, type=Path)
    pose.add_argument("--shots", help="comma separated shot ids to (re)check")
    pose.add_argument("--no-vision", action="store_true", help="geometry only")
    pose.add_argument("--show", action="store_true", help="print the recorded result without checking again")
    _add_model_flags(pose)
    args = parser.parse_args(argv)
    try:
        if args.command == "models":
            print("\n".join(vision.list_models(args.provider)))
            return 0
        if args.command == "show":
            print(show(args.job))
            return 0
        if args.command == "variety":
            data = read_job(args.job)
            report = variety(data["timeline"]["shots"], None if args.authored else chosen_cameras(args.job),
                             (data["story"].get("judge") or {}).get("variety"), locked_neighbours_only=args.authored)
            print(describe_variety(report))
            for problem in report["problems"]:
                print("problem: " + problem)
            return 1 if report["problems"] else 0
        config = {"provider": args.provider, "model": args.model, "ceiling_usd": args.ceiling}
        if args.command == "posture":
            from . import posture
            if not args.show:
                cfg = resolve_config(read_job(args.job)["story"], config, load_rubric())
                stager = make_stager(args.job)
                try:
                    posture.check_shots(args.job, stager, cfg, [s for s in args.shots.split(",") if s] if args.shots else None, not args.no_vision)
                finally:
                    stager.close()
            record = _load_judge(args.job).get("posture") or {}
            print(posture.table(record))
            return 1 if (record.get("summary") or {}).get("failed") else 0
        if args.command == "video":
            doc = judge_video(args.job, config, args.references)
            print("video pass: %d frames, weakest %s, $%.4f" % (
                len(doc["video_pass"]["frames"]), ", ".join(doc["video_pass"]["weakest"]), doc["usage"]["cost_usd"]))
            return 0
        config.update(retries=args.retries, max_candidates=args.max_candidates)
        config["shots"] = [s for s in args.shots.split(",") if s] if args.shots else None
        stager = make_stager(args.job)
        try:
            doc = run_shots(args.job, stager, config)
        finally:
            close = getattr(stager, "close", None)
            if close:
                close()
        print("judge.json: %d shots, $%.4f of $%.2f" % (len(doc["shots"]), doc["usage"]["cost_usd"], doc["usage"]["ceiling_usd"]))
        return 0
    except vision.Pending as exc:
        print("pending: %s" % exc, file=sys.stderr)
        return 3
    except (RuntimeError, vision.VisionError, EditError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
