#!/bin/bash
# WiFiDrop 启动器 —— 双击即可运行
cd "$(dirname "$0")" || exit 1

PY="/Users/tal/.workbuddy/binaries/python/envs/default/bin/python"

# 若首选解释器不可用，退回系统 python3
if [ ! -x "$PY" ]; then
    PY="$(command -v python3)"
fi

if [ -z "$PY" ]; then
    echo "❌ 找不到 python3"
    read -r -p "按回车退出…"
    exit 1
fi

# 检查依赖
if ! "$PY" -c "import flask, qrcode" 2>/dev/null; then
    echo "📦 首次运行，正在安装依赖…"
    "$PY" -m pip install flask qrcode -i https://pypi.tuna.tsinghua.edu.cn/simple --quiet
fi

echo "🚀 启动 WiFiDrop…"
clear
exec "$PY" server.py "$@"
