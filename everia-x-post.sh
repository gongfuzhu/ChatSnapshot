#!/usr/bin/env bash
# everia 已下载图集 -> X 浏览器自动发帖（复用有头 Chrome CDP 9222 的登录态）。
# xChina 流程完全独立，本脚本只处理 everia 图集。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 代理 + 桌面显示（有头 Chrome 发帖需要）
export https_proxy=http://127.0.0.1:7890
export http_proxy=http://127.0.0.1:7890
export ALL_PROXY=socks5://127.0.0.1:7891
export DISPLAY="${DISPLAY:-:0}"
if [ -z "${XAUTHORITY:-}" ]; then
  AUTH="$(ls -t /run/user/1000/.mutter-Xwaylandauth.* 2>/dev/null | head -1)"
  [ -n "$AUTH" ] && export XAUTHORITY="$AUTH"
fi

if [ -f ".venv/bin/activate" ]; then
  source .venv/bin/activate
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] everia -> X 发帖任务开始"
# 随机延迟 0-10 分钟（SKIP_DELAY=1 跳过）
if [ -z "${SKIP_DELAY:-}" ]; then
  DELAY=$((RANDOM % 600))
  if [ "$DELAY" -gt 0 ]; then
    echo "随机延迟 ${DELAY} 秒..."
    sleep "$DELAY"
  fi
fi

python everia_post_x.py "$@"
EXIT_CODE=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 退出码: $EXIT_CODE"
exit $EXIT_CODE
