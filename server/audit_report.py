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
import time
import threading
from urllib.parse import unquote
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
SERVER_LOG = os.path.join(BASE, 'logs', 'server.log')
AUDIT_LOG = os.path.join(BASE, 'logs', 'audit.log')
OUT = '/Users/mac/WorkBuddy/2026-09-24-20-39-28/登录记录-最新版.html'
# 【18号 2026-11-13 17:30】上网地址 -> 归属地缓存（只存本机 logs/，不进 git、不上公网）
GEO_CACHE = os.path.join(BASE, 'logs', 'ip_geo_cache.json')

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


_geo_lock = threading.Lock()


def _load_geo_cache():
    try:
        with open(GEO_CACHE, encoding='utf-8') as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_geo_cache(d):
    try:
        os.makedirs(os.path.dirname(GEO_CACHE), exist_ok=True)
        tmp = GEO_CACHE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, GEO_CACHE)
    except Exception:
        pass


def _private_ip(ip):
    return (not ip or ip in ('127.0.0.1', '::1', 'localhost')
            or ip.startswith(('10.', '192.168.', '169.254.'))
            or any(ip.startswith('172.%d.' % i) for i in range(16, 32)))


def geo_lookup(ip, cache):
    """公网地址 -> 归属地：查第三方 IP 库得到的「推测」，不是设备 GPS 坐标。
    查不到就返回空，页面显式显示「未查到」，绝不拿旧结果或猜一个地名顶上。"""
    if not ip:
        return ''
    if _private_ip(ip):
        return '本机 / 内网'
    hit = cache.get(ip)
    now = time.time()
    if hit and (hit.get('loc') or now - hit.get('ts', 0) < 600):
        return hit.get('loc', '')
    loc = ''
    try:
        from urllib.request import urlopen
        from urllib.parse import quote as urlquote
        url = 'https://whois.pconline.com.cn/ipJson.jsp?ip=%s&json=true' % urlquote(ip)
        with urlopen(url, timeout=2.5) as r:
            raw = r.read().decode('gbk', 'replace').strip()
        d = json.loads(raw)
        if not d.get('err'):
            parts = []
            for k in ('pro', 'city', 'region'):
                v = (d.get(k) or '').strip()
                if v and v not in parts:
                    parts.append(v)
            loc = ' '.join(parts)
    except Exception:
        loc = ''
    cache[ip] = {'loc': loc, 'ts': now}
    return loc


def attach_locations(rows):
    """同一个上网地址只查一次并缓存；老记录没有地址字段就留空，不冒充、不回填。"""
    with _geo_lock:
        cache = _load_geo_cache()
        dirty = False
        for r in rows:
            ip = r.get('ip', '')
            if not ip:
                r['loc'] = ''
                continue
            if _private_ip(ip):
                r['loc'] = '本机 / 内网'
                continue
            before = cache.get(ip)
            r['loc'] = geo_lookup(ip, cache)
            if before != cache.get(ip):
                dirty = True
        if dirty:
            _save_geo_cache(cache)
    return rows


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
                'dev': '', 'ua': '旧版服务端记录', 'ip': '', 'loc': '',
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
        r'(?:info=(\S*)\s+)?dev=(\S+)\s+(?:ip=(\S*)\s+)?ua=(.*)$')
    with open(AUDIT_LOG, encoding='utf-8', errors='replace') as f:
        for line in f:
            m = pat.match(line.strip())
            if not m:
                continue
            recv, at, sysx, act, acct, store, info, dev, ip, ua = m.groups()
            a, b = parse_ts(at), parse_ts(recv)
            delayed = bool(a and b and abs((b - a).total_seconds()) > 60)
            out.append({
                'at': at, 'recv': recv,
                'sys': sysx if sysx in SYS_CN else 'board',
                'act': ACT_CN.get(act, act),
                'acct': acct or '(空)', 'store': store or '',
                'dev': dev, 'ua': ua.strip(), 'ip': ip or '', 'loc': '',
                'info': unquote(info or ''),
                'src': '新版留痕', 'delay': delayed,
            })
    return out


