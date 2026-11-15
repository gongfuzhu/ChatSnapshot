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

    def update_many(self, filt, update):
        ids = set(filt.get("video_id", {}).get("$in", []))
        n = 0
        for d in self.docs:
            if d.get("video_id") in ids:
                d.update(update.get("$set", {}))
                n += 1
        return _Res(n)

    def delete_one(self, filt):
        before = len(self.docs)
        self.docs = [d for d in self.docs if d.get("video_id") != filt["video_id"]]
        return type("R", (), {"deleted_count": before - len(self.docs)})()

    def aggregate(self, pipeline):
        """仅支持 select_next_model 用到的 match+group(min)+sort+limit 固定管道。"""
        rows = list(self.docs)
        for stage in pipeline:
            if "$match" in stage:
                rows = [d for d in rows if _cursor_match(d, stage["$match"])]
            elif "$group" in stage:
                key = stage["$group"]["_id"]
                field = stage["$group"]["oldest"]["$min"].lstrip("$")
                groups = {}
                for d in rows:
                    uname = d.get("model", {}).get("username", "")
                    groups.setdefault(uname, []).append(d.get(field) or "")
                rows = [{"_id": u, "oldest": min(v) if v else ""}
                        for u, v in groups.items()]
            elif "$sort" in stage:
                field = next(iter(stage["$sort"]))
                rows.sort(key=lambda r: r.get(field) or "")
            elif "$limit" in stage:
                rows = rows[: stage["$limit"]]
        return iter(rows)

    def create_index(self, *a, **k):
        return None


def _cursor_match(d, cond):
    for k, c in cond.items():
        val = _get_nested(d, k)
        if isinstance(c, dict) and "$ne" in c:
            if val == c["$ne"]:
                return False
        else:
            if val != c:
                return False
    return True


def _get_nested(d, dotted):
    """支持 'model.username' 点号路径取值。"""
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


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
        return _cursor_match(d, self.query)

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


# ---------- 失效(410)处理：先刷新链接重试，刷新不到才删除 ----------

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
        self.updated.append((filt["video_id"], update))

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


def test_process_one_deletes_expired_when_refresh_unavailable(monkeypatch):
    col = FakeDelCol()

    def boom(doc, dry_run=False):
        raise pm.ExpiredLinkError("410 Gone")

    monkeypatch.setattr(pm, "publish_one", boom)
    # 接口已不再返回该视频 -> 刷新失败 -> 走删除兜底
    monkeypatch.setattr(pm, "fetch_all_videos", lambda uid, proxies: [])
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    doc = {"video_id": 9001, "user_id": 123}
    result = pm._process_one(col, doc, _args(), allow_skip=True)
    assert result == "expired"
    assert col.deleted == [9001]          # 刷新不到才删除
    assert col.updated == []              # 未回写发布标记


def test_process_one_refresh_then_retry_publishes(monkeypatch):
    col = FakeDelCol()
    state = {"calls": 0}

    def flaky(doc, dry_run=False):
        state["calls"] += 1
        # 第一次旧链接 410；刷新后第二次成功
        if state["calls"] == 1:
            raise pm.ExpiredLinkError("410 Gone")
        return True

    monkeypatch.setattr(pm, "publish_one", flaky)
    new_url = "https://x/master.m3u8?md5=new&expires=999"
    monkeypatch.setattr(pm, "fetch_all_videos",
                        lambda uid, proxies: [{"id": 9001, "accessMode": "free",
                                               "videoUrl": new_url}])
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    doc = {"video_id": 9001, "user_id": 123,
           "video_url": "https://x/master.m3u8?md5=old&expires=1"}
    result = pm._process_one(col, doc, _args(), allow_skip=True)
    assert result == "published"
    assert state["calls"] == 2            # 失效后用新链接重试了一次
    assert col.deleted == []              # 刷新成功，不删除
    assert col.updated                     # 先刷新写库、再回写发布标记
    assert doc["video_url"] == new_url    # 内存 doc 已换成新链接
    # 第一条 update_one 是刷新链接写入
    refresh_update = col.updated[0][1]["$set"]
    assert refresh_update["video_url"] == new_url


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
    assert [vid for vid, _ in col.updated] == [9003]   # 成功才回写 x_published


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


# ---------- 按模特维度：选组、free 优先、多视频合并 ----------

def _vdoc(vid, username, created, access="paid", published=None):
    return {"video_id": vid, "publish_status": "approved", "x_published": published,
            "created_at": created, "access_mode": access,
            "video_url": f"https://x/{vid}.mp4",
            "model": {"username": username}, "title": "t"}


def test_sort_videos_group_free_first_and_caps_at_four():
    docs = [
        _vdoc(1, "m", "2026-01-01", "paid"),
        _vdoc(2, "m", "2026-01-02", "free"),
        _vdoc(3, "m", "2026-01-03", "paid"),
        _vdoc(4, "m", "2026-01-04", "free"),
        _vdoc(5, "m", "2026-01-05", "paid"),
        _vdoc(6, "m", "2026-01-06", "free"),
    ]
    group = pm.sort_videos_for_group(docs, group_size=4)
    assert len(group) == 4                      # 最多 4 条
    # free 全部排前且组内按 created_at FIFO
    assert [d["video_id"] for d in group] == [2, 4, 6, 1]
    assert all(d["access_mode"] == "free" for d in group[:3])


