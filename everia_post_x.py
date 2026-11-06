#!/usr/bin/env python3
"""everia 已下载图集 -> X 自动发帖（独立脚本，xChina 流程不受影响）。

数据来源是 everia_download.py 已下载到 everia_downloads/ 的图集（元数据在
everia_posted.json）。本脚本：
  1. 从已下载图集中挑一套“还没发过 X”的（独立去重 everia_x_posted.json，
     与下载脚本的去重文件分开，互不干扰）；
  2. 均匀精选 n 张（首/中/尾，复用 xchina 的 spread_indices，X 单帖最多 4 图）；
  3. webp/png 先转成临时 jpg（X 图片附件对 webp 兼容性不稳），发帖后清理；
  4. 复用 xchina_post_x.post_to_x_browser 的成熟发帖逻辑
     （可见弹窗定位、等媒体传完、多信号成功判定、登录失效报错）。

用法：
  python everia_post_x.py              # 选一套未发图集发 X（默认 4 张）
  python everia_post_x.py --n 4
  python everia_post_x.py --slug <目录名>   # 指定已下载的某套
  python everia_post_x.py --dry-run    # 只选图/转换，不发帖
"""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

from xchina_post_x import (
    cdp_alive,
    launch_chrome,
    post_to_x_browser,
    spread_indices,
)
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
DL_ROOT = HERE / "everia_downloads"
DL_POSTED = HERE / "everia_posted.json"        # 下载脚本的元数据
X_POSTED = HERE / "everia_x_posted.json"       # 本脚本独立去重
MAX_N = 4

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")


def log(msg):
    print(f"[everia-X] {msg}", flush=True)


def load_json(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log(f"读取 {path.name} 失败，按空处理: {e}")
        return {}


def save_x_posted(data):
    tmp = X_POSTED.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, X_POSTED)


def build_caption(meta):
    """用 everia 的标题 + 标签拼 X 文案。"""
    title = (meta.get("title") or "").strip()
    tags = [t for t in (meta.get("tags") or []) if t]
    parts = [title] if title else []
    if tags:
        parts.append(" / ".join(tags[:3]))
    parts.append("#写真 #美女 #套图")
    return "\n".join(p for p in parts if p)


def pick_album(slug):
    """返回 (slug, meta, 图片目录)。优先指定 slug，否则选第一套未发 X 的。"""
    dl_meta = load_json(DL_POSTED)
    x_posted = load_json(X_POSTED)
    if not dl_meta:
        raise RuntimeError("everia_posted.json 为空，请先用 everia_download.py 下载图集")

    if slug:
        if slug not in dl_meta:
            raise RuntimeError(f"指定 slug 不在已下载记录中: {slug}")
        candidates = [slug]
    else:
        # 按下载时间从新到旧
        candidates = sorted(
            dl_meta.keys(),
            key=lambda s: dl_meta[s].get("ts", 0),
            reverse=True,
        )

    for s in candidates:
        meta = dl_meta[s]
        album_dir = DL_ROOT / s
        if not album_dir.is_dir():
            continue
        imgs = sorted(
            p for p in album_dir.iterdir()
            if p.suffix.lower() in (".webp", ".jpg", ".jpeg", ".png")
        )
        if len(imgs) < 1:
            continue
        if s in x_posted:
            log(f"跳过已发 X: {s}")
            continue
        return s, meta, imgs
    return None, None, None


def to_jpg_temp(src_path, tmpdir, idx):
    """把单张图转成临时 jpg（透明铺白底），返回 jpg 路径。已是 jpg 直接用原路径。"""
    src = Path(src_path)
    if src.suffix.lower() in (".jpg", ".jpeg"):
        return str(src)
    out = Path(tmpdir) / f"{idx:05d}.jpg"
    with Image.open(src) as im:
        im = ImageOps_exif(im)
        if im.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        im.save(out, "JPEG", quality=92)
    return str(out)


def ImageOps_exif(im):
    from PIL import ImageOps
    return ImageOps.exif_transpose(im)


def main():
    ap = argparse.ArgumentParser(description="everia 已下载图集发 X")
    ap.add_argument("--n", type=int, default=4, help=f"发 X 张数（最多 {MAX_N}）")
    ap.add_argument("--slug", default="", help="指定 everia_downloads 下的图集目录名")
    ap.add_argument("--dry-run", action="store_true", help="只选图转换，不发帖")
    args = ap.parse_args()

    n = max(1, min(args.n, MAX_N))
    slug, meta, all_imgs = pick_album(args.slug)
    if not slug:
        log("没有可发的新图集（都已发过 X 或本地无图片），本次跳过。")
        return 2

    total = len(all_imgs)
    idx = spread_indices(total, n)          # 1-based
    chosen = [all_imgs[i - 1] for i in idx]
    caption = build_caption(meta)
    log(f"选中《{meta.get('title') or slug}》共 {total} 张，"
        f"X 选图序号 {idx}（{len(chosen)} 张）")
    log(f"文案:\n{caption}")

    # webp/png 转临时 jpg
    tmpdir = tempfile.mkdtemp(prefix="everia_x_")
    x_paths = [to_jpg_temp(p, tmpdir, i) for i, p in enumerate(chosen, 1)]
    try:
        if args.dry_run:
            log(f"[dry-run] 不发帖。待发文件: {x_paths}")
            return 0

        if not cdp_alive():
            log("CDP 9222 未运行，启动发帖浏览器 ...")
            launch_chrome()
            time.sleep(2)

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
            ctx = browser.contexts[0]
            post_to_x_browser(ctx, x_paths, caption, attach_progress_only=True)
            browser.close()

        # 记录去重
        x_posted = load_json(X_POSTED)
        x_posted[slug] = {
            "title": meta.get("title", ""),
            "pcount": total,
            "x_sent": len(x_paths),
            "indices": idx,
            "ts": int(time.time()),
        }
        save_x_posted(x_posted)
        log(f"✅ 已发 X 并记录去重: {slug}")
        return 0
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
