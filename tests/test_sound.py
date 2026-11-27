from __future__ import annotations

import array
import io
import json
import math
import shutil
import wave

import pytest

from pipeline import sound

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is not available")

SECRET = "sk_test_SECRET_0123456789"
WORD_STARTS = [0.2, 0.7, 1.2, 1.9, 2.6, 3.3]


def make_job(root, config=None, duration=4.0, timeline=True):
    job = root / "job"
    job.mkdir()
    words = [{"i": i, "start": start, "end": start + 0.3} for i, start in enumerate(WORD_STARTS)]
    (job / "words.json").write_text(json.dumps({"version": 1, "duration": duration, "words": words}))
    if timeline:
        (job / "timeline.json").write_text(json.dumps({"version": 1, "shots": [{"id": "c1", "start": 0.0}, {"id": "c2", "start": 2.0}]}))
    if config is None:
        config = {
            "ambience": [{"name": "room", "prompt": "quiet bedroom at night", "seconds": 1, "gain_db": -8, "from": 0, "to": None,
                          "loop": True, "fade": 0.5}],
            "cues": [{"name": "creak", "prompt": "slow wooden door creak", "seconds": 1, "gain_db": 0,
                      "at": {"shot": "c2", "offset": 0.5}, "fade_out": 0.1}],
            "prompt_influence": 0.5,
        }
    (job / "sound.json").write_text(json.dumps(config))
    return job


def only(job, name):
    config = json.loads((job / "sound.json").read_text())
    return {key: [entry for entry in config.get(key, []) if entry["name"] == name] for key in ("ambience", "cues")}


def rewrite(job, config):
    (job / "sound.json").write_text(json.dumps(config))


def rms(samples, start, end):
    chunk = samples[int(start * sound.RATE):int(end * sound.RATE)]
    return math.sqrt(sum(value * value for value in chunk) / len(chunk))


def bed_samples(job):
    return sound.read_wav(job / "bed.wav")


def wav_bytes(seconds=1.0):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sound.RATE)
        handle.writeframes(array.array("h", sound.synthetic_samples("fake audio", seconds, "cue")).tobytes())
    return buffer.getvalue()


def timing(tmp_path):
    return sound.Timing(make_job(tmp_path))


def test_time_forms_resolve_against_timeline_and_words(tmp_path):
    clock = timing(tmp_path)
    assert clock.total == pytest.approx(4.5)
    assert clock.resolve(1.25, "x") == pytest.approx(1.25)
    assert clock.resolve({"time": 3.0}, "x") == pytest.approx(3.0)
    assert clock.resolve({"shot": "c2"}, "x") == pytest.approx(2.0)
    assert clock.resolve({"shot": "c2", "offset": 0.75}, "x") == pytest.approx(2.75)
    assert clock.resolve({"word": 3, "offset": -0.1}, "x") == pytest.approx(1.8)
    assert clock.resolve({"word": 0}, "x") == pytest.approx(0.2)


@pytest.mark.parametrize("spec, message", [
    ({"shot": "zz"}, "unknown shot id 'zz'"),
    ({"word": 99}, "outside words.json"),
    ({"word": True}, "outside words.json"),
    ({"shot": "c1", "time": 1}, "must be seconds"),
    ({"bogus": 1}, "must be seconds"),
    ("1.5", "must be seconds"),
    ({"time": "soon"}, "must be a number"),
])
def test_bad_time_specs_are_explained(tmp_path, spec, message):
    with pytest.raises(sound.SoundError, match=message):
        timing(tmp_path).resolve(spec, "cue 'x'.at")


def test_unknown_shot_message_lists_the_known_ones(tmp_path):
    with pytest.raises(sound.SoundError, match="c1, c2"):
        timing(tmp_path).resolve({"shot": "zz"}, "cue 'x'.at")


def test_shot_without_timeline_names_the_missing_file(tmp_path):
    job = make_job(tmp_path, timeline=False)
    with pytest.raises(sound.SoundError, match="timeline.json"):
        sound.Timing(job).resolve({"shot": "c1"}, "cue 'x'.at")
    assert sound.Timing(job).resolve({"time": 1.0}, "x") == 1.0


