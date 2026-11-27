import json

from pipeline import catalog, posture, studio


def test_catalog_reads_anchors_cameras_pieces_and_lights_from_a_set_file(tmp_path):
    source = tmp_path / "room.luau"
    source.write_text('''
b.info.lighting = "night_room"
b:anchor("mid", 1.2, 0, 0.6, 180)
b:anchor("door", 3, 4.2, 7)
b:camera("wide", { 6.6, 6.4, 5.6 }, { -4, 2.2, -3 }, 50)
b:piece("door", "hinge", CFrame.new(), "Y", function() end)
b:light(Vector3.new(0, 8, 0), Color3.new(1, 1, 1), 1, 20, { name = "lamp" })
''')
    found = catalog.read_set(source)
    assert found["lighting"] == "night_room"
    assert found["anchors"] == {"mid": {"pos": [1.2, 0.0, 0.6], "yaw": 180.0}, "door": {"pos": [3.0, 4.2, 7.0], "yaw": 0.0}}
    assert found["cameras"]["wide"] == {"pos": [6.6, 6.4, 5.6], "look": [-4, 2.2, -3]}
    assert found["pieces"] == {"door": "hinge"} and found["lights"] == ["lamp"]


def test_catalog_lays_a_jobs_poses_and_sets_over_the_repository(tmp_path):
    try:
        plain = catalog.collect(None)
        assert "stand" in plain["poses"] and plain["poses"]["stand"]["rest"] and "mouth" in plain["body_points"]
        job = tmp_path / "job"
        (job / "sets").mkdir(parents=True)
        name = sorted(plain["sets"])[0]
        (job / "sets" / (name + ".luau")).write_text('b:anchor("only_here", 1, 2, 3, 90)\n')
        (job / "poses.json").write_text(json.dumps({"fix_wave": {"action": "waving", "joints": {"Head": [0, 10, 0]}}}))
        (job / "shotlist.json").write_text(json.dumps({"cast": {"me": {}}, "shots": [{"id": "a1", "set": name}, {"id": "a2", "set": name}]}))
        found = catalog.collect(job)
        assert found["poses"]["fix_wave"]["action"] == "waving" and "stand" in found["poses"]
        assert list(found["sets"][name]["anchors"]) == ["only_here"]
        assert found["shots_by_set"] == {name: ["a1", "a2"]} and list(found["cast"]) == ["me"]
    finally:
        studio.use_job(None)


def test_posture_file_keeps_every_sampled_frame_and_marks_what_is_out_of_frame(tmp_path):
    samples = [
        {"frame": 10, "u": 0, "actors": {"me": {"issues": [{"code": "clip_self", "detail": "LeftHand->RightHand", "depth": 0.16, "seen": True}]}}},
        {"frame": 20, "u": 0.5, "actors": {"me": {"issues": [{"code": "floating", "detail": "nothing under or against the body", "seen": False}]}}},
        {"frame": 30, "u": 1, "actors": {"me": {"issues": []}}},
    ]
    frames = posture.frames_record(samples)
    assert [frame["frame"] for frame in frames] == [10, 20, 30]
    assert frames[0]["issues"][0]["kind"] == "intersect" and frames[0]["issues"][0]["actor"] == "me" and frames[0]["issues"][0]["in_frame"]
    assert frames[1]["issues"][0]["kind"] == "floating" and not frames[1]["issues"][0]["in_frame"]
    posture.write_public(tmp_path, [{"id": "k1", "status": "fail", "reasons": ["clip_self"], "frames": frames}, {"id": "h1", "status": "no_character"}])
    written = json.loads((tmp_path / "posture.json").read_text())
    assert written["version"] == 1 and written["shots"]["k1"]["status"] == "fail" and len(written["shots"]["k1"]["frames"]) == 3
    assert written["shots"]["h1"] == {"status": "pass", "characters": False, "reasons": [], "frames": []}
    assert "describes" not in written


def test_a_floating_piece_is_a_finding_of_the_set_even_with_nobody_in_the_shot(tmp_path):
    samples = [
        {"frame": 5, "u": 0, "actors": {}, "scene": [{"code": "piece_floating", "detail": "clock touches no surface and no hand", "seen": True},
                                                      {"code": "piece_twice", "detail": "Part of handle is built twice in the same place", "seen": False}]},
        {"frame": 9, "u": 1, "actors": {}, "scene": [{"code": "piece_floating", "detail": "clock touches no surface and no hand", "seen": True}]},
    ]
    found = posture.fold(samples)
    assert [(f["who"], f["code"], f["seen"], f["at"]) for f in found] == [("the set", "piece_floating", True, [0, 1]), ("the set", "piece_twice", False, [0])]
    frames = posture.frames_record(samples)
    assert frames[0]["issues"][0] == {"kind": "floating_piece", "actor": "the set", "detail": "clock touches no surface and no hand", "code": "piece_floating", "in_frame": True}
    assert frames[0]["issues"][1]["kind"] == "duplicate_piece" and not frames[0]["issues"][1]["in_frame"]
    entry = {"id": "b3", "status": "fail", "characters": False, "reasons": ["piece_floating"], "frames": frames}
    assert posture.summarise([entry]) == {"shots": 1, "with_characters": 0, "failed": 1, "failed_ids": ["b3"], "groups": {"a prop or set piece floats free": ["b3"]}}
    posture.write_public(tmp_path, [entry])
    written = json.loads((tmp_path / "posture.json").read_text())["shots"]["b3"]
    assert written["status"] == "fail" and written["characters"] is False and written["reasons"] == ["piece_floating"]


def test_posture_file_says_what_it_describes_and_which_shots_went_stale(tmp_path):
    shot = {"id": "k1", "set": "house_bedroom", "cast": [{"who": "me", "pose": "recoil"}], "frame_start": 0, "frame_end": 30}
    before = posture.shot_stamp(shot, {"on": "me"}, {"me": {}})
    assert before == posture.shot_stamp(dict(shot, text="other words"), {"on": "me"}, {"me": {}})
    after = posture.shot_stamp(dict(shot, cast=[{"who": "me", "pose": "cower"}]), {"on": "me"}, {"me": {}})
    assert after != before and before != posture.shot_stamp(shot, {"on": "me", "yaw": 30}, {"me": {}})
    posture.write_public(tmp_path, [{"id": "k1", "status": "pass", "stamp": before, "checked": "then"}],
                         {"job": "the-door", "label": "pass 7 draft", "stale_shots": ["k1"]}, {"k1": after})
    written = json.loads((tmp_path / "posture.json").read_text())
    assert written["describes"]["label"] == "pass 7 draft" and written["shots"]["k1"]["current"] is False and written["shots"]["k1"]["checked"] == "then"


def test_a_face_set_on_a_head_that_cannot_move_is_a_finding_against_that_character():
    samples = [{"frame": 3, "u": 0.5, "actors": {"caller": {"issues": [{"code": "face_fixed", "detail": "the face is set to talk but this avatar's head has no face controls, so it stays as it is", "seen": True}]}}, "scene": []}]
    frames = posture.frames_record(samples)
    assert frames[0]["issues"] == [{"kind": "frozen_face", "actor": "caller", "detail": samples[0]["actors"]["caller"]["issues"][0]["detail"], "code": "face_fixed", "in_frame": True}]
    assert posture.GROUPS["face_fixed"] == "a face is set on a head that cannot move"
