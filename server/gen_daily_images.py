#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【18号 2026-11-19 v2 照搬底表样式】每天最后一次刷新后，用看板 data.json 的李家村数据，
生成两张与桌面底表【一模一样】的图片：
  图1 = 绩效打分表（业绩指标 + 全科生 + 考核项，3员工分行 + 合计）
  图2 = 员工每日达成表 + 每日任务缺口表

样式参数全部取自底表真实单元格 + 原图像素取色：
  表头深蓝 #7F90C8 / 红头 #C00000 / 姓名列浅蓝 #EBF0FA
  业绩合计橙 #F4B084 / 达成任务合计绿 #BCE6D8
  绿字 #00B050 / 红字 #FF0000 / 字号 11px / 行高约24px
  红绿规则：完成率 >= 时间进度(当日/当月天数) 显示绿，否则红（底表条件格式口径）

只读看板数据，不碰底表、不依赖 Excel 重算。数据与看板完全同源。
失败显式报错退出，绝不产出旧图冒充。
"""
import os, sys, json, argparse, subprocess, shutil, calendar
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BASE)
DATA_JSON = os.path.join(REPO, 'data.json')
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
PEOPLE = ['邵乐乐', '杨丽华', '李泽']              # 图1业绩区/全科生区：3人（原图口径）
PEOPLE2 = ['邵乐乐', '杨丽华', '李泽', '张博晨']    # 图2达成/任务表：原图含张博晨行

# 业绩区品类：key -> 底表表头
PERF_COLS = [
    ('毛利', '毛利（35分）'), ('手机', '手机（15分）'), ('PC', 'PC'),
    ('平板', '平板'), ('穿戴', '穿戴'), ('音频', '音频'),
    ('HD', 'HD（5分）'), ('智慧办公', '💻 智慧办公（8分）'),
    ('音频穿戴', '🎧 音频穿戴（8分）'), ('销额', '销额'),
]


def n(v):
    return float(v) if v is not None else 0.0


def fmt_int(v):
    return f'{int(round(n(v)))}'


def fmt_rate(v):
    return f'{n(v) * 100:.0f}%'


def fmt_rate1(v):
    return f'{n(v) * 100:.1f}%'


def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def load_data():
    if not os.path.exists(DATA_JSON):
        raise RuntimeError(f'看板数据不存在: {DATA_JSON}')
    d = json.load(open(DATA_JSON, encoding='utf-8'))
    ljc = d.get('ljc')
    if not ljc or not ljc.get('meta', {}).get('date'):
        raise RuntimeError('data.json 缺 ljc 或 date')
    today = datetime.now().strftime('%Y-%m-%d')
    if ljc['meta']['date'][:10] != today:
        raise RuntimeError(f"看板数据日期={ljc['meta']['date']} 非今天({today})，拒绝用旧数据出图")
    return ljc


def time_progress(date_str):
    """底表 I1 = DAY(B1)/当月天数 = 当日/当月总天数"""
    y, m, dd = [int(x) for x in date_str[:10].split('-')]
    return dd / calendar.monthrange(y, m)[1]


# ============ 完全照搬底表样式（高清像素级对齐原图 3299x561）============
# 原图实测：子列宽约70-87px、数据行高40px、文字约14px。
# 基准 CSS 用 scale=2 渲染：下面 1倍尺寸×2 ≈ 原图。
#   列宽靠 padding 拉到约 70px（原图）→ 1倍需约 35px/列
#   行高 1倍 20px ×2 = 40px（原图）
#   字号 1倍 7px ×2 = 14px（原图）
CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Microsoft YaHei','PingFang SC Semibold','PingFang SC',sans-serif;background:#fff;color:#000;padding:0;-webkit-font-smoothing:auto;-webkit-text-stroke:1px currentColor}
.wrap{display:inline-block;padding:0;background:#fff}
table{border-collapse:collapse;font-size:17px;font-weight:900;table-layout:fixed}
td{border:1px solid #000;text-align:center;padding:2px 18px;white-space:nowrap;height:44px;color:#000;line-height:44px}
/* 顶部日期条 */
.hd{position:relative;display:flex;justify-content:flex-start;align-items:center;height:26px;padding:0 8px;font-size:17px;font-weight:900;border:1px solid #000;border-bottom:none}
.hd .d{color:#000}.hd .p{position:absolute;left:50%;transform:translateX(-50%);color:#ff0000;font-size:20px}
/* 表头 */
.grp{background:#7f90c8;color:#fff;font-size:17px}
.sub{background:#7f90c8;color:#fff;font-size:17px}
.sub .gap-h{color:#ff0000}
.name-h{background:#ff0000;color:#fff;font-size:17px}
.name{background:#ebf0fa;font-size:17px}
.name-l{background:#ebf0fa;font-size:17px;text-align:left;padding-left:6px}
.perf-h{background:#c00000;color:#fff;font-size:17px}
/* 数字色(底表默认黑字) */
.num{color:#08090c}
.r-g{color:#00b050}.r-r{color:#ff0000}
/* 合计行 */
.tot-perf td{background:#f4b084}
.tot-green td{background:#bce6d8}
/* 分块标题带 */
.band{background:#7f90c8;color:#fff}
"""


