#!/bin/bash
# storyos 售票厅公网隧道守护：固定 SSH 密钥 + 固定子域名连 serveo，断线自动重连
# 模式同 sn-trace（com.lijiacun.dashboard.serveo），子域名/端口不同，互不干扰
KEY="/Users/mac/.ssh/sn_trace_serveo"
SUB="lijiacun-board"
PORT=8802
LOG="/Users/mac/.local/share/TeleAgent/TeleAgent的工作空间/storyos/server/logs/serveo_tunnel.log"
URLFILE="/Users/mac/.local/share/TeleAgent/TeleAgent的工作空间/storyos/server/current_public_url.txt"

mkdir -p "$(dirname "$LOG")"

while true; do
  echo "[$(date)] 启动 serveo 隧道 子域名=$SUB 端口=$PORT" >> "$LOG"
  ssh -i "$KEY" \
      -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null \
      -o ServerAliveInterval=30 \
      -o ServerAliveCountMax=3 \
      -o ExitOnForwardFailure=yes \
      -R "${SUB}:80:localhost:${PORT}" serveo.net >> "$LOG" 2>&1 &
  SSH_PID=$!
  while kill -0 "$SSH_PID" 2>/dev/null; do
    grep "Forwarding HTTP traffic from" "$LOG" | tail -1 | grep -oE "https://[a-zA-Z0-9._-]+" > "$URLFILE"
    sleep 2
  done
  wait "$SSH_PID"
  echo "[$(date)] 隧道断开，5秒后重连" >> "$LOG"
  sleep 5
done
