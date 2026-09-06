"""推荐直播间录制 15 秒视频并发布到 X（浏览器自动化方案）。

流程：推荐直播间接口 → 选观看人数最多的 public 直播间
      → ffmpeg 录制 480p 流 15 秒 → 复用 upload_grid_to_x 浏览器编排发帖。
"""
import os
import shutil
import subprocess
import sys
from datetime import datetime

import requests
from playwright.sync_api import sync_playwright

from upload_grid_to_x import (
    DEBUG_PORT,
    ensure_browser,
    render_post_text,
    upload_media_to_x,
)

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


def extract_stream_url(model):
    """取 480p 流地址，缺失时兜底 stream.url，均无返回空字符串。"""
    stream = model.get("stream", {})
    return stream.get("urls", {}).get("480p", "") or stream.get("url", "")


def build_ffmpeg_args(stream_url, out_path, seconds, proxy=PROXY):
    """构造 ffmpeg 拉流录制命令。重编码为 H.264/AAC，满足 X 上传要求。"""
    args = [
        "ffmpeg", "-y",
        "-headers", "Referer: https://creative.whitetrafsa.com/\r\n",
    ]
    if proxy:
        args += ["-http_proxy", proxy]
    args += [
        "-i", stream_url,
        "-t", str(seconds),
        "-c:v", "libx264", "-c:a", "aac",
        "-movflags", "+faststart",
        out_path,
    ]
    return args


def record_stream(stream_url, out_path, seconds=RECORD_SECONDS):
    """调用 ffmpeg 录制 HLS 流为 mp4。ffmpeg 缺失抛 FileNotFoundError，录制失败抛 RuntimeError。"""
    if not shutil.which("ffmpeg"):
        raise FileNotFoundError(
            "未找到 ffmpeg，请先安装（如 winget install ffmpeg 或 apt install ffmpeg）"
        )
    result = subprocess.run(
        build_ffmpeg_args(stream_url, out_path, seconds),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 录制失败: {result.stderr[-500:]}")
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 10 * 1024:
        raise RuntimeError(f"录制结果异常（文件过小或不存在）: {out_path}")


def generate_video(out_path=None):
    """选流并录制，返回 (视频路径, username, stream_url)。数据不足时报错退出。"""
    data = fetch_recommended()
    model = pick_top_streamer(data.get("models", []))
    if model is None:
        print("[错误] 推荐接口没有可用直播间，退出。", file=sys.stderr)
        sys.exit(1)
    stream_url = extract_stream_url(model)
    if not stream_url:
        print("[错误] 直播间没有可用的 480p 流地址，退出。", file=sys.stderr)
        sys.exit(1)
    username = model.get("username", "")
    if not username:
        print("[错误] 直播间没有 username，退出。", file=sys.stderr)
        sys.exit(1)
    if out_path is None:
        out_path = datetime.now().strftime("stream_%Y%m%d_%H%M%S.mp4")
    print(f"[信息] 选定直播间: {username}（观看 {model.get('viewersCount', 0)}）")
    record_stream(stream_url, out_path, RECORD_SECONDS)
    print(f"[完成] 已录制视频: {out_path}")
    return out_path, username, stream_url


def main():
    video_path = datetime.now().strftime("stream_%Y%m%d_%H%M%S.mp4")
    proc = None
    started_by_us = False
    try:
        _, username, _ = generate_video(out_path=video_path)
        post_text = render_post_text(POST_TEXT, username)
        print(f"[信息] 发帖文案: {post_text}")

        proc, started_by_us = ensure_browser(port=DEBUG_PORT)

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(
                f"http://127.0.0.1:{DEBUG_PORT}"
            )
            context = browser.contexts[0]
            page = context.new_page()
            upload_media_to_x(page, video_path, post_text)
    except SystemExit:
        raise
    except Exception as e:
        print(f"[ERROR] 错误: {e}", file=sys.stderr)
        print("\n请确保:")
        print("1. 系统已安装 ffmpeg 并在 PATH 中")
        print("2. Chrome 调试端口可用，且已登录 X 账号")
        sys.exit(1)
    finally:
        # 临时视频用完即删
        if video_path and os.path.exists(video_path):
            os.remove(video_path)
            print(f"[信息] 已删除临时视频: {video_path}")
        # 自己启动的浏览器发帖后关闭；复用已有的保持不动
        if started_by_us and proc is not None:
            proc.terminate()
            print("[信息] 已关闭本脚本启动的 Chrome")


if __name__ == "__main__":
    main()
