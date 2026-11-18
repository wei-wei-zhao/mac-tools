#!/bin/bash
# ============================================================
#  WiFiDrop 更新服务器（后台常驻版）
#  由 launchd 开机自启，不需要手动开窗口
# ============================================================

RELEASE_DIR="/Users/tal/WorkBuddy/2026-10-28-11-06-49/wifidrop/dist/release"
PORT=8790
PY="/Users/tal/.workbuddy/binaries/python/envs/default/bin/python3"
LOG="$HOME/Library/Logs/wifidrop-update-server.log"

# 回退到系统 python3
[ -x "$PY" ] || PY="$(command -v python3)"

mkdir -p "$(dirname "$LOG")"

# 目录不存在就退出（避免 launchd 疯狂重启）
if [ ! -d "$RELEASE_DIR" ]; then
    echo "$(date) 目录不存在: $RELEASE_DIR" >> "$LOG"
    exit 1
fi

echo "$(date) 启动更新服务器 port=$PORT dir=$RELEASE_DIR" >> "$LOG"

exec "$PY" -m http.server "$PORT" --directory "$RELEASE_DIR" >> "$LOG" 2>&1
