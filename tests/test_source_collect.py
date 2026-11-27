from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest

from pipeline.source import collect


def post(i, created):
    return {"id": str(i), "created_utc": created}


def listing(rows, after=None):
    return {"kind": "Listing", "data": {"after": after, "children": [
        {"kind": "t3", "data": row} for row in rows]}}


def test_archive_resumes_boundary_second(monkeypatch):
    rows = [post(i, 1000 + i // 3) for i in range(105)]
    def request(req):
        params = parse_qs(urlsplit(req.full_url).query)
        after = int(params.get("after", ["0"])[0])
        return {"data": [r for r in rows if r["created_utc"] > after][:100]}
    monkeypatch.setattr(collect, "_request", request)
    monkeypatch.setattr(collect.time, "sleep", lambda _: None)
    first, resume = collect.archive_page("nosleep")
    second, done = collect.archive_page("nosleep", resume)
    assert resume == "1033"
    assert first + second == rows
    assert done is None


def test_archive_fails_instead_of_skipping_stalled_cursor(monkeypatch):
    monkeypatch.setattr(collect.time, "sleep", lambda _: None)
    monkeypatch.setattr(collect, "_request", lambda _: {"data": [post(i, 4) for i in range(100)]})
    with pytest.raises(collect.CollectionError, match="full second"):
        collect.archive_page("nosleep", "4")


def test_oauth_paging_refresh_and_inclusive_cutoff(monkeypatch):
    requests = []
    responses = iter([{"access_token": "example", "expires_in": 3600, "token_type": "bearer"},
                      listing([dict(post(0, 1), stickied=True), post(2, 51)], "t3_2"),
                      listing([post(3, 50), post(4, 49)], "t3_4"),
                      listing([dict(post(3, 50), score=17)])])
    def request(req):
        requests.append(req)
        return next(responses)
    monkeypatch.setattr(collect, "_request", request)
    monkeypatch.setattr(collect.time, "sleep", lambda _: None)
    client = collect.RedditClient("test", "example", "test-user-agent")
    assert client.new_page("nosleep", since=50) == ([post(2, 51)], "t3_2")
    assert client.new_page("nosleep", "t3_2", since=50) == ([post(3, 50)], None)
    assert client.refresh(["3", "4"]) == [dict(post(3, 50), score=17)]
    assert requests[0].data == b"grant_type=client_credentials"
    assert requests[0].get_header("Authorization").startswith("Basic ")
    assert requests[1].get_header("Authorization") == "Bearer example"
    assert requests[1].get_header("User-agent") == "test-user-agent"
    assert "after=t3_2" in requests[2].full_url
    assert parse_qs(urlsplit(requests[3].full_url).query)["id"] == ["t3_3,t3_4"]


def test_malformed_listing_never_means_deleted(monkeypatch):
    client = collect.RedditClient("test", "example", "test-user-agent")
    client._token, client._expires = "example", float("inf")
    monkeypatch.setattr(collect, "_request", lambda _: {"error": 403})
    with pytest.raises(collect.CollectionError, match="listing"):
        client.refresh(["123"])
    with pytest.raises(ValueError, match="100"):
        client.refresh([str(i) for i in range(101)])


def test_limit_waits_and_access_denial_does_not_retry(monkeypatch):
    delays = []
    errors = iter([(429, {"Retry-After": "10", "X-Ratelimit-Reset": "25"}), (403, {})])
    class Opener:
        def open(self, request, timeout):
            code, headers = next(errors)
            raise HTTPError(request.full_url, code, "", headers, None)
    monkeypatch.setattr(collect, "build_opener", lambda _: Opener())
    monkeypatch.setattr(collect.time, "sleep", delays.append)
    with pytest.raises(collect.CollectionError, match="403"):
        collect._request(Request("https://test.invalid"))
    assert delays == [25]
