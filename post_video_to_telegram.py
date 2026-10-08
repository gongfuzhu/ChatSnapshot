"""录制直播视频并发布到 Telegram 频道。

复用 record_stream_to_x.generate_video（选流/录制/重试/取 topic）、
upload_grid_to_x.build_post_text（与 X 一致的随机文案/主题/标签）。
用法: python post_video_to_telegram.py
"""
import os
import sys
from datetime import datetime

from record_stream_to_x import generate_video
from upload_grid_to_x import build_post_text
from telegram_poster import send_video, TelegramPublishError


def main():
    video_path = datetime.now().strftime("stream_%Y%m%d_%H%M%S.mp4")
    try:
        _, username, _, topic = generate_video(out_path=video_path)
        caption = build_post_text(username, topic=topic)
        print(f"[信息] 发布文案: {caption}")
        try:
            send_video(video_path, caption)
        except TelegramPublishError as e:
            print(f"[错误] Telegram 发布失败: {e}", file=sys.stderr)
            sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        print(f"[ERROR] 错误: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if video_path and os.path.exists(video_path):
            os.remove(video_path)
            print(f"[信息] 已删除临时视频: {video_path}")


if __name__ == "__main__":
    main()
