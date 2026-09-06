"""X (Twitter) API v2 客户端 — OAuth 1.0a 认证。

支持：媒体上传、发帖、读取用户信息。
凭证加载顺序：环境变量(X_CONSUMER_KEY 等) > x_config.py
"""
import base64
import os
import time
from pathlib import Path

import requests
from requests_oauthlib import OAuth1

# 代理设置
_PROXY = os.environ.get("X_PROXY", "http://127.0.0.1:7890")
PROXIES = {"http": _PROXY, "https": _PROXY} if _PROXY else None

API_BASE = "https://api.x.com/2"
UPLOAD_URL = "https://upload.x.com/1.1/media/upload.json"
CHUNK_SIZE = 4 * 1024 * 1024  # 4MB per chunk


def _load_credentials():
    """从环境变量或 x_config.py 加载凭证。"""
    keys = ["CONSUMER_KEY", "CONSUMER_SECRET", "ACCESS_TOKEN", "ACCESS_TOKEN_SECRET"]
    env_vals = {k: os.environ.get(f"X_{k}", "") for k in keys}
    if all(env_vals.values()):
        return env_vals
    try:
        import x_config  # type: ignore
        return {
            "CONSUMER_KEY": getattr(x_config, "CONSUMER_KEY", ""),
            "CONSUMER_SECRET": getattr(x_config, "CONSUMER_SECRET", ""),
            "ACCESS_TOKEN": getattr(x_config, "ACCESS_TOKEN", ""),
            "ACCESS_TOKEN_SECRET": getattr(x_config, "ACCESS_TOKEN_SECRET", ""),
        }
    except ImportError:
        return env_vals


CREDS = _load_credentials()


def _oauth():
    return OAuth1(
        CREDS["CONSUMER_KEY"],
        CREDS["CONSUMER_SECRET"],
        CREDS["ACCESS_TOKEN"],
        CREDS["ACCESS_TOKEN_SECRET"],
    )


def check_auth():
    """验证凭证是否有效，返回 (ok, user_info_or_error_msg)。"""
    if not all(CREDS.values()):
        missing = [k for k, v in CREDS.items() if not v]
        return False, f"缺少凭证: {', '.join(missing)}"
    try:
        r = requests.get(
            f"{API_BASE}/users/me", auth=_oauth(), proxies=PROXIES, timeout=15
        )
        if r.status_code == 200:
            return True, r.json()["data"]
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


def _guess_media_type(path):
    ext = path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".mp4": "video/mp4",
        ".mov": "video/mp4",
    }.get(ext, "image/jpeg")


def upload_media(file_path, category="tweet_image"):
    """上传图片/视频到 X，返回 media_id_string。
    小文件单步上传，大文件分片上传。
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    media_type = _guess_media_type(path)
    file_size = path.stat().st_size
    is_video = media_type.startswith("video")

    # 小图片用单步上传（base64）
    if not is_video and file_size <= 5 * 1024 * 1024:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        r = requests.post(
            UPLOAD_URL,
            auth=_oauth(),
            proxies=PROXIES,
            data={"media_data": b64, "media_category": category},
            timeout=60,
        )
        if r.status_code != 200:
            raise RuntimeError(f"上传失败: {r.status_code} {r.text[:200]}")
        return r.json()["media_id_string"]

    # 分片上传（INIT -> APPEND -> FINALIZE）
    return _upload_chunked(path, media_type, category, file_size)


def _upload_chunked(path, media_type, category, file_size):
    """INIT/APPEND/FINALIZE 分片上传。"""
    oauth = _oauth()

    # INIT
    r = requests.post(
        UPLOAD_URL,
        auth=oauth,
        proxies=PROXIES,
        params={
            "command": "INIT",
            "media_type": media_type,
            "total_bytes": file_size,
            "media_category": category,
        },
        timeout=30,
    )
    if r.status_code != 202:
        raise RuntimeError(f"INIT 失败: {r.status_code} {r.text[:200]}")
    media_id = r.json()["media_id_string"]

    # APPEND
    with open(path, "rb") as f:
        seg = 0
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            r = requests.post(
                UPLOAD_URL,
                auth=oauth,
                proxies=PROXIES,
                params={"command": "APPEND", "media_id": media_id, "segment_index": seg},
                files={"media": chunk},
                timeout=120,
            )
            if r.status_code not in (200, 204):
                raise RuntimeError(f"APPEND 失败(seg {seg}): {r.status_code} {r.text[:200]}")
            seg += 1

    # FINALIZE
    r = requests.post(
        UPLOAD_URL,
        auth=oauth,
        proxies=PROXIES,
        params={"command": "FINALIZE", "media_id": media_id},
        timeout=60,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"FINALIZE 失败: {r.status_code} {r.text[:200]}")

    # 视频需要等待处理完成
    info = r.json()
    if info.get("processing_info", {}).get("state") == "pending":
        _wait_for_media(media_id)

    return media_id


def _wait_for_media(media_id, timeout=300):
    """轮询等待媒体处理完成。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = requests.get(
            UPLOAD_URL,
            auth=_oauth(),
            proxies=PROXIES,
            params={"command": "STATUS", "media_id": media_id},
            timeout=30,
        )
        info = r.json()
        state = info.get("processing_info", {}).get("state", "succeeded")
        if state == "succeeded":
            return
        if state == "failed":
            raise RuntimeError(f"媒体处理失败: {info}")
        check_after = info.get("processing_info", {}).get("check_after_secs", 5)
        time.sleep(check_after)
    raise TimeoutError("媒体处理超时")


def post_tweet(text="", media_ids=None):
    """发帖，支持附带媒体。返回响应 JSON。"""
    payload = {}
    if text:
        payload["text"] = text
    if media_ids:
        if isinstance(media_ids, str):
            media_ids = [media_ids]
        payload["media"] = {"media_ids": media_ids}

    r = requests.post(
        f"{API_BASE}/tweets",
        auth=_oauth(),
        proxies=PROXIES,
        json=payload,
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"发帖失败: {r.status_code} {r.text[:300]}")
    return r.json()
