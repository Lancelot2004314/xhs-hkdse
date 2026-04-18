#!/bin/bash
# 質心 XHS Studio · 一键启动脚本
# 用法 (在 repo 根目录):
#     bash start.sh
#
# 启动 xhs-mcp + webapp, 后台跑, 日志写到各自目录
# 停止: bash stop.sh

set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"

# 检测平台
OS=$(uname -s)
ARCH=$(uname -m)
case "$OS-$ARCH" in
  Darwin-arm64)  PLATFORM="darwin-arm64" ;;
  Darwin-x86_64) PLATFORM="darwin-amd64" ;;
  Linux-x86_64)  PLATFORM="linux-amd64"  ;;
  Linux-aarch64) PLATFORM="linux-arm64"  ;;
  *) echo "❌ 不支持的平台: $OS-$ARCH"; exit 1 ;;
esac

MCP_BIN="xhs-mcp/xiaohongshu-mcp-$PLATFORM"
LOGIN_BIN="xhs-mcp/xiaohongshu-login-$PLATFORM"
COOKIES="xhs-mcp/cookies.json"

# 检查二进制
if [ ! -x "$MCP_BIN" ]; then
  echo "❌ 找不到 $MCP_BIN, 先跑 bash install.sh"; exit 1
fi
if [ ! -f "webapp/config/app_config.json" ]; then
  echo "❌ 找不到 webapp/config/app_config.json, 先跑 bash install.sh"; exit 1
fi
# 检查 cookies
if [ ! -f "$COOKIES" ]; then
  echo "⚠️  $COOKIES 不存在, 你还没扫码登录小红书"
  echo "   先跑: cd xhs-mcp && ./xiaohongshu-login-$PLATFORM"
  echo "   (扫完再跑本脚本)"
  exit 1
fi

# 启动 xhs-mcp
if lsof -nP -i:18060 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "🟢 xhs-mcp 已在 :18060 跑着, 跳过"
else
  echo "🚀 启动 xhs-mcp ..."
  cd xhs-mcp
  nohup "./xiaohongshu-mcp-$PLATFORM" -port :18060 > xhs-mcp.log 2>&1 &
  echo $! > .xhs-mcp.pid
  cd "$ROOT"
  sleep 2
  if lsof -nP -i:18060 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "   ✅ xhs-mcp 已起 (PID=$(cat xhs-mcp/.xhs-mcp.pid), log=xhs-mcp/xhs-mcp.log)"
  else
    echo "   ❌ xhs-mcp 启动失败, 看日志: xhs-mcp/xhs-mcp.log"
    exit 1
  fi
fi

# 启动 webapp
if lsof -nP -i:8080 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "🟢 webapp 已在 :8080 跑着, 跳过"
else
  echo "🚀 启动 webapp ..."
  cd webapp
  # shellcheck disable=SC1091
  source .venv/bin/activate
  nohup python app.py > app.log 2>&1 &
  echo $! > .webapp.pid
  cd "$ROOT"
  for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    if lsof -nP -i:8080 -sTCP:LISTEN >/dev/null 2>&1; then
      echo "   ✅ webapp 已起 (PID=$(cat webapp/.webapp.pid), log=webapp/app.log)"
      break
    fi
    if [ "$i" = "10" ]; then
      echo "   ❌ webapp 10 秒还没监听 :8080, 看日志: webapp/app.log"
      exit 1
    fi
  done
fi

echo ""
echo "================================================================"
echo "  🎉 全部启动完成!"
echo "================================================================"
echo ""
echo "  本地访问:    http://localhost:8080/studio"

LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "")
if [ -n "$LAN_IP" ]; then
  echo "  局域网访问:  http://$LAN_IP:8080/studio  (同 wifi 同事可访问)"
fi

echo ""
echo "  停止:        bash stop.sh"
echo "  实时日志:    tail -f webapp/app.log xhs-mcp/xhs-mcp.log"
echo ""

# 自动开浏览器 (macOS 才有 open)
if command -v open >/dev/null 2>&1; then
  sleep 1
  open "http://localhost:8080/studio"
fi
