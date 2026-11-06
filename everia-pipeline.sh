#!/usr/bin/env bash
# everia 一体化任务：下载一套最新图集 ->（自动出 PDF）-> 发 X 4 张 -> PDF 发 Telegram。
# 各环节各自去重，重复运行安全：
#   - everia_download.py：已下载的套图会跳过（everia_posted.json）
#   - everia_post_x.py ：已发过 X 的套图会跳过（everia_x_posted.json）
# 退出码：0 全部成功或无新内容；非 0 有环节失败（供 cron 告警）。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 代理 + 桌面显示（X 发帖需要有头 Chrome）
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

echo "[$(date '+%Y-%m-%d %H:%M:%S')] everia 一体化任务开始（下载→PDF→X→TG）"

# 随机延迟 0-10 分钟（SKIP_DELAY=1 跳过）
if [ -z "${SKIP_DELAY:-}" ]; then
  DELAY=$((RANDOM % 600))
  if [ "$DELAY" -gt 0 ]; then
    echo "随机延迟 ${DELAY} 秒..."
    sleep "$DELAY"
  fi
fi

# 1) 下载最新 1 套并生成 PDF（已下载会自动跳过）
python everia_download.py --pages 1 --limit 1 --pdf
DL_RC=$?
if [ "$DL_RC" -ne 0 ]; then
  echo "[错误] 下载/PDF 环节失败（退出码 $DL_RC），终止本次任务。"
  exit "$DL_RC"
fi

# 2) 选一套未发过 X 的图集发 X，并把 PDF 发到 Telegram
python everia_post_x.py --n 4 --tg-pdf
POST_RC=$?
# 退出码 2 = 没有可发的新图集（属正常，不算失败）；3 = X 成功但 TG 失败
if [ "$POST_RC" -eq 2 ]; then
  echo "[信息] 没有待发的新图集，本次跳过。"
  exit 0
fi
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 任务结束，发帖环节退出码: $POST_RC"
exit "$POST_RC"
