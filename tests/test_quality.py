from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from pipeline import quality, vision
from tests.test_vision import write_png

FFMPEG = shutil.which("ffmpeg")
SHOTS = [("s01", 0.0, 1.0, "bedroom", 0, 2), ("s02", 1.0, 2.0, "hall", 3, 5), ("s03", 2.0, 3.0, "bedroom", 6, 7), ("s04", 3.0, 4.0, "hall", None, None)]
WORDS = [("My", "narrator"), ("mom", "narrator"), ("texted", "narrator"), ("Don't", "mom"), ("open", "mom"), ("it.", "mom"),
         ("I", "narrator"), ("ran.", "narrator")]


def make_job(root: Path, model: str = "fake-model") -> Path:
    job = root / "job"
    job.mkdir()
    shots = []
    for shot_id, start, end, place, first, last in SHOTS:
        shot = {"id": shot_id, "start": start, "end": end, "set": place}
        if first is not None:
            shot.update(word_start=first, word_end=last)
        shots.append(shot)
    (job / "shots.json").write_text(json.dumps({"version": 1, "fps": 30, "width": 1080, "height": 1920, "content_start": 0.0,
                                                "content_end": 4.0, "shots": shots}))
    (job / "words.json").write_text(json.dumps({"version": 1, "words": [{"i": i, "token": t, "speaker": s} for i, (t, s) in enumerate(WORDS)]}))
    (job / "story.json").write_text(json.dumps({"title": "The Door", "judge": {"prices": {"fake": [1.0, 1.0]}},
                                                "quality": {"provider": "gemini", "model": model, "workers": 4}}))
    (job / "video.mp4").write_bytes(b"video")
    (job / "final.mp4").write_bytes(b"final")
    return job


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(quality, "build_sheet", lambda tiles, out, height, columns: (out.parent.mkdir(parents=True, exist_ok=True), write_png(out)))
    monkeypatch.setattr(quality, "measure", lambda video: [{"luma": 60.0, "lit": 0.4, "change": 1.0} for _ in range(120)])
    return make_job(tmp_path)


def judge_of(prompt: str) -> str:
    for key, spec in json.loads(quality.RUBRIC_PATH.read_text())["judges"].items():
        if "Judge: %s." % spec["title"] in prompt:
            return key
    return "video"


def shot_of(prompt: str) -> str:
    return prompt.split("\nShot ", 1)[1].split(" ", 1)[0] if "\nShot " in prompt else ""


def fake_ask(replies=None, cost=0.001, log=None, delay=0.0):
    replies = replies or {}

    def ask(provider, model, system, prompt, images, **kwargs):
        key, shot = judge_of(prompt), shot_of(prompt)
        if log is not None:
            log.append((shot, key))
        if delay:
            time.sleep(delay)
        reply = replies.get((shot, key), replies.get(key))
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            reply = ({"scores": {"pacing": 8, "repetition": 7, "ending": 9, "consistency": 8}, "weakest": [{"shot": "s02", "reason": "idle"}],
                      "reason": "fine"} if key == "video" else {"visible": "a frame", "applicable": True, "defects": [], "score": 9, "reason": "clean"})
        return {"text": json.dumps(reply), "json": reply, "usage": {"input_tokens": 1000, "output_tokens": 100}, "cost_usd": cost,
                "model": model, "provider": provider, "price_assumed": False}
    return ask


def by_id(doc):
    return {shot["id"]: shot for shot in doc["shots"]}


def test_frames_are_spread_inside_the_shot():
    shot = {"start": 1.0, "end": 2.0}
    assert quality.frame_range(shot, 30) == (30, 59)
    assert quality.frame_numbers(shot, 30, 3) == [35, 45, 55]
    assert quality.frame_numbers({"start": 0.0, "end": 0.1}, 30, 8) == [0, 1, 2]


def test_spoken_groups_words_by_speaker(job):
    _, shots = quality.load_shots(job)
    assert [s["line"] for s in shots] == ["NARRATOR: My mom texted", "MOM: Don't open it.", "NARRATOR: I ran.", ""]
    assert shots[1]["set"] == "hall"


