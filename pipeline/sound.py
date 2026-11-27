"""Sound stage: bed.wav from sound.json (a music score, ambience loops, one-shot cues, an optional synthetic pad).

Usage: python -m pipeline.sound --job <folder> [--provider elevenlabs|synthetic] [--refresh NAME ...] [--dry-run]

<job>/sound.json lists "ambience" (looped under a span of the video) and "cues" (one-shots at a moment),
each with a text prompt, plus an optional "pad" (pipeline.bed) and "prompt_influence". "music" is one
generated instrumental score for the whole bed: {"prompt", "gain_db", "duck_db", "cuts": [{"at", "seconds"}],
"fallback": mood}; it drops by duck_db under every cue, goes silent at each cut and falls back to the
synthetic pad when the music endpoint refuses. "normalise": true levels every clip before its gain_db
(ambience and music to -20 dBFS RMS, cues to a -6 dBFS peak), so gains mean the same for every sound. Moments are
seconds, {"time": s}, {"shot": id, "offset": s} or {"word": index, "offset": s}, resolved against
timeline.json (shot starts) and words.json (word starts, narration duration). The bed is the narration
duration plus half a second, written as a 48 kHz mono 16-bit WAV with its sample peak at -3 dBFS; the
edit stage loops or trims it and sets it under the voice (python -m pipeline.edit --music bed.wav).

ElevenLabs (the sound-generation endpoint, key in ELEVENLABS_API_KEY, read at call time and never
written anywhere) makes the sounds; the "synthetic" provider makes deterministic stand-ins with the
standard library so the stage runs with no network and no key. Every sound is cached as
<job>/sfx/<hash>.wav next to a .json holding its prompt, so re-runs bill nothing; --refresh NAME makes
one ambience or cue again. The run is described in <job>/sound_report.json.
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import bed
from .edit import ffmpeg_bin

RATE = 48000
KEY_VAR = "ELEVENLABS_API_KEY"
URL = "https://api.elevenlabs.io/v1/sound-generation"
MUSIC_URL = "https://api.elevenlabs.io/v1/music"
MUSIC_MIN_SECONDS = 10.0
DUCK_HOLD = 1.5
DUCK_ATTACK, DUCK_RELEASE = 0.08, 0.5
CUT_RETURN = 0.3
LEVEL_RMS, LEVEL_PEAK = 0.1, 0.5
MIN_SECONDS = 0.5
MAX_SECONDS = 22.0
TAIL_SECONDS = 0.5
PEAK_DBFS = -3.0
RETRIES = 3
CUE_ATTACK = 240
DEFAULT_INFLUENCE = 0.3
PROVIDERS = ("elevenlabs", "synthetic")


def scrub(text: str) -> str:
    """Hide the API key if it appears in text."""
    key = os.environ.get(KEY_VAR, "")
    return text.replace(key, "***") if len(key) >= 4 else text


class SoundError(RuntimeError):
    """Raised for failures the operator can act on; the message never holds the API key."""

    def __init__(self, message: str) -> None:
        super().__init__(scrub(message))


@dataclass
class Item:
    """One scheduled sound. For a cue, fade is the fade-out; for ambience it is the fade in and out."""

    name: str
    kind: str
    prompt: str
    seconds: float
    gain_db: float
    start: float
    end: float
    loop: bool = True
    fade: float = 0.0
    key: str = ""


@dataclass
class Plan:
    items: List[Item]
    total: float
    influence: float
    pad: Optional[Dict[str, Any]] = None
    music: Optional[Dict[str, Any]] = None
    normalise: bool = False


def pause(seconds: float) -> None:
    time.sleep(seconds)


def http_post(url: str, headers: Dict[str, str], body: bytes, timeout: float = 180.0) -> Tuple[int, Dict[str, str], bytes]:
    """The one network call: returns (status, response headers, body), including for HTTP errors."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()
    except OSError as exc:
        raise SoundError("network error calling ElevenLabs: %s" % exc)


