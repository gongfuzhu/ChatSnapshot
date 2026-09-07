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
    # 文案与图片版一致（三语 + 直播链接），{username} 占位符保留
    assert rs.POST_TEXT == (
        "正在直播\n Live streaming now. \n ただいま配信中です。 \n"
        "https://zh.streams.modelapp.org/{username}"
    )
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
    assert "-http_proxy" in args and "http://127.0.0.1:7890" in args
    assert args[-1] == "out.mp4"


def test_build_ffmpeg_args_no_proxy():
    args = rs.build_ffmpeg_args("https://x/master.m3u8", "out.mp4", 15, proxy="")
    assert "-http_proxy" not in args


def test_record_stream_missing_ffmpeg(monkeypatch):
    monkeypatch.setattr(rs.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError):
        rs.record_stream("https://x/master.m3u8", "out.mp4")


def test_generate_video_picks_records_and_returns(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rs, "fetch_recommended", lambda: SAMPLE)
    monkeypatch.setattr(rs, "record_stream", lambda url, path, seconds: None)
    path, username, stream_url = rs.generate_video()
    assert username == "cara-"
    assert stream_url == SAMPLE["models"][2]["stream"]["urls"]["480p"]
    assert path.endswith(".mp4")


def test_generate_video_no_models_exits(monkeypatch):
    monkeypatch.setattr(rs, "fetch_recommended", lambda: {"models": []})
    with pytest.raises(SystemExit):
        rs.generate_video()


def test_generate_video_no_stream_url_exits(monkeypatch):
    data = {"models": [{"id": 1, "username": "a", "status": "public", "viewersCount": 1}]}
    monkeypatch.setattr(rs, "fetch_recommended", lambda: data)
    with pytest.raises(SystemExit):
        rs.generate_video()


def test_post_text_render():
    expected = (
        "正在直播\n Live streaming now. \n ただいま配信中です。 \n"
        "https://zh.streams.modelapp.org/enya-"
    )
    assert rs.render_post_text(rs.POST_TEXT, "enya-") == expected


def test_main_cleans_up_when_record_fails(monkeypatch, tmp_path):
    """录制失败（generate_video 抛出）时，main 的 finally 应清理残留视频文件。"""
    monkeypatch.chdir(tmp_path)
    # 固定 datetime.now() 返回值，使 video_path 可预测
    fixed_ts = rs.datetime(2026, 1, 2, 3, 4, 5)

    class FakeDatetime:
        @classmethod
        def now(cls):
            return fixed_ts

        def __getattr__(self, name):
            return getattr(rs.datetime, name)

    monkeypatch.setattr(rs, "datetime", FakeDatetime)
    expected_name = "stream_20260102_030405.mp4"
    expected_path = tmp_path / expected_name

    def fake_generate_video(out_path=None):
        # 模拟 generate_video 录制到一半创建了文件然后失败
        with open(expected_path, "wb") as f:
            f.write(b"x" * 100)
        raise RuntimeError("boom")

    monkeypatch.setattr(rs, "generate_video", fake_generate_video)

    with pytest.raises(SystemExit):
        rs.main()

    assert not expected_path.exists(), "录制失败后临时视频应被清理"
