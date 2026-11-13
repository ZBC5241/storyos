#!/usr/bin/env python3
# storyos 售票厅 —— 登录鉴权 + 数据下发（2026-10-23 建，18号）
# 背景：原看板把数据+密码全内嵌页面，未登录 curl 即得全部经营数据（P0 泄露）。
#       本服务把数据搬到本机，验票才吐；GitHub Pages 上只留空壳页面。
#
# 端点：
#   POST /api/login   {account, password} → {token, store, expiresIn}   （限流 5 次/分/IP）【仅 storyos】
#   POST /api/mms/login {account, password} → {token, account, expiresIn}   【仅 MMS，独立账号表/独立密钥/独立限流】
#   POST /api/mms/change-password {account, password, newPassword} → {ok} 【仅 MMS，只改 MMS 自己的账号表】
#   POST /api/change-password {account, currentPassword, newPassword} → {ok}（限流同 login；凭账号+当前密码自助改密）
#   GET  /api/data?token=...               → {store, data}              （按 token 的 store 授权）
#   GET  /api/marks?token=...              → {marks, updatedAt}         （会员回访标记，跨设备同步）
#   POST /api/marks {token, changes}       → {marks, updatedAt}         （按 _ts 做 last-write-wins）
#   GET  /api/health                       → {ok, age}                  （供保活探活）
#   GET  /workbench                        → 未登录给登录页；验票后给轻量加载器
#   GET  /api/wb/manifest                  → 分片清单 + token（验票后）
#   GET  /api/wb/chunk?i=N                → 页面第 N 片（验票后）
#   GET  /workbench/login.css              → 复用 storyos 登录页同一个 CSS
#   GET  /workbench/logout                 → 清会话
#   POST /workbench/auth {token}           → 验票后写会话 Cookie
# 2026-11-07 增 /api/marks：只同步「回访标记」，会员明细仍留在本地单文件里，不进任何公网静态资源。
# 2026-11-08 账号体系拆分：/api/wb/*、/api/marks、/workbench/* 全部只认 MMS 独立票据；
#             /api/login、/api/data、/api/change-password 全部只认 storyos 原票据。两侧零交叉。
# 2026-11-07 增 /workbench：与 storyos 同一安全范式——空壳可访问，会员明细验票后由本机下发，
#             页面数据文件只放 server/data/（0600），绝不进 GitHub Pages 或任何公开静态网址。
# 🔴 分片原因：serveo 免费隧道实测在单个 HTTP 响应约 256KB 处断流（本机 6.8MB 完整，
#    公网只到 265772 字节即 curl 92）。故整页切成 ~200KB UTF-8 的片，由加载器并发拉齐后
#    在浏览器本地拼装，任一片失败都不显示残缺页面（数据真实性红线）。
#
# 数据源：server/data/{ljc,hyc}.json（由 inject_data.py 在管线每次跑完后写入，mtime 变化自动重载）
# 授权模型：token = base64url(store|exp) + HMAC-SHA256 签名；单店 token 只能取本店数据，
#           zbc(all) token 一次取两店（前端切换器用，不再重复请求）。
#
# 纯标准库，无第三方依赖；macOS python3 直接跑。
import base64, gzip, hashlib, hmac, json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote
from http.cookies import SimpleCookie
from html import escape as html_escape, unescape as html_unescape

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, 'config.json')
DATA_DIR = os.path.join(BASE, 'data')
# MMS 会员工作台的结构化源（film_sprint 每日 08:00 生成，与 workbench.html 同源）
MMS_DATA_DIR = os.path.normpath(os.path.join(BASE, '..', '..', 'film_sprint', 'data'))
MMS_QUERY_CACHE = os.path.join(MMS_DATA_DIR, 'member_query_cache.json')
MMS_UNIFIED = os.path.join(MMS_DATA_DIR, 'unified_cache.json')
MMS_HEALTH = os.path.join(MMS_DATA_DIR, 'health.json')
MMS_PROMO = os.path.join(MMS_DATA_DIR, 'promo_snapshot.json')
# 会员工作台成品（每日 08:00 由 publish_workbench_online.py 原子写入，0600，从不静态托管）
WB_HTML = os.path.join(DATA_DIR, 'workbench.html')
LOG_PATH = os.path.join(BASE, 'logs', 'server.log')
# 【18号 2026-11-12】登录留痕：看板/库存查询都是纯本地登录，登录本身零联网；
# 这里只提供一条「顺手发个小纸条」的通道，独立于任何登录/数据接口，不参与鉴权。
AUDIT_PATH = os.path.join(BASE, 'logs', 'audit.log')
AUDIT_GIF = (b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff'
             b'\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00'
             b'\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b')   # 1x1 透明 GIF：图片加载成功即代表纸条送达

# ---------- 配置 ----------
def load_config():
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return json.load(f)

CFG = load_config()
SECRET = CFG['token_secret'].encode('utf-8')
TTL_SECONDS = int(CFG.get('ttl_hours', 48)) * 3600
PORT = int(CFG.get('port', 8798))

# ---------- MMS 会员工作台：独立账号 / 独立密钥 / 独立票据（2026-11-08 晨哥定）----------
# 与 storyos 完全隔离，两边「没有任何关系」：
#   ① 账号表独立：mms_accounts.json（5 人） vs config.json（10 人，含华阳城）
#   ② 签名密钥独立：MMS_SECRET vs SECRET，票据互不认可
#   ③ 登录端点独立：/api/mms/login vs /api/login（连限流桶都分开）
#   ④ 会话 Cookie 独立：mms_sess vs 无（storyos 页面只用 localStorage）
#   ⑤ 浏览器存储独立：mms_token_v1 / mms_sync_token_v1 vs storyos_token_v1
# 因此：任一侧删号、改密、签票、退出、过期，都不会影响另一侧。
# mms_accounts.json 为 0600 且已进 .gitignore，绝不进任何 git 仓库 / 公开静态资源。
MMS_CFG_PATH = os.path.join(BASE, 'mms_accounts.json')

def load_mms_config():
    with open(MMS_CFG_PATH, encoding='utf-8') as f:
        return json.load(f)

MCFG = load_mms_config()
MMS_SECRET = MCFG['token_secret'].encode('utf-8')
MMS_TTL_SECONDS = int(MCFG.get('ttl_hours', 48)) * 3600
_mms_cfg_lock = threading.Lock()

def save_mms_config():
    with _mms_cfg_lock:
        tmp = MMS_CFG_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(MCFG, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, MMS_CFG_PATH)

