#!/usr/bin/env bash
# ChatSnapshot 直播间视频定时发帖脚本
# 每 3 小时执行一次：选 TOP 直播间 → 录制 15 秒 → 发布到 X

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ===== 代理 =====
export https_proxy=http://127.0.0.1:7890
export http_proxy=http://127.0.0.1:7890
export ALL_PROXY=socks5://127.0.0.1:7891

# ===== Chrome 无头模式（后台运行必须开）=====
export CHROME_HEADLESS=1

# ===== Python 虚拟环境 =====
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# ===== 运行 =====
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始执行视频发帖..."
python record_stream_to_x.py 2>&1
EXIT_CODE=$?

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 执行完毕，退出码: $EXIT_CODE"
exit $EXIT_CODE
