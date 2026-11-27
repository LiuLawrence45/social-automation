"""Voice stage: narration.wav and words.json from story.json.

Usage: python -m pipeline.voice --job <folder> [--provider auto|elevenlabs|say] [--reuse-raw]

ElevenLabs is the narrator (ELEVENLABS_API_KEY is read from the environment and never written
anywhere); the macOS "say" voice is the free fallback behind the same synthesize() function.
story.json "lines" ({"speaker", "text"}) give each speaking character a voice from voice.speakers (voice_id,
voice_name, settings, process); consecutive lines by one speaker are one segment, cached under voice_work/segments/,
and the joined take goes through the stages below as one narration. The raw take is tightened (long pauses cut), sped up to the target pace without changing pitch,
and levelled to leave true-peak headroom. story.json's voice.speed (default 1.0) scales the whole
narration: 1.2 raises the target pace and shortens the allowed pauses by that factor. Word timings on the final file come from the provider's
alignment carried through those edits (timing_source "carried", the default when the provider
returns an alignment) or from the local aligner (pipeline.align) snapped to sound onsets
("aligner", the default for the fallback voice). Carried timings that sit more than 30 ms early or late on average against the
sound onsets are shifted by that average. Both are checked against the sound onsets in
the final file and the aligner always runs as a transcript check when it is installed.
"""
from __future__ import annotations

import argparse
import array
import base64
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import align, story as story_mod
from .edit import ffmpeg_bin

RATE = 48000
WINDOW = 480
DEFAULTS = {
    "provider": "auto",
    "model": "eleven_v4",
    "voice_id": "TX3LPaxmHKxFdv7VOQHJ",
    "voice_name": "Liam",
    "settings": {"speed": 1.1},
    "say_voice": "Samantha",
    "target_wpm": 215,
    "speed": 1.0,
    "max_tempo": 1.3,
    "pause_sentence": 0.3,
    "pause_inner": 0.2,
    "peak_dbtp": -1.5,
    "timing_source": "auto",
    "segment_gap": 0.12,
    "speakers": {},
}
NARRATOR = "narrator"
PROCESS_KEYS = ("pitch", "tempo", "filter", "gain_db")
SPEAKER_KEYS = ("voice_id", "voice_name", "settings", "process")
Span = Tuple[float, float]


class VoiceError(RuntimeError):
    """Raised for failures the operator can act on."""


def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if proc.returncode != 0:
        raise VoiceError("command failed (%d): %s\n%s" % (proc.returncode, " ".join(cmd[:6]), proc.stderr[-1500:]))
    return proc


def read_wav(path: Path) -> "array.array":
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != RATE or handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise VoiceError("%s must be 48 kHz mono 16-bit" % path)
        samples = array.array("h")
        samples.frombytes(handle.readframes(handle.getnframes()))
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def write_wav(path: Path, samples: "array.array") -> None:
    data = array.array("h", samples)
    if sys.byteorder == "big":
        data.byteswap()
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(data.tobytes())


def to_wav(source: Path, target: Path, input_args: Sequence[str] = ()) -> None:
    _run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y"] + list(input_args) +
         ["-i", str(source), "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", str(target)])


def spans_from_characters(tokens: Sequence[str], characters: Sequence[str], starts: Sequence[float], ends: Sequence[float]) -> List[Span]:
    """Turn a character-level alignment into one (start, end) per whitespace token."""
    spans = []
    cursor = 0
    count = len(characters)
    for token in tokens:
        while cursor < count and characters[cursor].isspace():
            cursor += 1
        first = cursor
        taken = 0
        while cursor < count and taken < len(token):
            taken += len(characters[cursor])
            cursor += 1
        if first >= count or taken < len(token):
            raise VoiceError("the returned alignment does not cover the script (stopped at %r)" % token)
        spans.append((float(starts[first]), float(ends[cursor - 1])))
    return spans


