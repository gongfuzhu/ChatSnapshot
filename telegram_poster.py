"""Telegram Bot 发布模块：把直播图片/视频发布到 Telegram 频道。

直接走 Telegram Bot HTTP API，无需浏览器。支持：
  - sendPhoto 发送图片（九宫格）
  - sendVideo 发送视频（直播录制片段）

文案复用 upload_grid_to_x.build_post_text（与 X 一致的随机模板/主题/标签）。

加固点：
  - 上传前做代理/网络预检（代理偶发断开时提前发现）
  - 所有请求对「连接错误 / 5xx / 429」做指数退避重试
  - 429 遵循 Telegram 返回的 retry_after

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

# 重试参数
MAX_ATTEMPTS = 5
RETRY_BASE_DELAY = 5          # 指数退避基数（秒）：5,10,20,40
PROXY_PRECHECK_URL = "https://api.telegram.org"
PROXY_PRECHECK_TIMEOUT = 10


class TelegramPublishError(RuntimeError):
    """Telegram API 调用失败（重试耗尽或不可重试的业务错误）。"""


def _log(msg):
    print(f"[tg] {msg}", flush=True)


def precheck_proxy():
    """预检代理到 Telegram 的连通性。失败不致命（后续上传还有重试），只告警。"""
    if not PROXY:
        return True
    try:
        r = requests.get(PROXY_PRECHECK_URL, proxies=PROXIES,
                         timeout=PROXY_PRECHECK_TIMEOUT)
        # 根路径返回 404/401 都说明 TLS 链路是通的
        if r.status_code < 500:
            return True
        _log(f"[预检] Telegram 端点返回 HTTP {r.status_code}，继续尝试上传")
    except requests.RequestException as e:
        _log(f"[预检] 代理连接 Telegram 失败: {e}，稍后由上传重试兜底")
    return False


def _parse_retry_after(resp):
    """从 429 响应里解析 retry_after（秒）。"""
    try:
        data = resp.json()
        params = data.get("parameters") or {}
        ra = params.get("retry_after")
        if isinstance(ra, (int, float)):
            return int(ra)
    except ValueError:
        pass
    return None


def _call_with_retry(fn, label="request"):
    """对一个无参请求回调做指数退避重试。

    fn 每次被调用时应重新构造请求（重新打开文件流），返回 requests.Response。
    重试条件：连接类异常（ProxyError/ConnectionError/Timeout）、HTTP 429、5xx。
    不重试：4xx 业务错误（400/403 等），直接返回响应交由 _check_response 处理。
    """
    last_exc = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = fn()
        except (requests.exceptions.ProxyError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError) as e:
            last_exc = e
            if attempt == MAX_ATTEMPTS:
                break
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            _log(f"{label} 连接失败（第 {attempt}/{MAX_ATTEMPTS} 次）: {e}；{delay}s 后重试")
            time.sleep(delay)
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = _parse_retry_after(resp)
            if attempt == MAX_ATTEMPTS:
                return resp  # 交给 _check_response 抛带描述的错误
            delay = retry_after if retry_after else RETRY_BASE_DELAY * (2 ** (attempt - 1))
            _log(f"{label} 收到 HTTP {resp.status_code}（第 {attempt}/{MAX_ATTEMPTS} 次）"
                 f"，{delay}s 后重试")
            time.sleep(delay)
            continue
        return resp

    raise TelegramPublishError(
        f"{label} 网络层重试 {MAX_ATTEMPTS} 次仍失败: {last_exc}")


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


def _upload(method, field_name, media_path, caption="", timeout=300):
    """通用 multipart 上传（sendPhoto / sendVideo），带预检 + 重试。

    注意：requests 重试时文件流需要可重复读取，所以每次尝试都重新 open。
    """
    abs_path = os.path.abspath(media_path)
    if not os.path.exists(abs_path):
        raise TelegramPublishError(f"媒体文件不存在: {abs_path}")

    cap = caption[:1024]
    url = f"{API_BASE}/{method}"
    precheck_proxy()

    # 文件流每次重试都要重新 open，因此把 body 构造放进 _request_with_retry 的回调里
    def _do_post():
        with open(abs_path, "rb") as f:
            files = {field_name: (os.path.basename(abs_path), f,
                                  "application/octet-stream")}
            data = {"chat_id": CHANNEL}
            if cap:
                data["caption"] = cap
            return requests.post(url, data=data, files=files,
                                 proxies=PROXIES, timeout=timeout)

    resp = _call_with_retry(_do_post, label=method)
    result = _check_response(resp)
    _log(f"{method} 发布成功，message_id={result.get('message_id')}")
    return result


def send_photo(photo_path, caption=""):
    """发送图片到频道。"""
    print(f"上传图片: {photo_path} ({os.path.getsize(photo_path)} 字节)")
    return _upload("sendPhoto", "photo", photo_path, caption, timeout=120)


def send_document(doc_path, caption=""):
    """发送文档（如 PDF）到频道。Bot 常规上传上限 50MB。"""
    print(f"上传文档: {doc_path} ({os.path.getsize(doc_path)} 字节)")
    return _upload("sendDocument", "document", doc_path, caption, timeout=300)


def send_video(video_path, caption=""):
    """发送视频到频道。"""
    print(f"上传视频: {video_path} ({os.path.getsize(video_path)} 字节)")
    return _upload("sendVideo", "video", video_path, caption, timeout=600)


def send_media_group(photo_paths, caption="", per_batch=10):
    """以相册（media group）形式批量发图，每批最多 per_batch（Telegram 上限 10）张。

    caption 只挂在每批第一张上；这里由调用方决定是否只给首批传文案。
    返回所有批次的 result 列表。
    """
    results = []
    batches = [photo_paths[i:i + per_batch]
               for i in range(0, len(photo_paths), per_batch)]
    total = len(batches)
    precheck_proxy()
    for bi, batch in enumerate(batches):
        def _do_post(batch=batch, bi=bi):
            media = []
            files = {}
            opened = []
            try:
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
                    fh = open(ap, "rb")
                    opened.append(fh)
                    files[key] = (os.path.basename(ap), fh,
                                  "application/octet-stream")
                data = {"chat_id": CHANNEL, "media": json.dumps(media)}
                return requests.post(f"{API_BASE}/sendMediaGroup", data=data,
                                     files=files, proxies=PROXIES, timeout=300)
            finally:
                # 注意：requests 在 post 返回前已读完文件，这里可安全关闭
                for fh in opened:
                    fh.close()

        resp = _call_with_retry(_do_post, label=f"sendMediaGroup 批次{bi + 1}")
        result = _check_response(resp)
        results.append(result)
        print(f"[OK] 相册批次 {bi + 1}/{total} 发送成功（{len(batch)} 张）")
        if bi < total - 1:
            time.sleep(2)  # 避免触发 Bot API 速率
    return results
