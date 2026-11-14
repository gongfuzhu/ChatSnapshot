#!/usr/bin/env python3
"""把 MongoDB 中标注为「可发布」(publish_status=approved) 的模特视频发布到 X。

流程（与直播录制发帖相互独立，复用同一套 Chrome CDP 上传）：
  1. 查库取 approved 且尚未发布 X 的视频（FIFO，按平台创建时间）
  2. 按链接形态下载为本地 mp4：m3u8 走代理拉流，mp4 直链直连，统一截断 140 秒
  3. 复用 upload_grid_to_x 的浏览器编排发 X
  4. 发布成功后回写 x_published=true / x_published_at，保证幂等不重发

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
from upload_grid_to_x import (
    DEBUG_PORT,
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
        raise RuntimeError(f"ffmpeg 下载失败: {result.stderr[-400:]}")
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 10 * 1024:
        raise RuntimeError(f"下载结果异常（文件过小或不存在）: {out_path}")
    # 复用录制链路的冻结帧质检（发现问题自动 CFR 修复，不阻断）
    return ensure_smooth_video(out_path)


def find_pending(col, limit=1, video_id=None):
    """取 approved 且未发布 X 的候选，按平台创建时间 FIFO。"""
    query = {"publish_status": "approved", "x_published": {"$ne": True}}
    if video_id is not None:
        query["video_id"] = video_id
    return list(col.find(query).sort("created_at", ASCENDING).limit(limit))


def mark_published(col, video_id):
    """发布成功后回写幂等标记与发布时间（候选查询已排除已发布，不会重复调用）。"""
    return col.update_one(
        {"video_id": video_id},
        {"$set": {"x_published": True,
                  "x_published_at": datetime.now(timezone.utc).isoformat()}},
    )


def build_text_for(doc):
    """用模特名 + 视频标题生成发帖文案（视频库内容，非直播，不用直播话术）。"""
    username = ((doc.get("model") or {}).get("username") or "").strip()
    title = clean_topic(doc.get("title", "") or "")
    parts = []
    if title:
        parts.append(title)
    if username:
        parts.append(f"模特：{username}")
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


def main():
    ap = argparse.ArgumentParser(description="发布 approved 模特视频到 X 并回写已发布标记")
    ap.add_argument("--limit", type=int, default=1, help="本次最多发布几条（默认 1）")
    ap.add_argument("--video-id", type=int, default=None, help="只处理指定 video_id")
    ap.add_argument("--dry-run", action="store_true", help="只下载验证，不发帖不回写")
    args = ap.parse_args()

    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=12000)
    col = client[MONGO_DB][MONGO_COLLECTION]
    col.create_index([("publish_status", ASCENDING), ("x_published", ASCENDING)])

    docs = find_pending(col, limit=args.limit, video_id=args.video_id)
    if not docs:
        log("没有待发布的 approved 视频（全部已发布或尚未标注）")
        return
    log(f"本次候选 {len(docs)} 条")

    done = 0
    for doc in docs:
        vid = doc["video_id"]
        out_path = f"modelx_{vid}.mp4"
        try:
            published = publish_one(doc, dry_run=args.dry_run)
            if published:
                mark_published(col, vid)
                log(f"视频 {vid} 已发布并回写 x_published=true")
                done += 1
        except Exception as e:  # 单条失败不影响后续，且不回写、下次仍可重试
            log(f"视频 {vid} 处理失败: {e}")
        finally:
            # 正式发布后清理临时文件；dry-run 保留供检查
            if not args.dry_run and os.path.exists(out_path):
                os.remove(out_path)
                log(f"已删除临时文件 {out_path}")
    log(f"结束：成功发布 {done}/{len(docs)} 条")


if __name__ == "__main__":
    main()