def _load_json(path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise SoundError("%s not found" % path)
    except ValueError as exc:
        raise SoundError("%s is not valid JSON: %s" % (path, exc))


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SoundError("%s must be a number, got %r" % (where, value))
    return float(value)


class Timing:
    """Narration duration, word starts and shot starts of a job."""

    def __init__(self, job: Path) -> None:
        words_path = job / "words.json"
        if not words_path.is_file():
            raise SoundError("%s not found; run python -m pipeline.voice --job %s first" % (words_path, job))
        words = _load_json(words_path)
        try:
            self.duration = float(words["duration"])
            self.word_starts = [float(word["start"]) for word in words["words"]]
        except (KeyError, TypeError, ValueError):
            raise SoundError("%s needs a numeric 'duration' and a 'words' list with a 'start' on each word" % words_path)
        self.shots = None  # type: Optional[Dict[str, float]]
        self.timeline_path = job / "timeline.json"
        if self.timeline_path.is_file():
            timeline = _load_json(self.timeline_path)
            try:
                self.shots = {str(shot["id"]): float(shot["start"]) for shot in timeline["shots"]}
            except (KeyError, TypeError, ValueError):
                raise SoundError("%s needs a 'shots' list with an 'id' and a 'start' on each shot" % self.timeline_path)

    @property
    def total(self) -> float:
        return self.duration + TAIL_SECONDS

    def resolve(self, spec: Any, where: str) -> float:
        """Seconds for plain seconds, {"time"}, {"shot", "offset"} or {"word", "offset"}."""
        if isinstance(spec, (int, float)) and not isinstance(spec, bool):
            return _number(spec, where)
        anchors = [name for name in ("shot", "word", "time") if isinstance(spec, dict) and name in spec]
        if len(anchors) != 1 or set(spec) - {"shot", "word", "time", "offset"}:
            raise SoundError('%s must be seconds, {"time": s}, {"shot": id, "offset": s} or {"word": index, "offset": s}, got %s'
                             % (where, json.dumps(spec)))
        offset = _number(spec.get("offset", 0), where + ".offset")
        anchor = anchors[0]
        if anchor == "time":
            return _number(spec["time"], where + ".time") + offset
        if anchor == "word":
            index = spec["word"]
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(self.word_starts):
                raise SoundError("%s: word index %r is outside words.json (0 to %d)" % (where, index, len(self.word_starts) - 1))
            return self.word_starts[index] + offset
        shot = spec["shot"]
        if self.shots is None:
            raise SoundError("%s refers to shot %r but %s does not exist; run python -m pipeline.story timeline" %
                             (where, shot, self.timeline_path))
        if not isinstance(shot, str) or shot not in self.shots:
            raise SoundError("%s: unknown shot id %r (timeline.json has: %s)" % (where, shot, ", ".join(self.shots) or "none"))
        return self.shots[shot] + offset


def load_config(job: Path) -> Dict[str, Any]:
    path = job / "sound.json"
    if not path.is_file():
        raise SoundError("%s not found; describe the ambience and cues there" % path)
    config = _load_json(path)
    if not isinstance(config, dict):
        raise SoundError("%s must hold a JSON object" % path)
    return config


def clamp_seconds(seconds: float) -> float:
    return min(MAX_SECONDS, max(MIN_SECONDS, seconds))


def _entry(raw: Any, kind: str, index: int, seen: set) -> Tuple[Dict[str, Any], str, str, str]:
    if not isinstance(raw, dict):
        raise SoundError("%s[%d] must be an object" % (kind, index))
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SoundError("%s[%d] needs a non-empty 'name'" % (kind, index))
    where = "%s %r" % (kind, name)
    if name in seen:
        raise SoundError("%s: the name is used twice; names must be unique across ambience and cues" % where)
    seen.add(name)
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise SoundError("%s needs a non-empty 'prompt' describing the sound" % where)
    return raw, name, prompt.strip(), where


def build_plan(config: Dict[str, Any], timing: Timing) -> Plan:
    """Validate sound.json and resolve every time against the narration."""
    total = timing.total
    influence = _number(config.get("prompt_influence", DEFAULT_INFLUENCE), "prompt_influence")
    if not 0.0 <= influence <= 1.0:
        raise SoundError("prompt_influence must be between 0 and 1, got %s" % influence)
    items = []  # type: List[Item]
    seen = set()  # type: set
    for kind, key in (("ambience", "ambience"), ("cue", "cues")):
        entries = config.get(key, [])
        if not isinstance(entries, list):
            raise SoundError("'%s' must be a list" % key)
        for index, raw in enumerate(entries):
            raw, name, prompt, where = _entry(raw, kind, index, seen)
            seconds = _number(raw.get("seconds", 10.0 if kind == "ambience" else 2.0), where + ".seconds")
            if seconds <= 0:
                raise SoundError("%s: seconds must be positive" % where)
            seconds = clamp_seconds(seconds)
            gain = _number(raw.get("gain_db", 0.0), where + ".gain_db")
            if kind == "cue":
                if "at" not in raw:
                    raise SoundError('%s needs an "at" time such as {"shot": "c1", "offset": 0.0}' % where)
                start = timing.resolve(raw["at"], where + ".at")
                if start < 0 or start >= total:
                    raise SoundError("%s starts at %.2f s, outside the bed (0 to %.2f s); move it or check words.json" % (where, start, total))
                fade = _number(raw.get("fade_out", 0.05), where + ".fade_out")
                items.append(Item(name, kind, prompt, seconds, gain, start, min(total, start + seconds), False, max(fade, 0.0)))
                continue
            start = timing.resolve(raw.get("from", 0.0), where + ".from")
            end = total if raw.get("to") is None else timing.resolve(raw["to"], where + ".to")
            if start < 0 or start >= total:
                raise SoundError("%s starts at %.2f s, outside the bed (0 to %.2f s)" % (where, start, total))
            end = min(end, total)
            if end <= start:
                raise SoundError("%s ends at %.2f s, not after its start at %.2f s" % (where, end, start))
            loop = raw.get("loop", True)
            if not isinstance(loop, bool):
                raise SoundError("%s: loop must be true or false" % where)
            fade = _number(raw.get("fade", 0.5), where + ".fade")
            items.append(Item(name, kind, prompt, seconds, gain, start, end if loop else min(end, start + seconds), loop, max(fade, 0.0)))
    pad = config.get("pad")
    if pad is not None:
        if not isinstance(pad, dict) or pad.get("mood") not in bed.MOODS:
            raise SoundError("pad needs a 'mood' of %s" % " or ".join(sorted(bed.MOODS)))
        pad = {"mood": pad["mood"], "gain_db": _number(pad.get("gain_db", -14.0), "pad.gain_db")}
    music = config.get("music")
    if music is not None:
        if not isinstance(music, dict) or not str(music.get("prompt") or "").strip():
            raise SoundError("music needs a non-empty 'prompt' describing the score")
        if music.get("fallback", "sad") not in bed.MOODS:
            raise SoundError("music.fallback must be %s" % " or ".join(sorted(bed.MOODS)))
        cuts = []
        for index, cut in enumerate(music.get("cuts", [])):
            if not isinstance(cut, dict) or "at" not in cut:
                raise SoundError('music.cuts[%d] needs an "at" time' % index)
            cuts.append([timing.resolve(cut["at"], "music.cuts[%d].at" % index), _number(cut.get("seconds", 1.0), "music.cuts[%d].seconds" % index)])
        music = {"prompt": str(music["prompt"]).strip(), "gain_db": _number(music.get("gain_db", 0.0), "music.gain_db"),
                 "duck_db": _number(music.get("duck_db", -8.0), "music.duck_db"), "cuts": cuts,
                 "fade_in": _number(music.get("fade_in", 1.0), "music.fade_in"), "fade_out": _number(music.get("fade_out", 0.3), "music.fade_out"),
                 "fallback": music.get("fallback", "sad")}
    if not items and pad is None and music is None:
        raise SoundError("sound.json has no music, ambience, cues or pad, so there is nothing to mix")
    return Plan(items, total, influence, pad, music, config.get("normalise") is True)


def cache_key(provider: str, prompt: str, seconds: float, influence: float) -> str:
    text = "%s|%s|%.3f|%.3f" % (provider, prompt, clamp_seconds(seconds), influence)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def write_wav(path: Path, samples: Sequence[int]) -> None:
    data = array.array("h", samples)
    if sys.byteorder == "big":
        data.byteswap()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(data.tobytes())


def read_wav(path: Path) -> List[float]:
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != RATE or handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise SoundError("%s must be 48 kHz mono 16-bit" % path)
        samples = array.array("h")
        samples.frombytes(handle.readframes(handle.getnframes()))
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        raise SoundError("%s holds no audio" % path)
    return [float(value) for value in samples]


def _normalised(values: List[float], peak: float = 0.5) -> List[int]:
    top = max(abs(value) for value in values) or 1.0
    return [int(round(value / top * peak * 32767)) for value in values]


def synthetic_samples(prompt: str, seconds: float, kind: str) -> List[int]:
    """Deterministic stand-in: low-passed noise with a drone for ambience, a decaying tone burst for a cue."""
    rng = random.Random(int(hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12], 16))
    count = int(clamp_seconds(seconds) * RATE)
    two_pi = 2 * math.pi
    values = []  # type: List[float]
    if kind == "ambience":
        coefficient = 1 - math.exp(-two_pi * rng.uniform(200, 1200) / RATE)
        drone = two_pi * rng.uniform(45, 110) / RATE
        slow = two_pi * rng.uniform(0.05, 0.25) / RATE
        phase = rng.uniform(0, two_pi)
        state = 0.0
        for index in range(count):
            state += coefficient * (rng.uniform(-1, 1) - state)
            values.append(state * 3.0 * (0.8 + 0.2 * math.sin(slow * index + phase)) + 0.15 * math.sin(drone * index))
        return _normalised(values)
    base = two_pi * rng.uniform(180, 900) / RATE
    decay = max(clamp_seconds(seconds) / 4.0, 0.05) * RATE
    release = int(0.02 * RATE)
    for index in range(count):
        envelope = math.exp(-index / decay) * min(1.0, index / 96.0) * min(1.0, (count - index) / release)
        tone = math.sin(base * index) + 0.5 * math.sin(2.01 * base * index) + 0.25 * math.sin(3.5 * base * index)
        burst = rng.uniform(-1, 1) * math.exp(-index / (0.01 * RATE))
        values.append((tone + 1.5 * burst) * envelope)
    return _normalised(values)


