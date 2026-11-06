#!/usr/bin/env python3
"""everia.club 写真套图下载器（纯 requests，无需浏览器）。

站点特性（2026-11 实测）：
  - WordPress 站，列表页 https://everia.club/page/N/，文章页 /YYYY/MM/DD/slug/
  - 正文图在 <div class="entry-content"> 内，<img src> 即原图，无懒加载混淆、
    无图集分页；图床为外部域名（karubox.top / reiobox.top，可能轮换），
    直连 HTTP 200，无需 Referer/cookie，仅需走 mihomo 代理。
  - 图片为 webp（X 支持；如需 jpg 用 --to-jpg，依赖 Pillow）。

用法：
  python everia_download.py                 # 下载列表里第一套未下载的套图
  python everia_download.py --limit 3       # 一次下载 3 套
  python everia_download.py --pages 3       # 扫描前 3 个列表页
  python everia_download.py --url <文章URL>  # 直接下载指定套图
  python everia_download.py --to-jpg        # 下载并转成 jpg
去重：everia_posted.json（key=文章 slug），已下载的跳过。
"""
import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urlparse, unquote

import requests
from bs4 import BeautifulSoup

# ---------- UTF-8 输出 ----------
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

# ---------- 配置 ----------
BASE = "https://everia.club"
HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_ROOT = os.path.join(HERE, "everia_downloads")
POSTED_JSON = os.path.join(HERE, "everia_posted.json")
PROXY = "http://127.0.0.1:7890"
PROXIES = {"http": PROXY, "https": PROXY}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
REQUEST_TIMEOUT = 25
RETRIES = 3
RETRY_SLEEP = 3
# 单张下载间隔，礼貌限速
IMG_INTERVAL = 1.0
# 这些路径/关键字的图片不是正文套图（头像、主题资源、图标等），需排除
IMG_BLACKLIST = ("avatar", "logo", "icon", "wp-content/themes", "gravatar",
                 "wp-includes", "favicon", "sprite", "banner")
IMG_EXTS = (".webp", ".jpg", ".jpeg", ".png")
# ---------- 配置结束 ----------


def log(msg):
    print(f"[everia] {msg}", flush=True)


