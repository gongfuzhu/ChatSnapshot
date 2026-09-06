"""推荐直播间录制 15 秒视频并发布到 X（浏览器自动化方案）。

流程：推荐直播间接口 → 选观看人数最多的 public 直播间
      → ffmpeg 录制 480p 流 15 秒 → 复用 upload_grid_to_x 浏览器编排发帖。
"""
import os
import sys

import requests

from upload_grid_to_x import render_post_text

# 设置 UTF-8 输出
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

# ========== 配置 ==========
API_URL = (
    "https://go.whitetrafsa.com/api/models?landing=Player"
    "&sortBy=paidUsers&modelsList=princiana&stripcashR=0&forceClient=1"
    "&modelPromotion=0&acclan=0&abTest=player_PlayerD20260826"
    "&abTestVariant=player_PlayerD20260826_PlayerGA_9"
    "&seenAbTest=1&seenDomain=1&seenLanding=1&limit=20"
)
HEADERS = {
    "accept": "*/*",
    "origin": "https://creative.whitetrafsa.com",
    "referer": "https://creative.whitetrafsa.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    ),
}
RECORD_SECONDS = 15
# HTTP 代理（设为空字符串则不使用代理）
PROXY = "http://127.0.0.1:7890"
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None
# 发帖文案。{username} 会被替换为被录制主播的用户名。
POST_TEXT = "直播间：{username}"
# ========== 配置结束 ==========


def fetch_recommended():
    """请求推荐直播间接口，返回 JSON。失败时报错退出。"""
    try:
        resp = requests.get(
            API_URL, headers=HEADERS, proxies=PROXIES, timeout=15
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        print(f"[错误] 请求推荐接口失败: {e}", file=sys.stderr)
        sys.exit(1)


def pick_top_streamer(models):
    """选观看人数最多且 status 为 public 的直播间；无 public 时兜底全量最大。"""
    if not models:
        return None
    public = [m for m in models if m.get("status") == "public"]
    pool = public if public else models
    return max(pool, key=lambda m: m.get("viewersCount", 0))