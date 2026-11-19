#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【18号 2026-11-19】每天最后一次刷新后，用看板 data.json 的李家村数据，
生成两张与桌面底表同款的图片：
  图1 = 绩效打分表（业绩指标 + 全科生 + 考核项，按员工分行 + 合计）
  图2 = 员工每日达成表 + 每日任务缺口表

只读看板数据，不碰底表、不依赖 Excel 重算。数据与看板完全同源。
输出 PNG 到 --out 目录。失败显式报错退出，绝不产出旧图冒充。
"""
import os, sys, json, argparse, subprocess, shutil
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BASE)
DATA_JSON = os.path.join(REPO, 'data.json')
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
PEOPLE = ['邵乐乐', '杨丽华', '李泽', '张博晨']

# 业绩区品类（对应底表4列：任务/完成/缺口/完成率）
PERF_COLS = [
    ('毛利', '毛利（35分）'), ('手机', '手机（15分）'), ('PC', 'PC'),
    ('平板', '平板'), ('穿戴', '穿戴'), ('音频', '音频'),
    ('HD', 'HD（5分）'), ('智慧办公', '💻 智慧办公（8分）'),
    ('音频穿戴', '🎧 音频穿戴（8分）'), ('销额', '销额'),
]
# 全科生区：特殊列结构，单独渲染
# 考核项区：考核机型/乐机收/太力回收/增值/健康度/星联会员，单独渲染


def n(v):
    return float(v) if v is not None else 0.0


def fmt_int(v):
    return f'{int(round(n(v))):,}'


def fmt_rate(v):
    return f'{n(v) * 100:.0f}%'


def fmt_rate1(v):
    return f'{n(v) * 100:.1f}%'


def gap(task, done):
    return fmt_int(n(done) - n(task))


def gap_color(v):
    return 'g' if n(v) >= 0 else 'r'


def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def load_data():
    if not os.path.exists(DATA_JSON):
        raise RuntimeError(f'看板数据不存在: {DATA_JSON}')
    d = json.load(open(DATA_JSON, encoding='utf-8'))
    ljc = d.get('ljc')
    if not ljc or not ljc.get('meta', {}).get('date'):
        raise RuntimeError('data.json 缺 ljc 或 date')
    # 新鲜度：date 必须是今天
    today = datetime.now().strftime('%Y-%m-%d')
    if ljc['meta']['date'][:10] != today:
        raise RuntimeError(f"看板数据日期={ljc['meta']['date']} 非今天({today})，拒绝用旧数据出图")
    return ljc


CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'PingFang SC','Microsoft YaHei',sans-serif;background:#fff;color:#222;padding:0}
.wrap{display:inline-block;padding:10px 12px;background:#fff}
.hd{display:flex;justify-content:space-between;align-items:center;border:1px solid #c9d2e8;border-bottom:none;padding:6px 12px;font-size:20px;font-weight:700}
.hd .d{color:#222}.hd .s{color:#e02020}
table{border-collapse:collapse;font-size:13px;table-layout:fixed}
td,th{border:1px solid #c9d2e8;text-align:center;padding:5px 6px;white-space:nowrap}
.grp{background:#7b8bc4;color:#fff;font-weight:700;font-size:14px;padding:7px 4px}
.sub{background:#dbe4f7;color:#33406b;font-weight:600;font-size:12px;padding:5px 3px}
.sub .red{color:#e02020}
.name{background:#eef3ff;font-weight:700;width:64px;color:#222}
.num{font-variant-numeric:tabular-nums}
.tot td{background:#ffd9a0;font-weight:700}
.rate.g,.pos{color:#12924a}.rate.r,.neg{color:#e02020}
.perf-head{background:#c0392b;color:#fff;font-weight:700;font-size:15px;padding:8px}
.perf-head .sub2{background:#c0392b}
.perf-name{background:#eef2fb;font-weight:700;width:56px}
.perf-tot td{background:#bfe6cf;font-weight:700}
.dim{color:#9aa4c0}
"""

RATE_CLS = 'g'   # 完成率统一绿色数字显示（底表达成率红绿按缺口，简化为统一色，重点看数）


