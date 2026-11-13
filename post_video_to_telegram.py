"""录制直播视频并发布到 Telegram 频道。

复用 record_stream_to_x.generate_video（选流/录制/重试/取 topic）、
upload_grid_to_x.build_post_text（与 X 一致的随机文案/主题/标签）。

加固策略（上传失败不丢内容）：
  - 发布失败时不删视频，移动到 pending_videos/ 并在 pending_videos/pending.json
    中登记 {path, caption, ts}，本次任务仍以非零退出码告警
  - 每次运行开头先扫描 pending 队列尝试补发；补发成功才删文件
  - 补发阶段失败不影响当次新录制；补发全程失败不额外报错刷屏（只记录）
用法: python post_video_to_telegram.py
"""
import json
import os
import shutil
import sys
from datetime import datetime

from record_stream_to_x import generate_video
from upload_grid_to_x import build_post_text
from telegram_poster import send_video, TelegramPublishError

PENDING_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "pending_videos")
PENDING_JSON = os.path.join(PENDING_DIR, "pending.json")
# 超过 7 天的积压视频不再自动补发（避免频道被旧内容刷屏），仅保留文件待人工处理
PENDING_MAX_AGE_SEC = 7 * 24 * 3600


def _load_pending():
    if not os.path.exists(PENDING_JSON):
        return []
    try:
        with open(PENDING_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        print(f"[待发] pending.json 读取失败，忽略: {e}", file=sys.stderr)
        return []


def _save_pending(items):
    os.makedirs(PENDING_DIR, exist_ok=True)
    tmp = PENDING_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PENDING_JSON)


def _queue_pending(video_path, caption):
    """把发布失败的视频移入待发目录并登记。"""
    os.makedirs(PENDING_DIR, exist_ok=True)
    dest = os.path.join(PENDING_DIR, os.path.basename(video_path))
    if os.path.abspath(video_path) != os.path.abspath(dest):
        shutil.move(video_path, dest)
    items = _load_pending()
    items.append({
        "path": dest,
        "caption": caption,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "retries": 0,
    })
    _save_pending(items)
    print(f"[待发] 视频已保留待补发: {dest}")


def _drain_pending():
    """尝试补发队列中的视频。

    返回 (成功数, 仍失败数)。过期或文件丢失的条目直接清理。
    """
    items = _load_pending()
    if not items:
        return 0, 0
    print(f"[待发] 发现 {len(items)} 个待补发视频，开始尝试补发...")
    now = datetime.now()
    kept, ok_count, fail_count = [], 0, 0
    for item in items:
        path = item.get("path", "")
        caption = item.get("caption", "")
        # 清理丢失文件
        if not os.path.exists(path):
            print(f"[待发] 文件已不存在，移除记录: {path}")
            continue
        # 过期积压：不自动发，保留在队列里等人工处理
        try:
            age = (now - datetime.fromisoformat(item.get("ts", ""))).total_seconds()
        except ValueError:
            age = 0
        if age > PENDING_MAX_AGE_SEC:
            print(f"[待发] 超过 {PENDING_MAX_AGE_SEC // 86400} 天，跳过自动补发: {path}")
            kept.append(item)
            continue
        try:
            send_video(path, caption)
            os.remove(path)
            print(f"[待发] 补发成功并删除: {path}")
            ok_count += 1
        except TelegramPublishError as e:
            item["retries"] = item.get("retries", 0) + 1
            print(f"[待发] 补发仍失败（第 {item['retries']} 次）: {e}",
                  file=sys.stderr)
            kept.append(item)
            fail_count += 1
    _save_pending(kept)
    return ok_count, fail_count


def main():
    # 1) 先补发历史失败的视频（失败不阻塞本次录制）
    try:
        ok, fail = _drain_pending()
        if ok or fail:
            print(f"[待发] 补发结束：成功 {ok}，仍失败 {fail}")
    except Exception as e:
        print(f"[待发] 补发流程异常（不影响本次任务）: {e}", file=sys.stderr)

    # 2) 正常录制 + 发布本次新视频
    video_path = datetime.now().strftime("stream_%Y%m%d_%H%M%S.mp4")
    publish_failed = False
    try:
        _, username, _, topic = generate_video(out_path=video_path)
        caption = build_post_text(username, topic=topic)
        print(f"[信息] 发布文案: {caption}")
        try:
            send_video(video_path, caption)
        except TelegramPublishError as e:
            print(f"[错误] Telegram 发布失败: {e}", file=sys.stderr)
            # 关键：保留视频，登记待补发，而不是删掉
            try:
                _queue_pending(video_path, caption)
            except Exception as qe:
                print(f"[错误] 登记待发队列也失败，视频保留在原位 "
                      f"{video_path}: {qe}", file=sys.stderr)
            publish_failed = True
    except SystemExit:
        raise
    except Exception as e:
        print(f"[ERROR] 错误: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        # 只有发布成功（视频未被移走）时才清理；入队的文件已 move 走
        if video_path and os.path.exists(video_path) and not publish_failed:
            os.remove(video_path)
            print(f"[信息] 已删除临时视频: {video_path}")

    if publish_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
