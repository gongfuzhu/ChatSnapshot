"""fetch_model_albums 单元测试：free 过滤、文档转换、翻页、幂等 upsert。"""

import fetch_model_albums as fa


def _album(album_id, mode, n_photos=2, user_id=286872565):
    return {
        "id": album_id, "userId": user_id, "name": "Public", "description": "",
        "accessMode": mode, "cost": 0, "minFanClubTier": None, "likes": 5,
        "photosCount": n_photos, "isReserved": True,
        "preview": "http://x/preview", "previewMicro": "http://x/micro",
        "createdAt": "2026-10-21T08:30:30Z",
        "photos": [
            {"id": album_id * 100 + i, "createdAt": "2026-10-30T10:50:16Z",
             "order": i, "status": "approved", "aspectRatio": "0.56",
             "isNudeContent": False, "isHidden": False, "primaryColor": "#abc",
             "url": f"http://x/{album_id}-{i}-full",
             "urlThumb": f"http://x/{album_id}-{i}-thumb",
             "urlPreview": f"http://x/{album_id}-{i}-preview",
             "urlThumbMicro": f"http://x/{album_id}-{i}-micro"}
            for i in range(n_photos)
        ],
    }


def test_photo_to_doc_maps_fields():
    p = _album(100, "free")["photos"][0]
    d = fa.photo_to_doc(p)
    assert d["photo_id"] == p["id"]
    assert d["url"].endswith("-full")
    assert d["url_thumb"].endswith("-thumb")
    assert d["aspect_ratio"] == "0.56"
    assert d["is_nude"] is False


def test_album_to_document_keeps_free_metadata():
    album = _album(18503683, "free", n_photos=6)
    doc = fa.album_to_document(album, 286872565, "Emacccc")
    assert doc["_id"] == 18503683
    assert doc["album_id"] == 18503683
    assert doc["access_mode"] == "free"
    assert doc["model"]["username"] == "Emacccc"
    assert doc["model"]["id"] == 286872565
    assert len(doc["photos"]) == 6
    assert doc["photos_count"] == 6
    assert "fetched_at" in doc and "first_seen" not in doc  # first_seen 由 setOnInsert 写


def test_fetch_free_albums_filters_paid(monkeypatch):
    # 单页返回 1 free + 2 付费
    payload = {
        "totalItems": 3,
        "albums": [_album(1, "free"), _album(2, "paid"), _album(3, "fanClub")],
    }
    monkeypatch.setattr(fa, "get_json", lambda url, h, p: payload)
    monkeypatch.setattr(fa, "fetch_username", lambda uid, p: "Emacccc")
    docs, stats = fa.fetch_free_albums(286872565, None)
    assert stats["free"] == 1
    assert stats["skipped_paid"] == 2
    assert stats["total_albums"] == 3
    assert [d["album_id"] for d in docs] == [1]      # 只有 free 入库候选
    assert all(d["access_mode"] == "free" for d in docs)


def test_fetch_free_albums_paginates(monkeypatch):
    pages = [
        {"totalItems": 3, "albums": [_album(1, "free"), _album(2, "free")]},
        {"totalItems": 3, "albums": [_album(3, "free")]},
    ]
    seq = {"i": 0}

    def fake_get_json(url, h, p):
        out = pages[seq["i"]]
        seq["i"] += 1
        return out

    monkeypatch.setattr(fa, "get_json", fake_get_json)
    monkeypatch.setattr(fa, "fetch_username", lambda uid, p: "U")
    docs, stats = fa.fetch_free_albums(1, None)
    assert seq["i"] == 2                  # 翻了两页
    assert len(docs) == 3
    assert stats["free"] == 3


def test_fetch_free_albums_username_failure_non_fatal(monkeypatch):
    def fake_get_json(url, h, p):
        # users/{uid} 详情（取 username）失败；albums 接口正常
        if "/albums" in url:
            return {"totalItems": 1, "albums": [_album(1, "free")]}
        raise RuntimeError("接口异常")
    monkeypatch.setattr(fa, "get_json", fake_get_json)
    docs, stats = fa.fetch_free_albums(1, None)
    assert len(docs) == 1                 # username 失败不阻断
    assert docs[0]["model"]["username"] == ""


class _FakeResult:
    def __init__(self, upserted):
        self.upserted_id = 1 if upserted else None


class _FakeAlbumCol:
    def __init__(self):
        self.store = {}
        self.indexes = []

    def create_index(self, keys, unique=False, **k):
        self.indexes.append((keys, unique))

    def update_one(self, filt, update, upsert=False):
        _id = filt["_id"]
        if _id in self.store:
            self.store[_id].update(update.get("$set", {}))
            return _FakeResult(False)
        doc = dict(update["$set"])
        doc.update(update.get("$setOnInsert", {}))
        self.store[_id] = doc
        return _FakeResult(True)


class _FakeMongo:
    def __init__(self, uri, **k):
        self.col = _FakeAlbumCol()
        class _Admin:
            def command(self_inner, name):
                return {"ok": 1}
        self.admin = _Admin()

    def __getitem__(self, name):
        class _Db:
            def __getitem__(self_inner, cname):
                return self.col
        return _Db()

    def close(self):
        pass


def test_save_to_mongo_idempotent(monkeypatch):
    fake = _FakeMongo("uri")
    monkeypatch.setattr(fa, "MongoClient", lambda *a, **k: fake)
    doc = fa.album_to_document(_album(18503683, "free"), 286872565, "Emacccc")
    ins1, upd1 = fa.save_to_mongo([doc])
    assert (ins1, upd1) == (1, 0)
    # 第二次：同 _id，变为更新，且 first_seen 只在首插写入不被覆盖
    ins2, upd2 = fa.save_to_mongo([doc])
    assert (ins2, upd2) == (0, 1)
    assert len(fake.col.store) == 1                    # 无重复文档
    assert "first_seen" in fake.col.store[18503683]
    # 唯一索引已建在 album_id
    assert any(keys == [("album_id", 1)] and uniq for keys, uniq in fake.col.indexes)
