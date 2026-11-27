"""Page readers for authorized access. Persist each cursor with its page."""
import base64
import json
import os
import re
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class CollectionError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _delay(value, default):
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0, dt.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return default


def _request(request):
    opener = build_opener(_NoRedirect())
    for attempt in range(4):
        try:
            with opener.open(request, timeout=45) as response:
                result = json.load(response)
                if float(response.headers.get("X-Ratelimit-Remaining", 100)) < 1:
                    time.sleep(_delay(response.headers.get("X-Ratelimit-Reset"), 60))
                return result
        except HTTPError as exc:
            headers, code = exc.headers, exc.code
            exc.close()
            if code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise CollectionError(f"Source returned HTTP {code}; checkpoint unchanged") from None
            delay = max(_delay(headers.get("Retry-After"), 2 ** attempt),
                        _delay(headers.get("X-Ratelimit-Reset"), 0) if code == 429 else 0)
            time.sleep(delay)


def _subreddit(name):
    if not re.fullmatch(r"[a-zA-Z0-9_]{1,21}", name):
        raise ValueError("Invalid subreddit name")
    return name


def archive_page(subreddit, cursor=None):
    """Return rows oldest first. Cursor is the first second not yet committed."""
    query = {"subreddit": _subreddit(subreddit), "sort": "asc", "limit": 100}
    if cursor is not None:
        query["after"] = int(cursor) - 1
    url = "https://arctic-shift.photon-reddit.com/api/posts/search?" + urlencode(query)
    time.sleep(1)
    rows = _request(Request(url, headers={"User-Agent": "social-automation:story-source:0.1"}))["data"]
    if not isinstance(rows, list):
        raise CollectionError("Archive did not return a page")
    seconds = [int(row["created_utc"]) for row in rows]
    if seconds != sorted(seconds):
        raise CollectionError("Archive returned an unordered page")
    if len(rows) < 100:
        return rows, None
    boundary = seconds[-1]
    if seconds[0] == boundary or (cursor is not None and boundary <= int(cursor)):
        raise CollectionError("Cannot page across a full second of archive posts; use an authorized dump")
    return [row for row in rows if int(row["created_utc"]) < boundary], str(boundary)


class RedditClient:
    """Approved application-only OAuth, for public listings and live score checks."""

    def __init__(self, client_id, client_secret, user_agent):
        if not all((client_id, client_secret, user_agent)):
            raise ValueError("Set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET and REDDIT_USER_AGENT for your approved app")
        self._basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self._user_agent = user_agent
        self._token = None
        self._expires = 0
        self._next_call = 0

    @classmethod
    def from_env(cls):
        return cls(*(os.environ.get(key) for key in
                     ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT")))

    def _get(self, path, query):
        if not self._token or time.monotonic() >= self._expires:
            token = _request(Request("https://www.reddit.com/api/v1/access_token",
                                     data=b"grant_type=client_credentials",
                                     headers={"Authorization": "Basic " + self._basic,
                                              "User-Agent": self._user_agent}))
            if not token.get("access_token") or token.get("token_type", "").lower() != "bearer":
                raise CollectionError("Reddit did not issue a bearer token")
            self._token = token["access_token"]
            self._expires = time.monotonic() + max(0, int(token["expires_in"]) - 60)
        time.sleep(max(0, self._next_call - time.monotonic()))
        self._next_call = time.monotonic() + 1
        query = dict(query, raw_json=1)
        result = _request(Request("https://oauth.reddit.com" + path + "?" + urlencode(query),
                                  headers={"Authorization": "Bearer " + self._token,
                                           "User-Agent": self._user_agent}))
        if result.get("kind") != "Listing" or not isinstance(result["data"]["children"], list):
            raise CollectionError("Reddit did not return a listing")
        data = result["data"]
        rows = []
        for child in data["children"]:
            if child.get("kind") != "t3" or not child.get("data", {}).get("id"):
                raise CollectionError("Unexpected item in Reddit submission listing")
            rows.append(child["data"])
        return rows, data["after"]

    def new_page(self, subreddit, cursor=None, since=0):
        query = {"limit": 100}
        if cursor:
            query["after"] = cursor
        rows, after = self._get("/r/" + _subreddit(subreddit) + "/new", query)
        if after and after == cursor:
            raise CollectionError("Reddit repeated the listing cursor")
        if any(int(row["created_utc"]) < since and not row.get("stickied") for row in rows):
            after = None
        return [row for row in rows if int(row["created_utc"]) >= since], after

    def refresh(self, ids):
        """Return live submissions. A requested ID absent on success is inaccessible."""
        ids = list(dict.fromkeys(ids))
        if not ids:
            return []
        if len(ids) > 100 or not all(re.fullmatch(r"[a-z0-9]+", key) for key in ids):
            raise ValueError("Pass at most 100 bare Reddit submission IDs")
        rows, _ = self._get("/api/info", {"id": ",".join("t3_" + key for key in ids)})
        if any(row["id"] not in ids for row in rows):
            raise CollectionError("Reddit returned a submission not requested")
        return rows