def test_verdict_rules():
    spec = {"threshold": 6, "fail_if": [{"key": "idle", "text": ""}, {"key": "arms_out", "text": ""}]}
    assert quality.verdict(spec, {"score": 9, "defects": []})["status"] == "pass"
    assert quality.verdict(spec, {"score": 5, "defects": []})["status"] == "fail"
    assert quality.verdict(spec, {"score": 7, "defects": [{"key": "idle", "where": "mom"}]})["status"] == "fail"
    assert quality.verdict(spec, {"score": 7, "defects": ["arms_out"]})["defects"] == [{"key": "arms_out", "where": ""}]
    assert quality.verdict(spec, {"score": 8, "defects": [{"key": "idle"}]})["status"] == "pass"
    assert quality.verdict(spec, {"score": 7, "defects": [{"key": "made_up"}]})["status"] == "pass"
    assert quality.verdict(spec, {"defects": []})["status"] == "fail"
    flagged = dict(spec, flags={"frozen_character": {"defect": "statue", "text": ""}})
    frozen = quality.verdict(flagged, {"score": 9, "defects": [], "frozen_character": True})
    assert frozen["status"] == "fail" and frozen["defects"] == [{"key": "statue", "where": "frozen_character"}]
    assert quality.verdict(flagged, {"score": 9, "defects": [], "frozen_character": False})["status"] == "pass"
    skipped = quality.verdict(spec, {"applicable": False, "score": 2, "defects": [{"key": "idle"}]})
    assert (skipped["status"], skipped["score"], skipped["defects"]) == ("pass", None, [])


def test_full_run_writes_one_report(job, monkeypatch):
    log = []
    replies = {("s02", "posture"): {"visible": "mom stands", "defects": [{"key": "idle", "where": "mom, every frame"}], "score": 3, "reason": "just stands"},
               ("s03", "motion"): {"defects": [], "score": 4, "reason": "slides"}}
    monkeypatch.setattr(vision, "ask", fake_ask(replies, log=log))
    doc = quality.run_quality(job)
    shots = by_id(doc)
    assert [shots[i]["status"] for i in ("s01", "s02", "s03", "s04")] == ["pass", "fail", "fail", "pass"]
    assert shots["s02"]["failed"] == ["posture"] and shots["s03"]["failed"] == ["motion"]
    assert shots["s01"]["judges"]["continuity"]["reason"].startswith("first shot")
    assert ("s01", "continuity") not in log and len(log) == 4 * 8 - 1 + 3
    summary = doc["summary"]
    assert summary["judges"]["posture"]["failing"] == ["s02"] and summary["judges"]["motion"]["fail"] == 1
    assert summary["failing_by_set"] == {"hall": ["s02"], "bedroom": ["s03"]}
    assert summary["frames_sampled"] > 0 and summary["spend"]["calls"] == len(log)
    assert doc["video_judge"]["status"] == "pass" and doc["video_judge"]["weakest"][0]["shot"] == "s02"
    assert json.loads((job / "quality.json").read_text())["summary"]["fail"] == 2
    report = (job / "QUALITY.md").read_text()
    assert "| posture | 1 |" in report and "just stands" in report and "### hall (1)" in report
    assert (job / "quality" / "sheets" / shots["s02"]["judges"]["posture"]["sheet"].split("/")[-1]).is_file()
    assert json.loads((job / "quality" / "ledger.json").read_text())["runs"][0]["calls"] == len(log)


def test_a_judge_that_cannot_answer_never_passes_the_shot(job, monkeypatch):
    monkeypatch.setattr(vision, "ask", fake_ask({"motion": vision.VisionError("gemini request failed with HTTP 500")}))
    doc = quality.run_quality(job, judges=["posture", "motion"])
    assert {s["status"] for s in doc["shots"]} == {"incomplete"}
    assert doc["summary"]["judges"]["motion"]["unjudged"] == 4 and doc["summary"]["pass"] == 0
    assert "Not fully judged" in (job / "QUALITY.md").read_text()


