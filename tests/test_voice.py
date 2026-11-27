from __future__ import annotations

import array
import math

import pytest

from pipeline import voice

RATE = voice.RATE


def tone(seconds, level=9000):
    return [int(level * math.sin(2 * math.pi * 220 * index / RATE)) for index in range(int(seconds * RATE))]


def silence(seconds):
    return [0] * int(seconds * RATE)


def test_spans_from_characters_groups_characters_into_tokens():
    text = "Hi there."
    characters = list(text)
    starts = [index * 0.1 for index in range(len(characters))]
    ends = [start + 0.1 for start in starts]
    spans = voice.spans_from_characters(["Hi", "there."], characters, starts, ends)
    assert spans[0] == pytest.approx((0.0, 0.2))
    assert spans[1] == pytest.approx((0.3, 0.9))


def test_spans_from_characters_rejects_a_short_alignment():
    with pytest.raises(voice.VoiceError, match="does not cover the script"):
        voice.spans_from_characters(["Hi", "there."], list("Hi the"), [0.0] * 6, [0.1] * 6)


def test_cut_samples_and_mapped_times_agree():
    samples = array.array("h", [index % 2000 for index in range(2 * RATE)])
    cuts = [(0, 4800), (48000, 57600)]
    out = voice.apply_cuts(samples, cuts)
    assert voice.map_time(0.5, cuts) == pytest.approx(0.4)
    assert voice.map_time(1.1, cuts) == pytest.approx((48000 - 4800) / RATE)
    assert len(out) / RATE == pytest.approx(voice.map_time(2.0, cuts))


def test_tighten_trims_long_pauses_and_keeps_words_on_their_sound():
    samples = array.array("h", silence(0.5) + tone(0.3) + silence(1.5) + tone(0.3) + silence(0.2) + tone(0.3) + silence(1.0))
    spans = [(0.5, 0.8), (2.3, 2.6), (2.8, 3.1)]
    cfg = dict(voice.DEFAULTS, target_wpm=30, max_tempo=1.3, pause_sentence=0.3, pause_inner=0.2)
    result = voice.tighten(samples, spans, [0, 1, 1], cfg)
    assert result["tempo"] == 1.0
    assert result["removed_seconds"] > 2.0
    first, second, third = result["spans"]
    assert first[0] < 0.08
    assert 0.2 < second[0] - first[1] < 0.4
    assert third[0] - second[1] == pytest.approx(0.2, abs=0.03)
    env = voice.envelope(result["samples"])
    for start, _ in result["spans"]:
        onset = voice.acoustic_onset(env, start)
        assert onset is not None and abs(onset - start) < 0.03


def test_tempo_rises_to_reach_the_target_pace():
    samples = array.array("h", silence(0.1) + tone(0.9) + silence(0.1) + tone(0.9) + silence(0.1))
    cfg = dict(voice.DEFAULTS, target_wpm=90, max_tempo=1.3)
    result = voice.tighten(samples, [(0.1, 1.0), (1.1, 2.0)], [0, 0], cfg)
    assert 1.0 < result["tempo"] <= 1.3
    assert result["spans"][1][0] == pytest.approx(voice.map_time(1.1, result["cuts"]) / result["tempo"])
