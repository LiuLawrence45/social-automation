import json
import shutil
from pathlib import Path

import pytest

from pipeline import bed, checks, judge, make, story as story_mod

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "found-box"
STAGE_ORDER = ["pipeline.sound", "pipeline.judge shots", "pipeline.judge posture", "pipeline.render video", "pipeline.edit", "pipeline.quality"]


class Fake:
    """Stands in for every stage command: writes what the real stage would, with no Studio, key or network."""

    def __init__(self, job, failing=None, head=0.3, posture_failed=None):
        self.job = Path(job)
        self.calls = []
        self.failing = failing or {}
        self.head = head
        self.posture_failed = posture_failed or []
        self.envs = []

    def __call__(self, command, cwd, env, log):
        module, args = command[2], command[3:]
        self.calls.append([module] + list(args))
        self.envs.append({"cwd": str(cwd), "WRITER_LEDGER": env.get("WRITER_LEDGER"), "STUDIO_HOLDER": env.get("STUDIO_HOLDER")})
        job = self.job
        if module == "pipeline.writer":
            story = {"version": 1, "slug": job.name, "title": "T", "source": {"kind": "original"}, "music": "A thin drone.",
                     "lines": [{"speaker": "narrator", "text": "The till beeped at night."}, {"speaker": "kiosk", "text": "Unexpected item."}],
                     "cast": [{"speaker": "narrator", "name": "Narrator", "kind": "role"},
                              {"speaker": "kiosk", "name": "Kiosk", "kind": "voice", "role": "the machine", "voice": {"gender": "female", "processing": "none"}}],
                     "sound_cues": [{"line": 1, "at": "start", "kind": "sfx", "prompt": "A beep.", "seconds": 1.0}]}
            (job / "story.json").write_text(json.dumps(story), encoding="utf-8")
            (job / "script_judge.json").write_text(json.dumps({"passed": True}), encoding="utf-8")
            with open(env["WRITER_LEDGER"], "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"cost_usd": 0.06}) + "\n")
        elif module == "pipeline.voice":
            story = story_mod.validate_story(json.loads((job / "story.json").read_text(encoding="utf-8")))
            speakers = [line["speaker"] for line in story_mod.story_lines(story) for _ in story_mod.tokenize(line["text"])]
            tokens = story_mod.tokenize(story["script"])
            words = [{"i": i, "token": token, "display": token, "start": round(i * 0.3, 3), "end": round(i * 0.3 + 0.25, 3), "sentence": 0, "speaker": speakers[i]}
                     for i, token in enumerate(tokens)]
            (job / "words.json").write_text(json.dumps({"version": 1, "audio": "narration.wav", "duration": round(len(tokens) * 0.3, 3), "words": words}), encoding="utf-8")
            (job / "narration.wav").write_bytes(b"RIFF")
            provider = "say" if "--provider" in args or (story.get("voice") or {}).get("provider") == "say" else "elevenlabs"
            (job / "voice_report.json").write_text(json.dumps({"provider": provider, "final_wpm": 200, "speakers": {"narrator": {"characters_billed": 1000}}}), encoding="utf-8")
        elif module == "pipeline.story":
            return story_mod.main(list(args))
        elif module == "pipeline.sound":
            (job / "bed.wav").write_bytes(b"RIFF")
            provider = "synthetic" if "synthetic" in args else "elevenlabs"
            (job / "sound_report.json").write_text(json.dumps({"provider": provider, "duration": 5.0, "generated": 1, "reused": 0, "items": [{"cost": "500"}], "music": {"cost": None}}), encoding="utf-8")
        elif module == "pipeline.judge" and args[0] == "shots":
            timeline = json.loads((job / "timeline.json").read_text(encoding="utf-8"))
            old = json.loads((job / "judge.json").read_text(encoding="utf-8")) if (job / "judge.json").is_file() else {"usage": {"cost_usd": 0.0}}
            shots = [{"id": shot["id"], "chosen": {"camera": dict(shot["camera"]), "metrics": {"head": [0.5, self.head]}}} for shot in timeline["shots"]]
            cost = 0.0 if "none" in args else 0.05
            (job / "judge.json").write_text(json.dumps(dict(old, shots=shots, usage={"cost_usd": old["usage"]["cost_usd"] + cost}, config={"model": "m"}, variety={"problems": []})), encoding="utf-8")
        elif module == "pipeline.judge" and args[0] == "posture":
            doc = json.loads((job / "judge.json").read_text(encoding="utf-8"))
            doc["posture"] = {"shots": [{"id": shot_id, "reasons": ["floating: a prop touches nothing"]} for shot_id in self.posture_failed],
                              "summary": {"shots": len(doc["shots"]), "failed": len(self.posture_failed), "failed_ids": list(self.posture_failed)}}
            (job / "judge.json").write_text(json.dumps(doc), encoding="utf-8")
            (job / "posture.json").write_text(json.dumps({"version": 1, "shots": {}}), encoding="utf-8")
            return 1 if self.posture_failed else 0
        elif module == "pipeline.render":
            timeline = json.loads((job / "timeline.json").read_text(encoding="utf-8"))
            (job / "video.mp4").write_bytes(json.dumps(timeline["shots"], sort_keys=True).encode("utf-8"))
            (job / "shots.json").write_text(json.dumps(story_mod.shots_handoff(timeline)), encoding="utf-8")
            (job / "render_report.json").write_text(json.dumps({"frames_total": 10, "frames_rendered": 10, "frames_per_second": 6, "shots_kept": 0}), encoding="utf-8")
        elif module == "pipeline.edit":
            (job / "final.mp4").write_bytes(b"final" + (job / "video.mp4").read_bytes())
            (job / "edit_report.json").write_text(json.dumps({"output_duration": 5.0, "caption_events": 9}), encoding="utf-8")
        elif module == "pipeline.quality":
            timeline = json.loads((job / "timeline.json").read_text(encoding="utf-8"))
            judges = {"posture": {"failing": [shot_id for shot_id, keys in self.failing.items() if "posture" in keys]},
                      "story": {"failing": [shot_id for shot_id, keys in self.failing.items() if "story" in keys]}}
            shots = [{"id": shot["id"], "set": shot["set"], "line": shot.get("text"), "judges": {key: {"reason": "it failed"} for key in self.failing.get(shot["id"], [])}}
                     for shot in timeline["shots"]]
            summary = {"shots": len(shots), "pass": len(shots) - len(self.failing), "fail": len(self.failing), "incomplete": 0, "judges": judges}
            (job / "quality.json").write_text(json.dumps({"summary": summary, "shots": shots}), encoding="utf-8")
            (job / "QUALITY.md").write_text("# Quality\n", encoding="utf-8")
            ledger = job / "quality" / "ledger.json"
            ledger.parent.mkdir(exist_ok=True)
            runs = json.loads(ledger.read_text(encoding="utf-8")) if ledger.is_file() else {"runs": []}
            runs["runs"].append({"cost_usd": 0.1})
            ledger.write_text(json.dumps(runs), encoding="utf-8")
        return 0

    def modules(self):
        return [" ".join(call[:2]) if call[0] in ("pipeline.judge", "pipeline.story", "pipeline.render", "pipeline.writer") else call[0] for call in self.calls]


def good_sample(tmp_path):
    """The sample story with a shot list that passes the defaults."""
    source = tmp_path / "source"
    shutil.copytree(str(SAMPLE), str(source))
    story = json.loads((source / "story.json").read_text(encoding="utf-8"))
    story["judge"] = {"provider": "anthropic", "posture": True, "variety": {"cap": 1.0, "slack": 0.0, "min_moving": 0.0}}
    (source / "story.json").write_text(json.dumps(story), encoding="utf-8")
    shotlist = json.loads((source / "shotlist.json").read_text(encoding="utf-8"))
    for shot in shotlist["shots"]:
        tag(shot)
        for actor in shot.get("cast", []):
            actor.setdefault("action", "nod")
    (source / "shotlist.json").write_text(json.dumps(shotlist), encoding="utf-8")
    return source


def tag(shot):
    if not judge.is_locked(shot):
        shot["camera"].setdefault("type", judge.camera_type(shot["camera"]))


def run(job, fake, *args):
    return make.main(["--out", str(job)] + list(args), runner=fake)


def state_of(job):
    return json.loads((job / "make.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    for name in ("ELEVENLABS_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(name, "x")
    monkeypatch.delenv("MAKE_WRITER", raising=False)
    monkeypatch.delenv("MAKE_RUN", raising=False)


def test_dry_run_runs_and_writes_nothing(tmp_path, capsys):
    job = tmp_path / "job"
    fake = Fake(job)
    assert run(job, fake, "--story", str(SAMPLE / "story.json"), "--dry-run") == 0
    assert not job.exists() and fake.calls == []
    printed = capsys.readouterr().out
    assert all(stage in printed for stage in make.STAGES)


def test_sample_stops_on_the_defaults_then_finishes(tmp_path):
    job = tmp_path / "job"
    fake = Fake(job)
    assert run(job, fake, "--story", str(SAMPLE / "story.json")) == 3
    assert state_of(job)["stages"]["checks"]["status"] == "needs_agent"
    request = (job / "REQUEST-checks.md").read_text(encoding="utf-8")
    assert "no_idle" in request and "s04" in request and "camera_tag" in request and "pipeline.make --out" in request and "{run}" not in request
    assert "pipeline.judge shots" not in fake.modules()
    story = json.loads((job / "story.json").read_text(encoding="utf-8"))
    story["judge"]["variety"] = {"cap": 1.0, "slack": 0.0, "min_moving": 0.0}
    (job / "story.json").write_text(json.dumps(story), encoding="utf-8")
    shotlist = json.loads((job / "shotlist.json").read_text(encoding="utf-8"))
    shotlist["shots"][3]["cast"][1]["action"] = "nod"
    for shot in shotlist["shots"]:
        tag(shot)
    (job / "shotlist.json").write_text(json.dumps(shotlist), encoding="utf-8")
    assert checks.check_job(job)["errors"] == []
    assert run(job, fake) == 0
    state = state_of(job)
    assert [state["stages"][stage]["status"] for stage in make.STAGES] == ["done"] * len(make.STAGES)
    assert not (job / "REQUEST-checks.md").exists() and (job / "requests_done").is_dir()
    table = (job / "MAKE.md").read_text(encoding="utf-8")
    assert (job / "final.mp4").is_file() and "| render | done |" in table and "| final | done | 1 |" in table
    assert state["stages"]["camera"]["spend_usd"] == pytest.approx(0.05) and state["stages"]["quality"]["spend_usd"] == pytest.approx(0.1)
    assert make.ledger_total(job) == pytest.approx(0.30)
    assert state["stages"]["voice"]["inputs"] and state["stages"]["render"]["outputs"] == ["video.mp4", "shots.json"]
    assert [name for name in fake.modules() if name != "pipeline.story timeline"][-6:] == STAGE_ORDER


def test_checks_read_the_shot_list_not_a_stale_timeline(tmp_path):
    job = tmp_path / "job"
    assert run(job, Fake(job), "--story", str(SAMPLE / "story.json"), "--to", "shots") == 0
    assert "camera_variety" in checks.check_job(job)["counts"]
    shotlist = json.loads((job / "shotlist.json").read_text(encoding="utf-8"))
    by = {shot["id"]: shot for shot in shotlist["shots"]}
    by["s01"]["camera"]["type"] = by["s04"]["camera"]["type"] = "wide"
    by["s04"]["cast"][1]["action"] = "nod"
    by["s06"]["camera"]["pitch"], by["s09"]["camera"]["pitch"], by["s10"]["camera"]["dutch"] = 24, -15, 8
    for shot in shotlist["shots"]:
        tag(shot)
    (job / "shotlist.json").write_text(json.dumps(shotlist), encoding="utf-8")
    assert checks.check_job(job)["errors"] == []


def test_rerun_skips_finished_stages_and_from_reruns(tmp_path):
    job = tmp_path / "job"
    fake = Fake(job)
    assert run(job, fake, "--story", str(good_sample(tmp_path) / "story.json")) == 0
    done = len(fake.calls)
    assert run(job, fake) == 0
    assert len(fake.calls) == done
    assert run(job, fake, "--from", "edit") == 0
    assert [call[0] for call in fake.calls[done:]] == ["pipeline.edit", "pipeline.quality"]
    assert "--changed" in fake.calls[-1]


def test_to_stops_after_a_stage(tmp_path):
    job = tmp_path / "job"
    fake = Fake(job)
    assert run(job, fake, "--story", str(good_sample(tmp_path) / "story.json"), "--to", "voice") == 0
    assert fake.modules() == ["pipeline.voice"] and not (job / "timeline.json").exists()


def test_story_without_a_shot_list_stops_for_an_agent(tmp_path):
    job = tmp_path / "job"
    source = tmp_path / "story.json"
    shutil.copyfile(str(SAMPLE / "story.json"), str(source))
    fake = Fake(job)
    assert run(job, fake, "--story", str(source)) == 3
    request = (job / "REQUEST-shots.md").read_text(encoding="utf-8")
    for heading in ("## What is needed", "## Inputs", "## Commands", "## Done when"):
        assert heading in request
    draft = json.loads((job / "shotlist.draft.json").read_text(encoding="utf-8"))
    story = story_mod.validate_story(json.loads((job / "story.json").read_text(encoding="utf-8")))
    assert " ".join(shot["text"] for shot in draft["shots"]) == story["script"]
    shotlist = json.loads((SAMPLE / "shotlist.json").read_text(encoding="utf-8"))
    shotlist["shots"][0]["set"] = "moon_base"
    (job / "shotlist.json").write_text(json.dumps(shotlist), encoding="utf-8")
    assert run(job, fake) == 3
    assert "moon_base" in (job / "REQUEST-set-moon_base.md").read_text(encoding="utf-8")
    (job / "sets").mkdir()
    shutil.copyfile(str(SAMPLE.parent.parent / "studio" / "director" / "sets" / "hallway.luau"), str(job / "sets" / "moon_base.luau"))
    assert run(job, fake, "--to", "shots") == 0
    assert not (job / "REQUEST-set-moon_base.md").exists() and (job / "timeline.json").is_file()


def test_fix_loop_rejudges_only_the_changed_shot(tmp_path):
    job = tmp_path / "job"
    fake = Fake(job, failing={"s05": ["posture"]})
    assert run(job, fake, "--story", str(good_sample(tmp_path) / "story.json")) == 3
    request = (job / "REQUEST-fix-1.md").read_text(encoding="utf-8")
    assert "s05" in request and "posture: it failed" in request and "--accept s05" in request
    assert run(job, fake) == 3
    assert state_of(job)["fix"]["round"] == 1
    shotlist = json.loads((job / "shotlist.json").read_text(encoding="utf-8"))
    shotlist["shots"][4]["cast"][0]["action"] = "hesitate"
    (job / "shotlist.json").write_text(json.dumps(shotlist), encoding="utf-8")
    fake.failing = {}
    before = len(fake.calls)
    assert run(job, fake) == 0
    later = fake.calls[before:]
    camera = [call for call in later if call[:2] == ["pipeline.judge", "shots"]][0]
    assert camera[camera.index("--shots") + 1] == "s05"
    assert "--changed" in [call for call in later if call[0] == "pipeline.quality"][0]
    assert not list(job.glob("REQUEST-fix-*.md"))


def test_fix_rounds_run_out_and_accept_closes(tmp_path):
    job = tmp_path / "job"
    fake = Fake(job, failing={"s05": ["story"]})
    source = good_sample(tmp_path)
    assert run(job, fake, "--story", str(source / "story.json"), "--fix-rounds", "0") == 0
    assert "still fail" in state_of(job)["stages"]["fix"]["note"] and (job / "MAKE.md").is_file()
    other = tmp_path / "job2"
    fake2 = Fake(other, failing={"s05": ["story"]})
    assert run(other, fake2, "--story", str(source / "story.json")) == 3
    assert run(other, fake2, "--accept", "s05") == 1
    assert run(other, fake2, "--accept", "s05", "--reason", "the judge misreads the note") == 0
    assert "accepted" in state_of(other)["stages"]["fix"]["note"]


def test_spend_ceiling_stops_paid_stages(tmp_path):
    job = tmp_path / "job"
    fake = Fake(job)
    source = good_sample(tmp_path)
    story = json.loads((source / "story.json").read_text(encoding="utf-8"))
    story["voice"] = {"provider": "elevenlabs", "voice_id": "v"}
    (source / "story.json").write_text(json.dumps(story), encoding="utf-8")
    assert run(job, fake, "--story", str(source / "story.json"), "--budget", "0.2") == 1
    state = state_of(job)
    assert state["stages"]["voice"]["spend_usd"] == pytest.approx(0.3)
    assert state["stages"]["sound"]["status"] == "failed" and "ceiling" in state["stages"]["sound"]["note"]
    assert run(job, fake, "--budget", "0.5") == 0
    state = state_of(job)
    assert state["stages"]["quality"]["status"] == "skipped" and state["stages"]["final"]["status"] == "done"
    assert "pipeline.quality" not in fake.modules()


def test_missing_key_is_named_and_offline_needs_none(tmp_path, monkeypatch):
    job = tmp_path / "job"
    fake = Fake(job)
    for name in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(name)
    source = good_sample(tmp_path)
    assert run(job, fake, "--story", str(source / "story.json")) == 1
    note = state_of(job)["stages"]["sound"]["note"]
    assert "ELEVENLABS_API_KEY is not set" in note
    assert run(job, fake, "--offline") == 0
    state = state_of(job)
    assert state["stages"]["quality"]["status"] == "skipped" and make.ledger_total(job) == 0
    assert ["pipeline.sound", "--job", str(job.resolve()), "--provider", "synthetic"] in fake.calls


def test_caption_band_and_posture_stop_before_the_render(tmp_path):
    job = tmp_path / "job"
    fake = Fake(job, head=0.7)
    source = good_sample(tmp_path)
    assert run(job, fake, "--story", str(source / "story.json")) == 3
    assert "caption band" in (job / "REQUEST-camera.md").read_text(encoding="utf-8")
    assert "pipeline.render video" not in fake.modules()
    fake.head = 0.3
    fake.posture_failed = ["s02"]
    assert run(job, fake, "--from", "camera") == 3
    assert "s02: floating" in (job / "REQUEST-posture.md").read_text(encoding="utf-8")
    assert "pipeline.render video" not in fake.modules()
    fake.posture_failed = []
    assert run(job, fake, "--from", "posture") == 0


def test_writer_is_called_from_its_own_checkout_with_its_own_ledger(tmp_path):
    job = tmp_path / "job"
    writer = tmp_path / "writer"
    (writer / "pipeline" / "writer").mkdir(parents=True)
    text = tmp_path / "story.txt"
    text.write_text("Once, a till beeped.", encoding="utf-8")
    fake = Fake(job)
    assert run(job, fake, "--story", str(text)) == 1
    assert "MAKE_WRITER" in state_of(job)["stages"]["script"]["note"]
    assert run(job, fake, "--story", str(text), "--writer", str(writer), "--narrator", "allan", "--to", "cast") == 0
    assert fake.calls[0][:2] == ["pipeline.writer", "write"] and fake.envs[0]["cwd"] == str(writer)
    assert fake.envs[0]["WRITER_LEDGER"] == str(job.resolve() / "writer_ledger.jsonl")
    assert state_of(job)["stages"]["script"]["spend_usd"] == pytest.approx(0.06)
    story = json.loads((job / "story.json").read_text(encoding="utf-8"))
    assert story["voice"]["voice_name"] == "Adam" and story["casting"]["narrator"] == "allan" and "judge" in story
    assert json.loads((job / "cast.json").read_text(encoding="utf-8"))["members"]["kiosk"]["auto"] is True


def test_sound_json_from_the_writers_cues():
    story = story_mod.validate_story({"version": 1, "slug": "t", "title": "T", "source": {"kind": "original"}, "music": "A thin drone.", "writer": {"genre": "scary"},
                                      "lines": [{"speaker": "narrator", "text": "One two three."}, {"speaker": "narrator", "text": "Four five."}],
                                      "sound_cues": [{"line": 0, "at": "start", "kind": "ambience", "prompt": "Hum.", "seconds": 3},
                                                     {"line": 1, "at": "start", "kind": "sfx", "prompt": "Beep.", "seconds": 1},
                                                     {"line": 1, "at": "end", "kind": "silence", "prompt": "Quiet.", "seconds": 1}]})
    sound = make.sound_from_story(story)
    plain = make.sound_from_story(story_mod.validate_story(json.loads((SAMPLE / "story.json").read_text(encoding="utf-8"))))
    assert sound["music"]["fallback"] in bed.MOODS and plain["music"]["fallback"] in bed.MOODS
    assert sound["music"]["prompt"].startswith("A thin drone.") and sound["music"]["cuts"] == [{"at": {"word": 4, "offset": 0.3}, "seconds": 1.0}]
    assert sound["ambience"][0]["from"] == {"word": 0, "offset": 0} and sound["cues"][0]["at"] == {"word": 3, "offset": 0.0}


def avatar_job(tmp_path, shots, lines=None, **story_extra):
    job = tmp_path / "avatars"
    job.mkdir()
    text = " ".join(shot["text"] for shot in shots)
    story = {"version": 1, "slug": "avatars", "title": "T", "source": {"kind": "original"}, "checks": {"off": ["camera_variety"]}}
    story.update({"lines": lines} if lines else {"script": text})
    story.update(story_extra)
    (job / "story.json").write_text(json.dumps(story), encoding="utf-8")
    full = [dict({"set": "house_hall", "camera": {"from": "door_wide", "move": "push"}}, **shot) for shot in shots]
    (job / "shotlist.json").write_text(json.dumps({"version": 1, "cast": {"me": {"rig": "avatar", "bundle": 722}}, "shots": full}), encoding="utf-8")
    return job


def rules(result, kind="errors"):
    return sorted({(entry["rule"], entry["shot"]) for entry in result[kind]})


def test_checks_idle_face_fear_and_motion(tmp_path):
    me = {"who": "me", "at": "hall_mid"}
    job = avatar_job(tmp_path, [
        {"id": "a", "text": "I stood there.", "cast": [dict(me, face="uneasy")]},
        {"id": "b", "text": "It was quiet.", "cast": [dict(me, pose="still", face="uneasy")]},
        {"id": "c", "text": "Then I screamed.", "cast": [dict(me, pose="recoil", face="smile")]},
        {"id": "d", "text": "I screamed again.", "cast": [dict(me, pose="gasp_back", face="neutral")]},
        {"id": "e", "text": "I grabbed the handle.", "cast": [dict(me, pose="listen", face="scared")]},
        {"id": "f", "text": "Nothing.", "cast": [dict(me, pose="hug_self")]},
        {"id": "g", "text": "I backed away.", "cast": [dict(me, pose="back_away", face="terrified", to="hall_far")]},
    ])
    found = rules(checks.check_job(job))
    assert ("no_idle", "a") in found and ("no_idle", "b") in found
    assert ("fear_face", "c") in found and ("fear_face", "d") in found
    assert ("motion", "e") in found and ("face", "f") in found
    assert not [entry for entry in found if entry[1] == "g"]


def test_checks_unknown_face_sun_patch_and_reach(tmp_path):
    me = {"who": "me", "at": "hall_mid", "pose": "listen"}
    job = avatar_job(tmp_path, [
        {"id": "a", "text": "One.", "cast": [dict(me, face="calm")]},
        {"id": "b", "text": "Two.", "cast": [dict(me, pose="hug_self", face="uneasy", reach={"L": {"to": "thigh"}})], "props": [{"prop": "sun_patch", "at": "hall_mid"}]},
        {"id": "c", "text": "Three.", "cast": [dict(me, pose="clutch_chest", face=[[0, "uneasy"], [0.5, "scared"]], reach={"L": "ribs"}, holds={"prop": "phone", "hand": "R"})]},
    ])
    result = checks.check_job(job)
    assert rules(result) == [("names", "a"), ("sun_patch", "b")]
    assert "face 'calm' does not exist" in result["errors"][0]["message"]
    assert rules(result, "warnings") == [("reach", "b"), ("reach", "c")]


def test_checks_time_of_day_and_allow(tmp_path):
    me = {"who": "me", "at": "hall_mid", "face": "uneasy", "pose": "listen"}
    job = avatar_job(tmp_path, [
        {"id": "a", "text": "That night it came.", "time": "night_hall", "cast": [me]},
        {"id": "b", "text": "The next morning", "time": "night_hall", "cast": [dict(me, pose="hug_self")]},
        {"id": "c", "text": "it was gone.", "time": "interior", "cast": [dict(me, pose="clutch_chest")]},
        {"id": "d", "text": "Truly gone.", "time": "grey_morning", "cast": [dict(me, pose="rub_eye")]},
        {"id": "e", "text": "Every night since,", "time": "day", "daypart": "any", "cast": [dict(me, pose="explain")]},
        {"id": "f", "text": "I wait.", "time": "night_dread", "cast": [me], "allow": ["no_idle"], "allow_reason": "a held beat"},
    ])
    result = checks.check_job(job)
    assert rules(result) == [("time_of_day", "b")]
    assert ("time_of_day", "c") in rules(result, "warnings")


def test_checks_pose_variety_and_names(tmp_path):
    me = {"who": "me", "at": "hall_mid", "face": "scared"}
    shots = [{"id": "s%d" % index, "text": "Word%d." % index, "cast": [dict(me, pose="hand_over_mouth" if index < 4 else "recoil")]} for index in range(6)]
    shots.append({"id": "bad", "text": "End.", "set": "nowhere", "cast": [dict(me, at="no_anchor", pose="made_up")]})
    result = checks.check_job(avatar_job(tmp_path, shots))
    messages = " | ".join(entry["message"] for entry in result["errors"])
    assert "hand_over_mouth in 4 of" in messages and "more than 2 shots running" in messages
    assert "set 'nowhere' does not exist" in messages and "pose 'made_up' does not exist" in messages
    assert ("names", "bad") in rules(result)