def issue_mms_token(account):
    """payload = mms|账号|过期时间，用 MMS 独立密钥签名；storyos verify_token 不认。"""
    exp = int(time.time()) + MMS_TTL_SECONDS
    payload = base64.urlsafe_b64encode(('mms|%s|%d' % (account, exp)).encode('utf-8')).decode().rstrip('=')
    sig = hmac.new(MMS_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return payload + '.' + sig

def verify_mms_token(token):
    """只认 MMS 独立密钥签发的票；账号一旦从表里删掉，未过期的旧票也立刻失效。"""
    try:
        payload, sig = token.rsplit('.', 1)
        expect = hmac.new(MMS_SECRET, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return None
        raw = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)).decode('utf-8')
        parts = raw.split('|')
        if len(parts) != 3 or parts[0] != 'mms':
            return None
        if int(parts[2]) < time.time():
            return None
        if parts[1] not in MCFG['accounts']:
            return None
        return parts[1]
    except Exception:
        return None

# ---------- 账号独立密码（2026-10-23 自助改密）----------
# CFG 常驻内存：改密直接改内存 CFG（登录立即生效）+ 原子落盘（重启不丢），无需重启服务。
_cfg_lock = threading.Lock()

def resolve_account(account):
    """账号名归一：先直查，B 后缀容错（漏输/多输 B），返回规范账号名或 None"""
    if account in CFG['accounts']:
        return account
    if account.endswith('B'):
        alt = account[:-1]
        if alt in CFG['accounts']:
            return alt
    return None

def effective_password(account):
    """每人独立密码优先，缺失回退全局 password"""
    ap = CFG.get('accountPasswords') or {}
    return str(ap.get(account) or CFG.get('password', ''))

def save_config():
    with _cfg_lock:
        tmp = CONFIG_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(CFG, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_PATH)

# ---------- 日志 ----------
_log_lock = threading.Lock()
def log(msg):
    line = time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + msg + '\n'
    with _log_lock:
        try:
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(line)
        except Exception:
            pass

_audit_lock = threading.Lock()
def audit_log(msg):
    """来访留痕单独成文件，不混进 server.log（避免污染会员工作台日志）。"""
    line = time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + msg + '\n'
    with _audit_lock:
        try:
            with open(AUDIT_PATH, 'a', encoding='utf-8') as f:
                f.write(line)
        except Exception:
            pass

def audit_clean(v, n):
    """防日志注入：去掉换行/制表，超长截断，不接受任何控制字符。"""
    s = str(v or '').replace('\r', ' ').replace('\n', ' ').replace('\t', ' ')
    return ''.join(ch if 32 <= ord(ch) < 127 or ord(ch) > 127 else ' ' for ch in s)[:n]

def audit_info(v, n):
    """业务动作详情必须重新 URL 编码：查询串解码后空格会回来，直接写会撑破日志的单字段格式。
    先按字符数截断再编码，避免截断落在 %XX 中间导致清单页解码出乱码。"""
    return quote(audit_clean(v, n), safe='')

# ---------- 数据缓存（mtime 变化自动重载，管线更新无需重启服务） ----------
class DataStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache = {}   # store -> (mtime, obj)
    def get(self, store):
        path = os.path.join(DATA_DIR, store + '.json')
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        with self._lock:
            hit = self._cache.get(store)
            if hit and hit[0] == mtime:
                return hit[1]
            with open(path, 'rb') as f:
                obj = json.loads(f.read().decode('utf-8'))
            self._cache[store] = (mtime, obj)
            return obj
    def age_seconds(self):
        newest = 0
        for store in ('ljc', 'hyc'):
            try:
                newest = max(newest, os.path.getmtime(os.path.join(DATA_DIR, store + '.json')))
            except OSError:
                pass
        if not newest:
            return None
        return round(time.time() - newest)

DATA = DataStore()

# ---------- 会员工作台在线版：空壳公开 + 验票下发（2026-11-07） ----------
# 判断标准不是「有没有登录框」，而是「数据在不在静态文件里」。
# server/data/workbench.html 由 film_sprint/publish_workbench_online.py 在每日 08:00 链路末端写入，
# 从不作为静态资源托管；GET /workbench 只有验票通过才把它吐出来。
WB_HTML_PATH = os.path.join(DATA_DIR, 'workbench.html')
WB_META_PATH = os.path.join(DATA_DIR, 'workbench_meta.json')
WB_LOGIN_PATH = os.path.join(BASE, 'workbench_login.html')
WB_LOADER_PATH = os.path.join(BASE, 'workbench_loader.html')
WB_CSS_PATH = os.path.join(os.path.dirname(BASE), 'login.css')
WB_COOKIE = 'mms_sess'   # 2026-11-08 独立会话名，与 storyos 页面不共用任何 Cookie
_wb_lock = threading.Lock()
_wb_render = {'parts_key': None, 'parts': None, 'chars': 0}

def wb_meta():
    try:
        with open(WB_META_PATH, 'rb') as f:
            return json.loads(f.read().decode('utf-8'))
    except (OSError, ValueError):
        return {'status': 'missing', 'publishedAt': None, 'note': '在线版尚未发布'}

def wb_pub_age(meta):
    """返回页面真实发布时刻距今秒数；无法解析返回 None（不猜、随后显式标红）"""
    try:
        t = time.mktime(time.strptime(str(meta.get('publishedAt')), '%Y-%m-%d %H:%M:%S'))
        return time.time() - t
    except Exception:
        return None

def wb_warn(meta):
    st = str(meta.get('status') or 'unknown')
    pub = str(meta.get('publishedAt') or '未记录')
    age = wb_pub_age(meta)
    if st != 'ok':
        txt = '本轮在线数据未全部更新成功（%s）；当前显示的是 %s 发布的页面' % (st, pub)
    elif age is None or age > 30 * 3600:
        txt = '在线版页面已超过 30 小时未更新或发布时间未记录；当前显示的是 %s 发布的页面' % pub
    else:
        return b''
    note = str(meta.get('note') or '')
    txt += '，各块真实采集时点见页面内标注' + ((' · ' + note) if note else '')
    return ('<div style="position:relative;z-index:5;margin:10px 14px 0;padding:10px 12px;'
            'background:#FFF1F2;border:1px solid #FECDD3;color:#B3122C;border-radius:10px;'
            'font-size:12px;line-height:1.55;font-weight:700">⚠ %s</div>'
            % html_escape(txt)).encode('utf-8')

