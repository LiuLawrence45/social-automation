"""Preview frames of named shots from any job folder, for agents that author shots without touching Studio.

  python -m pipeline.preview --job <folder> --shots a,b [--at 0.1,0.5,0.9] [--out <folder>] [--height 960]
                             [--check] [--judged] [--holder NAME]

One hold of the Studio lock from start to finish (pipeline/lock.py): the command waits its turn, loads this
job's timeline, cast, poses and sets whatever Studio held before, poses each named shot, and writes
  <out>/<shot>/<shot>_<NNN>.png   one frame per moment (NNN is the moment as a percentage of the shot)
  <out>/sheet.jpg                 every frame, labelled with shot id and moment, caption band drawn in
  <out>/preview.json              frame numbers, timings, which sets were rebuilt
  <out>/posture.json              with --check: clipping, joint limits, floating and idle findings per frame
It restores nothing afterwards; the next caller loads what it needs. The job folder is the caller's own copy
(python -m pipeline.submit copy), holding shotlist.json and a timeline.json made from it
(python -m pipeline.story timeline --job <folder>). Cameras are the shot list's own unless --judged asks for
the cameras the judge picked in that folder's judge.json.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import judge, posture
from .lock import LockTimeout
from .render import JudgeStager, RenderError
from .studio import DirectorError
from .studio_mcp import MCPError

COLORS = ["red", "green", "blue", "yellow", "magenta", "cyan", "orange", "white"]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.preview", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--shots", required=True, help="comma separated shot ids")
    parser.add_argument("--at", default="0.1,0.5,0.9", help="moments of each shot, 0 (first frame) to 1 (last frame)")
    parser.add_argument("--out", type=Path, help="output folder (default <job>/preview)")
    parser.add_argument("--height", type=int, default=960, help="height of each PNG in pixels (the render is 1920)")
    parser.add_argument("--sheet-height", type=int, default=480)
    parser.add_argument("--check", action="store_true", help="also run the geometric posture check on the same frames")
    parser.add_argument("--judged", action="store_true", help="use the cameras picked in this folder's judge.json")
    parser.add_argument("--holder", help="name shown to others waiting for Studio")
    args = parser.parse_args(argv)
    if args.holder:
        os.environ["STUDIO_HOLDER"] = args.holder
    job = args.job.expanduser().resolve()
    out = (args.out.expanduser() if args.out else job / "preview").resolve()
    moments = [min(1.0, max(0.0, float(piece))) for piece in args.at.split(",") if piece.strip()]
    wanted = [piece.strip() for piece in args.shots.split(",") if piece.strip()]
    if not (job / "timeline.json").is_file():
        print("error: %s has no timeline.json; run python -m pipeline.story timeline --job %s" % (job, job), file=sys.stderr)
        return 2
    stager = None
    started = time.time()
    try:
        stager = JudgeStager(job, height=args.height)
        loaded = time.time()
        missing = [shot_id for shot_id in wanted if shot_id not in stager.shots]
        if missing:
            raise RenderError("unknown shot id(s): %s" % ", ".join(missing))
        words = judge.read_json(job / "words.json", required=False) or []
        if isinstance(words, dict):
            words = words.get("words", [])
        cameras = judge.chosen_cameras(job) if args.judged else {}
        tiles, record, public = [], {}, {}
        for position, shot_id in enumerate(wanted):
            shot = stager.shots[shot_id]
            camera = cameras.get(shot_id, shot.get("camera"))
            frames = stager.frames_at(shot_id, moments)
            paths = stager.moments(shot_id, camera, moments, out / shot_id, args.height, "png")
            text = judge.shot_text(shot, words) if shot.get("text") else ""
            kept = []
            for path, moment, frame in zip(paths, moments, frames):
                target = path.with_name("%s_%03d.png" % (shot_id, int(round(moment * 100))))
                path.replace(target)
                kept.append(str(target))
                tiles.append({"path": target, "label": "%s %.2f" % (shot_id, moment), "word": text.split(" ")[0] if text else "", "color": COLORS[position % len(COLORS)]})
            record[shot_id] = {"set": shot.get("set"), "frames": frames, "files": kept, "text": text}
            if args.check and shot.get("cast"):
                measured = stager.posture(shot_id, camera, moments)
                samples = measured.get("samples") if isinstance(measured, dict) else []
                found = posture.frames_record(samples if isinstance(samples, list) else [])
                failing = any(issue["in_frame"] for frame in found for issue in frame["issues"])
                public[shot_id] = {"status": "fail" if failing else "pass", "characters": posture.characters_seen(samples), "frames": found}
        mode = "text" if judge.drawtext_available() else "boxes"
        judge.contact_sheet(tiles, out / "sheet.jpg", args.sheet_height, len(moments), mode)
        summary = {"job": str(job), "out": str(out), "sheet": str(out / "sheet.jpg"), "moments": moments, "shots": record,
                   "sets_rebuilt": stager.stage.director.rebuilt.get("rebuilt", {}), "director_installed": stager.stage.director.installs > 0,
                   "seconds": {"wait_and_load": round(loaded - started, 1), "frames": round(time.time() - loaded, 1)}}
        with open(out / "preview.json", "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=1)
        if args.check:
            with open(out / "posture.json", "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "shots": public}, handle, indent=1)
            for shot_id, found in public.items():
                lines = sorted({"%s: %s %s" % (issue["actor"], issue["kind"], issue["detail"]) for frame in found["frames"] for issue in frame["issues"] if issue["in_frame"]})
                print("%s %s%s" % (shot_id, found["status"], "".join("\n    " + line for line in lines)))
        print(json.dumps({key: summary[key] for key in ("out", "sheet", "sets_rebuilt", "seconds")}, indent=1))
        return 0
    except (RenderError, MCPError, DirectorError, LockTimeout, judge.JudgeError, subprocess.CalledProcessError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    finally:
        if stager is not None:
            stager.close()


if __name__ == "__main__":
    sys.exit(main())
