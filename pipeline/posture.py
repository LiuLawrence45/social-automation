"""Posture and clipping checks: geometry measured in the posed Studio scene, then a vision pass over frames of each shot.

Every shot that shows a character is sampled at several moments. The geometric layer reports body parts
passing through scenery, through the body itself, through another character or a held prop, arms held out
with nothing in hand, the stock splayed stance, joints bent past their limits, floating bodies, hands that
miss what they reach for and characters with nothing to do. The vision layer scores frames of the same
shot for natural posture, visible clipping and whether the action reads. A shot passes only when both do.

    python -m pipeline.judge posture --job JOB [--shots a,b] [--no-vision]
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import vision
from .edit import ffmpeg_bin
from .studio import LIBRARY_DIR, director_hash

SAMPLES = (0.0, 0.25, 0.5, 0.75, 1.0)
MOMENTS = (0.1, 0.5, 0.9)
DEFAULTS = {"threshold": 6.5, "samples": list(SAMPLES), "moments": list(MOMENTS), "vision": True, "still_height": 640, "max_tokens": 1200}
GROUPS = {
    "clip_scenery": "passes through scenery",
    "clip_self": "body passes through itself",
    "clip_other": "passes through another character",
    "clip_prop": "held prop passes through body or scenery",
    "arms_out": "arms held out for no reason",
    "arm_out": "arms held out for no reason",
    "arms_splayed": "stock stance, limbs splayed",
    "stance_wide": "stock stance, limbs splayed",
    "joint_limit": "joint bent past its limit",
    "floating": "floating",
    "hand_off_target": "hand misses what it reaches for",
    "no_action": "standing with nothing to do",
    "prop_missing": "posed to hold something that is not there",
    "vision_posture": "posture looks unnatural (vision)",
    "vision_clipping": "clipping is visible (vision)",
    "vision_action": "action does not read (vision)",
}
CRITERIA = [
    {"key": "posture", "question": "Does every visible character hold itself the way a real person would: limbs relaxed or used on purpose, "
                                   "no arms stuck straight out, no T-pose or splayed stance, no frozen mid-gesture limb, no twisted or over-bent joint? (10 = fully natural)"},
    {"key": "clipping", "question": "Is the frame free of anything passing through anything else: a limb through the torso, a hand through a phone, "
                                    "door, wall, desk or bed, feet through the floor, a prop through a face? (10 = nothing intersects)"},
    {"key": "action", "question": "Is each visible character plainly doing what the line says, or something that fits it, readable at a glance, "
                                  "not just standing there? (10 = unmistakable)"},
]
SYSTEM_PROMPT = (
    "You are an animation supervisor checking character posing in a vertical 9:16 short video that tells a narrated story "
    "with animated characters. Judge only what is visible. Be strict and use the full 0 to 10 range. "
    "Reply with one JSON object and nothing else."
)


class PostureError(RuntimeError):
    """Raised for failures the operator can act on."""


def settings(story: Dict[str, Any]) -> Dict[str, Any]:
    """Defaults, then story.json "judge"."posture" (true or false switch the whole check; a mapping sets values)."""
    merged = dict(DEFAULTS)
    block = (story.get("judge") or {}).get("posture")
    if isinstance(block, dict):
        merged.update(block)
    return merged


def enabled(story: Dict[str, Any]) -> bool:
    block = (story.get("judge") or {}).get("posture")
    return bool(block)


def load_poses() -> Dict[str, Any]:
    from .studio import load_library
    return load_library()["avatar_poses"]


KINDS = {"clip_scenery": "intersect", "clip_self": "intersect", "clip_other": "intersect", "clip_prop": "intersect", "joint_limit": "joint_limit",
         "floating": "floating", "arms_out": "stiff_arms", "arm_out": "stiff_arms", "arms_splayed": "stiff_arms", "stance_wide": "stiff_arms",
         "no_action": "idle", "hand_off_target": "reach_miss", "prop_missing": "prop_missing"}


def describe(issue: Dict[str, Any]) -> str:
    code, detail, depth = issue.get("code"), str(issue.get("detail") or ""), issue.get("depth")
    if str(code).startswith("clip_") and "->" in detail:
        part, other = detail.split("->", 1)
        return "%s inside %s%s" % (part, other, " by %.2f studs" % depth if depth is not None else "")
    if code == "hand_off_target" and depth is not None:
        return "hand %s is %.2f studs from its target" % (detail, depth)
    return detail or GROUPS.get(code, str(code))


def frames_record(samples: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per sampled frame, what the geometric check found on characters in the frame (the shape posture.json publishes)."""
    out = []
    for sample in samples:
        issues = []
        actors = sample.get("actors") if isinstance(sample.get("actors"), dict) else {}
        for who in sorted(actors):
            record = actors[who] if isinstance(actors[who], dict) else {}
            for issue in record.get("issues") if isinstance(record.get("issues"), list) else []:
                kind = KINDS.get(issue.get("code"), str(issue.get("code")))
                if kind == "intersect" and issue.get("code") == "clip_scenery" and "floor" in str(issue.get("detail")).lower():
                    kind = "sunk"
                issues.append({"kind": kind, "actor": who, "detail": describe(issue), "code": issue.get("code"), "in_frame": bool(issue.get("seen"))})
        out.append({"frame": sample.get("frame"), "u": sample.get("u"), "issues": issues})
    return out


