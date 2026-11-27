"""What a shot list can name, read from the source without Studio: anchors, set cameras, moving pieces and lights of
each set, props, poses, stock animations, faces, lighting moods and the job's cast.

  python -m pipeline.catalog [--job <folder>] [--set NAME] [--json]

With --job, the folder's own poses.json and sets/<set>.luau are laid over the repository's, as they are when
that job is previewed or rendered.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import studio

NUMBER = r"\s*(-?[\d.]+)\s*"


# Props that are still built, so an old shot list stages, but that a new shot must not use.
WITHDRAWN = {"sun_patch": "a flat translucent sheet reads as paper lying on the floor or stuck to the wall, and nobody casts a shadow on it; "
                          "daylight has to come from the lighting mood and bright windows"}


def read_set(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    anchors = {m.group(1): {"pos": [float(m.group(2)), float(m.group(3)), float(m.group(4))], "yaw": float(m.group(5) or 0)}
               for m in re.finditer(r'b:anchor\("(\w+)",%s,%s,%s(?:,%s)?\)' % (NUMBER, NUMBER, NUMBER, NUMBER), text)}
    cameras = {m.group(1): {"pos": json.loads("[%s]" % m.group(2)), "look": json.loads("[%s]" % m.group(3))}
               for m in re.finditer(r'b:camera\("(\w+)",\s*\{([^}]*)\},\s*\{([^}]*)\}', text)}
    pieces = {m.group(1): m.group(2) for m in re.finditer(r'b:piece\("(\w+)",\s*"(\w+)"', text)}
    lights = sorted(set(re.findall(r'b:light\([^\n]*name = "(\w+)"', text)))
    mood = re.search(r'b\.info\.lighting = "(\w+)"', text)
    return {"file": str(path), "lighting": mood.group(1) if mood else None, "anchors": anchors, "cameras": cameras, "pieces": pieces, "lights": lights}


def collect(job: Optional[Path]) -> Dict[str, Any]:
    studio.use_job(job)
    library = studio.load_library()
    poses = {}
    for name, pose in library.get("avatar_poses", {}).items():
        if name.startswith("_") or not isinstance(pose, dict):
            continue
        poses[name] = {"action": pose.get("action"), "rest": bool(pose.get("rest")), "holds": pose.get("holds"), "ground": pose.get("ground", "feet"),
                       "reach": pose.get("reach") or {}, "moves": bool(pose.get("wobble"))}
    props = sorted(re.findall(r"(?m)^function BUILD\.(\w+)", (studio.DIRECTOR_DIR / "props.luau").read_text(encoding="utf-8")))
    points = sorted(re.findall(r'(?m)^\t(\w+) = \{ "\w+", \{', (studio.DIRECTOR_DIR / "pose.luau").read_text(encoding="utf-8")))
    out = {"sets": {name: read_set(path) for name, path in sorted(studio.set_files().items())}, "props": props, "poses": poses,
           "body_points": points, "animations": sorted(library.get("animations", {})), "faces": sorted(library.get("expressions", {})),
           "lighting": sorted(library.get("lighting", {}))}
    if job is not None and (job / "shotlist.json").is_file():
        with open(job / "shotlist.json", "r", encoding="utf-8") as handle:
            shotlist = json.load(handle)
        out["cast"] = shotlist.get("cast", {})
        by_set = {}  # type: Dict[str, List[str]]
        for shot in shotlist.get("shots", []):
            by_set.setdefault(shot.get("set"), []).append(shot["id"])
        out["shots_by_set"] = by_set
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.catalog", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job", type=Path)
    parser.add_argument("--set", dest="only")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    found = collect(args.job.expanduser() if args.job else None)
    if args.only:
        found["sets"] = {name: value for name, value in found["sets"].items() if name == args.only}
    if args.json:
        print(json.dumps(found, indent=1))
        return 0
    for name, info in found["sets"].items():
        print("set %s  (lighting %s, %s)" % (name, info["lighting"], info["file"]))
        for anchor, value in info["anchors"].items():
            print("  anchor %-16s pos %-22s yaw %g" % (anchor, value["pos"], value["yaw"]))
        for camera, value in info["cameras"].items():
            print("  camera %-16s from %s look %s" % (camera, value["pos"], value["look"]))
        if info["pieces"]:
            print("  pieces (set_state): %s" % ", ".join("%s (%s)" % item for item in info["pieces"].items()))
        if info["lights"]:
            print("  lights: %s" % ", ".join(info["lights"]))
    if "shots_by_set" in found:
        for name, ids in found["shots_by_set"].items():
            print("shots in %s: %s" % (name, ",".join(ids)))
        print("cast: %s" % ", ".join(sorted(found["cast"])))
    print("props: %s" % ", ".join(name for name in found["props"] if name not in WITHDRAWN))
    for name, why in sorted(WITHDRAWN.items()):
        if name in found["props"]:
            print("  do not use %s: %s" % (name, why))
    print("faces: %s" % ", ".join(found["faces"]))
    print("animations: %s" % ", ".join(found["animations"]))
    print("lighting moods (time): %s" % ", ".join(found["lighting"]))
    print("body points a hand can reach: %s" % ", ".join(found["body_points"]))
    print("poses:")
    for name, pose in found["poses"].items():
        notes = [note for note in ("rest" if pose["rest"] else "", "holds a prop in %s" % pose["holds"] if pose["holds"] else "",
                                   "planted on %s" % pose["ground"] if pose["ground"] != "feet" else "", "moves" if pose["moves"] else "") if note]
        print("  %-18s %s%s" % (name, pose["action"], "  [%s]" % "; ".join(notes) if notes else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
