"""Vision stage: one interface for asking a vision model about images.

Usage (library): vision.ask(provider, model, system, prompt, [Path, ...], prices={...})

Providers are "gemini" (GEMINI_API_KEY), "anthropic" (ANTHROPIC_API_KEY) and "manual" (no network:
the request is written to <manual_dir>/<id>.request.json and a person or agent writes
<id>.response.json next to it; until then ask() raises Pending). API keys are read from the
environment at call time, never printed or written anywhere, and scrubbed from error messages.
Model ids are always passed in by the caller; list_models() shows what a provider offers.

Cost comes from the caller's price table ({"model-id-or-prefix": [usd_per_million_in, usd_per_million_out]},
longest prefix wins). A model missing from the table is charged a conservative default and the
result carries "price_assumed": true. Budget stops a run once its spend reaches a ceiling.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
ANTHROPIC_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
KEY_VARIABLES = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
DEFAULT_PRICE = (5.0, 25.0)
RETRIES = 3
BACKOFF_SECONDS = 2.0
ERROR_BODY_CHARS = 300


class VisionError(RuntimeError):
    """Raised for failures the operator can act on."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class BudgetExceeded(RuntimeError):
    """Raised when the spend ceiling has been reached."""


class Pending(Exception):
    """Raised by the manual provider while no response file exists."""

    def __init__(self, request_id: str, path: Path) -> None:
        super().__init__("waiting for a response to %s: write %s" % (path, path.with_name(request_id + ".response.json")))
        self.request_id = request_id
        self.path = path


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _scrub(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _send(request: urllib.request.Request, timeout: float) -> Tuple[int, str]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        return 0, "network error: %s" % exc


def _post_json(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: float) -> Tuple[int, str]:
    """POST a JSON body and return (status, response text); status 0 means the network failed."""
    merged = dict(headers)
    merged["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=merged, method="POST")
    return _send(request, timeout)


def _get_json(url: str, headers: Dict[str, str], timeout: float) -> Tuple[int, str]:
    return _send(urllib.request.Request(url, headers=dict(headers), method="GET"), timeout)


def _call(send: Callable[[], Tuple[int, str]], secrets: Sequence[str], what: str) -> Dict[str, Any]:
    """Run one request with retries on 429, 5xx and network failures; return the parsed JSON object."""
    status, text = 0, ""
    for attempt in range(RETRIES + 1):
        status, text = send()
        if 200 <= status < 300:
            break
        retryable = status == 429 or status >= 500 or status == 0
        if not retryable or attempt == RETRIES:
            raise VisionError("%s failed with HTTP %d: %s" % (what, status, _scrub(text, secrets)[:ERROR_BODY_CHARS]), status)
        _sleep(BACKOFF_SECONDS * (2 ** attempt))
    try:
        data = json.loads(text)
    except ValueError:
        raise VisionError("%s returned a body that is not JSON: %s" % (what, _scrub(text, secrets)[:ERROR_BODY_CHARS]), status)
    if not isinstance(data, dict):
        raise VisionError("%s returned JSON that is not an object" % what, status)
    return data


def _key(provider: str) -> str:
    name = KEY_VARIABLES[provider]
    value = os.environ.get(name)
    if not value:
        raise VisionError("%s is not set in the environment" % name)
    return value


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Parse the first JSON object in a model reply, tolerating code fences and leading prose."""
    text = text.strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            data, _ = decoder.raw_decode(text[start:])
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(data, dict):
            return data
        start = text.find("{", start + 1)
    return None


def price_for(model: str, prices: Optional[Dict[str, Sequence[float]]]) -> Tuple[float, float, bool]:
    """USD per million input and output tokens for a model, and whether the default had to be assumed."""
    table = prices or {}
    best = ""
    for prefix in table:
        if model.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    if not best:
        return DEFAULT_PRICE[0], DEFAULT_PRICE[1], True
    return float(table[best][0]), float(table[best][1]), False


def _mime(path: Path, data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    raise VisionError("%s is not a JPEG or PNG image" % path)


def _encode_images(images: Sequence[Path]) -> List[Tuple[str, str]]:
    encoded = []
    for path in images:
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            raise VisionError("cannot read image %s: %s" % (path, exc))
        encoded.append((_mime(Path(path), data), base64.b64encode(data).decode("ascii")))
    return encoded


def _gemini(model: str, system: str, prompt: str, images: Sequence[Path], max_output_tokens: int, timeout: float) -> Dict[str, Any]:
    key = _key("gemini")
    parts: List[Dict[str, Any]] = [{"inline_data": {"mime_type": mime, "data": data}} for mime, data in _encode_images(images)]
    parts.append({"text": prompt})
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"maxOutputTokens": max_output_tokens, "responseMimeType": "application/json", "temperature": 0.2},
    }
    url = "%s/models/%s:generateContent" % (GEMINI_BASE, model.replace("models/", "", 1))
    data = _call(lambda: _post_json(url, {"x-goog-api-key": key}, body, timeout), [key], "gemini request")
    candidates = data.get("candidates") or []
    if not candidates:
        feedback = json.dumps(data.get("promptFeedback", {}))
        raise VisionError("gemini returned no candidates (%s)" % _scrub(feedback, [key])[:ERROR_BODY_CHARS])
    reply = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in reply if not p.get("thought"))
    usage = data.get("usageMetadata") or {}
    return {
        "text": text,
        "input_tokens": int(usage.get("promptTokenCount", 0)),
        "output_tokens": int(usage.get("candidatesTokenCount", 0)) + int(usage.get("thoughtsTokenCount", 0)),
    }


def _anthropic(model: str, system: str, prompt: str, images: Sequence[Path], max_output_tokens: int, timeout: float) -> Dict[str, Any]:
    key = _key("anthropic")
    content: List[Dict[str, Any]] = [
        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}} for mime, data in _encode_images(images)]
    content.append({"type": "text", "text": prompt})
    body = {"model": model, "max_tokens": max_output_tokens, "system": system, "messages": [{"role": "user", "content": content}]}
    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    data = _call(lambda: _post_json(ANTHROPIC_BASE + "/messages", headers, body, timeout), [key], "anthropic request")
    text = "".join(block.get("text", "") for block in data.get("content") or [] if block.get("type") == "text")
    usage = data.get("usage") or {}
    return {"text": text, "input_tokens": int(usage.get("input_tokens", 0)), "output_tokens": int(usage.get("output_tokens", 0))}


def _manual_id(system: str, prompt: str, images: Sequence[Path]) -> str:
    digest = hashlib.sha1()
    digest.update(system.encode("utf-8"))
    digest.update(prompt.encode("utf-8"))
    for path in images:
        try:
            digest.update(Path(path).read_bytes())
        except OSError:
            digest.update(str(path).encode("utf-8"))
    return digest.hexdigest()[:12]


def _manual(system: str, prompt: str, images: Sequence[Path], manual_dir: Optional[Path], manual_id: Optional[str]) -> Dict[str, Any]:
    if manual_dir is None:
        raise VisionError("the manual provider needs a manual_dir")
    directory = Path(manual_dir)
    request_id = manual_id or _manual_id(system, prompt, images)
    request_path = directory / (request_id + ".request.json")
    response_path = directory / (request_id + ".response.json")
    if not response_path.is_file():
        directory.mkdir(parents=True, exist_ok=True)
        request = {"id": request_id, "system": system, "prompt": prompt, "images": [str(p) for p in images],
                   "response_file": str(response_path)}
        request_path.write_text(json.dumps(request, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        raise Pending(request_id, request_path)
    raw = response_path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except ValueError:
        return {"text": raw, "input_tokens": 0, "output_tokens": 0, "json": extract_json(raw)}
    if not isinstance(data, dict):
        raise VisionError("%s must contain a JSON object" % response_path)
    if isinstance(data.get("text"), str):
        parsed = data["json"] if isinstance(data.get("json"), dict) else extract_json(data["text"])
        return {"text": data["text"], "input_tokens": 0, "output_tokens": 0, "json": parsed}
    return {"text": raw, "input_tokens": 0, "output_tokens": 0, "json": data}


def ask(provider: str, model: str, system: str, prompt: str, images: Sequence[Path], *, max_output_tokens: int = 1500,
        timeout: float = 120, prices: Optional[Dict[str, Sequence[float]]] = None, manual_dir: Optional[Path] = None,
        manual_id: Optional[str] = None) -> Dict[str, Any]:
    """Ask one vision question about images; returns text, parsed json, usage, cost_usd, model and provider."""
    if provider == "manual":
        raw = _manual(system, prompt, images, manual_dir, manual_id)
    elif provider == "gemini":
        raw = _gemini(model, system, prompt, images, max_output_tokens, timeout)
    elif provider == "anthropic":
        raw = _anthropic(model, system, prompt, images, max_output_tokens, timeout)
    else:
        raise VisionError("unknown provider %r (use gemini, anthropic or manual)" % provider)
    rate_in, rate_out, assumed = price_for(model, prices)
    cost = 0.0 if provider == "manual" else (raw["input_tokens"] * rate_in + raw["output_tokens"] * rate_out) / 1e6
    return {
        "text": raw["text"],
        "json": raw["json"] if "json" in raw else extract_json(raw["text"]),
        "usage": {"input_tokens": raw["input_tokens"], "output_tokens": raw["output_tokens"]},
        "cost_usd": round(cost, 6),
        "model": model,
        "provider": provider,
        "price_assumed": assumed and provider != "manual",
    }


def list_models(provider: str, timeout: float = 30) -> List[str]:
    """Model ids the provider offers, so the operator can pick one."""
    if provider not in KEY_VARIABLES:
        raise VisionError("unknown provider %r (use gemini or anthropic)" % provider)
    key = _key(provider)
    if provider == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
        data = _call(lambda: _get_json(ANTHROPIC_BASE + "/models?limit=1000", headers, timeout), [key], "anthropic model list")
        return [str(entry.get("id", "")) for entry in data.get("data") or []]
    ids: List[str] = []
    token = ""
    while True:
        url = "%s/models?pageSize=200%s" % (GEMINI_BASE, "&pageToken=" + token if token else "")
        data = _call(lambda: _get_json(url, {"x-goog-api-key": key}, timeout), [key], "gemini model list")
        for entry in data.get("models") or []:
            if "generateContent" in (entry.get("supportedGenerationMethods") or ["generateContent"]):
                ids.append(str(entry.get("name", "")).replace("models/", "", 1))
        token = data.get("nextPageToken") or ""
        if not token:
            return ids


class Budget:
    """Running spend over a ceiling in USD; check() raises once the ceiling is reached."""

    def __init__(self, ceiling_usd: float) -> None:
        self.ceiling_usd = float(ceiling_usd)
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0
        self.stopped_by_ceiling = False

    def check(self) -> None:
        if self.cost_usd >= self.ceiling_usd:
            self.stopped_by_ceiling = True
            raise BudgetExceeded("spent $%.4f of the $%.2f ceiling" % (self.cost_usd, self.ceiling_usd))

    def add(self, result: Dict[str, Any]) -> None:
        self.calls += 1
        self.input_tokens += int(result["usage"]["input_tokens"])
        self.output_tokens += int(result["usage"]["output_tokens"])
        self.cost_usd += float(result["cost_usd"])

    def summary(self) -> Dict[str, Any]:
        return {"calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cost_usd": round(self.cost_usd, 6), "ceiling_usd": self.ceiling_usd,
                "stopped_by_ceiling": self.stopped_by_ceiling}