def shot_stamp(shot: Dict[str, Any], camera: Any, cast: Any) -> str:
    """Changes when this one shot's staging, timing, camera or the cast does."""
    basis = {"shot": {k: shot.get(k) for k in ("id", "set", "cast", "props", "set_state", "frame_start", "frame_end")}, "camera": camera, "cast": cast}
    return hashlib.sha1(json.dumps(basis, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def file_sha1(path: Path) -> Optional[str]:
    return hashlib.sha1(path.read_bytes()).hexdigest() if path.is_file() else None


def write_public(job: Path, entries: Sequence[Dict[str, Any]], describes: Optional[Dict[str, Any]] = None, stamps: Optional[Dict[str, str]] = None) -> None:
    """posture.json beside the job: the geometric findings per shot and frame, readable without Studio.

    "describes" says which shot list and timeline the file was measured on; a shot whose staging changed since
    it was measured carries "current": false until it is checked again.
    """
    shots = {}
    for entry in entries:
        record = {"status": "fail" if entry.get("status") == "fail" else "pass", "characters": entry.get("status") != "no_character",
                  "reasons": entry.get("reasons", []), "frames": entry.get("frames", [])}
        if stamps is not None:
            record["current"] = entry.get("stamp") == stamps.get(entry["id"])
            record["checked"] = entry.get("checked")
        if entry.get("vision"):
            record["vision"] = {"lowest": entry["vision"].get("lowest"), "sheet": entry["vision"].get("sheet")}
        shots[entry["id"]] = record
    target = Path(job) / "posture.json"
    partial = target.with_suffix(".json.part")
    with open(partial, "w", encoding="utf-8") as handle:
        json.dump(dict({"version": 1}, **({"describes": describes} if describes else {}), shots=shots), handle, indent=1)
    partial.replace(target)


def pose_names(actor: Dict[str, Any]) -> List[str]:
    pose = actor.get("pose")
    names = [pose] if isinstance(pose, str) else [key.get("pose") for key in pose if isinstance(key, dict)] if isinstance(pose, list) else []
    return [piece for name in names if isinstance(name, str) for piece in name.split("+")]


def cast_actions(shot: Dict[str, Any], poses: Dict[str, Any]) -> List[Dict[str, str]]:
    """What each cast member is meant to be doing, from the pose library's "action" wording."""
    out = []
    for actor in shot.get("cast") or []:
        said = []
        for name in pose_names(actor):
            entry = poses.get(name)
            action = entry.get("action") if isinstance(entry, dict) and not entry.get("rest") else None
            if action and action not in said:
                said.append(action)
        if actor.get("to") and not said:
            said.append("walking")
        out.append({"who": actor["who"], "action": ", then ".join(said) if said else "none stated"})
    return out


def fold(samples: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One finding per character, code and detail across the sampled moments, with where it was seen."""
    found: Dict[Any, Dict[str, Any]] = {}
    for sample in samples:
        actors = sample.get("actors")
        if not isinstance(actors, dict):
            continue
        for who, record in actors.items():
            issues = record.get("issues") if isinstance(record, dict) else None
            for issue in issues if isinstance(issues, list) else []:
                key = (who, issue.get("code"), issue.get("detail"))
                entry = found.setdefault(key, {"who": who, "code": issue.get("code"), "detail": issue.get("detail"), "seen": False, "at": [], "depth": None})
                entry["at"].append(sample.get("u"))
                entry["seen"] = entry["seen"] or bool(issue.get("seen"))
                if issue.get("depth") is not None:
                    entry["depth"] = max(entry["depth"] or 0.0, float(issue["depth"]))
    return sorted(found.values(), key=lambda e: (not e["seen"], e["who"], e["code"] or "", e["detail"] or ""))


def characters_seen(samples: Sequence[Dict[str, Any]]) -> bool:
    for sample in samples:
        actors = sample.get("actors")
        for record in (actors.values() if isinstance(actors, dict) else []):
            if (record.get("metrics") or {}).get("seen"):
                return True
    return False


def moments_sheet(paths: Sequence[Path], out: Path) -> None:
    """Join frames of one shot left to right in time order."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error"]
    for path in paths:
        cmd += ["-i", str(path)]
    graph = "%shstack=inputs=%d,format=yuvj420p[out]" % ("".join("[%d:v]" % i for i in range(len(paths))), len(paths)) if len(paths) > 1 else "[0:v]format=yuvj420p[out]"
    subprocess.run(cmd + ["-filter_complex", graph, "-map", "[out]", "-frames:v", "1", "-q:v", "3", str(out)], check=True)


def prompt(story: Dict[str, Any], text: str, actions: Sequence[Dict[str, str]], count: int) -> str:
    lines = ["Story genre: %s." % story.get("genre", "story")]
    if text:
        lines.append('The narrator says during this shot: "%s"' % text)
    else:
        lines.append("This shot is silent: it holds on the picture after the last line.")
    lines.append("The image shows %d frames of this one shot, left to right in time order." % count)
    lines.append("Characters in the scene and what each is meant to be doing:")
    for item in actions:
        lines.append("- %s: %s" % (item["who"], item["action"]))
    lines.append("A dark featureless figure with glowing eyes is the story's monster; its odd head angle is intended, the rest of its body still has to read as a body.")
    lines += ["", "Score each frame from 0 to 10:"]
    lines += ["- %s: %s" % (c["key"], c["question"]) for c in CRITERIA]
    lines += ["If no character is visible in a frame, give it 10 on all three and say so.", "",
              'Reply with JSON only: {"frames": [{"index": 1, "scores": {"posture": 0, "clipping": 0, "action": 0}, "problems": ["short phrase"]}], '
              '"verdict": "one sentence on the worst problem, or \\"clean\\""}']
    return "\n".join(lines)


def parse(data: Optional[Dict[str, Any]], count: int) -> Optional[Dict[str, Any]]:
    """Per-frame scores from a reply; None when no frame carries usable scores."""
    items = (data or {}).get("frames")
    if not isinstance(items, list):
        return None
    frames = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("scores"), dict):
            continue
        scores = {}
        for criterion in CRITERIA:
            try:
                scores[criterion["key"]] = min(10.0, max(0.0, float(item["scores"].get(criterion["key"]))))
            except (TypeError, ValueError):
                scores = {}
                break
        if scores:
            problems = [str(p) for p in item.get("problems", []) if p] if isinstance(item.get("problems"), list) else []
            frames.append({"index": item.get("index"), "scores": scores, "problems": problems})
    if not frames:
        return None
    lowest = {c["key"]: min(f["scores"][c["key"]] for f in frames) for c in CRITERIA}
    return {"frames": frames[:count], "lowest": lowest, "verdict": str((data or {}).get("verdict", ""))}


def key_for(timeline: Dict[str, Any], cameras: Dict[str, Dict[str, Any]]) -> str:
    """Changes whenever staging, timing, a chosen camera or the director does, so a stale record is not trusted."""
    basis = {"cast": timeline.get("cast"), "director": director_hash(),
             "shots": [{k: shot.get(k) for k in ("id", "set", "cast", "props", "set_state", "frame_start", "frame_end")} for shot in timeline["shots"]],
             "cameras": {shot["id"]: cameras.get(shot["id"], shot.get("camera")) for shot in timeline["shots"]}}
    return hashlib.sha1(json.dumps(basis, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def summarise(entries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    groups: Dict[str, List[str]] = {}
    failed, with_characters = [], 0
    for entry in entries:
        if entry["status"] != "no_character":
            with_characters += 1
        if entry["status"] != "fail":
            continue
        failed.append(entry["id"])
        for reason in entry["reasons"]:
            groups.setdefault(GROUPS.get(reason, reason), [])
            if entry["id"] not in groups[GROUPS.get(reason, reason)]:
                groups[GROUPS.get(reason, reason)].append(entry["id"])
    return {"shots": len(entries), "with_characters": with_characters, "failed": len(failed), "failed_ids": failed, "groups": groups}


def check_shots(job: Path, stager: Any, cfg: Dict[str, Any], shots: Optional[Sequence[str]] = None, use_vision: bool = True) -> Dict[str, Any]:
    """Check every shot (or the listed ones) and write the result under "posture" in judge.json.

    cfg is the judge configuration (provider, model, prices, ceiling_usd); stager offers posture() and moments().
    """
    from . import judge
    job = Path(job)
    data = judge.read_job(job)
    timeline, story = data["timeline"], data["story"]
    opts = settings(story)
    use_vision = use_vision and bool(opts["vision"]) and cfg.get("provider") != "none"
    if use_vision:
        judge.require_model(cfg)
    from .studio import use_job
    use_job(job)
    poses = load_poses()
    cameras = judge.chosen_cameras(job)
    known = [shot["id"] for shot in timeline["shots"]]
    if shots is not None:
        missing = [s for s in shots if s not in known]
        if missing:
            raise PostureError("unknown shot ids: %s" % ", ".join(missing))
    doc = judge._load_judge(job)
    record = doc.get("posture") if isinstance(doc.get("posture"), dict) else {}
    key = key_for(timeline, cameras)
    entries = {e["id"]: e for e in record.get("shots", []) if isinstance(e, dict) and "id" in e} if shots is not None else {}
    budget = vision.Budget(cfg["ceiling_usd"])
    stopped = False
    stamps = {shot["id"]: shot_stamp(shot, cameras.get(shot["id"], shot.get("camera")), timeline.get("cast")) for shot in timeline["shots"]}

    def save() -> None:
        ordered = [entries[i] for i in known if i in entries]
        usage = judge._merge_usage(record.get("usage"), budget.summary())
        doc["posture"] = {"version": 1, "key": key, "threshold": opts["threshold"], "samples": opts["samples"], "moments": opts["moments"],
                          "vision": use_vision, "model": cfg.get("model") if use_vision else None, "shots": ordered,
                          "summary": summarise(ordered), "usage": usage}
        judge._write_judge(job, doc)
        describes = {"job": job.name, "label": story.get("label") if isinstance(story.get("label"), str) else None, "key": key,
                     "shotlist_sha1": file_sha1(job / "shotlist.json"), "timeline_sha1": file_sha1(job / "timeline.json"),
                     "written": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "stale_shots": [i for i in known if i in entries and entries[i].get("stamp") != stamps[i]]}
        write_public(job, ordered, describes, stamps)

    for shot in timeline["shots"]:
        shot_id = shot["id"]
        if shots is not None and shot_id not in shots:
            continue
        camera = cameras.get(shot_id, shot.get("camera"))
        text = judge.shot_text(shot, data["words"])
        entry: Dict[str, Any] = {"id": shot_id, "text": text, "status": "pass", "reasons": [], "cast": cast_actions(shot, poses), "geometry": [], "vision": None,
                                 "stamp": stamps[shot_id], "checked": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        if not shot.get("cast"):
            entry["status"] = "no_character"
        else:
            measured = stager.posture(shot_id, camera, opts["samples"])
            samples = measured.get("samples") if isinstance(measured, dict) else []
            samples = samples if isinstance(samples, list) else []
            entry["geometry"] = fold(samples)
            entry["frames"] = frames_record(samples)
            for finding in entry["geometry"]:
                if finding["seen"] and finding["code"] not in entry["reasons"]:
                    entry["reasons"].append(finding["code"])
            if not characters_seen(samples):
                entry["status"] = "no_character"
                entry["reasons"] = []
            elif use_vision and not stopped:
                paths = stager.moments(shot_id, camera, opts["moments"], job / "judge" / "posture" / shot_id, opts["still_height"])
                sheet = job / "judge" / "posture" / ("%s.jpg" % shot_id)
                moments_sheet(paths, sheet)
                question = prompt(story, text, entry["cast"], len(paths))
                scored = None
                try:
                    for attempt in range(3):
                        budget.check()
                        result = vision.ask(cfg["provider"], cfg["model"], SYSTEM_PROMPT, question, [sheet], max_output_tokens=opts["max_tokens"],
                                            prices=cfg.get("prices"), manual_dir=job / "judge" / "manual", manual_id="posture_%s_%d" % (shot_id, attempt))
                        budget.add(result)
                        (sheet.with_suffix(".txt")).write_text(result["text"], encoding="utf-8")
                        scored = parse(result["json"], len(paths))
                        if scored:
                            break
                except vision.BudgetExceeded:
                    stopped = True
                if scored:
                    scored["sheet"] = judge._rel(job, sheet)
                    entry["vision"] = scored
                    for criterion in CRITERIA:
                        if scored["lowest"][criterion["key"]] < opts["threshold"]:
                            entry["reasons"].append("vision_" + criterion["key"])
                elif not stopped:
                    raise PostureError("the model returned no usable posture scores for %s" % shot_id)
            if entry["reasons"]:
                entry["status"] = "fail"
        entries[shot_id] = entry
        note = ", ".join(entry["reasons"]) if entry["reasons"] else ""
        lowest = (entry["vision"] or {}).get("lowest")
        seen = "" if not lowest else "  vision %s" % "/".join("%.0f" % lowest[c["key"]] for c in CRITERIA)
        print("%s %-12s %s%s" % (shot_id, entry["status"], note, seen), flush=True)
        save()
    save()
    return doc


def require_clean(job: Path, timeline: Dict[str, Any], story: Dict[str, Any]) -> None:
    """Refuse a render while any shot fails the posture check or the record is missing or out of date."""
    if not enabled(story):
        return
    from . import judge
    record = judge._load_judge(Path(job)).get("posture")
    if not isinstance(record, dict) or not record.get("shots"):
        raise PostureError("no posture check on record; run python -m pipeline.judge posture --job %s" % job)
    if record.get("key") != key_for(timeline, judge.chosen_cameras(Path(job))):
        raise PostureError("staging, cameras or the director changed since the posture check; run python -m pipeline.judge posture --job %s" % job)
    checked = {entry["id"] for entry in record["shots"]}
    missing = [shot["id"] for shot in timeline["shots"] if shot["id"] not in checked]
    if missing:
        raise PostureError("shots without a posture check: %s" % ", ".join(missing))
    failed = (record.get("summary") or {}).get("failed_ids") or []
    if failed:
        raise PostureError("shots failing the posture check: %s (re-pose them and run the check again, or pass --skip-posture)" % ", ".join(failed))


def table(record: Dict[str, Any]) -> str:
    summary = record.get("summary") or {}
    lines = ["%d shots, %d with characters, %d failing" % (summary.get("shots", 0), summary.get("with_characters", 0), summary.get("failed", 0))]
    for group, ids in sorted((summary.get("groups") or {}).items(), key=lambda item: -len(item[1])):
        lines.append("%2d  %-44s %s" % (len(ids), group, " ".join(ids)))
    usage = record.get("usage") or {}
    lines.append("vision: %s calls, $%.4f" % (usage.get("calls", 0), usage.get("cost_usd", 0.0)))
    return "\n".join(lines)
