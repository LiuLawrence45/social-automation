from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest

from pipeline import vision

PNG_RGB = (200, 30, 30)


def write_png(path: Path, width: int = 4, height: int = 6, rgb=PNG_RGB) -> Path:
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return path


@pytest.fixture
def image(tmp_path):
    return write_png(tmp_path / "a.png")


@pytest.fixture
def calls(monkeypatch):
    log = []
    replies = []

    def fake_post(url, headers, body, timeout):
        log.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        return replies.pop(0) if len(replies) > 1 else replies[0]

    monkeypatch.setattr(vision, "_post_json", fake_post)
    monkeypatch.setattr(vision, "_sleep", lambda seconds: None)
    return log, replies


def test_extract_json_handles_plain_fenced_and_prose():
    assert vision.extract_json('{"a": 1}') == {"a": 1}
    assert vision.extract_json('```json\n{"a": {"b": [1, 2]}}\n```') == {"a": {"b": [1, 2]}}
    assert vision.extract_json('Sure! Here is the result: {"a": "x}y"} hope it helps') == {"a": "x}y"}
    assert vision.extract_json("no braces here") is None
    assert vision.extract_json("[1, 2, 3]") is None
    assert vision.extract_json("{broken") is None


def test_price_for_longest_prefix_and_default():
    prices = {"gemini": [1.0, 2.0], "gemini-pro": [3.0, 4.0]}
    assert vision.price_for("gemini-pro-2", prices) == (3.0, 4.0, False)
    assert vision.price_for("gemini-flash", prices) == (1.0, 2.0, False)
    assert vision.price_for("mystery", prices) == (5.0, 25.0, True)
    assert vision.price_for("mystery", None) == (5.0, 25.0, True)


def test_budget_ceiling_and_summary():
    budget = vision.Budget(1.0)
    budget.check()
    budget.add({"usage": {"input_tokens": 100, "output_tokens": 10}, "cost_usd": 0.4})
    budget.check()
    budget.add({"usage": {"input_tokens": 50, "output_tokens": 5}, "cost_usd": 0.6})
    assert budget.summary() == {"calls": 2, "input_tokens": 150, "output_tokens": 15, "cost_usd": 1.0,
                                "ceiling_usd": 1.0, "stopped_by_ceiling": False}
    with pytest.raises(vision.BudgetExceeded):
        budget.check()
    assert budget.summary()["stopped_by_ceiling"] is True