_build_lock = threading.Lock()


def log_bytes():
    return sum(os.path.getsize(p) for p in (SERVER_LOG, AUDIT_LOG) if os.path.exists(p))


def build(scope='today', window_days=0, person=None, out=None):
    """线程安全：服务端每收到一条纸条就刷新一次，多个请求并发也不互相写坏文件。

    【18号 2026-11-13 16:30 晨哥定调】清单页默认**只装今天**（自然日 00:00 起）：
    历史数据不在页面里出现，但日志文件一条都不删。
    --days / --all / --person 仍可随时把历史单独导出来。
    scope: today=今天 / days=近 N 天 / all=全部历史。
    """
    with _build_lock:
        all_rows = old_records() + new_records()
        # 同一秒有多笔时，让「后写进日志的」排在前面；sort 稳定，按人聚合取第一条才是真正最后一次
        all_rows.reverse()
        all_rows.sort(key=lambda r: r['at'], reverse=True)
        from datetime import timedelta
        cutoff = ''
        if scope == 'today':
            cutoff = datetime.now().strftime('%Y-%m-%d') + ' 00:00:00'
        elif window_days:
            cutoff = (datetime.now() - timedelta(days=window_days)).strftime('%Y-%m-%d %H:%M:%S')
        # 今天口径下，没有真实发生时刻的记录无法归到今天，不冒充今天的记录
        rows = [r for r in all_rows if not cutoff or (r['at'] != '-' and r['at'] >= cutoff)]
        if person:
            rows = [r for r in rows if person in r['acct']]
        rows = attach_locations(rows)
        gen = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # 用「日志库」最后一条，不用本页最后一条：筛选/窗口下也不会让人误以为记录断了
        last = all_rows[0]['at'] if all_rows else '（暂无记录）'
        valid_ats = [r['at'] for r in all_rows if not r['at'].startswith('-')]
        devices = sorted({r['dev'] for r in rows if r['dev']})
        accounts = sorted({r['acct'] for r in rows if r['acct'] and r['acct'] != '(空)'})
        data = {
            'rows': rows, 'generatedAt': gen, 'lastAt': last,
            'devices': devices, 'accounts': accounts,
            'sysCn': SYS_CN,
            'meta': {
                'scope': scope,
                'scopeLabel': '今天' if scope == 'today' else ('全部历史' if scope == 'all' else '近 %d 天' % window_days),
                'windowDays': window_days,
                'totalAll': len(all_rows),
                'shown': len(rows),
                'earliestAll': min(valid_ats) if valid_ats else '（暂无）',
                'logBytes': log_bytes(),
                'logSize': '%.1f KB' % (log_bytes() / 1024.0),
                'person': person or '',
            },
        }
        tpl = open(os.path.join(BASE, 'audit_report_template.html'), encoding='utf-8').read()
        # 内联 JSON 里若出现 </script> 会提前闭合脚本块，统一转义 < 防注入
        payload = json.dumps(data, ensure_ascii=False).replace('<', '\\u003c')
        html = tpl.replace('__DATA__', payload)
        target = out or OUT
        tmp = target + '.tmp.%d' % os.getpid()
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(html)
        os.replace(tmp, target)
        return len(rows)


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='生成使用监控台清单页')
    ap.add_argument('--days', type=int, default=0, help='装最近 N 天（默认只装今天）')
    ap.add_argument('--all', action='store_true', help='把日志库全部历史装进这一页')
    ap.add_argument('--person', default='', help='只导某个人（姓名可只写一部分）')
    ap.add_argument('--out', default=OUT, help='输出文件')
    a = ap.parse_args()
    kw = {'person': a.person or None, 'out': a.out}
    if a.all:
        kw['scope'] = 'all'
    elif a.days:
        kw['scope'], kw['window_days'] = 'days', a.days
    else:
        kw['scope'] = 'today'
    n = build(**kw)
    print('✅ 已生成 %s | 口径 %s | 本页 %d 条' % (a.out, kw['scope'], n))
