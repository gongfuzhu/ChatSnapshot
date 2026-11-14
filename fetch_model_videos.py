#!/usr/bin/env python3
"""抓取模特视频接口，按 accessMode 选择视频链接，连同模特信息存入 MongoDB Atlas。

数据源：
  视频  GET /api/front/v2/users/{user_id}/videos
  模特  GET /api/front/v2/users/{user_id}        （item 为模特资料）
选链规则（用户指定）：
  accessMode == "free" -> videoUrl（完整视频）
  其他（付费/粉丝俱乐部等）-> trailerUrl（预告片）
存储：
  库/集合见 model_db_config.py（默认 clusterchat.model_videos），
  以 video_id 做唯一键 upsert，重复运行只更新不产生重复文档。

用法：
  python fetch_model_videos.py                      # 默认 user 289118173
  python fetch_model_videos.py --user-id 289118173
  python fetch_model_videos.py --user-id 111 --user-id 222
  python fetch_model_videos.py --use-proxy          # 走 mihomo 代理
  python fetch_model_videos.py --dry-run            # 只抓取打印，不写库
"""
import argparse
import sys
import time
from datetime import datetime, timezone

import requests

# ---------- 配置 ----------
API_BASE = "https://zh.streams.modelapp.org/api/front/v2"
FRONT_VERSION = "12.0.94"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
DEFAULT_USER_ID = 289118173
PAGE_LIMIT = 100
MAX_RETRIES = 3

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

try:
    from model_db_config import MONGO_URI, MONGO_DB, MONGO_COLLECTION
except ImportError:
    print("[错误] 缺少 model_db_config.py，请复制 model_db_config.example.py "
          "并填入真实 MongoDB 连接串。", file=sys.stderr)
    raise


def log(msg):
    print(f"[model-videos] {msg}", flush=True)


def build_headers(user_id):
    return {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "content-type": "application/json",
        "front-version": FRONT_VERSION,
        "user-agent": USER_AGENT,
        "referer": f"https://zh.streams.modelapp.org/{user_id}",
    }


def get_json(url, headers, proxies, retries=MAX_RETRIES):
    """带指数退避重试的 GET JSON。"""
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=headers, proxies=proxies, timeout=25)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            if i < retries - 1:
                wait = 3 * (i + 1)
                log(f"请求失败（{type(e).__name__}: {str(e)[:80]}），{wait}s 后重试 {i+2}/{retries}")
                time.sleep(wait)
    raise RuntimeError(f"GET {url} 重试 {retries} 次仍失败: {last}")


def fetch_model(user_id, proxies):
    """获取模特资料（/users/{id} 的 item）。失败返回仅含 id 的最小信息。"""
    url = f"{API_BASE}/users/{user_id}"
    try:
        data = get_json(url, build_headers(user_id), proxies)
        item = data.get("item") or {}
        if item:
            return item
    except Exception as e:
        log(f"[警告] 模特资料获取失败，仅保留 user_id: {str(e)[:120]}")
    return {"id": user_id, "username": ""}


def fetch_all_videos(user_id, proxies):
    """分页拉取某模特的全部视频，返回视频列表。"""
    headers = build_headers(user_id)
    out, seen, page = [], set(), 1
    total_items = None
    while True:
        url = f"{API_BASE}/users/{user_id}/videos"
        data = get_json(url, headers, proxies) if page == 1 else get_json(
            f"{url}?page={page}&limit={PAGE_LIMIT}", headers, proxies)
        if page == 1:
            total_items = data.get("totalItems")
        batch = data.get("videos") or []
        if not batch:
            break
        new = 0
        for v in batch:
            vid = v.get("id")
            if vid is not None and vid not in seen:
                seen.add(vid)
                out.append(v)
                new += 1
        # 终止条件：本页无新数据，或已达 totalItems，或接口不支持分页（一页返回全部）
        if new == 0 or (total_items and len(out) >= total_items) or len(batch) < PAGE_LIMIT:
            break
        page += 1
    log(f"模特 {user_id} 共抓取 {len(out)} 个视频（接口 totalItems={total_items}）")
    return out