def test_plan_resolves_ambience_and_cue_spans(tmp_path):
    job = make_job(tmp_path, {
        "ambience": [{"name": "wind", "prompt": "wind", "seconds": 5, "from": {"shot": "c2"}, "to": {"word": 5, "offset": 0.5}}],
        "cues": [{"name": "hit", "prompt": "hit", "seconds": 1, "at": {"time": 1.0}}],
    })
    plan = sound.build_plan(sound.load_config(job), sound.Timing(job))
    wind, hit = plan.items
    assert (wind.start, wind.end) == (pytest.approx(2.0), pytest.approx(3.8))
    assert (hit.start, hit.end) == (pytest.approx(1.0), pytest.approx(2.0))
    assert plan.total == pytest.approx(4.5)
    assert plan.influence == sound.DEFAULT_INFLUENCE


def test_cue_is_silent_before_its_time_and_audible_after(tmp_path):
    job = make_job(tmp_path, {"cues": [{"name": "hit", "prompt": "a hit", "seconds": 1, "at": {"shot": "c2", "offset": 0.5}, "fade_out": 0.1}]})
    sound.make_bed(job, "synthetic")
    samples = bed_samples(job)
    first = next(index for index, value in enumerate(samples) if abs(value) > 1)
    assert abs(first / sound.RATE - 2.5) < 0.01
    assert max(abs(value) for value in samples[:int(2.49 * sound.RATE)]) == 0
    assert rms(samples, 2.5, 2.7) > 500
    assert max(abs(value) for value in samples[int(3.55 * sound.RATE):]) == 0


def test_cue_past_the_end_is_refused(tmp_path):
    job = make_job(tmp_path, {"cues": [{"name": "late", "prompt": "late", "seconds": 1, "at": {"time": 4.5}}]})
    with pytest.raises(sound.SoundError, match="cue 'late' starts at 4.50 s, outside the bed"):
        sound.make_bed(job, "synthetic")


def test_cue_running_past_the_end_is_trimmed(tmp_path):
    job = make_job(tmp_path, {"cues": [{"name": "tail", "prompt": "tail", "seconds": 2, "at": {"time": 4.0}}]})
    report = sound.make_bed(job, "synthetic")
    assert report["items"][0]["end"] == pytest.approx(4.5)
    assert len(bed_samples(job)) == int(4.5 * sound.RATE)


def test_ambience_loops_across_its_span_and_fades_at_both_ends(tmp_path):
    job = make_job(tmp_path, {"ambience": [{"name": "room", "prompt": "room tone", "seconds": 1, "from": 0.5, "to": 4.0, "fade": 1.0}]})
    sound.make_bed(job, "synthetic")
    samples = bed_samples(job)
    assert max(abs(value) for value in samples[:int(0.49 * sound.RATE)]) == 0
    assert max(abs(value) for value in samples[int(4.01 * sound.RATE):]) == 0
    middle = rms(samples, 2.0, 3.0)
    assert middle > 1000
    assert rms(samples, 0.5, 0.55) < 0.2 * middle
    assert rms(samples, 3.95, 4.0) < 0.2 * middle
    assert rms(samples, 3.0, 3.5) > 0.5 * middle


def test_ambience_without_loop_plays_once(tmp_path):
    job = make_job(tmp_path, {"ambience": [{"name": "room", "prompt": "room tone", "seconds": 1, "loop": False, "fade": 0.05}]})
    report = sound.make_bed(job, "synthetic")
    samples = bed_samples(job)
    assert report["items"][0]["end"] == pytest.approx(1.0)
    assert rms(samples, 0.3, 0.7) > 1000
    assert max(abs(value) for value in samples[int(1.01 * sound.RATE):]) == 0


def test_loop_clip_seams_do_not_jump():
    clip = [float(value) for value in sound.synthetic_samples("seam", 1.0, "ambience")]
    looped = sound.loop_clip(clip, 3 * len(clip))
    assert len(looped) == 3 * len(clip)
    steps = [abs(looped[i + 1] - looped[i]) for i in range(len(looped) - 1)]
    assert max(steps) < 3 * max(abs(clip[i + 1] - clip[i]) for i in range(len(clip) - 1))


