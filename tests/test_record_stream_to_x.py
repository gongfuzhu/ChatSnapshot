import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import record_stream_to_x as rs

_SAMPLE_PATH = os.path.join(os.path.dirname(__file__), "sample_recommended_response.json")
with open(_SAMPLE_PATH, encoding="utf-8") as f:
    SAMPLE = json.load(f)


def test_pick_top_streamer_prefers_public_max_viewers():
    model = rs.pick_top_streamer(SAMPLE["models"])
    # bella 观看数最高但是 private，应选 public 中最高的 cara
    assert model["username"] == "cara-"


def test_pick_top_streamer_fallback_when_no_public():
    models = [
        {"id": 1, "username": "a", "status": "private", "viewersCount": 100},
        {"id": 2, "username": "b", "status": "away", "viewersCount": 300},
    ]
    model = rs.pick_top_streamer(models)
    assert model["username"] == "b"


def test_pick_top_streamer_empty_returns_none():
    assert rs.pick_top_streamer([]) is None


def test_fetch_recommended_uses_headers_and_proxy(monkeypatch):
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return SAMPLE

    def fake_get(url, headers=None, proxies=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["proxies"] = proxies
        return FakeResp()

    monkeypatch.setattr(rs.requests, "get", fake_get)
    data = rs.fetch_recommended()
    assert data == SAMPLE
    assert captured["url"] == rs.API_URL
    assert captured["headers"] == rs.HEADERS
    assert captured["proxies"] == rs.PROXIES


def test_config_constants_exist():
    assert rs.RECORD_SECONDS == 15
    assert rs.POST_TEXT == "直播间：{username}"
    assert isinstance(rs.API_URL, str)
    assert "go.whitetrafsa.com/api/models" in rs.API_URL


def test_extract_stream_url_480p():
    model = SAMPLE["models"][0]
    assert rs.extract_stream_url(model) == model["stream"]["urls"]["480p"]


def test_extract_stream_url_fallback_to_stream_url():
    model = {"stream": {"url": "https://x/master.m3u8"}}
    assert rs.extract_stream_url(model) == "https://x/master.m3u8"


def test_extract_stream_url_missing():
    assert rs.extract_stream_url({}) == ""


def test_build_ffmpeg_args_contains_required_flags():
    args = rs.build_ffmpeg_args(
        "https://x/master.m3u8", "out.mp4", 15, proxy="http://127.0.0.1:7890"
    )
    joined = " ".join(args)
    assert args[0] == "ffmpeg"
    assert "-y" in args
    assert "https://x/master.m3u8" in args
    assert "-t" in args and "15" in args
    assert "-c:v" in args and "libx264" in args
    assert "-c:a" in args and "aac" in args
    assert "+faststart" in joined
    assert "-proxy" in args and "http://127.0.0.1:7890" in args
    assert args[-1] == "out.mp4"


def test_build_ffmpeg_args_no_proxy():
    args = rs.build_ffmpeg_args("https://x/master.m3u8", "out.mp4", 15, proxy="")
    assert "-proxy" not in args


def test_record_stream_missing_ffmpeg(monkeypatch):
    monkeypatch.setattr(rs.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError):
        rs.record_stream("https://x/master.m3u8", "out.mp4")