#!/usr/bin/env python3
"""抓取 rosi(jinyemimi) 博主归档列表 entries_s 并幂等存入 MongoDB。

接口：POST http://rosi.jinyemimi.com/api_beta1_3/api.php
鉴权：Cookie 中的 PHPSESSID（见 rosi_config.py，已 gitignore）。

设计：
- 列表按时间倒序，每页 14 条；作品 id 做幂等主键，重复跑只更新不产生重复。
- 增量更新：默认从第 1 页向后翻，一旦某一整页的作品都已在库（进入历史区）即停止，
  因此每月运行时只需翻到新旧分界处，不必扫全部 434 页。
- 首次运行（空集合）会持续翻页；可用 --max-pages 限制页数先小范围验证，
  或 --full 显式全量抓取。
"""
import argparse
import sys
import time
from datetime import datetime, timezone

import requests
from pymongo import ASCENDING, MongoClient

from rosi_config import (
    API_URL, DEFAULT_BID, DEFAULT_TYPE, MAX_RETRIES,
    MONGO_COLLECTION, MONGO_DB, MONGO_URI, PAGE_SIZE,
    PHPSESSID, REQUEST_TIMEOUT,
)

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

# typeid 含义：1=图集，10=视频（其余原样保留）
TYPE_NAME = {"1": "album", "10": "video"}


def log(msg):
    print(f"[rosi] {msg}")


def _to_int(value, default=None):
    """接口多数数值以字符串返回，安全转 int；无法转换时保留默认值。"""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def to_document(entry):
    """把接口单条 entries_s 规整为入库文档（纯函数，便于测试）。"""
    pub_ts = _to_int(entry.get("pubdate"))
    return {
        "_id": _to_int(entry.get("id")),                       # 作品 id 做幂等主键
        "entry_id": _to_int(entry.get("id")),
        "typeid": _to_int(entry.get("typeid")),
        "type_name": TYPE_NAME.get(str(entry.get("typeid")), "unknown"),
        "channel": _to_int(entry.get("channel")),
        "title": entry.get("title", ""),
        "shorttitle": entry.get("shorttitle", ""),
        "writer": entry.get("writer", ""),
        "litpic": entry.get("litpic", ""),
        "cdn": entry.get("cdn", ""),
        "face": entry.get("face", ""),
        "minlitpic": entry.get("minlitpic", ""),
        "description": entry.get("description", ""),
        "introduce": entry.get("introduce", ""),
        "sex": entry.get("sex", ""),
        "mid": _to_int(entry.get("mid")),
        "source": _to_int(entry.get("source")),
        "click": _to_int(entry.get("click"), 0),
        "stow": _to_int(entry.get("stow"), 0),
        "money": _to_int(entry.get("money"), 0),
        "needmoney": _to_int(entry.get("needmoney"), 0),
        "m_isbuy": _to_int(entry.get("m_isbuy"), 0),
        "img_width": _to_int(entry.get("img_width")),
        "img_height": _to_int(entry.get("img_height")),
        "pubdate_ts": pub_ts,
        "pubdate_iso": (
            datetime.fromtimestamp(pub_ts, tz=timezone.utc).isoformat()
            if pub_ts else None
        ),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def build_payload(page, bid=DEFAULT_BID, sort_type=DEFAULT_TYPE):
    """构造请求 body。"""
    return {
        "controller": "archives",
        "action": "Sort",
        "m_mid": "",
        "page": page,
        "type": sort_type,
        "bid": bid,
    }


def fetch_page(page, session=None, bid=DEFAULT_BID, sort_type=DEFAULT_TYPE):
    """抓取单页，返回解析后的 JSON dict；失败重试后抛异常。"""
    sess = session or requests
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Cookie": f"PHPSESSID={PHPSESSID}",
    }
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = sess.post(
                API_URL, json=build_payload(page, bid, sort_type),
                headers=headers, timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            if int(data.get("error", {}).get("state", 0)) != 0:
                raise RuntimeError(f"接口返回错误: {data.get('error')}")
            return data
        except Exception as e:  # 网络/解析错误统一重试
            last_err = e
            log(f"第 {page} 页第 {attempt} 次请求失败: {e}")
            time.sleep(2 * attempt)
    raise RuntimeError(f"第 {page} 页重试 {MAX_RETRIES} 次仍失败: {last_err}")


def is_full_page_stored(entries, existing_ids):
    """一整页作品都已在库 → 已进入历史区，可停止增量翻页。"""
    if not entries:
        return True
    return all(_to_int(e.get("id")) in existing_ids for e in entries)


def save_to_mongo(docs):
    """按 _id 幂等 upsert，返回 (插入数, 更新数)。"""
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=12000)
    try:
        client.admin.command("ping")
        col = client[MONGO_DB][MONGO_COLLECTION]
        col.create_index([("entry_id", ASCENDING)], unique=True)
        col.create_index([("pubdate_ts", ASCENDING)])
        col.create_index([("typeid", ASCENDING)])
        inserted = updated = 0
        for doc in docs:
            doc = dict(doc)
            first_seen = doc.pop("fetched_at")
            set_on_insert = {"first_seen": first_seen}
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


def main():
    ap = argparse.ArgumentParser(description="抓取 rosi 归档 entries_s 入库（幂等增量）")
    ap.add_argument("--max-pages", type=int, default=0,
                    help="最多抓多少页（0=不限制，靠增量自动停止）")
    ap.add_argument("--full", action="store_true",
                    help="全量模式：不因整页已存在而停止（仍幂等）")
    ap.add_argument("--bid", type=int, default=DEFAULT_BID)
    args = ap.parse_args()

    session = requests.Session()
    # 先取第一页以拿到总页数
    first = fetch_page(1, session=session, bid=args.bid)
    total_pages = _to_int(first.get("total_page"), 1)
    total_entries = _to_int(first.get("total_entiries"), 0)
    log(f"接口报告：作品总数 {total_entries}，总页数 {total_pages}，每页 {PAGE_SIZE} 条")

    # 已存在 id 集合用于增量停止判断
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=12000)
    col = client[MONGO_DB][MONGO_COLLECTION]
    existing_ids = set(col.distinct("_id"))
    client.close()
    log(f"库内已有 {len(existing_ids)} 条，开始增量抓取")

    page = 1
    total_ins = total_upd = 0
    fail_pages = 0
    while True:
        if args.max_pages and page > args.max_pages:
            log(f"达到 --max-pages={args.max_pages}，停止")
            break
        data = first if page == 1 else fetch_page(page, session=session, bid=args.bid)
        entries = data.get("entries_s", [])
        if not entries:
            log(f"第 {page} 页无数据，停止")
            break

        if not args.full and is_full_page_stored(entries, existing_ids):
            log(f"第 {page} 页全部已在库，增量到达历史分界，停止")
            break

        docs = [to_document(e) for e in entries]
        ins, upd = save_to_mongo(docs)
        total_ins += ins
        total_upd += upd
        existing_ids.update(d["_id"] for d in docs)
        log(f"第 {page}/{total_pages} 页：本页 {len(entries)} 条，新增 {ins}，更新 {upd}")

        if page >= total_pages:
            log("已到最后一页，完成")
            break
        page += 1
        time.sleep(0.4)  # 温和限速，避免压接口

    log(f"✅ 完成：累计新增 {total_ins} 条，更新 {total_upd} 条，失败页 {fail_pages}")


if __name__ == "__main__":
    main()
