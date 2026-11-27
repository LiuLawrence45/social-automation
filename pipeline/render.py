"""Render stage: play the timeline in Roblox Studio frame by frame and capture it (macOS only).

Studio 0.748 does not let scripts capture the viewport (StudioCaptureService answers "Feature not
supported yet"), so every frame is posed through the Studio MCP server and the Studio window is
captured with macOS screencapture. A small frame-number barcode drawn in a corner of the viewport is
read back from each capture, so a captured picture is known to show the frame that was asked for.

The Studio window is wider than it is tall, so the camera is rolled 90 degrees and the picture is
turned back while encoding; that uses about three times the pixels of an upright crop.

Usage (Studio open on a place with the sets built, MCP server enabled):
  python -m pipeline.render window [--fit]              show the Studio window, optionally fill the screen
  python -m pipeline.render calibrate                   find the viewport inside the window
  python -m pipeline.render still --job <folder> --frame N --out frame.png
  python -m pipeline.render sheet --job <folder> [--out sheet.jpg] [--cols 8] [--at 0.5] [--shots a,b]
  python -m pipeline.render video --job <folder> [--shots a,b] [--force] [--reuse]

video writes <job>/video.mp4 (1080x1920, no audio), <job>/shots.json and <job>/render_report.json.
Each shot is kept as <job>/render/<id>.h264 and rendered again only when its entry in timeline.json,
the cast or the director source changed (--reuse ignores director source changes, --force renders all).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import judge, story
from .edit import ffmpeg_bin
from .studio import Director, DirectorError, connect, director_hash
from .studio_mcp import MCPError

REPO = Path(__file__).resolve().parent.parent
HELPER_SOURCE = REPO / "studio" / "tools" / "studio_window.swift"
FONT = REPO / "assets" / "fonts" / "Montserrat-ExtraBold.ttf"
WIDTH, HEIGHT = 1080, 1920
ROLL = 90.0


class RenderError(RuntimeError):
    """Raised for failures the operator can act on."""


def helper_path() -> Path:
    """The compiled window helper; built with swiftc on first use and after its source changes."""
    override = os.environ.get("STUDIO_WINDOW_TOOL")
    if override:
        return Path(override)
    home = Path(os.environ.get("SA_HOME") or (Path.home() / "Library" / "Caches" / "social-automation"))
    target = home / "tools" / "bin" / "studio_window"
    if not target.exists() or target.stat().st_mtime < HELPER_SOURCE.stat().st_mtime:
        target.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(["swiftc", "-O", str(HELPER_SOURCE), "-o", str(target)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if proc.returncode != 0:
            raise RenderError("could not build the window helper with swiftc (install the Xcode command line tools):\n" + proc.stderr[-1500:])
    return target


def helper(*args: str) -> Dict[str, Any]:
    proc = subprocess.run([str(helper_path())] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if proc.returncode != 0:
        raise RenderError(proc.stderr.strip() or "window helper failed")
    return json.loads(proc.stdout)


class Bitmap:
    """An uncompressed BMP as screencapture writes it."""

    def __init__(self, data: bytes) -> None:
        if data[:2] != b"BM":
            raise RenderError("screencapture did not write a BMP")
        self.data = data
        self.offset = struct.unpack_from("<I", data, 10)[0]
        self.width, height, _, self.bits = struct.unpack_from("<iiHH", data, 18)
        self.top_down = height < 0
        self.height = abs(height)
        if self.bits not in (24, 32):
            raise RenderError("unexpected BMP depth %d" % self.bits)
        self.step = self.bits // 8
        self.row_size = ((self.bits * self.width + 31) // 32) * 4

    def _row(self, y: int) -> int:
        return self.offset + (y if self.top_down else self.height - 1 - y) * self.row_size

    def pixel(self, x: int, y: int) -> Tuple[int, int, int]:
        at = self._row(y) + x * self.step
        blue, green, red = self.data[at], self.data[at + 1], self.data[at + 2]
        return red, green, blue

    def crop(self, x: int, y: int, width: int, height: int) -> bytes:
        rows = []
        for line in range(y, y + height):
            at = self._row(line) + x * self.step
            rows.append(self.data[at:at + width * self.step])
        return b"".join(rows)

    @property
    def pix_fmt(self) -> str:
        return "bgra" if self.bits == 32 else "bgr24"


class Stage:
    """The Studio window, the viewport inside it and the director that poses frames."""

    def __init__(self, director: Director, roll: float = ROLL, settle: int = 1, settle_cut: int = 4, settle_set: float = 2.0,
                 settle_light: float = 1.0) -> None:
        self.director = director
        self.roll = roll
        self.settle = settle
        self.settle_cut = settle_cut
        self.settle_set = settle_set
        self.settle_light = settle_light
        self.tmp = Path(tempfile.mkdtemp(prefix="sa-render-")) / "frame.bmp"
        self.window = None  # type: Optional[Dict[str, Any]]
        self.viewport = None  # type: Optional[Tuple[int, int, int, int]]
        self.marker = {}  # type: Dict[str, int]
        self.retries = 0
        self.captures = 0
        self.server = None  # type: Optional[HTTPServer]
        self.on_frame = None  # type: Optional[Callable[[int, bytes, Bitmap], None]]
        self.failure = None  # type: Optional[Exception]

    def grab(self) -> Bitmap:
        proc = subprocess.run(["screencapture", "-x", "-o", "-t", "bmp", "-l", str(self.window["id"]), str(self.tmp)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if proc.returncode != 0 or not self.tmp.exists():
            raise RenderError("screencapture failed (the terminal needs Screen Recording access): " + proc.stderr.strip())
        self.captures += 1
        return Bitmap(self.tmp.read_bytes())

    def calibrate(self) -> Dict[str, Any]:
        """Find the viewport rectangle in window pixels by covering it with one flat colour."""
        helper("activate")
        deadline = time.time() + 5
        while True:
            listing = helper("list")
            if listing["windows"]:
                break
            if time.time() > deadline:
                raise RenderError("Roblox Studio has no window on this desktop")
            time.sleep(0.25)
        helper("park")
        self.window = listing["windows"][0]
        pattern = 0xA5C3
        info = self.director.run("calibrate", {"on": True, "pattern": pattern})
        try:
            deadline = time.time() + 5
            while True:
                image = self.grab()
                is_cover = lambda p: p[0] > 235 and p[1] < 20 and p[2] > 235
                columns = [x for x in range(image.width) if is_cover(image.pixel(x, image.height // 2))]
                if columns:
                    middle = (columns[0] + columns[-1]) // 2
                    rows = [y for y in range(image.height) if is_cover(image.pixel(middle, y))]
                    left, right, top, bottom = columns[0], columns[-1], rows[0], rows[-1]
                    break
                if time.time() > deadline:
                    raise RenderError("the calibration colour never showed in the Studio window; is the 3D view visible?")
                time.sleep(0.1)
            self.viewport = (left, top, right - left + 1, bottom - top + 1)
            self.marker = {"cell": info["cell"], "bits": info["bits"], "margin": info["margin"]}
            self.scale = self.viewport[2] / float(info["viewport"][0])
            if abs(self.viewport[3] / float(info["viewport"][1]) - self.scale) > 0.02:
                raise RenderError("viewport %s does not match Studio's %s" % (self.viewport, info["viewport"]))
            seen = self.read_marker(image)
            if seen != pattern:
                raise RenderError("the frame marker read back as %s, expected %d" % (seen, pattern))
        finally:
            self.director.run("calibrate", {"on": False})
        width, height = self.crop_size()
        strip = (self.marker["bits"] + 4) * self.marker["cell"] + 2 * self.marker["margin"]
        if (self.viewport[2] - width) / 2 < strip * self.scale:
            raise RenderError("the viewport is too narrow to keep the frame marker outside the picture; widen the Studio window")
        return {"window": self.window, "viewport": list(self.viewport), "scale": self.scale, "crop": [width, height],
                "upscale": round(WIDTH / float(min(width, height)), 3)}

    def crop_size(self) -> Tuple[int, int]:
        _, _, vw, vh = self.viewport
        if self.roll:
            height = vh - vh % 2
            width = min(vw, int(height * HEIGHT / WIDTH))
            return width - width % 2, height
        width = min(vw, int(vh * WIDTH / HEIGHT))
        return width - width % 2, vh - vh % 2

    def read_marker(self, image: Bitmap) -> Optional[int]:
        left, top, _, _ = self.viewport
        cell, bits, margin = self.marker["cell"], self.marker["bits"], self.marker["margin"]
        values = []
        for index in range(bits + 4):
            x = left + int((margin + (index + 0.5) * cell) * self.scale)
            y = top + int((margin + cell / 2.0) * self.scale)
            red, green, blue = image.pixel(x, y)
            if abs(red - green) > 40 or abs(green - blue) > 40:
                return None
            values.append(1 if red > 128 else 0)
        if values[0] != 1 or values[1] != 0 or values[-2] != 0 or values[-1] != 1:
            return None
        number = 0
        for bit in values[2:-2]:
            number = number * 2 + bit
        return number

    def _sink(self) -> str:
        """A local HTTP endpoint Studio calls once per posed frame; the frame is captured before it answers."""
        if self.server is None:
            stage = self

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self) -> None:
                    try:
                        status, body = stage._arrived(int(self.path.rsplit("/", 1)[-1]))
                    except Exception as exc:
                        stage.failure = exc
                        status, body = 500, str(exc)
                    data = body.encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

                def log_message(self, *args: Any) -> None:
                    pass

            self.server = HTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def _arrived(self, index: int) -> Tuple[int, str]:
        image = self.grab()
        if self.read_marker(image) != index:
            self.retries += 1
            return 409, "not on screen yet"
        left, top, vw, vh = self.viewport
        width, height = self.crop_size()
        self.on_frame(index, image.crop(left + (vw - width) // 2, top + (vh - height) // 2, width, height), image)
        return 200, "ok"

    def capture(self, frames: List[int], on_frame: Callable[[int, bytes, Bitmap], None],
                variants: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Pose the frames in order inside one Studio call and hand each captured picture to on_frame.

        variants are extra pictures of one frame under another camera: {"shot", "camera", "frame", "marker"},
        reported to on_frame under their marker number.

        Studio holds every frame until the capture has been taken and checked against the frame marker.
        The whole run stays inside one execute_luau call because Studio takes the edit camera back
        (and drops its roll) as soon as a call returns.
        """
        self.on_frame = on_frame
        self.failure = None
        try:
            args = {"frames": frames, "roll": self.roll, "sink": self._sink(), "settle": self.settle, "settle_cut": self.settle_cut,
                    "settle_set": self.settle_set, "settle_light": self.settle_light}
            if variants:
                args["variants"] = variants
            return self.director.run("capture", args, timeout=120 + 3.0 * (len(frames) + len(variants or [])))
        except DirectorError:
            if self.failure is not None:
                raise RenderError(str(self.failure))
            raise

    def release(self) -> None:
        try:
            self.director.run("release")
        except (DirectorError, MCPError):
            pass

    def video_filter(self, width: int = WIDTH, height: int = HEIGHT) -> str:
        steps = []
        if self.roll > 0:
            steps.append("transpose=2")
        elif self.roll < 0:
            steps.append("transpose=1")
        steps.append("scale=%d:%d:flags=lanczos:out_color_matrix=bt709:out_range=tv" % (width, height))
        return ",".join(steps)

    def ffmpeg_input(self, image: Bitmap, fps: float) -> List[str]:
        width, height = self.crop_size()
        return [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", image.pix_fmt,
                "-s", "%dx%d" % (width, height), "-r", str(fps), "-i", "-"]


