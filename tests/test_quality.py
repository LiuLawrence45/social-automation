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
    assert ("s01", "continuity") not in log and len(log) == 4 * 8 - 1 + 3 + 2 * 2
    assert len(shots["s02"]["judges"]["posture"]["votes"]) == 3 and doc["summary"]["voted"]["kept"] == ["s02/posture", "s03/motion"]
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


def test_a_stray_set_piece_in_frame_fails_clipping_and_names_the_piece(job, monkeypatch):
    def piece(kind, detail, seen=True):
        return {"kind": kind, "actor": "the set", "detail": detail, "code": "piece", "in_frame": seen}
    (job / "posture.json").write_text(json.dumps({"version": 1, "shots": {
        "s01": {"status": "fail", "characters": False, "reasons": ["piece_floating"],
                "frames": [{"frame": 5, "issues": [piece("floating_piece", "clock touches no surface and no hand")]}]},
        "s02": {"status": "fail", "reasons": ["piece_twice"],
                "frames": [{"frame": 40, "issues": [piece("duplicate_piece", "Part of handle is built twice in the same place")]}]},
        "s03": {"status": "fail", "reasons": ["piece_twice"], "frames": [{"frame": 70, "issues": [piece("duplicate_piece", "handle", seen=False)]}]},
        "s04": {"status": "fail", "reasons": ["vision_natural"], "frames": []}}}))
    monkeypatch.setattr(vision, "ask", fake_ask())
    doc = quality.run_quality(job, judges=["posture", "clipping"])
    shots = by_id(doc)
    clock = shots["s01"]["judges"]["clipping"]
    assert clock["status"] == "fail" and clock["picture_status"] == "pass" and "votes" not in clock
    assert clock["reason"] == "clean; geometry: floating set piece: clock touches no surface and no hand (frame 5)"
    assert shots["s02"]["judges"]["clipping"]["reason"].endswith("set piece built twice: Part of handle is built twice in the same place (frame 40)")
    assert shots["s01"]["failed"] == shots["s02"]["failed"] == ["clipping"] and shots["s01"]["judges"]["posture"]["status"] == "pass"
    assert shots["s03"]["status"] == "pass" and shots["s04"]["status"] == "pass"
    assert doc["summary"]["judges"]["clipping"]["geometry_only"] == ["s01", "s02"]


def test_a_face_set_on_a_head_that_cannot_show_it_fails_expression(job, monkeypatch):
    (job / "posture.json").write_text(json.dumps({"version": 1, "shots": {
        "s02": {"status": "fail", "reasons": ["face_fixed"], "frames": [{"frame": 40, "issues": [
            {"kind": "frozen_face", "actor": "dispatcher", "detail": "the head has no face controls", "code": "face_fixed", "in_frame": True}]}]},
        "s03": {"status": "pass", "frames": []}}}))
    monkeypatch.setattr(vision, "ask", fake_ask())
    doc = quality.run_quality(job, judges=["posture", "expression", "clipping"])
    shots = by_id(doc)
    face = shots["s02"]["judges"]["expression"]
    assert shots["s02"]["failed"] == ["expression"] and face["picture_status"] == "pass" and "votes" not in face
    assert face["reason"] == "clean; geometry: dispatcher frozen_face: the head has no face controls (frame 40)"
    assert shots["s03"]["judges"]["expression"]["geometry"]["status"] == "pass" and shots["s03"]["status"] == "pass"
    assert doc["summary"]["judges"]["expression"]["geometry_only"] == ["s02"]


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


def in_turn(answers, log):
    """A fake model that gives each (shot, judge) its listed replies in order, then a clean pass."""
    lock = threading.Lock()
    clean = fake_ask()

    def ask(provider, model, system, prompt, images, **kwargs):
        pair = (shot_of(prompt), judge_of(prompt))
        with lock:
            log.append(pair)
            reply = answers[pair].pop(0) if answers.get(pair) else None
        return fake_ask({pair: reply})(provider, model, system, prompt, images, **kwargs) if reply else clean(provider, model, system, prompt, images, **kwargs)
    return ask