def _elevenlabs(text: str, tokens: Sequence[str], voice: Dict[str, Any], workdir: Path) -> Dict[str, Any]:
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise VoiceError("ELEVENLABS_API_KEY is not set in the environment")
    attempts = [("pcm_48000", True), ("pcm_48000", False), ("mp3_44100_128", False)]
    last_error = "no attempt made"
    for output_format, with_settings in attempts:
        body = {"text": text, "model_id": voice["model"]}
        if with_settings and voice.get("settings"):
            body["voice_settings"] = voice["settings"]
        if voice.get("seed") is not None:
            body["seed"] = voice["seed"]
        for field in ("previous_text", "next_text"):
            if voice.get(field):
                body[field] = voice[field]
        url = "https://api.elevenlabs.io/v1/text-to-speech/%s/with-timestamps?output_format=%s" % (voice["voice_id"], output_format)
        request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                         headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                payload = json.load(response)
                characters_billed = response.headers.get("character-cost") or response.headers.get("x-character-count")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            last_error = "HTTP %d for %s%s: %s" % (exc.code, output_format, "" if with_settings else " without voice settings", detail)
            if exc.code in (401, 403, 429):
                break
            continue
        except urllib.error.URLError as exc:
            last_error = "network error: %s" % exc.reason
            break
        alignment = payload.get("alignment") or payload.get("normalized_alignment")
        if not alignment:
            raise VoiceError("ElevenLabs returned no alignment")
        raw = workdir / ("raw_take." + ("mp3" if output_format.startswith("mp3") else "pcm"))
        raw.write_bytes(base64.b64decode(payload["audio_base64"]))
        wav = workdir / "raw.wav"
        to_wav(raw, wav, [] if output_format.startswith("mp3") else ["-f", "s16le", "-ar", output_format.split("_")[1], "-ac", "1"])
        raw.unlink()
        spans = spans_from_characters(tokens, alignment["characters"], alignment["character_start_times_seconds"],
                                      alignment["character_end_times_seconds"])
        return {"provider": "elevenlabs", "wav": wav, "spans": spans,
                "meta": {"model": voice["model"], "voice_id": voice["voice_id"], "voice_name": voice.get("voice_name"),
                         "output_format": output_format, "settings_sent": bool(with_settings and voice.get("settings")),
                         "characters": len(text), "characters_billed": characters_billed}}
    raise VoiceError("ElevenLabs request failed: " + last_error)


def _say(text: str, tokens: Sequence[str], voice: Dict[str, Any], workdir: Path) -> Dict[str, Any]:
    aiff = workdir / "raw_take.aiff"
    _run(["say", "-v", voice.get("say_voice", "Samantha"), "-o", str(aiff), text])
    wav = workdir / "raw.wav"
    to_wav(aiff, wav)
    aiff.unlink()
    return {"provider": "say", "wav": wav, "spans": None, "meta": {"voice_name": voice.get("say_voice", "Samantha"), "characters": len(text)}}


def synthesize(text: str, tokens: Sequence[str], voice: Dict[str, Any], workdir: Path) -> Dict[str, Any]:
    """The one synthesis function: ElevenLabs first, the local voice behind it."""
    provider = voice.get("provider", "auto")
    if provider in ("auto", "elevenlabs"):
        try:
            return _elevenlabs(text, tokens, voice, workdir)
        except VoiceError as exc:
            if provider == "elevenlabs":
                raise
            fallback = _say(text, tokens, voice, workdir)
            fallback["meta"]["fallback_reason"] = str(exc)
            return fallback
    if provider == "say":
        return _say(text, tokens, voice, workdir)
    raise VoiceError("unknown voice provider %r" % provider)


def envelope(samples: "array.array") -> List[float]:
    """RMS per 10 ms window."""
    values = []
    for offset in range(0, len(samples) - WINDOW + 1, WINDOW):
        chunk = samples[offset:offset + WINDOW]
        values.append(math.sqrt(sum(value * value for value in chunk) / WINDOW))
    return values


def _quiet_run(env: List[float], threshold: float, first: int, last: int) -> Optional[Tuple[int, int]]:
    best = None
    start = None
    for index in range(max(first, 0), min(last, len(env))):
        if env[index] < threshold:
            if start is None:
                start = index
        elif start is not None:
            if best is None or index - start > best[1] - best[0]:
                best = (start, index)
            start = None
    if start is not None:
        index = min(last, len(env))
        if best is None or index - start > best[1] - best[0]:
            best = (start, index)
    return best


