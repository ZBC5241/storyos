#!/usr/bin/env python3
# storyos 售票厅 —— 登录鉴权 + 数据下发（2026-10-23 建，18号）
# 背景：原看板把数据+密码全内嵌页面，未登录 curl 即得全部经营数据（P0 泄露）。
#       本服务把数据搬到本机，验票才吐；GitHub Pages 上只留空壳页面。
#
# 端点：
#   POST /api/login   {account, password} → {token, store, expiresIn}   （限流 5 次/分/IP）
#   POST /api/change-password {account, currentPassword, newPassword} → {ok}（限流同 login；凭账号+当前密码自助改密）
#   GET  /api/data?token=...               → {store, data}              （按 token 的 store 授权）
#   GET  /api/health                       → {ok, age}                  （供保活探活）
#
# 数据源：server/data/{ljc,hyc}.json（由 inject_data.py 在管线每次跑完后写入，mtime 变化自动重载）
# 授权模型：token = base64url(store|exp) + HMAC-SHA256 签名；单店 token 只能取本店数据，
#           zbc(all) token 一次取两店（前端切换器用，不再重复请求）。
#
# 纯标准库，无第三方依赖；macOS python3 直接跑。
import base64, gzip, hashlib, hmac, json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, 'config.json')
DATA_DIR = os.path.join(BASE, 'data')
LOG_PATH = os.path.join(BASE, 'logs', 'server.log')

# ---------- 配置 ----------
def load_config():
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return json.load(f)

CFG = load_config()
SECRET = CFG['token_secret'].encode('utf-8')
TTL_SECONDS = int(CFG.get('ttl_hours', 48)) * 3600
PORT = int(CFG.get('port', 8798))

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

    def _cors(self):
        origin = self.headers.get('Origin', '')
        # 白名单：Pages 官方源 + 本机调试（localhost/127.0.0.1 任意端口）+ file:// 测试页(Origin=null)
        ok = (origin == 'https://zbc5241.github.io' or origin == 'null'
              or origin.startswith('http://localhost') or origin.startswith('http://127.0.0.1'))
        allow = origin if ok else 'https://zbc5241.github.io'
        self.send_header('Access-Control-Allow-Origin', allow)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Cache-Control', 'no-store')

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        # gzip：双店全量载荷 ~525KB，压缩后 ~80KB——手机流量友好，也避免大响应体在隧道里被截断
        gz = 'gzip' in (self.headers.get('Accept-Encoding') or '') and len(body) > 1024
        if gz:
            body = gzip.compress(body, 6)
        self.send_response(code)
        self._cors()
        if gz:
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Vary', 'Origin, Accept-Encoding')
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/health':
            age = DATA.age_seconds()
            self._json(200 if age is not None else 503, {'ok': age is not None, 'dataAgeSec': age})
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
        self._json(404, {'error': 'not found'})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ('/api/login', '/api/change-password'):
            self._json(404, {'error': 'not found'})
            return
        ip = self.client_address[0]
        if rate_limited(ip):
            self._json(429, {'error': '尝试过于频繁，请 1 分钟后再试'})
            log('%s 429 限流 ip=%s' % (path, ip))
            return
        try:
            n = int(self.headers.get('Content-Length', '0'))
            body = json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception:
            self._json(400, {'error': 'bad request'})
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
