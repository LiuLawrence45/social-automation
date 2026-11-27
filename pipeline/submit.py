"""Hand re-authored shots from a fixer's own job folder back to the main job, safely with several fixers at once.

  python -m pipeline.submit copy  --job <main job> --to <new folder>             start a working copy
  python -m pipeline.submit send  --job <working copy> --to <main job> --group NAME --shots a,b [--note TEXT]
  python -m pipeline.submit list  --job <main job>
  python -m pipeline.submit merge --job <main job> [--only NAME] [--dry-run]     (the owner of the main job only)

A fixer never writes into the main job's files. send copies the named shots (taken from the working copy's
shotlist.json), the copy's poses.json and its sets/<set>.luau into a new folder under <main job>/submissions/,
written elsewhere first and moved into place whole (a set file the copy did not change is left out). merge applies pending submissions in order: each shot
replaces the shot of the same id (its id, set and words may not change), poses are added to <main job>/poses.json,
set files to <main job>/sets/. <main job>/submissions/groups.json says which group may send which shots and
which set files; a pose may not redefine one that another group or the library already defines differently.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import studio

COPIED = ["story.json", "shotlist.json", "timeline.json", "words.json", "judge.json", "poses.json"]
FIXED = ["id", "set", "text", "hold"]


class SubmitError(RuntimeError):
    """Raised for problems the sender or the owner can act on."""


def read(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        if default is not None:
            return default
        raise SubmitError("%s not found" % path)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write(path: Path, data: Any) -> None:
    partial = path.with_name(path.name + ".part")
    with open(partial, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1)
    partial.replace(path)


def command_copy(args: argparse.Namespace) -> Dict[str, Any]:
    source, target = args.job.expanduser().resolve(), args.to.expanduser().resolve()
    if target.exists():
        raise SubmitError("%s already exists" % target)
    target.mkdir(parents=True)
    copied = []
    for name in COPIED:
        if (source / name).is_file():
            shutil.copy2(source / name, target / name)
            copied.append(name)
    if (source / "sets").is_dir():
        shutil.copytree(source / "sets", target / "sets")
        copied.append("sets/")
    return {"copied": copied, "to": str(target)}


def groups(main: Path) -> Dict[str, Any]:
    return read(main / "submissions" / "groups.json", {})


def command_send(args: argparse.Namespace) -> Dict[str, Any]:
    source, main = args.job.expanduser().resolve(), args.to.expanduser().resolve()
    ids = [piece.strip() for piece in args.shots.split(",") if piece.strip()]
    mine = {shot["id"]: shot for shot in read(source / "shotlist.json")["shots"]}
    theirs = {shot["id"]: shot for shot in read(main / "shotlist.json")["shots"]}
    allowed = groups(main).get(args.group)
    problems = []
    for shot_id in ids:
        if shot_id not in mine or shot_id not in theirs:
            problems.append("%s is not a shot of both folders" % shot_id)
            continue
        for key in FIXED:
            if mine[shot_id].get(key) != theirs[shot_id].get(key):
                problems.append("%s changes '%s', which is tied to the narration and may not change" % (shot_id, key))
        if allowed is not None and shot_id not in allowed.get("shots", []):
            problems.append("%s is not one of group %s's shots" % (shot_id, args.group))
    sets = []
    for path in sorted((source / "sets").glob("*.luau")) if (source / "sets").is_dir() else []:
        current = main / "sets" / path.name
        if not current.is_file():
            current = studio.DIRECTOR_DIR / "sets" / path.name
        if current.is_file() and current.read_bytes() == path.read_bytes():
            continue
        sets.append(path)
        if allowed is not None and path.stem not in allowed.get("sets", []):
            problems.append("set file %s does not belong to group %s" % (path.name, args.group))
    if problems:
        raise SubmitError("; ".join(problems))
    name = "%s-%s" % (time.strftime("%Y%m%dT%H%M%S"), args.group)
    folder = main / "submissions"
    folder.mkdir(parents=True, exist_ok=True)
    staging = folder / (".part-%s" % uuid.uuid4().hex[:8])
    staging.mkdir()
    write(staging / "shots.json", [mine[shot_id] for shot_id in ids])
    if (source / "poses.json").is_file():
        shutil.copy2(source / "poses.json", staging / "poses.json")
    if sets:
        (staging / "sets").mkdir()
        for path in sets:
            shutil.copy2(path, staging / "sets" / path.name)
    write(staging / "meta.json", {"group": args.group, "shots": ids, "from": str(source), "note": args.note or "", "sent": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    while (folder / name).exists():
        name += "x"
    os.rename(staging, folder / name)
    return {"submission": str(folder / name), "shots": ids, "sets": [path.name for path in sets]}


def pending(main: Path, only: Optional[str] = None) -> List[Path]:
    folder = main / "submissions"
    found = [path for path in sorted(folder.iterdir()) if path.is_dir() and not path.name.startswith(".") and (path / "meta.json").is_file()] if folder.is_dir() else []
    return [path for path in found if not (path / "merged.json").exists() and (only is None or path.name == only)]


def command_list(args: argparse.Namespace) -> Dict[str, Any]:
    main = args.job.expanduser().resolve()
    folder = main / "submissions"
    out = []
    for path in sorted(folder.iterdir()) if folder.is_dir() else []:
        if path.is_dir() and (path / "meta.json").is_file():
            meta = read(path / "meta.json")
            out.append({"name": path.name, "group": meta.get("group"), "shots": meta.get("shots"), "note": meta.get("note"), "merged": (path / "merged.json").exists()})
    return {"submissions": out}


def command_merge(args: argparse.Namespace) -> Dict[str, Any]:
    main = args.job.expanduser().resolve()
    shotlist = read(main / "shotlist.json")
    position = {shot["id"]: index for index, shot in enumerate(shotlist["shots"])}
    poses = read(main / "poses.json", {})
    library = read(studio.LIBRARY_DIR / "avatar_poses.json")
    owners = read(main / "submissions" / "pose_owners.json", {})
    report = {"merged": [], "changed": [], "camera_changed": [], "staging_changed": [], "sets": [], "poses": []}  # type: Dict[str, List[Any]]
    for path in pending(main, args.only):
        meta = read(path / "meta.json")
        group = meta.get("group")
        allowed = groups(main).get(group)
        incoming = read(path / "poses.json", {})
        for name, pose in incoming.items():
            if name.startswith("_"):
                continue
            if name in library and library[name] != pose:
                raise SubmitError("%s: pose '%s' redefines a library pose; give it a new name" % (path.name, name))
            if name in poses and poses[name] != pose and owners.get(name) not in (None, group):
                raise SubmitError("%s: pose '%s' is already defined differently by group %s" % (path.name, name, owners.get(name)))
        for shot in read(path / "shots.json"):
            if shot["id"] not in position:
                raise SubmitError("%s: unknown shot %s" % (path.name, shot["id"]))
            current = shotlist["shots"][position[shot["id"]]]
            for key in FIXED:
                if shot.get(key) != current.get(key):
                    raise SubmitError("%s: shot %s changes '%s'" % (path.name, shot["id"], key))
            if allowed is not None and shot["id"] not in allowed.get("shots", []):
                raise SubmitError("%s: shot %s is not one of group %s's shots" % (path.name, shot["id"], group))
            if shot != current:
                report["changed"].append(shot["id"])
                if shot.get("camera") != current.get("camera"):
                    report["camera_changed"].append(shot["id"])
                if {k: v for k, v in shot.items() if k != "camera"} != {k: v for k, v in current.items() if k != "camera"}:
                    report["staging_changed"].append(shot["id"])
            shotlist["shots"][position[shot["id"]]] = shot
        for name, pose in incoming.items():
            if not name.startswith("_") and name not in library and poses.get(name) != pose:
                poses[name] = pose
                owners[name] = group
                report["poses"].append(name)
        for source in sorted((path / "sets").glob("*.luau")) if (path / "sets").is_dir() else []:
            if allowed is not None and source.stem not in allowed.get("sets", []):
                raise SubmitError("%s: set file %s does not belong to group %s" % (path.name, source.name, group))
            if not args.dry_run:
                (main / "sets").mkdir(exist_ok=True)
                shutil.copy2(source, main / "sets" / source.name)
            report["sets"].append(source.stem)
        report["merged"].append(path.name)
        if not args.dry_run:
            write(path / "merged.json", {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    if not args.dry_run and report["merged"]:
        write(main / "shotlist.json", shotlist)
        if poses:
            write(main / "poses.json", poses)
            write(main / "submissions" / "pose_owners.json", owners)
    for key in ("changed", "camera_changed", "staging_changed", "sets", "poses"):
        report[key] = sorted(set(report[key]), key=lambda value: position.get(value, 0))
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.submit", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    copy = sub.add_parser("copy")
    copy.add_argument("--job", required=True, type=Path)
    copy.add_argument("--to", required=True, type=Path)
    send = sub.add_parser("send")
    send.add_argument("--job", required=True, type=Path)
    send.add_argument("--to", required=True, type=Path)
    send.add_argument("--group", required=True)
    send.add_argument("--shots", required=True)
    send.add_argument("--note")
    sub.add_parser("list").add_argument("--job", required=True, type=Path)
    merge = sub.add_parser("merge")
    merge.add_argument("--job", required=True, type=Path)
    merge.add_argument("--only")
    merge.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = {"copy": command_copy, "send": command_send, "list": command_list, "merge": command_merge}[args.command](args)
    except (SubmitError, OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