def test_peak_is_minus_three_dbfs_even_with_hot_gains(tmp_path):
    job = make_job(tmp_path)
    config = json.loads((job / "sound.json").read_text())
    config["cues"][0]["gain_db"] = 24
    config["ambience"][0]["gain_db"] = 12
    rewrite(job, config)
    report = sound.make_bed(job, "synthetic")
    samples = bed_samples(job)
    peak = max(abs(value) for value in samples)
    assert 20 * math.log10(peak / 32768.0) == pytest.approx(-3.0, abs=0.01)
    assert report["peak_dbfs"] == pytest.approx(-3.0, abs=0.01)
    assert peak < 32767


def test_quiet_gains_are_brought_up_to_the_same_peak(tmp_path):
    job = make_job(tmp_path)
    config = json.loads((job / "sound.json").read_text())
    config["cues"][0]["gain_db"] = -40
    config["ambience"][0]["gain_db"] = -40
    rewrite(job, config)
    assert sound.make_bed(job, "synthetic")["peak_dbfs"] == pytest.approx(-3.0, abs=0.01)


def test_report_contents_and_bed_format(tmp_path):
    job = make_job(tmp_path)
    report = sound.make_bed(job, "synthetic")
    assert json.loads((job / "sound_report.json").read_text()) == report
    assert set(report) >= {"duration", "provider", "items", "peak_dbfs", "generated", "reused", "wall_seconds"}
    assert report["duration"] == pytest.approx(4.5)
    assert report["provider"] == "synthetic"
    assert (report["generated"], report["reused"]) == (2, 0)
    room, creak = report["items"]
    assert set(room) == {"name", "kind", "prompt", "file", "cached", "start", "end", "gain_db", "cost"}
    assert (room["name"], room["kind"], room["cached"], room["gain_db"]) == ("room", "ambience", False, -8)
    assert (room["start"], room["end"]) == (0.0, pytest.approx(4.5))
    assert (creak["name"], creak["kind"], creak["start"], creak["end"]) == ("creak", "cue", 2.5, 3.5)
    assert creak["prompt"] == "slow wooden door creak"
    assert creak["cost"] is None
    assert (job / creak["file"]).is_file()
    with wave.open(str(job / "bed.wav"), "rb") as handle:
        assert (handle.getnchannels(), handle.getsampwidth(), handle.getframerate()) == (1, 2, 48000)
        assert handle.getnframes() == int(4.5 * 48000)


