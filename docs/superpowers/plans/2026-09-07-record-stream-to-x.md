# 推荐直播间录制视频发 X 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从推荐直播间接口取观看人数最高的 public 直播间，用 ffmpeg 录制 480p 流 15 秒为 mp4，复用浏览器自动化上传视频到 X，文案「直播间：{username}」。

**Architecture:** 新建 `record_stream_to_x.py`（接口请求 + 选流 + ffmpeg 录制 + 发帖编排），并把 `upload_grid_to_x.py` 的 `upload_image_to_x` 泛化为 `upload_media_to_x`（兼容视频预览检测与更长等待）。九宫格图片业务行为不变。

**Tech Stack:** Python 3、requests、ffmpeg（系统安装）、Playwright（CDP 连接 Chrome）、pytest。

**Spec:** `docs/superpowers/specs/2026-09-07-record-stream-to-x-design.md`

## Global Constraints

- 九宫格图片业务行为不变，仅允许改 `upload_image_to_x` 函数名与内部等待参数
- 接口 URL 按用户 curl 原样保留（含 `modelsList=princiana`）
- 录制时长固定 15 秒，输出必须为 H.264/AAC mp4（`-c:v libx264 -c:a aac -movflags +faststart`，重编码而非 `-c copy`）
- 代理：`PROXY = "http://127.0.0.1:7890"`，空字符串关闭；非空时 requests 走 proxies、ffmpeg 追加 `-proxy`
- 文案精确为 `"直播间：{username}"`，`{username}` 替换为被录制主播的 `username`
- 临时 mp4 发帖后在 `finally` 中删除；脚本自己启动的 Chrome 发帖后关闭
- 新测试文件遵循现有风格：`sys.path.insert` 导入项目根、模块级常量 fixture

---

### Task 1: 泛化 `upload_media_to_x`

**Files:**
- Modify: `upload_grid_to_x.py:215-263`（函数改名与参数调整）
- Modify: `upload_grid_to_x.py:291`（main 调用点）
- Test: `tests/test_upload_grid_to_x.py`（追加测试）

**Interfaces:**
- Consumes: 无（本任务不依赖其他任务）
- Produces: `upload_media_to_x(page, media_path, post_text)` — Playwright page、媒体文件路径（图片或视频）、最终文案；后续 Task 4 的 main 直接调用

- [ ] **Step 1: 写失败测试**

在 `tests/test_upload_grid_to_x.py` 末尾追加：

```python
def test_upload_media_to_x_exists_and_old_name_gone():
    assert hasattr(ux, "upload_media_to_x")
    assert not hasattr(ux, "upload_image_to_x")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_upload_grid_to_x.py::test_upload_media_to_x_exists_and_old_name_gone -v`
Expected: FAIL（`upload_media_to_x` 不存在）

- [ ] **Step 3: 实现改名与等待参数调整**

将 `upload_grid_to_x.py` 中的 `upload_image_to_x` 整个函数替换为（签名与 3 处等待变化，其余逻辑不变）：

```python
def upload_media_to_x(page, media_path, post_text=""):
    """在给定页面上传媒体（图片或视频）并发布。post_text 为最终文案（已完成占位符替换）。"""
    page.goto("https://x.com/compose/post")
    print("已打开发帖页面")

    # 先聚焦编辑区，确保页面可交互
    editor = page.wait_for_selector(
        '[data-testid="tweetTextarea_0"]', timeout=10000
    )
    editor.click()
    if post_text:
        editor.type(post_text)

    # 上传媒体
    page.wait_for_selector('input[type="file"]', timeout=10000)
    file_input = page.locator('input[type="file"]').first

    # 用绝对路径，避免工作目录问题
    abs_path = os.path.abspath(media_path)
    print(f"上传媒体: {abs_path} ({os.path.getsize(abs_path)} 字节)")
    file_input.set_input_files(abs_path)

    # 等待媒体出现在编辑区（通过检测图片预览元素）
    print("等待媒体上传完成...")
    try:
        # X 上传完成后会有预览：图片为 img，视频为 video
        page.wait_for_selector(
            '[data-testid="attachments"] img, '
            '[data-testid="attachments"] video, '
            '[data-testid="filePreview"]',
            timeout=90000,  # 视频处理比图片慢，上限放宽
        )
        print("✅ 媒体预览出现，上传成功")
    except Exception as e:
        print(f"⚠️ 未检测到媒体预览元素: {e}")
        # 截个图看看实际情况
        debug_path = f"debug_upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        page.screenshot(path=debug_path, full_page=True)
        print(f"   已保存调试截图: {debug_path}")

    # 再多等几秒确保处理完成
    page.wait_for_timeout(3000)

    # 等待发布按钮可用（视频转码慢，上限放宽）
    page.wait_for_selector(
        '[data-testid="tweetButton"]:not([disabled])', timeout=120000
    )
    print("发布按钮可用，正在发布...")
    page.locator('[data-testid="tweetButton"]:not([disabled])').click()
    print("[OK] 发布成功!")
    page.wait_for_timeout(3000)
```

