"""Drive the Luau director inside Roblox Studio through the Studio MCP server.

The director source (studio/director/*.luau and studio/director/sets/*.luau) and the data
library (studio/library/*.json) are bundled into one Luau chunk, sent with execute_luau and kept
in Studio's shared table, so the place file never has to carry the code.

Usage (on the machine running Studio, with a place open and the MCP server enabled):
  python -m pipeline.studio prepare                 clean the open place and add the effects
  python -m pipeline.studio build-sets [--only a,b] build the set library into the place
  python -m pipeline.studio load --job <folder>     store timeline.json in the place and build its cast
  python -m pipeline.studio frame <n> [--shot out.png] [--roll 90]
  python -m pipeline.studio info                    sets, anchors, cameras, props, viewport
  python -m pipeline.studio bundle --out file.luau  write the bundle (for syntax checks)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .studio_mcp import MCPError, StudioMCP

REPO = Path(__file__).resolve().parent.parent
DIRECTOR_DIR = REPO / "studio" / "director"
LIBRARY_DIR = REPO / "studio" / "library"
MARK_START, MARK_END, MISSING = "@@DIRECTOR@@", "@@END@@", "@@NO_DIRECTOR@@"
LONG_OPEN, LONG_CLOSE = "[=====[", "]=====]"


class DirectorError(RuntimeError):
    """Raised when the director reports an error from inside Studio."""


def load_library() -> Dict[str, Any]:
    library = {}
    for path in sorted(LIBRARY_DIR.glob("*.json")):
        with open(path, "r", encoding="utf-8") as handle:
            library[path.stem] = json.load(handle)
    return library


def set_names() -> List[str]:
    return sorted(path.stem for path in (DIRECTOR_DIR / "sets").glob("*.luau"))


def _long_string(text: str) -> str:
    if LONG_CLOSE in text:
        raise DirectorError("data contains the Luau long-string terminator")
    return LONG_OPEN + "\n" + text + LONG_CLOSE


def director_hash() -> str:
    digest = hashlib.sha1()
    for path in sorted(DIRECTOR_DIR.rglob("*.luau")) + sorted(LIBRARY_DIR.glob("*.json")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def install_chunk() -> str:
    """One Luau chunk that defines every director module and the library and keeps them in shared.__director.

    Studio keeps the shared table between execute_luau calls, so the director is sent once per
    Studio session (and again whenever its source changes) instead of once per command.
    """
    parts = [
        "local __modules, __loaded = {}, {}",
        "local function __require(name)",
        "\tif __loaded[name] == nil then",
        "\t\tlocal loader = __modules[name]",
        "\t\tif not loader then error('no director module ' .. name) end",
        "\t\t__loaded[name] = loader()",
        "\tend",
        "\treturn __loaded[name]",
        "end",
        "local __SET_NAMES = { %s }" % ", ".join('"%s"' % name for name in set_names()),
    ]
    modules = [(path.stem, path) for path in sorted(DIRECTOR_DIR.glob("*.luau"))]
    modules += [("set_" + path.stem, path) for path in sorted((DIRECTOR_DIR / "sets").glob("*.luau"))]
    for name, path in modules:
        parts.append('__modules["%s"] = function()' % name)
        parts.append(path.read_text(encoding="utf-8"))
        parts.append("end")
    parts += [
        "local __http = game:GetService('HttpService')",
        "local __lib = __http:JSONDecode(%s)" % _long_string(json.dumps(load_library())),
        "local __main = __require('main')",
        "shared.__director = { hash = '%s', run = function(text)" % director_hash(),
        "\tlocal command = __http:JSONDecode(text)",
        "\tlocal ok, result = xpcall(function() return __main(command, __lib) end,",
        "\t\tfunction(problem) return tostring(problem) .. '\\n' .. debug.traceback() end)",
        "\treturn '%s' .. __http:JSONEncode({ ok = ok, result = result }) .. '%s'" % (MARK_START, MARK_END),
        "end }",
        "return 'installed'",
    ]
    return "\n".join(parts) + "\n"


def call_chunk(command: Dict[str, Any]) -> str:
    return "\n".join([
        "local d = shared.__director",
        "if not d or d.hash ~= '%s' then return '%s' end" % (director_hash(), MISSING),
        "return d.run(%s)" % _long_string(json.dumps(command)),
    ]) + "\n"


class Director:
    """Runs director commands in one Studio instance."""

    def __init__(self, client: StudioMCP) -> None:
        self.client = client
        self.installs = 0

    def run(self, name: str, args: Optional[Dict[str, Any]] = None, timeout: float = 300.0) -> Any:
        chunk = call_chunk({"name": name, "args": args or {}})
        text = self.client.luau(chunk, "Edit", timeout)
        if MISSING in text:
            self.client.luau(install_chunk(), "Edit", timeout)
            self.installs += 1
            text = self.client.luau(chunk, "Edit", timeout)
        start = text.find(MARK_START)
        end = text.find(MARK_END, start)
        if start < 0 or end < 0:
            raise DirectorError("Studio returned no director result: %s" % text[:1500])
        payload = json.loads(text[start + len(MARK_START):end])
        if not payload.get("ok"):
            raise DirectorError(str(payload.get("result"))[:3000])
        return payload.get("result")

    def load(self, timeline: Dict[str, Any]) -> Any:
        """Store the timeline in the place and build its cast."""
        key = hashlib.sha1(json.dumps(timeline, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        return self.run("load", {"timeline": timeline, "key": key})

    def screenshot(self, target: Path) -> Path:
        """Save what the Studio viewport shows now, through the server's screen_capture tool (JPEG)."""
        result = self.client.call("screen_capture", {"capture_id": "ScreenCapture_1"})
        for item in result.get("content", []):
            if item.get("type") == "image":
                target.write_bytes(base64.b64decode(item["data"]))
                return target
        raise DirectorError("screen_capture returned no image: %s" % json.dumps(result)[:600])


