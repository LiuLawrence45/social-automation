"""Load and validate the words.json and shots.json hand-off files."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
TIME_TOLERANCE = 0.05


class HandoffError(ValueError):
    """Raised when a hand-off file breaks its contract."""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise HandoffError(message)


def _load_json(path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise HandoffError("%s not found" % path)
    except json.JSONDecodeError as exc:
        raise HandoffError("%s is not valid JSON: %s" % (path, exc))


def validate_words(data: Any) -> Dict[str, Any]:
    """Check a parsed words.json and return it."""
    _require(isinstance(data, dict), "words.json: top level must be an object")
    _require(data.get("version") == 1, "words.json: version must be 1")
    _require(isinstance(data.get("audio"), str) and data["audio"], "words.json: audio must be a file name")
    _require(_is_number(data.get("duration")) and data["duration"] > 0, "words.json: duration must be a positive number")
    words = data.get("words")
    _require(isinstance(words, list) and words, "words.json: words must be a non-empty list")
    previous_start = 0.0
    for index, word in enumerate(words):
        where = "words.json: words[%d]" % index
        _require(isinstance(word, dict), where + " must be an object")
        _require(word.get("i") == index, where + ".i must equal its position %d" % index)
        for key in ("token", "display"):
            _require(isinstance(word.get(key), str) and word[key].strip(), where + ".%s must be a non-empty string" % key)
        _require("\n" not in word["display"], where + ".display must be a single line")
        _require(_is_number(word.get("start")) and _is_number(word.get("end")), where + " needs numeric start and end")
        _require(word["start"] >= 0, where + ".start must be >= 0")
        _require(word["end"] > word["start"], where + ".end must be after start")
        _require(word["start"] >= previous_start, where + ".start must not go back in time")
        _require(word["end"] <= data["duration"] + TIME_TOLERANCE, where + ".end is past duration")
        if "sentence" in word:
            _require(isinstance(word["sentence"], int) and not isinstance(word["sentence"], bool) and word["sentence"] >= 0,
                     where + ".sentence must be a non-negative integer")
        previous_start = word["start"]
    return data


def validate_shots(data: Any, word_count: Optional[int] = None, video_duration: Optional[float] = None) -> Dict[str, Any]:
    """Check a parsed shots.json: ordered, non-overlapping, inside the video."""
    _require(isinstance(data, dict), "shots.json: top level must be an object")
    _require(data.get("version") == 1, "shots.json: version must be 1")
    _require(_is_number(data.get("fps")) and data["fps"] > 0, "shots.json: fps must be positive")
    _require(data.get("width") == 1080 and data.get("height") == 1920, "shots.json: width and height must be 1080x1920")
    for key in ("content_start", "content_end"):
        _require(_is_number(data.get(key)), "shots.json: %s must be a number" % key)
    _require(data["content_start"] >= 0, "shots.json: content_start must be >= 0")
    _require(data["content_end"] > data["content_start"], "shots.json: content_end must be after content_start")
    shots = data.get("shots")
    _require(isinstance(shots, list) and shots, "shots.json: shots must be a non-empty list")
    limit = video_duration if video_duration is not None else data["content_end"]
    _require(data["content_end"] <= limit + TIME_TOLERANCE, "shots.json: content_end is past the end of video.mp4")
    previous_end = 0.0
    seen = set()
    for index, shot in enumerate(shots):
        where = "shots.json: shots[%d]" % index
        _require(isinstance(shot, dict), where + " must be an object")
        _require(isinstance(shot.get("id"), str) and shot["id"], where + ".id must be a non-empty string")
        _require(shot["id"] not in seen, where + ".id %r is duplicated" % shot["id"])
        seen.add(shot["id"])
        _require(_is_number(shot.get("start")) and _is_number(shot.get("end")), where + " needs numeric start and end")
        _require(shot["end"] > shot["start"], where + ".end must be after start")
        _require(shot["start"] >= previous_end - 1e-6, where + " overlaps or precedes the previous shot")
        _require(shot["start"] >= 0 and shot["end"] <= limit + TIME_TOLERANCE, where + " is outside the video")
        for key in ("word_start", "word_end"):
            if key in shot:
                _require(isinstance(shot[key], int) and not isinstance(shot[key], bool) and shot[key] >= 0, where + ".%s must be a non-negative integer" % key)
                if word_count is not None:
                    _require(shot[key] <= word_count, where + ".%s is past the last word" % key)
        if "word_start" in shot and "word_end" in shot:
            _require(shot["word_end"] >= shot["word_start"], where + ".word_end is before word_start")
        if "set" in shot:
            _require(isinstance(shot["set"], str), where + ".set must be a string")
        if "emphasis" in shot:
            _require(isinstance(shot["emphasis"], bool), where + ".emphasis must be a boolean")
        previous_end = shot["end"]
    return data


def load_job(job: Path, video_duration: Optional[float] = None) -> Dict[str, Any]:
    """Load and validate words.json and shots.json from a job folder."""
    job = Path(job)
    words = validate_words(_load_json(job / "words.json"))
    shots = validate_shots(_load_json(job / "shots.json"), len(words["words"]), video_duration)
    return {"words": words, "shots": shots}
