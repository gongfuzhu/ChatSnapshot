# 九宫格自动上传到 X 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一个脚本生成直播封面九宫格图片，自动探测/启动 Chrome，并上传发布到 X (Twitter)。

**Architecture:** 复用 `snapshot_grid.py` 的图片生成函数生成一张 JPEG；用端口探测决定复用已有调试浏览器还是用 subprocess 自启真实 Chrome；通过 Playwright CDP 连接上传图片并发帖；自启的浏览器发帖后关闭，复用的保持不动。

**Tech Stack:** Python 3、Playwright（sync API）、requests、Pillow、subprocess、socket。

## Global Constraints

- 目标平台：Windows（路径、Chrome 可执行文件、`%LOCALAPPDATA%`）。
- UTF-8 输出兼容（`sys.stdout.reconfigure`）。
- 所有面向用户的提示用中文。
- 依赖限制：仅在现有 `requests` + `pillow` 上新增 `playwright`；浏览器驱动使用系统 Chrome，无需 `playwright install`。
- 配置项集中在脚本顶部：`CHROME_PATH`、`DEBUG_PORT`、`USER_DATA_DIR`、`X_COMPOSE_URL`、`POST_TEXT`。
- `USER_DATA_DIR` 默认 `%LOCALAPPDATA%\chrome-x-debug`。

---

### Task 1: 端口探测与 Chrome 启动参数（纯逻辑，可测）

**Files:**
- Create: `upload_grid_to_x.py`
- Test: `tests/test_upload_grid_to_x.py`

**Interfaces:**
- Consumes: 无。
- Produces:
  - `is_port_open(host: str, port: int, timeout: float = 0.5) -> bool`
  - `build_chrome_args(chrome_path: str, port: int, user_data_dir: str) -> list[str]`
  - 模块级常量 `DEBUG_PORT: int`、`USER_DATA_DIR: str`、`CHROME_PATH: str`、`X_COMPOSE_URL: str`、`POST_TEXT: str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_upload_grid_to_x.py
import socket
import upload_grid_to_x as ux


def test_is_port_open_false_on_unused_port():
    # 选一个几乎不可能被占用的高位端口
    assert ux.is_port_open("127.0.0.1", 59999, timeout=0.2) is False


def test_is_port_open_true_on_listening_socket():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert ux.is_port_open("127.0.0.1", port, timeout=0.5) is True
    finally:
        srv.close()


def test_build_chrome_args_contains_flags():
    args = ux.build_chrome_args(r"C:\chrome.exe", 9222, r"C:\data")
    assert args[0] == r"C:\chrome.exe"
    assert "--remote-debugging-port=9222" in args
    assert r"--user-data-dir=C:\data" in args


def test_config_constants_exist():
    assert isinstance(ux.DEBUG_PORT, int)
    assert ux.X_COMPOSE_URL.startswith("https://")
    assert isinstance(ux.POST_TEXT, str)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_upload_grid_to_x.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'upload_grid_to_x'`

- [ ] **Step 3: Write minimal implementation**

```python
# upload_grid_to_x.py
"""生成九宫格封面并自动上传到 X (Twitter)。

使用方法:
    python upload_grid_to_x.py [图片路径]
    不指定路径时会实时生成一张九宫格再上传。
"""
import os
import socket
import subprocess
import sys
import time
from datetime import datetime

# 设置 UTF-8 输出，解决 Windows 编码问题
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

# ========== 配置 ==========
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
DEBUG_PORT = 9222
# 自启 Chrome 的用户数据目录（独立目录，与日常 Chrome 隔离，首次需登录一次 X）
# 想复用日常配置免登录、可改为默认配置目录，但需先关闭所有日常 Chrome
USER_DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "chrome-x-debug"
)
X_COMPOSE_URL = "https://x.com/compose/post"
POST_TEXT = ""  # 可选文案，空则纯图片发帖
# ========== 配置结束 ==========


def is_port_open(host, port, timeout=0.5):
    """探测 TCP 端口是否可连接。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def build_chrome_args(chrome_path, port, user_data_dir):
    """构造带远程调试端口的 Chrome 启动参数。"""
    return [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_upload_grid_to_x.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add upload_grid_to_x.py tests/test_upload_grid_to_x.py
git commit -m "feat: 端口探测与 Chrome 启动参数构造"
```

---

### Task 2: Chrome 启动/连接编排

**Files:**
- Modify: `upload_grid_to_x.py`
- Test: `tests/test_upload_grid_to_x.py`

**Interfaces:**
- Consumes: `is_port_open`, `build_chrome_args`, `CHROME_PATH`, `DEBUG_PORT`, `USER_DATA_DIR`
- Produces:
  - `launch_chrome(chrome_path=CHROME_PATH, port=DEBUG_PORT, user_data_dir=USER_DATA_DIR, wait=15.0) -> subprocess.Popen`（找不到可执行文件抛 `FileNotFoundError`；端口超时抛 `TimeoutError`）
  - `ensure_browser(port=DEBUG_PORT) -> tuple[subprocess.Popen | None, bool]` 返回 `(proc, started_by_us)`；端口已开返回 `(None, False)`，否则启动并返回 `(proc, True)`

