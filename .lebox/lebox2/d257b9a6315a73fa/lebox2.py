#!/usr/bin/env python3
"""lebox2 - ShunCodex GitHub channel v2 client (single file, W2 draft 0.1).

Spec: docs/GITHUB_CONNECTION_V2_PROTOCOL.md (draft 0.2).  Commands:
  join [--continue]   connect (A: git checkout of the collaboration repo; B: GitHub Device Flow + REST)
  call <tool> [json]  one MCP tools/call through the desktop
  status              resume a pending request, or show the state
  bye                 tell the desktop this Agent is leaving
  guide               print a short usage text
Every failure prints one line:  STOP: <reason> | <hint>   (exit 2).  A request without a result prints
PENDING n (exit 3): run `status`, never re-send.
State lives in ~/.lebox-state (not rebuildable).  No secret is ever printed.
"""
import argparse, base64, hashlib, importlib, json, os, secrets, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

V = 'scx-gh-v2'
CLIENT = 'lebox2/0.3'
PAYLOAD_LIMIT = 64 * 1024
CLIENT_ID = os.environ.get('LEBOX_CLIENT_ID', 'Iv23litSi7zqW2VLZMIh')  # public GitHub App client id
STATE_HOME = Path(os.environ.get('LEBOX_STATE_HOME') or (Path.home() / '.lebox-state'))
POLL = float(os.environ.get('LEBOX_POLL', '1.5'))
KEY_INFO = b'scx-gh/v2/keys'


class Stop(Exception):
    def __init__(self, code, reason, hint=''):
        super().__init__(reason); self.code, self.reason, self.hint = code, reason, hint


class Pending(Exception):
    def __init__(self, n): super().__init__(n); self.n = n


# ---------------------------------------------------------------- crypto
def _crypto():
    try:
        import cryptography  # noqa: F401
    except ImportError:
        deps = STATE_HOME / 'deps'
        if str(deps) not in sys.path: sys.path.insert(0, str(deps))
        try:
            import cryptography  # noqa: F401
        except ImportError:
            print('首次运行：正在安装加密库 cryptography 到 ' + str(deps) + '（会访问 pypi.org，只装这一个库）', file=sys.stderr)
            subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet', '--upgrade', '--target', str(deps), 'cryptography'],
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            importlib.invalidate_caches()       # the folder did not exist when the first import failed
            if str(deps) not in sys.path: sys.path.insert(0, str(deps))
            try:
                import cryptography  # noqa: F401,F811
            except ImportError:
                raise Stop('DEPS', '缺少加密库 cryptography 且自动安装失败', '运行 pip install cryptography 后重试')
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.exceptions import InvalidSignature
    return hashes, serialization, ec, AESGCM, HKDF, InvalidSignature


def canon(o): return json.dumps(o, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
def b64e(b): return base64.b64encode(b).decode('ascii')
def b64d(s): return base64.b64decode(s.encode('ascii'))
def sha(b): return hashlib.sha256(b).hexdigest()
def now(): return int(time.time())
def fingerprint(spki_der): return hashlib.sha256(spki_der).hexdigest()[:20]


def new_ephemeral():
    _, ser, ec, *_ = _crypto()
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(ser.Encoding.X962, ser.PublicFormat.UncompressedPoint)
    return priv, pub


def load_priv(b64):
    _, ser, ec, *_ = _crypto()
    return ec.derive_private_key(int.from_bytes(b64d(b64), 'big'), ec.SECP256R1())


def priv_scalar_b64(priv): return b64e(priv.private_numbers().private_value.to_bytes(32, 'big'))


def sas_of(hello): return '%06d' % (int.from_bytes(hashlib.sha256(canon(hello)).digest()[:4], 'big') % 1000000)


def derive_keys(priv, peer_pub_b64, transcript):
    hashes, _, ec, _, HKDF, _ = _crypto()
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64d(peer_pub_b64))
    shared = priv.exchange(ec.ECDH(), peer)
    okm = HKDF(algorithm=hashes.SHA256(), length=64, salt=transcript, info=KEY_INFO).derive(shared)
    return okm[:32], okm[32:]


def seal(key, header, plain):
    *_, AESGCM, _, _ = _crypto()
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plain, canon(header))
    return canon({'header': header, 'nonce': b64e(nonce), 'ct': b64e(ct)})


