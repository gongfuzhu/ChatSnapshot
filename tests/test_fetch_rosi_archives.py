import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymongo
import fetch_rosi_archives as fr


# ---------- to_document ----------

def _entry(**over):
    base = {
        "id": "46908", "typeid": "1", "channel": "3", "title": "NO.5458",
        "writer": "rosi", "litpic": "http://x/a.rosi", "pubdate": "1794623153",
        "money": "0", "description": "", "mid": "1", "click": "711", "stow": "33",
        "img_width": "1400", "img_height": "934", "shorttitle": "5458-x",
        "source": "86", "cdn": "http://rs/jpg/", "face": "http://x/f.jpg",
        "sex": "女", "minlitpic": "http://x/d.png", "introduce": "",
        "needmoney": "5", "m_isbuy": 0,
    }
    base.update(over)
    return base


def test_to_document_converts_types_and_maps_album():
    doc = fr.to_document(_entry())
    assert doc["_id"] == 46908 and doc["entry_id"] == 46908
    assert doc["typeid"] == 1 and doc["type_name"] == "album"
    assert doc["click"] == 711 and doc["stow"] == 33
    assert doc["needmoney"] == 5 and doc["m_isbuy"] == 0
    assert doc["title"] == "NO.5458" and doc["shorttitle"] == "5458-x"
    # pubdate 时间戳转 ISO
    assert doc["pubdate_ts"] == 1794623153
    expected_iso = datetime.fromtimestamp(1794623153, tz=timezone.utc).isoformat()
    assert doc["pubdate_iso"] == expected_iso
    assert "fetched_at" in doc and "T" in doc["fetched_at"]


def test_to_document_maps_video_typeid():
    doc = fr.to_document(_entry(typeid="10", title="VIDEO.NO.675"))
    assert doc["typeid"] == 10 and doc["type_name"] == "video"


def test_to_document_handles_unknown_type_and_empty_pubdate():
    doc = fr.to_document(_entry(typeid="99", pubdate=""))
    assert doc["type_name"] == "unknown"
    assert doc["pubdate_ts"] is None and doc["pubdate_iso"] is None


def test_to_document_bad_number_uses_default():
    # 计数字段(click/stow)坏值回退为 0；普通字段(img_width)坏值回退 None
    doc = fr.to_document(_entry(click="abc", stow="", img_width="xx"))
    assert doc["click"] == 0
    assert doc["stow"] == 0
    assert doc["img_width"] is None


# ---------- build_payload ----------

def test_build_payload_shape():
    p = fr.build_payload(3, bid=1, sort_type="t")
    assert p == {"controller": "archives", "action": "Sort", "m_mid": "",
                 "page": 3, "type": "t", "bid": 1}


# ---------- is_full_page_stored ----------

def test_full_page_stored_detection():
    entries = [_entry(id="1"), _entry(id="2")]
    assert fr.is_full_page_stored(entries, {1, 2, 99}) is True
    assert fr.is_full_page_stored(entries, {1}) is False
    assert fr.is_full_page_stored([], set()) is True


# ---------- fetch_page 解析（fake session）----------

class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _Resp(self.payload)


def test_fetch_page_parses_and_sends_cookie(monkeypatch):
    monkeypatch.setattr(fr, "MAX_RETRIES", 1)
    payload = {"error": {"state": 0}, "entries_s": [_entry()], "total_page": 434}
    sess = FakeSession(payload)
    out = fr.fetch_page(1, session=sess)
    assert out["total_page"] == 434
    # Cookie 头携带 PHPSESSID
    assert "PHPSESSID=" in sess.calls[0]["headers"]["Cookie"]
    assert sess.calls[0]["json"]["page"] == 1


def test_fetch_page_raises_on_api_error(monkeypatch):
    monkeypatch.setattr(fr, "MAX_RETRIES", 1)
    sess = FakeSession({"error": {"state": 1, "msg": "bad"}, "entries_s": []})
    with pytest.raises(RuntimeError):
        fr.fetch_page(1, session=sess)


# ---------- save_to_mongo 幂等（fake MongoClient）----------

class _Res:
    def __init__(self, upserted_id):
        self.upserted_id = upserted_id


class FakeCol:
    def __init__(self):
        self.store = {}
        self.indexes = []
        self.calls = []

    def create_index(self, keys, **kw):
        self.indexes.append(keys)

    def distinct(self, field):
        if field == "_id":
            return list(self.store.keys())
        return []

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
def fake_mongo(monkeypatch):
    col = FakeCol()

    class Client:
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

    monkeypatch.setattr(pymongo, "MongoClient", Client)
    monkeypatch.setattr(fr, "MongoClient", Client)
    return col


def test_save_to_mongo_idempotent_and_preserves_first_seen(fake_mongo):
    docs = [fr.to_document(_entry(id="100")), fr.to_document(_entry(id="101"))]
    ins, upd = fr.save_to_mongo(docs)
    assert ins == 2 and upd == 0
    assert 100 in fake_mongo.store and 101 in fake_mongo.store
    first_seen_1 = fake_mongo.store[100]["first_seen"]

    # 再次保存（模拟重抓）：应转为更新，first_seen 不变
    docs2 = [fr.to_document(_entry(id="100", click="999"))]
    ins2, upd2 = fr.save_to_mongo(docs2)
    assert ins2 == 0 and upd2 == 1
    assert fake_mongo.store[100]["first_seen"] == first_seen_1  # 首次入库时间保留
    assert fake_mongo.store[100]["click"] == 999               # 动态字段已更新
    # $set 中不含 first_seen（只走 setOnInsert）
    for upd_doc in fake_mongo.calls:
        assert "first_seen" not in upd_doc.get("$set", {})


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
