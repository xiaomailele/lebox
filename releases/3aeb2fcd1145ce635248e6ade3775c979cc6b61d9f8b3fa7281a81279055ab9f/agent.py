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
    path = _state_no_links(path)
    info = path.stat()
    if not path.is_file() or info.st_size > 1_200_000 or info.st_nlink != 1:
        raise StateLocationError('STATE_FILE_UNSAFE')
    if os.name != 'nt' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise StateLocationError('STATE_FILE_NOT_PRIVATE')
    return path.read_bytes()

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
    lock = _state_no_links(root / 'client.lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if info.st_nlink != 1 or (os.name != 'nt' and (info.st_uid != os.getuid() or info.st_mode & 0o077)):
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
    _state_no_links(root); root.mkdir(mode=0o700, exist_ok=True); os.chmod(root, 0o700)
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
        # GitHub's git endpoints accept the token as HTTP Basic (x-access-token:<token>), not as a Bearer header.
        import base64 as _b64
        basic = _b64.b64encode(('x-access-token:' + bearer).encode()).decode()
        cmd += ['-c', 'http.extraheader=Authorization: Basic ' + basic]
    cmd += ['ls-remote', '--heads', url]
    return subprocess.run(cmd, capture_output=True, text=True, env=env).returncode == 0


def check_token_scope(token, repo):
    """The GitHub App's user token covers every repository the App is installed on. Refuse to continue if that is more than
    the collaboration repository, so a leaked token could never reach anything else (the user narrows the installation once)."""
    import urllib.request as _u
    try:
        req = _u.Request('https://api.github.com/user/installations', headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'lebox/1.0'})
        with _u.urlopen(req, timeout=30) as r:
            insts = json.loads(r.read(200000)).get('installations', [])
        seen = []
        for inst in insts[:10]:
            req = _u.Request('https://api.github.com/user/installations/%s/repositories?per_page=100' % inst['id'], headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'lebox/1.0'})
            with _u.urlopen(req, timeout=30) as r:
                seen += [x['full_name'].lower() for x in json.loads(r.read(400000)).get('repositories', [])]
    except Exception:
        return  # Scope check is advisory; the App's own permissions still bound the token.
    others = sorted(set(x for x in seen if x != repo.lower()))
    if others:
        stop('该令牌不止能访问 %s，还能访问：%s' % (repo, ', '.join(others[:5]) + ('…' if len(others) > 5 else '')),
             '请用户在 GitHub → Settings → Applications → lebox 应用 → Configure，把 Repository access 改为只选 %s，然后重新运行' % repo)
    print('OK: 令牌范围自检通过（仅 %s）' % repo, flush=True)


