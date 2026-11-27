"""Caption text normalisation, timing and ASS generation for one-word captions."""
from __future__ import annotations

import math
import re
from fractions import Fraction
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

FONT_FILE = "Montserrat-ExtraBold.ttf"
FONT_NAME = "Montserrat ExtraBold"
FONT_UNITS_PER_EM = 1000
FONT_WIN_ASCENT = 1109
FONT_WIN_DESCENT = 453
FONT_CAP_HEIGHT = 700
TARGET_EM_PX = 100
ASS_FONT_SIZE = round(TARGET_EM_PX * (FONT_WIN_ASCENT + FONT_WIN_DESCENT) / FONT_UNITS_PER_EM)
OUTLINE_PX = 8
SHADOW_OFFSET_PX = 6
SHADOW_BLUR = 7
SHADOW_ALPHA_HEX = "99"
CENTER_Y_FRACTION = 0.69
MIN_WORD_SECONDS = 0.12
LONG_TAIL_SECONDS = 0.6
LAST_WORD_HOLD_SECONDS = 0.4

_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18",
    "nineteen": "19", "twenty": "20", "thirty": "30", "forty": "40",
    "fifty": "50", "sixty": "60", "seventy": "70", "eighty": "80", "ninety": "90",
}
_I_FORMS = {"i": "I", "i'm": "I'm", "i'll": "I'll", "i've": "I've", "i'd": "I'd"}


def display_text(token: str, names: Iterable[str] = ()) -> str:
    """Return the caption text for a spoken token.

    Strips punctuation (inner apostrophes and ':' or '.'/',' between digits stay),
    lowercases except names, "I" and its contractions, and turns single number
    words into digits.
    """
    text = token.replace("\u2019", "'").replace("\u2018", "'")
    chars: List[str] = []
    for index, ch in enumerate(text):
        prev_ch = text[index - 1] if index > 0 else ""
        next_ch = text[index + 1] if index + 1 < len(text) else ""
        if ch.isalnum():
            chars.append(ch)
        elif ch == "'" and prev_ch.isalpha() and next_ch.isalpha():
            chars.append(ch)
        elif ch in ":.," and prev_ch.isdigit() and next_ch.isdigit():
            chars.append(ch)
    word = "".join(chars)
    if not word:
        return ""
    lower = word.lower()
    if lower in _NUMBER_WORDS:
        return _NUMBER_WORDS[lower]
    if lower in _I_FORMS:
        return _I_FORMS[lower]
    known = {name.lower(): name for name in names}
    if lower in known:
        return known[lower]
    return lower


def caption_events(
    words: Sequence[dict], fps: Fraction, total_frames: int, narration_end: Optional[float] = None
) -> List[Tuple[int, int, str]]:
    """Return (start_frame, end_frame, text) events, end exclusive, on the frame grid.

    A word shows until the next word starts. Words shorter than 120 ms are
    extended into the following gap by delaying the next word, never overlapping.
    The first word starts at frame 0 and the last word holds to the last frame,
    unless the video runs LONG_TAIL_SECONDS or more past narration_end (seconds):
    then it ends LAST_WORD_HOLD_SECONDS after its own end and the tail has no caption.
    """
    min_frames = max(1, math.ceil(MIN_WORD_SECONDS * fps - Fraction(1, 1000)))
    starts = [int(round(Fraction(str(w["start"])) * fps)) for w in words]
    starts[0] = 0
    last_end = None
    if narration_end is not None and total_frames / fps - Fraction(str(narration_end)) >= LONG_TAIL_SECONDS:
        last_end = int(round((Fraction(str(words[-1]["end"])) + Fraction(str(LAST_WORD_HOLD_SECONDS))) * fps))
    events: List[Tuple[int, int, str]] = []
    cursor = 0
    for idx, word in enumerate(words):
        begin = max(starts[idx], cursor)
        if begin >= total_frames:
            break
        natural_end = starts[idx + 1] if idx + 1 < len(words) else total_frames
        end = max(natural_end, begin + min_frames)
        if idx + 1 == len(words):
            end = last_end if last_end is not None else total_frames
            end = max(end, begin + min_frames)
        end = min(end, total_frames)
        events.append((begin, end, word["display"]))
        cursor = end
    if events and last_end is None:
        last_begin, _, last_text = events[-1]
        events[-1] = (last_begin, total_frames, last_text)
    return events


def _ass_time(frame: int, fps: Fraction) -> str:
    centis = int((Fraction(frame) / fps) * 100)
    hours, rem = divmod(centis, 360000)
    minutes, rem = divmod(rem, 6000)
    seconds, cs = divmod(rem, 100)
    return "%d:%02d:%02d.%02d" % (hours, minutes, seconds, cs)


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(
    events: Sequence[Tuple[int, int, str]],
    fps: Fraction,
    width: int = 1080,
    height: int = 1920,
) -> str:
    """Return ASS text: a blurred shadow layer under a white, stroked text layer.

    Times are floored to centiseconds so a frame-aligned event is never
    rendered one frame late.
    """
    em = ASS_FONT_SIZE * FONT_UNITS_PER_EM / (FONT_WIN_ASCENT + FONT_WIN_DESCENT)
    descent_px = em * FONT_WIN_DESCENT / FONT_UNITS_PER_EM
    cap_px = em * FONT_CAP_HEIGHT / FONT_UNITS_PER_EM
    x = width / 2
    baseline = height * CENTER_Y_FRACTION + cap_px / 2
    bottom = baseline + descent_px
    style = (
        "Style: {name},{font},{size},{primary},&H000000FF,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,{outline},0,2,10,10,10,1"
    )
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: %d" % width,
        "PlayResY: %d" % height,
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
        "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding",
        style.format(name="Shadow", font=FONT_NAME, size=ASS_FONT_SIZE, primary="&H00000000", outline=OUTLINE_PX),
        style.format(name="Main", font=FONT_NAME, size=ASS_FONT_SIZE, primary="&H00FFFFFF", outline=OUTLINE_PX),
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for begin, end, text in events:
        start_t, end_t = _ass_time(begin, fps), _ass_time(end, fps)
        body = _escape(text)
        shadow_tag = "{\\an2\\pos(%.1f,%.1f)\\blur%d\\alpha&H%s&}" % (
            x, bottom + SHADOW_OFFSET_PX, SHADOW_BLUR, SHADOW_ALPHA_HEX)
        main_tag = "{\\an2\\pos(%.1f,%.1f)}" % (x, bottom)
        lines.append("Dialogue: 0,%s,%s,Shadow,,0,0,0,,%s%s" % (start_t, end_t, shadow_tag, body))
        lines.append("Dialogue: 1,%s,%s,Main,,0,0,0,,%s%s" % (start_t, end_t, main_tag, body))
    return "\n".join(lines) + "\n"
