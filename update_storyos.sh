#!/bin/bash
# ============================================================
# update_storyos.sh —— storyos 双店看板定时更新入口（launchd 调用）
# 建：2026-09-24（18号，晨哥拍板：一天20档，晚间高频）
#
# 链路：复用 shop/data.json（李家村·店长号管线）+ shophyc/data.json（华阳城·经理号管线）
#       → inject_data.py 注入 index.html → 有变化才 git push（SSH-443）
# 特性：零拉数、零浏览器、零用友登录（不与任何链路抢账号/端口，单次约 3-5 秒）
#
# 排期（com.storyos.dashboard.update，20档/天）：
#   白天低频  12:16 / 13:46 / 15:16 / 16:46 / 17:16
#   傍晚中频  18:16 / 18:46 / 19:16 / 19:46
#   高峰密集  20:16 / 20:31 / 20:46 / 21:01 / 21:16 / 21:31 / 21:46 / 22:01 / 22:16（20:00-22:30 每约15分钟）
#   收尾      22:31 / 22:59
# 数据新鲜度跟随上游：李家村=shop 档位后最新，华阳城=shophyc 档位后最新
# ============================================================
set -u
S="/Users/mac/.local/share/TeleAgent/TeleAgent的工作空间/storyos"
LOG="$S/logs/update.log"
NOTIFY="/Users/mac/.local/share/TeleAgent/TeleAgent的工作空间/shop/notify_fail.py"
PY="/Users/mac/.local/share/TeleAgent/runtimes/python/bin/python3"
export HOME=/Users/mac
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
mkdir -p "$(dirname "$LOG")"
[ -f "$LOG" ] && [ "$(stat -f%z "$LOG" 2>/dev/null || echo 0)" -gt 2097152 ] && : > "$LOG"

# ---- 互斥锁（mkdir 原子锁，仿华阳城）----
LOCK_DIR="/tmp/storyos_update.lock.d"
if [ -d "$LOCK_DIR" ]; then
  if [ -n "$(find "$LOCK_DIR" -maxdepth 0 -mmin +15 2>/dev/null)" ]; then
    echo "⚠️  陈旧锁(>15min)强制释放"
    rm -rf "$LOCK_DIR"
  else
    echo "🔒 上一轮仍在运行，本次跳过" >> "$LOG"
    exit 0
  fi
fi
mkdir "$LOCK_DIR" 2>/dev/null || exit 0
trap 'rm -rf "$LOCK_DIR" 2>/dev/null' EXIT

{
  echo ""
  echo "============================================================"
  echo "  storyos 双店看板更新 · $(date '+%F %T')"
  echo "============================================================"
  cd "$S"

  echo "▶ 1. 注入双店数据"
  "$PY" inject_data.py
  RC=$?
  if [ "$RC" -ne 0 ]; then
    echo "❌ 注入失败 RC=$RC"
    exit "$RC"
  fi

  echo "▶ 2. 检查变化并推送"
  if git diff --quiet index.html; then
    echo "⏭ index.html 无变化，跳过 push"
    exit 0
  fi
  git add index.html
  git commit -m "auto(storyos): 定时更新双店数据 $(date '+%Y-%m-%d %H:%M')" >/dev/null 2>&1
  echo "[push] git push origin main（90s 防挂起超时）"
  git push origin main > /tmp/storyos_push.log 2>&1 &
  pushpid=$!
  ( sleep 90 && kill -9 "$pushpid" 2>/dev/null ) & w=$!
  wait "$pushpid"; PUSH_RC=$?
  kill "$w" 2>/dev/null
  tail -2 /tmp/storyos_push.log
  echo "push RC=$PUSH_RC"
  exit "$PUSH_RC"
} >> "$LOG" 2>&1
RC=$?
if [ "$RC" -ne 0 ]; then
  /usr/bin/python3 "$NOTIFY" --stage "storyos看板" --exit-code "$RC" --log "$LOG" --tail 10 >> "$LOG" 2>&1
fi
exit "$RC"
