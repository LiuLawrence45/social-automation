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
