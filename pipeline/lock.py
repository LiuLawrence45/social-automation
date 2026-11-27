"""One machine-wide lock for everything that talks to Roblox Studio or captures its window.

Studio holds one timeline, one cast and one camera at a time, and frames are read off the screen, so two
programs that interleave their calls spoil each other's pictures. Every pipeline command that opens a Studio
session takes this lock first and gives it back when the session closes.

The lock is a directory ($STUDIO_LOCK, else $SA_HOME/studio.lock, else ~/social-automation-output/studio.lock)
holding owner.json: who has it, since when, from which process. A waiter polls until the lock is free or the
wait runs out ($STUDIO_LOCK_WAIT seconds, default 3600). A lock whose process has died, or that has not been
touched for $STUDIO_LOCK_STALE seconds (default 1800; every director call touches it), is taken over.
A process that already holds the lock, and any program it starts, passes straight through.

  python -m pipeline.lock status        who holds the lock
  python -m pipeline.lock break         remove a lock left behind (only when its holder is truly gone)
"""
from __future__ import annotations

import atexit
import getpass
import json
import os
import shutil
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

ENV_TOKEN = "STUDIO_LOCK_TOKEN"


class LockTimeout(RuntimeError):
    """Raised when the lock stays taken for the whole wait."""


def lock_path() -> Path:
    explicit = os.environ.get("STUDIO_LOCK")
    if explicit:
        return Path(explicit)
    home = os.environ.get("SA_HOME")
    return (Path(home) if home else Path.home() / "social-automation-output") / "studio.lock"


def default_holder() -> str:
    name = os.environ.get("STUDIO_HOLDER")
    if name:
        return name
    return "%s: %s" % (getpass.getuser(), " ".join(sys.argv[:4])[:120])


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class StudioLock:
    def __init__(self, holder: Optional[str] = None, wait: Optional[float] = None, stale: Optional[float] = None,
                 path: Optional[Path] = None, poll: float = 1.0) -> None:
        self.path = Path(path) if path else lock_path()
        self.holder = holder or default_holder()
        self.wait = float(wait if wait is not None else os.environ.get("STUDIO_LOCK_WAIT", 3600))
        self.stale = float(stale if stale is not None else os.environ.get("STUDIO_LOCK_STALE", 1800))
        self.poll = poll
        self.token = None  # type: Optional[str]
        self.inherited = False

    def owner(self) -> Optional[Dict[str, Any]]:
        try:
            with open(self.path / "owner.json", "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def _age(self) -> float:
        for target in (self.path / "owner.json", self.path):
            try:
                return time.time() - target.stat().st_mtime
            except OSError:
                continue
        return 0.0

    def is_stale(self, owner: Optional[Dict[str, Any]]) -> bool:
        if owner is None:
            return self._age() > 10.0
        if owner.get("host") == socket.gethostname() and isinstance(owner.get("pid"), int) and not _alive(owner["pid"]):
            return True
        return self._age() > self.stale

    def _take_over(self) -> None:
        aside = self.path.with_name("%s.stale-%s" % (self.path.name, uuid.uuid4().hex[:8]))
        try:
            os.rename(self.path, aside)
        except OSError:
            return
        shutil.rmtree(aside, ignore_errors=True)

    def acquire(self) -> "StudioLock":
        inherited = os.environ.get(ENV_TOKEN)
        if inherited:
            owner = self.owner()
            if owner and owner.get("token") == inherited:
                self.inherited = True
                return self
        deadline = time.time() + self.wait
        said = 0.0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                os.mkdir(self.path)
            except FileExistsError:
                owner = self.owner()
                if self.is_stale(owner):
                    self._take_over()
                    continue
                if time.time() >= deadline:
                    raise LockTimeout("Studio is still in use by %s (since %s) after waiting %d s; lock at %s"
                                      % ((owner or {}).get("holder", "an unknown holder"), (owner or {}).get("started", "?"), self.wait, self.path))
                if time.time() - said > 30:
                    said = time.time()
                    print("waiting for Studio: held by %s since %s" % ((owner or {}).get("holder", "?"), (owner or {}).get("started", "?")),
                          file=sys.stderr, flush=True)
                time.sleep(self.poll)
                continue
            self.token = uuid.uuid4().hex
            record = {"holder": self.holder, "pid": os.getpid(), "host": socket.gethostname(), "token": self.token,
                      "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "command": " ".join(sys.argv)[:300]}
            with open(self.path / "owner.json", "w", encoding="utf-8") as handle:
                json.dump(record, handle, indent=1)
            os.environ[ENV_TOKEN] = self.token
            atexit.register(self.release)
            return self

    def touch(self) -> None:
        """Say the holder is still working, so a long render is not mistaken for a dead one."""
        if self.token:
            try:
                os.utime(self.path / "owner.json", None)
            except OSError:
                pass

    def release(self) -> None:
        if self.inherited or not self.token:
            return
        owner = self.owner()
        if owner and owner.get("token") == self.token:
            shutil.rmtree(self.path, ignore_errors=True)
        if os.environ.get(ENV_TOKEN) == self.token:
            del os.environ[ENV_TOKEN]
        self.token = None

    def __enter__(self) -> "StudioLock":
        return self.acquire()

    def __exit__(self, *exc: Any) -> None:
        self.release()


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    lock = StudioLock()
    owner = lock.owner()
    if args[:1] == ["break"]:
        if lock.path.exists():
            lock._take_over()
            print("removed the lock held by %s" % (owner or {}).get("holder", "an unknown holder"))
        else:
            print("no lock")
        return 0
    if not lock.path.exists():
        print("free")
    else:
        print(json.dumps(dict(owner or {}, idle_seconds=round(lock._age(), 1), stale=lock.is_stale(owner)), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
