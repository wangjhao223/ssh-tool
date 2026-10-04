#!/usr/bin/env bash
# 输入服务器密码，建立仅供本项目复用的 SSH 连接（空闲 8 小时后断开）。
# 用法: bash scripts/connect.sh [服务器名，默认取 remote-ws.conf]
# 断开: bash scripts/connect.sh <服务器名> --close
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
DIR=$(cd "$SCRIPT_DIR/.." && pwd)
SERVER=$(python3 "$DIR/mcp/local_connection.py" resolve-server "${1:-}")
HOST="srv-$SERVER"
CFG="$DIR/mcp/ssh_config"
RUNTIME=$(python3 "$DIR/mcp/local_connection.py")
SSH=(ssh -F "$CFG" -o "ControlPath=$RUNTIME/cm-%C" -o "UserKnownHostsFile=$DIR/mcp/known_hosts")
if [ "${2:-}" = "--close" ]; then
  "${SSH[@]}" -O exit "$HOST" 2>/dev/null && echo "已断开本项目的 $HOST" || echo "$HOST 未连接"
  exit 0
fi
if "${SSH[@]}" -O check "$HOST" >/dev/null 2>&1; then
  echo "$HOST 已连接"
  exit 0
fi
python3 "$DIR/mcp/local_connection.py" preflight "$SERVER"
exec 9>"$RUNTIME/login-$HOST.lock"
if ! flock -n 9; then
  echo "$HOST 的登录窗口已打开，请在原窗口完成登录。"
  exit 0
fi
if "${SSH[@]}" -O check "$HOST" >/dev/null 2>&1; then
  echo "$HOST 已连接"
  exit 0
fi
# Close the login lock in SSH so the background master does not keep it locked.
"${SSH[@]}" -o ConnectTimeout=15 -fN "$HOST" 9>&-
echo "已连接 $HOST（空闲 8 小时后自动断开）"
