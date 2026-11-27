import json

import pytest

from pipeline import cast, story as story_mod


def make_story(lines, **extra):
    story = {"version": 1, "slug": "t", "title": "T", "source": {"kind": "original"}, "lines": lines}
    story.update(extra)
    return story_mod.validate_story(story)


def test_registry_is_valid_and_holds_the_pool():
    registry = cast.load_registry()
    assert {"lawrence", "jesslyn", "allan", "sharath"} <= set(registry["characters"])
    voices = [entry["voice"]["voice_id"] for entry in registry["characters"].values()]
    assert len(voices) == len(set(voices))


def test_registry_is_outside_the_director_hash():
    from pipeline import studio
    assert cast.REGISTRY.parent != studio.LIBRARY_DIR
    assert studio.LIBRARY_DIR not in cast.REGISTRY.parents


def test_pool_speaker_gets_voice_and_avatar_without_casting():
    registry = cast.load_registry()
    story = make_story([{"speaker": "narrator", "text": "It rang."}, {"speaker": "allan", "text": "Hello?"}])
    out = cast.expand(story, registry)
    assert out["speakers"]["allan"] == registry["characters"]["allan"]["voice"]
    assert out["avatars"]["allan"] == registry["characters"]["allan"]["avatar"]
    assert out["narrator"] is None and out["voice"] == {}
    assert out["names"] == ["Allan"]


def test_narrator_takes_a_members_voice_and_avatar():
    registry = cast.load_registry()
    story = make_story([{"speaker": "narrator", "text": "It rang."}], casting={"narrator": {"is": "lawrence", "as": "me"}})
    out = cast.expand(story, registry)
    assert out["voice"]["voice_id"] == registry["characters"]["lawrence"]["voice"]["voice_id"]
    assert out["avatars"] == {"me": registry["characters"]["lawrence"]["avatar"]}
    assert out["narrator"] == {"is": "lawrence", "actor": "me"}
    applied = cast.apply_story(story, out, registry)
    assert applied["voice"]["voice_id"] == out["voice"]["voice_id"]
    assert applied["voice"]["model"] == registry["voice_defaults"]["model"]
    assert "Lawrence" in applied["names"]


def test_narrator_flag_and_narrating_settings():
    registry = cast.load_registry()
    story = make_story([{"speaker": "narrator", "text": "It rang."}, {"speaker": "lawrence", "text": "Who is it?"}])
    out = cast.expand(story, registry, narrator="allan")
    assert out["voice"]["voice_id"] == registry["characters"]["allan"]["voice"]["voice_id"]
    assert out["voice"]["settings"]["speed"] == registry["narrator_voice"]["settings"]["speed"]
    assert out["voice"]["settings"]["stability"] == registry["characters"]["allan"]["voice"]["settings"]["stability"]
    assert set(out["avatars"]) == {"allan", "lawrence"}
    assert out["speakers"]["lawrence"]["voice_id"] == registry["characters"]["lawrence"]["voice"]["voice_id"]


def test_costume_matches_the_first_video():
    registry = cast.load_registry()
    story = make_story([{"speaker": "narrator", "text": "I called."}, {"speaker": "jesslyn", "text": "Stay on the line."}],
                       casting={"jesslyn": {"costume": "dispatcher"}, "allan": {"costume": "officer"}, "sharath": {"costume": "officer"}})
    out = cast.expand(story, registry)
    assert out["avatars"]["jesslyn"] == {"rig": "avatar", "bundle": 741, "headset": {"side": "L"}}
    assert out["avatars"]["allan"] == {"rig": "avatar", "bundle": 573, "vest": {"text": "POLICE"}, "legs": "#141926"}
    assert out["avatars"]["sharath"] == {"rig": "avatar", "bundle": 749, "vest": {"text": "POLICE"}}
    assert out["speakers"]["jesslyn"] == {"voice_id": "EXAVITQu4vr4xnSDxMaL", "voice_name": "Sarah", "settings": {"speed": 1, "stability": 0.7},
                                          "process": {"filter": "highpass=f=380,lowpass=f=3100,acompressor=threshold=-18dB:ratio=4:makeup=3", "gain_db": -1}}
    assert "allan" not in out["speakers"]


