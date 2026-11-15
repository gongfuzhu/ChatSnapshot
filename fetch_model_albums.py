#!/usr/bin/env python3
"""抓取模特相册接口，仅保存 accessMode=free 的相册（含图片元数据）到 MongoDB。

数据源（同域，国内通常可直连）：
  相册  GET /api/front/v2/users/{user_id}/albums?limit=&offset=
        每个相册内嵌 photos（含全图 url / 缩略图 / 宽高比等）
  模特  GET /api/front/v2/users/{user_id}  （取 username，失败不阻断）

存储：
  新集合 clusterchat.model_albums，以 album_id 做唯一键幂等 upsert；
  只保存 accessMode == "free" 的相册，付费/粉丝俱乐部相册直接跳过、不入库。
  本脚本只存元数据（图片 URL 等），不下载任何图片文件。

用法：
  python fetch_model_albums.py --user-id 286872565
  python fetch_model_albums.py --user-id 111 --user-id 222
  python fetch_model_albums.py --user-id 111 --use-proxy
  python fetch_model_albums.py --user-id 111 --dry-run       # 只抓取打印，不写库
"""
import argparse
import sys
from datetime import datetime, timezone

from pymongo import ASCENDING, MongoClient

from fetch_model_videos import API_BASE, build_headers, get_json

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

try:
    from model_db_config import MONGO_URI, MONGO_DB
except ImportError:
    print("[错误] 缺少 model_db_config.py，请复制 model_db_config.example.py "
          "并填入真实 MongoDB 连接串。", file=sys.stderr)
    raise

ALBUM_COLLECTION = "model_albums"
PAGE_LIMIT = 100
FREE = "free"


def log(msg):
    print(f"[model-albums] {msg}", flush=True)


def fetch_albums_page(user_id, offset, proxies):
    """取单页相册列表，返回 (albums, total_items)。"""
    url = f"{API_BASE}/users/{user_id}/albums?limit={PAGE_LIMIT}&offset={offset}"
    data = get_json(url, build_headers(user_id), proxies)
    return data.get("albums") or [], data.get("totalItems", 0)


def fetch_username(user_id, proxies):
    """取模特 username 用于文档可读性；任何失败返回空串，不阻断主流程。"""
    try:
        data = get_json(f"{API_BASE}/users/{user_id}", build_headers(user_id), proxies)
        item = (data or {}).get("item") or {}
        return item.get("username") or ""
    except Exception as e:  # noqa: BLE001
        log(f"查询模特 username 失败(user_id={user_id})：{type(e).__name__}，将只存 user_id")
        return ""


def photo_to_doc(photo):
    """单张图片元数据（不含图片二进制）。"""
    return {
        "photo_id": photo.get("id"),
        "created_at": photo.get("createdAt", ""),
        "order": photo.get("order"),
        "status": photo.get("status", ""),
        "aspect_ratio": photo.get("aspectRatio"),
        "is_nude": photo.get("isNudeContent"),
        "is_hidden": photo.get("isHidden"),
        "primary_color": photo.get("primaryColor"),
        "url": photo.get("url", ""),              # 全图
        "url_thumb": photo.get("urlThumb", ""),
        "url_preview": photo.get("urlPreview", ""),
        "url_thumb_micro": photo.get("urlThumbMicro", ""),
    }


def album_to_document(album, user_id, username):
    """把 free 相册规整为待入库文档（内嵌图片元数据）。"""
    now = datetime.now(timezone.utc).isoformat()
    photos = [photo_to_doc(p) for p in (album.get("photos") or [])]
    return {
        "_id": album["id"],                    # 相册 id 做幂等主键
        "album_id": album["id"],
        "user_id": album.get("userId") or user_id,
        "model": {
            "id": album.get("userId") or user_id,
            "username": username,
        },
        "name": album.get("name", ""),
        "description": album.get("description", ""),
        "access_mode": album.get("accessMode", ""),
        "cost": album.get("cost"),
        "min_fan_club_tier": album.get("minFanClubTier"),
        "likes": album.get("likes"),
        "photos_count": album.get("photosCount"),
        "is_reserved": album.get("isReserved"),
        "preview": album.get("preview", ""),
        "preview_micro": album.get("previewMicro", ""),
        "created_at": album.get("createdAt", ""),
        "photos": photos,
        "fetched_at": now,
    }


