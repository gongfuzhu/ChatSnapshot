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
# 模特列表接口（亚洲/语言筛选），用 offset 翻页；{offset} 占位由代码替换。
MODELS_LIST_URL = (
    "https://zh.streams.modelapp.org/api/front/models?removeShows=false&recInFeatured=false"
    "&limit=60&offset={offset}&primaryTag=girls"
    "&filterGroupTags=%5B%5B%22tagLanguageChinese%22%2C%22tagLanguageVietnamese%22%2C"
    "%22tagLanguageJapanese%22%5D%2C%5B%22ethnicityAsian%22%5D%5D"
    "&sortBy=stripRanking&parentTag=ethnicityAsian&nic=true&byw=false&rcmGrp=A&rbCnGr=true"
    "&iem=true&decMb=true&dmv=false&ctryTop=true&mlfv=false&rectf=false&sac=true"
)

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


def adapt_list_model(m):
    """把模特列表接口的原始字段适配为 to_document 期望的 model 结构。"""
    return {
        "id": m.get("id"),
        "username": m.get("username", ""),
        "isOnline": m.get("isOnline"),
        "isModel": m.get("isModel"),
        "isStudio": m.get("isStudio"),
        "userRanking": m.get("topBestPlace"),
        # 列表特有信息一并保留
        "country": m.get("country", ""),
        "viewersCount": m.get("viewersCount"),
        "isLive": m.get("isLive"),
    }


def fetch_models_from_list(max_models, proxies):
    """翻页拉取模特列表接口，返回适配后的模特列表（最多 max_models 个）。"""
    headers = build_headers("models")
    out, offset = [], 0
    page_size = 60
    while len(out) < max_models:
        data = get_json(MODELS_LIST_URL.format(offset=offset), headers, proxies)
        batch = data.get("models") or []
        if not batch:
            break
        for m in batch:
            if m.get("id") is not None:
                out.append(adapt_list_model(m))
                if len(out) >= max_models:
                    break
        log(f"列表 offset={offset}：本页 {len(batch)} 个，累计候选 {len(out)}"
            f"（filteredCount={data.get('filteredCount')}）")
        if len(batch) < page_size:
            break
        offset += page_size
    return out[:max_models]


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
            # 以下仅模特列表批量模式有值，单模特详情模式为 None
            "country": model.get("country"),
            "viewers_count": model.get("viewersCount"),
            "is_live": model.get("isLive"),
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
        # 发布审核三态：pending 待审核(默认) / approved 可发布 / rejected 不发布。
        # 仅首次插入写入，之后由 Web 页面维护，重复抓取不覆盖（见 save_to_mongo）。
        "publish_status": "pending",
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
            # publish_status 仅首次插入设默认 pending；从 $set 剔除，
            # 避免重复抓取覆盖人工在页面上做的审核标注。
            doc = {k: v for k, v in doc.items() if k != "publish_status"}
            set_on_insert = {"first_seen": doc["fetched_at"],
                             "publish_status": "pending"}
            res = col.update_one(
                {"_id": doc["_id"]},
                {"$set": doc, "$setOnInsert": set_on_insert},
                upsert=True,
            )
            if res.upserted_id is not None:
                inserted += 1
            else:
                updated += 1
        return inserted, updated
    finally:
        client.close()


def collect_for_model(model, proxies):
    """对单个模特抓视频并生成文档；单个模特失败不影响整体。"""
    uid = model.get("id")
    try:
        videos = fetch_all_videos(uid, proxies)
        return [to_document(v, model) for v in videos], None
    except Exception as e:  # noqa: BLE001
        return [], f"{type(e).__name__}: {str(e)[:100]}"


def main():
    ap = argparse.ArgumentParser(description="抓取模特视频并按规则选链存入 MongoDB")
    ap.add_argument("--user-id", type=int, nargs="+", default=None,
                    help=f"指定模特用户 ID，可多个；不指定时默认 {DEFAULT_USER_ID}")
    ap.add_argument("--from-list", action="store_true",
                    help="批量模式：从模特列表接口翻页抓取模特，再逐个保存其视频")
    ap.add_argument("--max-models", type=int, default=60,
                    help="--from-list 模式最多处理多少个模特（默认 60，即列表第一页）")
    ap.add_argument("--use-proxy", action="store_true",
                    help="经 mihomo 代理访问（默认直连，接口实测可直连）")
    ap.add_argument("--dry-run", action="store_true", help="只抓取打印，不写数据库")
    args = ap.parse_args()

    proxies = None
    if args.use_proxy:
        proxies = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}

    # 确定要处理的模特列表（统一为适配后的 model dict）
    if args.from_list:
        log(f"===== 批量模式：从模特列表抓取，最多 {args.max_models} 个 =====")
        models = fetch_models_from_list(args.max_models, proxies)
        log(f"列表返回 {len(models)} 个候选模特")
    else:
        uids = args.user_id or [DEFAULT_USER_ID]
        models = [fetch_model(uid, proxies) for uid in uids]

    all_docs, empty, failed = [], 0, []
    for i, model in enumerate(models, 1):
        uid = model.get("id")
        uname = model.get("username") or "(无用户名)"
        log(f"[{i}/{len(models)}] 模特 {uname} (id={uid})")
        docs, err = collect_for_model(model, proxies)
        if err:
            failed.append((uname, err))
            log(f"  [跳过] 抓取失败：{err}")
            continue
        if not docs:
            empty += 1
            log("  无视频，跳过")
            continue
        for d in docs:
            log(f"  视频 {d['video_id']} [{d['access_mode']}] -> {d['url_field']} "
                f"| {d['title'][:24]}")
        all_docs.extend(docs)

    log(f"统计：模特 {len(models)} 个 | 有视频并入库候选 {len(models)-empty-len(failed)} 个 "
        f"| 无视频 {empty} 个 | 失败 {len(failed)} 个 | 视频文档 {len(all_docs)} 条")
    if failed:
        log("失败明细：" + "；".join(f"{n}({e})" for n, e in failed[:10]))

    if args.dry_run:
        log("[dry-run] 不写库。")
        if all_docs:
            import json
            print(json.dumps(all_docs[0], ensure_ascii=False, indent=1))
        return 0

    if not all_docs:
        log("没有可写入的视频文档，结束。")
        return 0

    inserted, updated = save_to_mongo(all_docs)
    log(f"✅ 写入 {MONGO_DB}.{MONGO_COLLECTION}：新增 {inserted} 条，更新 {updated} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