def rate_cls(rate, tp):
    """底表条件格式：完成率 < 时间进度 绿，>= 时间进度 红（超前达标用红突出）"""
    return 'r-r' if n(rate) >= tp else 'r-g'


def build_img1(d):
    meta = d['meta']
    date = meta['date'][:10]
    tp = time_progress(date)
    store_p = d['store']['performance']
    people = d['people']
    rows = [(name, people[name]) for name in PEOPLE if name in people]

    h = ['<div class="wrap">']
    h.append(f'<div class="hd"><span class="d">{date}</span><span class="p">{tp*100:.0f}%</span></div>')

    # ===== 业绩指标区 =====
    h.append('<table>')
    # 组头：姓名(红) + 各品类(蓝) + 绩效(红)
    h.append('<tr><td class="name-h" rowspan="2" style="width:48px">姓名</td>')
    for key, label in PERF_COLS:
        h.append(f'<td class="grp" colspan="4">{label}</td>')
    h.append('<td class="perf-h" rowspan="2" style="width:34px">绩效</td></tr>')
    # 子头
    h.append('<tr>')
    for _ in PERF_COLS:
        h.append('<td class="sub">任务</td><td class="sub">完成</td>'
                 '<td class="sub"><span class="gap-h">缺口</span></td><td class="sub">完成率</td>')
    h.append('</tr>')
    # 数据行
    for name, pv in rows:
        perf = pv.get('performance', {})
        h.append(f'<tr><td class="name">{esc(name)}</td>')
        for key, _ in PERF_COLS:
            v = perf.get(key, {}) if isinstance(perf.get(key), dict) else {}
            t, dn, rt = v.get('task'), v.get('done'), v.get('rate')
            g = n(dn) - n(t)
            h.append(f'<td class="num">{fmt_int(t)}</td>'
                     f'<td class="num">{fmt_int(dn)}</td>'
                     f'<td class="num">{g:+.0f}</td>'
                     f'<td class="num {rate_cls(rt, tp)}">{fmt_rate(rt)}</td>')
        sc = n(perf.get('绩效'))
        h.append(f'<td class="perf-h" style="background:#c00000;color:#fff">{sc:.0f}</td></tr>')
    # 合计行（橙底）
    h.append('<tr class="tot-perf"><td class="name">合计</td>')
    for key, _ in PERF_COLS:
        v = store_p.get(key, {}) if isinstance(store_p.get(key), dict) else {}
        t, dn, rt = v.get('task'), v.get('done'), v.get('rate')
        g = n(dn) - n(t)
        h.append(f'<td>{fmt_int(t)}</td><td>{fmt_int(dn)}</td>'
                 f'<td>{g:+.0f}</td><td class="{rate_cls(rt, tp)}">{fmt_rate(rt)}</td>')
    h.append(f'<td class="perf-h">{n(store_p.get("绩效")):.0f}</td></tr>')
    h.append('</table>')

    # ===== 第二块：严格照底表 3 层表头结构 =====
    # 大标题层：姓名 | 全科生(14) | 考核项(7) | 增值20分(4) | 健康度(4) | 星联会员(3)
    h.append('<table style="margin-top:8px">')
    h.append('<tr><td class="name-h" rowspan="3" style="width:48px">姓名</td>'
             '<td class="band" colspan="14">全科生</td>'
             '<td class="band" colspan="7">考核项</td>'
             '<td class="band" colspan="4">增值（20分）</td>'
             '<td class="band" colspan="4">健康度</td>'
             '<td class="band" colspan="3">星联会员</td></tr>')
    # 子标题层（中）：电信业务/会员搭售/回收/贴膜 | 考核机型/乐收机/太力回收 | 增值 | 12.00% | 星联优享/尊享/合计
    h.append('<tr>'
             '<td class="grp" colspan="4">电信业务（5分）</td>'
             '<td class="grp" colspan="4">会员搭售率（30%）</td>'
             '<td class="grp" colspan="3">回收（25%）</td>'
             '<td class="grp" colspan="3">贴膜（50%）</td>'
             '<td class="grp" colspan="2">考核机型-100/台</td>'
             '<td class="grp" colspan="2">乐收机</td>'
             '<td class="grp" colspan="3">太力回收</td>'
             '<td class="grp" colspan="4">增值</td>'
             '<td class="grp" colspan="4">12.00%</td>'
             '<td class="grp">星联优享</td><td class="grp">星联尊享</td><td class="grp">合计</td>'
             '</tr>')
    # 明细层：逐列标题（共33列）
    h.append('<tr>'
             # 电信业务: 合约/完成/缺口/完成率
             '<td class="sub">合约</td><td class="sub">完成</td><td class="sub"><span class="gap-h">缺口</span></td><td class="sub">完成率</td>'
             # 会员搭售: 终端总量/会员/缺口/搭售率
             '<td class="sub">终端总量</td><td class="sub">会员</td><td class="sub"><span class="gap-h">缺口</span></td><td class="sub">搭售率</td>'
             # 回收: 单数/缺口/搭售率
             '<td class="sub">单数</td><td class="sub"><span class="gap-h">缺口</span></td><td class="sub">搭售率</td>'
             # 贴膜: 单数/缺口/搭售率
             '<td class="sub">单数</td><td class="sub"><span class="gap-h">缺口</span></td><td class="sub">搭售率</td>'
             # 考核机型: 任务/缺口
             '<td class="sub">任务</td><td class="sub"><span class="gap-h">缺口</span></td>'
             # 乐收机: 单量/增值
             '<td class="sub">单量</td><td class="sub">增值</td>'
             # 太力回收: 单量/金额/增值
             '<td class="sub">单量</td><td class="sub">金额</td><td class="sub">增值</td>'
             # 增值: 任务/完成/缺口/完成率
             '<td class="sub">任务</td><td class="sub">完成</td><td class="sub"><span class="gap-h">缺口</span></td><td class="sub">完成率</td>'
             # 健康度: 优惠券/占比/毛利率/增值率
             '<td class="sub">优惠券</td><td class="sub">占比</td><td class="sub">毛利率</td><td class="sub">增值率</td>'
             # 星联会员: 完成/完成/完成
             '<td class="sub">完成</td><td class="sub">完成</td><td class="sub">完成</td>'
             '</tr>')
    # 数据行
    for name, pv in rows:
        q = pv.get('qcs', {})
        D = lambda k: q.get(k, {}) if isinstance(q.get(k), dict) else {}
        hy, mem, rec, tm = D('合约'), D('会员搭售率'), D('回收搭售率'), D('贴膜率')
        kh, ls, tl, zz = D('考核机型'), D('乐机收'), D('太力回收'), D('增值')
        jk, xl = D('健康度'), D('星联会员')
        h.append(f'<tr><td class="name">{esc(name)}</td>')
        # 电信业务: 合约 任务/完成/缺口/完成率
        h.append(f'<td>{fmt_int(hy.get("task"))}</td><td>{fmt_int(hy.get("done"))}</td>'
                 f'<td>{fmt_int(hy.get("gap"))}</td><td class="{rate_cls(hy.get("rate"), tp)}">{fmt_rate(hy.get("rate"))}</td>')
        # 会员搭售: 终端总量/会员/缺口/搭售率
        h.append(f'<td>{fmt_int(mem.get("terminal"))}</td><td>{fmt_int(mem.get("care"))}</td>'
                 f'<td>{fmt_int(mem.get("gap"))}</td><td class="{rate_cls(mem.get("rate"), mem.get("target"))}">{fmt_rate1(mem.get("rate"))}</td>')
        # 回收: 单数/缺口/搭售率
        h.append(f'<td>{fmt_int(rec.get("orders"))}</td><td>{fmt_int(rec.get("gap"))}</td>'
                 f'<td class="{rate_cls(rec.get("rate"), rec.get("target"))}">{fmt_rate1(rec.get("rate"))}</td>')
        # 贴膜: 单数/缺口/搭售率
        h.append(f'<td>{fmt_int(tm.get("orders"))}</td><td>{fmt_int(tm.get("gap"))}</td>'
                 f'<td class="{rate_cls(tm.get("rate"), tm.get("target"))}">{fmt_rate1(tm.get("rate"))}</td>')
        # 考核机型: 任务/缺口
        h.append(f'<td>{fmt_int(kh.get("task"))}</td><td>{fmt_int(kh.get("gap"))}</td>')
        # 乐收机: 单量/增值
        h.append(f'<td>{fmt_int(ls.get("orders"))}</td><td>{fmt_int(ls.get("amount"))}</td>')
        # 太力回收: 单量/金额/增值
        h.append(f'<td>{fmt_int(tl.get("orders"))}</td><td>{fmt_int(tl.get("amount"))}</td><td>{fmt_int(tl.get("增值"))}</td>')
        # 增值: 任务/完成/缺口/完成率
        zt, zd = zz.get('task'), zz.get('done')
        h.append(f'<td>{fmt_int(zt)}</td><td>{fmt_int(zd)}</td>'
                 f'<td>{n(zd)-n(zt):+.0f}</td><td class="{rate_cls(zz.get("rate"), tp)}">{fmt_rate(zz.get("rate"))}</td>')
        # 健康度: 优惠券/占比/毛利率/增值率
        h.append(f'<td>{fmt_int(jk.get("coupon"))}</td><td>{fmt_rate1(jk.get("ratio"))}</td>'
                 f'<td>{fmt_rate1(jk.get("grossMargin"))}</td><td>{fmt_rate1(jk.get("增值率"))}</td>')
        # 星联会员: 完成/完成/完成 (星联优享/星联尊享/合计)
        h.append(f'<td>{fmt_int(xl.get("优享"))}</td><td>{fmt_int(xl.get("尊享"))}</td><td>{fmt_int(xl.get("合计"))}</td>')
        h.append('</tr>')
    h.append('</table>')
    h.append('</div>')
    return '\n'.join(h)


