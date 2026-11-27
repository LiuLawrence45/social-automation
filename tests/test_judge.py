from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from pipeline import judge, vision

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="FFmpeg is not available")
CONFIG = {"provider": "fake", "model": "fake-model", "still_height": 96, "ceiling_usd": 100.0}


def write_png(path: Path, rgb, width: int = 108, height: int = 192) -> None:
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def make_job(root: Path, story_extra=None) -> Path:
    job = root / "job"
    job.mkdir()
    shots = [
        {"id": "s01", "text": "On Monday, I found a box.", "start": 0.0, "end": 2.0, "frame_start": 0, "frame_end": 60, "set": "hall",
         "cast": [{"who": "me", "at": "door"}], "camera": {"on": "me", "frame": "full", "yaw": 14, "move": "push", "dof": 0.4}},
        {"id": "s02", "text": "It was not mine.", "start": 2.0, "end": 4.0, "frame_start": 60, "frame_end": 120, "set": "hall",
         "cast": [{"who": "me", "at": "door"}, {"who": "manager", "at": "desk"}], "camera": {"on": "me", "frame": "close"}},
        {"id": "s03", "text": "Nobody came.", "start": 4.0, "end": 6.0, "frame_start": 120, "frame_end": 180, "set": "hall",
         "cast": [], "camera": {"from": "my_door_wide", "move": "push"}},
    ]
    (job / "timeline.json").write_text(json.dumps({"fps": 30, "shots": shots}))
    story = {"title": "The Box", "genre": "mystery"}
    story.update(story_extra or {})
    (job / "story.json").write_text(json.dumps(story))
    (job / "words.json").write_text(json.dumps({"words": []}))
    return job


class FakeStager:
    def __init__(self, bad=(), all_bad=False):
        self.bad = set(bad)
        self.all_bad = all_bad
        self.geometry_calls = []
        self.still_calls = []

    def geometry(self, shot_id, cameras, previous):
        self.geometry_calls.append((shot_id, cameras, previous))
        out = []
        for index, camera in enumerate(cameras):
            failing = self.all_bad or camera.get("yaw") in self.bad
            out.append({"ok": not failing, "issues": ["head out of frame"] * (1 + index % 2) if failing else [], "metrics": {}})
        return out

    def stills(self, shot_id, cameras, out_dir):
        self.still_calls.append((shot_id, cameras, out_dir))
        paths = []
        for index, _ in enumerate(cameras):
            path = Path(out_dir) / ("c%d.png" % index)
            write_png(path, (40 * index % 255, 90, 160))
            paths.append(path)
        return paths


class FakeAsk:
    def __init__(self, totals=None, default=5.0, cost=0.01):
        self.totals = totals or {}
        self.default = default
        self.cost = cost
        self.calls = []

    def __call__(self, provider, model, system, prompt, images, **kwargs):
        self.calls.append({"prompt": prompt, "images": list(images), "kwargs": kwargs})
        if "contact sheet of candidate camera setups" in prompt:
            names = dict(re.findall(r"(?m)^([A-Z]): (\S+) \(geometry notes", prompt))
            items = []
            for label, name in re.findall(r"(?m)^([A-Z]): (\S+) \(geometry notes", prompt):
                value = self.totals.get(name, self.default)
                items.append({"label": label, "scores": {k: value for k in ("readable", "composition", "mood", "variety", "captions", "defects")},
                              "reason": "scored %s" % name})
            payload = {"candidates": items}
            assert names
        else:
            ids = re.search(r"reading order, row by row, left to right: ([^.]*)\.", prompt).group(1).split(", ")
            keys = ("readable", "composition", "mood", "variety", "captions", "defects", "quality")
            payload = {"frames": [{"shot": sid, "scores": {k: (index % 10) for k in keys}, "reason": "frame %s" % sid}
                                  for index, sid in enumerate(ids)]}
        return {"text": json.dumps(payload), "json": payload, "usage": {"input_tokens": 100, "output_tokens": 20},
                "cost_usd": self.cost, "model": model, "provider": provider, "price_assumed": False}


@pytest.fixture
def job(tmp_path):
    return make_job(tmp_path)


def use(monkeypatch, ask):
    monkeypatch.setattr(vision, "ask", ask)
    return ask