同一文件 `main()` 中调用点改名：

```python
            upload_media_to_x(page, image_path, post_text)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS（原有测试不受影响）

- [ ] **Step 5: Commit**

```bash
git add upload_grid_to_x.py tests/test_upload_grid_to_x.py
git commit -m "refactor: upload_image_to_x 泛化为 upload_media_to_x，兼容视频上传"
```

---

### Task 2: 接口数据层（fixture + fetch + 选流）

**Files:**
- Create: `tests/sample_recommended_response.json`
- Create: `record_stream_to_x.py`（本任务只创建模块与数据层函数）
- Test: `tests/test_record_stream_to_x.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `fetch_recommended() -> dict`（GET 接口返回 JSON）
  - `pick_top_streamer(models: list) -> dict | None`（取 `viewersCount` 最大且 `status == "public"`；无 public 时兜底取全量最大；空列表返回 `None`）
  - 模块常量 `API_URL`、`HEADERS`、`RECORD_SECONDS`、`PROXY`、`POST_TEXT`、`PROXIES`

- [ ] **Step 1: 创建响应样本 fixture**

创建 `tests/sample_recommended_response.json`（3 个模型：bella 观看最多但非 public，用于验证过滤；cara 是 public 中观看最多者，应被选中）：

```json
{
    "count": 3,
    "models": [
        {
            "id": 70032198,
            "username": "enya-",
            "status": "public",
            "viewersCount": 2648,
            "stream": {
                "url": "https://edge-hls.example.com/hls/70032198/master/70032198_480p.m3u8",
                "urls": {
                    "480p": "https://edge-hls.example.com/hls/70032198/master/70032198_480p.m3u8"
                }
            }
        },
        {
            "id": 70032199,
            "username": "bella-",
            "status": "private",
            "viewersCount": 9000,
            "stream": {
                "url": "https://edge-hls.example.com/hls/70032199/master/70032199_480p.m3u8",
                "urls": {
                    "480p": "https://edge-hls.example.com/hls/70032199/master/70032199_480p.m3u8"
                }
            }
        },
        {
            "id": 70032200,
            "username": "cara-",
            "status": "public",
            "viewersCount": 5000,
            "stream": {
                "url": "https://edge-hls.example.com/hls/70032200/master/70032200_480p.m3u8",
                "urls": {
                    "480p": "https://edge-hls.example.com/hls/70032200/master/70032200_480p.m3u8"
                }
            }
        }
    ]
}
```

- [ ] **Step 2: 写失败测试**

创建 `tests/test_record_stream_to_x.py`：

```python
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import record_stream_to_x as rs

_SAMPLE_PATH = os.path.join(os.path.dirname(__file__), "sample_recommended_response.json")
with open(_SAMPLE_PATH, encoding="utf-8") as f:
    SAMPLE = json.load(f)


def test_pick_top_streamer_prefers_public_max_viewers():
    model = rs.pick_top_streamer(SAMPLE["models"])
    # bella 观看数最高但是 private，应选 public 中最高的 cara
    assert model["username"] == "cara-"


def test_pick_top_streamer_fallback_when_no_public():
    models = [
        {"id": 1, "username": "a", "status": "private", "viewersCount": 100},
        {"id": 2, "username": "b", "status": "away", "viewersCount": 300},
    ]
    model = rs.pick_top_streamer(models)
    assert model["username"] == "b"


def test_pick_top_streamer_empty_returns_none():
    assert rs.pick_top_streamer([]) is None


def test_fetch_recommended_uses_headers_and_proxy(monkeypatch):
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return SAMPLE

    def fake_get(url, headers=None, proxies=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["proxies"] = proxies
        return FakeResp()

    monkeypatch.setattr(rs.requests, "get", fake_get)
    data = rs.fetch_recommended()
    assert data == SAMPLE
    assert captured["url"] == rs.API_URL
    assert captured["headers"] == rs.HEADERS
    assert captured["proxies"] == rs.PROXIES


def test_config_constants_exist():
    assert rs.RECORD_SECONDS == 15
    assert rs.POST_TEXT == "直播间：{username}"
    assert isinstance(rs.API_URL, str)
    assert "go.whitetrafsa.com/api/models" in rs.API_URL
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/test_record_stream_to_x.py -v`
Expected: FAIL（`No module named 'record_stream_to_x'`）

