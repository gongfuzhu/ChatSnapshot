#!/usr/bin/env python3
"""把 MongoDB 中标注为「可发布」(publish_status=approved) 的模特视频发布到 X。

按模特维度发布（与直播录制发帖相互独立，复用同一套 Chrome CDP 上传）：
  1. 选出下一个有待发视频的模特（按其最早待发视频的 created_at 轮转）
  2. 取该模特最多 GROUP_SIZE(4) 条待发视频，free 完整视频优先、组内 FIFO；不足 4 条有几条发几条
  3. 逐一下载为本地 mp4（m3u8 走代理/mp4 直连，统一截 140 秒），410 失效自动刷新/删除跳过
  4. 把下载成功的多个视频作为同一条推文的多个附件一次发出（单条推文最多 4 个视频）
  5. 发布成功后批量回写这些视频的 x_published=true / x_published_at，保证幂等不重发

字段约定：
  publish_status == "approved"  人工审核通过、允许发布
  x_published == True           已发布到 X（候选查询据此排除）
  x_published_at                首次成功发布的 UTC 时间
"""
import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

from pymongo import ASCENDING, MongoClient
from playwright.sync_api import sync_playwright

from model_db_config import MONGO_URI, MONGO_DB, MONGO_COLLECTION
from fetch_model_videos import (
    API_BASE,
    build_headers,
    fetch_all_videos,
    get_json,
    pick_video_url,
)
from upload_grid_to_x import (
    DEBUG_PORT,
    LIVE_URL,
    ensure_browser,
    upload_media_to_x,
)
from record_stream_to_x import PROXY, clean_topic, ensure_smooth_video

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

# X 免费账号单条视频上限 140 秒，统一截断
MAX_VIDEO_SECONDS = 140
# ffmpeg 下载总超时兜底：截断时长 + 网络余量
FFMPEG_MARGIN = 120
# 按模特维度发布：每条多视频推文最多合并的视频数（X 单条上限 4 个视频）
GROUP_SIZE = 4
# 可发布候选的基础查询
PENDING_QUERY = {"publish_status": "approved", "x_published": {"$ne": True}}


class ExpiredLinkError(RuntimeError):
    """视频链接已失效（HLS 签名过期，服务端返回 410 Gone）。"""


def log(msg):
    print(f"[model-x] {msg}")


def is_hls(url):
    """根据 URL 判断是否 HLS 流（m3u8）。"""
    path = (url or "").lower().split("?", 1)[0]
    return path.endswith(".m3u8")


def build_download_args(url, out_path, max_seconds=MAX_VIDEO_SECONDS):
    """构造下载命令。

    m3u8 走 mihomo 代理（直连会超时）；mp4 直链直连（实测走代理反而被拖慢）。
    统一 -t max_seconds 截断、-c copy 原样拷贝（源已是 h264+aac，X 兼容）。
    """
    args = ["ffmpeg", "-y"]
    if is_hls(url):
        args += ["-http_proxy", PROXY,
                 "-reconnect", "1", "-reconnect_streamed", "1",
                 "-reconnect_on_network_error", "1",
                 "-reconnect_delay_max", "3"]
    args += ["-rw_timeout", "20000000", "-i", url,
             "-t", str(max_seconds),
             "-c", "copy", "-movflags", "+faststart", out_path]
    return args