def test_geometry_and_picture_each_fail_on_their_own(job, monkeypatch):
    (job / "posture.json").write_text(json.dumps({"version": 1, "shots": {
        "s01": {"status": "fail", "frames": [{"frame": 12, "issues": [{"kind": "intersect", "actor": "me", "detail": "hand in door"}]}]},
        "s02": {"status": "pass", "frames": []},
        "s03": {"status": "fail", "frames": [{"frame": 70, "issues": [{"kind": "stiff_arms", "actor": "mom", "detail": ""}]}]}}}))
    monkeypatch.setattr(vision, "ask", fake_ask({("s02", "clipping"): {"defects": [{"key": "floating"}], "score": 4, "reason": "feet off the floor"}}))
    shots = by_id(quality.run_quality(job, judges=["posture", "clipping"]))
    assert shots["s01"]["judges"]["clipping"]["status"] == "fail"
    assert shots["s01"]["judges"]["clipping"]["reason"] == "clean; geometry: me intersect: hand in door (frame 12)"
    assert shots["s01"]["judges"]["posture"]["status"] == "pass"
    assert shots["s02"]["judges"]["clipping"]["status"] == "fail" and shots["s02"]["judges"]["clipping"]["geometry"]["status"] == "pass"
    assert shots["s03"]["failed"] == ["posture"] and shots["s03"]["judges"]["posture"]["picture_status"] == "pass"
    doc = quality.run_quality(job, report_only=True)
    assert by_id(doc)["s01"]["judges"]["clipping"]["reason"].count("geometry:") == 1
    assert doc["summary"]["judges"]["clipping"]["geometry_only"] == ["s01"] and doc["summary"]["judges"]["posture"]["geometry_only"] == ["s03"]
    other = job / "later.json"
    other.write_text(json.dumps({"version": 1, "described": "pass 7", "shots": {
        "s01": {"status": "pass", "frames": [{"frame": 12, "issues": [{"kind": "intersect", "actor": "me", "in_frame": False}]}]},
        "s04": {"status": "fail", "frames": [{"frame": 100, "issues": [{"kind": "reach_miss", "actor": "me", "detail": "", "in_frame": True},
                                                                      {"kind": "prop_missing", "actor": "me", "in_frame": False}]}]}}}))
    shots = by_id(quality.run_quality(job, report_only=True, posture=other))
    assert shots["s01"]["judges"]["clipping"]["status"] == "pass" and shots["s01"]["judges"]["clipping"]["reason"] == "clean"
    assert shots["s04"]["failed"] == ["posture"] and shots["s04"]["judges"]["clipping"]["status"] == "pass"
    report = (job / "QUALITY.md").read_text()
    assert "Geometry merged from" in report and chr(96) + "later.json@" in report and chr(96) + "posture.json@" in report


def test_geometry_is_merged_only_for_the_shots_judged(job, monkeypatch):
    monkeypatch.setattr(vision, "ask", fake_ask())
    quality.run_quality(job, judges=["posture", "clipping"])
    (job / "posture.json").write_text(json.dumps({"version": 1, "describes": {"label": "next pass"}, "shots": {
        shot: {"status": "fail", "frames": [{"frame": 1, "issues": [{"kind": "idle", "actor": "me"}]}]} for shot in ("s01", "s02")}}))
    doc = quality.run_quality(job, shots=["s02"], judges=["posture"])
    shots = by_id(doc)
    assert "geometry" not in shots["s01"]["judges"]["posture"] and shots["s02"]["judges"]["posture"]["status"] == "fail"
    assert shots["s02"]["judges"]["posture"]["geometry"]["source"].endswith("(next pass)")
    assert list(doc["summary"]["geometry_sources"].values()) == [1]
    assert quality.verdict_pairs(doc)[("s02", "posture")] == "pass"
    assert "geometry" not in by_id(quality.run_quality(job, report_only=True))["s01"]["judges"]["posture"]


def test_geometry_in_the_earlier_shape_counts_measured_reasons_only(job):
    quality.run_quality(job, judges=["posture", "clipping"])
    earlier = job / "earlier.json"
    earlier.write_text(json.dumps({"version": 1, "shots": [], "posture": {"model": "some-model", "shots": [
        {"id": "s01", "status": "fail", "reasons": ["vision_posture", "vision_clipping"],
         "geometry": [{"who": "me", "code": "clip_scenery", "detail": "LeftFoot->Part", "seen": False, "at": [0, 1], "depth": 0.48}]},
        {"id": "s02", "status": "fail", "reasons": ["arm_out", "clip_scenery", "vision_action"], "geometry": [
            {"who": "mom", "code": "arm_out", "detail": "L arm held out", "seen": True, "at": [0], "depth": None},
            {"who": "mom", "code": "clip_scenery", "detail": "LeftFoot->Part", "seen": True, "at": [0, 0.25], "depth": 0.38},
            {"who": "mom", "code": "stance_wide", "detail": "both legs angled outwards", "seen": False, "at": [0.25], "depth": None}]},
        {"id": "s03", "status": "no_character", "reasons": [], "geometry": []},
        {"id": "s04", "status": "fail", "current": False, "reasons": ["no_action"], "geometry": []}]}}))
    doc = quality.run_quality(job, report_only=True, posture=earlier)
    shots = by_id(doc)
    assert shots["s01"]["failed"] == [] and shots["s01"]["judges"]["clipping"]["geometry"]["issues"] == []
    assert shots["s02"]["failed"] == ["posture", "clipping"]
    assert shots["s02"]["judges"]["posture"]["geometry"]["issues"] == ["mom arm_out: L arm held out (at 0 of the shot)"]
    assert shots["s02"]["judges"]["clipping"]["geometry"]["issues"] == ["mom clip_scenery: LeftFoot->Part by 0.38 studs (at 0, 0.25 of the shot)"]
    assert shots["s03"]["failed"] == [] and shots["s04"]["failed"] == [] and "geometry" not in shots["s04"]["judges"]["posture"]
    assert doc["geometry_stale"] == ["s04"] and "marks them stale: s04" in (job / "QUALITY.md").read_text()