def test_a_borderline_fail_needs_the_majority_of_three_votes(job, monkeypatch):
    (job / "posture.json").write_text(json.dumps({"version": 1, "shots": {
        "s01": {"status": "fail", "frames": [{"frame": 12, "issues": [{"kind": "intersect", "actor": "me", "detail": "hand in door"}]}]},
        "s02": {"status": "pass", "frames": []}}}))
    near = {"defects": [{"key": "idle"}], "score": 5, "reason": "a little stiff"}
    plain = {"defects": [{"key": "statue"}], "score": 2, "reason": "frozen"}
    log = []
    answers = {("s01", "clipping"): [{"defects": [{"key": "floating"}], "score": 5, "reason": "floats"}],
               ("s02", "posture"): [near], ("s03", "motion"): [plain, dict(plain, score=3)],
               ("s04", "story"): [{"defects": [], "score": 2, "reason": "wrong place"}],
               ("s04", "lighting"): [{"defects": [], "score": 2, "reason": "lost"}],
               ("s04", "framing"): [{"defects": [], "score": 5, "reason": "tight"}, {"defects": [], "score": 9, "reason": "fine"},
                                    {"defects": [], "score": 4, "reason": "cut off"}]}
    monkeypatch.setattr(vision, "ask", in_turn(answers, log))
    doc = quality.run_quality(job, judges=list(quality.SHOT_JUDGES))
    shots = by_id(doc)
    posture = shots["s02"]["judges"]["posture"]
    assert posture["status"] == posture["picture_status"] == "pass" and shots["s02"]["status"] == "pass"
    assert [v["status"] for v in posture["votes"]] == ["fail", "pass", "pass"] and posture["votes"][0]["defects"] == ["idle"]
    motion = shots["s03"]["judges"]["motion"]
    assert motion["status"] == "fail" and motion["score"] == 2 and [v["status"] for v in motion["votes"]] == ["fail", "fail", "pass"]
    framing = shots["s04"]["judges"]["framing"]
    assert framing["status"] == "fail" and framing["score"] == 4 and len(framing["votes"]) == 3
    assert "votes" not in shots["s04"]["judges"]["story"] and "votes" not in shots["s04"]["judges"]["lighting"]
    assert shots["s01"]["judges"]["clipping"]["status"] == "fail" and "votes" not in shots["s01"]["judges"]["clipping"]
    assert log.count(("s01", "clipping")) == 1 and log.count(("s04", "story")) == 1 and log.count(("s02", "posture")) == 3
    assert doc["summary"]["voted"] == {"kept": ["s03/motion", "s04/framing"], "cleared": ["s02/posture"]}
    report = (job / "QUALITY.md").read_text()
    assert "2 stayed fails (s03/motion s04/framing); 1 became passes (s02/posture)" in report and "2 of 3 votes" in report
    assert doc["runs"][-1]["voted"] == 3
    shots["s02"]["judges"]["posture"]["status"] = "stale"
    assert quality.summarise(doc)["voted"] == {"kept": ["s03/motion", "s04/framing"], "cleared": []}


def test_confirm_votes_on_the_fails_already_in_the_report_and_an_unanswered_vote_keeps_the_fail(job, monkeypatch):
    near = {"defects": [{"key": "idle"}], "score": 5, "reason": "a little stiff"}
    monkeypatch.setattr(vision, "ask", fake_ask({("s02", "posture"): near, ("s03", "posture"): near}))
    doc = quality.run_quality(job, judges=["posture", "story"], overrides={"confirm": {"votes": 1}})
    assert doc["summary"]["fail"] == 2 and doc["summary"]["voted"] == {"kept": [], "cleared": []}
    log = []
    monkeypatch.setattr(vision, "ask", fake_ask({("s03", "posture"): vision.VisionError("gemini request failed with HTTP 500")}, log=log))
    shots = by_id(quality.run_quality(job, judges=["posture", "story"], confirm=True))
    assert sorted(log) == [("s02", "posture"), ("s02", "posture"), ("s03", "posture")]
    assert shots["s02"]["status"] == "pass" and len(shots["s02"]["judges"]["posture"]["votes"]) == 3
    assert shots["s03"]["judges"]["posture"]["status"] == "fail" and "votes" not in shots["s03"]["judges"]["posture"]


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