def test_rubric_weights_sum_to_one_and_totals_are_weighted():
    rubric = judge.load_rubric()
    assert sum(c["weight"] for c in rubric["criteria"]) == pytest.approx(1.0)
    assert rubric["threshold"] == 6.5
    scores = {"readable": 10, "composition": 0, "mood": 0, "variety": 0, "captions": 0, "defects": 10}
    assert judge.total_score(scores, rubric["criteria"]) == pytest.approx(4.0)
    assert judge.total_score({"readable": 99, "mood": "bad"}, rubric["criteria"]) == pytest.approx(3.0)


def test_propose_one_actor_round_zero():
    shot = {"id": "a", "cast": [{"who": "me"}], "camera": {"on": "me", "frame": "full", "move": "push", "dof": 0.3, "follow": True}}
    names = [c["name"] for c in judge.propose(shot, 0, 6)]
    assert names == ["author", "close_three_quarter", "medium_other_side", "low_angle", "high_angle", "dutch_close"]
    candidates = {c["name"]: c["camera"] for c in judge.propose(shot, 0, 6)}
    assert candidates["author"] == shot["camera"] and candidates["author"] is not shot["camera"]
    assert candidates["low_angle"] == {"dof": 0.3, "follow": True, "on": "me", "frame": "medium", "yaw": 20, "pitch": -18, "head": 0.3, "move": "push"}
    assert candidates["dutch_close"]["dutch"] == 12 and "ots" not in candidates["dutch_close"]


def test_propose_two_actors_uses_over_shoulder_and_pov_later():
    shot = {"id": "a", "cast": [{"who": "me"}, {"who": "manager"}], "camera": {"frame": "medium"}}
    first = judge.propose(shot, 0, 6)
    assert first[-1] == {"name": "over_shoulder", "camera": {"on": "me", "ots": "manager", "move": "creep"}}
    assert "dutch_close" not in [c["name"] for c in first]
    second = judge.propose(shot, 1, 10)
    assert [c["name"] for c in second] == ["wide_establishing", "extreme_close", "profile", "dutch_low", "back_view", "pov"]
    assert second[-1]["camera"] == {"on": "me", "pov": "manager", "move": "handheld"}
    assert "author" not in [c["name"] for c in second]


def test_propose_later_rounds_jitter_yaw_and_truncate_keeps_author():
    shot = {"id": "a", "cast": [{"who": "me"}], "camera": {"on": "me"}}
    third = judge.propose(shot, 2, 10)
    round_one = {c["name"]: c["camera"]["yaw"] for c in judge.propose(shot, 1, 10)}
    for candidate in third:
        base = candidate["name"].rsplit("_r", 1)[0]
        assert abs(candidate["camera"]["yaw"] - round_one[base]) == 20
    assert [c["name"] for c in judge.propose(shot, 0, 3)][0] == "author"
    assert len(judge.propose(shot, 0, 3)) == 3
    assert [c["name"] for c in judge.propose(shot, 0, 0)] == ["author"]


def test_propose_locked_shots_return_only_the_author_camera():
    for camera in ({"from": "wide", "move": "push"}, {"pos": [0, 1, 2]}, {"on": "me", "locked": True}):
        shot = {"id": "a", "cast": [{"who": "me"}], "camera": camera}
        assert judge.is_locked(shot)
        assert judge.propose(shot, 0, 6) == [{"name": "author", "camera": camera}]
    assert judge.is_locked({"id": "b", "cast": [], "camera": {"frame": "wide"}})


def test_shot_text_falls_back_to_words_json():
    words = [{"display": w} for w in ["On", "Monday,", "I", "found"]]
    assert judge.shot_text({"text": "kept"}, words) == "kept"
    assert judge.shot_text({"word_start": 1, "word_end": 2}, words) == "Monday, I"
    assert judge.shot_text({}, words) == ""


def test_config_merges_defaults_story_and_flags(tmp_path):
    rubric = judge.load_rubric()
    cfg = judge.resolve_config({"judge": {"max_candidates": 4, "ceiling_usd": 1.0}}, {"ceiling_usd": 0.5, "model": None, "retries": 2}, rubric)
    assert cfg["max_candidates"] == 4 and cfg["ceiling_usd"] == 0.5 and cfg["retries"] == 2
    assert cfg["provider"] == "gemini" and cfg["threshold"] == 6.5 and cfg["still_height"] == 480 and cfg["prices"] == {}
    with pytest.raises(judge.JudgeError, match="python -m pipeline.judge models --provider gemini"):
        judge.require_model(cfg)
    judge.require_model(dict(cfg, provider="none"))


