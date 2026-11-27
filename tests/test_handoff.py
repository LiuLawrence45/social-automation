from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline import handoff

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"

WORDS = {"version": 1, "audio": "narration.wav", "duration": 5.0,
         "words": [{"i": 0, "token": "Nine", "display": "9", "start": 0.1, "end": 0.4},
                   {"i": 1, "token": "people", "display": "people", "start": 0.4, "end": 0.9, "sentence": 0}]}
SHOTS = {"version": 1, "fps": 30, "width": 1080, "height": 1920, "content_start": 0.0, "content_end": 5.0,
         "shots": [{"id": "s01", "start": 0.0, "end": 2.0, "word_start": 0, "word_end": 1, "set": "x", "emphasis": True},
                   {"id": "s02", "start": 2.0, "end": 5.0}]}


def mutated(base, *path_and_value):
    data = copy.deepcopy(base)
    *path, value = path_and_value
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return data


def test_valid_files_pass():
    handoff.validate_words(WORDS)
    handoff.validate_shots(SHOTS, 2, 5.0)


@pytest.mark.parametrize("bad", [
    mutated(WORDS, "version", 2),
    mutated(WORDS, "duration", 0),
    mutated(WORDS, "words", []),
    mutated(WORDS, "words", 1, "i", 5),
    mutated(WORDS, "words", 0, "display", ""),
    mutated(WORDS, "words", 0, "end", 0.05),
    mutated(WORDS, "words", 1, "start", 0.05),
    mutated(WORDS, "words", 1, "end", 9.0),
    mutated(WORDS, "words", 0, "sentence", -1),
])
def test_bad_words_rejected(bad):
    with pytest.raises(handoff.HandoffError):
        handoff.validate_words(bad)


@pytest.mark.parametrize("bad", [
    mutated(SHOTS, "width", 720),
    mutated(SHOTS, "content_end", 0.0),
    mutated(SHOTS, "content_end", 9.0),
    mutated(SHOTS, "shots", 1, "start", 1.0),
    mutated(SHOTS, "shots", 1, "end", 9.0),
    mutated(SHOTS, "shots", 1, "id", "s01"),
    mutated(SHOTS, "shots", 0, "word_end", 99),
    mutated(SHOTS, "shots", 0, "emphasis", "yes"),
    mutated(SHOTS, "shots", 0, "end", 0.0),
])
def test_bad_shots_rejected(bad):
    with pytest.raises(handoff.HandoffError):
        handoff.validate_shots(bad, 2, 5.0)


def test_missing_file(tmp_path):
    with pytest.raises(handoff.HandoffError):
        handoff.load_job(tmp_path)


def test_schema_files_match_validator():
    words = json.loads((SCHEMAS / "words.schema.json").read_text())
    shots = json.loads((SCHEMAS / "shots.schema.json").read_text())
    assert set(words["required"]) == set(WORDS)
    assert set(shots["required"]) <= set(SHOTS)
    assert set(words["properties"]["words"]["items"]["required"]) <= set(WORDS["words"][0])
