#!/bin/bash
# 双击后在终端里显示访问地址与二维码
cd "$(dirname "$0")" || exit 1
IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)
[ -z "$IP" ] && IP="127.0.0.1"
URL="http://$IP:8765"

echo ""
echo "  📱 手机浏览器打开（需同一 WiFi）："
echo ""
echo "     $URL"
echo ""
if curl -s --noproxy '*' -o /dev/null --max-time 2 "$URL"; then
    echo "  ✅ 服务正在运行"
else
    echo "  ⚠️  服务没在运行，请先双击「启动.command」"
fi
echo ""
echo "  二维码图片：$(pwd)/手机扫码打开.png"
echo ""
read -r -p "  按回车关闭此窗口…"
