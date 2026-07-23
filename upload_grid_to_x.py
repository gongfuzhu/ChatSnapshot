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
from snapshot_grid import (
    GRID,
    download_images,
    fetch_data,
    make_grid,
    pick_9_covers,
)

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


from playwright.sync_api import sync_playwright


def upload_image_to_x(page, image_path):
    """在给定页面上传图片并发布。"""
    page.goto(X_COMPOSE_URL)
    print("已打开发帖页面")

    # 先等编辑区就绪并聚焦，确保页面可交互（即使不填文案）
    editor = page.wait_for_selector(
        '[data-testid="tweetTextarea_0"]', timeout=10000
    )
    editor.click()
    if POST_TEXT:
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