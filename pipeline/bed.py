"""Music bed: writes a quiet synthetic pad (soft chords with a slow bell line) as a WAV file.

It is generated from sine waves by this file alone, so there is nothing to license or download.
Chords are semitone offsets from A3 (220 Hz): warm is C, F, Am, G and sad is Am, F, C, G.
The edit stage loops or trims it and sets it 11 LU under the voice.

Usage: python -m pipeline.bed --out bed.wav [--seconds 75] [--mood warm|sad] [--seed 1]
"""
from __future__ import annotations

import argparse
import array
import math
import random
import sys
import wave
from typing import List, Optional

RATE = 48000
MOODS = {
    "warm": [[3, 7, 10, 15], [8, 12, 15, 19], [0, 3, 7, 12], [10, 14, 17, 22]],
    "sad": [[0, 3, 7, 12], [8, 12, 15, 20], [3, 7, 10, 15], [10, 14, 17, 19]],
}


def note(semitones: float) -> float:
    return 220.0 * 2 ** (semitones / 12.0)


def render(seconds: float, mood: str = "warm", seed: int = 1, bar: float = 4.8) -> array.array:
    chords = MOODS[mood]
    total = int(seconds * RATE)
    left = [0.0] * total
    right = [0.0] * total
    rng = random.Random(seed)
    two_pi = 2 * math.pi
    fade = int(1.6 * RATE)
    bar_samples = int(bar * RATE)
    for index in range(int(math.ceil(seconds / bar)) + 1):
        chord = chords[index % len(chords)]
        start = index * bar_samples - fade // 2
        length = bar_samples + fade
        for voice, semitones in enumerate(chord):
            for detune, pan in ((-0.04, 0.3), (0.05, 0.7)):
                step = two_pi * note(semitones + detune - 12) / RATE
                phase = rng.random() * two_pi
                gain = 0.11 / (1 + voice * 0.35)
                for offset in range(length):
                    at = start + offset
                    if at < 0 or at >= total:
                        continue
                    edge = min(offset, length - offset)
                    envelope = 0.5 - 0.5 * math.cos(math.pi * min(1.0, edge / fade))
                    sample = math.sin(phase + step * offset) * gain * envelope
                    left[at] += sample * (1 - pan)
                    right[at] += sample * pan
        for beat in range(4):
            semitones = chord[rng.randrange(len(chord))] + 12
            step = two_pi * note(semitones) / RATE
            begin = index * bar_samples + int(beat * bar_samples / 4) + rng.randrange(600)
            pan = 0.3 + 0.4 * rng.random()
            ring = int(2.2 * RATE)
            for offset in range(ring):
                at = begin + offset
                if at >= total:
                    break
                envelope = math.exp(-offset / (0.55 * RATE)) * min(1.0, offset / 240.0)
                sample = (math.sin(step * offset) + 0.3 * math.sin(2 * step * offset)) * 0.05 * envelope
                left[at] += sample * (1 - pan)
                right[at] += sample * pan
    peak = max(max(abs(value) for value in left), max(abs(value) for value in right), 1e-9)
    scale = 0.5 / peak
    out = array.array("h")
    edge = int(0.5 * RATE)
    for index in range(total):
        ramp = min(1.0, index / edge, (total - index) / edge)
        out.append(int(left[index] * scale * ramp * 32767))
        out.append(int(right[index] * scale * ramp * 32767))
    return out


def write(path: str, seconds: float = 75.0, mood: str = "warm", seed: int = 1) -> None:
    samples = render(seconds, mood, seed)
    if sys.byteorder == "big":
        samples.byteswap()
    with wave.open(path, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(samples.tobytes())


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.bed", description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--seconds", type=float, default=75.0)
    parser.add_argument("--mood", choices=sorted(MOODS), default="warm")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)
    write(args.out, args.seconds, args.mood, args.seed)
    print("wrote %s (%.0f s, %s)" % (args.out, args.seconds, args.mood))
    return 0


if __name__ == "__main__":
    sys.exit(main())