def unseal(key, env_bytes):
    *_, AESGCM, _, _ = _crypto()
    env = json.loads(env_bytes)
    try:
        return env['header'], AESGCM(key).decrypt(b64d(env['nonce']), b64d(env['ct']), canon(env['header']))
    except Exception:
        raise Stop('AUTH_FAIL', '收到的回复无法解密，可能被改动', '不要继续；重新接入')


def verify_sig(spki_der, signed_bytes, sig_b64):
    hashes, ser, ec, _, _, InvalidSignature = _crypto()
    pub = ser.load_der_public_key(spki_der)
    try:
        pub.verify(b64d(sig_b64), signed_bytes, ec.ECDSA(hashes.SHA256())); return True
    except InvalidSignature:
        return False


def heal_home():
    """A snapshot restore can also turn our state folder into 755. If it is ours, put it back to 700."""
    try:
        st = STATE_HOME.stat()
        if (st.st_mode & 0o077) and hasattr(os, 'getuid') and st.st_uid == os.getuid(): os.chmod(STATE_HOME, 0o700)
    except OSError: pass


def tighten(p):
    """A snapshot restore can leave our own state files at 644. If we own the file, tighten it to 600; otherwise refuse."""
    p = Path(p)
    st = p.stat()
    if not (st.st_mode & 0o077): return
    if hasattr(os, 'getuid') and st.st_uid == os.getuid():
        os.chmod(p, 0o600)
        print('已自动收紧本地文件权限为 600：' + str(p), file=sys.stderr)
        return
    raise Stop('STATE_PERMISSION', '本地状态文件不是你自己的，且别人可读', '运行 ls -l ' + str(p) + ' 检查后再继续')


def unsigned(d): return {k: v for k, v in d.items() if k != 'sig'}


# ---------------------------------------------------------------- transports (create-only)
class GitTransport:
    """A mode: a local git checkout of the collaboration repo, branch set by the platform."""
    kind = 'git'

    def __init__(self, repo, branch, remote='origin'):
        self.repo, self.branch, self.remote = str(repo), branch, remote

    def _g(self, *a, check=True):
        r = subprocess.run(['git', '-C', self.repo, *a], capture_output=True)
        if check and r.returncode != 0:
            raise RuntimeError('git ' + ' '.join(a[:2]) + ': ' + r.stderr.decode('utf-8', 'replace')[:200])
        return r

    def _fetch(self): return self._g('fetch', '--quiet', self.remote, self.branch, check=False).returncode == 0
    def _ref(self): return f'{self.remote}/{self.branch}'

    def get(self, path):
        self._fetch()
        r = self._g('cat-file', '-p', f'{self._ref()}:{path}', check=False)
        return r.stdout if r.returncode == 0 else None

    def list(self, d):
        self._fetch()
        r = self._g('ls-tree', '--name-only', self._ref(), d.rstrip('/') + '/', check=False)
        return [x.split('/')[-1] for x in r.stdout.decode().split('\n') if x] if r.returncode == 0 else []

    def put(self, path, data):
        for _ in range(6):
            if self._fetch(): self._g('rebase', '--autostash', self._ref(), check=False)
            full = Path(self.repo) / path
            if full.exists():
                if full.read_bytes() != data: return 'conflict'
                self._g('push', '--quiet', self.remote, f'HEAD:refs/heads/{self.branch}', check=False)   # make sure an earlier commit got out
                return 'same'
            full.parent.mkdir(parents=True, exist_ok=True); full.write_bytes(data)
            self._g('add', '--', path)
            self._g('-c', 'user.name=lebox2', '-c', 'user.email=lebox2@localhost', 'commit', '-q', '-m', 'lebox2: ' + path.split('/')[-1], '--', path)
            if self._g('push', '--quiet', self.remote, f'HEAD:refs/heads/{self.branch}', check=False).returncode == 0:
                return 'created'
        raise RuntimeError('push failed after retries')