# 单片段 gzip 后字节上限。实测 Serveo 走 HTTP/2 时单响应约 80KB 以上会触发
# ERR_HTTP2_PROTOCOL_ERROR（双店载荷 gzip 80KB 已验证安全），故压到 70KB 留余量，
# 6.8MB 整页约切 8 片，浏览器并发拉齐明显快于原来的 35 片。
WB_GZ_LIMIT = 70000

def split_html(s, gz_limit=WB_GZ_LIMIT):
    """按「单片段 gzip 后」字节上限切分，保证字符边界完整。
    每片独立 gzip 后控制在 WB_GZ_LIMIT 内，片数远少于按原始 200KB 切的 35 片，
    浏览器并发拉齐后本地拼装，登录后加载明显更快。"""
    step = 8192
    parts, cur = [], []
    for i in range(0, len(s), step):
        seg = s[i:i + step]
        cur.append(seg)
        gz = len(gzip.compress(''.join(cur).encode('utf-8'), 6))
        if gz > gz_limit and len(cur) > 1:
            parts.append(''.join(cur[:-1]))
            cur = [cur[-1]]
    if cur:
        parts.append(''.join(cur))
    return parts

def wb_chunks(meta):
    """按 (文件 mtime, 告警状态) 缓存分片，发布后自动失效重建"""
    try:
        mt = os.path.getmtime(WB_HTML_PATH)
    except OSError:
        return None, 0
    warn = wb_warn(meta)
    key = (mt, warn, WB_GZ_LIMIT)
    with _wb_lock:
        if _wb_render['parts_key'] == key and _wb_render['parts']:
            return _wb_render['parts'], _wb_render['chars']
        with open(WB_HTML_PATH, 'rb') as f:
            s = f.read().decode('utf-8')
        if warn:
            s = s.replace('<body>', '<body>' + warn.decode('utf-8'), 1)
        parts = split_html(s)
        _wb_render.update(parts_key=key, parts=parts, chars=len(s))
        log('workbench 分片重建 %d 片 / %d 字符' % (len(parts), len(s)))
        return parts, len(s)

# ---------- MMS 会员工作台：结构化「摘要 + 按需明细」接口 ----------
# 照搬 storyos /api/data：登录只发精简摘要（小 payload），点某会员才发其明细。
# 首屏不再硬塞 6.8MB 单体 HTML，自然无需切片、登录快。
_mms_lock = threading.Lock()
_mms_cache = {'key': None, 'obj': None}

def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return -1

def _read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None

# ---------- MMS 在线版：界面放 Pages CDN，隧道只传纯数据（照搬 storyos） ----------
# 18:20 架构对齐 storyos：成品 = 纯 UI 骨架(~250KB) + W(~548KB) + F(2KB) + Q(~5.29MB)。
# 纯 UI 骨架由 build_mms_shell.py 剥离三段数据后静态托管到 GitHub Pages(mms/mms_shell.html)，
# 走 CDN 秒下；隧道只下发 W/F（/api/wb/wf）与精简 Q（/api/wb/q）两个小包，
# 浏览器注入占位符后渲染。历史客户 2579 人的 purchases/cat/series/orders 首屏不用，
# 点开才走 /api/wb/hist。旧 /api/wb/shell、manifest/chunk 保留供回滚，新链路不调用。
_wb_shell_lock = threading.Lock()
_wb_shell_cache = {'mtime': 0.0, 'obj': None}

# 内层 iframe 垫片：点开历史客户才拉全量消费明细；失败显式标红，不拿空/旧数据冒充
_WB_HIST_SHIM = """<script>
(function(){
  var P=window.parent, API=(P&&P.__WB_API__)||'', TOK=(P&&P.__WB_TOKEN__)||'';
  var _open=window.openMember;
  if(typeof _open!=='function') return;
  window.openMember=function(m,back){
    if(m&&m._hist&&!m.__full){
      var box=document.getElementById('main');
      if(box) box.innerHTML='<div class="empty">正在加载该客户全量消费明细…</div>';
      return fetch(API+'/api/wb/hist?ph='+encodeURIComponent(m.phone)+'&token='+encodeURIComponent(TOK))
        .then(function(r){if(!r.ok)throw new Error(r.status);return r.json();})
        .then(function(j){if(!j.ok)throw new Error(j.error||'fail');m.consumption=j.rec;m.__full=true;return _open(m,back);})
        .catch(function(){if(box)box.innerHTML='<div class="empty" style="color:#b3122c">该客户明细本轮加载失败，未显示旧数据，请重试。</div>';});
    }
    return _open(m,back);
  };
})();
</script>"""

