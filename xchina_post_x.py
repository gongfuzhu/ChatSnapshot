#!/usr/bin/env python3
"""xChina 每日套图 -> X 自动发帖。

流程：
1. 连接有头 Chrome（CDP 9222，持久 profile 已过 Cloudflare 盾 + 日本代理）；
   没在跑就用 Wayland/X11 显示环境自动拉起。
2. 打开列表页 https://xchina.co/photos （按发布时间降序），取最新一套
   未发过的图集（posted.json 去重）。
3. 在 img.xchina.io 页面上下文里 fetch 前 N 张原图（00001.jpg …，真实浏览器
   指纹 + cookie 才能过 CF；curl/headless 均 403）。
4. 复用 x_api.upload_media / post_tweet 发 X（一条最多 4 图）。

用法：
  python xchina_post_x.py                 # 正常跑：取最新未发图集并发 X
  python xchina_post_x.py --dry-run       # 只选图+下载，不发帖
  python xchina_post_x.py --album 6acf..  # 指定图集 ID
  python xchina_post_x.py --n 4
"""
import argparse
import base64
import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

from upload_grid_to_x import XLoginExpiredError, _dismiss_x_popups

CDP = "http://127.0.0.1:9222"
PROFILE_DIR = str(Path.home() / ".config/chrome-x-debug")
PROXY = "http://127.0.0.1:7890"
LIST_URL = "https://xchina.co/photos"
IMG_BASE = "https://img.xchina.io/photos"
HERE = Path(__file__).resolve().parent
POSTED_LOG = HERE / "xchina_posted.json"
DL_DIR = HERE / "xchina_downloads"

# ---------- Chrome 生命周期 ----------