def install_token_credential(token):
    # In-memory credential cache only (never written to disk); the URL in the repo config stays token-free.
    subprocess.run(['git', 'config', '--global', 'credential.helper', 'cache --timeout=86400'], capture_output=True)
    subprocess.run(['git', 'credential', 'approve'], input='protocol=https\nhost=github.com\nusername=x-access-token\npassword=%s\n\n' % token, text=True, capture_output=True)


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
        install_token_credential(PASTED_TOKEN)
    elif CLIENT_ID:
        PASTED_TOKEN = device_flow_token(CLIENT_ID, repo)
        if not git_can_read(url, PASTED_TOKEN):
            stop('授权已完成，但该令牌读不到仓库 %s' % repo, '请用户确认 lebox 应用已安装到这个仓库（GitHub → Settings → Applications → lebox → Configure）')
        AUTH_SOURCE = 'device-flow'
        check_token_scope(PASTED_TOKEN, repo)
        install_token_credential(PASTED_TOKEN)
    else:
        stop('沙盒没有该仓库的 GitHub 授权，说明里也没有应用 ID 或令牌', '请用户在本地协作端点「连接 Agent」复制最新说明')
    if pathlib.Path(name, '.git').exists():
        return pathlib.Path(name).resolve()
    r = subprocess.run(['git', 'clone', '-q', url, name], capture_output=True, text=True, env=dict(os.environ, GIT_TERMINAL_PROMPT='0'))
    if r.returncode:
        stop('git clone 失败：' + r.stderr.strip()[:200], '把这行原样告诉用户')
    print('OK: 已克隆 %s（授权来源：%s）' % (repo, '沙盒已有授权' if AUTH_SOURCE == 'sandbox' else '说明里的令牌'), flush=True)
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
            stop('沙盒里没有 git，说明里也没有应用 ID 或令牌',
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
    return subprocess.run([python, '-c', 'import cryptography'], capture_output=True).returncode == 0


def ensure_dependency(tools):
    global PYTHON
    if has_cryptography(PYTHON):
        return
    req = str(tools / 'requirements.txt')
    # 1) plain pip; 2) PEP 668 "externally managed" system Python (Debian/Ubuntu sandboxes) -> --break-system-packages;
    # 3) no write access to site-packages -> --user; 4) last resort: a private venv outside the repository.
    attempts = [
        [PYTHON, '-m', 'pip', 'install', '-q', '-r', req],
        [PYTHON, '-m', 'pip', 'install', '-q', '--break-system-packages', '-r', req],
        [PYTHON, '-m', 'pip', 'install', '-q', '--user', '-r', req],
    ]
    errors = []
    for cmd in attempts:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and has_cryptography(PYTHON):
            print('OK: 依赖已安装 (%s)' % ' '.join(cmd[3:-2]).strip() or 'pip', flush=True)
            return
        errors.append((r.stderr or r.stdout).strip().splitlines()[-1:] or ['exit %d' % r.returncode])
    venv = pathlib.Path(tempfile.gettempdir()) / 'lebox-venv'
    r = subprocess.run([sys.executable, '-m', 'venv', str(venv)], capture_output=True, text=True)
    if r.returncode == 0:
        vpy = venv / ('Scripts' if os.name == 'nt' else 'bin') / ('python.exe' if os.name == 'nt' else 'python')
        r = subprocess.run([str(vpy), '-m', 'pip', 'install', '-q', '-r', req], capture_output=True, text=True)
        if r.returncode == 0 and has_cryptography(str(vpy)):
            PYTHON = str(vpy)
            print('OK: 依赖已安装到私有 venv %s' % venv, flush=True)
            return
        errors.append((r.stderr or r.stdout).strip().splitlines()[-1:] or ['venv pip exit %d' % r.returncode])
    else:
        errors.append(['venv: ' + (r.stderr.strip().splitlines() or ['unavailable'])[-1]])
    detail = ' / '.join(e[0][:120] for e in errors if e)
    stop('无法安装依赖 cryptography（已尝试 pip、--break-system-packages、--user、venv）：' + detail,
         '请用户检查沙盒是否允许 pip 联网安装；或让 Agent 手动安装 cryptography 后重新运行 join')



def register_recovery(tools, repo_dir_args):
    """After pairing: seal this Agent's GitHub token with a fresh random key K, hand the opaque blob to the owner's desktop
    (which republishes it to the public tools repo as recovery/<rid>.bin), and print K once for the chat. Only when the token
    came from a device flow / pasted token (sandbox-provided auth needs no recovery)."""
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
        print('READY: 会话已建立（API 模式，GitHub 授权来源：%s；后续调用需保持环境变量 %s）。接下来：' % ({'sandbox': '沙盒环境变量', 'device-flow': '用户刚在 GitHub 设备授权页面批准', 'pasted-token': '说明里的令牌'}.get(AUTH_SOURCE, AUTH_SOURCE), TOKEN_ENV), flush=True)
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
    print('READY: 会话已建立（GitHub 授权来源：%s）。接下来（注意用这个解释器：%s）：' % ({'sandbox': '沙盒已有授权', 'device-flow': '用户刚在 GitHub 设备授权页面批准（令牌仅在内存）', 'pasted-token': '说明里的令牌，已放入 git 内存凭据缓存'}.get(AUTH_SOURCE, AUTH_SOURCE), PYTHON), flush=True)
    print_remote_handoff(tools)
    print('  若沙盒被回收或收到 session_closed_by_desktop：用对话里保留的 LEBOX_RECOVERY 重新运行 boot.py --recover <那一串>（免授权）；没有恢复串则重新运行 join。', flush=True)


# Filled only by the desktop's immutable-release builder. A raw source checkout fails closed.
EMBEDDED_CLIENT_B64 = 'IyEvdXNyL2Jpbi9lbnYgcHl0aG9uMwoiIiJQcm9qZWN0IGNvbGxhYm9yYXRpb24gY2xpZW50LiBVc2VzIGV4aXN0aW5nLCBleHBsaWNpdGx5IGF1dGhvcml6ZWQgR2l0IGFjY2VzcyBvbmx5LgpQcml2YXRlIHN0YXRlIHN0YXlzIG91dHNpZGUgdGhlIHJlcG9zaXRvcnkuIE5ldmVyIHN3aXRjaGVzIGJyYW5jaGVzLCBvdmVyd3JpdGVzIGEgZmlsZSwKZm9yY2VzIGEgcHVzaCwgY3JlYXRlcyBhIFBSLCBpbmhlcml0cyBhbm90aGVyIGNoYXQgaWRlbnRpdHkgb3IgYXBwcm92ZXMgcHJvamVjdCBhY2Nlc3MuCiIiIgppbXBvcnQgYXJncGFyc2UKaW1wb3J0IGJhc2U2NAppbXBvcnQgY29udGV4dGxpYgppbXBvcnQgaGFzaGxpYgppbXBvcnQgaG1hYwppbXBvcnQganNvbgppbXBvcnQgb3MKZnJvbSBwYXRobGliIGltcG9ydCBQYXRoCmltcG9ydCByZQppbXBvcnQgc2VjcmV0cwppbXBvcnQgc2h1dGlsCmltcG9ydCBzdWJwcm9jZXNzCmltcG9ydCBzeXMKaW1wb3J0IHRlbXBmaWxlCmltcG9ydCB0aW1lCmltcG9ydCB6bGliCmZyb20gdXJsbGliLnBhcnNlIGltcG9ydCB1cmxzcGxpdCwgcXVvdGUKaW1wb3J0IHVybGxpYi5yZXF1ZXN0CmltcG9ydCB1cmxsaWIuZXJyb3IKCiMgQkVHSU4gTEVCT1ggUEVSU0lTVEVOVCBTVEFURSBMQVlPVVQgVjEKIyBTZWxmLWNvbnRhaW5lZCwgaWRlbnRpY2FsIGluIGJvdGggdHJ1c3RlZCBlbnRyeSBwb2ludHM7IG5vIHVudmVyaWZpZWQgcnVudGltZSBpbXBvcnQuCmltcG9ydCBjb250ZXh0bGliIGFzIF9zdGF0ZV9jb250ZXh0bGliCmltcG9ydCBwYXRobGliIGFzIF9zdGF0ZV9wYXRobGliCgpjbGFzcyBTdGF0ZUxvY2F0aW9uRXJyb3IoVmFsdWVFcnJvcik6CiAgICBwYXNzCgpfU1RBVEVfRVhDTFVERUQgPSBmcm96ZW5zZXQoKCcuZ2l0JywgJy5naXQtY3JlZGVudGlhbHMnLCAnLm5ldHJjJywgJy5hcmVuYScsICcuY2FjaGUnLCAnLmxvY2FsJywgJy52ZW52JywgJy5ucG0nLCAnLm5leHQnLAogICAgJy5udXh0JywgJy5vdXRwdXQnLCAnLnBhcmNlbC1jYWNoZScsICcucHl0ZXN0X2NhY2hlJywgJy5ydWZmX2NhY2hlJywgJy5zdmVsdGUta2l0JywKICAgICcudG94JywgJy50dXJibycsICcudml0ZScsICcubXlweV9jYWNoZScsICcubm94JywgJ19fcHljYWNoZV9fJywgJ25vZGVfbW9kdWxlcycsCiAgICAnYnVpbGQnLCAnY292ZXJhZ2UnLCAnZGlzdCcsICdvdXQnLCAndGFyZ2V0JykpCgpkZWYgX3N0YXRlX25vX2xpbmtzKHBhdGgpOgogICAgcGF0aCA9IF9zdGF0ZV9wYXRobGliLlBhdGgob3MucGF0aC5hYnNwYXRoKHN0cihwYXRoKSkpCiAgICBmb3IgaXRlbSBpbiAocGF0aCwgKnBhdGgucGFyZW50cyk6CiAgICAgICAgaWYgaXRlbS5pc19zeW1saW5rKCk6CiAgICAgICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignU1RBVEVfUEFUSF9MSU5LOiBwcmVzZXJ2ZSBvcmlnaW5hbCBzdGF0ZTsgZG8gbm90IGZvbGxvdyBsaW5rcycpCiAgICByZXR1cm4gcGF0aAoKZGVmIHN0YXRlX3JlcG9fYm91bmRhcnkocGF0aCk6CiAgICBpZiBwYXRoIGlzIE5vbmU6CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIHBhdGggPSBfc3RhdGVfbm9fbGlua3MocGF0aCkKICAgIGZvciBpdGVtIGluIChwYXRoLCAqcGF0aC5wYXJlbnRzKToKICAgICAgICBpZiAoaXRlbSAvICcuZ2l0JykuZXhpc3RzKCk6CiAgICAgICAgICAgIHJldHVybiBpdGVtLnJlc29sdmUoKQogICAgcmV0dXJuIE5vbmUgICMgQVBJIHB1YmxpY2F0aW9uIGRvZXMgbm90IG1ha2UgYWxsIG9mIEhPTUUgYSBHaXQgd29ya3RyZWUuCgpkZWYgcGVyc2lzdGVudF9zdGF0ZV9ob21lKHJlcG89Tm9uZSk6CiAgICBob21lID0gX3N0YXRlX25vX2xpbmtzKF9zdGF0ZV9wYXRobGliLlBhdGguaG9tZSgpKS5yZXNvbHZlKCkKICAgIHZhbHVlID0gb3MuZW52aXJvbi5nZXQoJ0xFQk9YX1NUQVRFX0hPTUUnKSBvciBvcy5lbnZpcm9uLmdldCgnU0NYX1NUQVRFX0hPTUUnKQogICAgcm9vdCA9IF9zdGF0ZV9wYXRobGliLlBhdGgodmFsdWUpLmV4cGFuZHVzZXIoKSBpZiB2YWx1ZSBlbHNlIGhvbWUgLyAnLmxlYm94LXN0YXRlJwogICAgaWYgbm90IHJvb3QuaXNfYWJzb2x1dGUoKToKICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0hPTUVfQUJTT0xVVEVfUkVRVUlSRUQnKQogICAgcm9vdCA9IF9zdGF0ZV9ub19saW5rcyhyb290KQogICAgaWYgcm9vdCA9PSBob21lIG9yIG5vdCByb290LmlzX3JlbGF0aXZlX3RvKGhvbWUpIG9yIGFueShwIGluIF9TVEFURV9FWENMVURFRCBmb3IgcCBpbiByb290LnJlbGF0aXZlX3RvKGhvbWUpLnBhcnRzKToKICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0hPTUVfTk9UX1BFUlNJU1RFTlQ6IGNob29zZSBhIHByaXZhdGUsIG5vbi1leGNsdWRlZCBkaXJlY3RvcnkgaW5zaWRlIEhPTUUnKQogICAgYm91bmRhcnkgPSBzdGF0ZV9yZXBvX2JvdW5kYXJ5KHJlcG8pCiAgICBhY3R1YWxfYm91bmRhcnkgPSBzdGF0ZV9yZXBvX2JvdW5kYXJ5KHJvb3QpCiAgICBpZiAoYm91bmRhcnkgaXMgbm90IE5vbmUgYW5kIHJvb3QuaXNfcmVsYXRpdmVfdG8oYm91bmRhcnkpKSBvciBhY3R1YWxfYm91bmRhcnkgaXMgbm90IE5vbmU6CiAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdTVEFURV9IT01FX0lOX1JFUE9TSVRPUlk6IGRvIG5vdCBwdWJsaXNoIHByaXZhdGUgc3RhdGUnKQogICAgcm9vdC5ta2Rpcihtb2RlPTBvNzAwLCBwYXJlbnRzPVRydWUsIGV4aXN0X29rPVRydWUpCiAgICBfc3RhdGVfbm9fbGlua3Mocm9vdCkKICAgIGlmIG9zLm5hbWUgIT0gJ250JyBhbmQgcm9vdC5zdGF0KCkuc3RfdWlkICE9IG9zLmdldHVpZCgpOgogICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignU1RBVEVfSE9NRV9PV05FUl9NSVNNQVRDSCcpCiAgICBvcy5jaG1vZChyb290LCAwbzcwMCkKICAgIHJldHVybiByb290CgpkZWYgX3N0YXRlX2pzb24ocmF3KToKICAgIGlmIGxlbihyYXcpID4gMV8yMDBfMDAwOgogICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignU1RBVEVfU0laRV9MSU1JVCcpCiAgICBkZWYgdW5pcXVlKHBhaXJzKToKICAgICAgICB2YWx1ZSA9IHt9CiAgICAgICAgZm9yIGtleSwgaXRlbSBpbiBwYWlyczoKICAgICAgICAgICAgaWYga2V5IGluIHZhbHVlOgogICAgICAgICAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdTVEFURV9EVVBMSUNBVEVfRklFTEQnKQogICAgICAgICAgICB2YWx1ZVtrZXldID0gaXRlbQogICAgICAgIHJldHVybiB2YWx1ZQogICAgdHJ5OgogICAgICAgIGRlZiBpbnZhbGlkX2NvbnN0YW50KF8pOgogICAgICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0lOVkFMSURfTlVNQkVSJykKICAgICAgICB2YWx1ZSA9IGpzb24ubG9hZHMocmF3LCBvYmplY3RfcGFpcnNfaG9vaz11bmlxdWUsIHBhcnNlX2NvbnN0YW50PWludmFsaWRfY29uc3RhbnQpCiAgICBleGNlcHQgKFZhbHVlRXJyb3IsIFVuaWNvZGVFcnJvcikgYXMgZXJyb3I6CiAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdTVEFURV9JTlZBTElEX0pTT046IG9yaWdpbmFsIGZpbGUgcHJlc2VydmVkJykgZnJvbSBlcnJvcgogICAgaWYgbm90IGlzaW5zdGFuY2UodmFsdWUsIGRpY3QpOgogICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignU1RBVEVfT0JKRUNUX1JFUVVJUkVEJykKICAgIHJldHVybiB2YWx1ZQoKZGVmIF9zdGF0ZV9jYW5vbmljYWwodmFsdWUpOgogICAgcmV0dXJuIGpzb24uZHVtcHModmFsdWUsIGVuc3VyZV9hc2NpaT1GYWxzZSwgc29ydF9rZXlzPVRydWUsIHNlcGFyYXRvcnM9KCcsJywgJzonKSwgYWxsb3dfbmFuPUZhbHNlKS5lbmNvZGUoJ3V0Zi04JykKCmRlZiBfc3RhdGVfcmVhZChwYXRoKToKICAgIHBhdGggPSBfc3RhdGVfbm9fbGlua3MocGF0aCkKICAgIGluZm8gPSBwYXRoLnN0YXQoKQogICAgaWYgbm90IHBhdGguaXNfZmlsZSgpIG9yIGluZm8uc3Rfc2l6ZSA+IDFfMjAwXzAwMCBvciBpbmZvLnN0X25saW5rICE9IDE6CiAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdTVEFURV9GSUxFX1VOU0FGRScpCiAgICBpZiBvcy5uYW1lICE9ICdudCcgYW5kIChpbmZvLnN0X3VpZCAhPSBvcy5nZXR1aWQoKSBvciBpbmZvLnN0X21vZGUgJiAwbzA3Nyk6CiAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdTVEFURV9GSUxFX05PVF9QUklWQVRFJykKICAgIHJldHVybiBwYXRoLnJlYWRfYnl0ZXMoKQoKZGVmIF9zdGF0ZV93cml0ZShwYXRoLCByYXcpOgogICAgcGF0aCA9IF9zdGF0ZV9ub19saW5rcyhwYXRoKQogICAgdGVtcCA9IHBhdGgud2l0aF9uYW1lKCcuc3RhdGUtd3JpdGUtJyArIG9zLnVyYW5kb20oMTIpLmhleCgpKQogICAgZmQgPSBvcy5vcGVuKHRlbXAsIG9zLk9fV1JPTkxZIHwgb3MuT19DUkVBVCB8IG9zLk9fRVhDTCwgMG82MDApCiAgICB0cnk6CiAgICAgICAgd2l0aCBvcy5mZG9wZW4oZmQsICd3YicpIGFzIGZpbGU6CiAgICAgICAgICAgIGZpbGUud3JpdGUocmF3KTsgZmlsZS5mbHVzaCgpOyBvcy5mc3luYyhmaWxlLmZpbGVubygpKQogICAgICAgIG9zLnJlcGxhY2UodGVtcCwgcGF0aCkKICAgICAgICBpZiBvcy5uYW1lICE9ICdudCc6CiAgICAgICAgICAgIGRpcmVjdG9yeSA9IG9zLm9wZW4ocGF0aC5wYXJlbnQsIG9zLk9fUkRPTkxZIHwgZ2V0YXR0cihvcywgJ09fRElSRUNUT1JZJywgMCkpCiAgICAgICAgICAgIHRyeTogb3MuZnN5bmMoZGlyZWN0b3J5KQogICAgICAgICAgICBmaW5hbGx5OiBvcy5jbG9zZShkaXJlY3RvcnkpCiAgICBmaW5hbGx5OgogICAgICAgIHRlbXAudW5saW5rKG1pc3Npbmdfb2s9VHJ1ZSkKCkBfc3RhdGVfY29udGV4dGxpYi5jb250ZXh0bWFuYWdlcgpkZWYgX3N0YXRlX2xvY2socm9vdCk6CiAgICBsb2NrID0gX3N0YXRlX25vX2xpbmtzKHJvb3QgLyAnY2xpZW50LmxvY2snKQogICAgZmQgPSBvcy5vcGVuKGxvY2ssIG9zLk9fQ1JFQVQgfCBvcy5PX1JEV1IgfCBnZXRhdHRyKG9zLCAnT19OT0ZPTExPVycsIDApLCAwbzYwMCkKICAgIHRyeToKICAgICAgICBpbmZvID0gb3MuZnN0YXQoZmQpCiAgICAgICAgaWYgaW5mby5zdF9ubGluayAhPSAxIG9yIChvcy5uYW1lICE9ICdudCcgYW5kIChpbmZvLnN0X3VpZCAhPSBvcy5nZXR1aWQoKSBvciBpbmZvLnN0X21vZGUgJiAwbzA3NykpOgogICAgICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0xPQ0tfTk9UX1BSSVZBVEUnKQogICAgICAgIGlmIG9zLm5hbWUgPT0gJ250JzoKICAgICAgICAgICAgaW1wb3J0IG1zdmNydAogICAgICAgICAgICBpZiBvcy5mc3RhdChmZCkuc3Rfc2l6ZSA9PSAwOiBvcy53cml0ZShmZCwgYicwJykKICAgICAgICAgICAgb3MubHNlZWsoZmQsIDAsIDApOyBtc3ZjcnQubG9ja2luZyhmZCwgbXN2Y3J0LkxLX05CTENLLCAxKQogICAgICAgIGVsc2U6CiAgICAgICAgICAgIGltcG9ydCBmY250bAogICAgICAgICAgICBmY250bC5mbG9jayhmZCwgZmNudGwuTE9DS19FWCB8IGZjbnRsLkxPQ0tfTkIpCiAgICAgICAgeWllbGQKICAgIGV4Y2VwdCAoQmxvY2tpbmdJT0Vycm9yLCBQZXJtaXNzaW9uRXJyb3IpIGFzIGVycm9yOgogICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignU1RBVEVfQlVTWTogb3JpZ2luYWwgb3BlcmF0aW9uIG1heSBzdGlsbCBiZSBydW5uaW5nJykgZnJvbSBlcnJvcgogICAgZmluYWxseToKICAgICAgICBvcy5jbG9zZShmZCkKCmRlZiBwZXJzaXN0ZW50X3Nlc3Npb25fZGlyZWN0b3J5KGNvbmZpZywgcmVwbz1Ob25lKToKICAgIGJpbmRpbmcgPSBjb25maWcuZ2V0KCdiaW5kaW5nJywgJycpCiAgICBpZiBub3QgaXNpbnN0YW5jZShiaW5kaW5nLCBzdHIpIG9yIG5vdCByZS5mdWxsbWF0Y2gocidbMC05YS1mXXszMn0nLCBiaW5kaW5nKToKICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0JJTkRJTkdfSU5WQUxJRCcpCiAgICBlbmNvZGVkID0gX3N0YXRlX2Nhbm9uaWNhbChjb25maWcpOyBkaWdlc3QgPSBoYXNobGliLnNoYTI1NihlbmNvZGVkKS5oZXhkaWdlc3QoKQogICAgcm9vdCA9IHBlcnNpc3RlbnRfc3RhdGVfaG9tZShyZXBvKSAvICgnc2N4LWNvbGxhYm9yYXRpb24tJyArIGJpbmRpbmcpCiAgICBfc3RhdGVfbm9fbGlua3Mocm9vdCk7IHJvb3QubWtkaXIobW9kZT0wbzcwMCwgZXhpc3Rfb2s9VHJ1ZSk7IG9zLmNobW9kKHJvb3QsIDBvNzAwKQogICAgb2xkID0gX3N0YXRlX25vX2xpbmtzKF9zdGF0ZV9wYXRobGliLlBhdGgodGVtcGZpbGUuZ2V0dGVtcGRpcigpKSAvIHJvb3QubmFtZSkKICAgIGJvdW5kYXJ5ID0gc3RhdGVfcmVwb19ib3VuZGFyeShyZXBvKQogICAgaWYgYm91bmRhcnkgaXMgbm90IE5vbmUgYW5kIG9sZC5yZXNvbHZlKCkuaXNfcmVsYXRpdmVfdG8oYm91bmRhcnkpOgogICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignTEVHQUNZX1NUQVRFX0lOX1JFUE9TSVRPUlknKQogICAgdGFyZ2V0ID0gcm9vdCAvICdzdGF0ZS5qc29uJzsgZGVzY3JpcHRvciA9IHJvb3QgLyAnY29ubmVjdGlvbi5qc29uJwogICAgZGVmIHZhbGlkYXRlKHJhdyk6CiAgICAgICAgaWYgX3N0YXRlX2pzb24ocmF3KS5nZXQoJ2NvbmZpZ19oYXNoJykgIT0gZGlnZXN0OgogICAgICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0NPTkZJR19NSVNNQVRDSDogbmV2ZXIgcmVwbGFjZSBhbiBleGlzdGluZyBpZGVudGl0eScpCiAgICB3aXRoIF9zdGF0ZV9sb2NrKHJvb3QpOgogICAgICAgIGlmIGRlc2NyaXB0b3IuZXhpc3RzKCkgYW5kIF9zdGF0ZV9jYW5vbmljYWwoX3N0YXRlX2pzb24oX3N0YXRlX3JlYWQoZGVzY3JpcHRvcikpKSAhPSBlbmNvZGVkOgogICAgICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0RFU0NSSVBUT1JfTUlTTUFUQ0gnKQogICAgICAgIGlmIHRhcmdldC5leGlzdHMoKTogdmFsaWRhdGUoX3N0YXRlX3JlYWQodGFyZ2V0KSkKICAgICAgICBpZiBvbGQucmVzb2x2ZSgpICE9IHJvb3QucmVzb2x2ZSgpIGFuZCAob2xkIC8gJ3N0YXRlLmpzb24nKS5leGlzdHMoKToKICAgICAgICAgICAgd2l0aCBfc3RhdGVfbG9jayhvbGQpOgogICAgICAgICAgICAgICAgb3JpZ2luYWwgPSBfc3RhdGVfcmVhZChvbGQgLyAnc3RhdGUuanNvbicpOyB2YWxpZGF0ZShvcmlnaW5hbCkKICAgICAgICAgICAgICAgIHNvdXJjZV9oYXNoID0gaGFzaGxpYi5zaGEyNTYob3JpZ2luYWwpLmhleGRpZ2VzdCgpCiAgICAgICAgICAgICAgICByZWNlaXB0ID0gcm9vdCAvICdtaWdyYXRpb24uanNvbicKICAgICAgICAgICAgICAgIHJlY29yZCA9IHsnZm9ybWF0JzogJ2xlYm94LXN0YXRlLW1pZ3JhdGlvbi12MScsICdzb3VyY2UnOiBzdHIob2xkLnJlc29sdmUoKSksCiAgICAgICAgICAgICAgICAgICAgICAgICAgJ3NvdXJjZV9zaGEyNTYnOiBzb3VyY2VfaGFzaCwgJ2NvbmZpZ19oYXNoJzogZGlnZXN0fQogICAgICAgICAgICAgICAgaWYgcmVjZWlwdC5leGlzdHMoKSBhbmQgbm90IHRhcmdldC5leGlzdHMoKToKICAgICAgICAgICAgICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX01JR1JBVElPTl9UQVJHRVRfTUlTU0lORzogcHJlc2VydmUgb3JpZ2luYWwgZXZpZGVuY2UnKQogICAgICAgICAgICAgICAgaWYgdGFyZ2V0LmV4aXN0cygpIGFuZCBfc3RhdGVfcmVhZCh0YXJnZXQpICE9IG9yaWdpbmFsOgogICAgICAgICAgICAgICAgICAgIGlmIG5vdCByZWNlaXB0LmV4aXN0cygpIG9yIF9zdGF0ZV9qc29uKF9zdGF0ZV9yZWFkKHJlY2VpcHQpKSAhPSByZWNvcmQ6CiAgICAgICAgICAgICAgICAgICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignU1RBVEVfTUlHUkFUSU9OX0NPTkZMSUNUOiBrZWVwIGJvdGggc3RhdGVzIGFuZCByZWNvbmNpbGUnKQogICAgICAgICAgICAgICAgZWxpZiBub3QgcmVjZWlwdC5leGlzdHMoKToKICAgICAgICAgICAgICAgICAgICBpZiBub3QgdGFyZ2V0LmV4aXN0cygpOiBfc3RhdGVfd3JpdGUodGFyZ2V0LCBvcmlnaW5hbCkKICAgICAgICAgICAgICAgICAgICBfc3RhdGVfd3JpdGUocmVjZWlwdCwgX3N0YXRlX2Nhbm9uaWNhbChyZWNvcmQpKQogICAgICAgICAgICAgICAgZWxpZiBfc3RhdGVfanNvbihfc3RhdGVfcmVhZChyZWNlaXB0KSkgIT0gcmVjb3JkOgogICAgICAgICAgICAgICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignU1RBVEVfTUlHUkFUSU9OX0NPTkZMSUNUOiBsZWdhY3kgc3RhdGUgY2hhbmdlZCcpCiAgICAgICAgaWYgbm90IGRlc2NyaXB0b3IuZXhpc3RzKCk6IF9zdGF0ZV93cml0ZShkZXNjcmlwdG9yLCBlbmNvZGVkKQogICAgcmV0dXJuIHJvb3QKCiMgRU5EIExFQk9YIFBFUlNJU1RFTlQgU1RBVEUgTEFZT1VUIFYxCgpWRVJTSU9OID0gJ3NjeC1naC1tMi12MScKTElNSVQgPSAxMDBfMDAwCiMgQWRhcHRpdmUgcG9sbGluZzogZmFzdCByaWdodCBhZnRlciB3ZSBwdWJsaXNoIHNvbWV0aGluZywgYmFja2luZyBvZmYgdG8gYSAxMCBzIGNlaWxpbmcgKEdpdCBmZXRjaGVzIGFyZSBjaGVhcCBhbmQKIyB0aGUgZGVza3RvcCBhbnN3ZXJzIHdpdGhpbiBzZWNvbmRzOyBhIGZpeGVkIDE1LTIwIHMgaW50ZXJ2YWwgd2FzIHRoZSBkb21pbmFudCBjb3N0IHBlciB0b29sIGNhbGwpLgpQT0xMX1NURVBTID0gKDIsIDIsIDMsIDQsIDYsIDgsIDEwKQoKZGVmIHBvbGxfZGVsYXkoYXR0ZW1wdCk6CiAgICByZXR1cm4gUE9MTF9TVEVQU1ttaW4oYXR0ZW1wdCwgbGVuKFBPTExfU1RFUFMpIC0gMSldCgpjbGFzcyBTdG9wKEV4Y2VwdGlvbik6CiAgICBwYXNzCgpkZWYgcmVxdWlyZShvaywgbWVzc2FnZSk6CiAgICBpZiBub3Qgb2s6CiAgICAgICAgcmFpc2UgU3RvcChtZXNzYWdlKQoKZGVmIGNhbm9uaWNhbCh2YWx1ZSk6CiAgICByZXR1cm4ganNvbi5kdW1wcyh2YWx1ZSwgZW5zdXJlX2FzY2lpPUZhbHNlLCBzb3J0X2tleXM9VHJ1ZSwgc2VwYXJhdG9ycz0oJywnLCAnOicpLCBhbGxvd19uYW49RmFsc2UpLmVuY29kZSgndXRmLTgnKQoKZGVmIGxvYWRfanNvbihkYXRhLCBsaW1pdD1MSU1JVCk6CiAgICByZXF1aXJlKGxlbihkYXRhKSA8PSBsaW1pdCwgJ0RhdGEgZXhjZWVkcyB0aGUgc2Vzc2lvbiBsaW1pdC4nKQogICAgZGVmIHVuaXF1ZShwYWlycyk6CiAgICAgICAgcmVzdWx0ID0ge30KICAgICAgICBmb3Iga2V5LCB2YWx1ZSBpbiBwYWlyczoKICAgICAgICAgICAgcmVxdWlyZShrZXkgbm90IGluIHJlc3VsdCwgJ0R1cGxpY2F0ZSBKU09OIGZpZWxkLicpCiAgICAgICAgICAgIHJlc3VsdFtrZXldID0gdmFsdWUKICAgICAgICByZXR1cm4gcmVzdWx0CiAgICByZXR1cm4ganNvbi5sb2FkcyhkYXRhLCBvYmplY3RfcGFpcnNfaG9vaz11bmlxdWUpCgpkZWYgZXhhY3QodmFsdWUsIGZpZWxkcyk6CiAgICByZXF1aXJlKGlzaW5zdGFuY2UodmFsdWUsIGRpY3QpIGFuZCBzZXQodmFsdWUpID09IHNldChmaWVsZHMpLCAnVW5leHBlY3RlZCBtZXNzYWdlIGZpZWxkcy4nKQoKZGVmIGI2NCh2YWx1ZSk6CiAgICByZXR1cm4gYmFzZTY0LmI2NGVuY29kZSh2YWx1ZSkuZGVjb2RlKCdhc2NpaScpCgpkZWYgdW5iNjQodmFsdWUsIGxlbmd0aD1Ob25lKToKICAgIHJlcXVpcmUoaXNpbnN0YW5jZSh2YWx1ZSwgc3RyKSBhbmQgbGVuKHZhbHVlKSA8PSA5MF8wMDAsICdJbnZhbGlkIGVuY29kZWQgdmFsdWUuJykKICAgIHJhdyA9IGJhc2U2NC5iNjRkZWNvZGUodmFsdWUsIHZhbGlkYXRlPVRydWUpCiAgICByZXF1aXJlKGxlbmd0aCBpcyBOb25lIG9yIGxlbihyYXcpID09IGxlbmd0aCwgJ0ludmFsaWQgZW5jb2RlZCBsZW5ndGguJykKICAgIHJldHVybiByYXcKCmRlZiBjcnlwdG8oKToKICAgIHRyeToKICAgICAgICBmcm9tIGNyeXB0b2dyYXBoeS5oYXptYXQucHJpbWl0aXZlcyBpbXBvcnQgaGFzaGVzLCBzZXJpYWxpemF0aW9uCiAgICAgICAgZnJvbSBjcnlwdG9ncmFwaHkuaGF6bWF0LnByaW1pdGl2ZXMuYXN5bW1ldHJpYyBpbXBvcnQgZWMKICAgICAgICBmcm9tIGNyeXB0b2dyYXBoeS5oYXptYXQucHJpbWl0aXZlcy5rZGYuaGtkZiBpbXBvcnQgSEtERgogICAgICAgIGZyb20gY3J5cHRvZ3JhcGh5Lmhhem1hdC5wcmltaXRpdmVzLmNpcGhlcnMuYWVhZCBpbXBvcnQgQUVTR0NNCiAgICAgICAgcmV0dXJuIGhhc2hlcywgc2VyaWFsaXphdGlvbiwgZWMsIEhLREYsIEFFU0dDTQogICAgZXhjZXB0IEltcG9ydEVycm9yOgogICAgICAgIHJhaXNlIFN0b3AoJ1JlcXVpcmVzIHRoZSBQeXRob24gY3J5cHRvZ3JhcGh5IHBhY2thZ2UuIEluc3RhbGwgaXQgb25seSBpZiB0aGUgZW52aXJvbm1lbnQgcGVybWl0cyBkZXBlbmRlbmNpZXM7IGRvIG5vdCBieXBhc3MgcGxhdGZvcm0gcmVzdHJpY3Rpb25zLicpCgpkZWYgdmFsaWRhdGVfY29uZmlnKGMpOgogICAgZXhhY3QoYywgWyd2ZXJzaW9uJywgJ3JlcG9zaXRvcnknLCAncmVwb19pZCcsICdicmFuY2gnLCAnYmluZGluZycsICdlcG9jaCcsICdwcm9qZWN0X2lkJywgJ21heF9vcGVyYXRpb25zJywgJ2V4cGlyZXNfYXQnXSkKICAgIHJlcXVpcmUoY1sndmVyc2lvbiddID09IFZFUlNJT04sICdVbnN1cHBvcnRlZCBjb2xsYWJvcmF0aW9uIHZlcnNpb247IHJlcXVlc3QgYSBmcmVzaCB0b29sIHBhY2thZ2UuJykKICAgIHJlcXVpcmUoaXNpbnN0YW5jZShjWydyZXBvc2l0b3J5J10sIHN0cikgYW5kIHJlLmZ1bGxtYXRjaChyJ1tBLVphLXowLTldW0EtWmEtejAtOS1dKi9bQS1aYS16MC05Ll8tXSsnLCBjWydyZXBvc2l0b3J5J10pLCAnSW52YWxpZCByZXBvc2l0b3J5LicpCiAgICByZXF1aXJlKGNbJ3JlcG9zaXRvcnknXS5zcGxpdCgnLycpWzFdIG5vdCBpbiAoJy4nLCAnLi4nKSwgJ0ludmFsaWQgcmVwb3NpdG9yeS4nKQogICAgcmVxdWlyZShpc2luc3RhbmNlKGNbJ3JlcG9faWQnXSwgc3RyKSBhbmQgcmUuZnVsbG1hdGNoKHInWzEtOV1bMC05XXswLDE5fScsIGNbJ3JlcG9faWQnXSksICdJbnZhbGlkIHJlcG9zaXRvcnkgSUQuJykKICAgIGJyYW5jaCA9IGNbJ2JyYW5jaCddCiAgICByZXF1aXJlKGlzaW5zdGFuY2UoYnJhbmNoLCBzdHIpIGFuZCAxIDw9IGxlbihicmFuY2gpIDw9IDIwMCBhbmQgcmUuZnVsbG1hdGNoKHInW0EtWmEtejAtOS5fLy1dKycsIGJyYW5jaCkKICAgICAgICAgICAgYW5kICcuLicgbm90IGluIGJyYW5jaCBhbmQgbm90IGJyYW5jaC5lbmRzd2l0aCgnLicpIGFuZCBicmFuY2gubG93ZXIoKSBub3QgaW4gKCdtYWluJywgJ21hc3RlcicpCiAgICAgICAgICAgIGFuZCBhbGwocCBhbmQgbm90IHAuc3RhcnRzd2l0aCgnLicpIGFuZCBub3QgcC5sb3dlcigpLmVuZHN3aXRoKCcubG9jaycpIGZvciBwIGluIGJyYW5jaC5zcGxpdCgnLycpKSwgJ0ludmFsaWQgY29sbGFib3JhdGlvbiBicmFuY2guJykKICAgIGZvciBrZXkgaW4gKCdiaW5kaW5nJywgJ2Vwb2NoJyk6CiAgICAgICAgcmVxdWlyZShpc2luc3RhbmNlKGNba2V5XSwgc3RyKSBhbmQgcmUuZnVsbG1hdGNoKHInWzAtOWEtZl17MzJ9JywgY1trZXldKSwgJ0ludmFsaWQgc2Vzc2lvbiBpZGVudGl0eS4nKQogICAgcmVxdWlyZShpc2luc3RhbmNlKGNbJ3Byb2plY3RfaWQnXSwgc3RyKSBhbmQgcmUuZnVsbG1hdGNoKHInW0EtWmEtejAtOV8tXXsxLDEyMH0nLCBjWydwcm9qZWN0X2lkJ10pLCAnSW52YWxpZCBwcm9qZWN0IGlkZW50aXR5LicpCiAgICAjIDAgbWVhbnMgdW5saW1pdGVkOiB0aGUgc2Vzc2lvbiBpcyBsb25nLWxpdmVkIGFuZCBlbmRzIG9ubHkgd2hlbiB0aGUgdXNlciBzdG9wcyBpdCBvbiB0aGVpciBtYWNoaW5lLgogICAgcmVxdWlyZSh0eXBlKGNbJ21heF9vcGVyYXRpb25zJ10pIGlzIGludCBhbmQgY1snbWF4X29wZXJhdGlvbnMnXSA+PSAwIGFuZCB0eXBlKGNbJ2V4cGlyZXNfYXQnXSkgaXMgaW50IGFuZCBjWydleHBpcmVzX2F0J10gPj0gMCwKICAgICAgICAgICAgJ0ludmFsaWQgc2Vzc2lvbiBsaW1pdHMuJykKICAgIHJlcXVpcmUoY1snZXhwaXJlc19hdCddID09IDAgb3IgY1snZXhwaXJlc19hdCddIC0gdGltZS50aW1lKCkgPiAwLAogICAgICAgICAgICAnU2Vzc2lvbiBleHBpcmVkIG9yIGludmFsaWQuIFJlcXVlc3QgYSBuZXcgcGFja2FnZTsgZG8gbm90IHJldXNlIG9sZCByZXF1ZXN0cy4nKQoKZGVmIHBpbnMoYyk6CiAgICByZXR1cm4ge2tleTogY1trZXldIGZvciBrZXkgaW4gKCd2ZXJzaW9uJywgJ3JlcG9faWQnLCAnYnJhbmNoJywgJ2JpbmRpbmcnLCAnZXBvY2gnKX0KCmRlZiBtZXNzYWdlX3BhdGgoYywga2luZCwgb3ApOgogICAgcmVxdWlyZShraW5kIGluICgnaGVsbG8nLCAnY29uZmlybWF0aW9uJywgJ3JlcXVlc3QnLCAncmVzcG9uc2UnLCAnY2xvc2VkJywgJ3JlbmV3JywgJ3Jlc3VsdHF1ZXJ5JywgJ3Jlc3VsdHJlcGx5JykgYW5kIHJlLmZ1bGxtYXRjaChyJ1thLXowLTldW2EtejAtOS1dezAsNzl9Jywgb3ApLCAnSW52YWxpZCBtZXNzYWdlIHBhdGguJykKICAgIHJldHVybiBmIi5sZWJveC9zZXNzaW9uLXtjWydiaW5kaW5nJ119L3tvcH0ue2tpbmR9Lmpzb24iCgpjbGFzcyBHaXRNYWlsYm94OgogICAgZGVmIF9faW5pdF9fKHNlbGYsIHJlcG8sIGNvbmZpZywgcHJpdmF0ZSk6CiAgICAgICAgc2VsZi5yZXBvLCBzZWxmLmMsIHNlbGYucHJpdmF0ZSA9IHJlcG8ucmVzb2x2ZSgpLCBjb25maWcsIHByaXZhdGUKICAgICAgICBzZWxmLnJlZiA9ICdyZWZzL3NjeC1jb2xsYWJvcmF0aW9uLycgKyBjb25maWdbJ2JpbmRpbmcnXQogICAgICAgIGhvb2tzID0gcHJpdmF0ZSAvICdlbXB0eS1ob29rcycKICAgICAgICBob29rcy5ta2RpcihleGlzdF9vaz1UcnVlLCBtb2RlPTBvNzAwKQogICAgICAgIHJlcXVpcmUobm90IGhvb2tzLmlzX3N5bWxpbmsoKSBhbmQgbm90IGFueShob29rcy5pdGVyZGlyKCkpLCAnUHJpdmF0ZSBob29rcyBkaXJlY3RvcnkgaXMgbm90IGVtcHR5LicpCiAgICAgICAgc2VsZi5iYXNlID0gWydnaXQnLCAnLWMnLCAnY29yZS5ob29rc1BhdGg9JyArIHN0cihob29rcyksICctYycsICdjb3JlLmZzbW9uaXRvcj1mYWxzZScsCiAgICAgICAgICAgICAgICAgICAgICctYycsICdjb21taXQuZ3BnU2lnbj1mYWxzZScsICctYycsICdwdXNoLmdwZ1NpZ249ZmFsc2UnLCAnLWMnLCAnaHR0cC5mb2xsb3dSZWRpcmVjdHM9ZmFsc2UnLAogICAgICAgICAgICAgICAgICAgICAnLWMnLCAncHJvdG9jb2wuZXh0LmFsbG93PW5ldmVyJywgJy1jJywgJ3Byb3RvY29sLmZpbGUuYWxsb3c9bmV2ZXInLCAnLUMnLCBzdHIoc2VsZi5yZXBvKV0KICAgICAgICAjIFZhbGlkYXRlIHJlc29sdmVkIGZldGNoIEFORCBwdXNoIFVSTHM7IG5ldmVyIGxldCBhIGhpZGRlbiBwdXNodXJsIHNlbGVjdCBhbm90aGVyIHJlcG9zaXRvcnkuCiAgICAgICAgZm9yIGFyZ3MgaW4gWygncmVtb3RlJywgJ2dldC11cmwnLCAnLS1hbGwnLCAnb3JpZ2luJyksICgncmVtb3RlJywgJ2dldC11cmwnLCAnLS1wdXNoJywgJy0tYWxsJywgJ29yaWdpbicpXToKICAgICAgICAgICAgdXJscyA9IHNlbGYucnVuKCphcmdzKS5kZWNvZGUoKS5zcGxpdGxpbmVzKCkKICAgICAgICAgICAgcmVxdWlyZShsZW4odXJscykgPT0gMSwgJ0V4YWN0bHkgb25lIGF1dGhvcml6ZWQgb3JpZ2luIFVSTCBpcyByZXF1aXJlZC4nKQogICAgICAgICAgICBvcmlnaW4gPSB1cmxzWzBdLnN0cmlwKCkKICAgICAgICAgICAgaWYgb3JpZ2luLnN0YXJ0c3dpdGgoJ2dpdEBnaXRodWIuY29tOicpOgogICAgICAgICAgICAgICAgdGFyZ2V0ID0gb3JpZ2luW2xlbignZ2l0QGdpdGh1Yi5jb206Jyk6XQogICAgICAgICAgICBlbHNlOgogICAgICAgICAgICAgICAgcGFyc2VkID0gdXJsc3BsaXQob3JpZ2luKQogICAgICAgICAgICAgICAgcmVxdWlyZShwYXJzZWQuc2NoZW1lID09ICdodHRwcycgYW5kIHBhcnNlZC5ob3N0bmFtZSA9PSAnZ2l0aHViLmNvbScgYW5kIHBhcnNlZC5wb3J0IGluIChOb25lLCA0NDMpCiAgICAgICAgICAgICAgICAgICAgICAgIGFuZCBub3QgcGFyc2VkLnF1ZXJ5IGFuZCBub3QgcGFyc2VkLmZyYWdtZW50LCAnT3JpZ2luIG11c3QgYmUgdGhlIGF1dGhvcml6ZWQgZ2l0aHViLmNvbSByZXBvc2l0b3J5LicpCiAgICAgICAgICAgICAgICB0YXJnZXQgPSBwYXJzZWQucGF0aC5sc3RyaXAoJy8nKQogICAgICAgICAgICByZXF1aXJlKHRhcmdldC5yZW1vdmVzdWZmaXgoJy5naXQnKS5sb3dlcigpID09IGNvbmZpZ1sncmVwb3NpdG9yeSddLmxvd2VyKCksICdSZXBvc2l0b3J5IGRvZXMgbm90IG1hdGNoIHRoaXMgc2Vzc2lvbi4nKQogICAgICAgIGJyYW5jaCA9IHNlbGYucnVuKCdzeW1ib2xpYy1yZWYnLCAnLS1zaG9ydCcsICdIRUFEJykuZGVjb2RlKCkuc3RyaXAoKQogICAgICAgIHJlcXVpcmUoYnJhbmNoID09IGNvbmZpZ1snYnJhbmNoJ10sCiAgICAgICAgICAgICAgICBmIlRoaXMgcGFja2FnZSBpcyBib3VuZCB0byBicmFuY2gge2NvbmZpZ1snYnJhbmNoJ119IGJ1dCB0aGUgY2hlY2tlZC1vdXQgYnJhbmNoIGlzIHticmFuY2h9LiAiCiAgICAgICAgICAgICAgICAnSXQgd2FzIGlzc3VlZCBmb3IgYSBkaWZmZXJlbnQgY29udmVyc2F0aW9uLiBEbyBOT1Qgc3dpdGNoIGJyYW5jaGVzIG9yIHJldXNlIGl0OiB0ZWxsIHRoZSB1c2VyIHRvIGNsaWNrICcKICAgICAgICAgICAgICAgICci6L+e5o6l5pawIEFnZW50IiBpbiB0aGUgU2h1bkNvZGV4IEdpdEh1YiDljY/kvZwgbWVudSBmb3IgVEhJUyBjb252ZXJzYXRpb24gYW5kIHBhc3RlIHRoZSBuZXcgaW5zdHJ1Y3Rpb25zIGhlcmUuJykKCiAgICBkZWYgcnVuKHNlbGYsICphcmdzLCBkYXRhPU5vbmUsIGVudj1Ob25lKToKICAgICAgICBzYWZlX2VudiA9IGRpY3Qob3MuZW52aXJvbiBpZiBlbnYgaXMgTm9uZSBlbHNlIGVudikKICAgICAgICBzYWZlX2VudlsnR0lUX1RFUk1JTkFMX1BST01QVCddID0gJzAnCiAgICAgICAgcmVzdWx0ID0gc3VicHJvY2Vzcy5ydW4oc2VsZi5iYXNlICsgbGlzdChhcmdzKSwgaW5wdXQ9ZGF0YSwgc3Rkb3V0PXN1YnByb2Nlc3MuUElQRSwgc3RkZXJyPXN1YnByb2Nlc3MuUElQRSwKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBlbnY9c2FmZV9lbnYsIHRpbWVvdXQ9NDUsIGNoZWNrPUZhbHNlKQogICAgICAgIHJlcXVpcmUocmVzdWx0LnJldHVybmNvZGUgPT0gMCwgJ0dpdCBvcGVyYXRpb24gd2FzIG5vdCBjb25maXJtZWQuIENoZWNrIHJlcG9zaXRvcnkgYXV0aG9yaXphdGlvbiBhbmQgb3JpZ2luYWwgcmVxdWVzdCBzdGF0dXM7IG5vIGZvcmNlIHB1c2ggb3IgYXV0b21hdGljIHJldHJ5IHdhcyBwZXJmb3JtZWQuJykKICAgICAgICByZXR1cm4gcmVzdWx0LnN0ZG91dAoKICAgIGRlZiBmZXRjaChzZWxmKToKICAgICAgICBzZWxmLnJ1bignZmV0Y2gnLCAnLS1xdWlldCcsICctLW5vLXRhZ3MnLCAnLS1uby13cml0ZS1mZXRjaC1oZWFkJywgJ29yaWdpbicsIGYicmVmcy9oZWFkcy97c2VsZi5jWydicmFuY2gnXX06e3NlbGYucmVmfSIpCiAgICAgICAgdGlwID0gc2VsZi5ydW4oJ3Jldi1wYXJzZScsICctLXZlcmlmeScsIHNlbGYucmVmKS5kZWNvZGUoKS5zdHJpcCgpCiAgICAgICAgcmVxdWlyZShyZS5mdWxsbWF0Y2goJ1swLTlhLWZdezQwfScsIHRpcCksICdVbnN1cHBvcnRlZCBHaXQgb2JqZWN0IGlkZW50aXR5LicpCiAgICAgICAgcmV0dXJuIHRpcAoKICAgIGRlZiBhdChzZWxmLCB0aXAsIHBhdGgpOgogICAgICAgIGxpc3RpbmcgPSBzZWxmLnJ1bignbHMtdHJlZScsICcteicsIHRpcCwgJy0tJywgcGF0aCkKICAgICAgICBpZiBub3QgbGlzdGluZzoKICAgICAgICAgICAgcmV0dXJuIE5vbmUKICAgICAgICBwYXJ0cyA9IGxpc3Rpbmcuc3BsaXQoYidcMCcpCiAgICAgICAgcmVxdWlyZShsZW4ocGFydHMpID09IDIgYW5kIHBhcnRzWzFdID09IGInJywgJ1VuZXhwZWN0ZWQgR2l0IHRyZWUgZW50cnkuJykKICAgICAgICBtZXRhZGF0YSwgYWN0dWFsID0gcGFydHNbMF0uc3BsaXQoYidcdCcsIDEpCiAgICAgICAgbW9kZSwga2luZCwgb2lkID0gbWV0YWRhdGEuc3BsaXQoYicgJykKICAgICAgICByZXF1aXJlKG1vZGUgPT0gYicxMDA2NDQnIGFuZCBraW5kID09IGInYmxvYicgYW5kIGFjdHVhbC5kZWNvZGUoKSA9PSBwYXRoLCAnTWVzc2FnZSBpcyBub3QgYW4gb3JkaW5hcnkgZmlsZS4nKQogICAgICAgIHNpemUgPSBpbnQoc2VsZi5ydW4oJ2NhdC1maWxlJywgJy1zJywgb2lkLmRlY29kZSgpKSkKICAgICAgICByZXF1aXJlKDAgPCBzaXplIDw9IExJTUlULCAnTWVzc2FnZSBleGNlZWRzIHRoZSBzaXplIGxpbWl0LicpCiAgICAgICAgY29udGVudCA9IHNlbGYucnVuKCdjYXQtZmlsZScsICdibG9iJywgb2lkLmRlY29kZSgpKQogICAgICAgIHJlcXVpcmUobGVuKGNvbnRlbnQpID09IHNpemUsICdHaXQgb2JqZWN0IHNpemUgbWlzbWF0Y2guJykKICAgICAgICByZXR1cm4gY29udGVudAoKICAgIGRlZiByZWFkKHNlbGYsIGtpbmQsIG9wKToKICAgICAgICByZXR1cm4gc2VsZi5hdChzZWxmLmZldGNoKCksIG1lc3NhZ2VfcGF0aChzZWxmLmMsIGtpbmQsIG9wKSkKCiAgICBkZWYgY3JlYXRlKHNlbGYsIGtpbmQsIG9wLCBkYXRhKToKICAgICAgICByZXF1aXJlKDAgPCBsZW4oZGF0YSkgPD0gTElNSVQsICdNZXNzYWdlIGV4Y2VlZHMgdGhlIHNpemUgbGltaXQuJykKICAgICAgICBwYXRoID0gbWVzc2FnZV9wYXRoKHNlbGYuYywga2luZCwgb3ApCiAgICAgICAgdGlwID0gc2VsZi5mZXRjaCgpCiAgICAgICAgcHJldmlvdXMgPSBzZWxmLmF0KHRpcCwgcGF0aCkKICAgICAgICBpZiBwcmV2aW91cyBpcyBub3QgTm9uZToKICAgICAgICAgICAgcmVxdWlyZShwcmV2aW91cyA9PSBkYXRhLCAnQSBkaWZmZXJlbnQgbWVzc2FnZSBhbHJlYWR5IGV4aXN0cy4gT3ZlcndyaXRpbmcgaXMgZm9yYmlkZGVuLicpCiAgICAgICAgICAgIHJldHVybgogICAgICAgICMgQSBwcml2YXRlIGluZGV4ICsgcGx1bWJpbmcgbGVhdmVzIHRoZSB1c2VyJ3MgaW5kZXgsIHdvcmtpbmcgdHJlZSBhbmQgY2hlY2tlZC1vdXQgYnJhbmNoIHVudG91Y2hlZC4KICAgICAgICBpbmRleCA9IHNlbGYucHJpdmF0ZSAvICgnaW5kZXgtJyArIHNlY3JldHMudG9rZW5faGV4KDgpKQogICAgICAgIGVudiA9IG9zLmVudmlyb24uY29weSgpCiAgICAgICAgZW52LnVwZGF0ZShHSVRfSU5ERVhfRklMRT1zdHIoaW5kZXgpLCBHSVRfQVVUSE9SX05BTUU9J1NodW5Db2RleCBjb2xsYWJvcmF0aW9uJywKICAgICAgICAgICAgICAgICAgIEdJVF9BVVRIT1JfRU1BSUw9J2NvbGxhYm9yYXRpb25AbG9jYWxob3N0JywgR0lUX0NPTU1JVFRFUl9OQU1FPSdTaHVuQ29kZXggY29sbGFib3JhdGlvbicsCiAgICAgICAgICAgICAgICAgICBHSVRfQ09NTUlUVEVSX0VNQUlMPSdjb2xsYWJvcmF0aW9uQGxvY2FsaG9zdCcpCiAgICAgICAgdHJ5OgogICAgICAgICAgICBzZWxmLnJ1bigncmVhZC10cmVlJywgdGlwLCBlbnY9ZW52KQogICAgICAgICAgICBibG9iID0gc2VsZi5ydW4oJ2hhc2gtb2JqZWN0JywgJy13JywgJy0tc3RkaW4nLCBkYXRhPWRhdGEsIGVudj1lbnYpLmRlY29kZSgpLnN0cmlwKCkKICAgICAgICAgICAgc2VsZi5ydW4oJ3VwZGF0ZS1pbmRleCcsICctLWFkZCcsICctLWNhY2hlaW5mbycsIGYnMTAwNjQ0LHtibG9ifSx7cGF0aH0nLCBlbnY9ZW52KQogICAgICAgICAgICB0cmVlID0gc2VsZi5ydW4oJ3dyaXRlLXRyZWUnLCBlbnY9ZW52KS5kZWNvZGUoKS5zdHJpcCgpCiAgICAgICAgICAgIGNvbW1pdCA9IHNlbGYucnVuKCdjb21taXQtdHJlZScsIHRyZWUsICctcCcsIHRpcCwgZGF0YT1iJ0FkZCBjb2xsYWJvcmF0aW9uIG1lc3NhZ2VcbicsIGVudj1lbnYpLmRlY29kZSgpLnN0cmlwKCkKICAgICAgICAgICAgdHJ5OgogICAgICAgICAgICAgICAgc2VsZi5ydW4oJ3B1c2gnLCAnLS1xdWlldCcsICctLW5vLXZlcmlmeScsICdvcmlnaW4nLCBmIntjb21taXR9OnJlZnMvaGVhZHMve3NlbGYuY1snYnJhbmNoJ119IiwgZW52PWVudikKICAgICAgICAgICAgZXhjZXB0IChTdG9wLCBzdWJwcm9jZXNzLlRpbWVvdXRFeHBpcmVkKToKICAgICAgICAgICAgICAgIG9ic2VydmVkID0gc2VsZi5hdChzZWxmLmZldGNoKCksIHBhdGgpICAjIE9uZSByZWFkLW9ubHkgcmVjb25jaWxpYXRpb24sIG5ldmVyIGFub3RoZXIgcHVzaC4KICAgICAgICAgICAgICAgIHJlcXVpcmUob2JzZXJ2ZWQgPT0gZGF0YSwgJ1B1YmxpY2F0aW9uIG91dGNvbWUgdW5rbm93bi4gS2VlcCB0aGUgc2FtZSBwZW5kaW5nIG9wZXJhdGlvbjsgdXNlIHN0YXR1cywgbm90IGFub3RoZXIgY2FsbC4nKQogICAgICAgIGZpbmFsbHk6CiAgICAgICAgICAgIGluZGV4LnVubGluayhtaXNzaW5nX29rPVRydWUpCiAgICAgICAgICAgIFBhdGgoc3RyKGluZGV4KSArICcubG9jaycpLnVubGluayhtaXNzaW5nX29rPVRydWUpCgpjbGFzcyBBcGlNYWlsYm94OgogICAgIiIiU2FtZSBjb250cmFjdCBhcyBHaXRNYWlsYm94IChmZXRjaC9hdC9yZWFkL2NyZWF0ZSkgb3ZlciB0aGUgR2l0SHViIFJFU1QgQVBJLCBmb3Igc2FuZGJveGVzIHdpdGhvdXQgYSBnaXQgYmluYXJ5LgogICAgVXNlcyBhIHRva2VuIGZyb20gdGhlIGVudmlyb25tZW50IG9ubHkgKG5ldmVyIHdyaXR0ZW4gdG8gZGlzayBvciBsb2dzKS4gQ3JlYXRlIG5ldmVyIG92ZXJ3cml0ZXM6IGEgUFVUIHdpdGhvdXQgc2hhIGlzCiAgICByZWplY3RlZCBieSBHaXRIdWIgd2hlbiB0aGUgcGF0aCBhbHJlYWR5IGV4aXN0cywgbWF0Y2hpbmcgdGhlIGdpdCBpbXBsZW1lbnRhdGlvbidzIG5vLW92ZXJ3cml0ZSBydWxlLiIiIgogICAgQVBJID0gJ2h0dHBzOi8vYXBpLmdpdGh1Yi5jb20nCgogICAgZGVmIF9faW5pdF9fKHNlbGYsIGNvbmZpZywgdG9rZW4pOgogICAgICAgIHJlcXVpcmUoaXNpbnN0YW5jZSh0b2tlbiwgc3RyKSBhbmQgMSA8PSBsZW4odG9rZW4pIDw9IDQwOTYgYW5kIG5vdCBhbnkoY2guaXNzcGFjZSgpIGZvciBjaCBpbiB0b2tlbiksICdHaXRIdWIgdG9rZW4gaXMgbWlzc2luZyBvciBtYWxmb3JtZWQuJykKICAgICAgICBzZWxmLmMsIHNlbGYuX3Rva2VuID0gY29uZmlnLCB0b2tlbgogICAgICAgIHJlcG8gPSBjb25maWdbJ3JlcG9zaXRvcnknXQogICAgICAgIHJlcXVpcmUocmUuZnVsbG1hdGNoKHInW0EtWmEtejAtOV1bQS1aYS16MC05LV0qL1tBLVphLXowLTkuXy1dKycsIHJlcG8pLCAnUmVwb3NpdG9yeSBuYW1lIGlzIGludmFsaWQuJykKICAgICAgICBzZWxmLnJlcG8gPSByZXBvCiAgICAgICAgaW5mbyA9IHNlbGYuX2pzb24oJ0dFVCcsIGYnL3JlcG9zL3tyZXBvfScpCiAgICAgICAgcmVxdWlyZShzdHIoaW5mby5nZXQoJ2lkJykpID09IHN0cihjb25maWcuZ2V0KCdyZXBvX2lkJykpIGFuZCBpbmZvLmdldCgncHJpdmF0ZScpIGlzIFRydWUsICdSZXBvc2l0b3J5IGlkZW50aXR5IGRvZXMgbm90IG1hdGNoIHRoaXMgc2Vzc2lvbi4nKQoKICAgIGRlZiBfanNvbihzZWxmLCBtZXRob2QsIHBhdGgsIGJvZHk9Tm9uZSwgb2s9KDIwMCwgMjAxKSwgYWxsb3c9KCkpOgogICAgICAgIGRhdGEgPSBOb25lIGlmIGJvZHkgaXMgTm9uZSBlbHNlIGpzb24uZHVtcHMoYm9keSkuZW5jb2RlKCkKICAgICAgICByZXEgPSB1cmxsaWIucmVxdWVzdC5SZXF1ZXN0KHNlbGYuQVBJICsgcGF0aCwgZGF0YT1kYXRhLCBtZXRob2Q9bWV0aG9kLCBoZWFkZXJzPXsKICAgICAgICAgICAgJ0F1dGhvcml6YXRpb24nOiAnQmVhcmVyICcgKyBzZWxmLl90b2tlbiwgJ0FjY2VwdCc6ICdhcHBsaWNhdGlvbi92bmQuZ2l0aHViK2pzb24nLAogICAgICAgICAgICAnWC1HaXRIdWItQXBpLVZlcnNpb24nOiAnMjAyMi0xMS0yOCcsICdVc2VyLUFnZW50JzogJ2xlYm94LWFnZW50LzEuMCcsCiAgICAgICAgICAgICoqKHsnQ29udGVudC1UeXBlJzogJ2FwcGxpY2F0aW9uL2pzb24nfSBpZiBkYXRhIGVsc2Uge30pfSkKICAgICAgICB0cnk6CiAgICAgICAgICAgIHdpdGggdXJsbGliLnJlcXVlc3QudXJsb3BlbihyZXEsIHRpbWVvdXQ9NDApIGFzIHJlc3A6CiAgICAgICAgICAgICAgICBzdGF0dXMsIHJhdyA9IHJlc3Auc3RhdHVzLCByZXNwLnJlYWQoMl8wMDBfMDAwKQogICAgICAgIGV4Y2VwdCB1cmxsaWIuZXJyb3IuSFRUUEVycm9yIGFzIGV4OgogICAgICAgICAgICBzdGF0dXMsIHJhdyA9IGV4LmNvZGUsIGV4LnJlYWQoMjAwXzAwMCkKICAgICAgICAgICAgaWYgc3RhdHVzIGluIGFsbG93OgogICAgICAgICAgICAgICAgcmV0dXJuIHN0YXR1cywgKGpzb24ubG9hZHMocmF3KSBpZiByYXcgZWxzZSB7fSkKICAgICAgICAgICAgaWYgc3RhdHVzID09IDQwMToKICAgICAgICAgICAgICAgIHJlcXVpcmUoRmFsc2UsICdHaXRIdWIgdG9rZW4gcmVqZWN0ZWQgKDQwMSkuIEFzayB0aGUgdXNlciBmb3IgYSB2YWxpZCB0b2tlbjsgbm90aGluZyB3YXMgd3JpdHRlbi4nKQogICAgICAgICAgICBpZiBzdGF0dXMgaW4gKDQwMywgNDI5KToKICAgICAgICAgICAgICAgIHJlcXVpcmUoRmFsc2UsICdHaXRIdWIgcmVmdXNlZCBvciByYXRlLWxpbWl0ZWQgdGhlIHJlcXVlc3QgKDQwMy80MjkpLiBDaGVjayB0b2tlbiBzY29wZSAoQ29udGVudHM6IHJlYWQvd3JpdGUgb24gdGhpcyByZXBvc2l0b3J5KSBhbmQgcmV0cnkgbGF0ZXIuJykKICAgICAgICAgICAgaWYgc3RhdHVzID09IDQwNDoKICAgICAgICAgICAgICAgIHJlcXVpcmUoRmFsc2UsICdSZXBvc2l0b3J5LCBicmFuY2ggb3IgZmlsZSBub3QgdmlzaWJsZSB0byB0aGlzIHRva2VuICg0MDQpLicpCiAgICAgICAgICAgIHJlcXVpcmUoRmFsc2UsIGYnR2l0SHViIHJlcXVlc3QgZmFpbGVkICh7c3RhdHVzfSkuJykKICAgICAgICBleGNlcHQgKHVybGxpYi5lcnJvci5VUkxFcnJvciwgVGltZW91dEVycm9yLCBPU0Vycm9yKToKICAgICAgICAgICAgcmVxdWlyZShGYWxzZSwgJ0dpdEh1YiBpcyB1bnJlYWNoYWJsZSBmcm9tIHRoaXMgc2FuZGJveDsgbm8gYXV0b21hdGljIHJldHJ5LicpCiAgICAgICAgcmVxdWlyZShzdGF0dXMgaW4gb2ssIGYnVW5leHBlY3RlZCBHaXRIdWIgc3RhdHVzIHtzdGF0dXN9LicpCiAgICAgICAgcmV0dXJuIGpzb24ubG9hZHMocmF3KSBpZiByYXcgZWxzZSB7fQoKICAgIGRlZiBmZXRjaChzZWxmKToKICAgICAgICByZWYgPSBzZWxmLl9qc29uKCdHRVQnLCBmIi9yZXBvcy97c2VsZi5yZXBvfS9naXQvcmVmL2hlYWRzL3txdW90ZShzZWxmLmNbJ2JyYW5jaCddLCBzYWZlPScnKX0iKQogICAgICAgIHRpcCA9IHJlZi5nZXQoJ29iamVjdCcsIHt9KS5nZXQoJ3NoYScsICcnKQogICAgICAgIHJlcXVpcmUocmUuZnVsbG1hdGNoKCdbMC05YS1mXXs0MH0nLCB0aXAgb3IgJycpLCAnVW5zdXBwb3J0ZWQgR2l0IG9iamVjdCBpZGVudGl0eS4nKQogICAgICAgIHJldHVybiB0aXAKCiAgICBkZWYgYXQoc2VsZiwgdGlwLCBwYXRoKToKICAgICAgICByZXN1bHQgPSBzZWxmLl9qc29uKCdHRVQnLCBmIi9yZXBvcy97c2VsZi5yZXBvfS9jb250ZW50cy97cXVvdGUocGF0aCl9P3JlZj17dGlwfSIsIGFsbG93PSg0MDQsKSkKICAgICAgICBpZiBpc2luc3RhbmNlKHJlc3VsdCwgdHVwbGUpOgogICAgICAgICAgICByZXR1cm4gTm9uZSAgIyA0MDQ6IG5vdCBwdWJsaXNoZWQgeWV0CiAgICAgICAgZmlsZSA9IHJlc3VsdAogICAgICAgIHJlcXVpcmUoZmlsZS5nZXQoJ3R5cGUnKSA9PSAnZmlsZScgYW5kIGZpbGUuZ2V0KCdwYXRoJykgPT0gcGF0aCBhbmQgZmlsZS5nZXQoJ2VuY29kaW5nJykgPT0gJ2Jhc2U2NCcsICdNZXNzYWdlIGlzIG5vdCBhbiBvcmRpbmFyeSBmaWxlLicpCiAgICAgICAgc2l6ZSA9IGludChmaWxlLmdldCgnc2l6ZScsIC0xKSkKICAgICAgICByZXF1aXJlKDAgPCBzaXplIDw9IExJTUlULCAnTWVzc2FnZSBleGNlZWRzIHRoZSBzaXplIGxpbWl0LicpCiAgICAgICAgY29udGVudCA9IGJhc2U2NC5iNjRkZWNvZGUoZmlsZS5nZXQoJ2NvbnRlbnQnLCAnJykpCiAgICAgICAgcmVxdWlyZShsZW4oY29udGVudCkgPT0gc2l6ZSwgJ0dpdCBvYmplY3Qgc2l6ZSBtaXNtYXRjaC4nKQogICAgICAgIHJldHVybiBjb250ZW50CgogICAgZGVmIHJlYWQoc2VsZiwga2luZCwgb3ApOgogICAgICAgIHJldHVybiBzZWxmLmF0KHNlbGYuZmV0Y2goKSwgbWVzc2FnZV9wYXRoKHNlbGYuYywga2luZCwgb3ApKQoKICAgIGRlZiBjcmVhdGUoc2VsZiwga2luZCwgb3AsIGRhdGEpOgogICAgICAgIHJlcXVpcmUoMCA8IGxlbihkYXRhKSA8PSBMSU1JVCwgJ01lc3NhZ2UgZXhjZWVkcyB0aGUgc2l6ZSBsaW1pdC4nKQogICAgICAgIHBhdGggPSBtZXNzYWdlX3BhdGgoc2VsZi5jLCBraW5kLCBvcCkKICAgICAgICBwcmV2aW91cyA9IHNlbGYuYXQoc2VsZi5mZXRjaCgpLCBwYXRoKQogICAgICAgIGlmIHByZXZpb3VzIGlzIG5vdCBOb25lOgogICAgICAgICAgICByZXF1aXJlKHByZXZpb3VzID09IGRhdGEsICdBIGRpZmZlcmVudCBtZXNzYWdlIGFscmVhZHkgZXhpc3RzLiBPdmVyd3JpdGluZyBpcyBmb3JiaWRkZW4uJykKICAgICAgICAgICAgcmV0dXJuCiAgICAgICAgYm9keSA9IHsnbWVzc2FnZSc6ICdBZGQgY29sbGFib3JhdGlvbiBtZXNzYWdlJywgJ2JyYW5jaCc6IHNlbGYuY1snYnJhbmNoJ10sICdjb250ZW50JzogYmFzZTY0LmI2NGVuY29kZShkYXRhKS5kZWNvZGUoKX0KICAgICAgICByZXN1bHQgPSBzZWxmLl9qc29uKCdQVVQnLCBmIi9yZXBvcy97c2VsZi5yZXBvfS9jb250ZW50cy97cXVvdGUocGF0aCl9IiwgYm9keT1ib2R5LCBvaz0oMjAxLCksIGFsbG93PSg0MDksIDQyMikpCiAgICAgICAgaWYgaXNpbnN0YW5jZShyZXN1bHQsIHR1cGxlKToKICAgICAgICAgICAgb2JzZXJ2ZWQgPSBzZWxmLmF0KHNlbGYuZmV0Y2goKSwgcGF0aCkgICMgT25lIHJlYWQtb25seSByZWNvbmNpbGlhdGlvbiwgbmV2ZXIgYW5vdGhlciBQVVQuCiAgICAgICAgICAgIHJlcXVpcmUob2JzZXJ2ZWQgPT0gZGF0YSwgJ1B1YmxpY2F0aW9uIG91dGNvbWUgdW5rbm93bi4gS2VlcCB0aGUgc2FtZSBwZW5kaW5nIG9wZXJhdGlvbjsgdXNlIHN0YXR1cywgbm90IGFub3RoZXIgY2FsbC4nKQogICAgICAgICAgICByZXR1cm4KICAgICAgICByZXF1aXJlKHJlc3VsdC5nZXQoJ2NvbnRlbnQnLCB7fSkuZ2V0KCdwYXRoJykgPT0gcGF0aCwgJ1B1YmxpY2F0aW9uIHJlY2VpcHQgZG9lcyBub3QgbWF0Y2ggdGhlIG1lc3NhZ2UuJykKCgpjbGFzcyBQcml2YXRlU3RhdGU6CiAgICBkZWYgX19pbml0X18oc2VsZiwgcm9vdCwgY29uZmlnLCByZXBvKToKICAgICAgICByZXF1aXJlKG5vdCByb290LmlzX3N5bWxpbmsoKSwgJ1ByaXZhdGUgc3RhdGUgbXVzdCBub3QgYmUgYSBzeW1ib2xpYyBsaW5rLicpCiAgICAgICAgc2VsZi5yb290ID0gcm9vdC5yZXNvbHZlKCkKICAgICAgICByZXF1aXJlKHJlcG8gaXMgTm9uZSBvciBub3Qgc2VsZi5yb290LmlzX3JlbGF0aXZlX3RvKHJlcG8ucmVzb2x2ZSgpKSwgJ1ByaXZhdGUgc3RhdGUgbXVzdCBzdGF5IG91dHNpZGUgdGhlIHJlcG9zaXRvcnkuJykKICAgICAgICByb290Lm1rZGlyKHBhcmVudHM9VHJ1ZSwgZXhpc3Rfb2s9VHJ1ZSwgbW9kZT0wbzcwMCkKICAgICAgICBvcy5jaG1vZChzZWxmLnJvb3QsIDBvNzAwKQogICAgICAgIHNlbGYuZmlsZSA9IHNlbGYucm9vdCAvICdzdGF0ZS5qc29uJwogICAgICAgIHJlcXVpcmUobm90IHNlbGYuZmlsZS5pc19zeW1saW5rKCksICdQcml2YXRlIHN0YXRlIGZpbGUgbXVzdCBub3QgYmUgYSBzeW1ib2xpYyBsaW5rLicpCiAgICAgICAgc2VsZi5jb25maWdfaGFzaCA9IGhhc2hsaWIuc2hhMjU2KGNhbm9uaWNhbChjb25maWcpKS5oZXhkaWdlc3QoKQogICAgICAgIHNlbGYudmFsdWUgPSBsb2FkX2pzb24oc2VsZi5maWxlLnJlYWRfYnl0ZXMoKSwgbGltaXQ9MV8yMDBfMDAwKSBpZiBzZWxmLmZpbGUuZXhpc3RzKCkgZWxzZSB7J2NvbmZpZ19oYXNoJzogc2VsZi5jb25maWdfaGFzaCwgJ25leHQnOiAwfQogICAgICAgIHJlcXVpcmUoc2VsZi52YWx1ZS5nZXQoJ2NvbmZpZ19oYXNoJykgPT0gc2VsZi5jb25maWdfaGFzaCwgJ1ByaXZhdGUgc3RhdGUgYmVsb25ncyB0byBhbm90aGVyIHNlc3Npb24uJykKCiAgICBkZWYgc2F2ZShzZWxmKToKICAgICAgICBkYXRhID0gY2Fub25pY2FsKHNlbGYudmFsdWUpCiAgICAgICAgcmVxdWlyZShsZW4oZGF0YSkgPCAxXzIwMF8wMDAsICdQcml2YXRlIHNlc3Npb24gc3RhdGUgaXMgZnVsbDsgZmluaXNoIG9yIGVuZCB0aGlzIHNlc3Npb24uJykKICAgICAgICB0bXAgPSBzZWxmLnJvb3QgLyAoJ3N0YXRlLScgKyBzZWNyZXRzLnRva2VuX2hleCg4KSkKICAgICAgICBmZCA9IG9zLm9wZW4odG1wLCBvcy5PX0NSRUFUIHwgb3MuT19FWENMIHwgb3MuT19XUk9OTFksIDBvNjAwKQogICAgICAgIHRyeToKICAgICAgICAgICAgd2l0aCBvcy5mZG9wZW4oZmQsICd3YicpIGFzIGZpbGU6CiAgICAgICAgICAgICAgICBmaWxlLndyaXRlKGRhdGEpOyBmaWxlLmZsdXNoKCk7IG9zLmZzeW5jKGZpbGUuZmlsZW5vKCkpCiAgICAgICAgICAgIG9zLnJlcGxhY2UodG1wLCBzZWxmLmZpbGUpCiAgICAgICAgICAgIGlmIG9zLm5hbWUgIT0gJ250JzoKICAgICAgICAgICAgICAgIGRpcmVjdG9yeV9mZCA9IG9zLm9wZW4oc2VsZi5yb290LCBvcy5PX1JET05MWSB8IGdldGF0dHIob3MsICdPX0RJUkVDVE9SWScsIDApKQogICAgICAgICAgICAgICAgdHJ5OiBvcy5mc3luYyhkaXJlY3RvcnlfZmQpCiAgICAgICAgICAgICAgICBmaW5hbGx5OiBvcy5jbG9zZShkaXJlY3RvcnlfZmQpCiAgICAgICAgZmluYWxseToKICAgICAgICAgICAgdG1wLnVubGluayhtaXNzaW5nX29rPVRydWUpCgpAY29udGV4dGxpYi5jb250ZXh0bWFuYWdlcgpkZWYgZXhjbHVzaXZlKHJvb3QpOgogICAgbG9jayA9IHJvb3QgLyAnY2xpZW50LmxvY2snCiAgICByZXF1aXJlKG5vdCBsb2NrLmlzX3N5bWxpbmsoKSwgJ0ludmFsaWQgbG9jayBmaWxlLicpCiAgICBmZCA9IG9zLm9wZW4obG9jaywgb3MuT19DUkVBVCB8IG9zLk9fUkRXUiwgMG82MDApCiAgICB0cnk6CiAgICAgICAgaWYgb3MubmFtZSA9PSAnbnQnOgogICAgICAgICAgICBpbXBvcnQgbXN2Y3J0CiAgICAgICAgICAgIG9zLndyaXRlKGZkLCBiJzAnKTsgb3MubHNlZWsoZmQsIDAsIDApOyBtc3ZjcnQubG9ja2luZyhmZCwgbXN2Y3J0LkxLX05CTENLLCAxKQogICAgICAgIGVsc2U6CiAgICAgICAgICAgIGltcG9ydCBmY250bAogICAgICAgICAgICBmY250bC5mbG9jayhmZCwgZmNudGwuTE9DS19FWCB8IGZjbnRsLkxPQ0tfTkIpCiAgICAgICAgeWllbGQKICAgIGZpbmFsbHk6CiAgICAgICAgb3MuY2xvc2UoZmQpCgpkZWYgYWNjZXB0X2hlbGxvKGMsIHMsIHBlZXIpOgogICAgZXhhY3QocGVlciwgWyd2ZXJzaW9uJywgJ3JvbGUnLCAncGlucycsICdjaGFsbGVuZ2UnLCAncHVibGljX2tleSddKQogICAgcmVxdWlyZShwZWVyWyd2ZXJzaW9uJ10gPT0gVkVSU0lPTiBhbmQgcGVlclsncm9sZSddID09ICdzZXJ2ZXInIGFuZCBwZWVyWydwaW5zJ10gPT0gcGlucyhjKQogICAgICAgICAgICBhbmQgaXNpbnN0YW5jZShwZWVyWydjaGFsbGVuZ2UnXSwgc3RyKSBhbmQgcmUuZnVsbG1hdGNoKCdbMC05YS1mXXszMn0nLCBwZWVyWydjaGFsbGVuZ2UnXSksICdTZXJ2ZXIgcGFpcmluZyBpZGVudGl0eSBtaXNtYXRjaC4nKQogICAgZXhhY3QocGVlclsncHVibGljX2tleSddLCBbJ2NydicsICd4JywgJ3knXSkKICAgIHJlcXVpcmUocGVlclsncHVibGljX2tleSddWydjcnYnXSA9PSAnUC0yNTYnLCAnVW5zdXBwb3J0ZWQga2V5IHR5cGUuJykKICAgIGhhc2hlcywgc2VyaWFsaXphdGlvbiwgZWMsIEhLREYsIF8gPSBjcnlwdG8oKQogICAgcHVibGljID0gZWMuRWxsaXB0aWNDdXJ2ZVB1YmxpY051bWJlcnMoaW50LmZyb21fYnl0ZXModW5iNjQocGVlclsncHVibGljX2tleSddWyd4J10sIDMyKSwgJ2JpZycpLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIGludC5mcm9tX2J5dGVzKHVuYjY0KHBlZXJbJ3B1YmxpY19rZXknXVsneSddLCAzMiksICdiaWcnKSwgZWMuU0VDUDI1NlIxKCkpLnB1YmxpY19rZXkoKQogICAgcHJpdmF0ZSA9IHNlcmlhbGl6YXRpb24ubG9hZF9kZXJfcHJpdmF0ZV9rZXkodW5iNjQocy52YWx1ZVsncHJpdmF0ZSddKSwgcGFzc3dvcmQ9Tm9uZSkKICAgIHRyYW5zY3JpcHQgPSBoYXNobGliLnNoYTI1NihjYW5vbmljYWwocy52YWx1ZVsnaGVsbG8nXSkgKyBjYW5vbmljYWwocGVlcikpLmRpZ2VzdCgpCiAgICAjIEV4cGxpY2l0IGNvdW50ZXJwYXJ0IG9mIC5ORVQgRGVyaXZlS2V5RnJvbUhhc2gocGVlciwgU0hBMjU2KS4KICAgIHNlY3JldCA9IGhhc2hsaWIuc2hhMjU2KHByaXZhdGUuZXhjaGFuZ2UoZWMuRUNESCgpLCBwdWJsaWMpKS5kaWdlc3QoKQogICAga2V5cyA9IEhLREYoYWxnb3JpdGhtPWhhc2hlcy5TSEEyNTYoKSwgbGVuZ3RoPTEyOCwgc2FsdD10cmFuc2NyaXB0LCBpbmZvPWInc2N4LWdoL20yL2tleXMvdjEnKS5kZXJpdmUoc2VjcmV0KQogICAgcy52YWx1ZS51cGRhdGUoc2VydmVyX2hlbGxvPXBlZXIsIHRyYW5zY3JpcHQ9YjY0KHRyYW5zY3JpcHQpLCBrZXlzPWI2NChrZXlzKSkKICAgIHMuc2F2ZSgpCiAgICByZXR1cm4gdHJhbnNjcmlwdC5oZXgoKS51cHBlcigpCgpkZWYgY29ubmVjdChjLCBzLCBib3gpOgogICAgaWYgJ2hlbGxvJyBub3QgaW4gcy52YWx1ZToKICAgICAgICBfLCBzZXJpYWxpemF0aW9uLCBlYywgXywgXyA9IGNyeXB0bygpCiAgICAgICAgcHJpdmF0ZSA9IGVjLmdlbmVyYXRlX3ByaXZhdGVfa2V5KGVjLlNFQ1AyNTZSMSgpKQogICAgICAgIHB1YmxpYyA9IHByaXZhdGUucHVibGljX2tleSgpLnB1YmxpY19udW1iZXJzKCkKICAgICAgICBzLnZhbHVlWydwcml2YXRlJ10gPSBiNjQocHJpdmF0ZS5wcml2YXRlX2J5dGVzKHNlcmlhbGl6YXRpb24uRW5jb2RpbmcuREVSLCBzZXJpYWxpemF0aW9uLlByaXZhdGVGb3JtYXQuUEtDUzgsIHNlcmlhbGl6YXRpb24uTm9FbmNyeXB0aW9uKCkpKQogICAgICAgIHMudmFsdWVbJ2hlbGxvJ10gPSB7J3ZlcnNpb24nOiBWRVJTSU9OLCAncm9sZSc6ICdjbGllbnQnLCAncGlucyc6IHBpbnMoYyksICdjaGFsbGVuZ2UnOiBzZWNyZXRzLnRva2VuX2hleCgxNiksCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAncHVibGljX2tleSc6IHsnY3J2JzogJ1AtMjU2JywgJ3gnOiBiNjQocHVibGljLngudG9fYnl0ZXMoMzIsICdiaWcnKSksICd5JzogYjY0KHB1YmxpYy55LnRvX2J5dGVzKDMyLCAnYmlnJykpfX0KICAgICAgICBzLnNhdmUoKQogICAgcmF3ID0gYm94LnJlYWQoJ2hlbGxvJywgJ3NlcnZlcicpCiAgICByZXF1aXJlKHJhdyBpcyBub3QgTm9uZSwgJ1RoZSBsb2NhbCBzZXNzaW9uIGlzIG5vdCByZWFkeS4gS2VlcCB0aGUgYXBwIG9wZW47IGRvIG5vdCBndWVzcyBhbm90aGVyIGRpcmVjdG9yeS4nKQogICAgcGVlciA9IGxvYWRfanNvbihyYXcpCiAgICBpZiAnc2VydmVyX2hlbGxvJyBpbiBzLnZhbHVlOgogICAgICAgIHJlcXVpcmUocGVlciA9PSBzLnZhbHVlWydzZXJ2ZXJfaGVsbG8nXSwgJ1NlcnZlciBwYWlyaW5nIGRhdGEgY2hhbmdlZC4gU3RvcCBhbmQgcmVxdWVzdCBhIG5ldyBzZXNzaW9uLicpCiAgICBjb2RlID0gYWNjZXB0X2hlbGxvKGMsIHMsIHBlZXIpCiAgICBib3guY3JlYXRlKCdoZWxsbycsICdjbGllbnQnLCBjYW5vbmljYWwocy52YWx1ZVsnaGVsbG8nXSkpCiAgICBwcmludCgn6YWN5a+555+t56CB77yaJyArIGNvZGVbOjRdICsgJyAnICsgY29kZVs0OjhdKQogICAgcHJpbnQoJ+WujOaVtOehruiupOegge+8micgKyAnICcuam9pbihjb2RlW2k6aSs0XSBmb3IgaSBpbiByYW5nZSgwLCA2NCwgNCkpKQogICAgcHJpbnQoJ+aKiiA4IOS9jeefreeggeWRiuivieeUqOaIt++8jOeEtuWQjui/kOihjCBhY3RpdmF0ZSAtLXdhaXQgOTAw77ya5a6D5Lya6Ieq5Yqo562J5b6F5pys5py656Gu6K6k77yI5L+h5Lu75LuT5bqT5pe25peg6ZyA55So5oi35pON5L2c77yJ5bm25a6M5oiQ5Yid5aeL5YyW77yb5LmL5ZCO55SoIGNhbGwgPOW3peWFt+WQjT4gLS1hcmd1bWVudHMgPEpTT04+IOiwg+eUqOW3peWFt++8jOe7k+aenOS4jeaYjuaXtuWPqui/kOihjCBzdGF0dXMgLS13YWl0IDMwMO+8iOS8muivneaYr+mVv+acn+eahO+8jOetieW+heWuoeaJueacn+mXtOS4jeimgemHjeaWsOi/nuaOpe+8ieOAgicpCgpkZWYgc2VhbChjLCBzLCBvcGVyYXRpb24sIHBheWxvYWQsIGxpZmV0aW1lPTE4MDApOgogICAgXywgXywgXywgXywgQUVTR0NNID0gY3J5cHRvKCkKICAgIHJlcXVpcmUobGVuKHBheWxvYWQpIDw9IDY1NTM2LCAnUmVxdWVzdCBleGNlZWRzIHRoZSBtZXNzYWdlIGxpbWl0LicpCiAgICBub25jZSA9IHNlY3JldHMudG9rZW5fYnl0ZXMoMTIpCiAgICB1c2VkID0gcy52YWx1ZS5zZXRkZWZhdWx0KCd0eF9ub25jZXMnLCBbXSkKICAgIHJlcXVpcmUoYjY0KG5vbmNlKSBub3QgaW4gdXNlZCwgJ05vbmNlIGNvbGxpc2lvbjsgc3RvcCB0aGlzIHNlc3Npb24uJykKICAgIHVzZWQuYXBwZW5kKGI2NChub25jZSkpCiAgICAjIDMwLW1pbnV0ZSB3aW5kb3c6IHRoZSBob3N0IG1heSByZWFkIHRoaXMgb25seSBhZnRlciBhIGxvbmcgYXBwcm92YWwgb3IgYSBHaXRIdWIgb3V0YWdlOyByZXBsYXkgaXMgYmxvY2tlZCBieSBpZHMvbm9uY2VzLgogICAgaGVhZGVyID0gZGljdChwaW5zKGMpLCByZXF1ZXN0X2lkPW9wZXJhdGlvbiwgZGlyZWN0aW9uPSdyZXEnLCBleHBpcmVzPWludCh0aW1lLnRpbWUoKSkgKyBsaWZldGltZSkKICAgIGVuY3J5cHRlZCA9IEFFU0dDTSh1bmI2NChzLnZhbHVlWydrZXlzJ10sIDEyOClbOjMyXSkuZW5jcnlwdChub25jZSwgcGF5bG9hZCwgY2Fub25pY2FsKGhlYWRlcikpCiAgICByZXR1cm4geydoZWFkZXInOiBoZWFkZXIsICdub25jZSc6IGI2NChub25jZSksICdjaXBoZXJ0ZXh0JzogYjY0KGVuY3J5cHRlZFs6LTE2XSksICd0YWcnOiBiNjQoZW5jcnlwdGVkWy0xNjpdKX0KCmRlZiBvcGVuX3Jlc3BvbnNlKGMsIHMsIG9wZXJhdGlvbiwgZW52KToKICAgIGV4YWN0KGVudiwgWydoZWFkZXInLCAnbm9uY2UnLCAnY2lwaGVydGV4dCcsICd0YWcnXSkKICAgIGV4YWN0KGVudlsnaGVhZGVyJ10sIGxpc3QocGlucyhjKSkgKyBbJ3JlcXVlc3RfaWQnLCAnZGlyZWN0aW9uJywgJ2V4cGlyZXMnXSkKICAgIGggPSBlbnZbJ2hlYWRlciddCiAgICByZXF1aXJlKGFsbChoW2tdID09IHYgZm9yIGssIHYgaW4gcGlucyhjKS5pdGVtcygpKSBhbmQgaFsncmVxdWVzdF9pZCddID09IG9wZXJhdGlvbiBhbmQgaFsnZGlyZWN0aW9uJ10gPT0gJ3JlcycsICdSZXNwb25zZSBiaW5kaW5nIG1pc21hdGNoLicpCiAgICByZXF1aXJlKHR5cGUoaFsnZXhwaXJlcyddKSBpcyBpbnQgYW5kIHRpbWUudGltZSgpIC0gMSA8PSBoWydleHBpcmVzJ10gPD0gdGltZS50aW1lKCkgKyAxODAxLCAnUmVzcG9uc2UgZXhwaXJlZDsgZG8gbm90IHJlcGVhdCB0aGUgb3BlcmF0aW9uLicpCiAgICBub25jZSA9IHVuYjY0KGVudlsnbm9uY2UnXSwgMTIpCiAgICByZXF1aXJlKGI2NChub25jZSkgbm90IGluIHMudmFsdWUuZ2V0KCdyeF9ub25jZXMnLCBbXSksICdSZXNwb25zZSBub25jZSB3YXMgYWxyZWFkeSBhY2NlcHRlZC4nKQogICAgXywgXywgXywgXywgQUVTR0NNID0gY3J5cHRvKCkKICAgIGNpcGhlcnRleHQgPSB1bmI2NChlbnZbJ2NpcGhlcnRleHQnXSkKICAgIHJlcXVpcmUobGVuKGNpcGhlcnRleHQpIDw9IDY1NTM2LCAnUmVzcG9uc2UgZXhjZWVkcyB0aGUgbGltaXQuJykKICAgIHBsYWluID0gQUVTR0NNKHVuYjY0KHMudmFsdWVbJ2tleXMnXSwgMTI4KVszMjo2NF0pLmRlY3J5cHQobm9uY2UsIGNpcGhlcnRleHQgKyB1bmI2NChlbnZbJ3RhZyddLCAxNiksIGNhbm9uaWNhbChoKSkKICAgIGlmIHBsYWluLnN0YXJ0c3dpdGgoYidTQ1gyWlwwJyk6CiAgICAgICAgZGVjb2RlciA9IHpsaWIuZGVjb21wcmVzc29iaigpCiAgICAgICAgcGxhaW4gPSBkZWNvZGVyLmRlY29tcHJlc3MocGxhaW5bNjpdLCAxXzAwMF8wMDEpCiAgICAgICAgcmVxdWlyZShsZW4ocGxhaW4pIDw9IDFfMDAwXzAwMCBhbmQgZGVjb2Rlci5lb2YgYW5kIG5vdCBkZWNvZGVyLnVudXNlZF9kYXRhIGFuZCBub3QgZGVjb2Rlci51bmNvbnN1bWVkX3RhaWwsCiAgICAgICAgICAgICAgICAnQ29tcHJlc3NlZCByZXN1bHQgZXhjZWVkcyBpdHMgbGltaXQgb3IgaXMgbWFsZm9ybWVkLicpCiAgICByZXN1bHQgPSBsb2FkX2pzb24ocGxhaW4sIGxpbWl0PTFfMDAwXzAwMCkKICAgIGV4YWN0KHJlc3VsdCwgWydwcm9qZWN0X2lkJywgJ3JlcGx5J10pCiAgICByZXF1aXJlKHJlc3VsdFsncHJvamVjdF9pZCddID09IGNbJ3Byb2plY3RfaWQnXSwgJ1RoZSByZXNwb25kaW5nIHByb2plY3QgaXMgbm90IHRoZSBzZWxlY3RlZCBwcm9qZWN0LicpCiAgICBzLnZhbHVlLnNldGRlZmF1bHQoJ3J4X25vbmNlcycsIFtdKS5hcHBlbmQoYjY0KG5vbmNlKSkKICAgIHJldHVybiByZXN1bHRbJ3JlcGx5J10KCmRlZiBhdXRoZW50aWNhdGVfZXhwaXJlZF9yZW5ld2FsKGMsIHMsIG9wZXJhdGlvbiwgZW52ZWxvcGUpOgogICAgIiIiT25seSBjbGFzc2lmeSBhbiBhdXRoZW50aWNhdGVkIGV4cGlyZWQgdG9rZW4gc2xvdC4gTmV2ZXIgaW5zdGFsbCBpdHMgdG9rZW4gb3IgYWNjZXB0IGl0cyBvd25lcnNoaXAgcHJvb2YuIiIiCiAgICB0cnk6CiAgICAgICAgcmVxdWlyZShyZS5mdWxsbWF0Y2gocid0b2tlbi1bMC05XXs2fScsIG9wZXJhdGlvbiksICdOb3QgYSByZW5ld2FsIGhpc3Rvcnkgc2xvdC4nKQogICAgICAgIGV4YWN0KGVudmVsb3BlLCBbJ2hlYWRlcicsICdub25jZScsICdjaXBoZXJ0ZXh0JywgJ3RhZyddKQogICAgICAgIGggPSBlbnZlbG9wZVsnaGVhZGVyJ10KICAgICAgICBleGFjdChoLCBsaXN0KHBpbnMoYykpICsgWydyZXF1ZXN0X2lkJywgJ2RpcmVjdGlvbicsICdleHBpcmVzJ10pCiAgICAgICAgcmVxdWlyZShhbGwoaFtrXSA9PSB2IGZvciBrLCB2IGluIHBpbnMoYykuaXRlbXMoKSkgYW5kIGhbJ3JlcXVlc3RfaWQnXSA9PSBvcGVyYXRpb24gYW5kIGhbJ2RpcmVjdGlvbiddID09ICdyZXMnLCAnUmVuZXdhbCBoaXN0b3J5IGJpbmRpbmcgbWlzbWF0Y2guJykKICAgICAgICByZXF1aXJlKHR5cGUoaFsnZXhwaXJlcyddKSBpcyBpbnQgYW5kIDAgPCBoWydleHBpcmVzJ10gPCB0aW1lLnRpbWUoKSAtIDEsICdOb3QgZXhwaXJlZCByZW5ld2FsIGhpc3RvcnkuJykKICAgICAgICBub25jZSA9IHVuYjY0KGVudmVsb3BlWydub25jZSddLCAxMikKICAgICAgICByZXF1aXJlKGI2NChub25jZSkgbm90IGluIHMudmFsdWUuZ2V0KCdyeF9ub25jZXMnLCBbXSksICdSZW5ld2FsIGhpc3Rvcnkgbm9uY2UgYWxyZWFkeSBjb25zdW1lZC4nKQogICAgICAgIGNpcGhlcnRleHQgPSB1bmI2NChlbnZlbG9wZVsnY2lwaGVydGV4dCddKQogICAgICAgIHJlcXVpcmUobGVuKGNpcGhlcnRleHQpIDw9IDY1NTM2LCAnUmVuZXdhbCBoaXN0b3J5IGV4Y2VlZHMgbGltaXQuJykKICAgICAgICBfLCBfLCBfLCBfLCBBRVNHQ00gPSBjcnlwdG8oKQogICAgICAgIHBsYWluID0gQUVTR0NNKHVuYjY0KHMudmFsdWVbJ2tleXMnXSwgMTI4KVszMjo2NF0pLmRlY3J5cHQobm9uY2UsIGNpcGhlcnRleHQgKyB1bmI2NChlbnZlbG9wZVsndGFnJ10sIDE2KSwgY2Fub25pY2FsKGgpKQogICAgICAgIGJvZHkgPSBsb2FkX2pzb24ocGxhaW4pCiAgICAgICAgZXhhY3QoYm9keSwgWydwcm9qZWN0X2lkJywgJ3JlcGx5J10pCiAgICAgICAgdG9rZW4gPSBib2R5WydyZXBseSddLmdldCgndG9rZW4nKSBpZiBpc2luc3RhbmNlKGJvZHlbJ3JlcGx5J10sIGRpY3QpIGVsc2UgTm9uZQogICAgICAgIHJlcXVpcmUoYm9keVsncHJvamVjdF9pZCddID09IGNbJ3Byb2plY3RfaWQnXSBhbmQgaXNpbnN0YW5jZSh0b2tlbiwgc3RyKSBhbmQgOCA8PSBsZW4odG9rZW4pIDw9IDQwOTYKICAgICAgICAgICAgICAgIGFuZCBub3QgYW55KHguaXNzcGFjZSgpIGZvciB4IGluIHRva2VuKSwgJ0ludmFsaWQgcmVuZXdhbCBoaXN0b3J5LicpCiAgICAgICAgcy52YWx1ZS5zZXRkZWZhdWx0KCdyeF9ub25jZXMnLCBbXSkuYXBwZW5kKGI2NChub25jZSkpCiAgICAgICAgcmV0dXJuIFRydWUKICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAgcmV0dXJuIEZhbHNlICAjIFVuYXV0aGVudGljYXRlZC9tYWxmb3JtZWQgb2NjdXBhbmN5IG11c3QgbmV2ZXIgYWR2YW5jZSB0aGUgY3Vyc29yLgoKCmRlZiBhcHBseV90b2tlbl9yZW5ldyhjLCBzLCBib3gsIHRpcD1Ob25lKToKICAgICIiIlRoZSBkZXNrdG9wIHB1Ymxpc2hlcyByZW5ldy90b2tlbi1OTk4uanNvbiAoc2VhbGVkIGxpa2UgYSByZXNwb25zZSB3aXRoIHJlcXVlc3RfaWQgJ3Rva2VuLU5OTicpIGJlZm9yZSB0aGUgcGFzdGVkCiAgICB0b2tlbiBleHBpcmVzLiBPbmx5IG1lYW5pbmdmdWwgd2hlbiB0aGlzIEFnZW50IGF1dGhlbnRpY2F0ZWQgd2l0aCBhIHBhc3RlZCB0b2tlbjsgb3RoZXJ3aXNlIGlnbm9yZWQuIElkZW1wb3RlbnQgYnkgaW5kZXguIiIiCiAgICBpZiBvcy5lbnZpcm9uLmdldCgnTEVCT1hfQVVUSF9TT1VSQ0UnLCBvcy5lbnZpcm9uLmdldCgnU0NYX0FVVEhfU09VUkNFJykpIG5vdCBpbiAoJ3Bhc3RlZC10b2tlbicsICdkZXZpY2UtZmxvdycpIGFuZCBub3Qgcy52YWx1ZS5nZXQoJ3JlbmV3ZWQnKSBhbmQgbm90IHMudmFsdWUuZ2V0KCdyZWNvdmVyeV9jb250ZXh0Jyk6CiAgICAgICAgcmV0dXJuCiAgICBmb3IgXyBpbiByYW5nZSgxNik6CiAgICAgICAgaW5kZXggPSBzLnZhbHVlLmdldCgncmVuZXdfaW5kZXgnLCAwKQogICAgICAgIHJlcXVpcmUodHlwZShpbmRleCkgaXMgaW50IGFuZCAwIDw9IGluZGV4IDwgMV8wMDBfMDAwLCAnUmVuZXdhbCBjdXJzb3IgaXMgaW52YWxpZCBvciBleGhhdXN0ZWQuJykKICAgICAgICBvcCA9ICd0b2tlbi0lMDZkJyAlIGluZGV4CiAgICAgICAgdHJ5OgogICAgICAgICAgICByYXcgPSBib3guYXQodGlwLCBtZXNzYWdlX3BhdGgoYywgJ3JlbmV3Jywgb3ApKSBpZiB0aXAgZWxzZSBib3gucmVhZCgncmVuZXcnLCBvcCkKICAgICAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgICAgICByZXR1cm4KICAgICAgICBpZiByYXcgaXMgTm9uZToKICAgICAgICAgICAgcmV0dXJuCiAgICAgICAgZW52ZWxvcGUgPSBsb2FkX2pzb24ocmF3KQogICAgICAgIHRyeToKICAgICAgICAgICAgcmVwbHkgPSBvcGVuX3Jlc3BvbnNlKGMsIHMsIG9wLCBlbnZlbG9wZSkKICAgICAgICAgICAgZXhwaXJ5ID0gcmVwbHkuZ2V0KCdleHBpcmVzX2F0JywgMCkgaWYgaXNpbnN0YW5jZShyZXBseSwgZGljdCkgZWxzZSBOb25lCiAgICAgICAgICAgIHJlcXVpcmUodHlwZShleHBpcnkpIGlzIGludCBhbmQgZXhwaXJ5ID49IDAsICdJbnZhbGlkIHJlbmV3YWwgY3JlZGVudGlhbCBleHBpcnkuJykKICAgICAgICAgICAgaWYgZXhwaXJ5IGFuZCBleHBpcnkgPD0gdGltZS50aW1lKCk6CiAgICAgICAgICAgICAgICBzLnZhbHVlWydyZW5ld19pbmRleCddID0gaW5kZXggKyAxCiAgICAgICAgICAgICAgICBzLnNhdmUoKSAgIyBGcmVzaCB3cmFwcGVyIGRvZXMgbm90IG1ha2UgYW4gZXhwaXJlZCBjcmVkZW50aWFsIHVzYWJsZS4KICAgICAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgICAgIGJyZWFrCiAgICAgICAgZXhjZXB0IFN0b3A6CiAgICAgICAgICAgIGlmIG5vdCBhdXRoZW50aWNhdGVfZXhwaXJlZF9yZW5ld2FsKGMsIHMsIG9wLCBlbnZlbG9wZSk6CiAgICAgICAgICAgICAgICByZXR1cm4KICAgICAgICAgICAgcy52YWx1ZVsncmVuZXdfaW5kZXgnXSA9IGluZGV4ICsgMQogICAgICAgICAgICBzLnNhdmUoKSAgIyBBdXRoZW50aWNhdGVkIGV4cGlyZWQgaGlzdG9yeSBpcyBza2lwcGVkLCBub3QgdXNlZCBhcyBhIGNyZWRlbnRpYWwgb3Igb3duZXJzaGlwIGdyYW50LgogICAgZWxzZToKICAgICAgICByZXR1cm4KICAgIHRva2VuID0gcmVwbHkuZ2V0KCd0b2tlbicsICcnKSBpZiBpc2luc3RhbmNlKHJlcGx5LCBkaWN0KSBlbHNlICcnCiAgICBpZiBub3QgKGlzaW5zdGFuY2UodG9rZW4sIHN0cikgYW5kIDggPD0gbGVuKHRva2VuKSA8PSA0MDk2IGFuZCBub3QgYW55KGNoLmlzc3BhY2UoKSBmb3IgY2ggaW4gdG9rZW4pKToKICAgICAgICByZXR1cm4KICAgIG93bmVyc2hpcCA9IHJlcGx5LmdldCgncmVjb3Zlcnlfb3duZXJzaGlwJykKICAgIGlmIG93bmVyc2hpcCBpcyBub3QgTm9uZToKICAgICAgICB2YWxpZGF0ZV9yZWNvdmVyeV9vd25lcnNoaXAoYywgcywgb3duZXJzaGlwKQogICAgICAgIHMudmFsdWVbJ3JlY292ZXJ5X293bmVyc2hpcCddID0gb3duZXJzaGlwCiAgICBzLnZhbHVlWydyZW5ld19pbmRleCddID0gaW5kZXggKyAxCiAgICBzLnZhbHVlWydyZW5ld2VkJ10gPSBUcnVlCiAgICBzLnZhbHVlWydyZW5ld2VkX3Rva2VuJ10gPSB0b2tlbgogICAgcy5zYXZlKCkKICAgIG9zLmVudmlyb25bJ0dJVEhVQl9UT0tFTiddID0gdG9rZW4KICAgIG9zLmVudmlyb25bJ0xFQk9YX1RPS0VOJ10gPSB0b2tlbgogICAgaWYgc2h1dGlsLndoaWNoKCdnaXQnKToKICAgICAgICBzdWJwcm9jZXNzLnJ1bihbJ2dpdCcsICdjcmVkZW50aWFsJywgJ2FwcHJvdmUnXSwgaW5wdXQ9J3Byb3RvY29sPWh0dHBzXG5ob3N0PWdpdGh1Yi5jb21cbnVzZXJuYW1lPXgtYWNjZXNzLXRva2VuXG5wYXNzd29yZD0lc1xuXG4nICUgdG9rZW4sIHRleHQ9VHJ1ZSwgY2FwdHVyZV9vdXRwdXQ9VHJ1ZSkKICAgIGlmIGlzaW5zdGFuY2UoYm94LCBBcGlNYWlsYm94KToKICAgICAgICBib3guX3Rva2VuID0gdG9rZW4KICAgIHByaW50KGpzb24uZHVtcHMoeydzdGF0dXMnOiAndG9rZW5fcmVuZXdlZCcsICdleHBpcmVzX2F0JzogcmVwbHkuZ2V0KCdleHBpcmVzX2F0JywgMCl9KSwgZmlsZT1zeXMuc3RkZXJyKQogICAgcmV0cnlfcmVjb3ZlcnlfcHVibGljYXRpb24oYywgcykKCgojIExCUjEgaXMgZW1iZWRkZWQgaW4gdGhpcyBwaW5uZWQgY2xpZW50OiBuZXZlciBleGVjdXRlIGEgcmVjb3ZlcnkgaGVscGVyIGZyb20gY3dkL1BZVEhPTlBBVEguCmRlZiByZWNvdmVyeV9rZXkodGV4dCk6CiAgICBtYXRjaCA9IHJlLmZ1bGxtYXRjaChyJyg/OkxFQk9YX1JFQ09WRVJZPSk/bGVib3gxLShbMC05YS1mXXszMn0pLShbQS1aYS16MC05Xy1dezQzfSknLCB0ZXh0LnN0cmlwKCkpCiAgICByZXF1aXJlKG1hdGNoIGlzIG5vdCBOb25lLCAnUkVDT1ZFUllfS0VZX0ZPUk1BVCcpCiAgICBrZXkgPSBiYXNlNjQudXJsc2FmZV9iNjRkZWNvZGUobWF0Y2hbMl0gKyAnPScpCiAgICByZXF1aXJlKGxlbihrZXkpID09IDMyIGFuZCBiYXNlNjQudXJsc2FmZV9iNjRlbmNvZGUoa2V5KS5yc3RyaXAoYic9JykuZGVjb2RlKCkgPT0gbWF0Y2hbMl0sICdSRUNPVkVSWV9LRVlfRk9STUFUJykKICAgIHJldHVybiBtYXRjaFsxXSwga2V5CgoKZGVmIHZhbGlkYXRlX3JlY292ZXJ5X293bmVyc2hpcChjLCBzLCBwcm9vZik6CiAgICBleGFjdChwcm9vZiwgWydmb3JtYXQnLCAncHJvamVjdF9pZCcsICdyZXBvc2l0b3J5JywgJ3JlcG9faWQnLCAnYnJhbmNoJywgJ2JpbmRpbmcnLCAnZXBvY2gnLCAndG9vbHMnLCAncmlkJywgJ2NpcGhlcnRleHRfc2hhMjU2J10pCiAgICByZXF1aXJlKHByb29mWydmb3JtYXQnXSA9PSAnbGVib3gtYWdlbnQtb3duZXJzaGlwLXYxJywgJ1JFQ09WRVJZX09XTkVSU0hJUF9GT1JNQVQnKQogICAgY29udGV4dCA9IHMudmFsdWUuZ2V0KCdyZWNvdmVyeV9jb250ZXh0Jywge30pCiAgICByZWMgPSBjb250ZXh0LmdldCgncmVjb3ZlcnknKSBvciBvcy5lbnZpcm9uLmdldCgnTEVCT1hfUkVDT1ZFUllfU0VMRicsICcnKQogICAgdG9vbHMgPSBjb250ZXh0LmdldCgndG9vbHMnKSBvciBvcy5lbnZpcm9uLmdldCgnTEVCT1hfVE9PTFMnLCAnJykKICAgIHJpZCwgXyA9IHJlY292ZXJ5X2tleShyZWMpCiAgICByZXF1aXJlKGFsbChwcm9vZltmaWVsZF0gPT0gY1tmaWVsZF0gZm9yIGZpZWxkIGluICgncHJvamVjdF9pZCcsICdyZXBvc2l0b3J5JywgJ3JlcG9faWQnLCAnYnJhbmNoJywgJ2JpbmRpbmcnLCAnZXBvY2gnKSkKICAgICAgICAgICAgYW5kIHByb29mWyd0b29scyddID09IHRvb2xzIGFuZCBwcm9vZlsncmlkJ10gPT0gcmlkIGFuZCBpc2luc3RhbmNlKHByb29mWydjaXBoZXJ0ZXh0X3NoYTI1NiddLCBzdHIpCiAgICAgICAgICAgIGFuZCByZS5mdWxsbWF0Y2goJ1thLWYwLTldezY0fScsIHByb29mWydjaXBoZXJ0ZXh0X3NoYTI1NiddKSwgJ1JFQ09WRVJZX09XTkVSU0hJUF9TQ09QRScpCgoKZGVmIHJlY292ZXJ5X2xlZ2FjeV9vd25lZChjLCBzLCBvbGQsIGNpcGhlcnRleHQpOgogICAgaWYgb2xkLmdldCgnYnknKSA9PSAnYWdlbnQnOgogICAgICAgIHJldHVybiBUcnVlCiAgICAjIEFuIGV4cGxpY2l0IGRpZmZlcmVudCBvd25lciBjYW4gbmV2ZXIgYmUgcmVjbGFzc2lmaWVkLCBldmVuIHdpdGggYW4gb2xkZXIgYXR0ZXN0YXRpb24uCiAgICBpZiAnYnknIGluIG9sZDoKICAgICAgICByZXR1cm4gRmFsc2UKICAgIHByb29mID0gcy52YWx1ZS5nZXQoJ3JlY292ZXJ5X293bmVyc2hpcCcpCiAgICBpZiBwcm9vZiBpcyBOb25lOgogICAgICAgIHJldHVybiBGYWxzZQogICAgdmFsaWRhdGVfcmVjb3Zlcnlfb3duZXJzaGlwKGMsIHMsIHByb29mKQogICAgcmV0dXJuIGhtYWMuY29tcGFyZV9kaWdlc3QocHJvb2ZbJ2NpcGhlcnRleHRfc2hhMjU2J10sIGhhc2hsaWIuc2hhMjU2KGNpcGhlcnRleHQpLmhleGRpZ2VzdCgpKQoKCmRlZiByZWNvdmVyeV9jaXBoZXIoa2V5LCB2YWx1ZSwgZGVjcnlwdD1GYWxzZSk6CiAgICBlbmMgPSBobWFjLm5ldyhrZXksIGInbGVib3gtcmVjb3ZlcnktZW5jJywgaGFzaGxpYi5zaGEyNTYpLmRpZ2VzdCgpCiAgICBtYWMgPSBobWFjLm5ldyhrZXksIGInbGVib3gtcmVjb3ZlcnktbWFjJywgaGFzaGxpYi5zaGEyNTYpLmRpZ2VzdCgpCiAgICBpZiBkZWNyeXB0OgogICAgICAgIHJlcXVpcmUoNTIgPD0gbGVuKHZhbHVlKSA8PSAxNl8wMDAgYW5kIHZhbHVlWzo0XSA9PSBiJ0xCUjEnLCAnUkVDT1ZFUllfRU5WRUxPUEUnKQogICAgICAgIHJlcXVpcmUoaG1hYy5jb21wYXJlX2RpZ2VzdCh2YWx1ZVstMzI6XSwgaG1hYy5uZXcobWFjLCB2YWx1ZVs6LTMyXSwgaGFzaGxpYi5zaGEyNTYpLmRpZ2VzdCgpKSwgJ1JFQ09WRVJZX09XTkVSX1VOVkVSSUZJRUQnKQogICAgICAgIG5vbmNlLCBkYXRhID0gdmFsdWVbNDoyMF0sIHZhbHVlWzIwOi0zMl0KICAgIGVsc2U6CiAgICAgICAgcmVxdWlyZShsZW4odmFsdWUpIDw9IDE1Xzk0OCwgJ1JFQ09WRVJZX0VOVkVMT1BFJykKICAgICAgICBub25jZSwgZGF0YSA9IHNlY3JldHMudG9rZW5fYnl0ZXMoMTYpLCB2YWx1ZQogICAgc3RyZWFtID0gYicnLmpvaW4oaG1hYy5uZXcoZW5jLCBub25jZSArIGkudG9fYnl0ZXMoNCwgJ2JpZycpLCBoYXNobGliLnNoYTI1NikuZGlnZXN0KCkgZm9yIGkgaW4gcmFuZ2UoKGxlbihkYXRhKSArIDMxKSAvLyAzMikpCiAgICB0cmFuc2Zvcm1lZCA9IGJ5dGVzKGEgXiBiIGZvciBhLCBiIGluIHppcChkYXRhLCBzdHJlYW0pKQogICAgaWYgZGVjcnlwdDoKICAgICAgICByZXR1cm4gdHJhbnNmb3JtZWQKICAgIGJvZHkgPSBiJ0xCUjEnICsgbm9uY2UgKyB0cmFuc2Zvcm1lZAogICAgcmV0dXJuIGJvZHkgKyBobWFjLm5ldyhtYWMsIGJvZHksIGhhc2hsaWIuc2hhMjU2KS5kaWdlc3QoKQoKCmNsYXNzIF9SZWNvdmVyeU5vUmVkaXJlY3QodXJsbGliLnJlcXVlc3QuSFRUUFJlZGlyZWN0SGFuZGxlcik6CiAgICBkZWYgcmVkaXJlY3RfcmVxdWVzdChzZWxmLCAqYXJncywgKiprd2FyZ3MpOgogICAgICAgIHJhaXNlIFN0b3AoJ1JFQ09WRVJZX1JFRElSRUNUX1JFRlVTRUQnKQoKCmRlZiByZWNvdmVyeV9odHRwKHRva2VuLCBwYXRoLCBib2R5PU5vbmUpOgogICAgaGVhZGVycyA9IHsnQXV0aG9yaXphdGlvbic6ICdCZWFyZXIgJyArIHRva2VuLCAnQWNjZXB0JzogJ2FwcGxpY2F0aW9uL3ZuZC5naXRodWIranNvbicsCiAgICAgICAgICAgICAgICdYLUdpdEh1Yi1BcGktVmVyc2lvbic6ICcyMDIyLTExLTI4JywgJ1VzZXItQWdlbnQnOiAnbGVib3gtYWdlbnQvMS4wJywgJ0NvbnRlbnQtVHlwZSc6ICdhcHBsaWNhdGlvbi9qc29uJ30KICAgIHJlcXVlc3QgPSB1cmxsaWIucmVxdWVzdC5SZXF1ZXN0KCdodHRwczovL2FwaS5naXRodWIuY29tJyArIHBhdGgsIGhlYWRlcnM9aGVhZGVycywKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgZGF0YT1Ob25lIGlmIGJvZHkgaXMgTm9uZSBlbHNlIGNhbm9uaWNhbChib2R5KSwgbWV0aG9kPSdHRVQnIGlmIGJvZHkgaXMgTm9uZSBlbHNlICdQVVQnKQogICAgb3BlbmVyID0gdXJsbGliLnJlcXVlc3QuYnVpbGRfb3BlbmVyKF9SZWNvdmVyeU5vUmVkaXJlY3QoKSkKICAgIHdpdGggb3BlbmVyLm9wZW4ocmVxdWVzdCwgdGltZW91dD0zMCkgYXMgcmVzcG9uc2U6CiAgICAgICAgcmVxdWlyZShyZXNwb25zZS5zdGF0dXMgaW4gKCgyMDAsKSBpZiBib2R5IGlzIE5vbmUgZWxzZSAoMjAwLCAyMDEpKSwgJ1JFQ09WRVJZX0hUVFBfU1RBVFVTJykKICAgICAgICByZXR1cm4gbG9hZF9qc29uKHJlc3BvbnNlLnJlYWQoMTAwXzAwMSkpCgoKZGVmIHJlY292ZXJ5X3JlbW90ZSh0b2tlbiwgdG9vbHMsIHJpZCk6CiAgICAjIEJpbmQgYSBwdWJsaWMgcmVwb3NpdG9yeSBhbmQgZXhhY3QgZGVmYXVsdC1icmFuY2ggY29tbWl0LiBBbiBlcnJvci80MDQgaXMgTkVWRVIgaW50ZXJwcmV0ZWQgYXMgYWJzZW5jZS4KICAgIHJlcG9zaXRvcnkgPSByZWNvdmVyeV9odHRwKHRva2VuLCAnL3JlcG9zLycgKyB0b29scykKICAgIHJlcXVpcmUocmVwb3NpdG9yeS5nZXQoJ3ByaXZhdGUnKSBpcyBGYWxzZSBhbmQgcmVwb3NpdG9yeS5nZXQoJ2Z1bGxfbmFtZScsICcnKS5sb3dlcigpID09IHRvb2xzLmxvd2VyKCkKICAgICAgICAgICAgYW5kIHR5cGUocmVwb3NpdG9yeS5nZXQoJ2lkJykpIGlzIGludCBhbmQgcmVwb3NpdG9yeVsnaWQnXSA+IDAsICdSRUNPVkVSWV9SRVBPU0lUT1JZJykKICAgIGJyYW5jaCA9IHJlcG9zaXRvcnkuZ2V0KCdkZWZhdWx0X2JyYW5jaCcsICcnKQogICAgcmVxdWlyZShpc2luc3RhbmNlKGJyYW5jaCwgc3RyKSBhbmQgMSA8PSBsZW4oYnJhbmNoKSA8PSAyMDAgYW5kIHJlLmZ1bGxtYXRjaChyJ1tBLVphLXowLTkuXy8tXSsnLCBicmFuY2gpCiAgICAgICAgICAgIGFuZCAnLi4nIG5vdCBpbiBicmFuY2ggYW5kIGFsbChwIGFuZCBub3QgcC5zdGFydHN3aXRoKCcuJykgYW5kIG5vdCBwLmVuZHN3aXRoKCcubG9jaycpIGZvciBwIGluIGJyYW5jaC5zcGxpdCgnLycpKSwgJ1JFQ09WRVJZX0JSQU5DSCcpCiAgICByZWYgPSByZWNvdmVyeV9odHRwKHRva2VuLCAnL3JlcG9zLyVzL2dpdC9yZWYvaGVhZHMvJXMnICUgKHRvb2xzLCBxdW90ZShicmFuY2gsIHNhZmU9JycpKSkKICAgIHJlcXVpcmUocmVmLmdldCgnb2JqZWN0Jywge30pLmdldCgndHlwZScpID09ICdjb21taXQnLCAnUkVDT1ZFUllfQ09NTUlUJykKICAgIHRpcCA9IHJlZlsnb2JqZWN0J10uZ2V0KCdzaGEnLCAnJykKICAgIHJlcXVpcmUoaXNpbnN0YW5jZSh0aXAsIHN0cikgYW5kIHJlLmZ1bGxtYXRjaCgnW2EtZjAtOV17NDB9JywgdGlwKSwgJ1JFQ09WRVJZX0NPTU1JVCcpCiAgICBwYXRoID0gJ3JlY292ZXJ5LyVzLmJpbicgJSByaWQKICAgIGZpbGUgPSByZWNvdmVyeV9odHRwKHRva2VuLCAnL3JlcG9zLyVzL2NvbnRlbnRzLyVzP3JlZj0lcycgJSAodG9vbHMsIHBhdGgsIHRpcCkpCiAgICByZXF1aXJlKGZpbGUuZ2V0KCd0eXBlJykgPT0gJ2ZpbGUnIGFuZCBmaWxlLmdldCgnZW5jb2RpbmcnKSA9PSAnYmFzZTY0JyBhbmQgZmlsZS5nZXQoJ3BhdGgnKSA9PSBwYXRoLCAnUkVDT1ZFUllfRklMRV9UWVBFJykKICAgIGVuY29kZWQgPSBmaWxlLmdldCgnY29udGVudCcsICcnKQogICAgcmVxdWlyZShpc2luc3RhbmNlKGVuY29kZWQsIHN0cikgYW5kIGxlbihlbmNvZGVkKSA8PSAzMF8wMDAsICdSRUNPVkVSWV9TSVpFJykKICAgIGRhdGEgPSB1bmI2NChlbmNvZGVkLnJlcGxhY2UoJ1xuJywgJycpLnJlcGxhY2UoJ1xyJywgJycpKQogICAgc2hhID0gaGFzaGxpYi5zaGExKGInYmxvYiAnICsgc3RyKGxlbihkYXRhKSkuZW5jb2RlKCkgKyBiJ1wwJyArIGRhdGEpLmhleGRpZ2VzdCgpCiAgICByZXF1aXJlKHR5cGUoZmlsZS5nZXQoJ3NpemUnKSkgaXMgaW50IGFuZCBmaWxlWydzaXplJ10gPT0gbGVuKGRhdGEpIGFuZCAxIDw9IGxlbihkYXRhKSA8PSAxNl8wMDAKICAgICAgICAgICAgYW5kIGZpbGUuZ2V0KCdzaGEnKSA9PSBzaGEsICdSRUNPVkVSWV9DT05URU5UX0hBU0gnKQogICAgcmV0dXJuIHsncmVwb3NpdG9yeV9pZCc6IHJlcG9zaXRvcnlbJ2lkJ10sICdicmFuY2gnOiBicmFuY2gsICdzaGEnOiBzaGEsICdkYXRhJzogZGF0YX0KCgpkZWYgcmVzZWFsX3JlY292ZXJ5KHRva2VuLCBjLCBzKToKICAgICIiIk9uZSBjb25kaXRpb25hbCB3cml0ZSBvZiBkdXJhYmxlIGV4YWN0IGJ5dGVzLiBBIHRvbWJzdG9uZSBhbHdheXMgd2lucywgaW5jbHVkaW5nIGR1cmluZyByZWFkYmFjay4KICAgIExlZ2FjeSBjaXBoZXJ0ZXh0IHdpdGhvdXQgZXhwbGljaXQgQWdlbnQgb3duZXJzaGlwIGlzIHJlYWQtb25seTsgbWlzc2luZy9mb3JlaWduIGJsb2JzIGFyZSBuZXZlciByZXBsYWNlZC4KICAgIENhbGxlciBob2xkcyB0aGUgb3JpZ2luYWwgc2Vzc2lvbidzIGV4Y2x1c2l2ZSBsb2NrLiBObyBidXNpbmVzcyByZXF1ZXN0IG9yIGlkZW50aXR5IGlzIGNoYW5nZWQgaGVyZS4KICAgICIiIgogICAgcmVjLCB0b29scyA9IG9zLmVudmlyb24uZ2V0KCdMRUJPWF9SRUNPVkVSWV9TRUxGJywgJycpLCBvcy5lbnZpcm9uLmdldCgnTEVCT1hfVE9PTFMnLCAnJykKICAgIGNvbnRleHQgPSBzLnZhbHVlLmdldCgncmVjb3ZlcnlfY29udGV4dCcsIHt9KQogICAgaWYgbm90IHJlYzoKICAgICAgICByZWMsIHRvb2xzID0gY29udGV4dC5nZXQoJ3JlY292ZXJ5JywgJycpLCBjb250ZXh0LmdldCgndG9vbHMnLCAnJykKICAgIGVsaWYgY29udGV4dDoKICAgICAgICByZXF1aXJlKGNvbnRleHQgPT0geydyZWNvdmVyeSc6IHJlYywgJ3Rvb2xzJzogdG9vbHN9LCAnUkVDT1ZFUllfU0NPUEVfTUlTTUFUQ0gnKQogICAgaWYgbm90IHJlYyBvciBub3QgdG9vbHM6CiAgICAgICAgcmV0dXJuICdub3RfY29uZmlndXJlZCcKICAgIHJlcXVpcmUocmUuZnVsbG1hdGNoKHInW0EtWmEtejAtOV1bQS1aYS16MC05LV0qL1tBLVphLXowLTkuXy1dKycsIHRvb2xzKQogICAgICAgICAgICBhbmQgdG9vbHMuc3BsaXQoJy8nKVsxXSBub3QgaW4gKCcuJywgJy4uJyksICdSRUNPVkVSWV9UT09MUycpCiAgICByZXF1aXJlKG9zLmVudmlyb24uZ2V0KCdMRUJPWF9SRVBPJywgY1sncmVwb3NpdG9yeSddKSA9PSBjWydyZXBvc2l0b3J5J10sICdSRUNPVkVSWV9SRVBPU0lUT1JZX01JU01BVENIJykKICAgIHJlcXVpcmUoaXNpbnN0YW5jZSh0b2tlbiwgc3RyKSBhbmQgOCA8PSBsZW4odG9rZW4pIDw9IDQwOTYgYW5kIG5vdCBhbnkoeC5pc3NwYWNlKCkgZm9yIHggaW4gdG9rZW4pLCAnUkVDT1ZFUllfVE9LRU4nKQogICAgcmlkLCBrZXkgPSByZWNvdmVyeV9rZXkocmVjKQogICAgc2NvcGUgPSBkaWN0KHBpbnMoYyksIHByb2plY3RfaWQ9Y1sncHJvamVjdF9pZCddLCByZXBvc2l0b3J5PWNbJ3JlcG9zaXRvcnknXSwgdG9vbHM9dG9vbHMsIHJpZD1yaWQsCiAgICAgICAgICAgICAgICAga2V5X2lkPWhhc2hsaWIuc2hhMjU2KGInbGVib3gtcmVjb3ZlcnktaWQnICsga2V5KS5oZXhkaWdlc3QoKSkKICAgIHN0YXRlID0gcy52YWx1ZS5nZXQoJ3JlY292ZXJ5X3B1YmxpY2F0aW9uJykKICAgIGlmIHN0YXRlIGlzIG5vdCBOb25lOgogICAgICAgIHJlcXVpcmUoaXNpbnN0YW5jZShzdGF0ZSwgZGljdCkgYW5kIHN0YXRlLmdldCgnZm9ybWF0JykgPT0gJ2xlYm94LWFnZW50LXB1YmxpY2F0aW9uLXYxJyBhbmQgc3RhdGUuZ2V0KCdzY29wZScpID09IHNjb3BlLCAnUkVDT1ZFUllfU0NPUEVfTUlTTUFUQ0gnKQogICAgICAgIHJlcXVpcmUoc3RhdGUuZ2V0KCdzdGF0dXMnKSBpbiAoJ3BlbmRpbmcnLCAncHVibGlzaGVkJywgJ3Jldm9rZWQnKSwgJ1JFQ09WRVJZX1NUQVRFJykKICAgICAgICByZXF1aXJlKHN0YXRlWydzdGF0dXMnXSAhPSAncmV2b2tlZCcsICdSRUNPVkVSWV9SRVZPS0VEJykKICAgIHJlbW90ZSA9IHJlY292ZXJ5X3JlbW90ZSh0b2tlbiwgdG9vbHMsIHJpZCkKICAgIGlmIHN0YXRlIGlzIE5vbmU6CiAgICAgICAgc3RhdGUgPSB7J2Zvcm1hdCc6ICdsZWJveC1hZ2VudC1wdWJsaWNhdGlvbi12MScsICdzY29wZSc6IHNjb3BlLCAnc3RhdHVzJzogJ3B1Ymxpc2hlZCcsCiAgICAgICAgICAgICAgICAgJ3JlcG9zaXRvcnlfaWQnOiByZW1vdGVbJ3JlcG9zaXRvcnlfaWQnXSwgJ2JyYW5jaCc6IHJlbW90ZVsnYnJhbmNoJ10sICdnZW5lcmF0aW9uJzogMH0KICAgICAgICBzLnZhbHVlWydyZWNvdmVyeV9wdWJsaWNhdGlvbiddID0gc3RhdGUKICAgIHJlcXVpcmUocmVtb3RlWydyZXBvc2l0b3J5X2lkJ10gPT0gc3RhdGVbJ3JlcG9zaXRvcnlfaWQnXSBhbmQgcmVtb3RlWydicmFuY2gnXSA9PSBzdGF0ZVsnYnJhbmNoJ10sICdSRUNPVkVSWV9SRU1PVEVfU0NPUEVfQ0hBTkdFRCcpCiAgICBkZWYgZGVueV9yZXZva2VkKG9ic2VydmVkKToKICAgICAgICBpZiBvYnNlcnZlZFsnZGF0YSddLnN0YXJ0c3dpdGgoYidMRUJPWC1SRVZPS0VELVYxJyk6CiAgICAgICAgICAgIHN0YXRlWydzdGF0dXMnXSA9ICdyZXZva2VkJzsgcy5zYXZlKCkgICMgUmV0YWluIGFsbCBwZW5kaW5nIGV4YWN0IGJ5dGVzOyBwZXJtYW5lbnQgbG9jYWwgZGVuaWFsLgogICAgICAgICAgICByYWlzZSBTdG9wKCdSRUNPVkVSWV9SRVZPS0VEJykKICAgIGRlbnlfcmV2b2tlZChyZW1vdGUpCiAgICBpZiBzdGF0ZS5nZXQoJ291dGJveCcpOgogICAgICAgICMgQW4gYW1iaWd1b3VzIHByZXZpb3VzIHdyaXRlIGNhbiBPTkxZIHJlY29uY2lsZSBpdHMgb3JpZ2luYWwgYnl0ZXMgYW5kIG9yaWdpbmFsIENBUyBiYXNlbGluZS4KICAgICAgICB0YXJnZXQgPSB1bmI2NChzdGF0ZVsnb3V0Ym94J10pCiAgICAgICAgcmVxdWlyZShoYXNobGliLnNoYTI1Nih0YXJnZXQpLmhleGRpZ2VzdCgpID09IHN0YXRlLmdldCgnb3V0Ym94X3NoYTI1NicpLCAnUkVDT1ZFUllfT1VUQk9YX0NPUlJVUFQnKQogICAgZWxzZToKICAgICAgICBvbGQgPSBsb2FkX2pzb24ocmVjb3ZlcnlfY2lwaGVyKGtleSwgcmVtb3RlWydkYXRhJ10sIFRydWUpLCBsaW1pdD0xNl8wMDApCiAgICAgICAgcmVxdWlyZShpc2luc3RhbmNlKG9sZCwgZGljdCkgYW5kIG9sZC5nZXQoJ3YnKSA9PSAxIGFuZCByZWNvdmVyeV9sZWdhY3lfb3duZWQoYywgcywgb2xkLCByZW1vdGVbJ2RhdGEnXSkKICAgICAgICAgICAgICAgIGFuZCBvbGQuZ2V0KCdyZXBvJykgPT0gY1sncmVwb3NpdG9yeSddIGFuZCBvbGQuZ2V0KCd0b29scycpID09IHRvb2xzCiAgICAgICAgICAgICAgICBhbmQgKCdzY29wZScgbm90IGluIG9sZCBvciBvbGRbJ3Njb3BlJ10gPT0gc2NvcGUpLCAnUkVDT1ZFUllfT1dORVJfVU5WRVJJRklFRCcpCiAgICAgICAgaWYgb2xkLmdldCgndG9rZW4nKSA9PSB0b2tlbiBhbmQgb2xkLmdldCgnc2NvcGUnKSA9PSBzY29wZToKICAgICAgICAgICAgcmV0dXJuICdwdWJsaXNoZWQnCiAgICAgICAgcmVxdWlyZShzdGF0ZVsnZ2VuZXJhdGlvbiddIDwgMTI4LCAnUkVDT1ZFUllfR0VORVJBVElPTl9MSU1JVCcpCiAgICAgICAgcGF5bG9hZCA9IHsndic6IDEsICdyZXBvJzogY1sncmVwb3NpdG9yeSddLCAndG9vbHMnOiB0b29scywgJ3Rva2VuJzogdG9rZW4sICdyZW5ld2VkJzogaW50KHRpbWUudGltZSgpKSwgJ2J5JzogJ2FnZW50JywgJ3Njb3BlJzogc2NvcGV9CiAgICAgICAgdGFyZ2V0ID0gcmVjb3ZlcnlfY2lwaGVyKGtleSwgY2Fub25pY2FsKHBheWxvYWQpKQogICAgICAgIHN0YXRlLnVwZGF0ZShzdGF0dXM9J3BlbmRpbmcnLCBnZW5lcmF0aW9uPXN0YXRlWydnZW5lcmF0aW9uJ10gKyAxLCBvdXRib3g9YjY0KHRhcmdldCksCiAgICAgICAgICAgICAgICAgICAgIG91dGJveF9zaGEyNTY9aGFzaGxpYi5zaGEyNTYodGFyZ2V0KS5oZXhkaWdlc3QoKSwgYmFzZWxpbmVfc2hhPXJlbW90ZVsnc2hhJ10pCiAgICAgICAgcy5zYXZlKCkgICMgTm8gbmV0d29yayBtdXRhdGlvbiB1bnRpbCB0aGUgZXhhY3QgY2lwaGVydGV4dCBhbmQgYmFzZWxpbmUgcmVhY2ggZHVyYWJsZSBwcml2YXRlIHN0b3JhZ2UuCiAgICBkZWYgYWNrbm93bGVkZ2Uob2JzZXJ2ZWQpOgogICAgICAgIHJlcXVpcmUob2JzZXJ2ZWRbJ3JlcG9zaXRvcnlfaWQnXSA9PSBzdGF0ZVsncmVwb3NpdG9yeV9pZCddIGFuZCBvYnNlcnZlZFsnYnJhbmNoJ10gPT0gc3RhdGVbJ2JyYW5jaCddLCAnUkVDT1ZFUllfUkVNT1RFX1NDT1BFX0NIQU5HRUQnKQogICAgICAgIGRlbnlfcmV2b2tlZChvYnNlcnZlZCkKICAgICAgICBpZiBvYnNlcnZlZFsnZGF0YSddICE9IHRhcmdldDoKICAgICAgICAgICAgcmV0dXJuIEZhbHNlCiAgICAgICAgc3RhdGUudXBkYXRlKHN0YXR1cz0ncHVibGlzaGVkJywgcHVibGlzaGVkX3NoYT1vYnNlcnZlZFsnc2hhJ10pCiAgICAgICAgc3RhdGUucG9wKCdvdXRib3gnLCBOb25lKTsgc3RhdGUucG9wKCdiYXNlbGluZV9zaGEnLCBOb25lKTsgcy5zYXZlKCkKICAgICAgICByZXR1cm4gVHJ1ZQogICAgaWYgYWNrbm93bGVkZ2UocmVtb3RlKToKICAgICAgICByZXR1cm4gJ3B1Ymxpc2hlZCcKICAgIHJlcXVpcmUocmVtb3RlWydzaGEnXSA9PSBzdGF0ZS5nZXQoJ2Jhc2VsaW5lX3NoYScpLCAnUkVDT1ZFUllfQ09OVEVOVF9DT05GTElDVCcpCiAgICBzLnNhdmUoKSAgIyBBbHNvIGNvdmVycyBhIHByZXZpb3VzIHBlcnNpc3RlbmNlIGZhaWx1cmU7IG5ldmVyIHB1Ymxpc2ggb25seSBpbi1tZW1vcnkgaW50ZW50LgogICAgYm9keSA9IHsnbWVzc2FnZSc6ICdsZWJveDogcmVuZXcgJyArIHJpZFs6OF0sICdicmFuY2gnOiBzdGF0ZVsnYnJhbmNoJ10sICdzaGEnOiBzdGF0ZVsnYmFzZWxpbmVfc2hhJ10sICdjb250ZW50JzogYjY0KHRhcmdldCl9CiAgICB0cnk6CiAgICAgICAgcmVjb3ZlcnlfaHR0cCh0b2tlbiwgJy9yZXBvcy8lcy9jb250ZW50cy9yZWNvdmVyeS8lcy5iaW4nICUgKHRvb2xzLCByaWQpLCBib2R5KQogICAgZXhjZXB0IEV4Y2VwdGlvbjoKICAgICAgICAjIFRoZSByZXF1ZXN0IG1heSBoYXZlIGNvbW1pdHRlZC4gT25lIHJlYWQtb25seSByZWNvbmNpbGlhdGlvbjsgTk8gYmxpbmQgcmV0cnkgb3IgcmVzZWFsLgogICAgICAgIHBhc3MKICAgIG9ic2VydmVkID0gcmVjb3ZlcnlfcmVtb3RlKHRva2VuLCB0b29scywgcmlkKQogICAgaWYgYWNrbm93bGVkZ2Uob2JzZXJ2ZWQpOgogICAgICAgIHJldHVybiAncHVibGlzaGVkJwogICAgcmV0dXJuICdwZW5kaW5nJyAgIyBFdmVuIDIwMC8yMDEgaXMgbm90IGFuIGFja25vd2xlZGdlbWVudCB3aXRob3V0IGV4YWN0IHJlYWRiYWNrLgoKCmRlZiByZW1lbWJlcl9yZWNvdmVyeV9jb250ZXh0KGMsIHMpOgogICAgIyBTZWNyZXQtYmVhcmluZyBjb250ZXh0IGJlbG9uZ3Mgb25seSBpbiB0aGlzIG9yaWdpbmFsIHNlc3Npb24ncyBvd25lci1wcml2YXRlIHN0YXRlLCBuZXZlciBhbiBpbnN0YWxsIHJlY2VpcHQuCiAgICByZWMsIHRvb2xzID0gb3MuZW52aXJvbi5nZXQoJ0xFQk9YX1JFQ09WRVJZX1NFTEYnLCAnJyksIG9zLmVudmlyb24uZ2V0KCdMRUJPWF9UT09MUycsICcnKQogICAgaWYgbm90IHJlYzoKICAgICAgICByZXR1cm4KICAgIHJlY292ZXJ5X2tleShyZWMpCiAgICByZXF1aXJlKHJlLmZ1bGxtYXRjaChyJ1tBLVphLXowLTldW0EtWmEtejAtOS1dKi9bQS1aYS16MC05Ll8tXSsnLCB0b29scykKICAgICAgICAgICAgYW5kIHRvb2xzLnNwbGl0KCcvJylbMV0gbm90IGluICgnLicsICcuLicpIGFuZCBvcy5lbnZpcm9uLmdldCgnTEVCT1hfUkVQTycsIGNbJ3JlcG9zaXRvcnknXSkgPT0gY1sncmVwb3NpdG9yeSddLCAnUkVDT1ZFUllfU0NPUEVfTUlTTUFUQ0gnKQogICAgY29udGV4dCA9IHsncmVjb3ZlcnknOiByZWMsICd0b29scyc6IHRvb2xzfQogICAgcHJldmlvdXMgPSBzLnZhbHVlLmdldCgncmVjb3ZlcnlfY29udGV4dCcpCiAgICByZXF1aXJlKHByZXZpb3VzIGlzIE5vbmUgb3IgcHJldmlvdXMgPT0gY29udGV4dCwgJ1JFQ09WRVJZX1NDT1BFX01JU01BVENIJykKICAgIGlmIHByZXZpb3VzIGlzIE5vbmU6CiAgICAgICAgcy52YWx1ZVsncmVjb3ZlcnlfY29udGV4dCddID0gY29udGV4dAogICAgICAgIHMuc2F2ZSgpCgoKZGVmIHJldHJ5X3JlY292ZXJ5X3B1YmxpY2F0aW9uKGMsIHMpOgogICAgdG9rZW4gPSBzLnZhbHVlLmdldCgncmVuZXdlZF90b2tlbicpCiAgICBpZiBub3QgdG9rZW46CiAgICAgICAgcmV0dXJuCiAgICB0cnk6CiAgICAgICAgcmVzdWx0ID0gcmVzZWFsX3JlY292ZXJ5KHRva2VuLCBjLCBzKQogICAgICAgIGlmIHJlc3VsdCAhPSAnbm90X2NvbmZpZ3VyZWQnOgogICAgICAgICAgICBwcmludChqc29uLmR1bXBzKHsnc3RhdHVzJzogJ3JlY292ZXJ5XycgKyByZXN1bHR9KSwgZmlsZT1zeXMuc3RkZXJyKQogICAgZXhjZXB0IEV4Y2VwdGlvbiBhcyBlcnJvcjoKICAgICAgICAjIERvIG5vdCBpbmNsdWRlIGEgdG9rZW4tYmVhcmluZyBIVFRQIGV4Y2VwdGlvbiwgVVJMIG9yIGRlY3J5cHRlZCBwYXlsb2FkIGluIGRpYWdub3N0aWNzLgogICAgICAgIGNvZGUgPSBzdHIoZXJyb3IpIGlmIGlzaW5zdGFuY2UoZXJyb3IsIFN0b3ApIGFuZCByZS5mdWxsbWF0Y2gocidSRUNPVkVSWV9bQS1aX10rJywgc3RyKGVycm9yKSkgZWxzZSAnUkVDT1ZFUllfTk9UX0NPTkZJUk1FRCcKICAgICAgICBwcmludChqc29uLmR1bXBzKHsnc3RhdHVzJzogJ3JlY292ZXJ5X25vdF9jb25maXJtZWQnLCAnY29kZSc6IGNvZGV9KSwgZmlsZT1zeXMuc3RkZXJyKQoKZGVmIGNsb3NlZF9tYXJrZXIoYywgcywgYm94LCB0aXA9Tm9uZSk6CiAgICAjIFRoZSBkZXNrdG9wIHdyaXRlcyBzZXJ2ZXIuY2xvc2VkLmpzb24gd2hlbiBpdCBzdG9wcyBvciByZS1iaW5kcyB0aGlzIHNlc3Npb24uIFBsYWludGV4dCwgc21hbGwsIGNoZWNrZWQgb24gZXZlcnkgcG9sbCBzbyBhCiAgICAjIHN1cGVyc2VkZWQgQWdlbnQgbGVhcm5zIHdpdGhpbiBvbmUgZmV0Y2ggaW5zdGVhZCBvZiB3YWl0aW5nIG91dCBpdHMgdGltZW91dC4KICAgIHRyeToKICAgICAgICByYXcgPSBib3guYXQodGlwLCBtZXNzYWdlX3BhdGgoYywgJ2Nsb3NlZCcsICdzZXJ2ZXInKSkgaWYgdGlwIGVsc2UgYm94LnJlYWQoJ2Nsb3NlZCcsICdzZXJ2ZXInKQogICAgZXhjZXB0IEV4Y2VwdGlvbjoKICAgICAgICByZXR1cm4gTm9uZQogICAgaWYgcmF3IGlzIE5vbmU6CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIHRyeToKICAgICAgICBpbmZvID0gbG9hZF9qc29uKHJhdywgbGltaXQ9NDA5NikKICAgICAgICBpZiBpbmZvLmdldCgnZm9ybWF0JykgIT0gJ3NjeC1zZXNzaW9uLWNsb3NlZC12MScgb3IgaW5mby5nZXQoJ2JpbmRpbmcnKSAhPSBjWydiaW5kaW5nJ106CiAgICAgICAgICAgIHJldHVybiBOb25lCiAgICAgICAgcmV0dXJuIGluZm8KICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAgcmV0dXJuIE5vbmUKCmRlZiByZXBvcnRfY2xvc2VkKHMsIGluZm8sIHBlbmRpbmc9Tm9uZSk6CiAgICBzLnZhbHVlWydjbG9zZWQnXSA9IHsncmVhc29uJzogaW5mby5nZXQoJ3JlYXNvbicsICcnKSwgJ2Nsb3NlZF9hdCc6IGluZm8uZ2V0KCdjbG9zZWRfYXQnLCAwKX0KICAgIHMudmFsdWVbJ2FjdGl2ZSddID0gRmFsc2UKICAgIHMuc2F2ZSgpCiAgICBvdXQgPSB7J3N0YXR1cyc6ICdzZXNzaW9uX2Nsb3NlZF9ieV9kZXNrdG9wJywgJ3JlYXNvbic6IGluZm8uZ2V0KCdyZWFzb24nLCAnJyksCiAgICAgICAgICAgJ25leHQnOiBpbmZvLmdldCgnbmV4dCcsICdSZS1qb2luOiBydW4gIHB5dGhvbjMgLmxlYm94L2FnZW50LnB5IGpvaW4gIGluIHRoZSByZXBvc2l0b3J5LicpfQogICAgaWYgcGVuZGluZzoKICAgICAgICBvdXRbJ29wZXJhdGlvbl9pZCddID0gcGVuZGluZ1snaWQnXQogICAgICAgIG91dFsnbm90ZSddID0gJ1RoZSBwZW5kaW5nIG9wZXJhdGlvbiB3aWxsIG5ldmVyIGJlIGFuc3dlcmVkIG9uIHRoaXMgY2hhbm5lbDsgaXRzIHByb2Nlc3Npbmcgc3RhdGUgaXMgdW5rbm93biwgZG8gbm90IHJlc3VibWl0IGJsaW5kbHkuJwogICAgcHJpbnQoanNvbi5kdW1wcyhvdXQsIGVuc3VyZV9hc2NpaT1GYWxzZSwgaW5kZW50PTIpLCBmaWxlPXN5cy5zdGRlcnIpCiAgICBzeXMuZXhpdCgzKQoKZGVmIHZlcmlmeV9wZW5kaW5nX3JlcXVlc3QoYywgcGVuZGluZywgYm94LCB0aXApOgogICAgIiIiQmluZCByZXNwb25zZSBhY2NlcHRhbmNlIHRvIGV4YWN0IHB1Ymxpc2hlZCBieXRlcywgbm90IGp1c3QgYSByZXVzZWQgb3BlcmF0aW9uIElELgogICAgRmFpbHMgYmVmb3JlIGFueSByZWNvdmVyeSBwdWJsaWNhdGlvbiwgcmVuZXdhbCwgcmVzcG9uc2UgZGVjcnlwdCwgc2F2ZSBvciBwZW5kaW5nIGNsZWFyLgogICAgTmV2ZXIgcmVwYWlycy9yZXNlYWxzL3JlcGxheXMgYSByZXF1ZXN0IGFuZCBuZXZlciB0cmVhdHMgcGxhaW50ZXh0IGVxdWFsaXR5IGFzIGFkbWlzc2lvbi4KICAgICIiIgogICAgcmVxdWlyZShpc2luc3RhbmNlKHBlbmRpbmcsIGRpY3QpIGFuZCBpc2luc3RhbmNlKHBlbmRpbmcuZ2V0KCdpZCcpLCBzdHIpCiAgICAgICAgICAgIGFuZCByZS5mdWxsbWF0Y2gocidvcC1bMC05XXs2fScsIHBlbmRpbmdbJ2lkJ10pCiAgICAgICAgICAgIGFuZCBwZW5kaW5nLmdldCgnbWV0aG9kJykgaW4gKCdpbml0aWFsaXplJywgJ3Rvb2xzL2xpc3QnLCAndG9vbHMvY2FsbCcpLCAnUEVORElOR19SRVFVRVNUX0lOVkFMSUQnKQogICAgZW52ID0gcGVuZGluZy5nZXQoJ2VudmVsb3BlJykKICAgIHJlcXVpcmUoaXNpbnN0YW5jZShlbnYsIGRpY3QpIGFuZCBzZXQoZW52KSA9PSB7J2hlYWRlcicsICdub25jZScsICdjaXBoZXJ0ZXh0JywgJ3RhZyd9LCAnUEVORElOR19SRVFVRVNUX0lOVkFMSUQnKQogICAgaCA9IGVudi5nZXQoJ2hlYWRlcicpCiAgICByZXF1aXJlKGlzaW5zdGFuY2UoaCwgZGljdCkgYW5kIHNldChoKSA9PSBzZXQocGlucyhjKSkgfCB7J3JlcXVlc3RfaWQnLCAnZGlyZWN0aW9uJywgJ2V4cGlyZXMnfSwgJ1BFTkRJTkdfUkVRVUVTVF9JTlZBTElEJykKICAgIHJlcXVpcmUoYWxsKGhba10gPT0gdiBmb3IgaywgdiBpbiBwaW5zKGMpLml0ZW1zKCkpIGFuZCBoWydyZXF1ZXN0X2lkJ10gPT0gcGVuZGluZ1snaWQnXQogICAgICAgICAgICBhbmQgaFsnZGlyZWN0aW9uJ10gPT0gJ3JlcScsICdQRU5ESU5HX1JFUVVFU1RfQklORElORycpCiAgICByZXF1aXJlKHR5cGUoaFsnZXhwaXJlcyddKSBpcyBpbnQgYW5kIGhbJ2V4cGlyZXMnXSA+IDAKICAgICAgICAgICAgYW5kIGFsbChpc2luc3RhbmNlKGVudltrXSwgc3RyKSBmb3IgayBpbiAoJ25vbmNlJywgJ2NpcGhlcnRleHQnLCAndGFnJykpLCAnUEVORElOR19SRVFVRVNUX0lOVkFMSUQnKQogICAgdHJ5OgogICAgICAgIGZvciBmaWVsZCwgbGVuZ3RoIGluICgoJ25vbmNlJywgMTIpLCAoJ3RhZycsIDE2KSwgKCdjaXBoZXJ0ZXh0JywgTm9uZSkpOgogICAgICAgICAgICBkZWNvZGVkID0gdW5iNjQoZW52W2ZpZWxkXSwgbGVuZ3RoKQogICAgICAgICAgICByZXF1aXJlKGxlbihkZWNvZGVkKSA8PSA2NTUzNiBhbmQgYjY0KGRlY29kZWQpID09IGVudltmaWVsZF0sICdQRU5ESU5HX1JFUVVFU1RfSU5WQUxJRCcpCiAgICBleGNlcHQgKFZhbHVlRXJyb3IsIFR5cGVFcnJvciwgU3RvcCk6CiAgICAgICAgcmFpc2UgU3RvcCgnUEVORElOR19SRVFVRVNUX0lOVkFMSUQnKSBmcm9tIE5vbmUKICAgIGV4cGVjdGVkID0gY2Fub25pY2FsKGVudikKICAgIHJlcXVpcmUobGVuKGV4cGVjdGVkKSA8PSBMSU1JVCwgJ1BFTkRJTkdfUkVRVUVTVF9JTlZBTElEJykKICAgIGlmIHRpcCBpcyBOb25lOgogICAgICAgIHJldHVybiAgIyBTaGFwZS9iaW5kaW5nIHByZWZsaWdodCBvbmx5OyBubyB0cmFuc3BvcnQgb3Igc3RhdGUgYWNjZXNzLgogICAgcHVibGlzaGVkID0gYm94LmF0KHRpcCwgbWVzc2FnZV9wYXRoKGMsICdyZXF1ZXN0JywgcGVuZGluZ1snaWQnXSkpCiAgICByZXF1aXJlKGlzaW5zdGFuY2UocHVibGlzaGVkLCBieXRlcykgYW5kIDAgPCBsZW4ocHVibGlzaGVkKSA8PSBMSU1JVCwgJ1BFTkRJTkdfUkVRVUVTVF9VTlZFUklGSUVEJykKICAgIHJlcXVpcmUoaG1hYy5jb21wYXJlX2RpZ2VzdChoYXNobGliLnNoYTI1NihwdWJsaXNoZWQpLmRpZ2VzdCgpLCBoYXNobGliLnNoYTI1NihleHBlY3RlZCkuZGlnZXN0KCkpLAogICAgICAgICAgICAnUEVORElOR19SRVFVRVNUX0NPTkZMSUNUOiBvcmlnaW5hbCBwZW5kaW5nIHByZXNlcnZlZDsgZG8gbm90IHJlc3VibWl0IG9yIHN1YnN0aXR1dGUgYW5vdGhlciByZXF1ZXN0JykKCmRlZiBzdGF0dXMoYywgcywgYm94LCB3YWl0PTApOgogICAgcGVuZGluZyA9IHMudmFsdWUuZ2V0KCdwZW5kaW5nJykKICAgIGlmIHBlbmRpbmcgaXMgTm9uZToKICAgICAgICByZXRyeV9yZWNvdmVyeV9wdWJsaWNhdGlvbihjLCBzKQogICAgICAgIGluZm8gPSBjbG9zZWRfbWFya2VyKGMsIHMsIGJveCkKICAgICAgICBpZiBpbmZvOgogICAgICAgICAgICByZXBvcnRfY2xvc2VkKHMsIGluZm8pCiAgICAgICAgcHJpbnQoanNvbi5kdW1wcyhzLnZhbHVlLmdldCgnbGFzdF9yZXBseScsIHsnc3RhdHVzJzogJ25vX3BlbmRpbmdfcmVxdWVzdCd9KSwgZW5zdXJlX2FzY2lpPUZhbHNlLCBpbmRlbnQ9MikpCiAgICAgICAgcmV0dXJuCiAgICB2ZXJpZnlfcGVuZGluZ19yZXF1ZXN0KGMsIHBlbmRpbmcsIGJveCwgTm9uZSkKICAgIHVudGlsID0gdGltZS5tb25vdG9uaWMoKSArIHdhaXQKICAgIGF0dGVtcHQgPSAwCiAgICByZWNvdmVyeV9jaGVja2VkID0gRmFsc2UKICAgIHdoaWxlIFRydWU6CiAgICAgICAgdGlwID0gYm94LmZldGNoKCkKICAgICAgICByZXF1aXJlKGlzaW5zdGFuY2UodGlwLCBzdHIpIGFuZCBib29sKHRpcCksICdQRU5ESU5HX1JFUVVFU1RfVU5WRVJJRklFRCcpCiAgICAgICAgdmVyaWZ5X3BlbmRpbmdfcmVxdWVzdChjLCBwZW5kaW5nLCBib3gsIHRpcCkKICAgICAgICBpZiBub3QgcmVjb3ZlcnlfY2hlY2tlZDoKICAgICAgICAgICAgcmV0cnlfcmVjb3ZlcnlfcHVibGljYXRpb24oYywgcykKICAgICAgICAgICAgcmVjb3ZlcnlfY2hlY2tlZCA9IFRydWUKICAgICAgICByYXcgPSBib3guYXQodGlwLCBtZXNzYWdlX3BhdGgoYywgJ3Jlc3BvbnNlJywgcGVuZGluZ1snaWQnXSkpCiAgICAgICAgaWYgcmF3IGlzIE5vbmU6CiAgICAgICAgICAgIGFwcGx5X3Rva2VuX3JlbmV3KGMsIHMsIGJveCwgdGlwKQogICAgICAgICAgICBpbmZvID0gY2xvc2VkX21hcmtlcihjLCBzLCBib3gsIHRpcCkKICAgICAgICAgICAgaWYgaW5mbzoKICAgICAgICAgICAgICAgIHJlcG9ydF9jbG9zZWQocywgaW5mbywgcGVuZGluZykKICAgICAgICBpZiByYXcgaXMgbm90IE5vbmU6CiAgICAgICAgICAgIHJlcGx5ID0gb3Blbl9yZXNwb25zZShjLCBzLCBwZW5kaW5nWydpZCddLCBsb2FkX2pzb24ocmF3KSkKICAgICAgICAgICAgaWYgcGVuZGluZ1snbWV0aG9kJ10gPT0gJ2luaXRpYWxpemUnIGFuZCBpc2luc3RhbmNlKHJlcGx5LCBkaWN0KSBhbmQgJ3Jlc3VsdCcgaW4gcmVwbHk6CiAgICAgICAgICAgICAgICBzLnZhbHVlWydpbml0aWFsaXplZCddID0gVHJ1ZQogICAgICAgICAgICBzLnZhbHVlWydsYXN0X3JlcGx5J10gPSByZXBseQogICAgICAgICAgICBkZWwgcy52YWx1ZVsncGVuZGluZyddCiAgICAgICAgICAgIHMuc2F2ZSgpCiAgICAgICAgICAgIHByaW50KGpzb24uZHVtcHMocmVwbHksIGVuc3VyZV9hc2NpaT1GYWxzZSwgaW5kZW50PTIpKQogICAgICAgICAgICByZXR1cm4KICAgICAgICBpZiB0aW1lLm1vbm90b25pYygpID49IHVudGlsOgogICAgICAgICAgICBwcmludChqc29uLmR1bXBzKHsnc3RhdHVzJzogJ3Jlc3BvbnNlX25vdF9yZWNlaXZlZCcsICdvcGVyYXRpb25faWQnOiBwZW5kaW5nWydpZCddLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAnbmV4dCc6ICdVc2Ugc3RhdHVzIHRvIHJlY29uY2lsZSB0aGlzIG9yaWdpbmFsIG9wZXJhdGlvbi4gTm8gcmVzcG9uc2UgaGFzIGJlZW4gcmVjZWl2ZWQ7IHRoaXMgZG9lcyBub3QgcHJvdmUgcGVuZGluZyBwZXJtaXNzaW9uIGFwcHJvdmFsLiBQcm9jZXNzaW5nIHN0YXRlIGlzIHVua25vd247IGRvIG5vdCBzdWJtaXQgdGhlIG9wZXJhdGlvbiBhZ2Fpbi4nfSkpCiAgICAgICAgICAgIHJldHVybgogICAgICAgIHRpbWUuc2xlZXAobWluKHBvbGxfZGVsYXkoYXR0ZW1wdCksIG1heCgwLjAsIHVudGlsIC0gdGltZS5tb25vdG9uaWMoKSkpIG9yIDEpCiAgICAgICAgYXR0ZW1wdCArPSAxCgpkZWYgcmVxdWVzdChjLCBzLCBib3gsIG1ldGhvZCwgYXJncywgd2FpdD05MCk6CiAgICByZXF1aXJlKG5vdCBzLnZhbHVlLmdldCgncGVuZGluZycpLCAnVGhlcmUgaXMgYW4gdW5yZXNvbHZlZCByZXF1ZXN0LiBSdW4gc3RhdHVzIGluc3RlYWQgb2Ygc3VibWl0dGluZyBhbm90aGVyIG9wZXJhdGlvbi4nKQogICAgYXBwbHlfdG9rZW5fcmVuZXcoYywgcywgYm94KQogICAgaWYgcy52YWx1ZS5nZXQoJ2Nsb3NlZCcpOgogICAgICAgIHJlcG9ydF9jbG9zZWQocywgeydyZWFzb24nOiBzLnZhbHVlWydjbG9zZWQnXS5nZXQoJ3JlYXNvbicsICcnKSwgJ25leHQnOiAnVGhpcyBzZXNzaW9uIHdhcyBjbG9zZWQgYnkgdGhlIGRlc2t0b3AuIFJlLWpvaW46IHJ1biAgcHl0aG9uMyAubGVib3gvYWdlbnQucHkgam9pbiAgaW4gdGhlIHJlcG9zaXRvcnkuJ30pCiAgICByZXF1aXJlKHMudmFsdWUuZ2V0KCdhY3RpdmUnKSwgJ1BhaXJpbmcgaXMgbm90IGFjdGl2ZS4nKQogICAgcmVxdWlyZShtZXRob2QgPT0gJ2luaXRpYWxpemUnIG9yIHMudmFsdWUuZ2V0KCdpbml0aWFsaXplZCcpLCAnSW5pdGlhbGl6ZSBtdXN0IGNvbXBsZXRlIGJlZm9yZSB0b29sIGNhbGxzLicpCiAgICByZXF1aXJlKGNbJ21heF9vcGVyYXRpb25zJ10gPT0gMCBvciBzLnZhbHVlWyduZXh0J10gPCBjWydtYXhfb3BlcmF0aW9ucyddLCAnU2Vzc2lvbiBtZXNzYWdlIGxpbWl0IHJlYWNoZWQ7IHJlcXVlc3QgYSBuZXcgc2Vzc2lvbi4nKQogICAgcmVxdWlyZShzLnZhbHVlWyduZXh0J10gPCAxXzAwMF8wMDAsICdTZXNzaW9uIG1lc3NhZ2UgbGltaXQgcmVhY2hlZDsgcmVxdWVzdCBhIG5ldyBzZXNzaW9uLicpCiAgICBvcCA9IGYib3Ate3MudmFsdWVbJ25leHQnXTowNmR9IgogICAgZW52ID0gc2VhbChjLCBzLCBvcCwgY2Fub25pY2FsKHsnb3BlcmF0aW9uX2lkJzogb3AsICdtZXRob2QnOiBtZXRob2QsICdhcmd1bWVudHMnOiBhcmdzfSkpCiAgICBzLnZhbHVlWyduZXh0J10gKz0gMQogICAgcy52YWx1ZVsncGVuZGluZyddID0geydpZCc6IG9wLCAnbWV0aG9kJzogbWV0aG9kLCAnZW52ZWxvcGUnOiBlbnZ9CiAgICBzLnNhdmUoKSAgIyBQZXJzaXN0IGV4YWN0IGNpcGhlcnRleHQgQkVGT1JFIHB1Ymxpc2hpbmcgYW55dGhpbmcuCiAgICBib3guY3JlYXRlKCdyZXF1ZXN0Jywgb3AsIGNhbm9uaWNhbChlbnYpKQogICAgc3RhdHVzKGMsIHMsIGJveCwgd2FpdD13YWl0KQoKZGVmIGFjdGl2YXRlKGMsIHMsIGJveCwgYXBwcm92ZWQsIHdhaXQ9MCk6CiAgICByZXF1aXJlKCdrZXlzJyBpbiBzLnZhbHVlLCAnUnVuIGNvbm5lY3QgZmlyc3QuJykKICAgIGlmIHMudmFsdWUuZ2V0KCdwZW5kaW5nJyk6CiAgICAgICAgcmV0dXJuIHN0YXR1cyhjLCBzLCBib3gsIHdhaXQpCiAgICBpZiBzLnZhbHVlLmdldCgnaW5pdGlhbGl6ZWQnKToKICAgICAgICBwcmludCgnQWxyZWFkeSBpbml0aWFsaXplZC4gVXNlIGxpc3QsIGNhbGwgb3Igc3RhdHVzLicpOyByZXR1cm4KICAgICMgVGhlIGxvY2FsIG1hY2hpbmUgcHVibGlzaGVzIGNvbmZpcm1hdGlvbi9zZXJ2ZXIgb25seSBhZnRlciB0aGUgdXNlciAob3IgdGhlIHRydXN0ZWQtcmVwb3NpdG9yeSBzZXR0aW5nKSBjb25maXJtZWQgdGhlIHBhaXJpbmcuCiAgICB1bnRpbCA9IHRpbWUubW9ub3RvbmljKCkgKyBtYXgoMCwgd2FpdCkKICAgIGF0dGVtcHQgPSAwCiAgICB3aGlsZSBUcnVlOgogICAgICAgIHJhdyA9IGJveC5yZWFkKCdjb25maXJtYXRpb24nLCAnc2VydmVyJykKICAgICAgICBpZiByYXcgaXMgTm9uZToKICAgICAgICAgICAgaW5mbyA9IGNsb3NlZF9tYXJrZXIoYywgcywgYm94KQogICAgICAgICAgICBpZiBpbmZvOgogICAgICAgICAgICAgICAgcmVwb3J0X2Nsb3NlZChzLCBpbmZvKQogICAgICAgIGlmIHJhdyBpcyBub3QgTm9uZSBvciB0aW1lLm1vbm90b25pYygpID49IHVudGlsOgogICAgICAgICAgICBicmVhawogICAgICAgIHRpbWUuc2xlZXAobWluKHBvbGxfZGVsYXkoYXR0ZW1wdCksIG1heCgwLjAsIHVudGlsIC0gdGltZS5tb25vdG9uaWMoKSkpIG9yIDEpCiAgICAgICAgYXR0ZW1wdCArPSAxCiAgICByZXF1aXJlKHJhdyBpcyBub3QgTm9uZSwgJ1dhaXRpbmcgZm9yIGxvY2FsIGNvbmZpcm1hdGlvbi4gUnVuIGFjdGl2YXRlIC0td2FpdCA5MDAgYWdhaW47IGRvIG5vdCBzdGFydCB0b29scyB5ZXQuJykKICAgIGNvbmZpcm1hdGlvbiA9IGxvYWRfanNvbihyYXcpCiAgICBleGFjdChjb25maXJtYXRpb24sIFsncm9sZScsICdiaW5kaW5nJywgJ2Vwb2NoJywgJ3RhZyddKQogICAgcmVxdWlyZShjb25maXJtYXRpb25bJ3JvbGUnXSA9PSAnc2VydmVyJyBhbmQgY29uZmlybWF0aW9uWydiaW5kaW5nJ10gPT0gY1snYmluZGluZyddIGFuZCBjb25maXJtYXRpb25bJ2Vwb2NoJ10gPT0gY1snZXBvY2gnXSwgJ0NvbmZpcm1hdGlvbiBpZGVudGl0eSBtaXNtYXRjaC4nKQogICAga2V5cyA9IHVuYjY0KHMudmFsdWVbJ2tleXMnXSwgMTI4KQogICAgdHJhbnNjcmlwdCA9IHVuYjY0KHMudmFsdWVbJ3RyYW5zY3JpcHQnXSwgMzIpCiAgICByZXF1aXJlKGhtYWMuY29tcGFyZV9kaWdlc3QodW5iNjQoY29uZmlybWF0aW9uWyd0YWcnXSwgMzIpLCBobWFjLmRpZ2VzdChrZXlzWzk2OjEyOF0sIHRyYW5zY3JpcHQsICdzaGEyNTYnKSksICdDb25maXJtYXRpb24gZmFpbGVkLicpCiAgICBib3guY3JlYXRlKCdjb25maXJtYXRpb24nLCAnY2xpZW50JywgY2Fub25pY2FsKHsncm9sZSc6ICdjbGllbnQnLCAnYmluZGluZyc6IGNbJ2JpbmRpbmcnXSwgJ2Vwb2NoJzogY1snZXBvY2gnXSwKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgJ3RhZyc6IGI2NChobWFjLmRpZ2VzdChrZXlzWzY0Ojk2XSwgdHJhbnNjcmlwdCwgJ3NoYTI1NicpKX0pKQogICAgcy52YWx1ZVsnYWN0aXZlJ10gPSBUcnVlCiAgICBzLnNhdmUoKQogICAgcmVxdWVzdChjLCBzLCBib3gsICdpbml0aWFsaXplJywge30sIHdhaXQ9bWF4KDkwLCB3YWl0KSkKCiMgSW5kZXBlbmRlbnQgcmVzdWx0LWNvbnRyb2wgc3RhdGUuIFRoaXMgbmV2ZXIgc2F2ZXMgb3IgZWRpdHMgUHJpdmF0ZVN0YXRlLnZhbHVlLCBwZW5kaW5nIG9yIGJ1c2luZXNzIGNvdW50ZXJzLgpkZWYgX3Jlc3VsdF9jb250cm9sX2xvYWQoYywgcyk6CiAgICBwYXRoID0gcy5yb290IC8gJ3Jlc3VsdC1yZWFkLmpzb24nCiAgICBzY29wZSA9IGhhc2hsaWIuc2hhMjU2KGNhbm9uaWNhbChjKSkuaGV4ZGlnZXN0KCkKICAgIHZhbHVlID0gX3N0YXRlX2pzb24oX3N0YXRlX3JlYWQocGF0aCkpIGlmIHBhdGguZXhpc3RzKCkgZWxzZSB7J3Njb3BlJzogc2NvcGUsICduZXh0JzogMH0KICAgIHJlcXVpcmUoaXNpbnN0YW5jZSh2YWx1ZSwgZGljdCkgYW5kIHZhbHVlLmdldCgnc2NvcGUnKSA9PSBzY29wZSBhbmQgdHlwZSh2YWx1ZS5nZXQoJ25leHQnKSkgaXMgaW50CiAgICAgICAgICAgIGFuZCAwIDw9IHZhbHVlWyduZXh0J10gPD0gMTAwMDAwMCwgJ1JFU1VMVF9DT05UUk9MX1NUQVRFJykKICAgIHJldHVybiBwYXRoLCB2YWx1ZQoKCmRlZiBfcmVzdWx0X2NvbnRyb2xfc2F2ZShwYXRoLCB2YWx1ZSk6CiAgICByYXcgPSBjYW5vbmljYWwodmFsdWUpCiAgICByZXF1aXJlKGxlbihyYXcpIDw9IDQwMDAwMCwgJ1JFU1VMVF9DT05UUk9MX0xJTUlUJykKICAgIF9zdGF0ZV93cml0ZShwYXRoLCByYXcpCgoKZGVmIF9yZXN1bHRfcmVwbHkoYywgcywgb3BlcmF0aW9uLCBlbnYpOgogICAgIyBBdXRoZW50aWNhdGUgYmVmb3JlIGRlY2lkaW5nIHdoZXRoZXIgYW4gZXhwaXJlZCBDT05UUk9MIHJlY2VpcHQgY2FuIGFkdmFuY2UgdGhlIHJlYWQgY3Vyc29yLgogICAgIyBPcmRpbmFyeSBvcGVuX3Jlc3BvbnNlIGFuZCBidXNpbmVzcyBUVEwvcmVwbGF5IHJ1bGVzIHN0YXkgdW5jaGFuZ2VkLgogICAgcmVxdWlyZShyZS5mdWxsbWF0Y2gocidycmVhZC1bMC05XXs2fScsIG9wZXJhdGlvbiksICdSRVNVTFRfUVVFUllfSUQnKQogICAgZXhhY3QoZW52LCBbJ2hlYWRlcicsICdub25jZScsICdjaXBoZXJ0ZXh0JywgJ3RhZyddKQogICAgaCA9IGVudlsnaGVhZGVyJ107IGV4YWN0KGgsIGxpc3QocGlucyhjKSkgKyBbJ3JlcXVlc3RfaWQnLCAnZGlyZWN0aW9uJywgJ2V4cGlyZXMnXSkKICAgIHJlcXVpcmUoYWxsKGhba10gPT0gdiBmb3IgaywgdiBpbiBwaW5zKGMpLml0ZW1zKCkpIGFuZCBoWydyZXF1ZXN0X2lkJ10gPT0gb3BlcmF0aW9uCiAgICAgICAgICAgIGFuZCBoWydkaXJlY3Rpb24nXSA9PSAncmVzJyBhbmQgdHlwZShoWydleHBpcmVzJ10pIGlzIGludCBhbmQgaFsnZXhwaXJlcyddID4gMCwgJ1JFU1VMVF9SRVBMWV9CSU5ESU5HJykKICAgIF8sIF8sIF8sIF8sIEFFU0dDTSA9IGNyeXB0bygpCiAgICBjaXBoZXIgPSB1bmI2NChlbnZbJ2NpcGhlcnRleHQnXSk7IHJlcXVpcmUobGVuKGNpcGhlcikgPD0gNjU1MzYsICdSRVNVTFRfUkVQTFlfU0laRScpCiAgICBwbGFpbiA9IEFFU0dDTSh1bmI2NChzLnZhbHVlWydrZXlzJ10sIDEyOClbMzI6NjRdKS5kZWNyeXB0KHVuYjY0KGVudlsnbm9uY2UnXSwgMTIpLCBjaXBoZXIgKyB1bmI2NChlbnZbJ3RhZyddLCAxNiksIGNhbm9uaWNhbChoKSkKICAgIGJvZHkgPSBsb2FkX2pzb24ocGxhaW4sIGxpbWl0PTY1NTM2KTsgZXhhY3QoYm9keSwgWydwcm9qZWN0X2lkJywgJ3JlcGx5J10pCiAgICByZXF1aXJlKGJvZHlbJ3Byb2plY3RfaWQnXSA9PSBjWydwcm9qZWN0X2lkJ10sICdSRVNVTFRfUkVQTFlfUFJPSkVDVCcpCiAgICByZXR1cm4gYm9keVsncmVwbHknXSwgdGltZS50aW1lKCkgLSAxIDw9IGhbJ2V4cGlyZXMnXSA8PSB0aW1lLnRpbWUoKSArIDE4MDEKCgpkZWYgX29yaWdpbmFsX3Jlc3VsdF9kZWNvZGUoYywgcGF5bG9hZCk6CiAgICByZXF1aXJlKGxlbihwYXlsb2FkKSA8PSA2NTUzNiwgJ1JFU1VMVF9QQVlMT0FEX1NJWkUnKQogICAgaWYgcGF5bG9hZC5zdGFydHN3aXRoKGInU0NYMlpcMCcpOgogICAgICAgIGRlY29kZXIgPSB6bGliLmRlY29tcHJlc3NvYmooKQogICAgICAgIHBheWxvYWQgPSBkZWNvZGVyLmRlY29tcHJlc3MocGF5bG9hZFs2Ol0sIDEwMDAwMDEpCiAgICAgICAgcmVxdWlyZShsZW4ocGF5bG9hZCkgPD0gMTAwMDAwMCBhbmQgZGVjb2Rlci5lb2YgYW5kIG5vdCBkZWNvZGVyLnVudXNlZF9kYXRhIGFuZCBub3QgZGVjb2Rlci51bmNvbnN1bWVkX3RhaWwsCiAgICAgICAgICAgICAgICAnUkVTVUxUX0NPTVBSRVNTRURfTElNSVQnKQogICAgYm9keSA9IGxvYWRfanNvbihwYXlsb2FkLCBsaW1pdD0xMDAwMDAwKTsgZXhhY3QoYm9keSwgWydwcm9qZWN0X2lkJywgJ3JlcGx5J10pCiAgICByZXF1aXJlKGJvZHlbJ3Byb2plY3RfaWQnXSA9PSBjWydwcm9qZWN0X2lkJ10sICdSRVNVTFRfT1JJR0lOQUxfUFJPSkVDVCcpCiAgICByZXR1cm4gYm9keVsncmVwbHknXQoKCmRlZiByZXN1bHRfcmVhZChjLCBzLCBib3gsIHdhaXQ9MCk6CiAgICByZXF1aXJlKHMudmFsdWUuZ2V0KCdhY3RpdmUnKSBhbmQgcy52YWx1ZS5nZXQoJ2tleXMnKSwgJ09yaWdpbmFsIHBhaXJlZCBpZGVudGl0eSByZXF1aXJlZDsgZG8gbm90IGNyZWF0ZSBhbm90aGVyIGlkZW50aXR5LicpCiAgICBwZW5kaW5nID0gcy52YWx1ZS5nZXQoJ3BlbmRpbmcnKQogICAgcmVxdWlyZShpc2luc3RhbmNlKHBlbmRpbmcsIGRpY3QpIGFuZCByZS5mdWxsbWF0Y2gocidvcC1bMC05XXs2fScsIHBlbmRpbmcuZ2V0KCdpZCcsICcnKSksICdObyBvcmlnaW5hbCBwZW5kaW5nIG9wZXJhdGlvbiB0byByZWFkLicpCiAgICBvcmlnaW5hbCA9IGNhbm9uaWNhbChwZW5kaW5nWydlbnZlbG9wZSddKQogICAgb3JpZ2luYWxfaGFzaCA9IGhhc2hsaWIuc2hhMjU2KG9yaWdpbmFsKS5oZXhkaWdlc3QoKS51cHBlcigpCiAgICBub25jZSA9IHBlbmRpbmdbJ2VudmVsb3BlJ11bJ25vbmNlJ107IHVuYjY0KG5vbmNlLCAxMikKICAgIGggPSBwZW5kaW5nWydlbnZlbG9wZSddWydoZWFkZXInXQogICAgcmVxdWlyZShhbGwoaFtrXSA9PSB2IGZvciBrLCB2IGluIHBpbnMoYykuaXRlbXMoKSkgYW5kIGhbJ3JlcXVlc3RfaWQnXSA9PSBwZW5kaW5nWydpZCddIGFuZCBoWydkaXJlY3Rpb24nXSA9PSAncmVxJywgJ1JFU1VMVF9PUklHSU5BTF9CSU5ESU5HJykKICAgIHBhdGgsIGNvbnRyb2wgPSBfcmVzdWx0X2NvbnRyb2xfbG9hZChjLCBzKQogICAgcmVxdWlyZShjb250cm9sLmdldCgnb3JpZ2luYWxfc2hhMjU2Jywgb3JpZ2luYWxfaGFzaCkgPT0gb3JpZ2luYWxfaGFzaCwgJ1JFU1VMVF9PUklHSU5BTF9DSEFOR0VEJykKICAgIGNvbnRyb2xbJ29yaWdpbmFsX3NoYTI1NiddID0gb3JpZ2luYWxfaGFzaAogICAgZGVhZGxpbmUgPSB0aW1lLm1vbm90b25pYygpICsgbWF4KDAsIG1pbih3YWl0LCAxODAwKSkKICAgICMgQXQgbW9zdCBzaXh0ZWVuIHJldGlyZWQgY29udHJvbCBnZW5lcmF0aW9ucyBwZXIgaW52b2NhdGlvbiwgaW5kZXBlbmRlbnQgb2YgYnVzaW5lc3MgbnVtYmVyaW5nLgogICAgZm9yIF8gaW4gcmFuZ2UoMTYpOgogICAgICAgIGlmICdwZW5kaW5nJyBub3QgaW4gY29udHJvbDoKICAgICAgICAgICAgcmVxdWlyZShjb250cm9sWyduZXh0J10gPCAxMDAwMDAwLCAnUkVTVUxUX1FVRVJZX0xJTUlUJykKICAgICAgICAgICAgb2Zmc2V0ID0gbGVuKHVuYjY0KGNvbnRyb2wuZ2V0KCdkYXRhJywgJycpKSkKICAgICAgICAgICAgaWYgY29udHJvbC5nZXQoJ2NvbXBsZXRlJyk6CiAgICAgICAgICAgICAgICAjIEVhY2ggZXhwbGljaXQgbmV3IHJlYWQgbXVzdCByZS1jaGVjayBDVVJSRU5UIGF1dGhvcml6YXRpb24sIG5vdCBzZXJ2ZSBhIHN0YWxlIGNhY2hlZCByZXN1bHQuCiAgICAgICAgICAgICAgICBmb3Iga2V5IGluICgnZGF0YScsICd0b3RhbCcsICdyZXN1bHRfc2hhMjU2JywgJ2F1dGhvcml6YXRpb25fdmVyc2lvbicsICdjb21wbGV0ZScpOgogICAgICAgICAgICAgICAgICAgIGNvbnRyb2wucG9wKGtleSwgTm9uZSkKICAgICAgICAgICAgICAgIG9mZnNldCA9IDAKICAgICAgICAgICAgcSA9IGRpY3QocHJvdG9jb2w9J3NjeC1yZXN1bHQtcmVhZC12MScsIHByb2plY3RfaWQ9Y1sncHJvamVjdF9pZCddLCByZXBvc2l0b3J5PWNbJ3JlcG9zaXRvcnknXSwKICAgICAgICAgICAgICAgICAgICAgcmVwb19pZD1jWydyZXBvX2lkJ10sIGJyYW5jaD1jWydicmFuY2gnXSwgYmluZGluZz1jWydiaW5kaW5nJ10sIGVwb2NoPWNbJ2Vwb2NoJ10sCiAgICAgICAgICAgICAgICAgICAgIG9wZXJhdGlvbl9pZD1wZW5kaW5nWydpZCddLCByZXF1ZXN0X3NoYTI1Nj1vcmlnaW5hbF9oYXNoLCByZXF1ZXN0X25vbmNlPW5vbmNlLAogICAgICAgICAgICAgICAgICAgICBjaGFsbGVuZ2U9c2VjcmV0cy50b2tlbl9oZXgoMzIpLCBvZmZzZXQ9b2Zmc2V0LAogICAgICAgICAgICAgICAgICAgICBhdXRob3JpemF0aW9uX3ZlcnNpb249Y29udHJvbC5nZXQoJ2F1dGhvcml6YXRpb25fdmVyc2lvbicsICcnKSkKICAgICAgICAgICAgcmVxdWlyZShvZmZzZXQgaW4gKDAsIDMyNzY4KSwgJ1JFU1VMVF9QQUdFX09GRlNFVCcpCiAgICAgICAgICAgIG9wID0gJ3JyZWFkLSUwNmQnICUgY29udHJvbFsnbmV4dCddCiAgICAgICAgICAgICMgSXNvbGF0ZWQgbm9uY2UgYm9va2tlZXBpbmc6IHNlYWwgbmV2ZXIgbXV0YXRlcyBvciBzYXZlcyBvcmlnaW5hbCBpZGVudGl0eSBzdGF0ZSBoZXJlLgogICAgICAgICAgICBjbGFzcyBDb250cm9sSWRlbnRpdHk6IHBhc3MKICAgICAgICAgICAgZGV0YWNoZWQgPSBDb250cm9sSWRlbnRpdHkoKTsgZGV0YWNoZWQudmFsdWUgPSBkaWN0KHMudmFsdWUpCiAgICAgICAgICAgIGRldGFjaGVkLnZhbHVlWyd0eF9ub25jZXMnXSA9IGxpc3Qocy52YWx1ZS5nZXQoJ3R4X25vbmNlcycsIFtdKSkgKyBsaXN0KGNvbnRyb2wuZ2V0KCd0eF9ub25jZXMnLCBbXSkpCiAgICAgICAgICAgIGVudiA9IHNlYWwoYywgZGV0YWNoZWQsIG9wLCBjYW5vbmljYWwocSksIGxpZmV0aW1lPTEyMCkKICAgICAgICAgICAgY29udHJvbFsndHhfbm9uY2VzJ10gPSAoY29udHJvbC5nZXQoJ3R4X25vbmNlcycsIFtdKSArIFtlbnZbJ25vbmNlJ11dKVstODE5MjpdCiAgICAgICAgICAgIGNvbnRyb2xbJ3BlbmRpbmcnXSA9IHsnaWQnOiBvcCwgJ3F1ZXJ5JzogcSwgJ2VudmVsb3BlJzogZW52fQogICAgICAgICAgICBfcmVzdWx0X2NvbnRyb2xfc2F2ZShwYXRoLCBjb250cm9sKSAgIyBNdXN0IGJlIGR1cmFibGUgYmVmb3JlIGNyZWF0aW5nIGFueXRoaW5nIG9uIEdpdEh1Yi4KICAgICAgICBhY3RpdmUgPSBjb250cm9sWydwZW5kaW5nJ107IG9wID0gYWN0aXZlWydpZCddOyBxID0gYWN0aXZlWydxdWVyeSddOyBlbnYgPSBhY3RpdmVbJ2VudmVsb3BlJ10KICAgICAgICByZXF1aXJlKG9wID09ICdycmVhZC0lMDZkJyAlIGNvbnRyb2xbJ25leHQnXSBhbmQgcVsncmVxdWVzdF9zaGEyNTYnXSA9PSBvcmlnaW5hbF9oYXNoCiAgICAgICAgICAgICAgICBhbmQgcVsncmVxdWVzdF9ub25jZSddID09IG5vbmNlIGFuZCBxWydvcGVyYXRpb25faWQnXSA9PSBwZW5kaW5nWydpZCddLCAnUkVTVUxUX0NPTlRST0xfTUlTTUFUQ0gnKQogICAgICAgIHJhd19xdWVyeSA9IGNhbm9uaWNhbChlbnYpCiAgICAgICAgdGlwID0gYm94LmZldGNoKCkKICAgICAgICByYXdfcmVwbHkgPSBib3guYXQodGlwLCBtZXNzYWdlX3BhdGgoYywgJ3Jlc3VsdHJlcGx5Jywgb3ApKQogICAgICAgIGlmIHJhd19yZXBseSBpcyBub3QgTm9uZToKICAgICAgICAgICAgcmVwbHksIGZyZXNoID0gX3Jlc3VsdF9yZXBseShjLCBzLCBvcCwgbG9hZF9qc29uKHJhd19yZXBseSkpCiAgICAgICAgICAgIGV4YWN0KHJlcGx5LCBbJ3Byb3RvY29sJywgJ3F1ZXJ5JywgJ3F1ZXJ5X3NoYTI1NicsICdzdGF0dXMnLCAnYXV0aG9yaXphdGlvbl92ZXJzaW9uJywgJ3RvdGFsJywgJ3Jlc3VsdF9zaGEyNTYnLCAnY2h1bmsnXSkKICAgICAgICAgICAgcmVxdWlyZShyZXBseVsncHJvdG9jb2wnXSA9PSAnc2N4LXJlc3VsdC1yZWFkLXYxJyBhbmQgcmVwbHlbJ3F1ZXJ5J10gPT0gcQogICAgICAgICAgICAgICAgICAgIGFuZCByZXBseVsncXVlcnlfc2hhMjU2J10gPT0gaGFzaGxpYi5zaGEyNTYocmF3X3F1ZXJ5KS5oZXhkaWdlc3QoKS51cHBlcigpLCAnUkVTVUxUX1BST09GX01JU01BVENIJykKICAgICAgICAgICAgIyBBdXRoZW50aWNhdGVkIGV4cGlyZWQgcmVwbGllcyBvbmx5IHJldGlyZSBjb250cm9sIHNsb3RzOyBuZXZlciBleHBvc2UgdGhlaXIgcGF5bG9hZC4KICAgICAgICAgICAgaWYgbm90IGZyZXNoOgogICAgICAgICAgICAgICAgY29udHJvbC5wb3AoJ3BlbmRpbmcnKTsgY29udHJvbFsnbmV4dCddICs9IDEKICAgICAgICAgICAgICAgIF9yZXN1bHRfY29udHJvbF9zYXZlKHBhdGgsIGNvbnRyb2wpOyBjb250aW51ZQogICAgICAgICAgICBpZiByZXBseVsnc3RhdHVzJ10gIT0gJ2F2YWlsYWJsZSc6CiAgICAgICAgICAgICAgICByZXF1aXJlKHJlcGx5WydzdGF0dXMnXSBpbiAoJ3VuYXV0aG9yaXplZCcsICdhdXRob3JpemF0aW9uX2NoYW5nZWQnLCAncXVlcnlfZXhwaXJlZCcsICdvcmlnaW5hbF9yZXF1ZXN0X21pc3NpbmcnLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgJ25vdF9mb3VuZCcsICdjb25mbGljdCcsICd1bmtub3duJywgJ2FyY2hpdmVfaW52YWxpZCcsICdhcmNoaXZlX2xpbWl0JywgJ29mZnNldF9pbnZhbGlkJykKICAgICAgICAgICAgICAgICAgICAgICAgYW5kIHJlcGx5WydjaHVuayddID09ICcnIGFuZCByZXBseVsndG90YWwnXSA9PSAwIGFuZCByZXBseVsncmVzdWx0X3NoYTI1NiddID09ICcnCiAgICAgICAgICAgICAgICAgICAgICAgIGFuZCByZXBseVsnYXV0aG9yaXphdGlvbl92ZXJzaW9uJ10gPT0gJycsICdSRVNVTFRfTkVHQVRJVkVfUFJPT0YnKQogICAgICAgICAgICAgICAgY29udHJvbC5wb3AoJ3BlbmRpbmcnKTsgY29udHJvbFsnbmV4dCddICs9IDEKICAgICAgICAgICAgICAgIGZvciBrZXkgaW4gKCdkYXRhJywgJ3RvdGFsJywgJ3Jlc3VsdF9zaGEyNTYnLCAnYXV0aG9yaXphdGlvbl92ZXJzaW9uJywgJ2NvbXBsZXRlJyk6IGNvbnRyb2wucG9wKGtleSwgTm9uZSkKICAgICAgICAgICAgICAgIF9yZXN1bHRfY29udHJvbF9zYXZlKHBhdGgsIGNvbnRyb2wpCiAgICAgICAgICAgICAgICBhbnN3ZXIgPSB7J3N0YXR1cyc6IHJlcGx5WydzdGF0dXMnXSwgJ29wZXJhdGlvbl9pZCc6IHBlbmRpbmdbJ2lkJ10sICdwZW5kaW5nX3ByZXNlcnZlZCc6IFRydWUsICdidXNpbmVzc19yZXBsYXllZCc6IEZhbHNlfQogICAgICAgICAgICAgICAgcHJpbnQoanNvbi5kdW1wcyhhbnN3ZXIsIGVuc3VyZV9hc2NpaT1GYWxzZSkpOyByZXR1cm4gYW5zd2VyCiAgICAgICAgICAgIHJlcXVpcmUoaXNpbnN0YW5jZShyZXBseVsnYXV0aG9yaXphdGlvbl92ZXJzaW9uJ10sIHN0cikgYW5kIDAgPCBsZW4ocmVwbHlbJ2F1dGhvcml6YXRpb25fdmVyc2lvbiddKSA8PSAyNTYKICAgICAgICAgICAgICAgICAgICBhbmQgdHlwZShyZXBseVsndG90YWwnXSkgaXMgaW50IGFuZCAwIDw9IHJlcGx5Wyd0b3RhbCddIDw9IDY1NTM2CiAgICAgICAgICAgICAgICAgICAgYW5kIGlzaW5zdGFuY2UocmVwbHlbJ3Jlc3VsdF9zaGEyNTYnXSwgc3RyKSBhbmQgcmUuZnVsbG1hdGNoKHInW0EtRjAtOV17NjR9JywgcmVwbHlbJ3Jlc3VsdF9zaGEyNTYnXSksICdSRVNVTFRfUEFHRV9NRVRBREFUQScpCiAgICAgICAgICAgIGRhdGEgPSB1bmI2NChjb250cm9sLmdldCgnZGF0YScsICcnKSk7IGNodW5rID0gdW5iNjQocmVwbHlbJ2NodW5rJ10pCiAgICAgICAgICAgIHJlcXVpcmUocVsnb2Zmc2V0J10gPT0gbGVuKGRhdGEpIGFuZCBsZW4oZGF0YSkgPD0gcmVwbHlbJ3RvdGFsJ10KICAgICAgICAgICAgICAgICAgICBhbmQgbGVuKGNodW5rKSA9PSBtaW4oMzI3NjgsIHJlcGx5Wyd0b3RhbCddIC0gbGVuKGRhdGEpKSwgJ1JFU1VMVF9QQUdFX0xFTkdUSCcpCiAgICAgICAgICAgIGZvciBrZXkgaW4gKCd0b3RhbCcsICdyZXN1bHRfc2hhMjU2JywgJ2F1dGhvcml6YXRpb25fdmVyc2lvbicpOgogICAgICAgICAgICAgICAgcmVxdWlyZShrZXkgbm90IGluIGNvbnRyb2wgb3IgY29udHJvbFtrZXldID09IHJlcGx5W2tleV0sICdSRVNVTFRfUEFHRV9DT05GTElDVCcpCiAgICAgICAgICAgICAgICBjb250cm9sW2tleV0gPSByZXBseVtrZXldCiAgICAgICAgICAgIGRhdGEgKz0gY2h1bmsKICAgICAgICAgICAgY29udHJvbFsnZGF0YSddID0gYjY0KGRhdGEpCiAgICAgICAgICAgIGNvbnRyb2wucG9wKCdwZW5kaW5nJyk7IGNvbnRyb2xbJ25leHQnXSArPSAxCiAgICAgICAgICAgIGlmIGxlbihkYXRhKSA9PSByZXBseVsndG90YWwnXToKICAgICAgICAgICAgICAgIHJlcXVpcmUoaGFzaGxpYi5zaGEyNTYoZGF0YSkuaGV4ZGlnZXN0KCkudXBwZXIoKSA9PSByZXBseVsncmVzdWx0X3NoYTI1NiddLCAnUkVTVUxUX1RPVEFMX0hBU0gnKQogICAgICAgICAgICAgICAgcmVzdWx0ID0gX29yaWdpbmFsX3Jlc3VsdF9kZWNvZGUoYywgZGF0YSkKICAgICAgICAgICAgICAgIGNvbnRyb2xbJ2NvbXBsZXRlJ10gPSBUcnVlCiAgICAgICAgICAgICAgICBfcmVzdWx0X2NvbnRyb2xfc2F2ZShwYXRoLCBjb250cm9sKQogICAgICAgICAgICAgICAgYW5zd2VyID0geydzdGF0dXMnOiAnb3JpZ2luYWxfcmVzdWx0X3JlYWQnLCAnb3BlcmF0aW9uX2lkJzogcGVuZGluZ1snaWQnXSwgJ3JlcGx5JzogcmVzdWx0LAogICAgICAgICAgICAgICAgICAgICAgICAgICdwZW5kaW5nX3ByZXNlcnZlZCc6IFRydWUsICdidXNpbmVzc19yZXBsYXllZCc6IEZhbHNlfQogICAgICAgICAgICAgICAgcHJpbnQoanNvbi5kdW1wcyhhbnN3ZXIsIGVuc3VyZV9hc2NpaT1GYWxzZSwgaW5kZW50PTIpKTsgcmV0dXJuIGFuc3dlcgogICAgICAgICAgICBfcmVzdWx0X2NvbnRyb2xfc2F2ZShwYXRoLCBjb250cm9sKQogICAgICAgICAgICBjb250aW51ZQogICAgICAgIGlmIHRpbWUudGltZSgpID4gZW52WydoZWFkZXInXVsnZXhwaXJlcyddOgogICAgICAgICAgICAjIFB1Ymxpc2ggYW4gdW5zZW50IGV4cGlyZWQgcXVlcnkgRVhBQ1RMWSBzbyB0aGUgaG9zdCBjYW4gYXV0aGVudGljYXRlIGFuZCByZXRpcmUgdGhlIHNsb3QuCiAgICAgICAgICAgICMgTmV2ZXIgbGVhdmUgYSBzZXF1ZW50aWFsIGhvbGUsIGFuZCBuZXZlciByZS1zZWFsIHRoaXMgSUQuCiAgICAgICAgICAgIG9jY3VwaWVkID0gYm94LmF0KHRpcCwgbWVzc2FnZV9wYXRoKGMsICdyZXN1bHRxdWVyeScsIG9wKSkKICAgICAgICAgICAgcmVxdWlyZShvY2N1cGllZCBpcyBOb25lIG9yIG9jY3VwaWVkID09IHJhd19xdWVyeSwgJ1JFU1VMVF9RVUVSWV9PQ0NVUElFRCcpCiAgICAgICAgICAgIGlmIG9jY3VwaWVkIGlzIE5vbmU6IGJveC5jcmVhdGUoJ3Jlc3VsdHF1ZXJ5Jywgb3AsIHJhd19xdWVyeSkKICAgICAgICAgICAgIyBBbiBhdXRoZW50aWNhdGVkIG9yaWdpbmFsIHF1ZXJ5IHBlcnNpc3RlZCBieSB1cyBjYW4gYmUgYWJhbmRvbmVkIE9OTFkgaW4gdGhpcyBjb250cm9sIGxhbmUuCiAgICAgICAgICAgIGNvbnRyb2wucG9wKCdwZW5kaW5nJyk7IGNvbnRyb2xbJ25leHQnXSArPSAxCiAgICAgICAgICAgIF9yZXN1bHRfY29udHJvbF9zYXZlKHBhdGgsIGNvbnRyb2wpOyBjb250aW51ZQogICAgICAgIG9jY3VwaWVkID0gYm94LmF0KHRpcCwgbWVzc2FnZV9wYXRoKGMsICdyZXN1bHRxdWVyeScsIG9wKSkKICAgICAgICByZXF1aXJlKG9jY3VwaWVkIGlzIE5vbmUgb3Igb2NjdXBpZWQgPT0gcmF3X3F1ZXJ5LCAnUkVTVUxUX1FVRVJZX09DQ1VQSUVEJykKICAgICAgICBpZiBvY2N1cGllZCBpcyBOb25lOiBib3guY3JlYXRlKCdyZXN1bHRxdWVyeScsIG9wLCByYXdfcXVlcnkpCiAgICAgICAgaWYgdGltZS5tb25vdG9uaWMoKSA+PSBkZWFkbGluZToKICAgICAgICAgICAgYW5zd2VyID0geydzdGF0dXMnOiAncmVzdWx0X3JlYWRfcGVuZGluZycsICdvcGVyYXRpb25faWQnOiBwZW5kaW5nWydpZCddLCAncGVuZGluZ19wcmVzZXJ2ZWQnOiBUcnVlLCAnYnVzaW5lc3NfcmVwbGF5ZWQnOiBGYWxzZX0KICAgICAgICAgICAgcHJpbnQoanNvbi5kdW1wcyhhbnN3ZXIsIGVuc3VyZV9hc2NpaT1GYWxzZSkpOyByZXR1cm4gYW5zd2VyCiAgICAgICAgdGltZS5zbGVlcChtaW4oMiwgbWF4KDAsIGRlYWRsaW5lIC0gdGltZS5tb25vdG9uaWMoKSkpKQogICAgYW5zd2VyID0geydzdGF0dXMnOiAncmVzdWx0X3JlYWRfcGVuZGluZycsICdvcGVyYXRpb25faWQnOiBwZW5kaW5nWydpZCddLCAncGVuZGluZ19wcmVzZXJ2ZWQnOiBUcnVlLCAnYnVzaW5lc3NfcmVwbGF5ZWQnOiBGYWxzZX0KICAgIHByaW50KGpzb24uZHVtcHMoYW5zd2VyLCBlbnN1cmVfYXNjaWk9RmFsc2UpKTsgcmV0dXJuIGFuc3dlcgoKCmRlZiBtYWluKCk6CiAgICBwYXJzZXIgPSBhcmdwYXJzZS5Bcmd1bWVudFBhcnNlcihkZXNjcmlwdGlvbj0nVXNlIGFuIGV4cGxpY2l0bHkgYXV0aG9yaXplZCBTaHVuQ29kZXggY29sbGFib3JhdGlvbiBzZXNzaW9uLicpCiAgICBwYXJzZXIuYWRkX2FyZ3VtZW50KCctLWNvbmZpZycsIHR5cGU9UGF0aCwgZGVmYXVsdD1QYXRoKF9fZmlsZV9fKS5yZXNvbHZlKCkud2l0aF9uYW1lKCdjb25uZWN0aW9uLmpzb24nKSkKICAgIHBhcnNlci5hZGRfYXJndW1lbnQoJy0tcmVwbycsIHR5cGU9UGF0aCwgZGVmYXVsdD1QYXRoLmN3ZCgpKQogICAgc3ViID0gcGFyc2VyLmFkZF9zdWJwYXJzZXJzKGRlc3Q9J2NvbW1hbmQnLCByZXF1aXJlZD1UcnVlKQogICAgc3ViLmFkZF9wYXJzZXIoJ2Nvbm5lY3QnKTsgc3ViLmFkZF9wYXJzZXIoJ2xpc3QnKQogICAgc3RhdHVzX3BhcnNlciA9IHN1Yi5hZGRfcGFyc2VyKCdzdGF0dXMnKTsgc3RhdHVzX3BhcnNlci5hZGRfYXJndW1lbnQoJy0td2FpdCcsIHR5cGU9aW50LCBkZWZhdWx0PTAsIGhlbHA9J3NlY29uZHMgdG8ga2VlcCBwb2xsaW5nIGZvciB0aGUgcGVuZGluZyByZXNwb25zZScpCiAgICByZXN1bHRfcGFyc2VyID0gc3ViLmFkZF9wYXJzZXIoJ3Jlc3VsdC1yZWFkJyk7IHJlc3VsdF9wYXJzZXIuYWRkX2FyZ3VtZW50KCctLXdhaXQnLCB0eXBlPWludCwgZGVmYXVsdD0wKQogICAgYWN0aXZhdGVfcGFyc2VyID0gc3ViLmFkZF9wYXJzZXIoJ2FjdGl2YXRlJyk7IGFjdGl2YXRlX3BhcnNlci5hZGRfYXJndW1lbnQoJy0tYXBwcm92ZWQnLCBhY3Rpb249J3N0b3JlX3RydWUnKQogICAgYWN0aXZhdGVfcGFyc2VyLmFkZF9hcmd1bWVudCgnLS13YWl0JywgdHlwZT1pbnQsIGRlZmF1bHQ9MCwgaGVscD0nc2Vjb25kcyB0byBwb2xsIGZvciB0aGUgbG9jYWwgY29uZmlybWF0aW9uIGJlZm9yZSBnaXZpbmcgdXAnKQogICAgY2FsbF9wYXJzZXIgPSBzdWIuYWRkX3BhcnNlcignY2FsbCcpOyBjYWxsX3BhcnNlci5hZGRfYXJndW1lbnQoJ25hbWUnKTsgY2FsbF9wYXJzZXIuYWRkX2FyZ3VtZW50KCctLWFyZ3VtZW50cycsIGRlZmF1bHQ9J3t9JykKICAgIGNhbGxfcGFyc2VyLmFkZF9hcmd1bWVudCgnLS13YWl0JywgdHlwZT1pbnQsIGRlZmF1bHQ9OTAsIGhlbHA9J3NlY29uZHMgdG8gd2FpdCBmb3IgdGhlIHJlc3BvbnNlIGJlZm9yZSByZXR1cm5pbmcgcmVzcG9uc2Vfbm90X3JlY2VpdmVkJykKICAgIG9wdHMgPSBwYXJzZXIucGFyc2VfYXJncygpCiAgICBjID0gbG9hZF9qc29uKG9wdHMuY29uZmlnLnJlYWRfYnl0ZXMoKSk7IHZhbGlkYXRlX2NvbmZpZyhjKQogICAgIyBUcmFuc3BvcnQ6IGdpdCB3aGVuIGF2YWlsYWJsZSAoQXJlbmEtc3R5bGUgc2FuZGJveGVzKTsgb3RoZXJ3aXNlIHRoZSBHaXRIdWIgUkVTVCBBUEkgd2l0aCBhIHRva2VuIGZyb20gdGhlIGVudmlyb25tZW50LgogICAgdHJhbnNwb3J0ID0gb3MuZW52aXJvbi5nZXQoJ0xFQk9YX1RSQU5TUE9SVCcsIG9zLmVudmlyb24uZ2V0KCdTQ1hfVFJBTlNQT1JUJywgJycpKS5zdHJpcCgpLmxvd2VyKCkKICAgIHRva2VuX2VudiA9IG9zLmVudmlyb24uZ2V0KCdMRUJPWF9UT0tFTl9FTlYnLCBvcy5lbnZpcm9uLmdldCgnU0NYX1RPS0VOX0VOVicsICdHSVRIVUJfVE9LRU4nKSkKICAgIGhhdmVfZ2l0ID0gc2h1dGlsLndoaWNoKCdnaXQnKSBpcyBub3QgTm9uZQogICAgdXNlX2FwaSA9IHRyYW5zcG9ydCA9PSAnYXBpJyBvciAobm90IGhhdmVfZ2l0IGFuZCB0cmFuc3BvcnQgIT0gJ2dpdCcpCiAgICBpZiB1c2VfYXBpOgogICAgICAgIHRva2VuID0gb3MuZW52aXJvbi5nZXQodG9rZW5fZW52LCAnJykKICAgICAgICByZXBvID0gUGF0aChvcHRzLnJlcG8pLnJlc29sdmUoKQogICAgZWxzZToKICAgICAgICAjIEVzdGFibGlzaCB3b3JrdHJlZSBib3VuZGFyeSB3aXRob3V0IHJ1bm5pbmcgaG9va3Mgb3IgY2hlY2tvdXQgZmlsdGVycy4KICAgICAgICByZXN1bHQgPSBzdWJwcm9jZXNzLnJ1bihbJ2dpdCcsICctYycsICdjb3JlLmZzbW9uaXRvcj1mYWxzZScsICctQycsIHN0cihvcHRzLnJlcG8pLCAncmV2LXBhcnNlJywgJy0tc2hvdy10b3BsZXZlbCddLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIHN0ZG91dD1zdWJwcm9jZXNzLlBJUEUsIHN0ZGVycj1zdWJwcm9jZXNzLlBJUEUsIHRpbWVvdXQ9MTAsIGNoZWNrPUZhbHNlKQogICAgICAgIHJlcXVpcmUocmVzdWx0LnJldHVybmNvZGUgPT0gMCwgJ1J1biBmcm9tIHRoZSBhdXRob3JpemVkIEdpdCByZXBvc2l0b3J5LicpCiAgICAgICAgcmVwbyA9IFBhdGgocmVzdWx0LnN0ZG91dC5kZWNvZGUoKS5zdHJpcCgpKS5yZXNvbHZlKCkKICAgIGJvdW5kYXJ5ID0gc3RhdGVfcmVwb19ib3VuZGFyeShyZXBvKSBpZiB1c2VfYXBpIGVsc2UgcmVwbwogICAgdHJ5OgogICAgICAgIHJvb3QgPSBwZXJzaXN0ZW50X3Nlc3Npb25fZGlyZWN0b3J5KGMsIGJvdW5kYXJ5KQogICAgZXhjZXB0IChTdGF0ZUxvY2F0aW9uRXJyb3IsIE9TRXJyb3IpIGFzIGVycm9yOgogICAgICAgIHJhaXNlIFN0b3AoJ1ByaXZhdGUgc3RhdGUgcmVjb3ZlcnkgdW5hdmFpbGFibGU6ICcgKyAoc3RyKGVycm9yKSBpZiBpc2luc3RhbmNlKGVycm9yLCBTdGF0ZUxvY2F0aW9uRXJyb3IpIGVsc2UgdHlwZShlcnJvcikuX19uYW1lX18pKQogICAgaWYgb3B0cy5jb21tYW5kIG5vdCBpbiAoJ2Nvbm5lY3QnLCAnYWN0aXZhdGUnKToKICAgICAgICByZXF1aXJlKChyb290IC8gJ3N0YXRlLmpzb24nKS5leGlzdHMoKSwgJ09yaWdpbmFsIHByaXZhdGUgc3RhdGUgaXMgbWlzc2luZzsgcHJlc2VydmUgcGVuZGluZyBldmlkZW5jZSBhbmQgcmVjb3ZlciB0aGUgb3JpZ2luYWwgaWRlbnRpdHksIGRvIG5vdCBjcmVhdGUgYSBuZXcgc2Vzc2lvbi4nKQogICAgIyBDb25zdHJ1Y3Qvc2F2ZSBzdGF0ZSBvbmx5IGFmdGVyIGhvbGRpbmcgdGhlIHBlci1zZXNzaW9uIGV4Y2x1c2l2ZSBsb2NrLgogICAgcmVxdWlyZShub3Qgcm9vdC5pc19zeW1saW5rKCkgYW5kIChib3VuZGFyeSBpcyBOb25lIG9yIG5vdCByb290LnJlc29sdmUoKS5pc19yZWxhdGl2ZV90byhib3VuZGFyeSkpLCAnUHJpdmF0ZSBzdGF0ZSBsb2NhdGlvbiBpcyB1bnNhZmUuJykKICAgIHJvb3QubWtkaXIobW9kZT0wbzcwMCwgZXhpc3Rfb2s9VHJ1ZSk7IG9zLmNobW9kKHJvb3QsIDBvNzAwKQogICAgd2l0aCBleGNsdXNpdmUocm9vdCk6CiAgICAgICAgcyA9IFByaXZhdGVTdGF0ZShyb290LCBjLCBib3VuZGFyeSkKICAgICAgICBpZiBvcHRzLmNvbW1hbmQgIT0gJ3Jlc3VsdC1yZWFkJzogcmVtZW1iZXJfcmVjb3ZlcnlfY29udGV4dChjLCBzKQogICAgICAgIGlmIHMudmFsdWUuZ2V0KCdyZW5ld2VkX3Rva2VuJyk6CiAgICAgICAgICAgIHRva2VuID0gcy52YWx1ZVsncmVuZXdlZF90b2tlbiddCiAgICAgICAgICAgIHJlcXVpcmUoaXNpbnN0YW5jZSh0b2tlbiwgc3RyKSBhbmQgOCA8PSBsZW4odG9rZW4pIDw9IDQwOTYgYW5kIG5vdCBhbnkoeC5pc3NwYWNlKCkgZm9yIHggaW4gdG9rZW4pLCAnSW52YWxpZCBzYXZlZCByZW5ld2FsIHRva2VuLicpCiAgICAgICAgICAgIG9zLmVudmlyb25bdG9rZW5fZW52XSA9IHRva2VuCiAgICAgICAgICAgIG9zLmVudmlyb25bJ0dJVEhVQl9UT0tFTiddID0gdG9rZW4KICAgICAgICAgICAgb3MuZW52aXJvblsnTEVCT1hfVE9LRU4nXSA9IHRva2VuCiAgICAgICAgaWYgdXNlX2FwaToKICAgICAgICAgICAgcmVxdWlyZSh0b2tlbiwgJ09yaWdpbmFsIGFjY2VzcyBjcmVkZW50aWFsIHVuYXZhaWxhYmxlOyBwcmVzZXJ2ZSBvcmlnaW5hbCBpZGVudGl0eSBhbmQgcGVuZGluZyBvcGVyYXRpb24uJykKICAgICAgICBib3ggPSBBcGlNYWlsYm94KGMsIHRva2VuKSBpZiB1c2VfYXBpIGVsc2UgR2l0TWFpbGJveChyZXBvLCBjLCBzLnJvb3QpCiAgICAgICAgaWYgb3B0cy5jb21tYW5kID09ICdjb25uZWN0JzogY29ubmVjdChjLCBzLCBib3gpCiAgICAgICAgZWxpZiBvcHRzLmNvbW1hbmQgPT0gJ2FjdGl2YXRlJzogYWN0aXZhdGUoYywgcywgYm94LCBvcHRzLmFwcHJvdmVkLCBvcHRzLndhaXQpCiAgICAgICAgZWxpZiBvcHRzLmNvbW1hbmQgPT0gJ3N0YXR1cyc6IHN0YXR1cyhjLCBzLCBib3gsIHdhaXQ9bWF4KDAsIG9wdHMud2FpdCkpCiAgICAgICAgZWxpZiBvcHRzLmNvbW1hbmQgPT0gJ3Jlc3VsdC1yZWFkJzogcmVzdWx0X3JlYWQoYywgcywgYm94LCB3YWl0PW1heCgwLCBvcHRzLndhaXQpKQogICAgICAgIGVsaWYgb3B0cy5jb21tYW5kID09ICdsaXN0JzogcmVxdWVzdChjLCBzLCBib3gsICd0b29scy9saXN0Jywge30pCiAgICAgICAgZWxzZToKICAgICAgICAgICAgcmVxdWlyZShyZS5mdWxsbWF0Y2gocidbQS1aYS16XVtBLVphLXowLTlfXXswLDk5fScsIG9wdHMubmFtZSksICdJbnZhbGlkIHRvb2wgbmFtZS4nKQogICAgICAgICAgICBhcmdzID0gbG9hZF9qc29uKG9wdHMuYXJndW1lbnRzLmVuY29kZSgpKQogICAgICAgICAgICByZXF1aXJlKGlzaW5zdGFuY2UoYXJncywgZGljdCksICdUb29sIGFyZ3VtZW50cyBtdXN0IGJlIGFuIG9iamVjdC4nKQogICAgICAgICAgICByZXF1ZXN0KGMsIHMsIGJveCwgJ3Rvb2xzL2NhbGwnLCB7J25hbWUnOiBvcHRzLm5hbWUsICdhcmd1bWVudHMnOiBhcmdzfSwgd2FpdD1tYXgoMCwgb3B0cy53YWl0KSkKCmlmIF9fbmFtZV9fID09ICdfX21haW5fXyc6CiAgICB0cnk6CiAgICAgICAgbWFpbigpCiAgICBleGNlcHQgKFN0b3AsIFN0YXRlTG9jYXRpb25FcnJvcikgYXMgZXg6CiAgICAgICAgcHJpbnQoanNvbi5kdW1wcyh7J3N0YXR1cyc6ICdzdG9wcGVkJywgJ21lc3NhZ2UnOiBzdHIoZXgpfSwgZW5zdXJlX2FzY2lpPUZhbHNlKSwgZmlsZT1zeXMuc3RkZXJyKTsgc3lzLmV4aXQoMikKICAgIGV4Y2VwdCAoS2V5Ym9hcmRJbnRlcnJ1cHQsIEV4Y2VwdGlvbik6CiAgICAgICAgIyBOZXZlciBwcmludCByYXcgR2l0IHN0ZGVyciwgY3JlZGVudGlhbC1iZWFyaW5nIFVSTHMsIHByaXZhdGUga2V5IHN0YXRlIG9yIHRyYWNlYmFjayBsb2NhbHMuCiAgICAgICAgcHJpbnQoanNvbi5kdW1wcyh7J3N0YXR1cyc6ICdub3RfY29uZmlybWVkJywgJ21lc3NhZ2UnOiAnVGhlIG9wZXJhdGlvbiB3YXMgaW50ZXJydXB0ZWQgb3IgcmVqZWN0ZWQuIEtlZXAgcHJpdmF0ZSBzdGF0ZSBhbmQgaW5zcGVjdCBzdGF0dXM7IGRvIG5vdCByZXBlYXQgYSBwb3NzaWJseSBkaXNwYXRjaGVkIG9wZXJhdGlvbi4nfSksIGZpbGU9c3lzLnN0ZGVycikKICAgICAgICBzeXMuZXhpdCgzKQo='
EMBEDDED_CLIENT_SHA256 = 'ab46dd18ebdf457282233ef0764af36ac4c9956540005b4d38cf3eb7996fe1da'


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