def plan_cuts(env: List[float], spans: Sequence[Span], sentence_ids: Sequence[int], pause_sentence: float, pause_inner: float,
              tempo: float, lead: float = 0.03, trail: float = 0.12) -> List[Tuple[int, int]]:
    """Sample ranges to remove so no pause outlasts its limit once the take is sped up by tempo."""
    threshold = max(max(env) * 10 ** (-36 / 20.0), 20.0)
    per_second = RATE / WINDOW
    total = len(env) * WINDOW
    cuts = []
    loud = [index for index, value in enumerate(env) if value >= threshold]
    if not loud:
        return cuts
    head = max(0, int(loud[0] * WINDOW - lead * tempo * RATE))
    if head > 0:
        cuts.append((0, head))
    for index in range(len(spans) - 1):
        limit = (pause_sentence if sentence_ids[index] != sentence_ids[index + 1] else pause_inner) * tempo
        run = _quiet_run(env, threshold, int((spans[index][1] - 0.08) * per_second), int((spans[index + 1][0] + 0.08) * per_second) + 1)
        if run is None:
            continue
        length = (run[1] - run[0]) / per_second
        if length <= limit + 0.02:
            continue
        remove = int((length - limit) * RATE)
        middle = (run[0] + run[1]) * WINDOW // 2
        cuts.append((middle - remove // 2, middle - remove // 2 + remove))
    tail = min(total, int((loud[-1] + 1) * WINDOW + trail * tempo * RATE))
    if tail < total:
        cuts.append((tail, total))
    return cuts


def apply_cuts(samples: "array.array", cuts: Sequence[Tuple[int, int]], fade: int = 192) -> "array.array":
    """Remove sample ranges, cross-fading a few milliseconds at every splice."""
    out = array.array("h")
    cursor = 0
    for start, end in cuts:
        piece = samples[cursor:start]
        if len(out) >= fade and len(piece) >= fade and cursor > 0:
            for index in range(fade):
                weight = index / float(fade)
                out[len(out) - fade + index] = int(out[len(out) - fade + index] * (1 - weight) + piece[index] * weight)
            piece = piece[fade:]
        out.extend(piece)
        cursor = end
    piece = samples[cursor:]
    if len(out) >= fade and len(piece) >= fade and cursor > 0:
        for index in range(fade):
            weight = index / float(fade)
            out[len(out) - fade + index] = int(out[len(out) - fade + index] * (1 - weight) + piece[index] * weight)
        piece = piece[fade:]
    out.extend(piece)
    return out


def map_time(value: float, cuts: Sequence[Tuple[int, int]], fade: int = 192) -> float:
    """Where a time in the raw take lands after the cuts."""
    sample = value * RATE
    removed = 0.0
    for start, end in cuts:
        if sample >= end:
            removed += end - start + (fade if start > 0 else 0)
        elif sample > start:
            removed += sample - start
    return max(0.0, (sample - removed) / RATE)


def tighten(samples: "array.array", spans: Sequence[Span], sentence_ids: Sequence[int], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Cut pauses and pick the tempo that reaches the target pace."""
    env = envelope(samples)
    words = len(spans)
    tempo = 1.0
    cuts = []  # type: List[Tuple[int, int]]
    for _ in range(6):
        cuts = plan_cuts(env, spans, sentence_ids, cfg["pause_sentence"], cfg["pause_inner"], tempo)
        kept = (len(samples) - sum(end - start for start, end in cuts)) / float(RATE)
        tempo = min(cfg["max_tempo"], max(1.0, cfg["target_wpm"] * kept / (60.0 * words)))
    tightened = apply_cuts(samples, cuts)
    mapped = [(map_time(start, cuts) / tempo, map_time(end, cuts) / tempo) for start, end in spans]
    return {"samples": tightened, "tempo": tempo, "cuts": cuts, "spans": mapped,
            "removed_seconds": sum(end - start for start, end in cuts) / float(RATE)}


def _threshold(env: List[float]) -> float:
    return max(max(env) * 10 ** (-36 / 20.0), 20.0)


def acoustic_onset(env: List[float], near: float, reach: float = 0.4) -> Optional[float]:
    """The closest quiet-to-loud transition to a time, within reach seconds."""
    threshold = _threshold(env)
    per_second = RATE / WINDOW
    centre = int(near * per_second)
    best = None
    for index in range(max(2, centre - int(reach * per_second)), min(len(env), centre + int(reach * per_second))):
        if env[index] >= threshold and env[index - 1] < threshold and env[index - 2] < threshold:
            if best is None or abs(index - centre) < abs(best - centre):
                best = index
    return None if best is None else best / per_second


def snap_to_onsets(spans: Sequence[Span], env: List[float]) -> List[Span]:
    """Move word starts that sit inside a pause forward to where the sound begins."""
    threshold = _threshold(env)
    per_second = RATE / WINDOW
    snapped = []
    for start, end in spans:
        index = int(start * per_second)
        if index < len(env) and env[index] < threshold:
            limit = min(len(env), index + int(0.5 * per_second), int(end * per_second) + 1)
            while index < limit and env[index] < threshold:
                index += 1
            if index < limit:
                start = index / per_second
        snapped.append((start, max(end, start + 0.02)))
    return snapped


def onset_error(spans: Sequence[Span], sentence_ids: Sequence[int], env: List[float]) -> Dict[str, Any]:
    """How far sentence-initial word starts sit from the sound onsets in the audio."""
    errors = []
    for index, span in enumerate(spans):
        if index == 0 or sentence_ids[index - 1] != sentence_ids[index]:
            onset = acoustic_onset(env, span[0])
            if onset is not None:
                errors.append(span[0] - onset)
    if not errors:
        return {"checked": 0}
    return {"checked": len(errors), "mean_abs_seconds": round(sum(abs(value) for value in errors) / len(errors), 4),
            "bias_seconds": round(sum(errors) / len(errors), 4), "worst_seconds": round(max(errors, key=abs), 4)}


def true_peak(path: Path) -> float:
    proc = subprocess.run([ffmpeg_bin(), "-hide_banner", "-nostats", "-i", str(path), "-af", "loudnorm=print_format=json", "-f", "null", "-"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    start = proc.stderr.rfind("{")
    stop = proc.stderr.rfind("}")
    if proc.returncode != 0 or start < 0:
        raise VoiceError("could not measure loudness: %s" % proc.stderr[-800:])
    return float(json.loads(proc.stderr[start:stop + 1])["input_tp"])


def finish(tightened: Path, target: Path, tempo: float, peak_dbtp: float) -> Dict[str, float]:
    """Speed up without changing pitch, then set the gain so the true peak sits at peak_dbtp."""
    sped = target.with_name("narration_sped.wav")
    _run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(tightened), "-af", "atempo=%.6f" % tempo,
          "-ar", str(RATE), "-ac", "1", "-c:a", "pcm_s16le", str(sped)])
    measured = true_peak(sped)
    gain = peak_dbtp - measured
    _run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(sped), "-af", "volume=%.3fdB" % gain,
          "-ar", str(RATE), "-ac", "1", "-c:a", "pcm_s16le", str(target)])
    sped.unlink()
    return {"gain_db": round(gain, 3), "true_peak_dbtp": round(true_peak(target), 2)}


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def pause_stats(spans: Sequence[Span], sentence_ids: Sequence[int]) -> Dict[str, Any]:
    gaps = [spans[index + 1][0] - spans[index][1] for index in range(len(spans) - 1)]
    between = [gap for index, gap in enumerate(gaps) if sentence_ids[index] != sentence_ids[index + 1]]
    return {"longest": round(max(gaps), 3) if gaps else 0.0,
            "sentence_mean": round(sum(between) / len(between), 3) if between else 0.0,
            "over_half_second": sum(1 for gap in gaps if gap > 0.5)}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_process(process: Any, speaker: str) -> Dict[str, Any]:
    """Validate one speaker's process object."""
    where = "voice.speakers.%s.process" % speaker
    if not isinstance(process, dict):
        raise VoiceError(where + " must be an object")
    unknown = sorted(set(process) - set(PROCESS_KEYS))
    if unknown:
        raise VoiceError("%s has unknown keys %s (allowed: %s)" % (where, ", ".join(unknown), ", ".join(PROCESS_KEYS)))
    for key in ("pitch", "tempo"):
        if key in process and not (_is_number(process[key]) and 0.5 <= process[key] <= 2.0):
            raise VoiceError("%s.%s must be a number between 0.5 and 2.0" % (where, key))
    if "filter" in process and not (isinstance(process["filter"], str) and process["filter"].strip()):
        raise VoiceError(where + ".filter must be a non-empty FFmpeg audio filter chain")
    if "gain_db" in process and not (_is_number(process["gain_db"]) and -40 <= process["gain_db"] <= 40):
        raise VoiceError(where + ".gain_db must be a number between -40 and 40")
    return process


def speaker_voice(cfg: Dict[str, Any], speaker: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """The voice settings and the process for a speaker; a speaker not in voice.speakers is the narrator voice."""
    speakers = cfg.get("speakers") or {}
    entry = speakers.get(speaker, {})
    where = "voice.speakers.%s" % speaker
    if not isinstance(entry, dict):
        raise VoiceError(where + " must be an object")
    unknown = sorted(set(entry) - set(SPEAKER_KEYS))
    if unknown:
        raise VoiceError("%s has unknown keys %s (allowed: %s)" % (where, ", ".join(unknown), ", ".join(SPEAKER_KEYS)))
    voice = dict(cfg)
    if "voice_id" in entry:
        if not (isinstance(entry["voice_id"], str) and entry["voice_id"].strip()):
            raise VoiceError(where + ".voice_id must be a non-empty string")
        voice["voice_id"] = entry["voice_id"]
        voice["voice_name"] = entry.get("voice_name")
    elif "voice_name" in entry:
        voice["voice_name"] = entry["voice_name"]
    if "settings" in entry:
        if not isinstance(entry["settings"], dict):
            raise VoiceError(where + ".settings must be an object")
        voice["settings"] = entry["settings"]
    return voice, check_process(entry.get("process", {}), speaker)


def make_segments(lines: Sequence[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Group consecutive lines by the same speaker into one segment each."""
    segments = []  # type: List[Dict[str, Any]]
    for line in lines:
        if segments and segments[-1]["speaker"] == line["speaker"]:
            segments[-1]["text"] += " " + line["text"]
        else:
            segments.append({"speaker": line["speaker"], "text": line["text"]})
    for segment in segments:
        segment["tokens"] = story_mod.tokenize(segment["text"])
    return segments


def segment_key(voice: Dict[str, Any], text: str, before: str, after: str, process: Dict[str, Any]) -> str:
    parts = {"provider": voice.get("provider"), "model": voice.get("model"), "voice_id": voice.get("voice_id"),
             "settings": voice.get("settings"), "seed": voice.get("seed"), "say_voice": voice.get("say_voice"),
             "text": text, "previous": before, "next": after}
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()[:20]


def process_filter(process: Dict[str, Any]) -> str:
    """The FFmpeg audio chain for a process: pitch keeps the duration, tempo changes it."""
    chain = []
    pitch = process.get("pitch", 1.0)
    if pitch != 1.0:
        chain += ["asetrate=%d" % round(RATE * pitch), "aresample=%d" % RATE, "atempo=%.6f" % (1.0 / pitch)]
    if process.get("tempo", 1.0) != 1.0:
        chain.append("atempo=%.6f" % process["tempo"])
    if process.get("filter"):
        chain.append(process["filter"])
    if process.get("gain_db"):
        chain.append("volume=%.3fdB" % process["gain_db"])
    return ",".join(chain)


def _billed(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def synthesize_segments(cfg: Dict[str, Any], segments: Sequence[Dict[str, Any]], workdir: Path) -> Dict[str, Any]:
    """Generate (or load from cache) every segment, process it and join them into one raw take."""
    gap = cfg["segment_gap"]
    if not (_is_number(gap) and 0 <= gap <= 2):
        raise VoiceError("voice.segment_gap must be a number of seconds between 0 and 2")
    for speaker in cfg.get("speakers") or {}:
        speaker_voice(cfg, speaker)
    cache = workdir / "segments"
    scratch = cache / "_work"
    scratch.mkdir(parents=True, exist_ok=True)
    samples = array.array("h")
    spans = []  # type: Optional[List[Span]]
    providers = []
    stats = {}  # type: Dict[str, Dict[str, Any]]
    first_meta = None
    for index, segment in enumerate(segments):
        voice, process = speaker_voice(cfg, segment["speaker"])
        before = segments[index - 1]["text"] if index else ""
        after = segments[index + 1]["text"] if index + 1 < len(segments) else ""
        voice["previous_text"], voice["next_text"] = before or None, after or None
        key = segment_key(voice, segment["text"], before, after, process)
        wav_path, json_path = cache / (key + ".wav"), cache / (key + ".json")
        cached = wav_path.exists() and json_path.exists()
        if cached:
            take = json.loads(json_path.read_text(encoding="utf-8"))
        else:
            take = synthesize(segment["text"], segment["tokens"], voice, scratch)
            os.replace(str(take["wav"]), str(wav_path))
            take = {"provider": take["provider"], "spans": take["spans"], "meta": take["meta"]}
            if "fallback_reason" not in take["meta"]:
                json_path.write_text(json.dumps(take), encoding="utf-8")
        scale = 1.0 / process.get("tempo", 1.0)
        chain = process_filter(process)
        source = wav_path
        if chain:
            source = scratch / "processed.wav"
            _run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav_path), "-af", chain,
                  "-ar", str(RATE), "-ac", "1", "-c:a", "pcm_s16le", str(source)])
        if index:
            samples.extend(array.array("h", bytes(2 * int(gap * RATE))))
        offset = len(samples) / float(RATE)
        samples.extend(read_wav(source))
        if take["spans"] is None or spans is None:
            spans = None
        else:
            spans.extend((offset + start * scale, offset + end * scale) for start, end in take["spans"])
        providers.append(take["provider"])
        first_meta = first_meta or take["meta"]
        item = stats.setdefault(segment["speaker"], {
            "voice_id": voice.get("voice_id"), "voice_name": voice.get("voice_name"), "segments": 0, "characters": 0,
            "characters_sent": 0, "characters_billed": None, "cached_segments": 0, "process": process or None})
        item["segments"] += 1
        item["characters"] += len(segment["text"])
        if cached:
            item["cached_segments"] += 1
        else:
            item["characters_sent"] += len(segment["text"])
            billed = _billed(take["meta"].get("characters_billed"))
            if billed is not None:
                item["characters_billed"] = (item["characters_billed"] or 0) + billed
    shutil.rmtree(str(scratch), ignore_errors=True)
    wav = workdir / "raw.wav"
    write_wav(wav, samples)
    meta = dict(first_meta)
    meta["characters"] = sum(len(segment["text"]) for segment in segments)
    if len(segments) > 1:
        meta["segments"] = len(segments)
        billed = [item["characters_billed"] for item in stats.values() if item["characters_billed"] is not None]
        meta["characters_billed"] = sum(billed) if billed else None
    return {"provider": providers[0] if len(set(providers)) == 1 else "mixed", "wav": wav, "spans": spans, "meta": meta, "speakers": stats}


def make_narration(job: Path, provider: Optional[str] = None, reuse_raw: bool = False) -> Dict[str, Any]:
    """Run the whole voice stage for a job folder and return the report."""
    job = Path(job)
    started = time.time()
    story = story_mod.validate_story(story_mod.load_json(job / "story.json"))
    cfg = dict(DEFAULTS)
    cfg.update(story.get("voice", {}))
    if provider:
        cfg["provider"] = provider
    speed = float(cfg["speed"])
    if not 0.5 <= speed <= 2.0:
        raise VoiceError("voice.speed must be between 0.5 and 2.0")
    cfg["target_wpm"] *= speed
    cfg["max_tempo"] *= speed
    cfg["pause_sentence"] /= speed
    cfg["pause_inner"] /= speed
    tokens = story_mod.tokenize(story["script"])
    sentences = story_mod.sentence_ids(tokens)
    segments = make_segments(story_mod.story_lines(story))
    token_speakers = [segment["speaker"] for segment in segments for _ in segment["tokens"]]
    workdir = job / "voice_work"
    workdir.mkdir(exist_ok=True)
    raw_meta = workdir / "raw.json"
    if reuse_raw and raw_meta.exists() and (workdir / "raw.wav").exists():
        take = json.loads(raw_meta.read_text(encoding="utf-8"))
        take["wav"] = workdir / "raw.wav"
    else:
        take = synthesize_segments(cfg, segments, workdir)
        raw_meta.write_text(json.dumps({"provider": take["provider"], "spans": take["spans"], "meta": take["meta"],
                                        "speakers": take["speakers"]}), encoding="utf-8")
    raw_duration = wav_duration(take["wav"])
    raw_spans = take["spans"]
    if raw_spans is None:
        measured = align.measure(take["wav"], tokens, workdir)
        if measured is None:
            raise VoiceError("the fallback voice returns no word timings and the aligner is not installed (set ALIGN_PYTHON)")
        raw_spans = align.fill_gaps([tuple(span) if span else None for span in measured["spans"]], raw_duration)
    raw_spans = [tuple(span) for span in raw_spans]
    result = tighten(read_wav(take["wav"]), raw_spans, sentences, cfg)
    tightened = workdir / "tightened.wav"
    write_wav(tightened, result["samples"])
    narration = job / "narration.wav"
    levels = finish(tightened, narration, result["tempo"], cfg["peak_dbtp"])
    duration = wav_duration(narration)
    carried = [(min(start, duration), min(end, duration)) for start, end in result["spans"]]
    final_env = envelope(read_wav(narration))
    source = cfg["timing_source"]
    if source == "auto":
        source = "carried" if take["spans"] is not None else "aligner"
    timing = {"source": source, "aligner": None}
    drift = onset_error(carried, sentences, final_env)
    if drift.get("checked", 0) >= 5 and abs(drift["bias_seconds"]) > 0.03:
        shift = drift["bias_seconds"]
        carried = [(min(max(start - shift, 0.0), duration), min(max(end - shift, 0.0), duration)) for start, end in carried]
        timing["carried_shift_seconds"] = round(-shift, 4)
    spans = carried
    measured = align.measure(narration, tokens, workdir)
    if measured is not None:
        found = [tuple(span) if span else None for span in measured["spans"]]
        snapped = snap_to_onsets(align.fill_gaps(found, duration, carried), final_env)
        deltas = [abs(snapped[index][0] - carried[index][0]) for index in range(len(tokens)) if found[index]]
        timing["aligner"] = {
            "model": measured["model"], "matched": measured["matched"], "tokens": measured["tokens"],
            "mean_abs_start_delta_vs_carried": round(sum(deltas) / len(deltas), 4) if deltas else None,
            "max_abs_start_delta_vs_carried": round(max(deltas), 4) if deltas else None,
            "onset_error": onset_error(snapped, sentences, final_env)}
        if source == "aligner":
            spans = snapped
    elif source == "aligner":
        raise VoiceError("timing_source is 'aligner' but the aligner is not installed (set ALIGN_PYTHON)")
    timing["onset_error"] = onset_error(spans, sentences, final_env)
    shown = story_mod.display_words(story)
    words = []
    previous = 0.0
    for index, token in enumerate(tokens):
        start = max(round(spans[index][0], 3), previous)
        end = max(round(min(spans[index][1], duration), 3), round(start + 0.02, 3))
        words.append({"i": index, "token": token, "display": shown[index], "start": start, "end": end, "sentence": sentences[index],
                      "speaker": token_speakers[index]})
        previous = start
    payload = {"version": 1, "audio": "narration.wav", "duration": round(duration, 3), "words": words, "timing": timing,
               "carried_starts": [round(span[0], 3) for span in carried]}
    with open(job / "words.json", "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1)
    final_spans = [(word["start"], word["end"]) for word in words]
    report = {
        "provider": take["provider"], "voice": take["meta"], "words": len(tokens), "speed": speed,
        "raw_seconds": round(raw_duration, 3), "raw_wpm": round(len(tokens) / raw_duration * 60, 1),
        "removed_seconds": round(result["removed_seconds"], 3), "tempo": round(result["tempo"], 4),
        "final_seconds": round(duration, 3), "final_wpm": round(len(tokens) / duration * 60, 1),
        "speakers": take.get("speakers", {}), "pauses": pause_stats(final_spans, sentences), "levels": levels, "timing": timing,
        "wall_seconds": round(time.time() - started, 1),
    }
    with open(job / "voice_report.json", "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.voice", description=__doc__)
    parser.add_argument("--job", required=True)
    parser.add_argument("--provider", choices=["auto", "elevenlabs", "say"])
    parser.add_argument("--reuse-raw", action="store_true", help="reuse the saved raw take instead of synthesising again")
    args = parser.parse_args(argv)
    try:
        report = make_narration(Path(args.job), args.provider, args.reuse_raw)
    except (VoiceError, story_mod.StoryError, RuntimeError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
