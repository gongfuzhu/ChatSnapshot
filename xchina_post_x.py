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

def spread_indices(total, k):
    """在 [1, total] 内均匀选 k 个 1-based 索引（覆盖首/中/尾），升序去重。
    避免 X 四宫格总是连续取开头几张，更能代表整套。"""
    if total <= k:
        return list(range(1, total + 1))
    k = min(k, total)
    idx = []
    for i in range(k):
        # 末位锚定 total，其余按等分插值并四舍五入
        v = 1 + round(i * (total - 1) / (k - 1)) if k > 1 else 1
        idx.append(v)
    # 去重（等分点可能撞在一起），撞了就向后顺延补齐
    out = []
    for v in sorted(idx):
        while v in out and v < total:
            v += 1
        if v not in out:
            out.append(v)
    # 极端情况下补齐到 k 个
    v = 1
    while len(out) < k and v <= total:
        if v not in out:
            out.append(v)
        v += 1
    return sorted(out)[:k]


def download_originals(browser_ctx, aid, indices):
    """在 img.xchina.io 上下文内按给定 1-based 索引 fetch 原图，返回按索引升序的本地路径。
    已缓存(>100KB)的跳过下载。"""
    outdir = DL_DIR / aid
    outdir.mkdir(parents=True, exist_ok=True)
    page = browser_ctx.new_page()
    first = f"{IMG_BASE}/{aid}/{min(indices):05d}.jpg"
    page.goto(first, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)  # 让 CF 挑战（若有）完成
    paths = []
    for i in indices:
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
    # 监听发推响应：新版 X 的 GraphQL mutation 名可能不是 CreateTweet，
    # 用 graphql 路径里的操作名 + 状态一起记录，成功判定见下方。
    create_resp = {"status": None}
    gql_hits = []

    def _on_response(resp):
        try:
            u = resp.url
            if "CreateTweet" in u:
                create_resp["status"] = resp.status
            if "/graphql/" in u and resp.request.method == "POST":
                op = u.split("/graphql/")[1].split("?")[0]
                gql_hits.append((op, resp.status))
        except Exception:
            pass

    page.on("response", _on_response)
    try:
        page.goto("https://x.com/home", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        # 在进入 compose 之前关闭首页可能出现的引导/通知弹窗；
        # 打开 compose 后绝不能再点“关闭”类按钮，否则会把发帖弹窗本身关掉。
        _dismiss_x_popups(page)
        page.goto("https://x.com/compose/post")
        if "login" in page.url or "onboarding" in page.url:
            raise XLoginExpiredError(
                f"X 登录态已失效，跳转登录页: {page.url}。请通过向日葵重新登录 X。")
        # compose/post 以 dialog 弹窗呈现。页面可能同时存在一个不可见的全屏遮罩 dialog，
        # 因此统一用“可见的 dialog”，避免选到隐藏遮罩。
        vis_dialog = '[role="dialog"]:visible'
        d_editor_sel = '[role="dialog"]:visible [data-testid="tweetTextarea_0"]'
        try:
            editor = page.wait_for_selector(d_editor_sel, timeout=15000)
        except Exception:
            if "login" in page.url or "onboarding" in page.url:
                raise XLoginExpiredError(f"X 登录态失效（找不到发帖框，URL={page.url}）")
            raise
        # 可见弹窗 JS：页面可能有两个 role=dialog（一个不可见遮罩 + 真正弹窗），
        # 原生 querySelector 不支持 :visible，用尺寸/可见性手动挑出真正弹窗。
        VDLG = (
            "() => { const ds=[...document.querySelectorAll('[role=\"dialog\"]')];"
            "const ok=ds.filter(d=>{const r=d.getBoundingClientRect();"
            "return r.width>100 && r.height>100 && d.offsetParent!==null;});"
            "return ok.sort((a,b)=>b.getBoundingClientRect().height-a.getBoundingClientRect().height)[0] || null; }"
        )

        # X 的 layers 遮罩会拦截真实 click（pointer events intercepted），
        # 先关浮层，再把无文字全屏遮挡层置为 pointer-events:none，最后聚焦+点击【弹窗内】编辑框。
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
                    const dlg = (%s)();
                    const el = dlg ? dlg.querySelector('[data-testid="tweetTextarea_0"]') : null;
                    if (el) { el.focus(); el.click(); }
                }""" % VDLG
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
                    const dlg = (%s)();
                    const el = dlg ? dlg.querySelector('[data-testid="tweetTextarea_0"]') : null;
                    return el ? (el.innerText || '').trim().length : 0;
                }""" % VDLG
            )
            if not filled:
                print("[信息] 键盘输入未生效，用 JS 填入文案 ...")
                _unblock_editor()
                page.evaluate(
                    """(t) => {
                        const dlg = (%s)();
                        const el = dlg ? dlg.querySelector('[data-testid="tweetTextarea_0"]') : null;
                        if (!el) return;
                        el.focus();
                        // 模拟真实输入：写入文本并派发 input 事件让 React 识别
                        document.execCommand('insertText', false, t);
                    }""" % VDLG,
                    text,
                )

        # file input 也必须取弹窗内的（背景 home 内嵌框也有一个，传错会导致弹窗关闭）
        page.wait_for_selector('[role="dialog"]:visible input[type="file"]', timeout=10000)
        file_input = page.locator('[role="dialog"]:visible input[type="file"]').first
        abs_paths = [os.path.abspath(p) for p in image_paths]
        print("上传图片:", abs_paths)
        file_input.set_input_files(abs_paths)  # 多图一次选中

        # 等待【可见弹窗内】所有图片预览出现
        try:
            page.wait_for_function(
                """(n) => {
                    const dlg = (%s)();
                    if (!dlg) return false;
                    return dlg.querySelectorAll('[data-testid="attachments"] img').length >= n;
                }""" % VDLG,
                arg=len(abs_paths), timeout=90000)
            print("✅ 全部图片预览出现")
        except Exception:
            page.wait_for_timeout(5000)  # 预览选择器可能变动，退化为固定等待

        # 关键：预览缩略图是本地即时生成的，出现≠上传完成。媒体真正传到 X 服务器期间，
        # 每张图上会有 [role=progressbar] 上传进度条；没传完时点击发布会被前端静默忽略
        # （不发任何请求、CreateTweet 永远为 None）。必须等所有进度条消失。
        try:
            page.wait_for_function(
                """() => {
                    const dlg = (%s)();
                    if (!dlg) return false;
                    const bars = dlg.querySelectorAll('[role="progressbar"]');
                    // 同时确认图片数齐
                    const imgs = dlg.querySelectorAll('[data-testid="attachments"] img').length;
                    return bars.length === 0 && imgs >= %d;
                }""" % (VDLG, len(abs_paths)),
                timeout=180000,
            )
            print("✅ 图片全部上传完成（进度条消失）")
        except Exception:
            # 兜底：再固定等 8 秒，并检查是否仍有进度条
            page.wait_for_timeout(8000)
            still = page.evaluate(
                """() => { const dlg=(%s)(); return dlg ? dlg.querySelectorAll('[role="progressbar"]').length : -1; }""" % VDLG
            )
            print(f"[信息] 进度条等待超时，当前剩余进度条 {still}")

        page.wait_for_timeout(1500)

        first_line = text.split("\n")[0] if text else ""

        # 上传后定位【可见弹窗内】正文框
        def _dialog_editor_xy():
            return page.evaluate(r"""() => {
                const ds=[...document.querySelectorAll('[role="dialog"]')];
                const dlg=ds.filter(d=>{const r=d.getBoundingClientRect();
                    return r.width>100&&r.height>100&&d.offsetParent!==null;})
                  .sort((a,b)=>b.getBoundingClientRect().height-a.getBoundingClientRect().height)[0];
                if(!dlg) return null;
                const e=dlg.querySelector('[data-testid="tweetTextarea_0"]');
                if(!e) return null;
                const r=e.getBoundingClientRect();
                return {cx:r.x+r.width/2, cy:r.y+r.height/2, vis:r.width>0&&r.height>0};
            }""")

        def _dialog_editor_text():
            return page.evaluate(
                """() => {
                    const dlg = (%s)();
                    const e = dlg ? dlg.querySelector('[data-testid="tweetTextarea_0"]') : null;
                    return e ? e.innerText : '';
                }""" % VDLG
            )

        # 若文案在上传重渲染后丢失，重新点【弹窗内】框并输入
        if first_line and first_line not in _dialog_editor_text():
            print("[信息] 弹窗正文框缺少文案，重新输入 ...")
            xy = _dialog_editor_xy()
            if xy and xy.get("vis"):
                page.mouse.click(xy["cx"], xy["cy"])
                page.wait_for_timeout(400)
            page.keyboard.type(text, delay=15)
            page.wait_for_timeout(500)
            if first_line not in _dialog_editor_text():
                raise RuntimeError("文案无法填入弹窗正文框，中止以免发空帖")

        # 提交：点击可见弹窗内真正的发布按钮（tweetButton）。
        # 先等待该按钮可用；此时不要再 mouse.click 编辑器（可能点到弹窗外导致弹窗关闭）。
        try:
            page.wait_for_function(
                """() => {
                    const dlg = (%s)();
                    if (!dlg) return false;
                    const b = dlg.querySelector('[data-testid="tweetButton"]');
                    return b && !b.disabled && b.getAttribute('aria-disabled') !== 'true'
                           && b.offsetParent !== null;
                }""" % VDLG,
                timeout=20000,
            )
        except Exception:
            pass

        # 若可见弹窗在提交前消失（内容被丢弃到背景框），直接报错，避免发空帖
        dlg_ok = page.evaluate(
            """() => {
                const dlg = (%s)();
                return !!(dlg && dlg.querySelector('[data-testid="tweetTextarea_0"]'));
            }""" % VDLG
        )
        if not dlg_ok:
            raise RuntimeError(
                f"发帖弹窗在提交前意外关闭（url={page.url}），内容未发出。请重试。"
            )

        # 关键：输入 #话题 后会残留“话题自动补全”下拉层，会吞掉对发布按钮的点击
        # （点击被下拉消费、不触发 CreateTweet）。提交前先聚焦编辑框并按 Escape 关掉建议层。
        try:
            page.evaluate(
                """() => {
                    const dlg = (%s)();
                    const el = dlg && dlg.querySelector('[data-testid="tweetTextarea_0"]');
                    if (el) el.focus();
                }""" % VDLG
            )
            page.keyboard.press("Escape")
            page.wait_for_timeout(600)
        except Exception:
            pass

        # 先把弹窗内发布按钮滚动进可视区（4 图时弹窗很高，按钮在滚动区域外，
        # 不在视口内的 JS .click() 不会真正触发提交）。
        page.evaluate(
            """() => {
                const dlg = (%s)();
                const b = dlg && dlg.querySelector('[data-testid="tweetButton"]');
                if (b) b.scrollIntoView({block:'center'});
            }""" % VDLG
        )
        page.wait_for_timeout(500)

        # 关键：X 的全局遮罩层（css-g5y9jx / r-ipm5af 等 overlay/backdrop）可能盖在发布按钮上，
        # 吞掉一切点击（JS click / 物理 click 都不触发 CreateTweet）。
        # 用 elementFromPoint 检测按钮中心实际命中元素；若不是按钮本身（或其祖先），
        # 就把拦截链上不属于弹窗的覆盖层逐个置 pointer-events:none，直到按钮可被点中。
        for _ in range(8):
            hit = page.evaluate(
                """() => {
                    const dlg = (%s)();
                    const bt = dlg && dlg.querySelector('[data-testid="tweetButton"]');
                    if (!bt) return null;
                    const r = bt.getBoundingClientRect();
                    const x = r.x + r.width/2, y = r.y + r.height/2;
                    const e = document.elementFromPoint(x, y);
                    if (!e) return null;
                    // 命中的是按钮或按钮内部 → 已可点
                    if (e === bt || bt.contains(e)) return {ok:true};
                    // 否则沿命中元素向上，把不在弹窗内、且全屏覆盖的层禁用指针
                    let n = e, guard = 0;
                    while (n && guard < 10) {
                        if (n === document.body) break;
                        const insideDlg = dlg.contains(n);
                        const rr = n.getBoundingClientRect();
                        if (!insideDlg && rr.width >= innerWidth*0.6 && rr.height >= innerHeight*0.6) {
                            n.style.pointerEvents = 'none';
                            return {ok:false, cleared:(n.className||'').toString().slice(0,50)};
                        }
                        n = n.parentElement; guard++;
                    }
                    // 命中元素虽不满足“全屏”，但只要不在按钮内也先禁用它本身
                    if (!bt.contains(e) && !dlg.contains(e)) {
                        e.style.pointerEvents = 'none';
                        return {ok:false, cleared:'hit'};
                    }
                    return {ok:true};
                }""" % VDLG
            )
            if not hit or hit.get("ok"):
                break
            page.wait_for_timeout(200)

        clicked = ""
        # 用 Playwright 真实点击（自动滚动+派发可信事件）；force 规避 layers 遮罩的可操作性拦截。
        try:
            submit_btn = page.locator(
                '[role="dialog"]:visible [data-testid="tweetButton"]'
            ).last
            submit_btn.click(timeout=8000, force=True)
            clicked = "tweetButton"
        except Exception as _e:
            print(f"[信息] 真实点击失败({str(_e)[:60]})，退回 JS 点击 ...")
            clicked = page.evaluate(
                """() => {
                    const dlg = (%s)();
                    if (!dlg) return '';
                    const isUsable = b => b && !b.disabled
                        && b.getAttribute('aria-disabled') !== 'true'
                        && b.offsetParent !== null;
                    let btn = dlg.querySelector('[data-testid="tweetButton"]');
                    if (!isUsable(btn)) {
                        btn = [...dlg.querySelectorAll(
                            '[data-testid="tweetButton"],[data-testid="tweetButtonInline"]')]
                            .find(isUsable) || null;
                    }
                    if (isUsable(btn)) { btn.scrollIntoView({block:'center'}); btn.click(); return btn.getAttribute('data-testid'); }
                    return '';
                }""" % VDLG
            )
        if clicked:
            print(f"点击发布按钮提交（{clicked}）...")
        else:
            print("未找到可用发布按钮，Ctrl+Enter 提交 ...")
            page.keyboard.press("Control+Enter")

        # 成功判定（多信号）：
        #  A) CreateTweet / 发推类 GraphQL mutation 返回 200
        #  B) 弹窗关闭且离开 compose/post（发推成功后 X 会回到 /home）
        #  C) 成功 toast（“帖子已发布/Your post was sent”）
        # 失败信号：错误 toast。
        post_mutation_ok = {"v": False, "op": ""}
        def _check_mutation():
            for op, st in gql_hits:
                low = op.lower()
                if st == 200 and ("createtweet" in low or "create_tweet" in low):
                    post_mutation_ok["v"] = True; post_mutation_ok["op"] = op
            return post_mutation_ok["v"]

        deadline = time.monotonic() + 60
        err = ""
        while time.monotonic() < deadline:
            try:
                t = page.locator('[data-testid="toast"]').first
                if t.is_visible(timeout=300):
                    toast = t.inner_text()
                    low = toast.lower()
                    if any(k in low for k in
                           ["try again", "something went wrong",
                            "rate", "limit", "retry", "失败", "错误", "稍候", "重试",
                            "couldn't", "could not", "unable"]):
                        err = f"X 返回错误提示: {toast.strip()[:160]}"
                        break
                    if any(k in low for k in
                           ["post has been sent", "your post was sent", "帖子已发布",
                            "已发布", "已发送"]):
                        print(f"[OK] X 发帖成功（成功 toast: {toast.strip()[:40]}）")
                        return
            except Exception:
                pass
            _check_mutation()
            left_compose = "compose/post" not in page.url
            if post_mutation_ok["v"] and left_compose:
                page.wait_for_timeout(1500)
                print(f"[OK] X 浏览器发帖成功（{post_mutation_ok['op']} 200，跳转 {page.url}）")
                return
            # 兜底：弹窗消失且离开 compose（新版无 CreateTweet 命名时）
            if left_compose:
                dlg_gone = page.evaluate(
                    """() => { const ds=[...document.querySelectorAll('[role="dialog"]')];
                        return !ds.some(d=>{const r=d.getBoundingClientRect();return r.width>100&&r.height>100;}); }"""
                )
                if dlg_gone and not err:
                    page.wait_for_timeout(1500)
                    if "compose/post" not in page.url:
                        print(f"[OK] X 浏览器发帖成功（弹窗关闭并跳转 {page.url}，gql={gql_hits[-2:]}）")
                        return
            page.wait_for_timeout(700)

        shot = f"xchina_fail_{int(time.time())}.png"
        try:
            page.screenshot(path=shot)
        except Exception:
            pass
        print("[调试] 点击后 GraphQL POST 操作:", gql_hits[-12:])
        raise RuntimeError(
            err or f"未能确认发帖成功（CreateTweet={create_resp['status']}，url={page.url}）。截图: {shot}")
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

        # X 用均匀分散的 n 张（首/中/尾），TG 发整套前 tg_total 张。
        # 合并两者所需索引一次性下载（缓存复用，分散索引基本含在 TG 范围内）。
        tg_total = 0
        if not args.no_tg:
            tg_total = min(pcount or n, args.max_tg)
        total_available = pcount or max(n, tg_total)
        x_idx = spread_indices(total_available, n)
        tg_idx = list(range(1, tg_total + 1))
        all_idx = sorted(set(x_idx) | set(tg_idx))
        print(f"下载原图（共 {len(all_idx)} 张）；X 选图序号: {x_idx} ...")
        paths = download_originals(ctx, aid, all_idx)
        path_by_idx = {int(Path(p).stem): p for p in paths}
        x_paths = [path_by_idx[i] for i in x_idx if i in path_by_idx]

        caption = build_caption(chosen)
        print("文案:\n" + caption + "\n")

        if args.dry_run:
            print(f"[dry-run] 不发帖。X 用序号 {x_idx}（{len(x_paths)} 张），"
                  f"TG {'将发 ' + str(tg_total) + ' 张' if not args.no_tg else '跳过'}")
            b.close()
            return 0

        if not args.tg_only:
            post_to_x_browser(ctx, x_paths, caption)
        b.close()

    # Telegram 整套相册（Bot API，走代理，不需要浏览器）
    tg_sent = 0
    if not args.no_tg and tg_total > 0:
        from telegram_poster import send_media_group, TelegramPublishError
        tg_paths = [path_by_idx[i] for i in tg_idx if i in path_by_idx]
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
