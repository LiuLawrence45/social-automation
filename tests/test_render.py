from __future__ import annotations

import struct

import pytest

from pipeline import render


def bmp(width, height, pixel, bits=24, top_down=False):
    step = bits // 8
    row_size = ((bits * width + 31) // 32) * 4
    rows = []
    for y in (range(height) if top_down else range(height - 1, -1, -1)):
        row = bytearray()
        for x in range(width):
            red, green, blue = pixel(x, y)
            row += bytes([blue, green, red] + ([255] if step == 4 else []))
        row += bytes(row_size - len(row))
        rows.append(bytes(row))
    header = b"BM" + struct.pack("<IHHI", 54 + row_size * height, 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, width, -height if top_down else height, 1, bits, 0, row_size * height, 2835, 2835, 0, 0)
    return render.Bitmap(header + info + b"".join(rows))


def gradient(x, y):
    return (x * 10, y * 20, 7)


@pytest.mark.parametrize("bits,top_down", [(24, False), (32, True)])
def test_bitmap_reads_pixels_and_crops(bits, top_down):
    image = bmp(7, 5, gradient, bits, top_down)
    assert (image.width, image.height) == (7, 5)
    assert image.pixel(3, 2) == (30, 40, 7)
    assert image.pixel(6, 4) == (60, 80, 7)
    step = bits // 8
    crop = image.crop(2, 1, 3, 2)
    assert len(crop) == 3 * 2 * step
    assert crop[0:3] == bytes([7, 20, 20])
    assert image.pix_fmt == ("bgra" if bits == 32 else "bgr24")


def stage_with_marker(number, left=3, top=124, cell=6, bits=16, margin=2, damage=None):
    stage = render.Stage(None)
    stage.viewport = (left, top, 400, 300)
    stage.marker = {"cell": cell, "bits": bits, "margin": margin}
    stage.scale = 1.0
    pattern = [1, 0] + [(number >> bit) & 1 for bit in range(bits - 1, -1, -1)] + [0, 1]
    if damage is not None:
        pattern[damage] = 1 - pattern[damage]

    def pixel(x, y):
        inside_x, inside_y = x - left - margin, y - top - margin
        if 0 <= inside_y < cell and 0 <= inside_x < cell * len(pattern):
            value = 255 if pattern[inside_x // cell] else 0
            return (value, value, value)
        return (90, 140, 200)

    return stage, bmp(left + 200, top + 40, pixel)


def test_frame_marker_round_trip():
    for number in (0, 1, 1835, 42435, 65535):
        stage, image = stage_with_marker(number)
        assert stage.read_marker(image) == number


def test_frame_marker_rejects_a_broken_guard_cell_or_scene_colour():
    stage, image = stage_with_marker(77, damage=0)
    assert stage.read_marker(image) is None
    stage, _ = stage_with_marker(77)
    assert stage.read_marker(bmp(260, 200, lambda x, y: (90, 140, 200))) is None


def test_crop_uses_the_long_side_when_the_camera_is_rolled():
    stage = render.Stage(None, roll=90.0)
    stage.viewport = (3, 124, 1914, 882)
    assert stage.crop_size() == (1568, 882)
    assert stage.video_filter().startswith("transpose=2,scale=1080:1920")
    upright = render.Stage(None, roll=0.0)
    upright.viewport = (3, 124, 1914, 882)
    assert upright.crop_size() == (496, 882)
    assert upright.video_filter().startswith("scale=1080:1920")


def test_shots_render_grouped_by_set_and_can_be_picked():
    timeline = {"shots": [{"id": "a", "set": "hall"}, {"id": "b", "set": "street"}, {"id": "c", "set": "hall"}, {"id": "d", "set": "street"}]}
    assert render.by_set(timeline["shots"]) == [0, 2, 1, 3]
    assert [shot["id"] for shot in render.pick_shots(timeline, "c, a")] == ["a", "c"]
    assert render.pick_shots(timeline, None) == timeline["shots"]
    with pytest.raises(render.RenderError, match="unknown shot"):
        render.pick_shots(timeline, "zz")



def test_load_timeline_applies_the_judge_choice(tmp_path):
    import json

    (tmp_path / "timeline.json").write_text(json.dumps({"fps": 30, "shots": [{"id": "a", "camera": {"on": "me"}}, {"id": "b", "camera": {"from": "wide"}}]}))
    assert render.load_timeline(tmp_path)["shots"][0]["camera"] == {"on": "me"}
    (tmp_path / "judge.json").write_text(json.dumps({"shots": [{"id": "a", "chosen": {"camera": {"on": "me", "yaw": 40}}}]}))
    assert [s["camera"] for s in render.load_timeline(tmp_path)["shots"]] == [{"on": "me", "yaw": 40}, {"from": "wide"}]
    assert render.load_timeline(tmp_path, judged=False)["shots"][0]["camera"] == {"on": "me"}
