#!/usr/bin/env bash
# xChina 套图 -> X 浏览器自动发帖（每天 3 次，由 cron 调用）
# 不走 X 官方 API；复用已过 Cloudflare 盾 + 已登录 X 的有头 Chrome（CDP 9222）。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 代理（Chrome 与浏览器内请求走 mihomo；X API 已不使用）
export https_proxy=http://127.0.0.1:7890
export http_proxy=http://127.0.0.1:7890
export ALL_PROXY=socks5://127.0.0.1:7891

# 必须有桌面显示（有头 Chrome 才能过 CF 盾）。给 cron 补上显示环境。
export DISPLAY="${DISPLAY:-:0}"
if [ -z "${XAUTHORITY:-}" ]; then
  AUTH="$(ls -t /run/user/1000/.mutter-Xwaylandauth.* 2>/dev/null | head -1)"
  [ -n "$AUTH" ] && export XAUTHORITY="$AUTH"
fi

if [ -f ".venv/bin/activate" ]; then
  source .venv/bin/activate
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] xChina 发帖任务开始"
# 随机延迟 0-10 分钟，避免时间太机械（SKIP_DELAY=1 跳过）
if [ -z "${SKIP_DELAY:-}" ]; then
  DELAY=$((RANDOM % 600))
  if [ "$DELAY" -gt 0 ]; then
    echo "随机延迟 ${DELAY} 秒..."
    sleep "$DELAY"
  fi
fi

python xchina_post_x.py --n 4 2>&1
EXIT_CODE=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 退出码: $EXIT_CODE"
exit $EXIT_CODE