@needs_ffmpeg
def test_picks_the_best_total_and_writes_judge_json(job, monkeypatch, capsys):
    ask = use(monkeypatch, FakeAsk({"low_angle": 9.0}))
    stager = FakeStager()
    doc = judge.judge_shots(job, stager, CONFIG)
    first, second, third = doc["shots"]
    assert first["status"] == "picked" and first["chosen"]["name"] == "low_angle" and first["chosen"]["total"] == pytest.approx(9.0)
    assert first["chosen"]["camera"]["dof"] == 0.4
    candidate = first["rounds"][0]["candidates"][0]
    assert set(candidate) >= {"label", "name", "camera", "geometry", "still", "scores", "total", "reason"}
    assert (job / first["rounds"][0]["sheet"]).is_file()
    assert (job / candidate["still"]).is_file()
    assert third["status"] == "locked" and third["chosen"]["camera"] == {"from": "my_door_wide", "move": "push"}
    assert len(ask.calls) == 2
    assert "mystery" in ask.calls[0]["prompt"] and "The Box" in ask.calls[0]["prompt"] and "On Monday, I found a box." in ask.calls[0]["prompt"]
    assert "The last tile, PREV" not in ask.calls[0]["prompt"] and "The last tile, PREV" in ask.calls[1]["prompt"]
    assert stager.geometry_calls[0][2] is None and stager.geometry_calls[1][2]["name"] == "low_angle"
    saved = json.loads((job / "judge.json").read_text())
    assert saved["version"] == 1 and saved["usage"]["calls"] == 2 and saved["usage"]["cost_usd"] == pytest.approx(0.02)
    assert saved["rubric"]["threshold"] == 6.5 and saved["config"]["labels"] in ("text", "boxes")
    assert "fake-model" in json.dumps(saved["config"]) and "key" not in json.dumps(saved["config"]).lower()
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 3 and out[0].startswith("s01 picked")
    assert judge.chosen_cameras(job)["s01"]["frame"] == "medium"


@needs_ffmpeg
def test_totals_come_from_the_rubric_not_the_model(job, monkeypatch):
    ask = FakeAsk()
    original = ask.__call__

    def lying(*args, **kwargs):
        result = original(*args, **kwargs)
        for item in result["json"]["candidates"]:
            item["total"] = 10
        return result

    use(monkeypatch, lying)
    doc = judge.judge_shots(job, FakeStager(), dict(CONFIG, retries=0), ["s01"])
    assert doc["shots"][0]["chosen"]["total"] == pytest.approx(5.0)
    assert doc["shots"][0]["status"] == "below_threshold"


@needs_ffmpeg
def test_geometry_failures_are_dropped(job, monkeypatch):
    use(monkeypatch, FakeAsk({"close_three_quarter": 9.0}))
    stager = FakeStager(bad=[30])
    doc = judge.judge_shots(job, stager, CONFIG, ["s01"])
    record = doc["shots"][0]["rounds"][0]
    assert "close_three_quarter" not in [c["name"] for c in record["candidates"]]
    assert [d["name"] for d in record["dropped"]] == ["close_three_quarter"]
    assert doc["shots"][0]["chosen"]["name"] != "close_three_quarter"
    assert [c["label"] for c in record["candidates"]] == list("ABCDE")
    assert len(stager.still_calls[0][1]) == 5


def test_all_geometry_failures_keep_the_least_bad_without_asking(job, monkeypatch):
    ask = use(monkeypatch, FakeAsk())
    doc = judge.judge_shots(job, FakeStager(all_bad=True), CONFIG, ["s01"])
    entry = doc["shots"][0]
    assert entry["status"] == "geometry_failed" and ask.calls == []
    assert len(entry["rounds"]) == 2
    assert len(entry["chosen"]["camera"]) > 0 and entry["chosen"]["name"] == "author"


@needs_ffmpeg
def test_restages_when_below_threshold_and_compares_all_rounds(job, monkeypatch):
    ask = use(monkeypatch, FakeAsk({"profile": 8.0, "low_angle": 6.0}, default=4.0))
    doc = judge.judge_shots(job, FakeStager(), CONFIG, ["s01"])
    entry = doc["shots"][0]
    assert [r["round"] for r in entry["rounds"]] == [0, 1] and len(ask.calls) == 2
    assert entry["status"] == "picked" and entry["chosen"]["name"] == "profile" and entry["chosen"]["round"] == 1
    ask2 = use(monkeypatch, FakeAsk({"low_angle": 6.0}, default=4.0))
    doc = judge.judge_shots(job, FakeStager(), CONFIG, ["s01"])
    entry = doc["shots"][0]
    assert len(ask2.calls) == 2 and entry["status"] == "below_threshold"
    assert entry["chosen"]["name"] == "low_angle" and entry["chosen"]["round"] == 0