def test_role_played_by_a_member_and_job_overrides():
    registry = cast.load_registry()
    story = make_story([{"speaker": "narrator", "text": "She texted."}, {"speaker": "mom", "text": "Stay there."}],
                       casting={"mom": {"is": "jesslyn", "avatar": {"skin": "#aa8866"}, "voice": {"settings": {"stability": 0.9}, "process": {"gain_db": 2}}}})
    out = cast.expand(story, registry)
    assert out["avatars"]["mom"] == {"rig": "avatar", "bundle": 741, "skin": "#aa8866"}
    assert out["speakers"]["mom"]["voice_name"] == "Sarah"
    assert out["speakers"]["mom"]["settings"] == {"speed": 1, "stability": 0.9}
    assert out["speakers"]["mom"]["process"] == {"gain_db": 2}
    assert registry["characters"]["jesslyn"]["avatar"] == {"rig": "avatar", "bundle": 741}


def test_extras_are_defined_in_the_job_only():
    registry = cast.load_registry()
    before = json.dumps(registry, sort_keys=True)
    story = make_story([{"speaker": "narrator", "text": "It spoke."}, {"speaker": "thing", "text": "Open up."}, {"speaker": "machine", "text": "Unexpected item."},
                        {"speaker": "crowd", "text": "Boo."}],
                       casting={"thing": {"avatar": {"rig": "avatar", "bundle": 572, "size": 1.28}, "voice": {"voice_id": "abc", "process": {"pitch": 0.9}}},
                                "machine": {"voice": {"voice_id": "xyz"}}, "crowd": {"voice": "narrator"}})
    out = cast.expand(story, registry)
    assert out["avatars"] == {"thing": {"rig": "avatar", "bundle": 572, "size": 1.28}}
    assert out["speakers"]["thing"] == {"voice_id": "abc", "process": {"pitch": 0.9}}
    assert out["speakers"]["machine"] == {"voice_id": "xyz"}
    assert "crowd" not in out["speakers"] and out["unvoiced"] == ["crowd"]
    assert json.dumps(registry, sort_keys=True) == before


def test_unknown_speaker_is_refused_with_what_to_add():
    story = make_story([{"speaker": "narrator", "text": "It spoke."}, {"speaker": "stranger", "text": "Hey."}])
    with pytest.raises(cast.CastError) as failure:
        cast.expand(story)
    assert "casting.stranger" in str(failure.value)


def test_writer_cast_list_names_pool_people():
    registry = cast.load_registry()
    story = make_story([{"speaker": "narrator", "text": "I ran."}, {"speaker": "sharath", "text": "Stop."}],
                       cast=[{"speaker": "narrator", "name": "Lawrence", "kind": "person"}, {"speaker": "sharath", "name": "Sharath", "kind": "person"}])
    out = cast.expand(story, registry)
    assert out["narrator"]["is"] == "lawrence"
    assert out["speakers"]["sharath"]["voice_id"] == registry["characters"]["sharath"]["voice"]["voice_id"]


def test_write_job_sets_story_shotlist_and_record(tmp_path):
    story = {"version": 1, "slug": "t", "title": "T", "source": {"kind": "original"},
             "lines": [{"speaker": "narrator", "text": "It rang."}, {"speaker": "allan", "text": "Hello?"}]}
    shotlist = {"version": 1, "cast": {"allan": {"rig": "avatar", "bundle": 1}, "cat": "cat"},
                "shots": [{"id": "a", "text": "It rang. Hello?", "set": "office", "camera": {"from": "office_front"}}]}
    (tmp_path / "story.json").write_text(json.dumps(story), encoding="utf-8")
    (tmp_path / "shotlist.json").write_text(json.dumps(shotlist), encoding="utf-8")
    assert cast.main(["expand", "--job", str(tmp_path), "--narrator", "lawrence", "--write"]) == 0
    written = json.loads((tmp_path / "story.json").read_text(encoding="utf-8"))
    assert written["casting"]["narrator"] == "lawrence"
    assert written["voice"]["voice_name"] == "Brian" and written["voice"]["speakers"]["allan"]["voice_name"] == "Adam"
    assert "script" not in written
    story_mod.validate_story(written)
    listed = json.loads((tmp_path / "shotlist.json").read_text(encoding="utf-8"))
    assert listed["cast"]["allan"]["bundle"] == 573 and listed["cast"]["lawrence"]["bundle"] == 722 and listed["cast"]["cat"] == "cat"
    record = json.loads((tmp_path / "cast.json").read_text(encoding="utf-8"))
    assert sorted(record["shotlist_changed"]) == ["allan", "lawrence"]
    assert cast.check_shotlist(listed, record) == []
    listed["cast"]["allan"]["bundle"] = 2
    assert cast.check_shotlist(listed, record)


def test_sheet_job_is_a_valid_job(tmp_path):
    ids = cast.sheet_job(tmp_path)
    loaded = story_mod.load_story(tmp_path)
    assert [shot["id"] for shot in loaded["shotlist"]["shots"]] == ids
