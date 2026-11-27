"""Defaults: what the first videos' fix rounds taught, checked on the shot list before Studio is touched.

  python -m pipeline.checks --job J [--json]        exit 0 clean, 1 when a rule fails

Every rule reads only the job folder and the repository (story.json, shotlist.json, words.json, timeline.json,
cast.json, the job's poses.json and sets, the pose and lighting libraries). Errors stop pipeline.make before the
camera judge; warnings are printed and kept in <job>/checks.json.

  names         a shot names a set, anchor, pose or lighting mood that does not exist
  cast          a pool member's shot-list cast entry differs from the cast registry (pipeline.cast)
  no_idle       a visible character has no action: no pose, a resting pose (stand, still) or an idle animation
  face          a visible avatar states no face, so the last shot's face or the blank default carries over
  fear_face     on a scare line a visible character shows no fear (scared, terrified, frozen, uneasy, worried, crying) or smiles
  motion        the line implies action and nobody in the shot moves, or a character is frozen for a long shot
  pose_variety  one pose carries too large a share of a character's shots, or too many shots in a row
  camera_variety  neighbouring locked cameras share type and side, a type exceeds its share, too little camera movement
  time_of_day   a line says morning or night and the shot's light says the opposite, from that line on
  new_pose      a pose defined in the job's poses.json has no action wording; warning until the geometry check has passed it

Rules that need Studio or a picture live elsewhere: floating or duplicated parts, clipping and poses that miss
their target are the geometry stage (pipeline.judge posture); a head or in-world text in the caption band is the
camera judge's geometry, turned into a stop by pipeline.make; whether the action reads, the face fits and the
light shows the time of day in the picture is the quality judges' rubric (pipeline/quality_rubric.json).

story.json "checks" tunes or disables: {"pose_share": 0.25, "pose_run": 2, "still_seconds": 2.0, "off": ["time_of_day"]}.
A shot may carry "allow": ["no_idle"] with "allow_reason": "why" to pass a rule on purpose, "scare": true|false to
say whether it is a scare line, and "daypart": "day"|"night"|"any" to say what time it is from that shot on.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import cast as cast_mod
from . import catalog, judge, story as story_mod

SETTINGS = {"pose_share": 0.25, "pose_min_uses": 3, "pose_run": 2, "still_seconds": 2.0, "off": []}
FEAR = {"scared", "terrified", "frozen", "uneasy", "worried", "crying"}
HAPPY = {"smile", "happy"}
IDLE_ACTIONS = {"idle", "breathe", "sit_idle", "idle_look"}
SCARE_WORDS = {"scream", "screamed", "screaming", "screams", "shriek", "shrieked", "froze", "frozen", "terrified", "terror", "horror", "horrified",
               "blood", "bloody", "dead", "corpse", "scared", "afraid", "panic", "panicked", "trembling", "shaking", "gasped", "run", "ran", "hide", "hid"}
ACTION_WORDS = {"walk", "walked", "walking", "ran", "run", "running", "grab", "grabbed", "open", "opened", "opening", "turn", "turned", "turning",
                "reach", "reached", "reaching", "climb", "climbed", "jump", "jumped", "backed", "pick", "picked", "knock", "knocked", "knocking",
                "drop", "dropped", "slam", "slammed", "pull", "pulled", "push", "pushed", "step", "stepped", "crawl", "crawled", "spun", "lifted",
                "threw", "typed", "dialed", "dialled", "scanned", "pressed", "tapped", "taped", "shrugged", "waved", "pointed", "nodded", "hurried", "rushed", "bolted", "fled"}
DAY_WORDS = {"morning", "sunrise", "dawn", "noon", "afternoon", "daylight", "midday"}
NIGHT_WORDS = {"night", "midnight", "tonight", "evening", "dusk", "nightfall", "overnight"}
LIGHT = {"day": "day", "grey_morning": "day", "interior_golden": "day", "evening": "night", "night_room": "night", "night_hall": "night",
         "night_dread": "night", "interior_night": "night", "interior_dim": "night"}


def light_class(name: Optional[str]) -> str:
    """day, night or neutral for a lighting mood; an unknown mood is read from its name."""
    if not name:
        return "neutral"
    if name in LIGHT:
        return LIGHT[name]
    if "night" in name or "dusk" in name or "dark" in name:
        return "night"
    if "day" in name or "morning" in name or "noon" in name or "sun" in name:
        return "day"
    return "neutral"


def _norm(token: str) -> str:
    return re.sub(r"[^a-z']", "", token.lower().replace("\u2019", "'"))


def _is_avatar(spec: Any) -> bool:
    return isinstance(spec, dict) and spec.get("rig") == "avatar"


def _visible(shot: Dict[str, Any]) -> List[Dict[str, Any]]:
    camera = shot.get("camera") or {}
    hidden = {camera.get("pov")} | set(camera.get("hide") or [] if isinstance(camera.get("hide"), list) else [camera.get("hide")])
    return [actor for actor in shot.get("cast") or [] if actor.get("who") not in hidden and actor.get("visible", True) is not False]


def _pose_names(actor: Dict[str, Any]) -> List[str]:
    pose = actor.get("pose")
    names = [pose] if isinstance(pose, str) else [key.get("pose") for key in pose if isinstance(key, dict)] if isinstance(pose, list) else []
    return [piece for name in names if isinstance(name, str) for piece in name.split("+")]


def _faces(actor: Dict[str, Any]) -> List[str]:
    face = actor.get("face")
    if isinstance(face, str):
        return [face]
    if isinstance(face, list):
        return [str(key[1]) if isinstance(key, (list, tuple)) and len(key) > 1 else str(key.get("face")) if isinstance(key, dict) else str(key) for key in face]
    return []


def _stock(actor: Dict[str, Any]) -> Optional[str]:
    for key in ("anim", "action"):
        if isinstance(actor.get(key), str):
            return actor[key]
    return None


def _alive(actor: Dict[str, Any], poses: Dict[str, Any]) -> bool:
    """Whether a character changes during the shot: walks, changes pose or face, reaches, or holds a pose that moves."""
    if actor.get("to") or (isinstance(actor.get("pose"), list) and len(actor["pose"]) > 1) or (isinstance(actor.get("face"), list) and len(actor["face"]) > 1):
        return True
    if _stock(actor) and _stock(actor) not in IDLE_ACTIONS:
        return True
    if any((poses.get(name) or {}).get("moves") for name in _pose_names(actor)):
        return True
    reach = actor.get("reach")
    if isinstance(reach, dict) and any(isinstance(side, dict) and any(key in side for key in ("from", "by", "release", "gone")) for side in reach.values()):
        return True
    return isinstance(actor.get("look_at"), list) and len(actor["look_at"]) > 1


def _place(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("anchor"), str):
        return value["anchor"]
    return None


def check_job(job: Path) -> Dict[str, Any]:
    """Run every rule on a job folder; returns {"errors": [...], "warnings": [...], "counts": {...}}."""
    job = Path(job)
    loaded = story_mod.load_story(job)
    story, shotlist = loaded["story"], loaded["shotlist"]
    settings = dict(SETTINGS, **(story.get("checks") or {}))
    off = set(settings.get("off") or [])
    found = catalog.collect(job)
    poses, sets = found["poses"], found["sets"]
    cast = shotlist["cast"]
    shots = shotlist["shots"]
    words = story_mod.load_json(job / "words.json")["words"] if (job / "words.json").is_file() else []
    mapped = story_mod.map_shots(story, shotlist)
    timeline = story_mod.load_json(job / "timeline.json") if (job / "timeline.json").is_file() else None
    seconds = {shot["id"]: float(shot["end"]) - float(shot["start"]) for shot in (timeline or {}).get("shots", [])}
    record = story_mod.load_json(job / "cast.json") if (job / "cast.json").is_file() else None
    wrong = {name for name, member in ((record or {}).get("members") or {}).items() if member.get("effect") in ("wrong", "close")}
    job_poses = set(json.loads((job / "poses.json").read_text(encoding="utf-8"))) if (job / "poses.json").is_file() else set()
    measured = {}
    for name in ("posture.json", "preview/posture.json"):
        if (job / name).is_file():
            measured.update((story_mod.load_json(job / name).get("shots") or {}))
    errors, warnings = [], []  # type: List[Dict[str, Any]], List[Dict[str, Any]]

    def report(rule: str, shot: Optional[Dict[str, Any]], message: str, who: Optional[str] = None, warn: bool = False) -> None:
        if rule in off or (shot is not None and rule in (shot.get("allow") or []) and shot.get("allow_reason")):
            return
        entry = {"rule": rule, "shot": shot["id"] if shot else None, "who": who, "message": message}
        (warnings if warn else errors).append(entry)

    if record is not None:
        for problem in cast_mod.check_shotlist(shotlist, record):
            report("cast", None, problem)
    state = None  # type: Optional[str]
    uses = {}  # type: Dict[str, List[List[str]]]
    for index, shot in enumerate(shots):
        text_tokens = [_norm(token) for token in story_mod.tokenize(shot.get("text", ""))]
        info = sets.get(shot["set"])
        if info is None:
            report("names", shot, "set %r does not exist (sets: %s); design it first (REQUEST-set)" % (shot["set"], ", ".join(sorted(sets))))
        mood = shot.get("time") or (shotlist.get("defaults") or {}).get("time") or (info or {}).get("lighting")
        if shot.get("time") and shot["time"] not in found["lighting"]:
            report("names", shot, "lighting mood %r does not exist (moods: %s)" % (shot["time"], ", ".join(found["lighting"])))
        if shot.get("daypart") in ("day", "night"):
            state = shot["daypart"]
        elif shot.get("daypart") == "any":
            state = None
        else:
            said = [word for word in text_tokens if word in DAY_WORDS or word in NIGHT_WORDS]
            if said:
                state = "day" if said[-1] in DAY_WORDS else "night"
        lit = light_class(mood)
        if state == "day" and lit == "night":
            report("time_of_day", shot, "the script has reached daytime but the light is %r (a night mood); use a day mood, or set \"daypart\" on the shot if the line does not mean now" % mood)
        elif state == "night" and lit == "day":
            report("time_of_day", shot, "the script has reached night but the light is %r (a day mood); use a night mood, or set \"daypart\" on the shot" % mood)
        elif state == "day" and lit == "neutral" and not any(prop.get("prop") == "sun_patch" for prop in shot.get("props") or []):
            report("time_of_day", shot, "daytime in the script, light %r shows no time of day; the story judge wants visible daylight (a day mood or a sun_patch prop)" % mood, warn=True)
        visible = _visible(shot)
        speakers = {word.get("speaker") for word in words[mapped[index]["word_start"]:mapped[index]["word_end"] + 1]} if words and "word_start" in mapped[index] else set()
        scare = shot.get("scare") if isinstance(shot.get("scare"), bool) else bool(set(text_tokens) & SCARE_WORDS or speakers & wrong)
        moving = False
        for actor in visible:
            who = actor["who"]
            spec = cast.get(who)
            names = _pose_names(actor)
            for key in ("at", "to"):
                anchor = _place(actor.get(key))
                if info is not None and anchor is not None and anchor not in info["anchors"] and anchor not in cast:
                    report("names", shot, "%s.%s names anchor %r, which set %s does not have" % (who, key, anchor, shot["set"]), who)
            for name in names:
                if name not in poses:
                    report("names", shot, "pose %r does not exist (python -m pipeline.catalog lists the poses)" % name, who)
                elif name in job_poses and (measured.get(shot["id"]) or {}).get("status") != "pass":
                    report("new_pose", shot, "pose %r is new in this job and no geometry check has passed this shot yet; the posture stage must pass it before render" % name, who, warn=True)
            if _is_avatar(spec):
                active = [name for name in names if name in poses and not poses[name]["rest"]]
                stock = _stock(actor)
                if not active and not (stock and stock not in IDLE_ACTIONS):
                    report("no_idle", shot, "%s has no action: %s. Name a pose with an action that fits the line" % (who, "pose %s is a resting pose" % ", ".join(names) if names else "no pose is the idle stand"), who)
                faceless = spec.get("faceless") or spec.get("silhouette")
                faces = _faces(actor)
                if not faceless and not faces:
                    report("face", shot, "%s states no face; say what the face does in every shot" % who, who)
                if scare and not faceless and faces:
                    if set(faces) & HAPPY:
                        report("fear_face", shot, "%s smiles on a scare line" % who, who)
                    elif not set(faces) & FEAR:
                        report("fear_face", shot, "%s shows %s on a scare line; use one of %s, or set \"scare\": false on the shot" % (who, ", ".join(faces), ", ".join(sorted(FEAR))), who)
                if active:
                    uses.setdefault(who, []).append([shot["id"], "+".join(names)])
            else:
                stock = _stock(actor)
                if not stock or stock in IDLE_ACTIONS:
                    report("no_idle", shot, "%s has no action (action or anim is missing or idle)" % who, who)
            alive = _alive(actor, poses)
            moving = moving or alive
            if not alive and seconds.get(shot["id"], 0.0) >= float(settings["still_seconds"]):
                report("motion", shot, "%s does not move for %.1f s; walk, change pose or face during the shot, or use a pose that moves" % (who, seconds[shot["id"]]), who)
        changes = any(isinstance(value, list) for value in (shot.get("set_state") or {}).values())
        if visible and not moving and not changes and set(text_tokens) & ACTION_WORDS:
            report("motion", shot, "the line implies action (%s) and nobody in the shot moves" % ", ".join(sorted(set(text_tokens) & ACTION_WORDS)))
    for who, rows in uses.items():
        counts = {}  # type: Dict[str, int]
        for _, pose in rows:
            counts[pose] = counts.get(pose, 0) + 1
        for pose, count in counts.items():
            if count >= int(settings["pose_min_uses"]) and count / float(len(rows)) > float(settings["pose_share"]):
                report("pose_variety", None, "%s holds pose %s in %d of %d shots (%s); the cap is %.0f%%. Vary the reactions" %
                       (who, pose, count, len(rows), ", ".join(shot_id for shot_id, name in rows if name == pose), 100 * float(settings["pose_share"])), who)
        run = 1
        for before, after in zip(rows, rows[1:]):
            run = run + 1 if before[1] == after[1] else 1
            if run == int(settings["pose_run"]) + 1:
                report("pose_variety", None, "%s holds pose %s for more than %d shots running, up to %s" % (who, after[1], int(settings["pose_run"]), after[0]), who)
    spread = judge.variety((timeline or {}).get("shots") or shots, None, (story.get("judge") or {}).get("variety"), locked_neighbours_only=True)
    for problem in spread["problems"]:
        report("camera_variety", None, problem)
    counts = {}  # type: Dict[str, int]
    for entry in errors:
        counts[entry["rule"]] = counts.get(entry["rule"], 0) + 1
    return {"version": 1, "shots": len(shots), "errors": errors, "warnings": warnings, "counts": counts, "settings": settings}


def describe(result: Dict[str, Any]) -> str:
    lines = []
    for kind in ("errors", "warnings"):
        for entry in result[kind]:
            lines.append("%s %-14s %-6s %s" % ("FAIL" if kind == "errors" else "warn", entry["rule"], entry["shot"] or "-", entry["message"]))
    lines.append("%d shots: %d failing rule checks, %d warnings" % (result["shots"], len(result["errors"]), len(result["warnings"])))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.checks", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = check_job(args.job.expanduser())
    except story_mod.StoryError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, indent=1) if args.json else describe(result))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