def open_stage(args: argparse.Namespace) -> Stage:
    client = connect(getattr(args, "studio", None))
    return Stage(Director(client), roll=args.roll, settle=args.settle, settle_light=args.settle_light)


def load_timeline(job: Path, judged: bool = True) -> Dict[str, Any]:
    """timeline.json, with each shot's camera replaced by the judge's choice when judge.json has one."""
    path = job / "timeline.json"
    if not path.exists():
        raise RenderError("%s not found; run python -m pipeline.story timeline --job %s" % (path, job))
    with open(path, "r", encoding="utf-8") as handle:
        timeline = json.load(handle)
    chosen = judge.chosen_cameras(job) if judged else {}
    for shot in timeline["shots"]:
        if shot["id"] in chosen:
            shot["camera"] = chosen[shot["id"]]
    return timeline


class JudgeStager:
    """The Studio side of the judge stage: geometric checks and candidate stills for one job."""

    def __init__(self, job: Path, studio: Optional[str] = None, roll: float = ROLL, height: int = 480, at: float = 0.6) -> None:
        self.job = Path(job)
        self.timeline = load_timeline(self.job, judged=False)
        self.shots = {shot["id"]: shot for shot in self.timeline["shots"]}
        self.stage = Stage(Director(connect(studio)), roll=roll)
        self.stage.director.load(self.timeline)
        self.stage.calibrate()
        self.height = height
        self.at = at
        self.marker = 0

    def geometry(self, shot_id: str, cameras: List[Dict[str, Any]], previous: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        metrics = (previous or {}).get("metrics")
        args = {"shot": shot_id, "cameras": cameras, "roll": self.stage.roll}
        if isinstance(metrics, dict) and metrics:
            args["previous"] = metrics
        results = self.stage.director.run("judge", args)
        for result in results:
            if not isinstance(result.get("metrics"), dict):
                result["metrics"] = {}
            if not isinstance(result.get("issues"), list):
                result["issues"] = []
        return results

    def stills(self, shot_id: str, cameras: List[Dict[str, Any]], out_dir: Path) -> List[Path]:
        shot = self.shots[shot_id]
        frame = shot["frame_start"] + int((shot["frame_end"] - shot["frame_start"] - 1) * self.at)
        variants = []
        for camera in cameras:
            self.marker += 1
            variants.append({"shot": shot_id, "camera": camera, "frame": frame, "marker": self.marker})
        got = {}  # type: Dict[int, Tuple[bytes, Bitmap]]
        self.stage.capture([], lambda index, pixels, image: got.__setitem__(index, (pixels, image)), variants)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for position, variant in enumerate(variants):
            pixels, image = got[variant["marker"]]
            target = out_dir / ("%s.jpg" % "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[position])
            cmd = self.stage.ffmpeg_input(image, 1) + ["-vf", self.stage.video_filter(self.height * WIDTH // HEIGHT, self.height),
                                                       "-frames:v", "1", "-q:v", "3", str(target)]
            subprocess.run(cmd, input=pixels, check=True)
            paths.append(target)
        return paths

    def close(self) -> None:
        self.stage.release()
        self.stage.director.client.close()


def pick_shots(timeline: Dict[str, Any], wanted: Optional[str]) -> List[Dict[str, Any]]:
    if not wanted:
        return timeline["shots"]
    names = [name.strip() for name in wanted.split(",") if name.strip()]
    known = {shot["id"] for shot in timeline["shots"]}
    missing = [name for name in names if name not in known]
    if missing:
        raise RenderError("unknown shot id(s): %s" % ", ".join(missing))
    return [shot for shot in timeline["shots"] if shot["id"] in names]


def by_set(shots: List[Dict[str, Any]]) -> List[int]:
    """Positions of the shots grouped by set (sets in order of first use), so the camera changes region as rarely as possible."""
    first = {}  # type: Dict[str, int]
    for position, shot in enumerate(shots):
        first.setdefault(shot["set"], position)
    return sorted(range(len(shots)), key=lambda position: (first[shots[position]["set"]], position))


def command_still(stage: Stage, args: argparse.Namespace) -> Dict[str, Any]:
    timeline = load_timeline(Path(args.job))
    stage.director.load(timeline)
    calibration = stage.calibrate()
    got = []  # type: List[Tuple[bytes, Bitmap]]
    stage.capture([args.frame], lambda index, pixels, image: got.append((pixels, image)))
    cmd = stage.ffmpeg_input(got[0][1], timeline["fps"]) + ["-vf", stage.video_filter(), "-frames:v", "1", str(args.out)]
    subprocess.run(cmd, input=got[0][0], check=True)
    return {"out": str(args.out), "calibration": calibration}


def command_sheet(stage: Stage, args: argparse.Namespace) -> Dict[str, Any]:
    """One picture per shot (taken at --at of its length) laid out as a contact sheet."""
    job = Path(args.job)
    timeline = load_timeline(job)
    stage.director.load(timeline)
    stage.calibrate()
    shots = pick_shots(timeline, args.shots)
    wanted = [shot["frame_start"] + int((shot["frame_end"] - shot["frame_start"] - 1) * args.at) for shot in shots]
    got = {}  # type: Dict[int, bytes]
    images = []  # type: List[Bitmap]
    order = [wanted[position] for position in by_set(shots)]
    stage.capture(order, lambda index, pixels, image: (got.__setitem__(index, pixels), images.append(image)))
    frames = [got[index] for index in wanted]
    image = images[0]
    cols = min(args.cols, len(shots))
    rows = (len(shots) + cols - 1) // cols
    label = "drawtext=fontfile='%s':text='%%{eif\\:n+%d\\:d}':x=8:y=8:fontsize=30:fontcolor=white:borderw=3:bordercolor=black" % (FONT, args.number_from)
    out = Path(args.out) if args.out else job / "sheet.jpg"
    chain = "%s,%s,tile=%dx%d" % (stage.video_filter(args.tile_width, args.tile_width * HEIGHT // WIDTH), label, cols, rows)
    cmd = stage.ffmpeg_input(image, 1) + ["-vf", chain, "-frames:v", "1", "-q:v", "3", str(out)]
    subprocess.run(cmd, input=b"".join(frames), check=True)
    return {"out": str(out), "shots": ["%d %s" % (number + args.number_from, shot["id"]) for number, shot in enumerate(shots)]}


def shot_key(shot: Dict[str, Any], timeline: Dict[str, Any], stage: Stage, reuse: bool) -> str:
    basis = {"shot": shot, "cast": timeline["cast"], "fps": timeline["fps"], "roll": stage.roll, "crop": stage.crop_size(),
             "director": "" if reuse else director_hash()}
    return hashlib.sha1(json.dumps(basis, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def command_video(stage: Stage, args: argparse.Namespace) -> Dict[str, Any]:
    """Render changed shots, join every shot into video.mp4 and write shots.json and render_report.json.

    Shots are raw H.264 streams without B-frames, so joining them is byte concatenation, and wrapping
    the joined stream once gives the container exact constant-frame-rate timestamps.
    """
    job = Path(args.job)
    timeline = load_timeline(job)
    started = time.time()
    stage.director.load(timeline)
    calibration = stage.calibrate()
    folder = job / "render"
    folder.mkdir(exist_ok=True)
    only = {shot["id"] for shot in pick_shots(timeline, args.shots)} if args.shots else None
    rendered, kept, frames_done = [], [], 0
    capture_seconds = 0.0
    for position in by_set(timeline["shots"]):
        shot = timeline["shots"][position]
        target = folder / (shot["id"] + ".h264")
        key_file = folder / (shot["id"] + ".key")
        key = shot_key(shot, timeline, stage, args.reuse)
        current = target.exists() and key_file.exists() and key_file.read_text().strip() == key
        wanted = (only is None and (args.force or not current)) or (only is not None and (shot["id"] in only or not target.exists()))
        if not wanted:
            kept.append(shot["id"])
            continue
        began = time.time()
        encoders = []  # type: List[subprocess.Popen]

        def encode(index: int, pixels: bytes, image: Bitmap, target: Path = target) -> None:
            if not encoders:
                cmd = stage.ffmpeg_input(image, timeline["fps"]) + [
                    "-vf", stage.video_filter(), "-c:v", "libx264", "-preset", "fast", "-crf", str(args.crf), "-bf", "0", "-pix_fmt", "yuv420p",
                    "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
                    "-r", str(timeline["fps"]), "-an", "-f", "h264", str(target)]
                encoders.append(subprocess.Popen(cmd, stdin=subprocess.PIPE))
            encoders[0].stdin.write(pixels)

        try:
            done = stage.capture(list(range(shot["frame_start"], shot["frame_end"])), encode)
            frames_done += done["frames"]
        finally:
            if encoders:
                encoders[0].stdin.close()
                if encoders[0].wait() != 0:
                    raise RenderError("FFmpeg failed while encoding shot %s" % shot["id"])
        key_file.write_text(key)
        rendered.append(shot["id"])
        capture_seconds += time.time() - began
        print("shot %s: %d frames in %.1f s" % (shot["id"], shot["frame_end"] - shot["frame_start"], time.time() - began), flush=True)
    joined = folder / "all.h264"
    with open(joined, "wb") as handle:
        for shot in timeline["shots"]:
            handle.write((folder / (shot["id"] + ".h264")).read_bytes())
    subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-framerate", str(timeline["fps"]), "-i", str(joined),
                    "-c", "copy", "-bsf:v", "setts=pts=N:dts=N:duration=1:time_base=1/%d" % timeline["fps"],
                    "-video_track_timescale", str(timeline["fps"]), "-movflags", "+faststart",
                    str(job / "video.mp4")], check=True)
    joined.unlink()
    with open(job / "shots.json", "w", encoding="utf-8") as handle:
        json.dump(story.shots_handoff(timeline), handle, indent=1)
    report = {
        "frames_total": timeline["frames"], "frames_rendered": frames_done, "shots_rendered": rendered, "shots_kept": len(kept),
        "seconds_total": round(time.time() - started, 2), "seconds_capturing": round(capture_seconds, 2),
        "frames_per_second": round(frames_done / capture_seconds, 2) if capture_seconds else None,
        "captures": stage.captures, "recaptures_waiting_for_frame": stage.retries, "calibration": calibration,
        "roll": stage.roll, "director_installs": stage.director.installs,
    }
    with open(job / "render_report.json", "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.render", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--studio", help="studio id when several Studio windows are open")
    parser.add_argument("--roll", type=float, default=ROLL, help="camera roll in degrees; 0 renders upright in a narrow crop")
    parser.add_argument("--settle", type=int, default=1, help="Studio frames to wait after posing a frame")
    parser.add_argument("--settle-light", type=float, default=1.0, help="seconds to wait after a shot moves the key light, so its shadows are rebuilt")
    sub = parser.add_subparsers(dest="command", required=True)
    window = sub.add_parser("window")
    window.add_argument("--fit", action="store_true", help="resize the Studio window to fill the screen")
    sub.add_parser("calibrate")
    still = sub.add_parser("still")
    still.add_argument("--job", required=True)
    still.add_argument("--frame", type=int, required=True)
    still.add_argument("--out", required=True)
    sheet = sub.add_parser("sheet")
    sheet.add_argument("--job", required=True)
    sheet.add_argument("--out")
    sheet.add_argument("--cols", type=int, default=8)
    sheet.add_argument("--at", type=float, default=0.5)
    sheet.add_argument("--shots")
    sheet.add_argument("--tile-width", type=int, default=270)
    sheet.add_argument("--number-from", type=int, default=0)
    video = sub.add_parser("video")
    video.add_argument("--job", required=True)
    video.add_argument("--shots")
    video.add_argument("--force", action="store_true")
    video.add_argument("--reuse", action="store_true")
    video.add_argument("--crf", type=int, default=14)
    args = parser.parse_args(argv)
    stage = None
    try:
        if args.command == "window":
            if args.fit:
                visible = helper("list")["visible"]
                helper("frame", str(visible["x"]), str(visible["y"]), str(visible["width"]), str(visible["height"]))
            print(json.dumps(helper("list"), indent=1))
            return 0
        stage = open_stage(args)
        if args.command == "calibrate":
            result = stage.calibrate()
        elif args.command == "still":
            result = command_still(stage, args)
        elif args.command == "sheet":
            result = command_sheet(stage, args)
        else:
            result = command_video(stage, args)
        print(json.dumps(result, indent=1))
        return 0
    except (RenderError, MCPError, DirectorError, story.StoryError, subprocess.CalledProcessError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    finally:
        if stage is not None:
            stage.release()
            stage.director.client.close()


if __name__ == "__main__":
    sys.exit(main())
