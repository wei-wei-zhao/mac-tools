#!/bin/bash
# ============================================================
#  WiFiDrop 更新包服务器（内网用）
#  双击运行，把本机的 release 目录通过 HTTP 共享出去，
#  同事就能从这个地址自动更新。
#
#  前提：同事和这台 Mac 在同一个内网
#  保持这个终端窗口开着，关了就断了
# ============================================================

cd "$(dirname "$0")" || exit 1

PORT=8790
DIR="$(pwd)/dist/release"

if [ ! -d "$DIR" ]; then
    echo "❌ 找不到 $DIR"
    read -r -p "按回车关闭…"
    exit 1
fi

IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)
if [ -z "$IP" ]; then
    IP="127.0.0.1"
fi

echo "════════════════════════════════════════════════════"
echo "        WiFiDrop 更新包服务器"
echo "════════════════════════════════════════════════════"
echo ""
echo "  共享目录：$DIR"
echo ""
echo "  📡 同事端的更新地址应为："
echo ""
echo "     http://$IP:$PORT/version.json"
echo ""
echo "  把这个地址填进打包命令的 --base-url 即可："
echo "     python3 release.py 1.5.0 --base-url http://$IP:$PORT/"
echo ""
echo "────────────────────────────────────────────────────"
echo "  可用文件："
ls -1 "$DIR" | sed 's/^/    /'
echo "────────────────────────────────────────────────────"
echo ""
echo "  ⏹  按 Ctrl+C 停止共享"
echo "════════════════════════════════════════════════════"
echo ""

exec python3 -m http.server "$PORT" --directory "$DIR"
