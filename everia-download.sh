#!/usr/bin/env bash
# everia.club 套图下载（纯 requests，无需浏览器/桌面）。
# 用法：
#   bash everia-download.sh                 # 下载最新 1 套未下载的（webp 原图）
#   bash everia-download.sh --limit 3       # 一次 3 套
#   bash everia-download.sh --to-jpg        # 转 jpg
#   bash everia-download.sh --url <文章URL>  # 指定套图
# 去重：everia_posted.json；输出：everia_downloads/（已 Samba 共享 [everia]）
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 走 mihomo 代理（站点与图床均需）
export https_proxy=http://127.0.0.1:7890
export http_proxy=http://127.0.0.1:7890
export ALL_PROXY=socks5://127.0.0.1:7891

if [ -f ".venv/bin/activate" ]; then
  source .venv/bin/activate
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] everia 下载任务开始"
# 随机延迟 0-10 分钟（SKIP_DELAY=1 跳过）
if [ -z "${SKIP_DELAY:-}" ]; then
  DELAY=$((RANDOM % 600))
  if [ "$DELAY" -gt 0 ]; then
    echo "随机延迟 ${DELAY} 秒..."
    sleep "$DELAY"
  fi
fi

python everia_download.py "$@"
EXIT_CODE=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 退出码: $EXIT_CODE"
exit $EXIT_CODE