def test_continuity_and_motion_read_the_script_and_are_told_about_the_set(job):
    settings = quality.load_settings({"quality": {"sets": {"bedroom": "one white door with a lever handle"}}})
    meta, shots = quality.load_shots(job)
    spec = settings["judges"]["continuity"]
    plan = quality.plan_sheet(spec, 2, shots, meta, {"clean": job / "video.mp4", "captioned": job / "final.mp4"})
    story = {"title": "The Door", "lines": [{"speaker": "narrator", "text": "They started at the front door."}]}
    prompt = quality.build_prompt("continuity", spec, settings, story, 2, shots, plan, None)
    assert "Whole script, for context only:\nNARRATOR: They started at the front door." in prompt
    assert 'About the set "bedroom": one white door with a lever handle' in prompt
    assert "Last earlier shot in the same set (s01): NARRATOR: My mom texted" in prompt
    assert "One set holds several doors" in prompt and "a turned or tilted head" in prompt and "another corner of one set does not count" in prompt
    assert "About the set" not in quality.build_prompt("continuity", spec, quality.load_settings({}), story, 2, shots, plan, None)
    motion = settings["judges"]["motion"]
    assert motion["script"] and "text message" in motion["notes"] and "Words that imply an action still need the action." in motion["notes"]
    assert "speaking or working while the arms never move is frozen" in motion["flags"]["frozen_character"]["text"]
    assert "whose words say they are texting" in motion["flags"]["frozen_character"]["text"]
    assert "quietly reading, sitting or waiting under plain narration is not" in motion["flags"]["frozen_character"]["text"]
    morning = [d["text"] for d in settings["judges"]["story"]["fail_if"] if d["key"] == "wrong_time"][0]
    assert "A dim windowless corridor or room fails" in morning and "stuck on a wall or floor is not evidence" in morning
    assert "a transom over a door or a glass pane that glows white or pale" in morning and "whether or not a window is in frame" in morning


def test_a_thing_that_moves_needs_a_hand_on_it_and_a_doll_pose_fails():
    settings = quality.load_settings({})
    posture, motion = settings["judges"]["posture"], settings["judges"]["motion"]
    texts = {d["key"]: d["text"] for d in posture["fail_if"]}
    assert "no hand is on it" in texts["hand_misses"] and "a wrist or forearm lies across it" in texts["hand_misses"]
    assert "from the other side of a door, with no character at it in the frame, is not this" in texts["hand_misses"]
    assert "like a doll that has been set down" in texts["splayed"] and "legs planted unnaturally wide" in texts["splayed"]
    assert "Judge the first frame as closely as the last" in posture["notes"] and "find the hand that does it" in posture["notes"]
    assert quality.verdict(posture, {"score": 7, "defects": [{"key": "hand_misses", "where": "F1680"}]})["status"] == "fail"
    assert "no_cause" in [d["key"] for d in motion["fail_if"]] and motion["flags"]["moved_by_nothing"]["defect"] == "no_cause"
    flag = motion["flags"]["moved_by_nothing"]["text"]
    assert "a lever pressed down while the hands are up at the chest" in flag and "from the first frame on" in flag
    assert "False when no character is at it in the frame and the words give the cause" in flag
    moved = quality.verdict(motion, {"score": 10, "defects": [], "frozen_character": False, "moved_by_nothing": True})
    assert moved["status"] == "fail" and moved["defects"] == [{"key": "no_cause", "where": "moved_by_nothing"}]
    assert quality.verdict(motion, {"score": 10, "defects": [], "frozen_character": False, "moved_by_nothing": False})["status"] == "pass"


