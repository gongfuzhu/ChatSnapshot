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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
