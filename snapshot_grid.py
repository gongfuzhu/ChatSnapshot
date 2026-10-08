"""直播封面九宫格生成脚本。"""
import io
import random
import sys
from datetime import datetime

import requests
from PIL import Image

API_URL = (
    "https://zh.streams.modelapp.org/api/front/v2/models"
    "?primaryTag=girls&limit=24&topLimit=61&favoritesLimit=24&msBlock=true"
    "&byw=false&flags=0&srwm=false&rcmGrp=A&rbCnGr=true&iem=true&decMb=true"
    "&ctryTop=true&mlfv=false&rectf=false&eab=false&nic=true&removeShows=true"
    "&uniq=tbm65h13l0wkexi2"
)
IMG_BASE = "https://img.doppiocdn.org/thumbs"
# 封面 CDN 在国内晚高峰会被严重限速（直连每张约 25-30s），走代理可降到 1s 内。
# 设为空字符串则直连。
IMG_PROXY = "http://127.0.0.1:7890"
IMG_PROXIES = (
    {"http": IMG_PROXY, "https": IMG_PROXY} if IMG_PROXY else None
)
CELL_W = 320
CELL_H = 240
GAP = 4
GRID = 3
TIMEOUT = 15
JPEG_QUALITY = 90
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def build_cover_url(model):
    return f"{IMG_BASE}/{model['snapshotTimestamp']}/{model['id']}"


def pick_9_covers(data, n=9):
    blocks = [b for b in data.get("blocks", []) if b.get("models")]
    # 汇总去重后的可用模型总数，作为兜底上限
    all_ids = {m["id"] for b in blocks for m in b["models"]}
    target = min(n, len(all_ids))

    picked = []
    seen = set()
    while len(picked) < target:
        block = random.choice(blocks)
        model = random.choice(block["models"])
        if model["id"] in seen:
            continue
        seen.add(model["id"])
        picked.append({"id": model["id"], "url": build_cover_url(model)})
    return picked


def fetch_data():
    try:
        resp = requests.get(
            API_URL, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        print(f"[错误] 请求接口失败: {e}", file=sys.stderr)
        sys.exit(1)


def _download_one(c):
    """下载单张封面，返回 (cover, PIL.Image)；失败返回 (cover, None)。"""
    try:
        resp = requests.get(
            c["url"], headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT, proxies=IMG_PROXIES,
        )
        resp.raise_for_status()
        return c, Image.open(io.BytesIO(resp.content)).convert("RGB")
    except (requests.RequestException, OSError) as e:
        print(f"[警告] 下载封面失败 id={c['id']}: {e}", file=sys.stderr)
        return c, None


def download_images(covers):
    """并行下载封面（保持 covers 原有顺序），失败的项自动跳过。"""
    from concurrent.futures import ThreadPoolExecutor

    results = []
    with ThreadPoolExecutor(max_workers=min(9, len(covers) or 1)) as ex:
        for c, img in ex.map(_download_one, covers):
            results.append((c, img))

    # 按 covers 顺序返回成功图片，并记录成功 id 集合
    ok_ids = {c["id"] for c, img in results if img is not None}
    images = [img for c, img in results if img is not None]
    # 若要严格对齐 covers 顺序（map 本身按提交顺序返回，这里仅兜底）
    by_id = {c["id"]: img for c, img in results if img is not None}
    images = [by_id[c["id"]] for c in covers if c["id"] in ok_ids]
    return images


def fit_cover(img, w, h):
    src_w, src_h = img.size
    scale = max(w / src_w, h / src_h)
    new_w, new_h = int(src_w * scale + 0.5), int(src_h * scale + 0.5)
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    return resized.crop((left, top, left + w, top + h))


def make_grid(images, out_path):
    canvas_w = GRID * CELL_W + (GRID - 1) * GAP
    canvas_h = GRID * CELL_H + (GRID - 1) * GAP
    canvas = Image.new("RGB", (canvas_w, canvas_h), "white")
    for idx in range(GRID * GRID):
        if idx >= len(images):
            break
        cell = fit_cover(images[idx], CELL_W, CELL_H)
        row, col = divmod(idx, GRID)
        x = col * (CELL_W + GAP)
        y = row * (CELL_H + GAP)
        canvas.paste(cell, (x, y))
    canvas.save(out_path, "JPEG", quality=JPEG_QUALITY)


def main():
    data = fetch_data()
    covers = pick_9_covers(data, n=GRID * GRID)
    if len(covers) < GRID * GRID:
        print(f"[警告] 可用模型仅 {len(covers)} 个，不足 9 个，空位将留白。")
    images = download_images(covers)
    if not images:
        print("[错误] 没有任何封面下载成功，退出。", file=sys.stderr)
        sys.exit(1)
    out_path = datetime.now().strftime("grid_%Y%m%d_%H%M%S.jpg")
    make_grid(images, out_path)
    print(f"[完成] 已生成九宫格: {out_path}（{len(images)} 张封面）")


if __name__ == "__main__":
    main()