def test_a_fail_the_reader_holds_fails_the_shot_whatever_the_judge_said(job, monkeypatch, tmp_path):
    monkeypatch.setattr(vision, "ask", fake_ask({("s02", "posture"): {"defects": [{"key": "idle"}], "score": 3, "reason": "stands"}}))
    doc = quality.run_quality(job, judges=["posture", "motion"])
    assert by_id(doc)["s03"]["status"] == "pass"
    marks = {name: by_id(doc)[name]["judges"]["posture"]["render"][name] for name in ("s02", "s03")}
    hold = {"shot": "s03", "judge": "posture", "render": marks["s03"], "frames": [65], "reason": "the lever goes down with no hand on it"}
    path = job / "quality" / "overrules.json"
    path.write_text(json.dumps({"reader": "a person", "holds": [hold]}))
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"shots": [{"id": "s03", "status": "pass", "judges": {"posture": {"status": "pass"}}}]}))
    doc = quality.run_quality(job, report_only=True, against=old)
    shot = by_id(doc)["s03"]
    assert shot["status"] == "fail" and shot["failed"] == ["posture"] and shot["held"] == ["posture"]
    assert shot["judges"]["posture"]["status"] == "pass" and shot["judges"]["posture"]["held"]["frames"] == [65]
    row = doc["summary"]["judges"]["posture"]
    assert (row["fail"], row["pass"], row["held"], row["failing"]) == (2, 2, ["s03"], ["s02", "s03"])
    assert doc["summary"]["fail"] == 2 and doc["summary"]["fail_by_hold"] == 1 and doc["comparison"]["posture"]["new"] == ["s02", "s03"]
    report = (job / "QUALITY.md").read_text()
    assert "| s03 | posture | pass 9/10: clean | the lever goes down with no hand on it | 65 |" in report
    assert "  - posture, held by the reader (frames 65): the lever goes down with no hand on it" in report
    assert "| posture | 2 | 1 | 0 | 2 | 0 |" in report and "Shots that fail on these alone: 1." in report
    entry = {"shot": "s02", "judge": "posture", "render": marks["s02"], "answers": "stands", "frames": [34], "reason": "looks fine"}
    path.write_text(json.dumps({"holds": [dict(hold, shot="s02", render=marks["s02"], frames=[34])], "overrules": [entry]}))
    doc = quality.run_quality(job, report_only=True)
    assert by_id(doc)["s02"]["status"] == "fail" and doc["overrules"]["void"] == [{"shot": "s02", "judge": "posture", "why": "the reader holds this fail"}]
    path.write_text(json.dumps({"holds": [dict(hold, render="000000000000")]}))
    doc = quality.run_quality(job, report_only=True)
    assert by_id(doc)["s03"]["status"] == "pass" and by_id(doc)["s03"]["held"] == [] and doc["overrules"]["held"] == []
    assert "Not applied: s03/posture (held on an earlier render; the shot has to be read again)." in (job / "QUALITY.md").read_text()
    path.write_text(json.dumps({"holds": [dict(hold, frames=[5])]}))
    with pytest.raises(quality.QualityError, match="hold of s03/posture names frames outside the shot"):
        quality.run_quality(job, report_only=True)
    path.write_text(json.dumps({"holds": [dict(hold, reason="")]}))
    with pytest.raises(quality.QualityError, match="a hold needs"):
        quality.run_quality(job, report_only=True)


def test_the_report_prints_what_the_whole_job_cost(job, monkeypatch):
    monkeypatch.setattr(vision, "ask", fake_ask(cost=0.01))
    quality.run_quality(job, judges=["posture"], out=job / "bakeoff.json")
    doc = quality.run_quality(job, judges=["posture"])
    usage = doc["usage"]
    assert (usage["calls"], usage["cost_usd"]) == (4, 0.04) and (usage["job_runs"], usage["job_calls"], usage["job_total_usd"]) == (2, 8, 0.08)
    report = (job / "QUALITY.md").read_text()
    assert "Spend on this job $0.0800 over 8 calls in 2 runs, the total of `quality/ledger.json`; ceiling" in report
    assert "Of that, $0.0400 over 4 calls (4000 input and 400 output tokens, measured) went on the runs listed in this report" in report
    (job / "quality" / "ledger.json").write_text(json.dumps({"runs": doc["runs"]}))
    quality.run_quality(job, report_only=True)
    report = (job / "QUALITY.md").read_text()
    assert "Spend on this job $0.0400 over 4 calls in 1 runs" in report and "Of that" not in report


