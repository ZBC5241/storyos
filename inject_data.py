#!/usr/bin/env python3
# inject_data.py —— storyos 双店看板数据注入器（2026-09-24 建，2026-10-23 B 方案改造）
# 职责：把 shop/data.json（李家村·店长号管线产出）+ shophyc/data.json（华阳城·经理号管线产出）
#       ① 写入售票厅数据目录 server/data/{ljc,hyc}.json（服务端鉴权后下发的唯一数据源）
#       ② 原地替换进 index.html 的 EMBEDDED_DATA_LJC / EMBEDDED_DATA_HYC 常量后【立即剥离为 null】
#          —— GitHub Pages 上的 index.html 永远是空壳，经营数据不再随页面泄露
# 设计：
#   - 零拉数、零浏览器、零用友登录（复用上游，杜绝账号互踢/导出排队）
#   - 读源 JSON 带重试（防上游写入窗口读到半截文件）
#   - 数据无变化则不写任何文件（git diff 为空 → update_storyos.sh 自动跳过 push）
#   - ⚠️ Pages 侧空壳只在【前端代码变化】时才需要 push；数据更新走售票厅，不经过 git
import json, sys, time, os

BASE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(BASE, 'index.html')
WORKSPACE = '/Users/mac/.local/share/TeleAgent/TeleAgent的工作空间'
SERVER_DATA = os.path.join(BASE, 'server', 'data')
SOURCES = [
    (os.path.join(WORKSPACE, 'shop/data.json'), 'EMBEDDED_DATA_LJC', '李家村', 'ljc'),
    (os.path.join(WORKSPACE, 'shophyc/data.json'), 'EMBEDDED_DATA_HYC', '华阳城', 'hyc'),
]

def read_json(path, tries=3, wait=5):
    for i in range(tries):
        try:
            with open(path, 'rb') as f:
                return json.loads(f.read().decode('utf-8'))
        except Exception as e:
            print(f"  ⚠️ 读取 {os.path.basename(path)} 失败({e})，重试 {i+1}/{tries}")
            time.sleep(wait)
    return None

def replace_constant(html, name, new_txt):
    """把 const NAME = <旧值>; 的旧值替换为 new_txt。旧值可能是 JSON 对象，也可能是已剥离的 null。"""
    dec = json.JSONDecoder()
    i = html.find(f'const {name} = ')
    if i < 0:
        return None, None
    j = html.find('=', i) + 1
    while html[j] in ' \t':
        j += 1
    if html[j] == 'n':                       # 已剥离态：null
        end = html.find(';', j)
        old_txt = 'null'
    else:
        old, end = dec.raw_decode(html[j:])
        old_txt = json.dumps(old, ensure_ascii=False, separators=(',', ':'))
    return html[:j] + new_txt + html[j + end:], old_txt

def main():
    os.makedirs(SERVER_DATA, exist_ok=True)
    with open(HTML, 'r', encoding='utf-8') as f:
        html = f.read()
    changed = False
    for path, name, label, store in SOURCES:
        data = read_json(path)
        if data is None:
            print(f"  ❌ {label} 源数据读取失败，本档保留旧数据")
            continue
        new_txt = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        html2, old_txt = replace_constant(html, name, new_txt)
        if html2 is None:
            print(f"  ❌ index.html 中未找到 {name}")
            sys.exit(1)
        if new_txt == old_txt:
            print(f"  ⏭ {label}({name}) 无变化")
            html = html2                     # 无变化也统一走后面的剥离，保持空壳态
        else:
            print(f"  ✅ {label}({name}) 已注入 meta.date={data.get('meta', {}).get('date')}")
            html = html2
            changed = True
        # 售票厅数据源：与本档注入的数据严格一致
        with open(os.path.join(SERVER_DATA, store + '.json'), 'w', encoding='utf-8') as f:
            f.write(new_txt)
        # 剥离：Pages 页面里的数据常量置 null（前端改从售票厅拉取，见 index.html 登录/拉数逻辑）
        html, _ = replace_constant(html, name, 'null')
    if not changed:
        # 数据没变时仍要确保剥离态 + 售票厅文件在位（首次改造 / 中途回滚过）
        stripped = ('const EMBEDDED_DATA_LJC = null;' in html
                    and 'const EMBEDDED_DATA_HYC = null;' in html)
        if stripped and os.path.exists(os.path.join(SERVER_DATA, 'ljc.json')) \
                and os.path.exists(os.path.join(SERVER_DATA, 'hyc.json')):
            print("⏭ 双店数据均无变化，空壳态就绪，不写 index.html")
            return
    with open(HTML, 'w', encoding='utf-8') as f:
        f.write(html)
    print("✅ index.html 已更新（空壳：数据已剥离，由售票厅鉴权下发）")

main()
