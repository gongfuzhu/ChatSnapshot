#!/usr/bin/env bash
# 启动"X 第二个账号浏览器"：独立 profile + 日本代理，手动登录另一个 X 账号。
# 与发帖自动化(chrome-x-debug / 9222)完全隔离：不同 user-data-dir、不开调试端口。
set -uo pipefail

# Wayland/X 显示环境（图形界面必需）
export DISPLAY="${DISPLAY:-:0}"
if [ -z "${XAUTHORITY:-}" ]; then
  AUTH="$(ls -t /run/user/1000/.mutter-Xwaylandauth.* 2>/dev/null | head -1)"
  [ -n "$AUTH" ] && export XAUTHORITY="$AUTH"
fi

exec google-chrome \
  --new-window \
  --user-data-dir="$HOME/.config/chrome-x-account2" \
  --proxy-server=http://127.0.0.1:7890 \
  --no-first-run \
  --no-default-browser-check \
  --disable-blink-features=AutomationControlled \
  "https://x.com/login"
