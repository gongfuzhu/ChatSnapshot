"""生成九宫格封面并上传到 X（浏览器自动化方案）。

两种模式：
  1. 复用已打开的 Chrome（调试端口 9222 已在运行，且已登录 X）
  2. 启动新的带调试端口的 Chrome（独立用户数据目录，首次需手动登录）

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

from playwright.sync_api import sync_playwright

from snapshot_grid import (
    GRID,
    download_images,
    fetch_data,
    make_grid,
    pick_9_covers,
)

# 设置 UTF-8 输出
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

# ========== 配置 ==========
DEBUG_PORT = 9222
# HTTP 代理（设为空字符串则不使用代理）
PROXY = "http://127.0.0.1:7890"
# 发帖文案。可用 {username} 占位符，会被替换为第一张封面的主播用户名。
POST_TEXT = (
    "正在直播\n Live streaming now. \n ただいま配信中です。 \n"
    " https://zh.streams.modelapp.org/{username}"
)
# ========== 配置结束 ==========


def is_port_open(host, port, timeout=0.5):
    """探测 TCP 端口是否可连接。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _find_chrome():
    """自动探测 Chrome 可执行文件路径（Linux/macOS/Windows）。"""
    if sys.platform.startswith("win"):
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    else:
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]


def _get_user_data_dir():
    """获取 Chrome 用户数据目录（独立目录，与日常 Chrome 隔离）。"""
    if sys.platform.startswith("win"):
        return os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "chrome-x-debug",
        )
    elif sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~/Library/Application Support"), "chrome-x-debug"
        )
    else:
        return os.path.join(
            os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.config")),
            "chrome-x-debug",
        )


CHROME_PATH = _find_chrome()
USER_DATA_DIR = _get_user_data_dir()


def build_chrome_args(chrome_path, port, user_data_dir, proxy="", headless=False):
    """构造带远程调试端口的 Chrome 启动参数。"""
    args = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
    ]
    if proxy:
        args.append(f"--proxy-server={proxy}")
    if headless:
        args.append("--headless=new")
        args.append("--no-sandbox")
        args.append("--disable-gpu")
    return args


def launch_chrome(chrome_path=CHROME_PATH, port=DEBUG_PORT,
                  user_data_dir=USER_DATA_DIR, proxy=PROXY, wait=15.0,
                  headless=None):
    """启动带调试端口的 Chrome，轮询等待端口就绪后返回进程。

    自动设置 Wayland/X11 显示环境变量，确保在 cron/后台也能启动 GUI。
    headless 为 None 时自动检测：无显示环境则用 headless。
    """
    if not os.path.exists(chrome_path):
        raise FileNotFoundError(f"找不到 Chrome: {chrome_path}")

    # 自动判断是否用 headless
    # 优先级：CHROME_HEADLESS 环境变量 > 自动检测
    if headless is None:
        env_headless = os.environ.get("CHROME_HEADLESS", "").lower()
        if env_headless in ("1", "true", "yes"):
            headless = True
        elif env_headless in ("0", "false", "no"):
            headless = False
        else:
            # 没有显式设置时：检查是否有可用显示环境
            has_display = bool(
                os.environ.get("DISPLAY")
                or os.environ.get("WAYLAND_DISPLAY")
            )
            headless = not has_display

    # 确保显示环境变量存在（cron 中可能没有）
    env = os.environ.copy()
    if not headless and not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        # 尝试从用户运行时目录检测
        runtime_dir = env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        if os.path.exists(os.path.join(runtime_dir, "wayland-0")):
            env["WAYLAND_DISPLAY"] = "wayland-0"
            env["XDG_RUNTIME_DIR"] = runtime_dir
        elif os.path.exists("/tmp/.X11-unix/X0"):
            env["DISPLAY"] = ":0"
            env["XAUTHORITY"] = os.path.expanduser("~/.Xauthority")
        else:
            # 完全没有显示环境，强制 headless
            headless = True

    args = build_chrome_args(chrome_path, port, user_data_dir, proxy, headless)
    proc = subprocess.Popen(
        args,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if is_port_open("127.0.0.1", port):
            return proc
        time.sleep(0.3)
    proc.terminate()
    raise TimeoutError(f"Chrome 调试端口 {port} 在 {wait}s 内未就绪")


def ensure_browser(port=DEBUG_PORT):
    """端口已开则复用，否则启动新 Chrome。返回 (proc, started_by_us)。"""
    if is_port_open("127.0.0.1", port):
        print(f"[信息] 检测到调试端口 {port} 已开，复用现有浏览器")
        return None, False
    print(f"[信息] 未检测到调试端口 {port}，启动新的 Chrome")
    proc = launch_chrome(port=port)
    return proc, True


def build_username_map(data):
    """从接口数据构建 id -> username 映射。"""
    umap = {}
    for block in data.get("blocks", []):
        for model in block.get("models", []):
            umap[model["id"]] = model.get("username", "")
    return umap


def render_post_text(template, username):
    """用 username 替换文案模板里的 {username} 占位符。"""
    return template.replace("{username}", username)


def generate_grid():
    """实时生成一张九宫格图片，返回 (文件路径, 第一张封面的 username)。"""
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
    username = build_username_map(data).get(covers[0]["id"], "")
    return out_path, username


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

    # 等待媒体出现在编辑区（通过检测媒体预览元素）
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


def main():
    # 获取图片路径：指定则用指定的，否则实时生成
    if len(sys.argv) > 1:
        image_path = sys.argv[1]
        if not os.path.exists(image_path):
            print(f"[ERROR] 文件不存在: {image_path}")
            sys.exit(1)
        username = ""
    else:
        image_path, username = generate_grid()

    post_text = render_post_text(POST_TEXT, username)
    if post_text:
        print(f"[信息] 发帖文案: {post_text}")

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
            upload_media_to_x(page, image_path, post_text)
    except Exception as e:
        print(f"[ERROR] 错误: {e}", file=sys.stderr)
        print("\n请确保:")
        print(f"1. Chrome 路径正确（当前探测到: {CHROME_PATH}）")
        print("2. 已在该 Chrome 配置中登录了 X 账号")
        print(f"   （独立目录 {USER_DATA_DIR} 需手动登录一次）")
        sys.exit(1)
    finally:
        # 自己启动的浏览器发帖后关闭；复用已有的保持不动
        if started_by_us and proc is not None:
            proc.terminate()
            print("[信息] 已关闭本脚本启动的 Chrome")


if __name__ == "__main__":
    main()
