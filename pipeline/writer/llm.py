"""One small model client for the writer stage: Anthropic, Gemini, OpenRouter or OpenAI over HTTPS.

Keys are read from the environment at call time and never written anywhere. Every call is appended
to a ledger file (provider, model, purpose, tokens, cost) and a Ledger refuses a call once its
ceiling is spent. Prices are US dollars per million tokens and can be overridden per call.
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

PRICES = {
    "claude-haiku-5-5": {"in": 0.10, "out": 0.50},
    "claude-sonnet-5-5": {"in": 2.0, "out": 10.0},
    "claude-opus-5-5": {"in": 4.0, "out": 20.0},
    "gemini-3.5-flash-lite": {"in": 0.30, "out": 2.50, "audio_in": 0.60},
    "gemini-3.1-flash-lite": {"in": 0.25, "out": 1.50, "audio_in": 0.50},
    "gemini-3.8-flash": {"in": 0.75, "out": 3.75, "audio_in": 1.00},
    "gpt-6-luna": {"in": 0.10, "out": 0.50},
    "gpt-6.2-sol": {"in": 2.0, "out": 10.0},
}
ENV_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY", "openrouter": "OPENROUTER_API_KEY", "openai": "OPENAI_API_KEY"}
DEFAULT_MODELS = {
    "anthropic": {"strong": "claude-sonnet-5-5", "cheap": "claude-haiku-5-5"},
    "gemini": {"strong": "gemini-3.8-flash", "cheap": "gemini-3.1-flash-lite"},
    "openrouter": {"strong": "anthropic/claude-sonnet-5.5", "cheap": "anthropic/claude-haiku-5.5"},
    "openai": {"strong": "gpt-6.2-sol", "cheap": "gpt-6-luna"},
}
_LOCK = threading.Lock()


class LLMError(RuntimeError):
    """A model call failed in a way the operator can act on."""


class BudgetError(LLMError):
    """The ledger's spend ceiling is reached."""


class Ledger(object):
    """Append-only spend log with a ceiling in dollars."""

    def __init__(self, path: Optional[Path] = None, ceiling: Optional[float] = None) -> None:
        self.path = Path(path) if path else None
        self.ceiling = ceiling
        self.session = 0.0
        self.calls = 0

    def spent(self) -> float:
        if self.path is None or not self.path.exists():
            return self.session
        total = 0.0
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    total += float(json.loads(line).get("cost_usd") or 0.0)
        return total

    def check(self) -> None:
        if self.ceiling is not None and self.spent() >= self.ceiling:
            raise BudgetError("spend ceiling of $%.2f reached ($%.4f spent)" % (self.ceiling, self.spent()))

    def add(self, row: Dict[str, Any]) -> None:
        with _LOCK:
            self.session += float(row.get("cost_usd") or 0.0)
            self.calls += 1
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row) + "\n")


class RunLedger(Ledger):
    """A ledger whose ceiling counts only this run, while every call is still written to the shared spend log."""

    def __init__(self, path: Optional[Path], run_ceiling: Optional[float]) -> None:
        Ledger.__init__(self, path, None)
        self.start = Ledger.spent(self)
        self.run_ceiling = run_ceiling

    def run_spent(self) -> float:
        return self.spent() - self.start

    def check(self) -> None:
        if self.run_ceiling is not None and self.run_spent() >= self.run_ceiling:
            raise BudgetError("this run's spend ceiling of $%.2f is reached" % self.run_ceiling)


def run_ledger(ceiling: Optional[float] = None) -> RunLedger:
    """A ledger for one run, logging to the file named by WRITER_LEDGER when it is set."""
    path = os.environ.get("WRITER_LEDGER")
    return RunLedger(Path(path) if path else None, ceiling)


def pick_provider(preferred: Optional[str] = None) -> str:
    if preferred:
        return preferred
    for name in ("anthropic", "openrouter", "gemini", "openai"):
        if os.environ.get(ENV_KEYS[name]):
            return name
    raise LLMError("no model key in the environment (looked for %s)" % ", ".join(sorted(ENV_KEYS.values())))


def cost_of(model: str, input_tokens: int, output_tokens: int, audio_tokens: int = 0, prices: Optional[Dict[str, Any]] = None) -> float:
    table = dict(PRICES)
    table.update(prices or {})
    key = model.split("/")[-1].replace("claude-haiku-5.5", "claude-haiku-5-5").replace("claude-sonnet-5.5", "claude-sonnet-5-5").replace("claude-opus-5.5", "claude-opus-5-5")
    price = table.get(model) or table.get(key)
    if price is None:
        raise LLMError("no price for model %r; pass prices={model: {'in': x, 'out': y}}" % model)
    text_tokens = input_tokens - audio_tokens
    return (text_tokens * price["in"] + audio_tokens * price.get("audio_in", price["in"]) + output_tokens * price["out"]) / 1e6


def _post(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    last = "no attempt made"
    for attempt in range(4):
        request = urllib.request.Request(url, data=data, headers=dict(headers, **{"Content-Type": "application/json"}), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            last = "HTTP %d: %s" % (exc.code, detail)
            if exc.code not in (408, 409, 429, 500, 502, 503, 504, 529):
                break
        except (urllib.error.URLError, OSError) as exc:
            last = str(exc)
        time.sleep(2.0 * (attempt + 1))
    raise LLMError("model request failed: " + last)


def _anthropic(key: str, model: str, system: Optional[str], prompt: str, max_tokens: int, timeout: float, effort: Optional[str]) -> Dict[str, Any]:
    body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
    if system:
        body["system"] = system
    if effort:
        body["output_config"] = {"effort": effort}
    reply = _post("https://api.anthropic.com/v1/messages", {"x-api-key": key, "anthropic-version": "2023-06-01"}, body, timeout)
    text = "".join(part.get("text", "") for part in reply.get("content", []) if part.get("type") == "text")
    usage = reply.get("usage", {})
    return {"text": text, "input_tokens": int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0)), "audio_tokens": 0, "truncated": reply.get("stop_reason") == "max_tokens"}