def cdp_alive():
    try:
        r = requests.get(f"{CDP}/json/version", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def launch_chrome():
    """在桌面会话上拉起有头 Chrome（cron 下需要显式 DISPLAY/XAUTHORITY）。"""
    env = os.environ.copy()
    if not env.get("DISPLAY"):
        env["DISPLAY"] = ":0"
    if not env.get("XAUTHORITY"):
        cands = sorted(glob.glob("/run/user/1000/.mutter-Xwaylandauth.*"))
        if cands:
            env["XAUTHORITY"] = cands[-1]
    cmd = [
        "/usr/bin/google-chrome",
        "--new-window",
        f"--remote-debugging-port=9222",
        f"--user-data-dir={PROFILE_DIR}",
        f"--proxy-server={PROXY}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(30):
        if cdp_alive():
            return
        time.sleep(1)
    raise RuntimeError("Chrome 启动后 CDP 9222 仍不可用（检查桌面会话/显示环境）")


# ---------- 选图集 ----------

def pick_albums(page, limit=12):
    page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)
    # 列表懒加载，等一下
    try:
        page.wait_for_selector(f'a[href^="/photo/"]', timeout=20000)
    except Exception:
        pass
    page.wait_for_timeout(3000)
    rows = page.evaluate(
        """() => [...document.querySelectorAll('a[href^="/photo/"]')]
           .filter(a => !/\\/photo\\/[0-9a-f]+\\/\\d+/.test(a.getAttribute('href')))
           .map(a => {
             const href = a.getAttribute('href');
             const img = a.querySelector('img');
             return {href, text: (a.innerText||'').trim(), thumb: img ? img.src : ''};
           })"""
    )
    albums = []
    seen = set()
    for r in rows:
        m = re.match(r"/photo/([0-9a-f]+)$", r["href"])
        if not m:
            continue
        aid = m.group(1)
        if aid in seen:
            continue
        seen.add(aid)
        lines = [x.strip() for x in r["text"].splitlines() if x.strip()]
        # 形如：[系列, "499P + 6V" 或 标题, 模特/日期 ...]
        series = lines[0] if lines else ""
        pcount = None
        title = ""
        model = ""
        for ln in lines:
            mm = re.match(r"(\d+)P", ln)
            if mm and pcount is None:
                pcount = int(mm.group(1))
        # 标题/模特：去掉系列行和张数行后拼接
        rest = [ln for ln in lines[1:] if not re.match(r"\d+P", ln)]
        if rest:
            title = rest[0]
            model = rest[-1] if len(rest) > 1 else rest[0]
        albums.append({
            "id": aid, "series": series, "pcount": pcount,
            "title": title, "model": model, "thumb": r["thumb"],
        })
    return albums[:limit]


def album_meta(page, aid):
    """进图集页拿准确标题与张数。"""
    page.goto(f"https://xchina.co/photo/{aid}", wait_until="domcontentloaded",
              timeout=60000)
    page.wait_for_timeout(2500)
    title = page.title().split(" - ")[0].strip()
    pcount = None
    try:
        txt = page.evaluate("document.body.innerText")
        m = re.search(r"(\d+)P", txt)
        if m:
            pcount = int(m.group(1))
    except Exception:
        pass
    return title, pcount


# ---------- 下载原图 ----------

def download_originals(browser_ctx, aid, n):
    """在 img.xchina.io 上下文内 fetch 前 n 张原图，返回本地路径列表。"""
    outdir = DL_DIR / aid
    outdir.mkdir(parents=True, exist_ok=True)
    page = browser_ctx.new_page()
    first = f"{IMG_BASE}/{aid}/00001.jpg"
    page.goto(first, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)  # 让 CF 挑战（若有）完成
    paths = []
    for i in range(1, n + 1):
        url = f"{IMG_BASE}/{aid}/{i:05d}.jpg"
        local = outdir / f"{i:05d}.jpg"
        if local.exists() and local.stat().st_size > 100_000:
            paths.append(str(local)); continue
        res = None
        for attempt in range(4):
            res = page.evaluate(
                """async ({u, ref}) => {
                  try {
                    const r = await fetch(u, {credentials:'include',
                      headers:{'Referer': ref, 'Accept':'image/avif,image/webp,*/*'}});
                    if (r.status !== 200) return {st:r.status};
                    const b = await r.arrayBuffer();
                    const bin = new Uint8Array(b);
                    let s=''; for(let k=0;k<bin.length;k++) s+=String.fromCharCode(bin[k]);
                    return {st:200, n:b.byteLength, b64:btoa(s)};
                  } catch(e) { return {st:0, err:String(e)}; }
                }""", {"u": url, "ref": f"https://xchina.co/photo/{aid}"})
            if res.get("st") == 200:
                break
            wait = 2 + attempt * 3
            print(f"  重试 {i:02d} (HTTP {res.get('st')})，{wait}s 后重试")
            time.sleep(wait)
        if not res or res.get("st") != 200:
            raise RuntimeError(f"原图 {url} 下载失败 HTTP {res.get('st') if res else '?'}（CF 盾/登录态？）")
        data = base64.b64decode(res["b64"])
        if data[:3] != b"\xff\xd8\xff":
            raise RuntimeError(f"{url} 返回的不是 JPEG（{len(data)} 字节）")
        local.write_bytes(data)
        paths.append(str(local))
        print(f"  下载 {i:02d}: {len(data)//1024}KB")
        time.sleep(1.5)  # 避免触发 CF 速率限制
    page.close()
    return paths


# ---------- 浏览器发帖（复用已登录 X 的有头 Chrome，不走官方 API） ----------

def post_to_x_browser(ctx, image_paths, text):
    page = ctx.new_page()
    try:
        page.goto("https://x.com/home", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        page.goto("https://x.com/compose/post")
        page.wait_for_timeout(2000)
        if "login" in page.url or "onboarding" in page.url:
            raise XLoginExpiredError(
                f"X 登录态已失效，跳转登录页: {page.url}。请通过向日葵重新登录 X。")
        _dismiss_x_popups(page)
        try:
            editor = page.wait_for_selector('[data-testid="tweetTextarea_0"]', timeout=10000)
        except Exception:
            if "login" in page.url or "onboarding" in page.url:
                raise XLoginExpiredError(f"X 登录态失效（找不到发帖框，URL={page.url}）")
            raise
        # X 的 layers 遮罩会拦截真实 click（pointer events intercepted），
        # 先关浮层，再把无文字全屏遮挡层置为 pointer-events:none，最后聚焦+点击编辑框。
        def _unblock_editor():
            page.evaluate(
                """() => {
                    const layers = document.querySelector('#layers');
                    if (layers) {
                        layers.querySelectorAll('div').forEach(d => {
                            const r = d.getBoundingClientRect();
                            if (r.width >= innerWidth * 0.9 && r.height >= innerHeight * 0.9
                                && (d.innerText || '').trim().length < 40) {
                                d.style.pointerEvents = 'none';
                            }
                        });
                    }
                    const el = document.querySelector('[data-testid="tweetTextarea_0"]');
                    if (el) { el.focus(); el.click(); }
                }"""
            )

        try:
            editor.click(timeout=8000)
        except Exception:
            print("[信息] 编辑区点击被遮挡，关闭浮层后聚焦 ...")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            page.wait_for_timeout(500)
            _unblock_editor()
        if text:
            # 聚焦后再输入；若编辑器仍为空（键盘事件被遮罩吞掉），用 JS 兜底填入
            page.keyboard.type(text, delay=15)
            page.wait_for_timeout(300)
            filled = page.evaluate(
                """() => {
                    const el = document.querySelector('[data-testid="tweetTextarea_0"]');
                    return el ? (el.innerText || '').trim().length : 0;
                }"""
            )
            if not filled:
                print("[信息] 键盘输入未生效，用 JS 填入文案 ...")
                _unblock_editor()
                page.evaluate(
                    """(t) => {
                        const el = document.querySelector('[data-testid="tweetTextarea_0"]');
                        if (!el) return;
                        el.focus();
                        // 模拟真实输入：写入文本并派发 input 事件让 React 识别
                        document.execCommand('insertText', false, t);
                    }""",
                    text,
                )

        page.wait_for_selector('input[type="file"]', timeout=10000)
        file_input = page.locator('input[type="file"]').first
        abs_paths = [os.path.abspath(p) for p in image_paths]
        print("上传图片:", abs_paths)
        file_input.set_input_files(abs_paths)  # 多图一次选中

        # 等待所有图片预览出现
        try:
            page.wait_for_function(
                "(n) => document.querySelectorAll('[data-testid=\"attachments\"] img').length >= n",
                arg=len(abs_paths), timeout=90000)
            print("✅ 全部图片预览出现")
        except Exception:
            page.wait_for_timeout(5000)  # 预览选择器可能变动，退化为固定等待

        page.wait_for_timeout(2000)
        selectors = [
            '[data-testid="tweetButtonInline"]:not([disabled])',
            '[data-testid="tweetButton"]:not([disabled])',
            'button[data-testid^="tweetButton"]:not([disabled])',
        ]
        for i, sel in enumerate(selectors):
            try:
                page.wait_for_selector(
                    sel, timeout=5000 if i < len(selectors) - 1 else 60000)
                print(f"发布按钮可用（选择器 {i}），发布中 ...")
                page.evaluate(f'''() => {{
                    const btn = document.querySelector('{sel.split(':not')[0]}');
                    if (btn && !btn.disabled) btn.click();
                }}''')
                break
            except Exception:
                if i == len(selectors) - 1:
                    raise

        # 点击后轮询：成功 = 离开 compose/post 页（跳回 /home）；
        # 失败 = 出现错误 toast。注意：/home 顶部自带内嵌快捷发帖框(tweetTextarea_0)，
        # 不能用“编辑器是否存在”判断弹窗是否关闭。
        deadline = time.monotonic() + 25
        err = ""
        while time.monotonic() < deadline:
            try:
                t = page.locator('[data-testid="toast"]').first
                if t.is_visible(timeout=300):
                    toast = t.inner_text()
                    low = toast.lower()
                    if any(k in low for k in
                           ["try again", "error", "something went wrong",
                            "rate", "limit", "retry", "失败", "错误"]):
                        err = f"X 返回错误提示: {toast.strip()[:160]}"
                        break
            except Exception:
                pass
            if "compose/post" not in page.url:
                page.wait_for_timeout(2000)  # 等新推落位
                if "compose/post" not in page.url:
                    print(f"[OK] X 浏览器发帖成功（跳转至 {page.url}）")
                    return
            page.wait_for_timeout(800)

        shot = f"xchina_fail_{int(time.time())}.png"
        try:
            page.screenshot(path=shot)
        except Exception:
            pass
        raise RuntimeError(err or f"未能确认发帖成功（仍停留在 {page.url}）。截图: {shot}")
    finally:
        page.close()


# ---------- 去重记录 ----------

def load_posted():
    if POSTED_LOG.exists():
        try:
            return json.loads(POSTED_LOG.read_text())
        except Exception:
            return {}
    return {}


def save_posted(log):
    POSTED_LOG.write_text(json.dumps(log, ensure_ascii=False, indent=2))


# ---------- 主流程 ----------

def build_caption(meta):
    series = meta.get("series") or ""
    title = meta.get("title") or ""
    model = meta.get("model") or ""
    parts = []
    head = title or model or series
    if head:
        parts.append(head)
    sub_parts = []
    if series and series != head:
        sub_parts.append(series)
    if model and model != head:
        sub_parts.append(model)
    sub = " / ".join(sub_parts)
    if sub:
        parts.append(sub)
    parts.append("#写真 #美女 #套图")
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=4, help="发 X 的张数（最多 4）")
    ap.add_argument("--album", default="", help="指定图集 ID")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-tg", action="store_true", help="只发 X，不发 Telegram 整套")
    ap.add_argument("--tg-only", action="store_true", help="只发 Telegram 整套，不发 X")
    ap.add_argument("--max-tg", type=int, default=600, help="TG 单套最多发送张数保护")
    args = ap.parse_args()
    n = min(max(args.n, 1), 4)

    if not cdp_alive():
        print("CDP 未就绪，启动有头 Chrome ...", flush=True)
        launch_chrome()
        time.sleep(3)

    posted = load_posted()
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(CDP)
        ctx = b.contexts[0]
        lpage = ctx.new_page()

        if args.album:
            candidates = [{"id": args.album, "series": "", "pcount": None,
                           "title": "", "model": "", "thumb": ""}]
        else:
            candidates = pick_albums(lpage)
            print(f"列表取到 {len(candidates)} 套候选")

        chosen = None
        for c in candidates:
            if c["id"] in posted:
                print(f"  跳过已发: {c['id']} {c.get('title') or c.get('series')}")
                continue
            chosen = c
            break
        if not chosen:
            print("没有未发的新图集，本次跳过。")
            lpage.close(); b.close()
            return 2

        aid = chosen["id"]
        title, pcount = album_meta(lpage, aid)
        if title:
            chosen["title"] = title
        if pcount:
            chosen["pcount"] = pcount
        print(f"选中图集 {aid}: {chosen.get('series')} | {title} | {pcount}P")
        lpage.close()

        # X 用前 n 张；TG 发整套。一次下载到目标数量（缓存复用，前 n 张含在内）。
        tg_total = 0
        if not args.no_tg:
            tg_total = min(pcount or n, args.max_tg)
        total_dl = max(n, tg_total if not args.no_tg else n)
        print(f"下载原图（共 {total_dl} 张）...")
        paths = download_originals(ctx, aid, total_dl)

        caption = build_caption(chosen)
        print("文案:\n" + caption + "\n")

        if args.dry_run:
            print(f"[dry-run] 不发帖。X 用 {len(paths[:n])} 张，"
                  f"TG {'将发 ' + str(tg_total) + ' 张' if not args.no_tg else '跳过'}")
            b.close()
            return 0

        if not args.tg_only:
            post_to_x_browser(ctx, paths[:n], caption)
        b.close()

    # Telegram 整套相册（Bot API，走代理，不需要浏览器）
    tg_sent = 0
    if not args.no_tg and tg_total > 0:
        from telegram_poster import send_media_group, TelegramPublishError
        tg_paths = paths[:tg_total]
        print(f"发送 Telegram 整套相册：{len(tg_paths)} 张 ...")
        try:
            res = send_media_group(tg_paths, caption=caption)
            tg_sent = sum(len(r) for r in res)
            print(f"[OK] Telegram 共 {len(res)} 批相册发送完成")
        except TelegramPublishError as e:
            print(f"[错误] Telegram 发布失败: {e}", file=sys.stderr)
            # X 已成功，不当整体失败处理，但记录告警；抛出以便 cron 发告警
            raise

    posted[aid] = {
        "title": chosen.get("title"), "series": chosen.get("series"),
        "model": chosen.get("model"), "pcount": chosen.get("pcount"),
        "ts": int(time.time()),
        "x_sent": not args.tg_only,
        "tg_sent_count": tg_sent,
        "images": [Path(x).name for x in paths],
    }
    save_posted(posted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
