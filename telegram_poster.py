"""Telegram Bot 发布模块：把直播图片/视频发布到 Telegram 频道。

直接走 Telegram Bot HTTP API，无需浏览器。支持：
  - sendPhoto 发送图片（九宫格）
  - sendVideo 发送视频（直播录制片段）

文案复用 upload_grid_to_x.build_post_text（与 X 一致的随机模板/主题/标签）。

配置见 tg_config.py（BOT_TOKEN / CHANNEL / PROXY），该文件不入库。
"""
import json
import os
import sys
import time

import requests

# 设置 UTF-8 输出
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

try:
    from tg_config import BOT_TOKEN, CHANNEL, PROXY
except ImportError:
    print("[错误] 缺少 tg_config.py，请复制 tg_config.example.py 并填入真实配置。",
          file=sys.stderr)
    raise

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None


class TelegramPublishError(RuntimeError):
    """Telegram API 调用失败。"""


def _check_response(resp):
    """校验 Telegram API 返回，失败抛异常。"""
    try:
        data = resp.json()
    except ValueError:
        raise TelegramPublishError(f"非 JSON 响应，HTTP {resp.status_code}: {resp.text[:300]}")
    if not data.get("ok"):
        raise TelegramPublishError(
            f"Telegram API 返回错误: {data.get('error_code')} {data.get('description')}"
        )
    return data["result"]


def _upload(method, field_name, media_path, caption=""):
    """通用 multipart 上传（sendPhoto / sendVideo）。"""
    abs_path = os.path.abspath(media_path)
    if not os.path.exists(abs_path):
        raise TelegramPublishError(f"媒体文件不存在: {abs_path}")

    # Telegram 图片说明上限 1024 字符
    cap = caption[:1024]
    url = f"{API_BASE}/{method}"
    with open(abs_path, "rb") as f:
        files = {field_name: (os.path.basename(abs_path), f, "application/octet-stream")}
        data = {"chat_id": CHANNEL}
        if cap:
            data["caption"] = cap
        resp = requests.post(url, data=data, files=files, proxies=PROXIES, timeout=120)
    result = _check_response(resp)
    print(f"[OK] {method} 发布成功，message_id={result.get('message_id')}")
    return result


def send_photo(photo_path, caption=""):
    """发送图片到频道。"""
    print(f"上传图片: {photo_path} ({os.path.getsize(photo_path)} 字节)")
    return _upload("sendPhoto", "photo", photo_path, caption)


def send_video(video_path, caption=""):
    """发送视频到频道。"""
    print(f"上传视频: {video_path} ({os.path.getsize(video_path)} 字节)")
    return _upload("sendVideo", "video", video_path, caption)


def send_media_group(photo_paths, caption="", per_batch=10):
    """以相册（media group）形式批量发图，每批最多 per_batch（Telegram 上限 10）张。

    caption 只挂在每批第一张上；这里由调用方决定是否只给首批传文案。
    返回所有批次的 result 列表。
    """
    results = []
    batches = [photo_paths[i:i + per_batch]
               for i in range(0, len(photo_paths), per_batch)]
    total = len(batches)
    for bi, batch in enumerate(batches):
        media = []
        files = {}
        for ii, p in enumerate(batch):
            ap = os.path.abspath(p)
            if not os.path.exists(ap):
                raise TelegramPublishError(f"媒体文件不存在: {ap}")
            key = f"photo{ii}"
            item = {"type": "photo", "media": f"attach://{key}"}
            # 文案只放在第一批的第一张
            if caption and bi == 0 and ii == 0:
                item["caption"] = caption[:1024]
            media.append(item)
            files[key] = (os.path.basename(ap), open(ap, "rb"),
                          "application/octet-stream")
        data = {"chat_id": CHANNEL, "media": json.dumps(media)}
        try:
            resp = requests.post(f"{API_BASE}/sendMediaGroup", data=data,
                                 files=files, proxies=PROXIES, timeout=300)
        finally:
            for _, (_, fh, _) in files.items():
                fh.close()
        result = _check_response(resp)
        results.append(result)
        print(f"[OK] 相册批次 {bi + 1}/{total} 发送成功（{len(batch)} 张）")
        if bi < total - 1:
            time.sleep(2)  # 避免触发 Bot API 速率
    return results
