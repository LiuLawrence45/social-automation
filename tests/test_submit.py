import json

import pytest

from pipeline import studio, submit


def shot(shot_id, **extra):
    base = {"id": shot_id, "set": "house_hall", "text": "words of %s" % shot_id, "camera": {"from": "door_front", "type": "wide"}}
    base.update(extra)
    return base


@pytest.fixture
def main(tmp_path):
    job = tmp_path / "main"
    job.mkdir()
    (job / "shotlist.json").write_text(json.dumps({"cast": {"me": {}}, "shots": [shot("a1"), shot("a2"), shot("b1")]}))
    (job / "story.json").write_text("{}")
    (job / "submissions").mkdir()
    (job / "submissions" / "groups.json").write_text(json.dumps({"hall": {"shots": ["a1", "a2"], "sets": ["house_hall"]}, "bed": {"shots": ["b1"], "sets": []}}))
    return job


def run(*argv):
    return submit.main([str(item) for item in argv])


def copy_of(main, tmp_path, name):
    target = tmp_path / name
    assert run("copy", "--job", main, "--to", target) == 0
    return target


def edit(folder, shot_id, **changes):
    data = json.loads((folder / "shotlist.json").read_text())
    for entry in data["shots"]:
        if entry["id"] == shot_id:
            entry.update(changes)
    (folder / "shotlist.json").write_text(json.dumps(data))


def shots_of(folder):
    return {entry["id"]: entry for entry in json.loads((folder / "shotlist.json").read_text())["shots"]}


def test_copy_refuses_an_existing_folder(main, tmp_path, capsys):
    copy_of(main, tmp_path, "fix")
    assert run("copy", "--job", main, "--to", tmp_path / "fix") == 2
    assert "already exists" in capsys.readouterr().err


def test_send_then_merge_replaces_only_the_named_shots_and_reports_what_changed(main, tmp_path, capsys):
    fix = copy_of(main, tmp_path, "fix")
    edit(fix, "a1", cast=[{"who": "me", "at": "hall_mid", "pose": "listen"}])
    edit(fix, "a2", camera={"from": "corridor", "type": "wide"})
    edit(fix, "b1", cast=[{"who": "me", "at": "hall_mid"}])
    assert run("send", "--job", fix, "--to", main, "--group", "hall", "--shots", "a1,a2", "--note", "posed") == 0
    assert "cast" not in shots_of(main)["a1"]
    capsys.readouterr()
    assert run("merge", "--job", main) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["changed"] == ["a1", "a2"] and report["staging_changed"] == ["a1"] and report["camera_changed"] == ["a2"]
    merged = shots_of(main)
    assert merged["a1"]["cast"][0]["pose"] == "listen" and merged["a2"]["camera"]["from"] == "corridor"
    assert "cast" not in merged["b1"]
    assert run("merge", "--job", main) == 0
    assert json.loads(capsys.readouterr().out)["merged"] == []


def test_send_refuses_another_groups_shot_a_changed_caption_and_a_foreign_set_file(main, tmp_path, capsys):
    fix = copy_of(main, tmp_path, "fix")
    assert run("send", "--job", fix, "--to", main, "--group", "hall", "--shots", "b1") == 2
    assert "not one of group hall" in capsys.readouterr().err
    edit(fix, "a1", text="other words")
    assert run("send", "--job", fix, "--to", main, "--group", "hall", "--shots", "a1") == 2
    assert "tied to the narration" in capsys.readouterr().err
    edit(fix, "a1", text="words of a1")
    (fix / "sets").mkdir()
    (fix / "sets" / "hospital.luau").write_text("return {}")
    assert run("send", "--job", fix, "--to", main, "--group", "hall", "--shots", "a1") == 2
    assert "does not belong" in capsys.readouterr().err
    assert [path.name for path in (main / "submissions").iterdir()] == ["groups.json"]


def test_merge_carries_poses_and_set_files_and_refuses_to_redefine_a_library_pose(main, tmp_path, capsys):
    library = json.loads((studio.LIBRARY_DIR / "avatar_poses.json").read_text())
    fix = copy_of(main, tmp_path, "fix")
    (fix / "poses.json").write_text(json.dumps({"hall_peek": {"action": "peering", "joints": {"Head": [0, 20, 0]}}}))
    (fix / "sets").mkdir()
    (fix / "sets" / "house_hall.luau").write_text("return {}")
    edit(fix, "a1", cast=[{"who": "me", "at": "hall_mid", "pose": "hall_peek"}])
    assert run("send", "--job", fix, "--to", main, "--group", "hall", "--shots", "a1") == 0
    capsys.readouterr()
    assert run("merge", "--job", main) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["poses"] == ["hall_peek"] and report["sets"] == ["house_hall"]
    assert json.loads((main / "poses.json").read_text())["hall_peek"]["action"] == "peering"
    assert (main / "sets" / "house_hall.luau").is_file()

    other = copy_of(main, tmp_path, "other")
    (other / "poses.json").write_text(json.dumps({"stand": dict(library["stand"], action="something else")}))
    assert run("send", "--job", other, "--to", main, "--group", "bed", "--shots", "b1") == 0
    assert run("merge", "--job", main) == 2
    assert "redefines a library pose" in capsys.readouterr().err


def test_a_later_submission_of_the_same_shot_wins_and_dry_run_writes_nothing(main, tmp_path, capsys, monkeypatch):
    fix = copy_of(main, tmp_path, "fix")
    edit(fix, "a1", cast=[{"who": "me", "pose": "listen"}])
    stamps = iter(["20260101T000001", "2026-01-01T00:00:01", "20260101T000002", "2026-01-01T00:00:02"] + ["2026-01-01T00:00:03"] * 8)
    monkeypatch.setattr(submit.time, "strftime", lambda *args: next(stamps))
    assert run("send", "--job", fix, "--to", main, "--group", "hall", "--shots", "a1") == 0
    edit(fix, "a1", cast=[{"who": "me", "pose": "recoil"}])
    assert run("send", "--job", fix, "--to", main, "--group", "hall", "--shots", "a1") == 0
    capsys.readouterr()
    assert run("merge", "--job", main, "--dry-run") == 0
    assert len(json.loads(capsys.readouterr().out)["merged"]) == 2
    assert "cast" not in shots_of(main)["a1"]
    assert run("merge", "--job", main) == 0
    assert shots_of(main)["a1"]["cast"][0]["pose"] == "recoil"