def connect(studio_id: Optional[str] = None, timeout: float = 300.0, wait: float = 20.0) -> StudioMCP:
    """Start an MCP session and pick the Studio instance (the only one, unless studio_id is given).

    Studio registers with a new server process a second or two after it starts, so the list is polled.
    """
    client = StudioMCP(timeout=timeout)
    client.start()
    if studio_id is None:
        studios = []  # type: List[Dict[str, Any]]
        deadline = time.time() + wait
        while not studios:
            try:
                studios = json.loads(client.call_text("list_roblox_studios")).get("studios", [])
            except ValueError:
                studios = []
            if studios or time.time() > deadline:
                break
            time.sleep(0.5)
        if not studios:
            client.close()
            raise MCPError("no Studio instance is connected: open a place in Studio and enable 'Studio as MCP server' in Assistant settings")
        if len(studios) > 1:
            client.close()
            raise MCPError("several Studio instances are open (%s); pass --studio <id>" % ", ".join("%s=%s" % (s.get("name"), s.get("id")) for s in studios))
        studio_id = studios[0].get("id")
    client.studio_id = studio_id
    return client


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.studio", description=__doc__)
    parser.add_argument("--studio", help="studio_id when several Studio windows are open")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    build = sub.add_parser("build-sets")
    build.add_argument("--only", help="comma separated set names")
    load = sub.add_parser("load")
    load.add_argument("--job", required=True)
    frame = sub.add_parser("frame")
    frame.add_argument("n", type=int)
    frame.add_argument("--roll", type=float, default=0.0)
    frame.add_argument("--shot", help="save a viewport screenshot to this PNG")
    sub.add_parser("info")
    sub.add_parser("clear")
    out = sub.add_parser("bundle")
    out.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.command == "bundle":
        Path(args.out).write_text(install_chunk(), encoding="utf-8")
        return 0
    try:
        client = connect(args.studio)
    except MCPError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    try:
        director = Director(client)
        if args.command == "prepare":
            result = director.run("prepare")
        elif args.command == "build-sets":
            result = director.run("build_sets", {"only": args.only.split(",")} if args.only else {})
        elif args.command == "load":
            with open(Path(args.job) / "timeline.json", "r", encoding="utf-8") as handle:
                result = director.load(json.load(handle))
        elif args.command == "frame":
            result = director.run("frame", {"frame": args.n, "roll": args.roll})
            if args.shot:
                director.screenshot(Path(args.shot))
        elif args.command == "clear":
            result = director.run("clear")
        else:
            result = director.run("info")
        print(json.dumps(result, indent=1))
        return 0
    except (MCPError, DirectorError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