def build_img1(d):
    """绩效打分表：业绩区 + 全科生区 + 考核项区"""
    meta = d['meta']
    date = meta['date'][:10]
    store_p = d['store']['performance']
    store_q = d['store']['qcs']
    score = n(store_p.get('绩效'))
    people = d['people']

    h = ['<div class="wrap">']
    h.append(f'<div class="hd"><span class="d">{date}</span><span class="s">{score:.0f}%</span></div>')

    # ===== 业绩指标区 =====
    h.append('<table>')
    # 第一层组头
    h.append('<tr><td class="name" rowspan="2">姓名</td>')
    for key, label in PERF_COLS:
        if key == '销额':
            h.append(f'<td class="grp" colspan="4">{label}</td>')
        else:
            h.append(f'<td class="grp" colspan="4">{label}</td>')
    h.append('<td class="grp" rowspan="2" style="background:#c0392b">绩效</td></tr>')
    # 第二层：任务/完成/缺口/完成率
    h.append('<tr>')
    for _ in PERF_COLS:
        h.append('<td class="sub">任务</td><td class="sub">完成</td>'
                 '<td class="sub"><span class="red">缺口</span></td><td class="sub">完成率</td>')
    h.append('</tr>')
    # 数据行：每个员工 + 合计
    rows = [(name, people[name]) for name in PEOPLE if name in people]
    for name, pv in rows:
        perf = pv.get('performance', {})
        h.append(f'<tr><td class="name">{esc(name)}</td>')
        for key, _ in PERF_COLS:
            v = perf.get(key, {}) if isinstance(perf.get(key), dict) else {}
            t, dn, rt = v.get('task'), v.get('done'), v.get('rate')
            g = n(dn) - n(t)
            h.append(f'<td class="num">{fmt_int(t)}</td>'
                     f'<td class="num">{fmt_int(dn)}</td>'
                     f'<td class="num {gap_color(g)}">{gap(t, dn)}</td>'
                     f'<td class="num rate">{fmt_rate(rt)}</td>')
        sc = n(perf.get('绩效'))
        h.append(f'<td class="num" style="color:#c0392b;font-weight:700">{sc:.1f}</td></tr>')
    # 合计行（用 store 级）
    h.append('<tr class="tot"><td class="name">合计</td>')
    for key, _ in PERF_COLS:
        v = store_p.get(key, {}) if isinstance(store_p.get(key), dict) else {}
        t, dn, rt = v.get('task'), v.get('done'), v.get('rate')
        g = n(dn) - n(t)
        h.append(f'<td class="num">{fmt_int(t)}</td>'
                 f'<td class="num">{fmt_int(dn)}</td>'
                 f'<td class="num {gap_color(g)}">{gap(t, dn)}</td>'
                 f'<td class="num rate">{fmt_rate(rt)}</td>')
    h.append(f'<td class="num" style="color:#c0392b">{score:.1f}</td></tr>')
    h.append('</table>')

    # ===== 全科生区 =====
    h.append('<table style="margin-top:10px">')
    h.append('<tr><td class="grp" colspan="17">全科生</td></tr>')
    # 组头
    h.append('<tr><td class="name" rowspan="3"></td>'
             '<td class="grp" colspan="4">电信业务（5分）</td>'
             '<td class="grp" colspan="4">会员搭售率（30%）</td>'
             '<td class="grp" colspan="4">回收（25%）</td>'
             '<td class="grp" colspan="4">贴膜（50%）</td>'
             '</tr>')
    # 子头
    h.append('<tr>'
             '<td class="sub">任务</td><td class="sub">完成</td><td class="sub"><span class="red">缺口</span></td><td class="sub">完成率</td>'
             '<td class="sub">终端量</td><td class="sub">会员</td><td class="sub"><span class="red">缺口</span></td><td class="sub">搭售率</td>'
             '<td class="sub">单数</td><td class="sub"><span class="red">缺口</span></td><td class="sub">搭售率</td><td class="sub">目标</td>'
             '<td class="sub">单数</td><td class="sub"><span class="red">缺口</span></td><td class="sub">搭售率</td><td class="sub">目标</td>'
             '</tr>')
    # 数据行
    for name, pv in rows:
        q = pv.get('qcs', {})
        hy = q.get('合约', {}) if isinstance(q.get('合约'), dict) else {}
        mem = q.get('会员搭售率', {}) if isinstance(q.get('会员搭售率'), dict) else {}
        rec = q.get('回收搭售率', {}) if isinstance(q.get('回收搭售率'), dict) else {}
        tm = q.get('贴膜率', {}) if isinstance(q.get('贴膜率'), dict) else {}
        h.append(f'<tr><td class="name">{esc(name)}</td>')
        # 合约
        ht, hd = hy.get('task'), hy.get('done'); g = n(hd) - n(ht)
        h.append(f'<td>{fmt_int(ht)}</td><td>{fmt_int(hd)}</td><td class="{gap_color(g)}">{gap(ht, hd)}</td><td class="rate">{fmt_rate1(hy.get("rate"))}</td>')
        # 会员搭售
        h.append(f'<td>{fmt_int(mem.get("terminal"))}</td><td>{fmt_int(mem.get("care"))}</td>'
                 f'<td class="{gap_color(mem.get("gap"))}">{gap(0, mem.get("gap"))}</td>'
                 f'<td class="rate">{fmt_rate1(mem.get("rate"))}</td>')
        # 回收
        h.append(f'<td>{fmt_int(rec.get("orders"))}</td>'
                 f'<td class="{gap_color(rec.get("gap"))}">{gap(0, rec.get("gap"))}</td>'
                 f'<td class="rate">{fmt_rate1(rec.get("rate"))}</td><td>{fmt_rate1(rec.get("target"))}</td>')
        # 贴膜
        h.append(f'<td>{fmt_int(tm.get("orders"))}</td>'
                 f'<td class="{gap_color(tm.get("gap"))}">{gap(0, tm.get("gap"))}</td>'
                 f'<td class="rate">{fmt_rate1(tm.get("rate"))}</td><td>{fmt_rate1(tm.get("target"))}</td>')
        h.append('</tr>')
    h.append('</table>')

    # ===== 考核项区 =====
    h.append('<table style="margin-top:10px">')
    h.append('<tr><td class="grp" colspan="9">考核项</td>'
             '<td class="grp" colspan="4">增值（20分）</td>'
             '<td class="grp" colspan="4">健康度</td>'
             '<td class="grp" colspan="3">星联会员</td></tr>')
    h.append('<tr><td class="name" rowspan="2"></td>'
             '<td class="sub" colspan="2">考核机型-100/台</td>'
             '<td class="sub" colspan="2">乐收机</td>'
             '<td class="sub" colspan="2">太力回收</td>'
             '<td class="sub">任务</td><td class="sub">完成</td><td class="sub"><span class="red">缺口</span></td><td class="sub">完成率</td>'
             '<td class="sub">优惠券</td><td class="sub">占比</td><td class="sub">毛利率</td><td class="sub">增值率</td>'
             '<td class="sub">星联优享</td><td class="sub">星联尊享</td><td class="sub">合计</td>'
             '</tr>')
    h.append('<tr>'
             '<td class="sub">任务</td><td class="sub"><span class="red">缺口</span></td>'
             '<td class="sub">单量</td><td class="sub">增值</td>'
             '<td class="sub">单量</td><td class="sub">金额</td>'
             '</tr>')
    for name, pv in rows:
        q = pv.get('qcs', {})
        kh = q.get('考核机型', {}) if isinstance(q.get('考核机型'), dict) else {}
        ls = q.get('乐机收', {}) if isinstance(q.get('乐机收'), dict) else {}
        tl = q.get('太力回收', {}) if isinstance(q.get('太力回收'), dict) else {}
        zz = q.get('增值', {}) if isinstance(q.get('增值'), dict) else {}
        jk = q.get('健康度', {}) if isinstance(q.get('健康度'), dict) else {}
        xl = q.get('星联会员', {}) if isinstance(q.get('星联会员'), dict) else {}
        h.append(f'<tr><td class="name">{esc(name)}</td>')
        # 考核机型
        h.append(f'<td colspan="2">{fmt_int(kh.get("task"))} / <span class="{gap_color(kh.get("gap"))}">{gap(0, kh.get("gap"))}</span></td>')
        # 乐收机
        h.append(f'<td>{fmt_int(ls.get("orders"))}</td><td>{fmt_int(ls.get("amount"))}</td>')
        # 太力回收
        h.append(f'<td>{fmt_int(tl.get("orders"))}</td><td>{fmt_int(tl.get("amount"))}</td>')
        # 增值
        zt, zd = zz.get('task'), zz.get('done'); zg = n(zd) - n(zt)
        h.append(f'<td>{fmt_int(zt)}</td><td>{fmt_int(zd)}</td>'
                 f'<td class="{gap_color(zg)}">{gap(zt, zd)}</td><td class="rate">{fmt_rate(zz.get("rate"))}</td>')
        # 健康度
        h.append(f'<td>{fmt_int(jk.get("coupon"))}</td><td>{fmt_rate1(jk.get("ratio"))}</td>'
                 f'<td>{fmt_rate1(jk.get("grossMargin"))}</td><td>{fmt_rate1(jk.get("增值率"))}</td>')
        # 星联会员
        h.append(f'<td>{fmt_int(xl.get("优享"))}</td><td>{fmt_int(xl.get("尊享"))}</td><td>{fmt_int(xl.get("合计"))}</td>')
        h.append('</tr>')
    h.append('</table>')
    h.append('</div>')
    return '\n'.join(h)