def test_ceiling_holds_across_threads_and_runs(job, monkeypatch):
    monkeypatch.setattr(vision, "ask", fake_ask(cost=0.04, delay=0.01))
    overrides = {"ceiling_usd": 0.30, "reserve_usd": 0.05, "workers": 8}
    doc = quality.run_quality(job, judges=["posture", "clipping", "story"], overrides=overrides)
    assert doc["usage"]["cost_usd"] <= 0.30 and doc["usage"]["stopped_by_ceiling"] is True
    assert 0 < doc["usage"]["calls"] < 12 and doc["summary"]["incomplete"] > 0
    stopped = [r for s in doc["shots"] for r in s["judges"].values() if r["status"] == "error"]
    assert stopped and all("ceiling" in r["reason"] for r in stopped)
    second = quality.run_quality(job, judges=["framing"], overrides=overrides, out=job / "second.json")
    assert second["runs"][-1]["calls"] <= 1 and second["usage"]["job_total_usd"] <= 0.30


def test_shared_budget_never_overspends():
    budget = quality.SharedBudget(1.0, 0.1)
    taken = []

    def worker():
        try:
            budget.take()
        except vision.BudgetExceeded:
            return
        taken.append(1)
        time.sleep(0.01)
        budget.settle({"usage": {"input_tokens": 1, "output_tokens": 1}, "cost_usd": 0.1})

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(taken) == 10 and budget.summary()["cost_usd"] == 1.0 and budget.stopped_by_ceiling


def test_listed_shots_merge_and_the_next_shot_gets_continuity(job, monkeypatch):
    monkeypatch.setattr(vision, "ask", fake_ask({("s02", "posture"): {"defects": [{"key": "idle"}], "score": 3, "reason": "stands"}}))
    quality.run_quality(job)
    log = []
    monkeypatch.setattr(vision, "ask", fake_ask(log=log))
    doc = quality.run_quality(job, shots=["s02"])
    assert sorted(log) == sorted([("s02", key) for key in quality.SHOT_JUDGES] + [("s03", "continuity")])
    assert by_id(doc)["s02"]["status"] == "pass" and doc["summary"]["pass"] == 4 and len(doc["runs"]) == 2
    assert doc["usage"]["calls"] == doc["runs"][0]["calls"] + len(log)
    table = quality.compare({"shots": [{"id": "s02", "status": "fail", "judges": {"posture": {"status": "fail"}}}]}, doc)
    assert table["posture"] == {"before": 1, "after": 0, "fixed": ["s02"], "new": [], "still": []}