- [ ] **Step 1: Write the failing test**

```python
# 追加到 tests/test_upload_grid_to_x.py
import pytest


def test_launch_chrome_missing_executable():
    with pytest.raises(FileNotFoundError):
        ux.launch_chrome(chrome_path=r"C:\no\such\chrome.exe", port=59998, wait=1.0)


def test_ensure_browser_reuses_open_port(monkeypatch):
    monkeypatch.setattr(ux, "is_port_open", lambda *a, **k: True)
    proc, started = ux.ensure_browser(port=9222)
    assert proc is None
    assert started is False


def test_ensure_browser_launches_when_closed(monkeypatch):
    calls = {}

    def fake_launch(**kwargs):
        calls["launched"] = True
        return "FAKE_PROC"

    monkeypatch.setattr(ux, "is_port_open", lambda *a, **k: False)
    monkeypatch.setattr(ux, "launch_chrome", lambda **k: fake_launch(**k))
    proc, started = ux.ensure_browser(port=9222)
    assert proc == "FAKE_PROC"
    assert started is True
    assert calls["launched"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_upload_grid_to_x.py -v`
Expected: FAIL with `AttributeError: module 'upload_grid_to_x' has no attribute 'launch_chrome'`

- [ ] **Step 3: Write minimal implementation**

```python
# 追加到 upload_grid_to_x.py（放在 build_chrome_args 之后）
def launch_chrome(chrome_path=CHROME_PATH, port=DEBUG_PORT,
                  user_data_dir=USER_DATA_DIR, wait=15.0):
    """启动带调试端口的 Chrome，轮询等待端口就绪后返回进程。"""
    if not os.path.exists(chrome_path):
        raise FileNotFoundError(f"找不到 Chrome: {chrome_path}")
    args = build_chrome_args(chrome_path, port, user_data_dir)
    proc = subprocess.Popen(args)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if is_port_open("127.0.0.1", port):
            return proc
        time.sleep(0.3)
    raise TimeoutError(f"Chrome 调试端口 {port} 在 {wait}s 内未就绪")


def ensure_browser(port=DEBUG_PORT):
    """端口已开则复用，否则启动新 Chrome。返回 (proc, started_by_us)。"""
    if is_port_open("127.0.0.1", port):
        print(f"[信息] 检测到调试端口 {port} 已开，复用现有浏览器")
        return None, False
    print(f"[信息] 未检测到调试端口 {port}，启动新的 Chrome")
    proc = launch_chrome(port=port)
    return proc, True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_upload_grid_to_x.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: Commit**

```bash
git add upload_grid_to_x.py tests/test_upload_grid_to_x.py
git commit -m "feat: Chrome 启动与连接编排"
```

---

### Task 3: 图片生成（复用 snapshot_grid）

**Files:**
- Modify: `upload_grid_to_x.py`
- Test: `tests/test_upload_grid_to_x.py`

**Interfaces:**
- Consumes: `snapshot_grid` 的 `GRID`, `fetch_data`, `pick_9_covers`, `download_images`, `make_grid`
- Produces: `generate_grid() -> str`（生成图片文件，返回路径；无封面下载成功时 `sys.exit(1)`）

- [ ] **Step 1: Write the failing test**

```python
# 追加到 tests/test_upload_grid_to_x.py
from PIL import Image


def test_generate_grid_creates_file(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ux, "fetch_data", lambda: {"blocks": []})
    monkeypatch.setattr(ux, "pick_9_covers", lambda data, n: [{"id": 1, "url": "u"}])
    fake_img = Image.new("RGB", (320, 240), "red")
    monkeypatch.setattr(ux, "download_images", lambda covers: [fake_img])
    path = ux.generate_grid()
    assert os.path.exists(path)
    assert path.endswith(".jpg")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_upload_grid_to_x.py::test_generate_grid_creates_file -v`
Expected: FAIL with `AttributeError: module 'upload_grid_to_x' has no attribute 'generate_grid'`

- [ ] **Step 3: Write minimal implementation**

```python
# upload_grid_to_x.py 顶部 import 区追加
from snapshot_grid import (
    GRID,
    download_images,
    fetch_data,
    make_grid,
    pick_9_covers,
)

# 追加到 upload_grid_to_x.py（放在 ensure_browser 之后）
def generate_grid():
    """实时生成一张九宫格图片，返回文件路径。"""
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
    return out_path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_upload_grid_to_x.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: Commit**

```bash
git add upload_grid_to_x.py tests/test_upload_grid_to_x.py
git commit -m "feat: 复用 snapshot_grid 生成九宫格图片"
```

---

### Task 4: 上传发帖 + 主流程编排