class ApiTransport:
    """B mode: GitHub Contents API with the sandbox's own user token."""
    kind = 'api'

    def __init__(self, repo, branch, token, base=None):
        self.repo, self.branch, self.token = repo, branch, token
        self.base = base or os.environ.get('LEBOX_API_BASE', 'https://api.github.com')
        self.etags = {}

    def _req(self, method, path, body=None, accept='application/vnd.github+json', etag=None):
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None)
        req.add_header('Authorization', 'Bearer ' + self.token); req.add_header('Accept', accept)
        req.add_header('User-Agent', CLIENT)
        if etag: req.add_header('If-None-Match', etag)
        try:
            r = urllib.request.urlopen(req, timeout=30); return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def _p(self, path): return f'/repos/{self.repo}/contents/{urllib.parse.quote(path)}'

    def get(self, path):
        et = self.etags.get(path)
        s, body, h = self._req('GET', self._p(path) + '?ref=' + urllib.parse.quote(self.branch), accept='application/vnd.github.raw', etag=et[0] if et else None)
        if s == 304: return et[1]
        if s == 404: return None
        if s == 200:
            if h.get('ETag'): self.etags[path] = (h['ETag'], body)
            return body
        raise RuntimeError(f'GET {path}: HTTP {s}')

    def list(self, d):
        s, body, _ = self._req('GET', self._p(d) + '?ref=' + urllib.parse.quote(self.branch))
        if s == 404: return []
        if s != 200: raise RuntimeError(f'LIST {d}: HTTP {s}')
        return [x['name'] for x in json.loads(body)]

    def put(self, path, data):
        s, body, _ = self._req('PUT', self._p(path), {'message': 'lebox2: ' + path.split('/')[-1], 'content': b64e(data), 'branch': self.branch})
        if s in (200, 201): return 'created'
        if s in (409, 422):
            cur = self.get(path)
            if cur is not None: return 'same' if cur == data else 'conflict'
        raise RuntimeError(f'PUT {path}: HTTP {s}')

    def ensure_branch(self):
        s, body, _ = self._req('GET', f'/repos/{self.repo}/git/ref/heads/{urllib.parse.quote(self.branch)}')
        if s == 200: return
        s, body, _ = self._req('GET', f'/repos/{self.repo}')
        default = json.loads(body)['default_branch']
        s, body, _ = self._req('GET', f'/repos/{self.repo}/git/ref/heads/{default}')
        sha_ = json.loads(body)['object']['sha']
        s, body, _ = self._req('POST', f'/repos/{self.repo}/git/refs', {'ref': 'refs/heads/' + self.branch, 'sha': sha_})
        if s not in (200, 201, 422): raise RuntimeError(f'create branch: HTTP {s}')


# ---------------------------------------------------------------- state
class State:
    def __init__(self, sid):
        self.dir = STATE_HOME / 'v2' / sid
        self.path = self.dir / 'state.json'
        self.d = {}

    @staticmethod
    def current_sid(explicit=None):
        if explicit: return explicit
        p = STATE_HOME / 'v2' / 'current'
        return p.read_text().strip() if p.exists() else None

    def load(self):
        if not self.path.exists(): raise Stop('NO_STATE', '还没有接入记录', '先运行 join')
        tighten(self.path)
        self.d = json.loads(self.path.read_text()); return self

    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(STATE_HOME, 0o700)
        tmp = self.dir / 'state.json.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(self.d, f); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def lock(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        f = open(self.dir / 'lock', 'w')
        try:
            import fcntl; fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError: pass
        except OSError: raise Stop('BUSY', '同一会话已有另一个 lebox2 在运行', '等它结束')
        return f


def cred_path(): return STATE_HOME / 'credentials.json'


def device_key():
    """Long-lived key of this sandbox (next to the GitHub credential). Lets the desktop recognise a returning device."""
    _, _, ec, *_ = _crypto()
    p = STATE_HOME / 'device.json'
    if p.exists():
        tighten(p)
        return load_priv(json.loads(p.read_text())['priv'])
    STATE_HOME.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_HOME, 0o700)
    priv = ec.generate_private_key(ec.SECP256R1())
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f: json.dump({'priv': priv_scalar_b64(priv)}, f)
    return priv


def sign_hello(hello):
    hashes, ser, ec, *_ = _crypto()
    priv = device_key()
    hello['dev'] = b64e(priv.public_key().public_bytes(ser.Encoding.DER, ser.PublicFormat.SubjectPublicKeyInfo))
    hello.pop('sig', None)
    hello['sig'] = b64e(priv.sign(canon(hello), ec.ECDSA(hashes.SHA256())))


