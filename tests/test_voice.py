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

import base64
import io
import json
import shutil

from pipeline import handoff, story as story_mod

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not available")

LINES = [
    {"speaker": "narrator", "text": "Nine people went in."},
    {"speaker": "narrator", "text": "Nobody came out."},
    {"speaker": "ghost", "text": "Stay with me."},
    {"speaker": "narrator", "text": "Then it was quiet."},
]
VOICE = {"speakers": {"ghost": {"voice_id": "GHOST", "voice_name": "Echo", "process": {"tempo": 0.5}}}}


def make_story(lines=LINES, voice=VOICE, script=None):
    data = {"version": 1, "slug": "demo", "title": "Demo", "source": {"kind": "original"}, "voice": dict(voice)}
    if lines is not None:
        data["lines"] = lines
    else:
        data["script"] = script
    return data


def make_job(tmp_path, story_data):
    job = tmp_path / "job"
    job.mkdir()
    (job / "story.json").write_text(json.dumps(story_data))
    return job


class FakeSynth:
    def __init__(self):
        self.calls = []

    def __call__(self, text, tokens, voice, workdir):
        self.calls.append({"text": text, "voice_id": voice["voice_id"], "previous": voice.get("previous_text"),
                           "next": voice.get("next_text")})
        wav = workdir / "raw.wav"
        voice_mod_write(wav, [0] * int(0.1 * RATE) + tone(0.3 * len(tokens)))
        spans = [(0.1 + 0.3 * index, 0.1 + 0.3 * index + 0.25) for index in range(len(tokens))]
        return {"provider": "elevenlabs", "wav": wav, "spans": spans,
                "meta": {"model": "m", "voice_id": voice["voice_id"], "voice_name": voice.get("voice_name"),
                         "characters": len(text), "characters_billed": str(len(text))}}


def voice_mod_write(path, samples):
    voice.write_wav(path, array.array("h", samples))


@pytest.fixture
def fake(monkeypatch):
    synth = FakeSynth()
    monkeypatch.setattr(voice, "synthesize", synth)
    return synth


def test_lines_join_into_the_script_and_a_plain_story_is_one_narrator_line():
    data = story_mod.validate_story(make_story())
    assert data["script"] == "Nine people went in. Nobody came out. Stay with me. Then it was quiet."
    plain = story_mod.validate_story(make_story(None, {}, data["script"]))
    assert story_mod.tokenize(plain["script"]) == story_mod.tokenize(data["script"])
    assert story_mod.story_lines(plain) == [{"speaker": "narrator", "text": data["script"]}]


def test_lines_are_validated():
    with pytest.raises(story_mod.StoryError, match="lines\\[1\\].speaker"):
        story_mod.validate_story(make_story([{"speaker": "a", "text": "Hi."}, {"speaker": "", "text": "Yo."}]))


def test_consecutive_lines_by_one_speaker_form_a_segment():
    segments = voice.make_segments(LINES)
    assert [(s["speaker"], s["text"]) for s in segments] == [
        ("narrator", "Nine people went in. Nobody came out."), ("ghost", "Stay with me."), ("narrator", "Then it was quiet.")]
    assert [len(s["tokens"]) for s in segments] == [7, 3, 4]


@pytest.mark.parametrize("process, message", [
    ({"pitch": 3}, "pitch must be a number between 0.5 and 2.0"),
    ({"tempo": "fast"}, "tempo must be a number"),
    ({"filter": ""}, "filter must be a non-empty"),
    ({"gain_db": 99}, "gain_db must be a number"),
    ({"loud": 1}, "unknown keys loud"),
])
def test_process_errors_are_clear(process, message):
    cfg = dict(voice.DEFAULTS, speakers={"ghost": {"process": process}})
    with pytest.raises(voice.VoiceError, match=message):
        voice.speaker_voice(cfg, "ghost")


def test_process_filter_builds_the_chain():
    chain = voice.process_filter({"pitch": 0.5, "tempo": 0.95, "filter": "highpass=f=300", "gain_db": -3})
    assert chain == "asetrate=24000,aresample=48000,atempo=2.000000,atempo=0.950000,highpass=f=300,volume=-3.000dB"
    assert voice.process_filter({}) == ""


@needs_ffmpeg
def test_segments_use_their_voice_context_and_scaled_alignment(tmp_path, fake):
    cfg = dict(voice.DEFAULTS, speakers=VOICE["speakers"])
    take = voice.synthesize_segments(cfg, voice.make_segments(LINES), tmp_path)
    assert [c["voice_id"] for c in fake.calls] == [voice.DEFAULTS["voice_id"], "GHOST", voice.DEFAULTS["voice_id"]]
    assert fake.calls[0]["previous"] is None and fake.calls[0]["next"] == "Stay with me."
    assert fake.calls[1]["previous"] == "Nine people went in. Nobody came out." and fake.calls[1]["next"] == "Then it was quiet."
    assert fake.calls[2]["next"] is None
    first = 0.1 + 0.3 * 7
    ghost_start = first + 0.12
    spans = take["spans"]
    assert len(spans) == 14
    assert spans[7][0] == pytest.approx(ghost_start + 0.1 / 0.5, abs=0.01)
    assert spans[7][1] - spans[7][0] == pytest.approx(0.25 / 0.5, abs=0.01)
    ghost_len = (0.1 + 0.9) / 0.5
    assert spans[10][0] == pytest.approx(ghost_start + ghost_len + 0.12 + 0.1, abs=0.02)
    assert voice.wav_duration(take["wav"]) == pytest.approx(first + 0.12 + ghost_len + 0.12 + 0.1 + 1.2, abs=0.02)