def test_bed_is_deterministic(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = make_job(tmp_path / "a")
    second = make_job(tmp_path / "b")
    sound.make_bed(first, "synthetic")
    sound.make_bed(second, "synthetic")
    assert (first / "bed.wav").read_bytes() == (second / "bed.wav").read_bytes()


def test_cache_file_names_and_sidecars(tmp_path):
    job = make_job(tmp_path)
    report = sound.make_bed(job, "synthetic")
    creak = report["items"][1]
    key = sound.cache_key("synthetic", "slow wooden door creak", 1, 0.5)
    assert creak["file"] == "sfx/%s.wav" % key
    sidecar = json.loads((job / "sfx" / (key + ".json")).read_text())
    assert sidecar["prompt"] == "slow wooden door creak"
    assert len(key) == 16


def test_rerun_reuses_everything_and_refresh_regenerates_one(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    sound.make_bed(job, "synthetic")
    files = sorted((job / "sfx").glob("*.wav"))
    stamps = {path: path.stat().st_mtime_ns for path in files}
    calls = []
    real = sound.generate
    monkeypatch.setattr(sound, "generate", lambda *args, **kwargs: calls.append(args[0]) or real(*args, **kwargs))
    again = sound.make_bed(job, "synthetic")
    assert (again["generated"], again["reused"]) == (0, 2)
    assert all(entry["cached"] for entry in again["items"])
    assert calls == []
    assert {path: path.stat().st_mtime_ns for path in files} == stamps
    refreshed = sound.make_bed(job, "synthetic", refresh=["creak"])
    assert (refreshed["generated"], refreshed["reused"]) == (1, 1)
    assert calls == ["slow wooden door creak"]
    assert [entry["cached"] for entry in refreshed["items"]] == [True, False]


def test_changed_prompt_or_seconds_makes_a_new_sound(tmp_path):
    job = make_job(tmp_path)
    sound.make_bed(job, "synthetic")
    config = json.loads((job / "sound.json").read_text())
    config["cues"][0]["seconds"] = 1.5
    rewrite(job, config)
    report = sound.make_bed(job, "synthetic")
    assert (report["generated"], report["reused"]) == (1, 1)
    assert len(list((job / "sfx").glob("*.wav"))) == 3


def test_pad_is_mixed_underneath(tmp_path):
    job = make_job(tmp_path, {"pad": {"mood": "sad", "gain_db": -14},
                              "cues": [{"name": "hit", "prompt": "hit", "seconds": 1, "at": {"time": 2.0}}]}, duration=2.5)
    report = sound.make_bed(job, "synthetic")
    samples = bed_samples(job)
    assert report["pad"] == {"mood": "sad", "gain_db": -14.0}
    assert report["peak_dbfs"] == pytest.approx(-3.0, abs=0.01)
    assert rms(samples, 1.0, 1.5) > 50
    assert rms(samples, 1.0, 1.5) < 0.3 * rms(samples, 2.0, 2.1)


def test_dry_run_writes_nothing_and_needs_no_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(sound.KEY_VAR, raising=False)
    job = make_job(tmp_path)
    assert sound.main(["--job", str(job), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "creak" in out and "room" in out and "generate" in out
    assert "2 to generate, 0 cached" in out
    assert not (job / "sfx").exists() and not (job / "bed.wav").exists() and not (job / "sound_report.json").exists()


def test_dry_run_shows_cached_sounds(tmp_path, capsys):
    job = make_job(tmp_path)
    sound.make_bed(job, "synthetic")
    assert sound.main(["--job", str(job), "--provider", "synthetic", "--dry-run", "--refresh", "creak"]) == 0
    assert "1 to generate, 1 cached" in capsys.readouterr().out


def test_cli_runs_and_prints_a_summary(tmp_path, capsys):
    job = make_job(tmp_path)
    assert sound.main(["--job", str(job), "--provider", "synthetic"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("bed.wav: 4.50 s, peak -3.00 dBFS, 2 generated, 0 reused")
    assert (job / "bed.wav").is_file()


def test_cli_refresh_accepts_several_names(tmp_path):
    job = make_job(tmp_path)
    sound.make_bed(job, "synthetic")
    assert sound.main(["--job", str(job), "--provider", "synthetic", "--refresh", "room", "creak"]) == 0
    assert json.loads((job / "sound_report.json").read_text())["generated"] == 2


@pytest.mark.parametrize("config, message", [
    ({"cues": [{"name": "c", "at": {"time": 1}}]}, "cue 'c' needs a non-empty 'prompt'"),
    ({"cues": [{"name": "c", "prompt": "  ", "at": {"time": 1}}]}, "non-empty 'prompt'"),
    ({"ambience": [{"name": "a"}]}, "ambience 'a' needs a non-empty 'prompt'"),
    ({"cues": [{"prompt": "x", "at": {"time": 1}}]}, "needs a non-empty 'name'"),
    ({"cues": [{"name": "c", "prompt": "x"}]}, 'needs an "at" time'),
    ({"cues": [{"name": "c", "prompt": "x", "at": {"shot": "nope"}}]}, "unknown shot id 'nope'"),
    ({"cues": [{"name": "c", "prompt": "x", "at": {"time": 9}}]}, "outside the bed"),
    ({"cues": [{"name": "c", "prompt": "x", "at": {"time": -1}}]}, "outside the bed"),
    ({"cues": [{"name": "c", "prompt": "x", "at": 1}], "ambience": [{"name": "c", "prompt": "y"}]}, "used twice"),
    ({"ambience": [{"name": "a", "prompt": "x", "from": 3, "to": 2}]}, "not after its start"),
    ({"ambience": [{"name": "a", "prompt": "x", "from": 9}]}, "outside the bed"),
    ({"ambience": [{"name": "a", "prompt": "x", "seconds": 0}]}, "seconds must be positive"),
    ({"ambience": [{"name": "a", "prompt": "x", "loop": "yes"}]}, "loop must be true or false"),
    ({"ambience": [{"name": "a", "prompt": "x"}], "prompt_influence": 2}, "between 0 and 1"),
    ({"pad": {"mood": "angry"}}, "pad needs a 'mood'"),
    ({}, "nothing to mix"),
])
def test_validation_errors_are_actionable(tmp_path, config, message):
    job = make_job(tmp_path, config)
    with pytest.raises(sound.SoundError, match=message):
        sound.make_bed(job, "synthetic")
    assert not (job / "bed.wav").exists()


def test_missing_inputs_are_named(tmp_path):
    job = make_job(tmp_path)
    (job / "words.json").unlink()
    with pytest.raises(sound.SoundError, match="words.json not found"):
        sound.make_bed(job, "synthetic")
    (job / "sound.json").unlink()
    with pytest.raises(sound.SoundError, match="sound.json not found"):
        sound.load_config(job)


def test_unknown_refresh_name_lists_the_known_ones(tmp_path):
    job = make_job(tmp_path)
    with pytest.raises(sound.SoundError, match="--refresh names nope but sound.json has only: room, creak"):
        sound.make_bed(job, "synthetic", refresh=["nope"])


def test_cli_reports_errors_with_exit_code_two(tmp_path, capsys):
    job = make_job(tmp_path, {"cues": [{"name": "c", "at": {"time": 1}}]})
    assert sound.main(["--job", str(job), "--provider", "synthetic"]) == 2
    assert "needs a non-empty 'prompt'" in capsys.readouterr().err


def test_missing_key_names_only_the_variable(tmp_path, monkeypatch):
    monkeypatch.delenv(sound.KEY_VAR, raising=False)
    job = make_job(tmp_path)
    with pytest.raises(sound.SoundError) as caught:
        sound.make_bed(job, "elevenlabs")
    assert str(caught.value) == "ELEVENLABS_API_KEY is not set in the environment"
    assert not (job / "sfx").exists() or not list((job / "sfx").glob("*.wav"))


def test_a_cached_rerun_needs_no_key(tmp_path, monkeypatch):
    monkeypatch.delenv(sound.KEY_VAR, raising=False)
    job = make_job(tmp_path)
    sound.make_bed(job, "synthetic")
    plan = sound.build_plan(sound.load_config(job), sound.Timing(job))
    for item in plan.items:
        stand_in = job / "sfx" / (sound.cache_key("synthetic", item.prompt, item.seconds, plan.influence) + ".wav")
        stand_in.rename(job / "sfx" / (sound.cache_key("elevenlabs", item.prompt, item.seconds, plan.influence) + ".wav"))
    report = sound.make_bed(job, "elevenlabs")
    assert (report["generated"], report["reused"]) == (0, 2)


class FakeApi:
    def __init__(self, statuses=(200,), cost="42"):
        self.calls = []
        self.statuses = list(statuses)
        self.cost = cost

    def __call__(self, url, headers, body, timeout=0):
        self.calls.append({"url": url, "headers": dict(headers), "body": json.loads(body.decode("utf-8"))})
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status != 200:
            return status, {}, ("error echoing %s" % headers["xi-api-key"]).encode("utf-8")
        reply = {"content-type": "audio/mpeg"}
        if self.cost is not None:
            reply["Character-Cost"] = self.cost
        return 200, reply, wav_bytes(1.0)


@pytest.fixture
def api(monkeypatch):
    fake = FakeApi()
    monkeypatch.setenv(sound.KEY_VAR, SECRET)
    monkeypatch.setattr(sound, "http_post", fake)
    monkeypatch.setattr(sound, "pause", lambda seconds: None)
    return fake


@needs_ffmpeg
def test_elevenlabs_request_follows_the_documented_shape(tmp_path, api):
    job = make_job(tmp_path, {
        "ambience": [{"name": "room", "prompt": "quiet room", "seconds": 40, "from": 0, "to": None}],
        "cues": [{"name": "creak", "prompt": "door creak", "seconds": 0.1, "at": {"time": 1.0}}],
        "prompt_influence": 0.5})
    report = sound.make_bed(job, "elevenlabs")
    assert len(api.calls) == 2
    room, creak = api.calls
    assert room["url"] == "https://api.elevenlabs.io/v1/sound-generation"
    assert room["headers"]["xi-api-key"] == SECRET
    assert room["body"] == {"text": "quiet room", "duration_seconds": 22.0, "prompt_influence": 0.5}
    assert creak["body"] == {"text": "door creak", "duration_seconds": 0.5, "prompt_influence": 0.5}
    assert [entry["cost"] for entry in report["items"]] == ["42", "42"]
    assert report["provider"] == "elevenlabs"
    assert report["peak_dbfs"] == pytest.approx(-3.0, abs=0.01)
    with wave.open(str(job / report["items"][0]["file"]), "rb") as handle:
        assert (handle.getnchannels(), handle.getsampwidth(), handle.getframerate()) == (1, 2, 48000)


@needs_ffmpeg
def test_the_key_never_reaches_a_file_or_the_output(tmp_path, api, capsys):
    job = make_job(tmp_path)
    assert sound.main(["--job", str(job), "--provider", "elevenlabs"]) == 0
    sound.main(["--job", str(job), "--provider", "elevenlabs", "--dry-run"])
    streams = capsys.readouterr()
    assert SECRET not in streams.out + streams.err
    files = [path for path in job.rglob("*") if path.is_file()]
    assert files
    for path in files:
        assert SECRET.encode("utf-8") not in path.read_bytes(), path
    assert "creak" in (job / "sound_report.json").read_text()


@needs_ffmpeg
def test_cost_header_is_found_case_insensitively_and_absent_is_null(tmp_path, monkeypatch):
    fake = FakeApi(cost=None)
    monkeypatch.setenv(sound.KEY_VAR, SECRET)
    monkeypatch.setattr(sound, "http_post", fake)
    job = make_job(tmp_path)
    assert [entry["cost"] for entry in sound.make_bed(job, "elevenlabs")["items"]] == [None, None]
    monkeypatch.setattr(sound, "http_post", lambda url, headers, body, timeout=0: (200, {"CHARACTER-COST": "7"}, wav_bytes()))
    assert [entry["cost"] for entry in sound.make_bed(job, "elevenlabs", refresh=["room"])["items"]] == ["7", None]


@needs_ffmpeg
def test_cached_sounds_are_not_billed_again(tmp_path, api):
    job = make_job(tmp_path)
    sound.make_bed(job, "elevenlabs")
    assert len(api.calls) == 2
    again = sound.make_bed(job, "elevenlabs")
    assert len(api.calls) == 2
    assert (again["generated"], again["reused"]) == (0, 2)
    assert [entry["cost"] for entry in again["items"]] == [None, None]
    sound.make_bed(job, "elevenlabs", refresh=["creak"])
    assert len(api.calls) == 3


@needs_ffmpeg
def test_rate_limits_and_server_errors_are_retried(tmp_path, api, monkeypatch):
    api.statuses = [429, 503, 500, 200]
    waits = []
    monkeypatch.setattr(sound, "pause", waits.append)
    job = make_job(tmp_path, {"cues": [{"name": "hit", "prompt": "a hit", "seconds": 1, "at": {"time": 1.0}}]})
    report = sound.make_bed(job, "elevenlabs")
    assert len(api.calls) == 4
    assert waits == [1.0, 2.0, 4.0]
    assert report["generated"] == 1


def test_retries_stop_after_three_and_the_error_hides_the_key(tmp_path, api):
    api.statuses = [503]
    job = make_job(tmp_path, {"cues": [{"name": "hit", "prompt": "a hit", "seconds": 1, "at": {"time": 1.0}}]})
    with pytest.raises(sound.SoundError) as caught:
        sound.make_bed(job, "elevenlabs")
    assert len(api.calls) == 4
    assert "HTTP 503" in str(caught.value)
    assert SECRET not in str(caught.value)
    assert "***" in str(caught.value)
    assert not (job / "bed.wav").exists()
    assert not list((job / "sfx").glob("*.wav"))


def test_client_errors_are_not_retried(tmp_path, api):
    api.statuses = [401]
    job = make_job(tmp_path, {"cues": [{"name": "hit", "prompt": "a hit", "seconds": 1, "at": {"time": 1.0}}]})
    with pytest.raises(sound.SoundError, match="HTTP 401"):
        sound.make_bed(job, "elevenlabs")
    assert len(api.calls) == 1


def test_main_scrubs_the_key_from_errors(tmp_path, api, capsys):
    api.statuses = [422]
    job = make_job(tmp_path, {"cues": [{"name": "hit", "prompt": "a hit", "seconds": 1, "at": {"time": 1.0}}]})
    assert sound.main(["--job", str(job), "--provider", "elevenlabs"]) == 2
    err = capsys.readouterr().err
    assert "HTTP 422" in err and SECRET not in err


def test_generate_synthetic_is_deterministic_and_clamped(tmp_path):
    first = sound.generate("rain", 40, tmp_path / "a.wav", prompt_influence=0.3, provider="synthetic", kind="ambience")
    second = sound.generate("rain", 22, tmp_path / "b.wav", prompt_influence=0.9, provider="synthetic", kind="ambience")
    assert first["seconds"] == pytest.approx(22.0)
    assert (tmp_path / "a.wav").read_bytes() == (tmp_path / "b.wav").read_bytes()
    assert second["cost"] is None
    short = sound.generate("tick", 0.1, tmp_path / "c.wav", prompt_influence=0.3, provider="synthetic")
    assert short["seconds"] == pytest.approx(0.5)
    other = sound.generate("tock", 0.5, tmp_path / "d.wav", prompt_influence=0.3, provider="synthetic")
    assert (tmp_path / "c.wav").read_bytes() != (tmp_path / "d.wav").read_bytes()
    assert other["file"].endswith("d.wav")


def test_generate_rejects_an_unknown_provider(tmp_path):
    with pytest.raises(sound.SoundError, match="unknown provider 'bark'"):
        sound.generate("x", 1, tmp_path / "x.wav", prompt_influence=0.3, provider="bark")



def test_music_ducks_under_cues_and_goes_silent_in_cuts():
    items = [sound.Item("hit", "cue", "boom", 1.0, 0.0, 2.0, 3.0, False)]
    music = {"prompt": "drone", "gain_db": 0.0, "duck_db": -6.0, "cuts": [[4.0, 0.5]], "fade_in": 0.1, "fade_out": 0.1, "fallback": "sad"}
    envelope = sound.music_envelope(sound.Plan(items, 6.0, 0.3, None, music, True), 6 * sound.RATE)

    def at(seconds):
        return envelope[int(seconds * sound.RATE)]

    assert at(0.0) == 0.0 and at(1.0) == pytest.approx(1.0)
    assert at(2.5) == pytest.approx(10 ** (-6 / 20.0))
    assert at(4.2) == 0.0 and at(5.5) == pytest.approx(1.0)


def test_level_brings_clips_to_a_common_loudness():
    quiet = [0.01 * math.sin(i / 10.0) for i in range(4800)]
    levelled = sound.level(quiet, "ambience")
    assert math.sqrt(sum(v * v for v in levelled) / len(levelled)) == pytest.approx(sound.LEVEL_RMS)
    assert max(abs(v) for v in sound.level(quiet, "cue")) == pytest.approx(sound.LEVEL_PEAK)
    assert sound.level([0.0] * 10, "cue") == [0.0] * 10


def test_music_uses_the_pad_with_the_synthetic_provider(tmp_path):
    job = make_job(tmp_path, {"normalise": True, "music": {"prompt": "slow uneasy drone", "cuts": [{"at": {"shot": "c2", "offset": 0.0}, "seconds": 0.5}]},
                              "cues": [{"name": "creak", "prompt": "door creak", "seconds": 1, "at": 1.0}]})
    report = sound.make_bed(job, provider="synthetic")
    assert report["music"]["source"].startswith("synthetic pad") and report["music"]["cuts"] == [[2.0, 0.5]]
    assert report["normalise"] is True and (job / "bed.wav").is_file()


def test_music_needs_a_prompt(tmp_path):
    job = make_job(tmp_path, {"music": {"gain_db": -3}})
    with pytest.raises(sound.SoundError, match="prompt"):
        sound.make_bed(job, provider="synthetic")
