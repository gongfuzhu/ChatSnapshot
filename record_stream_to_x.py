"""推荐直播间录制 15 秒视频并发布到 X（浏览器自动化方案）。

流程：推荐直播间接口 → 选观看人数最多的 public 直播间
      → ffmpeg 录制 480p 流 15 秒 → cam 详情接口取直播间主题
      → 复用 upload_grid_to_x 浏览器编排发帖。
"""
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

import requests
from playwright.sync_api import sync_playwright

from upload_grid_to_x import (
    DEBUG_PORT,
    build_post_text,
    ensure_browser,
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
    # 地区标识：返回中国区内容
    "accept-language": "zh-CN,zh;q=0.9",
    "origin": "https://creative.whitetrafsa.com",
    "referer": "https://creative.whitetrafsa.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    ),
}
RECORD_SECONDS = 15
# 直播间详情接口：录制成功后取 cam.topic 作为发帖文案的主题
CAM_API_URL = (
    "https://zh.streams.modelapp.org/api/front/v2/models/{model_id}/cam"
)
# topic 可能是很长的促销文案，发帖前截断到该长度（超出加省略号）
TOPIC_MAX_LEN = 30
# HTTP 代理（设为空字符串则不使用代理）
PROXY = "http://127.0.0.1:7890"
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None
# 发帖文案由 upload_grid_to_x 的模板池随机生成（build_post_text），
# 多模板轮换 + 链接/标签/语言变化，并尽量带上直播间主题 cam.topic。
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


def build_cam_headers(username=""):
    """构造 cam 详情接口请求头。referer 带主播名，更接近浏览器真实请求。"""
    headers = {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "content-type": "application/json",
        "front-version": "12.0.94",
        "user-agent": HEADERS["user-agent"],
    }
    if username:
        headers["referer"] = f"https://zh.streams.modelapp.org/{username}"
    return headers


def clean_topic(raw, max_len=TOPIC_MAX_LEN):
    """清洗 cam.topic：空值返回空串，折叠所有空白为单个空格，超长截断并加省略号。"""
    if not raw:
        return ""
    text = re.sub(r"\s+", " ", str(raw)).strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "…"
    return text


def fetch_cam_topic(model_id, username=""):
    """请求 cam 详情接口取 cam.topic（已清洗）。任何失败都降级为空串，不阻断发帖。"""
    try:
        resp = requests.get(
            CAM_API_URL.format(model_id=model_id),
            headers=build_cam_headers(username),
            proxies=PROXIES,
            timeout=10,
        )
        resp.raise_for_status()
        topic = clean_topic(resp.json().get("cam", {}).get("topic", ""))
        if topic:
            print(f"[信息] 直播间主题: {topic}")
        return topic
    except (requests.RequestException, ValueError) as e:
        print(f"[警告] 获取直播间主题失败，文案将不带主题: {str(e)[:150]}",
              file=sys.stderr)
        return ""


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


def _candidate_streamers(models):
    """返回按观看人数倒序排列的可录直播间（有 480p 流且 status=public）。"""
    public = [m for m in models if m.get("status") == "public"]
    pool = public if public else models
    pool = [m for m in pool if extract_stream_url(m) and m.get("username")]
    pool.sort(key=lambda m: m.get("viewersCount", 0), reverse=True)
    return pool


def generate_video(out_path=None, max_attempts=3):
    """选流并录制，失败自动换下一个直播间重试。返回 (视频路径, username, stream_url, topic)。

    HLS 流地址可能过期或主播临时断流（返回 404），因此最多尝试 max_attempts 个直播间。
    topic 为该直播间的 cam.topic，取不到时为空串。
    """
    data = fetch_recommended()
    candidates = _candidate_streamers(data.get("models", []))
    if not candidates:
        print("[错误] 推荐接口没有可用直播间，退出。", file=sys.stderr)
        sys.exit(1)

    if out_path is None:
        out_path = datetime.now().strftime("stream_%Y%m%d_%H%M%S.mp4")

    last_err = None
    for idx, model in enumerate(candidates[:max_attempts]):
        username = model["username"]
        stream_url = extract_stream_url(model)
        print(f"[信息] 尝试 {idx + 1}/{min(len(candidates), max_attempts)}: "
              f"{username}（观看 {model.get('viewersCount', 0)}）")
        # 每次尝试用独立的临时文件，避免上次的残留
        attempt_path = out_path if idx == 0 else out_path.replace(".mp4", f"_{idx}.mp4")
        try:
            record_stream(stream_url, attempt_path, RECORD_SECONDS)
        except Exception as e:
            last_err = e
            print(f"[警告] 录制失败: {str(e)[:200]}", file=sys.stderr)
            if os.path.exists(attempt_path):
                os.remove(attempt_path)
            continue

        # 成功：如果不是第一次，把文件重命名回目标名
        if attempt_path != out_path:
            if os.path.exists(out_path):
                os.remove(out_path)
            os.rename(attempt_path, out_path)
        print(f"[完成] 已录制视频: {out_path}")
        topic = fetch_cam_topic(model.get("id"), username)
        return out_path, username, stream_url, topic

    print(f"[错误] 连续 {max_attempts} 个直播间录制均失败，退出。最后错误: {last_err}",
          file=sys.stderr)
    sys.exit(1)


def main():
    video_path = datetime.now().strftime("stream_%Y%m%d_%H%M%S.mp4")
    proc = None
    started_by_us = False
    try:
        _, username, _, topic = generate_video(out_path=video_path)
        post_text = build_post_text(username, topic=topic)
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