def build_img2(d):
    """员工每日达成表 + 每日任务缺口表"""
    meta = d['meta']
    date = meta['date'][:10]
    mmdd = date[5:].replace('-', '-')
    people = d['people']

    items = ['手机', '毛利', '增值', '智慧办公', '音频穿戴', 'HD', '会员', '回收', '贴膜', '合约', '滞销', '优享/会员']

    h = ['<div class="wrap">']

    # ===== 达成表 =====
    h.append('<table>')
    h.append(f'<tr><td class="perf-head" colspan="{len(items)+1}">{mmdd}达成</td></tr>')
    h.append('<tr><td class="perf-name" rowspan="2">姓名</td>')
    for it in items:
        h.append(f'<td class="sub2" style="background:#c0392b;color:#fff">{it}</td>')
    h.append('</tr><tr>')
    for _ in items:
        h.append('<td class="sub2" style="background:#c0392b;color:#fff">完成</td>')
    h.append('</tr>')
    for name in PEOPLE:
        if name not in people:
            continue
        dd = people[name].get('dailyDone', {}) or {}
        h.append(f'<tr><td class="perf-name">{esc(name)}</td>')
        for it in items:
            v = dd.get(it, 0)
            h.append(f'<td class="num">{fmt_int(v)}</td>')
        h.append('</tr>')
    # 合计
    h.append('<tr class="perf-tot"><td class="perf-name">合计</td>')
    for it in items:
        s = sum(n((people.get(p, {}).get('dailyDone', {}) or {}).get(it, 0)) for p in PEOPLE if p in people)
        h.append(f'<td class="num">{fmt_int(s)}</td>')
    h.append('</tr></table>')

    # ===== 每日任务缺口表 =====
    h.append('<table style="margin-top:24px">')
    h.append(f'<tr><td class="perf-head" colspan="{len(items)+1}">每日任务</td></tr>')
    h.append('<tr><td class="perf-name" rowspan="2">姓名</td>')
    for it in items:
        h.append(f'<td class="sub2" style="background:#c0392b;color:#fff">{it}</td>')
    h.append('</tr><tr>')
    for _ in items:
        h.append('<td class="sub2" style="background:#c0392b;color:#fff">任务</td>')
    h.append('</tr>')
    for name in PEOPLE:
        if name not in people:
            continue
        gp = people[name].get('dailyGap', {}) or {}
        h.append(f'<tr><td class="perf-name">{esc(name)}</td>')
        for it in items:
            v = -n(gp.get(it, 0))  # 缺口取负显示（与底表一致）
            v = int(round(v))
            cls = 'g' if v >= 0 else 'r'
            h.append(f'<td class="num {cls}">{v:,}</td>')
        h.append('</tr>')
    h.append('<tr class="perf-tot"><td class="perf-name">合计</td>')
    for it in items:
        s = sum(-n((people.get(p, {}).get('dailyGap', {}) or {}).get(it, 0)) for p in PEOPLE if p in people)
        s = int(round(s))
        cls = 'g' if s >= 0 else 'r'
        h.append(f'<td class="num {cls}">{s:,}</td>')
    h.append('</tr></table>')
    h.append('</div>')
    return '\n'.join(h)