def _run_ffmpeg(args: List[str]) -> None:
    try:
        proc = subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y"] + args,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    except FileNotFoundError:
        raise SoundError("cannot run FFmpeg: not found (set FFMPEG or add FFmpeg to PATH)")
    if proc.returncode != 0:
        raise SoundError("FFmpeg could not convert the sound (%d): %s" % (proc.returncode, proc.stderr[-600:]))


def _elevenlabs(prompt: str, seconds: float, influence: float, target: Path) -> Optional[str]:
    key = os.environ.get(KEY_VAR)
    if not key:
        raise SoundError("%s is not set in the environment" % KEY_VAR)
    body = json.dumps({"text": prompt, "duration_seconds": round(seconds, 3), "prompt_influence": influence}).encode("utf-8")
    headers = {"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"}
    status, response_headers, data = 0, {}, b""
    for attempt in range(RETRIES + 1):
        status, response_headers, data = http_post(URL, headers, body)
        if (status == 429 or status >= 500) and attempt < RETRIES:
            pause(2.0 ** attempt)
            continue
        break
    if status != 200:
        detail = data[:300].decode("utf-8", "replace")
        raise SoundError("ElevenLabs sound generation failed with HTTP %d: %s" % (status, detail))
    if not data:
        raise SoundError("ElevenLabs returned an empty response")
    cost = next((value for name, value in response_headers.items() if name.lower() == "character-cost"), None)
    source = target.with_name(target.name + ".src")
    source.write_bytes(data)
    try:
        _run_ffmpeg(["-i", str(source), "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", "-f", "wav", str(target)])
    finally:
        source.unlink()
    return None if cost is None else str(cost)


