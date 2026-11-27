from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline import story, studio

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "examples" / "found-box"


def test_bundle_carries_every_module_and_the_library():
    chunk = studio.install_chunk()
    for path in (REPO / "studio" / "director").glob("*.luau"):
        assert '__modules["%s"]' % path.stem in chunk
    for name in studio.set_names():
        assert '__modules["set_%s"]' % name in chunk
    assert "shared.__director = { hash = '%s'" % studio.director_hash() in chunk
    assert set(studio.load_library()) == {"actions", "animations", "avatar_poses", "characters", "expressions", "faces", "lighting", "poses", "rubric"}


def test_call_chunk_checks_the_installed_hash_and_carries_the_command():
    chunk = studio.call_chunk({"command": "frame", "args": {"n": 12}})
    assert studio.director_hash() in chunk
    assert studio.MISSING in chunk
    assert json.dumps({"command": "frame", "args": {"n": 12}}) in chunk


def test_long_strings_refuse_their_own_terminator():
    with pytest.raises(studio.DirectorError):
        studio._long_string("oops " + studio.LONG_CLOSE)


def test_actions_build_on_poses_that_exist():
    library = studio.load_library()
    for name, action in library["actions"].items():
        base = action.get("base")
        if isinstance(base, str):
            assert base in library["poses"], "action %s uses unknown pose %s" % (name, base)


def test_example_shot_list_uses_only_what_the_library_and_sets_provide():
    loaded = story.load_story(EXAMPLE)
    library = studio.load_library()
    sets = {}
    for name in studio.set_names():
        source = (REPO / "studio" / "director" / "sets" / (name + ".luau")).read_text(encoding="utf-8")
        sets[name] = {"anchors": set(re.findall(r'anchor\("([a-z_0-9]+)"', source)), "cameras": set(re.findall(r'camera\("([a-z_0-9]+)"', source))}
    props_source = (REPO / "studio" / "director" / "props.luau").read_text(encoding="utf-8")
    for role, character in loaded["shotlist"]["cast"].items():
        assert character in library["characters"], role
    for shot in loaded["shotlist"]["shots"]:
        assert shot["set"] in sets, shot["id"]
        known = sets[shot["set"]]
        for actor in shot.get("cast", []):
            if isinstance(actor["at"], str):
                assert actor["at"] in known["anchors"], shot["id"]
            if "action" in actor:
                assert actor["action"] in library["actions"], shot["id"]
            if "pose" in actor:
                assert actor["pose"] in library["poses"], shot["id"]
            faces = actor.get("face", "neutral")
            for face in ([faces] if isinstance(faces, str) else [key[1] for key in faces]):
                assert face in library["faces"], shot["id"]
        for prop in shot.get("props", []):
            assert re.search(r"\b%s\b" % re.escape(prop["prop"]), props_source), shot["id"]
        camera = shot["camera"].get("from", shot["camera"])
        if isinstance(camera, str):
            assert camera in known["cameras"], shot["id"]
        if "time" in shot:
            assert shot["time"] in library["lighting"], shot["id"]


@pytest.mark.skipif(shutil.which("luau-compile") is None, reason="luau-compile is not installed")
def test_bundle_compiles(tmp_path):
    target = tmp_path / "bundle.luau"
    target.write_text(studio.install_chunk(), encoding="utf-8")
    proc = subprocess.run(["luau-compile", "--null", str(target)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    assert proc.returncode == 0, proc.stdout