def build_img2(d):
    """员工每日达成表 + 每日任务缺口表（红头白字、3员工+合计、绿底合计）"""
    meta = d['meta']
    date = meta['date'][:10]
    mmdd = date[5:].replace('-', '-')
    people = d['people']
    items = ['手机', '毛利', '增值', '智慧办公', '音频穿戴', 'HD', '会员', '回收', '贴膜', '合约', '滞销', '优享/会员']

    h = ['<div class="wrap">']

    def one_table(title, sub_label, field, gap=False):
        t = ['<table>']
        t.append(f'<tr><td class="perf-h" colspan="{len(items)+1}" style="height:26px">{title}</td></tr>')
        t.append(f'<tr><td class="name-h" rowspan="2" style="width:64px">姓名</td>')
        for it in items:
            t.append(f'<td class="perf-h">{it}</td>')
        t.append('</tr><tr>')
        for _ in items:
            t.append(f'<td class="perf-h">{sub_label}</td>')
        t.append('</tr>')
        for name in PEOPLE2:
            if name not in people:
                continue
            src = (people[name].get(field, {}) or {})
            t.append(f'<tr><td class="name">{esc(name)}</td>')
            for it in items:
                v = n(src.get(it, 0))
                if gap:
                    v = -v
                iv = int(round(v))
                # 缺口表：正数红(还欠)、负数绿(超额)、0绿 —— 底表条件格式口径
                cls = 'r-r' if iv > 0 else 'r-g'
                t.append(f'<td class="num {cls if gap else ""}">{iv}</td>')
            t.append('</tr>')
        # 合计
        t.append('<tr class="tot-green"><td class="name">合计</td>')
        for it in items:
            s = sum(n((people.get(p, {}).get(field, {}) or {}).get(it, 0)) for p in PEOPLE2 if p in people)
            if gap:
                s = -s
            t.append(f'<td>{int(round(s))}</td>')
        t.append('</tr></table>')
        return ''.join(t)

    h.append(one_table(f'{mmdd}达成', '完成', 'dailyDone', gap=False))
    h.append('<div style="height:22px"></div>')
    h.append(one_table('每日任务', '任务', 'dailyGap', gap=True))
    h.append('</div>')
    return '\n'.join(h)


