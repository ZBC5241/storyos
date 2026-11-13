#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【18号 2026-11-13】每小时把「今天的使用记录监控台」推一份文件到企微群。

口径：只装今天（audit_report 默认 scope='today'），历史记录不进页面、也不进群。
群推送：只发文件，不发文字说明；chat_id 每次从 sessions list 现取，不复用旧值。
防重复：同一小时成功推过就跳过；权限过期时静默跳过并留痕，不刷群、不反复告警。
失败留痕：server/logs/usage_push.log 无论成败都写一行，绝不「零留痕」。
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
CLI = shutil.which('wecom-cli') or '/Users/mac/.workbuddy/binaries/node/cli-connector-packages/bin/wecom-cli'
DRY = '--dry-run' in sys.argv


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


def run_cli(args, fake=None):
    if DRY:
        return fake
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


def notify_once(key, msg):
    """本机通知按「同一天同一类」去重，权限没恢复时不反复打扰。"""
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
    if state.get('lastOkHour') == hour and not DRY:
        log('SKIP 本小时已成功推过一版:' + str(state.get('lastFile')))
        out({'ok': True, 'skipped': True, 'reason': 'already_pushed_this_hour',
             'rows': state.get('lastRows')})

    # 1. 重新生成最新版（只装今天），这一步与企微无关，权限过期也要保持页面新鲜
    rows = audit_report.build()
    src = audit_report.OUT
    stamp = datetime.now().strftime('%Y-%m-%d-%H时')
    filename = '使用记录-%s.html' % stamp
    os.makedirs(TMPDIR, exist_ok=True)
    dst = os.path.join(TMPDIR, filename)
    shutil.copyfile(src, dst)

    # 2. 现取会话，精确匹配群名
    sessions_resp = run_cli(['message', 'aibot', 'sessions', 'list'],
                            fake={'sessions': [{'chat_name': GROUP_NAME,
                                                'chat_id': 'dry-run',
                                                'chat_type': 'group'}]})
    if auth_expired(sessions_resp):
        log('SKIP 机器人消息权限过期(errcode=%s)，本轮未推送 | 今日%s条' %
            (sessions_resp.get('errcode'), rows))
        notify_once('auth', '企微机器人消息权限已过期，使用记录暂时推不到群里')
        out({'ok': True, 'skipped': True, 'reason': 'wecom_auth_expired', 'rows': rows})
    sessions = find_key(sessions_resp, 'sessions') or []
    hits = [x for x in sessions if x.get('chat_name') == GROUP_NAME]
    if len(hits) != 1:
        log('FAIL 目标群命中 %d 个，停止发送' % len(hits))
        notify_once('target', '没找到唯一的推送目标群，本轮未推送')
        out({'ok': False, 'error': 'group_not_unique', 'matched': len(hits)})
    chat_id = hits[0].get('chat_id')
    if not chat_id:
        log('FAIL 会话缺少 chat_id')
        out({'ok': False, 'error': 'missing_chat_id'})

    # 3. 上传文件
    up = run_cli(['media', 'upload', '--json',
                  json.dumps({'file_path': dst}, ensure_ascii=False)],
                 fake={'type': 'file', 'media_id': 'dry-run-media'})
    if auth_expired(up):
        log('SKIP 上传阶段权限过期，本轮未推送')
        out({'ok': True, 'skipped': True, 'reason': 'wecom_auth_expired', 'rows': rows})
    media_id = find_key(up, 'media_id')
    if not media_id:
        log('FAIL 上传没拿到 media_id | 回包:' + json.dumps(up, ensure_ascii=False)[:300])
        notify_once('upload', '文件上传失败，本轮未推送')
        out({'ok': False, 'error': 'upload_failed'})

    # 4. 只发文件，不发文字说明
    send = run_cli(['message', 'aibot', 'send', '--json',
                    json.dumps({'chat_id': chat_id, 'msg_type': 'file',
                                'file': {'media_id': media_id}}, ensure_ascii=False)],
                   fake={'success': True})
    if auth_expired(send):
        log('SKIP 发送阶段权限过期，本轮未推送')
        out({'ok': True, 'skipped': True, 'reason': 'wecom_auth_expired', 'rows': rows})
    ok = send.get('success') is True or send.get('ok') is True or (
        send.get('errcode') in (0, None) and send.get('errmsg') in (None, 'ok'))
    if not ok:
        log('FAIL 发送失败 | 回包:' + json.dumps(send, ensure_ascii=False)[:300])
        notify_once('send', '文件发送失败，本轮未推送')
        out({'ok': False, 'error': 'send_failed'})

    state['lastOkHour'] = hour
    state['lastFile'] = filename
    state['lastRows'] = rows
    state['lastAt'] = now()
    save_state(state)
    log('OK 今日%s条 -> %s | %s%s' % (rows, GROUP_NAME, filename, ' [DRYRUN]' if DRY else ''))
    out({'ok': True, 'rows': rows, 'file': filename, 'group': GROUP_NAME,
         'generatedAt': now(), 'dryRun': DRY})


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log('FAIL 未捕获异常: %s: %s' % (type(e).__name__, e))
        out({'ok': False, 'error': 'exception', 'detail': str(e)[:300]})
