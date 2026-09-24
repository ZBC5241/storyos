#!/bin/bash
# ============================================================
# fetch_round.sh —— storyos 拉数轮（B 方案数据源头，2026-09-24 建；同日改全经理号版）
# 职责：storyos 自己拉双店数据，全部用**经理工号 18591910491**（晨哥拍板）
#   A. 李家村：
#      ① shop/fetch_yonyou_fast.py --account manager → 全公司毛利明细 xlsx（77s）
#      ② storyos/filter_maoli_ljc.py → 过滤出李家村毛利明细
#      ③ storyos/fetch_sales_analysis_ljc.py → 经理号+门店ID精准锁店拉李家村
#        销售分析（JSON API，store ID=2387138245738627078，~40-130s）
#        → 直接写 shop/sa_aug_cache.json（渠道数据，merge_qudao 消费）
#      ④ shop/run_pipeline.py <ljc毛利> --no-sa --no-xs --no-push → 产 shop/data.json
#      ⚠️ 销售分析 xlsx 导出被服务端分页截断（全公司 1120/22.9万行，李家村仅 44 条溢入），
#        故渠道数据必须走 JSON API 路线，xlsx 只作 run_pipeline 的占位参数
#   B. 华阳城：shophyc/update_hyc.sh --no-push（经理号，自带登录态体检，10-13min）
# 产出：shop/data.json + shophyc/data.json（由 storyos/inject_data.py 消费注入）
# 单轮约 15-17 分钟；plist 每 15 分钟一档（10:06-22:51），锁防重叠（重叠档自动跳过）
# 账号：全部经理号；与库存链路（9223 长驻经理会话）并存为已验证现状（shophyc+kucunos
#       多月共存先例），双方均有登录态体检/recover_login 自愈
# ============================================================
set -u
WS="/Users/mac/.local/share/TeleAgent/TeleAgent的工作空间"
SHOP="$WS/shop"; HYC="$WS/shophyc"; S="$WS/storyos"
DL="/Users/mac/.local/share/TeleAgent/playwright-mcp"
LOG="$S/logs/fetch.log"
NOTIFY="$WS/shop/notify_fail.py"
PY="/Users/mac/.local/share/TeleAgent/runtimes/python/bin/python3"
export HOME=/Users/mac
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
export NODE_PATH="/Users/mac/.workbuddy/binaries/node/workspace/node_modules"
mkdir -p "$(dirname "$LOG")"
[ -f "$LOG" ] && [ "$(stat -f%z "$LOG" 2>/dev/null || echo 0)" -gt 5242880 ] && { tail -3000 "$LOG" > "$LOG.tmp"; mv -f "$LOG.tmp" "$LOG"; }

# 超时包装（防 Playwright/管线挂起）
run_to() {
  local secs=$1; shift
  "$@" & local p=$!
  ( sleep "$secs" && kill -9 "$p" 2>/dev/null ) & local w=$!
  wait "$p"; local rc=$?
  kill "$w" 2>/dev/null; wait "$w" 2>/dev/null
  return $rc
}

# 互斥锁（mkdir 原子锁；单轮 ~15min，stale 30min 自动释放）
LOCK_DIR="/tmp/storyos_fetch.lock.d"
if [ -d "$LOCK_DIR" ]; then
  if [ -n "$(find "$LOCK_DIR" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
    echo "⚠️  陈旧锁(>30min)强制释放"
    rm -rf "$LOCK_DIR"
  else
    echo "🔒 上一轮拉数仍在运行，本次跳过" >> "$LOG"
    exit 0
  fi
fi
mkdir "$LOCK_DIR" 2>/dev/null || exit 0
trap 'rm -rf "$LOCK_DIR" 2>/dev/null' EXIT

{
  echo ""
  echo "============================================================"
  echo "  storyos 拉数轮 · $(date '+%F %T')"
  echo "============================================================"

  echo "▶ A1. 经理号拉全公司毛利明细（超时 300s）"
  cd "$SHOP"
  run_to 300 "${PY}" fetch_yonyou_fast.py --account manager > /tmp/storyos_fetch_ljc.log 2>&1
  FETCH_RC=$?
  tail -2 /tmp/storyos_fetch_ljc.log
  eval $(grep "^PROFIT_FILE\|^SALES_FILE" /tmp/storyos_fetch_ljc.log)
  if [ "$FETCH_RC" -ne 0 ] || [ -z "${PROFIT_FILE:-}" ] || [ ! -f "$PROFIT_FILE" ]; then
    echo "❌ 毛利明细拉数失败（RC=$FETCH_RC），中止本轮"
    exit 1
  fi

  echo "▶ A2. 过滤李家村毛利明细"
  run_to 120 "${PY}" "$S/filter_maoli_ljc.py" "$PROFIT_FILE" 2>&1 | tail -4
  RC_F=$?
  [ "$RC_F" -ne 0 ] && { echo "❌ 毛利过滤失败 RC=$RC_F，中止本轮"; exit 1; }
  LJC_PROFIT="$DL/ljc/李家村门店毛利明细表-华为终端.xlsx"
  LJC_SA="$DL/销售分析_0924.xlsx"   # 占位参数（--no-sa 不消费，防分页截断数据入渠道口径）

  echo "▶ A3. 经理号精准拉李家村销售分析（JSON API，直写渠道缓存）"
  cd "$S"
  run_to 400 "${PY}" fetch_sales_analysis_ljc.py 2>&1 | tail -5
  RC_S=$?
  [ "$RC_S" -ne 0 ] && { echo "❌ 李家村销售分析拉取失败 RC=$RC_S，中止本轮"; exit 1; }

  echo "▶ A4. 李家村管线复算（--no-sa --no-xs --no-push）"
  cd "$SHOP"
  run_to 300 "${PY}" run_pipeline.py "$LJC_PROFIT" "$LJC_SA" --no-sa --no-xs --no-push > /tmp/storyos_ljc_pipeline.log 2>&1
  RC1=$?
  tail -3 /tmp/storyos_ljc_pipeline.log
  if [ "$RC1" -ne 0 ]; then
    echo "❌ 李家村管线失败 RC=$RC1，中止本轮"
    exit 1
  fi
  echo "  ✅ 李家村 data.json + 渠道缓存 已更新（未推 shop 仓库）"

  echo "▶ B. 华阳城自拉（经理号，--no-push，超时 1200s）"
  cd "$HYC"
  run_to 1200 bash update_hyc.sh --no-push > /tmp/storyos_hyc_pipeline.log 2>&1
  RC2=$?
  tail -5 /tmp/storyos_hyc_pipeline.log
  if [ "$RC2" -ne 0 ]; then
    echo "❌ 华阳城管线失败 RC=$RC2，中止本轮"
    exit 1
  fi
  echo "  ✅ 华阳城 data.json 已更新（未推 shophyc 仓库）"

  echo "✅ 双店拉数完成 · $(date '+%H:%M:%S')"
} >> "$LOG" 2>&1
RC=$?
if [ "$RC" -ne 0 ]; then
  /usr/bin/python3 "$NOTIFY" --stage "storyos拉数轮" --exit-code "$RC" --log "$LOG" --tail 10 >> "$LOG" 2>&1
fi
exit "$RC"