- [ ] **Step 4: 实现数据层**

创建 `record_stream_to_x.py`：

```python
"""推荐直播间录制 15 秒视频并发布到 X（浏览器自动化方案）。

流程：推荐直播间接口 → 选观看人数最多的 public 直播间
      → ffmpeg 录制 480p 流 15 秒 → 复用 upload_grid_to_x 浏览器编排发帖。
"""
import os
import sys

import requests

from upload_grid_to_x import render_post_text

# 设置 UTF-8 输出
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

# ========== 配置 ==========
API_URL = (
    "https://go.whitetrafsa.com/api/models?landing=Player"
    "&sortBy=paidUsers&modelsList=princiana&stripcashR=0&forceClient=1"
    "&modelPromotion=0&acclan=0&abTest=player_PlayerD20260826"
    "&abTestVariant=player_PlayerD20260826_PlayerGA_9"
    "&seenAbTest=1&seenDomain=1&seenLanding=1&limit=20"
)
HEADERS = {
    "accept": "*/*",
    "origin": "https://creative.whitetrafsa.com",
    "referer": "https://creative.whitetrafsa.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    ),
}
RECORD_SECONDS = 15
# HTTP 代理（设为空字符串则不使用代理）
PROXY = "http://127.0.0.1:7890"
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None
# 发帖文案。{username} 会被替换为被录制主播的用户名。
POST_TEXT = "直播间：{username}"
# ========== 配置结束 ==========


def fetch_recommended():
    """请求推荐直播间接口，返回 JSON。失败时报错退出。"""
    try:
        resp = requests.get(
            API_URL, headers=HEADERS, proxies=PROXIES, timeout=15
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        print(f"[错误] 请求推荐接口失败: {e}", file=sys.stderr)
        sys.exit(1)


def pick_top_streamer(models):
    """选观看人数最多且 status 为 public 的直播间；无 public 时兜底全量最大。"""
    if not models:
        return None
    public = [m for m in models if m.get("status") == "public"]
    pool = public if public else models
    return max(pool, key=lambda m: m.get("viewersCount", 0))
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_record_stream_to_x.py -v`
Expected: 5 个测试全部 PASS

- [ ] **Step 6: Commit**

```bash
git add record_stream_to_x.py tests/test_record_stream_to_x.py tests/sample_recommended_response.json
git commit -m "feat: 推荐直播间接口数据层（fetch + 选流）"
```

---

### Task 3: ffmpeg 录制

**Files:**
- Modify: `record_stream_to_x.py`（追加录制函数）
- Test: `tests/test_record_stream_to_x.py`（追加测试）

**Interfaces:**
- Consumes: 模块常量 `RECORD_SECONDS`、`PROXY`（Task 2）
- Produces:
  - `build_ffmpeg_args(stream_url: str, out_path: str, seconds: int, proxy: str = PROXY) -> list[str]`
  - `record_stream(stream_url: str, out_path: str, seconds: int = RECORD_SECONDS) -> None`（失败抛异常）
  - `extract_stream_url(model: dict) -> str`（取 `stream.urls["480p"]`，缺失时兜底 `stream.url`，均无返回 `""`）

- [ ] **Step 1: 写失败测试**

在 `tests/test_record_stream_to_x.py` 末尾追加：

```python
def test_extract_stream_url_480p():
    model = SAMPLE["models"][0]
    assert rs.extract_stream_url(model) == model["stream"]["urls"]["480p"]


def test_extract_stream_url_fallback_to_stream_url():
    model = {"stream": {"url": "https://x/master.m3u8"}}
    assert rs.extract_stream_url(model) == "https://x/master.m3u8"


def test_extract_stream_url_missing():
    assert rs.extract_stream_url({}) == ""


def test_build_ffmpeg_args_contains_required_flags():
    args = rs.build_ffmpeg_args(
        "https://x/master.m3u8", "out.mp4", 15, proxy="http://127.0.0.1:7890"
    )
    joined = " ".join(args)
    assert args[0] == "ffmpeg"
    assert "-y" in args
    assert "https://x/master.m3u8" in args
    assert "-t" in args and "15" in args
    assert "-c:v" in args and "libx264" in args
    assert "-c:a" in args and "aac" in args
    assert "+faststart" in joined
    assert "-proxy" in args and "http://127.0.0.1:7890" in args
    assert args[-1] == "out.mp4"


def test_build_ffmpeg_args_no_proxy():
    args = rs.build_ffmpeg_args("https://x/master.m3u8", "out.mp4", 15, proxy="")
    assert "-proxy" not in args


def test_record_stream_missing_ffmpeg(monkeypatch):
    monkeypatch.setattr(rs.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError):
        rs.record_stream("https://x/master.m3u8", "out.mp4")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_record_stream_to_x.py -v`