def wb_shell_payload():
    """读成品 → (UI骨架含W/F但Q为占位, 精简Q转义文本, 全量Q供明细按需, 真实时点)。
    mtime 变化自动重载（与 storyos DataStore 同范式）；失败保留上一版供前端显式告警。"""
    mt = _mtime(WB_HTML)
    if mt < 0:
        return None
    with _wb_shell_lock:
        if _wb_shell_cache['mtime'] == mt and _wb_shell_cache['obj'] is not None:
            return _wb_shell_cache['obj']
        try:
            with open(WB_HTML, encoding='utf-8') as f:
                s = f.read()
            kq, kg = 'const Q = ', '\nconst GN = Q.gname;'
            iq, jq = s.find(kq), s.find(kg, s.find(kq))
            if iq < 0 or jq < 0:
                raise ValueError('Q 注入锚点缺失')
            Q = json.loads(html_unescape(s[iq + len(kq):jq].strip().rstrip(';')))
            small = dict(Q)
            small['history'] = {
                ph: {k: v for k, v in (rec or {}).items()
                     if k not in ('purchases', 'cat', 'series', 'orders')}
                for ph, rec in (Q.get('history') or {}).items()
            }
            qraw = json.dumps(small, ensure_ascii=False, separators=(',', ':'))
            qtext = html_escape(qraw)
            # W/F 原文（裸 JSON，注入外层 <script> 的占位符，逐字保留不重新序列化）
            kw, kfw = 'const W = ', ';\nconst F = '
            aw, bw = s.find(kw), s.find(kfw, s.find(kw))
            kf, kgw = 'const F = ', ';\nconst GN = W.gname;'
            af, bf = s.find(kf), s.find(kgw, s.find(kf))
            if aw < 0 or bw < 0 or af < 0 or bf < 0:
                raise ValueError('W/F 注入锚点缺失')
            wtext, ftext = s[aw + len(kw):bw], s[af + len(kf):bf]
            shell = s[:iq + len(kq)] + '__INJECT_Q__;' + s[jq:]
            pos = shell.rfind('&lt;/body&gt;')
            if pos > 0:
                shell = shell[:pos] + html_escape(_WB_HIST_SHIM) + shell[pos:]
            obj = {'shell': shell, 'q': qtext, 'qraw': qraw, 'w': wtext, 'f': ftext,
                   'shim': html_escape(_WB_HIST_SHIM), 'fullQ': Q,
                   'generatedAt': Q.get('generatedAt'), 'asOf': Q.get('asOf'), 'mtime': mt}
            _wb_shell_cache.update(mtime=mt, obj=obj)
            log('workbench 数据重建 w=%dKB f=%dKB q=%dKB' % (
                len(wtext) // 1024, len(ftext) // 1024, len(qtext) // 1024))
            return obj
        except Exception as e:
            # 数据真实性红线：构建失败绝不拿上一版缓存冒充本轮数据，直接 503 让前端标红；
            # 同时清空缓存，避免下一次请求命中旧对象又静默变成「旧数据当新数据」
            log('workbench 骨架构建失败: %s' % e)
            _wb_shell_cache.update(mtime=0.0, obj=None)
            return None

def mms_query():
    """加载四 Tab 所需的多源结构化缓存（mtime 变化自动重载）。
    返回 (query, unified, health, promo, hmap)，主源缺失时保留上一版不编造。"""
    key = (_mtime(MMS_QUERY_CACHE), _mtime(MMS_UNIFIED),
           _mtime(MMS_HEALTH), _mtime(MMS_PROMO))
    with _mms_lock:
        if _mms_cache['key'] == key and _mms_cache['obj'] is not None:
            return _mms_cache['obj']
        q = _read_json(MMS_QUERY_CACHE)
        if q is None:
            return _mms_cache['obj']
        u = _read_json(MMS_UNIFIED) or {}
        h = _read_json(MMS_HEALTH) or {}
        pr = _read_json(MMS_PROMO) or {}
        # 历史客户稳定短 id（不暴露完整手机号，供按需明细反查）
        hmap = {}
        for ph in (q.get('history') or {}):
            hmap[hid_of(ph)] = ph
        obj = (q, u, h, pr, hmap)
        _mms_cache.update(key=key, obj=obj)
        return obj

def hid_of(ph):
    return 'h' + hashlib.md5(str(ph).encode('utf-8')).hexdigest()[:10]

def _mask_name(n):
    if not n:
        return ''
    s = str(n)
    return s[0] + '*' * (len(s) - 1) if len(s) > 1 else s

def _mask_phone(p):
    if not p:
        return ''
    s = str(p)
    return '****' + s[-4:] if len(s) >= 4 else s

def wb_login_html():
    try:
        with open(WB_LOGIN_PATH, 'rb') as f:
            return f.read()
    except OSError:
        return '<meta charset="utf-8"><h1>登录页缺失</h1>'.encode('utf-8')

def wb_loader_html():
    try:
        with open(WB_LOADER_PATH, 'rb') as f:
            return f.read()
    except OSError:
        return '<meta charset="utf-8"><h1>加载器缺失</h1>'.encode('utf-8')

# ---------- 会员回访标记：跨设备同步（2026-11-07） ----------
# 每条带 _ts(毫秒) 做 last-write-wins；删除写成墓碑 {deleted:true,_ts}，避免被旧记录复活。
# 写盘失败必须抛错返回 500，绝不静默当成功（数据真实性红线）。
MARKS_PATH = os.path.join(DATA_DIR, 'marks.json')
_marks_lock = threading.Lock()
MARK_FIELDS = ('result', 'note', 'nextDate', 'time')

def read_marks():
    with _marks_lock:
        try:
            with open(MARKS_PATH, 'rb') as f:
                doc = json.loads(f.read().decode('utf-8'))
            if isinstance(doc, dict) and isinstance(doc.get('marks'), dict):
                return doc
        except (OSError, ValueError):
            pass
        return {'marks': {}, 'updatedAt': None, 'revision': 0}

def write_marks(doc):
    tmp = MARKS_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, separators=(',', ':'))
    os.chmod(tmp, 0o600)
    os.replace(tmp, MARKS_PATH)

def merge_marks(changes):
    """逐条按 _ts 比较，返回 (最新文档, 实际写入条数)。异常直接抛出。"""
    with _marks_lock:
        try:
            with open(MARKS_PATH, 'rb') as f:
                doc = json.loads(f.read().decode('utf-8'))
            if not isinstance(doc, dict) or not isinstance(doc.get('marks'), dict):
                doc = {'marks': {}, 'revision': 0}
        except (OSError, ValueError):
            doc = {'marks': {}, 'revision': 0}
        marks = doc['marks']
        applied = 0
        for mid, item in changes.items():
            if not isinstance(mid, str) or not mid or len(mid) > 64 or not isinstance(item, dict):
                continue
            ts = item.get('_ts')
            if not isinstance(ts, (int, float)) or ts <= 0:
                continue
            old = marks.get(mid)
            if isinstance(old, dict) and isinstance(old.get('_ts'), (int, float)) and old['_ts'] > ts:
                continue
            clean = {'_ts': ts, 'deleted': bool(item.get('deleted'))}
            for k in MARK_FIELDS:
                v = item.get(k)
                clean[k] = str(v)[:500] if v is not None else ''
            for k in ('visited', 'done', 'block'):
                clean[k] = bool(item.get(k))
            marks[mid] = clean
            applied += 1
        if applied:
            doc = {'marks': marks, 'updatedAt': time.strftime('%Y-%m-%d %H:%M:%S'),
                   'revision': int(doc.get('revision', 0)) + 1}
            write_marks(doc)
        return doc, applied

# ---------- 登录限流（内存滑动窗口：60 秒内同 IP 最多 5 次） ----------
_rl_lock = threading.Lock()
_rl = {}   # ip -> [ts, ...]
def rate_limited(ip):
    now = time.time()
    with _rl_lock:
        hits = [t for t in _rl.get(ip, []) if now - t < 60]
        if len(hits) >= 5:
            _rl[ip] = hits
            return True
        hits.append(now)
        _rl[ip] = hits
        return False

def rate_limited_key(ip, key, limit):
    """独立桶：标记同步频率高于登录，不能共用 5次/分 的登录限流"""
    now = time.time()
    k = (key, ip)
    with _rl_lock:
        hits = [t for t in _rl.get(k, []) if now - t < 60]
        if len(hits) >= limit:
            _rl[k] = hits
            return True
        hits.append(now)
        _rl[k] = hits
        return False

# ---------- token：base64url(store|exp) . HMAC-SHA256 ----------
def issue_token(store):
    exp = int(time.time()) + TTL_SECONDS
    payload = base64.urlsafe_b64encode(f'{store}|{exp}'.encode()).decode().rstrip('=')
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return payload + '.' + sig

def verify_token(token):
    try:
        payload, sig = token.rsplit('.', 1)
        expect = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return None
        store, exp = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)).decode().split('|')
        if int(exp) < time.time():
            return None
        if store not in ('ljc', 'hyc', 'all'):
            return None
        return store
    except Exception:
        return None

# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):     # 静音默认访问日志，走自己的 log()
        pass

    def _cors(self, open_any=False):
        origin = self.headers.get('Origin', '')
        # 白名单：Pages 官方源 + 本机调试（localhost/127.0.0.1 任意端口）+ file:// 测试页(Origin=null)
        # /api/marks 由 token 鉴权（不靠 CORS 挡人），单文件页面可能由任意内置浏览器打开，回显其 Origin
        ok = (origin == 'https://zbc5241.github.io' or origin == 'null'
              or origin.startswith('http://localhost') or origin.startswith('http://127.0.0.1'))
        allow = origin if (ok or (open_any and origin)) else 'https://zbc5241.github.io'
        self.send_header('Access-Control-Allow-Origin', allow)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Cache-Control', 'no-store')

    def _json(self, code, obj, open_cors=False):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        # gzip：双店全量载荷 ~525KB，压缩后 ~80KB——手机流量友好，也避免大响应体在隧道里被截断
        gz = 'gzip' in (self.headers.get('Accept-Encoding') or '') and len(body) > 1024
        if gz:
            body = gzip.compress(body, 6)
        self.send_response(code)
        self._cors(open_cors)
        if gz:
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Vary', 'Origin, Accept-Encoding')
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send(self, code, body, ctype='text/html; charset=utf-8', headers=None, gz_bytes=None, cors=False):
        accepts = 'gzip' in (self.headers.get('Accept-Encoding') or '')
        if gz_bytes is not None and accepts:
            body, enc = gz_bytes, True
        elif accepts and len(body) > 1024:
            body, enc = gzip.compress(body, 6), True
        else:
            enc = False
        self.send_response(code)
        if cors:
            origin = self.headers.get('Origin', '')
            allow = origin if origin in ('https://zbc5241.github.io', 'null') \
                or origin.startswith('http://localhost') or origin.startswith('http://127.0.0.1') \
                else 'https://zbc5241.github.io'
            self.send_header('Access-Control-Allow-Origin', allow)
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Vary', 'Accept-Encoding')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if enc:
            self.send_header('Content-Encoding', 'gzip')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _wb_cookie(self):
        try:
            c = SimpleCookie()
            c.load(self.headers.get('Cookie', ''))
            return c[WB_COOKIE].value if WB_COOKIE in c else ''
        except Exception:
            return ''

    def _wb_session_cookie(self, token):
        return ('%s=%s; Path=/; Max-Age=%d; HttpOnly; Secure; SameSite=Lax'
                % (WB_COOKIE, token, MMS_TTL_SECONDS))

    def _do_workbench(self):
        # 隧道 /workbench 现为「空壳状态页」：仅一行字，与 storyos 隧道根同构。
        # 真身工作台在 GitHub Pages(https://zbc5241.github.io/mms/) 登录后验票从本机 8802 下发。
        body = b'workbench ticket office ok'
        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _do_wb_manifest(self, token):
        meta = wb_meta()
        parts, chars = wb_chunks(meta)
        if not parts:
            self._send(503, b'{"error":"workbench data not ready"}',
                       ctype='application/json; charset=utf-8', cors=True)
            return
        obj = {'ok': True, 'chunks': len(parts), 'chars': chars, 'token': token,
               'status': meta.get('status'), 'publishedAt': meta.get('publishedAt'),
               'note': meta.get('note', '')}
        self._send(200, json.dumps(obj, ensure_ascii=False).encode('utf-8'),
                   ctype='application/json; charset=utf-8', cors=True)

    def _do_wb_chunk(self, idx):
        parts, _ = wb_chunks(wb_meta())
        if not parts or idx < 0 or idx >= len(parts):
            self._send(404, b'chunk not found', ctype='text/plain; charset=utf-8', cors=True)
            return
        self._send(200, parts[idx].encode('utf-8'), ctype='text/plain; charset=utf-8', cors=True)

    def _do_wb_auth(self, body):
        tok = str(body.get('token') or '') if isinstance(body, dict) else ''
        if not verify_mms_token(tok):
            self._send(401, b'{"error":"token invalid"}', ctype='application/json; charset=utf-8')
            return
        self._send(200, b'{"ok":true}', ctype='application/json; charset=utf-8',
                   headers={'Set-Cookie': self._wb_session_cookie(tok)})
        log('/workbench/auth 200 会话建立 ip=%s' % self.client_address[0])

    def _do_wb_wf(self, token):
        """界面已在 Pages CDN，这里只下发 W/F 纯数据 + 真实时点 + 内层垫片。"""
        d = wb_shell_payload()
        if not d:
            self._send(503, b'{"error":"workbench not ready"}',
                       ctype='application/json; charset=utf-8', cors=True)
            return
        obj = {'ok': True, 'w': d['w'], 'f': d['f'], 'shim': d['shim'],
               'generatedAt': d['generatedAt'], 'asOf': d['asOf']}
        self._send(200, json.dumps(obj, ensure_ascii=False).encode('utf-8'),
                   ctype='application/json; charset=utf-8', cors=True)

    def _do_wb_shell(self, token):
        d = wb_shell_payload()
        if not d:
            self._send(503, b'{"error":"workbench not ready"}',
                       ctype='application/json; charset=utf-8', cors=True)
            return
        self._send(200, d['shell'].encode('utf-8'),
                   ctype='text/html; charset=utf-8', cors=True)

    def _do_wb_q(self, token):
        d = wb_shell_payload()
        if not d:
            self._send(503, b'{"error":"workbench not ready"}',
                       ctype='application/json; charset=utf-8', cors=True)
            return
        self._send(200, d['q'].encode('utf-8'),
                   ctype='text/plain; charset=utf-8', cors=True)

    def _do_wb_qraw(self, token):
        """裸 JSON（未做 HTML 转义）：比转义态少 30KB gzip，前端注入 srcdoc 时自行转义。"""
        d = wb_shell_payload()
        if not d:
            self._send(503, b'{"error":"workbench not ready"}',
                       ctype='application/json; charset=utf-8', cors=True)
            return
        self._send(200, d['qraw'].encode('utf-8'),
                   ctype='application/json; charset=utf-8', cors=True)

    def _do_wb_qpart(self, token, q):
        """【18号 2026-11-08】免费隧道传 195KB 单响应常触发浏览器 HTTP/2 断流
           (net::ERR_HTTP2_PROTOCOL_ERROR)。按「字符」切成 n 片并发下发，每片独立
           UTF-8（不切断多字节汉字），前端 text() 拼接即为完整裸 JSON。"""
        d = wb_shell_payload()
        if not d:
            self._send(503, b'{"error":"workbench not ready"}',
                       ctype='application/json; charset=utf-8', cors=True)
            return
        try:
            n = max(1, min(12, int((q.get('n') or ['6'])[0])))
            i = int((q.get('i') or ['0'])[0])
        except (ValueError, TypeError):
            self._send(400, b'{"error":"bad part"}',
                       ctype='application/json; charset=utf-8', cors=True)
            return
        s = d['qraw']
        size = (len(s) + n - 1) // n
        part = s[i * size:(i + 1) * size] if 0 <= i < n else ''
        self._send(200, part.encode('utf-8'),
                   ctype='text/plain; charset=utf-8', cors=True)

    def _do_wb_hist(self, token, ph):
        d = wb_shell_payload()
        if not d:
            self._send(503, b'{"error":"workbench not ready"}',
                       ctype='application/json; charset=utf-8', cors=True)
            return
        rec = (d['fullQ'].get('history') or {}).get(ph)
        if rec is None:
            self._send(404, b'{"error":"history member not found"}',
                       ctype='application/json; charset=utf-8', cors=True)
            return
        body = {'ok': True, 'rec': rec, 'generatedAt': d['generatedAt']}
        self._send(200, json.dumps(body, ensure_ascii=False).encode('utf-8'),
                   ctype='application/json; charset=utf-8', cors=True)

    def _do_wb_summary(self, token):
        got = mms_query()
        if not got:
            self._send(503, b'{"error":"mms member cache not ready"}',
                       ctype='application/json; charset=utf-8', cors=True)
            return
        q, u, health, promo, hmap = got
        # 在册会员：只发列表字段（券数组/消费/画像留给按需明细）
        members = []
        for m in (q.get('members') or []):
            c = m.get('consumption') or {}
            members.append({
                'id': str(m.get('mid') or ''), 'name': _mask_name(m.get('name')),
                'phone': _mask_phone(m.get('phone')), 'emp': m.get('emp') or '',
                'level': m.get('level') or '', 'reg': m.get('reg') or '',
                'nu': m.get('n_unused', 0), 'ns': m.get('n_used', 0), 'ne': m.get('n_expired', 0),
                'last': c.get('last') or '',
            })
        # 历史客户索引：稳定短 id + 脱敏 + 消费笔数 + 末次消费日
        hist = q.get('history') or {}
        history = []
        for ph, v in hist.items():
            pur = (v.get('purchases') or []) if isinstance(v, dict) else []
            history.append({'id': hid_of(ph),
                            'name': _mask_name(v.get('name') if isinstance(v, dict) else ''),
                            'phone': _mask_phone(ph), 'n': len(pur),
                            'last': pur[0].get('date') if pur else ''})
        # 活动行情精简（60 条 → 只留前 12 条名称/截止）
        promos = promo.get('promos') or []
        promo_out = {'asOf': promo.get('asOf'), 'count': len(promos),
                     'items': [{'name': x.get('name'), 'end': x.get('end')}
                               for x in promos[:12] if isinstance(x, dict)]}
        mdoc = read_marks()
        obj = {
            'ok': True, 'store': q.get('store') or '',
            'generatedAt': q.get('generatedAt'),      # 会员/券真实生成时刻
            'unifiedAt': u.get('generatedAt'),         # 总览聚合真实时刻
            'asOf': q.get('asOf') or {},               # 分块真实采集时点
            'health': health,                          # 各块成败 status（前端据此标红）
            'gname': u.get('gname') or q.get('gname') or {},
            'total': u.get('total') or {},
            'emps': u.get('emps') or [],
            'promo': promo_out,
            'members': members,
            'historyCount': q.get('history_count') or len(history),
            'history': history,
            'marks': mdoc.get('marks') or {},          # 回访标记（跨设备同步）
            'marksUpdatedAt': mdoc.get('updatedAt'),
        }
        self._send(200, json.dumps(obj, ensure_ascii=False).encode('utf-8'),
                   ctype='application/json; charset=utf-8', cors=True)

    def _do_wb_member(self, token, mid):
        got = mms_query()
        if not got:
            self._send(503, b'{"error":"mms member cache not ready"}',
                       ctype='application/json; charset=utf-8', cors=True)
            return
        q, u, health, promo, hmap = got
        key = str(mid)
        # 历史客户：短 id → phone → 完整消费记录
        if key in hmap:
            rec = (q.get('history') or {}).get(hmap[key])
            if rec is None:
                self._send(404, b'{"error":"member not found"}',
                           ctype='application/json; charset=utf-8', cors=True)
                return
            body = {'ok': True, 'kind': 'history', 'member': rec, 'generatedAt': q.get('generatedAt')}
        else:
            detail = None
            for m in (q.get('members') or []):
                if str(m.get('mid')) == key:
                    detail = m
                    break
            if detail is None:
                self._send(404, b'{"error":"member not found"}',
                           ctype='application/json; charset=utf-8', cors=True)
                return
            body = {'ok': True, 'kind': 'member', 'member': detail, 'generatedAt': q.get('generatedAt')}
        # 明细接口才回填明文（遵守数据红线：仅验票后下发）
        self._send(200, json.dumps(body, ensure_ascii=False).encode('utf-8'),
                   ctype='application/json; charset=utf-8', cors=True)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors(True)   # 预检不返回业务数据，回显任意 Origin 无风险
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/health':
            age = DATA.age_seconds()
            self._json(200 if age is not None else 503, {'ok': age is not None, 'dataAgeSec': age})
            return
        if path == '/api/audit':
            # 来访小纸条：GET 图片形式，天生不受跨域限制；不鉴权、不读任何业务数据。
            q = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            def gv(k, n):
                return audit_clean((q.get(k) or [''])[0], n)
            _sys, _act = gv('sys', 16), gv('act', 16)
            # 【18号 2026-11-13】业务动作：搜索 / 串码追踪；info 只放关键词或串码，不记密码、不记库存数值
            AUDIT_ACTS = ('ok', 'fail', 'logout', 'open', 'search', 'trace', 'detail', 'export', 'copy')
            if _sys in ('board', 'inventory') and _act in AUDIT_ACTS:
                # ts=浏览器端事件真实发生时刻；断网补报时行首时间是「收到时刻」，
                # 两者必须分开存，绝不拿收到时刻冒充登录时刻。
                try:
                    _at = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(gv('ts', 20)) / 1000.0))
                except Exception:
                    _at = '-'
                # 【18号 2026-11-13 17:30】来源上网地址：serveo 会把真实公网 IP 覆盖写进
                # X-Forwarded-For / X-Real-IP（17:28 实测：客户端伪造这两个头会被隧道覆盖），
                # 本机直连则是 127.0.0.1。这里只记地址本身，地名由清单页查库后标注为「推测」。
                _xff = (self.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
                _ip = audit_clean(_xff or (self.headers.get('X-Real-IP') or '').strip()
                                  or self.client_address[0], 45)
                _lat, _lng, _acc, _geo = gv('lat', 20), gv('lng', 20), gv('acc', 12), gv('geo', 12)
                audit_log('at=%s sys=%s act=%s acct=%s store=%s info=%s dev=%s ip=%s lat=%s lng=%s acc=%s geo=%s ua=%s' % (
                    _at, _sys, _act, gv('acct', 32), gv('store', 12),
                    audit_info(gv('info', 160), 160), gv('dev', 32), _ip,
                    _lat, _lng, _acc, _geo, gv('ua', 120)))
                # 每收到一条就顺手刷新「谁来过」清单页，晨哥双击本地文件永远是最新；
                # 报告失败绝不能影响留痕本身，故独立吞异常。
                try:
                    import audit_report
                    audit_report.build()
                except Exception:
                    pass
            self.send_response(200)
            self._cors(True)
            self.send_header('Content-Type', 'image/gif')
            self.send_header('Content-Length', str(len(AUDIT_GIF)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                self.wfile.write(AUDIT_GIF)
            except Exception:
                pass
            return
        if path == '/':
            body = b'storyos ticket office ok'
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == '/mms':
            # mms 落地页：与 storyos 根路径同构，只回一行状态字，不在隧道内嵌任何数据
            body = b'mms ticket office ok'
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ('/workbench', '/workbench/'):
            self._do_workbench()
            return
        if path in ('/api/wb/manifest', '/api/wb/chunk'):
            q = parse_qs(urlparse(self.path).query)
            token = (q.get('token') or [None])[0] or self._wb_cookie()
            if not token or not verify_mms_token(token):
                self._send(401, b'{"error":"unauthorized"}',
                           ctype='application/json; charset=utf-8', cors=True)
                return
            if path.endswith('manifest'):
                self._do_wb_manifest(token)
            else:
                try:
                    idx = int((q.get('i') or ['-1'])[0])
                except (ValueError, TypeError):
                    idx = -1
                self._do_wb_chunk(idx)
            return
        if path in ('/api/wb/shell', '/api/wb/q', '/api/wb/qraw', '/api/wb/qpart', '/api/wb/hist', '/api/wb/wf'):
            q = parse_qs(urlparse(self.path).query)
            token = (q.get('token') or [None])[0] or self._wb_cookie()
            if not token or not verify_mms_token(token):
                self._send(401, b'{"error":"unauthorized"}',
                           ctype='application/json; charset=utf-8', cors=True)
                return
            if path.endswith('/shell'):
                self._do_wb_shell(token)
            elif path.endswith('/wf'):
                self._do_wb_wf(token)
            elif path.endswith('/q'):
                self._do_wb_q(token)
            elif path.endswith('/qraw'):
                self._do_wb_qraw(token)
            elif path.endswith('/qpart'):
                self._do_wb_qpart(token, q)
            else:
                self._do_wb_hist(token, (q.get('ph') or [''])[0])
            return
        if path in ('/api/wb/summary', '/api/wb/member'):
            q = parse_qs(urlparse(self.path).query)
            token = (q.get('token') or [None])[0] or self._wb_cookie()
            if not token or not verify_mms_token(token):
                self._send(401, b'{"error":"unauthorized"}',
                           ctype='application/json; charset=utf-8', cors=True)
                return
            if path.endswith('summary'):
                self._do_wb_summary(token)
            else:
                self._do_wb_member(token, (q.get('id') or [''])[0])
            return
        if path == '/workbench/login.css':
            try:
                with open(WB_CSS_PATH, 'rb') as f:
                    css = f.read()
                self._send(200, css, ctype='text/css; charset=utf-8')
            except OSError:
                self._send(404, b'/* login.css missing */', ctype='text/css; charset=utf-8')
            return
        if path == '/workbench/logout':
            self._send(302, b'', headers={
                'Location': '/workbench',
                'Set-Cookie': '%s=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax' % WB_COOKIE})
            return
        if path == '/favicon.ico':
            self._send(204, b'')
            return
        if path == '/api/data':
            token = (parse_qs(urlparse(self.path).query).get('token') or [''])[0]
            store = verify_token(token)
            if not store:
                self._json(401, {'error': 'token invalid or expired'})
                log('/api/data 401 token无效 ip=%s' % self.client_address[0])
                return
            if store == 'all':
                ljc, hyc = DATA.get('ljc'), DATA.get('hyc')
                if ljc is None or hyc is None:
                    self._json(503, {'error': 'data not ready'})
                    return
                self._json(200, {'store': 'all', 'data': {'ljc': ljc, 'hyc': hyc}})
            else:
                obj = DATA.get(store)
                if obj is None:
                    self._json(503, {'error': 'data not ready'})
                    return
                self._json(200, {'store': store, 'data': obj})
            return
        if path == '/api/marks':
            token = (parse_qs(urlparse(self.path).query).get('token') or [''])[0]
            if not verify_mms_token(token):
                self._json(401, {'error': '登录已失效，请重新登录'}, open_cors=True)
                return
            doc = read_marks()
            self._json(200, {'ok': True, 'marks': doc['marks'],
                             'updatedAt': doc.get('updatedAt'),
                             'revision': doc.get('revision', 0)}, open_cors=True)
            return
        self._json(404, {'error': 'not found'})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ('/api/login', '/api/change-password', '/api/marks', '/workbench/auth',
                        '/api/mms/login', '/api/mms/change-password'):
            self._json(404, {'error': 'not found'})
            return
        ip = self.client_address[0]
        try:
            n = int(self.headers.get('Content-Length', '0'))
            if n <= 0 or n > 2000000:
                raise ValueError('bad length')
            body = json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception:
            self._json(400, {'error': 'bad request'})
            return
        if path == '/workbench/auth':
            self._do_wb_auth(body)
            return
        if path == '/api/marks':
            self._do_marks(body, ip)
            return
        if path in ('/api/mms/login', '/api/mms/change-password'):
            # 独立限流桶：MMS 登录/改密不占 storyos 的 5 次/分，反之亦然
            if rate_limited_key(ip, 'mms_auth', 5):
                self._json(429, {'error': '尝试过于频繁，请 1 分钟后再试'})
                log('%s 429 限流 ip=%s' % (path, ip))
                return
            self._do_mms_auth(body, ip, path)
            return
        if rate_limited(ip):
            self._json(429, {'error': '尝试过于频繁，请 1 分钟后再试'})
            log('%s 429 限流 ip=%s' % (path, ip))
            return
        if path == '/api/change-password':
            self._do_change_password(body, ip)
            return
        account = str(body.get('account', '')).strip().replace(' ', '').replace('\t', '')
        password = str(body.get('password', ''))
        store = CFG['accounts'].get(account)
        if not store and account.endswith('B'):           # 容错：漏输 B 后缀（与原前端一致）
            store = CFG['accounts'].get(account[:-1])
        if not store or not hmac.compare_digest(password.encode(), effective_password(resolve_account(account) or account).encode()):
            self._json(401, {'error': '账号或密码错误，请重新输入'})
            log('/api/login 401 失败 account=%s ip=%s' % (account or '(空)', ip))
            return
        token = issue_token(store)
        log('/api/login 200 成功 account=%s store=%s ip=%s' % (account, store, ip))
        self._json(200, {'token': token, 'store': store, 'expiresIn': TTL_SECONDS})

    def _do_mms_auth(self, body, ip, path):
        """MMS 独立登录 / 独立改密：只查 mms_accounts.json，绝不碰 storyos 的 CFG。"""
        if not isinstance(body, dict):
            self._json(400, {'error': 'bad request'})
            return
        account = str(body.get('account', '')).strip().replace(' ', '').replace('\t', '')
        password = str(body.get('password', ''))
        if account not in MCFG['accounts'] or not hmac.compare_digest(
                password.encode(), str(MCFG['accounts'][account]).encode()):
            self._json(401, {'error': '账号或密码错误，请重新输入'})
            log('%s 401 失败 account=%s ip=%s' % (path, account or '(空)', ip))
            return
        if path.endswith('change-password'):
            new = str(body.get('newPassword', ''))
            if not new or len(new) > 64:
                self._json(400, {'error': '新密码需为 1-64 位'})
                return
            if hmac.compare_digest(new.encode(), password.encode()):
                self._json(400, {'error': '新密码不能与当前密码相同'})
                return
            MCFG['accounts'][account] = new
            save_mms_config()
            log('/api/mms/change-password 200 成功 account=%s ip=%s' % (account, ip))
            self._json(200, {'ok': True})
            return
        token = issue_mms_token(account)
        log('/api/mms/login 200 成功 account=%s ip=%s' % (account, ip))
        self._json(200, {'token': token, 'account': account, 'expiresIn': MMS_TTL_SECONDS})

    def _do_marks(self, body, ip):
        """回访标记批量对账：MMS 独立票据验票 + 按 _ts 合并 + 原子落盘"""
        if not isinstance(body, dict):
            self._json(400, {'error': 'bad request'}, open_cors=True)
            return
        if rate_limited_key(ip, 'marks', 120):
            self._json(429, {'error': '同步过于频繁，请稍后再试'}, open_cors=True)
            log('/api/marks 429 限流 ip=%s' % ip)
            return
        token = str(body.get('token') or '')
        if not verify_mms_token(token):
            self._json(401, {'error': '登录已失效，请重新登录'}, open_cors=True)
            return
        changes = body.get('changes')
        if not isinstance(changes, dict) or len(changes) > 500:
            self._json(400, {'error': 'invalid changes'}, open_cors=True)
            return
        try:
            doc, applied = merge_marks(changes)
        except Exception as e:
            log('/api/marks 500 写盘失败 %s ip=%s' % (e, ip))
            self._json(500, {'error': '标记写入失败，本次未保存'}, open_cors=True)
            return
        if applied:
            log('/api/marks 200 写入%d条 共%d条 ip=%s' % (applied, len(doc['marks']), ip))
        self._json(200, {'ok': True, 'marks': doc['marks'],
                         'updatedAt': doc.get('updatedAt'),
                         'revision': doc.get('revision', 0)}, open_cors=True)

    def _do_change_password(self, body, ip):
        """自助改密：凭 账号+当前密码 核验后改本人密码（每人独立，互不影响）"""
        account = str(body.get('account', '')).strip().replace(' ', '').replace('\t', '')
        cur = str(body.get('currentPassword', ''))
        new = str(body.get('newPassword', ''))
        acct = resolve_account(account)
        if not acct:
            self._json(401, {'error': '账号不存在，请核对姓名（华阳城部分账号带 B 后缀）'})
            log('/api/change-password 401 账号不存在 account=%s ip=%s' % (account or '(空)', ip))
            return
        if not hmac.compare_digest(cur.encode(), effective_password(acct).encode()):
            self._json(401, {'error': '当前密码不正确'})
            log('/api/change-password 401 当前密码错误 account=%s ip=%s' % (acct, ip))
            return
        if not new or len(new) > 64:
            self._json(400, {'error': '新密码需为 1-64 位'})
            return
        if hmac.compare_digest(new.encode(), cur.encode()):
            self._json(400, {'error': '新密码不能与当前密码相同'})
            return
        CFG.setdefault('accountPasswords', {})[acct] = new
        save_config()
        log('/api/change-password 200 成功 account=%s ip=%s' % (acct, ip))
        self._json(200, {'ok': True})

def main():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    log('售票厅启动 端口=%d 数据目录=%s' % (PORT, DATA_DIR))
    print('storyos ticket office listening on 0.0.0.0:%d' % PORT)
    srv.serve_forever()

if __name__ == '__main__':
    main()