def fetch_free_albums(user_id, proxies):
    """翻页抓取某模特的全部 free 相册（付费相册跳过）。返回 (docs, stats)。"""
    username = fetch_username(user_id, proxies)
    docs, offset = [], 0
    total_items = None
    seen_albums = 0
    free_count = paid_count = 0
    while True:
        albums, total = fetch_albums_page(user_id, offset, proxies)
        if total_items is None:
            total_items = total
        if not albums:
            break
        for album in albums:
            seen_albums += 1
            if album.get("accessMode") != FREE:
                paid_count += 1
                continue
            free_count += 1
            docs.append(album_to_document(album, user_id, username))
        offset += len(albums)
        if total_items is not None and offset >= total_items:
            break
    stats = {"total_albums": total_items if total_items is not None else seen_albums,
             "seen": seen_albums, "free": free_count, "skipped_paid": paid_count}
    return docs, stats


def save_to_mongo(docs):
    """按 _id(album_id) 幂等 upsert，返回 (插入数, 更新数)。"""
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=12000)
    inserted = updated = 0
    try:
        client.admin.command("ping")
        col = client[MONGO_DB][ALBUM_COLLECTION]
        col.create_index([("album_id", ASCENDING)], unique=True)
        col.create_index([("user_id", ASCENDING), ("created_at", ASCENDING)])
        col.create_index([("access_mode", ASCENDING)])
        for doc in docs:
            set_on_insert = {"first_seen": doc["fetched_at"]}
            res = col.update_one(
                {"_id": doc["_id"]},
                {"$set": doc, "$setOnInsert": set_on_insert},
                upsert=True,
            )
            if res.upserted_id is not None:
                inserted += 1
            else:
                updated += 1
    finally:
        client.close()
    return inserted, updated


def main():
    ap = argparse.ArgumentParser(description="抓取模特 free 相册元数据存入 model_albums（不下载图片）")
    ap.add_argument("--user-id", type=int, action="append", required=True,
                    help="模特 user_id，可重复传入多个")
    ap.add_argument("--use-proxy", action="store_true", help="走 mihomo 代理（默认直连）")
    ap.add_argument("--dry-run", action="store_true", help="只抓取打印，不写库")
    args = ap.parse_args()

    proxies = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"} if args.use_proxy else None

    grand_new = grand_upd = grand_photos = 0
    for uid in args.user_id:
        log(f"开始抓取模特 {uid} 的 free 相册…")
        try:
            docs, stats = fetch_free_albums(uid, proxies)
        except Exception as e:  # noqa: BLE001
            log(f"模特 {uid} 抓取失败：{type(e).__name__}: {str(e)[:150]}")
            continue
        n_photos = sum(len(d["photos"]) for d in docs)
        grand_photos += n_photos
        log(f"模特 {uid}: 相册总数 {stats['total_albums']}，free {stats['free']} 个"
            f"（跳过付费 {stats['skipped_paid']}），内嵌图片共 {n_photos} 张")
        if args.dry_run:
            for d in docs:
                log(f"[dry-run] 相册 {d['album_id']}「{d['name']}」图片 {len(d['photos'])} 张 "
                    f"首图={d['photos'][0]['url'][:70] if d['photos'] else '(无)'}")
            continue
        if not docs:
            log(f"模特 {uid} 无 free 相册，跳过写库")
            continue
        ins, upd = save_to_mongo(docs)
        grand_new += ins
        grand_upd += upd
        log(f"模特 {uid} 写库完成：新增 {ins} / 更新 {upd}")

    if args.dry_run:
        log(f"[dry-run] 结束，共解析 free 图片 {grand_photos} 张，未写库")
    else:
        log(f"全部结束：新增相册 {grand_new} / 更新相册 {grand_upd} / free 图片 {grand_photos} 张")


if __name__ == "__main__":
    main()