def _gemini(key: str, model: str, system: Optional[str], prompt: str, max_tokens: int, temperature: Optional[float], timeout: float,
            audio: Optional[Path], want_json: bool) -> Dict[str, Any]:
    parts = []
    if audio is not None:
        mime = {"m4a": "audio/mp4", "mp4": "audio/mp4", "webm": "audio/webm", "mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg", "opus": "audio/ogg"}
        with open(audio, "rb") as handle:
            parts.append({"inline_data": {"mime_type": mime.get(Path(audio).suffix.lstrip(".").lower(), "audio/mp4"),
                                          "data": base64.b64encode(handle.read()).decode("ascii")}})
    parts.append({"text": prompt})
    config = {"maxOutputTokens": max_tokens}
    if temperature is not None:
        config["temperature"] = temperature
    if want_json:
        config["responseMimeType"] = "application/json"
    body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": config}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    reply = _post("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model, {"x-goog-api-key": key}, body, timeout)
    candidates = reply.get("candidates") or [{}]
    text = "".join(part.get("text", "") for part in candidates[0].get("content", {}).get("parts", []) if not part.get("thought"))
    usage = reply.get("usageMetadata", {})
    audio_tokens = sum(int(item.get("tokenCount", 0)) for item in usage.get("promptTokensDetails", []) if item.get("modality") == "AUDIO")
    output = int(usage.get("candidatesTokenCount", 0)) + int(usage.get("thoughtsTokenCount", 0) or 0)
    return {"text": text, "input_tokens": int(usage.get("promptTokenCount", 0)), "output_tokens": output, "audio_tokens": audio_tokens}


def _chat(url: str, key: str, model: str, system: Optional[str], prompt: str, max_tokens: int, temperature: Optional[float], timeout: float) -> Dict[str, Any]:
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if temperature is not None:
        body["temperature"] = temperature
    reply = _post(url, {"Authorization": "Bearer " + key}, body, timeout)
    usage = reply.get("usage", {})
    result = {"text": reply["choices"][0]["message"].get("content") or "", "input_tokens": int(usage.get("prompt_tokens", 0)),
              "output_tokens": int(usage.get("completion_tokens", 0)), "audio_tokens": 0}
    if usage.get("cost") is not None:
        result["cost_usd"] = float(usage["cost"])
    return result


def complete(prompt: str, system: Optional[str] = None, provider: Optional[str] = None, model: Optional[str] = None, tier: str = "strong",
             max_tokens: int = 2000, temperature: Optional[float] = None, ledger: Optional[Ledger] = None, purpose: str = "",
             audio: Optional[Path] = None, want_json: bool = False, prices: Optional[Dict[str, Any]] = None, timeout: float = 180.0,
             effort: Optional[str] = None) -> Dict[str, Any]:
    """Send one prompt and return {"text", "input_tokens", "output_tokens", "cost_usd", "provider", "model"}.

    temperature is not sent to Anthropic, whose current models reject it. effort ("low", "medium", "high") is
    Anthropic's control over how much the model thinks before it answers, which is most of what a call costs;
    the other providers ignore it.
    """
    provider = pick_provider(provider)
    if provider not in ENV_KEYS:
        raise LLMError("unknown provider %r" % provider)
    key = os.environ.get(ENV_KEYS[provider])
    if not key:
        raise LLMError("%s is not set in the environment" % ENV_KEYS[provider])
    model = model or DEFAULT_MODELS[provider][tier]
    if audio is not None and provider != "gemini":
        raise LLMError("audio input needs the gemini provider")
    if ledger is not None:
        ledger.check()
    if provider == "anthropic":
        result = _anthropic(key, model, system, prompt, max_tokens, timeout, effort)
    elif provider == "gemini":
        result = _gemini(key, model, system, prompt, max_tokens, temperature, timeout, audio, want_json)
    elif provider == "openrouter":
        result = _chat("https://openrouter.ai/api/v1/chat/completions", key, model, system, prompt, max_tokens, temperature, timeout)
    else:
        result = _chat("https://api.openai.com/v1/chat/completions", key, model, system, prompt, max_tokens, temperature, timeout)
    if "cost_usd" not in result:
        result["cost_usd"] = cost_of(model, result["input_tokens"], result["output_tokens"], result["audio_tokens"], prices)
    result["cost_usd"] = round(result["cost_usd"], 6)
    result.update({"provider": provider, "model": model})
    if ledger is not None:
        ledger.add({"time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "provider": provider, "model": model,
                    "purpose": purpose, "input_tokens": result["input_tokens"], "output_tokens": result["output_tokens"],
                    "cost_usd": result["cost_usd"]})
    return result


def extract_json(text: str) -> Any:
    """The first JSON object or array in a model reply, tolerating code fences and prose around it."""
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    match = re.search(r"[\[{]", cleaned)
    if match:
        try:
            return json.JSONDecoder().raw_decode(cleaned[match.start():])[0]
        except ValueError:
            pass
    raise LLMError("the model reply holds no complete JSON: %r" % text[:200])


Client = Callable[..., Dict[str, Any]]