def test_a_shot_rendered_again_sets_its_verdicts_aside(job, monkeypatch):
    def frames(video, lift=0.0, again=()):
        return [{"luma": 60.0 + lift + (40.0 if Path(video).name == "video.mp4" and index // 30 in again else 0.0), "lit": 0.4, "change": 1.0,
                 "cells": bytes([60])} for index in range(120)]

    monkeypatch.setattr(quality, "measure", frames)
    monkeypatch.setattr(vision, "ask", fake_ask())
    first = quality.run_quality(job)
    assert first["summary"]["pass"] == 4 and by_id(first)["s03"]["judges"]["continuity"]["render"].keys() == {"s01", "s02", "s03"}
    monkeypatch.setattr(quality, "measure", lambda video: frames(video, lift=1.0))
    assert quality.run_quality(job, report_only=True)["summary"]["pass"] == 4
    monkeypatch.setattr(quality, "measure", lambda video: frames(video, again=(1,)))
    doc = quality.run_quality(job, report_only=True, against=job / "quality.json")
    clean = [key for key in quality.SHOT_JUDGES if key != "framing"]
    assert doc["summary"]["stale"] == {"s02": clean, "s03": ["continuity"], "s04": ["continuity"]}
    assert by_id(doc)["s02"]["status"] == "incomplete" and by_id(doc)["s02"]["judges"]["posture"]["judged"] == "pass"
    assert doc["video_judge"]["status"] == "stale" and doc["comparison"]["any"]["fixed"] == []
    assert "Set aside, because the shot was rendered again" in (job / "QUALITY.md").read_text()
    log = []
    monkeypatch.setattr(vision, "ask", fake_ask(log=log))
    doc = quality.run_quality(job, changed=True)
    assert sorted(log) == sorted([("s02", key) for key in clean] + [("s03", "continuity"), ("s04", "continuity")])
    assert doc["summary"]["pass"] == 4 and doc["summary"]["stale"] == {} and doc["video_judge"]["status"] == "stale"
    log.clear()
    doc = quality.run_quality(job, changed=True, judges=["continuity", "video"])
    assert [key for _, key in log] == ["video"] * 3 and doc["video_judge"]["status"] == "pass" and len(doc["renders"]) == 2


def test_measured_darkness_fails_lighting_whatever_the_model_says(job, monkeypatch):
    monkeypatch.setattr(quality, "measure", lambda video: [{"luma": 2.0, "lit": 0.001, "change": 0.2} for _ in range(120)])
    monkeypatch.setattr(vision, "ask", fake_ask())
    record = by_id(quality.run_quality(job, judges=["lighting"]))["s01"]["judges"]["lighting"]
    assert record["status"] == "fail" and record["defects"][-1]["key"] == "measured_dark" and record["score"] == 9


def test_agreement_with_a_reader_and_before_after_table(job, monkeypatch, tmp_path):
    monkeypatch.setattr(vision, "ask", fake_ask({("s02", "posture"): {"defects": [{"key": "idle"}], "score": 3, "reason": "stands"}}))
    human = tmp_path / "human.json"
    human.write_text(json.dumps({"reader": "a person", "notes": ["read on sheets"], "verdicts": {
        "s02": {"posture": "fail", "clipping": "fail"}, "s03": {"posture": "fail", "motion": "pass"}}}))
    doc = quality.run_quality(job, human=human)
    after = doc["calibration"]["after"]
    assert (after["agree"], after["pairs"], after["rate"]) == (2, 4, 0.5)
    assert after["false_pass"] == ["s02/clipping", "s03/posture"] and after["false_fail"] == []
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"shots": [{"id": "s03", "status": "fail", "judges": {"motion": {"status": "fail"}}}]}))
    doc = quality.run_quality(job, human=human, baseline=old, against=old, report_only=True)
    assert doc["calibration"]["before"]["false_fail"] == ["s03/motion"]
    assert doc["comparison"]["motion"]["fixed"] == ["s03"] and doc["comparison"]["posture"]["new"] == ["s02"]
    report = (job / "QUALITY.md").read_text()
    assert "2 of 4 verdicts agree (50%)" in report and "## Before and after (old.json against this report)" in report


def test_settings_come_from_the_rubric_the_story_and_overrides(job):
    overrides = quality.parse_sets(["posture.frames=5", "model=other", "video.threshold=7", "lighting.min_lit_fraction=0.02"])
    settings = quality.load_settings(json.loads((job / "story.json").read_text()), overrides)
    assert settings["judges"]["posture"]["frames"] == 5 and settings["judges"]["posture"]["threshold"] == 6
    assert settings["model"] == "other" and settings["video"]["threshold"] == 7 and settings["prices"] == {"fake": [1.0, 1.0]}
    with pytest.raises(quality.QualityError):
        quality.parse_sets(["nonsense"])


def test_missing_model_and_unknown_names_are_refused(tmp_path, monkeypatch):
    job = make_job(tmp_path, model="")
    with pytest.raises(quality.QualityError, match="no model set"):
        quality.run_quality(job)
    with pytest.raises(quality.QualityError, match="unknown judges"):
        quality.run_quality(job, judges=["vibes"], overrides={"model": "m"})
    with pytest.raises(quality.QualityError, match="unknown shot ids"):
        quality.run_quality(job, shots=["zz"], overrides={"model": "m"})


