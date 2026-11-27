"""Collection routes for the shorts backlog. A route lists channels, hashtags and searches, fetches one
short's metadata and audio, and re-reads its counts. yt-dlp is the route that exists; a second one
(a hosted scraper, the YouTube Data API) is a subclass registered in ROUTES and picked with --route.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_SECONDS = 185


class CollectError(RuntimeError):
    """A listing or fetch failed."""


class Blocked(CollectError):
    """The source refused the request as automated traffic; try later, slower, or from another address."""


class Collector(object):
    """What a collection route must provide. Listings return dicts with id, title, view_count and, when the
    source gives them, duration, channel and channel_id. fetch and counts return the full info dict of one short
    (id, title, channel, channel_id, uploader_id, channel_follower_count, upload_date, duration, view_count,
    like_count, comment_count, tags, language)."""

    name = "base"

    def list_channel(self, channel: str) -> Dict[str, Any]:
        raise NotImplementedError

    def list_hashtag(self, tag: str, limit: int) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def search(self, query: str, limit: int) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def fetch(self, video: str, audio_dir: Path) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def counts(self, video: str) -> Dict[str, Any]:
        raise NotImplementedError


class YtDlp(Collector):
    """yt-dlp on PATH. Free; listings and counts work from anywhere, audio downloads are refused from a
    data-centre address after a few hundred requests and then need a home connection or a pause."""

    name = "yt-dlp"

    def _run(self, args: List[str]) -> str:
        proc = subprocess.run(["yt-dlp", "--no-warnings"] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if proc.returncode != 0:
            if "Sign in to confirm" in proc.stderr or "HTTP Error 429" in proc.stderr:
                raise Blocked(proc.stderr.strip()[-200:])
            raise CollectError(proc.stderr.strip()[-300:])
        return proc.stdout

    def _flat(self, url: str, limit: Optional[int] = None) -> Dict[str, Any]:
        return json.loads(self._run(["--flat-playlist", "-J"] + (["--playlist-end", str(limit)] if limit else []) + [url]))

    def list_channel(self, channel: str) -> Dict[str, Any]:
        url = "https://www.youtube.com/%s/shorts" % channel if channel.startswith("@") else "https://www.youtube.com/channel/%s/shorts" % channel
        data = self._flat(url)
        return {"channel": data.get("channel") or data.get("uploader"), "channel_id": data.get("channel_id"),
                "videos": [entry for entry in data.get("entries") or [] if entry.get("id")]}

    def list_hashtag(self, tag: str, limit: int) -> List[Dict[str, Any]]:
        return [entry for entry in self._flat("https://www.youtube.com/hashtag/%s/shorts" % tag, limit).get("entries") or [] if entry.get("id")]

    def search(self, query: str, limit: int) -> List[Dict[str, Any]]:
        entries = self._flat("ytsearch%d:%s" % (limit, query)).get("entries") or []
        return [entry for entry in entries if entry.get("id") and entry.get("duration") and entry["duration"] <= MAX_SECONDS]

    def fetch(self, video: str, audio_dir: Path) -> Optional[Dict[str, Any]]:
        audio_dir.mkdir(parents=True, exist_ok=True)
        out = self._run(["--no-progress", "--no-playlist", "--print-json", "--retries", "3",
                         "--match-filter", "duration <= %d & !is_live" % MAX_SECONDS,
                         "-f", "ba[ext=m4a][language^=en]/ba[ext=m4a]/ba", "-o", str(audio_dir / "%(id)s.%(ext)s"),
                         "https://www.youtube.com/watch?v=%s" % video])
        line = out.strip().splitlines()[-1] if out.strip() else ""
        return json.loads(line) if line.startswith("{") else None

    def counts(self, video: str) -> Dict[str, Any]:
        return json.loads(self._run(["--skip-download", "--no-playlist", "-J", "https://www.youtube.com/watch?v=%s" % video]))


ROUTES = {"yt-dlp": YtDlp}


def get(route: str) -> Collector:
    if route not in ROUTES:
        raise CollectError("unknown collection route %r (have: %s)" % (route, ", ".join(sorted(ROUTES))))
    return ROUTES[route]()