def test_sort_videos_group_fewer_than_four_returns_all():
    docs = [_vdoc(1, "m", "2026-01-01"), _vdoc(2, "m", "2026-01-02", "free")]
    group = pm.sort_videos_for_group(docs, group_size=4)
    assert len(group) == 2                      # 不足 4 条有几条发几条


def test_build_group_text_single_model_one_link():
    docs = [_vdoc(1, "JUJU8030", "2026-01-01"),
            _vdoc(2, "JUJU8030", "2026-01-02")]
    text = pm.build_group_text(docs)
    assert "JUJU8030" in text
    assert "精选 2 个视频" in text
    assert text.count("https://zh.streams.modelapp.org/JUJU8030") == 1  # 只写一次链接


def test_select_next_model_picks_oldest_pending_model():
    col = FakeCol([
        _vdoc(1, "B", "2026-03-01"),
        _vdoc(2, "A", "2026-01-01"),   # A 的最早待发更早
        _vdoc(3, "A", "2026-02-01"),
    ])
    assert pm.select_next_model(col) == "A"


def test_select_next_model_none_when_empty():
    col = FakeCol([_vdoc(1, "A", "2026-01-01", published=True)])
    assert pm.select_next_model(col) is None


def test_fetch_model_group_only_that_model_and_free_first():
    col = FakeCol([
        _vdoc(1, "A", "2026-01-01", "paid"),
        _vdoc(2, "A", "2026-01-02", "free"),
        _vdoc(3, "B", "2026-01-03", "free"),   # 别的模特，不应入组
    ])
    group = pm.fetch_model_group(col, "A", 4)
    assert [d["video_id"] for d in group] == [2, 1]   # 仅 A，free 优先


def test_publish_group_skips_expired_and_merges_rest(monkeypatch):
    col = FakeDelCol()
    docs = [_vdoc(1, "m", "2026-01-01"), _vdoc(2, "m", "2026-01-02"),
            _vdoc(3, "m", "2026-01-03")]

    def fake_download(col_, doc, dry_run=False):
        if doc["video_id"] == 2:
            return None          # 模拟该条 410 失效被删跳过
        return f"modelx_{doc['video_id']}.mp4"

    uploaded = {}

    def fake_upload(page, paths, text):
        uploaded["paths"] = paths
        uploaded["text"] = text

    monkeypatch.setattr(pm, "_download_one", fake_download)
    monkeypatch.setattr(pm, "ensure_browser", lambda port: (None, False))
    monkeypatch.setattr(pm.sync_playwright, "__call__", lambda: None, raising=False)

    class _PW:
        def __enter__(self_inner): return self_inner
        def __exit__(self_inner, *a): return False
        class chromium:
            @staticmethod
            def connect_over_cdp(url):
                class B:
                    contexts = [type("C", (), {"new_page": staticmethod(lambda: object())})()]
                return B()

    monkeypatch.setattr(pm, "sync_playwright", lambda: _PW())
    monkeypatch.setattr(pm, "upload_media_to_x", fake_upload)
    monkeypatch.setattr(os.path, "exists", lambda p: False)

    res = pm.publish_group(col, docs, dry_run=False)
    assert res["posted"] is True
    assert res["ok_vids"] == [1, 3]
    assert res["expired_vids"] == [2]
    # 两个有效视频作为同一条推文的多个附件
    assert len(uploaded["paths"]) == 2


def test_main_publishes_one_multivideo_post_and_marks_all(monkeypatch):
    docs = [
        _vdoc(1, "A", "2026-01-01", "free"),
        _vdoc(2, "A", "2026-01-02", "paid"),
        _vdoc(3, "A", "2026-01-03", "free"),
        _vdoc(4, "B", "2026-02-01", "free"),
    ]
    col = FakeCol(docs)

    class _Client:
        def __init__(self_inner, *a, **k):
            self_inner._col = col

        def __getitem__(self_inner, db):
            outer = self_inner

            class _DB:
                def __getitem__(self_s, name):
                    return outer._col

            return _DB()

        def close(self_inner):
            pass

    monkeypatch.setattr(pm, "MongoClient", _Client)

    seen_groups = []

    def fake_publish_group(col_, group, dry_run=False):
        seen_groups.append([d["video_id"] for d in group])
        return {"posted": True, "paths": [], "ok_vids": [d["video_id"] for d in group],
                "expired_vids": [], "failed_vids": []}

    monkeypatch.setattr(pm, "publish_group", fake_publish_group)
    monkeypatch.setattr(sys, "argv", ["post_model_video_to_x.py", "--limit", "1"])
    pm.main()

    # 只处理最早模特 A 的一组（最多4条，这里 A 有3条全取），B 不本轮处理
    assert seen_groups == [[1, 3, 2]]          # free 优先(FIFO)，A 的 3 条
    marked = [d["video_id"] for d in col.docs if d.get("x_published") is True]
    assert sorted(marked) == [1, 2, 3]         # 组内全部批量回写
    assert not any(d.get("x_published") for d in col.docs if d["video_id"] == 4)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