@needs_ffmpeg
def test_stops_at_the_budget_ceiling(job, monkeypatch):
    ask = use(monkeypatch, FakeAsk({"low_angle": 9.0}, cost=1.0))
    stager = FakeStager()
    doc = judge.judge_shots(job, stager, dict(CONFIG, ceiling_usd=1.0))
    first, second, third = doc["shots"]
    assert first["status"] == "picked" and len(ask.calls) == 1
    assert second["status"] == "budget_stopped" and second["chosen"]["name"] == "author" and second["chosen"]["total"] is None
    assert second["rounds"][0]["sheet"] is None and len(stager.still_calls) == 1
    assert third["status"] == "locked"
    assert doc["usage"]["stopped_by_ceiling"] is True and doc["usage"]["calls"] == 1


@needs_ffmpeg
def test_budget_running_out_mid_shot_keeps_the_best_scored(job, monkeypatch):
    use(monkeypatch, FakeAsk({"low_angle": 6.0}, default=4.0, cost=1.0))
    doc = judge.judge_shots(job, FakeStager(), dict(CONFIG, ceiling_usd=1.0), ["s01"])
    entry = doc["shots"][0]
    assert entry["status"] == "budget_stopped" and entry["chosen"]["name"] == "low_angle" and len(entry["rounds"]) == 2


def test_provider_none_picks_by_geometry_only(job, monkeypatch):
    ask = use(monkeypatch, FakeAsk())
    stager = FakeStager(bad=[None])
    doc = judge.judge_shots(job, stager, {"provider": "none"})
    assert [s["status"] for s in doc["shots"]] == ["geometry_only", "geometry_only", "locked"]
    assert doc["shots"][0]["chosen"]["name"] == "author" and ask.calls == [] and stager.still_calls == []
    assert doc["usage"]["calls"] == 0


def test_geometry_only_prefers_the_first_ok_when_author_fails(job, monkeypatch):
    use(monkeypatch, FakeAsk())
    doc = judge.judge_shots(job, FakeStager(bad=[14]), {"provider": "none"}, ["s01"])
    assert doc["shots"][0]["chosen"]["name"] == "close_three_quarter"


def test_empty_model_is_an_error(job):
    with pytest.raises(judge.JudgeError, match="python -m pipeline.judge models --provider gemini"):
        judge.judge_shots(job, FakeStager(), {})


def test_unknown_shot_id_is_an_error(job):
    with pytest.raises(judge.JudgeError, match="unknown shot ids: nope"):
        judge.judge_shots(job, FakeStager(), CONFIG, ["nope"])


@needs_ffmpeg
def test_partial_rerun_merges_into_existing_results(job, monkeypatch):
    use(monkeypatch, FakeAsk({"low_angle": 9.0}))
    judge.judge_shots(job, FakeStager(), CONFIG)
    before = json.loads((job / "judge.json").read_text())
    use(monkeypatch, FakeAsk({"high_angle": 9.5}))
    stager = FakeStager()
    doc = judge.judge_shots(job, stager, CONFIG, ["s02"])
    assert [s["id"] for s in doc["shots"]] == ["s01", "s02", "s03"]
    assert doc["shots"][0] == before["shots"][0] and doc["shots"][2] == before["shots"][2]
    assert doc["shots"][1]["chosen"]["name"] == "high_angle"
    assert stager.geometry_calls[0][2]["name"] == "low_angle"
    assert doc["usage"]["calls"] == 3
    assert json.loads((job / "judge.json").read_text())["shots"][1]["chosen"]["name"] == "high_angle"
    assert set(judge.chosen_cameras(job)) == {"s01", "s02", "s03"}


@needs_ffmpeg
def test_prev_tile_uses_the_previous_shots_chosen_still(job, monkeypatch):
    ask = use(monkeypatch, FakeAsk({"low_angle": 9.0}))
    judge.judge_shots(job, FakeStager(), CONFIG)
    second_sheet = job / "judge" / "s02_r0.jpg"
    first_sheet = job / "judge" / "s01_r0.jpg"
    probe = lambda p: subprocess.run([FFMPEG, "-hide_banner", "-i", str(p)], stderr=subprocess.PIPE, universal_newlines=True).stderr
    assert re.search(r"Video: mjpeg.* (\d+)x96", probe(first_sheet)).group(1) == str(54 * 6)
    assert re.search(r"Video: mjpeg.* (\d+)x96", probe(second_sheet)).group(1) == str(54 * 7)


