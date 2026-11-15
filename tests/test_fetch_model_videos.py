import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymongo
import fetch_model_videos as fm


# ---------- to_document 默认标注 ----------

def test_to_document_defaults_publish_status_approved():
    video = {"id": 111, "userId": 222, "accessMode": "free",
             "videoUrl": "https://x/a.m3u8", "title": "t"}
    model = {"id": 222, "username": "m"}
    doc = fm.to_document(video, model)
    assert doc["publish_status"] == "approved"
    assert doc["video_id"] == 111


# ---------- save_to_mongo 幂等与标注保护（fake MongoClient）----------

class _Res:
    def __init__(self, upserted_id):
        self.upserted_id = upserted_id
        self.matched_count = 1 if upserted_id is None else 0
        self.modified_count = self.matched_count


class FakeCol:
    """模拟同一集合的持久状态（跨多次 save_to_mongo 调用共享）。"""
    def __init__(self):
        self.store = {}
        self.calls = []

    def create_index(self, *a, **k):
        return None

    def update_one(self, filt, update, upsert=False):
        _id = filt["_id"]
        self.calls.append(update)
        if _id not in self.store:
            doc = {}
            doc.update(update.get("$setOnInsert", {}))
            doc.update(update.get("$set", {}))
            self.store[_id] = doc
            return _Res(_id)
        self.store[_id].update(update.get("$set", {}))
        return _Res(None)


class FakeAdmin:
    def command(self, *a, **k):
        return {"ok": 1}


@pytest.fixture
def shared_col(monkeypatch):
    col = FakeCol()

    class FakeClient:
        def __init__(self, *a, **k):
            self._col = col

        @property
        def admin(self):
            return FakeAdmin()

        def __getitem__(self, db):
            outer = self

            class _DB:
                def __getitem__(self_inner, name):
                    return outer._col

            return _DB()

        def close(self):
            pass

    monkeypatch.setattr(pymongo, "MongoClient", FakeClient)
    return col


def _doc(vid, status):
    d = fm.to_document(
        {"id": vid, "userId": 1, "accessMode": "paid",
         "trailerUrl": "https://x/t.mp4", "title": "x"},
        {"id": 1, "username": "m"},
    )
    d["publish_status"] = status
    return d


def test_save_new_video_inserts_approved_then_idempotent(shared_col):
    inserted, updated = fm.save_to_mongo([_doc(1001, "approved")])
    assert inserted == 1 and updated == 0
    assert shared_col.store[1001]["publish_status"] == "approved"
    # 再抓同一视频：转为更新、不重复插入
    inserted2, updated2 = fm.save_to_mongo([_doc(1001, "approved")])
    assert inserted2 == 0 and updated2 == 1


def test_save_does_not_overwrite_existing_manual_status(shared_col):
    # 首次插入（默认 approved）
    fm.save_to_mongo([_doc(2002, "approved")])
    assert shared_col.store[2002]["publish_status"] == "approved"

    # 模拟人工在页面改成 rejected
    shared_col.store[2002]["publish_status"] = "rejected"

    # 再次抓取同一视频
    fm.save_to_mongo([_doc(2002, "approved")])
    # 人工标注必须保留
    assert shared_col.store[2002]["publish_status"] == "rejected"
    # $set 里绝不允许出现 publish_status（只能走 setOnInsert）
    for upd in shared_col.calls:
        assert "publish_status" not in upd.get("$set", {})


# ---------- main 集成：视频 + free 相册协同 ----------

def _patch_common(monkeypatch, album_docs=None, album_raise=False, video_docs=None):
    monkeypatch.setattr(fm, "fetch_model",
                        lambda uid, p: {"id": uid, "username": "Emacccc"})
    monkeypatch.setattr(fm, "collect_for_model",
                        lambda model, p: (video_docs or [_doc(9001, "approved")], None))
    monkeypatch.setattr(fm, "save_to_mongo", lambda docs: (len(docs), 0))

    import fetch_model_albums as fa
    captured = {"save_calls": 0, "fetch_uids": []}

    def fake_fetch(uid, proxies, username=None):
        captured["fetch_uids"].append((uid, username))
        if album_raise:
            raise RuntimeError("albums 接口异常")
        return (album_docs or [], {"total_albums": 1, "seen": 1,
                                   "free": len(album_docs or []), "skipped_paid": 0})

    def fake_save(docs):
        captured["save_calls"] += 1
        return (len(docs), 0)

    monkeypatch.setattr(fa, "fetch_free_albums", fake_fetch)
    monkeypatch.setattr(fa, "save_albums_to_mongo", fake_save)
    monkeypatch.setattr(fa, "save_to_mongo", fake_save)  # 别名指向同函数时一并 mock
    return captured


def test_main_fetches_albums_alongside_videos(monkeypatch):
    album_doc = {"_id": 777, "album_id": 777, "photos": [{"photo_id": 1}, {"photo_id": 2}]}
    cap = _patch_common(monkeypatch, album_docs=[album_doc])
    monkeypatch.setattr(sys, "argv", ["x", "--user-id", "286872565"])
    rc = fm.main()
    assert rc == 0
    assert cap["fetch_uids"] == [(286872565, "Emacccc")]   # 复用了视频流程的 username
    assert cap["save_calls"] == 1                            # 相册被幂等写库一次


def test_main_skip_albums_flag_disables_album_pipeline(monkeypatch):
    cap = _patch_common(monkeypatch, album_docs=[{"_id": 1}])
    monkeypatch.setattr(sys, "argv", ["x", "--user-id", "1", "--skip-albums"])
    rc = fm.main()
    assert rc == 0
    assert cap["fetch_uids"] == []                           # 完全没碰相册接口
    assert cap["save_calls"] == 0


def test_main_album_failure_does_not_break_videos(monkeypatch):
    # 相册接口抛错时，视频仍正常处理、main 正常返回 0
    cap = _patch_common(monkeypatch, album_raise=True)
    monkeypatch.setattr(sys, "argv", ["x", "--user-id", "1"])
    rc = fm.main()
    assert rc == 0
    assert cap["fetch_uids"] == [(1, "Emacccc")]
    assert cap["save_calls"] == 0                            # 相册失败未写库，但未中断流程


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
