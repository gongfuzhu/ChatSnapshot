#!/usr/bin/env bash
# 启动"发布视频用浏览器"：有头 Chrome + 日本代理 + X 专用 profile + CDP 9222
# 供手动登录 X / 排查发帖使用。
set -uo pipefail

# Wayland/X 显示环境（图形界面必需）
export DISPLAY="${DISPLAY:-:0}"
if [ -z "${XAUTHORITY:-}" ]; then
  AUTH="$(ls -t /run/user/1000/.mutter-Xwaylandauth.* 2>/dev/null | head -1)"
  [ -n "$AUTH" ] && export XAUTHORITY="$AUTH"
fi

# 若已有 9222 实例在跑，直接聚焦而不是重复启动
if curl -s --max-time 2 http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
  echo "9222 调试端口已有 Chrome 运行，直接打开首页。"
  exec google-chrome \
    --remote-debugging-port=9222 \
    --user-data-dir="$HOME/.config/chrome-x-debug" \
    --proxy-server=http://127.0.0.1:7890 \
    "https://x.com/home"
fi

exec google-chrome \
  --new-window \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.config/chrome-x-debug" \
  --proxy-server=http://127.0.0.1:7890 \
  --no-first-run \
  --no-default-browser-check \
  --disable-blink-features=AutomationControlled \
  "https://x.com/home"