@needs_ffmpeg
def test_box_labels_fallback_is_recorded(job, monkeypatch):
    monkeypatch.setattr(judge, "drawtext_available", lambda: False)
    ask = use(monkeypatch, FakeAsk({"low_angle": 9.0}))
    doc = judge.judge_shots(job, FakeStager(), CONFIG, ["s01"])
    assert doc["config"]["labels"] == "boxes" and (job / doc["shots"][0]["rounds"][0]["sheet"]).is_file()
    assert "coloured box" in ask.calls[0]["prompt"]


def test_chosen_cameras_empty_without_a_file(tmp_path):
    assert judge.chosen_cameras(tmp_path) == {}


def make_video(job: Path, seconds: int = 14) -> None:
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=108x192:rate=5:duration=%d" % seconds, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(job / "video.mp4")],
                   check=True)


@needs_ffmpeg
def test_video_pass_builds_sheets_and_lists_the_weakest(tmp_path, monkeypatch):
    job = tmp_path / "job"
    job.mkdir()
    shots = [{"id": "s%02d" % i, "text": "word%d more" % i, "start": float(i), "end": float(i + 1)} for i in range(14)]
    (job / "timeline.json").write_text(json.dumps({"fps": 5, "shots": shots}))
    (job / "story.json").write_text(json.dumps({"title": "T"}))
    make_video(job)
    refs = tmp_path / "refs"
    for index in range(6):
        write_png(refs / ("r%d.png" % index), (index * 30, 0, 0))
    ask = use(monkeypatch, FakeAsk())
    doc = judge.judge_video(job, CONFIG, refs)
    video_pass = doc["video_pass"]
    assert len(video_pass["sheets"]) == 2 and len(ask.calls) == 2
    assert all((job / s).is_file() for s in video_pass["sheets"])
    assert len(video_pass["frames"]) == 14 and len(video_pass["references"]) == 4
    assert len(ask.calls[0]["images"]) == 5 and ask.calls[0]["images"][-1] == job / video_pass["sheets"][0]
    assert "what a finished video in this format looks like" in ask.calls[0]["prompt"]
    assert len(video_pass["weakest"]) == 8
    totals = {f["shot"]: f["total"] for f in video_pass["frames"]}
    assert [totals[s] for s in video_pass["weakest"]] == sorted(totals[s] for s in video_pass["weakest"])
    assert max(totals[s] for s in video_pass["weakest"]) <= sorted(totals.values())[7]
    assert video_pass["frames"][3]["time"] == pytest.approx(3.5)
    assert doc["usage"]["calls"] == 2
    assert doc["rubric"]["criteria"][0]["key"] == "readable"
    criteria = judge.video_rubric(judge.load_rubric())["criteria"]
    assert criteria[-1] == {"key": "quality", "weight": 0.2, "question": criteria[-1]["question"]}
    assert sum(c["weight"] for c in criteria) == pytest.approx(1.0)
    assert criteria[0]["weight"] == pytest.approx(0.24)