def test_gemini_request_and_usage(monkeypatch, calls, image):
    log, replies = calls
    monkeypatch.setenv("GEMINI_API_KEY", "gem-secret")
    replies.append((200, json.dumps({
        "candidates": [{"content": {"parts": [{"text": "thinking", "thought": True}, {"text": '{"ok": true}'}]}}],
        "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 200, "thoughtsTokenCount": 300}})))
    result = vision.ask("gemini", "gemini-x", "SYS", "PROMPT", [image, image], max_output_tokens=900, timeout=7,
                        prices={"gemini-x": [1.0, 10.0]})
    call = log[0]
    assert call["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-x:generateContent"
    assert call["headers"] == {"x-goog-api-key": "gem-secret"}
    assert call["timeout"] == 7
    body = call["body"]
    assert body["systemInstruction"] == {"parts": [{"text": "SYS"}]}
    assert body["generationConfig"] == {"maxOutputTokens": 900, "responseMimeType": "application/json", "temperature": 0.2}
    parts = body["contents"][0]["parts"]
    assert body["contents"][0]["role"] == "user"
    assert [list(p) for p in parts] == [["inline_data"], ["inline_data"], ["text"]]
    assert parts[0]["inline_data"]["mime_type"] == "image/png"
    assert parts[-1] == {"text": "PROMPT"}
    assert result["text"] == '{"ok": true}' and result["json"] == {"ok": True}
    assert result["usage"] == {"input_tokens": 1000, "output_tokens": 500}
    assert result["cost_usd"] == pytest.approx((1000 * 1.0 + 500 * 10.0) / 1e6)
    assert result["model"] == "gemini-x" and result["provider"] == "gemini" and result["price_assumed"] is False


def test_anthropic_request_and_usage(monkeypatch, calls, image):
    log, replies = calls
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-secret")
    replies.append((200, json.dumps({"content": [{"type": "text", "text": 'Here: {"n": 2}'}], "usage": {"input_tokens": 10, "output_tokens": 4}})))
    result = vision.ask("anthropic", "claude-x", "SYS", "PROMPT", [image])
    call = log[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"] == {"x-api-key": "ant-secret", "anthropic-version": "2023-06-01"}
    body = call["body"]
    assert body["model"] == "claude-x" and body["max_tokens"] == 1500 and body["system"] == "SYS"
    content = body["messages"][0]["content"]
    assert body["messages"][0]["role"] == "user"
    assert content[0]["type"] == "image" and content[0]["source"]["type"] == "base64" and content[0]["source"]["media_type"] == "image/png"
    assert content[-1] == {"type": "text", "text": "PROMPT"}
    assert result["json"] == {"n": 2} and result["usage"] == {"input_tokens": 10, "output_tokens": 4}
    assert result["price_assumed"] is True
    assert result["cost_usd"] == pytest.approx((10 * 5.0 + 4 * 25.0) / 1e6)


def test_missing_key_names_only_the_variable(monkeypatch, image):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(vision.VisionError, match="GEMINI_API_KEY"):
        vision.ask("gemini", "m", "s", "p", [image])


def test_unknown_provider_and_bad_image(tmp_path):
    with pytest.raises(vision.VisionError, match="unknown provider"):
        vision.ask("nope", "m", "s", "p", [])
    bad = tmp_path / "x.png"
    bad.write_bytes(b"not an image")
    with pytest.raises(vision.VisionError, match="not a JPEG or PNG"):
        vision._encode_images([bad])


def test_retries_then_succeeds(monkeypatch, calls, image):
    log, replies = calls
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    ok = (200, json.dumps({"candidates": [{"content": {"parts": [{"text": "{}"}]}}], "usageMetadata": {}}))
    replies[:] = [(429, "slow down"), (503, "busy"), ok]
    result = vision.ask("gemini", "m", "s", "p", [image])
    assert len(log) == 3 and result["json"] == {}


def test_gives_up_after_three_retries_and_scrubs_the_key(monkeypatch, calls, image):
    log, replies = calls
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-very-secret")
    replies[:] = [(500, "sk-very-secret " + "x" * 600)]
    with pytest.raises(vision.VisionError) as info:
        vision.ask("anthropic", "m", "s", "p", [image])
    assert len(log) == 4
    assert info.value.status == 500 and "HTTP 500" in str(info.value)
    assert "sk-very-secret" not in str(info.value) and "[redacted]" in str(info.value)
    assert len(str(info.value).split("HTTP 500: ", 1)[1]) <= 300


def test_client_errors_are_not_retried(monkeypatch, calls, image):
    log, replies = calls
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    replies[:] = [(400, '{"error": "bad request"}')]
    with pytest.raises(vision.VisionError, match="HTTP 400"):
        vision.ask("gemini", "m", "s", "p", [image])
    assert len(log) == 1


def test_manual_provider_round_trip(tmp_path, image):
    manual = tmp_path / "manual"
    with pytest.raises(vision.Pending) as info:
        vision.ask("manual", "human", "SYS", "PROMPT", [image], manual_dir=manual, manual_id="s01_r0")
    assert info.value.request_id == "s01_r0"
    request = json.loads((manual / "s01_r0.request.json").read_text())
    assert request["system"] == "SYS" and request["prompt"] == "PROMPT" and request["images"] == [str(image)]
    (manual / "s01_r0.response.json").write_text(json.dumps({"candidates": [{"label": "A"}]}))
    result = vision.ask("manual", "human", "SYS", "PROMPT", [image], manual_dir=manual, manual_id="s01_r0")
    assert result["json"] == {"candidates": [{"label": "A"}]}
    assert result["usage"] == {"input_tokens": 0, "output_tokens": 0} and result["cost_usd"] == 0.0
    assert result["provider"] == "manual"


def test_manual_id_defaults_to_a_stable_hash(tmp_path, image):
    manual = tmp_path / "manual"
    ids = set()
    for _ in range(2):
        with pytest.raises(vision.Pending) as info:
            vision.ask("manual", "m", "s", "p", [image], manual_dir=manual)
        ids.add(info.value.request_id)
    assert len(ids) == 1


def test_list_models(monkeypatch):
    seen = []
    pages = {
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200": {
            "models": [{"name": "models/g-1", "supportedGenerationMethods": ["generateContent"]},
                       {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]}], "nextPageToken": "t2"},
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200&pageToken=t2": {
            "models": [{"name": "models/g-2", "supportedGenerationMethods": ["generateContent"]}]},
        "https://api.anthropic.com/v1/models?limit=1000": {"data": [{"id": "c-1"}, {"id": "c-2"}]},
    }

    def fake_get(url, headers, timeout):
        seen.append((url, headers))
        return 200, json.dumps(pages[url])

    monkeypatch.setattr(vision, "_get_json", fake_get)
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    assert vision.list_models("gemini") == ["g-1", "g-2"]
    assert vision.list_models("anthropic") == ["c-1", "c-2"]
    assert seen[0][1] == {"x-goog-api-key": "g"}
    assert seen[-1][1] == {"x-api-key": "a", "anthropic-version": "2023-06-01"}
    with pytest.raises(vision.VisionError):
        vision.list_models("manual")
