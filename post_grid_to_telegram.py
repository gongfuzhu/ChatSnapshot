"""生成九宫格图片并发布到 Telegram 频道。

复用 snapshot_grid 的取数/拼图 与 upload_grid_to_x.build_post_text 的随机文案。
用法: python post_grid_to_telegram.py
"""
import sys

from snapshot_grid import (
    download_images,
    fetch_data,
    make_grid,
    pick_9_covers,
)
from upload_grid_to_x import build_post_text, build_username_map
from telegram_poster import send_photo, TelegramPublishError
from datetime import datetime


def main():
    data = fetch_data()
    covers = pick_9_covers(data, n=9)
    if not covers:
        print("[错误] 没有可用封面，退出。", file=sys.stderr)
        sys.exit(1)

    images = download_images(covers)
    if not images:
        print("[错误] 封面全部下载失败，退出。", file=sys.stderr)
        sys.exit(1)

    out_path = datetime.now().strftime("grid_%Y%m%d_%H%M%S.jpg")
    make_grid(images, out_path)

    username = build_username_map(data).get(covers[0]["id"], "")
    caption = build_post_text(username) if username else ""
    print(f"[信息] 发布文案: {caption}")

    try:
        send_photo(out_path, caption)
    except TelegramPublishError as e:
        print(f"[错误] Telegram 发布失败: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        import os
        if os.path.exists(out_path):
            os.remove(out_path)
            print(f"[信息] 已删除临时图片: {out_path}")


if __name__ == "__main__":
    main()
