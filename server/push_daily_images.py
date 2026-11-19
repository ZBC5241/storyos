#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【18号 2026-11-19】每天最后一次刷新后，把李家村两张战报图推到企微群。

流程：读看板最新 data.json -> gen_daily_images 生成两张PNG
      -> 群机器人 webhook 发 image 消息（两张图）-> 留痕/去重/失败本机通知。

红线：
  1) 只读当天真实看板数据；data.json 日期非今天 -> 直接失败，绝不推旧图。
  2) webhook 凭据只读本机 ~/.workbuddy/secrets/wecom_group_webhook.txt，不入库。
  3) 同一天成功推过就跳过（--force 可强推）；无论成败写 logs/daily_image_push.log。
  4) 失败本机通知按天去重，不刷屏。
"""
import os, sys, json, base64, hashlib, shutil, subprocess
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(BASE, 'logs')
PUSH_LOG = os.path.join(LOGS, 'daily_image_push.log')
STATE = os.path.join(LOGS, 'daily_image_push_state.json')
IMGDIR = os.path.join(BASE, 'daily_images')
WEBHOOK_CONF = os.path.expanduser('~/.workbuddy/secrets/wecom_group_webhook.txt')

DRY = '--dry-run' in sys.argv
FORCE = '--force' in sys.argv


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def log(line):
    os.makedirs(LOGS, exist_ok=True)
    with open(PUSH_LOG, 'a', encoding='utf-8') as f:
        f.write(now() + ' ' + line + '\n')


def out(obj):
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(0 if obj.get('ok') else 1)


def load_webhook():
    try:
        u = open(WEBHOOK_CONF, encoding='utf-8').read().strip()
    except Exception:
        return ''
    if not u.startswith('https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key='):
        return ''
    return u


def load_state():
    try:
        return json.load(open(STATE, encoding='utf-8'))
    except Exception:
        return {}


def save_state(st):
    if DRY:
        return
    os.makedirs(LOGS, exist_ok=True)
    tmp = STATE + '.tmp'
    json.dump(st, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
    os.replace(tmp, STATE)


def notify_once(key, msg):
    if DRY:
        return
    st = load_state()
    tag = key + '|' + datetime.now().strftime('%Y-%m-%d')
    if st.get('lastNotify') == tag:
        return
    try:
        subprocess.run(['osascript', '-e',
                        'display notification "%s" with title "18号 · 李家村战报推送"' % msg],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except Exception:
        pass
    st['lastNotify'] = tag
    save_state(st)


def gen_images():
    """调用生成脚本，返回 (info, [路径])。失败抛异常。"""
    import gen_daily_images as g
    # 直接调 main 逻辑拿输出：用 subprocess 调，拿 JSON
    py = sys.executable
    r = subprocess.run([py, os.path.join(BASE, 'gen_daily_images.py'), '--out', IMGDIR],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    raw = r.stdout.decode('utf-8', 'replace').strip()
    try:
        info = json.loads(raw)
    except Exception:
        raise RuntimeError('生成脚本回包非JSON: ' + raw[:300])
    if not info.get('ok'):
        raise RuntimeError('生成失败: ' + str(info.get('error')))
    return info, info['images']


def send_image(webhook, path):
    """企业微信群机器人：发一张 image 消息（base64 + md5）。"""
    import requests
    with open(path, 'rb') as f:
        content = f.read()
    payload = {
        'msgtype': 'image',
        'image': {
            'base64': base64.b64encode(content).decode('ascii'),
            'md5': hashlib.md5(content).hexdigest(),
        },
    }
    r = requests.post(webhook, json=payload, timeout=60)
    resp = r.json()
    return resp.get('errcode') == 0, resp


def main():
    today = datetime.now().strftime('%Y-%m-%d')
    st = load_state()
    if st.get('lastOkDate') == today and not DRY and not FORCE:
        log('SKIP 今天已成功推过: ' + str(st.get('lastAt')))
        out({'ok': True, 'skipped': True, 'reason': 'already_pushed_today',
             'lastAt': st.get('lastAt')})

    # 1) 生成两张图（内含日期新鲜度校验）
    try:
        info, imgs = gen_images()
    except Exception as e:
        log('FAIL 生成图片失败: %s' % e)
        notify_once('gen', '李家村战报图生成失败，未推送')
        out({'ok': False, 'error': 'gen_failed', 'detail': str(e)})

    if DRY:
        log('OK [DRY] 已生成两张图(未发群): %s | fetchTime=%s' %
            ([os.path.basename(x) for x in imgs], info.get('fetchTime')))
        out({'ok': True, 'dryRun': True, 'images': imgs,
             'date': info['date'], 'fetchTime': info.get('fetchTime'),
             'snapshot': info.get('snapshot')})

    webhook = load_webhook()
    if not webhook:
        log('FAIL webhook 配置缺失')
        notify_once('push', '李家村战报推送失败：webhook 配置缺失')
        out({'ok': False, 'error': 'webhook_missing'})

    # 2) 依次发两张图
    results = []
    for p in imgs:
        ok, resp = send_image(webhook, p)
        results.append({'file': os.path.basename(p), 'ok': ok, 'resp': resp})
        log('SEND %s -> %s' % (os.path.basename(p), json.dumps(resp, ensure_ascii=False)[:200]))

    failed = [x for x in results if not x['ok']]
    if failed:
        log('FAIL 有 %d 张图发送失败' % len(failed))
        notify_once('push', '李家村战报图片推送失败，本轮未完全送达')
        out({'ok': False, 'error': 'send_failed', 'results': results})

    st['lastOkDate'] = today
    st['lastAt'] = now()
    st['lastImages'] = [os.path.basename(x) for x in imgs]
    st['lastChannel'] = '企微群(webhook)'
    st['lastFetchTime'] = info.get('fetchTime')
    st['lastSnapshot'] = info.get('snapshot')
    save_state(st)
    log('OK 已推 2 张图到企微群 | fetchTime=%s 快照=%s' %
        (info.get('fetchTime'), info.get('snapshot')))
    out({'ok': True, 'date': info['date'], 'images': st['lastImages'],
         'channel': '企微群(webhook)', 'fetchTime': info.get('fetchTime'),
         'snapshot': info.get('snapshot'), 'generatedAt': now()})


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log('FAIL 未捕获异常: %s: %s' % (type(e).__name__, e))
        out({'ok': False, 'error': 'exception', 'detail': str(e)[:300]})
