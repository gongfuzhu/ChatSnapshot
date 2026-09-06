#!/usr/bin/env bash
# ChatSnapshot 九宫格每日发帖脚本
# 由 cron 定时调用：每天早上 9:00

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ===== 注意：不要在这里设置 DISPLAY/WAYLAND_DISPLAY =====
# Python 脚本会自动检测显示环境，没有可用显示时自动切换 headless 模式。
# 如果在这里强制设置 Wayland，会导致 Python 误以为有 GUI 可用而不启用 headless，
# 但实际上 cron 环境下 Wayland 不可用，最终 Chrome 启动失败。

# ===== 代理 =====
export https_proxy=http://127.0.0.1:7890
export http_proxy=http://127.0.0.1:7890
export ALL_PROXY=socks5://127.0.0.1:7891

# ===== Chrome 无头模式（cron 后台运行必须开）=====
export CHROME_HEADLESS=1

# ===== Python 虚拟环境 =====
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# ===== 运行 =====
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始执行..."
python upload_grid_to_x.py 2>&1
EXIT_CODE=$?

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 执行完毕，退出码: $EXIT_CODE"
exit $EXIT_CODE
