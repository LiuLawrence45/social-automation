from __future__ import annotations

import array
import wave

from pipeline import bed


def test_bed_is_quiet_stereo_and_the_asked_length(tmp_path):
    target = tmp_path / "bed.wav"
    assert bed.main(["--out", str(target), "--seconds", "2", "--mood", "sad"]) == 0
    with wave.open(str(target), "rb") as handle:
        assert (handle.getnchannels(), handle.getsampwidth(), handle.getframerate()) == (2, 2, bed.RATE)
        assert handle.getnframes() == 2 * bed.RATE
        samples = array.array("h", handle.readframes(handle.getnframes()))
    peak = max(abs(value) for value in samples)
    assert 4000 < peak <= 0.5 * 32767 + 1
    assert samples[0] == 0 and abs(samples[-1]) < 200


def test_bed_is_repeatable(tmp_path):
    first, second = tmp_path / "a.wav", tmp_path / "b.wav"
    bed.write(str(first), 1.0, "warm", 3)
    bed.write(str(second), 1.0, "warm", 3)
    assert first.read_bytes() == second.read_bytes()