@needs_ffmpeg
def test_video_pass_adds_usage_to_an_existing_judge_json(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    make_video(job, 7)
    use(monkeypatch, FakeAsk({"low_angle": 9.0}))
    judge.judge_shots(job, FakeStager(), CONFIG)
    doc = judge.judge_video(job, CONFIG)
    assert doc["usage"]["calls"] == 3 and len(doc["shots"]) == 3 and doc["video_pass"]["references"] == []
    assert doc["video_pass"]["weakest"][0] == "s01"


def test_video_pass_needs_a_video(job, monkeypatch):
    use(monkeypatch, FakeAsk())
    with pytest.raises(judge.JudgeError, match="missing"):
        judge.judge_video(job, CONFIG)


@needs_ffmpeg
def test_cli_show_and_shots_without_studio(job, monkeypatch, capsys):
    use(monkeypatch, FakeAsk({"low_angle": 9.0}))
    judge.judge_shots(job, FakeStager(), CONFIG)
    capsys.readouterr()
    assert judge.main(["show", "--job", str(job)]) == 0
    shown = capsys.readouterr().out
    assert "low_angle" in shown and "locked" in shown and "9.00" in shown and shown.splitlines()[0].split()[:3] == ["shot", "status", "chosen"]
    def no_studio(path):
        raise RuntimeError("Studio is not running")

    monkeypatch.setattr(judge, "make_stager", no_studio)
    assert judge.main(["shots", "--job", str(job), "--provider", "none"]) == 1
    assert "Studio is not running" in capsys.readouterr().err


def test_run_shots_passes_settings_through(job, monkeypatch):
    use(monkeypatch, FakeAsk())
    doc = judge.run_shots(job, FakeStager(), {"provider": "none", "shots": ["s02"], "max_candidates": 2})
    assert [s["id"] for s in doc["shots"]] == ["s02"] and doc["config"]["max_candidates"] == 2


def test_models_command_reports_missing_key(monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert judge.main(["models", "--provider", "gemini"]) == 1
    assert "GEMINI_API_KEY" in capsys.readouterr().err


def test_manual_provider_pending_returns_three(job, monkeypatch, capsys):
    assert judge.main(["show", "--job", str(job)]) == 1
    monkeypatch.setattr(judge, "make_stager", lambda j: FakeStager())
    assert judge.main(["shots", "--job", str(job), "--provider", "manual", "--model", "human", "--shots", "s01"]) == 3
    assert "pending" in capsys.readouterr().err
    assert (job / "judge" / "manual" / "s01_r0_0.request.json").is_file()



class SweepStager:
    """Passes only cameras whose yaw lies in a window, like a subject standing by a wall."""

    def __init__(self, low, high):
        self.low, self.high = low, high

    def geometry(self, shot_id, cameras, previous):
        return [{"ok": self.low <= c.get("yaw", 999) <= self.high, "issues": [], "metrics": {}} for c in cameras]


def test_scout_keeps_valid_setups_nearest_the_author_first():
    shot = {"id": "s01", "cast": [{"who": "me"}], "camera": {"on": "me", "frame": "close", "yaw": 150, "pitch": 6, "move": "creep", "dof": 0.4}}
    found = judge.scout(shot, SweepStager(90, 180), None, 3)
    assert len(found) == 3 and all(90 <= c["camera"]["yaw"] <= 180 for c in found)
    first = found[0]
    assert (first["camera"]["yaw"], first["camera"]["frame"], first["camera"]["dof"]) == (150, "close", 0.4)
    assert first["name"] == "scout_close_y150_p6" and first["geometry"]["ok"]
    assert len({(c["camera"]["yaw"], c["camera"]["pitch"], c["camera"]["frame"]) for c in found}) == 3
    assert judge.scout(shot, SweepStager(500, 600), None, 3) == []
    assert judge.scout(shot, SweepStager(90, 180), None, 0) == []


def test_face_on_keeps_every_candidate_in_front_of_the_subject():
    shot = {"id": "s01", "cast": [{"who": "me"}, {"who": "mom"}], "camera": {"on": "me", "frame": "close", "yaw": 20, "face_on": True}}
    for round_index in (0, 1):
        proposals = [c for c in judge.propose(shot, round_index, 9) if c["name"] != "author"]
        assert proposals and all(abs(c["camera"]["yaw"]) <= judge.FACE_ON_YAW and c["camera"]["face_on"] for c in proposals)
    found = judge.scout(shot, SweepStager(-180, 180), None, 6)
    assert len(found) == 6 and all(abs(c["camera"]["yaw"]) <= judge.FACE_ON_YAW for c in found)


def test_intent_reaches_the_prompt():
    criteria = judge.load_rubric()["criteria"]
    candidates = [{"label": "A", "name": "author", "geometry": {"issues": []}}]
    assert "his scared face" in judge.shots_prompt(criteria, {}, "The problem?", candidates, False, "text", "his scared face")
    assert "director wants" not in judge.shots_prompt(criteria, {}, "The problem?", candidates, False, "text")


@needs_ffmpeg
def test_an_unusable_reply_keeps_the_run_going(job, monkeypatch):
    def broken(*args, **kwargs):
        raise judge.JudgeError("the model returned no usable scores")

    monkeypatch.setattr(judge, "_ask_scores", broken)
    doc = judge.judge_shots(job, FakeStager(), CONFIG)
    entry = doc["shots"][0]
    assert entry["status"] == "geometry_only" and entry["chosen"]["name"] == "author"
    assert entry["rounds"][0]["error"].startswith("the model returned")