**Files:**
- Modify: `upload_grid_to_x.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: `ensure_browser`, `generate_grid`, `X_COMPOSE_URL`, `POST_TEXT`, `DEBUG_PORT`, Playwright `sync_playwright`
- Produces:
  - `upload_image_to_x(page, image_path)`
  - `main()`

- [ ] **Step 1: 追加 playwright 依赖**

```
# requirements.txt 追加一行
playwright
```

- [ ] **Step 2: 编写上传与主流程**

说明：浏览器交互无法在无网络/无 GUI 环境稳定单元测试，采用手动集成验证（Step 3）。实现如下。

```python
# upload_grid_to_x.py 顶部 import 区追加
from playwright.sync_api import sync_playwright

# 追加到 upload_grid_to_x.py（放在 generate_grid 之后）
def upload_image_to_x(page, image_path):
    """在给定页面上传图片并发布。"""
    page.goto(X_COMPOSE_URL)
    print("已打开发帖页面")

    if POST_TEXT:
        editor = page.wait_for_selector(
            '[data-testid="tweetTextarea_0"]', timeout=10000
        )
        editor.click()
        editor.type(POST_TEXT)

    page.wait_for_selector('input[type="file"]', timeout=10000)
    file_input = page.locator('input[type="file"]').first
    file_input.set_input_files(image_path)
    print(f"图片上传中: {os.path.basename(image_path)}")

    # 图片渲染比视频快，稍等后等待发布按钮可用
    page.wait_for_timeout(3000)
    page.wait_for_selector(
        '[data-testid="tweetButton"]:not([disabled])', timeout=60000
    )
    print("图片处理完成，正在发布...")
    page.locator('[data-testid="tweetButton"]:not([disabled])').click()
    print("[OK] 发布成功!")
    page.wait_for_timeout(3000)


def main():
    # 获取图片路径：指定则用指定的，否则实时生成
    if len(sys.argv) > 1:
        image_path = sys.argv[1]
        if not os.path.exists(image_path):
            print(f"[ERROR] 文件不存在: {image_path}")
            sys.exit(1)
    else:
        image_path = generate_grid()

    proc = None
    started_by_us = False
    try:
        proc, started_by_us = ensure_browser(port=DEBUG_PORT)
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(
                f"http://127.0.0.1:{DEBUG_PORT}"
            )
            context = browser.contexts[0]
            page = context.new_page()
            upload_image_to_x(page, image_path)
    except Exception as e:
        print(f"[ERROR] 错误: {e}")
        print("\n请确保:")
        print("1. Chrome 路径正确（脚本顶部 CHROME_PATH）")
        print("2. 已在该 Chrome 配置中登录了 X 账号")
        print(f"   （首次使用独立目录 {USER_DATA_DIR} 需手动登录一次）")
        sys.exit(1)
    finally:
        # 自己启动的浏览器发帖后关闭；复用已有的保持不动
        if started_by_us and proc is not None:
            proc.terminate()
            print("[信息] 已关闭本脚本启动的 Chrome")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: 手动集成验证**

先确保没有别的 Chrome 占用/干扰，运行：

Run: `python upload_grid_to_x.py`
Expected:
- 打印生成九宫格路径
- 打印"启动新的 Chrome"或"复用现有浏览器"
- Chrome 打开发帖页并自动填图
- 打印"[OK] 发布成功!"
- 自启时最后打印"[信息] 已关闭本脚本启动的 Chrome"

首次用独立 `USER_DATA_DIR` 时若未登录，会停在登录页——手动登录一次 X 后重跑即可。

- [ ] **Step 4: 回归单元测试**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS（含 snapshot_grid 原有测试与本脚本 8 项）

- [ ] **Step 5: Commit**

```bash
git add upload_grid_to_x.py requirements.txt
git commit -m "feat: 上传九宫格图片到 X 并编排主流程"
```

---

## Self-Review

**1. Spec coverage:**
- 生成图片 → Task 3 ✅
- 端口探测决定复用/自启 → Task 1（探测）+ Task 2（编排）✅
- 自启带独立可配置 `USER_DATA_DIR` → Task 1 常量 + Task 2 `launch_chrome` ✅
- 上传发帖（可选文案、file input、tweetButton）→ Task 4 ✅
- 自启的关掉、复用的保留 → Task 4 `main` 的 `finally` ✅
- 支持命令行传图片路径 → Task 4 `main` ✅
- 错误处理与中文提示 → 各 Task ✅
- 新增 playwright 依赖 → Task 4 ✅

**2. Placeholder scan:** 无 TBD/TODO；所有代码步骤含完整代码。浏览器交互部分明确标注为手动集成验证并给出具体预期，非占位符。

**3. Type consistency:** `is_port_open`、`build_chrome_args`、`launch_chrome`、`ensure_browser`(返回 `(proc, started_by_us)`)、`generate_grid`、`upload_image_to_x(page, image_path)`、`main` 在各任务间签名一致。`started_by_us` 命名全程统一。