def download_video(url, out_path, max_seconds=MAX_VIDEO_SECONDS):
    """下载远程视频为本地 mp4，返回质检告警（可能为空）。"""
    if not shutil.which("ffmpeg"):
        raise FileNotFoundError("未找到 ffmpeg，请先安装 ffmpeg")
    timeout = max_seconds + FFMPEG_MARGIN
    try:
        result = subprocess.run(
            build_download_args(url, out_path, max_seconds),
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        if os.path.exists(out_path):
            os.remove(out_path)
        raise RuntimeError(f"ffmpeg 下载超时(>{timeout}s): {url[:80]}")
    if result.returncode != 0:
        stderr = result.stderr or ""
        # HLS 签名过期：服务端对 master.m3u8 返回 410 Gone（ffmpeg rc=8）
        if "410 Gone" in stderr or "4XX Client Error" in stderr and "410" in stderr:
            if os.path.exists(out_path):
                os.remove(out_path)
            raise ExpiredLinkError(f"视频链接已失效(410 Gone): {url[:80]}")
        raise RuntimeError(f"ffmpeg 下载失败: {stderr[-400:]}")
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 10 * 1024:
        raise RuntimeError(f"下载结果异常（文件过小或不存在）: {out_path}")
    # 复用录制链路的冻结帧质检（发现问题自动 CFR 修复，不阻断）
    return ensure_smooth_video(out_path)


def find_pending(col, limit=1, video_id=None):
    """取 approved 且未发布 X 的候选，按平台创建时间 FIFO。"""
    query = dict(PENDING_QUERY)
    if video_id is not None:
        query["video_id"] = video_id
    return list(col.find(query).sort("created_at", ASCENDING).limit(limit))


def sort_videos_for_group(docs, group_size=GROUP_SIZE):
    """同一模特的待发视频排序并截取一组：free 优先，组内按 created_at FIFO。

    排序键：access_mode=='free' 的排前（键 0，其余 1），再按 created_at 升序。
    纯函数，便于单测；返回至多 group_size 条。
    """
    ordered = sorted(
        docs,
        key=lambda d: (0 if d.get("access_mode") == "free" else 1,
                       d.get("created_at") or ""),
    )
    return ordered[:group_size]


def select_next_model(col):
    """选出本轮要发布的模特：取其最早一条待发视频 created_at 最靠前的模特。

    按模特维度轮转，保证不同模特轮流被发，而不是总集中在同一人。
    返回 username（可能为空串），无候选时返回 None。
    """
    pipe = [
        {"$match": PENDING_QUERY},
        {"$group": {"_id": "$model.username",
                    "oldest": {"$min": "$created_at"}}},
        {"$sort": {"oldest": ASCENDING}},
        {"$limit": 1},
    ]
    row = next(iter(col.aggregate(pipe)), None)
    return None if row is None else (row["_id"] or "")


def fetch_model_group(col, username, group_size=GROUP_SIZE):
    """取指定模特的一组待发视频（free 优先、FIFO，至多 group_size 条）。"""
    query = dict(PENDING_QUERY)
    query["model.username"] = username
    # 多拉一些，交由纯函数做 free 优先 + 截取（free 与非 free 都要按 created_at 排）
    docs = list(col.find(query).sort("created_at", ASCENDING))
    return sort_videos_for_group(docs, group_size)


def mark_published(col, video_id):
    """发布成功后回写幂等标记与发布时间（候选查询已排除已发布，不会重复调用）。"""
    return col.update_one(
        {"video_id": video_id},
        {"$set": {"x_published": True,
                  "x_published_at": datetime.now(timezone.utc).isoformat()}},
    )


def mark_many_published(col, video_ids):
    """一条多视频推文发布成功后，批量回写组内所有视频的幂等标记。"""
    ts = datetime.now(timezone.utc).isoformat()
    return col.update_many(
        {"video_id": {"$in": list(video_ids)}},
        {"$set": {"x_published": True, "x_published_at": ts}},
    )


def delete_expired(col, video_id):
    """链接失效(410)且无法刷新时，从库中删除，避免反复占用发布队列。"""
    return col.delete_one({"video_id": video_id})


def refresh_video_url(col, doc):
    """重新调接口为失效视频换取带新签名(expires/md5)的播放链接并写库。

    free 视频的 videoUrl 是带过期时间的 HLS 签名直链，过期后服务端返回 410；
    重新请求 /users/{user_id}/videos 即可拿到同一 video_id 的新链接。
    选链规则与抓取端一致（free -> videoUrl，其余 -> trailerUrl）。
    返回新链接字符串；接口里找不到该视频或拿不到链接时返回 None。
    """
    vid = doc.get("video_id")
    uid = doc.get("user_id") or (doc.get("model") or {}).get("id")
    if not uid:
        log(f"视频 {vid} 缺少 user_id，无法重新拉取链接")
        return None
    try:
        videos = fetch_all_videos(uid, None)
    except Exception as e:  # noqa: BLE001
        log(f"视频 {vid} 重新拉取接口失败: {type(e).__name__}: {str(e)[:120]}")
        return None
    fresh = next((v for v in videos if v.get("id") == vid), None)
    if fresh is None:
        log(f"接口已不再返回视频 {vid}（可能被下架），无法刷新链接")
        return None
    new_url, url_field = pick_video_url(fresh)
    if not new_url or new_url == doc.get("video_url"):
        log(f"视频 {vid} 刷新得到的链接为空或未变化，放弃刷新")
        return None
    col.update_one(
        {"video_id": vid},
        {"$set": {
            "video_url": new_url,
            "url_field": url_field,
            "raw_video_url": fresh.get("videoUrl", ""),
            "raw_trailer_url": fresh.get("trailerUrl", ""),
            "link_refreshed_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    doc["video_url"] = new_url
    doc["url_field"] = url_field
    log(f"视频 {vid} 已刷新播放链接（{url_field}），新 expires 签名已写库")
    return new_url


def build_text_for(doc):
    """用模特名 + 视频标题 + 模特直播链接生成发帖文案（视频库内容，非直播话术）。"""
    username = ((doc.get("model") or {}).get("username") or "").strip()
    title = clean_topic(doc.get("title", "") or "")
    parts = []
    if title:
        parts.append(title)
    if username:
        parts.append(f"模特：{username}")
        parts.append(LIVE_URL.replace("{username}", username))
    if not parts:
        parts.append("精选视频分享")
    parts.append("#模特 #视频")
    return "\n".join(parts)


def fetch_cam_topic(user_id):
    """实时查模特当前直播间话题 cam.topic。

    接口：GET /api/front/v2/models/{user_id}/cam -> {"cam": {"topic": ...}}。
    仅当 cam.isCamActive 且 topic 非空白时返回文本；其余情况（下播/未设话题/接口异常）
    一律返回 None，绝不阻断发布流程。
    """
    if not user_id:
        return None
    url = f"{API_BASE}/models/{user_id}/cam"
    try:
        data = get_json(url, build_headers(user_id), None)
    except Exception as e:  # noqa: BLE001
        log(f"查询 cam.topic 失败(user_id={user_id})：{type(e).__name__}，文案回退不带话题")
        return None
    cam = (data or {}).get("cam") or {}
    if not cam.get("isCamActive"):
        return None
    topic = (cam.get("topic") or "").strip()
    return topic or None


def _group_topic(docs):
    """取一组视频所属模特的当前直播话题（同一模特，user_id 相同）。查不到返回 None。"""
    for d in docs:
        uid = d.get("user_id") or (d.get("model") or {}).get("id")
        if uid:
            return fetch_cam_topic(uid)
    return None


def build_group_text(docs, topic=None):
    """一条多视频推文的文案：同一模特的多个视频，只写一次模特名与直播链接。

    topic 为实时查询到的 cam.topic（直播话题），有值时放在最前；为空则不带。
    """
    if not docs:
        return "精选视频分享\n#模特 #视频"
    first = docs[0]
    username = ((first.get("model") or {}).get("username") or "").strip()
    parts = []
    if topic:
        parts.append(clean_topic(topic))
    if username:
        parts.append(f"模特：{username}")
        parts.append(LIVE_URL.replace("{username}", username))
    if not parts:
        parts.append("精选视频分享")
    parts.append("#模特 #视频")
    return "\n".join(parts)


def publish_one(doc, dry_run=False):
    """处理单条视频：下载 → (发布) → 回写。dry_run 只下载不发帖。"""
    vid = doc["video_id"]
    url = doc.get("video_url")
    if not url:
        raise RuntimeError(f"视频 {vid} 缺少 video_url")
    out_path = f"modelx_{vid}.mp4"
    log(f"视频 {vid} ({doc.get('access_mode')}, "
        f"{'HLS' if is_hls(url) else 'MP4'}, 时长{doc.get('duration')}s) 开始下载…")
    warn = download_video(url, out_path)
    if warn:
        log(f"质检告警: {warn}")
    size_mb = os.path.getsize(out_path) / 1024 / 1024
    log(f"下载完成: {out_path} ({size_mb:.1f}MB)")

    if dry_run:
        log(f"[dry-run] 跳过实际发帖与回写，保留文件 {out_path}")
        return False

    post_text = build_text_for(doc)
    log(f"发帖文案: {post_text}")
    proc, started_by_us = ensure_browser(port=DEBUG_PORT)
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
            context = browser.contexts[0]
            page = context.new_page()
            upload_media_to_x(page, out_path, post_text)
    finally:
        if started_by_us and proc is not None:
            proc.terminate()
            log("已关闭本脚本启动的 Chrome")
    return True


def _download_one(col, doc, dry_run=False):
    """下载组内单条视频并处理 410 失效。

    成功返回本地 mp4 路径；链接失效且刷新失败被删除时返回 None（该条跳过，不阻断整组）。
    非 410 的其他异常向上抛，由调用方计为失败。
    """
    vid = doc["video_id"]
    url = doc.get("video_url")
    if not url:
        raise RuntimeError(f"视频 {vid} 缺少 video_url")
    out_path = f"modelx_{vid}.mp4"
    log(f"视频 {vid} ({doc.get('access_mode')}, "
        f"{'HLS' if is_hls(url) else 'MP4'}, 时长{doc.get('duration')}s) 开始下载…")
    try:
        warn = download_video(url, out_path)
        if warn:
            log(f"质检告警: {warn}")
        return out_path
    except ExpiredLinkError:
        if dry_run:
            log(f"视频 {vid} 链接失效(410)，[dry-run] 不刷新不删除，跳过该条")
            return None
        # 正式模式：先尝试换新签名链接重试一次
        new_url = refresh_video_url(col, doc)
        if new_url:
            try:
                warn = download_video(new_url, out_path)
                if warn:
                    log(f"质检告警: {warn}")
                log(f"视频 {vid} 刷新链接后下载成功")
                return out_path
            except ExpiredLinkError:
                log(f"视频 {vid} 刷新后链接仍失效(410)")
        delete_expired(col, vid)
        log(f"已删除失效视频 {vid}，从本组跳过")
        return None


def publish_group(col, docs, dry_run=False):
    """把同一模特的一组视频下载后合并成一条多视频推文。

    返回 dict：posted 是否真正发帖、paths 本地文件、ok_vids 成功条目、
    expired_vids 失效被删条目、failed_vids 其他失败条目。
    """
    paths, ok_docs, expired_vids, failed_vids = [], [], [], []
    for doc in docs:
        vid = doc["video_id"]
        try:
            p = _download_one(col, doc, dry_run)
            if p:
                paths.append(p)
                ok_docs.append(doc)
            else:
                expired_vids.append(vid)
        except Exception as e:  # 单条其他失败不阻断整组，保留待下次重试
            log(f"视频 {vid} 下载失败（保留待重试）: {e}")
            failed_vids.append(vid)
            if os.path.exists(f"modelx_{vid}.mp4"):
                os.remove(f"modelx_{vid}.mp4")

    result = {"posted": False, "paths": paths, "ok_vids": [d["video_id"] for d in ok_docs],
              "expired_vids": expired_vids, "failed_vids": failed_vids}
    if not paths:
        log("本组没有可成功下载的视频，跳过发帖")
        return result

    if dry_run:
        preview = build_group_text(ok_docs, topic=_group_topic(ok_docs))
        log(f"[dry-run] 已下载 {len(paths)} 个视频，将合并为 1 条推文；保留文件: "
            f"{[os.path.basename(p) for p in paths]}")
        log(f"[dry-run] 将使用文案:\n{preview}")
        return result

    post_text = build_group_text(ok_docs, topic=_group_topic(ok_docs))
    log(f"多视频推文文案:\n{post_text}")
    proc, started_by_us = ensure_browser(port=DEBUG_PORT)
    page = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
            context = browser.contexts[0]
            # 清理历史失败可能残留的发帖标签页，避免标签页泄漏拖垮低内存机器上的浏览器
            for stale in list(context.pages):
                try:
                    if "x.com/compose/post" in (stale.url or ""):
                        stale.close()
                except Exception:  # noqa: BLE001
                    pass
            page = context.new_page()
            # 一次选中多个视频，作为同一条推文的多个附件
            upload_media_to_x(page, paths, post_text)
    finally:
        # 无论成功失败都关闭本次标签页，杜绝标签页泄漏
        if page is not None:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
        if started_by_us and proc is not None:
            proc.terminate()
            log("已关闭本脚本启动的 Chrome")
        for pth in paths:
            if os.path.exists(pth):
                os.remove(pth)
                log(f"已删除临时文件 {pth}")
    result["posted"] = True
    return result


def main():
    ap = argparse.ArgumentParser(description="发布 approved 模特视频到 X 并回写已发布标记")
    ap.add_argument("--limit", type=int, default=1,
                    help="本次最多发几条多视频推文（每条=1个模特的最多4个视频，默认1条）")
    ap.add_argument("--video-id", type=int, default=None, help="只处理指定 video_id")
    ap.add_argument("--dry-run", action="store_true", help="只下载验证，不发帖不回写不删除")
    args = ap.parse_args()

    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=12000)
    col = client[MONGO_DB][MONGO_COLLECTION]
    col.create_index([("publish_status", ASCENDING), ("x_published", ASCENDING)])

    # 指定单条模式：不做「换下一条」，仅处理该 video_id
    if args.video_id is not None:
        docs = find_pending(col, limit=1, video_id=args.video_id)
        if not docs:
            log(f"video_id={args.video_id} 不在可发布候选中（不存在/非approved/已发布）")
            return
        _process_one(col, docs[0], args, allow_skip=False)
        return

    # dry-run：只验证「下一个模特的一组」视频，不发帖不回写不删除
    if args.dry_run:
        username = select_next_model(col)
        if username is None:
            log("没有待发布的 approved 视频（全部已发布或尚未标注）")
            return
        group = fetch_model_group(col, username, GROUP_SIZE)
        log(f"[dry-run] 下一轮模特：{username or '(无名)'}，本组 {len(group)} 条 "
            f"（free {sum(1 for d in group if d.get('access_mode') == 'free')} 条）")
        publish_group(col, group, dry_run=True)
        return

    # 按模特维度发布：每轮选 1 个模特，其最多 GROUP_SIZE 条视频合并为 1 条多视频推文
    posts = 0          # 成功发出的推文条数
    videos_pub = 0     # 成功发布的视频总数
    expired_total = 0
    failed_total = 0
    used_models = set()
    guard = 0
    while posts < args.limit and guard < args.limit + 200:
        guard += 1
        username = select_next_model(col)
        if username is None:
            log("已无待发布候选，结束")
            break
        if username in used_models:
            # 理论上回写后该模特会从候选消失；防御性退出避免死循环
            break
        used_models.add(username)

        group = fetch_model_group(col, username, GROUP_SIZE)
        if not group:
            continue
        log(f"本轮模特：{username or '(无名)'}，待发 {len(group)} 条"
            f"（free {sum(1 for d in group if d.get('access_mode') == 'free')} 条），合并为 1 条推文")
        res = publish_group(col, group, dry_run=False)
        expired_total += len(res["expired_vids"])
        failed_total += len(res["failed_vids"])
        if res["posted"]:
            mark_many_published(col, res["ok_vids"])
            posts += 1
            videos_pub += len(res["ok_vids"])
            log(f"模特 {username or '(无名)'} 的 {len(res['ok_vids'])} 个视频已合并发布并回写")
        else:
            log(f"模特 {username or '(无名)'} 本组无可用视频，未发帖（失效 {len(res['expired_vids'])} / "
                f"失败 {len(res['failed_vids'])}）")

    log(f"结束：发出推文 {posts} 条（含视频 {videos_pub} 个）| 失效删除 {expired_total} 条 | "
        f"其他失败 {failed_total} 条")


def _process_one(col, doc, args, allow_skip):
    """处理单条，返回 'published' / 'expired' / 'failed' / 'dryrun'。"""
    vid = doc["video_id"]
    out_path = f"modelx_{vid}.mp4"
    try:
        published = publish_one(doc, dry_run=args.dry_run)
        if published:
            mark_published(col, vid)
            log(f"视频 {vid} 已发布并回写 x_published=true")
            return "published"
        return "dryrun"
    except ExpiredLinkError as e:
        log(f"视频 {vid} 链接失效: {e}")
        if args.dry_run:
            log("[dry-run] 不刷新、不删除该条，仅报告失效")
            return "expired"
        # 优先重新调接口换取新签名链接并重试一次，而非直接丢弃
        new_url = refresh_video_url(col, doc)
        if new_url:
            log(f"视频 {vid} 用刷新后的链接重试发布…")
            try:
                if publish_one(doc, dry_run=False):
                    mark_published(col, vid)
                    log(f"视频 {vid} 刷新链接后已发布并回写 x_published=true")
                    return "published"
            except ExpiredLinkError as e2:
                log(f"视频 {vid} 刷新后链接仍失效: {e2}")
            except Exception as e2:  # noqa: BLE001
                log(f"视频 {vid} 刷新链接后发布失败（保留待下次重试）: {e2}")
                return "failed"
        res = delete_expired(col, vid)
        log(f"已从库中删除失效视频 {vid}（删除 {res.deleted_count} 条），自动换下一条")
        if not allow_skip:
            log("指定单条模式，无下一条可换")
        return "expired"
    except Exception as e:  # 其他失败不删除、不回写，下次仍可重试
        log(f"视频 {vid} 处理失败: {e}")
        return "failed"
    finally:
        # 正式发布后清理临时文件；dry-run 保留供检查
        if not args.dry_run and os.path.exists(out_path):
            os.remove(out_path)
            log(f"已删除临时文件 {out_path}")


if __name__ == "__main__":
    main()