def save_cred(tok):
    STATE_HOME.mkdir(parents=True, exist_ok=True)
    fd = os.open(cred_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f: json.dump({'access_token': tok}, f)


def load_cred():
    p = cred_path()
    if not p.exists(): return None
    heal_home()
    tighten(p)
    return json.loads(p.read_text()).get('access_token')


def transport_of(st):
    d = st.d
    if d['transport'] == 'git': return GitTransport(d['repo_dir'], d['branch'])
    tok = load_cred()
    if not tok: raise Stop('B_AUTH_EXPIRED', '沙盒里没有 GitHub 授权', '如果你在对话里保存过 LEBOX_BACKUP 开头的那一行，运行 restore 把它放回；没有就运行 join 重新授权一次')
    return ApiTransport(d['repo'], d['branch'], tok)


BACKUP_PREFIX = 'LEBOX_BACKUP='


def make_backup(repo):
    tok = load_cred()
    if not tok: raise Stop('NO_CRED', '沙盒里没有 GitHub 授权，没有东西可备份', '先完成 join')
    body = b64e(canon({'v': 1, 'repo': repo, 'tok': tok}))
    return BACKUP_PREFIX + body + '.' + hashlib.sha256(body.encode()).hexdigest()[:8]


def parse_backup(line):
    line = line.strip()
    if not line.startswith(BACKUP_PREFIX) or '.' not in line: raise Stop('BAD_BACKUP', '这不是 lebox2 的备份行', '原样粘贴 LEBOX_BACKUP= 开头的整行')
    body, chk = line[len(BACKUP_PREFIX):].rsplit('.', 1)
    if hashlib.sha256(body.encode()).hexdigest()[:8] != chk: raise Stop('BAD_BACKUP', '备份行不完整或被改动', '重新原样复制整行')
    try:
        d = json.loads(b64d(body)); assert d['v'] == 1 and d['tok'] and d['repo']
    except Exception: raise Stop('BAD_BACKUP', '备份行无法读取', '重新原样复制整行')
    return d


def cmd_backup(a):
    st = State(State.current_sid(a.sid)).load() if State.current_sid(a.sid) else None
    repo = st.d['repo'] if st else ''
    if not repo: raise Stop('NO_STATE', '还没有接入记录', '先运行 join')
    print('下面这一行只含 GitHub 授权（只能访问协作仓库，对桌面执行工具仍需桌面批准），不含设备钥匙。')
    print('是否保存到对话里由你和你的用户决定；不要写进仓库、日志或文件。沙盒被清空后可用 restore 放回，省掉一次 GitHub 授权。')
    print(make_backup(repo)); return 0


def cmd_restore(a):
    d = parse_backup(a.line)
    try: s, _, _ = ApiTransport(d['repo'], 'main', d['tok'])._req('GET', '/repos/' + d['repo'])
    except OSError as e: raise Stop('NETWORK', '连不上 GitHub（' + str(e)[:60] + '）', '稍后重试同一条 restore')
    if s != 200: raise Stop('BACKUP_EXPIRED', f'备份里的 GitHub 授权已失效（HTTP {s}）', '不带 --restore 重新运行 join，用网址和短码授权一次')
    cur = load_cred()
    if cur != d['tok']: save_cred(d['tok'])
    print('已恢复 GitHub 授权。接着运行 join（不会再要求网址和短码）；如果 ~/.lebox-state 还在，直接运行 status。'); return 0


def sdir(sid): return f'.lebox/v2/{sid}'


def wait_for(fn, timeout):
    end, t0 = time.time() + timeout, time.time()
    while True:
        v = fn()
        if v is not None: return v
        if time.time() >= end: return None
        time.sleep(POLL if time.time() - t0 < 300 else 5)


# ---------------------------------------------------------------- join
def git_out(repo, *a):
    r = subprocess.run(['git', '-C', str(repo), *a], capture_output=True)
    return r.stdout.decode().strip() if r.returncode == 0 else ''


def cmd_join(a):
    if a.cont:
        sid = State.current_sid(a.sid)
        if not sid: raise Stop('NO_STATE', '还没有接入记录', '先运行 join')
        st = State(sid).load()
    else:
        st = new_session(a)
    lk = st.lock()
    try:
        if st.d.get('device_flow'): continue_device_flow(st)
        if not st.d.get('hello_sent'): send_hello(st, a)
        elif not st.d.get('hello_put'): put_hello(st)
        elif st.d.get('gen') is None: print_sas(st)
        wait_ack(st, a.wait)
    finally:
        lk.close()


def new_session(a):
    sid = a.sid or ''.join(secrets.choice('abcdefghijklmnopqrstuvwxyz0123456789') for _ in range(16))
    repo = a.repo or git_out(os.getcwd(), 'rev-parse', '--show-toplevel')
    mode = a.mode
    if mode == 'auto': mode = 'git' if (repo and git_out(repo, 'rev-parse', '--abbrev-ref', 'HEAD') not in ('', 'HEAD')) else 'api'
    st = State(sid)
    st.d = {'v': V, 'sid': sid, 'desktop_fp': a.desktop_fp, 'repo': a.repo_name, 'repo_id': a.repo_id, 'transport': mode,
            'gen': None, 'n_next': 1, 'ackn': 0, 'pending': None, 'device_flow': None, 'first': True, 'want_next': False, 'label': a.label}
    if mode == 'git':
        if not repo: raise Stop('NO_REPO', '当前目录不是 git 仓库', '在协作仓库的检出目录里运行，或加 --repo')
        st.d['repo_dir'] = repo
        st.d['branch'] = git_out(repo, 'rev-parse', '--abbrev-ref', 'HEAD')
        if not a.repo_name:
            u = git_out(repo, 'remote', 'get-url', 'origin'); st.d['repo'] = u.rstrip('/').removesuffix('.git').split('github.com/')[-1] if 'github.com' in u else u
    else:
        st.d['branch'] = 'agent/' + sid
        if not a.repo_name: raise Stop('NO_REPO', 'B 模式需要 --repo-name owner/name', '使用桌面接入说明里的完整命令')
    st.save()
    (STATE_HOME / 'v2').mkdir(parents=True, exist_ok=True)
    (STATE_HOME / 'v2' / 'current').write_text(sid)
    if mode == 'api' and not load_cred():
        device_flow_start(st)
    return st


def oauth(path, **form):
    base = os.environ.get('LEBOX_OAUTH_BASE', 'https://github.com')
    req = urllib.request.Request(base + path, data=urllib.parse.urlencode(form).encode(), headers={'Accept': 'application/json', 'User-Agent': CLIENT})
    try: return json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e: return json.loads(e.read() or b'{}')


def device_flow_start(st):
    r = oauth('/login/device/code', client_id=CLIENT_ID)
    if 'device_code' not in r: raise Stop('DEVICE_FLOW', 'GitHub 没有返回授权码', '确认应用已启用设备授权')
    st.d['device_flow'] = {'device_code': r['device_code'], 'expires': now() + int(r.get('expires_in', 900)), 'interval': int(r.get('interval', 5))}
    st.save()
    print(f"请打开 {r['verification_uri']} 输入授权码 {r['user_code']}，批准后运行: python3 lebox2.py join --continue")
    print('授权码不是秘密，只有你本人登录批准后才有用。')
    raise SystemExit(0)


def continue_device_flow(st):
    df = st.d['device_flow']
    end = min(now() + 60, df['expires'])
    while now() < end:
        r = oauth('/login/oauth/access_token', client_id=CLIENT_ID, device_code=df['device_code'], grant_type='urn:ietf:params:oauth:grant-type:device_code')
        if 'access_token' in r:
            save_cred(r['access_token']); st.d['device_flow'] = None; st.save(); return
        e = r.get('error')
        if e == 'slow_down': df['interval'] += 5
        elif e != 'authorization_pending':
            st.d['device_flow'] = None; st.save()
            raise Stop('DEVICE_FLOW', 'GitHub 授权失败：' + str(e), '重新运行 join')
        time.sleep(float(os.environ.get('LEBOX_DEVICE_POLL', df['interval'])))
    if now() >= df['expires']:
        st.d['device_flow'] = None; st.save(); raise Stop('DEVICE_FLOW', '授权码已过期', '重新运行 join')
    raise Stop('NOT_APPROVED', '你还没有在 GitHub 页面批准', '批准后再运行 join --continue')


def send_hello(st, a):
    d = st.d
    tr = transport_of(st)
    if tr.kind == 'api': tr.ensure_branch()
    priv, pub = new_ephemeral()
    hello = {'v': V, 'kind': 'hello', 'sid': d['sid'], 'repo_id': d['repo_id'], 'branch': d['branch'], 'desktop_fp': d['desktop_fp'],
             'pub': b64e(pub), 'challenge': b64e(os.urandom(16)), 'client': CLIENT,
             'caps': {'transport': tr.kind, 'python': '%d.%d' % sys.version_info[:2]}, 'label': d.get('label') or a.label, 'ts': now()}
    sign_hello(hello)
    d['eph_priv'] = priv_scalar_b64(priv); d['hello'] = hello; d['hello_sent'] = True
    st.save()  # persist before the network write
    put_hello(st)


def put_hello(st):
    d = st.d
    transport_of(st).put(sdir(d['sid']) + '/hello.json', canon(d['hello']))   # same bytes on every retry
    d['hello_put'] = True; st.save()
    print_sas(st)


def print_sas(st):
    d = st.d
    print(f"核对短语：{sas_of(d['hello'])}  请与桌面批准卡上的 6 位数字核对，一致再点【批准】。")
    print('（如果桌面认得这台设备，会自动批准，不用核对。）')


def adopt_ack(st, name, raw):
    d = st.d
    ack = json.loads(raw)
    unsigned_ack = canon(unsigned(ack))
    spki = b64d(ack['srv_static'])
    if fingerprint(spki) != d['desktop_fp'] or ack.get('sid') != d['sid']:
        raise Stop('AUTH_FAIL', '桌面回复无法确认来自你的 ShunCodex', '重新从桌面复制接入说明；不要继续')
    if d.get('srv_static') and d['srv_static'] != ack['srv_static']:
        raise Stop('AUTH_FAIL', '桌面签名密钥与首次不同', '重新从桌面复制接入说明；不要继续')
    if not verify_sig(spki, unsigned_ack, ack.get('sig', '')):
        raise Stop('AUTH_FAIL', '桌面回复的签名不对', '重新从桌面复制接入说明；不要继续')
    hello_hash = sha(canon(d['hello']))
    if ack.get('hello_hash') != hello_hash:
        raise Stop('AUTH_FAIL', '桌面回复对应的不是这次接入请求', '重新运行 join')
    gen = int(ack['gen'])
    if d.get('gen') is not None and gen <= d['gen'] and gen != 0:
        return False
    if 'enc_c' not in d:
        if 'pub' not in ack: raise Stop('AUTH_FAIL', '首个桌面回复缺少密钥材料', '重新运行 join')
        priv = load_priv(d['eph_priv'])
        d['enc_c'], d['enc_s'] = [b64e(k) for k in derive_keys(priv, ack['pub'], hashlib.sha256(canon(d['hello']) + unsigned_ack).digest())]
        d['srv_static'] = ack['srv_static']; d.pop('eph_priv', None)
    d['gen'] = gen
    d['seen'] = max(d.get('seen', 0), int(ack.get('at_gen', gen)))
    st.save()
    return True


def wait_ack(st, timeout):
    d = st.d
    if d.get('gen') is not None:
        print_connected(st); return
    tr = transport_of(st)
    base = sdir(d['sid'])

    def find():
        for nm in sorted(tr.list(base), key=lambda x: (len(x), x)):
            if nm.startswith('ack.') and nm.endswith('.json'):
                raw = tr.get(base + '/' + nm)
                if raw and adopt_ack(st, nm, raw): return True
            if nm.startswith('bye.') and nm.endswith('.json'):
                raw = tr.get(base + '/' + nm)
                if raw:
                    b = json.loads(raw)
                    if b.get('by') == 'desktop' and b.get('reason') == 'revoke': raise Stop('USER_REJECTED', '你在桌面拒绝了这次接入', '无需处理')
        return None
    if wait_for(find, timeout) is None:
        raise Stop('NOT_APPROVED', '桌面还没有批准这次接入', '到桌面点【批准】后运行 join --continue')
    print_connected(st)


def print_connected(st):
    g = st.d['gen']
    print('已连接，第 %d 代。' % g if g else '已登记，但当前另一个 Agent 在线；发起第一次调用时会按桌面的切换设置处理。')
    print('不知道桌面有哪些工具？运行 python3 lebox2.py tools，不要猜工具名。')
    if st.d.get('transport') == 'api':
        print('可选：运行 python3 lebox2.py backup，把打印的那一行留在你自己的对话里；沙盒文件被清空时用 restore 放回即可，不用再授权 GitHub。')


# ---------------------------------------------------------------- requests
def refresh_grants(st, tr):
    d = st.d; base = sdir(d['sid'])
    for nm in tr.list(base):
        if nm.startswith('ack.') and nm.endswith('.json'):
            try: g = int(nm.split('.')[1])
            except ValueError: continue
            if g > (d['gen'] or 0):
                raw = tr.get(base + '/' + nm)
                if raw: adopt_ack(st, nm, raw)
        elif nm.startswith('bye.') and nm.endswith('.json') and not d.get('bye_seen', {}).get(nm):
            raw = tr.get(base + '/' + nm)
            if not raw: continue
            b = json.loads(raw)
            if b.get('by') == 'desktop' and verify_sig(b64d(d['srv_static']), canon(unsigned(b)), b.get('sig', '')):
                d.setdefault('bye_seen', {})[nm] = True
                d['seen'] = max(d.get('seen', 0), int(b.get('gen', 0)))
                if b.get('reason') == 'superseded': d['want_next'] = True
                if b.get('reason') == 'revoke': raise Stop('REVOKED', '这个 Agent 的授权已被撤销', '如需使用，重新接入为新 Agent')
                st.save()


def fetch_result(st, tr, n):
    d = st.d
    raw = tr.get(f"{sdir(d['sid'])}/{n:010d}.res.json")
    if raw is None: return None
    h, plain = unseal(b64d(d['enc_s']), raw)
    if h.get('sid') != d['sid'] or h.get('n') != n or h.get('kind') != 'res':
        raise Stop('AUTH_FAIL', '收到的回复与请求对不上', '不要继续；重新接入')
    d['seen'] = max(d.get('seen', 0), int(h.get('gen', 0)))      # generation the desktop says is online now
    return json.loads(plain)


def finish(st, n, res):
    d = st.d
    d['pending'] = None; d['n_next'] = n + 1; d['ackn'] = n; d['first'] = False
    code = (res.get('error') or {}).get('code')
    if not res.get('ok') and code in ('SUPERSEDED', 'GEN_STALE', 'BUSY_UNCONFIRMED', 'WAITING_SWITCH_APPROVAL'): d['want_next'] = True
    st.save()
    if res.get('ok'):
        print(json.dumps(res['result'], ensure_ascii=False)); return 0
    e = res['error']
    raise Stop(e.get('code', 'ERROR'), e.get('message', '桌面返回错误'), STOP_HINT.get(e.get('code'), ''))


STOP_HINT = {'GEN_STALE': '重新发起一次新调用', 'SUPERSEDED': '如需切换，重新发起一次新调用（自动模式会切换，手动模式需桌面批准）',
             'WAITING_SWITCH_APPROVAL': '到桌面点“切换到它”', 'BUSY_UNCONFIRMED': '等任务结束，或到桌面处理', 'PAUSED': '到桌面点“恢复”',
             'REVOKED': '如需使用，重新接入为新 Agent', 'SEQUENCE_GAP': '不要重试；让用户在桌面查看', 'REQUEST_CONTENT_CONFLICT': '不要重试；报告给用户'}


def resume_pending(st, tr, wait):
    d = st.d; p = d['pending']; n = p['n']
    res = fetch_result(st, tr, n)
    if res is None:
        path = f"{sdir(d['sid'])}/{n:010d}.req.json"
        if tr.get(path) is None:
            r = tr.put(path, b64d(p['envelope']))          # byte-identical resend, never a new id
            if r == 'conflict': raise Stop('REQUEST_CONTENT_CONFLICT', '同一请求编号的内容不一致', '不要重试；报告给用户')
        res = wait_for(lambda: fetch_result(st, tr, n), wait)
    if res is None: raise Pending(n)
    return finish(st, n, res)


def cmd_call(a):
    sid = State.current_sid(a.sid)
    if not sid: raise Stop('NO_STATE', '还没有接入记录', '先运行 join')
    st = State(sid).load(); lk = st.lock()
    try:
        d = st.d
        if d.get('gen') is None: raise Stop('NOT_APPROVED', '这次接入还没有被桌面批准', '运行 join --continue')
        tr = transport_of(st)
        if d['pending']: return resume_pending(st, tr, a.wait)
        refresh_grants(st, tr)
        args = json.loads(a.args) if a.args else {}
        plain = canon({'tool': a.tool, 'arguments': args})
        if len(plain) > PAYLOAD_LIMIT: raise Stop('PAYLOAD_LIMIT', '内容超过 64 KB', '缩小请求或分段')
        n = d['n_next']
        want = bool(d['first'] or d.get('want_next'))
        header = {'v': V, 'kind': 'req', 'sid': d['sid'], 'gen': d['gen'], 'n': n, 'ackn': d['ackn'], 'want_online': want, 'seen_gen': d.get('seen', 0), 'ts': now()}
        env = seal(b64d(d['enc_c']), header, plain)
        d['pending'] = {'n': n, 'hash': sha(env), 'envelope': b64e(env)}; d['want_next'] = False
        st.save()                                               # persist pending BEFORE the network write
        return resume_pending(st, tr, a.wait)
    finally:
        lk.close()


def cmd_tools(a):
    """Ask the desktop which tools this Agent may call (a reserved name answered by the desktop, never executed)."""
    a.tool, a.args = 'lebox_tools', ''
    return cmd_call(a)


def cmd_status(a):
    sid = State.current_sid(a.sid)
    if not sid: print('还没有接入记录。运行 join。'); return 0
    st = State(sid).load(); d = st.d
    if d.get('pending'):
        lk = st.lock()
        try: return resume_pending(st, transport_of(st), a.wait)
        finally: lk.close()
    state = '未批准' if d.get('gen') is None else ('已登记未上线' if d['gen'] == 0 else '已连接')
    print(f"状态：{state}；第 {d.get('gen')} 代；下一个请求序号 {d['n_next']}；方式 {d['transport']}；分支 {d['branch']}")
    if d.get('gen') is None and d.get('hello_put'): print_sas(st)
    return 0


def cmd_bye(a):
    sid = State.current_sid(a.sid)
    if not sid: return 0
    st = State(sid).load(); d = st.d
    transport_of(st).put(f"{sdir(sid)}/bye.agent.json", canon({'v': V, 'kind': 'bye', 'sid': sid, 'by': 'agent', 'reason': 'agent_exit', 'gen': d.get('gen') or 0}))
    print('已通知桌面：本 Agent 退出。'); return 0


GUIDE = """lebox2 用法：
  python3 lebox2.py join --desktop-fp <指纹> --repo-id <id> [--repo-name owner/name]   首次接入（A 在协作仓库检出目录里运行；B 加 --mode api）
  python3 lebox2.py join --continue                                                     批准后继续
  python3 lebox2.py tools                                                               看桌面开放了哪些工具（名字、一句话说明、必填参数）
  python3 lebox2.py call <工具名> '<JSON 参数>'                                        调用桌面上的工具
  python3 lebox2.py status                                                              取回未完成请求或查看状态
  python3 lebox2.py backup / restore '<LEBOX_BACKUP=…>'                                 （仅 B，可选）打印/放回一行 GitHub 授权备份
看到 PENDING n：只运行 status，不要重新发起。看到 STOP：把整行原样告诉用户。"""


def main(argv=None):
    p = argparse.ArgumentParser(prog='lebox2'); sub = p.add_subparsers(dest='cmd', required=True)
    j = sub.add_parser('join'); j.add_argument('--continue', dest='cont', action='store_true')
    j.add_argument('--desktop-fp', default=''); j.add_argument('--repo-id', default=''); j.add_argument('--repo-name', default='')
    j.add_argument('--repo', default=''); j.add_argument('--mode', choices=['auto', 'git', 'api'], default='auto')
    j.add_argument('--label', default='Agent'); j.add_argument('--wait', type=float, default=120); j.add_argument('--sid', default='')
    c = sub.add_parser('call'); c.add_argument('tool'); c.add_argument('args', nargs='?', default='')
    for s_ in (c, sub.add_parser('status'), sub.add_parser('bye'), sub.add_parser('tools')):
        s_.add_argument('--wait', type=float, default=120); s_.add_argument('--sid', default='')
    sub.add_parser('guide')
    b = sub.add_parser('backup'); b.add_argument('--sid', default='')
    r_ = sub.add_parser('restore'); r_.add_argument('line')
    a = p.parse_args(argv)
    try:
        if a.cmd == 'guide': print(GUIDE); return 0
        if a.cmd == 'join':
            if not a.cont and not a.desktop_fp: raise Stop('ARGS', '缺少 --desktop-fp（桌面接入说明里的指纹）', '重新从桌面复制接入说明')
            cmd_join(a); return 0
        return {'call': cmd_call, 'status': cmd_status, 'bye': cmd_bye, 'tools': cmd_tools, 'backup': cmd_backup, 'restore': cmd_restore}[a.cmd](a) or 0
    except Pending as e:
        print(f'PENDING {e.n}  请求已发出，结果还没回来。稍后运行 status；不要重新发起。'); return 3
    except Stop as e:
        print(f'STOP: {e.reason} | {e.hint}' if e.hint else f'STOP: {e.reason}'); return 2
    except RuntimeError as e:
        print(f'STOP: 传输失败（{str(e)[:120]}） | 稍后重试同一命令，不要换 id'); return 2


if __name__ == '__main__':
    sys.exit(main())