def http_get(url, binary=False, referer=None):
    """带重试的 GET。binary=True 返回 bytes，否则返回 text。"""
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    last = None
    for i in range(RETRIES):
        try:
            r = requests.get(url, headers=headers, proxies=PROXIES,
                             timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.content if binary else r.text
        except requests.RequestException as e:
            last = e
            if i < RETRIES - 1:
                wait = RETRY_SLEEP * (i + 1)
                log(f"请求失败（{str(e)[:70]}），{wait}s 后重试 {i + 2}/{RETRIES}")
                time.sleep(wait)
    raise RuntimeError(f"GET 失败（已重试{RETRIES}次）{url[:80]}: {last}")


def load_posted():
    if not os.path.exists(POSTED_JSON):
        return {}
    try:
        with open(POSTED_JSON, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        log(f"去重文件读取失败，按空处理: {e}")
        return {}


def save_posted(data):
    tmp = POSTED_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, POSTED_JSON)


def slug_from_url(url):
    """从文章 URL 提取末段 slug 作为唯一标识与目录名。

    URL 里的中文是 percent-encoded，先 unquote 还原；只替换文件系统非法字符，
    保留中文与字母数字（Linux/Samba 均支持中文目录名，可读性更好）。
    """
    path = urlparse(url).path.rstrip("/")
    slug = unquote(path.rsplit("/", 1)[-1])
    # 替换真正非法/控制字符和空白为下划线，保留中文、字母数字、中划线、点
    slug = re.sub(r'[\\/:*?"<>|\s]+', "_", slug).strip("_")
    return slug or f"post_{int(time.time())}"


def list_latest(pages=2):
    """抓前 pages 个列表页，返回去重保序的文章 URL 列表。"""
    posts, seen = [], set()
    for n in range(1, pages + 1):
        list_url = BASE if n == 1 else f"{BASE}/page/{n}/"
        try:
            html = http_get(list_url)
        except Exception as e:
            log(f"列表页 {list_url} 抓取失败: {e}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            # 文章页特征：everia.club/YYYY/MM/DD/slug/
            if not re.match(r"https?://everia\.club/\d{4}/\d{2}/\d{2}/[^/]+/?$", href):
                continue
            # 排除文章内分页：末段为纯数字（如 .../lingyu69/2/），slug 必含字母
            last = unquote(urlparse(href).path.rstrip("/").rsplit("/", 1)[-1])
            if last.isdigit():
                continue
            href = href.rstrip("/") + "/"
            if href not in seen:
                seen.add(href)
                posts.append(href)
        log(f"列表页 {n} 扫描完成，累计候选 {len(posts)} 套")
    return posts


def _extract_images(soup):
    """从一个页面的 entry-content 中按顺序提取正文图片 URL（去重、排除非套图资源）。"""
    content = soup.select_one("div.entry-content")
    if content is None:
        content = soup.find("article") or soup
    imgs, seen = [], set()
    for img in content.find_all("img"):
        src = (img.get("src") or img.get("data-src")
               or img.get("data-lazy-src") or "").strip()
        if not src or src.startswith("data:"):
            continue
        low = src.lower()
        if not low.endswith(IMG_EXTS):
            continue
        if any(bad in low for bad in IMG_BLACKLIST):
            continue
        if src not in seen:
            seen.add(src)
            imgs.append(src)
    return imgs


def _detect_page_count(soup):
    """从 post-page-numbers 分页导航检测文章内分页数；无分页返回 1。"""
    nums = set()
    for a in soup.select("a.post-page-numbers"):
        m = re.search(r"/(\d+)/?$", a.get("href", ""))
        if m:
            nums.add(int(m.group(1)))
    for span in soup.select("span.post-page-numbers.current"):
        t = span.get_text(strip=True)
        if t.isdigit():
            nums.add(int(t))
    return max(nums) if nums else 1


def parse_post(url):
    """解析文章（含 WordPress 文章内分页 /2/ /3/ ...），汇总所有页正文图。

    返回 (title, tags, [图片URL...])，图片按页码顺序拼接、全局去重。
    """
    base = url.rstrip("/")
    html = http_get(base + "/", referer=BASE)
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(" ", strip=True)
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    title = re.sub(r"\s*[–-]\s*Everia\.club\s*$", "", title).strip()

    tags = [t.get_text(strip=True) for t in
            soup.select('a[rel~="tag"]')] or []

    page_count = _detect_page_count(soup)
    imgs, seen = [], set()
    for n in range(1, page_count + 1):
        page_soup = soup if n == 1 else BeautifulSoup(
            http_get(f"{base}/{n}/", referer=BASE), "html.parser")
        page_imgs = _extract_images(page_soup)
        added = 0
        for src in page_imgs:
            if src not in seen:
                seen.add(src)
                imgs.append(src)
                added += 1
        log(f"  第 {n}/{page_count} 页：本页 {len(page_imgs)} 张，新增 {added} 张")
    return title, tags, imgs


def ext_of(url):
    path = urlparse(url).path.lower()
    for e in IMG_EXTS:
        if path.endswith(e):
            return ".jpg" if e == ".jpeg" else e
    return ".webp"


def download_set(url, to_jpg=False):
    """下载一整套。返回 (目录, 张数)。已下载抛 FileExistsError。"""
    slug = slug_from_url(url)
    posted = load_posted()
    if slug in posted:
        raise FileExistsError(f"已下载过，跳过: {slug}")

    title, tags, imgs = parse_post(url)
    if not imgs:
        raise RuntimeError(f"未解析到正文图片: {url}")

    dest_dir = os.path.join(DOWNLOAD_ROOT, slug)
    os.makedirs(dest_dir, exist_ok=True)
    log(f"开始下载《{title or slug}》共 {len(imgs)} 张 -> {dest_dir}")

    saved, failed = [], []
    for i, img_url in enumerate(imgs, 1):
        ext = ext_of(img_url)
        name = f"{i:05d}{ext}"
        out = os.path.join(dest_dir, name)
        try:
            data = http_get(img_url, binary=True, referer=url)
            if len(data) < 1024:
                raise RuntimeError(f"文件过小({len(data)}B)，疑似异常")
            with open(out, "wb") as f:
                f.write(data)
            final = name
            if to_jpg and ext in (".webp", ".png"):
                final = to_jpeg(out)
            saved.append(final)
            if i % 10 == 0 or i == len(imgs):
                log(f"  进度 {i}/{len(imgs)}")
        except Exception as e:
            failed.append((img_url, str(e)[:80]))
            log(f"  [警告] 第{i}张下载失败: {str(e)[:80]}")
        time.sleep(IMG_INTERVAL)

    if failed and not saved:
        raise RuntimeError(f"全部 {len(imgs)} 张下载失败")
    if failed:
        log(f"[注意] {len(failed)} 张失败，成功 {len(saved)} 张")

    posted[slug] = {
        "url": url,
        "title": title,
        "tags": tags,
        "pcount": len(imgs),
        "saved": len(saved),
        "to_jpg": bool(to_jpg),
        "ts": int(time.time()),
        "images": saved,
        "failed": [u for u, _ in failed],
    }
    save_posted(posted)
    log(f"✅ 完成《{title or slug}》：保存 {len(saved)}/{len(imgs)} 张")
    return dest_dir, len(saved)


def to_jpeg(path):
    """把 webp/png 转成同目录 jpg，删除原文件，返回 jpg 文件名。"""
    from PIL import Image
    jpg_path = os.path.splitext(path)[0] + ".jpg"
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.save(jpg_path, "JPEG", quality=92)
    os.remove(path)
    return os.path.basename(jpg_path)


def main():
    ap = argparse.ArgumentParser(description="everia.club 套图下载器")
    ap.add_argument("--url", help="直接下载指定文章页 URL")
    ap.add_argument("--pages", type=int, default=2, help="扫描列表页数（默认2）")
    ap.add_argument("--limit", type=int, default=1, help="本次下载套数（默认1）")
    ap.add_argument("--to-jpg", action="store_true", help="webp/png 转 jpg")
    args = ap.parse_args()

    if args.url:
        candidates = [args.url.rstrip("/") + "/"]
    else:
        candidates = list_latest(args.pages)
        if not candidates:
            log("未发现任何套图链接，退出。")
            sys.exit(1)

    posted = load_posted()
    done = 0
    for url in candidates:
        if done >= args.limit:
            break
        slug = slug_from_url(url)
        if slug in posted:
            log(f"跳过已下载: {slug}")
            continue
        try:
            download_set(url, to_jpg=args.to_jpg)
            done += 1
        except FileExistsError as e:
            log(str(e))
        except Exception as e:
            log(f"[错误] 下载失败 {url}: {e}")

    if done == 0:
        log("没有新套图可下载（可能都已下载或本次抓取失败）。")
    log(f"本次结束，新下载 {done} 套。")


if __name__ == "__main__":
    main()