def pick_video_url(v):
    """按规则选链：free -> videoUrl，否则 trailerUrl。返回 (url, 来源字段名)。"""
    if v.get("accessMode") == "free":
        return v.get("videoUrl") or "", "videoUrl"
    return v.get("trailerUrl") or "", "trailerUrl"


def to_document(video, model):
    """把接口视频 + 模特信息规整为待入库文档。"""
    chosen_url, url_field = pick_video_url(video)
    return {
        "_id": video["id"],                 # 用视频 id 做幂等主键
        "video_id": video["id"],
        "user_id": video.get("userId") or model.get("id"),
        "model": {
            "id": model.get("id"),
            "username": model.get("username", ""),
            "is_online": model.get("isOnline"),
            "is_model": model.get("isModel"),
            "is_studio": model.get("isStudio"),
            "ranking": model.get("userRanking"),
        },
        "title": video.get("title", ""),
        "description": video.get("description", ""),
        "access_mode": video.get("accessMode", ""),
        "url_field": url_field,             # 记录选用了哪个链接字段
        "video_url": chosen_url,            # 按规则选出的最终链接
        "raw_video_url": video.get("videoUrl", ""),
        "raw_trailer_url": video.get("trailerUrl", ""),
        "cover_url": video.get("coverUrl", ""),
        "duration": video.get("duration"),
        "likes": video.get("likes"),
        "views_count": video.get("viewsCount"),
        "cost": video.get("cost"),
        "status": video.get("status", ""),
        "video_type": video.get("type", ""),
        "presets": video.get("presets", []),
        "created_at": video.get("createdAt", ""),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def save_to_mongo(docs):
    """按 _id 幂等 upsert，返回 (插入数, 更新数)。"""
    from pymongo import MongoClient, ASCENDING
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=12000)
    try:
        client.admin.command("ping")
        col = client[MONGO_DB][MONGO_COLLECTION]
        col.create_index([("video_id", ASCENDING)], unique=True)
        col.create_index([("user_id", ASCENDING), ("created_at", ASCENDING)])
        inserted = updated = 0
        for doc in docs:
            res = col.update_one(
                {"_id": doc["_id"]},
                {"$set": doc, "$setOnInsert": {"first_seen": doc["fetched_at"]}},
                upsert=True,
            )
            if res.upserted_id is not None:
                inserted += 1
            else:
                updated += 1
        return inserted, updated
    finally:
        client.close()


def main():
    ap = argparse.ArgumentParser(description="抓取模特视频并按规则选链存入 MongoDB")
    ap.add_argument("--user-id", type=int, nargs="+", default=[DEFAULT_USER_ID],
                    help=f"模特用户 ID，可多个（默认 {DEFAULT_USER_ID}）")
    ap.add_argument("--use-proxy", action="store_true",
                    help="经 mihomo 代理访问（默认直连，接口实测可直连）")
    ap.add_argument("--dry-run", action="store_true", help="只抓取打印，不写数据库")
    args = ap.parse_args()

    proxies = None
    if args.use_proxy:
        proxies = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}

    all_docs = []
    for uid in args.user_id:
        log(f"===== 处理模特 user_id={uid} =====")
        model = fetch_model(uid, proxies)
        log(f"模特: {model.get('username') or '(无用户名)'}  在线={model.get('isOnline')}")
        videos = fetch_all_videos(uid, proxies)
        docs = [to_document(v, model) for v in videos]
        for d in docs:
            log(f"  视频 {d['video_id']} [{d['access_mode']}] -> {d['url_field']} "
                f"| {d['title'][:24]}")
        all_docs.extend(docs)

    log(f"合计 {len(all_docs)} 条视频文档")
    if args.dry_run:
        log("[dry-run] 不写库。样例文档：")
        if all_docs:
            import json
            print(json.dumps(all_docs[0], ensure_ascii=False, indent=1))
        return 0

    inserted, updated = save_to_mongo(all_docs)
    log(f"✅ 写入 {MONGO_DB}.{MONGO_COLLECTION}：新增 {inserted} 条，更新 {updated} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
