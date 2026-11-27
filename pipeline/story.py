"""Story stage: load a story and its shot list, map shots to words, build the director timeline.

Usage:
  python -m pipeline.story check --job <folder>
  python -m pipeline.story timeline --job <folder> [--fps 30] [--tail 0.1]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import captions

ABBREVIATIONS = {"mr.", "mrs.", "ms.", "dr.", "st.", "jr.", "sr."}
SENTENCE_END = re.compile(r"[.!?][\"')\]]*$")
MIN_SHOT_SECONDS = 0.3


class StoryError(ValueError):
    """Raised when story.json or shotlist.json breaks its contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise StoryError(message)


def load_json(path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise StoryError("%s not found" % path)
    except json.JSONDecodeError as exc:
        raise StoryError("%s is not valid JSON: %s" % (path, exc))


def tokenize(script: str) -> List[str]:
    """Split a narration script into spoken tokens (whitespace separated, punctuation kept)."""
    return script.split()


def sentence_ids(tokens: List[str]) -> List[int]:
    """Give every token the index of the sentence it belongs to."""
    ids = []
    current = 0
    for token in tokens:
        ids.append(current)
        if SENTENCE_END.search(token) and token.lower() not in ABBREVIATIONS:
            current += 1
    return ids


def story_lines(story: Dict[str, Any]) -> List[Dict[str, str]]:
    """The script as (speaker, text) lines; a story without lines is one narrator line."""
    return story.get("lines") or [{"speaker": "narrator", "text": story["script"]}]


def display_words(story: Dict[str, Any]) -> List[str]:
    """Caption text for every token, honouring story.display_overrides."""
    tokens = tokenize(story["script"])
    overrides = story.get("display_overrides", {})
    names = story.get("names", [])
    shown = []
    for index, token in enumerate(tokens):
        if "#%d" % index in overrides:
            text = overrides["#%d" % index]
        elif token in overrides:
            text = overrides[token]
        else:
            text = captions.display_text(token, names)
        _require(bool(text.strip()), "story.json: token %d %r has an empty caption; add a display_overrides entry" % (index, token))
        shown.append(text)
    return shown


def validate_story(story: Any) -> Dict[str, Any]:
    _require(isinstance(story, dict), "story.json: top level must be an object")
    _require(story.get("version") == 1, "story.json: version must be 1")
    lines = story.get("lines")
    if lines is not None:
        _require(isinstance(lines, list) and lines, "story.json: lines must be a non-empty list")
        for index, line in enumerate(lines):
            where = "story.json: lines[%d]" % index
            _require(isinstance(line, dict), where + " must be an object")
            _require(isinstance(line.get("speaker"), str) and line["speaker"].strip(), where + ".speaker must be a non-empty string")
            _require(isinstance(line.get("text"), str) and line["text"].strip(), where + ".text must be a non-empty string")
        story["script"] = " ".join(line["text"] for line in lines)
    for key in ("slug", "title", "script"):
        _require(isinstance(story.get(key), str) and story[key].strip(), "story.json: %s must be a non-empty string" % key)
    _require(re.match(r"^[a-z0-9][a-z0-9-]*$", story["slug"]) is not None, "story.json: slug must be lowercase letters, digits and dashes")
    source = story.get("source")
    _require(isinstance(source, dict) and isinstance(source.get("kind"), str), "story.json: source must be an object with a kind")
    _require(isinstance(story.get("names", []), list), "story.json: names must be a list")
    _require(isinstance(story.get("display_overrides", {}), dict), "story.json: display_overrides must be an object")
    _require(isinstance(story.get("voice", {}), dict), "story.json: voice must be an object")
    _require(isinstance(story.get("voice", {}).get("speakers", {}), dict), "story.json: voice.speakers must be an object")
    _require(len(tokenize(story["script"])) > 0, "story.json: script has no words")
    display_words(story)
    return story


def _norm(token: str) -> str:
    return re.sub(r"[^a-z0-9']", "", token.lower().replace("\u2019", "'"))


def map_shots(story: Dict[str, Any], shotlist: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Give every shot its inclusive word range by consuming the script in order."""
    tokens = tokenize(story["script"])
    cursor = 0
    mapped = []
    for index, shot in enumerate(shotlist["shots"]):
        where = "shotlist.json: shots[%d] (%s)" % (index, shot.get("id"))
        if "hold" in shot:
            _require(cursor == len(tokens), where + " is a silent shot (hold) but the script still has words after it; silent shots go after the last word")
            mapped.append(dict(shot))
            continue
        words = tokenize(shot["text"])
        _require(len(words) > 0, where + ".text has no words")
        for offset, word in enumerate(words):
            _require(cursor + offset < len(tokens), where + ".text runs past the end of the script")
            expected = tokens[cursor + offset]
            _require(_norm(word) == _norm(expected),
                     where + ".text word %r does not match script word %d %r" % (word, cursor + offset, expected))
        entry = dict(shot)
        entry["word_start"] = cursor
        entry["word_end"] = cursor + len(words) - 1
        mapped.append(entry)
        cursor += len(words)
    _require(cursor == len(tokens), "shotlist.json: shots cover %d of %d script words; the next uncovered word is %r"
             % (cursor, len(tokens), tokens[cursor] if cursor < len(tokens) else ""))
    return mapped


def validate_shotlist(shotlist: Any, story: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    _require(isinstance(shotlist, dict), "shotlist.json: top level must be an object")
    _require(shotlist.get("version") == 1, "shotlist.json: version must be 1")
    cast = shotlist.get("cast")
    _require(isinstance(cast, dict) and cast, "shotlist.json: cast must be a non-empty object of role -> character")
    for role, spec in cast.items():
        _require(isinstance(spec, (str, dict)), "shotlist.json: cast.%s must be a character name or an object" % role)
    shots = shotlist.get("shots")
    _require(isinstance(shots, list) and shots, "shotlist.json: shots must be a non-empty list")
    seen = set()
    for index, shot in enumerate(shots):
        where = "shotlist.json: shots[%d]" % index
        _require(isinstance(shot, dict), where + " must be an object")
        _require(isinstance(shot.get("id"), str) and shot["id"], where + ".id must be a non-empty string")
        _require(shot["id"] not in seen, where + ".id %r is duplicated" % shot["id"])
        seen.add(shot["id"])
        if "hold" in shot:
            hold = shot["hold"]
            _require(isinstance(hold, (int, float)) and not isinstance(hold, bool) and hold > 0, where + ".hold must be a positive number of seconds")
            _require(not str(shot.get("text", "")).strip(), where + " has both text and hold; a silent shot carries no words")
            _require(index > 0, where + " is a silent shot; the first shot must carry words")
        else:
            _require(isinstance(shot.get("text"), str) and shot["text"].strip(), where + ".text must be the words this shot covers, or give the shot a hold in seconds")
        _require(isinstance(shot.get("set"), str) and shot["set"], where + ".set must name a set")
        _require(isinstance(shot.get("camera"), dict), where + ".camera must be an object")
        for actor_index, actor in enumerate(shot.get("cast", [])):
            _require(isinstance(actor, dict) and actor.get("who") in cast,
                     where + ".cast[%d].who must be one of %s" % (actor_index, sorted(cast)))
            _require("at" in actor, where + ".cast[%d] needs an 'at' position" % actor_index)
        for prop_index, prop in enumerate(shot.get("props", [])):
            _require(isinstance(prop, dict) and isinstance(prop.get("prop"), str), where + ".props[%d].prop must name a prop" % prop_index)
        if "emphasis" in shot:
            _require(isinstance(shot["emphasis"], bool), where + ".emphasis must be a boolean")
    if story is not None:
        map_shots(story, shotlist)
    return shotlist


def build_timeline(story: Dict[str, Any], shotlist: Dict[str, Any], words: Dict[str, Any], fps: int = 30, tail: float = 0.1) -> Dict[str, Any]:
    """Turn the shot list into frame ranges driven by the measured word timings."""
    tokens = tokenize(story["script"])
    timed = words["words"]
    _require(len(timed) == len(tokens), "words.json has %d words but the script has %d" % (len(timed), len(tokens)))
    narration_frames = int(math.ceil((words["duration"] + tail) * fps - 1e-6))
    mapped = map_shots(story, shotlist)
    starts = []
    total_frames = narration_frames
    for index, shot in enumerate(mapped):
        if "hold" in shot:
            frame = total_frames
            total_frames += max(1, int(round(shot["hold"] * fps)))
        else:
            frame = 0 if index == 0 else int(round(timed[shot["word_start"]]["start"] * fps))
            if starts:
                frame = max(frame, starts[-1] + 1)
            _require(frame < narration_frames, "the last worded shot starts after the end of the narration")
        starts.append(frame)
    shots = []
    warnings = []
    for index, shot in enumerate(mapped):
        first = starts[index]
        last = starts[index + 1] if index + 1 < len(mapped) else total_frames
        entry = dict(shot)
        entry.update({"index": index, "frame_start": first, "frame_end": last,
                      "start": round(first / fps, 6), "end": round(last / fps, 6)})
        if (last - first) / fps < MIN_SHOT_SECONDS:
            warnings.append("%s lasts %.2f s" % (shot["id"], (last - first) / fps))
        shots.append(entry)
    return {
        "version": 1,
        "slug": story["slug"],
        "fps": fps,
        "width": 1080,
        "height": 1920,
        "frames": total_frames,
        "duration": round(total_frames / fps, 6),
        "narration_duration": words["duration"],
        "tail": round((total_frames - narration_frames) / fps, 6),
        "cast": shotlist["cast"],
        "defaults": shotlist.get("defaults", {}),
        "shots": shots,
        "warnings": warnings,
    }


def shots_handoff(timeline: Dict[str, Any]) -> Dict[str, Any]:
    """The shots.json the edit stage reads, from a rendered timeline."""
    return {
        "version": 1,
        "fps": timeline["fps"],
        "width": timeline["width"],
        "height": timeline["height"],
        "content_start": 0.0,
        "content_end": timeline["duration"],
        "shots": [
            dict({"id": shot["id"], "start": shot["start"], "end": shot["end"]},
                 **({"word_start": shot["word_start"], "word_end": shot["word_end"]} if "word_start" in shot else {}),
                 set=shot["set"], emphasis=bool(shot.get("emphasis", False)))
            for shot in timeline["shots"]
        ],
    }


def load_story(job: Path) -> Dict[str, Any]:
    story = validate_story(load_json(Path(job) / "story.json"))
    shotlist = validate_shotlist(load_json(Path(job) / "shotlist.json"), story)
    return {"story": story, "shotlist": shotlist}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.story", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "timeline"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--job", required=True)
        if name == "timeline":
            cmd.add_argument("--fps", type=int, default=30)
            cmd.add_argument("--tail", type=float, default=0.1)
    args = parser.parse_args(argv)
    job = Path(args.job)
    try:
        loaded = load_story(job)
        story, shotlist = loaded["story"], loaded["shotlist"]
        tokens = tokenize(story["script"])
        if args.command == "check":
            mapped = map_shots(story, shotlist)
            sets = sorted({shot["set"] for shot in mapped})
            print("ok: %d words, %d sentences, %d shots, %d sets (%s), %.2f words per shot"
                  % (len(tokens), sentence_ids(tokens)[-1] + 1, len(mapped), len(sets), ", ".join(sets), len(tokens) / len(mapped)))
            return 0
        words = load_json(job / "words.json")
        timeline = build_timeline(story, shotlist, words, args.fps, args.tail)
        timeline["built_from"] = {"shotlist_sha1": hashlib.sha1((job / "shotlist.json").read_bytes()).hexdigest()}
        with open(job / "timeline.json", "w", encoding="utf-8") as handle:
            json.dump(timeline, handle, indent=1)
        lengths = [shot["end"] - shot["start"] for shot in timeline["shots"]]
        print("timeline.json: %d shots over %.2f s at %d fps (mean %.2f s, shortest %.2f s, longest %.2f s)"
              % (len(lengths), timeline["duration"], args.fps, sum(lengths) / len(lengths), min(lengths), max(lengths)))
        for warning in timeline["warnings"]:
            print("warning: " + warning)
        return 0
    except StoryError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