def screenshot(html, out_path, width=3600):
    """用系统 Chrome 把 HTML 截成 PNG（按内容高度）"""
    tmp_html = out_path + '.html'
    with open(tmp_html, 'w', encoding='utf-8') as f:
        f.write(f'<!DOCTYPE html><html><head><meta charset="UTF-8"><style>{CSS}</style></head><body>{html}</body></html>')
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, headless=True)
        page = browser.new_page(viewport={'width': width, 'height': 1400}, device_scale_factor=1)
        page.goto('file://' + tmp_html, wait_until='load')
        page.wait_for_timeout(300)
        el = page.query_selector('.wrap')
        el.screenshot(path=out_path)
        browser.close()
    os.remove(tmp_html)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(BASE, 'daily_images'))
    ap.add_argument('--date', help='覆盖校验日期（仅调试用）；正常不留空')
    args = ap.parse_args()

    d = load_data()
    if args.date:
        d['meta']['date'] = args.date  # 仅本地调试
    os.makedirs(args.out, exist_ok=True)

    date = d['meta']['date'][:10]
    p1 = os.path.join(args.out, f'李家村_绩效打分表_{date}.png')
    p2 = os.path.join(args.out, f'李家村_每日达成任务_{date}.png')

    screenshot(build_img1(d), p1)
    screenshot(build_img2(d), p2)

    # 校验：文件存在且非空
    for p in (p1, p2):
        if not os.path.exists(p) or os.path.getsize(p) < 5000:
            raise RuntimeError(f'图片生成异常: {p}')

    print(json.dumps({
        'ok': True,
        'date': date,
        'generatedAt': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'fetchTime': d['meta'].get('fetchTime'),
        'snapshot': d['meta'].get('generatedAt'),
        'images': [p1, p2],
        'sizes': [os.path.getsize(p1), os.path.getsize(p2)],
    }, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
        sys.exit(1)