Expected: 新增 6 个测试 FAIL（函数不存在），原有 5 个 PASS

- [ ] **Step 3: 实现录制函数**

在 `record_stream_to_x.py` 中：文件顶部 import 区追加 `shutil`、`subprocess`（保持字母序：`os`、`shutil`、`subprocess`、`sys`），并在 `pick_top_streamer` 之后追加：

```python
def extract_stream_url(model):
    """取 480p 流地址，缺失时兜底 stream.url，均无返回空字符串。"""
    stream = model.get("stream", {})
    return stream.get("urls", {}).get("480p", "") or stream.get("url", "")


def build_ffmpeg_args(stream_url, out_path, seconds, proxy=PROXY):
    """构造 ffmpeg 拉流录制命令。重编码为 H.264/AAC，满足 X 上传要求。"""
    args = [
        "ffmpeg", "-y",
        "-headers", "Referer: https://creative.whitetrafsa.com/\r\n",
    ]
    if proxy:
        args += ["-proxy", proxy]
    args += [
        "-i", stream_url,
        "-t", str(seconds),
        "-c:v", "libx264", "-c:a", "aac",
        "-movflags", "+faststart",
        out_path,
    ]
    return args


def record_stream(stream_url, out_path, seconds=RECORD_SECONDS):
    """调用 ffmpeg 录制 HLS 流为 mp4。ffmpeg 缺失抛 FileNotFoundError，录制失败抛 RuntimeError。"""
    if not shutil.which("ffmpeg"):
        raise FileNotFoundError(
            "未找到 ffmpeg，请先安装（如 winget install ffmpeg 或 apt install ffmpeg）"
        )
    result = subprocess.run(
        build_ffmpeg_args(stream_url, out_path, seconds),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 录制失败: {result.stderr[-500:]}")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add record_stream_to_x.py tests/test_record_stream_to_x.py
git commit -m "feat: ffmpeg 录制 480p 直播流 15 秒为 H.264/AAC mp4"
```

---

### Task 4: 主流程编排

**Files:**
- Modify: `record_stream_to_x.py`（追加 `generate_video` 与 `main`）
- Test: `tests/test_record_stream_to_x.py`（追加测试）

**Interfaces:**
- Consumes:
  - Task 2: `fetch_recommended()`、`pick_top_streamer(models)`
  - Task 3: `extract_stream_url(model)`、`record_stream(stream_url, out_path, seconds)`
  - `upload_grid_to_x`（Task 1 + 现有）: `ensure_browser(port) -> (proc, started_by_us)`、`upload_media_to_x(page, media_path, post_text)`、`render_post_text(template, username)`、`DEBUG_PORT`
- Produces: 可直接运行的命令行脚本 `python record_stream_to_x.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_record_stream_to_x.py` 末尾追加：

```python
def test_generate_video_picks_records_and_returns(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rs, "fetch_recommended", lambda: SAMPLE)
    monkeypatch.setattr(rs, "record_stream", lambda url, path, seconds: None)
    path, username, stream_url = rs.generate_video()
    assert username == "cara-"
    assert stream_url == SAMPLE["models"][2]["stream"]["urls"]["480p"]
    assert path.endswith(".mp4")
    assert os.path.exists(path)


def test_generate_video_no_models_exits(monkeypatch):
    monkeypatch.setattr(rs, "fetch_recommended", lambda: {"models": []})
    with pytest.raises(SystemExit):
        rs.generate_video()


def test_generate_video_no_stream_url_exits(monkeypatch):
    data = {"models": [{"id": 1, "username": "a", "status": "public", "viewersCount": 1}]}
    monkeypatch.setattr(rs, "fetch_recommended", lambda: data)
    with pytest.raises(SystemExit):
        rs.generate_video()


def test_post_text_render():
    assert rs.render_post_text(rs.POST_TEXT, "enya-") == "直播间：enya-"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_record_stream_to_x.py -v`
