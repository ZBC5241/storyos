#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【18号 2026-11-13】每小时把「今天的使用记录监控台」推一份文件到企微群。

通道（16:50 改）：
  主 = 群机器人 webhook（晨哥 16:48 提供）。这是独立于「智能机器人」的通道，
       智能机器人「消息」权限 2026-11-13 过期(errcode 850003)不影响它。
  兜底 = wecom-cli aibot 群发；仅当 webhook 缺失/失效时才试，权限过期就静默跳过。

凭据安全（红线）：webhook 地址只放在本机
  ~/.workbuddy/secrets/wecom_group_webhook.txt
绝不写进 storyos 仓库——该仓库有定时任务会自动 push 到公网 GitHub Pages。

口径：只装今天（audit_report 默认 scope='today'），历史记录不进页面也不进群。
      只发文件、不发文字说明；同一小时成功推过自动跳过；无论成败都留痕。
"""
import os
import sys
import json
import shutil
import subprocess
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import audit_report

LOGS = os.path.join(BASE, 'logs')
PUSH_LOG = os.path.join(LOGS, 'usage_push.log')
STATE = os.path.join(LOGS, 'usage_push_state.json')
TMPDIR = '/tmp/sb_usage_push'
GROUP_NAME = '🤖李家村数据看板'
WEBHOOK_CONF = os.path.expanduser('~/.workbuddy/secrets/wecom_group_webhook.txt')
CLI = shutil.which('wecom-cli') or '/Users/mac/.workbuddy/binaries/node/cli-connector-packages/bin/wecom-cli'
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


def jparse(raw):
    raw = raw.strip()
    try:
        return json.loads(raw, strict=False)
    except Exception:
        i, j = raw.find('{'), raw.rfind('}')
        if i < 0 or j <= i:
            raise ValueError('CLI 回包不是 JSON: ' + raw[:200])
        return json.loads(raw[i:j + 1], strict=False)


def run_cli(args):
    p = subprocess.run([CLI] + args, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=180)
    return jparse(p.stdout.decode('utf-8', 'replace'))


def find_key(o, k):
    if isinstance(o, dict):
        if k in o:
            return o[k]
        for v in o.values():
            r = find_key(v, k)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in o:
            r = find_key(v, k)
            if r is not None:
                return r
    return None


def auth_expired(resp):
    return isinstance(resp, dict) and resp.get('errcode') in (850002, 850003)


def load_webhook():
    """凭据只从本机读；校验域名，防止配置被换成陌生地址后误发。"""
    try:
        u = open(WEBHOOK_CONF, encoding='utf-8').read().strip()
    except Exception:
        return ''
    if not u.startswith('https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key='):
        return ''
    return u


def send_by_webhook(path, filename):
    """群机器人：先上传临时素材 -> 再发 file 消息。群里只出现一个文件，无文字。"""
    import requests
    u = load_webhook()
    if not u:
        return False, 'webhook_missing', '本机没找到有效的 webhook 配置'
    up_url = u.replace('/cgi-bin/webhook/send', '/cgi-bin/webhook/upload_media') + '&type=file'
    try:
        with open(path, 'rb') as f:
            r = requests.post(up_url, files={'media': (filename, f, 'text/html')}, timeout=60)
        up = r.json()
        if up.get('errcode') != 0:
            return False, 'upload_failed', json.dumps(up, ensure_ascii=False)[:200]
        media_id = up.get('media_id')
        if not media_id:
            return False, 'upload_failed', '上传回包缺 media_id'
        s = requests.post(u, json={'msgtype': 'file', 'file': {'media_id': media_id}}, timeout=60)
        sd = s.json()
        if sd.get('errcode') != 0:
            return False, 'send_failed', json.dumps(sd, ensure_ascii=False)[:200]
        return True, 'webhook', filename
    except Exception as e:
        return False, 'webhook_exception', '%s: %s' % (type(e).__name__, str(e)[:180])


def send_by_cli(path, filename):
    """兜底：智能机器人通道。权限过期时静默跳过，不刷群、不反复告警。"""
    try:
        resp = run_cli(['message', 'aibot', 'sessions', 'list'])
        if auth_expired(resp):
            return False, 'wecom_auth_expired', '智能机器人消息权限过期'
        sessions = find_key(resp, 'sessions') or []
        hits = [x for x in sessions if x.get('chat_name') == GROUP_NAME]
        if len(hits) != 1:
            return False, 'group_not_unique', '目标群命中 %d 个' % len(hits)
        chat_id = hits[0].get('chat_id')
        up = run_cli(['media', 'upload', '--json',
                      json.dumps({'file_path': path}, ensure_ascii=False)])
        if auth_expired(up):
            return False, 'wecom_auth_expired', '上传阶段权限过期'
        media_id = find_key(up, 'media_id')
        if not media_id:
            return False, 'upload_failed', '兜底上传没拿到 media_id'
        send = run_cli(['message', 'aibot', 'send', '--json',
                        json.dumps({'chat_id': chat_id, 'msg_type': 'file',
                                    'file': {'media_id': media_id}}, ensure_ascii=False)])
        if auth_expired(send):
            return False, 'wecom_auth_expired', '发送阶段权限过期'
        ok = send.get('success') is True or send.get('ok') is True or (
            send.get('errcode') in (0, None) and send.get('errmsg') in (None, 'ok'))
        return (True, 'aibot', filename) if ok else (False, 'send_failed', '兜底发送失败')
    except Exception as e:
        return False, 'cli_exception', '%s: %s' % (type(e).__name__, str(e)[:180])


def notify_once(key, msg):
    """本机通知按「同一天同一类」去重，避免反复打扰。"""
    if DRY:
        return
    st = load_state()
    tag = key + '|' + datetime.now().strftime('%Y-%m-%d')
    if st.get('lastNotify') == tag:
        return
    try:
        subprocess.run(['osascript', '-e',
                        'display notification "%s" with title "18号 · 使用记录推送"' % msg],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except Exception:
        pass
    st['lastNotify'] = tag
    save_state(st)


def load_state():
    try:
        with open(STATE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    if DRY:
        return
    os.makedirs(LOGS, exist_ok=True)
    tmp = STATE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE)


def main():
    hour = datetime.now().strftime('%Y-%m-%d %H')
    state = load_state()
    if state.get('lastOkHour') == hour and not DRY and not FORCE:
        log('SKIP 本小时已成功推过一版:' + str(state.get('lastFile')))
        out({'ok': True, 'skipped': True, 'reason': 'already_pushed_this_hour',
             'rows': state.get('lastRows')})

    # 1. 重新生成最新版（只装今天），与企微通道无关
    rows = audit_report.build()
    src = audit_report.OUT
    stamp = datetime.now().strftime('%Y-%m-%d-%H时')
    filename = '使用记录-%s.html' % stamp
    os.makedirs(TMPDIR, exist_ok=True)
    dst = os.path.join(TMPDIR, filename)
    shutil.copyfile(src, dst)
    size = os.path.getsize(dst)

    if DRY:
        log('OK 今日%s条 -> (空跑不发群) | %s (%s字节) [DRYRUN]' %
            (rows, filename, size))
        out({'ok': True, 'rows': rows, 'file': filename, 'size': size,
             'channel': 'dryrun', 'dryRun': True})

    # 2. 主通道：群机器人 webhook
    ok, channel, detail = send_by_webhook(dst, filename)

    # 3. 主通道没成才走兜底
    if not ok:
        log('WARN webhook 未成功 (%s: %s)，试兜底通道' % (channel, detail))
        ok, channel, detail = send_by_cli(dst, filename)

    if not ok:
        log('FAIL 本轮未推送 | %s: %s | 今日%s条' % (channel, detail, rows))
        if channel == 'wecom_auth_expired':
            notify_once('auth', '企微两条通道都发不出去，使用记录暂时推不到群里')
            out({'ok': True, 'skipped': True, 'reason': 'wecom_auth_expired', 'rows': rows})
        notify_once('push', '使用记录文件推送失败，本轮未推到群里')
        out({'ok': False, 'error': channel, 'detail': detail, 'rows': rows})

    state['lastOkHour'] = hour
    state['lastFile'] = filename
    state['lastRows'] = rows
    state['lastAt'] = now()
    state['lastChannel'] = channel
    save_state(state)
    # webhook 是「发到地址绑定的那个群」，不冒充 aibot 的群名，留痕如实写
    target = '企微群(webhook 地址绑定)' if channel == 'webhook' else GROUP_NAME
    log('OK 今日%s条 -> %s | %s (%s字节) | 通道=%s' %
        (rows, target, filename, size, channel))
    out({'ok': True, 'rows': rows, 'file': filename, 'size': size,
         'channel': channel, 'generatedAt': now(), 'dryRun': False})


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log('FAIL 未捕获异常: %s: %s' % (type(e).__name__, e))
        out({'ok': False, 'error': 'exception', 'detail': str(e)[:300]})