def screenshot(html, out_path, width=7000, scale=2):
    """高清渲染：CSS 1倍布局已对齐原图 3299x561，device_scale_factor=2 让文字边框
    按矢量重绘到 2 倍物理像素（约 6600px 宽），放大依然清晰。"""
    tmp_html = out_path + '.html'
    with open(tmp_html, 'w', encoding='utf-8') as f:
        f.write(f'<!DOCTYPE html><html><head><meta charset="UTF-8"><style>{CSS}</style></head><body>{html}</body></html>')
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, headless=True)
        page = browser.new_page(viewport={'width': width, 'height': 2400}, device_scale_factor=scale)
        page.goto('file://' + tmp_html, wait_until='load')
        page.wait_for_timeout(400)
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
        d['meta']['date'] = args.date
    os.makedirs(args.out, exist_ok=True)

    date = d['meta']['date'][:10]
    p1 = os.path.join(args.out, f'李家村_绩效打分表_{date}.png')
    p2 = os.path.join(args.out, f'李家村_每日达成任务_{date}.png')

    screenshot(build_img1(d), p1)
    screenshot(build_img2(d), p2)

    for p in (p1, p2):
        if not os.path.exists(p) or os.path.getsize(p) < 5000:
            raise RuntimeError(f'图片生成异常: {p}')

    print(json.dumps({
        'ok': True,
        'date': date,
        'generatedAt': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'fetchTime': d['meta'].get('fetchTime'),
        'snapshot': d['meta'].get('generatedAt'),
        'timeProgress': round(time_progress(date) * 100),
        'images': [p1, p2],
        'sizes': [os.path.getsize(p1), os.path.getsize(p2)],
    }, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False))
        sys.exit(1)
