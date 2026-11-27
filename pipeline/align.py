"""Measure word timings on a finished audio file with a local aligner (mlx-whisper).

The aligner needs Apple Silicon and the mlx-whisper package, which lives in its own
virtual environment; the rest of the pipeline calls this module through that interpreter:

  <venv>/bin/python -m pipeline.align --audio narration.wav --script script.txt --out measured.json
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
_NUMBERS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
            "eight": "8", "nine": "9", "ten": "10", "twenty": "20", "thirty": "30", "forty": "40", "fifty": "50"}


def normalize(token: str) -> str:
    """Reduce a token to what an ASR model would write for it."""
    text = re.sub(r"[^a-z0-9]", "", token.lower().replace("’", "'"))
    return _NUMBERS.get(text, text)


def transcribe_words(audio: Path, prompt: str, model: str = DEFAULT_MODEL) -> List[Dict[str, Any]]:
    """Run mlx-whisper with word timestamps and return [{'word', 'start', 'end'}]."""
    import mlx_whisper

    result = mlx_whisper.transcribe(str(audio), path_or_hf_repo=model, word_timestamps=True, language="en",
                                    initial_prompt=prompt[:800], condition_on_previous_text=False, temperature=0.0)
    words = []
    for segment in result.get("segments", []):
        for word in segment.get("words", []):
            text = word.get("word", "").strip()
            if text:
                words.append({"word": text, "start": float(word["start"]), "end": float(word["end"])})
    return words


def match_tokens(tokens: Sequence[str], heard: Sequence[Dict[str, Any]]) -> List[Optional[Tuple[float, float]]]:
    """Give each script token the time span of the heard word it lines up with, or None."""
    want = [normalize(token) for token in tokens]
    got = [normalize(word["word"]) for word in heard]
    spans = [None] * len(tokens)  # type: List[Optional[Tuple[float, float]]]
    matcher = difflib.SequenceMatcher(a=want, b=got, autojunk=False)
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(a1 - a0):
                word = heard[b0 + offset]
                spans[a0 + offset] = (word["start"], word["end"])
        elif tag == "replace" and (a1 - a0) == (b1 - b0):
            for offset in range(a1 - a0):
                word = heard[b0 + offset]
                spans[a0 + offset] = (word["start"], word["end"])
        elif tag == "replace" and (a1 - a0) == 1 and b1 > b0:
            spans[a0] = (heard[b0]["start"], heard[b1 - 1]["end"])
    return spans


def fill_gaps(spans: List[Optional[Tuple[float, float]]], duration: float,
              hints: Optional[Sequence[Tuple[float, float]]] = None) -> List[Tuple[float, float]]:
    """Fill unmatched tokens from hints when given, otherwise spread them evenly between their neighbours."""
    filled = list(spans)
    index = 0
    while index < len(filled):
        if filled[index] is not None:
            index += 1
            continue
        end = index
        while end < len(filled) and filled[end] is None:
            end += 1
        left = filled[index - 1][1] if index > 0 else 0.0
        right = filled[end][0] if end < len(filled) else duration
        if hints is not None:
            h0, h1 = hints[index][0], hints[end - 1][1]
            scale = (right - left) / (h1 - h0) if h1 > h0 else 0.0
            for position in range(index, end):
                start = left + (hints[position][0] - h0) * scale
                stop = left + (hints[position][1] - h0) * scale
                filled[position] = (start, max(stop, start + 0.02))
        else:
            step = (right - left) / (end - index)
            for position in range(index, end):
                filled[position] = (left + step * (position - index), left + step * (position - index + 1))
        index = end
    previous = 0.0
    cleaned = []
    for start, stop in filled:
        start = max(start, previous)
        stop = min(max(stop, start + 0.02), duration) if duration > start + 0.02 else start + 0.02
        cleaned.append((round(start, 4), round(stop, 4)))
        previous = start
    return cleaned


def aligner_python() -> Optional[str]:
    """The interpreter that can import mlx_whisper: ALIGN_PYTHON, else this one if it has the package."""
    configured = os.environ.get("ALIGN_PYTHON")
    if configured:
        return configured
    try:
        import mlx_whisper  # noqa: F401
        return sys.executable
    except Exception:
        return None


def measure(audio: Path, tokens: Sequence[str], workdir: Path, model: str = DEFAULT_MODEL) -> Optional[Dict[str, Any]]:
    """Run the aligner in its interpreter; returns {'spans': [...], 'heard': n, 'matched': n} or None if unavailable."""
    python = aligner_python()
    if python is None:
        return None
    script = Path(workdir) / "align_script.txt"
    out = Path(workdir) / "align_measured.json"
    script.write_text(" ".join(tokens), encoding="utf-8")
    env = dict(os.environ)
    repo = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run([python, "-m", "pipeline.align", "--audio", str(audio), "--script", str(script), "--out", str(out),
                           "--model", model], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError("aligner failed: %s" % proc.stderr[-1500:])
    with open(out, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.align", description=__doc__)
    parser.add_argument("--audio", required=True)
    parser.add_argument("--script", required=True, help="text file with the spoken tokens, whitespace separated")
    parser.add_argument("--out", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args(argv)
    tokens = Path(args.script).read_text(encoding="utf-8").split()
    heard = transcribe_words(Path(args.audio), " ".join(tokens), args.model)
    spans = match_tokens(tokens, heard)
    payload = {"model": args.model, "heard": len(heard), "matched": sum(1 for span in spans if span is not None),
               "tokens": len(tokens), "spans": spans, "heard_words": heard}
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    print("aligned %d of %d tokens (%d heard)" % (payload["matched"], len(tokens), len(heard)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
