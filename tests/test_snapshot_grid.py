import json
import os

from PIL import Image as _Image

import snapshot_grid as sg

_SAMPLE_PATH = os.path.join(os.path.dirname(__file__), "sample_response.json")
with open(_SAMPLE_PATH, encoding="utf-8") as f:
    SAMPLE = json.load(f)


def test_build_cover_url():
    model = {"id": 199570657, "snapshotTimestamp": "1784733180"}
    assert (
        sg.build_cover_url(model)
        == "https://img.doppiocdn.org/thumbs/1784733180/199570657"
    )


def test_pick_covers_dedup_and_limit():
    # 样例只有 2 个可用模型，请求 9 个应只返回 2 个且不重复
    picks = sg.pick_9_covers(SAMPLE, n=9)
    ids = [p["id"] for p in picks]
    assert len(picks) == 2
    assert len(set(ids)) == 2
    assert set(ids) == {199570657, 242433118}


def test_pick_covers_url_shape():
    picks = sg.pick_9_covers(SAMPLE, n=9)
    for p in picks:
        assert p["url"].startswith("https://img.doppiocdn.org/thumbs/")


def test_pick_covers_respects_n():
    picks = sg.pick_9_covers(SAMPLE, n=1)
    assert len(picks) == 1


def test_fit_cover_exact_size():
    src = _Image.new("RGB", (1000, 500), "red")
    out = sg.fit_cover(src, 320, 240)
    assert out.size == (320, 240)


def test_fit_cover_tall_source():
    src = _Image.new("RGB", (100, 900), "blue")
    out = sg.fit_cover(src, 320, 240)
    assert out.size == (320, 240)


def test_make_grid_creates_jpg(tmp_path):
    imgs = [_Image.new("RGB", (400, 300), "green") for _ in range(9)]
    out = tmp_path / "grid.jpg"
    sg.make_grid(imgs, str(out))
    assert out.exists()
    result = _Image.open(str(out))
    expected_w = sg.GRID * sg.CELL_W + (sg.GRID - 1) * sg.GAP
    expected_h = sg.GRID * sg.CELL_H + (sg.GRID - 1) * sg.GAP
    assert result.size == (expected_w, expected_h)


def test_make_grid_handles_fewer_images(tmp_path):
    imgs = [_Image.new("RGB", (400, 300), "green") for _ in range(4)]
    out = tmp_path / "grid_partial.jpg"
    sg.make_grid(imgs, str(out))
    assert out.exists()