Expected: 新增 4 个测试 FAIL（`generate_video` 不存在；`test_post_text_render` 应直接 PASS，因 `render_post_text` 来自 Task 1 前的现有代码）

- [ ] **Step 3: 实现主流程**

在 `record_stream_to_x.py` 中：顶部 import 区改为：

```python
import os
import shutil
import subprocess
import sys
from datetime import datetime

import requests

from upload_grid_to_x import (
    DEBUG_PORT,
    ensure_browser,
    render_post_text,
    upload_media_to_x,
)
```

文件末尾追加：

```python
def generate_video():
    """选流并录制，返回 (视频路径, username, stream_url)。数据不足时报错退出。"""
    data = fetch_recommended()
    model = pick_top_streamer(data.get("models", []))
    if model is None:
        print("[错误] 推荐接口没有可用直播间，退出。", file=sys.stderr)
        sys.exit(1)
    stream_url = extract_stream_url(model)
    if not stream_url:
        print("[错误] 直播间没有可用的 480p 流地址，退出。", file=sys.stderr)
        sys.exit(1)
    username = model.get("username", "")
    out_path = datetime.now().strftime("stream_%Y%m%d_%H%M%S.mp4")
    print(f"[信息] 选定直播间: {username}（观看 {model.get('viewersCount', 0)}）")
    record_stream(stream_url, out_path, RECORD_SECONDS)
    print(f"[完成] 已录制视频: {out_path}")
    return out_path, username, stream_url


def main():
    video_path, username, _ = generate_video()
    post_text = render_post_text(POST_TEXT, username)
    print(f"[信息] 发帖文案: {post_text}")

    proc = None
    started_by_us = False
    try:
        proc, started_by_us = ensure_browser(port=DEBUG_PORT)
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(
                f"http://127.0.0.1:{DEBUG_PORT}"
            )
            context = browser.contexts[0]
            page = context.new_page()
            upload_media_to_x(page, video_path, post_text)
    except SystemExit:
        raise
    except Exception as e:
        print(f"[ERROR] 错误: {e}", file=sys.stderr)
        print("\n请确保:")
        print("1. 系统已安装 ffmpeg 并在 PATH 中")
        print("2. Chrome 调试端口可用，且已登录 X 账号")
        sys.exit(1)
    finally:
        # 临时视频用完即删
        if os.path.exists(video_path):
            os.remove(video_path)
            print(f"[信息] 已删除临时视频: {video_path}")
        # 自己启动的浏览器发帖后关闭；复用已有的保持不动
        if started_by_us and proc is not None:
            proc.terminate()
            print("[信息] 已关闭本脚本启动的 Chrome")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add record_stream_to_x.py tests/test_record_stream_to_x.py
git commit -m "feat: 主流程编排——选流录制发帖，临时视频与浏览器清理"
```

---

### Task 5: 手动端到端验证（集成项，不自动化）

**Files:**
- 无代码改动；发现问题时修复后重跑 Task 1-4 的测试

**Interfaces:**
- Consumes: Task 1-4 的全部产物
- Produces: 真实环境验证记录

- [ ] **Step 1: 环境检查**

Run: `ffmpeg -version`
Expected: 输出版本信息。若无，提示用户 `winget install ffmpeg` 后重试。

- [ ] **Step 2: 干跑选流（不发帖）**

Run: `python -c "import record_stream_to_x as rs; d = rs.fetch_recommended(); m = rs.pick_top_streamer(d.get('models', [])); print(m['username'], m['viewersCount'], rs.extract_stream_url(m))"`
Expected: 打印真实 username、观看数与 480p m3u8 地址。

- [ ] **Step 3: 单独验证录制**

用上一步输出的 m3u8 地址：

Run: `python -c "import record_stream_to_x as rs; rs.record_stream('<m3u8地址>', 'test_record.mp4')"` 然后 `ffprobe -v error -show_streams -select_streams v test_record.mp4` 检查编码为 h264，时长约 15 秒。

- [ ] **Step 4: 完整发帖验证**

前置条件：本地代理 7890 已运行；Chrome 调试端口 9222 已登录 X（或允许脚本启动新 Chrome 并手动登录）。

Run: `python record_stream_to_x.py`
Expected: 依次输出「选定直播间 → 已录制视频 → 发帖文案 → 上传媒体 → 媒体预览出现 → 发布成功 → 已删除临时视频」；X 主页可见带视频的新帖，文案为「直播间：{username}」。

- [ ] **Step 5: 收尾**

验证通过后删除 `test_record.mp4`。若端到端有修改代码，重跑 `python -m pytest tests/ -v` 确认全绿后 commit。