def _elevenlabs_music(prompt: str, seconds: float, target: Path) -> None:
    key = os.environ.get(KEY_VAR)
    if not key:
        raise SoundError("%s is not set in the environment" % KEY_VAR)
    body = json.dumps({"prompt": prompt, "music_length_ms": int(round(max(MUSIC_MIN_SECONDS, seconds) * 1000)),
                       "force_instrumental": True}).encode("utf-8")
    headers = {"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"}
    status, data = 0, b""
    for attempt in range(RETRIES + 1):
        status, _, data = http_post(MUSIC_URL, headers, body, timeout=600.0)
        if (status == 429 or status >= 500) and attempt < RETRIES:
            pause(2.0 ** attempt)
            continue
        break
    if status != 200 or not data:
        raise SoundError("ElevenLabs music generation failed with HTTP %d: %s" % (status, data[:300].decode("utf-8", "replace")))
    source = target.with_name(target.name + ".src")
    source.write_bytes(data)
    try:
        _run_ffmpeg(["-i", str(source), "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", "-f", "wav", str(target)])
    finally:
        source.unlink()


def obtain_music(job: Path, plan: Plan, provider: str, refresh: Sequence[str]) -> Tuple[List[float], Dict[str, Any]]:
    """The score as samples plus its report entry; the synthetic pad stands in when the endpoint refuses."""
    music = plan.music or {}
    text = "music|%s|%.1f" % (music["prompt"], plan.total)
    key = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    wav, sidecar = sound_paths(job, key)
    entry = {"name": "music", "kind": "music", "prompt": music["prompt"], "file": "sfx/%s.wav" % key, "cached": False,
             "start": 0.0, "end": round(plan.total, 3), "gain_db": music["gain_db"], "duck_db": music["duck_db"],
             "cuts": [[round(at, 3), seconds] for at, seconds in music["cuts"]], "source": "ElevenLabs music generation"}
    if provider == "elevenlabs" and wav.is_file() and "music" not in refresh:
        entry["cached"] = True
        return read_wav(wav), entry
    if provider == "elevenlabs":
        try:
            wav.parent.mkdir(parents=True, exist_ok=True)
            part = wav.with_name(wav.name + ".part")
            _elevenlabs_music(music["prompt"], plan.total, part)
            os.replace(str(part), str(wav))
            with open(sidecar, "w", encoding="utf-8") as handle:
                json.dump({"prompt": music["prompt"], "provider": "elevenlabs music", "seconds": round(plan.total, 3)}, handle, indent=1)
            return read_wav(wav), entry
        except SoundError as exc:
            entry["fallback_reason"] = str(exc)
    stereo = bed.render(plan.total, music["fallback"])
    entry.update({"source": "synthetic pad from pipeline.bed (%s)" % music["fallback"], "file": None})
    return [(stereo[2 * index] + stereo[2 * index + 1]) / 65536.0 for index in range(len(stereo) // 2)], entry


def level(clip: List[float], kind: str) -> List[float]:
    """Scale a clip to the common level for its kind."""
    if not clip:
        return clip
    if kind == "cue":
        measure, target = max(abs(value) for value in clip), LEVEL_PEAK
    else:
        measure, target = math.sqrt(sum(value * value for value in clip) / len(clip)), LEVEL_RMS
    return clip if measure < 1e-5 else [value * target / measure for value in clip]


def music_envelope(plan: Plan, count: int) -> List[float]:
    """Gain for the score over time: ducked under cues, silent in cuts, faded at both ends."""
    music = plan.music or {}
    envelope = [1.0] * count
    floor = 10 ** (music["duck_db"] / 20.0)

    def dip(start: float, end: float, low: float, attack: float, release: float) -> None:
        a, b = int(start * RATE), int(end * RATE)
        ramp_in, ramp_out = max(1, int(attack * RATE)), max(1, int(release * RATE))
        for index in range(max(0, a - ramp_in), min(count, b + ramp_out)):
            if index < a:
                value = 1.0 + (low - 1.0) * (index - (a - ramp_in)) / ramp_in
            elif index < b:
                value = low
            else:
                value = low + (1.0 - low) * (index - b) / ramp_out
            envelope[index] = min(envelope[index], value)

    for item in plan.items:
        if item.kind == "cue":
            dip(item.start, min(item.end, item.start + DUCK_HOLD), floor, DUCK_ATTACK, DUCK_RELEASE)
    for at, seconds in music["cuts"]:
        dip(at, at + seconds, 0.0, 0.005, CUT_RETURN)
    fade_edges(envelope, int(music["fade_in"] * RATE), int(music["fade_out"] * RATE))
    return envelope


def generate(prompt: str, seconds: float, target: Path, *, prompt_influence: float, provider: str, kind: str = "cue") -> Dict[str, Any]:
    """Make one sound as a 48 kHz mono 16-bit WAV at target; returns its length and the credit cost, if reported."""
    target = Path(target)
    seconds = clamp_seconds(seconds)
    target.parent.mkdir(parents=True, exist_ok=True)
    cost = None  # type: Optional[str]
    if provider == "synthetic":
        write_wav(target, synthetic_samples(prompt, seconds, kind))
    elif provider == "elevenlabs":
        cost = _elevenlabs(prompt, seconds, prompt_influence, target)
    else:
        raise SoundError("unknown provider %r (use one of %s)" % (provider, ", ".join(PROVIDERS)))
    with wave.open(str(target), "rb") as handle:
        duration = handle.getnframes() / float(handle.getframerate())
    return {"file": str(target), "seconds": duration, "cost": cost, "provider": provider}


def sound_paths(job: Path, key: str) -> Tuple[Path, Path]:
    return job / "sfx" / (key + ".wav"), job / "sfx" / (key + ".json")


def obtain(job: Path, item: Item, provider: str, influence: float, refresh: Sequence[str]) -> Tuple[bool, Optional[str]]:
    """Reuse the cached sound or generate it; returns (reused, cost billed by this call)."""
    wav, sidecar = sound_paths(job, item.key)
    if wav.is_file() and item.name not in refresh:
        return True, None
    part = wav.with_name(wav.name + ".part")
    result = generate(item.prompt, item.seconds, part, prompt_influence=influence, provider=provider, kind=item.kind)
    os.replace(str(part), str(wav))
    with open(sidecar, "w", encoding="utf-8") as handle:
        json.dump({"prompt": item.prompt, "provider": provider, "seconds": round(item.seconds, 3),
                   "prompt_influence": influence, "cost": result["cost"]}, handle, indent=1)
    return False, result["cost"]


def loop_clip(clip: List[float], length: int) -> List[float]:
    """Repeat a clip to the length, cross-fading each seam with equal power."""
    if len(clip) >= length:
        return clip[:length]
    fade = min(int(0.4 * RATE), len(clip) // 4)
    ramp = [(math.cos(0.5 * math.pi * step / fade), math.sin(0.5 * math.pi * step / fade)) for step in range(fade)]
    out = list(clip)
    while len(out) < length:
        base = len(out) - fade
        for step, (down, up) in enumerate(ramp):
            out[base + step] = out[base + step] * down + clip[step] * up
        out.extend(clip[fade:])
    return out[:length]


def fade_edges(samples: List[float], fade_in: int, fade_out: int) -> None:
    count = len(samples)
    fade_in, fade_out = min(fade_in, count // 2), min(fade_out, count // 2)
    for index in range(fade_in):
        samples[index] *= 0.5 - 0.5 * math.cos(math.pi * index / fade_in)
    for index in range(fade_out):
        samples[count - 1 - index] *= 0.5 - 0.5 * math.cos(math.pi * index / fade_out)


def layer(item: Item, clip: List[float], count: int) -> Tuple[int, List[float]]:
    """The item's samples (gain and fades applied) and the sample where they start."""
    start = int(round(item.start * RATE))
    gain = 10 ** (item.gain_db / 20.0)
    if item.kind == "ambience":
        length = min(count, int(round(item.end * RATE))) - start
        body = loop_clip(clip, length) if item.loop else clip[:length]
        fade_edges(body, int(item.fade * RATE), int(item.fade * RATE))
    else:
        body = clip[:count - start]
        fade_edges(body, CUE_ATTACK, int(item.fade * RATE))
    return start, [value * gain for value in body]


def mix(plan: Plan, clips: Dict[str, List[float]], score: Optional[List[float]] = None) -> array.array:
    """Sum every layer and scale the bed so its sample peak is at PEAK_DBFS."""
    count = int(round(plan.total * RATE))
    bus = [0.0] * count
    if score is not None and plan.music is not None:
        gain = 10 ** (plan.music["gain_db"] / 20.0)
        for index, shape in enumerate(music_envelope(plan, min(count, len(score)))):
            bus[index] += score[index] * shape * gain
    for item in plan.items:
        start, body = layer(item, clips[item.key], count)
        for index, value in enumerate(body):
            bus[start + index] += value
    if plan.pad is not None:
        stereo = bed.render(plan.total, plan.pad["mood"])
        gain = 10 ** (plan.pad["gain_db"] / 20.0) / 2.0
        for index in range(min(count, len(stereo) // 2)):
            bus[index] += (stereo[2 * index] + stereo[2 * index + 1]) * gain
    peak = max(abs(value) for value in bus)
    if peak < 1e-3:
        raise SoundError("the bed is silent; check the gains in sound.json")
    scale = round(32768 * 10 ** (PEAK_DBFS / 20.0)) / peak
    return array.array("h", (int(round(value * scale)) for value in bus))


def make_bed(job: Path, provider: str = "elevenlabs", refresh: Sequence[str] = (), dry_run: bool = False) -> Dict[str, Any]:
    """Run the whole sound stage for a job folder and return the report."""
    job = Path(job)
    started = time.time()
    if provider not in PROVIDERS:
        raise SoundError("unknown provider %r (use one of %s)" % (provider, ", ".join(PROVIDERS)))
    plan = build_plan(load_config(job), Timing(job))
    unknown = sorted(set(refresh) - {item.name for item in plan.items} - ({"music"} if plan.music else set()))
    if unknown:
        raise SoundError("--refresh names %s but sound.json has only: %s" % (", ".join(unknown), ", ".join(item.name for item in plan.items)))
    entries = []
    clips = {}  # type: Dict[str, List[float]]
    for item in plan.items:
        item.key = cache_key(provider, item.prompt, item.seconds, plan.influence)
        wav, _ = sound_paths(job, item.key)
        reused, cost = wav.is_file() and item.name not in refresh, None
        if not dry_run:
            reused, cost = obtain(job, item, provider, plan.influence, refresh)
            clips[item.key] = level(read_wav(wav), item.kind) if plan.normalise else read_wav(wav)
            if item.kind == "cue" or not item.loop:
                item.end = min(item.end, item.start + len(clips[item.key]) / float(RATE))
        entries.append({"name": item.name, "kind": item.kind, "prompt": item.prompt, "file": "sfx/%s.wav" % item.key, "cached": reused,
                        "start": round(item.start, 3), "end": round(item.end, 3), "gain_db": item.gain_db, "cost": cost})
    score = None  # type: Optional[List[float]]
    music_entry = None  # type: Optional[Dict[str, Any]]
    if plan.music is not None and not dry_run:
        score, music_entry = obtain_music(job, plan, provider, refresh)
        if plan.normalise:
            score = level(score, "music")
    report = {"duration": round(plan.total, 3), "provider": provider, "prompt_influence": plan.influence, "pad": plan.pad, "items": entries,
              "music": music_entry, "normalise": plan.normalise,
              "peak_dbfs": None, "generated": sum(not entry["cached"] for entry in entries),
              "reused": sum(entry["cached"] for entry in entries)}  # type: Dict[str, Any]
    if dry_run:
        report["dry_run"] = True
        report["wall_seconds"] = round(time.time() - started, 2)
        return report
    samples = mix(plan, clips, score)
    write_wav(job / "bed.wav", samples)
    report["peak_dbfs"] = round(20 * math.log10(max(abs(value) for value in samples) / 32768.0), 2)
    report["wall_seconds"] = round(time.time() - started, 2)
    with open(job / "sound_report.json", "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1)
    return report


def describe(report: Dict[str, Any]) -> List[str]:
    lines = []
    for entry in report["items"]:
        lines.append("%-8s %-16s %6.2f-%6.2f s  %+5.1f dB  %s  %s" % (
            entry["kind"], entry["name"], entry["start"], entry["end"], entry["gain_db"],
            "cached  " if entry["cached"] else "generate", entry["file"]))
    if report["pad"]:
        lines.append("pad      %-16s %6.2f-%6.2f s  %+5.1f dB" % (report["pad"]["mood"], 0.0, report["duration"], report["pad"]["gain_db"]))
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.sound", description=__doc__)
    parser.add_argument("--job", required=True)
    parser.add_argument("--provider", choices=PROVIDERS, default="elevenlabs")
    parser.add_argument("--refresh", nargs="+", action="extend", default=[], metavar="NAME", help="make this ambience or cue again")
    parser.add_argument("--dry-run", action="store_true", help="print the schedule and what would be generated; no network, no files")
    args = parser.parse_args(argv)
    try:
        report = make_bed(Path(args.job), args.provider, args.refresh, args.dry_run)
    except SoundError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if args.dry_run:
        print("\n".join(describe(report)))
        print("dry run: %.2f s bed, %d to generate, %d cached (%s)" % (report["duration"], report["generated"], report["reused"], report["provider"]))
        return 0
    print("bed.wav: %.2f s, peak %.2f dBFS, %d generated, %d reused (%s)" % (
        report["duration"], report["peak_dbfs"], report["generated"], report["reused"], report["provider"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