@needs_ffmpeg
def test_segment_cache_bills_only_changed_segments(tmp_path, fake):
    cfg = dict(voice.DEFAULTS, speakers=VOICE["speakers"])
    voice.synthesize_segments(cfg, voice.make_segments(LINES), tmp_path)
    assert len(fake.calls) == 3
    again = voice.synthesize_segments(cfg, voice.make_segments(LINES), tmp_path)
    assert len(fake.calls) == 3
    assert again["speakers"]["ghost"]["cached_segments"] == 1 and again["speakers"]["ghost"]["characters_sent"] == 0
    changed = LINES[:3] + [{"speaker": "narrator", "text": "Then it was calm."}]
    voice.synthesize_segments(cfg, voice.make_segments(changed), tmp_path)
    assert [c["text"] for c in fake.calls[3:]] == ["Stay with me.", "Then it was calm."]


@needs_ffmpeg
def test_make_narration_tags_words_and_reports_speakers(tmp_path, fake):
    job = make_job(tmp_path, make_story())
    report = voice.make_narration(job)
    words = json.loads((job / "words.json").read_text())
    speakers = [word["speaker"] for word in words["words"]]
    assert speakers == ["narrator"] * 7 + ["ghost"] * 3 + ["narrator"] * 4
    handoff.validate_words(words)
    assert report["speakers"]["ghost"]["voice_id"] == "GHOST"
    assert report["speakers"]["ghost"]["voice_name"] == "Echo"
    assert report["speakers"]["ghost"]["segments"] == 1
    assert report["speakers"]["ghost"]["characters_sent"] == len("Stay with me.")
    assert report["speakers"]["ghost"]["characters_billed"] == len("Stay with me.")
    assert report["speakers"]["ghost"]["process"] == {"tempo": 0.5}
    assert report["speakers"]["narrator"]["segments"] == 2
    assert json.loads((job / "voice_report.json").read_text())["speakers"].keys() == {"narrator", "ghost"}
    calls = len(fake.calls)
    voice.make_narration(job, reuse_raw=True)
    assert len(fake.calls) == calls


@needs_ffmpeg
def test_a_story_without_lines_is_one_narrator_segment(tmp_path, fake):
    job = make_job(tmp_path, make_story(None, {}, "Nine people went in. Nobody came out."))
    report = voice.make_narration(job)
    assert len(fake.calls) == 1
    assert fake.calls[0]["previous"] is None and fake.calls[0]["next"] is None
    words = json.loads((job / "words.json").read_text())["words"]
    assert {word["speaker"] for word in words} == {"narrator"}
    assert list(report["speakers"]) == ["narrator"]


def test_words_with_and_without_speaker_pass_handoff():
    word = {"i": 0, "token": "Hi", "display": "Hi", "start": 0.1, "end": 0.3}
    base = {"version": 1, "audio": "narration.wav", "duration": 1.0}
    handoff.validate_words(dict(base, words=[word]))
    handoff.validate_words(dict(base, words=[dict(word, speaker="ghost")]))


class FakeResponse(io.BytesIO):
    headers = {"character-cost": "13"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@needs_ffmpeg
def test_request_carries_voice_id_and_neighbour_text(tmp_path, monkeypatch):
    sent = []

    def fake_urlopen(request, timeout=0):
        body = json.loads(request.data.decode("utf-8"))
        sent.append((request.full_url, body))
        text = body["text"]
        payload = {"audio_base64": base64.b64encode(bytes(2 * 4800)).decode("ascii"),
                   "alignment": {"characters": list(text), "character_start_times_seconds": [0.01 * i for i in range(len(text))],
                                 "character_end_times_seconds": [0.01 * i + 0.01 for i in range(len(text))]}}
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setattr(voice.urllib.request, "urlopen", fake_urlopen)
    cfg = dict(voice.DEFAULTS, provider="elevenlabs", speakers=VOICE["speakers"])
    voice.synthesize_segments(cfg, voice.make_segments(LINES), tmp_path)
    assert "/text-to-speech/GHOST/with-timestamps" in sent[1][0]
    assert sent[1][1]["previous_text"] == "Nine people went in. Nobody came out."
    assert sent[1][1]["next_text"] == "Then it was quiet."
    assert "previous_text" not in sent[0][1] and sent[0][1]["next_text"] == "Stay with me."
    assert voice.DEFAULTS["voice_id"] in sent[0][0]

