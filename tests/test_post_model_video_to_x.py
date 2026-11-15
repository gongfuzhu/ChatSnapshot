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


# ---------- 失效(410)处理：删除并换下一条 ----------

class _DelRes:
    def __init__(self, n):
        self.deleted_count = n


class FakeDelCol:
    def __init__(self):
        self.deleted = []
        self.updated = []

    def delete_one(self, filt):
        self.deleted.append(filt["video_id"])
        return _DelRes(1)

    def update_one(self, filt, update):
        self.updated.append(filt["video_id"])

    def create_index(self, *a, **k):
        return None


def _args(dry_run=False, limit=1, video_id=None):
    class A: pass
    a = A()
    a.dry_run = dry_run
    a.limit = limit
    a.video_id = video_id
    return a


def test_expired_link_error_class_exists():
    assert issubclass(pm.ExpiredLinkError, RuntimeError)


def test_process_one_deletes_expired_in_normal_mode(monkeypatch):
    col = FakeDelCol()

    def boom(doc, dry_run=False):
        raise pm.ExpiredLinkError("410 Gone")

    monkeypatch.setattr(pm, "publish_one", boom)
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    doc = {"video_id": 9001}
    result = pm._process_one(col, doc, _args(), allow_skip=True)
    assert result == "expired"
    assert col.deleted == [9001]          # 失效即删除
    assert col.updated == []              # 未回写发布标记


def test_process_one_dry_run_reports_expired_but_keeps_data(monkeypatch):
    col = FakeDelCol()

    def boom(doc, dry_run=False):
        raise pm.ExpiredLinkError("410 Gone")

    monkeypatch.setattr(pm, "publish_one", boom)
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    result = pm._process_one(col, {"video_id": 9002}, _args(dry_run=True), allow_skip=False)
    assert result == "expired"
    assert col.deleted == []              # dry-run 绝不删除


def test_process_one_published_marks_and_does_not_delete(monkeypatch):
    col = FakeDelCol()
    monkeypatch.setattr(pm, "publish_one", lambda doc, dry_run=False: True)
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    result = pm._process_one(col, {"video_id": 9003}, _args(), allow_skip=True)
    assert result == "published"
    assert col.deleted == []
    assert col.updated == [9003]          # 成功才回写 x_published


def test_process_one_other_failure_keeps_for_retry(monkeypatch):
    col = FakeDelCol()

    def boom(doc, dry_run=False):
        raise RuntimeError("网络抖动")

    monkeypatch.setattr(pm, "publish_one", boom)
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    result = pm._process_one(col, {"video_id": 9004}, _args(), allow_skip=True)
    assert result == "failed"
    assert col.deleted == []              # 非410错误不删除，留待重试
    assert col.updated == []


def test_download_video_raises_expired_on_410(monkeypatch):
    class _Proc:
        returncode = 8
        stderr = "[https] HTTP error 410 Gone\nError opening input: 4XX Client Error"

    monkeypatch.setattr(pm.shutil, "which", lambda x: "/usr/bin/ffmpeg")
    monkeypatch.setattr(pm.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    with pytest.raises(pm.ExpiredLinkError):
        pm.download_video("https://x/master.m3u8?md5=z", "/tmp/o.mp4")


# ---------- main 队列循环：失效删除后自动换下一条 ----------

class _QueueCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **k):
        return self

    def limit(self, n):
        return self

    def __iter__(self):
        return iter(self._docs)


class QueueCol(FakeDelCol):
    """模拟发布队列：前两次取队列返回含失效头部，删除后失效项消失。"""
    def __init__(self, docs):
        super().__init__()
        self._docs = docs

    def find(self, query, *a, **k):
        return _QueueCursor(list(self._docs))

    def delete_one(self, filt):
        vid = filt["video_id"]
        before = len(self._docs)
        self._docs = [d for d in self._docs if d["video_id"] != vid]
        self.deleted.append(vid)
        return _DelRes(before - len(self._docs))


def test_main_queue_skips_expired_and_publishes_next(monkeypatch):
    expired_doc = {"video_id": 7001, "video_url": "https://x/a.m3u8"}
    good_doc = {"video_id": 7002, "video_url": "https://x/b.mp4"}
    col = QueueCol([expired_doc, good_doc])

    class _Client:
        def __init__(self, *a, **k):
            self._col = col

        def __getitem__(self, db):
            outer = self

            class _DB:
                def __getitem__(self_inner, name):
                    return outer._col

            return _DB()

        def close(self):
            pass

    monkeypatch.setattr(pm, "MongoClient", _Client)
    monkeypatch.setattr(os.path, "exists", lambda p: False)

    calls = []

    def fake_publish(doc, dry_run=False):
        calls.append(doc["video_id"])
        if doc["video_id"] == 7001:
            raise pm.ExpiredLinkError("410 Gone")
        return True   # 第二条成功

    monkeypatch.setattr(pm, "publish_one", fake_publish)
    monkeypatch.setattr(sys, "argv", ["post_model_video_to_x.py", "--limit", "1"])
    pm.main()

    # 失效的被删除，有效的被发布；最终发够 1 条
    assert col.deleted == [7001]
    assert col.updated == [7002]          # 只有成功的那条回写发布标记
    assert calls == [7001, 7002]          # 先试失效项，删除后换了下一条
    assert all(d["video_id"] != 7001 for d in col._docs)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
