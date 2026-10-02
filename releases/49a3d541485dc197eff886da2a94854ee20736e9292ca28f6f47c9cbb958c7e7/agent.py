# lebox collaboration launcher · lebox-join-v15
# Lives in the repository at /.lebox/agent.py (maintained by the lebox desktop). Run from the repository working
# directory of an Arena conversation:  python .lebox/agent.py join
# It pushes a join request on the current arena/* branch, waits for the desktop to publish this session's bootstrap,
# verifies and unpacks it outside the repository, installs the one dependency, pairs and activates. Every failure prints one
# "STOP:" line meant to be relayed to the user verbatim. No tokens, cookies or keys are requested; only your existing git auth.
import hashlib, json, os, pathlib, re, shlex, shutil, subprocess, sys, tempfile, time

# BEGIN LEBOX PERSISTENT STATE LAYOUT V1
# Self-contained, identical in both trusted entry points; no unverified runtime import.
import contextlib as _state_contextlib
import pathlib as _state_pathlib

class StateLocationError(ValueError):
    pass

_STATE_EXCLUDED = frozenset(('.git', '.git-credentials', '.netrc', '.arena', '.cache', '.local', '.venv', '.npm', '.next',
    '.nuxt', '.output', '.parcel-cache', '.pytest_cache', '.ruff_cache', '.svelte-kit',
    '.tox', '.turbo', '.vite', '.mypy_cache', '.nox', '__pycache__', 'node_modules',
    'build', 'coverage', 'dist', 'out', 'target'))

def _state_no_links(path):
    path = _state_pathlib.Path(os.path.abspath(str(path)))
    for item in (path, *path.parents):
        if item.is_symlink():
            raise StateLocationError('STATE_PATH_LINK: preserve original state; do not follow links')
    return path

def state_repo_boundary(path):
    if path is None:
        return None
    path = _state_no_links(path)
    for item in (path, *path.parents):
        if (item / '.git').exists():
            return item.resolve()
    return None  # API publication does not make all of HOME a Git worktree.

def persistent_state_home(repo=None):
    home = _state_no_links(_state_pathlib.Path.home()).resolve()
    value = os.environ.get('LEBOX_STATE_HOME') or os.environ.get('SCX_STATE_HOME')
    root = _state_pathlib.Path(value).expanduser() if value else home / '.lebox-state'
    if not root.is_absolute():
        raise StateLocationError('STATE_HOME_ABSOLUTE_REQUIRED')
    root = _state_no_links(root)
    if root == home or not root.is_relative_to(home) or any(p in _STATE_EXCLUDED for p in root.relative_to(home).parts):
        raise StateLocationError('STATE_HOME_NOT_PERSISTENT: choose a private, non-excluded directory inside HOME')
    boundary = state_repo_boundary(repo)
    actual_boundary = state_repo_boundary(root)
    if (boundary is not None and root.is_relative_to(boundary)) or actual_boundary is not None:
        raise StateLocationError('STATE_HOME_IN_REPOSITORY: do not publish private state')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _state_no_links(root)
    if os.name != 'nt' and root.stat().st_uid != os.getuid():
        raise StateLocationError('STATE_HOME_OWNER_MISMATCH')
    os.chmod(root, 0o700)
    return root

def _state_json(raw):
    if len(raw) > 1_200_000:
        raise StateLocationError('STATE_SIZE_LIMIT')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise StateLocationError('STATE_DUPLICATE_FIELD')
            value[key] = item
        return value
    try:
        def invalid_constant(_):
            raise StateLocationError('STATE_INVALID_NUMBER')
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (ValueError, UnicodeError) as error:
        raise StateLocationError('STATE_INVALID_JSON: original file preserved') from error
    if not isinstance(value, dict):
        raise StateLocationError('STATE_OBJECT_REQUIRED')
    return value

def _state_canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')

def _state_read(path):
    import stat
    path = _state_no_links(path)
    # Validate the file descriptor that will actually be read; never stat then reopen.
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1_200_000 or info.st_nlink != 1:
            raise StateLocationError('STATE_FILE_UNSAFE')
        if os.name != 'nt' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise StateLocationError('STATE_FILE_NOT_PRIVATE')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(1_200_001)
        if len(raw) > 1_200_000:
            raise StateLocationError('STATE_SIZE_LIMIT')
        return raw
    finally:
        os.close(fd)


def _state_write(path, raw):
    path = _state_no_links(path)
    temp = path.with_name('.state-write-' + os.urandom(12).hex())
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as file:
            file.write(raw); file.flush(); os.fsync(file.fileno())
        os.replace(temp, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try: os.fsync(directory)
            finally: os.close(directory)
    finally:
        temp.unlink(missing_ok=True)

@_state_contextlib.contextmanager
def _state_lock(root):
    import stat
    root = _state_no_links(root)
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or (os.name != 'nt' and (info.st_uid != os.getuid() or info.st_mode & 0o077)):
        raise StateLocationError('STATE_DIRECTORY_NOT_PRIVATE')
    lock = _state_no_links(root / 'client.lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (os.name != 'nt' and (info.st_uid != os.getuid() or info.st_mode & 0o077)):
            raise StateLocationError('STATE_LOCK_NOT_PRIVATE')
        if os.name == 'nt':
            import msvcrt
            if os.fstat(fd).st_size == 0: os.write(fd, b'0')
            os.lseek(fd, 0, 0); msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except (BlockingIOError, PermissionError) as error:
        raise StateLocationError('STATE_BUSY: original operation may still be running') from error
    finally:
        os.close(fd)

def persistent_session_directory(config, repo=None):
    binding = config.get('binding', '')
    if not isinstance(binding, str) or not re.fullmatch(r'[0-9a-f]{32}', binding):
        raise StateLocationError('STATE_BINDING_INVALID')
    encoded = _state_canonical(config); digest = hashlib.sha256(encoded).hexdigest()
    root = persistent_state_home(repo) / ('scx-collaboration-' + binding)
    _state_no_links(root); root.mkdir(mode=0o700, exist_ok=True)
    if os.name != 'nt' and root.stat().st_uid != os.getuid():
        raise StateLocationError('STATE_DIRECTORY_OWNER_MISMATCH')
    os.chmod(root, 0o700)
    old = _state_no_links(_state_pathlib.Path(tempfile.gettempdir()) / root.name)
    boundary = state_repo_boundary(repo)
    if boundary is not None and old.resolve().is_relative_to(boundary):
        raise StateLocationError('LEGACY_STATE_IN_REPOSITORY')
    target = root / 'state.json'; descriptor = root / 'connection.json'
    def validate(raw):
        if _state_json(raw).get('config_hash') != digest:
            raise StateLocationError('STATE_CONFIG_MISMATCH: never replace an existing identity')
    with _state_lock(root):
        if descriptor.exists() and _state_canonical(_state_json(_state_read(descriptor))) != encoded:
            raise StateLocationError('STATE_DESCRIPTOR_MISMATCH')
        if target.exists(): validate(_state_read(target))
        if old.resolve() != root.resolve() and (old / 'state.json').exists():
            with _state_lock(old):
                original = _state_read(old / 'state.json'); validate(original)
                source_hash = hashlib.sha256(original).hexdigest()
                receipt = root / 'migration.json'
                record = {'format': 'lebox-state-migration-v1', 'source': str(old.resolve()),
                          'source_sha256': source_hash, 'config_hash': digest}
                if receipt.exists() and not target.exists():
                    raise StateLocationError('STATE_MIGRATION_TARGET_MISSING: preserve original evidence')
                if target.exists() and _state_read(target) != original:
                    if not receipt.exists() or _state_json(_state_read(receipt)) != record:
                        raise StateLocationError('STATE_MIGRATION_CONFLICT: keep both states and reconcile')
                elif not receipt.exists():
                    if not target.exists(): _state_write(target, original)
                    _state_write(receipt, _state_canonical(record))
                elif _state_json(_state_read(receipt)) != record:
                    raise StateLocationError('STATE_MIGRATION_CONFLICT: legacy state changed')
        if not descriptor.exists(): _state_write(descriptor, encoded)
    return root

# END LEBOX PERSISTENT STATE LAYOUT V1

# BEGIN LEBOX PROCESS GIT AUTH V1
# Self-contained in both pinned entry points; no credential-helper/global-config persistence.
def _git_auth_repository(value):
    from urllib.parse import urlsplit
    if not isinstance(value, str):
        raise StateLocationError('GIT_AUTH_TARGET_INVALID')
    if value.startswith('git@github.com:'):
        value = value[len('git@github.com:'):]
    elif '://' in value:
        try:
            parsed = urlsplit(value)
            if (parsed.scheme != 'https' or parsed.hostname != 'github.com' or parsed.port not in (None, 443)
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError()
            value = parsed.path[1:] if parsed.path.startswith('/') else parsed.path
        except ValueError:
            raise StateLocationError('GIT_AUTH_TARGET_INVALID') from None
    if value.endswith('.git'):
        value = value[:-4]
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value) or any(p in ('.', '..') for p in value.split('/')):
        raise StateLocationError('GIT_AUTH_TARGET_INVALID')
    return value


def _git_auth_environment(token, repository):
    """Use current explicitly supplied/verified credentials in process memory, never argv or helper storage."""
    import base64 as _auth_base64
    repository = _git_auth_repository(repository)
    if (not isinstance(token, str) or not 8 <= len(token) <= 4096
            or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in token)):
        raise StateLocationError('GIT_AUTH_CREDENTIAL_INVALID')
    env = dict(os.environ)
    count = env.get('GIT_CONFIG_COUNT', '0')
    if not re.fullmatch(r'(?:0|[1-9][0-9]{0,2})', count) or int(count) > 256:
        raise StateLocationError('GIT_AUTH_CONFIG_UNVERIFIED')
    count = int(count)
    if any('GIT_CONFIG_KEY_%d' % i not in env or 'GIT_CONFIG_VALUE_%d' % i not in env for i in range(count)):
        raise StateLocationError('GIT_AUTH_CONFIG_UNVERIFIED')
    authorization = 'Authorization: Basic ' + _auth_base64.b64encode(('x-access-token:' + token).encode()).decode('ascii')
    entries = [('credential.helper', ''), ('core.askPass', ''), ('http.followRedirects', 'false'),
               ('http.extraHeader', ''), ('http.https://github.com/.extraHeader', '')]
    for suffix in ('', '.git'):
        key = 'http.https://github.com/' + repository + suffix + '.extraHeader'
        entries.extend(((key, ''), (key, authorization),
                        ('credential.https://github.com/' + repository + suffix + '.helper', ''),
                        ('credential.https://github.com/' + repository + suffix + '.useHttpPath', 'true')))
    # Reuse only this exact trailing guard block for the same repository. Repeated renewal
    # must not grow process config without bound; unrelated inherited entries stay untouched.
    if count >= len(entries):
        tail = [(env['GIT_CONFIG_KEY_%d' % i], env['GIT_CONFIG_VALUE_%d' % i])
                for i in range(count - len(entries), count)]
        if all(old_key == key and (old_value.startswith('Authorization: Basic ')
                if value == authorization else old_value == value)
                for (old_key, old_value), (key, value) in zip(tail, entries)):
            count -= len(entries)
    if count + len(entries) > 256:
        raise StateLocationError('GIT_AUTH_CONFIG_UNVERIFIED')
    for index, (key, value) in enumerate(entries, count):
        env['GIT_CONFIG_KEY_%d' % index] = key
        env['GIT_CONFIG_VALUE_%d' % index] = value
    env['GIT_CONFIG_COUNT'] = str(count + len(entries))
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['GIT_ASKPASS'] = ''
    env['SSH_ASKPASS'] = ''
    return env
def _git_auth_ready_environment(token, repository):
    # GIT_CONFIG_COUNT/KEY/VALUE support is required; old/unknown Git must not silently
    # fall back to global helpers or platform credentials. Never include raw stderr.
    env = _git_auth_environment(token, repository)
    try:
        result = subprocess.run(['git', '--version'], capture_output=True, text=True, timeout=5, env=env)
        match = re.match(r'^git version ([0-9]+)\.([0-9]+)(?:[.\s]|$)', result.stdout or '')
        if result.returncode != 0 or match is None or tuple(map(int, match.groups())) < (2, 31):
            raise StateLocationError('GIT_PROCESS_AUTH_REQUIRES_2_31')
    except (OSError, subprocess.TimeoutExpired):
        raise StateLocationError('GIT_PROCESS_AUTH_UNAVAILABLE') from None
    return env
# END LEBOX PROCESS GIT AUTH V1

VERSION = 'lebox-join-v15'
JOIN_SUBJECT = 'lebox: join'
BOOTSTRAP = re.compile(r'\.(lebox|shuncodex-bridge-test)/session-[0-9a-f]{32}/bootstrap\.json')
WAIT_SECONDS = 900
BRANCH_OVERRIDE = ''  # set by --branch
TOKEN_ENV = os.environ.get('LEBOX_TOKEN_ENV', 'GITHUB_TOKEN')
# Token pasted with the instructions (LEBOX_TOKEN). Used ONLY when the sandbox has no GitHub authorization of its own.
PASTED_TOKEN = os.environ.get('LEBOX_TOKEN', '').strip()
# GitHub App public client id (from the instructions or --client-id). With it, a sandbox without any GitHub authorization
# obtains its own token through GitHub's Device Flow: the user enters an 8-character code on github.com/login/device.
CLIENT_ID = os.environ.get('LEBOX_CLIENT_ID', '').strip()
# Pairing code pre-issued by the desktop (the device_code of a Device Flow the user approves from a lebox popup).
# With it the Agent never prints a code: it just collects the token GitHub already holds for that approval.
DEVICE_CODE = os.environ.get('LEBOX_PAIR', '').strip()
AUTH_SOURCE = ''   # 'sandbox' | 'pasted-token' — reported in READY
REPO_HINT = os.environ.get('LEBOX_REPO', '').strip()

API_MODE = False        # no git binary → GitHub REST API with a token from the environment
API_TOKEN = ''
API_REPO = ''           # owner/name, from --repo or LEBOX_REPO
import base64, urllib.request, urllib.error
from urllib.parse import quote


def api(method, path, body=None, ok=(200, 201), allow=()):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request('https://api.github.com' + path, data=data, method=method, headers={
        'Authorization': 'Bearer ' + API_TOKEN, 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'lebox/1.0', **({'Content-Type': 'application/json'} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, (json.loads(r.read(2_000_000) or b'{}'))
    except urllib.error.HTTPError as ex:
        raw = ex.read(100_000)
        if ex.code in allow:
            return ex.code, (json.loads(raw) if raw else {})
        if ex.code == 401:
            stop('GitHub 令牌无效（401）', '请用户提供仅限该仓库、Contents 读写权限的细粒度令牌，放在环境变量 %s' % TOKEN_ENV)
        if ex.code in (403, 429):
            stop('GitHub 拒绝或限流（%d）' % ex.code, '检查令牌是否有该仓库 Contents 读写权限；稍后重试')
        if ex.code == 404:
            stop('令牌看不到仓库 %s 或分支（404）' % API_REPO, '确认令牌授权了这个私有仓库')
        stop('GitHub 请求失败（%d）' % ex.code, '把这行原样告诉用户')
    except (urllib.error.URLError, TimeoutError, OSError) as ex:
        stop('沙盒无法访问 api.github.com：%s' % str(ex)[:120], '请用户检查沙盒网络')


def api_branch_tip(branch):
    st, ref = api('GET', '/repos/%s/git/ref/heads/%s' % (API_REPO, quote(branch, safe='')), allow=(404,))
    return None if st == 404 else ref['object']['sha']


def api_bootstrap_files(tip):
    st, tree = api('GET', '/repos/%s/git/trees/%s?recursive=1' % (API_REPO, tip))
    return [e['path'] for e in tree.get('tree', []) if e.get('type') == 'blob' and BOOTSTRAP.fullmatch(e['path'])]


def api_blob(tip, path):
    st, f = api('GET', '/repos/%s/contents/%s?ref=%s' % (API_REPO, quote(path), tip))
    return base64.b64decode(f['content'])


def api_added_at(tip, path):
    st, commits = api('GET', '/repos/%s/commits?sha=%s&path=%s&per_page=1' % (API_REPO, tip, quote(path)))
    if not commits:
        return 0
    return int(time.mktime(time.strptime(commits[-1]['commit']['committer']['date'], '%Y-%m-%dT%H:%M:%SZ'))) - time.timezone


def api_push_join(branch):
    """Create the branch (from the default branch) if needed, then add an empty 'lebox: join' commit via the Git Data API."""
    tip = api_branch_tip(branch)
    if tip is None:
        st, repo = api('GET', '/repos/%s' % API_REPO)
        base = api_branch_tip(repo['default_branch'])
        api('POST', '/repos/%s/git/refs' % API_REPO, {'ref': 'refs/heads/' + branch, 'sha': base})
        tip = base
        print('OK: 已基于默认分支创建 %s' % branch, flush=True)
    st, parent = api('GET', '/repos/%s/git/commits/%s' % (API_REPO, tip))
    st, commit = api('POST', '/repos/%s/git/commits' % API_REPO, {'message': JOIN_SUBJECT, 'tree': parent['tree']['sha'], 'parents': [tip]})
    api('PATCH', '/repos/%s/git/refs/heads/%s' % (API_REPO, quote(branch, safe='')), {'sha': commit['sha']})
    print('OK: 已推送接入请求 (%s) 到 %s/%s（API 模式）' % (JOIN_SUBJECT, API_REPO, branch), flush=True)



def stop(reason, advice=''):
    print('STOP: ' + reason + (' | ' + advice if advice else ''), flush=True)
    sys.exit(2)


def git(*args, check=True, text=True):
    r = subprocess.run(['git', *args], capture_output=True, text=text)
    if check and r.returncode:
        raise RuntimeError((r.stderr if text else r.stderr.decode('utf-8', 'replace')).strip())
    return r.stdout


def remote_url():
    try:
        return git('remote', 'get-url', 'origin').strip()
    except RuntimeError:
        return ''


def device_flow_token(client_id, repo):
    """GitHub Device Flow with the App's public client id. No secret involved; the user approves on github.com.
    Returns the user token (scope = the App's installation on the user's account, i.e. the collaboration repository)."""
    if os.environ.get('LEBOX_SECRET_FREE') == '1':
        stop('SAFE_AUTHORIZATION_UI_REQUIRED', '请运行已校验 boot.py 的专用授权页入口；不在聊天生成短码')
    import urllib.request as _u, urllib.parse as _p
    def post(path, fields):
        req = _u.Request('https://github.com' + path, data=_p.urlencode(fields).encode(), method='POST',
                         headers={'Accept': 'application/json', 'User-Agent': 'lebox/1.0'})
        try:
            with _u.urlopen(req, timeout=30) as r:
                return json.loads(r.read(16384) or b'{}')
        except urllib.error.HTTPError as ex:
            stop('GitHub 设备授权服务返回 %d' % ex.code, '请用户稍后重试，或确认 lebox 应用已启用 Device Flow')
        except (urllib.error.URLError, TimeoutError, OSError):
            stop('沙盒无法访问 github.com', '请用户检查沙盒网络')
    global DEVICE_CODE
    if DEVICE_CODE:
        # Desktop-issued pairing code: the user approves (or already approved) from the lebox popup; no code to relay.
        code, interval, expires = DEVICE_CODE, 5, 900
        print('OK: 使用 协作端预先签发的配对码，等待用户在本地协作端弹窗/GitHub 页面点 Authorize（无需转告任何代码）…', flush=True)
    else:
        start = post('/login/device/code', {'client_id': client_id})
        code, user_code, verify = start.get('device_code', ''), start.get('user_code', ''), start.get('verification_uri', '')
        interval, expires = int(start.get('interval', 5)), int(start.get('expires_in', 900))
        if not code or not user_code or verify != 'https://github.com/login/device':
            stop('GitHub 未返回有效的设备授权信息', '请用户确认 lebox 应用已启用 Device Flow（%s）' % (start.get('error_description') or start.get('error') or 'unknown'))
        print('', flush=True)
        print('ACTION REQUIRED（请把下面这两行原样转告用户）：', flush=True)
        print('  请在浏览器打开 https://github.com/login/device ，输入一次性代码  %s  ，然后点 Authorize。' % user_code, flush=True)
        print('  这是授权给「lebox」应用，只能访问仓库 %s；代码 %d 分钟内有效，我会在这里等待。' % (repo, expires // 60), flush=True)
        print('', flush=True)
    deadline = time.time() + expires
    while time.time() < deadline:
        time.sleep(interval)
        r = post('/login/oauth/access_token', {'client_id': client_id, 'device_code': code, 'grant_type': 'urn:ietf:params:oauth:grant-type:device_code'})
        err = r.get('error')
        if err == 'authorization_pending':
            continue
        if err == 'slow_down':
            interval = min(90, interval + 5); continue
        if err == 'access_denied':
            stop('用户在 GitHub 上拒绝了授权', '如需继续，请用户重新运行 join 并在 GitHub 页面点 Authorize')
        if err == 'expired_token':
            if DEVICE_CODE:
                DEVICE_CODE = ''
                print('OK: 协作端预签发的配对码已过期，改为申请新的设备码', flush=True)
                return device_flow_token(client_id, repo)
            stop('设备授权代码已过期（用户未在 %d 分钟内完成）' % (expires // 60), '重新运行 join 会生成新的代码')
        if err in ('incorrect_device_code', 'device_flow_disabled') and DEVICE_CODE:
            DEVICE_CODE = ''
            print('OK: 预签发的配对码不可用（%s），改为申请新的设备码' % err, flush=True)
            return device_flow_token(client_id, repo)
        if err:
            stop('GitHub 设备授权失败：%s' % (r.get('error_description') or err), '把这行原样告诉用户')
        tok = r.get('access_token', '')
        if tok and r.get('token_type', '').lower() == 'bearer':
            print('OK: 用户已在 GitHub 完成授权（令牌只保存在本进程内存与 git 内存凭据缓存中）', flush=True)
            return tok
    stop('等待 GitHub 授权超时', '重新运行 join 会生成新的代码')


def repo_url(repo):
    return 'https://github.com/%s.git' % repo


def git_can_read(url, bearer=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    cmd = ['git']
    if bearer:
        env = _git_auth_ready_environment(bearer, url)
    cmd += ['ls-remote', '--heads', url]
    return subprocess.run(cmd, capture_output=True, text=True, env=env).returncode == 0


def check_token_scope(token, repo):
    import urllib.request as _u
    import urllib.error as _e
    tools = os.environ.get('LEBOX_TOOLS') or (repo.split('/')[0] + '/lebox')
    def request(path):
        req = _u.Request('https://api.github.com' + path, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'lebox-agent/1.0'})
        try:
            with _u.urlopen(req, timeout=30) as response:
                return response.status, json.loads(response.read(1_000_001))
        except Exception:
            return 0, {}
    def pages(path, field):
        values = []
        for page_number in range(1, 101):
            status, payload = request(path + ('&' if '?' in path else '?') + 'per_page=100&page=' + str(page_number))
            if status != 200 or not isinstance(payload, dict):
                stop('SCOPE_UNKNOWN', '无法核对 GitHub App 授权范围；保留原状态，处理权限或限流后重试')
            items, total = payload.get(field), payload.get('total_count')
            if not isinstance(items, list) or type(total) is not int or total < 0:
                stop('SCOPE_UNKNOWN', 'GitHub 授权范围响应不完整，不能报告验证通过')
            values.extend(items)
            if len(values) == total:
                return values
            if not items or len(values) > total:
                stop('SCOPE_CHANGED', '分页期间授权范围发生变化，请重新只读核对')
        stop('SCOPE_UNKNOWN', '授权分页超出安全上限，未进行接入')
    names = set()
    for installation in pages('/user/installations', 'installations'):
        if not isinstance(installation, dict) or type(installation.get('id')) is not int:
            stop('SCOPE_UNKNOWN', '安装标识无效')
        for repository in pages('/user/installations/%s/repositories' % installation['id'], 'repositories'):
            name = repository.get('full_name') if isinstance(repository, dict) else None
            if not isinstance(name, str) or '/' not in name:
                stop('SCOPE_UNKNOWN', '仓库范围响应不完整')
            names.add(name.lower())
    allowed = {repo.lower(), tools.lower()}
    if repo.lower() not in names:
        stop('SCOPE_UNKNOWN', '未确认协作仓库在当前 GitHub App 授权范围内')
    if names - allowed:
        stop('SCOPE_TOO_BROAD', '请在 GitHub App 安装设置中仅保留协作仓库和工具仓库；程序不会自行扩大权限')
    print('OK: GitHub App 仓库授权范围已核对；仍需验证目标读写能力', flush=True)


def install_token_credential(token, repository=None):
    # Rebuild only process-local access. No global .gitconfig, helper socket, credential approve or disk secret.
    target = repository or API_REPO or REPO_HINT or remote_url()
    os.environ.update(_git_auth_ready_environment(token, target))


def ensure_repo_checkout(repo):
    """Not inside a repo: clone into ./<name> using sandbox auth first, then the pasted token. Returns (dir, auth_source)."""
    global AUTH_SOURCE
    url = repo_url(repo)
    name = repo.split('/')[1]
    global PASTED_TOKEN
    if git_can_read(url):
        AUTH_SOURCE = 'sandbox'
    elif PASTED_TOKEN and git_can_read(url, PASTED_TOKEN):
        AUTH_SOURCE = 'pasted-token'
        install_token_credential(PASTED_TOKEN, repo)
    elif CLIENT_ID:
        PASTED_TOKEN = device_flow_token(CLIENT_ID, repo)
        if not git_can_read(url, PASTED_TOKEN):
            stop('授权已完成，但该令牌读不到仓库 %s' % repo, '请用户确认 lebox 应用已安装到这个仓库（GitHub → Settings → Applications → lebox → Configure）')
        AUTH_SOURCE = 'device-flow'
        check_token_scope(PASTED_TOKEN, repo)
        install_token_credential(PASTED_TOKEN, repo)
    else:
        stop('当前入口没有可用的 GitHub 授权或设备授权配置', '请用户在本地协作端点「连接 Agent」复制最新说明；不要在聊天提供 Token')
    if pathlib.Path(name, '.git').exists():
        return pathlib.Path(name).resolve()
    r = subprocess.run(['git', 'clone', '-q', url, name], capture_output=True, text=True, env=dict(os.environ, GIT_TERMINAL_PROMPT='0'))
    if r.returncode:
        stop('git clone 失败：' + r.stderr.strip()[:200], '把这行原样告诉用户')
    print('OK: 已克隆 %s（授权来源：%s）' % (repo, {'sandbox': '沙盒已有授权', 'device-flow': '用户在 GitHub 设备授权页面批准', 'pasted-token': '安全环境或私有授权缓存'}.get(AUTH_SOURCE, '已提供的 GitHub 授权')), flush=True)
    return pathlib.Path(name).resolve()


PROJECT_ID = ""

def demand_repository(config):
    if PROJECT_ID and config.get("project_id") != PROJECT_ID:
        stop("会话属于另一项目，不能恢复到当前项目", "请使用当前项目的独立会话分支，未转移身份或重放操作")
    if API_MODE:
        target = API_REPO
    else:
        url = remote_url()
        if url.startswith('git@github.com:'):
            target = url[len('git@github.com:'):]
        else:
            from urllib.parse import urlsplit
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or parsed.hostname != 'github.com' or parsed.port not in (None, 443) or parsed.query or parsed.fragment:
                stop('origin 不是已授权的 GitHub 仓库地址', '未恢复或创建任何 Agent 身份')
            target = parsed.path.lstrip('/')
        target = target.removesuffix('.git')
    if not isinstance(config.get('repository'), str) or config['repository'].lower() != target.lower():
        stop('会话资料属于另一仓库，不能自动匹配', '保留原状态，检查当前项目仓库；未转移身份或权限')


def legacy_project_branch(repo, scope):
    """Recover a route only when its original private state proves the same project."""
    if not PROJECT_ID:
        return None
    state_home = persistent_state_home(pathlib.Path.cwd())
    temp = pathlib.Path(tempfile.gettempdir())
    name = 'lebox-route-' + hashlib.sha256(scope.encode()).hexdigest()
    branches = set()
    for directory in (state_home / name, temp / name):
        marker = directory / 'branch'
        if not marker.exists():
            continue
        branch = _state_read(marker).decode('utf-8').strip()
        if not re.fullmatch(r'agent/[0-9a-f]{32}', branch):
            stop('旧自动分支缓存损坏', '保留原资料，不生成替代分支')
        branches.add(branch)
    if not branches:
        return None
    if len(branches) != 1:
        stop('新旧分支缓存冲突', '保留两份记录，不按时间猜选')
    branch = next(iter(branches))
    target = repo.lower().removeprefix('https://github.com/').removeprefix('git@github.com:').removesuffix('.git')
    matches = set()
    configs = list(temp.glob('lebox-tools-*/connection.json')) + list(state_home.glob('scx-collaboration-*/connection.json'))
    for file in configs:
        if file.is_symlink() or file.parent.is_symlink() or file.stat().st_size > 1_200_000:
            continue
        try:
            config = json.loads(file.read_bytes(), object_pairs_hook=unique)
            if config.get('project_id') != PROJECT_ID or config.get('repository', '').lower() != target or config.get('branch') != branch:
                continue
            binding = config.get('binding', '')
            if not re.fullmatch(r'[0-9a-f]{32}', binding):
                raise ValueError()
            root = persistent_session_directory(config, pathlib.Path.cwd())
            state_file = root / 'state.json'
            if not state_file.exists():
                stop('找到旧项目会话，但原私有状态不可用', '保留原资料；未创建替代身份')
            state = _state_json(_state_read(state_file))
            if not state.get('keys') or not state.get('hello'):
                raise ValueError()
            if state.get('closed'):
                if state.get('pending'):
                    stop('原操作结果仍未确认', '保留原状态，只查询原结果')
                continue
            matches.add(binding)
        except (OSError, ValueError, KeyError, TypeError):
            stop('旧项目会话资料不完整', '保留原资料，不自动替换身份')
    if len(matches) != 1:
        stop('旧分支无法唯一核验原身份', '保留缓存和操作记录，不生成替代分支')
    return branch


def automatic_branch(repo):
    """Remember a transport branch, never credentials or an authenticated session identity."""
    import secrets
    scope = repo.lower() + '\n' + str(pathlib.Path.cwd().resolve()) + '\n' + os.environ.get('LEBOX_RECOVERY_SELF', '')
    legacy_scope = scope
    if not PROJECT_ID:
        stop('自动分支恢复需要原项目标识', '使用原项目说明或明确的原分支，不猜身份')
    legacy_project_scope = scope + '\nproject=' + PROJECT_ID
    target_repo = repo.lower().removeprefix('https://github.com/').removeprefix('git@github.com:').removesuffix('.git')
    scope = target_repo + '\nproject=' + PROJECT_ID + '\nrecovery=' + os.environ.get('LEBOX_RECOVERY_SELF', '')
    key = hashlib.sha256(scope.encode()).hexdigest()
    root = persistent_state_home(pathlib.Path.cwd()) / ('lebox-route-' + key)
    boundary = state_repo_boundary(pathlib.Path.cwd())
    if root.is_symlink() or (boundary is not None and root.resolve().is_relative_to(boundary)):
        stop('自动分支缓存路径不安全', '保留原资料，不退回临时目录')
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    marker = root / 'branch'
    if marker.is_symlink():
        stop('自动分支缓存不可用', '请检查临时目录')
    preferred = None
    if not marker.exists():
        candidates = set()
        for old_scope in (legacy_project_scope, legacy_scope):
            branch = legacy_project_branch(repo, old_scope)
            if branch:
                candidates.add(branch)
        if len(candidates) > 1:
            stop('旧分支定位存在冲突', '保留原状态，不自动选择或生成新分支')
        preferred = next(iter(candidates), None)
        if preferred is None:
            for descriptor in persistent_state_home(pathlib.Path.cwd()).glob('scx-collaboration-*/connection.json'):
                config = _state_json(_state_read(descriptor))
                if config.get('project_id') == PROJECT_ID and config.get('repository', '').lower() == target_repo:
                    stop('存在原会话但分支定位材料不匹配', '保留原身份，使用明确的原分支；未生成替代分支')
    with _state_lock(root):
        if marker.exists():
            branch = _state_read(marker).decode('utf-8').strip()
            if not re.fullmatch(r'agent/[0-9a-f]{32}', branch):
                stop('自动分支缓存损坏', '保留原状态，不自动创建替代会话')
            return branch
        branch = preferred or 'agent/' + secrets.token_hex(16)
        _state_write(marker, branch.encode('utf-8'))
        return branch


def local_resume_candidate(branch, paths, read_raw):
    """Select only by an exact descriptor hash plus private cryptographic state, not branch name."""
    matches = []
    for path in paths:
        if not BOOTSTRAP.fullmatch(path):
            continue
        binding = path.split('/session-', 1)[1].split('/')[0]
        name = 'scx-collaboration-' + binding
        candidate_roots = (persistent_state_home(pathlib.Path.cwd()) / name, pathlib.Path(tempfile.gettempdir()) / name)
        if not any((directory / 'state.json').exists() for directory in candidate_roots):
            continue
        try:
            raw = read_raw(path)
            if len(raw) > 1_200_000:
                raise ValueError()
            bundle = json.loads(raw, object_pairs_hook=unique)
            entry = bundle['files']['connection.json']
            content = entry['content'].encode('utf-8')
            if hashlib.sha256(content).hexdigest() != entry['sha256']:
                raise ValueError()
            config = json.loads(content, object_pairs_hook=unique)
            demand_repository(config)
            if config.get('binding') != binding or config.get('branch') != branch:
                raise ValueError()
            root = persistent_session_directory(config, pathlib.Path.cwd())
            state = _state_json(_state_read(root / 'state.json'))
            digest = hashlib.sha256(json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            if config.get('branch') != branch:
                continue
            demand_repository(config)
            if config.get('binding') != binding or state.get('config_hash') != digest:
                raise ValueError()
            if state.get('closed'):
                if state.get('pending'):
                    stop('旧会话已关闭但原操作仍未确认', '只检查原请求，不新建会话或重新执行')
                continue
            if not state.get('keys') or not state.get('hello'):
                stop('旧会话配对尚未完整', '保留原状态，使用原会话客户端继续核对，不重新配对')
            matches.append((path, state))
        except (OSError, ValueError, KeyError, TypeError):
            stop('旧会话状态或描述不匹配', '保留原资料，未发出新的接入请求')
    if len(matches) > 1:
        stop('发现多个可恢复会话，不能自动选择身份', '使用该 Agent 原工具目录中的客户端查询状态')
    return matches[0] if matches else None


def preissued_bootstrap(branch, paths, read_raw, exists):
    """Only an unclaimed, unclosed offer can start a new identity. Never reuse an active peer."""
    offers, active = [], []
    for path in paths:
        try:
            raw = read_raw(path)
            if len(raw) > 1_200_000:
                raise ValueError()
            bundle = json.loads(raw, object_pairs_hook=unique)
            entry = bundle['files']['connection.json']
            content = entry['content'].encode()
            if hashlib.sha256(content).hexdigest() != entry['sha256']:
                raise ValueError()
            config = json.loads(content, object_pairs_hook=unique)
            if config.get('branch') != branch:
                continue
            demand_repository(config)
            if not re.fullmatch(r'[0-9a-f]{32}', config.get('binding', '')) or not path.endswith('/session-' + config['binding'] + '/bootstrap.json'):
                raise ValueError()
            directory = path.rsplit('/', 1)[0]
            if exists(directory + '/server.closed.json'):
                continue
            if exists(directory + '/client.hello.json'):
                active.append(path)
            else:
                offers.append(path)
        except (ValueError, TypeError, KeyError):
            stop('接入资料无法验证', '未创建新身份，也未停止其他 Agent')
    if len(offers) > 1:
        stop('此分支存在多个未认领会话，无法唯一匹配', '请在桌面端核对目标会话，不自动猜测身份')
    if offers:
        return offers[0]
    if active:
        stop('此分支已有 Agent，但本沙盒缺少它的原会话私有状态', '不要等待批准或删除 pending；用原状态恢复，或在桌面端明确替换选中的 Agent，其他 Agent 不受影响')
    return None


def api_file_exists(tip, path):
    status, _ = api('GET', '/repos/%s/contents/%s?ref=%s' % (API_REPO, quote(path), tip), allow=(404,))
    return status == 200


def git_file_exists(path):
    result = subprocess.run(['git', 'ls-tree', '-z', 'FETCH_HEAD', '--', path], capture_output=True)
    if result.returncode != 0:
        stop('无法核对原会话文件', '不创建新身份，请检查仓库状态')
    return bool(result.stdout)


def resume_existing(branch, paths, tip=None):
    read_raw = (lambda p: api_blob(tip, p)) if API_MODE else (lambda p: git('show', 'FETCH_HEAD:' + p, text=False))
    match = local_resume_candidate(branch, paths, read_raw)
    if match is None:
        return False
    path, state = match
    tools = api_unpack(branch, tip, path) if API_MODE else unpack(branch, path)
    if API_MODE:
        os.environ['LEBOX_TRANSPORT'] = 'api'
        os.environ[TOKEN_ENV] = API_TOKEN
    if AUTH_SOURCE in ('pasted-token', 'device-flow'):
        os.environ['LEBOX_AUTH_SOURCE'] = 'pasted-token'
    ensure_dependency(tools)
    print('RESUMING: 使用原会话身份；不推送新接入请求，不重发业务操作。', flush=True)
    if state.get('initialized') and not state.get('pending'):
        tools = upgrade_client(tools / 'connection.json')
        if run_agent(tools, '--repo', os.getcwd(), 'ensure-active', '--wait', '180') != 0:
            stop('原身份尚未取得当前在线槽位', '保留原身份与控制请求，不提交业务或自动换身份')
    command = ['status', '--wait', '0'] if state.get('pending') or state.get('initialized') else ['activate', '--wait', '900']
    if run_agent(tools, '--repo', os.getcwd(), *command) != 0:
        stop('原会话尚未恢复（见上方状态）', '保留原状态，不重发或重新配对')
    print_remote_handoff(tools, api_mode=API_MODE)
    return True


def doctor(verbose=True):
    global API_MODE, API_TOKEN, API_REPO, AUTH_SOURCE, PASTED_TOKEN
    ok = []
    if sys.version_info < (3, 10):
        stop('Python 版本低于 3.10（当前 %d.%d）' % sys.version_info[:2], '请使用 python3.10 或更高版本运行')
    if shutil.which('git') is None or os.environ.get('LEBOX_TRANSPORT', '').lower() == 'api':
        # No git binary: fall back to the GitHub REST API with a token from the environment.
        API_REPO = (API_REPO or REPO_HINT).strip()
        API_TOKEN = os.environ.get(TOKEN_ENV, '').strip() or PASTED_TOKEN
        AUTH_SOURCE = 'sandbox' if os.environ.get(TOKEN_ENV, '').strip() else 'pasted-token'
        if not API_TOKEN and CLIENT_ID and API_REPO:
            API_TOKEN = device_flow_token(CLIENT_ID, API_REPO)
            AUTH_SOURCE = 'device-flow'
            check_token_scope(API_TOKEN, API_REPO)
        if not API_TOKEN:
            stop('未找到 git，也没有可用的 GitHub 授权或设备授权配置',
                 '请用户在本地协作端点「连接 Agent」重新复制最新说明')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+', API_REPO):
            stop('API 模式需要指定仓库', '加参数 --repo owner/name（例如 --repo xiaomailele/lele）或设置环境变量 LEBOX_REPO')
        API_MODE = True
        ok.append('python %d.%d · 无 git，使用 GitHub API 模式' % sys.version_info[:2])
        st, info = api('GET', '/repos/' + API_REPO)
        if not info.get('private'):
            stop('仓库 %s 不是私有仓库' % API_REPO, '协作只允许私有仓库')
        ok.append('repo ' + API_REPO + ' 可读（令牌有效）')
        branch = BRANCH_OVERRIDE
        if not branch or branch == 'agent/auto':
            branch = automatic_branch(API_REPO)
        if branch.lower() in ('main', 'master') or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}', branch) or '..' in branch:
            stop('分支 %r 不能用作会话分支' % branch, '换一个不是 main/master 的专用分支名')
        ok.append('branch ' + branch + '（自定义专用分支）')
        if verbose:
            for line in ok:
                print('OK: ' + line, flush=True)
        return branch
    ok.append('git / python %d.%d' % sys.version_info[:2])
    if subprocess.run(['git', 'rev-parse', '--is-inside-work-tree'], capture_output=True, text=True).stdout.strip() != 'true':
        target = (API_REPO or REPO_HINT).strip()
        if not target:
            stop('当前目录不是 git 仓库', '先 git clone 该仓库并 cd 进去，或加 --repo owner/name 让脚本自动克隆')
        os.chdir(ensure_repo_checkout(target))
    else:
        # Inside a repo: decide the auth source once, the same fixed order (sandbox first, pasted token second).
        url = remote_url()
        if git_can_read(url):
            AUTH_SOURCE = 'sandbox'
        elif PASTED_TOKEN and git_can_read(url, PASTED_TOKEN):
            AUTH_SOURCE = 'pasted-token'
            install_token_credential(PASTED_TOKEN)
        elif CLIENT_ID:
            PASTED_TOKEN = device_flow_token(CLIENT_ID, (API_REPO or REPO_HINT or url))
            AUTH_SOURCE = 'device-flow'
            install_token_credential(PASTED_TOKEN)
    url = remote_url()
    if not url and (API_REPO or REPO_HINT):
        subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/%s.git' % (API_REPO or REPO_HINT)], capture_output=True)
        url = remote_url()
        if url:
            print('OK: 已补回 origin（沙盒恢复时 .git/config 被丢弃）', flush=True)
    if not url:
        stop('仓库没有 origin 远端', '请在 git clone 得到的目录里运行，或加 --repo owner/name')
    ok.append('repo ' + re.sub(r'://[^@/]+@', '://', url))
    r = subprocess.run(['git', 'ls-remote', '--heads', 'origin'], capture_output=True, text=True)
    if r.returncode:
        err = r.stderr.lower()
        if 'authentication' in err or 'could not read username' in err or '403' in err or 'permission' in err:
            stop('你的 GitHub 连接器没有该私有仓库的读写权限', '请用户在 Arena 设置里重新授权 GitHub 并勾选此仓库，然后重试')
        if 'could not resolve host' in err or 'network' in err or 'timed out' in err:
            stop('沙盒无法访问 github.com', '请用户检查沙盒网络后重试')
        stop('git ls-remote 失败：' + r.stderr.strip()[:200], '把这行原样告诉用户')
    ok.append('GitHub 可读')
    branch = BRANCH_OVERRIDE or git('symbolic-ref', '--short', '-q', 'HEAD', check=False).strip()
    if AUTH_SOURCE == 'sandbox' and BRANCH_OVERRIDE == 'agent/auto':
        stop('平台固定分支不能改成自动分支', '沿用平台既有工作分支，未运行 checkout')
    if BRANCH_OVERRIDE == 'agent/auto' or (not BRANCH_OVERRIDE and (not branch or branch.lower() in ('main', 'master')) and AUTH_SOURCE == 'pasted-token'):
        # Platforms without a conversation branch: mint a unique one. Arena keeps its own arena/* branch untouched.
        branch = branch if branch.startswith('agent/') and branch != 'agent/auto' else automatic_branch(url)
        r = subprocess.run(['git', 'checkout', '-q', '-B', branch], capture_output=True, text=True)
        if r.returncode:
            stop('无法创建分支 %s：%s' % (branch, r.stderr.strip()[:160]), '请先提交或暂存本地改动')
    if not branch:
        stop('当前处于分离 HEAD 或分支尚未创建', '请在会话的工作分支上运行，或用 --branch <名字> 指定一个专用分支')
    if branch.lower() in ('main', 'master') or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}', branch) or '..' in branch:
        stop('当前分支 %r 不能用作会话分支（不能是 main/master）' % branch, 'Arena 请在对话默认分支上运行；其他平台用 --branch agent/<平台>-<日期> 指定一个专用分支')
    if BRANCH_OVERRIDE and BRANCH_OVERRIDE != 'agent/auto':
        current = git('symbolic-ref', '--short', '-q', 'HEAD', check=False).strip()
        if current != branch:
            if AUTH_SOURCE == 'sandbox':
                stop('平台固定分支与指定分支不一致', '保留平台工作分支，不自动切换或重建')
            r = subprocess.run(['git', 'checkout', '-q', '-B', branch], capture_output=True, text=True)
            if r.returncode:
                stop('无法切换到分支 %s：%s' % (branch, r.stderr.strip()[:160]), '请先提交或暂存本地改动')
    ok.append('branch ' + branch + ('（Arena 会话分支）' if branch.startswith('arena/') else '（自定义专用分支，本地协作端以 lebox: join 提交为准）'))
    try:
        import cryptography  # noqa: F401
        ok.append('cryptography 已安装')
    except ImportError:
        ok.append('cryptography 未安装（join 时自动 pip install）')
    if verbose:
        for line in ok:
            print('OK: ' + line, flush=True)
    return branch


def bootstrap_files(ref):
    names = git('ls-tree', '-r', '--name-only', ref).split('\n')
    return [n for n in names if BOOTSTRAP.fullmatch(n)]


def added_at(ref, name):
    out = git('log', '-1', '--format=%ct', '--diff-filter=A', ref, '--', name).strip()
    return int(out or 0)


def fetch_branch(branch, missing_ok=False):
    r = subprocess.run(['git', 'fetch', '--no-tags', 'origin', branch], capture_output=True, text=True)
    if r.returncode:
        if missing_ok and "couldn't find remote ref" in r.stderr:
            return False  # Brand-new conversation: the branch is created by our own push below.
        stop('git fetch 失败：' + r.stderr.strip()[:200], '把这行原样告诉用户')
    return True


def push_join(branch, remote_exists):
    if remote_exists:
        # The desktop appends message commits to this branch; bring the local branch up to date before adding the join commit.
        r = subprocess.run(['git', 'merge', '--ff-only', 'FETCH_HEAD'], capture_output=True, text=True)
        if r.returncode:
            r = subprocess.run(['git', 'rebase', 'FETCH_HEAD'], capture_output=True, text=True)
            if r.returncode:
                subprocess.run(['git', 'rebase', '--abort'], capture_output=True)
                stop('本地分支与远端分叉且无法自动变基', '让 Agent 处理本地未推送的提交后重新运行 join')
    try:
        git('commit', '--allow-empty', '-m', JOIN_SUBJECT)
    except RuntimeError as ex:
        if 'please tell me who you are' in str(ex).lower() or 'author identity unknown' in str(ex).lower():
            git('-c', 'user.name=arena-agent', '-c', 'user.email=arena-agent@users.noreply.github.com', 'commit', '--allow-empty', '-m', JOIN_SUBJECT)
        else:
            stop('无法创建接入提交：' + str(ex)[:200], '把这行原样告诉用户')
    r = subprocess.run(['git', 'push', '-u', 'origin', 'HEAD'], capture_output=True, text=True)
    if r.returncode:
        err = r.stderr.lower()
        if 'protected' in err or 'permission' in err or '403' in err:
            stop('推送被拒绝（无写权限或分支受保护）', '请用户确认 GitHub 授权包含此仓库的写权限')
        if 'rejected' in err and ('fetch first' in err or 'non-fast-forward' in err):
            stop('远端分支比本地新，推送被拒绝', '让 Agent 先 git pull --rebase origin ' + branch + ' 再重新运行 join')
        stop('git push 失败：' + r.stderr.strip()[:200], '把这行原样告诉用户')
    print('OK: 已推送接入请求 (%s) 到 origin/%s' % (JOIN_SUBJECT, branch), flush=True)


def wait_bootstrap(branch, ignore, since):
    deadline = time.time() + WAIT_SECONDS
    delay, last_hint = 3, 0
    while time.time() < deadline:
        fetch_branch(branch)
        current = bootstrap_files('FETCH_HEAD')
        fresh = [n for n in current if n not in ignore and added_at('FETCH_HEAD', n) >= since - 120]
        if fresh:
            return max(fresh, key=lambda n: added_at('FETCH_HEAD', n))
        waited = int(time.time() - (deadline - WAIT_SECONDS))
        hint = ''
        if waited - last_hint >= 180:
            hint = '  ← 若本机没有反应：请用户在本地协作端 点 GitHub 徽章 →「连接 Agent」'
            last_hint = waited
        print('waiting for desktop session bootstrap... %ds%s' % (waited, hint), flush=True)
        time.sleep(delay)
        delay = min(delay + 2, 15)
    stop('15 分钟内用户本地的协作端 没有发布接入资料', '请用户在电脑端 lebox 点 GitHub 徽章 →「连接 Agent」，然后让 Agent 重新运行 join')


def unique(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            stop('引导文件包含重复键，已拒绝', '请用户在本机重新连接')
        d[k] = v
    return d


def unpack(branch, path):
    raw = git('show', 'FETCH_HEAD:' + path, text=False)
    digest = hashlib.sha256(raw).hexdigest()
    try:
        b = json.loads(raw, object_pairs_hook=unique)
        assert b['format'] in ('scx-bootstrap-v1', 'lebox-bootstrap-v1') and set(b) == {'format', 'files'}
        assert set(b['files']) == {'agent_collaboration.py', 'connection.json', 'requirements.txt'}
    except Exception:
        stop('引导文件格式不正确，已拒绝', '请用户在本机重新连接')
    out = pathlib.Path(tempfile.mkdtemp(prefix='lebox-tools-'))
    for name, f in b['files'].items():
        data = f['content'].encode('utf-8')
        if set(f) != {'content', 'sha256'} or hashlib.sha256(data).hexdigest() != f['sha256']:
            stop('引导文件中 %s 的校验值不匹配，已拒绝' % name, '请用户在本机重新连接')
        with open(out / name, 'xb') as fh:
            fh.write(data)
    c = json.loads((out / 'connection.json').read_bytes())
    if c.get('branch') != branch or not path.endswith('/session-%s/bootstrap.json' % c.get('binding')):
        stop('引导文件属于另一条分支或会话，已拒绝', '请用户在本机重新连接')
    demand_repository(c)
    print('OK: 引导文件已校验 (sha256=%s) → TOOLS_DIR=%s' % (digest[:16], out), flush=True)
    print('OK: repository=%s branch=%s binding=%s' % (c.get('repository'), c.get('branch'), c.get('binding')), flush=True)
    return out


PYTHON = sys.executable  # Interpreter that has cryptography; may become a venv python below.


def has_cryptography(python):
    # A saved venv/interpreter can disappear with the sandbox runtime layer.
    probe = ('import cryptography; '
             'from cryptography.hazmat.primitives.ciphers.aead import AESGCM; '
             'assert int(cryptography.__version__.split(".", 1)[0]) >= 42')
    try:
        return subprocess.run([python, '-c', probe], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_dependency(tools):
    """Rebuild disposable dependencies only; never repair/copy an identity or change system Python."""
    global PYTHON
    if has_cryptography(PYTHON):
        return
    if PYTHON != sys.executable and has_cryptography(sys.executable):
        PYTHON = sys.executable
        return
    # Both current bootstrap producers emit this requirement. Do not run arbitrary pip options,
    # URLs or extra packages from a changed/missing tool directory after snapshot recovery.
    try:
        requirement = _state_no_links(pathlib.Path(tools) / 'requirements.txt')
        info = requirement.stat()
        if not requirement.is_file() or info.st_nlink != 1 or info.st_size > 4096:
            raise ValueError()
        raw = requirement.read_bytes()
        if raw.strip() != b'cryptography>=42':
            raise ValueError()
    except (OSError, ValueError):
        stop('DEPENDENCY_REQUIREMENT_UNVERIFIED', '保留原身份；使用已校验工具目录，不手改依赖或客户端 pin')
    try:
        # A unique owner-private directory, not the shared historical /tmp/lebox-venv.
        runtime = _state_no_links(pathlib.Path(tempfile.mkdtemp(prefix='lebox-runtime-')))
        if state_repo_boundary(runtime) is not None:
            raise ValueError()
        if os.name != 'nt' and runtime.stat().st_uid != os.getuid():
            raise ValueError()
        os.chmod(runtime, 0o700)
        req = runtime / 'requirements.txt'
        _state_write(req, raw)  # pin the checked bytes for this attempt; do not follow a later replacement
        venv = runtime / 'venv'
        result = subprocess.run([sys.executable, '-m', 'venv', str(venv)],
                                capture_output=True, timeout=120)
        if result.returncode != 0:
            stop('DEPENDENCY_RUNTIME_UNAVAILABLE', '平台未能创建独立 Python 环境；未尝试修改系统依赖')
        vpy = venv / ('Scripts' if os.name == 'nt' else 'bin') / ('python.exe' if os.name == 'nt' else 'python')
        result = subprocess.run([str(vpy), '-m', 'pip', '--isolated', '--disable-pip-version-check',
                                 'install', '--no-input', '-q', '-r', str(req)],
                                capture_output=True, timeout=120)
        if result.returncode != 0 or not has_cryptography(str(vpy)):
            stop('DEPENDENCY_INSTALL_UNCONFIRMED', '独立环境依赖未确认；检查平台安装权限与网络，不重建身份或重发业务')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # Raw pip/Git diagnostics may contain credential-bearing mirror URLs; never echo them.
        stop('DEPENDENCY_RUNTIME_UNAVAILABLE', '独立依赖恢复中断；保留原身份与 pending，不改用系统 pip')
    PYTHON = str(vpy)
    print('OK: 依赖已在独立临时环境确认；运行时可重建，原身份与 pending 未改动。', flush=True)



def register_recovery(tools, repo_dir_args):
    """After pairing: seal this Agent's GitHub token with a fresh random key K, hand the opaque blob to the owner's desktop
    (which republishes it to the public tools repo as recovery/<rid>.bin), and print K once for the chat. Only when the token
    came from a device flow / pasted token (sandbox-provided auth needs no recovery)."""
    if os.environ.get('LEBOX_SECRET_FREE') == '1':
        return  # Private credential cache replaces chat recovery bearers; no registration business request.
    token = os.environ.get(TOKEN_ENV, '') or PASTED_TOKEN
    if AUTH_SOURCE not in ('device-flow', 'pasted-token') or not token or os.environ.get('LEBOX_RECOVERY_DONE') == '1':
        return  # boot.py already published the recovery line right after authorization
    try:
        import types
        path = _state_no_links(pathlib.Path(__file__).absolute().with_name('lebox_recovery.py'))
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != '00ead80d07cdc9bc385d6932d7dc930fab552f6c2760869f7dfb5238dcb473ba':
            raise ValueError('RECOVERY_MODULE_PIN_MISMATCH')
        lr = types.ModuleType('verified_lebox_recovery')
        exec(compile(raw, str(path), 'exec'), lr.__dict__)
        import secrets as _s, base64 as _b, json as _j
        rid = _s.token_hex(16); key = lr.new_key()
        payload = _j.dumps({'v': 1, 'repo': API_REPO or REPO_HINT or '', 'tools': tools_repo_hint(tools), 'token': token, 'issued': int(time.time()), 'by': 'agent'}).encode()
        env = _b.b64encode(lr.seal(key, payload)).decode()
        r = subprocess.run([PYTHON, str(tools / 'agent_collaboration.py'), 'call', 'lebox_recovery_register', '--arguments', _j.dumps({'rid': rid, 'envelope': env}), '--wait', '60', *repo_dir_args], capture_output=True, text=True)
        if r.returncode != 0 or '"error"' in r.stdout:
            print('NOTE: 恢复钥匙未能登记（本机未启用或写入失败）；沙盒回收后需重新授权。', flush=True)
            return
        print('', flush=True)
        print('RECOVERY（请把下面这一整行原样保留在对话里；沙盒被回收后，把它作为 --recover 参数重新运行 boot.py 即可免授权恢复）：', flush=True)
        print('LEBOX_RECOVERY=' + lr.format_recovery(rid, key), flush=True)
        print('', flush=True)
    except Exception as ex:
        print('NOTE: 恢复钥匙登记未确认；保留原身份与未决操作，不加载未校验模块。', flush=True)


def tools_repo_hint(tools):
    return os.environ.get('LEBOX_TOOLS', '')


def run_agent(tools, *args, timeout=None):
    r = subprocess.run([PYTHON, str(tools / 'agent_collaboration.py'), *args], text=True, timeout=timeout)
    return r.returncode


def api_wait_bootstrap(branch, ignore, since):
    deadline = time.time() + WAIT_SECONDS
    delay, last_hint = 3, 0
    while time.time() < deadline:
        tip = api_branch_tip(branch)
        current = api_bootstrap_files(tip) if tip else []
        fresh = [n for n in current if n not in ignore]
        if fresh:
            return tip, max(fresh, key=lambda n: api_added_at(tip, n))
        waited = int(time.time() - (deadline - WAIT_SECONDS))
        hint = ''
        if waited - last_hint >= 180:
            hint = '  ← 若本机没有反应：请用户在本地协作端 点 GitHub 徽章 →「连接 Agent」'
            last_hint = waited
        print('waiting for desktop session bootstrap... %ds%s' % (waited, hint), flush=True)
        time.sleep(delay)
        delay = min(delay + 2, 15)
    stop('15 分钟内用户本地的协作端 没有发布接入资料', '请用户在电脑端 lebox 点 GitHub 徽章 →「连接 Agent」，然后让 Agent 重新运行 join')


def api_unpack(branch, tip, path):
    raw = api_blob(tip, path)
    digest = hashlib.sha256(raw).hexdigest()
    try:
        b = json.loads(raw, object_pairs_hook=unique)
        assert b['format'] in ('scx-bootstrap-v1', 'lebox-bootstrap-v1') and set(b) == {'format', 'files'}
        assert set(b['files']) == {'agent_collaboration.py', 'connection.json', 'requirements.txt'}
    except Exception:
        stop('引导文件格式不正确，已拒绝', '请用户在本机重新连接')
    out = pathlib.Path(tempfile.mkdtemp(prefix='lebox-tools-'))
    for name, f in b['files'].items():
        data = f['content'].encode('utf-8')
        if set(f) != {'content', 'sha256'} or hashlib.sha256(data).hexdigest() != f['sha256']:
            stop('引导文件中 %s 的校验值不匹配，已拒绝' % name, '请用户在本机重新连接')
        with open(out / name, 'xb') as fh:
            fh.write(data)
    c = json.loads((out / 'connection.json').read_bytes())
    if c.get('branch') != branch or not path.endswith('/session-%s/bootstrap.json' % c.get('binding')):
        stop('引导文件属于另一条分支或会话，已拒绝', '请用户在本机重新连接')
    demand_repository(c)
    print('OK: 引导文件已校验 (sha256=%s) → TOOLS_DIR=%s' % (digest[:16], out), flush=True)
    print('OK: repository=%s branch=%s binding=%s' % (c.get('repository'), c.get('branch'), c.get('binding')), flush=True)
    return out


def remote_client_argv(tools, *args):
    # --repo is a global argparse option: it must precede list/call/status.
    # This identifies the LOCAL transport worktree, never the desktop project.
    return [PYTHON, str(tools / 'agent_collaboration.py'), '--repo', os.getcwd(), *args]


def print_remote_handoff(tools, api_mode=False):
    """Print actionable next steps only; never claim an unperformed remote read succeeded."""
    prefix = 'LEBOX_TRANSPORT=api ' if api_mode else ''
    def command(*args):
        return prefix + shlex.join(remote_client_argv(tools, *args))
    print('NEXT_REQUIRED: 会话建立，远端确认未完成。不要在 READY 处结束任务。', flush=True)
    print('LEBOX_CLIENT=' + json.dumps({'python': PYTHON, 'script': str(tools / 'agent_collaboration.py'),
          'local_transport_repository': os.getcwd(), 'transport': 'api' if api_mode else 'git',
          'remote_project_verified': False}, ensure_ascii=False), flush=True)
    print('以下示例适用于 POSIX shell；其他 shell 请保持参数顺序并按该 shell 引用路径。', flush=True)
    if api_mode:
        print('API 模式需保留已有授权环境变量 %s；不要打印或粘贴令牌。' % TOKEN_ENV, flush=True)
    print('新用户任务入口：有原业务 pending 时只核对原结果；否则先执行下面的身份激活命令，再提交新业务。', flush=True)
    print('  ' + command('ensure-active', '--wait', '180'), flush=True)
    print('status、心跳和后台轮询不得运行此入口；收到拒绝或被另一身份替换后，不自动重新 claim。', flush=True)
    print('1. 获取远端工具清单（不是列出本地文件）：', flush=True)
    print('  ' + command('list'), flush=True)
    print('2. 按初始化返回的项目指引恢复上下文；仅当清单包含 workspace_status 且 schema 支持 detail 时运行：', flush=True)
    print('  ' + command('call', 'workspace_status', '--arguments', '{"detail":"lite"}'), flush=True)
    print('检查远端回包中的项目名称和真实路径并确认目标一致，才报告“远端项目已确认”。工具缺失/权限失败就报告，不猜工具名。', flush=True)
    print('3. 用户的项目目录、文件和命令操作默认走清单中的远端工具；本地 Bash 仅启动此客户端。', flush=True)
    print('本地 pwd/ls/git ls-files 只反映 Agent 沙盒，不是 桌面端的远端项目；不得静默回退或冒充远端结果。用户明确要求本地操作时须标注“本地沙盒”。', flush=True)
    print('工具结果不明时只继续等待，不重复业务请求、不因普通工具错误重复 join：', flush=True)
    print('  ' + command('status', '--wait', '300'), flush=True)
    print('单次 apply_patch 控制在 60 KB 以内；目录工具的名称、参数和授权要求以真实清单及服务端指引为准。', flush=True)


def join():
    branch = doctor(verbose=True)
    if API_MODE:
        tip = api_branch_tip(branch)
        ignore = api_bootstrap_files(tip) if tip else []
        if resume_existing(branch, ignore, tip):
            return
        path = preissued_bootstrap(branch, ignore, lambda p: api_blob(tip, p), lambda p: api_file_exists(tip, p))
        if path is None:
            api_push_join(branch)
            tip, path = api_wait_bootstrap(branch, ignore, int(time.time()))
        tools = api_unpack(branch, tip, path)
        os.environ['LEBOX_TRANSPORT'] = 'api'  # agent_collaboration.py inherits: no git needed
        os.environ[TOKEN_ENV] = API_TOKEN
        if AUTH_SOURCE in ('pasted-token', 'device-flow'):
            os.environ['LEBOX_AUTH_SOURCE'] = 'pasted-token'
        ensure_dependency(tools)
        print('--- connect ---', flush=True)
        if run_agent(tools, '--repo', os.getcwd(), 'connect') != 0:
            stop('配对连接失败（见上方输出）', '把上方 stopped/not_confirmed 那行告诉用户')
        print('--- activate (waiting for local confirmation, up to 15 min) ---', flush=True)
        if run_agent(tools, '--repo', os.getcwd(), 'activate', '--wait', '900') != 0:
            stop('激活未完成（见上方输出）', '请用户在 本地协作端核对后按会话客户端指引检查状态；不要重复业务请求')
        os.environ[TOKEN_ENV] = API_TOKEN  # child processes (agent_collaboration.py) read it from here; never written to disk
        print('READY: 会话已建立（API 模式，GitHub 授权来源：%s；后续调用需保持环境变量 %s）。接下来：' % ({'sandbox': '沙盒环境变量', 'device-flow': '用户刚在 GitHub 设备授权页面批准', 'pasted-token': '安全环境或私有授权缓存'}.get(AUTH_SOURCE, AUTH_SOURCE), TOKEN_ENV), flush=True)
        print_remote_handoff(tools, api_mode=True)
        print('  若沙盒被回收或收到 session_closed_by_desktop：重新运行 join（同样参数）。', flush=True)
        return
    remote_exists = fetch_branch(branch, missing_ok=True)
    ignore = bootstrap_files('FETCH_HEAD') if remote_exists else []
    if resume_existing(branch, ignore):
        return
    path = preissued_bootstrap(branch, ignore, lambda p: git('show', 'FETCH_HEAD:' + p, text=False), git_file_exists)
    if path is None:
        since = int(time.time())
        push_join(branch, remote_exists)
        path = wait_bootstrap(branch, ignore, since)
    tools = unpack(branch, path)
    ensure_dependency(tools)
    if AUTH_SOURCE in ('pasted-token', 'device-flow'):
        os.environ['LEBOX_AUTH_SOURCE'] = 'pasted-token'  # agent_collaboration.py refreshes the git credential cache on token-renew
    print('--- connect ---', flush=True)
    if run_agent(tools, 'connect') != 0:
        stop('配对连接失败（见上方输出）', '把上方 stopped/not_confirmed 那行告诉用户')
    print('--- activate (waiting for local confirmation, up to 15 min) ---', flush=True)
    if run_agent(tools, 'activate', '--wait', '900') != 0:
        stop('激活未完成（见上方输出）', '通常是本机未确认配对；请用户在本地协作端核对后让 Agent 运行 %s %s/agent_collaboration.py activate --wait 900' % (PYTHON, tools))
    register_recovery(tools, [])
    print('READY: 会话已建立（GitHub 授权来源：%s）。接下来（注意用这个解释器：%s）：' % ({'sandbox': '沙盒已有授权', 'device-flow': '用户刚在 GitHub 设备授权页面批准（令牌仅在内存）', 'pasted-token': '安全环境或私有授权缓存，已放入 git 内存凭据缓存'}.get(AUTH_SOURCE, AUTH_SOURCE), PYTHON), flush=True)
    print_remote_handoff(tools)
    print('  若沙盒被回收：使用同一项目入口与原私有状态恢复。安全凭据通过平台设置提供，切勿把恢复钥匙或 Token 放入聊天。', flush=True)


# Filled only by the desktop's immutable-release builder. A raw source checkout fails closed.
EMBEDDED_CLIENT_B64 = 'IyEvdXNyL2Jpbi9lbnYgcHl0aG9uMwoiIiJQcm9qZWN0IGNvbGxhYm9yYXRpb24gY2xpZW50LiBVc2VzIGV4aXN0aW5nLCBleHBsaWNpdGx5IGF1dGhvcml6ZWQgR2l0IGFjY2VzcyBvbmx5LgpQcml2YXRlIHN0YXRlIHN0YXlzIG91dHNpZGUgdGhlIHJlcG9zaXRvcnkuIE5ldmVyIHN3aXRjaGVzIGJyYW5jaGVzLCBvdmVyd3JpdGVzIGEgZmlsZSwKZm9yY2VzIGEgcHVzaCwgY3JlYXRlcyBhIFBSLCBpbmhlcml0cyBhbm90aGVyIGNoYXQgaWRlbnRpdHkgb3IgYXBwcm92ZXMgcHJvamVjdCBhY2Nlc3MuCiIiIgppbXBvcnQgYXJncGFyc2UKaW1wb3J0IGJhc2U2NAppbXBvcnQgY29udGV4dGxpYgppbXBvcnQgaGFzaGxpYgppbXBvcnQgaG1hYwppbXBvcnQganNvbgppbXBvcnQgb3MKZnJvbSBwYXRobGliIGltcG9ydCBQYXRoCmltcG9ydCByZQppbXBvcnQgc2VjcmV0cwppbXBvcnQgc2h1dGlsCmltcG9ydCBzdWJwcm9jZXNzCmltcG9ydCBzeXMKaW1wb3J0IHRlbXBmaWxlCmltcG9ydCB0aW1lCmltcG9ydCB6bGliCmZyb20gdXJsbGliLnBhcnNlIGltcG9ydCB1cmxzcGxpdCwgcXVvdGUKaW1wb3J0IHVybGxpYi5yZXF1ZXN0CmltcG9ydCB1cmxsaWIuZXJyb3IKCiMgQkVHSU4gTEVCT1ggUEVSU0lTVEVOVCBTVEFURSBMQVlPVVQgVjEKIyBTZWxmLWNvbnRhaW5lZCwgaWRlbnRpY2FsIGluIGJvdGggdHJ1c3RlZCBlbnRyeSBwb2ludHM7IG5vIHVudmVyaWZpZWQgcnVudGltZSBpbXBvcnQuCmltcG9ydCBjb250ZXh0bGliIGFzIF9zdGF0ZV9jb250ZXh0bGliCmltcG9ydCBwYXRobGliIGFzIF9zdGF0ZV9wYXRobGliCgpjbGFzcyBTdGF0ZUxvY2F0aW9uRXJyb3IoVmFsdWVFcnJvcik6CiAgICBwYXNzCgpfU1RBVEVfRVhDTFVERUQgPSBmcm96ZW5zZXQoKCcuZ2l0JywgJy5naXQtY3JlZGVudGlhbHMnLCAnLm5ldHJjJywgJy5hcmVuYScsICcuY2FjaGUnLCAnLmxvY2FsJywgJy52ZW52JywgJy5ucG0nLCAnLm5leHQnLAogICAgJy5udXh0JywgJy5vdXRwdXQnLCAnLnBhcmNlbC1jYWNoZScsICcucHl0ZXN0X2NhY2hlJywgJy5ydWZmX2NhY2hlJywgJy5zdmVsdGUta2l0JywKICAgICcudG94JywgJy50dXJibycsICcudml0ZScsICcubXlweV9jYWNoZScsICcubm94JywgJ19fcHljYWNoZV9fJywgJ25vZGVfbW9kdWxlcycsCiAgICAnYnVpbGQnLCAnY292ZXJhZ2UnLCAnZGlzdCcsICdvdXQnLCAndGFyZ2V0JykpCgpkZWYgX3N0YXRlX25vX2xpbmtzKHBhdGgpOgogICAgcGF0aCA9IF9zdGF0ZV9wYXRobGliLlBhdGgob3MucGF0aC5hYnNwYXRoKHN0cihwYXRoKSkpCiAgICBmb3IgaXRlbSBpbiAocGF0aCwgKnBhdGgucGFyZW50cyk6CiAgICAgICAgaWYgaXRlbS5pc19zeW1saW5rKCk6CiAgICAgICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignU1RBVEVfUEFUSF9MSU5LOiBwcmVzZXJ2ZSBvcmlnaW5hbCBzdGF0ZTsgZG8gbm90IGZvbGxvdyBsaW5rcycpCiAgICByZXR1cm4gcGF0aAoKZGVmIHN0YXRlX3JlcG9fYm91bmRhcnkocGF0aCk6CiAgICBpZiBwYXRoIGlzIE5vbmU6CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIHBhdGggPSBfc3RhdGVfbm9fbGlua3MocGF0aCkKICAgIGZvciBpdGVtIGluIChwYXRoLCAqcGF0aC5wYXJlbnRzKToKICAgICAgICBpZiAoaXRlbSAvICcuZ2l0JykuZXhpc3RzKCk6CiAgICAgICAgICAgIHJldHVybiBpdGVtLnJlc29sdmUoKQogICAgcmV0dXJuIE5vbmUgICMgQVBJIHB1YmxpY2F0aW9uIGRvZXMgbm90IG1ha2UgYWxsIG9mIEhPTUUgYSBHaXQgd29ya3RyZWUuCgpkZWYgcGVyc2lzdGVudF9zdGF0ZV9ob21lKHJlcG89Tm9uZSk6CiAgICBob21lID0gX3N0YXRlX25vX2xpbmtzKF9zdGF0ZV9wYXRobGliLlBhdGguaG9tZSgpKS5yZXNvbHZlKCkKICAgIHZhbHVlID0gb3MuZW52aXJvbi5nZXQoJ0xFQk9YX1NUQVRFX0hPTUUnKSBvciBvcy5lbnZpcm9uLmdldCgnU0NYX1NUQVRFX0hPTUUnKQogICAgcm9vdCA9IF9zdGF0ZV9wYXRobGliLlBhdGgodmFsdWUpLmV4cGFuZHVzZXIoKSBpZiB2YWx1ZSBlbHNlIGhvbWUgLyAnLmxlYm94LXN0YXRlJwogICAgaWYgbm90IHJvb3QuaXNfYWJzb2x1dGUoKToKICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0hPTUVfQUJTT0xVVEVfUkVRVUlSRUQnKQogICAgcm9vdCA9IF9zdGF0ZV9ub19saW5rcyhyb290KQogICAgaWYgcm9vdCA9PSBob21lIG9yIG5vdCByb290LmlzX3JlbGF0aXZlX3RvKGhvbWUpIG9yIGFueShwIGluIF9TVEFURV9FWENMVURFRCBmb3IgcCBpbiByb290LnJlbGF0aXZlX3RvKGhvbWUpLnBhcnRzKToKICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0hPTUVfTk9UX1BFUlNJU1RFTlQ6IGNob29zZSBhIHByaXZhdGUsIG5vbi1leGNsdWRlZCBkaXJlY3RvcnkgaW5zaWRlIEhPTUUnKQogICAgYm91bmRhcnkgPSBzdGF0ZV9yZXBvX2JvdW5kYXJ5KHJlcG8pCiAgICBhY3R1YWxfYm91bmRhcnkgPSBzdGF0ZV9yZXBvX2JvdW5kYXJ5KHJvb3QpCiAgICBpZiAoYm91bmRhcnkgaXMgbm90IE5vbmUgYW5kIHJvb3QuaXNfcmVsYXRpdmVfdG8oYm91bmRhcnkpKSBvciBhY3R1YWxfYm91bmRhcnkgaXMgbm90IE5vbmU6CiAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdTVEFURV9IT01FX0lOX1JFUE9TSVRPUlk6IGRvIG5vdCBwdWJsaXNoIHByaXZhdGUgc3RhdGUnKQogICAgcm9vdC5ta2Rpcihtb2RlPTBvNzAwLCBwYXJlbnRzPVRydWUsIGV4aXN0X29rPVRydWUpCiAgICBfc3RhdGVfbm9fbGlua3Mocm9vdCkKICAgIGlmIG9zLm5hbWUgIT0gJ250JyBhbmQgcm9vdC5zdGF0KCkuc3RfdWlkICE9IG9zLmdldHVpZCgpOgogICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignU1RBVEVfSE9NRV9PV05FUl9NSVNNQVRDSCcpCiAgICBvcy5jaG1vZChyb290LCAwbzcwMCkKICAgIHJldHVybiByb290CgpkZWYgX3N0YXRlX2pzb24ocmF3KToKICAgIGlmIGxlbihyYXcpID4gMV8yMDBfMDAwOgogICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignU1RBVEVfU0laRV9MSU1JVCcpCiAgICBkZWYgdW5pcXVlKHBhaXJzKToKICAgICAgICB2YWx1ZSA9IHt9CiAgICAgICAgZm9yIGtleSwgaXRlbSBpbiBwYWlyczoKICAgICAgICAgICAgaWYga2V5IGluIHZhbHVlOgogICAgICAgICAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdTVEFURV9EVVBMSUNBVEVfRklFTEQnKQogICAgICAgICAgICB2YWx1ZVtrZXldID0gaXRlbQogICAgICAgIHJldHVybiB2YWx1ZQogICAgdHJ5OgogICAgICAgIGRlZiBpbnZhbGlkX2NvbnN0YW50KF8pOgogICAgICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0lOVkFMSURfTlVNQkVSJykKICAgICAgICB2YWx1ZSA9IGpzb24ubG9hZHMocmF3LCBvYmplY3RfcGFpcnNfaG9vaz11bmlxdWUsIHBhcnNlX2NvbnN0YW50PWludmFsaWRfY29uc3RhbnQpCiAgICBleGNlcHQgKFZhbHVlRXJyb3IsIFVuaWNvZGVFcnJvcikgYXMgZXJyb3I6CiAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdTVEFURV9JTlZBTElEX0pTT046IG9yaWdpbmFsIGZpbGUgcHJlc2VydmVkJykgZnJvbSBlcnJvcgogICAgaWYgbm90IGlzaW5zdGFuY2UodmFsdWUsIGRpY3QpOgogICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignU1RBVEVfT0JKRUNUX1JFUVVJUkVEJykKICAgIHJldHVybiB2YWx1ZQoKZGVmIF9zdGF0ZV9jYW5vbmljYWwodmFsdWUpOgogICAgcmV0dXJuIGpzb24uZHVtcHModmFsdWUsIGVuc3VyZV9hc2NpaT1GYWxzZSwgc29ydF9rZXlzPVRydWUsIHNlcGFyYXRvcnM9KCcsJywgJzonKSwgYWxsb3dfbmFuPUZhbHNlKS5lbmNvZGUoJ3V0Zi04JykKCmRlZiBfc3RhdGVfcmVhZChwYXRoKToKICAgIGltcG9ydCBzdGF0CiAgICBwYXRoID0gX3N0YXRlX25vX2xpbmtzKHBhdGgpCiAgICAjIFZhbGlkYXRlIHRoZSBmaWxlIGRlc2NyaXB0b3IgdGhhdCB3aWxsIGFjdHVhbGx5IGJlIHJlYWQ7IG5ldmVyIHN0YXQgdGhlbiByZW9wZW4uCiAgICBmZCA9IG9zLm9wZW4ocGF0aCwgb3MuT19SRE9OTFkgfCBnZXRhdHRyKG9zLCAnT19OT0ZPTExPVycsIDApIHwgZ2V0YXR0cihvcywgJ09fTk9OQkxPQ0snLCAwKSkKICAgIHRyeToKICAgICAgICBpbmZvID0gb3MuZnN0YXQoZmQpCiAgICAgICAgaWYgbm90IHN0YXQuU19JU1JFRyhpbmZvLnN0X21vZGUpIG9yIGluZm8uc3Rfc2l6ZSA+IDFfMjAwXzAwMCBvciBpbmZvLnN0X25saW5rICE9IDE6CiAgICAgICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignU1RBVEVfRklMRV9VTlNBRkUnKQogICAgICAgIGlmIG9zLm5hbWUgIT0gJ250JyBhbmQgKGluZm8uc3RfdWlkICE9IG9zLmdldHVpZCgpIG9yIGluZm8uc3RfbW9kZSAmIDBvMDc3KToKICAgICAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdTVEFURV9GSUxFX05PVF9QUklWQVRFJykKICAgICAgICB3aXRoIG9zLmZkb3BlbihmZCwgJ3JiJywgY2xvc2VmZD1GYWxzZSkgYXMgc3RyZWFtOgogICAgICAgICAgICByYXcgPSBzdHJlYW0ucmVhZCgxXzIwMF8wMDEpCiAgICAgICAgaWYgbGVuKHJhdykgPiAxXzIwMF8wMDA6CiAgICAgICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignU1RBVEVfU0laRV9MSU1JVCcpCiAgICAgICAgcmV0dXJuIHJhdwogICAgZmluYWxseToKICAgICAgICBvcy5jbG9zZShmZCkKCgpkZWYgX3N0YXRlX3dyaXRlKHBhdGgsIHJhdyk6CiAgICBwYXRoID0gX3N0YXRlX25vX2xpbmtzKHBhdGgpCiAgICB0ZW1wID0gcGF0aC53aXRoX25hbWUoJy5zdGF0ZS13cml0ZS0nICsgb3MudXJhbmRvbSgxMikuaGV4KCkpCiAgICBmZCA9IG9zLm9wZW4odGVtcCwgb3MuT19XUk9OTFkgfCBvcy5PX0NSRUFUIHwgb3MuT19FWENMLCAwbzYwMCkKICAgIHRyeToKICAgICAgICB3aXRoIG9zLmZkb3BlbihmZCwgJ3diJykgYXMgZmlsZToKICAgICAgICAgICAgZmlsZS53cml0ZShyYXcpOyBmaWxlLmZsdXNoKCk7IG9zLmZzeW5jKGZpbGUuZmlsZW5vKCkpCiAgICAgICAgb3MucmVwbGFjZSh0ZW1wLCBwYXRoKQogICAgICAgIGlmIG9zLm5hbWUgIT0gJ250JzoKICAgICAgICAgICAgZGlyZWN0b3J5ID0gb3Mub3BlbihwYXRoLnBhcmVudCwgb3MuT19SRE9OTFkgfCBnZXRhdHRyKG9zLCAnT19ESVJFQ1RPUlknLCAwKSkKICAgICAgICAgICAgdHJ5OiBvcy5mc3luYyhkaXJlY3RvcnkpCiAgICAgICAgICAgIGZpbmFsbHk6IG9zLmNsb3NlKGRpcmVjdG9yeSkKICAgIGZpbmFsbHk6CiAgICAgICAgdGVtcC51bmxpbmsobWlzc2luZ19vaz1UcnVlKQoKQF9zdGF0ZV9jb250ZXh0bGliLmNvbnRleHRtYW5hZ2VyCmRlZiBfc3RhdGVfbG9jayhyb290KToKICAgIGltcG9ydCBzdGF0CiAgICByb290ID0gX3N0YXRlX25vX2xpbmtzKHJvb3QpCiAgICBpbmZvID0gcm9vdC5zdGF0KCkKICAgIGlmIG5vdCBzdGF0LlNfSVNESVIoaW5mby5zdF9tb2RlKSBvciAob3MubmFtZSAhPSAnbnQnIGFuZCAoaW5mby5zdF91aWQgIT0gb3MuZ2V0dWlkKCkgb3IgaW5mby5zdF9tb2RlICYgMG8wNzcpKToKICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0RJUkVDVE9SWV9OT1RfUFJJVkFURScpCiAgICBsb2NrID0gX3N0YXRlX25vX2xpbmtzKHJvb3QgLyAnY2xpZW50LmxvY2snKQogICAgZmQgPSBvcy5vcGVuKGxvY2ssIG9zLk9fQ1JFQVQgfCBvcy5PX1JEV1IgfCBnZXRhdHRyKG9zLCAnT19OT0ZPTExPVycsIDApLCAwbzYwMCkKICAgIHRyeToKICAgICAgICBpbmZvID0gb3MuZnN0YXQoZmQpCiAgICAgICAgaWYgbm90IHN0YXQuU19JU1JFRyhpbmZvLnN0X21vZGUpIG9yIGluZm8uc3RfbmxpbmsgIT0gMSBvciAob3MubmFtZSAhPSAnbnQnIGFuZCAoaW5mby5zdF91aWQgIT0gb3MuZ2V0dWlkKCkgb3IgaW5mby5zdF9tb2RlICYgMG8wNzcpKToKICAgICAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdTVEFURV9MT0NLX05PVF9QUklWQVRFJykKICAgICAgICBpZiBvcy5uYW1lID09ICdudCc6CiAgICAgICAgICAgIGltcG9ydCBtc3ZjcnQKICAgICAgICAgICAgaWYgb3MuZnN0YXQoZmQpLnN0X3NpemUgPT0gMDogb3Mud3JpdGUoZmQsIGInMCcpCiAgICAgICAgICAgIG9zLmxzZWVrKGZkLCAwLCAwKTsgbXN2Y3J0LmxvY2tpbmcoZmQsIG1zdmNydC5MS19OQkxDSywgMSkKICAgICAgICBlbHNlOgogICAgICAgICAgICBpbXBvcnQgZmNudGwKICAgICAgICAgICAgZmNudGwuZmxvY2soZmQsIGZjbnRsLkxPQ0tfRVggfCBmY250bC5MT0NLX05CKQogICAgICAgIHlpZWxkCiAgICBleGNlcHQgKEJsb2NraW5nSU9FcnJvciwgUGVybWlzc2lvbkVycm9yKSBhcyBlcnJvcjoKICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0JVU1k6IG9yaWdpbmFsIG9wZXJhdGlvbiBtYXkgc3RpbGwgYmUgcnVubmluZycpIGZyb20gZXJyb3IKICAgIGZpbmFsbHk6CiAgICAgICAgb3MuY2xvc2UoZmQpCgpkZWYgcGVyc2lzdGVudF9zZXNzaW9uX2RpcmVjdG9yeShjb25maWcsIHJlcG89Tm9uZSk6CiAgICBiaW5kaW5nID0gY29uZmlnLmdldCgnYmluZGluZycsICcnKQogICAgaWYgbm90IGlzaW5zdGFuY2UoYmluZGluZywgc3RyKSBvciBub3QgcmUuZnVsbG1hdGNoKHInWzAtOWEtZl17MzJ9JywgYmluZGluZyk6CiAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdTVEFURV9CSU5ESU5HX0lOVkFMSUQnKQogICAgZW5jb2RlZCA9IF9zdGF0ZV9jYW5vbmljYWwoY29uZmlnKTsgZGlnZXN0ID0gaGFzaGxpYi5zaGEyNTYoZW5jb2RlZCkuaGV4ZGlnZXN0KCkKICAgIHJvb3QgPSBwZXJzaXN0ZW50X3N0YXRlX2hvbWUocmVwbykgLyAoJ3NjeC1jb2xsYWJvcmF0aW9uLScgKyBiaW5kaW5nKQogICAgX3N0YXRlX25vX2xpbmtzKHJvb3QpOyByb290Lm1rZGlyKG1vZGU9MG83MDAsIGV4aXN0X29rPVRydWUpCiAgICBpZiBvcy5uYW1lICE9ICdudCcgYW5kIHJvb3Quc3RhdCgpLnN0X3VpZCAhPSBvcy5nZXR1aWQoKToKICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0RJUkVDVE9SWV9PV05FUl9NSVNNQVRDSCcpCiAgICBvcy5jaG1vZChyb290LCAwbzcwMCkKICAgIG9sZCA9IF9zdGF0ZV9ub19saW5rcyhfc3RhdGVfcGF0aGxpYi5QYXRoKHRlbXBmaWxlLmdldHRlbXBkaXIoKSkgLyByb290Lm5hbWUpCiAgICBib3VuZGFyeSA9IHN0YXRlX3JlcG9fYm91bmRhcnkocmVwbykKICAgIGlmIGJvdW5kYXJ5IGlzIG5vdCBOb25lIGFuZCBvbGQucmVzb2x2ZSgpLmlzX3JlbGF0aXZlX3RvKGJvdW5kYXJ5KToKICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ0xFR0FDWV9TVEFURV9JTl9SRVBPU0lUT1JZJykKICAgIHRhcmdldCA9IHJvb3QgLyAnc3RhdGUuanNvbic7IGRlc2NyaXB0b3IgPSByb290IC8gJ2Nvbm5lY3Rpb24uanNvbicKICAgIGRlZiB2YWxpZGF0ZShyYXcpOgogICAgICAgIGlmIF9zdGF0ZV9qc29uKHJhdykuZ2V0KCdjb25maWdfaGFzaCcpICE9IGRpZ2VzdDoKICAgICAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdTVEFURV9DT05GSUdfTUlTTUFUQ0g6IG5ldmVyIHJlcGxhY2UgYW4gZXhpc3RpbmcgaWRlbnRpdHknKQogICAgd2l0aCBfc3RhdGVfbG9jayhyb290KToKICAgICAgICBpZiBkZXNjcmlwdG9yLmV4aXN0cygpIGFuZCBfc3RhdGVfY2Fub25pY2FsKF9zdGF0ZV9qc29uKF9zdGF0ZV9yZWFkKGRlc2NyaXB0b3IpKSkgIT0gZW5jb2RlZDoKICAgICAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdTVEFURV9ERVNDUklQVE9SX01JU01BVENIJykKICAgICAgICBpZiB0YXJnZXQuZXhpc3RzKCk6IHZhbGlkYXRlKF9zdGF0ZV9yZWFkKHRhcmdldCkpCiAgICAgICAgaWYgb2xkLnJlc29sdmUoKSAhPSByb290LnJlc29sdmUoKSBhbmQgKG9sZCAvICdzdGF0ZS5qc29uJykuZXhpc3RzKCk6CiAgICAgICAgICAgIHdpdGggX3N0YXRlX2xvY2sob2xkKToKICAgICAgICAgICAgICAgIG9yaWdpbmFsID0gX3N0YXRlX3JlYWQob2xkIC8gJ3N0YXRlLmpzb24nKTsgdmFsaWRhdGUob3JpZ2luYWwpCiAgICAgICAgICAgICAgICBzb3VyY2VfaGFzaCA9IGhhc2hsaWIuc2hhMjU2KG9yaWdpbmFsKS5oZXhkaWdlc3QoKQogICAgICAgICAgICAgICAgcmVjZWlwdCA9IHJvb3QgLyAnbWlncmF0aW9uLmpzb24nCiAgICAgICAgICAgICAgICByZWNvcmQgPSB7J2Zvcm1hdCc6ICdsZWJveC1zdGF0ZS1taWdyYXRpb24tdjEnLCAnc291cmNlJzogc3RyKG9sZC5yZXNvbHZlKCkpLAogICAgICAgICAgICAgICAgICAgICAgICAgICdzb3VyY2Vfc2hhMjU2Jzogc291cmNlX2hhc2gsICdjb25maWdfaGFzaCc6IGRpZ2VzdH0KICAgICAgICAgICAgICAgIGlmIHJlY2VpcHQuZXhpc3RzKCkgYW5kIG5vdCB0YXJnZXQuZXhpc3RzKCk6CiAgICAgICAgICAgICAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdTVEFURV9NSUdSQVRJT05fVEFSR0VUX01JU1NJTkc6IHByZXNlcnZlIG9yaWdpbmFsIGV2aWRlbmNlJykKICAgICAgICAgICAgICAgIGlmIHRhcmdldC5leGlzdHMoKSBhbmQgX3N0YXRlX3JlYWQodGFyZ2V0KSAhPSBvcmlnaW5hbDoKICAgICAgICAgICAgICAgICAgICBpZiBub3QgcmVjZWlwdC5leGlzdHMoKSBvciBfc3RhdGVfanNvbihfc3RhdGVfcmVhZChyZWNlaXB0KSkgIT0gcmVjb3JkOgogICAgICAgICAgICAgICAgICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX01JR1JBVElPTl9DT05GTElDVDoga2VlcCBib3RoIHN0YXRlcyBhbmQgcmVjb25jaWxlJykKICAgICAgICAgICAgICAgIGVsaWYgbm90IHJlY2VpcHQuZXhpc3RzKCk6CiAgICAgICAgICAgICAgICAgICAgaWYgbm90IHRhcmdldC5leGlzdHMoKTogX3N0YXRlX3dyaXRlKHRhcmdldCwgb3JpZ2luYWwpCiAgICAgICAgICAgICAgICAgICAgX3N0YXRlX3dyaXRlKHJlY2VpcHQsIF9zdGF0ZV9jYW5vbmljYWwocmVjb3JkKSkKICAgICAgICAgICAgICAgIGVsaWYgX3N0YXRlX2pzb24oX3N0YXRlX3JlYWQocmVjZWlwdCkpICE9IHJlY29yZDoKICAgICAgICAgICAgICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX01JR1JBVElPTl9DT05GTElDVDogbGVnYWN5IHN0YXRlIGNoYW5nZWQnKQogICAgICAgIGlmIG5vdCBkZXNjcmlwdG9yLmV4aXN0cygpOiBfc3RhdGVfd3JpdGUoZGVzY3JpcHRvciwgZW5jb2RlZCkKICAgIHJldHVybiByb290CgojIEVORCBMRUJPWCBQRVJTSVNURU5UIFNUQVRFIExBWU9VVCBWMQoKIyBCRUdJTiBMRUJPWCBQUk9DRVNTIEdJVCBBVVRIIFYxCiMgU2VsZi1jb250YWluZWQgaW4gYm90aCBwaW5uZWQgZW50cnkgcG9pbnRzOyBubyBjcmVkZW50aWFsLWhlbHBlci9nbG9iYWwtY29uZmlnIHBlcnNpc3RlbmNlLgpkZWYgX2dpdF9hdXRoX3JlcG9zaXRvcnkodmFsdWUpOgogICAgZnJvbSB1cmxsaWIucGFyc2UgaW1wb3J0IHVybHNwbGl0CiAgICBpZiBub3QgaXNpbnN0YW5jZSh2YWx1ZSwgc3RyKToKICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ0dJVF9BVVRIX1RBUkdFVF9JTlZBTElEJykKICAgIGlmIHZhbHVlLnN0YXJ0c3dpdGgoJ2dpdEBnaXRodWIuY29tOicpOgogICAgICAgIHZhbHVlID0gdmFsdWVbbGVuKCdnaXRAZ2l0aHViLmNvbTonKTpdCiAgICBlbGlmICc6Ly8nIGluIHZhbHVlOgogICAgICAgIHRyeToKICAgICAgICAgICAgcGFyc2VkID0gdXJsc3BsaXQodmFsdWUpCiAgICAgICAgICAgIGlmIChwYXJzZWQuc2NoZW1lICE9ICdodHRwcycgb3IgcGFyc2VkLmhvc3RuYW1lICE9ICdnaXRodWIuY29tJyBvciBwYXJzZWQucG9ydCBub3QgaW4gKE5vbmUsIDQ0MykKICAgICAgICAgICAgICAgICAgICBvciBwYXJzZWQudXNlcm5hbWUgb3IgcGFyc2VkLnBhc3N3b3JkIG9yIHBhcnNlZC5xdWVyeSBvciBwYXJzZWQuZnJhZ21lbnQpOgogICAgICAgICAgICAgICAgcmFpc2UgVmFsdWVFcnJvcigpCiAgICAgICAgICAgIHZhbHVlID0gcGFyc2VkLnBhdGhbMTpdIGlmIHBhcnNlZC5wYXRoLnN0YXJ0c3dpdGgoJy8nKSBlbHNlIHBhcnNlZC5wYXRoCiAgICAgICAgZXhjZXB0IFZhbHVlRXJyb3I6CiAgICAgICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignR0lUX0FVVEhfVEFSR0VUX0lOVkFMSUQnKSBmcm9tIE5vbmUKICAgIGlmIHZhbHVlLmVuZHN3aXRoKCcuZ2l0Jyk6CiAgICAgICAgdmFsdWUgPSB2YWx1ZVs6LTRdCiAgICBpZiBub3QgcmUuZnVsbG1hdGNoKHInW0EtWmEtejAtOV8uLV0rL1tBLVphLXowLTlfLi1dKycsIHZhbHVlKSBvciBhbnkocCBpbiAoJy4nLCAnLi4nKSBmb3IgcCBpbiB2YWx1ZS5zcGxpdCgnLycpKToKICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ0dJVF9BVVRIX1RBUkdFVF9JTlZBTElEJykKICAgIHJldHVybiB2YWx1ZQoKCmRlZiBfZ2l0X2F1dGhfZW52aXJvbm1lbnQodG9rZW4sIHJlcG9zaXRvcnkpOgogICAgIiIiVXNlIGN1cnJlbnQgZXhwbGljaXRseSBzdXBwbGllZC92ZXJpZmllZCBjcmVkZW50aWFscyBpbiBwcm9jZXNzIG1lbW9yeSwgbmV2ZXIgYXJndiBvciBoZWxwZXIgc3RvcmFnZS4iIiIKICAgIGltcG9ydCBiYXNlNjQgYXMgX2F1dGhfYmFzZTY0CiAgICByZXBvc2l0b3J5ID0gX2dpdF9hdXRoX3JlcG9zaXRvcnkocmVwb3NpdG9yeSkKICAgIGlmIChub3QgaXNpbnN0YW5jZSh0b2tlbiwgc3RyKSBvciBub3QgOCA8PSBsZW4odG9rZW4pIDw9IDQwOTYKICAgICAgICAgICAgb3IgYW55KGNoLmlzc3BhY2UoKSBvciBvcmQoY2gpIDwgMzIgb3Igb3JkKGNoKSA9PSAxMjcgZm9yIGNoIGluIHRva2VuKSk6CiAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdHSVRfQVVUSF9DUkVERU5USUFMX0lOVkFMSUQnKQogICAgZW52ID0gZGljdChvcy5lbnZpcm9uKQogICAgY291bnQgPSBlbnYuZ2V0KCdHSVRfQ09ORklHX0NPVU5UJywgJzAnKQogICAgaWYgbm90IHJlLmZ1bGxtYXRjaChyJyg/OjB8WzEtOV1bMC05XXswLDJ9KScsIGNvdW50KSBvciBpbnQoY291bnQpID4gMjU2OgogICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignR0lUX0FVVEhfQ09ORklHX1VOVkVSSUZJRUQnKQogICAgY291bnQgPSBpbnQoY291bnQpCiAgICBpZiBhbnkoJ0dJVF9DT05GSUdfS0VZXyVkJyAlIGkgbm90IGluIGVudiBvciAnR0lUX0NPTkZJR19WQUxVRV8lZCcgJSBpIG5vdCBpbiBlbnYgZm9yIGkgaW4gcmFuZ2UoY291bnQpKToKICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ0dJVF9BVVRIX0NPTkZJR19VTlZFUklGSUVEJykKICAgIGF1dGhvcml6YXRpb24gPSAnQXV0aG9yaXphdGlvbjogQmFzaWMgJyArIF9hdXRoX2Jhc2U2NC5iNjRlbmNvZGUoKCd4LWFjY2Vzcy10b2tlbjonICsgdG9rZW4pLmVuY29kZSgpKS5kZWNvZGUoJ2FzY2lpJykKICAgIGVudHJpZXMgPSBbKCdjcmVkZW50aWFsLmhlbHBlcicsICcnKSwgKCdjb3JlLmFza1Bhc3MnLCAnJyksICgnaHR0cC5mb2xsb3dSZWRpcmVjdHMnLCAnZmFsc2UnKSwKICAgICAgICAgICAgICAgKCdodHRwLmV4dHJhSGVhZGVyJywgJycpLCAoJ2h0dHAuaHR0cHM6Ly9naXRodWIuY29tLy5leHRyYUhlYWRlcicsICcnKV0KICAgIGZvciBzdWZmaXggaW4gKCcnLCAnLmdpdCcpOgogICAgICAgIGtleSA9ICdodHRwLmh0dHBzOi8vZ2l0aHViLmNvbS8nICsgcmVwb3NpdG9yeSArIHN1ZmZpeCArICcuZXh0cmFIZWFkZXInCiAgICAgICAgZW50cmllcy5leHRlbmQoKChrZXksICcnKSwgKGtleSwgYXV0aG9yaXphdGlvbiksCiAgICAgICAgICAgICAgICAgICAgICAgICgnY3JlZGVudGlhbC5odHRwczovL2dpdGh1Yi5jb20vJyArIHJlcG9zaXRvcnkgKyBzdWZmaXggKyAnLmhlbHBlcicsICcnKSwKICAgICAgICAgICAgICAgICAgICAgICAgKCdjcmVkZW50aWFsLmh0dHBzOi8vZ2l0aHViLmNvbS8nICsgcmVwb3NpdG9yeSArIHN1ZmZpeCArICcudXNlSHR0cFBhdGgnLCAndHJ1ZScpKSkKICAgICMgUmV1c2Ugb25seSB0aGlzIGV4YWN0IHRyYWlsaW5nIGd1YXJkIGJsb2NrIGZvciB0aGUgc2FtZSByZXBvc2l0b3J5LiBSZXBlYXRlZCByZW5ld2FsCiAgICAjIG11c3Qgbm90IGdyb3cgcHJvY2VzcyBjb25maWcgd2l0aG91dCBib3VuZDsgdW5yZWxhdGVkIGluaGVyaXRlZCBlbnRyaWVzIHN0YXkgdW50b3VjaGVkLgogICAgaWYgY291bnQgPj0gbGVuKGVudHJpZXMpOgogICAgICAgIHRhaWwgPSBbKGVudlsnR0lUX0NPTkZJR19LRVlfJWQnICUgaV0sIGVudlsnR0lUX0NPTkZJR19WQUxVRV8lZCcgJSBpXSkKICAgICAgICAgICAgICAgIGZvciBpIGluIHJhbmdlKGNvdW50IC0gbGVuKGVudHJpZXMpLCBjb3VudCldCiAgICAgICAgaWYgYWxsKG9sZF9rZXkgPT0ga2V5IGFuZCAob2xkX3ZhbHVlLnN0YXJ0c3dpdGgoJ0F1dGhvcml6YXRpb246IEJhc2ljICcpCiAgICAgICAgICAgICAgICBpZiB2YWx1ZSA9PSBhdXRob3JpemF0aW9uIGVsc2Ugb2xkX3ZhbHVlID09IHZhbHVlKQogICAgICAgICAgICAgICAgZm9yIChvbGRfa2V5LCBvbGRfdmFsdWUpLCAoa2V5LCB2YWx1ZSkgaW4gemlwKHRhaWwsIGVudHJpZXMpKToKICAgICAgICAgICAgY291bnQgLT0gbGVuKGVudHJpZXMpCiAgICBpZiBjb3VudCArIGxlbihlbnRyaWVzKSA+IDI1NjoKICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ0dJVF9BVVRIX0NPTkZJR19VTlZFUklGSUVEJykKICAgIGZvciBpbmRleCwgKGtleSwgdmFsdWUpIGluIGVudW1lcmF0ZShlbnRyaWVzLCBjb3VudCk6CiAgICAgICAgZW52WydHSVRfQ09ORklHX0tFWV8lZCcgJSBpbmRleF0gPSBrZXkKICAgICAgICBlbnZbJ0dJVF9DT05GSUdfVkFMVUVfJWQnICUgaW5kZXhdID0gdmFsdWUKICAgIGVudlsnR0lUX0NPTkZJR19DT1VOVCddID0gc3RyKGNvdW50ICsgbGVuKGVudHJpZXMpKQogICAgZW52WydHSVRfVEVSTUlOQUxfUFJPTVBUJ10gPSAnMCcKICAgIGVudlsnR0lUX0FTS1BBU1MnXSA9ICcnCiAgICBlbnZbJ1NTSF9BU0tQQVNTJ10gPSAnJwogICAgcmV0dXJuIGVudgpkZWYgX2dpdF9hdXRoX3JlYWR5X2Vudmlyb25tZW50KHRva2VuLCByZXBvc2l0b3J5KToKICAgICMgR0lUX0NPTkZJR19DT1VOVC9LRVkvVkFMVUUgc3VwcG9ydCBpcyByZXF1aXJlZDsgb2xkL3Vua25vd24gR2l0IG11c3Qgbm90IHNpbGVudGx5CiAgICAjIGZhbGwgYmFjayB0byBnbG9iYWwgaGVscGVycyBvciBwbGF0Zm9ybSBjcmVkZW50aWFscy4gTmV2ZXIgaW5jbHVkZSByYXcgc3RkZXJyLgogICAgZW52ID0gX2dpdF9hdXRoX2Vudmlyb25tZW50KHRva2VuLCByZXBvc2l0b3J5KQogICAgdHJ5OgogICAgICAgIHJlc3VsdCA9IHN1YnByb2Nlc3MucnVuKFsnZ2l0JywgJy0tdmVyc2lvbiddLCBjYXB0dXJlX291dHB1dD1UcnVlLCB0ZXh0PVRydWUsIHRpbWVvdXQ9NSwgZW52PWVudikKICAgICAgICBtYXRjaCA9IHJlLm1hdGNoKHInXmdpdCB2ZXJzaW9uIChbMC05XSspXC4oWzAtOV0rKSg/OlsuXHNdfCQpJywgcmVzdWx0LnN0ZG91dCBvciAnJykKICAgICAgICBpZiByZXN1bHQucmV0dXJuY29kZSAhPSAwIG9yIG1hdGNoIGlzIE5vbmUgb3IgdHVwbGUobWFwKGludCwgbWF0Y2guZ3JvdXBzKCkpKSA8ICgyLCAzMSk6CiAgICAgICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignR0lUX1BST0NFU1NfQVVUSF9SRVFVSVJFU18yXzMxJykKICAgIGV4Y2VwdCAoT1NFcnJvciwgc3VicHJvY2Vzcy5UaW1lb3V0RXhwaXJlZCk6CiAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdHSVRfUFJPQ0VTU19BVVRIX1VOQVZBSUxBQkxFJykgZnJvbSBOb25lCiAgICByZXR1cm4gZW52CiMgRU5EIExFQk9YIFBST0NFU1MgR0lUIEFVVEggVjEKClZFUlNJT04gPSAnc2N4LWdoLW0yLXYxJwpMSU1JVCA9IDEwMF8wMDAKIyBBZGFwdGl2ZSBwb2xsaW5nOiBmYXN0IHJpZ2h0IGFmdGVyIHdlIHB1Ymxpc2ggc29tZXRoaW5nLCBiYWNraW5nIG9mZiB0byBhIDEwIHMgY2VpbGluZyAoR2l0IGZldGNoZXMgYXJlIGNoZWFwIGFuZAojIHRoZSBkZXNrdG9wIGFuc3dlcnMgd2l0aGluIHNlY29uZHM7IGEgZml4ZWQgMTUtMjAgcyBpbnRlcnZhbCB3YXMgdGhlIGRvbWluYW50IGNvc3QgcGVyIHRvb2wgY2FsbCkuClBPTExfU1RFUFMgPSAoMiwgMiwgMywgNCwgNiwgOCwgMTApCgpkZWYgcG9sbF9kZWxheShhdHRlbXB0KToKICAgIHJldHVybiBQT0xMX1NURVBTW21pbihhdHRlbXB0LCBsZW4oUE9MTF9TVEVQUykgLSAxKV0KCmNsYXNzIFN0b3AoRXhjZXB0aW9uKToKICAgIHBhc3MKCmRlZiByZXF1aXJlKG9rLCBtZXNzYWdlKToKICAgIGlmIG5vdCBvazoKICAgICAgICByYWlzZSBTdG9wKG1lc3NhZ2UpCgpkZWYgY2Fub25pY2FsKHZhbHVlKToKICAgIHJldHVybiBqc29uLmR1bXBzKHZhbHVlLCBlbnN1cmVfYXNjaWk9RmFsc2UsIHNvcnRfa2V5cz1UcnVlLCBzZXBhcmF0b3JzPSgnLCcsICc6JyksIGFsbG93X25hbj1GYWxzZSkuZW5jb2RlKCd1dGYtOCcpCgpkZWYgbG9hZF9qc29uKGRhdGEsIGxpbWl0PUxJTUlUKToKICAgIHJlcXVpcmUobGVuKGRhdGEpIDw9IGxpbWl0LCAnRGF0YSBleGNlZWRzIHRoZSBzZXNzaW9uIGxpbWl0LicpCiAgICBkZWYgdW5pcXVlKHBhaXJzKToKICAgICAgICByZXN1bHQgPSB7fQogICAgICAgIGZvciBrZXksIHZhbHVlIGluIHBhaXJzOgogICAgICAgICAgICByZXF1aXJlKGtleSBub3QgaW4gcmVzdWx0LCAnRHVwbGljYXRlIEpTT04gZmllbGQuJykKICAgICAgICAgICAgcmVzdWx0W2tleV0gPSB2YWx1ZQogICAgICAgIHJldHVybiByZXN1bHQKICAgIHJldHVybiBqc29uLmxvYWRzKGRhdGEsIG9iamVjdF9wYWlyc19ob29rPXVuaXF1ZSkKCmRlZiBleGFjdCh2YWx1ZSwgZmllbGRzKToKICAgIHJlcXVpcmUoaXNpbnN0YW5jZSh2YWx1ZSwgZGljdCkgYW5kIHNldCh2YWx1ZSkgPT0gc2V0KGZpZWxkcyksICdVbmV4cGVjdGVkIG1lc3NhZ2UgZmllbGRzLicpCgpkZWYgYjY0KHZhbHVlKToKICAgIHJldHVybiBiYXNlNjQuYjY0ZW5jb2RlKHZhbHVlKS5kZWNvZGUoJ2FzY2lpJykKCmRlZiB1bmI2NCh2YWx1ZSwgbGVuZ3RoPU5vbmUpOgogICAgcmVxdWlyZShpc2luc3RhbmNlKHZhbHVlLCBzdHIpIGFuZCBsZW4odmFsdWUpIDw9IDkwXzAwMCwgJ0ludmFsaWQgZW5jb2RlZCB2YWx1ZS4nKQogICAgcmF3ID0gYmFzZTY0LmI2NGRlY29kZSh2YWx1ZSwgdmFsaWRhdGU9VHJ1ZSkKICAgIHJlcXVpcmUobGVuZ3RoIGlzIE5vbmUgb3IgbGVuKHJhdykgPT0gbGVuZ3RoLCAnSW52YWxpZCBlbmNvZGVkIGxlbmd0aC4nKQogICAgcmV0dXJuIHJhdwoKZGVmIGNyeXB0bygpOgogICAgdHJ5OgogICAgICAgIGZyb20gY3J5cHRvZ3JhcGh5Lmhhem1hdC5wcmltaXRpdmVzIGltcG9ydCBoYXNoZXMsIHNlcmlhbGl6YXRpb24KICAgICAgICBmcm9tIGNyeXB0b2dyYXBoeS5oYXptYXQucHJpbWl0aXZlcy5hc3ltbWV0cmljIGltcG9ydCBlYwogICAgICAgIGZyb20gY3J5cHRvZ3JhcGh5Lmhhem1hdC5wcmltaXRpdmVzLmtkZi5oa2RmIGltcG9ydCBIS0RGCiAgICAgICAgZnJvbSBjcnlwdG9ncmFwaHkuaGF6bWF0LnByaW1pdGl2ZXMuY2lwaGVycy5hZWFkIGltcG9ydCBBRVNHQ00KICAgICAgICByZXR1cm4gaGFzaGVzLCBzZXJpYWxpemF0aW9uLCBlYywgSEtERiwgQUVTR0NNCiAgICBleGNlcHQgSW1wb3J0RXJyb3I6CiAgICAgICAgcmFpc2UgU3RvcCgnUmVxdWlyZXMgdGhlIFB5dGhvbiBjcnlwdG9ncmFwaHkgcGFja2FnZS4gSW5zdGFsbCBpdCBvbmx5IGlmIHRoZSBlbnZpcm9ubWVudCBwZXJtaXRzIGRlcGVuZGVuY2llczsgZG8gbm90IGJ5cGFzcyBwbGF0Zm9ybSByZXN0cmljdGlvbnMuJykKCmRlZiB2YWxpZGF0ZV9jb25maWcoYyk6CiAgICBleGFjdChjLCBbJ3ZlcnNpb24nLCAncmVwb3NpdG9yeScsICdyZXBvX2lkJywgJ2JyYW5jaCcsICdiaW5kaW5nJywgJ2Vwb2NoJywgJ3Byb2plY3RfaWQnLCAnbWF4X29wZXJhdGlvbnMnLCAnZXhwaXJlc19hdCddKQogICAgcmVxdWlyZShjWyd2ZXJzaW9uJ10gPT0gVkVSU0lPTiwgJ1Vuc3VwcG9ydGVkIGNvbGxhYm9yYXRpb24gdmVyc2lvbjsgcmVxdWVzdCBhIGZyZXNoIHRvb2wgcGFja2FnZS4nKQogICAgcmVxdWlyZShpc2luc3RhbmNlKGNbJ3JlcG9zaXRvcnknXSwgc3RyKSBhbmQgcmUuZnVsbG1hdGNoKHInW0EtWmEtejAtOV1bQS1aYS16MC05LV0qL1tBLVphLXowLTkuXy1dKycsIGNbJ3JlcG9zaXRvcnknXSksICdJbnZhbGlkIHJlcG9zaXRvcnkuJykKICAgIHJlcXVpcmUoY1sncmVwb3NpdG9yeSddLnNwbGl0KCcvJylbMV0gbm90IGluICgnLicsICcuLicpLCAnSW52YWxpZCByZXBvc2l0b3J5LicpCiAgICByZXF1aXJlKGlzaW5zdGFuY2UoY1sncmVwb19pZCddLCBzdHIpIGFuZCByZS5mdWxsbWF0Y2gocidbMS05XVswLTldezAsMTl9JywgY1sncmVwb19pZCddKSwgJ0ludmFsaWQgcmVwb3NpdG9yeSBJRC4nKQogICAgYnJhbmNoID0gY1snYnJhbmNoJ10KICAgIHJlcXVpcmUoaXNpbnN0YW5jZShicmFuY2gsIHN0cikgYW5kIDEgPD0gbGVuKGJyYW5jaCkgPD0gMjAwIGFuZCByZS5mdWxsbWF0Y2gocidbQS1aYS16MC05Ll8vLV0rJywgYnJhbmNoKQogICAgICAgICAgICBhbmQgJy4uJyBub3QgaW4gYnJhbmNoIGFuZCBub3QgYnJhbmNoLmVuZHN3aXRoKCcuJykgYW5kIGJyYW5jaC5sb3dlcigpIG5vdCBpbiAoJ21haW4nLCAnbWFzdGVyJykKICAgICAgICAgICAgYW5kIGFsbChwIGFuZCBub3QgcC5zdGFydHN3aXRoKCcuJykgYW5kIG5vdCBwLmxvd2VyKCkuZW5kc3dpdGgoJy5sb2NrJykgZm9yIHAgaW4gYnJhbmNoLnNwbGl0KCcvJykpLCAnSW52YWxpZCBjb2xsYWJvcmF0aW9uIGJyYW5jaC4nKQogICAgZm9yIGtleSBpbiAoJ2JpbmRpbmcnLCAnZXBvY2gnKToKICAgICAgICByZXF1aXJlKGlzaW5zdGFuY2UoY1trZXldLCBzdHIpIGFuZCByZS5mdWxsbWF0Y2gocidbMC05YS1mXXszMn0nLCBjW2tleV0pLCAnSW52YWxpZCBzZXNzaW9uIGlkZW50aXR5LicpCiAgICByZXF1aXJlKGlzaW5zdGFuY2UoY1sncHJvamVjdF9pZCddLCBzdHIpIGFuZCByZS5mdWxsbWF0Y2gocidbQS1aYS16MC05Xy1dezEsMTIwfScsIGNbJ3Byb2plY3RfaWQnXSksICdJbnZhbGlkIHByb2plY3QgaWRlbnRpdHkuJykKICAgICMgMCBtZWFucyB1bmxpbWl0ZWQ6IHRoZSBzZXNzaW9uIGlzIGxvbmctbGl2ZWQgYW5kIGVuZHMgb25seSB3aGVuIHRoZSB1c2VyIHN0b3BzIGl0IG9uIHRoZWlyIG1hY2hpbmUuCiAgICByZXF1aXJlKHR5cGUoY1snbWF4X29wZXJhdGlvbnMnXSkgaXMgaW50IGFuZCBjWydtYXhfb3BlcmF0aW9ucyddID49IDAgYW5kIHR5cGUoY1snZXhwaXJlc19hdCddKSBpcyBpbnQgYW5kIGNbJ2V4cGlyZXNfYXQnXSA+PSAwLAogICAgICAgICAgICAnSW52YWxpZCBzZXNzaW9uIGxpbWl0cy4nKQogICAgcmVxdWlyZShjWydleHBpcmVzX2F0J10gPT0gMCBvciBjWydleHBpcmVzX2F0J10gLSB0aW1lLnRpbWUoKSA+IDAsCiAgICAgICAgICAgICdTZXNzaW9uIGV4cGlyZWQgb3IgaW52YWxpZC4gUmVxdWVzdCBhIG5ldyBwYWNrYWdlOyBkbyBub3QgcmV1c2Ugb2xkIHJlcXVlc3RzLicpCgpkZWYgcGlucyhjKToKICAgIHJldHVybiB7a2V5OiBjW2tleV0gZm9yIGtleSBpbiAoJ3ZlcnNpb24nLCAncmVwb19pZCcsICdicmFuY2gnLCAnYmluZGluZycsICdlcG9jaCcpfQoKZGVmIG1lc3NhZ2VfcGF0aChjLCBraW5kLCBvcCk6CiAgICByZXF1aXJlKGtpbmQgaW4gKCdoZWxsbycsICdjb25maXJtYXRpb24nLCAncmVxdWVzdCcsICdyZXNwb25zZScsICdjbG9zZWQnLCAncmVuZXcnLCAncmVzdWx0cXVlcnknLCAncmVzdWx0cmVwbHknLCAnYWN0aXZhdGlvbnF1ZXJ5JywgJ2FjdGl2YXRpb25yZXBseScpIGFuZCByZS5mdWxsbWF0Y2gocidbYS16MC05XVthLXowLTktXXswLDc5fScsIG9wKSwgJ0ludmFsaWQgbWVzc2FnZSBwYXRoLicpCiAgICByZXR1cm4gZiIubGVib3gvc2Vzc2lvbi17Y1snYmluZGluZyddfS97b3B9LntraW5kfS5qc29uIgoKY2xhc3MgR2l0TWFpbGJveDoKICAgIGRlZiBfX2luaXRfXyhzZWxmLCByZXBvLCBjb25maWcsIHByaXZhdGUpOgogICAgICAgIHNlbGYucmVwbywgc2VsZi5jLCBzZWxmLnByaXZhdGUgPSByZXBvLnJlc29sdmUoKSwgY29uZmlnLCBwcml2YXRlCiAgICAgICAgc2VsZi5yZWYgPSAncmVmcy9zY3gtY29sbGFib3JhdGlvbi8nICsgY29uZmlnWydiaW5kaW5nJ10KICAgICAgICBob29rcyA9IHByaXZhdGUgLyAnZW1wdHktaG9va3MnCiAgICAgICAgaG9va3MubWtkaXIoZXhpc3Rfb2s9VHJ1ZSwgbW9kZT0wbzcwMCkKICAgICAgICByZXF1aXJlKG5vdCBob29rcy5pc19zeW1saW5rKCkgYW5kIG5vdCBhbnkoaG9va3MuaXRlcmRpcigpKSwgJ1ByaXZhdGUgaG9va3MgZGlyZWN0b3J5IGlzIG5vdCBlbXB0eS4nKQogICAgICAgIHNlbGYuYmFzZSA9IFsnZ2l0JywgJy1jJywgJ2NvcmUuaG9va3NQYXRoPScgKyBzdHIoaG9va3MpLCAnLWMnLCAnY29yZS5mc21vbml0b3I9ZmFsc2UnLAogICAgICAgICAgICAgICAgICAgICAnLWMnLCAnY29tbWl0LmdwZ1NpZ249ZmFsc2UnLCAnLWMnLCAncHVzaC5ncGdTaWduPWZhbHNlJywgJy1jJywgJ2h0dHAuZm9sbG93UmVkaXJlY3RzPWZhbHNlJywKICAgICAgICAgICAgICAgICAgICAgJy1jJywgJ3Byb3RvY29sLmV4dC5hbGxvdz1uZXZlcicsICctYycsICdwcm90b2NvbC5maWxlLmFsbG93PW5ldmVyJywgJy1DJywgc3RyKHNlbGYucmVwbyldCiAgICAgICAgIyBWYWxpZGF0ZSByZXNvbHZlZCBmZXRjaCBBTkQgcHVzaCBVUkxzOyBuZXZlciBsZXQgYSBoaWRkZW4gcHVzaHVybCBzZWxlY3QgYW5vdGhlciByZXBvc2l0b3J5LgogICAgICAgIGZvciBhcmdzIGluIFsoJ3JlbW90ZScsICdnZXQtdXJsJywgJy0tYWxsJywgJ29yaWdpbicpLCAoJ3JlbW90ZScsICdnZXQtdXJsJywgJy0tcHVzaCcsICctLWFsbCcsICdvcmlnaW4nKV06CiAgICAgICAgICAgIHVybHMgPSBzZWxmLnJ1bigqYXJncykuZGVjb2RlKCkuc3BsaXRsaW5lcygpCiAgICAgICAgICAgIHJlcXVpcmUobGVuKHVybHMpID09IDEsICdFeGFjdGx5IG9uZSBhdXRob3JpemVkIG9yaWdpbiBVUkwgaXMgcmVxdWlyZWQuJykKICAgICAgICAgICAgb3JpZ2luID0gdXJsc1swXS5zdHJpcCgpCiAgICAgICAgICAgIGlmIG9yaWdpbi5zdGFydHN3aXRoKCdnaXRAZ2l0aHViLmNvbTonKToKICAgICAgICAgICAgICAgIHRhcmdldCA9IG9yaWdpbltsZW4oJ2dpdEBnaXRodWIuY29tOicpOl0KICAgICAgICAgICAgZWxzZToKICAgICAgICAgICAgICAgIHBhcnNlZCA9IHVybHNwbGl0KG9yaWdpbikKICAgICAgICAgICAgICAgIHJlcXVpcmUocGFyc2VkLnNjaGVtZSA9PSAnaHR0cHMnIGFuZCBwYXJzZWQuaG9zdG5hbWUgPT0gJ2dpdGh1Yi5jb20nIGFuZCBwYXJzZWQucG9ydCBpbiAoTm9uZSwgNDQzKQogICAgICAgICAgICAgICAgICAgICAgICBhbmQgbm90IHBhcnNlZC51c2VybmFtZSBhbmQgbm90IHBhcnNlZC5wYXNzd29yZCBhbmQgbm90IHBhcnNlZC5xdWVyeSBhbmQgbm90IHBhcnNlZC5mcmFnbWVudCwgJ09yaWdpbiBtdXN0IGJlIHRoZSBhdXRob3JpemVkIGdpdGh1Yi5jb20gcmVwb3NpdG9yeS4nKQogICAgICAgICAgICAgICAgdGFyZ2V0ID0gcGFyc2VkLnBhdGgubHN0cmlwKCcvJykKICAgICAgICAgICAgcmVxdWlyZSh0YXJnZXQucmVtb3Zlc3VmZml4KCcuZ2l0JykubG93ZXIoKSA9PSBjb25maWdbJ3JlcG9zaXRvcnknXS5sb3dlcigpLCAnUmVwb3NpdG9yeSBkb2VzIG5vdCBtYXRjaCB0aGlzIHNlc3Npb24uJykKICAgICAgICBicmFuY2ggPSBzZWxmLnJ1bignc3ltYm9saWMtcmVmJywgJy0tc2hvcnQnLCAnSEVBRCcpLmRlY29kZSgpLnN0cmlwKCkKICAgICAgICByZXF1aXJlKGJyYW5jaCA9PSBjb25maWdbJ2JyYW5jaCddLAogICAgICAgICAgICAgICAgZiJUaGlzIHBhY2thZ2UgaXMgYm91bmQgdG8gYnJhbmNoIHtjb25maWdbJ2JyYW5jaCddfSBidXQgdGhlIGNoZWNrZWQtb3V0IGJyYW5jaCBpcyB7YnJhbmNofS4gIgogICAgICAgICAgICAgICAgJ0l0IHdhcyBpc3N1ZWQgZm9yIGEgZGlmZmVyZW50IGNvbnZlcnNhdGlvbi4gRG8gTk9UIHN3aXRjaCBicmFuY2hlcyBvciByZXVzZSBpdDogdGVsbCB0aGUgdXNlciB0byBjbGljayAnCiAgICAgICAgICAgICAgICAnIui/nuaOpeaWsCBBZ2VudCIgaW4gdGhlIFNodW5Db2RleCBHaXRIdWIg5Y2P5L2cIG1lbnUgZm9yIFRISVMgY29udmVyc2F0aW9uIGFuZCBwYXN0ZSB0aGUgbmV3IGluc3RydWN0aW9ucyBoZXJlLicpCgogICAgZGVmIHJ1bihzZWxmLCAqYXJncywgZGF0YT1Ob25lLCBlbnY9Tm9uZSk6CiAgICAgICAgc2FmZV9lbnYgPSBkaWN0KG9zLmVudmlyb24gaWYgZW52IGlzIE5vbmUgZWxzZSBlbnYpCiAgICAgICAgc2FmZV9lbnZbJ0dJVF9URVJNSU5BTF9QUk9NUFQnXSA9ICcwJwogICAgICAgIHJlc3VsdCA9IHN1YnByb2Nlc3MucnVuKHNlbGYuYmFzZSArIGxpc3QoYXJncyksIGlucHV0PWRhdGEsIHN0ZG91dD1zdWJwcm9jZXNzLlBJUEUsIHN0ZGVycj1zdWJwcm9jZXNzLlBJUEUsCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgZW52PXNhZmVfZW52LCB0aW1lb3V0PTQ1LCBjaGVjaz1GYWxzZSkKICAgICAgICByZXF1aXJlKHJlc3VsdC5yZXR1cm5jb2RlID09IDAsICdHaXQgb3BlcmF0aW9uIHdhcyBub3QgY29uZmlybWVkLiBDaGVjayByZXBvc2l0b3J5IGF1dGhvcml6YXRpb24gYW5kIG9yaWdpbmFsIHJlcXVlc3Qgc3RhdHVzOyBubyBmb3JjZSBwdXNoIG9yIGF1dG9tYXRpYyByZXRyeSB3YXMgcGVyZm9ybWVkLicpCiAgICAgICAgcmV0dXJuIHJlc3VsdC5zdGRvdXQKCiAgICBkZWYgZmV0Y2goc2VsZik6CiAgICAgICAgc2VsZi5ydW4oJ2ZldGNoJywgJy0tcXVpZXQnLCAnLS1uby10YWdzJywgJy0tbm8td3JpdGUtZmV0Y2gtaGVhZCcsICdvcmlnaW4nLCBmInJlZnMvaGVhZHMve3NlbGYuY1snYnJhbmNoJ119OntzZWxmLnJlZn0iKQogICAgICAgIHRpcCA9IHNlbGYucnVuKCdyZXYtcGFyc2UnLCAnLS12ZXJpZnknLCBzZWxmLnJlZikuZGVjb2RlKCkuc3RyaXAoKQogICAgICAgIHJlcXVpcmUocmUuZnVsbG1hdGNoKCdbMC05YS1mXXs0MH0nLCB0aXApLCAnVW5zdXBwb3J0ZWQgR2l0IG9iamVjdCBpZGVudGl0eS4nKQogICAgICAgIHJldHVybiB0aXAKCiAgICBkZWYgYXQoc2VsZiwgdGlwLCBwYXRoKToKICAgICAgICBsaXN0aW5nID0gc2VsZi5ydW4oJ2xzLXRyZWUnLCAnLXonLCB0aXAsICctLScsIHBhdGgpCiAgICAgICAgaWYgbm90IGxpc3Rpbmc6CiAgICAgICAgICAgIHJldHVybiBOb25lCiAgICAgICAgcGFydHMgPSBsaXN0aW5nLnNwbGl0KGInXDAnKQogICAgICAgIHJlcXVpcmUobGVuKHBhcnRzKSA9PSAyIGFuZCBwYXJ0c1sxXSA9PSBiJycsICdVbmV4cGVjdGVkIEdpdCB0cmVlIGVudHJ5LicpCiAgICAgICAgbWV0YWRhdGEsIGFjdHVhbCA9IHBhcnRzWzBdLnNwbGl0KGInXHQnLCAxKQogICAgICAgIG1vZGUsIGtpbmQsIG9pZCA9IG1ldGFkYXRhLnNwbGl0KGInICcpCiAgICAgICAgcmVxdWlyZShtb2RlID09IGInMTAwNjQ0JyBhbmQga2luZCA9PSBiJ2Jsb2InIGFuZCBhY3R1YWwuZGVjb2RlKCkgPT0gcGF0aCwgJ01lc3NhZ2UgaXMgbm90IGFuIG9yZGluYXJ5IGZpbGUuJykKICAgICAgICBzaXplID0gaW50KHNlbGYucnVuKCdjYXQtZmlsZScsICctcycsIG9pZC5kZWNvZGUoKSkpCiAgICAgICAgcmVxdWlyZSgwIDwgc2l6ZSA8PSBMSU1JVCwgJ01lc3NhZ2UgZXhjZWVkcyB0aGUgc2l6ZSBsaW1pdC4nKQogICAgICAgIGNvbnRlbnQgPSBzZWxmLnJ1bignY2F0LWZpbGUnLCAnYmxvYicsIG9pZC5kZWNvZGUoKSkKICAgICAgICByZXF1aXJlKGxlbihjb250ZW50KSA9PSBzaXplLCAnR2l0IG9iamVjdCBzaXplIG1pc21hdGNoLicpCiAgICAgICAgcmV0dXJuIGNvbnRlbnQKCiAgICBkZWYgcmVhZChzZWxmLCBraW5kLCBvcCk6CiAgICAgICAgcmV0dXJuIHNlbGYuYXQoc2VsZi5mZXRjaCgpLCBtZXNzYWdlX3BhdGgoc2VsZi5jLCBraW5kLCBvcCkpCgogICAgZGVmIGNyZWF0ZShzZWxmLCBraW5kLCBvcCwgZGF0YSk6CiAgICAgICAgcmVxdWlyZSgwIDwgbGVuKGRhdGEpIDw9IExJTUlULCAnTWVzc2FnZSBleGNlZWRzIHRoZSBzaXplIGxpbWl0LicpCiAgICAgICAgcGF0aCA9IG1lc3NhZ2VfcGF0aChzZWxmLmMsIGtpbmQsIG9wKQogICAgICAgIHRpcCA9IHNlbGYuZmV0Y2goKQogICAgICAgIHByZXZpb3VzID0gc2VsZi5hdCh0aXAsIHBhdGgpCiAgICAgICAgaWYgcHJldmlvdXMgaXMgbm90IE5vbmU6CiAgICAgICAgICAgIHJlcXVpcmUocHJldmlvdXMgPT0gZGF0YSwgJ0EgZGlmZmVyZW50IG1lc3NhZ2UgYWxyZWFkeSBleGlzdHMuIE92ZXJ3cml0aW5nIGlzIGZvcmJpZGRlbi4nKQogICAgICAgICAgICByZXR1cm4KICAgICAgICAjIEEgcHJpdmF0ZSBpbmRleCArIHBsdW1iaW5nIGxlYXZlcyB0aGUgdXNlcidzIGluZGV4LCB3b3JraW5nIHRyZWUgYW5kIGNoZWNrZWQtb3V0IGJyYW5jaCB1bnRvdWNoZWQuCiAgICAgICAgaW5kZXggPSBzZWxmLnByaXZhdGUgLyAoJ2luZGV4LScgKyBzZWNyZXRzLnRva2VuX2hleCg4KSkKICAgICAgICBlbnYgPSBvcy5lbnZpcm9uLmNvcHkoKQogICAgICAgIGVudi51cGRhdGUoR0lUX0lOREVYX0ZJTEU9c3RyKGluZGV4KSwgR0lUX0FVVEhPUl9OQU1FPSdTaHVuQ29kZXggY29sbGFib3JhdGlvbicsCiAgICAgICAgICAgICAgICAgICBHSVRfQVVUSE9SX0VNQUlMPSdjb2xsYWJvcmF0aW9uQGxvY2FsaG9zdCcsIEdJVF9DT01NSVRURVJfTkFNRT0nU2h1bkNvZGV4IGNvbGxhYm9yYXRpb24nLAogICAgICAgICAgICAgICAgICAgR0lUX0NPTU1JVFRFUl9FTUFJTD0nY29sbGFib3JhdGlvbkBsb2NhbGhvc3QnKQogICAgICAgIHRyeToKICAgICAgICAgICAgc2VsZi5ydW4oJ3JlYWQtdHJlZScsIHRpcCwgZW52PWVudikKICAgICAgICAgICAgYmxvYiA9IHNlbGYucnVuKCdoYXNoLW9iamVjdCcsICctdycsICctLXN0ZGluJywgZGF0YT1kYXRhLCBlbnY9ZW52KS5kZWNvZGUoKS5zdHJpcCgpCiAgICAgICAgICAgIHNlbGYucnVuKCd1cGRhdGUtaW5kZXgnLCAnLS1hZGQnLCAnLS1jYWNoZWluZm8nLCBmJzEwMDY0NCx7YmxvYn0se3BhdGh9JywgZW52PWVudikKICAgICAgICAgICAgdHJlZSA9IHNlbGYucnVuKCd3cml0ZS10cmVlJywgZW52PWVudikuZGVjb2RlKCkuc3RyaXAoKQogICAgICAgICAgICBjb21taXQgPSBzZWxmLnJ1bignY29tbWl0LXRyZWUnLCB0cmVlLCAnLXAnLCB0aXAsIGRhdGE9YidBZGQgY29sbGFib3JhdGlvbiBtZXNzYWdlXG4nLCBlbnY9ZW52KS5kZWNvZGUoKS5zdHJpcCgpCiAgICAgICAgICAgIHRyeToKICAgICAgICAgICAgICAgIHNlbGYucnVuKCdwdXNoJywgJy0tcXVpZXQnLCAnLS1uby12ZXJpZnknLCAnb3JpZ2luJywgZiJ7Y29tbWl0fTpyZWZzL2hlYWRzL3tzZWxmLmNbJ2JyYW5jaCddfSIsIGVudj1lbnYpCiAgICAgICAgICAgIGV4Y2VwdCAoU3RvcCwgc3VicHJvY2Vzcy5UaW1lb3V0RXhwaXJlZCk6CiAgICAgICAgICAgICAgICBvYnNlcnZlZCA9IHNlbGYuYXQoc2VsZi5mZXRjaCgpLCBwYXRoKSAgIyBPbmUgcmVhZC1vbmx5IHJlY29uY2lsaWF0aW9uLCBuZXZlciBhbm90aGVyIHB1c2guCiAgICAgICAgICAgICAgICByZXF1aXJlKG9ic2VydmVkID09IGRhdGEsICdQdWJsaWNhdGlvbiBvdXRjb21lIHVua25vd24uIEtlZXAgdGhlIHNhbWUgcGVuZGluZyBvcGVyYXRpb247IHVzZSBzdGF0dXMsIG5vdCBhbm90aGVyIGNhbGwuJykKICAgICAgICBmaW5hbGx5OgogICAgICAgICAgICBpbmRleC51bmxpbmsobWlzc2luZ19vaz1UcnVlKQogICAgICAgICAgICBQYXRoKHN0cihpbmRleCkgKyAnLmxvY2snKS51bmxpbmsobWlzc2luZ19vaz1UcnVlKQoKY2xhc3MgQXBpTWFpbGJveDoKICAgICIiIlNhbWUgY29udHJhY3QgYXMgR2l0TWFpbGJveCAoZmV0Y2gvYXQvcmVhZC9jcmVhdGUpIG92ZXIgdGhlIEdpdEh1YiBSRVNUIEFQSSwgZm9yIHNhbmRib3hlcyB3aXRob3V0IGEgZ2l0IGJpbmFyeS4KICAgIFVzZXMgYSB0b2tlbiBmcm9tIHRoZSBlbnZpcm9ubWVudCBvbmx5IChuZXZlciB3cml0dGVuIHRvIGRpc2sgb3IgbG9ncykuIENyZWF0ZSBuZXZlciBvdmVyd3JpdGVzOiBhIFBVVCB3aXRob3V0IHNoYSBpcwogICAgcmVqZWN0ZWQgYnkgR2l0SHViIHdoZW4gdGhlIHBhdGggYWxyZWFkeSBleGlzdHMsIG1hdGNoaW5nIHRoZSBnaXQgaW1wbGVtZW50YXRpb24ncyBuby1vdmVyd3JpdGUgcnVsZS4iIiIKICAgIEFQSSA9ICdodHRwczovL2FwaS5naXRodWIuY29tJwoKICAgIGRlZiBfX2luaXRfXyhzZWxmLCBjb25maWcsIHRva2VuKToKICAgICAgICByZXF1aXJlKGlzaW5zdGFuY2UodG9rZW4sIHN0cikgYW5kIDEgPD0gbGVuKHRva2VuKSA8PSA0MDk2IGFuZCBub3QgYW55KGNoLmlzc3BhY2UoKSBmb3IgY2ggaW4gdG9rZW4pLCAnR2l0SHViIHRva2VuIGlzIG1pc3Npbmcgb3IgbWFsZm9ybWVkLicpCiAgICAgICAgc2VsZi5jLCBzZWxmLl90b2tlbiA9IGNvbmZpZywgdG9rZW4KICAgICAgICByZXBvID0gY29uZmlnWydyZXBvc2l0b3J5J10KICAgICAgICByZXF1aXJlKHJlLmZ1bGxtYXRjaChyJ1tBLVphLXowLTldW0EtWmEtejAtOS1dKi9bQS1aYS16MC05Ll8tXSsnLCByZXBvKSwgJ1JlcG9zaXRvcnkgbmFtZSBpcyBpbnZhbGlkLicpCiAgICAgICAgc2VsZi5yZXBvID0gcmVwbwogICAgICAgIGluZm8gPSBzZWxmLl9qc29uKCdHRVQnLCBmJy9yZXBvcy97cmVwb30nKQogICAgICAgIHJlcXVpcmUoc3RyKGluZm8uZ2V0KCdpZCcpKSA9PSBzdHIoY29uZmlnLmdldCgncmVwb19pZCcpKSBhbmQgaW5mby5nZXQoJ3ByaXZhdGUnKSBpcyBUcnVlLCAnUmVwb3NpdG9yeSBpZGVudGl0eSBkb2VzIG5vdCBtYXRjaCB0aGlzIHNlc3Npb24uJykKCiAgICBkZWYgX2pzb24oc2VsZiwgbWV0aG9kLCBwYXRoLCBib2R5PU5vbmUsIG9rPSgyMDAsIDIwMSksIGFsbG93PSgpKToKICAgICAgICBkYXRhID0gTm9uZSBpZiBib2R5IGlzIE5vbmUgZWxzZSBqc29uLmR1bXBzKGJvZHkpLmVuY29kZSgpCiAgICAgICAgcmVxID0gdXJsbGliLnJlcXVlc3QuUmVxdWVzdChzZWxmLkFQSSArIHBhdGgsIGRhdGE9ZGF0YSwgbWV0aG9kPW1ldGhvZCwgaGVhZGVycz17CiAgICAgICAgICAgICdBdXRob3JpemF0aW9uJzogJ0JlYXJlciAnICsgc2VsZi5fdG9rZW4sICdBY2NlcHQnOiAnYXBwbGljYXRpb24vdm5kLmdpdGh1Yitqc29uJywKICAgICAgICAgICAgJ1gtR2l0SHViLUFwaS1WZXJzaW9uJzogJzIwMjItMTEtMjgnLCAnVXNlci1BZ2VudCc6ICdsZWJveC1hZ2VudC8xLjAnLAogICAgICAgICAgICAqKih7J0NvbnRlbnQtVHlwZSc6ICdhcHBsaWNhdGlvbi9qc29uJ30gaWYgZGF0YSBlbHNlIHt9KX0pCiAgICAgICAgdHJ5OgogICAgICAgICAgICB3aXRoIHVybGxpYi5yZXF1ZXN0LnVybG9wZW4ocmVxLCB0aW1lb3V0PTQwKSBhcyByZXNwOgogICAgICAgICAgICAgICAgc3RhdHVzLCByYXcgPSByZXNwLnN0YXR1cywgcmVzcC5yZWFkKDJfMDAwXzAwMCkKICAgICAgICBleGNlcHQgdXJsbGliLmVycm9yLkhUVFBFcnJvciBhcyBleDoKICAgICAgICAgICAgc3RhdHVzLCByYXcgPSBleC5jb2RlLCBleC5yZWFkKDIwMF8wMDApCiAgICAgICAgICAgIGlmIHN0YXR1cyBpbiBhbGxvdzoKICAgICAgICAgICAgICAgIHJldHVybiBzdGF0dXMsIChqc29uLmxvYWRzKHJhdykgaWYgcmF3IGVsc2Uge30pCiAgICAgICAgICAgIGlmIHN0YXR1cyA9PSA0MDE6CiAgICAgICAgICAgICAgICByZXF1aXJlKEZhbHNlLCAnR2l0SHViIHRva2VuIHJlamVjdGVkICg0MDEpLiBBc2sgdGhlIHVzZXIgZm9yIGEgdmFsaWQgdG9rZW47IG5vdGhpbmcgd2FzIHdyaXR0ZW4uJykKICAgICAgICAgICAgaWYgc3RhdHVzIGluICg0MDMsIDQyOSk6CiAgICAgICAgICAgICAgICByZXF1aXJlKEZhbHNlLCAnR2l0SHViIHJlZnVzZWQgb3IgcmF0ZS1saW1pdGVkIHRoZSByZXF1ZXN0ICg0MDMvNDI5KS4gQ2hlY2sgdG9rZW4gc2NvcGUgKENvbnRlbnRzOiByZWFkL3dyaXRlIG9uIHRoaXMgcmVwb3NpdG9yeSkgYW5kIHJldHJ5IGxhdGVyLicpCiAgICAgICAgICAgIGlmIHN0YXR1cyA9PSA0MDQ6CiAgICAgICAgICAgICAgICByZXF1aXJlKEZhbHNlLCAnUmVwb3NpdG9yeSwgYnJhbmNoIG9yIGZpbGUgbm90IHZpc2libGUgdG8gdGhpcyB0b2tlbiAoNDA0KS4nKQogICAgICAgICAgICByZXF1aXJlKEZhbHNlLCBmJ0dpdEh1YiByZXF1ZXN0IGZhaWxlZCAoe3N0YXR1c30pLicpCiAgICAgICAgZXhjZXB0ICh1cmxsaWIuZXJyb3IuVVJMRXJyb3IsIFRpbWVvdXRFcnJvciwgT1NFcnJvcik6CiAgICAgICAgICAgIHJlcXVpcmUoRmFsc2UsICdHaXRIdWIgaXMgdW5yZWFjaGFibGUgZnJvbSB0aGlzIHNhbmRib3g7IG5vIGF1dG9tYXRpYyByZXRyeS4nKQogICAgICAgIHJlcXVpcmUoc3RhdHVzIGluIG9rLCBmJ1VuZXhwZWN0ZWQgR2l0SHViIHN0YXR1cyB7c3RhdHVzfS4nKQogICAgICAgIHJldHVybiBqc29uLmxvYWRzKHJhdykgaWYgcmF3IGVsc2Uge30KCiAgICBkZWYgZmV0Y2goc2VsZik6CiAgICAgICAgcmVmID0gc2VsZi5fanNvbignR0VUJywgZiIvcmVwb3Mve3NlbGYucmVwb30vZ2l0L3JlZi9oZWFkcy97cXVvdGUoc2VsZi5jWydicmFuY2gnXSwgc2FmZT0nJyl9IikKICAgICAgICB0aXAgPSByZWYuZ2V0KCdvYmplY3QnLCB7fSkuZ2V0KCdzaGEnLCAnJykKICAgICAgICByZXF1aXJlKHJlLmZ1bGxtYXRjaCgnWzAtOWEtZl17NDB9JywgdGlwIG9yICcnKSwgJ1Vuc3VwcG9ydGVkIEdpdCBvYmplY3QgaWRlbnRpdHkuJykKICAgICAgICByZXR1cm4gdGlwCgogICAgZGVmIGF0KHNlbGYsIHRpcCwgcGF0aCk6CiAgICAgICAgcmVzdWx0ID0gc2VsZi5fanNvbignR0VUJywgZiIvcmVwb3Mve3NlbGYucmVwb30vY29udGVudHMve3F1b3RlKHBhdGgpfT9yZWY9e3RpcH0iLCBhbGxvdz0oNDA0LCkpCiAgICAgICAgaWYgaXNpbnN0YW5jZShyZXN1bHQsIHR1cGxlKToKICAgICAgICAgICAgcmV0dXJuIE5vbmUgICMgNDA0OiBub3QgcHVibGlzaGVkIHlldAogICAgICAgIGZpbGUgPSByZXN1bHQKICAgICAgICByZXF1aXJlKGZpbGUuZ2V0KCd0eXBlJykgPT0gJ2ZpbGUnIGFuZCBmaWxlLmdldCgncGF0aCcpID09IHBhdGggYW5kIGZpbGUuZ2V0KCdlbmNvZGluZycpID09ICdiYXNlNjQnLCAnTWVzc2FnZSBpcyBub3QgYW4gb3JkaW5hcnkgZmlsZS4nKQogICAgICAgIHNpemUgPSBpbnQoZmlsZS5nZXQoJ3NpemUnLCAtMSkpCiAgICAgICAgcmVxdWlyZSgwIDwgc2l6ZSA8PSBMSU1JVCwgJ01lc3NhZ2UgZXhjZWVkcyB0aGUgc2l6ZSBsaW1pdC4nKQogICAgICAgIGNvbnRlbnQgPSBiYXNlNjQuYjY0ZGVjb2RlKGZpbGUuZ2V0KCdjb250ZW50JywgJycpKQogICAgICAgIHJlcXVpcmUobGVuKGNvbnRlbnQpID09IHNpemUsICdHaXQgb2JqZWN0IHNpemUgbWlzbWF0Y2guJykKICAgICAgICByZXR1cm4gY29udGVudAoKICAgIGRlZiByZWFkKHNlbGYsIGtpbmQsIG9wKToKICAgICAgICByZXR1cm4gc2VsZi5hdChzZWxmLmZldGNoKCksIG1lc3NhZ2VfcGF0aChzZWxmLmMsIGtpbmQsIG9wKSkKCiAgICBkZWYgY3JlYXRlKHNlbGYsIGtpbmQsIG9wLCBkYXRhKToKICAgICAgICByZXF1aXJlKDAgPCBsZW4oZGF0YSkgPD0gTElNSVQsICdNZXNzYWdlIGV4Y2VlZHMgdGhlIHNpemUgbGltaXQuJykKICAgICAgICBwYXRoID0gbWVzc2FnZV9wYXRoKHNlbGYuYywga2luZCwgb3ApCiAgICAgICAgcHJldmlvdXMgPSBzZWxmLmF0KHNlbGYuZmV0Y2goKSwgcGF0aCkKICAgICAgICBpZiBwcmV2aW91cyBpcyBub3QgTm9uZToKICAgICAgICAgICAgcmVxdWlyZShwcmV2aW91cyA9PSBkYXRhLCAnQSBkaWZmZXJlbnQgbWVzc2FnZSBhbHJlYWR5IGV4aXN0cy4gT3ZlcndyaXRpbmcgaXMgZm9yYmlkZGVuLicpCiAgICAgICAgICAgIHJldHVybgogICAgICAgIGJvZHkgPSB7J21lc3NhZ2UnOiAnQWRkIGNvbGxhYm9yYXRpb24gbWVzc2FnZScsICdicmFuY2gnOiBzZWxmLmNbJ2JyYW5jaCddLCAnY29udGVudCc6IGJhc2U2NC5iNjRlbmNvZGUoZGF0YSkuZGVjb2RlKCl9CiAgICAgICAgcmVzdWx0ID0gc2VsZi5fanNvbignUFVUJywgZiIvcmVwb3Mve3NlbGYucmVwb30vY29udGVudHMve3F1b3RlKHBhdGgpfSIsIGJvZHk9Ym9keSwgb2s9KDIwMSwpLCBhbGxvdz0oNDA5LCA0MjIpKQogICAgICAgIGlmIGlzaW5zdGFuY2UocmVzdWx0LCB0dXBsZSk6CiAgICAgICAgICAgIG9ic2VydmVkID0gc2VsZi5hdChzZWxmLmZldGNoKCksIHBhdGgpICAjIE9uZSByZWFkLW9ubHkgcmVjb25jaWxpYXRpb24sIG5ldmVyIGFub3RoZXIgUFVULgogICAgICAgICAgICByZXF1aXJlKG9ic2VydmVkID09IGRhdGEsICdQdWJsaWNhdGlvbiBvdXRjb21lIHVua25vd24uIEtlZXAgdGhlIHNhbWUgcGVuZGluZyBvcGVyYXRpb247IHVzZSBzdGF0dXMsIG5vdCBhbm90aGVyIGNhbGwuJykKICAgICAgICAgICAgcmV0dXJuCiAgICAgICAgcmVxdWlyZShyZXN1bHQuZ2V0KCdjb250ZW50Jywge30pLmdldCgncGF0aCcpID09IHBhdGgsICdQdWJsaWNhdGlvbiByZWNlaXB0IGRvZXMgbm90IG1hdGNoIHRoZSBtZXNzYWdlLicpCgoKY2xhc3MgUHJpdmF0ZVN0YXRlOgogICAgZGVmIF9faW5pdF9fKHNlbGYsIHJvb3QsIGNvbmZpZywgcmVwbyk6CiAgICAgICAgcmVxdWlyZShub3Qgcm9vdC5pc19zeW1saW5rKCksICdQcml2YXRlIHN0YXRlIG11c3Qgbm90IGJlIGEgc3ltYm9saWMgbGluay4nKQogICAgICAgIHNlbGYucm9vdCA9IF9zdGF0ZV9ub19saW5rcyhyb290KS5yZXNvbHZlKCkKICAgICAgICByZXF1aXJlKHJlcG8gaXMgTm9uZSBvciBub3Qgc2VsZi5yb290LmlzX3JlbGF0aXZlX3RvKHJlcG8ucmVzb2x2ZSgpKSwgJ1ByaXZhdGUgc3RhdGUgbXVzdCBzdGF5IG91dHNpZGUgdGhlIHJlcG9zaXRvcnkuJykKICAgICAgICByb290Lm1rZGlyKHBhcmVudHM9VHJ1ZSwgZXhpc3Rfb2s9VHJ1ZSwgbW9kZT0wbzcwMCkKICAgICAgICBpZiBvcy5uYW1lICE9ICdudCcgYW5kIHNlbGYucm9vdC5zdGF0KCkuc3RfdWlkICE9IG9zLmdldHVpZCgpOgogICAgICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0RJUkVDVE9SWV9PV05FUl9NSVNNQVRDSCcpCiAgICAgICAgb3MuY2htb2Qoc2VsZi5yb290LCAwbzcwMCkKICAgICAgICBzZWxmLmZpbGUgPSBzZWxmLnJvb3QgLyAnc3RhdGUuanNvbicKICAgICAgICByZXF1aXJlKG5vdCBzZWxmLmZpbGUuaXNfc3ltbGluaygpLCAnUHJpdmF0ZSBzdGF0ZSBmaWxlIG11c3Qgbm90IGJlIGEgc3ltYm9saWMgbGluay4nKQogICAgICAgIHNlbGYuY29uZmlnX2hhc2ggPSBoYXNobGliLnNoYTI1NihjYW5vbmljYWwoY29uZmlnKSkuaGV4ZGlnZXN0KCkKICAgICAgICBzZWxmLnZhbHVlID0gbG9hZF9qc29uKF9zdGF0ZV9yZWFkKHNlbGYuZmlsZSksIGxpbWl0PTFfMjAwXzAwMCkgaWYgc2VsZi5maWxlLmV4aXN0cygpIGVsc2Ugeydjb25maWdfaGFzaCc6IHNlbGYuY29uZmlnX2hhc2gsICduZXh0JzogMH0KICAgICAgICByZXF1aXJlKHNlbGYudmFsdWUuZ2V0KCdjb25maWdfaGFzaCcpID09IHNlbGYuY29uZmlnX2hhc2gsICdQcml2YXRlIHN0YXRlIGJlbG9uZ3MgdG8gYW5vdGhlciBzZXNzaW9uLicpCgogICAgZGVmIHNhdmUoc2VsZik6CiAgICAgICAgZGF0YSA9IGNhbm9uaWNhbChzZWxmLnZhbHVlKQogICAgICAgIHJlcXVpcmUobGVuKGRhdGEpIDwgMV8yMDBfMDAwLCAnUHJpdmF0ZSBzZXNzaW9uIHN0YXRlIGlzIGZ1bGw7IGZpbmlzaCBvciBlbmQgdGhpcyBzZXNzaW9uLicpCiAgICAgICAgdG1wID0gc2VsZi5yb290IC8gKCdzdGF0ZS0nICsgc2VjcmV0cy50b2tlbl9oZXgoOCkpCiAgICAgICAgZmQgPSBvcy5vcGVuKHRtcCwgb3MuT19DUkVBVCB8IG9zLk9fRVhDTCB8IG9zLk9fV1JPTkxZLCAwbzYwMCkKICAgICAgICB0cnk6CiAgICAgICAgICAgIHdpdGggb3MuZmRvcGVuKGZkLCAnd2InKSBhcyBmaWxlOgogICAgICAgICAgICAgICAgZmlsZS53cml0ZShkYXRhKTsgZmlsZS5mbHVzaCgpOyBvcy5mc3luYyhmaWxlLmZpbGVubygpKQogICAgICAgICAgICBvcy5yZXBsYWNlKHRtcCwgc2VsZi5maWxlKQogICAgICAgICAgICBpZiBvcy5uYW1lICE9ICdudCc6CiAgICAgICAgICAgICAgICBkaXJlY3RvcnlfZmQgPSBvcy5vcGVuKHNlbGYucm9vdCwgb3MuT19SRE9OTFkgfCBnZXRhdHRyKG9zLCAnT19ESVJFQ1RPUlknLCAwKSkKICAgICAgICAgICAgICAgIHRyeTogb3MuZnN5bmMoZGlyZWN0b3J5X2ZkKQogICAgICAgICAgICAgICAgZmluYWxseTogb3MuY2xvc2UoZGlyZWN0b3J5X2ZkKQogICAgICAgIGZpbmFsbHk6CiAgICAgICAgICAgIHRtcC51bmxpbmsobWlzc2luZ19vaz1UcnVlKQoKQGNvbnRleHRsaWIuY29udGV4dG1hbmFnZXIKZGVmIGV4Y2x1c2l2ZShyb290KToKICAgICMgVXNlIHRoZSBzYW1lIGRlc2NyaXB0b3Ivb3duZXIvbW9kZSBjaGVja3MgYXMgcmVjb3Zlcnk7IGRvIG5vdCByZW9wZW4gYSB3ZWFrZXIgbG9jayBwYXRoLgogICAgd2l0aCBfc3RhdGVfbG9jayhyb290KToKICAgICAgICB5aWVsZAoKZGVmIGFjY2VwdF9oZWxsbyhjLCBzLCBwZWVyKToKICAgIGV4YWN0KHBlZXIsIFsndmVyc2lvbicsICdyb2xlJywgJ3BpbnMnLCAnY2hhbGxlbmdlJywgJ3B1YmxpY19rZXknXSkKICAgIHJlcXVpcmUocGVlclsndmVyc2lvbiddID09IFZFUlNJT04gYW5kIHBlZXJbJ3JvbGUnXSA9PSAnc2VydmVyJyBhbmQgcGVlclsncGlucyddID09IHBpbnMoYykKICAgICAgICAgICAgYW5kIGlzaW5zdGFuY2UocGVlclsnY2hhbGxlbmdlJ10sIHN0cikgYW5kIHJlLmZ1bGxtYXRjaCgnWzAtOWEtZl17MzJ9JywgcGVlclsnY2hhbGxlbmdlJ10pLCAnU2VydmVyIHBhaXJpbmcgaWRlbnRpdHkgbWlzbWF0Y2guJykKICAgIGV4YWN0KHBlZXJbJ3B1YmxpY19rZXknXSwgWydjcnYnLCAneCcsICd5J10pCiAgICByZXF1aXJlKHBlZXJbJ3B1YmxpY19rZXknXVsnY3J2J10gPT0gJ1AtMjU2JywgJ1Vuc3VwcG9ydGVkIGtleSB0eXBlLicpCiAgICBoYXNoZXMsIHNlcmlhbGl6YXRpb24sIGVjLCBIS0RGLCBfID0gY3J5cHRvKCkKICAgIHB1YmxpYyA9IGVjLkVsbGlwdGljQ3VydmVQdWJsaWNOdW1iZXJzKGludC5mcm9tX2J5dGVzKHVuYjY0KHBlZXJbJ3B1YmxpY19rZXknXVsneCddLCAzMiksICdiaWcnKSwKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBpbnQuZnJvbV9ieXRlcyh1bmI2NChwZWVyWydwdWJsaWNfa2V5J11bJ3knXSwgMzIpLCAnYmlnJyksIGVjLlNFQ1AyNTZSMSgpKS5wdWJsaWNfa2V5KCkKICAgIHByaXZhdGUgPSBzZXJpYWxpemF0aW9uLmxvYWRfZGVyX3ByaXZhdGVfa2V5KHVuYjY0KHMudmFsdWVbJ3ByaXZhdGUnXSksIHBhc3N3b3JkPU5vbmUpCiAgICB0cmFuc2NyaXB0ID0gaGFzaGxpYi5zaGEyNTYoY2Fub25pY2FsKHMudmFsdWVbJ2hlbGxvJ10pICsgY2Fub25pY2FsKHBlZXIpKS5kaWdlc3QoKQogICAgIyBFeHBsaWNpdCBjb3VudGVycGFydCBvZiAuTkVUIERlcml2ZUtleUZyb21IYXNoKHBlZXIsIFNIQTI1NikuCiAgICBzZWNyZXQgPSBoYXNobGliLnNoYTI1Nihwcml2YXRlLmV4Y2hhbmdlKGVjLkVDREgoKSwgcHVibGljKSkuZGlnZXN0KCkKICAgIGtleXMgPSBIS0RGKGFsZ29yaXRobT1oYXNoZXMuU0hBMjU2KCksIGxlbmd0aD0xMjgsIHNhbHQ9dHJhbnNjcmlwdCwgaW5mbz1iJ3NjeC1naC9tMi9rZXlzL3YxJykuZGVyaXZlKHNlY3JldCkKICAgIHMudmFsdWUudXBkYXRlKHNlcnZlcl9oZWxsbz1wZWVyLCB0cmFuc2NyaXB0PWI2NCh0cmFuc2NyaXB0KSwga2V5cz1iNjQoa2V5cykpCiAgICBzLnNhdmUoKQogICAgcmV0dXJuIHRyYW5zY3JpcHQuaGV4KCkudXBwZXIoKQoKZGVmIGNvbm5lY3QoYywgcywgYm94KToKICAgIGlmICdoZWxsbycgbm90IGluIHMudmFsdWU6CiAgICAgICAgXywgc2VyaWFsaXphdGlvbiwgZWMsIF8sIF8gPSBjcnlwdG8oKQogICAgICAgIHByaXZhdGUgPSBlYy5nZW5lcmF0ZV9wcml2YXRlX2tleShlYy5TRUNQMjU2UjEoKSkKICAgICAgICBwdWJsaWMgPSBwcml2YXRlLnB1YmxpY19rZXkoKS5wdWJsaWNfbnVtYmVycygpCiAgICAgICAgcy52YWx1ZVsncHJpdmF0ZSddID0gYjY0KHByaXZhdGUucHJpdmF0ZV9ieXRlcyhzZXJpYWxpemF0aW9uLkVuY29kaW5nLkRFUiwgc2VyaWFsaXphdGlvbi5Qcml2YXRlRm9ybWF0LlBLQ1M4LCBzZXJpYWxpemF0aW9uLk5vRW5jcnlwdGlvbigpKSkKICAgICAgICBzLnZhbHVlWydoZWxsbyddID0geyd2ZXJzaW9uJzogVkVSU0lPTiwgJ3JvbGUnOiAnY2xpZW50JywgJ3BpbnMnOiBwaW5zKGMpLCAnY2hhbGxlbmdlJzogc2VjcmV0cy50b2tlbl9oZXgoMTYpLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgJ3B1YmxpY19rZXknOiB7J2Nydic6ICdQLTI1NicsICd4JzogYjY0KHB1YmxpYy54LnRvX2J5dGVzKDMyLCAnYmlnJykpLCAneSc6IGI2NChwdWJsaWMueS50b19ieXRlcygzMiwgJ2JpZycpKX19CiAgICAgICAgcy5zYXZlKCkKICAgIHJhdyA9IGJveC5yZWFkKCdoZWxsbycsICdzZXJ2ZXInKQogICAgcmVxdWlyZShyYXcgaXMgbm90IE5vbmUsICdUaGUgbG9jYWwgc2Vzc2lvbiBpcyBub3QgcmVhZHkuIEtlZXAgdGhlIGFwcCBvcGVuOyBkbyBub3QgZ3Vlc3MgYW5vdGhlciBkaXJlY3RvcnkuJykKICAgIHBlZXIgPSBsb2FkX2pzb24ocmF3KQogICAgaWYgJ3NlcnZlcl9oZWxsbycgaW4gcy52YWx1ZToKICAgICAgICByZXF1aXJlKHBlZXIgPT0gcy52YWx1ZVsnc2VydmVyX2hlbGxvJ10sICdTZXJ2ZXIgcGFpcmluZyBkYXRhIGNoYW5nZWQuIFN0b3AgYW5kIHJlcXVlc3QgYSBuZXcgc2Vzc2lvbi4nKQogICAgY29kZSA9IGFjY2VwdF9oZWxsbyhjLCBzLCBwZWVyKQogICAgYm94LmNyZWF0ZSgnaGVsbG8nLCAnY2xpZW50JywgY2Fub25pY2FsKHMudmFsdWVbJ2hlbGxvJ10pKQogICAgcHJpbnQoJ+mFjeWvueefreegge+8micgKyBjb2RlWzo0XSArICcgJyArIGNvZGVbNDo4XSkKICAgIHByaW50KCflrozmlbTnoa7orqTnoIHvvJonICsgJyAnLmpvaW4oY29kZVtpOmkrNF0gZm9yIGkgaW4gcmFuZ2UoMCwgNjQsIDQpKSkKICAgIHByaW50KCfmioogOCDkvY3nn63noIHlkYror4nnlKjmiLfvvIznhLblkI7ov5DooYwgYWN0aXZhdGUgLS13YWl0IDkwMO+8muWug+S8muiHquWKqOetieW+heacrOacuuehruiupO+8iOS/oeS7u+S7k+W6k+aXtuaXoOmcgOeUqOaIt+aTjeS9nO+8ieW5tuWujOaIkOWIneWni+WMlu+8m+S5i+WQjueUqCBjYWxsIDzlt6XlhbflkI0+IC0tYXJndW1lbnRzIDxKU09OPiDosIPnlKjlt6XlhbfvvIznu5PmnpzkuI3mmI7ml7blj6rov5DooYwgc3RhdHVzIC0td2FpdCAzMDDvvIjkvJror53mmK/plb/mnJ/nmoTvvIznrYnlvoXlrqHmibnmnJ/pl7TkuI3opoHph43mlrDov57mjqXvvInjgIInKQoKZGVmIHNlYWwoYywgcywgb3BlcmF0aW9uLCBwYXlsb2FkLCBsaWZldGltZT0xODAwKToKICAgIF8sIF8sIF8sIF8sIEFFU0dDTSA9IGNyeXB0bygpCiAgICByZXF1aXJlKGxlbihwYXlsb2FkKSA8PSA2NTUzNiwgJ1JlcXVlc3QgZXhjZWVkcyB0aGUgbWVzc2FnZSBsaW1pdC4nKQogICAgbm9uY2UgPSBzZWNyZXRzLnRva2VuX2J5dGVzKDEyKQogICAgdXNlZCA9IHMudmFsdWUuc2V0ZGVmYXVsdCgndHhfbm9uY2VzJywgW10pCiAgICByZXF1aXJlKGI2NChub25jZSkgbm90IGluIHVzZWQsICdOb25jZSBjb2xsaXNpb247IHN0b3AgdGhpcyBzZXNzaW9uLicpCiAgICB1c2VkLmFwcGVuZChiNjQobm9uY2UpKQogICAgIyAzMC1taW51dGUgd2luZG93OiB0aGUgaG9zdCBtYXkgcmVhZCB0aGlzIG9ubHkgYWZ0ZXIgYSBsb25nIGFwcHJvdmFsIG9yIGEgR2l0SHViIG91dGFnZTsgcmVwbGF5IGlzIGJsb2NrZWQgYnkgaWRzL25vbmNlcy4KICAgIGhlYWRlciA9IGRpY3QocGlucyhjKSwgcmVxdWVzdF9pZD1vcGVyYXRpb24sIGRpcmVjdGlvbj0ncmVxJywgZXhwaXJlcz1pbnQodGltZS50aW1lKCkpICsgbGlmZXRpbWUpCiAgICBlbmNyeXB0ZWQgPSBBRVNHQ00odW5iNjQocy52YWx1ZVsna2V5cyddLCAxMjgpWzozMl0pLmVuY3J5cHQobm9uY2UsIHBheWxvYWQsIGNhbm9uaWNhbChoZWFkZXIpKQogICAgcmV0dXJuIHsnaGVhZGVyJzogaGVhZGVyLCAnbm9uY2UnOiBiNjQobm9uY2UpLCAnY2lwaGVydGV4dCc6IGI2NChlbmNyeXB0ZWRbOi0xNl0pLCAndGFnJzogYjY0KGVuY3J5cHRlZFstMTY6XSl9CgpkZWYgb3Blbl9yZXNwb25zZShjLCBzLCBvcGVyYXRpb24sIGVudik6CiAgICBleGFjdChlbnYsIFsnaGVhZGVyJywgJ25vbmNlJywgJ2NpcGhlcnRleHQnLCAndGFnJ10pCiAgICBleGFjdChlbnZbJ2hlYWRlciddLCBsaXN0KHBpbnMoYykpICsgWydyZXF1ZXN0X2lkJywgJ2RpcmVjdGlvbicsICdleHBpcmVzJ10pCiAgICBoID0gZW52WydoZWFkZXInXQogICAgcmVxdWlyZShhbGwoaFtrXSA9PSB2IGZvciBrLCB2IGluIHBpbnMoYykuaXRlbXMoKSkgYW5kIGhbJ3JlcXVlc3RfaWQnXSA9PSBvcGVyYXRpb24gYW5kIGhbJ2RpcmVjdGlvbiddID09ICdyZXMnLCAnUmVzcG9uc2UgYmluZGluZyBtaXNtYXRjaC4nKQogICAgcmVxdWlyZSh0eXBlKGhbJ2V4cGlyZXMnXSkgaXMgaW50IGFuZCB0aW1lLnRpbWUoKSAtIDEgPD0gaFsnZXhwaXJlcyddIDw9IHRpbWUudGltZSgpICsgMTgwMSwgJ1Jlc3BvbnNlIGV4cGlyZWQ7IGRvIG5vdCByZXBlYXQgdGhlIG9wZXJhdGlvbi4nKQogICAgbm9uY2UgPSB1bmI2NChlbnZbJ25vbmNlJ10sIDEyKQogICAgcmVxdWlyZShiNjQobm9uY2UpIG5vdCBpbiBzLnZhbHVlLmdldCgncnhfbm9uY2VzJywgW10pLCAnUmVzcG9uc2Ugbm9uY2Ugd2FzIGFscmVhZHkgYWNjZXB0ZWQuJykKICAgIF8sIF8sIF8sIF8sIEFFU0dDTSA9IGNyeXB0bygpCiAgICBjaXBoZXJ0ZXh0ID0gdW5iNjQoZW52WydjaXBoZXJ0ZXh0J10pCiAgICByZXF1aXJlKGxlbihjaXBoZXJ0ZXh0KSA8PSA2NTUzNiwgJ1Jlc3BvbnNlIGV4Y2VlZHMgdGhlIGxpbWl0LicpCiAgICBwbGFpbiA9IEFFU0dDTSh1bmI2NChzLnZhbHVlWydrZXlzJ10sIDEyOClbMzI6NjRdKS5kZWNyeXB0KG5vbmNlLCBjaXBoZXJ0ZXh0ICsgdW5iNjQoZW52Wyd0YWcnXSwgMTYpLCBjYW5vbmljYWwoaCkpCiAgICBpZiBwbGFpbi5zdGFydHN3aXRoKGInU0NYMlpcMCcpOgogICAgICAgIGRlY29kZXIgPSB6bGliLmRlY29tcHJlc3NvYmooKQogICAgICAgIHBsYWluID0gZGVjb2Rlci5kZWNvbXByZXNzKHBsYWluWzY6XSwgMV8wMDBfMDAxKQogICAgICAgIHJlcXVpcmUobGVuKHBsYWluKSA8PSAxXzAwMF8wMDAgYW5kIGRlY29kZXIuZW9mIGFuZCBub3QgZGVjb2Rlci51bnVzZWRfZGF0YSBhbmQgbm90IGRlY29kZXIudW5jb25zdW1lZF90YWlsLAogICAgICAgICAgICAgICAgJ0NvbXByZXNzZWQgcmVzdWx0IGV4Y2VlZHMgaXRzIGxpbWl0IG9yIGlzIG1hbGZvcm1lZC4nKQogICAgcmVzdWx0ID0gbG9hZF9qc29uKHBsYWluLCBsaW1pdD0xXzAwMF8wMDApCiAgICBleGFjdChyZXN1bHQsIFsncHJvamVjdF9pZCcsICdyZXBseSddKQogICAgcmVxdWlyZShyZXN1bHRbJ3Byb2plY3RfaWQnXSA9PSBjWydwcm9qZWN0X2lkJ10sICdUaGUgcmVzcG9uZGluZyBwcm9qZWN0IGlzIG5vdCB0aGUgc2VsZWN0ZWQgcHJvamVjdC4nKQogICAgcy52YWx1ZS5zZXRkZWZhdWx0KCdyeF9ub25jZXMnLCBbXSkuYXBwZW5kKGI2NChub25jZSkpCiAgICByZXR1cm4gcmVzdWx0WydyZXBseSddCgpkZWYgYXV0aGVudGljYXRlX2V4cGlyZWRfcmVuZXdhbChjLCBzLCBvcGVyYXRpb24sIGVudmVsb3BlKToKICAgICIiIk9ubHkgY2xhc3NpZnkgYW4gYXV0aGVudGljYXRlZCBleHBpcmVkIHRva2VuIHNsb3QuIE5ldmVyIGluc3RhbGwgaXRzIHRva2VuIG9yIGFjY2VwdCBpdHMgb3duZXJzaGlwIHByb29mLiIiIgogICAgdHJ5OgogICAgICAgIHJlcXVpcmUocmUuZnVsbG1hdGNoKHIndG9rZW4tWzAtOV17Nn0nLCBvcGVyYXRpb24pLCAnTm90IGEgcmVuZXdhbCBoaXN0b3J5IHNsb3QuJykKICAgICAgICBleGFjdChlbnZlbG9wZSwgWydoZWFkZXInLCAnbm9uY2UnLCAnY2lwaGVydGV4dCcsICd0YWcnXSkKICAgICAgICBoID0gZW52ZWxvcGVbJ2hlYWRlciddCiAgICAgICAgZXhhY3QoaCwgbGlzdChwaW5zKGMpKSArIFsncmVxdWVzdF9pZCcsICdkaXJlY3Rpb24nLCAnZXhwaXJlcyddKQogICAgICAgIHJlcXVpcmUoYWxsKGhba10gPT0gdiBmb3IgaywgdiBpbiBwaW5zKGMpLml0ZW1zKCkpIGFuZCBoWydyZXF1ZXN0X2lkJ10gPT0gb3BlcmF0aW9uIGFuZCBoWydkaXJlY3Rpb24nXSA9PSAncmVzJywgJ1JlbmV3YWwgaGlzdG9yeSBiaW5kaW5nIG1pc21hdGNoLicpCiAgICAgICAgcmVxdWlyZSh0eXBlKGhbJ2V4cGlyZXMnXSkgaXMgaW50IGFuZCAwIDwgaFsnZXhwaXJlcyddIDwgdGltZS50aW1lKCkgLSAxLCAnTm90IGV4cGlyZWQgcmVuZXdhbCBoaXN0b3J5LicpCiAgICAgICAgbm9uY2UgPSB1bmI2NChlbnZlbG9wZVsnbm9uY2UnXSwgMTIpCiAgICAgICAgcmVxdWlyZShiNjQobm9uY2UpIG5vdCBpbiBzLnZhbHVlLmdldCgncnhfbm9uY2VzJywgW10pLCAnUmVuZXdhbCBoaXN0b3J5IG5vbmNlIGFscmVhZHkgY29uc3VtZWQuJykKICAgICAgICBjaXBoZXJ0ZXh0ID0gdW5iNjQoZW52ZWxvcGVbJ2NpcGhlcnRleHQnXSkKICAgICAgICByZXF1aXJlKGxlbihjaXBoZXJ0ZXh0KSA8PSA2NTUzNiwgJ1JlbmV3YWwgaGlzdG9yeSBleGNlZWRzIGxpbWl0LicpCiAgICAgICAgXywgXywgXywgXywgQUVTR0NNID0gY3J5cHRvKCkKICAgICAgICBwbGFpbiA9IEFFU0dDTSh1bmI2NChzLnZhbHVlWydrZXlzJ10sIDEyOClbMzI6NjRdKS5kZWNyeXB0KG5vbmNlLCBjaXBoZXJ0ZXh0ICsgdW5iNjQoZW52ZWxvcGVbJ3RhZyddLCAxNiksIGNhbm9uaWNhbChoKSkKICAgICAgICBib2R5ID0gbG9hZF9qc29uKHBsYWluKQogICAgICAgIGV4YWN0KGJvZHksIFsncHJvamVjdF9pZCcsICdyZXBseSddKQogICAgICAgIHRva2VuID0gYm9keVsncmVwbHknXS5nZXQoJ3Rva2VuJykgaWYgaXNpbnN0YW5jZShib2R5WydyZXBseSddLCBkaWN0KSBlbHNlIE5vbmUKICAgICAgICByZXF1aXJlKGJvZHlbJ3Byb2plY3RfaWQnXSA9PSBjWydwcm9qZWN0X2lkJ10gYW5kIGlzaW5zdGFuY2UodG9rZW4sIHN0cikgYW5kIDggPD0gbGVuKHRva2VuKSA8PSA0MDk2CiAgICAgICAgICAgICAgICBhbmQgbm90IGFueSh4Lmlzc3BhY2UoKSBmb3IgeCBpbiB0b2tlbiksICdJbnZhbGlkIHJlbmV3YWwgaGlzdG9yeS4nKQogICAgICAgIHMudmFsdWUuc2V0ZGVmYXVsdCgncnhfbm9uY2VzJywgW10pLmFwcGVuZChiNjQobm9uY2UpKQogICAgICAgIHJldHVybiBUcnVlCiAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgIHJldHVybiBGYWxzZSAgIyBVbmF1dGhlbnRpY2F0ZWQvbWFsZm9ybWVkIG9jY3VwYW5jeSBtdXN0IG5ldmVyIGFkdmFuY2UgdGhlIGN1cnNvci4KCgpkZWYgYXBwbHlfdG9rZW5fcmVuZXcoYywgcywgYm94LCB0aXA9Tm9uZSk6CiAgICAiIiJUaGUgZGVza3RvcCBwdWJsaXNoZXMgcmVuZXcvdG9rZW4tTk5OLmpzb24gKHNlYWxlZCBsaWtlIGEgcmVzcG9uc2Ugd2l0aCByZXF1ZXN0X2lkICd0b2tlbi1OTk4nKSBiZWZvcmUgdGhlIHBhc3RlZAogICAgdG9rZW4gZXhwaXJlcy4gT25seSBtZWFuaW5nZnVsIHdoZW4gdGhpcyBBZ2VudCBhdXRoZW50aWNhdGVkIHdpdGggYSBwYXN0ZWQgdG9rZW47IG90aGVyd2lzZSBpZ25vcmVkLiBJZGVtcG90ZW50IGJ5IGluZGV4LiIiIgogICAgaWYgb3MuZW52aXJvbi5nZXQoJ0xFQk9YX0FVVEhfU09VUkNFJywgb3MuZW52aXJvbi5nZXQoJ1NDWF9BVVRIX1NPVVJDRScpKSBub3QgaW4gKCdwYXN0ZWQtdG9rZW4nLCAnZGV2aWNlLWZsb3cnKSBhbmQgbm90IHMudmFsdWUuZ2V0KCdyZW5ld2VkJykgYW5kIG5vdCBzLnZhbHVlLmdldCgncmVjb3ZlcnlfY29udGV4dCcpOgogICAgICAgIHJldHVybgogICAgZm9yIF8gaW4gcmFuZ2UoMTYpOgogICAgICAgIGluZGV4ID0gcy52YWx1ZS5nZXQoJ3JlbmV3X2luZGV4JywgMCkKICAgICAgICByZXF1aXJlKHR5cGUoaW5kZXgpIGlzIGludCBhbmQgMCA8PSBpbmRleCA8IDFfMDAwXzAwMCwgJ1JlbmV3YWwgY3Vyc29yIGlzIGludmFsaWQgb3IgZXhoYXVzdGVkLicpCiAgICAgICAgb3AgPSAndG9rZW4tJTA2ZCcgJSBpbmRleAogICAgICAgIHRyeToKICAgICAgICAgICAgcmF3ID0gYm94LmF0KHRpcCwgbWVzc2FnZV9wYXRoKGMsICdyZW5ldycsIG9wKSkgaWYgdGlwIGVsc2UgYm94LnJlYWQoJ3JlbmV3Jywgb3ApCiAgICAgICAgZXhjZXB0IEV4Y2VwdGlvbjoKICAgICAgICAgICAgcmV0dXJuCiAgICAgICAgaWYgcmF3IGlzIE5vbmU6CiAgICAgICAgICAgIHJldHVybgogICAgICAgIGVudmVsb3BlID0gbG9hZF9qc29uKHJhdykKICAgICAgICB0cnk6CiAgICAgICAgICAgIHJlcGx5ID0gb3Blbl9yZXNwb25zZShjLCBzLCBvcCwgZW52ZWxvcGUpCiAgICAgICAgICAgIGV4cGlyeSA9IHJlcGx5LmdldCgnZXhwaXJlc19hdCcsIDApIGlmIGlzaW5zdGFuY2UocmVwbHksIGRpY3QpIGVsc2UgTm9uZQogICAgICAgICAgICByZXF1aXJlKHR5cGUoZXhwaXJ5KSBpcyBpbnQgYW5kIGV4cGlyeSA+PSAwLCAnSW52YWxpZCByZW5ld2FsIGNyZWRlbnRpYWwgZXhwaXJ5LicpCiAgICAgICAgICAgIGlmIGV4cGlyeSBhbmQgZXhwaXJ5IDw9IHRpbWUudGltZSgpOgogICAgICAgICAgICAgICAgcy52YWx1ZVsncmVuZXdfaW5kZXgnXSA9IGluZGV4ICsgMQogICAgICAgICAgICAgICAgcy5zYXZlKCkgICMgRnJlc2ggd3JhcHBlciBkb2VzIG5vdCBtYWtlIGFuIGV4cGlyZWQgY3JlZGVudGlhbCB1c2FibGUuCiAgICAgICAgICAgICAgICBjb250aW51ZQogICAgICAgICAgICBicmVhawogICAgICAgIGV4Y2VwdCBTdG9wOgogICAgICAgICAgICBpZiBub3QgYXV0aGVudGljYXRlX2V4cGlyZWRfcmVuZXdhbChjLCBzLCBvcCwgZW52ZWxvcGUpOgogICAgICAgICAgICAgICAgcmV0dXJuCiAgICAgICAgICAgIHMudmFsdWVbJ3JlbmV3X2luZGV4J10gPSBpbmRleCArIDEKICAgICAgICAgICAgcy5zYXZlKCkgICMgQXV0aGVudGljYXRlZCBleHBpcmVkIGhpc3RvcnkgaXMgc2tpcHBlZCwgbm90IHVzZWQgYXMgYSBjcmVkZW50aWFsIG9yIG93bmVyc2hpcCBncmFudC4KICAgIGVsc2U6CiAgICAgICAgcmV0dXJuCiAgICB0b2tlbiA9IHJlcGx5LmdldCgndG9rZW4nLCAnJykgaWYgaXNpbnN0YW5jZShyZXBseSwgZGljdCkgZWxzZSAnJwogICAgaWYgbm90IChpc2luc3RhbmNlKHRva2VuLCBzdHIpIGFuZCA4IDw9IGxlbih0b2tlbikgPD0gNDA5NiBhbmQgbm90IGFueShjaC5pc3NwYWNlKCkgZm9yIGNoIGluIHRva2VuKSk6CiAgICAgICAgcmV0dXJuCiAgICBvd25lcnNoaXAgPSByZXBseS5nZXQoJ3JlY292ZXJ5X293bmVyc2hpcCcpCiAgICBpZiBvd25lcnNoaXAgaXMgbm90IE5vbmU6CiAgICAgICAgdmFsaWRhdGVfcmVjb3Zlcnlfb3duZXJzaGlwKGMsIHMsIG93bmVyc2hpcCkKICAgICAgICBzLnZhbHVlWydyZWNvdmVyeV9vd25lcnNoaXAnXSA9IG93bmVyc2hpcAogICAgcy52YWx1ZVsncmVuZXdfaW5kZXgnXSA9IGluZGV4ICsgMQogICAgcy52YWx1ZVsncmVuZXdlZCddID0gVHJ1ZQogICAgcy52YWx1ZVsncmVuZXdlZF90b2tlbiddID0gdG9rZW4KICAgIHMuc2F2ZSgpCiAgICBvcy5lbnZpcm9uWydHSVRIVUJfVE9LRU4nXSA9IHRva2VuCiAgICBvcy5lbnZpcm9uWydMRUJPWF9UT0tFTiddID0gdG9rZW4KICAgIGlmIGlzaW5zdGFuY2UoYm94LCBHaXRNYWlsYm94KToKICAgICAgICBvcy5lbnZpcm9uLnVwZGF0ZShfZ2l0X2F1dGhfcmVhZHlfZW52aXJvbm1lbnQodG9rZW4sIGNbJ3JlcG9zaXRvcnknXSkpCiAgICBpZiBpc2luc3RhbmNlKGJveCwgQXBpTWFpbGJveCk6CiAgICAgICAgYm94Ll90b2tlbiA9IHRva2VuCiAgICBwcmludChqc29uLmR1bXBzKHsnc3RhdHVzJzogJ3Rva2VuX3JlbmV3ZWQnLCAnZXhwaXJlc19hdCc6IHJlcGx5LmdldCgnZXhwaXJlc19hdCcsIDApfSksIGZpbGU9c3lzLnN0ZGVycikKICAgIHJldHJ5X3JlY292ZXJ5X3B1YmxpY2F0aW9uKGMsIHMpCgoKIyBMQlIxIGlzIGVtYmVkZGVkIGluIHRoaXMgcGlubmVkIGNsaWVudDogbmV2ZXIgZXhlY3V0ZSBhIHJlY292ZXJ5IGhlbHBlciBmcm9tIGN3ZC9QWVRIT05QQVRILgpkZWYgcmVjb3Zlcnlfa2V5KHRleHQpOgogICAgbWF0Y2ggPSByZS5mdWxsbWF0Y2gocicoPzpMRUJPWF9SRUNPVkVSWT0pP2xlYm94MS0oWzAtOWEtZl17MzJ9KS0oW0EtWmEtejAtOV8tXXs0M30pJywgdGV4dC5zdHJpcCgpKQogICAgcmVxdWlyZShtYXRjaCBpcyBub3QgTm9uZSwgJ1JFQ09WRVJZX0tFWV9GT1JNQVQnKQogICAga2V5ID0gYmFzZTY0LnVybHNhZmVfYjY0ZGVjb2RlKG1hdGNoWzJdICsgJz0nKQogICAgcmVxdWlyZShsZW4oa2V5KSA9PSAzMiBhbmQgYmFzZTY0LnVybHNhZmVfYjY0ZW5jb2RlKGtleSkucnN0cmlwKGInPScpLmRlY29kZSgpID09IG1hdGNoWzJdLCAnUkVDT1ZFUllfS0VZX0ZPUk1BVCcpCiAgICByZXR1cm4gbWF0Y2hbMV0sIGtleQoKCmRlZiB2YWxpZGF0ZV9yZWNvdmVyeV9vd25lcnNoaXAoYywgcywgcHJvb2YpOgogICAgZXhhY3QocHJvb2YsIFsnZm9ybWF0JywgJ3Byb2plY3RfaWQnLCAncmVwb3NpdG9yeScsICdyZXBvX2lkJywgJ2JyYW5jaCcsICdiaW5kaW5nJywgJ2Vwb2NoJywgJ3Rvb2xzJywgJ3JpZCcsICdjaXBoZXJ0ZXh0X3NoYTI1NiddKQogICAgcmVxdWlyZShwcm9vZlsnZm9ybWF0J10gPT0gJ2xlYm94LWFnZW50LW93bmVyc2hpcC12MScsICdSRUNPVkVSWV9PV05FUlNISVBfRk9STUFUJykKICAgIGNvbnRleHQgPSBzLnZhbHVlLmdldCgncmVjb3ZlcnlfY29udGV4dCcsIHt9KQogICAgcmVjID0gY29udGV4dC5nZXQoJ3JlY292ZXJ5Jykgb3Igb3MuZW52aXJvbi5nZXQoJ0xFQk9YX1JFQ09WRVJZX1NFTEYnLCAnJykKICAgIHRvb2xzID0gY29udGV4dC5nZXQoJ3Rvb2xzJykgb3Igb3MuZW52aXJvbi5nZXQoJ0xFQk9YX1RPT0xTJywgJycpCiAgICByaWQsIF8gPSByZWNvdmVyeV9rZXkocmVjKQogICAgcmVxdWlyZShhbGwocHJvb2ZbZmllbGRdID09IGNbZmllbGRdIGZvciBmaWVsZCBpbiAoJ3Byb2plY3RfaWQnLCAncmVwb3NpdG9yeScsICdyZXBvX2lkJywgJ2JyYW5jaCcsICdiaW5kaW5nJywgJ2Vwb2NoJykpCiAgICAgICAgICAgIGFuZCBwcm9vZlsndG9vbHMnXSA9PSB0b29scyBhbmQgcHJvb2ZbJ3JpZCddID09IHJpZCBhbmQgaXNpbnN0YW5jZShwcm9vZlsnY2lwaGVydGV4dF9zaGEyNTYnXSwgc3RyKQogICAgICAgICAgICBhbmQgcmUuZnVsbG1hdGNoKCdbYS1mMC05XXs2NH0nLCBwcm9vZlsnY2lwaGVydGV4dF9zaGEyNTYnXSksICdSRUNPVkVSWV9PV05FUlNISVBfU0NPUEUnKQoKCmRlZiByZWNvdmVyeV9sZWdhY3lfb3duZWQoYywgcywgb2xkLCBjaXBoZXJ0ZXh0KToKICAgIGlmIG9sZC5nZXQoJ2J5JykgPT0gJ2FnZW50JzoKICAgICAgICByZXR1cm4gVHJ1ZQogICAgIyBBbiBleHBsaWNpdCBkaWZmZXJlbnQgb3duZXIgY2FuIG5ldmVyIGJlIHJlY2xhc3NpZmllZCwgZXZlbiB3aXRoIGFuIG9sZGVyIGF0dGVzdGF0aW9uLgogICAgaWYgJ2J5JyBpbiBvbGQ6CiAgICAgICAgcmV0dXJuIEZhbHNlCiAgICBwcm9vZiA9IHMudmFsdWUuZ2V0KCdyZWNvdmVyeV9vd25lcnNoaXAnKQogICAgaWYgcHJvb2YgaXMgTm9uZToKICAgICAgICByZXR1cm4gRmFsc2UKICAgIHZhbGlkYXRlX3JlY292ZXJ5X293bmVyc2hpcChjLCBzLCBwcm9vZikKICAgIHJldHVybiBobWFjLmNvbXBhcmVfZGlnZXN0KHByb29mWydjaXBoZXJ0ZXh0X3NoYTI1NiddLCBoYXNobGliLnNoYTI1NihjaXBoZXJ0ZXh0KS5oZXhkaWdlc3QoKSkKCgpkZWYgcmVjb3ZlcnlfY2lwaGVyKGtleSwgdmFsdWUsIGRlY3J5cHQ9RmFsc2UpOgogICAgZW5jID0gaG1hYy5uZXcoa2V5LCBiJ2xlYm94LXJlY292ZXJ5LWVuYycsIGhhc2hsaWIuc2hhMjU2KS5kaWdlc3QoKQogICAgbWFjID0gaG1hYy5uZXcoa2V5LCBiJ2xlYm94LXJlY292ZXJ5LW1hYycsIGhhc2hsaWIuc2hhMjU2KS5kaWdlc3QoKQogICAgaWYgZGVjcnlwdDoKICAgICAgICByZXF1aXJlKDUyIDw9IGxlbih2YWx1ZSkgPD0gMTZfMDAwIGFuZCB2YWx1ZVs6NF0gPT0gYidMQlIxJywgJ1JFQ09WRVJZX0VOVkVMT1BFJykKICAgICAgICByZXF1aXJlKGhtYWMuY29tcGFyZV9kaWdlc3QodmFsdWVbLTMyOl0sIGhtYWMubmV3KG1hYywgdmFsdWVbOi0zMl0sIGhhc2hsaWIuc2hhMjU2KS5kaWdlc3QoKSksICdSRUNPVkVSWV9PV05FUl9VTlZFUklGSUVEJykKICAgICAgICBub25jZSwgZGF0YSA9IHZhbHVlWzQ6MjBdLCB2YWx1ZVsyMDotMzJdCiAgICBlbHNlOgogICAgICAgIHJlcXVpcmUobGVuKHZhbHVlKSA8PSAxNV85NDgsICdSRUNPVkVSWV9FTlZFTE9QRScpCiAgICAgICAgbm9uY2UsIGRhdGEgPSBzZWNyZXRzLnRva2VuX2J5dGVzKDE2KSwgdmFsdWUKICAgIHN0cmVhbSA9IGInJy5qb2luKGhtYWMubmV3KGVuYywgbm9uY2UgKyBpLnRvX2J5dGVzKDQsICdiaWcnKSwgaGFzaGxpYi5zaGEyNTYpLmRpZ2VzdCgpIGZvciBpIGluIHJhbmdlKChsZW4oZGF0YSkgKyAzMSkgLy8gMzIpKQogICAgdHJhbnNmb3JtZWQgPSBieXRlcyhhIF4gYiBmb3IgYSwgYiBpbiB6aXAoZGF0YSwgc3RyZWFtKSkKICAgIGlmIGRlY3J5cHQ6CiAgICAgICAgcmV0dXJuIHRyYW5zZm9ybWVkCiAgICBib2R5ID0gYidMQlIxJyArIG5vbmNlICsgdHJhbnNmb3JtZWQKICAgIHJldHVybiBib2R5ICsgaG1hYy5uZXcobWFjLCBib2R5LCBoYXNobGliLnNoYTI1NikuZGlnZXN0KCkKCgpjbGFzcyBfUmVjb3ZlcnlOb1JlZGlyZWN0KHVybGxpYi5yZXF1ZXN0LkhUVFBSZWRpcmVjdEhhbmRsZXIpOgogICAgZGVmIHJlZGlyZWN0X3JlcXVlc3Qoc2VsZiwgKmFyZ3MsICoqa3dhcmdzKToKICAgICAgICByYWlzZSBTdG9wKCdSRUNPVkVSWV9SRURJUkVDVF9SRUZVU0VEJykKCgpkZWYgcmVjb3ZlcnlfaHR0cCh0b2tlbiwgcGF0aCwgYm9keT1Ob25lKToKICAgIGhlYWRlcnMgPSB7J0F1dGhvcml6YXRpb24nOiAnQmVhcmVyICcgKyB0b2tlbiwgJ0FjY2VwdCc6ICdhcHBsaWNhdGlvbi92bmQuZ2l0aHViK2pzb24nLAogICAgICAgICAgICAgICAnWC1HaXRIdWItQXBpLVZlcnNpb24nOiAnMjAyMi0xMS0yOCcsICdVc2VyLUFnZW50JzogJ2xlYm94LWFnZW50LzEuMCcsICdDb250ZW50LVR5cGUnOiAnYXBwbGljYXRpb24vanNvbid9CiAgICByZXF1ZXN0ID0gdXJsbGliLnJlcXVlc3QuUmVxdWVzdCgnaHR0cHM6Ly9hcGkuZ2l0aHViLmNvbScgKyBwYXRoLCBoZWFkZXJzPWhlYWRlcnMsCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIGRhdGE9Tm9uZSBpZiBib2R5IGlzIE5vbmUgZWxzZSBjYW5vbmljYWwoYm9keSksIG1ldGhvZD0nR0VUJyBpZiBib2R5IGlzIE5vbmUgZWxzZSAnUFVUJykKICAgIG9wZW5lciA9IHVybGxpYi5yZXF1ZXN0LmJ1aWxkX29wZW5lcihfUmVjb3ZlcnlOb1JlZGlyZWN0KCkpCiAgICB3aXRoIG9wZW5lci5vcGVuKHJlcXVlc3QsIHRpbWVvdXQ9MzApIGFzIHJlc3BvbnNlOgogICAgICAgIHJlcXVpcmUocmVzcG9uc2Uuc3RhdHVzIGluICgoMjAwLCkgaWYgYm9keSBpcyBOb25lIGVsc2UgKDIwMCwgMjAxKSksICdSRUNPVkVSWV9IVFRQX1NUQVRVUycpCiAgICAgICAgcmV0dXJuIGxvYWRfanNvbihyZXNwb25zZS5yZWFkKDEwMF8wMDEpKQoKCmRlZiByZWNvdmVyeV9yZW1vdGUodG9rZW4sIHRvb2xzLCByaWQpOgogICAgIyBCaW5kIGEgcHVibGljIHJlcG9zaXRvcnkgYW5kIGV4YWN0IGRlZmF1bHQtYnJhbmNoIGNvbW1pdC4gQW4gZXJyb3IvNDA0IGlzIE5FVkVSIGludGVycHJldGVkIGFzIGFic2VuY2UuCiAgICByZXBvc2l0b3J5ID0gcmVjb3ZlcnlfaHR0cCh0b2tlbiwgJy9yZXBvcy8nICsgdG9vbHMpCiAgICByZXF1aXJlKHJlcG9zaXRvcnkuZ2V0KCdwcml2YXRlJykgaXMgRmFsc2UgYW5kIHJlcG9zaXRvcnkuZ2V0KCdmdWxsX25hbWUnLCAnJykubG93ZXIoKSA9PSB0b29scy5sb3dlcigpCiAgICAgICAgICAgIGFuZCB0eXBlKHJlcG9zaXRvcnkuZ2V0KCdpZCcpKSBpcyBpbnQgYW5kIHJlcG9zaXRvcnlbJ2lkJ10gPiAwLCAnUkVDT1ZFUllfUkVQT1NJVE9SWScpCiAgICBicmFuY2ggPSByZXBvc2l0b3J5LmdldCgnZGVmYXVsdF9icmFuY2gnLCAnJykKICAgIHJlcXVpcmUoaXNpbnN0YW5jZShicmFuY2gsIHN0cikgYW5kIDEgPD0gbGVuKGJyYW5jaCkgPD0gMjAwIGFuZCByZS5mdWxsbWF0Y2gocidbQS1aYS16MC05Ll8vLV0rJywgYnJhbmNoKQogICAgICAgICAgICBhbmQgJy4uJyBub3QgaW4gYnJhbmNoIGFuZCBhbGwocCBhbmQgbm90IHAuc3RhcnRzd2l0aCgnLicpIGFuZCBub3QgcC5lbmRzd2l0aCgnLmxvY2snKSBmb3IgcCBpbiBicmFuY2guc3BsaXQoJy8nKSksICdSRUNPVkVSWV9CUkFOQ0gnKQogICAgcmVmID0gcmVjb3ZlcnlfaHR0cCh0b2tlbiwgJy9yZXBvcy8lcy9naXQvcmVmL2hlYWRzLyVzJyAlICh0b29scywgcXVvdGUoYnJhbmNoLCBzYWZlPScnKSkpCiAgICByZXF1aXJlKHJlZi5nZXQoJ29iamVjdCcsIHt9KS5nZXQoJ3R5cGUnKSA9PSAnY29tbWl0JywgJ1JFQ09WRVJZX0NPTU1JVCcpCiAgICB0aXAgPSByZWZbJ29iamVjdCddLmdldCgnc2hhJywgJycpCiAgICByZXF1aXJlKGlzaW5zdGFuY2UodGlwLCBzdHIpIGFuZCByZS5mdWxsbWF0Y2goJ1thLWYwLTldezQwfScsIHRpcCksICdSRUNPVkVSWV9DT01NSVQnKQogICAgcGF0aCA9ICdyZWNvdmVyeS8lcy5iaW4nICUgcmlkCiAgICBmaWxlID0gcmVjb3ZlcnlfaHR0cCh0b2tlbiwgJy9yZXBvcy8lcy9jb250ZW50cy8lcz9yZWY9JXMnICUgKHRvb2xzLCBwYXRoLCB0aXApKQogICAgcmVxdWlyZShmaWxlLmdldCgndHlwZScpID09ICdmaWxlJyBhbmQgZmlsZS5nZXQoJ2VuY29kaW5nJykgPT0gJ2Jhc2U2NCcgYW5kIGZpbGUuZ2V0KCdwYXRoJykgPT0gcGF0aCwgJ1JFQ09WRVJZX0ZJTEVfVFlQRScpCiAgICBlbmNvZGVkID0gZmlsZS5nZXQoJ2NvbnRlbnQnLCAnJykKICAgIHJlcXVpcmUoaXNpbnN0YW5jZShlbmNvZGVkLCBzdHIpIGFuZCBsZW4oZW5jb2RlZCkgPD0gMzBfMDAwLCAnUkVDT1ZFUllfU0laRScpCiAgICBkYXRhID0gdW5iNjQoZW5jb2RlZC5yZXBsYWNlKCdcbicsICcnKS5yZXBsYWNlKCdccicsICcnKSkKICAgIHNoYSA9IGhhc2hsaWIuc2hhMShiJ2Jsb2IgJyArIHN0cihsZW4oZGF0YSkpLmVuY29kZSgpICsgYidcMCcgKyBkYXRhKS5oZXhkaWdlc3QoKQogICAgcmVxdWlyZSh0eXBlKGZpbGUuZ2V0KCdzaXplJykpIGlzIGludCBhbmQgZmlsZVsnc2l6ZSddID09IGxlbihkYXRhKSBhbmQgMSA8PSBsZW4oZGF0YSkgPD0gMTZfMDAwCiAgICAgICAgICAgIGFuZCBmaWxlLmdldCgnc2hhJykgPT0gc2hhLCAnUkVDT1ZFUllfQ09OVEVOVF9IQVNIJykKICAgIHJldHVybiB7J3JlcG9zaXRvcnlfaWQnOiByZXBvc2l0b3J5WydpZCddLCAnYnJhbmNoJzogYnJhbmNoLCAnc2hhJzogc2hhLCAnZGF0YSc6IGRhdGF9CgoKZGVmIHJlc2VhbF9yZWNvdmVyeSh0b2tlbiwgYywgcyk6CiAgICAiIiJPbmUgY29uZGl0aW9uYWwgd3JpdGUgb2YgZHVyYWJsZSBleGFjdCBieXRlcy4gQSB0b21ic3RvbmUgYWx3YXlzIHdpbnMsIGluY2x1ZGluZyBkdXJpbmcgcmVhZGJhY2suCiAgICBMZWdhY3kgY2lwaGVydGV4dCB3aXRob3V0IGV4cGxpY2l0IEFnZW50IG93bmVyc2hpcCBpcyByZWFkLW9ubHk7IG1pc3NpbmcvZm9yZWlnbiBibG9icyBhcmUgbmV2ZXIgcmVwbGFjZWQuCiAgICBDYWxsZXIgaG9sZHMgdGhlIG9yaWdpbmFsIHNlc3Npb24ncyBleGNsdXNpdmUgbG9jay4gTm8gYnVzaW5lc3MgcmVxdWVzdCBvciBpZGVudGl0eSBpcyBjaGFuZ2VkIGhlcmUuCiAgICAiIiIKICAgIHJlYywgdG9vbHMgPSBvcy5lbnZpcm9uLmdldCgnTEVCT1hfUkVDT1ZFUllfU0VMRicsICcnKSwgb3MuZW52aXJvbi5nZXQoJ0xFQk9YX1RPT0xTJywgJycpCiAgICBjb250ZXh0ID0gcy52YWx1ZS5nZXQoJ3JlY292ZXJ5X2NvbnRleHQnLCB7fSkKICAgIGlmIG5vdCByZWM6CiAgICAgICAgcmVjLCB0b29scyA9IGNvbnRleHQuZ2V0KCdyZWNvdmVyeScsICcnKSwgY29udGV4dC5nZXQoJ3Rvb2xzJywgJycpCiAgICBlbGlmIGNvbnRleHQ6CiAgICAgICAgcmVxdWlyZShjb250ZXh0ID09IHsncmVjb3ZlcnknOiByZWMsICd0b29scyc6IHRvb2xzfSwgJ1JFQ09WRVJZX1NDT1BFX01JU01BVENIJykKICAgIGlmIG5vdCByZWMgb3Igbm90IHRvb2xzOgogICAgICAgIHJldHVybiAnbm90X2NvbmZpZ3VyZWQnCiAgICByZXF1aXJlKHJlLmZ1bGxtYXRjaChyJ1tBLVphLXowLTldW0EtWmEtejAtOS1dKi9bQS1aYS16MC05Ll8tXSsnLCB0b29scykKICAgICAgICAgICAgYW5kIHRvb2xzLnNwbGl0KCcvJylbMV0gbm90IGluICgnLicsICcuLicpLCAnUkVDT1ZFUllfVE9PTFMnKQogICAgcmVxdWlyZShvcy5lbnZpcm9uLmdldCgnTEVCT1hfUkVQTycsIGNbJ3JlcG9zaXRvcnknXSkgPT0gY1sncmVwb3NpdG9yeSddLCAnUkVDT1ZFUllfUkVQT1NJVE9SWV9NSVNNQVRDSCcpCiAgICByZXF1aXJlKGlzaW5zdGFuY2UodG9rZW4sIHN0cikgYW5kIDggPD0gbGVuKHRva2VuKSA8PSA0MDk2IGFuZCBub3QgYW55KHguaXNzcGFjZSgpIGZvciB4IGluIHRva2VuKSwgJ1JFQ09WRVJZX1RPS0VOJykKICAgIHJpZCwga2V5ID0gcmVjb3Zlcnlfa2V5KHJlYykKICAgIHNjb3BlID0gZGljdChwaW5zKGMpLCBwcm9qZWN0X2lkPWNbJ3Byb2plY3RfaWQnXSwgcmVwb3NpdG9yeT1jWydyZXBvc2l0b3J5J10sIHRvb2xzPXRvb2xzLCByaWQ9cmlkLAogICAgICAgICAgICAgICAgIGtleV9pZD1oYXNobGliLnNoYTI1NihiJ2xlYm94LXJlY292ZXJ5LWlkJyArIGtleSkuaGV4ZGlnZXN0KCkpCiAgICBzdGF0ZSA9IHMudmFsdWUuZ2V0KCdyZWNvdmVyeV9wdWJsaWNhdGlvbicpCiAgICBpZiBzdGF0ZSBpcyBub3QgTm9uZToKICAgICAgICByZXF1aXJlKGlzaW5zdGFuY2Uoc3RhdGUsIGRpY3QpIGFuZCBzdGF0ZS5nZXQoJ2Zvcm1hdCcpID09ICdsZWJveC1hZ2VudC1wdWJsaWNhdGlvbi12MScgYW5kIHN0YXRlLmdldCgnc2NvcGUnKSA9PSBzY29wZSwgJ1JFQ09WRVJZX1NDT1BFX01JU01BVENIJykKICAgICAgICByZXF1aXJlKHN0YXRlLmdldCgnc3RhdHVzJykgaW4gKCdwZW5kaW5nJywgJ3B1Ymxpc2hlZCcsICdyZXZva2VkJyksICdSRUNPVkVSWV9TVEFURScpCiAgICAgICAgcmVxdWlyZShzdGF0ZVsnc3RhdHVzJ10gIT0gJ3Jldm9rZWQnLCAnUkVDT1ZFUllfUkVWT0tFRCcpCiAgICByZW1vdGUgPSByZWNvdmVyeV9yZW1vdGUodG9rZW4sIHRvb2xzLCByaWQpCiAgICBpZiBzdGF0ZSBpcyBOb25lOgogICAgICAgIHN0YXRlID0geydmb3JtYXQnOiAnbGVib3gtYWdlbnQtcHVibGljYXRpb24tdjEnLCAnc2NvcGUnOiBzY29wZSwgJ3N0YXR1cyc6ICdwdWJsaXNoZWQnLAogICAgICAgICAgICAgICAgICdyZXBvc2l0b3J5X2lkJzogcmVtb3RlWydyZXBvc2l0b3J5X2lkJ10sICdicmFuY2gnOiByZW1vdGVbJ2JyYW5jaCddLCAnZ2VuZXJhdGlvbic6IDB9CiAgICAgICAgcy52YWx1ZVsncmVjb3ZlcnlfcHVibGljYXRpb24nXSA9IHN0YXRlCiAgICByZXF1aXJlKHJlbW90ZVsncmVwb3NpdG9yeV9pZCddID09IHN0YXRlWydyZXBvc2l0b3J5X2lkJ10gYW5kIHJlbW90ZVsnYnJhbmNoJ10gPT0gc3RhdGVbJ2JyYW5jaCddLCAnUkVDT1ZFUllfUkVNT1RFX1NDT1BFX0NIQU5HRUQnKQogICAgZGVmIGRlbnlfcmV2b2tlZChvYnNlcnZlZCk6CiAgICAgICAgaWYgb2JzZXJ2ZWRbJ2RhdGEnXS5zdGFydHN3aXRoKGInTEVCT1gtUkVWT0tFRC1WMScpOgogICAgICAgICAgICBzdGF0ZVsnc3RhdHVzJ10gPSAncmV2b2tlZCc7IHMuc2F2ZSgpICAjIFJldGFpbiBhbGwgcGVuZGluZyBleGFjdCBieXRlczsgcGVybWFuZW50IGxvY2FsIGRlbmlhbC4KICAgICAgICAgICAgcmFpc2UgU3RvcCgnUkVDT1ZFUllfUkVWT0tFRCcpCiAgICBkZW55X3Jldm9rZWQocmVtb3RlKQogICAgaWYgc3RhdGUuZ2V0KCdvdXRib3gnKToKICAgICAgICAjIEFuIGFtYmlndW91cyBwcmV2aW91cyB3cml0ZSBjYW4gT05MWSByZWNvbmNpbGUgaXRzIG9yaWdpbmFsIGJ5dGVzIGFuZCBvcmlnaW5hbCBDQVMgYmFzZWxpbmUuCiAgICAgICAgdGFyZ2V0ID0gdW5iNjQoc3RhdGVbJ291dGJveCddKQogICAgICAgIHJlcXVpcmUoaGFzaGxpYi5zaGEyNTYodGFyZ2V0KS5oZXhkaWdlc3QoKSA9PSBzdGF0ZS5nZXQoJ291dGJveF9zaGEyNTYnKSwgJ1JFQ09WRVJZX09VVEJPWF9DT1JSVVBUJykKICAgIGVsc2U6CiAgICAgICAgb2xkID0gbG9hZF9qc29uKHJlY292ZXJ5X2NpcGhlcihrZXksIHJlbW90ZVsnZGF0YSddLCBUcnVlKSwgbGltaXQ9MTZfMDAwKQogICAgICAgIHJlcXVpcmUoaXNpbnN0YW5jZShvbGQsIGRpY3QpIGFuZCBvbGQuZ2V0KCd2JykgPT0gMSBhbmQgcmVjb3ZlcnlfbGVnYWN5X293bmVkKGMsIHMsIG9sZCwgcmVtb3RlWydkYXRhJ10pCiAgICAgICAgICAgICAgICBhbmQgb2xkLmdldCgncmVwbycpID09IGNbJ3JlcG9zaXRvcnknXSBhbmQgb2xkLmdldCgndG9vbHMnKSA9PSB0b29scwogICAgICAgICAgICAgICAgYW5kICgnc2NvcGUnIG5vdCBpbiBvbGQgb3Igb2xkWydzY29wZSddID09IHNjb3BlKSwgJ1JFQ09WRVJZX09XTkVSX1VOVkVSSUZJRUQnKQogICAgICAgIGlmIG9sZC5nZXQoJ3Rva2VuJykgPT0gdG9rZW4gYW5kIG9sZC5nZXQoJ3Njb3BlJykgPT0gc2NvcGU6CiAgICAgICAgICAgIHJldHVybiAncHVibGlzaGVkJwogICAgICAgIHJlcXVpcmUoc3RhdGVbJ2dlbmVyYXRpb24nXSA8IDEyOCwgJ1JFQ09WRVJZX0dFTkVSQVRJT05fTElNSVQnKQogICAgICAgIHBheWxvYWQgPSB7J3YnOiAxLCAncmVwbyc6IGNbJ3JlcG9zaXRvcnknXSwgJ3Rvb2xzJzogdG9vbHMsICd0b2tlbic6IHRva2VuLCAncmVuZXdlZCc6IGludCh0aW1lLnRpbWUoKSksICdieSc6ICdhZ2VudCcsICdzY29wZSc6IHNjb3BlfQogICAgICAgIHRhcmdldCA9IHJlY292ZXJ5X2NpcGhlcihrZXksIGNhbm9uaWNhbChwYXlsb2FkKSkKICAgICAgICBzdGF0ZS51cGRhdGUoc3RhdHVzPSdwZW5kaW5nJywgZ2VuZXJhdGlvbj1zdGF0ZVsnZ2VuZXJhdGlvbiddICsgMSwgb3V0Ym94PWI2NCh0YXJnZXQpLAogICAgICAgICAgICAgICAgICAgICBvdXRib3hfc2hhMjU2PWhhc2hsaWIuc2hhMjU2KHRhcmdldCkuaGV4ZGlnZXN0KCksIGJhc2VsaW5lX3NoYT1yZW1vdGVbJ3NoYSddKQogICAgICAgIHMuc2F2ZSgpICAjIE5vIG5ldHdvcmsgbXV0YXRpb24gdW50aWwgdGhlIGV4YWN0IGNpcGhlcnRleHQgYW5kIGJhc2VsaW5lIHJlYWNoIGR1cmFibGUgcHJpdmF0ZSBzdG9yYWdlLgogICAgZGVmIGFja25vd2xlZGdlKG9ic2VydmVkKToKICAgICAgICByZXF1aXJlKG9ic2VydmVkWydyZXBvc2l0b3J5X2lkJ10gPT0gc3RhdGVbJ3JlcG9zaXRvcnlfaWQnXSBhbmQgb2JzZXJ2ZWRbJ2JyYW5jaCddID09IHN0YXRlWydicmFuY2gnXSwgJ1JFQ09WRVJZX1JFTU9URV9TQ09QRV9DSEFOR0VEJykKICAgICAgICBkZW55X3Jldm9rZWQob2JzZXJ2ZWQpCiAgICAgICAgaWYgb2JzZXJ2ZWRbJ2RhdGEnXSAhPSB0YXJnZXQ6CiAgICAgICAgICAgIHJldHVybiBGYWxzZQogICAgICAgIHN0YXRlLnVwZGF0ZShzdGF0dXM9J3B1Ymxpc2hlZCcsIHB1Ymxpc2hlZF9zaGE9b2JzZXJ2ZWRbJ3NoYSddKQogICAgICAgIHN0YXRlLnBvcCgnb3V0Ym94JywgTm9uZSk7IHN0YXRlLnBvcCgnYmFzZWxpbmVfc2hhJywgTm9uZSk7IHMuc2F2ZSgpCiAgICAgICAgcmV0dXJuIFRydWUKICAgIGlmIGFja25vd2xlZGdlKHJlbW90ZSk6CiAgICAgICAgcmV0dXJuICdwdWJsaXNoZWQnCiAgICByZXF1aXJlKHJlbW90ZVsnc2hhJ10gPT0gc3RhdGUuZ2V0KCdiYXNlbGluZV9zaGEnKSwgJ1JFQ09WRVJZX0NPTlRFTlRfQ09ORkxJQ1QnKQogICAgcy5zYXZlKCkgICMgQWxzbyBjb3ZlcnMgYSBwcmV2aW91cyBwZXJzaXN0ZW5jZSBmYWlsdXJlOyBuZXZlciBwdWJsaXNoIG9ubHkgaW4tbWVtb3J5IGludGVudC4KICAgIGJvZHkgPSB7J21lc3NhZ2UnOiAnbGVib3g6IHJlbmV3ICcgKyByaWRbOjhdLCAnYnJhbmNoJzogc3RhdGVbJ2JyYW5jaCddLCAnc2hhJzogc3RhdGVbJ2Jhc2VsaW5lX3NoYSddLCAnY29udGVudCc6IGI2NCh0YXJnZXQpfQogICAgdHJ5OgogICAgICAgIHJlY292ZXJ5X2h0dHAodG9rZW4sICcvcmVwb3MvJXMvY29udGVudHMvcmVjb3ZlcnkvJXMuYmluJyAlICh0b29scywgcmlkKSwgYm9keSkKICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAgIyBUaGUgcmVxdWVzdCBtYXkgaGF2ZSBjb21taXR0ZWQuIE9uZSByZWFkLW9ubHkgcmVjb25jaWxpYXRpb247IE5PIGJsaW5kIHJldHJ5IG9yIHJlc2VhbC4KICAgICAgICBwYXNzCiAgICBvYnNlcnZlZCA9IHJlY292ZXJ5X3JlbW90ZSh0b2tlbiwgdG9vbHMsIHJpZCkKICAgIGlmIGFja25vd2xlZGdlKG9ic2VydmVkKToKICAgICAgICByZXR1cm4gJ3B1Ymxpc2hlZCcKICAgIHJldHVybiAncGVuZGluZycgICMgRXZlbiAyMDAvMjAxIGlzIG5vdCBhbiBhY2tub3dsZWRnZW1lbnQgd2l0aG91dCBleGFjdCByZWFkYmFjay4KCgpkZWYgcmVtZW1iZXJfcmVjb3ZlcnlfY29udGV4dChjLCBzKToKICAgICMgU2VjcmV0LWJlYXJpbmcgY29udGV4dCBiZWxvbmdzIG9ubHkgaW4gdGhpcyBvcmlnaW5hbCBzZXNzaW9uJ3Mgb3duZXItcHJpdmF0ZSBzdGF0ZSwgbmV2ZXIgYW4gaW5zdGFsbCByZWNlaXB0LgogICAgcmVjLCB0b29scyA9IG9zLmVudmlyb24uZ2V0KCdMRUJPWF9SRUNPVkVSWV9TRUxGJywgJycpLCBvcy5lbnZpcm9uLmdldCgnTEVCT1hfVE9PTFMnLCAnJykKICAgIGlmIG5vdCByZWM6CiAgICAgICAgcmV0dXJuCiAgICByZWNvdmVyeV9rZXkocmVjKQogICAgcmVxdWlyZShyZS5mdWxsbWF0Y2gocidbQS1aYS16MC05XVtBLVphLXowLTktXSovW0EtWmEtejAtOS5fLV0rJywgdG9vbHMpCiAgICAgICAgICAgIGFuZCB0b29scy5zcGxpdCgnLycpWzFdIG5vdCBpbiAoJy4nLCAnLi4nKSBhbmQgb3MuZW52aXJvbi5nZXQoJ0xFQk9YX1JFUE8nLCBjWydyZXBvc2l0b3J5J10pID09IGNbJ3JlcG9zaXRvcnknXSwgJ1JFQ09WRVJZX1NDT1BFX01JU01BVENIJykKICAgIGNvbnRleHQgPSB7J3JlY292ZXJ5JzogcmVjLCAndG9vbHMnOiB0b29sc30KICAgIHByZXZpb3VzID0gcy52YWx1ZS5nZXQoJ3JlY292ZXJ5X2NvbnRleHQnKQogICAgcmVxdWlyZShwcmV2aW91cyBpcyBOb25lIG9yIHByZXZpb3VzID09IGNvbnRleHQsICdSRUNPVkVSWV9TQ09QRV9NSVNNQVRDSCcpCiAgICBpZiBwcmV2aW91cyBpcyBOb25lOgogICAgICAgIHMudmFsdWVbJ3JlY292ZXJ5X2NvbnRleHQnXSA9IGNvbnRleHQKICAgICAgICBzLnNhdmUoKQoKCmRlZiByZXRyeV9yZWNvdmVyeV9wdWJsaWNhdGlvbihjLCBzKToKICAgIHRva2VuID0gcy52YWx1ZS5nZXQoJ3JlbmV3ZWRfdG9rZW4nKQogICAgaWYgbm90IHRva2VuOgogICAgICAgIHJldHVybgogICAgdHJ5OgogICAgICAgIHJlc3VsdCA9IHJlc2VhbF9yZWNvdmVyeSh0b2tlbiwgYywgcykKICAgICAgICBpZiByZXN1bHQgIT0gJ25vdF9jb25maWd1cmVkJzoKICAgICAgICAgICAgcHJpbnQoanNvbi5kdW1wcyh7J3N0YXR1cyc6ICdyZWNvdmVyeV8nICsgcmVzdWx0fSksIGZpbGU9c3lzLnN0ZGVycikKICAgIGV4Y2VwdCBFeGNlcHRpb24gYXMgZXJyb3I6CiAgICAgICAgIyBEbyBub3QgaW5jbHVkZSBhIHRva2VuLWJlYXJpbmcgSFRUUCBleGNlcHRpb24sIFVSTCBvciBkZWNyeXB0ZWQgcGF5bG9hZCBpbiBkaWFnbm9zdGljcy4KICAgICAgICBjb2RlID0gc3RyKGVycm9yKSBpZiBpc2luc3RhbmNlKGVycm9yLCBTdG9wKSBhbmQgcmUuZnVsbG1hdGNoKHInUkVDT1ZFUllfW0EtWl9dKycsIHN0cihlcnJvcikpIGVsc2UgJ1JFQ09WRVJZX05PVF9DT05GSVJNRUQnCiAgICAgICAgcHJpbnQoanNvbi5kdW1wcyh7J3N0YXR1cyc6ICdyZWNvdmVyeV9ub3RfY29uZmlybWVkJywgJ2NvZGUnOiBjb2RlfSksIGZpbGU9c3lzLnN0ZGVycikKCmRlZiBjbG9zZWRfbWFya2VyKGMsIHMsIGJveCwgdGlwPU5vbmUpOgogICAgIyBUaGUgZGVza3RvcCB3cml0ZXMgc2VydmVyLmNsb3NlZC5qc29uIHdoZW4gaXQgc3RvcHMgb3IgcmUtYmluZHMgdGhpcyBzZXNzaW9uLiBQbGFpbnRleHQsIHNtYWxsLCBjaGVja2VkIG9uIGV2ZXJ5IHBvbGwgc28gYQogICAgIyBzdXBlcnNlZGVkIEFnZW50IGxlYXJucyB3aXRoaW4gb25lIGZldGNoIGluc3RlYWQgb2Ygd2FpdGluZyBvdXQgaXRzIHRpbWVvdXQuCiAgICB0cnk6CiAgICAgICAgcmF3ID0gYm94LmF0KHRpcCwgbWVzc2FnZV9wYXRoKGMsICdjbG9zZWQnLCAnc2VydmVyJykpIGlmIHRpcCBlbHNlIGJveC5yZWFkKCdjbG9zZWQnLCAnc2VydmVyJykKICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIGlmIHJhdyBpcyBOb25lOgogICAgICAgIHJldHVybiBOb25lCiAgICB0cnk6CiAgICAgICAgaW5mbyA9IGxvYWRfanNvbihyYXcsIGxpbWl0PTQwOTYpCiAgICAgICAgaWYgaW5mby5nZXQoJ2Zvcm1hdCcpICE9ICdzY3gtc2Vzc2lvbi1jbG9zZWQtdjEnIG9yIGluZm8uZ2V0KCdiaW5kaW5nJykgIT0gY1snYmluZGluZyddOgogICAgICAgICAgICByZXR1cm4gTm9uZQogICAgICAgIHJldHVybiBpbmZvCiAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgIHJldHVybiBOb25lCgpkZWYgcmVwb3J0X2Nsb3NlZChzLCBpbmZvLCBwZW5kaW5nPU5vbmUpOgogICAgcy52YWx1ZVsnY2xvc2VkJ10gPSB7J3JlYXNvbic6IGluZm8uZ2V0KCdyZWFzb24nLCAnJyksICdjbG9zZWRfYXQnOiBpbmZvLmdldCgnY2xvc2VkX2F0JywgMCl9CiAgICBzLnZhbHVlWydhY3RpdmUnXSA9IEZhbHNlCiAgICBzLnNhdmUoKQogICAgb3V0ID0geydzdGF0dXMnOiAnc2Vzc2lvbl9jbG9zZWRfYnlfZGVza3RvcCcsICdyZWFzb24nOiBpbmZvLmdldCgncmVhc29uJywgJycpLAogICAgICAgICAgICduZXh0JzogaW5mby5nZXQoJ25leHQnLCAnUmUtam9pbjogcnVuICBweXRob24zIC5sZWJveC9hZ2VudC5weSBqb2luICBpbiB0aGUgcmVwb3NpdG9yeS4nKX0KICAgIGlmIHBlbmRpbmc6CiAgICAgICAgb3V0WydvcGVyYXRpb25faWQnXSA9IHBlbmRpbmdbJ2lkJ10KICAgICAgICBvdXRbJ25vdGUnXSA9ICdUaGUgcGVuZGluZyBvcGVyYXRpb24gd2lsbCBuZXZlciBiZSBhbnN3ZXJlZCBvbiB0aGlzIGNoYW5uZWw7IGl0cyBwcm9jZXNzaW5nIHN0YXRlIGlzIHVua25vd24sIGRvIG5vdCByZXN1Ym1pdCBibGluZGx5LicKICAgIHByaW50KGpzb24uZHVtcHMob3V0LCBlbnN1cmVfYXNjaWk9RmFsc2UsIGluZGVudD0yKSwgZmlsZT1zeXMuc3RkZXJyKQogICAgc3lzLmV4aXQoMykKCmRlZiB2ZXJpZnlfcGVuZGluZ19yZXF1ZXN0KGMsIHBlbmRpbmcsIGJveCwgdGlwKToKICAgICIiIkJpbmQgcmVzcG9uc2UgYWNjZXB0YW5jZSB0byBleGFjdCBwdWJsaXNoZWQgYnl0ZXMsIG5vdCBqdXN0IGEgcmV1c2VkIG9wZXJhdGlvbiBJRC4KICAgIEZhaWxzIGJlZm9yZSBhbnkgcmVjb3ZlcnkgcHVibGljYXRpb24sIHJlbmV3YWwsIHJlc3BvbnNlIGRlY3J5cHQsIHNhdmUgb3IgcGVuZGluZyBjbGVhci4KICAgIE5ldmVyIHJlcGFpcnMvcmVzZWFscy9yZXBsYXlzIGEgcmVxdWVzdCBhbmQgbmV2ZXIgdHJlYXRzIHBsYWludGV4dCBlcXVhbGl0eSBhcyBhZG1pc3Npb24uCiAgICAiIiIKICAgIHJlcXVpcmUoaXNpbnN0YW5jZShwZW5kaW5nLCBkaWN0KSBhbmQgaXNpbnN0YW5jZShwZW5kaW5nLmdldCgnaWQnKSwgc3RyKQogICAgICAgICAgICBhbmQgcmUuZnVsbG1hdGNoKHInb3AtWzAtOV17Nn0nLCBwZW5kaW5nWydpZCddKQogICAgICAgICAgICBhbmQgcGVuZGluZy5nZXQoJ21ldGhvZCcpIGluICgnaW5pdGlhbGl6ZScsICd0b29scy9saXN0JywgJ3Rvb2xzL2NhbGwnKSwgJ1BFTkRJTkdfUkVRVUVTVF9JTlZBTElEJykKICAgIGVudiA9IHBlbmRpbmcuZ2V0KCdlbnZlbG9wZScpCiAgICByZXF1aXJlKGlzaW5zdGFuY2UoZW52LCBkaWN0KSBhbmQgc2V0KGVudikgPT0geydoZWFkZXInLCAnbm9uY2UnLCAnY2lwaGVydGV4dCcsICd0YWcnfSwgJ1BFTkRJTkdfUkVRVUVTVF9JTlZBTElEJykKICAgIGggPSBlbnYuZ2V0KCdoZWFkZXInKQogICAgcmVxdWlyZShpc2luc3RhbmNlKGgsIGRpY3QpIGFuZCBzZXQoaCkgPT0gc2V0KHBpbnMoYykpIHwgeydyZXF1ZXN0X2lkJywgJ2RpcmVjdGlvbicsICdleHBpcmVzJ30sICdQRU5ESU5HX1JFUVVFU1RfSU5WQUxJRCcpCiAgICByZXF1aXJlKGFsbChoW2tdID09IHYgZm9yIGssIHYgaW4gcGlucyhjKS5pdGVtcygpKSBhbmQgaFsncmVxdWVzdF9pZCddID09IHBlbmRpbmdbJ2lkJ10KICAgICAgICAgICAgYW5kIGhbJ2RpcmVjdGlvbiddID09ICdyZXEnLCAnUEVORElOR19SRVFVRVNUX0JJTkRJTkcnKQogICAgcmVxdWlyZSh0eXBlKGhbJ2V4cGlyZXMnXSkgaXMgaW50IGFuZCBoWydleHBpcmVzJ10gPiAwCiAgICAgICAgICAgIGFuZCBhbGwoaXNpbnN0YW5jZShlbnZba10sIHN0cikgZm9yIGsgaW4gKCdub25jZScsICdjaXBoZXJ0ZXh0JywgJ3RhZycpKSwgJ1BFTkRJTkdfUkVRVUVTVF9JTlZBTElEJykKICAgIHRyeToKICAgICAgICBmb3IgZmllbGQsIGxlbmd0aCBpbiAoKCdub25jZScsIDEyKSwgKCd0YWcnLCAxNiksICgnY2lwaGVydGV4dCcsIE5vbmUpKToKICAgICAgICAgICAgZGVjb2RlZCA9IHVuYjY0KGVudltmaWVsZF0sIGxlbmd0aCkKICAgICAgICAgICAgcmVxdWlyZShsZW4oZGVjb2RlZCkgPD0gNjU1MzYgYW5kIGI2NChkZWNvZGVkKSA9PSBlbnZbZmllbGRdLCAnUEVORElOR19SRVFVRVNUX0lOVkFMSUQnKQogICAgZXhjZXB0IChWYWx1ZUVycm9yLCBUeXBlRXJyb3IsIFN0b3ApOgogICAgICAgIHJhaXNlIFN0b3AoJ1BFTkRJTkdfUkVRVUVTVF9JTlZBTElEJykgZnJvbSBOb25lCiAgICBleHBlY3RlZCA9IGNhbm9uaWNhbChlbnYpCiAgICByZXF1aXJlKGxlbihleHBlY3RlZCkgPD0gTElNSVQsICdQRU5ESU5HX1JFUVVFU1RfSU5WQUxJRCcpCiAgICBpZiB0aXAgaXMgTm9uZToKICAgICAgICByZXR1cm4gICMgU2hhcGUvYmluZGluZyBwcmVmbGlnaHQgb25seTsgbm8gdHJhbnNwb3J0IG9yIHN0YXRlIGFjY2Vzcy4KICAgIHB1Ymxpc2hlZCA9IGJveC5hdCh0aXAsIG1lc3NhZ2VfcGF0aChjLCAncmVxdWVzdCcsIHBlbmRpbmdbJ2lkJ10pKQogICAgcmVxdWlyZShpc2luc3RhbmNlKHB1Ymxpc2hlZCwgYnl0ZXMpIGFuZCAwIDwgbGVuKHB1Ymxpc2hlZCkgPD0gTElNSVQsICdQRU5ESU5HX1JFUVVFU1RfVU5WRVJJRklFRCcpCiAgICByZXF1aXJlKGhtYWMuY29tcGFyZV9kaWdlc3QoaGFzaGxpYi5zaGEyNTYocHVibGlzaGVkKS5kaWdlc3QoKSwgaGFzaGxpYi5zaGEyNTYoZXhwZWN0ZWQpLmRpZ2VzdCgpKSwKICAgICAgICAgICAgJ1BFTkRJTkdfUkVRVUVTVF9DT05GTElDVDogb3JpZ2luYWwgcGVuZGluZyBwcmVzZXJ2ZWQ7IGRvIG5vdCByZXN1Ym1pdCBvciBzdWJzdGl0dXRlIGFub3RoZXIgcmVxdWVzdCcpCgpkZWYgc3RhdHVzKGMsIHMsIGJveCwgd2FpdD0wKToKICAgIHBlbmRpbmcgPSBzLnZhbHVlLmdldCgncGVuZGluZycpCiAgICBpZiBwZW5kaW5nIGlzIE5vbmU6CiAgICAgICAgcmV0cnlfcmVjb3ZlcnlfcHVibGljYXRpb24oYywgcykKICAgICAgICBpbmZvID0gY2xvc2VkX21hcmtlcihjLCBzLCBib3gpCiAgICAgICAgaWYgaW5mbzoKICAgICAgICAgICAgcmVwb3J0X2Nsb3NlZChzLCBpbmZvKQogICAgICAgIHByaW50KGpzb24uZHVtcHMocy52YWx1ZS5nZXQoJ2xhc3RfcmVwbHknLCB7J3N0YXR1cyc6ICdub19wZW5kaW5nX3JlcXVlc3QnfSksIGVuc3VyZV9hc2NpaT1GYWxzZSwgaW5kZW50PTIpKQogICAgICAgIHJldHVybgogICAgdmVyaWZ5X3BlbmRpbmdfcmVxdWVzdChjLCBwZW5kaW5nLCBib3gsIE5vbmUpCiAgICB1bnRpbCA9IHRpbWUubW9ub3RvbmljKCkgKyB3YWl0CiAgICBhdHRlbXB0ID0gMAogICAgcmVjb3ZlcnlfY2hlY2tlZCA9IEZhbHNlCiAgICB3aGlsZSBUcnVlOgogICAgICAgIHRpcCA9IGJveC5mZXRjaCgpCiAgICAgICAgcmVxdWlyZShpc2luc3RhbmNlKHRpcCwgc3RyKSBhbmQgYm9vbCh0aXApLCAnUEVORElOR19SRVFVRVNUX1VOVkVSSUZJRUQnKQogICAgICAgIHZlcmlmeV9wZW5kaW5nX3JlcXVlc3QoYywgcGVuZGluZywgYm94LCB0aXApCiAgICAgICAgaWYgbm90IHJlY292ZXJ5X2NoZWNrZWQ6CiAgICAgICAgICAgIHJldHJ5X3JlY292ZXJ5X3B1YmxpY2F0aW9uKGMsIHMpCiAgICAgICAgICAgIHJlY292ZXJ5X2NoZWNrZWQgPSBUcnVlCiAgICAgICAgcmF3ID0gYm94LmF0KHRpcCwgbWVzc2FnZV9wYXRoKGMsICdyZXNwb25zZScsIHBlbmRpbmdbJ2lkJ10pKQogICAgICAgIGlmIHJhdyBpcyBOb25lOgogICAgICAgICAgICBhcHBseV90b2tlbl9yZW5ldyhjLCBzLCBib3gsIHRpcCkKICAgICAgICAgICAgaW5mbyA9IGNsb3NlZF9tYXJrZXIoYywgcywgYm94LCB0aXApCiAgICAgICAgICAgIGlmIGluZm86CiAgICAgICAgICAgICAgICByZXBvcnRfY2xvc2VkKHMsIGluZm8sIHBlbmRpbmcpCiAgICAgICAgaWYgcmF3IGlzIG5vdCBOb25lOgogICAgICAgICAgICByZXBseSA9IG9wZW5fcmVzcG9uc2UoYywgcywgcGVuZGluZ1snaWQnXSwgbG9hZF9qc29uKHJhdykpCiAgICAgICAgICAgIGlmIHBlbmRpbmdbJ21ldGhvZCddID09ICdpbml0aWFsaXplJyBhbmQgaXNpbnN0YW5jZShyZXBseSwgZGljdCkgYW5kICdyZXN1bHQnIGluIHJlcGx5OgogICAgICAgICAgICAgICAgcy52YWx1ZVsnaW5pdGlhbGl6ZWQnXSA9IFRydWUKICAgICAgICAgICAgcy52YWx1ZVsnbGFzdF9yZXBseSddID0gcmVwbHkKICAgICAgICAgICAgZGVsIHMudmFsdWVbJ3BlbmRpbmcnXQogICAgICAgICAgICBzLnNhdmUoKQogICAgICAgICAgICBwcmludChqc29uLmR1bXBzKHJlcGx5LCBlbnN1cmVfYXNjaWk9RmFsc2UsIGluZGVudD0yKSkKICAgICAgICAgICAgcmV0dXJuCiAgICAgICAgaWYgdGltZS5tb25vdG9uaWMoKSA+PSB1bnRpbDoKICAgICAgICAgICAgcHJpbnQoanNvbi5kdW1wcyh7J3N0YXR1cyc6ICdyZXNwb25zZV9ub3RfcmVjZWl2ZWQnLCAnb3BlcmF0aW9uX2lkJzogcGVuZGluZ1snaWQnXSwKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgJ25leHQnOiAnVXNlIHN0YXR1cyB0byByZWNvbmNpbGUgdGhpcyBvcmlnaW5hbCBvcGVyYXRpb24uIE5vIHJlc3BvbnNlIGhhcyBiZWVuIHJlY2VpdmVkOyB0aGlzIGRvZXMgbm90IHByb3ZlIHBlbmRpbmcgcGVybWlzc2lvbiBhcHByb3ZhbC4gUHJvY2Vzc2luZyBzdGF0ZSBpcyB1bmtub3duOyBkbyBub3Qgc3VibWl0IHRoZSBvcGVyYXRpb24gYWdhaW4uJ30pKQogICAgICAgICAgICByZXR1cm4KICAgICAgICB0aW1lLnNsZWVwKG1pbihwb2xsX2RlbGF5KGF0dGVtcHQpLCBtYXgoMC4wLCB1bnRpbCAtIHRpbWUubW9ub3RvbmljKCkpKSBvciAxKQogICAgICAgIGF0dGVtcHQgKz0gMQoKZGVmIHJlcXVlc3QoYywgcywgYm94LCBtZXRob2QsIGFyZ3MsIHdhaXQ9OTApOgogICAgcmVxdWlyZShub3Qgcy52YWx1ZS5nZXQoJ3BlbmRpbmcnKSwgJ1RoZXJlIGlzIGFuIHVucmVzb2x2ZWQgcmVxdWVzdC4gUnVuIHN0YXR1cyBpbnN0ZWFkIG9mIHN1Ym1pdHRpbmcgYW5vdGhlciBvcGVyYXRpb24uJykKICAgIGFwcGx5X3Rva2VuX3JlbmV3KGMsIHMsIGJveCkKICAgIGlmIHMudmFsdWUuZ2V0KCdjbG9zZWQnKToKICAgICAgICByZXBvcnRfY2xvc2VkKHMsIHsncmVhc29uJzogcy52YWx1ZVsnY2xvc2VkJ10uZ2V0KCdyZWFzb24nLCAnJyksICduZXh0JzogJ1RoaXMgc2Vzc2lvbiB3YXMgY2xvc2VkIGJ5IHRoZSBkZXNrdG9wLiBSZS1qb2luOiBydW4gIHB5dGhvbjMgLmxlYm94L2FnZW50LnB5IGpvaW4gIGluIHRoZSByZXBvc2l0b3J5Lid9KQogICAgcmVxdWlyZShzLnZhbHVlLmdldCgnYWN0aXZlJyksICdQYWlyaW5nIGlzIG5vdCBhY3RpdmUuJykKICAgIHJlcXVpcmUobWV0aG9kID09ICdpbml0aWFsaXplJyBvciBzLnZhbHVlLmdldCgnaW5pdGlhbGl6ZWQnKSwgJ0luaXRpYWxpemUgbXVzdCBjb21wbGV0ZSBiZWZvcmUgdG9vbCBjYWxscy4nKQogICAgcmVxdWlyZShjWydtYXhfb3BlcmF0aW9ucyddID09IDAgb3Igcy52YWx1ZVsnbmV4dCddIDwgY1snbWF4X29wZXJhdGlvbnMnXSwgJ1Nlc3Npb24gbWVzc2FnZSBsaW1pdCByZWFjaGVkOyByZXF1ZXN0IGEgbmV3IHNlc3Npb24uJykKICAgIHJlcXVpcmUocy52YWx1ZVsnbmV4dCddIDwgMV8wMDBfMDAwLCAnU2Vzc2lvbiBtZXNzYWdlIGxpbWl0IHJlYWNoZWQ7IHJlcXVlc3QgYSBuZXcgc2Vzc2lvbi4nKQogICAgb3AgPSBmIm9wLXtzLnZhbHVlWyduZXh0J106MDZkfSIKICAgIGVudiA9IHNlYWwoYywgcywgb3AsIGNhbm9uaWNhbCh7J29wZXJhdGlvbl9pZCc6IG9wLCAnbWV0aG9kJzogbWV0aG9kLCAnYXJndW1lbnRzJzogYXJnc30pKQogICAgcy52YWx1ZVsnbmV4dCddICs9IDEKICAgIHMudmFsdWVbJ3BlbmRpbmcnXSA9IHsnaWQnOiBvcCwgJ21ldGhvZCc6IG1ldGhvZCwgJ2VudmVsb3BlJzogZW52fQogICAgcy5zYXZlKCkgICMgUGVyc2lzdCBleGFjdCBjaXBoZXJ0ZXh0IEJFRk9SRSBwdWJsaXNoaW5nIGFueXRoaW5nLgogICAgYm94LmNyZWF0ZSgncmVxdWVzdCcsIG9wLCBjYW5vbmljYWwoZW52KSkKICAgIHN0YXR1cyhjLCBzLCBib3gsIHdhaXQ9d2FpdCkKCmRlZiBhY3RpdmF0ZShjLCBzLCBib3gsIGFwcHJvdmVkLCB3YWl0PTApOgogICAgcmVxdWlyZSgna2V5cycgaW4gcy52YWx1ZSwgJ1J1biBjb25uZWN0IGZpcnN0LicpCiAgICBpZiBzLnZhbHVlLmdldCgncGVuZGluZycpOgogICAgICAgIHJldHVybiBzdGF0dXMoYywgcywgYm94LCB3YWl0KQogICAgaWYgcy52YWx1ZS5nZXQoJ2luaXRpYWxpemVkJyk6CiAgICAgICAgcHJpbnQoJ0FscmVhZHkgaW5pdGlhbGl6ZWQuIFVzZSBsaXN0LCBjYWxsIG9yIHN0YXR1cy4nKTsgcmV0dXJuCiAgICAjIFRoZSBsb2NhbCBtYWNoaW5lIHB1Ymxpc2hlcyBjb25maXJtYXRpb24vc2VydmVyIG9ubHkgYWZ0ZXIgdGhlIHVzZXIgKG9yIHRoZSB0cnVzdGVkLXJlcG9zaXRvcnkgc2V0dGluZykgY29uZmlybWVkIHRoZSBwYWlyaW5nLgogICAgdW50aWwgPSB0aW1lLm1vbm90b25pYygpICsgbWF4KDAsIHdhaXQpCiAgICBhdHRlbXB0ID0gMAogICAgd2hpbGUgVHJ1ZToKICAgICAgICByYXcgPSBib3gucmVhZCgnY29uZmlybWF0aW9uJywgJ3NlcnZlcicpCiAgICAgICAgaWYgcmF3IGlzIE5vbmU6CiAgICAgICAgICAgIGluZm8gPSBjbG9zZWRfbWFya2VyKGMsIHMsIGJveCkKICAgICAgICAgICAgaWYgaW5mbzoKICAgICAgICAgICAgICAgIHJlcG9ydF9jbG9zZWQocywgaW5mbykKICAgICAgICBpZiByYXcgaXMgbm90IE5vbmUgb3IgdGltZS5tb25vdG9uaWMoKSA+PSB1bnRpbDoKICAgICAgICAgICAgYnJlYWsKICAgICAgICB0aW1lLnNsZWVwKG1pbihwb2xsX2RlbGF5KGF0dGVtcHQpLCBtYXgoMC4wLCB1bnRpbCAtIHRpbWUubW9ub3RvbmljKCkpKSBvciAxKQogICAgICAgIGF0dGVtcHQgKz0gMQogICAgcmVxdWlyZShyYXcgaXMgbm90IE5vbmUsICdXYWl0aW5nIGZvciBsb2NhbCBjb25maXJtYXRpb24uIFJ1biBhY3RpdmF0ZSAtLXdhaXQgOTAwIGFnYWluOyBkbyBub3Qgc3RhcnQgdG9vbHMgeWV0LicpCiAgICBjb25maXJtYXRpb24gPSBsb2FkX2pzb24ocmF3KQogICAgZXhhY3QoY29uZmlybWF0aW9uLCBbJ3JvbGUnLCAnYmluZGluZycsICdlcG9jaCcsICd0YWcnXSkKICAgIHJlcXVpcmUoY29uZmlybWF0aW9uWydyb2xlJ10gPT0gJ3NlcnZlcicgYW5kIGNvbmZpcm1hdGlvblsnYmluZGluZyddID09IGNbJ2JpbmRpbmcnXSBhbmQgY29uZmlybWF0aW9uWydlcG9jaCddID09IGNbJ2Vwb2NoJ10sICdDb25maXJtYXRpb24gaWRlbnRpdHkgbWlzbWF0Y2guJykKICAgIGtleXMgPSB1bmI2NChzLnZhbHVlWydrZXlzJ10sIDEyOCkKICAgIHRyYW5zY3JpcHQgPSB1bmI2NChzLnZhbHVlWyd0cmFuc2NyaXB0J10sIDMyKQogICAgcmVxdWlyZShobWFjLmNvbXBhcmVfZGlnZXN0KHVuYjY0KGNvbmZpcm1hdGlvblsndGFnJ10sIDMyKSwgaG1hYy5kaWdlc3Qoa2V5c1s5NjoxMjhdLCB0cmFuc2NyaXB0LCAnc2hhMjU2JykpLCAnQ29uZmlybWF0aW9uIGZhaWxlZC4nKQogICAgYm94LmNyZWF0ZSgnY29uZmlybWF0aW9uJywgJ2NsaWVudCcsIGNhbm9uaWNhbCh7J3JvbGUnOiAnY2xpZW50JywgJ2JpbmRpbmcnOiBjWydiaW5kaW5nJ10sICdlcG9jaCc6IGNbJ2Vwb2NoJ10sCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICd0YWcnOiBiNjQoaG1hYy5kaWdlc3Qoa2V5c1s2NDo5Nl0sIHRyYW5zY3JpcHQsICdzaGEyNTYnKSl9KSkKICAgIHMudmFsdWVbJ2FjdGl2ZSddID0gVHJ1ZQogICAgcy5zYXZlKCkKICAgIHJlcXVlc3QoYywgcywgYm94LCAnaW5pdGlhbGl6ZScsIHt9LCB3YWl0PW1heCg5MCwgd2FpdCkpCgojIEluZGVwZW5kZW50IHJlc3VsdC1jb250cm9sIHN0YXRlLiBUaGlzIG5ldmVyIHNhdmVzIG9yIGVkaXRzIFByaXZhdGVTdGF0ZS52YWx1ZSwgcGVuZGluZyBvciBidXNpbmVzcyBjb3VudGVycy4KZGVmIF9yZXN1bHRfY29udHJvbF9sb2FkKGMsIHMpOgogICAgcGF0aCA9IHMucm9vdCAvICdyZXN1bHQtcmVhZC5qc29uJwogICAgc2NvcGUgPSBoYXNobGliLnNoYTI1NihjYW5vbmljYWwoYykpLmhleGRpZ2VzdCgpCiAgICB2YWx1ZSA9IF9zdGF0ZV9qc29uKF9zdGF0ZV9yZWFkKHBhdGgpKSBpZiBwYXRoLmV4aXN0cygpIGVsc2UgeydzY29wZSc6IHNjb3BlLCAnbmV4dCc6IDB9CiAgICByZXF1aXJlKGlzaW5zdGFuY2UodmFsdWUsIGRpY3QpIGFuZCB2YWx1ZS5nZXQoJ3Njb3BlJykgPT0gc2NvcGUgYW5kIHR5cGUodmFsdWUuZ2V0KCduZXh0JykpIGlzIGludAogICAgICAgICAgICBhbmQgMCA8PSB2YWx1ZVsnbmV4dCddIDw9IDEwMDAwMDAsICdSRVNVTFRfQ09OVFJPTF9TVEFURScpCiAgICByZXR1cm4gcGF0aCwgdmFsdWUKCgpkZWYgX3Jlc3VsdF9jb250cm9sX3NhdmUocGF0aCwgdmFsdWUpOgogICAgcmF3ID0gY2Fub25pY2FsKHZhbHVlKQogICAgcmVxdWlyZShsZW4ocmF3KSA8PSA0MDAwMDAsICdSRVNVTFRfQ09OVFJPTF9MSU1JVCcpCiAgICBfc3RhdGVfd3JpdGUocGF0aCwgcmF3KQoKCmRlZiBfcmVzdWx0X3JlcGx5KGMsIHMsIG9wZXJhdGlvbiwgZW52KToKICAgICMgQXV0aGVudGljYXRlIGJlZm9yZSBkZWNpZGluZyB3aGV0aGVyIGFuIGV4cGlyZWQgQ09OVFJPTCByZWNlaXB0IGNhbiBhZHZhbmNlIHRoZSByZWFkIGN1cnNvci4KICAgICMgT3JkaW5hcnkgb3Blbl9yZXNwb25zZSBhbmQgYnVzaW5lc3MgVFRML3JlcGxheSBydWxlcyBzdGF5IHVuY2hhbmdlZC4KICAgIHJlcXVpcmUocmUuZnVsbG1hdGNoKHIncnJlYWQtWzAtOV17Nn0nLCBvcGVyYXRpb24pLCAnUkVTVUxUX1FVRVJZX0lEJykKICAgIGV4YWN0KGVudiwgWydoZWFkZXInLCAnbm9uY2UnLCAnY2lwaGVydGV4dCcsICd0YWcnXSkKICAgIGggPSBlbnZbJ2hlYWRlciddOyBleGFjdChoLCBsaXN0KHBpbnMoYykpICsgWydyZXF1ZXN0X2lkJywgJ2RpcmVjdGlvbicsICdleHBpcmVzJ10pCiAgICByZXF1aXJlKGFsbChoW2tdID09IHYgZm9yIGssIHYgaW4gcGlucyhjKS5pdGVtcygpKSBhbmQgaFsncmVxdWVzdF9pZCddID09IG9wZXJhdGlvbgogICAgICAgICAgICBhbmQgaFsnZGlyZWN0aW9uJ10gPT0gJ3JlcycgYW5kIHR5cGUoaFsnZXhwaXJlcyddKSBpcyBpbnQgYW5kIGhbJ2V4cGlyZXMnXSA+IDAsICdSRVNVTFRfUkVQTFlfQklORElORycpCiAgICBfLCBfLCBfLCBfLCBBRVNHQ00gPSBjcnlwdG8oKQogICAgY2lwaGVyID0gdW5iNjQoZW52WydjaXBoZXJ0ZXh0J10pOyByZXF1aXJlKGxlbihjaXBoZXIpIDw9IDY1NTM2LCAnUkVTVUxUX1JFUExZX1NJWkUnKQogICAgcGxhaW4gPSBBRVNHQ00odW5iNjQocy52YWx1ZVsna2V5cyddLCAxMjgpWzMyOjY0XSkuZGVjcnlwdCh1bmI2NChlbnZbJ25vbmNlJ10sIDEyKSwgY2lwaGVyICsgdW5iNjQoZW52Wyd0YWcnXSwgMTYpLCBjYW5vbmljYWwoaCkpCiAgICBib2R5ID0gbG9hZF9qc29uKHBsYWluLCBsaW1pdD02NTUzNik7IGV4YWN0KGJvZHksIFsncHJvamVjdF9pZCcsICdyZXBseSddKQogICAgcmVxdWlyZShib2R5Wydwcm9qZWN0X2lkJ10gPT0gY1sncHJvamVjdF9pZCddLCAnUkVTVUxUX1JFUExZX1BST0pFQ1QnKQogICAgcmV0dXJuIGJvZHlbJ3JlcGx5J10sIHRpbWUudGltZSgpIC0gMSA8PSBoWydleHBpcmVzJ10gPD0gdGltZS50aW1lKCkgKyAxODAxCgoKZGVmIF9vcmlnaW5hbF9yZXN1bHRfZGVjb2RlKGMsIHBheWxvYWQpOgogICAgcmVxdWlyZShsZW4ocGF5bG9hZCkgPD0gNjU1MzYsICdSRVNVTFRfUEFZTE9BRF9TSVpFJykKICAgIGlmIHBheWxvYWQuc3RhcnRzd2l0aChiJ1NDWDJaXDAnKToKICAgICAgICBkZWNvZGVyID0gemxpYi5kZWNvbXByZXNzb2JqKCkKICAgICAgICBwYXlsb2FkID0gZGVjb2Rlci5kZWNvbXByZXNzKHBheWxvYWRbNjpdLCAxMDAwMDAxKQogICAgICAgIHJlcXVpcmUobGVuKHBheWxvYWQpIDw9IDEwMDAwMDAgYW5kIGRlY29kZXIuZW9mIGFuZCBub3QgZGVjb2Rlci51bnVzZWRfZGF0YSBhbmQgbm90IGRlY29kZXIudW5jb25zdW1lZF90YWlsLAogICAgICAgICAgICAgICAgJ1JFU1VMVF9DT01QUkVTU0VEX0xJTUlUJykKICAgIGJvZHkgPSBsb2FkX2pzb24ocGF5bG9hZCwgbGltaXQ9MTAwMDAwMCk7IGV4YWN0KGJvZHksIFsncHJvamVjdF9pZCcsICdyZXBseSddKQogICAgcmVxdWlyZShib2R5Wydwcm9qZWN0X2lkJ10gPT0gY1sncHJvamVjdF9pZCddLCAnUkVTVUxUX09SSUdJTkFMX1BST0pFQ1QnKQogICAgcmV0dXJuIGJvZHlbJ3JlcGx5J10KCgpkZWYgcmVzdWx0X3JlYWQoYywgcywgYm94LCB3YWl0PTApOgogICAgcmVxdWlyZShzLnZhbHVlLmdldCgnYWN0aXZlJykgYW5kIHMudmFsdWUuZ2V0KCdrZXlzJyksICdPcmlnaW5hbCBwYWlyZWQgaWRlbnRpdHkgcmVxdWlyZWQ7IGRvIG5vdCBjcmVhdGUgYW5vdGhlciBpZGVudGl0eS4nKQogICAgcGVuZGluZyA9IHMudmFsdWUuZ2V0KCdwZW5kaW5nJykKICAgIHJlcXVpcmUoaXNpbnN0YW5jZShwZW5kaW5nLCBkaWN0KSBhbmQgcmUuZnVsbG1hdGNoKHInb3AtWzAtOV17Nn0nLCBwZW5kaW5nLmdldCgnaWQnLCAnJykpLCAnTm8gb3JpZ2luYWwgcGVuZGluZyBvcGVyYXRpb24gdG8gcmVhZC4nKQogICAgb3JpZ2luYWwgPSBjYW5vbmljYWwocGVuZGluZ1snZW52ZWxvcGUnXSkKICAgIG9yaWdpbmFsX2hhc2ggPSBoYXNobGliLnNoYTI1NihvcmlnaW5hbCkuaGV4ZGlnZXN0KCkudXBwZXIoKQogICAgbm9uY2UgPSBwZW5kaW5nWydlbnZlbG9wZSddWydub25jZSddOyB1bmI2NChub25jZSwgMTIpCiAgICBoID0gcGVuZGluZ1snZW52ZWxvcGUnXVsnaGVhZGVyJ10KICAgIHJlcXVpcmUoYWxsKGhba10gPT0gdiBmb3IgaywgdiBpbiBwaW5zKGMpLml0ZW1zKCkpIGFuZCBoWydyZXF1ZXN0X2lkJ10gPT0gcGVuZGluZ1snaWQnXSBhbmQgaFsnZGlyZWN0aW9uJ10gPT0gJ3JlcScsICdSRVNVTFRfT1JJR0lOQUxfQklORElORycpCiAgICBwYXRoLCBjb250cm9sID0gX3Jlc3VsdF9jb250cm9sX2xvYWQoYywgcykKICAgIHJlcXVpcmUoY29udHJvbC5nZXQoJ29yaWdpbmFsX3NoYTI1NicsIG9yaWdpbmFsX2hhc2gpID09IG9yaWdpbmFsX2hhc2gsICdSRVNVTFRfT1JJR0lOQUxfQ0hBTkdFRCcpCiAgICBjb250cm9sWydvcmlnaW5hbF9zaGEyNTYnXSA9IG9yaWdpbmFsX2hhc2gKICAgIGRlYWRsaW5lID0gdGltZS5tb25vdG9uaWMoKSArIG1heCgwLCBtaW4od2FpdCwgMTgwMCkpCiAgICAjIEF0IG1vc3Qgc2l4dGVlbiByZXRpcmVkIGNvbnRyb2wgZ2VuZXJhdGlvbnMgcGVyIGludm9jYXRpb24sIGluZGVwZW5kZW50IG9mIGJ1c2luZXNzIG51bWJlcmluZy4KICAgIGZvciBfIGluIHJhbmdlKDE2KToKICAgICAgICBpZiAncGVuZGluZycgbm90IGluIGNvbnRyb2w6CiAgICAgICAgICAgIHJlcXVpcmUoY29udHJvbFsnbmV4dCddIDwgMTAwMDAwMCwgJ1JFU1VMVF9RVUVSWV9MSU1JVCcpCiAgICAgICAgICAgIG9mZnNldCA9IGxlbih1bmI2NChjb250cm9sLmdldCgnZGF0YScsICcnKSkpCiAgICAgICAgICAgIGlmIGNvbnRyb2wuZ2V0KCdjb21wbGV0ZScpOgogICAgICAgICAgICAgICAgIyBFYWNoIGV4cGxpY2l0IG5ldyByZWFkIG11c3QgcmUtY2hlY2sgQ1VSUkVOVCBhdXRob3JpemF0aW9uLCBub3Qgc2VydmUgYSBzdGFsZSBjYWNoZWQgcmVzdWx0LgogICAgICAgICAgICAgICAgZm9yIGtleSBpbiAoJ2RhdGEnLCAndG90YWwnLCAncmVzdWx0X3NoYTI1NicsICdhdXRob3JpemF0aW9uX3ZlcnNpb24nLCAnY29tcGxldGUnKToKICAgICAgICAgICAgICAgICAgICBjb250cm9sLnBvcChrZXksIE5vbmUpCiAgICAgICAgICAgICAgICBvZmZzZXQgPSAwCiAgICAgICAgICAgIHEgPSBkaWN0KHByb3RvY29sPSdzY3gtcmVzdWx0LXJlYWQtdjEnLCBwcm9qZWN0X2lkPWNbJ3Byb2plY3RfaWQnXSwgcmVwb3NpdG9yeT1jWydyZXBvc2l0b3J5J10sCiAgICAgICAgICAgICAgICAgICAgIHJlcG9faWQ9Y1sncmVwb19pZCddLCBicmFuY2g9Y1snYnJhbmNoJ10sIGJpbmRpbmc9Y1snYmluZGluZyddLCBlcG9jaD1jWydlcG9jaCddLAogICAgICAgICAgICAgICAgICAgICBvcGVyYXRpb25faWQ9cGVuZGluZ1snaWQnXSwgcmVxdWVzdF9zaGEyNTY9b3JpZ2luYWxfaGFzaCwgcmVxdWVzdF9ub25jZT1ub25jZSwKICAgICAgICAgICAgICAgICAgICAgY2hhbGxlbmdlPXNlY3JldHMudG9rZW5faGV4KDMyKSwgb2Zmc2V0PW9mZnNldCwKICAgICAgICAgICAgICAgICAgICAgYXV0aG9yaXphdGlvbl92ZXJzaW9uPWNvbnRyb2wuZ2V0KCdhdXRob3JpemF0aW9uX3ZlcnNpb24nLCAnJykpCiAgICAgICAgICAgIHJlcXVpcmUob2Zmc2V0IGluICgwLCAzMjc2OCksICdSRVNVTFRfUEFHRV9PRkZTRVQnKQogICAgICAgICAgICBvcCA9ICdycmVhZC0lMDZkJyAlIGNvbnRyb2xbJ25leHQnXQogICAgICAgICAgICAjIElzb2xhdGVkIG5vbmNlIGJvb2trZWVwaW5nOiBzZWFsIG5ldmVyIG11dGF0ZXMgb3Igc2F2ZXMgb3JpZ2luYWwgaWRlbnRpdHkgc3RhdGUgaGVyZS4KICAgICAgICAgICAgY2xhc3MgQ29udHJvbElkZW50aXR5OiBwYXNzCiAgICAgICAgICAgIGRldGFjaGVkID0gQ29udHJvbElkZW50aXR5KCk7IGRldGFjaGVkLnZhbHVlID0gZGljdChzLnZhbHVlKQogICAgICAgICAgICBkZXRhY2hlZC52YWx1ZVsndHhfbm9uY2VzJ10gPSBsaXN0KHMudmFsdWUuZ2V0KCd0eF9ub25jZXMnLCBbXSkpICsgbGlzdChjb250cm9sLmdldCgndHhfbm9uY2VzJywgW10pKQogICAgICAgICAgICBlbnYgPSBzZWFsKGMsIGRldGFjaGVkLCBvcCwgY2Fub25pY2FsKHEpLCBsaWZldGltZT0xMjApCiAgICAgICAgICAgIGNvbnRyb2xbJ3R4X25vbmNlcyddID0gKGNvbnRyb2wuZ2V0KCd0eF9ub25jZXMnLCBbXSkgKyBbZW52Wydub25jZSddXSlbLTgxOTI6XQogICAgICAgICAgICBjb250cm9sWydwZW5kaW5nJ10gPSB7J2lkJzogb3AsICdxdWVyeSc6IHEsICdlbnZlbG9wZSc6IGVudn0KICAgICAgICAgICAgX3Jlc3VsdF9jb250cm9sX3NhdmUocGF0aCwgY29udHJvbCkgICMgTXVzdCBiZSBkdXJhYmxlIGJlZm9yZSBjcmVhdGluZyBhbnl0aGluZyBvbiBHaXRIdWIuCiAgICAgICAgYWN0aXZlID0gY29udHJvbFsncGVuZGluZyddOyBvcCA9IGFjdGl2ZVsnaWQnXTsgcSA9IGFjdGl2ZVsncXVlcnknXTsgZW52ID0gYWN0aXZlWydlbnZlbG9wZSddCiAgICAgICAgcmVxdWlyZShvcCA9PSAncnJlYWQtJTA2ZCcgJSBjb250cm9sWyduZXh0J10gYW5kIHFbJ3JlcXVlc3Rfc2hhMjU2J10gPT0gb3JpZ2luYWxfaGFzaAogICAgICAgICAgICAgICAgYW5kIHFbJ3JlcXVlc3Rfbm9uY2UnXSA9PSBub25jZSBhbmQgcVsnb3BlcmF0aW9uX2lkJ10gPT0gcGVuZGluZ1snaWQnXSwgJ1JFU1VMVF9DT05UUk9MX01JU01BVENIJykKICAgICAgICByYXdfcXVlcnkgPSBjYW5vbmljYWwoZW52KQogICAgICAgIHRpcCA9IGJveC5mZXRjaCgpCiAgICAgICAgcmF3X3JlcGx5ID0gYm94LmF0KHRpcCwgbWVzc2FnZV9wYXRoKGMsICdyZXN1bHRyZXBseScsIG9wKSkKICAgICAgICBpZiByYXdfcmVwbHkgaXMgbm90IE5vbmU6CiAgICAgICAgICAgIHJlcGx5LCBmcmVzaCA9IF9yZXN1bHRfcmVwbHkoYywgcywgb3AsIGxvYWRfanNvbihyYXdfcmVwbHkpKQogICAgICAgICAgICBleGFjdChyZXBseSwgWydwcm90b2NvbCcsICdxdWVyeScsICdxdWVyeV9zaGEyNTYnLCAnc3RhdHVzJywgJ2F1dGhvcml6YXRpb25fdmVyc2lvbicsICd0b3RhbCcsICdyZXN1bHRfc2hhMjU2JywgJ2NodW5rJ10pCiAgICAgICAgICAgIHJlcXVpcmUocmVwbHlbJ3Byb3RvY29sJ10gPT0gJ3NjeC1yZXN1bHQtcmVhZC12MScgYW5kIHJlcGx5WydxdWVyeSddID09IHEKICAgICAgICAgICAgICAgICAgICBhbmQgcmVwbHlbJ3F1ZXJ5X3NoYTI1NiddID09IGhhc2hsaWIuc2hhMjU2KHJhd19xdWVyeSkuaGV4ZGlnZXN0KCkudXBwZXIoKSwgJ1JFU1VMVF9QUk9PRl9NSVNNQVRDSCcpCiAgICAgICAgICAgICMgQXV0aGVudGljYXRlZCBleHBpcmVkIHJlcGxpZXMgb25seSByZXRpcmUgY29udHJvbCBzbG90czsgbmV2ZXIgZXhwb3NlIHRoZWlyIHBheWxvYWQuCiAgICAgICAgICAgIGlmIG5vdCBmcmVzaDoKICAgICAgICAgICAgICAgIGNvbnRyb2wucG9wKCdwZW5kaW5nJyk7IGNvbnRyb2xbJ25leHQnXSArPSAxCiAgICAgICAgICAgICAgICBfcmVzdWx0X2NvbnRyb2xfc2F2ZShwYXRoLCBjb250cm9sKTsgY29udGludWUKICAgICAgICAgICAgaWYgcmVwbHlbJ3N0YXR1cyddICE9ICdhdmFpbGFibGUnOgogICAgICAgICAgICAgICAgcmVxdWlyZShyZXBseVsnc3RhdHVzJ10gaW4gKCd1bmF1dGhvcml6ZWQnLCAnYXV0aG9yaXphdGlvbl9jaGFuZ2VkJywgJ3F1ZXJ5X2V4cGlyZWQnLCAnb3JpZ2luYWxfcmVxdWVzdF9taXNzaW5nJywKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICdub3RfZm91bmQnLCAnY29uZmxpY3QnLCAndW5rbm93bicsICdhcmNoaXZlX2ludmFsaWQnLCAnYXJjaGl2ZV9saW1pdCcsICdvZmZzZXRfaW52YWxpZCcpCiAgICAgICAgICAgICAgICAgICAgICAgIGFuZCByZXBseVsnY2h1bmsnXSA9PSAnJyBhbmQgcmVwbHlbJ3RvdGFsJ10gPT0gMCBhbmQgcmVwbHlbJ3Jlc3VsdF9zaGEyNTYnXSA9PSAnJwogICAgICAgICAgICAgICAgICAgICAgICBhbmQgcmVwbHlbJ2F1dGhvcml6YXRpb25fdmVyc2lvbiddID09ICcnLCAnUkVTVUxUX05FR0FUSVZFX1BST09GJykKICAgICAgICAgICAgICAgIGNvbnRyb2wucG9wKCdwZW5kaW5nJyk7IGNvbnRyb2xbJ25leHQnXSArPSAxCiAgICAgICAgICAgICAgICBmb3Iga2V5IGluICgnZGF0YScsICd0b3RhbCcsICdyZXN1bHRfc2hhMjU2JywgJ2F1dGhvcml6YXRpb25fdmVyc2lvbicsICdjb21wbGV0ZScpOiBjb250cm9sLnBvcChrZXksIE5vbmUpCiAgICAgICAgICAgICAgICBfcmVzdWx0X2NvbnRyb2xfc2F2ZShwYXRoLCBjb250cm9sKQogICAgICAgICAgICAgICAgYW5zd2VyID0geydzdGF0dXMnOiByZXBseVsnc3RhdHVzJ10sICdvcGVyYXRpb25faWQnOiBwZW5kaW5nWydpZCddLCAncGVuZGluZ19wcmVzZXJ2ZWQnOiBUcnVlLCAnYnVzaW5lc3NfcmVwbGF5ZWQnOiBGYWxzZX0KICAgICAgICAgICAgICAgIHByaW50KGpzb24uZHVtcHMoYW5zd2VyLCBlbnN1cmVfYXNjaWk9RmFsc2UpKTsgcmV0dXJuIGFuc3dlcgogICAgICAgICAgICByZXF1aXJlKGlzaW5zdGFuY2UocmVwbHlbJ2F1dGhvcml6YXRpb25fdmVyc2lvbiddLCBzdHIpIGFuZCAwIDwgbGVuKHJlcGx5WydhdXRob3JpemF0aW9uX3ZlcnNpb24nXSkgPD0gMjU2CiAgICAgICAgICAgICAgICAgICAgYW5kIHR5cGUocmVwbHlbJ3RvdGFsJ10pIGlzIGludCBhbmQgMCA8PSByZXBseVsndG90YWwnXSA8PSA2NTUzNgogICAgICAgICAgICAgICAgICAgIGFuZCBpc2luc3RhbmNlKHJlcGx5WydyZXN1bHRfc2hhMjU2J10sIHN0cikgYW5kIHJlLmZ1bGxtYXRjaChyJ1tBLUYwLTldezY0fScsIHJlcGx5WydyZXN1bHRfc2hhMjU2J10pLCAnUkVTVUxUX1BBR0VfTUVUQURBVEEnKQogICAgICAgICAgICBkYXRhID0gdW5iNjQoY29udHJvbC5nZXQoJ2RhdGEnLCAnJykpOyBjaHVuayA9IHVuYjY0KHJlcGx5WydjaHVuayddKQogICAgICAgICAgICByZXF1aXJlKHFbJ29mZnNldCddID09IGxlbihkYXRhKSBhbmQgbGVuKGRhdGEpIDw9IHJlcGx5Wyd0b3RhbCddCiAgICAgICAgICAgICAgICAgICAgYW5kIGxlbihjaHVuaykgPT0gbWluKDMyNzY4LCByZXBseVsndG90YWwnXSAtIGxlbihkYXRhKSksICdSRVNVTFRfUEFHRV9MRU5HVEgnKQogICAgICAgICAgICBmb3Iga2V5IGluICgndG90YWwnLCAncmVzdWx0X3NoYTI1NicsICdhdXRob3JpemF0aW9uX3ZlcnNpb24nKToKICAgICAgICAgICAgICAgIHJlcXVpcmUoa2V5IG5vdCBpbiBjb250cm9sIG9yIGNvbnRyb2xba2V5XSA9PSByZXBseVtrZXldLCAnUkVTVUxUX1BBR0VfQ09ORkxJQ1QnKQogICAgICAgICAgICAgICAgY29udHJvbFtrZXldID0gcmVwbHlba2V5XQogICAgICAgICAgICBkYXRhICs9IGNodW5rCiAgICAgICAgICAgIGNvbnRyb2xbJ2RhdGEnXSA9IGI2NChkYXRhKQogICAgICAgICAgICBjb250cm9sLnBvcCgncGVuZGluZycpOyBjb250cm9sWyduZXh0J10gKz0gMQogICAgICAgICAgICBpZiBsZW4oZGF0YSkgPT0gcmVwbHlbJ3RvdGFsJ106CiAgICAgICAgICAgICAgICByZXF1aXJlKGhhc2hsaWIuc2hhMjU2KGRhdGEpLmhleGRpZ2VzdCgpLnVwcGVyKCkgPT0gcmVwbHlbJ3Jlc3VsdF9zaGEyNTYnXSwgJ1JFU1VMVF9UT1RBTF9IQVNIJykKICAgICAgICAgICAgICAgIHJlc3VsdCA9IF9vcmlnaW5hbF9yZXN1bHRfZGVjb2RlKGMsIGRhdGEpCiAgICAgICAgICAgICAgICBjb250cm9sWydjb21wbGV0ZSddID0gVHJ1ZQogICAgICAgICAgICAgICAgX3Jlc3VsdF9jb250cm9sX3NhdmUocGF0aCwgY29udHJvbCkKICAgICAgICAgICAgICAgIGFuc3dlciA9IHsnc3RhdHVzJzogJ29yaWdpbmFsX3Jlc3VsdF9yZWFkJywgJ29wZXJhdGlvbl9pZCc6IHBlbmRpbmdbJ2lkJ10sICdyZXBseSc6IHJlc3VsdCwKICAgICAgICAgICAgICAgICAgICAgICAgICAncGVuZGluZ19wcmVzZXJ2ZWQnOiBUcnVlLCAnYnVzaW5lc3NfcmVwbGF5ZWQnOiBGYWxzZX0KICAgICAgICAgICAgICAgIHByaW50KGpzb24uZHVtcHMoYW5zd2VyLCBlbnN1cmVfYXNjaWk9RmFsc2UsIGluZGVudD0yKSk7IHJldHVybiBhbnN3ZXIKICAgICAgICAgICAgX3Jlc3VsdF9jb250cm9sX3NhdmUocGF0aCwgY29udHJvbCkKICAgICAgICAgICAgY29udGludWUKICAgICAgICBpZiB0aW1lLnRpbWUoKSA+IGVudlsnaGVhZGVyJ11bJ2V4cGlyZXMnXToKICAgICAgICAgICAgIyBQdWJsaXNoIGFuIHVuc2VudCBleHBpcmVkIHF1ZXJ5IEVYQUNUTFkgc28gdGhlIGhvc3QgY2FuIGF1dGhlbnRpY2F0ZSBhbmQgcmV0aXJlIHRoZSBzbG90LgogICAgICAgICAgICAjIE5ldmVyIGxlYXZlIGEgc2VxdWVudGlhbCBob2xlLCBhbmQgbmV2ZXIgcmUtc2VhbCB0aGlzIElELgogICAgICAgICAgICBvY2N1cGllZCA9IGJveC5hdCh0aXAsIG1lc3NhZ2VfcGF0aChjLCAncmVzdWx0cXVlcnknLCBvcCkpCiAgICAgICAgICAgIHJlcXVpcmUob2NjdXBpZWQgaXMgTm9uZSBvciBvY2N1cGllZCA9PSByYXdfcXVlcnksICdSRVNVTFRfUVVFUllfT0NDVVBJRUQnKQogICAgICAgICAgICBpZiBvY2N1cGllZCBpcyBOb25lOiBib3guY3JlYXRlKCdyZXN1bHRxdWVyeScsIG9wLCByYXdfcXVlcnkpCiAgICAgICAgICAgICMgQW4gYXV0aGVudGljYXRlZCBvcmlnaW5hbCBxdWVyeSBwZXJzaXN0ZWQgYnkgdXMgY2FuIGJlIGFiYW5kb25lZCBPTkxZIGluIHRoaXMgY29udHJvbCBsYW5lLgogICAgICAgICAgICBjb250cm9sLnBvcCgncGVuZGluZycpOyBjb250cm9sWyduZXh0J10gKz0gMQogICAgICAgICAgICBfcmVzdWx0X2NvbnRyb2xfc2F2ZShwYXRoLCBjb250cm9sKTsgY29udGludWUKICAgICAgICBvY2N1cGllZCA9IGJveC5hdCh0aXAsIG1lc3NhZ2VfcGF0aChjLCAncmVzdWx0cXVlcnknLCBvcCkpCiAgICAgICAgcmVxdWlyZShvY2N1cGllZCBpcyBOb25lIG9yIG9jY3VwaWVkID09IHJhd19xdWVyeSwgJ1JFU1VMVF9RVUVSWV9PQ0NVUElFRCcpCiAgICAgICAgaWYgb2NjdXBpZWQgaXMgTm9uZTogYm94LmNyZWF0ZSgncmVzdWx0cXVlcnknLCBvcCwgcmF3X3F1ZXJ5KQogICAgICAgIGlmIHRpbWUubW9ub3RvbmljKCkgPj0gZGVhZGxpbmU6CiAgICAgICAgICAgIGFuc3dlciA9IHsnc3RhdHVzJzogJ3Jlc3VsdF9yZWFkX3BlbmRpbmcnLCAnb3BlcmF0aW9uX2lkJzogcGVuZGluZ1snaWQnXSwgJ3BlbmRpbmdfcHJlc2VydmVkJzogVHJ1ZSwgJ2J1c2luZXNzX3JlcGxheWVkJzogRmFsc2V9CiAgICAgICAgICAgIHByaW50KGpzb24uZHVtcHMoYW5zd2VyLCBlbnN1cmVfYXNjaWk9RmFsc2UpKTsgcmV0dXJuIGFuc3dlcgogICAgICAgIHRpbWUuc2xlZXAobWluKDIsIG1heCgwLCBkZWFkbGluZSAtIHRpbWUubW9ub3RvbmljKCkpKSkKICAgIGFuc3dlciA9IHsnc3RhdHVzJzogJ3Jlc3VsdF9yZWFkX3BlbmRpbmcnLCAnb3BlcmF0aW9uX2lkJzogcGVuZGluZ1snaWQnXSwgJ3BlbmRpbmdfcHJlc2VydmVkJzogVHJ1ZSwgJ2J1c2luZXNzX3JlcGxheWVkJzogRmFsc2V9CiAgICBwcmludChqc29uLmR1bXBzKGFuc3dlciwgZW5zdXJlX2FzY2lpPUZhbHNlKSk7IHJldHVybiBhbnN3ZXIKCgojIEV4cGxpY2l0IG5ldy13b3JrIGVudHJ5IG9ubHkuIE5ldmVyIGludm9rZWQgYnkgc3RhdHVzLCBoZWFydGJlYXQgb3IgYmFja2dyb3VuZCByZXRyaWVzLgpkZWYgZW5zdXJlX2FjdGl2ZShjLCBzLCBib3gsIHdhaXQ9MTgwKToKICAgIHJlcXVpcmUocy52YWx1ZS5nZXQoJ2FjdGl2ZScpIGFuZCBzLnZhbHVlLmdldCgna2V5cycpLCAnQUNUSVZBVElPTl9PUklHSU5BTF9JREVOVElUWV9SRVFVSVJFRCcpCiAgICByZXF1aXJlKG5vdCBzLnZhbHVlLmdldCgncGVuZGluZycpLCAnQUNUSVZBVElPTl9QRU5ESU5HX1BSRVNFUlZFRDogcXVlcnkgdGhlIG9yaWdpbmFsIG9wZXJhdGlvbjsgZG8gbm90IHN3aXRjaCBvciByZXBsYXknKQogICAgcGF0aCA9IHMucm9vdCAvICdhY3RpdmF0aW9uLmpzb24nCiAgICBzY29wZSA9IGhhc2hsaWIuc2hhMjU2KGNhbm9uaWNhbChjKSkuaGV4ZGlnZXN0KCkKICAgIGNvbnRyb2wgPSBfc3RhdGVfanNvbihfc3RhdGVfcmVhZChwYXRoKSkgaWYgcGF0aC5leGlzdHMoKSBlbHNlIHsnc2NvcGUnOiBzY29wZSwgJ25leHQnOiAwLCAncGhhc2UnOiAncHJvYmUnfQogICAgcmVxdWlyZShjb250cm9sLmdldCgnc2NvcGUnKSA9PSBzY29wZSBhbmQgdHlwZShjb250cm9sLmdldCgnbmV4dCcpKSBpcyBpbnQgYW5kIDAgPD0gY29udHJvbFsnbmV4dCddIDwgMTAwMDAwMCwgJ0FDVElWQVRJT05fU1RBVEUnKQogICAgaWYgY29udHJvbC5nZXQoJ3BoYXNlJykgPT0gJ2RvbmUnOiBjb250cm9sWydwaGFzZSddID0gJ3Byb2JlJwogICAgZGVhZGxpbmUgPSB0aW1lLm1vbm90b25pYygpICsgbWF4KDAsIG1pbih3YWl0LCAzMDApKQogICAgd2hpbGUgVHJ1ZToKICAgICAgICBpZiAncGVuZGluZycgbm90IGluIGNvbnRyb2w6CiAgICAgICAgICAgIHBoYXNlID0gY29udHJvbC5nZXQoJ3BoYXNlJywgJ3Byb2JlJykKICAgICAgICAgICAgcmVxdWlyZShwaGFzZSBpbiAoJ3Byb2JlJywgJ2NsYWltJywgJ2NvbmZpcm0nKSwgJ0FDVElWQVRJT05fUEhBU0UnKQogICAgICAgICAgICBxID0gZGljdChwcm90b2NvbD0nc2N4LWFjdGl2YXRpb24tdjEnLCBwcm9qZWN0X2lkPWNbJ3Byb2plY3RfaWQnXSwgYWN0aW9uPSdjbGFpbScgaWYgcGhhc2UgPT0gJ2NsYWltJyBlbHNlICdwcm9iZScsCiAgICAgICAgICAgICAgICAgICAgIGNoYWxsZW5nZT1zZWNyZXRzLnRva2VuX2hleCgzMiksIGdlbmVyYXRpb249Y29udHJvbC5nZXQoJ2dlbmVyYXRpb24nLCAnJykgaWYgcGhhc2UgPT0gJ2NsYWltJyBlbHNlICcnLAogICAgICAgICAgICAgICAgICAgICBhdXRob3JpemF0aW9uX3ZlcnNpb249Y29udHJvbC5nZXQoJ2dyYW50JywgJycpIGlmIHBoYXNlID09ICdjbGFpbScgZWxzZSAnJykKICAgICAgICAgICAgb3AgPSAnYWN0LSUwNmQnICUgY29udHJvbFsnbmV4dCddCiAgICAgICAgICAgIGNsYXNzIENvbnRyb2xJZGVudGl0eTogcGFzcwogICAgICAgICAgICBkZXRhY2hlZCA9IENvbnRyb2xJZGVudGl0eSgpOyBkZXRhY2hlZC52YWx1ZSA9IGRpY3Qocy52YWx1ZSkKICAgICAgICAgICAgZGV0YWNoZWQudmFsdWVbJ3R4X25vbmNlcyddID0gbGlzdChzLnZhbHVlLmdldCgndHhfbm9uY2VzJywgW10pKSArIGxpc3QoY29udHJvbC5nZXQoJ3R4X25vbmNlcycsIFtdKSkKICAgICAgICAgICAgZW52ID0gc2VhbChjLCBkZXRhY2hlZCwgb3AsIGNhbm9uaWNhbChxKSwgbGlmZXRpbWU9MTgwKQogICAgICAgICAgICBjb250cm9sWyd0eF9ub25jZXMnXSA9IChjb250cm9sLmdldCgndHhfbm9uY2VzJywgW10pICsgW2Vudlsnbm9uY2UnXV0pWy04MTkyOl0KICAgICAgICAgICAgY29udHJvbFsncGVuZGluZyddID0gZGljdChpZD1vcCwgcXVlcnk9cSwgZW52ZWxvcGU9ZW52LCBwaGFzZT1waGFzZSkKICAgICAgICAgICAgX3Jlc3VsdF9jb250cm9sX3NhdmUocGF0aCwgY29udHJvbCkKICAgICAgICBpdGVtID0gY29udHJvbFsncGVuZGluZyddOyBvcCwgcSwgZW52LCBwaGFzZSA9IGl0ZW1bJ2lkJ10sIGl0ZW1bJ3F1ZXJ5J10sIGl0ZW1bJ2VudmVsb3BlJ10sIGl0ZW1bJ3BoYXNlJ10KICAgICAgICByZXF1aXJlKG9wID09ICdhY3QtJTA2ZCcgJSBjb250cm9sWyduZXh0J10gYW5kIHFbJ3Byb2plY3RfaWQnXSA9PSBjWydwcm9qZWN0X2lkJ10sICdBQ1RJVkFUSU9OX1BFTkRJTkdfU0NPUEUnKQogICAgICAgIHJhdyA9IGNhbm9uaWNhbChlbnYpOyB0aXAgPSBib3guZmV0Y2goKQogICAgICAgIG9ic2VydmVkID0gYm94LmF0KHRpcCwgbWVzc2FnZV9wYXRoKGMsICdhY3RpdmF0aW9ucmVwbHknLCBvcCkpCiAgICAgICAgaWYgb2JzZXJ2ZWQgaXMgbm90IE5vbmU6CiAgICAgICAgICAgIHJlcGx5LCBmcmVzaCA9IF9hY3RpdmF0aW9uX3JlcGx5KGMsIHMsIG9wLCBsb2FkX2pzb24ob2JzZXJ2ZWQpKQogICAgICAgICAgICBleGFjdChyZXBseSwgWydwcm90b2NvbCcsICdxdWVyeScsICdxdWVyeV9zaGEyNTYnLCAnc3RhdHVzJywgJ2dlbmVyYXRpb24nLCAnYXV0aG9yaXphdGlvbl92ZXJzaW9uJywgJ2N1cnJlbnQnXSkKICAgICAgICAgICAgcmVxdWlyZShyZXBseVsncHJvdG9jb2wnXSA9PSAnc2N4LWFjdGl2YXRpb24tdjEnIGFuZCByZXBseVsncXVlcnknXSA9PSBxCiAgICAgICAgICAgICAgICAgICAgYW5kIHJlcGx5WydxdWVyeV9zaGEyNTYnXSA9PSBoYXNobGliLnNoYTI1NihyYXcpLmhleGRpZ2VzdCgpLnVwcGVyKCkKICAgICAgICAgICAgICAgICAgICBhbmQgdHlwZShyZXBseVsnY3VycmVudCddKSBpcyBib29sLCAnQUNUSVZBVElPTl9QUk9PRl9NSVNNQVRDSCcpCiAgICAgICAgICAgIGNvbnRyb2wucG9wKCdwZW5kaW5nJyk7IGNvbnRyb2xbJ25leHQnXSArPSAxCiAgICAgICAgICAgIGdvb2QgPSBmcmVzaCBhbmQgcmVwbHlbJ3N0YXR1cyddIGluICgnYXZhaWxhYmxlJywgJ3NlbGVjdGVkJykKICAgICAgICAgICAgaWYgbm90IGdvb2Q6CiAgICAgICAgICAgICAgICBjb250cm9sWydwaGFzZSddID0gJ2RvbmUnOyBfcmVzdWx0X2NvbnRyb2xfc2F2ZShwYXRoLCBjb250cm9sKQogICAgICAgICAgICAgICAgcmFpc2UgU3RvcCgnQUNUSVZBVElPTl9OT1RfR1JBTlRFRDogJyArIChyZXBseVsnc3RhdHVzJ10gaWYgZnJlc2ggZWxzZSAnZXhwaXJlZCcpICsgJzsgbm8gYnVzaW5lc3MgZGlzcGF0Y2hlZCcpCiAgICAgICAgICAgIHJlcXVpcmUoaXNpbnN0YW5jZShyZXBseVsnZ2VuZXJhdGlvbiddLCBzdHIpIGFuZCByZS5mdWxsbWF0Y2gocidbQS1GMC05XXs2NH0nLCByZXBseVsnZ2VuZXJhdGlvbiddKQogICAgICAgICAgICAgICAgICAgIGFuZCBpc2luc3RhbmNlKHJlcGx5WydhdXRob3JpemF0aW9uX3ZlcnNpb24nXSwgc3RyKSBhbmQgMCA8IGxlbihyZXBseVsnYXV0aG9yaXphdGlvbl92ZXJzaW9uJ10pIDw9IDI1NiwgJ0FDVElWQVRJT05fUFJPT0ZfRklFTERTJykKICAgICAgICAgICAgaWYgcGhhc2UgaW4gKCdwcm9iZScsICdjb25maXJtJykgYW5kIHJlcGx5WydjdXJyZW50J106CiAgICAgICAgICAgICAgICBjb250cm9sWydwaGFzZSddID0gJ2RvbmUnOyBfcmVzdWx0X2NvbnRyb2xfc2F2ZShwYXRoLCBjb250cm9sKQogICAgICAgICAgICAgICAgcHJpbnQoJ09OTElORV9TRUxFQ1RFRDogb3JpZ2luYWwgaWRlbnRpdHkgc2VsZWN0ZWQ7IG1vZGVsIGFjdGl2aXR5IGFuZCBidXNpbmVzcyBzdWNjZXNzIGFyZSBub3QgaW1wbGllZCcsIGZsdXNoPVRydWUpCiAgICAgICAgICAgICAgICByZXR1cm4KICAgICAgICAgICAgaWYgcGhhc2UgPT0gJ2NvbmZpcm0nOgogICAgICAgICAgICAgICAgY29udHJvbFsncGhhc2UnXSA9ICdkb25lJzsgX3Jlc3VsdF9jb250cm9sX3NhdmUocGF0aCwgY29udHJvbCkKICAgICAgICAgICAgICAgIHJhaXNlIFN0b3AoJ0FDVElWQVRJT05fU1VQRVJTRURFRDogYW5vdGhlciBpZGVudGl0eSBiZWNhbWUgY3VycmVudDsgbm8gYXV0b21hdGljIHJlY2xhaW0nKQogICAgICAgICAgICBjb250cm9sWydwaGFzZSddID0gJ2NsYWltJyBpZiBwaGFzZSA9PSAncHJvYmUnIGVsc2UgJ2NvbmZpcm0nCiAgICAgICAgICAgIGNvbnRyb2xbJ2dlbmVyYXRpb24nXSwgY29udHJvbFsnZ3JhbnQnXSA9IHJlcGx5WydnZW5lcmF0aW9uJ10sIHJlcGx5WydhdXRob3JpemF0aW9uX3ZlcnNpb24nXQogICAgICAgICAgICBfcmVzdWx0X2NvbnRyb2xfc2F2ZShwYXRoLCBjb250cm9sKQogICAgICAgICAgICBjb250aW51ZQogICAgICAgIG9jY3VwaWVkID0gYm94LmF0KHRpcCwgbWVzc2FnZV9wYXRoKGMsICdhY3RpdmF0aW9ucXVlcnknLCBvcCkpCiAgICAgICAgcmVxdWlyZShvY2N1cGllZCBpcyBOb25lIG9yIG9jY3VwaWVkID09IHJhdywgJ0FDVElWQVRJT05fUVVFUllfT0NDVVBJRUQnKQogICAgICAgIGlmIG9jY3VwaWVkIGlzIE5vbmU6IGJveC5jcmVhdGUoJ2FjdGl2YXRpb25xdWVyeScsIG9wLCByYXcpCiAgICAgICAgaWYgdGltZS50aW1lKCkgPiBlbnZbJ2hlYWRlciddWydleHBpcmVzJ106CiAgICAgICAgICAgICMgUmV0aXJlIGV4YWN0IGV4cGlyZWQgY29udHJvbCBieXRlczsgbmV2ZXIgcmVzZWFsIG9yIHJlcGxhY2UgYSBwZW5kaW5nIGJ1c2luZXNzIHJlcXVlc3QuCiAgICAgICAgICAgIGNvbnRyb2wucG9wKCdwZW5kaW5nJyk7IGNvbnRyb2xbJ25leHQnXSArPSAxOyBjb250cm9sWydwaGFzZSddID0gJ2RvbmUnCiAgICAgICAgICAgIF9yZXN1bHRfY29udHJvbF9zYXZlKHBhdGgsIGNvbnRyb2wpCiAgICAgICAgICAgIHJhaXNlIFN0b3AoJ0FDVElWQVRJT05fRVhQSVJFRDogb3JpZ2luYWwgY29udHJvbCByZXF1ZXN0IHByZXNlcnZlZDsgbm8gYXV0b21hdGljIGZyZXNoIGNsYWltJykKICAgICAgICBpZiB0aW1lLm1vbm90b25pYygpID49IGRlYWRsaW5lOgogICAgICAgICAgICByYWlzZSBTdG9wKCdBQ1RJVkFUSU9OX1dBSVRJTkc6IGNvbnRyb2wgcmVxdWVzdCBzYXZlZDsgcmV0cnkgdGhlIHNhbWUgZW50cnksIG5vdCBhIGJ1c2luZXNzIG9wZXJhdGlvbicpCiAgICAgICAgdGltZS5zbGVlcChtaW4oNSwgbWF4KDAsIGRlYWRsaW5lIC0gdGltZS5tb25vdG9uaWMoKSkpKQoKCmRlZiBfYWN0aXZhdGlvbl9yZXBseShjLCBzLCBvcGVyYXRpb24sIGVudik6CiAgICByZXF1aXJlKHJlLmZ1bGxtYXRjaChyJ2FjdC1bMC05XXs2fScsIG9wZXJhdGlvbiksICdBQ1RJVkFUSU9OX0lEJykKICAgIGV4YWN0KGVudiwgWydoZWFkZXInLCAnbm9uY2UnLCAnY2lwaGVydGV4dCcsICd0YWcnXSkKICAgIGggPSBlbnZbJ2hlYWRlciddOyBleGFjdChoLCBsaXN0KHBpbnMoYykpICsgWydyZXF1ZXN0X2lkJywgJ2RpcmVjdGlvbicsICdleHBpcmVzJ10pCiAgICByZXF1aXJlKGFsbChoW2tdID09IHYgZm9yIGssIHYgaW4gcGlucyhjKS5pdGVtcygpKSBhbmQgaFsncmVxdWVzdF9pZCddID09IG9wZXJhdGlvbgogICAgICAgICAgICBhbmQgaFsnZGlyZWN0aW9uJ10gPT0gJ3JlcycgYW5kIHR5cGUoaFsnZXhwaXJlcyddKSBpcyBpbnQgYW5kIGhbJ2V4cGlyZXMnXSA+IDAsICdBQ1RJVkFUSU9OX1JFUExZX1NDT1BFJykKICAgIF8sIF8sIF8sIF8sIEFFU0dDTSA9IGNyeXB0bygpCiAgICBjaXBoZXJ0ZXh0ID0gdW5iNjQoZW52WydjaXBoZXJ0ZXh0J10pOyByZXF1aXJlKGxlbihjaXBoZXJ0ZXh0KSA8PSAxMjAwMCwgJ0FDVElWQVRJT05fUkVQTFlfTElNSVQnKQogICAgcGxhaW4gPSBBRVNHQ00odW5iNjQocy52YWx1ZVsna2V5cyddLCAxMjgpWzMyOjY0XSkuZGVjcnlwdCh1bmI2NChlbnZbJ25vbmNlJ10sIDEyKSwgY2lwaGVydGV4dCArIHVuYjY0KGVudlsndGFnJ10sIDE2KSwgY2Fub25pY2FsKGgpKQogICAgYm9keSA9IGxvYWRfanNvbihwbGFpbiwgbGltaXQ9MTIwMDApOyBleGFjdChib2R5LCBbJ3Byb2plY3RfaWQnLCAncmVwbHknXSkKICAgIHJlcXVpcmUoYm9keVsncHJvamVjdF9pZCddID09IGNbJ3Byb2plY3RfaWQnXSwgJ0FDVElWQVRJT05fUFJPSkVDVCcpCiAgICByZXR1cm4gYm9keVsncmVwbHknXSwgdGltZS50aW1lKCkgLSAxIDw9IGhbJ2V4cGlyZXMnXSA8PSB0aW1lLnRpbWUoKSArIDE4MDEKCgpkZWYgbWFpbigpOgogICAgcGFyc2VyID0gYXJncGFyc2UuQXJndW1lbnRQYXJzZXIoZGVzY3JpcHRpb249J1VzZSBhbiBleHBsaWNpdGx5IGF1dGhvcml6ZWQgU2h1bkNvZGV4IGNvbGxhYm9yYXRpb24gc2Vzc2lvbi4nKQogICAgcGFyc2VyLmFkZF9hcmd1bWVudCgnLS1jb25maWcnLCB0eXBlPVBhdGgsIGRlZmF1bHQ9UGF0aChfX2ZpbGVfXykucmVzb2x2ZSgpLndpdGhfbmFtZSgnY29ubmVjdGlvbi5qc29uJykpCiAgICBwYXJzZXIuYWRkX2FyZ3VtZW50KCctLXJlcG8nLCB0eXBlPVBhdGgsIGRlZmF1bHQ9UGF0aC5jd2QoKSkKICAgIHN1YiA9IHBhcnNlci5hZGRfc3VicGFyc2VycyhkZXN0PSdjb21tYW5kJywgcmVxdWlyZWQ9VHJ1ZSkKICAgIHN1Yi5hZGRfcGFyc2VyKCdjb25uZWN0Jyk7IHN1Yi5hZGRfcGFyc2VyKCdsaXN0JykKICAgIG9ubGluZV9wYXJzZXIgPSBzdWIuYWRkX3BhcnNlcignZW5zdXJlLWFjdGl2ZScpOyBvbmxpbmVfcGFyc2VyLmFkZF9hcmd1bWVudCgnLS13YWl0JywgdHlwZT1pbnQsIGRlZmF1bHQ9MTgwKQogICAgc3RhdHVzX3BhcnNlciA9IHN1Yi5hZGRfcGFyc2VyKCdzdGF0dXMnKTsgc3RhdHVzX3BhcnNlci5hZGRfYXJndW1lbnQoJy0td2FpdCcsIHR5cGU9aW50LCBkZWZhdWx0PTAsIGhlbHA9J3NlY29uZHMgdG8ga2VlcCBwb2xsaW5nIGZvciB0aGUgcGVuZGluZyByZXNwb25zZScpCiAgICByZXN1bHRfcGFyc2VyID0gc3ViLmFkZF9wYXJzZXIoJ3Jlc3VsdC1yZWFkJyk7IHJlc3VsdF9wYXJzZXIuYWRkX2FyZ3VtZW50KCctLXdhaXQnLCB0eXBlPWludCwgZGVmYXVsdD0wKQogICAgYWN0aXZhdGVfcGFyc2VyID0gc3ViLmFkZF9wYXJzZXIoJ2FjdGl2YXRlJyk7IGFjdGl2YXRlX3BhcnNlci5hZGRfYXJndW1lbnQoJy0tYXBwcm92ZWQnLCBhY3Rpb249J3N0b3JlX3RydWUnKQogICAgYWN0aXZhdGVfcGFyc2VyLmFkZF9hcmd1bWVudCgnLS13YWl0JywgdHlwZT1pbnQsIGRlZmF1bHQ9MCwgaGVscD0nc2Vjb25kcyB0byBwb2xsIGZvciB0aGUgbG9jYWwgY29uZmlybWF0aW9uIGJlZm9yZSBnaXZpbmcgdXAnKQogICAgY2FsbF9wYXJzZXIgPSBzdWIuYWRkX3BhcnNlcignY2FsbCcpOyBjYWxsX3BhcnNlci5hZGRfYXJndW1lbnQoJ25hbWUnKTsgY2FsbF9wYXJzZXIuYWRkX2FyZ3VtZW50KCctLWFyZ3VtZW50cycsIGRlZmF1bHQ9J3t9JykKICAgIGNhbGxfcGFyc2VyLmFkZF9hcmd1bWVudCgnLS13YWl0JywgdHlwZT1pbnQsIGRlZmF1bHQ9OTAsIGhlbHA9J3NlY29uZHMgdG8gd2FpdCBmb3IgdGhlIHJlc3BvbnNlIGJlZm9yZSByZXR1cm5pbmcgcmVzcG9uc2Vfbm90X3JlY2VpdmVkJykKICAgIG9wdHMgPSBwYXJzZXIucGFyc2VfYXJncygpCiAgICBjID0gbG9hZF9qc29uKG9wdHMuY29uZmlnLnJlYWRfYnl0ZXMoKSk7IHZhbGlkYXRlX2NvbmZpZyhjKQogICAgIyBUcmFuc3BvcnQ6IGdpdCB3aGVuIGF2YWlsYWJsZSAoQXJlbmEtc3R5bGUgc2FuZGJveGVzKTsgb3RoZXJ3aXNlIHRoZSBHaXRIdWIgUkVTVCBBUEkgd2l0aCBhIHRva2VuIGZyb20gdGhlIGVudmlyb25tZW50LgogICAgdHJhbnNwb3J0ID0gb3MuZW52aXJvbi5nZXQoJ0xFQk9YX1RSQU5TUE9SVCcsIG9zLmVudmlyb24uZ2V0KCdTQ1hfVFJBTlNQT1JUJywgJycpKS5zdHJpcCgpLmxvd2VyKCkKICAgIHRva2VuX2VudiA9IG9zLmVudmlyb24uZ2V0KCdMRUJPWF9UT0tFTl9FTlYnLCBvcy5lbnZpcm9uLmdldCgnU0NYX1RPS0VOX0VOVicsICdHSVRIVUJfVE9LRU4nKSkKICAgIGhhdmVfZ2l0ID0gc2h1dGlsLndoaWNoKCdnaXQnKSBpcyBub3QgTm9uZQogICAgdXNlX2FwaSA9IHRyYW5zcG9ydCA9PSAnYXBpJyBvciAobm90IGhhdmVfZ2l0IGFuZCB0cmFuc3BvcnQgIT0gJ2dpdCcpCiAgICBpZiB1c2VfYXBpOgogICAgICAgIHRva2VuID0gb3MuZW52aXJvbi5nZXQodG9rZW5fZW52LCAnJykKICAgICAgICByZXBvID0gUGF0aChvcHRzLnJlcG8pLnJlc29sdmUoKQogICAgZWxzZToKICAgICAgICAjIEVzdGFibGlzaCB3b3JrdHJlZSBib3VuZGFyeSB3aXRob3V0IHJ1bm5pbmcgaG9va3Mgb3IgY2hlY2tvdXQgZmlsdGVycy4KICAgICAgICByZXN1bHQgPSBzdWJwcm9jZXNzLnJ1bihbJ2dpdCcsICctYycsICdjb3JlLmZzbW9uaXRvcj1mYWxzZScsICctQycsIHN0cihvcHRzLnJlcG8pLCAncmV2LXBhcnNlJywgJy0tc2hvdy10b3BsZXZlbCddLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIHN0ZG91dD1zdWJwcm9jZXNzLlBJUEUsIHN0ZGVycj1zdWJwcm9jZXNzLlBJUEUsIHRpbWVvdXQ9MTAsIGNoZWNrPUZhbHNlKQogICAgICAgIHJlcXVpcmUocmVzdWx0LnJldHVybmNvZGUgPT0gMCwgJ1J1biBmcm9tIHRoZSBhdXRob3JpemVkIEdpdCByZXBvc2l0b3J5LicpCiAgICAgICAgcmVwbyA9IFBhdGgocmVzdWx0LnN0ZG91dC5kZWNvZGUoKS5zdHJpcCgpKS5yZXNvbHZlKCkKICAgIGJvdW5kYXJ5ID0gc3RhdGVfcmVwb19ib3VuZGFyeShyZXBvKSBpZiB1c2VfYXBpIGVsc2UgcmVwbwogICAgdHJ5OgogICAgICAgIHJvb3QgPSBwZXJzaXN0ZW50X3Nlc3Npb25fZGlyZWN0b3J5KGMsIGJvdW5kYXJ5KQogICAgZXhjZXB0IChTdGF0ZUxvY2F0aW9uRXJyb3IsIE9TRXJyb3IpIGFzIGVycm9yOgogICAgICAgIHJhaXNlIFN0b3AoJ1ByaXZhdGUgc3RhdGUgcmVjb3ZlcnkgdW5hdmFpbGFibGU6ICcgKyAoc3RyKGVycm9yKSBpZiBpc2luc3RhbmNlKGVycm9yLCBTdGF0ZUxvY2F0aW9uRXJyb3IpIGVsc2UgdHlwZShlcnJvcikuX19uYW1lX18pKQogICAgaWYgb3B0cy5jb21tYW5kIG5vdCBpbiAoJ2Nvbm5lY3QnLCAnYWN0aXZhdGUnKToKICAgICAgICByZXF1aXJlKChyb290IC8gJ3N0YXRlLmpzb24nKS5leGlzdHMoKSwgJ09yaWdpbmFsIHByaXZhdGUgc3RhdGUgaXMgbWlzc2luZzsgcHJlc2VydmUgcGVuZGluZyBldmlkZW5jZSBhbmQgcmVjb3ZlciB0aGUgb3JpZ2luYWwgaWRlbnRpdHksIGRvIG5vdCBjcmVhdGUgYSBuZXcgc2Vzc2lvbi4nKQogICAgIyBDb25zdHJ1Y3Qvc2F2ZSBzdGF0ZSBvbmx5IGFmdGVyIGhvbGRpbmcgdGhlIHBlci1zZXNzaW9uIGV4Y2x1c2l2ZSBsb2NrLgogICAgcmVxdWlyZShub3Qgcm9vdC5pc19zeW1saW5rKCkgYW5kIChib3VuZGFyeSBpcyBOb25lIG9yIG5vdCByb290LnJlc29sdmUoKS5pc19yZWxhdGl2ZV90byhib3VuZGFyeSkpLCAnUHJpdmF0ZSBzdGF0ZSBsb2NhdGlvbiBpcyB1bnNhZmUuJykKICAgIHJvb3QubWtkaXIobW9kZT0wbzcwMCwgZXhpc3Rfb2s9VHJ1ZSk7IG9zLmNobW9kKHJvb3QsIDBvNzAwKQogICAgd2l0aCBleGNsdXNpdmUocm9vdCk6CiAgICAgICAgcyA9IFByaXZhdGVTdGF0ZShyb290LCBjLCBib3VuZGFyeSkKICAgICAgICBpZiBvcHRzLmNvbW1hbmQgbm90IGluICgncmVzdWx0LXJlYWQnLCAnZW5zdXJlLWFjdGl2ZScpOiByZW1lbWJlcl9yZWNvdmVyeV9jb250ZXh0KGMsIHMpCiAgICAgICAgaWYgcy52YWx1ZS5nZXQoJ3JlbmV3ZWRfdG9rZW4nKToKICAgICAgICAgICAgdG9rZW4gPSBzLnZhbHVlWydyZW5ld2VkX3Rva2VuJ10KICAgICAgICAgICAgcmVxdWlyZShpc2luc3RhbmNlKHRva2VuLCBzdHIpIGFuZCA4IDw9IGxlbih0b2tlbikgPD0gNDA5NiBhbmQgbm90IGFueSh4Lmlzc3BhY2UoKSBmb3IgeCBpbiB0b2tlbiksICdJbnZhbGlkIHNhdmVkIHJlbmV3YWwgdG9rZW4uJykKICAgICAgICAgICAgb3MuZW52aXJvblt0b2tlbl9lbnZdID0gdG9rZW4KICAgICAgICAgICAgb3MuZW52aXJvblsnR0lUSFVCX1RPS0VOJ10gPSB0b2tlbgogICAgICAgICAgICBvcy5lbnZpcm9uWydMRUJPWF9UT0tFTiddID0gdG9rZW4KICAgICAgICBpZiB1c2VfYXBpOgogICAgICAgICAgICByZXF1aXJlKHRva2VuLCAnT3JpZ2luYWwgYWNjZXNzIGNyZWRlbnRpYWwgdW5hdmFpbGFibGU7IHByZXNlcnZlIG9yaWdpbmFsIGlkZW50aXR5IGFuZCBwZW5kaW5nIG9wZXJhdGlvbi4nKQogICAgICAgIGVsc2U6CiAgICAgICAgICAgICMgQSBzYXZlZCwgdmVyaWZpZWQgcmVuZXdhbCBvciBleHBsaWNpdCBCIGNyZWRlbnRpYWwgY2FuIG91dGxpdmUgdGhlIG9sZCBoZWxwZXIgc29ja2V0LgogICAgICAgICAgICAjIE5ldmVyIHNlYXJjaCBhbm90aGVyIGNoYXQncyBjYWNoZSBvciBzaWxlbnRseSByZXBsYWNlIHBsYXRmb3JtLUEgY3JlZGVudGlhbHMuCiAgICAgICAgICAgIGdpdF90b2tlbiA9IHMudmFsdWUuZ2V0KCdyZW5ld2VkX3Rva2VuJywgJycpCiAgICAgICAgICAgIGlmIG5vdCBnaXRfdG9rZW4gYW5kIG9zLmVudmlyb24uZ2V0KCdMRUJPWF9BVVRIX1NPVVJDRScpIGluICgncGFzdGVkLXRva2VuJywgJ2RldmljZS1mbG93Jyk6CiAgICAgICAgICAgICAgICBnaXRfdG9rZW4gPSBvcy5lbnZpcm9uLmdldCgnTEVCT1hfVE9LRU4nLCAnJykKICAgICAgICAgICAgICAgIHJlcXVpcmUoZ2l0X3Rva2VuLCAnT3JpZ2luYWwgYWNjZXNzIGNyZWRlbnRpYWwgdW5hdmFpbGFibGU7IHByZXNlcnZlIG9yaWdpbmFsIGlkZW50aXR5IGFuZCBwZW5kaW5nIG9wZXJhdGlvbi4nKQogICAgICAgICAgICBpZiBnaXRfdG9rZW46CiAgICAgICAgICAgICAgICBvcy5lbnZpcm9uLnVwZGF0ZShfZ2l0X2F1dGhfcmVhZHlfZW52aXJvbm1lbnQoZ2l0X3Rva2VuLCBjWydyZXBvc2l0b3J5J10pKQogICAgICAgIGJveCA9IEFwaU1haWxib3goYywgdG9rZW4pIGlmIHVzZV9hcGkgZWxzZSBHaXRNYWlsYm94KHJlcG8sIGMsIHMucm9vdCkKICAgICAgICBpZiBvcHRzLmNvbW1hbmQgPT0gJ2Nvbm5lY3QnOiBjb25uZWN0KGMsIHMsIGJveCkKICAgICAgICBlbGlmIG9wdHMuY29tbWFuZCA9PSAnYWN0aXZhdGUnOiBhY3RpdmF0ZShjLCBzLCBib3gsIG9wdHMuYXBwcm92ZWQsIG9wdHMud2FpdCkKICAgICAgICBlbGlmIG9wdHMuY29tbWFuZCA9PSAnc3RhdHVzJzogc3RhdHVzKGMsIHMsIGJveCwgd2FpdD1tYXgoMCwgb3B0cy53YWl0KSkKICAgICAgICBlbGlmIG9wdHMuY29tbWFuZCA9PSAncmVzdWx0LXJlYWQnOiByZXN1bHRfcmVhZChjLCBzLCBib3gsIHdhaXQ9bWF4KDAsIG9wdHMud2FpdCkpCiAgICAgICAgZWxpZiBvcHRzLmNvbW1hbmQgPT0gJ2Vuc3VyZS1hY3RpdmUnOiBlbnN1cmVfYWN0aXZlKGMsIHMsIGJveCwgd2FpdD1tYXgoMCwgb3B0cy53YWl0KSkKICAgICAgICBlbGlmIG9wdHMuY29tbWFuZCA9PSAnbGlzdCc6IHJlcXVlc3QoYywgcywgYm94LCAndG9vbHMvbGlzdCcsIHt9KQogICAgICAgIGVsc2U6CiAgICAgICAgICAgIHJlcXVpcmUocmUuZnVsbG1hdGNoKHInW0EtWmEtel1bQS1aYS16MC05X117MCw5OX0nLCBvcHRzLm5hbWUpLCAnSW52YWxpZCB0b29sIG5hbWUuJykKICAgICAgICAgICAgYXJncyA9IGxvYWRfanNvbihvcHRzLmFyZ3VtZW50cy5lbmNvZGUoKSkKICAgICAgICAgICAgcmVxdWlyZShpc2luc3RhbmNlKGFyZ3MsIGRpY3QpLCAnVG9vbCBhcmd1bWVudHMgbXVzdCBiZSBhbiBvYmplY3QuJykKICAgICAgICAgICAgcmVxdWVzdChjLCBzLCBib3gsICd0b29scy9jYWxsJywgeyduYW1lJzogb3B0cy5uYW1lLCAnYXJndW1lbnRzJzogYXJnc30sIHdhaXQ9bWF4KDAsIG9wdHMud2FpdCkpCgppZiBfX25hbWVfXyA9PSAnX19tYWluX18nOgogICAgdHJ5OgogICAgICAgIG1haW4oKQogICAgZXhjZXB0IChTdG9wLCBTdGF0ZUxvY2F0aW9uRXJyb3IpIGFzIGV4OgogICAgICAgIHByaW50KGpzb24uZHVtcHMoeydzdGF0dXMnOiAnc3RvcHBlZCcsICdtZXNzYWdlJzogc3RyKGV4KX0sIGVuc3VyZV9hc2NpaT1GYWxzZSksIGZpbGU9c3lzLnN0ZGVycik7IHN5cy5leGl0KDIpCiAgICBleGNlcHQgKEtleWJvYXJkSW50ZXJydXB0LCBFeGNlcHRpb24pOgogICAgICAgICMgTmV2ZXIgcHJpbnQgcmF3IEdpdCBzdGRlcnIsIGNyZWRlbnRpYWwtYmVhcmluZyBVUkxzLCBwcml2YXRlIGtleSBzdGF0ZSBvciB0cmFjZWJhY2sgbG9jYWxzLgogICAgICAgIHByaW50KGpzb24uZHVtcHMoeydzdGF0dXMnOiAnbm90X2NvbmZpcm1lZCcsICdtZXNzYWdlJzogJ1RoZSBvcGVyYXRpb24gd2FzIGludGVycnVwdGVkIG9yIHJlamVjdGVkLiBLZWVwIHByaXZhdGUgc3RhdGUgYW5kIGluc3BlY3Qgc3RhdHVzOyBkbyBub3QgcmVwZWF0IGEgcG9zc2libHkgZGlzcGF0Y2hlZCBvcGVyYXRpb24uJ30pLCBmaWxlPXN5cy5zdGRlcnIpCiAgICAgICAgc3lzLmV4aXQoMykK'
EMBEDDED_CLIENT_SHA256 = 'c6c065c1388d341e2d4c00b7862d317c7e1114b31c8704e91e1d45cea660cb64'


def upgrade_client(connection_file):
    """Install verified code beside (never over) the old tool directory. Read identity; do not migrate or run it."""
    import contextlib, types
    raw = base64.b64decode(EMBEDDED_CLIENT_B64, validate=True)
    if not raw or hashlib.sha256(raw).hexdigest() != EMBEDDED_CLIENT_SHA256:
        stop('UPGRADE_CLIENT_PIN_REQUIRED：必须使用可信不可变发行包中的客户端', '不要运行未生成的源码或跳过旧 pin')
    client = types.ModuleType('lebox_verified_upgrade_client')
    client.__file__ = '<verified-client>'
    exec(compile(raw, client.__file__, 'exec'), client.__dict__)
    connection = _state_no_links(pathlib.Path(connection_file).expanduser())
    encoded = connection.read_bytes()
    config = client.load_json(encoded)
    client.validate_config(config)
    if REPO_HINT and config['repository'] != REPO_HINT or PROJECT_ID and config['project_id'] != PROJECT_ID:
        stop('UPGRADE_TARGET_MISMATCH：必须保留原项目、仓库与身份')
    digest = hashlib.sha256(client.canonical(config)).hexdigest()
    home = persistent_state_home(pathlib.Path.cwd())
    current = _state_no_links(home / ('scx-collaboration-' + config['binding']))
    legacy = _state_no_links(pathlib.Path(tempfile.gettempdir()) / current.name)
    candidates = [p for p in (current, legacy) if (p / 'state.json').exists()]
    if not candidates:
        stop('UPGRADE_IDENTITY_MISSING：原私有身份不存在', '保留原连接资料与 pending；不生成替代身份')
    with contextlib.ExitStack() as locks:
        for candidate in candidates:
            if state_repo_boundary(candidate) is not None:
                stop('UPGRADE_STATE_IN_REPOSITORY')
            locks.enter_context(_state_lock(candidate))
        values = [(p, _state_read(p / 'state.json')) for p in candidates]
        for path, value in values:
            record = _state_json(value)
            if record.get('config_hash') != digest or not record.get('private') or not record.get('hello') or not record.get('keys'):
                stop('UPGRADE_IDENTITY_MISMATCH：原身份不完整或属于其他连接')
            if len(base64.b64decode(record['keys'], validate=True)) != 128:
                stop('UPGRADE_IDENTITY_MISMATCH')
        if len(values) == 2 and values[0][1] != values[1][1]:
            proof = current / 'migration.json'
            expected = {'format': 'lebox-state-migration-v1', 'source': str(legacy.resolve()),
                        'source_sha256': hashlib.sha256(values[1][1]).hexdigest(), 'config_hash': digest}
            if not proof.exists() or _state_json(_state_read(proof)) != expected:
                stop('UPGRADE_STATE_CONFLICT：保留两份状态，禁止猜测原身份')
        # A fresh private code directory only. No credential copy, state rewrite, join, connect, or HTTP mutation.
        base = _state_no_links(home / 'verified-clients')
        base.mkdir(mode=0o700, exist_ok=True); os.chmod(base, 0o700)
        destination = pathlib.Path(tempfile.mkdtemp(prefix=EMBEDDED_CLIENT_SHA256[:16] + '-', dir=base))
        _state_write(destination / 'agent_collaboration.py', raw)
        _state_write(destination / 'connection.json', encoded)
        receipt = {'format': 'lebox-client-upgrade-v1', 'client_sha256': EMBEDDED_CLIENT_SHA256,
                   'config_sha256': digest, 'state_source': str(values[0][0]),
                   'state_sha256': hashlib.sha256(values[0][1]).hexdigest(), 'identity_replaced': False,
                   'state_migrated': False, 'business_replayed': False, 'pending_preserved': True}
        _state_write(destination / 'upgrade.json', _state_canonical(receipt))
        for path, value in values:
            if _state_read(path / 'state.json') != value:
                stop('UPGRADE_STATE_CHANGED：已停止，不运行新客户端')
    print('UPGRADE_READY: 已独立安装可信客户端；原身份、原连接资料、pending 和旧工具字节未改写。未授权、未配对、未运行业务。', flush=True)
    print('LEBOX_CLIENT=' + json.dumps({'python': sys.executable, 'script': str(destination / 'agent_collaboration.py'),
          'config': str(destination / 'connection.json'), 'client_sha256': EMBEDDED_CLIENT_SHA256,
          'original_result_retrieved': False}, ensure_ascii=False), flush=True)
    return destination


def self_check():
    """When agent.py.sha256 sits next to this file (in-repo copy published by the owner's client), refuse to run if the
    file was altered. Lets the one-line instructions omit the hash without giving up integrity."""
    here = os.path.abspath(__file__)
    side = here + '.sha256'
    if not os.path.exists(side):
        return
    try:
        want = open(side).read().split()[0].strip().lower()
        have = hashlib.sha256(open(here, 'rb').read()).hexdigest()
    except Exception:
        return
    if want != have:
        stop('agent.py 与同目录 agent.py.sha256 不一致（文件被修改或未完整更新）', '请用户在本地协作端点「连接 Agent」重新同步脚本；不要运行被改动的副本')
    print('OK: agent.py 自校验通过 (sha256=%s…)' % have[:16], flush=True)


def main():
    global JOIN_SUBJECT, PROJECT_ID
    self_check()
    global BRANCH_OVERRIDE
    args = sys.argv[1:]
    global API_REPO, REPO_HINT, CLIENT_ID, DEVICE_CODE
    if '--pair' in args:
        i = args.index('--pair')
        if i + 1 >= len(args):
            stop('--pair 需要一个配对码', '')
        DEVICE_CODE = args[i + 1].strip()
        del args[i:i + 2]
    if '--client-id' in args:
        i = args.index('--client-id')
        if i + 1 >= len(args):
            stop('--client-id 需要一个应用 ID', '')
        CLIENT_ID = args[i + 1].strip()
        del args[i:i + 2]
    if '--repo' in args:
        i = args.index('--repo')
        if i + 1 >= len(args):
            stop('--repo 需要 owner/name', '例如 --repo xiaomailele/lele')
        API_REPO = args[i + 1].strip()
        REPO_HINT = API_REPO
        del args[i:i + 2]
    if '--branch' in args:
        i = args.index('--branch')
        if i + 1 >= len(args):
            stop('--branch 需要一个分支名', '例如 --branch agent/codex-0922')
        BRANCH_OVERRIDE = args[i + 1].strip()
        del args[i:i + 2]
    if '--project-route' in args:
        i = args.index('--project-route')
        if i + 1 >= len(args) or not re.fullmatch(r'[0-9a-f]{32}', args[i+1]):
            stop('项目接入标识无效', '请重新复制当前项目的连接说明')
        JOIN_SUBJECT += ' project=' + args[i+1]
        del args[i:i+2]
    if '--project-id' in args:
        i = args.index('--project-id')
        if i + 1 >= len(args) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', args[i+1]):
            stop('项目标识无效', '请重新复制当前项目的说明')
        PROJECT_ID = args[i+1]
        del args[i:i+2]
    cmd = args[0] if args else 'join'
    if cmd == 'upgrade-client':
        if len(args) != 2:
            stop('upgrade-client 需要原 connection.json 的绝对路径')
        try:
            upgrade_client(args[1])
        except (StateLocationError, OSError, ValueError) as error:
            stop('UPGRADE_NOT_CONFIRMED：可信升级未确认', '保留原身份、原目录与 pending；不重新接入或重放业务')
    elif cmd == 'doctor':
        doctor(verbose=True)
        print('READY: 环境检查通过，可以运行 join', flush=True)
    elif cmd == 'join':
        join()
    elif cmd in ('-h', '--help', 'help'):
        print('usage: python .lebox/agent.py [doctor|join] [--branch <name>] [--repo owner/name] [--client-id <GitHub App client id>]   (' + VERSION + '; 无授权时走 GitHub Device Flow，由用户在 github.com/login/device 批准)')
    else:
        stop('未知命令 %r' % cmd, '只支持 doctor、join 和 upgrade-client')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        stop('已中断', '')
    except StateLocationError as ex:
        stop('原会话私有状态需要处理：' + str(ex), '保留原目录和 pending；不重新配对、不换分支')
    except RuntimeError as ex:
        stop('git 操作失败：' + str(ex)[:200], '把这行原样告诉用户')
