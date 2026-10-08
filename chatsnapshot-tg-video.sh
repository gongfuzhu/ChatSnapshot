#!/usr/bin/env bash
# ChatSnapshot Telegram 视频（直播录制）发布
# 内容与 X 一致，直接走 Telegram Bot HTTP API。
set -euo pipefail

# ===== 进入脚本所在目录（项目根）=====
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ===== 激活虚拟环境 =====
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
else
  echo "[错误] 未找到 .venv，请先创建并安装依赖。"
  exit 1
fi

# ===== 运行 =====
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始执行 Telegram 视频发布..."
# 随机延迟 0-15 分钟，避免发布时间机械（手动测试设 SKIP_DELAY=1 跳过）
if [ -z "${SKIP_DELAY:-}" ]; then
  DELAY=$((RANDOM % 900))
  if [ "$DELAY" -gt 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 随机延迟 ${DELAY} 秒后发布..."
    sleep "$DELAY"
  fi
fi
python post_video_to_telegram.py 2>&1
EXIT_CODE=$?

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 执行完毕，退出码: $EXIT_CODE"
exit $EXIT_CODE
