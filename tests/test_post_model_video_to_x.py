import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import post_model_video_to_x as pm


# ---------- 纯函数 ----------

def test_is_hls_by_suffix_and_ignores_query():
    assert pm.is_hls("https://x/a/master.m3u8?md5=abc&expires=1")
    assert pm.is_hls("https://x/a/master.M3U8")
    assert not pm.is_hls("https://x/trailer/720p/trailer.mp4")
    assert not pm.is_hls("https://x/no-ext")
    assert not pm.is_hls("")


def test_build_download_args_hls_uses_proxy_and_caps_duration():
    args = pm.build_download_args("https://x/a.m3u8", "/tmp/o.mp4", max_seconds=140)
    assert "ffmpeg" in args[0]
    # m3u8 必须走代理 + 重连参数
    assert pm.PROXY in args
    assert "-reconnect_streamed" in args
    # 截断 140 秒、流拷贝
    assert args[args.index("-t") + 1] == "140"
    assert "-c" in args and args[args.index("-c") + 1] == "copy"
    assert args[-1] == "/tmp/o.mp4"


def test_build_download_args_mp4_direct_no_proxy():
    url = "https://x/trailer.mp4"
    args = pm.build_download_args(url, "/tmp/o.mp4")
    # mp4 直连，不带 http_proxy
    assert pm.PROXY not in args
    assert url in args
    assert args[args.index("-t") + 1] == str(pm.MAX_VIDEO_SECONDS)


# ---------- 内存 Fake Collection ----------

class _Res:
    def __init__(self, modified):
        self.modified_count = modified
        self.matched_count = modified
        self.upserted_id = None


class FakeCol:
    """最小内存集合，支持 find().sort().limit() 与 update_one。"""
    def __init__(self, docs):
        self.docs = docs
        self.calls = []

    def find(self, query):
        return _Cursor(self, dict(query))

    def update_one(self, filt, update):
        self.calls.append((filt, update))
        for d in self.docs:
            if d.get("video_id") == filt["video_id"]:
                d.update(update.get("$set", {}))
                return _Res(1)
        return _Res(0)

    def create_index(self, *a, **k):
        return None


class _Cursor:
    def __init__(self, col, query):
        self.col = col
        self.query = query
        self._sort = None
        self._limit = None

    def sort(self, field, direction):
        self._sort = field
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _match(self, d):
        for k, cond in self.query.items():
            if isinstance(cond, dict) and "$ne" in cond:
                if d.get(k) == cond["$ne"]:
                    return False
            else:
                if d.get(k) != cond:
                    return False
        return True

    def __iter__(self):
        rows = [d for d in self.col.docs if self._match(d)]
        if self._sort:
            rows.sort(key=lambda d: d.get(self._sort) or "")
        if self._limit:
            rows = rows[: self._limit]
        return iter(rows)


def _doc(vid, status="approved", published=None, created=""):
    return {"video_id": vid, "publish_status": status,
            "x_published": published, "created_at": created,
            "video_url": f"https://x/{vid}.mp4",
            "model": {"username": "m"}, "title": "t"}


def test_find_pending_excludes_published_and_non_approved():
    col = FakeCol([
        _doc(1, "approved", None, "2026-01-01"),      # 候选
        _doc(2, "approved", True, "2026-01-02"),      # 已发布，排除
        _doc(3, "pending", None, "2026-01-03"),       # 未审核，排除
        _doc(4, "rejected", None, "2026-01-04"),      # 不发布，排除
        _doc(5, "approved", False, "2026-01-05"),     # 显式 False，候选
    ])
    out = pm.find_pending(col, limit=10)
    assert [d["video_id"] for d in out] == [1, 5]


def test_find_pending_fifo_order_and_limit():
    col = FakeCol([
        _doc(9, "approved", None, "2026-03-01"),
        _doc(7, "approved", None, "2026-01-01"),
        _doc(8, "approved", None, "2026-02-01"),
    ])
    out = pm.find_pending(col, limit=2)
    assert [d["video_id"] for d in out] == [7, 8]


def test_find_pending_specific_video_id():
    col = FakeCol([_doc(1), _doc(2)])
    out = pm.find_pending(col, video_id=2)
    assert [d["video_id"] for d in out] == [2]


def test_mark_published_sets_flag_and_timestamp():
    col = FakeCol([_doc(1)])
    res = pm.mark_published(col, 1)
    assert res.modified_count == 1
    d = col.docs[0]
    assert d["x_published"] is True
    assert isinstance(d["x_published_at"], str) and "T" in d["x_published_at"]
    # 标记后不再被候选查询选中
    assert pm.find_pending(col) == []


def test_build_text_for_uses_model_title_and_live_url():
    doc = {"model": {"username": "JUJU8030"}, "title": "恶魔来榨精了"}
    text = pm.build_text_for(doc)
    assert "JUJU8030" in text
    assert "恶魔来榨精了" in text
    # 直播链接按 username 拼出并出现在文案中
    assert "https://zh.streams.modelapp.org/JUJU8030" in text


def test_build_text_for_empty_title_does_not_raise():
    doc = {"model": {"username": "abc"}, "title": ""}
    text = pm.build_text_for(doc)
    assert "abc" in text
    assert "https://zh.streams.modelapp.org/abc" in text


def test_build_text_for_no_username_has_no_link():
    doc = {"model": {}, "title": "仅标题"}
    text = pm.build_text_for(doc)
    assert "仅标题" in text
    assert "https://" not in text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
