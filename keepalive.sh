#!/usr/bin/env bash
# remote-ws 保活：检查并刷新默认服务器（每次实时读取 remote-ws.conf 的 DEFAULT_SERVER）的项目专用 ControlMaster 连接（ControlPath 与 MCP/scripts 一致）。
# 检查用 BatchMode，绝不触发密码提示；连接正常时打开一个真实 ssh channel，刷新 8 小时保活计时。
# 日志只在状态变化或每天一条心跳时记录；断开时发桌面通知（Windows toast，4 小时最多一次）。
# 用法:
#   keepalive.sh            正常检查（cron 每小时调用）
#   keepalive.sh --quiet    只检查刷新，不写日志不通知（Kimi SessionStart hook 用）
#   keepalive.sh --notify-test  发一条测试通知后退出
#   keepalive.sh -h|--help  显示用法
set -uo pipefail
DIR=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
CFG="$DIR/mcp/ssh_config"
RUNTIME=$(python3 "$DIR/mcp/local_connection.py")   # 项目专用 socket 目录，与 MCP/scripts 共用
SSH=(ssh -F "$CFG" -o "ControlPath=$RUNTIME/cm-%C" -o "UserKnownHostsFile=$DIR/mcp/known_hosts")
LOG="$DIR/logs/keepalive.log"
STATE="$DIR/logs/keepalive.state"   # 1=up 0=down
STAMP="$DIR/logs/notify.stamp"      # 上次通知时间(epoch)
PS_EXE=/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe
NOTIFY_INTERVAL=14400               # 4 小时

ts() { date '+%F %T'; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

notify() {  # $1=标题 $2=正文；任一通道失败都静默降级，不影响主流程
  local title="${1//\'/\'\'}" body="${2//\'/\'\'}" b64
  if command -v notify-send >/dev/null 2>&1 \
      && notify-send --expire-time=10000 "$1" "$2" 2>/dev/null; then
    return 0
  fi
  if [ -x "$PS_EXE" ] && command -v iconv >/dev/null 2>&1 && command -v base64 >/dev/null 2>&1; then
    b64=$(printf '%s' "[void][Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]; \$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); \$x=\$t.GetElementsByTagName('text'); \$x.Item(0).AppendChild(\$t.CreateTextNode('$title'))|Out-Null; \$x.Item(1).AppendChild(\$t.CreateTextNode('$body'))|Out-Null; \$n=[Windows.UI.Notifications.ToastNotification]::new(\$t); ([Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe')).Show(\$n)" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)
    [ -n "$b64" ] && "$PS_EXE" -NoProfile -NonInteractive -EncodedCommand "$b64" >/dev/null 2>&1
  fi
  return 0
}

usage() {
  echo "用法: bash keepalive.sh [--quiet|--notify-test|-h|--help]"
  echo "  （无参数）          正常检查（cron 每小时调用）"
  echo "  --quiet             只检查刷新，不写日志不通知（SessionStart hook 用）"
  echo "  --notify-test       发一条测试通知后退出"
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  --notify-test)
    notify "remote-ws 测试" "这是一条测试通知：断线提醒工作正常"
    echo "已发送测试通知"
    exit 0 ;;
  --quiet) QUIET=1 ;;
  "") QUIET=0 ;;
  *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
esac

# 与 scripts/connect.sh 相同的方式实时解析默认服务器；解析失败时宁可退出，也不去保活错误的机器
if ! SERVER=$(python3 "$DIR/mcp/local_connection.py" resolve-server 2>&1); then
  log "无法确定默认服务器，保活退出（请检查 mcp/remote-ws.conf 的 DEFAULT_SERVER）: $SERVER"
  exit 1
fi
HOST="srv-$SERVER"

now=$(date +%s)
last=-1
[ -f "$STATE" ] && last=$(cat "$STATE" 2>/dev/null || echo -1)

if "${SSH[@]}" -o BatchMode=yes -o ConnectTimeout=10 "$HOST" true >/dev/null 2>&1; then
  cur=1
else
  cur=0
fi
echo "$cur" > "$STATE"

if [ "$cur" = 1 ]; then
  if [ "$last" = 0 ]; then
    if [ "$QUIET" = 0 ]; then
      log "$HOST 连接已恢复（保活成功）"
      notify "remote-ws: $HOST 已恢复" "与控制通道已恢复，MCP 可正常使用"
    fi
  elif [ "$last" = -1 ] && [ "$QUIET" = 0 ]; then
    log "保活检查启动，$HOST 连接正常"
  elif [ "$QUIET" = 0 ] && [ "$(date +%H)" = "04" ]; then
    log "$HOST 保活正常（连接活跃）"
  fi
else
  if [ "$last" != 0 ] && [ "$QUIET" = 0 ]; then
    log "$HOST 连接断开，保活无法自动重连（需要密码）。用户可执行: bash $DIR/scripts/connect.sh $SERVER"
  fi
  if [ "$QUIET" = 0 ]; then
    last_notify=0
    [ -f "$STAMP" ] && last_notify=$(cat "$STAMP" 2>/dev/null || echo 0)
    if [ "$last" != 0 ] || [ $((now - last_notify)) -ge "$NOTIFY_INTERVAL" ]; then
      notify "remote-ws: $HOST 连接已断开" "MCP 会按需打开登录窗口；也可在 WSL 终端执行: bash $DIR/scripts/connect.sh $SERVER（输一次密码即可）"
      echo "$now" > "$STAMP"
    fi
  fi
fi
exit 0
