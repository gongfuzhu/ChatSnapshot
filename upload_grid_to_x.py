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