def test_motion_gets_a_larger_second_sheet_and_the_frozen_flag(job, monkeypatch):
    seen = {}

    def ask(provider, model, system, prompt, images, **kwargs):
        seen["images"], seen["prompt"] = [Path(i).name for i in images], prompt
        reply = {"applicable": True, "defects": [], "score": 8, "frozen_character": True, "reason": "holds one pose"}
        return {"text": "", "json": reply, "usage": {"input_tokens": 1, "output_tokens": 1}, "cost_usd": 0.0}

    monkeypatch.setattr(vision, "ask", ask)
    record = by_id(quality.run_quality(job, shots=["s02"], judges=["motion"]))["s02"]["judges"]["motion"]
    assert seen["images"] == ["s02_cle8_480.jpg", "s02_cle3_720.jpg"] and '"frozen_character": false' in seen["prompt"]
    assert record["status"] == "fail" and record["defects"] == [{"key": "statue", "where": "frozen_character"}]


def test_a_job_can_rule_something_acceptable_for_every_judge(job, monkeypatch):
    prompts = []

    def ask(provider, model, system, prompt, images, **kwargs):
        prompts.append(prompt)
        return fake_ask()(provider, model, system, prompt, images, **kwargs)

    monkeypatch.setattr(vision, "ask", ask)
    quality.run_quality(job, shots=["s02"], judges=["continuity", "video"], overrides={"accepted": ["a mix of detailed avatar styles"]})
    assert len(prompts) == 5 and all("Accepted for this video and not a defect: a mix of detailed avatar styles" in p for p in prompts)
    prompts.clear()
    quality.run_quality(job, shots=["s02"], judges=["continuity"])
    assert prompts and not any("Accepted for this video" in p for p in prompts)


def test_prompt_carries_the_line_the_tiles_and_the_fail_keys(job):
    settings = quality.load_settings({})
    meta, shots = quality.load_shots(job)
    videos = {"clean": job / "video.mp4", "captioned": job / "final.mp4"}
    plan = quality.plan_sheet(settings["judges"]["continuity"], 2, shots, meta, videos)
    assert [t["label"] for t in plan["tiles"]] == ["PREV s02 F45", "s03 F67", "s03 F82", "EARLIER s01 F15"]
    assert plan["tiles"][1]["time"] == pytest.approx(67 / 30)
    prompt = quality.build_prompt("continuity", settings["judges"]["continuity"], settings, {"title": "The Door"}, 2, shots, plan, None)
    assert "Words spoken during this shot: NARRATOR: I ran." in prompt and "PREV s02 F45 (the shot before)" in prompt
    assert "- style_mismatch:" in prompt and '"defects"' in prompt


def test_label_png_is_a_png(tmp_path):
    data = quality.label_png("a2 F153", tmp_path / "label.png", 3).read_bytes()
    assert data.startswith(b"\x89PNG") and data[16:24] == (7 * 6 + 1).to_bytes(4, "big") * 0 + ((7 * 6 + 1) * 3).to_bytes(4, "big") + (27).to_bytes(4, "big")


@pytest.mark.skipif(not FFMPEG, reason="FFmpeg is not available")
def test_sheets_and_measures_from_a_real_video(tmp_path):
    video = tmp_path / "video.mp4"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=black:size=180x320:rate=30:duration=1", "-f", "lavfi", "-i",
                    "color=white:size=180x320:rate=30:duration=1", "-filter_complex", "[0:v][1:v]concat=n=2[v]", "-map", "[v]", "-pix_fmt", "yuv420p",
                    str(video)], check=True)
    frames = quality.measure(video)
    assert len(frames) == 60 and frames[29]["lit"] == 0.0 and frames[30]["lit"] == 1.0 and frames[30]["change"] > 200
    inside = quality.shot_measures(frames, 30, 59)
    assert inside["lit_fraction"] == 1.0 and inside["change_peak"] < 1.0
    sheet = tmp_path / "sheet.jpg"
    quality.build_sheet([{"video": video, "time": frame / 30, "label": "s01 F%d" % frame} for frame in (29, 30)], sheet, 240, 2)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(sheet)],
                           stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout.strip()
    assert probe == "272,240"
    halves = subprocess.run([FFMPEG, "-loglevel", "error", "-i", str(sheet), "-vf", "crop=20:20:100:200,scale=1:1,format=gray", "-f", "rawvideo", "-"],
                            stdout=subprocess.PIPE, check=True).stdout
    right = subprocess.run([FFMPEG, "-loglevel", "error", "-i", str(sheet), "-vf", "crop=20:20:236:200,scale=1:1,format=gray", "-f", "rawvideo", "-"],
                           stdout=subprocess.PIPE, check=True).stdout
    assert halves[0] < 40 and right[0] > 200
