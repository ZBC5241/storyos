#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""set_password.py — storyos 看板登录密码一键修改（2026-10-23 18号建）

用法:
    python3 set_password.py <新密码>

流程:
    ① 改 server/config.json 的 password
    ② 重启售票厅服务（pkill 后 launchd KeepAlive 自动拉起——密码是启动时读死的，必须重启才生效）
    ③ /api/health 探活 + /api/login 真实登录验证
"""
import json, os, sys, time, subprocess, urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(BASE, 'config.json')
ACCOUNT = 'zbc'          # 验证用账号（店长，store=all）
HEALTH = 'http://localhost:8802/api/health'
LOGIN = 'http://localhost:8802/api/login'


def main():
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        print("用法: python3 set_password.py <新密码>")
        return 1
    new = sys.argv[1].strip()
    if len(new) > 20:
        print("❌ 新密码超过20位，疑似误把整句话粘进来，已中止")
        return 1

    # ① 改配置
    cfg = json.load(open(CONF, encoding='utf-8'))
    old_len = len(cfg.get('password', ''))
    cfg['password'] = new
    with open(CONF, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"✅ config.json 密码已更新（{old_len}位 → {len(new)}位）")

    # ② 重启服务（KeepAlive 自动拉起）
    subprocess.run(['pkill', '-f', 'ticket_server.py'], capture_output=True)
    ok = False
    for _ in range(10):
        time.sleep(1)
        try:
            with urllib.request.urlopen(HEALTH, timeout=2) as r:
                if json.load(r).get('ok'):
                    ok = True
                    break
        except Exception:
            continue
    if not ok:
        print("❌ 服务10秒内未恢复，请查: launchctl list | grep storyos")
        return 2
    print("✅ 售票厅服务已重启并健康")

    # ③ 真实登录验证
    req = urllib.request.Request(
        LOGIN,
        data=json.dumps({'account': ACCOUNT, 'password': new}).encode('utf-8'),
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.load(r)
        print(f"✅ 新密码登录验证通过（store={d.get('store')}，有效期{int(d.get('expiresIn', 0)) // 3600}小时）")
    except Exception:
        print("❌ 新密码登录验证失败，请手动重跑或检查服务日志")
        return 3
    print("📌 提示：已登录员工的旧票最长48小时内仍有效；如需全员立即重新登录，"
          "把 config.json 的 token_secret 随便改一位后再跑一次本脚本即可。")
    return 0


if __name__ == '__main__':
    sys.exit(main())
