#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【18号 2026-11-13】「谁来过」清单页生成器

两个数据来源严格分开，不混着冒充：
  A. server.log —— 旧版「联网登录」时期的服务端登录记录（看板 10-23 ~ 11-12 21:44，会员系统同期）
  B. audit.log  —— 新版「本地登录 + 顺手留痕」，每条带浏览器端事件真实发生时刻 at

铁律：断网补报时行首时间是「收到时刻」，绝不拿它冒充登录时刻；
页面显式列出覆盖不到的空档期，不拿本页生成时间冒充数据采集时刻。
"""
import os
import re
import json
import threading
from urllib.parse import unquote
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
SERVER_LOG = os.path.join(BASE, 'logs', 'server.log')
AUDIT_LOG = os.path.join(BASE, 'logs', 'audit.log')
OUT = '/Users/mac/WorkBuddy/2026-09-24-20-39-28/登录记录-最新版.html'

SYS_CN = {
    'board': '数据看板', 'board_old': '数据看板（旧版）',
    'inventory': '库存查询', 'mms': '会员管理系统',
}
ACT_CN = {
    'ok': '登录成功', 'fail': '账号或密码错误', 'logout': '退出登录',
    'open': '打开页面', 'search': '搜库存', 'trace': '查串码',
    'detail': '看明细', 'export': '导出', 'copy': '复制',
    '200': '登录成功', '401': '账号或密码错误', '429': '尝试太频繁',
}


def parse_ts(s):
    try:
        return datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
    except Exception:
        return None


def old_records():
    """旧版联网登录：服务端本来就有真实登录日志，直接收进来，不编造。"""
    out = []
    if not os.path.exists(SERVER_LOG):
        return out
    pat = re.compile(
        r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(/api/(?:mms/)?login)\s+'
        r'(\d{3})\s+\S+\s+account=(\S*)(?:\s+store=(\S*))?')
    with open(SERVER_LOG, encoding='utf-8', errors='replace') as f:
        for line in f:
            m = pat.match(line.strip())
            if not m:
                continue
            ts, path, code, acct, store = m.groups()
            out.append({
                'at': ts, 'recv': ts,
                'sys': 'mms' if 'mms' in path else 'board_old',
                'act': ACT_CN.get(code, '登录' + code),
                'acct': acct or '(空)', 'store': store or '',
                'dev': '', 'ua': '旧版服务端记录',
                'src': '旧版联网登录', 'delay': False,
            })
    return out


def new_records():
    """新版留痕：at=事件真实发生时刻，行首=Mac 收到时刻，两者分开存。"""
    out = []
    if not os.path.exists(AUDIT_LOG):
        return out
    pat = re.compile(
        r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+'
        r'at=(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+'
        r'sys=(\S+)\s+act=(\S+)\s+acct=(\S*)\s+store=(\S*)\s+'
        r'(?:info=(\S*)\s+)?dev=(\S+)\s+ua=(.*)$')
    with open(AUDIT_LOG, encoding='utf-8', errors='replace') as f:
        for line in f:
            m = pat.match(line.strip())
            if not m:
                continue
            recv, at, sysx, act, acct, store, info, dev, ua = m.groups()
            a, b = parse_ts(at), parse_ts(recv)
            delayed = bool(a and b and abs((b - a).total_seconds()) > 60)
            out.append({
                'at': at, 'recv': recv,
                'sys': sysx if sysx in SYS_CN else 'board',
                'act': ACT_CN.get(act, act),
                'acct': acct or '(空)', 'store': store or '',
                'dev': dev, 'ua': ua.strip(), 'info': unquote(info or ''),
                'src': '新版留痕', 'delay': delayed,
            })
    return out


_build_lock = threading.Lock()

def build():
    """线程安全：服务端每收到一条纸条就刷新一次，多个请求并发也不互相写坏文件。"""
    with _build_lock:
        rows = old_records() + new_records()
        rows.sort(key=lambda r: r['at'], reverse=True)
        gen = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        last = rows[0]['at'] if rows else '（暂无记录）'
        devices = sorted({r['dev'] for r in rows if r['dev']})
        accounts = sorted({r['acct'] for r in rows if r['acct'] and r['acct'] != '(空)'})
        data = {
            'rows': rows, 'generatedAt': gen, 'lastAt': last,
            'devices': devices, 'accounts': accounts,
            'sysCn': SYS_CN,
        }
        tpl = open(os.path.join(BASE, 'audit_report_template.html'), encoding='utf-8').read()
        # 内联 JSON 里若出现 </script> 会提前闭合脚本块，统一转义 < 防注入
        payload = json.dumps(data, ensure_ascii=False).replace('<', '\\u003c')
        out = tpl.replace('__DATA__', payload)
        tmp = OUT + '.tmp.%d' % os.getpid()
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(out)
        os.replace(tmp, OUT)
        return len(rows)


if __name__ == '__main__':
    print('✅ 已生成 %s | 记录 %d 条' % (OUT, build()))