def test_a_fail_read_from_the_frames_and_overruled_in_writing_passes_the_shot(job, monkeypatch, tmp_path):
    monkeypatch.setattr(vision, "ask", fake_ask({("s02", "posture"): {"defects": [{"key": "idle"}], "score": 3, "reason": "stands | idle"},
                                                 ("s03", "motion"): {"defects": [{"key": "statue"}], "score": 2, "reason": "frozen"}}))
    record = by_id(quality.run_quality(job, judges=["posture", "motion"]))["s02"]["judges"]["posture"]
    entry = {"shot": "s02", "judge": "posture", "render": record["render"]["s02"], "answers": "stands | idle", "frames": [34, 52],
             "reason": "the hand comes down onto the handle between the two frames"}
    path = job / "quality" / "overrules.json"
    path.write_text(json.dumps({"reader": "a person", "overrules": [entry, dict(entry, shot="s03", judge="motion", frames=[65])]}))
    doc = quality.run_quality(job, report_only=True)
    shots = by_id(doc)
    assert shots["s02"]["status"] == "pass" and shots["s02"]["overruled"] == ["posture"] and shots["s02"]["failed"] == []
    assert shots["s02"]["judges"]["posture"]["status"] == "fail" and shots["s02"]["judges"]["posture"]["overruled"]["frames"] == [34, 52]
    assert shots["s03"]["status"] == "fail" and doc["overrules"]["void"] == [
        {"shot": "s03", "judge": "motion", "why": "the judge has given a new verdict since it was read"}]
    summary = doc["summary"]
    assert summary["pass_by_overrule"] == 1 and summary["judges"]["posture"]["fail"] == 0 and summary["judges"]["posture"]["overruled"] == ["s02"]
    report = (job / "QUALITY.md").read_text()
    assert "| s02 | posture 3/10 [idle] | stands / idle | the hand comes down onto the handle between the two frames | 34 52 |" in report
    assert "Not applied: s03/motion (the judge has given a new verdict since it was read)." in report
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"shots": [{"id": "s02", "status": "fail", "judges": {"posture": {"status": "fail"}}}]}))
    assert quality.run_quality(job, report_only=True, against=old)["comparison"]["posture"]["fixed"] == ["s02"]
    human = tmp_path / "human.json"
    human.write_text(json.dumps({"verdicts": {"s02": {"posture": "fail"}}}))
    doc = quality.run_quality(job, report_only=True, human=human)
    assert by_id(doc)["s02"]["status"] == "fail" and doc["overrules"]["void"][0]["why"] == "the reader holds this fail"
    path.write_text(json.dumps({"overrules": [dict(entry, render="000000000000")]}))
    doc = quality.run_quality(job, report_only=True)
    assert by_id(doc)["s02"]["failed"] == ["posture"] and doc["overrules"]["void"][0]["why"] == "the shot was rendered again after it was read"
    path.write_text(json.dumps({"overrules": [dict(entry, frames=[95])]}))
    with pytest.raises(quality.QualityError, match="outside the shot"):
        quality.run_quality(job, report_only=True)
    path.write_text(json.dumps({"overrules": [dict(entry, frames=[])]}))
    with pytest.raises(quality.QualityError, match="frame numbers"):
        quality.run_quality(job, report_only=True)
    path.unlink()
    doc = quality.run_quality(job, report_only=True)
    assert by_id(doc)["s02"]["status"] == "fail" and "overrules" not in doc and "## Overruled fails" not in (job / "QUALITY.md").read_text()


def test_a_fail_from_geometry_is_never_overruled(job, monkeypatch):
    (job / "posture.json").write_text(json.dumps({"version": 1, "shots": {
        "s03": {"status": "fail", "frames": [{"frame": 70, "issues": [{"kind": "stiff_arms", "actor": "mom", "detail": ""}]}]}}}))
    monkeypatch.setattr(vision, "ask", fake_ask({("s03", "posture"): {"defects": [{"key": "arms_out"}], "score": 2, "reason": "arms out"}}))
    record = by_id(quality.run_quality(job, shots=["s03"], judges=["posture"]))["s03"]["judges"]["posture"]
    (job / "quality" / "overrules.json").write_text(json.dumps({"overrules": [
        {"shot": "s03", "judge": "posture", "render": record["render"]["s03"], "answers": record["reason"], "frames": [70], "reason": "looks fine"}]}))
    doc = quality.run_quality(job, report_only=True)
    assert by_id(doc)["s03"]["status"] == "fail" and doc["overrules"]["void"][0]["why"] == "a fail from geometry or measurement is not overruled"


def test_the_lighting_judge_knows_a_light_patch_that_reads_as_a_sheet(job):
    settings = quality.load_settings(json.loads((job / "story.json").read_text()))
    assert "fake_light" in [d["key"] for d in settings["judges"]["lighting"]["fail_if"]]
    entry = quality.verdict(settings["judges"]["lighting"], {"defects": [{"key": "fake_light", "where": "F3"}], "score": 7, "reason": "window panes lie on the floor like paper"})
    assert entry["status"] == "fail" and entry["defects"][0]["key"] == "fake_light"


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
