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
    assert isinstance(rs.API_URL, str)
    assert "go.whitetrafsa.com/api/models" in rs.API_URL
    # cam 详情接口，{model_id} 运行时替换
    assert "{model_id}" in rs.CAM_API_URL
    assert "api/front/v2/models" in rs.CAM_API_URL


def test_clean_topic_collapses_whitespace_and_truncates():
    assert rs.clean_topic("  日常\n客厅\t直播 ") == "日常 客厅 直播"
    assert rs.clean_topic("") == ""
    assert rs.clean_topic(None) == ""
    out = rs.clean_topic("字" * 40, max_len=10)
    assert out == "字" * 10 + "…"
    # 未超长不补省略号
    assert rs.clean_topic("日常客厅", max_len=10) == "日常客厅"


def test_fetch_cam_topic_parses_topic(monkeypatch):
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"cam": {"topic": "日常客厅"}}

    def fake_get(url, headers=None, proxies=None, timeout=None):
        captured.update(url=url, headers=headers, proxies=proxies)
        return FakeResp()

    monkeypatch.setattr(rs.requests, "get", fake_get)
    assert rs.fetch_cam_topic(132789258, "Iridessa-") == "日常客厅"
    assert captured["url"].endswith("/models/132789258/cam")
    assert captured["headers"] == rs.build_cam_headers("Iridessa-")
    assert captured["proxies"] == rs.PROXIES


def test_fetch_cam_topic_failure_returns_empty(monkeypatch):
    """cam 接口任何异常都降级为空主题，不影响发帖主流程。"""

    def fake_get(*a, **k):
        raise rs.requests.RequestException("boom")

    monkeypatch.setattr(rs.requests, "get", fake_get)
    assert rs.fetch_cam_topic(1) == ""


def test_fetch_cam_topic_missing_field_returns_empty(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"cam": {}}

    monkeypatch.setattr(rs.requests, "get", lambda *a, **k: FakeResp())
    assert rs.fetch_cam_topic(1) == ""


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
    monkeypatch.setattr(rs, "fetch_cam_topic", lambda model_id, username="": "主题")
    path, username, stream_url, topic = rs.generate_video()
    assert username == "cara-"
    assert stream_url == SAMPLE["models"][2]["stream"]["urls"]["480p"]
    assert topic == "主题"
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


def test_build_post_text_includes_topic(monkeypatch):
    """有 topic 时文案包含清洗后的主题，无占位符残留、链接仍指向主播页。"""
    import upload_grid_to_x as ux
    monkeypatch.setattr(ux, "_template_bags", {})
    import random
    rng = random.Random(42)
    # topic 模板占模板池 1/3，连取 12 次必然覆盖到
    texts = [ux.build_post_text("enya-", topic="日常客厅", rng=rng) for _ in range(12)]
    assert any("日常客厅" in t for t in texts)
    for t in texts:
        assert "{" not in t
    # username 只出现在链接里，模板池多数带链接
    assert any("https://zh.streams.modelapp.org/enya-" in t for t in texts)


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
