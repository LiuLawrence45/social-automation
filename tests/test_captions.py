from __future__ import annotations

from fractions import Fraction

import pytest

from pipeline import captions

FPS = Fraction(30)


@pytest.mark.parametrize("token,expected", [
    ("Hello,", "hello"), ("WORLD!", "world"), ("\"quoted\"", "quoted"), ("Don't", "don't"),
    ("don\u2019t", "don't"), ("4:02", "4:02"), ("(wait)...", "wait"), ("nine", "9"), ("Twenty", "20"),
    ("ninety", "90"), ("zero", "0"), ("hundred", "hundred"), ("I", "I"), ("i'm", "I'm"), ("I'll", "I'll"),
    ("I've", "I've"), ("i'd", "I'd"), ("--", ""), ("It's", "it's"),
])
def test_display_text(token, expected):
    assert captions.display_text(token, []) == expected


def test_display_text_names():
    assert captions.display_text("mara,", ["Mara"]) == "Mara"
    assert captions.display_text("Mara's", ["Mara"]) == "mara's"
    assert captions.display_text("Roblox", ["Roblox"]) == "Roblox"


def words(*spans):
    return [{"start": s, "end": e, "display": "w%d" % i} for i, (s, e) in enumerate(spans)]


def test_first_word_starts_on_frame_zero_and_last_holds():
    events = captions.caption_events(words((0.12, 0.38), (0.4, 0.8)), FPS, 60)
    assert events[0][0] == 0
    assert events[-1][1] == 60


def test_word_holds_until_next_start_across_gap():
    events = captions.caption_events(words((0.0, 0.3), (2.0, 2.3)), FPS, 90)
    assert events[0] == (0, 60, "w0")
    assert events[1] == (60, 90, "w1")


def test_short_word_borrows_without_overlap():
    events = captions.caption_events(words((0.0, 0.3), (1.0, 1.06), (1.06, 1.5)), FPS, 60)
    assert events[1][1] - events[1][0] >= 4
    for (_, end, _), (start, _, _) in zip(events, events[1:]):
        assert start >= end


def test_events_contiguous_and_frame_aligned():
    events = captions.caption_events(words((0.0, 0.3), (0.33, 0.5), (0.9, 1.0)), FPS, 45)
    assert all(isinstance(v, int) for e in events for v in e[:2])
    for (_, end, _), (start, _, _) in zip(events, events[1:]):
        assert start == end


def test_ass_times_never_late():
    ass = captions.build_ass([(0, 20, "a"), (20, 45, "b")], FPS)
    assert "0:00:00.66" in ass and "0:00:01.50" in ass
    assert ass.count("Dialogue:") == 4
    assert "\\blur" in ass


def test_font_size_derivation():
    assert captions.ASS_FONT_SIZE == 156
