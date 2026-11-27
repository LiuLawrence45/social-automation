"""A small stdio MCP client for Roblox Studio's built-in MCP server.

Studio ships the server as a local binary that speaks MCP over stdin/stdout, so this client
must run on the machine where Studio runs.

Usage:
  python -m pipeline.studio_mcp tools                       list the server's tools
  python -m pipeline.studio_mcp studios                     list connected Studio instances
  python -m pipeline.studio_mcp call <tool> --args '{...}'  call any tool
  python -m pipeline.studio_mcp luau <file.luau> [--mode Edit|Client|Server]
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_COMMAND = "/Applications/RobloxStudio.app/Contents/MacOS/StudioMCP"
PROTOCOL_VERSION = "2025-06-18"


class MCPError(RuntimeError):
    """Raised when the server is unreachable, times out, or returns an error."""


class StudioMCP:
    """One MCP session with the Studio server process."""

    def __init__(self, command: Optional[str] = None, timeout: float = 120.0) -> None:
        self.command = command or os.environ.get("STUDIO_MCP") or DEFAULT_COMMAND
        self.timeout = timeout
        self.proc = None  # type: Optional[subprocess.Popen]
        self.lines = queue.Queue()  # type: queue.Queue
        self.next_id = 0
        self.server_info = {}  # type: Dict[str, Any]
        self.studio_id = None  # type: Optional[str]
        self.stderr_tail = []  # type: List[str]

    def __enter__(self) -> "StudioMCP":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def start(self) -> None:
        try:
            self.proc = subprocess.Popen([self.command], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            raise MCPError("Studio MCP server not found at %s (is Roblox Studio installed?)" % self.command)
        threading.Thread(target=self._pump, args=(self.proc.stdout, True), daemon=True).start()
        threading.Thread(target=self._pump, args=(self.proc.stderr, False), daemon=True).start()
        result = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                             "clientInfo": {"name": "social-automation", "version": "0.1.0"}}, timeout=30)
        self.server_info = result
        self.notify("notifications/initialized", {})

    def _pump(self, stream: Any, is_stdout: bool) -> None:
        for raw in iter(stream.readline, b""):
            if is_stdout:
                self.lines.put(raw)
            else:
                self.stderr_tail = (self.stderr_tail + [raw.decode("utf-8", "replace").rstrip()])[-20:]
        if is_stdout:
            self.lines.put(None)

    def _send(self, message: Dict[str, Any]) -> None:
        if self.proc is None or self.proc.poll() is not None:
            raise MCPError("the Studio MCP server is not running. " + " | ".join(self.stderr_tail[-3:]))
        self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        self.next_id += 1
        request_id = self.next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.time() + (timeout or self.timeout)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise MCPError("timed out after %.0f s waiting for %s" % (timeout or self.timeout, method))
            try:
                raw = self.lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if raw is None:
                raise MCPError("the Studio MCP server exited. " + " | ".join(self.stderr_tail[-3:]))
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if "method" in message and "id" in message:
                self._send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "not supported by this client"}})
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise MCPError("%s failed: %s" % (method, json.dumps(message["error"])[:1500]))
            return message.get("result", {})

    def list_tools(self) -> List[Dict[str, Any]]:
        tools = []
        cursor = None
        while True:
            result = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call(self, name: str, arguments: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Call a tool and return the raw MCP result ({'content': [...], 'isError': bool})."""
        arguments = dict(arguments or {})
        if self.studio_id and "studio_id" not in arguments and name != "list_roblox_studios":
            arguments["studio_id"] = self.studio_id
        return self.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)

    def call_text(self, name: str, arguments: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> str:
        """Call a tool and return its text content; raises on a tool-level error."""
        result = self.call(name, arguments, timeout)
        text = "\n".join(item.get("text", "") for item in result.get("content", []) if item.get("type") == "text")
        if result.get("isError"):
            raise MCPError("%s returned an error: %s" % (name, text[:3000]))
        return text

    def luau(self, code: str, mode: str = "Edit", timeout: Optional[float] = None) -> str:
        """Run Luau in Studio through execute_luau and return what it returned or printed."""
        return self.call_text("execute_luau", {"code": code, "datamodel_type": mode}, timeout)

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        self.proc = None
        lock = getattr(self, "lock", None)
        if lock is not None:
            self.lock = None
            lock.release()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.studio_mcp", description=__doc__)
    parser.add_argument("--studio", help="studio_id to target (see the studios command)")
    parser.add_argument("--timeout", type=float, default=120.0)
    sub = parser.add_subparsers(dest="command", required=True)
    tools_cmd = sub.add_parser("tools")
    tools_cmd.add_argument("--schema", action="store_true", help="print each tool's input schema")
    sub.add_parser("studios")
    call_cmd = sub.add_parser("call")
    call_cmd.add_argument("tool")
    call_cmd.add_argument("--args", default="{}")
    call_cmd.add_argument("--save-images", help="folder for any image content the tool returns")
    luau_cmd = sub.add_parser("luau")
    luau_cmd.add_argument("file")
    luau_cmd.add_argument("--mode", default="Edit", choices=["Edit", "Client", "Server"])
    args = parser.parse_args(argv)
    from .lock import StudioLock
    try:
        with StudioLock(), StudioMCP(timeout=args.timeout) as client:
            client.studio_id = args.studio
            if args.command == "tools":
                for tool in client.list_tools():
                    print(tool["name"] + (": " + json.dumps(tool.get("inputSchema", {})) if args.schema else ""))
            elif args.command == "studios":
                print(client.call_text("list_roblox_studios"))
            elif args.command == "luau":
                print(client.luau(Path(args.file).read_text(encoding="utf-8"), args.mode))
            else:
                result = client.call(args.tool, json.loads(args.args))
                for index, item in enumerate(result.get("content", [])):
                    if item.get("type") == "text":
                        print(item["text"])
                    elif item.get("type") == "image" and args.save_images:
                        import base64
                        folder = Path(args.save_images)
                        folder.mkdir(parents=True, exist_ok=True)
                        extension = item.get("mimeType", "image/png").split("/")[-1]
                        target = folder / ("%s_%d.%s" % (args.tool, index, extension))
                        target.write_bytes(base64.b64decode(item["data"]))
                        print("saved " + str(target))
                    else:
                        print("[%s content, %d bytes]" % (item.get("type"), len(json.dumps(item))))
                if result.get("isError"):
                    return 1
        return 0
    except MCPError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
