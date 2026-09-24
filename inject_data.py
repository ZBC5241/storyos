#!/usr/bin/env python3
# inject_data.py —— storyos 双店看板数据注入器（2026-09-24 建）
# 职责：把 shop/data.json（李家村·店长号管线产出）+ shophyc/data.json（华阳城·经理号管线产出）
#       原地替换进 index.html 的 EMBEDDED_DATA_LJC / EMBEDDED_DATA_HYC 常量
# 设计：
#   - 零拉数、零浏览器、零用友登录（复用上游，杜绝账号互踢/导出排队）
#   - 读源 JSON 带重试（防上游写入窗口读到半截文件）
#   - 数据无变化则不写文件（git diff 为空 → update_storyos.sh 自动跳过 push）
import json, sys, time, os

BASE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(BASE, 'index.html')
WORKSPACE = '/Users/mac/.local/share/TeleAgent/TeleAgent的工作空间'
SOURCES = [
    (os.path.join(WORKSPACE, 'shop/data.json'), 'EMBEDDED_DATA_LJC', '李家村'),
    (os.path.join(WORKSPACE, 'shophyc/data.json'), 'EMBEDDED_DATA_HYC', '华阳城'),
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

def main():
    with open(HTML, 'r', encoding='utf-8') as f:
        html = f.read()
    dec = json.JSONDecoder()
    changed = False
    for path, name, label in SOURCES:
        data = read_json(path)
        if data is None:
            print(f"  ❌ {label} 源数据读取失败，本档保留旧数据")
            continue
        i = html.find(f'const {name} = ')
        if i < 0:
            print(f"  ❌ index.html 中未找到 {name}")
            sys.exit(1)
        start = html.find('{', i)
        old, end = dec.raw_decode(html[start:])
        new_txt = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        old_txt = json.dumps(old, ensure_ascii=False, separators=(',', ':'))
        if new_txt == old_txt:
            print(f"  ⏭ {label}({name}) 无变化")
            continue
        html = html[:start] + new_txt + html[start + end:]
        print(f"  ✅ {label}({name}) 已注入 meta.date={data.get('meta', {}).get('date')}")
        changed = True
    if not changed:
        print("⏭ 双店数据均无变化，不写 index.html")
        return
    with open(HTML, 'w', encoding='utf-8') as f:
        f.write(html)
    print("✅ index.html 已更新")

main()
