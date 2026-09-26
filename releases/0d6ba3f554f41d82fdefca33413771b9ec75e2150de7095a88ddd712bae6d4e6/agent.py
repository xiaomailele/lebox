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
        # GitHub's git endpoints accept the token as HTTP Basic (x-access-token:<token>), not as a Bearer header.
        import base64 as _b64
        basic = _b64.b64encode(('x-access-token:' + bearer).encode()).decode()
        cmd += ['-c', 'http.extraheader=Authorization: Basic ' + basic]
    cmd += ['ls-remote', '--heads', url]
    return subprocess.run(cmd, capture_output=True, text=True, env=env).returncode == 0


def check_token_scope(token, repo):
    import urllib.request as _u
    import urllib.error as _e
    tools = os.environ.get('LEBOX_TOOLS') or (repo.split('/')[0] + '/lebox')
    def request(path):
        req = _u.Request('https://api.github.com' + path, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'ShunCodex/1.0'})
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
    print('  若沙盒被回收：使用同一项目入口与原私有状态恢复。安全凭据通过平台设置提供，切勿把恢复钥匙或 Token 放入聊天。', flush=True)


# Filled only by the desktop's immutable-release builder. A raw source checkout fails closed.
EMBEDDED_CLIENT_B64 = 'IyEvdXNyL2Jpbi9lbnYgcHl0aG9uMwoiIiJQcm9qZWN0IGNvbGxhYm9yYXRpb24gY2xpZW50LiBVc2VzIGV4aXN0aW5nLCBleHBsaWNpdGx5IGF1dGhvcml6ZWQgR2l0IGFjY2VzcyBvbmx5LgpQcml2YXRlIHN0YXRlIHN0YXlzIG91dHNpZGUgdGhlIHJlcG9zaXRvcnkuIE5ldmVyIHN3aXRjaGVzIGJyYW5jaGVzLCBvdmVyd3JpdGVzIGEgZmlsZSwKZm9yY2VzIGEgcHVzaCwgY3JlYXRlcyBhIFBSLCBpbmhlcml0cyBhbm90aGVyIGNoYXQgaWRlbnRpdHkgb3IgYXBwcm92ZXMgcHJvamVjdCBhY2Nlc3MuCiIiIgppbXBvcnQgYXJncGFyc2UKaW1wb3J0IGJhc2U2NAppbXBvcnQgY29udGV4dGxpYgppbXBvcnQgaGFzaGxpYgppbXBvcnQgaG1hYwppbXBvcnQganNvbgppbXBvcnQgb3MKZnJvbSBwYXRobGliIGltcG9ydCBQYXRoCmltcG9ydCByZQppbXBvcnQgc2VjcmV0cwppbXBvcnQgc2h1dGlsCmltcG9ydCBzdWJwcm9jZXNzCmltcG9ydCBzeXMKaW1wb3J0IHRlbXBmaWxlCmltcG9ydCB0aW1lCmltcG9ydCB6bGliCmZyb20gdXJsbGliLnBhcnNlIGltcG9ydCB1cmxzcGxpdCwgcXVvdGUKaW1wb3J0IHVybGxpYi5yZXF1ZXN0CmltcG9ydCB1cmxsaWIuZXJyb3IKCiMgQkVHSU4gTEVCT1ggUEVSU0lTVEVOVCBTVEFURSBMQVlPVVQgVjEKIyBTZWxmLWNvbnRhaW5lZCwgaWRlbnRpY2FsIGluIGJvdGggdHJ1c3RlZCBlbnRyeSBwb2ludHM7IG5vIHVudmVyaWZpZWQgcnVudGltZSBpbXBvcnQuCmltcG9ydCBjb250ZXh0bGliIGFzIF9zdGF0ZV9jb250ZXh0bGliCmltcG9ydCBwYXRobGliIGFzIF9zdGF0ZV9wYXRobGliCgpjbGFzcyBTdGF0ZUxvY2F0aW9uRXJyb3IoVmFsdWVFcnJvcik6CiAgICBwYXNzCgpfU1RBVEVfRVhDTFVERUQgPSBmcm96ZW5zZXQoKCcuZ2l0JywgJy5naXQtY3JlZGVudGlhbHMnLCAnLm5ldHJjJywgJy5hcmVuYScsICcuY2FjaGUnLCAnLmxvY2FsJywgJy52ZW52JywgJy5ucG0nLCAnLm5leHQnLAogICAgJy5udXh0JywgJy5vdXRwdXQnLCAnLnBhcmNlbC1jYWNoZScsICcucHl0ZXN0X2NhY2hlJywgJy5ydWZmX2NhY2hlJywgJy5zdmVsdGUta2l0JywKICAgICcudG94JywgJy50dXJibycsICcudml0ZScsICcubXlweV9jYWNoZScsICcubm94JywgJ19fcHljYWNoZV9fJywgJ25vZGVfbW9kdWxlcycsCiAgICAnYnVpbGQnLCAnY292ZXJhZ2UnLCAnZGlzdCcsICdvdXQnLCAndGFyZ2V0JykpCgpkZWYgX3N0YXRlX25vX2xpbmtzKHBhdGgpOgogICAgcGF0aCA9IF9zdGF0ZV9wYXRobGliLlBhdGgob3MucGF0aC5hYnNwYXRoKHN0cihwYXRoKSkpCiAgICBmb3IgaXRlbSBpbiAocGF0aCwgKnBhdGgucGFyZW50cyk6CiAgICAgICAgaWYgaXRlbS5pc19zeW1saW5rKCk6CiAgICAgICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignU1RBVEVfUEFUSF9MSU5LOiBwcmVzZXJ2ZSBvcmlnaW5hbCBzdGF0ZTsgZG8gbm90IGZvbGxvdyBsaW5rcycpCiAgICByZXR1cm4gcGF0aAoKZGVmIHN0YXRlX3JlcG9fYm91bmRhcnkocGF0aCk6CiAgICBpZiBwYXRoIGlzIE5vbmU6CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIHBhdGggPSBfc3RhdGVfbm9fbGlua3MocGF0aCkKICAgIGZvciBpdGVtIGluIChwYXRoLCAqcGF0aC5wYXJlbnRzKToKICAgICAgICBpZiAoaXRlbSAvICcuZ2l0JykuZXhpc3RzKCk6CiAgICAgICAgICAgIHJldHVybiBpdGVtLnJlc29sdmUoKQogICAgcmV0dXJuIE5vbmUgICMgQVBJIHB1YmxpY2F0aW9uIGRvZXMgbm90IG1ha2UgYWxsIG9mIEhPTUUgYSBHaXQgd29ya3RyZWUuCgpkZWYgcGVyc2lzdGVudF9zdGF0ZV9ob21lKHJlcG89Tm9uZSk6CiAgICBob21lID0gX3N0YXRlX25vX2xpbmtzKF9zdGF0ZV9wYXRobGliLlBhdGguaG9tZSgpKS5yZXNvbHZlKCkKICAgIHZhbHVlID0gb3MuZW52aXJvbi5nZXQoJ0xFQk9YX1NUQVRFX0hPTUUnKSBvciBvcy5lbnZpcm9uLmdldCgnU0NYX1NUQVRFX0hPTUUnKQogICAgcm9vdCA9IF9zdGF0ZV9wYXRobGliLlBhdGgodmFsdWUpLmV4cGFuZHVzZXIoKSBpZiB2YWx1ZSBlbHNlIGhvbWUgLyAnLmxlYm94LXN0YXRlJwogICAgaWYgbm90IHJvb3QuaXNfYWJzb2x1dGUoKToKICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0hPTUVfQUJTT0xVVEVfUkVRVUlSRUQnKQogICAgcm9vdCA9IF9zdGF0ZV9ub19saW5rcyhyb290KQogICAgaWYgcm9vdCA9PSBob21lIG9yIG5vdCByb290LmlzX3JlbGF0aXZlX3RvKGhvbWUpIG9yIGFueShwIGluIF9TVEFURV9FWENMVURFRCBmb3IgcCBpbiByb290LnJlbGF0aXZlX3RvKGhvbWUpLnBhcnRzKToKICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0hPTUVfTk9UX1BFUlNJU1RFTlQ6IGNob29zZSBhIHByaXZhdGUsIG5vbi1leGNsdWRlZCBkaXJlY3RvcnkgaW5zaWRlIEhPTUUnKQogICAgYm91bmRhcnkgPSBzdGF0ZV9yZXBvX2JvdW5kYXJ5KHJlcG8pCiAgICBhY3R1YWxfYm91bmRhcnkgPSBzdGF0ZV9yZXBvX2JvdW5kYXJ5KHJvb3QpCiAgICBpZiAoYm91bmRhcnkgaXMgbm90IE5vbmUgYW5kIHJvb3QuaXNfcmVsYXRpdmVfdG8oYm91bmRhcnkpKSBvciBhY3R1YWxfYm91bmRhcnkgaXMgbm90IE5vbmU6CiAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdTVEFURV9IT01FX0lOX1JFUE9TSVRPUlk6IGRvIG5vdCBwdWJsaXNoIHByaXZhdGUgc3RhdGUnKQogICAgcm9vdC5ta2Rpcihtb2RlPTBvNzAwLCBwYXJlbnRzPVRydWUsIGV4aXN0X29rPVRydWUpCiAgICBfc3RhdGVfbm9fbGlua3Mocm9vdCkKICAgIGlmIG9zLm5hbWUgIT0gJ250JyBhbmQgcm9vdC5zdGF0KCkuc3RfdWlkICE9IG9zLmdldHVpZCgpOgogICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignU1RBVEVfSE9NRV9PV05FUl9NSVNNQVRDSCcpCiAgICBvcy5jaG1vZChyb290LCAwbzcwMCkKICAgIHJldHVybiByb290CgpkZWYgX3N0YXRlX2pzb24ocmF3KToKICAgIGlmIGxlbihyYXcpID4gMV8yMDBfMDAwOgogICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignU1RBVEVfU0laRV9MSU1JVCcpCiAgICBkZWYgdW5pcXVlKHBhaXJzKToKICAgICAgICB2YWx1ZSA9IHt9CiAgICAgICAgZm9yIGtleSwgaXRlbSBpbiBwYWlyczoKICAgICAgICAgICAgaWYga2V5IGluIHZhbHVlOgogICAgICAgICAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdTVEFURV9EVVBMSUNBVEVfRklFTEQnKQogICAgICAgICAgICB2YWx1ZVtrZXldID0gaXRlbQogICAgICAgIHJldHVybiB2YWx1ZQogICAgdHJ5OgogICAgICAgIGRlZiBpbnZhbGlkX2NvbnN0YW50KF8pOgogICAgICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0lOVkFMSURfTlVNQkVSJykKICAgICAgICB2YWx1ZSA9IGpzb24ubG9hZHMocmF3LCBvYmplY3RfcGFpcnNfaG9vaz11bmlxdWUsIHBhcnNlX2NvbnN0YW50PWludmFsaWRfY29uc3RhbnQpCiAgICBleGNlcHQgKFZhbHVlRXJyb3IsIFVuaWNvZGVFcnJvcikgYXMgZXJyb3I6CiAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdTVEFURV9JTlZBTElEX0pTT046IG9yaWdpbmFsIGZpbGUgcHJlc2VydmVkJykgZnJvbSBlcnJvcgogICAgaWYgbm90IGlzaW5zdGFuY2UodmFsdWUsIGRpY3QpOgogICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignU1RBVEVfT0JKRUNUX1JFUVVJUkVEJykKICAgIHJldHVybiB2YWx1ZQoKZGVmIF9zdGF0ZV9jYW5vbmljYWwodmFsdWUpOgogICAgcmV0dXJuIGpzb24uZHVtcHModmFsdWUsIGVuc3VyZV9hc2NpaT1GYWxzZSwgc29ydF9rZXlzPVRydWUsIHNlcGFyYXRvcnM9KCcsJywgJzonKSwgYWxsb3dfbmFuPUZhbHNlKS5lbmNvZGUoJ3V0Zi04JykKCmRlZiBfc3RhdGVfcmVhZChwYXRoKToKICAgIHBhdGggPSBfc3RhdGVfbm9fbGlua3MocGF0aCkKICAgIGluZm8gPSBwYXRoLnN0YXQoKQogICAgaWYgbm90IHBhdGguaXNfZmlsZSgpIG9yIGluZm8uc3Rfc2l6ZSA+IDFfMjAwXzAwMCBvciBpbmZvLnN0X25saW5rICE9IDE6CiAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdTVEFURV9GSUxFX1VOU0FGRScpCiAgICBpZiBvcy5uYW1lICE9ICdudCcgYW5kIChpbmZvLnN0X3VpZCAhPSBvcy5nZXR1aWQoKSBvciBpbmZvLnN0X21vZGUgJiAwbzA3Nyk6CiAgICAgICAgcmFpc2UgU3RhdGVMb2NhdGlvbkVycm9yKCdTVEFURV9GSUxFX05PVF9QUklWQVRFJykKICAgIHJldHVybiBwYXRoLnJlYWRfYnl0ZXMoKQoKZGVmIF9zdGF0ZV93cml0ZShwYXRoLCByYXcpOgogICAgcGF0aCA9IF9zdGF0ZV9ub19saW5rcyhwYXRoKQogICAgdGVtcCA9IHBhdGgud2l0aF9uYW1lKCcuc3RhdGUtd3JpdGUtJyArIG9zLnVyYW5kb20oMTIpLmhleCgpKQogICAgZmQgPSBvcy5vcGVuKHRlbXAsIG9zLk9fV1JPTkxZIHwgb3MuT19DUkVBVCB8IG9zLk9fRVhDTCwgMG82MDApCiAgICB0cnk6CiAgICAgICAgd2l0aCBvcy5mZG9wZW4oZmQsICd3YicpIGFzIGZpbGU6CiAgICAgICAgICAgIGZpbGUud3JpdGUocmF3KTsgZmlsZS5mbHVzaCgpOyBvcy5mc3luYyhmaWxlLmZpbGVubygpKQogICAgICAgIG9zLnJlcGxhY2UodGVtcCwgcGF0aCkKICAgICAgICBpZiBvcy5uYW1lICE9ICdudCc6CiAgICAgICAgICAgIGRpcmVjdG9yeSA9IG9zLm9wZW4ocGF0aC5wYXJlbnQsIG9zLk9fUkRPTkxZIHwgZ2V0YXR0cihvcywgJ09fRElSRUNUT1JZJywgMCkpCiAgICAgICAgICAgIHRyeTogb3MuZnN5bmMoZGlyZWN0b3J5KQogICAgICAgICAgICBmaW5hbGx5OiBvcy5jbG9zZShkaXJlY3RvcnkpCiAgICBmaW5hbGx5OgogICAgICAgIHRlbXAudW5saW5rKG1pc3Npbmdfb2s9VHJ1ZSkKCkBfc3RhdGVfY29udGV4dGxpYi5jb250ZXh0bWFuYWdlcgpkZWYgX3N0YXRlX2xvY2socm9vdCk6CiAgICBsb2NrID0gX3N0YXRlX25vX2xpbmtzKHJvb3QgLyAnY2xpZW50LmxvY2snKQogICAgZmQgPSBvcy5vcGVuKGxvY2ssIG9zLk9fQ1JFQVQgfCBvcy5PX1JEV1IgfCBnZXRhdHRyKG9zLCAnT19OT0ZPTExPVycsIDApLCAwbzYwMCkKICAgIHRyeToKICAgICAgICBpbmZvID0gb3MuZnN0YXQoZmQpCiAgICAgICAgaWYgaW5mby5zdF9ubGluayAhPSAxIG9yIChvcy5uYW1lICE9ICdudCcgYW5kIChpbmZvLnN0X3VpZCAhPSBvcy5nZXR1aWQoKSBvciBpbmZvLnN0X21vZGUgJiAwbzA3NykpOgogICAgICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0xPQ0tfTk9UX1BSSVZBVEUnKQogICAgICAgIGlmIG9zLm5hbWUgPT0gJ250JzoKICAgICAgICAgICAgaW1wb3J0IG1zdmNydAogICAgICAgICAgICBpZiBvcy5mc3RhdChmZCkuc3Rfc2l6ZSA9PSAwOiBvcy53cml0ZShmZCwgYicwJykKICAgICAgICAgICAgb3MubHNlZWsoZmQsIDAsIDApOyBtc3ZjcnQubG9ja2luZyhmZCwgbXN2Y3J0LkxLX05CTENLLCAxKQogICAgICAgIGVsc2U6CiAgICAgICAgICAgIGltcG9ydCBmY250bAogICAgICAgICAgICBmY250bC5mbG9jayhmZCwgZmNudGwuTE9DS19FWCB8IGZjbnRsLkxPQ0tfTkIpCiAgICAgICAgeWllbGQKICAgIGV4Y2VwdCAoQmxvY2tpbmdJT0Vycm9yLCBQZXJtaXNzaW9uRXJyb3IpIGFzIGVycm9yOgogICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignU1RBVEVfQlVTWTogb3JpZ2luYWwgb3BlcmF0aW9uIG1heSBzdGlsbCBiZSBydW5uaW5nJykgZnJvbSBlcnJvcgogICAgZmluYWxseToKICAgICAgICBvcy5jbG9zZShmZCkKCmRlZiBwZXJzaXN0ZW50X3Nlc3Npb25fZGlyZWN0b3J5KGNvbmZpZywgcmVwbz1Ob25lKToKICAgIGJpbmRpbmcgPSBjb25maWcuZ2V0KCdiaW5kaW5nJywgJycpCiAgICBpZiBub3QgaXNpbnN0YW5jZShiaW5kaW5nLCBzdHIpIG9yIG5vdCByZS5mdWxsbWF0Y2gocidbMC05YS1mXXszMn0nLCBiaW5kaW5nKToKICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0JJTkRJTkdfSU5WQUxJRCcpCiAgICBlbmNvZGVkID0gX3N0YXRlX2Nhbm9uaWNhbChjb25maWcpOyBkaWdlc3QgPSBoYXNobGliLnNoYTI1NihlbmNvZGVkKS5oZXhkaWdlc3QoKQogICAgcm9vdCA9IHBlcnNpc3RlbnRfc3RhdGVfaG9tZShyZXBvKSAvICgnc2N4LWNvbGxhYm9yYXRpb24tJyArIGJpbmRpbmcpCiAgICBfc3RhdGVfbm9fbGlua3Mocm9vdCk7IHJvb3QubWtkaXIobW9kZT0wbzcwMCwgZXhpc3Rfb2s9VHJ1ZSk7IG9zLmNobW9kKHJvb3QsIDBvNzAwKQogICAgb2xkID0gX3N0YXRlX25vX2xpbmtzKF9zdGF0ZV9wYXRobGliLlBhdGgodGVtcGZpbGUuZ2V0dGVtcGRpcigpKSAvIHJvb3QubmFtZSkKICAgIGJvdW5kYXJ5ID0gc3RhdGVfcmVwb19ib3VuZGFyeShyZXBvKQogICAgaWYgYm91bmRhcnkgaXMgbm90IE5vbmUgYW5kIG9sZC5yZXNvbHZlKCkuaXNfcmVsYXRpdmVfdG8oYm91bmRhcnkpOgogICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignTEVHQUNZX1NUQVRFX0lOX1JFUE9TSVRPUlknKQogICAgdGFyZ2V0ID0gcm9vdCAvICdzdGF0ZS5qc29uJzsgZGVzY3JpcHRvciA9IHJvb3QgLyAnY29ubmVjdGlvbi5qc29uJwogICAgZGVmIHZhbGlkYXRlKHJhdyk6CiAgICAgICAgaWYgX3N0YXRlX2pzb24ocmF3KS5nZXQoJ2NvbmZpZ19oYXNoJykgIT0gZGlnZXN0OgogICAgICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0NPTkZJR19NSVNNQVRDSDogbmV2ZXIgcmVwbGFjZSBhbiBleGlzdGluZyBpZGVudGl0eScpCiAgICB3aXRoIF9zdGF0ZV9sb2NrKHJvb3QpOgogICAgICAgIGlmIGRlc2NyaXB0b3IuZXhpc3RzKCkgYW5kIF9zdGF0ZV9jYW5vbmljYWwoX3N0YXRlX2pzb24oX3N0YXRlX3JlYWQoZGVzY3JpcHRvcikpKSAhPSBlbmNvZGVkOgogICAgICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX0RFU0NSSVBUT1JfTUlTTUFUQ0gnKQogICAgICAgIGlmIHRhcmdldC5leGlzdHMoKTogdmFsaWRhdGUoX3N0YXRlX3JlYWQodGFyZ2V0KSkKICAgICAgICBpZiBvbGQucmVzb2x2ZSgpICE9IHJvb3QucmVzb2x2ZSgpIGFuZCAob2xkIC8gJ3N0YXRlLmpzb24nKS5leGlzdHMoKToKICAgICAgICAgICAgd2l0aCBfc3RhdGVfbG9jayhvbGQpOgogICAgICAgICAgICAgICAgb3JpZ2luYWwgPSBfc3RhdGVfcmVhZChvbGQgLyAnc3RhdGUuanNvbicpOyB2YWxpZGF0ZShvcmlnaW5hbCkKICAgICAgICAgICAgICAgIHNvdXJjZV9oYXNoID0gaGFzaGxpYi5zaGEyNTYob3JpZ2luYWwpLmhleGRpZ2VzdCgpCiAgICAgICAgICAgICAgICByZWNlaXB0ID0gcm9vdCAvICdtaWdyYXRpb24uanNvbicKICAgICAgICAgICAgICAgIHJlY29yZCA9IHsnZm9ybWF0JzogJ2xlYm94LXN0YXRlLW1pZ3JhdGlvbi12MScsICdzb3VyY2UnOiBzdHIob2xkLnJlc29sdmUoKSksCiAgICAgICAgICAgICAgICAgICAgICAgICAgJ3NvdXJjZV9zaGEyNTYnOiBzb3VyY2VfaGFzaCwgJ2NvbmZpZ19oYXNoJzogZGlnZXN0fQogICAgICAgICAgICAgICAgaWYgcmVjZWlwdC5leGlzdHMoKSBhbmQgbm90IHRhcmdldC5leGlzdHMoKToKICAgICAgICAgICAgICAgICAgICByYWlzZSBTdGF0ZUxvY2F0aW9uRXJyb3IoJ1NUQVRFX01JR1JBVElPTl9UQVJHRVRfTUlTU0lORzogcHJlc2VydmUgb3JpZ2luYWwgZXZpZGVuY2UnKQogICAgICAgICAgICAgICAgaWYgdGFyZ2V0LmV4aXN0cygpIGFuZCBfc3RhdGVfcmVhZCh0YXJnZXQpICE9IG9yaWdpbmFsOgogICAgICAgICAgICAgICAgICAgIGlmIG5vdCByZWNlaXB0LmV4aXN0cygpIG9yIF9zdGF0ZV9qc29uKF9zdGF0ZV9yZWFkKHJlY2VpcHQpKSAhPSByZWNvcmQ6CiAgICAgICAgICAgICAgICAgICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignU1RBVEVfTUlHUkFUSU9OX0NPTkZMSUNUOiBrZWVwIGJvdGggc3RhdGVzIGFuZCByZWNvbmNpbGUnKQogICAgICAgICAgICAgICAgZWxpZiBub3QgcmVjZWlwdC5leGlzdHMoKToKICAgICAgICAgICAgICAgICAgICBpZiBub3QgdGFyZ2V0LmV4aXN0cygpOiBfc3RhdGVfd3JpdGUodGFyZ2V0LCBvcmlnaW5hbCkKICAgICAgICAgICAgICAgICAgICBfc3RhdGVfd3JpdGUocmVjZWlwdCwgX3N0YXRlX2Nhbm9uaWNhbChyZWNvcmQpKQogICAgICAgICAgICAgICAgZWxpZiBfc3RhdGVfanNvbihfc3RhdGVfcmVhZChyZWNlaXB0KSkgIT0gcmVjb3JkOgogICAgICAgICAgICAgICAgICAgIHJhaXNlIFN0YXRlTG9jYXRpb25FcnJvcignU1RBVEVfTUlHUkFUSU9OX0NPTkZMSUNUOiBsZWdhY3kgc3RhdGUgY2hhbmdlZCcpCiAgICAgICAgaWYgbm90IGRlc2NyaXB0b3IuZXhpc3RzKCk6IF9zdGF0ZV93cml0ZShkZXNjcmlwdG9yLCBlbmNvZGVkKQogICAgcmV0dXJuIHJvb3QKCiMgRU5EIExFQk9YIFBFUlNJU1RFTlQgU1RBVEUgTEFZT1VUIFYxCgpWRVJTSU9OID0gJ3NjeC1naC1tMi12MScKTElNSVQgPSAxMDBfMDAwCiMgQWRhcHRpdmUgcG9sbGluZzogZmFzdCByaWdodCBhZnRlciB3ZSBwdWJsaXNoIHNvbWV0aGluZywgYmFja2luZyBvZmYgdG8gYSAxMCBzIGNlaWxpbmcgKEdpdCBmZXRjaGVzIGFyZSBjaGVhcCBhbmQKIyB0aGUgZGVza3RvcCBhbnN3ZXJzIHdpdGhpbiBzZWNvbmRzOyBhIGZpeGVkIDE1LTIwIHMgaW50ZXJ2YWwgd2FzIHRoZSBkb21pbmFudCBjb3N0IHBlciB0b29sIGNhbGwpLgpQT0xMX1NURVBTID0gKDIsIDIsIDMsIDQsIDYsIDgsIDEwKQoKZGVmIHBvbGxfZGVsYXkoYXR0ZW1wdCk6CiAgICByZXR1cm4gUE9MTF9TVEVQU1ttaW4oYXR0ZW1wdCwgbGVuKFBPTExfU1RFUFMpIC0gMSldCgpjbGFzcyBTdG9wKEV4Y2VwdGlvbik6CiAgICBwYXNzCgpkZWYgcmVxdWlyZShvaywgbWVzc2FnZSk6CiAgICBpZiBub3Qgb2s6CiAgICAgICAgcmFpc2UgU3RvcChtZXNzYWdlKQoKZGVmIGNhbm9uaWNhbCh2YWx1ZSk6CiAgICByZXR1cm4ganNvbi5kdW1wcyh2YWx1ZSwgZW5zdXJlX2FzY2lpPUZhbHNlLCBzb3J0X2tleXM9VHJ1ZSwgc2VwYXJhdG9ycz0oJywnLCAnOicpLCBhbGxvd19uYW49RmFsc2UpLmVuY29kZSgndXRmLTgnKQoKZGVmIGxvYWRfanNvbihkYXRhLCBsaW1pdD1MSU1JVCk6CiAgICByZXF1aXJlKGxlbihkYXRhKSA8PSBsaW1pdCwgJ0RhdGEgZXhjZWVkcyB0aGUgc2Vzc2lvbiBsaW1pdC4nKQogICAgZGVmIHVuaXF1ZShwYWlycyk6CiAgICAgICAgcmVzdWx0ID0ge30KICAgICAgICBmb3Iga2V5LCB2YWx1ZSBpbiBwYWlyczoKICAgICAgICAgICAgcmVxdWlyZShrZXkgbm90IGluIHJlc3VsdCwgJ0R1cGxpY2F0ZSBKU09OIGZpZWxkLicpCiAgICAgICAgICAgIHJlc3VsdFtrZXldID0gdmFsdWUKICAgICAgICByZXR1cm4gcmVzdWx0CiAgICByZXR1cm4ganNvbi5sb2FkcyhkYXRhLCBvYmplY3RfcGFpcnNfaG9vaz11bmlxdWUpCgpkZWYgZXhhY3QodmFsdWUsIGZpZWxkcyk6CiAgICByZXF1aXJlKGlzaW5zdGFuY2UodmFsdWUsIGRpY3QpIGFuZCBzZXQodmFsdWUpID09IHNldChmaWVsZHMpLCAnVW5leHBlY3RlZCBtZXNzYWdlIGZpZWxkcy4nKQoKZGVmIGI2NCh2YWx1ZSk6CiAgICByZXR1cm4gYmFzZTY0LmI2NGVuY29kZSh2YWx1ZSkuZGVjb2RlKCdhc2NpaScpCgpkZWYgdW5iNjQodmFsdWUsIGxlbmd0aD1Ob25lKToKICAgIHJlcXVpcmUoaXNpbnN0YW5jZSh2YWx1ZSwgc3RyKSBhbmQgbGVuKHZhbHVlKSA8PSA5MF8wMDAsICdJbnZhbGlkIGVuY29kZWQgdmFsdWUuJykKICAgIHJhdyA9IGJhc2U2NC5iNjRkZWNvZGUodmFsdWUsIHZhbGlkYXRlPVRydWUpCiAgICByZXF1aXJlKGxlbmd0aCBpcyBOb25lIG9yIGxlbihyYXcpID09IGxlbmd0aCwgJ0ludmFsaWQgZW5jb2RlZCBsZW5ndGguJykKICAgIHJldHVybiByYXcKCmRlZiBjcnlwdG8oKToKICAgIHRyeToKICAgICAgICBmcm9tIGNyeXB0b2dyYXBoeS5oYXptYXQucHJpbWl0aXZlcyBpbXBvcnQgaGFzaGVzLCBzZXJpYWxpemF0aW9uCiAgICAgICAgZnJvbSBjcnlwdG9ncmFwaHkuaGF6bWF0LnByaW1pdGl2ZXMuYXN5bW1ldHJpYyBpbXBvcnQgZWMKICAgICAgICBmcm9tIGNyeXB0b2dyYXBoeS5oYXptYXQucHJpbWl0aXZlcy5rZGYuaGtkZiBpbXBvcnQgSEtERgogICAgICAgIGZyb20gY3J5cHRvZ3JhcGh5Lmhhem1hdC5wcmltaXRpdmVzLmNpcGhlcnMuYWVhZCBpbXBvcnQgQUVTR0NNCiAgICAgICAgcmV0dXJuIGhhc2hlcywgc2VyaWFsaXphdGlvbiwgZWMsIEhLREYsIEFFU0dDTQogICAgZXhjZXB0IEltcG9ydEVycm9yOgogICAgICAgIHJhaXNlIFN0b3AoJ1JlcXVpcmVzIHRoZSBQeXRob24gY3J5cHRvZ3JhcGh5IHBhY2thZ2UuIEluc3RhbGwgaXQgb25seSBpZiB0aGUgZW52aXJvbm1lbnQgcGVybWl0cyBkZXBlbmRlbmNpZXM7IGRvIG5vdCBieXBhc3MgcGxhdGZvcm0gcmVzdHJpY3Rpb25zLicpCgpkZWYgdmFsaWRhdGVfY29uZmlnKGMpOgogICAgZXhhY3QoYywgWyd2ZXJzaW9uJywgJ3JlcG9zaXRvcnknLCAncmVwb19pZCcsICdicmFuY2gnLCAnYmluZGluZycsICdlcG9jaCcsICdwcm9qZWN0X2lkJywgJ21heF9vcGVyYXRpb25zJywgJ2V4cGlyZXNfYXQnXSkKICAgIHJlcXVpcmUoY1sndmVyc2lvbiddID09IFZFUlNJT04sICdVbnN1cHBvcnRlZCBjb2xsYWJvcmF0aW9uIHZlcnNpb247IHJlcXVlc3QgYSBmcmVzaCB0b29sIHBhY2thZ2UuJykKICAgIHJlcXVpcmUoaXNpbnN0YW5jZShjWydyZXBvc2l0b3J5J10sIHN0cikgYW5kIHJlLmZ1bGxtYXRjaChyJ1tBLVphLXowLTldW0EtWmEtejAtOS1dKi9bQS1aYS16MC05Ll8tXSsnLCBjWydyZXBvc2l0b3J5J10pLCAnSW52YWxpZCByZXBvc2l0b3J5LicpCiAgICByZXF1aXJlKGNbJ3JlcG9zaXRvcnknXS5zcGxpdCgnLycpWzFdIG5vdCBpbiAoJy4nLCAnLi4nKSwgJ0ludmFsaWQgcmVwb3NpdG9yeS4nKQogICAgcmVxdWlyZShpc2luc3RhbmNlKGNbJ3JlcG9faWQnXSwgc3RyKSBhbmQgcmUuZnVsbG1hdGNoKHInWzEtOV1bMC05XXswLDE5fScsIGNbJ3JlcG9faWQnXSksICdJbnZhbGlkIHJlcG9zaXRvcnkgSUQuJykKICAgIGJyYW5jaCA9IGNbJ2JyYW5jaCddCiAgICByZXF1aXJlKGlzaW5zdGFuY2UoYnJhbmNoLCBzdHIpIGFuZCAxIDw9IGxlbihicmFuY2gpIDw9IDIwMCBhbmQgcmUuZnVsbG1hdGNoKHInW0EtWmEtejAtOS5fLy1dKycsIGJyYW5jaCkKICAgICAgICAgICAgYW5kICcuLicgbm90IGluIGJyYW5jaCBhbmQgbm90IGJyYW5jaC5lbmRzd2l0aCgnLicpIGFuZCBicmFuY2gubG93ZXIoKSBub3QgaW4gKCdtYWluJywgJ21hc3RlcicpCiAgICAgICAgICAgIGFuZCBhbGwocCBhbmQgbm90IHAuc3RhcnRzd2l0aCgnLicpIGFuZCBub3QgcC5sb3dlcigpLmVuZHN3aXRoKCcubG9jaycpIGZvciBwIGluIGJyYW5jaC5zcGxpdCgnLycpKSwgJ0ludmFsaWQgY29sbGFib3JhdGlvbiBicmFuY2guJykKICAgIGZvciBrZXkgaW4gKCdiaW5kaW5nJywgJ2Vwb2NoJyk6CiAgICAgICAgcmVxdWlyZShpc2luc3RhbmNlKGNba2V5XSwgc3RyKSBhbmQgcmUuZnVsbG1hdGNoKHInWzAtOWEtZl17MzJ9JywgY1trZXldKSwgJ0ludmFsaWQgc2Vzc2lvbiBpZGVudGl0eS4nKQogICAgcmVxdWlyZShpc2luc3RhbmNlKGNbJ3Byb2plY3RfaWQnXSwgc3RyKSBhbmQgcmUuZnVsbG1hdGNoKHInW0EtWmEtejAtOV8tXXsxLDEyMH0nLCBjWydwcm9qZWN0X2lkJ10pLCAnSW52YWxpZCBwcm9qZWN0IGlkZW50aXR5LicpCiAgICAjIDAgbWVhbnMgdW5saW1pdGVkOiB0aGUgc2Vzc2lvbiBpcyBsb25nLWxpdmVkIGFuZCBlbmRzIG9ubHkgd2hlbiB0aGUgdXNlciBzdG9wcyBpdCBvbiB0aGVpciBtYWNoaW5lLgogICAgcmVxdWlyZSh0eXBlKGNbJ21heF9vcGVyYXRpb25zJ10pIGlzIGludCBhbmQgY1snbWF4X29wZXJhdGlvbnMnXSA+PSAwIGFuZCB0eXBlKGNbJ2V4cGlyZXNfYXQnXSkgaXMgaW50IGFuZCBjWydleHBpcmVzX2F0J10gPj0gMCwKICAgICAgICAgICAgJ0ludmFsaWQgc2Vzc2lvbiBsaW1pdHMuJykKICAgIHJlcXVpcmUoY1snZXhwaXJlc19hdCddID09IDAgb3IgY1snZXhwaXJlc19hdCddIC0gdGltZS50aW1lKCkgPiAwLAogICAgICAgICAgICAnU2Vzc2lvbiBleHBpcmVkIG9yIGludmFsaWQuIFJlcXVlc3QgYSBuZXcgcGFja2FnZTsgZG8gbm90IHJldXNlIG9sZCByZXF1ZXN0cy4nKQoKZGVmIHBpbnMoYyk6CiAgICByZXR1cm4ge2tleTogY1trZXldIGZvciBrZXkgaW4gKCd2ZXJzaW9uJywgJ3JlcG9faWQnLCAnYnJhbmNoJywgJ2JpbmRpbmcnLCAnZXBvY2gnKX0KCmRlZiBtZXNzYWdlX3BhdGgoYywga2luZCwgb3ApOgogICAgcmVxdWlyZShraW5kIGluICgnaGVsbG8nLCAnY29uZmlybWF0aW9uJywgJ3JlcXVlc3QnLCAncmVzcG9uc2UnLCAnY2xvc2VkJywgJ3JlbmV3JywgJ3Jlc3VsdHF1ZXJ5JywgJ3Jlc3VsdHJlcGx5JywgJ2FjdGl2YXRpb25xdWVyeScsICdhY3RpdmF0aW9ucmVwbHknKSBhbmQgcmUuZnVsbG1hdGNoKHInW2EtejAtOV1bYS16MC05LV17MCw3OX0nLCBvcCksICdJbnZhbGlkIG1lc3NhZ2UgcGF0aC4nKQogICAgcmV0dXJuIGYiLmxlYm94L3Nlc3Npb24te2NbJ2JpbmRpbmcnXX0ve29wfS57a2luZH0uanNvbiIKCmNsYXNzIEdpdE1haWxib3g6CiAgICBkZWYgX19pbml0X18oc2VsZiwgcmVwbywgY29uZmlnLCBwcml2YXRlKToKICAgICAgICBzZWxmLnJlcG8sIHNlbGYuYywgc2VsZi5wcml2YXRlID0gcmVwby5yZXNvbHZlKCksIGNvbmZpZywgcHJpdmF0ZQogICAgICAgIHNlbGYucmVmID0gJ3JlZnMvc2N4LWNvbGxhYm9yYXRpb24vJyArIGNvbmZpZ1snYmluZGluZyddCiAgICAgICAgaG9va3MgPSBwcml2YXRlIC8gJ2VtcHR5LWhvb2tzJwogICAgICAgIGhvb2tzLm1rZGlyKGV4aXN0X29rPVRydWUsIG1vZGU9MG83MDApCiAgICAgICAgcmVxdWlyZShub3QgaG9va3MuaXNfc3ltbGluaygpIGFuZCBub3QgYW55KGhvb2tzLml0ZXJkaXIoKSksICdQcml2YXRlIGhvb2tzIGRpcmVjdG9yeSBpcyBub3QgZW1wdHkuJykKICAgICAgICBzZWxmLmJhc2UgPSBbJ2dpdCcsICctYycsICdjb3JlLmhvb2tzUGF0aD0nICsgc3RyKGhvb2tzKSwgJy1jJywgJ2NvcmUuZnNtb25pdG9yPWZhbHNlJywKICAgICAgICAgICAgICAgICAgICAgJy1jJywgJ2NvbW1pdC5ncGdTaWduPWZhbHNlJywgJy1jJywgJ3B1c2guZ3BnU2lnbj1mYWxzZScsICctYycsICdodHRwLmZvbGxvd1JlZGlyZWN0cz1mYWxzZScsCiAgICAgICAgICAgICAgICAgICAgICctYycsICdwcm90b2NvbC5leHQuYWxsb3c9bmV2ZXInLCAnLWMnLCAncHJvdG9jb2wuZmlsZS5hbGxvdz1uZXZlcicsICctQycsIHN0cihzZWxmLnJlcG8pXQogICAgICAgICMgVmFsaWRhdGUgcmVzb2x2ZWQgZmV0Y2ggQU5EIHB1c2ggVVJMczsgbmV2ZXIgbGV0IGEgaGlkZGVuIHB1c2h1cmwgc2VsZWN0IGFub3RoZXIgcmVwb3NpdG9yeS4KICAgICAgICBmb3IgYXJncyBpbiBbKCdyZW1vdGUnLCAnZ2V0LXVybCcsICctLWFsbCcsICdvcmlnaW4nKSwgKCdyZW1vdGUnLCAnZ2V0LXVybCcsICctLXB1c2gnLCAnLS1hbGwnLCAnb3JpZ2luJyldOgogICAgICAgICAgICB1cmxzID0gc2VsZi5ydW4oKmFyZ3MpLmRlY29kZSgpLnNwbGl0bGluZXMoKQogICAgICAgICAgICByZXF1aXJlKGxlbih1cmxzKSA9PSAxLCAnRXhhY3RseSBvbmUgYXV0aG9yaXplZCBvcmlnaW4gVVJMIGlzIHJlcXVpcmVkLicpCiAgICAgICAgICAgIG9yaWdpbiA9IHVybHNbMF0uc3RyaXAoKQogICAgICAgICAgICBpZiBvcmlnaW4uc3RhcnRzd2l0aCgnZ2l0QGdpdGh1Yi5jb206Jyk6CiAgICAgICAgICAgICAgICB0YXJnZXQgPSBvcmlnaW5bbGVuKCdnaXRAZ2l0aHViLmNvbTonKTpdCiAgICAgICAgICAgIGVsc2U6CiAgICAgICAgICAgICAgICBwYXJzZWQgPSB1cmxzcGxpdChvcmlnaW4pCiAgICAgICAgICAgICAgICByZXF1aXJlKHBhcnNlZC5zY2hlbWUgPT0gJ2h0dHBzJyBhbmQgcGFyc2VkLmhvc3RuYW1lID09ICdnaXRodWIuY29tJyBhbmQgcGFyc2VkLnBvcnQgaW4gKE5vbmUsIDQ0MykKICAgICAgICAgICAgICAgICAgICAgICAgYW5kIG5vdCBwYXJzZWQucXVlcnkgYW5kIG5vdCBwYXJzZWQuZnJhZ21lbnQsICdPcmlnaW4gbXVzdCBiZSB0aGUgYXV0aG9yaXplZCBnaXRodWIuY29tIHJlcG9zaXRvcnkuJykKICAgICAgICAgICAgICAgIHRhcmdldCA9IHBhcnNlZC5wYXRoLmxzdHJpcCgnLycpCiAgICAgICAgICAgIHJlcXVpcmUodGFyZ2V0LnJlbW92ZXN1ZmZpeCgnLmdpdCcpLmxvd2VyKCkgPT0gY29uZmlnWydyZXBvc2l0b3J5J10ubG93ZXIoKSwgJ1JlcG9zaXRvcnkgZG9lcyBub3QgbWF0Y2ggdGhpcyBzZXNzaW9uLicpCiAgICAgICAgYnJhbmNoID0gc2VsZi5ydW4oJ3N5bWJvbGljLXJlZicsICctLXNob3J0JywgJ0hFQUQnKS5kZWNvZGUoKS5zdHJpcCgpCiAgICAgICAgcmVxdWlyZShicmFuY2ggPT0gY29uZmlnWydicmFuY2gnXSwKICAgICAgICAgICAgICAgIGYiVGhpcyBwYWNrYWdlIGlzIGJvdW5kIHRvIGJyYW5jaCB7Y29uZmlnWydicmFuY2gnXX0gYnV0IHRoZSBjaGVja2VkLW91dCBicmFuY2ggaXMge2JyYW5jaH0uICIKICAgICAgICAgICAgICAgICdJdCB3YXMgaXNzdWVkIGZvciBhIGRpZmZlcmVudCBjb252ZXJzYXRpb24uIERvIE5PVCBzd2l0Y2ggYnJhbmNoZXMgb3IgcmV1c2UgaXQ6IHRlbGwgdGhlIHVzZXIgdG8gY2xpY2sgJwogICAgICAgICAgICAgICAgJyLov57mjqXmlrAgQWdlbnQiIGluIHRoZSBTaHVuQ29kZXggR2l0SHViIOWNj+S9nCBtZW51IGZvciBUSElTIGNvbnZlcnNhdGlvbiBhbmQgcGFzdGUgdGhlIG5ldyBpbnN0cnVjdGlvbnMgaGVyZS4nKQoKICAgIGRlZiBydW4oc2VsZiwgKmFyZ3MsIGRhdGE9Tm9uZSwgZW52PU5vbmUpOgogICAgICAgIHNhZmVfZW52ID0gZGljdChvcy5lbnZpcm9uIGlmIGVudiBpcyBOb25lIGVsc2UgZW52KQogICAgICAgIHNhZmVfZW52WydHSVRfVEVSTUlOQUxfUFJPTVBUJ10gPSAnMCcKICAgICAgICByZXN1bHQgPSBzdWJwcm9jZXNzLnJ1bihzZWxmLmJhc2UgKyBsaXN0KGFyZ3MpLCBpbnB1dD1kYXRhLCBzdGRvdXQ9c3VicHJvY2Vzcy5QSVBFLCBzdGRlcnI9c3VicHJvY2Vzcy5QSVBFLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIGVudj1zYWZlX2VudiwgdGltZW91dD00NSwgY2hlY2s9RmFsc2UpCiAgICAgICAgcmVxdWlyZShyZXN1bHQucmV0dXJuY29kZSA9PSAwLCAnR2l0IG9wZXJhdGlvbiB3YXMgbm90IGNvbmZpcm1lZC4gQ2hlY2sgcmVwb3NpdG9yeSBhdXRob3JpemF0aW9uIGFuZCBvcmlnaW5hbCByZXF1ZXN0IHN0YXR1czsgbm8gZm9yY2UgcHVzaCBvciBhdXRvbWF0aWMgcmV0cnkgd2FzIHBlcmZvcm1lZC4nKQogICAgICAgIHJldHVybiByZXN1bHQuc3Rkb3V0CgogICAgZGVmIGZldGNoKHNlbGYpOgogICAgICAgIHNlbGYucnVuKCdmZXRjaCcsICctLXF1aWV0JywgJy0tbm8tdGFncycsICctLW5vLXdyaXRlLWZldGNoLWhlYWQnLCAnb3JpZ2luJywgZiJyZWZzL2hlYWRzL3tzZWxmLmNbJ2JyYW5jaCddfTp7c2VsZi5yZWZ9IikKICAgICAgICB0aXAgPSBzZWxmLnJ1bigncmV2LXBhcnNlJywgJy0tdmVyaWZ5Jywgc2VsZi5yZWYpLmRlY29kZSgpLnN0cmlwKCkKICAgICAgICByZXF1aXJlKHJlLmZ1bGxtYXRjaCgnWzAtOWEtZl17NDB9JywgdGlwKSwgJ1Vuc3VwcG9ydGVkIEdpdCBvYmplY3QgaWRlbnRpdHkuJykKICAgICAgICByZXR1cm4gdGlwCgogICAgZGVmIGF0KHNlbGYsIHRpcCwgcGF0aCk6CiAgICAgICAgbGlzdGluZyA9IHNlbGYucnVuKCdscy10cmVlJywgJy16JywgdGlwLCAnLS0nLCBwYXRoKQogICAgICAgIGlmIG5vdCBsaXN0aW5nOgogICAgICAgICAgICByZXR1cm4gTm9uZQogICAgICAgIHBhcnRzID0gbGlzdGluZy5zcGxpdChiJ1wwJykKICAgICAgICByZXF1aXJlKGxlbihwYXJ0cykgPT0gMiBhbmQgcGFydHNbMV0gPT0gYicnLCAnVW5leHBlY3RlZCBHaXQgdHJlZSBlbnRyeS4nKQogICAgICAgIG1ldGFkYXRhLCBhY3R1YWwgPSBwYXJ0c1swXS5zcGxpdChiJ1x0JywgMSkKICAgICAgICBtb2RlLCBraW5kLCBvaWQgPSBtZXRhZGF0YS5zcGxpdChiJyAnKQogICAgICAgIHJlcXVpcmUobW9kZSA9PSBiJzEwMDY0NCcgYW5kIGtpbmQgPT0gYidibG9iJyBhbmQgYWN0dWFsLmRlY29kZSgpID09IHBhdGgsICdNZXNzYWdlIGlzIG5vdCBhbiBvcmRpbmFyeSBmaWxlLicpCiAgICAgICAgc2l6ZSA9IGludChzZWxmLnJ1bignY2F0LWZpbGUnLCAnLXMnLCBvaWQuZGVjb2RlKCkpKQogICAgICAgIHJlcXVpcmUoMCA8IHNpemUgPD0gTElNSVQsICdNZXNzYWdlIGV4Y2VlZHMgdGhlIHNpemUgbGltaXQuJykKICAgICAgICBjb250ZW50ID0gc2VsZi5ydW4oJ2NhdC1maWxlJywgJ2Jsb2InLCBvaWQuZGVjb2RlKCkpCiAgICAgICAgcmVxdWlyZShsZW4oY29udGVudCkgPT0gc2l6ZSwgJ0dpdCBvYmplY3Qgc2l6ZSBtaXNtYXRjaC4nKQogICAgICAgIHJldHVybiBjb250ZW50CgogICAgZGVmIHJlYWQoc2VsZiwga2luZCwgb3ApOgogICAgICAgIHJldHVybiBzZWxmLmF0KHNlbGYuZmV0Y2goKSwgbWVzc2FnZV9wYXRoKHNlbGYuYywga2luZCwgb3ApKQoKICAgIGRlZiBjcmVhdGUoc2VsZiwga2luZCwgb3AsIGRhdGEpOgogICAgICAgIHJlcXVpcmUoMCA8IGxlbihkYXRhKSA8PSBMSU1JVCwgJ01lc3NhZ2UgZXhjZWVkcyB0aGUgc2l6ZSBsaW1pdC4nKQogICAgICAgIHBhdGggPSBtZXNzYWdlX3BhdGgoc2VsZi5jLCBraW5kLCBvcCkKICAgICAgICB0aXAgPSBzZWxmLmZldGNoKCkKICAgICAgICBwcmV2aW91cyA9IHNlbGYuYXQodGlwLCBwYXRoKQogICAgICAgIGlmIHByZXZpb3VzIGlzIG5vdCBOb25lOgogICAgICAgICAgICByZXF1aXJlKHByZXZpb3VzID09IGRhdGEsICdBIGRpZmZlcmVudCBtZXNzYWdlIGFscmVhZHkgZXhpc3RzLiBPdmVyd3JpdGluZyBpcyBmb3JiaWRkZW4uJykKICAgICAgICAgICAgcmV0dXJuCiAgICAgICAgIyBBIHByaXZhdGUgaW5kZXggKyBwbHVtYmluZyBsZWF2ZXMgdGhlIHVzZXIncyBpbmRleCwgd29ya2luZyB0cmVlIGFuZCBjaGVja2VkLW91dCBicmFuY2ggdW50b3VjaGVkLgogICAgICAgIGluZGV4ID0gc2VsZi5wcml2YXRlIC8gKCdpbmRleC0nICsgc2VjcmV0cy50b2tlbl9oZXgoOCkpCiAgICAgICAgZW52ID0gb3MuZW52aXJvbi5jb3B5KCkKICAgICAgICBlbnYudXBkYXRlKEdJVF9JTkRFWF9GSUxFPXN0cihpbmRleCksIEdJVF9BVVRIT1JfTkFNRT0nU2h1bkNvZGV4IGNvbGxhYm9yYXRpb24nLAogICAgICAgICAgICAgICAgICAgR0lUX0FVVEhPUl9FTUFJTD0nY29sbGFib3JhdGlvbkBsb2NhbGhvc3QnLCBHSVRfQ09NTUlUVEVSX05BTUU9J1NodW5Db2RleCBjb2xsYWJvcmF0aW9uJywKICAgICAgICAgICAgICAgICAgIEdJVF9DT01NSVRURVJfRU1BSUw9J2NvbGxhYm9yYXRpb25AbG9jYWxob3N0JykKICAgICAgICB0cnk6CiAgICAgICAgICAgIHNlbGYucnVuKCdyZWFkLXRyZWUnLCB0aXAsIGVudj1lbnYpCiAgICAgICAgICAgIGJsb2IgPSBzZWxmLnJ1bignaGFzaC1vYmplY3QnLCAnLXcnLCAnLS1zdGRpbicsIGRhdGE9ZGF0YSwgZW52PWVudikuZGVjb2RlKCkuc3RyaXAoKQogICAgICAgICAgICBzZWxmLnJ1bigndXBkYXRlLWluZGV4JywgJy0tYWRkJywgJy0tY2FjaGVpbmZvJywgZicxMDA2NDQse2Jsb2J9LHtwYXRofScsIGVudj1lbnYpCiAgICAgICAgICAgIHRyZWUgPSBzZWxmLnJ1bignd3JpdGUtdHJlZScsIGVudj1lbnYpLmRlY29kZSgpLnN0cmlwKCkKICAgICAgICAgICAgY29tbWl0ID0gc2VsZi5ydW4oJ2NvbW1pdC10cmVlJywgdHJlZSwgJy1wJywgdGlwLCBkYXRhPWInQWRkIGNvbGxhYm9yYXRpb24gbWVzc2FnZVxuJywgZW52PWVudikuZGVjb2RlKCkuc3RyaXAoKQogICAgICAgICAgICB0cnk6CiAgICAgICAgICAgICAgICBzZWxmLnJ1bigncHVzaCcsICctLXF1aWV0JywgJy0tbm8tdmVyaWZ5JywgJ29yaWdpbicsIGYie2NvbW1pdH06cmVmcy9oZWFkcy97c2VsZi5jWydicmFuY2gnXX0iLCBlbnY9ZW52KQogICAgICAgICAgICBleGNlcHQgKFN0b3AsIHN1YnByb2Nlc3MuVGltZW91dEV4cGlyZWQpOgogICAgICAgICAgICAgICAgb2JzZXJ2ZWQgPSBzZWxmLmF0KHNlbGYuZmV0Y2goKSwgcGF0aCkgICMgT25lIHJlYWQtb25seSByZWNvbmNpbGlhdGlvbiwgbmV2ZXIgYW5vdGhlciBwdXNoLgogICAgICAgICAgICAgICAgcmVxdWlyZShvYnNlcnZlZCA9PSBkYXRhLCAnUHVibGljYXRpb24gb3V0Y29tZSB1bmtub3duLiBLZWVwIHRoZSBzYW1lIHBlbmRpbmcgb3BlcmF0aW9uOyB1c2Ugc3RhdHVzLCBub3QgYW5vdGhlciBjYWxsLicpCiAgICAgICAgZmluYWxseToKICAgICAgICAgICAgaW5kZXgudW5saW5rKG1pc3Npbmdfb2s9VHJ1ZSkKICAgICAgICAgICAgUGF0aChzdHIoaW5kZXgpICsgJy5sb2NrJykudW5saW5rKG1pc3Npbmdfb2s9VHJ1ZSkKCmNsYXNzIEFwaU1haWxib3g6CiAgICAiIiJTYW1lIGNvbnRyYWN0IGFzIEdpdE1haWxib3ggKGZldGNoL2F0L3JlYWQvY3JlYXRlKSBvdmVyIHRoZSBHaXRIdWIgUkVTVCBBUEksIGZvciBzYW5kYm94ZXMgd2l0aG91dCBhIGdpdCBiaW5hcnkuCiAgICBVc2VzIGEgdG9rZW4gZnJvbSB0aGUgZW52aXJvbm1lbnQgb25seSAobmV2ZXIgd3JpdHRlbiB0byBkaXNrIG9yIGxvZ3MpLiBDcmVhdGUgbmV2ZXIgb3ZlcndyaXRlczogYSBQVVQgd2l0aG91dCBzaGEgaXMKICAgIHJlamVjdGVkIGJ5IEdpdEh1YiB3aGVuIHRoZSBwYXRoIGFscmVhZHkgZXhpc3RzLCBtYXRjaGluZyB0aGUgZ2l0IGltcGxlbWVudGF0aW9uJ3Mgbm8tb3ZlcndyaXRlIHJ1bGUuIiIiCiAgICBBUEkgPSAnaHR0cHM6Ly9hcGkuZ2l0aHViLmNvbScKCiAgICBkZWYgX19pbml0X18oc2VsZiwgY29uZmlnLCB0b2tlbik6CiAgICAgICAgcmVxdWlyZShpc2luc3RhbmNlKHRva2VuLCBzdHIpIGFuZCAxIDw9IGxlbih0b2tlbikgPD0gNDA5NiBhbmQgbm90IGFueShjaC5pc3NwYWNlKCkgZm9yIGNoIGluIHRva2VuKSwgJ0dpdEh1YiB0b2tlbiBpcyBtaXNzaW5nIG9yIG1hbGZvcm1lZC4nKQogICAgICAgIHNlbGYuYywgc2VsZi5fdG9rZW4gPSBjb25maWcsIHRva2VuCiAgICAgICAgcmVwbyA9IGNvbmZpZ1sncmVwb3NpdG9yeSddCiAgICAgICAgcmVxdWlyZShyZS5mdWxsbWF0Y2gocidbQS1aYS16MC05XVtBLVphLXowLTktXSovW0EtWmEtejAtOS5fLV0rJywgcmVwbyksICdSZXBvc2l0b3J5IG5hbWUgaXMgaW52YWxpZC4nKQogICAgICAgIHNlbGYucmVwbyA9IHJlcG8KICAgICAgICBpbmZvID0gc2VsZi5fanNvbignR0VUJywgZicvcmVwb3Mve3JlcG99JykKICAgICAgICByZXF1aXJlKHN0cihpbmZvLmdldCgnaWQnKSkgPT0gc3RyKGNvbmZpZy5nZXQoJ3JlcG9faWQnKSkgYW5kIGluZm8uZ2V0KCdwcml2YXRlJykgaXMgVHJ1ZSwgJ1JlcG9zaXRvcnkgaWRlbnRpdHkgZG9lcyBub3QgbWF0Y2ggdGhpcyBzZXNzaW9uLicpCgogICAgZGVmIF9qc29uKHNlbGYsIG1ldGhvZCwgcGF0aCwgYm9keT1Ob25lLCBvaz0oMjAwLCAyMDEpLCBhbGxvdz0oKSk6CiAgICAgICAgZGF0YSA9IE5vbmUgaWYgYm9keSBpcyBOb25lIGVsc2UganNvbi5kdW1wcyhib2R5KS5lbmNvZGUoKQogICAgICAgIHJlcSA9IHVybGxpYi5yZXF1ZXN0LlJlcXVlc3Qoc2VsZi5BUEkgKyBwYXRoLCBkYXRhPWRhdGEsIG1ldGhvZD1tZXRob2QsIGhlYWRlcnM9ewogICAgICAgICAgICAnQXV0aG9yaXphdGlvbic6ICdCZWFyZXIgJyArIHNlbGYuX3Rva2VuLCAnQWNjZXB0JzogJ2FwcGxpY2F0aW9uL3ZuZC5naXRodWIranNvbicsCiAgICAgICAgICAgICdYLUdpdEh1Yi1BcGktVmVyc2lvbic6ICcyMDIyLTExLTI4JywgJ1VzZXItQWdlbnQnOiAnbGVib3gtYWdlbnQvMS4wJywKICAgICAgICAgICAgKiooeydDb250ZW50LVR5cGUnOiAnYXBwbGljYXRpb24vanNvbid9IGlmIGRhdGEgZWxzZSB7fSl9KQogICAgICAgIHRyeToKICAgICAgICAgICAgd2l0aCB1cmxsaWIucmVxdWVzdC51cmxvcGVuKHJlcSwgdGltZW91dD00MCkgYXMgcmVzcDoKICAgICAgICAgICAgICAgIHN0YXR1cywgcmF3ID0gcmVzcC5zdGF0dXMsIHJlc3AucmVhZCgyXzAwMF8wMDApCiAgICAgICAgZXhjZXB0IHVybGxpYi5lcnJvci5IVFRQRXJyb3IgYXMgZXg6CiAgICAgICAgICAgIHN0YXR1cywgcmF3ID0gZXguY29kZSwgZXgucmVhZCgyMDBfMDAwKQogICAgICAgICAgICBpZiBzdGF0dXMgaW4gYWxsb3c6CiAgICAgICAgICAgICAgICByZXR1cm4gc3RhdHVzLCAoanNvbi5sb2FkcyhyYXcpIGlmIHJhdyBlbHNlIHt9KQogICAgICAgICAgICBpZiBzdGF0dXMgPT0gNDAxOgogICAgICAgICAgICAgICAgcmVxdWlyZShGYWxzZSwgJ0dpdEh1YiB0b2tlbiByZWplY3RlZCAoNDAxKS4gQXNrIHRoZSB1c2VyIGZvciBhIHZhbGlkIHRva2VuOyBub3RoaW5nIHdhcyB3cml0dGVuLicpCiAgICAgICAgICAgIGlmIHN0YXR1cyBpbiAoNDAzLCA0MjkpOgogICAgICAgICAgICAgICAgcmVxdWlyZShGYWxzZSwgJ0dpdEh1YiByZWZ1c2VkIG9yIHJhdGUtbGltaXRlZCB0aGUgcmVxdWVzdCAoNDAzLzQyOSkuIENoZWNrIHRva2VuIHNjb3BlIChDb250ZW50czogcmVhZC93cml0ZSBvbiB0aGlzIHJlcG9zaXRvcnkpIGFuZCByZXRyeSBsYXRlci4nKQogICAgICAgICAgICBpZiBzdGF0dXMgPT0gNDA0OgogICAgICAgICAgICAgICAgcmVxdWlyZShGYWxzZSwgJ1JlcG9zaXRvcnksIGJyYW5jaCBvciBmaWxlIG5vdCB2aXNpYmxlIHRvIHRoaXMgdG9rZW4gKDQwNCkuJykKICAgICAgICAgICAgcmVxdWlyZShGYWxzZSwgZidHaXRIdWIgcmVxdWVzdCBmYWlsZWQgKHtzdGF0dXN9KS4nKQogICAgICAgIGV4Y2VwdCAodXJsbGliLmVycm9yLlVSTEVycm9yLCBUaW1lb3V0RXJyb3IsIE9TRXJyb3IpOgogICAgICAgICAgICByZXF1aXJlKEZhbHNlLCAnR2l0SHViIGlzIHVucmVhY2hhYmxlIGZyb20gdGhpcyBzYW5kYm94OyBubyBhdXRvbWF0aWMgcmV0cnkuJykKICAgICAgICByZXF1aXJlKHN0YXR1cyBpbiBvaywgZidVbmV4cGVjdGVkIEdpdEh1YiBzdGF0dXMge3N0YXR1c30uJykKICAgICAgICByZXR1cm4ganNvbi5sb2FkcyhyYXcpIGlmIHJhdyBlbHNlIHt9CgogICAgZGVmIGZldGNoKHNlbGYpOgogICAgICAgIHJlZiA9IHNlbGYuX2pzb24oJ0dFVCcsIGYiL3JlcG9zL3tzZWxmLnJlcG99L2dpdC9yZWYvaGVhZHMve3F1b3RlKHNlbGYuY1snYnJhbmNoJ10sIHNhZmU9JycpfSIpCiAgICAgICAgdGlwID0gcmVmLmdldCgnb2JqZWN0Jywge30pLmdldCgnc2hhJywgJycpCiAgICAgICAgcmVxdWlyZShyZS5mdWxsbWF0Y2goJ1swLTlhLWZdezQwfScsIHRpcCBvciAnJyksICdVbnN1cHBvcnRlZCBHaXQgb2JqZWN0IGlkZW50aXR5LicpCiAgICAgICAgcmV0dXJuIHRpcAoKICAgIGRlZiBhdChzZWxmLCB0aXAsIHBhdGgpOgogICAgICAgIHJlc3VsdCA9IHNlbGYuX2pzb24oJ0dFVCcsIGYiL3JlcG9zL3tzZWxmLnJlcG99L2NvbnRlbnRzL3txdW90ZShwYXRoKX0/cmVmPXt0aXB9IiwgYWxsb3c9KDQwNCwpKQogICAgICAgIGlmIGlzaW5zdGFuY2UocmVzdWx0LCB0dXBsZSk6CiAgICAgICAgICAgIHJldHVybiBOb25lICAjIDQwNDogbm90IHB1Ymxpc2hlZCB5ZXQKICAgICAgICBmaWxlID0gcmVzdWx0CiAgICAgICAgcmVxdWlyZShmaWxlLmdldCgndHlwZScpID09ICdmaWxlJyBhbmQgZmlsZS5nZXQoJ3BhdGgnKSA9PSBwYXRoIGFuZCBmaWxlLmdldCgnZW5jb2RpbmcnKSA9PSAnYmFzZTY0JywgJ01lc3NhZ2UgaXMgbm90IGFuIG9yZGluYXJ5IGZpbGUuJykKICAgICAgICBzaXplID0gaW50KGZpbGUuZ2V0KCdzaXplJywgLTEpKQogICAgICAgIHJlcXVpcmUoMCA8IHNpemUgPD0gTElNSVQsICdNZXNzYWdlIGV4Y2VlZHMgdGhlIHNpemUgbGltaXQuJykKICAgICAgICBjb250ZW50ID0gYmFzZTY0LmI2NGRlY29kZShmaWxlLmdldCgnY29udGVudCcsICcnKSkKICAgICAgICByZXF1aXJlKGxlbihjb250ZW50KSA9PSBzaXplLCAnR2l0IG9iamVjdCBzaXplIG1pc21hdGNoLicpCiAgICAgICAgcmV0dXJuIGNvbnRlbnQKCiAgICBkZWYgcmVhZChzZWxmLCBraW5kLCBvcCk6CiAgICAgICAgcmV0dXJuIHNlbGYuYXQoc2VsZi5mZXRjaCgpLCBtZXNzYWdlX3BhdGgoc2VsZi5jLCBraW5kLCBvcCkpCgogICAgZGVmIGNyZWF0ZShzZWxmLCBraW5kLCBvcCwgZGF0YSk6CiAgICAgICAgcmVxdWlyZSgwIDwgbGVuKGRhdGEpIDw9IExJTUlULCAnTWVzc2FnZSBleGNlZWRzIHRoZSBzaXplIGxpbWl0LicpCiAgICAgICAgcGF0aCA9IG1lc3NhZ2VfcGF0aChzZWxmLmMsIGtpbmQsIG9wKQogICAgICAgIHByZXZpb3VzID0gc2VsZi5hdChzZWxmLmZldGNoKCksIHBhdGgpCiAgICAgICAgaWYgcHJldmlvdXMgaXMgbm90IE5vbmU6CiAgICAgICAgICAgIHJlcXVpcmUocHJldmlvdXMgPT0gZGF0YSwgJ0EgZGlmZmVyZW50IG1lc3NhZ2UgYWxyZWFkeSBleGlzdHMuIE92ZXJ3cml0aW5nIGlzIGZvcmJpZGRlbi4nKQogICAgICAgICAgICByZXR1cm4KICAgICAgICBib2R5ID0geydtZXNzYWdlJzogJ0FkZCBjb2xsYWJvcmF0aW9uIG1lc3NhZ2UnLCAnYnJhbmNoJzogc2VsZi5jWydicmFuY2gnXSwgJ2NvbnRlbnQnOiBiYXNlNjQuYjY0ZW5jb2RlKGRhdGEpLmRlY29kZSgpfQogICAgICAgIHJlc3VsdCA9IHNlbGYuX2pzb24oJ1BVVCcsIGYiL3JlcG9zL3tzZWxmLnJlcG99L2NvbnRlbnRzL3txdW90ZShwYXRoKX0iLCBib2R5PWJvZHksIG9rPSgyMDEsKSwgYWxsb3c9KDQwOSwgNDIyKSkKICAgICAgICBpZiBpc2luc3RhbmNlKHJlc3VsdCwgdHVwbGUpOgogICAgICAgICAgICBvYnNlcnZlZCA9IHNlbGYuYXQoc2VsZi5mZXRjaCgpLCBwYXRoKSAgIyBPbmUgcmVhZC1vbmx5IHJlY29uY2lsaWF0aW9uLCBuZXZlciBhbm90aGVyIFBVVC4KICAgICAgICAgICAgcmVxdWlyZShvYnNlcnZlZCA9PSBkYXRhLCAnUHVibGljYXRpb24gb3V0Y29tZSB1bmtub3duLiBLZWVwIHRoZSBzYW1lIHBlbmRpbmcgb3BlcmF0aW9uOyB1c2Ugc3RhdHVzLCBub3QgYW5vdGhlciBjYWxsLicpCiAgICAgICAgICAgIHJldHVybgogICAgICAgIHJlcXVpcmUocmVzdWx0LmdldCgnY29udGVudCcsIHt9KS5nZXQoJ3BhdGgnKSA9PSBwYXRoLCAnUHVibGljYXRpb24gcmVjZWlwdCBkb2VzIG5vdCBtYXRjaCB0aGUgbWVzc2FnZS4nKQoKCmNsYXNzIFByaXZhdGVTdGF0ZToKICAgIGRlZiBfX2luaXRfXyhzZWxmLCByb290LCBjb25maWcsIHJlcG8pOgogICAgICAgIHJlcXVpcmUobm90IHJvb3QuaXNfc3ltbGluaygpLCAnUHJpdmF0ZSBzdGF0ZSBtdXN0IG5vdCBiZSBhIHN5bWJvbGljIGxpbmsuJykKICAgICAgICBzZWxmLnJvb3QgPSByb290LnJlc29sdmUoKQogICAgICAgIHJlcXVpcmUocmVwbyBpcyBOb25lIG9yIG5vdCBzZWxmLnJvb3QuaXNfcmVsYXRpdmVfdG8ocmVwby5yZXNvbHZlKCkpLCAnUHJpdmF0ZSBzdGF0ZSBtdXN0IHN0YXkgb3V0c2lkZSB0aGUgcmVwb3NpdG9yeS4nKQogICAgICAgIHJvb3QubWtkaXIocGFyZW50cz1UcnVlLCBleGlzdF9vaz1UcnVlLCBtb2RlPTBvNzAwKQogICAgICAgIG9zLmNobW9kKHNlbGYucm9vdCwgMG83MDApCiAgICAgICAgc2VsZi5maWxlID0gc2VsZi5yb290IC8gJ3N0YXRlLmpzb24nCiAgICAgICAgcmVxdWlyZShub3Qgc2VsZi5maWxlLmlzX3N5bWxpbmsoKSwgJ1ByaXZhdGUgc3RhdGUgZmlsZSBtdXN0IG5vdCBiZSBhIHN5bWJvbGljIGxpbmsuJykKICAgICAgICBzZWxmLmNvbmZpZ19oYXNoID0gaGFzaGxpYi5zaGEyNTYoY2Fub25pY2FsKGNvbmZpZykpLmhleGRpZ2VzdCgpCiAgICAgICAgc2VsZi52YWx1ZSA9IGxvYWRfanNvbihzZWxmLmZpbGUucmVhZF9ieXRlcygpLCBsaW1pdD0xXzIwMF8wMDApIGlmIHNlbGYuZmlsZS5leGlzdHMoKSBlbHNlIHsnY29uZmlnX2hhc2gnOiBzZWxmLmNvbmZpZ19oYXNoLCAnbmV4dCc6IDB9CiAgICAgICAgcmVxdWlyZShzZWxmLnZhbHVlLmdldCgnY29uZmlnX2hhc2gnKSA9PSBzZWxmLmNvbmZpZ19oYXNoLCAnUHJpdmF0ZSBzdGF0ZSBiZWxvbmdzIHRvIGFub3RoZXIgc2Vzc2lvbi4nKQoKICAgIGRlZiBzYXZlKHNlbGYpOgogICAgICAgIGRhdGEgPSBjYW5vbmljYWwoc2VsZi52YWx1ZSkKICAgICAgICByZXF1aXJlKGxlbihkYXRhKSA8IDFfMjAwXzAwMCwgJ1ByaXZhdGUgc2Vzc2lvbiBzdGF0ZSBpcyBmdWxsOyBmaW5pc2ggb3IgZW5kIHRoaXMgc2Vzc2lvbi4nKQogICAgICAgIHRtcCA9IHNlbGYucm9vdCAvICgnc3RhdGUtJyArIHNlY3JldHMudG9rZW5faGV4KDgpKQogICAgICAgIGZkID0gb3Mub3Blbih0bXAsIG9zLk9fQ1JFQVQgfCBvcy5PX0VYQ0wgfCBvcy5PX1dST05MWSwgMG82MDApCiAgICAgICAgdHJ5OgogICAgICAgICAgICB3aXRoIG9zLmZkb3BlbihmZCwgJ3diJykgYXMgZmlsZToKICAgICAgICAgICAgICAgIGZpbGUud3JpdGUoZGF0YSk7IGZpbGUuZmx1c2goKTsgb3MuZnN5bmMoZmlsZS5maWxlbm8oKSkKICAgICAgICAgICAgb3MucmVwbGFjZSh0bXAsIHNlbGYuZmlsZSkKICAgICAgICAgICAgaWYgb3MubmFtZSAhPSAnbnQnOgogICAgICAgICAgICAgICAgZGlyZWN0b3J5X2ZkID0gb3Mub3BlbihzZWxmLnJvb3QsIG9zLk9fUkRPTkxZIHwgZ2V0YXR0cihvcywgJ09fRElSRUNUT1JZJywgMCkpCiAgICAgICAgICAgICAgICB0cnk6IG9zLmZzeW5jKGRpcmVjdG9yeV9mZCkKICAgICAgICAgICAgICAgIGZpbmFsbHk6IG9zLmNsb3NlKGRpcmVjdG9yeV9mZCkKICAgICAgICBmaW5hbGx5OgogICAgICAgICAgICB0bXAudW5saW5rKG1pc3Npbmdfb2s9VHJ1ZSkKCkBjb250ZXh0bGliLmNvbnRleHRtYW5hZ2VyCmRlZiBleGNsdXNpdmUocm9vdCk6CiAgICBsb2NrID0gcm9vdCAvICdjbGllbnQubG9jaycKICAgIHJlcXVpcmUobm90IGxvY2suaXNfc3ltbGluaygpLCAnSW52YWxpZCBsb2NrIGZpbGUuJykKICAgIGZkID0gb3Mub3Blbihsb2NrLCBvcy5PX0NSRUFUIHwgb3MuT19SRFdSLCAwbzYwMCkKICAgIHRyeToKICAgICAgICBpZiBvcy5uYW1lID09ICdudCc6CiAgICAgICAgICAgIGltcG9ydCBtc3ZjcnQKICAgICAgICAgICAgb3Mud3JpdGUoZmQsIGInMCcpOyBvcy5sc2VlayhmZCwgMCwgMCk7IG1zdmNydC5sb2NraW5nKGZkLCBtc3ZjcnQuTEtfTkJMQ0ssIDEpCiAgICAgICAgZWxzZToKICAgICAgICAgICAgaW1wb3J0IGZjbnRsCiAgICAgICAgICAgIGZjbnRsLmZsb2NrKGZkLCBmY250bC5MT0NLX0VYIHwgZmNudGwuTE9DS19OQikKICAgICAgICB5aWVsZAogICAgZmluYWxseToKICAgICAgICBvcy5jbG9zZShmZCkKCmRlZiBhY2NlcHRfaGVsbG8oYywgcywgcGVlcik6CiAgICBleGFjdChwZWVyLCBbJ3ZlcnNpb24nLCAncm9sZScsICdwaW5zJywgJ2NoYWxsZW5nZScsICdwdWJsaWNfa2V5J10pCiAgICByZXF1aXJlKHBlZXJbJ3ZlcnNpb24nXSA9PSBWRVJTSU9OIGFuZCBwZWVyWydyb2xlJ10gPT0gJ3NlcnZlcicgYW5kIHBlZXJbJ3BpbnMnXSA9PSBwaW5zKGMpCiAgICAgICAgICAgIGFuZCBpc2luc3RhbmNlKHBlZXJbJ2NoYWxsZW5nZSddLCBzdHIpIGFuZCByZS5mdWxsbWF0Y2goJ1swLTlhLWZdezMyfScsIHBlZXJbJ2NoYWxsZW5nZSddKSwgJ1NlcnZlciBwYWlyaW5nIGlkZW50aXR5IG1pc21hdGNoLicpCiAgICBleGFjdChwZWVyWydwdWJsaWNfa2V5J10sIFsnY3J2JywgJ3gnLCAneSddKQogICAgcmVxdWlyZShwZWVyWydwdWJsaWNfa2V5J11bJ2NydiddID09ICdQLTI1NicsICdVbnN1cHBvcnRlZCBrZXkgdHlwZS4nKQogICAgaGFzaGVzLCBzZXJpYWxpemF0aW9uLCBlYywgSEtERiwgXyA9IGNyeXB0bygpCiAgICBwdWJsaWMgPSBlYy5FbGxpcHRpY0N1cnZlUHVibGljTnVtYmVycyhpbnQuZnJvbV9ieXRlcyh1bmI2NChwZWVyWydwdWJsaWNfa2V5J11bJ3gnXSwgMzIpLCAnYmlnJyksCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgaW50LmZyb21fYnl0ZXModW5iNjQocGVlclsncHVibGljX2tleSddWyd5J10sIDMyKSwgJ2JpZycpLCBlYy5TRUNQMjU2UjEoKSkucHVibGljX2tleSgpCiAgICBwcml2YXRlID0gc2VyaWFsaXphdGlvbi5sb2FkX2Rlcl9wcml2YXRlX2tleSh1bmI2NChzLnZhbHVlWydwcml2YXRlJ10pLCBwYXNzd29yZD1Ob25lKQogICAgdHJhbnNjcmlwdCA9IGhhc2hsaWIuc2hhMjU2KGNhbm9uaWNhbChzLnZhbHVlWydoZWxsbyddKSArIGNhbm9uaWNhbChwZWVyKSkuZGlnZXN0KCkKICAgICMgRXhwbGljaXQgY291bnRlcnBhcnQgb2YgLk5FVCBEZXJpdmVLZXlGcm9tSGFzaChwZWVyLCBTSEEyNTYpLgogICAgc2VjcmV0ID0gaGFzaGxpYi5zaGEyNTYocHJpdmF0ZS5leGNoYW5nZShlYy5FQ0RIKCksIHB1YmxpYykpLmRpZ2VzdCgpCiAgICBrZXlzID0gSEtERihhbGdvcml0aG09aGFzaGVzLlNIQTI1NigpLCBsZW5ndGg9MTI4LCBzYWx0PXRyYW5zY3JpcHQsIGluZm89YidzY3gtZ2gvbTIva2V5cy92MScpLmRlcml2ZShzZWNyZXQpCiAgICBzLnZhbHVlLnVwZGF0ZShzZXJ2ZXJfaGVsbG89cGVlciwgdHJhbnNjcmlwdD1iNjQodHJhbnNjcmlwdCksIGtleXM9YjY0KGtleXMpKQogICAgcy5zYXZlKCkKICAgIHJldHVybiB0cmFuc2NyaXB0LmhleCgpLnVwcGVyKCkKCmRlZiBjb25uZWN0KGMsIHMsIGJveCk6CiAgICBpZiAnaGVsbG8nIG5vdCBpbiBzLnZhbHVlOgogICAgICAgIF8sIHNlcmlhbGl6YXRpb24sIGVjLCBfLCBfID0gY3J5cHRvKCkKICAgICAgICBwcml2YXRlID0gZWMuZ2VuZXJhdGVfcHJpdmF0ZV9rZXkoZWMuU0VDUDI1NlIxKCkpCiAgICAgICAgcHVibGljID0gcHJpdmF0ZS5wdWJsaWNfa2V5KCkucHVibGljX251bWJlcnMoKQogICAgICAgIHMudmFsdWVbJ3ByaXZhdGUnXSA9IGI2NChwcml2YXRlLnByaXZhdGVfYnl0ZXMoc2VyaWFsaXphdGlvbi5FbmNvZGluZy5ERVIsIHNlcmlhbGl6YXRpb24uUHJpdmF0ZUZvcm1hdC5QS0NTOCwgc2VyaWFsaXphdGlvbi5Ob0VuY3J5cHRpb24oKSkpCiAgICAgICAgcy52YWx1ZVsnaGVsbG8nXSA9IHsndmVyc2lvbic6IFZFUlNJT04sICdyb2xlJzogJ2NsaWVudCcsICdwaW5zJzogcGlucyhjKSwgJ2NoYWxsZW5nZSc6IHNlY3JldHMudG9rZW5faGV4KDE2KSwKICAgICAgICAgICAgICAgICAgICAgICAgICAgICdwdWJsaWNfa2V5JzogeydjcnYnOiAnUC0yNTYnLCAneCc6IGI2NChwdWJsaWMueC50b19ieXRlcygzMiwgJ2JpZycpKSwgJ3knOiBiNjQocHVibGljLnkudG9fYnl0ZXMoMzIsICdiaWcnKSl9fQogICAgICAgIHMuc2F2ZSgpCiAgICByYXcgPSBib3gucmVhZCgnaGVsbG8nLCAnc2VydmVyJykKICAgIHJlcXVpcmUocmF3IGlzIG5vdCBOb25lLCAnVGhlIGxvY2FsIHNlc3Npb24gaXMgbm90IHJlYWR5LiBLZWVwIHRoZSBhcHAgb3BlbjsgZG8gbm90IGd1ZXNzIGFub3RoZXIgZGlyZWN0b3J5LicpCiAgICBwZWVyID0gbG9hZF9qc29uKHJhdykKICAgIGlmICdzZXJ2ZXJfaGVsbG8nIGluIHMudmFsdWU6CiAgICAgICAgcmVxdWlyZShwZWVyID09IHMudmFsdWVbJ3NlcnZlcl9oZWxsbyddLCAnU2VydmVyIHBhaXJpbmcgZGF0YSBjaGFuZ2VkLiBTdG9wIGFuZCByZXF1ZXN0IGEgbmV3IHNlc3Npb24uJykKICAgIGNvZGUgPSBhY2NlcHRfaGVsbG8oYywgcywgcGVlcikKICAgIGJveC5jcmVhdGUoJ2hlbGxvJywgJ2NsaWVudCcsIGNhbm9uaWNhbChzLnZhbHVlWydoZWxsbyddKSkKICAgIHByaW50KCfphY3lr7nnn63noIHvvJonICsgY29kZVs6NF0gKyAnICcgKyBjb2RlWzQ6OF0pCiAgICBwcmludCgn5a6M5pW056Gu6K6k56CB77yaJyArICcgJy5qb2luKGNvZGVbaTppKzRdIGZvciBpIGluIHJhbmdlKDAsIDY0LCA0KSkpCiAgICBwcmludCgn5oqKIDgg5L2N55+t56CB5ZGK6K+J55So5oi377yM54S25ZCO6L+Q6KGMIGFjdGl2YXRlIC0td2FpdCA5MDDvvJrlroPkvJroh6rliqjnrYnlvoXmnKzmnLrnoa7orqTvvIjkv6Hku7vku5PlupPml7bml6DpnIDnlKjmiLfmk43kvZzvvInlubblrozmiJDliJ3lp4vljJbvvJvkuYvlkI7nlKggY2FsbCA85bel5YW35ZCNPiAtLWFyZ3VtZW50cyA8SlNPTj4g6LCD55So5bel5YW377yM57uT5p6c5LiN5piO5pe25Y+q6L+Q6KGMIHN0YXR1cyAtLXdhaXQgMzAw77yI5Lya6K+d5piv6ZW/5pyf55qE77yM562J5b6F5a6h5om55pyf6Ze05LiN6KaB6YeN5paw6L+e5o6l77yJ44CCJykKCmRlZiBzZWFsKGMsIHMsIG9wZXJhdGlvbiwgcGF5bG9hZCwgbGlmZXRpbWU9MTgwMCk6CiAgICBfLCBfLCBfLCBfLCBBRVNHQ00gPSBjcnlwdG8oKQogICAgcmVxdWlyZShsZW4ocGF5bG9hZCkgPD0gNjU1MzYsICdSZXF1ZXN0IGV4Y2VlZHMgdGhlIG1lc3NhZ2UgbGltaXQuJykKICAgIG5vbmNlID0gc2VjcmV0cy50b2tlbl9ieXRlcygxMikKICAgIHVzZWQgPSBzLnZhbHVlLnNldGRlZmF1bHQoJ3R4X25vbmNlcycsIFtdKQogICAgcmVxdWlyZShiNjQobm9uY2UpIG5vdCBpbiB1c2VkLCAnTm9uY2UgY29sbGlzaW9uOyBzdG9wIHRoaXMgc2Vzc2lvbi4nKQogICAgdXNlZC5hcHBlbmQoYjY0KG5vbmNlKSkKICAgICMgMzAtbWludXRlIHdpbmRvdzogdGhlIGhvc3QgbWF5IHJlYWQgdGhpcyBvbmx5IGFmdGVyIGEgbG9uZyBhcHByb3ZhbCBvciBhIEdpdEh1YiBvdXRhZ2U7IHJlcGxheSBpcyBibG9ja2VkIGJ5IGlkcy9ub25jZXMuCiAgICBoZWFkZXIgPSBkaWN0KHBpbnMoYyksIHJlcXVlc3RfaWQ9b3BlcmF0aW9uLCBkaXJlY3Rpb249J3JlcScsIGV4cGlyZXM9aW50KHRpbWUudGltZSgpKSArIGxpZmV0aW1lKQogICAgZW5jcnlwdGVkID0gQUVTR0NNKHVuYjY0KHMudmFsdWVbJ2tleXMnXSwgMTI4KVs6MzJdKS5lbmNyeXB0KG5vbmNlLCBwYXlsb2FkLCBjYW5vbmljYWwoaGVhZGVyKSkKICAgIHJldHVybiB7J2hlYWRlcic6IGhlYWRlciwgJ25vbmNlJzogYjY0KG5vbmNlKSwgJ2NpcGhlcnRleHQnOiBiNjQoZW5jcnlwdGVkWzotMTZdKSwgJ3RhZyc6IGI2NChlbmNyeXB0ZWRbLTE2Ol0pfQoKZGVmIG9wZW5fcmVzcG9uc2UoYywgcywgb3BlcmF0aW9uLCBlbnYpOgogICAgZXhhY3QoZW52LCBbJ2hlYWRlcicsICdub25jZScsICdjaXBoZXJ0ZXh0JywgJ3RhZyddKQogICAgZXhhY3QoZW52WydoZWFkZXInXSwgbGlzdChwaW5zKGMpKSArIFsncmVxdWVzdF9pZCcsICdkaXJlY3Rpb24nLCAnZXhwaXJlcyddKQogICAgaCA9IGVudlsnaGVhZGVyJ10KICAgIHJlcXVpcmUoYWxsKGhba10gPT0gdiBmb3IgaywgdiBpbiBwaW5zKGMpLml0ZW1zKCkpIGFuZCBoWydyZXF1ZXN0X2lkJ10gPT0gb3BlcmF0aW9uIGFuZCBoWydkaXJlY3Rpb24nXSA9PSAncmVzJywgJ1Jlc3BvbnNlIGJpbmRpbmcgbWlzbWF0Y2guJykKICAgIHJlcXVpcmUodHlwZShoWydleHBpcmVzJ10pIGlzIGludCBhbmQgdGltZS50aW1lKCkgLSAxIDw9IGhbJ2V4cGlyZXMnXSA8PSB0aW1lLnRpbWUoKSArIDE4MDEsICdSZXNwb25zZSBleHBpcmVkOyBkbyBub3QgcmVwZWF0IHRoZSBvcGVyYXRpb24uJykKICAgIG5vbmNlID0gdW5iNjQoZW52Wydub25jZSddLCAxMikKICAgIHJlcXVpcmUoYjY0KG5vbmNlKSBub3QgaW4gcy52YWx1ZS5nZXQoJ3J4X25vbmNlcycsIFtdKSwgJ1Jlc3BvbnNlIG5vbmNlIHdhcyBhbHJlYWR5IGFjY2VwdGVkLicpCiAgICBfLCBfLCBfLCBfLCBBRVNHQ00gPSBjcnlwdG8oKQogICAgY2lwaGVydGV4dCA9IHVuYjY0KGVudlsnY2lwaGVydGV4dCddKQogICAgcmVxdWlyZShsZW4oY2lwaGVydGV4dCkgPD0gNjU1MzYsICdSZXNwb25zZSBleGNlZWRzIHRoZSBsaW1pdC4nKQogICAgcGxhaW4gPSBBRVNHQ00odW5iNjQocy52YWx1ZVsna2V5cyddLCAxMjgpWzMyOjY0XSkuZGVjcnlwdChub25jZSwgY2lwaGVydGV4dCArIHVuYjY0KGVudlsndGFnJ10sIDE2KSwgY2Fub25pY2FsKGgpKQogICAgaWYgcGxhaW4uc3RhcnRzd2l0aChiJ1NDWDJaXDAnKToKICAgICAgICBkZWNvZGVyID0gemxpYi5kZWNvbXByZXNzb2JqKCkKICAgICAgICBwbGFpbiA9IGRlY29kZXIuZGVjb21wcmVzcyhwbGFpbls2Ol0sIDFfMDAwXzAwMSkKICAgICAgICByZXF1aXJlKGxlbihwbGFpbikgPD0gMV8wMDBfMDAwIGFuZCBkZWNvZGVyLmVvZiBhbmQgbm90IGRlY29kZXIudW51c2VkX2RhdGEgYW5kIG5vdCBkZWNvZGVyLnVuY29uc3VtZWRfdGFpbCwKICAgICAgICAgICAgICAgICdDb21wcmVzc2VkIHJlc3VsdCBleGNlZWRzIGl0cyBsaW1pdCBvciBpcyBtYWxmb3JtZWQuJykKICAgIHJlc3VsdCA9IGxvYWRfanNvbihwbGFpbiwgbGltaXQ9MV8wMDBfMDAwKQogICAgZXhhY3QocmVzdWx0LCBbJ3Byb2plY3RfaWQnLCAncmVwbHknXSkKICAgIHJlcXVpcmUocmVzdWx0Wydwcm9qZWN0X2lkJ10gPT0gY1sncHJvamVjdF9pZCddLCAnVGhlIHJlc3BvbmRpbmcgcHJvamVjdCBpcyBub3QgdGhlIHNlbGVjdGVkIHByb2plY3QuJykKICAgIHMudmFsdWUuc2V0ZGVmYXVsdCgncnhfbm9uY2VzJywgW10pLmFwcGVuZChiNjQobm9uY2UpKQogICAgcmV0dXJuIHJlc3VsdFsncmVwbHknXQoKZGVmIGF1dGhlbnRpY2F0ZV9leHBpcmVkX3JlbmV3YWwoYywgcywgb3BlcmF0aW9uLCBlbnZlbG9wZSk6CiAgICAiIiJPbmx5IGNsYXNzaWZ5IGFuIGF1dGhlbnRpY2F0ZWQgZXhwaXJlZCB0b2tlbiBzbG90LiBOZXZlciBpbnN0YWxsIGl0cyB0b2tlbiBvciBhY2NlcHQgaXRzIG93bmVyc2hpcCBwcm9vZi4iIiIKICAgIHRyeToKICAgICAgICByZXF1aXJlKHJlLmZ1bGxtYXRjaChyJ3Rva2VuLVswLTldezZ9Jywgb3BlcmF0aW9uKSwgJ05vdCBhIHJlbmV3YWwgaGlzdG9yeSBzbG90LicpCiAgICAgICAgZXhhY3QoZW52ZWxvcGUsIFsnaGVhZGVyJywgJ25vbmNlJywgJ2NpcGhlcnRleHQnLCAndGFnJ10pCiAgICAgICAgaCA9IGVudmVsb3BlWydoZWFkZXInXQogICAgICAgIGV4YWN0KGgsIGxpc3QocGlucyhjKSkgKyBbJ3JlcXVlc3RfaWQnLCAnZGlyZWN0aW9uJywgJ2V4cGlyZXMnXSkKICAgICAgICByZXF1aXJlKGFsbChoW2tdID09IHYgZm9yIGssIHYgaW4gcGlucyhjKS5pdGVtcygpKSBhbmQgaFsncmVxdWVzdF9pZCddID09IG9wZXJhdGlvbiBhbmQgaFsnZGlyZWN0aW9uJ10gPT0gJ3JlcycsICdSZW5ld2FsIGhpc3RvcnkgYmluZGluZyBtaXNtYXRjaC4nKQogICAgICAgIHJlcXVpcmUodHlwZShoWydleHBpcmVzJ10pIGlzIGludCBhbmQgMCA8IGhbJ2V4cGlyZXMnXSA8IHRpbWUudGltZSgpIC0gMSwgJ05vdCBleHBpcmVkIHJlbmV3YWwgaGlzdG9yeS4nKQogICAgICAgIG5vbmNlID0gdW5iNjQoZW52ZWxvcGVbJ25vbmNlJ10sIDEyKQogICAgICAgIHJlcXVpcmUoYjY0KG5vbmNlKSBub3QgaW4gcy52YWx1ZS5nZXQoJ3J4X25vbmNlcycsIFtdKSwgJ1JlbmV3YWwgaGlzdG9yeSBub25jZSBhbHJlYWR5IGNvbnN1bWVkLicpCiAgICAgICAgY2lwaGVydGV4dCA9IHVuYjY0KGVudmVsb3BlWydjaXBoZXJ0ZXh0J10pCiAgICAgICAgcmVxdWlyZShsZW4oY2lwaGVydGV4dCkgPD0gNjU1MzYsICdSZW5ld2FsIGhpc3RvcnkgZXhjZWVkcyBsaW1pdC4nKQogICAgICAgIF8sIF8sIF8sIF8sIEFFU0dDTSA9IGNyeXB0bygpCiAgICAgICAgcGxhaW4gPSBBRVNHQ00odW5iNjQocy52YWx1ZVsna2V5cyddLCAxMjgpWzMyOjY0XSkuZGVjcnlwdChub25jZSwgY2lwaGVydGV4dCArIHVuYjY0KGVudmVsb3BlWyd0YWcnXSwgMTYpLCBjYW5vbmljYWwoaCkpCiAgICAgICAgYm9keSA9IGxvYWRfanNvbihwbGFpbikKICAgICAgICBleGFjdChib2R5LCBbJ3Byb2plY3RfaWQnLCAncmVwbHknXSkKICAgICAgICB0b2tlbiA9IGJvZHlbJ3JlcGx5J10uZ2V0KCd0b2tlbicpIGlmIGlzaW5zdGFuY2UoYm9keVsncmVwbHknXSwgZGljdCkgZWxzZSBOb25lCiAgICAgICAgcmVxdWlyZShib2R5Wydwcm9qZWN0X2lkJ10gPT0gY1sncHJvamVjdF9pZCddIGFuZCBpc2luc3RhbmNlKHRva2VuLCBzdHIpIGFuZCA4IDw9IGxlbih0b2tlbikgPD0gNDA5NgogICAgICAgICAgICAgICAgYW5kIG5vdCBhbnkoeC5pc3NwYWNlKCkgZm9yIHggaW4gdG9rZW4pLCAnSW52YWxpZCByZW5ld2FsIGhpc3RvcnkuJykKICAgICAgICBzLnZhbHVlLnNldGRlZmF1bHQoJ3J4X25vbmNlcycsIFtdKS5hcHBlbmQoYjY0KG5vbmNlKSkKICAgICAgICByZXR1cm4gVHJ1ZQogICAgZXhjZXB0IEV4Y2VwdGlvbjoKICAgICAgICByZXR1cm4gRmFsc2UgICMgVW5hdXRoZW50aWNhdGVkL21hbGZvcm1lZCBvY2N1cGFuY3kgbXVzdCBuZXZlciBhZHZhbmNlIHRoZSBjdXJzb3IuCgoKZGVmIGFwcGx5X3Rva2VuX3JlbmV3KGMsIHMsIGJveCwgdGlwPU5vbmUpOgogICAgIiIiVGhlIGRlc2t0b3AgcHVibGlzaGVzIHJlbmV3L3Rva2VuLU5OTi5qc29uIChzZWFsZWQgbGlrZSBhIHJlc3BvbnNlIHdpdGggcmVxdWVzdF9pZCAndG9rZW4tTk5OJykgYmVmb3JlIHRoZSBwYXN0ZWQKICAgIHRva2VuIGV4cGlyZXMuIE9ubHkgbWVhbmluZ2Z1bCB3aGVuIHRoaXMgQWdlbnQgYXV0aGVudGljYXRlZCB3aXRoIGEgcGFzdGVkIHRva2VuOyBvdGhlcndpc2UgaWdub3JlZC4gSWRlbXBvdGVudCBieSBpbmRleC4iIiIKICAgIGlmIG9zLmVudmlyb24uZ2V0KCdMRUJPWF9BVVRIX1NPVVJDRScsIG9zLmVudmlyb24uZ2V0KCdTQ1hfQVVUSF9TT1VSQ0UnKSkgbm90IGluICgncGFzdGVkLXRva2VuJywgJ2RldmljZS1mbG93JykgYW5kIG5vdCBzLnZhbHVlLmdldCgncmVuZXdlZCcpIGFuZCBub3Qgcy52YWx1ZS5nZXQoJ3JlY292ZXJ5X2NvbnRleHQnKToKICAgICAgICByZXR1cm4KICAgIGZvciBfIGluIHJhbmdlKDE2KToKICAgICAgICBpbmRleCA9IHMudmFsdWUuZ2V0KCdyZW5ld19pbmRleCcsIDApCiAgICAgICAgcmVxdWlyZSh0eXBlKGluZGV4KSBpcyBpbnQgYW5kIDAgPD0gaW5kZXggPCAxXzAwMF8wMDAsICdSZW5ld2FsIGN1cnNvciBpcyBpbnZhbGlkIG9yIGV4aGF1c3RlZC4nKQogICAgICAgIG9wID0gJ3Rva2VuLSUwNmQnICUgaW5kZXgKICAgICAgICB0cnk6CiAgICAgICAgICAgIHJhdyA9IGJveC5hdCh0aXAsIG1lc3NhZ2VfcGF0aChjLCAncmVuZXcnLCBvcCkpIGlmIHRpcCBlbHNlIGJveC5yZWFkKCdyZW5ldycsIG9wKQogICAgICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAgICAgIHJldHVybgogICAgICAgIGlmIHJhdyBpcyBOb25lOgogICAgICAgICAgICByZXR1cm4KICAgICAgICBlbnZlbG9wZSA9IGxvYWRfanNvbihyYXcpCiAgICAgICAgdHJ5OgogICAgICAgICAgICByZXBseSA9IG9wZW5fcmVzcG9uc2UoYywgcywgb3AsIGVudmVsb3BlKQogICAgICAgICAgICBleHBpcnkgPSByZXBseS5nZXQoJ2V4cGlyZXNfYXQnLCAwKSBpZiBpc2luc3RhbmNlKHJlcGx5LCBkaWN0KSBlbHNlIE5vbmUKICAgICAgICAgICAgcmVxdWlyZSh0eXBlKGV4cGlyeSkgaXMgaW50IGFuZCBleHBpcnkgPj0gMCwgJ0ludmFsaWQgcmVuZXdhbCBjcmVkZW50aWFsIGV4cGlyeS4nKQogICAgICAgICAgICBpZiBleHBpcnkgYW5kIGV4cGlyeSA8PSB0aW1lLnRpbWUoKToKICAgICAgICAgICAgICAgIHMudmFsdWVbJ3JlbmV3X2luZGV4J10gPSBpbmRleCArIDEKICAgICAgICAgICAgICAgIHMuc2F2ZSgpICAjIEZyZXNoIHdyYXBwZXIgZG9lcyBub3QgbWFrZSBhbiBleHBpcmVkIGNyZWRlbnRpYWwgdXNhYmxlLgogICAgICAgICAgICAgICAgY29udGludWUKICAgICAgICAgICAgYnJlYWsKICAgICAgICBleGNlcHQgU3RvcDoKICAgICAgICAgICAgaWYgbm90IGF1dGhlbnRpY2F0ZV9leHBpcmVkX3JlbmV3YWwoYywgcywgb3AsIGVudmVsb3BlKToKICAgICAgICAgICAgICAgIHJldHVybgogICAgICAgICAgICBzLnZhbHVlWydyZW5ld19pbmRleCddID0gaW5kZXggKyAxCiAgICAgICAgICAgIHMuc2F2ZSgpICAjIEF1dGhlbnRpY2F0ZWQgZXhwaXJlZCBoaXN0b3J5IGlzIHNraXBwZWQsIG5vdCB1c2VkIGFzIGEgY3JlZGVudGlhbCBvciBvd25lcnNoaXAgZ3JhbnQuCiAgICBlbHNlOgogICAgICAgIHJldHVybgogICAgdG9rZW4gPSByZXBseS5nZXQoJ3Rva2VuJywgJycpIGlmIGlzaW5zdGFuY2UocmVwbHksIGRpY3QpIGVsc2UgJycKICAgIGlmIG5vdCAoaXNpbnN0YW5jZSh0b2tlbiwgc3RyKSBhbmQgOCA8PSBsZW4odG9rZW4pIDw9IDQwOTYgYW5kIG5vdCBhbnkoY2guaXNzcGFjZSgpIGZvciBjaCBpbiB0b2tlbikpOgogICAgICAgIHJldHVybgogICAgb3duZXJzaGlwID0gcmVwbHkuZ2V0KCdyZWNvdmVyeV9vd25lcnNoaXAnKQogICAgaWYgb3duZXJzaGlwIGlzIG5vdCBOb25lOgogICAgICAgIHZhbGlkYXRlX3JlY292ZXJ5X293bmVyc2hpcChjLCBzLCBvd25lcnNoaXApCiAgICAgICAgcy52YWx1ZVsncmVjb3Zlcnlfb3duZXJzaGlwJ10gPSBvd25lcnNoaXAKICAgIHMudmFsdWVbJ3JlbmV3X2luZGV4J10gPSBpbmRleCArIDEKICAgIHMudmFsdWVbJ3JlbmV3ZWQnXSA9IFRydWUKICAgIHMudmFsdWVbJ3JlbmV3ZWRfdG9rZW4nXSA9IHRva2VuCiAgICBzLnNhdmUoKQogICAgb3MuZW52aXJvblsnR0lUSFVCX1RPS0VOJ10gPSB0b2tlbgogICAgb3MuZW52aXJvblsnTEVCT1hfVE9LRU4nXSA9IHRva2VuCiAgICBpZiBzaHV0aWwud2hpY2goJ2dpdCcpOgogICAgICAgIHN1YnByb2Nlc3MucnVuKFsnZ2l0JywgJ2NyZWRlbnRpYWwnLCAnYXBwcm92ZSddLCBpbnB1dD0ncHJvdG9jb2w9aHR0cHNcbmhvc3Q9Z2l0aHViLmNvbVxudXNlcm5hbWU9eC1hY2Nlc3MtdG9rZW5cbnBhc3N3b3JkPSVzXG5cbicgJSB0b2tlbiwgdGV4dD1UcnVlLCBjYXB0dXJlX291dHB1dD1UcnVlKQogICAgaWYgaXNpbnN0YW5jZShib3gsIEFwaU1haWxib3gpOgogICAgICAgIGJveC5fdG9rZW4gPSB0b2tlbgogICAgcHJpbnQoanNvbi5kdW1wcyh7J3N0YXR1cyc6ICd0b2tlbl9yZW5ld2VkJywgJ2V4cGlyZXNfYXQnOiByZXBseS5nZXQoJ2V4cGlyZXNfYXQnLCAwKX0pLCBmaWxlPXN5cy5zdGRlcnIpCiAgICByZXRyeV9yZWNvdmVyeV9wdWJsaWNhdGlvbihjLCBzKQoKCiMgTEJSMSBpcyBlbWJlZGRlZCBpbiB0aGlzIHBpbm5lZCBjbGllbnQ6IG5ldmVyIGV4ZWN1dGUgYSByZWNvdmVyeSBoZWxwZXIgZnJvbSBjd2QvUFlUSE9OUEFUSC4KZGVmIHJlY292ZXJ5X2tleSh0ZXh0KToKICAgIG1hdGNoID0gcmUuZnVsbG1hdGNoKHInKD86TEVCT1hfUkVDT1ZFUlk9KT9sZWJveDEtKFswLTlhLWZdezMyfSktKFtBLVphLXowLTlfLV17NDN9KScsIHRleHQuc3RyaXAoKSkKICAgIHJlcXVpcmUobWF0Y2ggaXMgbm90IE5vbmUsICdSRUNPVkVSWV9LRVlfRk9STUFUJykKICAgIGtleSA9IGJhc2U2NC51cmxzYWZlX2I2NGRlY29kZShtYXRjaFsyXSArICc9JykKICAgIHJlcXVpcmUobGVuKGtleSkgPT0gMzIgYW5kIGJhc2U2NC51cmxzYWZlX2I2NGVuY29kZShrZXkpLnJzdHJpcChiJz0nKS5kZWNvZGUoKSA9PSBtYXRjaFsyXSwgJ1JFQ09WRVJZX0tFWV9GT1JNQVQnKQogICAgcmV0dXJuIG1hdGNoWzFdLCBrZXkKCgpkZWYgdmFsaWRhdGVfcmVjb3Zlcnlfb3duZXJzaGlwKGMsIHMsIHByb29mKToKICAgIGV4YWN0KHByb29mLCBbJ2Zvcm1hdCcsICdwcm9qZWN0X2lkJywgJ3JlcG9zaXRvcnknLCAncmVwb19pZCcsICdicmFuY2gnLCAnYmluZGluZycsICdlcG9jaCcsICd0b29scycsICdyaWQnLCAnY2lwaGVydGV4dF9zaGEyNTYnXSkKICAgIHJlcXVpcmUocHJvb2ZbJ2Zvcm1hdCddID09ICdsZWJveC1hZ2VudC1vd25lcnNoaXAtdjEnLCAnUkVDT1ZFUllfT1dORVJTSElQX0ZPUk1BVCcpCiAgICBjb250ZXh0ID0gcy52YWx1ZS5nZXQoJ3JlY292ZXJ5X2NvbnRleHQnLCB7fSkKICAgIHJlYyA9IGNvbnRleHQuZ2V0KCdyZWNvdmVyeScpIG9yIG9zLmVudmlyb24uZ2V0KCdMRUJPWF9SRUNPVkVSWV9TRUxGJywgJycpCiAgICB0b29scyA9IGNvbnRleHQuZ2V0KCd0b29scycpIG9yIG9zLmVudmlyb24uZ2V0KCdMRUJPWF9UT09MUycsICcnKQogICAgcmlkLCBfID0gcmVjb3Zlcnlfa2V5KHJlYykKICAgIHJlcXVpcmUoYWxsKHByb29mW2ZpZWxkXSA9PSBjW2ZpZWxkXSBmb3IgZmllbGQgaW4gKCdwcm9qZWN0X2lkJywgJ3JlcG9zaXRvcnknLCAncmVwb19pZCcsICdicmFuY2gnLCAnYmluZGluZycsICdlcG9jaCcpKQogICAgICAgICAgICBhbmQgcHJvb2ZbJ3Rvb2xzJ10gPT0gdG9vbHMgYW5kIHByb29mWydyaWQnXSA9PSByaWQgYW5kIGlzaW5zdGFuY2UocHJvb2ZbJ2NpcGhlcnRleHRfc2hhMjU2J10sIHN0cikKICAgICAgICAgICAgYW5kIHJlLmZ1bGxtYXRjaCgnW2EtZjAtOV17NjR9JywgcHJvb2ZbJ2NpcGhlcnRleHRfc2hhMjU2J10pLCAnUkVDT1ZFUllfT1dORVJTSElQX1NDT1BFJykKCgpkZWYgcmVjb3ZlcnlfbGVnYWN5X293bmVkKGMsIHMsIG9sZCwgY2lwaGVydGV4dCk6CiAgICBpZiBvbGQuZ2V0KCdieScpID09ICdhZ2VudCc6CiAgICAgICAgcmV0dXJuIFRydWUKICAgICMgQW4gZXhwbGljaXQgZGlmZmVyZW50IG93bmVyIGNhbiBuZXZlciBiZSByZWNsYXNzaWZpZWQsIGV2ZW4gd2l0aCBhbiBvbGRlciBhdHRlc3RhdGlvbi4KICAgIGlmICdieScgaW4gb2xkOgogICAgICAgIHJldHVybiBGYWxzZQogICAgcHJvb2YgPSBzLnZhbHVlLmdldCgncmVjb3Zlcnlfb3duZXJzaGlwJykKICAgIGlmIHByb29mIGlzIE5vbmU6CiAgICAgICAgcmV0dXJuIEZhbHNlCiAgICB2YWxpZGF0ZV9yZWNvdmVyeV9vd25lcnNoaXAoYywgcywgcHJvb2YpCiAgICByZXR1cm4gaG1hYy5jb21wYXJlX2RpZ2VzdChwcm9vZlsnY2lwaGVydGV4dF9zaGEyNTYnXSwgaGFzaGxpYi5zaGEyNTYoY2lwaGVydGV4dCkuaGV4ZGlnZXN0KCkpCgoKZGVmIHJlY292ZXJ5X2NpcGhlcihrZXksIHZhbHVlLCBkZWNyeXB0PUZhbHNlKToKICAgIGVuYyA9IGhtYWMubmV3KGtleSwgYidsZWJveC1yZWNvdmVyeS1lbmMnLCBoYXNobGliLnNoYTI1NikuZGlnZXN0KCkKICAgIG1hYyA9IGhtYWMubmV3KGtleSwgYidsZWJveC1yZWNvdmVyeS1tYWMnLCBoYXNobGliLnNoYTI1NikuZGlnZXN0KCkKICAgIGlmIGRlY3J5cHQ6CiAgICAgICAgcmVxdWlyZSg1MiA8PSBsZW4odmFsdWUpIDw9IDE2XzAwMCBhbmQgdmFsdWVbOjRdID09IGInTEJSMScsICdSRUNPVkVSWV9FTlZFTE9QRScpCiAgICAgICAgcmVxdWlyZShobWFjLmNvbXBhcmVfZGlnZXN0KHZhbHVlWy0zMjpdLCBobWFjLm5ldyhtYWMsIHZhbHVlWzotMzJdLCBoYXNobGliLnNoYTI1NikuZGlnZXN0KCkpLCAnUkVDT1ZFUllfT1dORVJfVU5WRVJJRklFRCcpCiAgICAgICAgbm9uY2UsIGRhdGEgPSB2YWx1ZVs0OjIwXSwgdmFsdWVbMjA6LTMyXQogICAgZWxzZToKICAgICAgICByZXF1aXJlKGxlbih2YWx1ZSkgPD0gMTVfOTQ4LCAnUkVDT1ZFUllfRU5WRUxPUEUnKQogICAgICAgIG5vbmNlLCBkYXRhID0gc2VjcmV0cy50b2tlbl9ieXRlcygxNiksIHZhbHVlCiAgICBzdHJlYW0gPSBiJycuam9pbihobWFjLm5ldyhlbmMsIG5vbmNlICsgaS50b19ieXRlcyg0LCAnYmlnJyksIGhhc2hsaWIuc2hhMjU2KS5kaWdlc3QoKSBmb3IgaSBpbiByYW5nZSgobGVuKGRhdGEpICsgMzEpIC8vIDMyKSkKICAgIHRyYW5zZm9ybWVkID0gYnl0ZXMoYSBeIGIgZm9yIGEsIGIgaW4gemlwKGRhdGEsIHN0cmVhbSkpCiAgICBpZiBkZWNyeXB0OgogICAgICAgIHJldHVybiB0cmFuc2Zvcm1lZAogICAgYm9keSA9IGInTEJSMScgKyBub25jZSArIHRyYW5zZm9ybWVkCiAgICByZXR1cm4gYm9keSArIGhtYWMubmV3KG1hYywgYm9keSwgaGFzaGxpYi5zaGEyNTYpLmRpZ2VzdCgpCgoKY2xhc3MgX1JlY292ZXJ5Tm9SZWRpcmVjdCh1cmxsaWIucmVxdWVzdC5IVFRQUmVkaXJlY3RIYW5kbGVyKToKICAgIGRlZiByZWRpcmVjdF9yZXF1ZXN0KHNlbGYsICphcmdzLCAqKmt3YXJncyk6CiAgICAgICAgcmFpc2UgU3RvcCgnUkVDT1ZFUllfUkVESVJFQ1RfUkVGVVNFRCcpCgoKZGVmIHJlY292ZXJ5X2h0dHAodG9rZW4sIHBhdGgsIGJvZHk9Tm9uZSk6CiAgICBoZWFkZXJzID0geydBdXRob3JpemF0aW9uJzogJ0JlYXJlciAnICsgdG9rZW4sICdBY2NlcHQnOiAnYXBwbGljYXRpb24vdm5kLmdpdGh1Yitqc29uJywKICAgICAgICAgICAgICAgJ1gtR2l0SHViLUFwaS1WZXJzaW9uJzogJzIwMjItMTEtMjgnLCAnVXNlci1BZ2VudCc6ICdsZWJveC1hZ2VudC8xLjAnLCAnQ29udGVudC1UeXBlJzogJ2FwcGxpY2F0aW9uL2pzb24nfQogICAgcmVxdWVzdCA9IHVybGxpYi5yZXF1ZXN0LlJlcXVlc3QoJ2h0dHBzOi8vYXBpLmdpdGh1Yi5jb20nICsgcGF0aCwgaGVhZGVycz1oZWFkZXJzLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBkYXRhPU5vbmUgaWYgYm9keSBpcyBOb25lIGVsc2UgY2Fub25pY2FsKGJvZHkpLCBtZXRob2Q9J0dFVCcgaWYgYm9keSBpcyBOb25lIGVsc2UgJ1BVVCcpCiAgICBvcGVuZXIgPSB1cmxsaWIucmVxdWVzdC5idWlsZF9vcGVuZXIoX1JlY292ZXJ5Tm9SZWRpcmVjdCgpKQogICAgd2l0aCBvcGVuZXIub3BlbihyZXF1ZXN0LCB0aW1lb3V0PTMwKSBhcyByZXNwb25zZToKICAgICAgICByZXF1aXJlKHJlc3BvbnNlLnN0YXR1cyBpbiAoKDIwMCwpIGlmIGJvZHkgaXMgTm9uZSBlbHNlICgyMDAsIDIwMSkpLCAnUkVDT1ZFUllfSFRUUF9TVEFUVVMnKQogICAgICAgIHJldHVybiBsb2FkX2pzb24ocmVzcG9uc2UucmVhZCgxMDBfMDAxKSkKCgpkZWYgcmVjb3ZlcnlfcmVtb3RlKHRva2VuLCB0b29scywgcmlkKToKICAgICMgQmluZCBhIHB1YmxpYyByZXBvc2l0b3J5IGFuZCBleGFjdCBkZWZhdWx0LWJyYW5jaCBjb21taXQuIEFuIGVycm9yLzQwNCBpcyBORVZFUiBpbnRlcnByZXRlZCBhcyBhYnNlbmNlLgogICAgcmVwb3NpdG9yeSA9IHJlY292ZXJ5X2h0dHAodG9rZW4sICcvcmVwb3MvJyArIHRvb2xzKQogICAgcmVxdWlyZShyZXBvc2l0b3J5LmdldCgncHJpdmF0ZScpIGlzIEZhbHNlIGFuZCByZXBvc2l0b3J5LmdldCgnZnVsbF9uYW1lJywgJycpLmxvd2VyKCkgPT0gdG9vbHMubG93ZXIoKQogICAgICAgICAgICBhbmQgdHlwZShyZXBvc2l0b3J5LmdldCgnaWQnKSkgaXMgaW50IGFuZCByZXBvc2l0b3J5WydpZCddID4gMCwgJ1JFQ09WRVJZX1JFUE9TSVRPUlknKQogICAgYnJhbmNoID0gcmVwb3NpdG9yeS5nZXQoJ2RlZmF1bHRfYnJhbmNoJywgJycpCiAgICByZXF1aXJlKGlzaW5zdGFuY2UoYnJhbmNoLCBzdHIpIGFuZCAxIDw9IGxlbihicmFuY2gpIDw9IDIwMCBhbmQgcmUuZnVsbG1hdGNoKHInW0EtWmEtejAtOS5fLy1dKycsIGJyYW5jaCkKICAgICAgICAgICAgYW5kICcuLicgbm90IGluIGJyYW5jaCBhbmQgYWxsKHAgYW5kIG5vdCBwLnN0YXJ0c3dpdGgoJy4nKSBhbmQgbm90IHAuZW5kc3dpdGgoJy5sb2NrJykgZm9yIHAgaW4gYnJhbmNoLnNwbGl0KCcvJykpLCAnUkVDT1ZFUllfQlJBTkNIJykKICAgIHJlZiA9IHJlY292ZXJ5X2h0dHAodG9rZW4sICcvcmVwb3MvJXMvZ2l0L3JlZi9oZWFkcy8lcycgJSAodG9vbHMsIHF1b3RlKGJyYW5jaCwgc2FmZT0nJykpKQogICAgcmVxdWlyZShyZWYuZ2V0KCdvYmplY3QnLCB7fSkuZ2V0KCd0eXBlJykgPT0gJ2NvbW1pdCcsICdSRUNPVkVSWV9DT01NSVQnKQogICAgdGlwID0gcmVmWydvYmplY3QnXS5nZXQoJ3NoYScsICcnKQogICAgcmVxdWlyZShpc2luc3RhbmNlKHRpcCwgc3RyKSBhbmQgcmUuZnVsbG1hdGNoKCdbYS1mMC05XXs0MH0nLCB0aXApLCAnUkVDT1ZFUllfQ09NTUlUJykKICAgIHBhdGggPSAncmVjb3ZlcnkvJXMuYmluJyAlIHJpZAogICAgZmlsZSA9IHJlY292ZXJ5X2h0dHAodG9rZW4sICcvcmVwb3MvJXMvY29udGVudHMvJXM/cmVmPSVzJyAlICh0b29scywgcGF0aCwgdGlwKSkKICAgIHJlcXVpcmUoZmlsZS5nZXQoJ3R5cGUnKSA9PSAnZmlsZScgYW5kIGZpbGUuZ2V0KCdlbmNvZGluZycpID09ICdiYXNlNjQnIGFuZCBmaWxlLmdldCgncGF0aCcpID09IHBhdGgsICdSRUNPVkVSWV9GSUxFX1RZUEUnKQogICAgZW5jb2RlZCA9IGZpbGUuZ2V0KCdjb250ZW50JywgJycpCiAgICByZXF1aXJlKGlzaW5zdGFuY2UoZW5jb2RlZCwgc3RyKSBhbmQgbGVuKGVuY29kZWQpIDw9IDMwXzAwMCwgJ1JFQ09WRVJZX1NJWkUnKQogICAgZGF0YSA9IHVuYjY0KGVuY29kZWQucmVwbGFjZSgnXG4nLCAnJykucmVwbGFjZSgnXHInLCAnJykpCiAgICBzaGEgPSBoYXNobGliLnNoYTEoYidibG9iICcgKyBzdHIobGVuKGRhdGEpKS5lbmNvZGUoKSArIGInXDAnICsgZGF0YSkuaGV4ZGlnZXN0KCkKICAgIHJlcXVpcmUodHlwZShmaWxlLmdldCgnc2l6ZScpKSBpcyBpbnQgYW5kIGZpbGVbJ3NpemUnXSA9PSBsZW4oZGF0YSkgYW5kIDEgPD0gbGVuKGRhdGEpIDw9IDE2XzAwMAogICAgICAgICAgICBhbmQgZmlsZS5nZXQoJ3NoYScpID09IHNoYSwgJ1JFQ09WRVJZX0NPTlRFTlRfSEFTSCcpCiAgICByZXR1cm4geydyZXBvc2l0b3J5X2lkJzogcmVwb3NpdG9yeVsnaWQnXSwgJ2JyYW5jaCc6IGJyYW5jaCwgJ3NoYSc6IHNoYSwgJ2RhdGEnOiBkYXRhfQoKCmRlZiByZXNlYWxfcmVjb3ZlcnkodG9rZW4sIGMsIHMpOgogICAgIiIiT25lIGNvbmRpdGlvbmFsIHdyaXRlIG9mIGR1cmFibGUgZXhhY3QgYnl0ZXMuIEEgdG9tYnN0b25lIGFsd2F5cyB3aW5zLCBpbmNsdWRpbmcgZHVyaW5nIHJlYWRiYWNrLgogICAgTGVnYWN5IGNpcGhlcnRleHQgd2l0aG91dCBleHBsaWNpdCBBZ2VudCBvd25lcnNoaXAgaXMgcmVhZC1vbmx5OyBtaXNzaW5nL2ZvcmVpZ24gYmxvYnMgYXJlIG5ldmVyIHJlcGxhY2VkLgogICAgQ2FsbGVyIGhvbGRzIHRoZSBvcmlnaW5hbCBzZXNzaW9uJ3MgZXhjbHVzaXZlIGxvY2suIE5vIGJ1c2luZXNzIHJlcXVlc3Qgb3IgaWRlbnRpdHkgaXMgY2hhbmdlZCBoZXJlLgogICAgIiIiCiAgICByZWMsIHRvb2xzID0gb3MuZW52aXJvbi5nZXQoJ0xFQk9YX1JFQ09WRVJZX1NFTEYnLCAnJyksIG9zLmVudmlyb24uZ2V0KCdMRUJPWF9UT09MUycsICcnKQogICAgY29udGV4dCA9IHMudmFsdWUuZ2V0KCdyZWNvdmVyeV9jb250ZXh0Jywge30pCiAgICBpZiBub3QgcmVjOgogICAgICAgIHJlYywgdG9vbHMgPSBjb250ZXh0LmdldCgncmVjb3ZlcnknLCAnJyksIGNvbnRleHQuZ2V0KCd0b29scycsICcnKQogICAgZWxpZiBjb250ZXh0OgogICAgICAgIHJlcXVpcmUoY29udGV4dCA9PSB7J3JlY292ZXJ5JzogcmVjLCAndG9vbHMnOiB0b29sc30sICdSRUNPVkVSWV9TQ09QRV9NSVNNQVRDSCcpCiAgICBpZiBub3QgcmVjIG9yIG5vdCB0b29sczoKICAgICAgICByZXR1cm4gJ25vdF9jb25maWd1cmVkJwogICAgcmVxdWlyZShyZS5mdWxsbWF0Y2gocidbQS1aYS16MC05XVtBLVphLXowLTktXSovW0EtWmEtejAtOS5fLV0rJywgdG9vbHMpCiAgICAgICAgICAgIGFuZCB0b29scy5zcGxpdCgnLycpWzFdIG5vdCBpbiAoJy4nLCAnLi4nKSwgJ1JFQ09WRVJZX1RPT0xTJykKICAgIHJlcXVpcmUob3MuZW52aXJvbi5nZXQoJ0xFQk9YX1JFUE8nLCBjWydyZXBvc2l0b3J5J10pID09IGNbJ3JlcG9zaXRvcnknXSwgJ1JFQ09WRVJZX1JFUE9TSVRPUllfTUlTTUFUQ0gnKQogICAgcmVxdWlyZShpc2luc3RhbmNlKHRva2VuLCBzdHIpIGFuZCA4IDw9IGxlbih0b2tlbikgPD0gNDA5NiBhbmQgbm90IGFueSh4Lmlzc3BhY2UoKSBmb3IgeCBpbiB0b2tlbiksICdSRUNPVkVSWV9UT0tFTicpCiAgICByaWQsIGtleSA9IHJlY292ZXJ5X2tleShyZWMpCiAgICBzY29wZSA9IGRpY3QocGlucyhjKSwgcHJvamVjdF9pZD1jWydwcm9qZWN0X2lkJ10sIHJlcG9zaXRvcnk9Y1sncmVwb3NpdG9yeSddLCB0b29scz10b29scywgcmlkPXJpZCwKICAgICAgICAgICAgICAgICBrZXlfaWQ9aGFzaGxpYi5zaGEyNTYoYidsZWJveC1yZWNvdmVyeS1pZCcgKyBrZXkpLmhleGRpZ2VzdCgpKQogICAgc3RhdGUgPSBzLnZhbHVlLmdldCgncmVjb3ZlcnlfcHVibGljYXRpb24nKQogICAgaWYgc3RhdGUgaXMgbm90IE5vbmU6CiAgICAgICAgcmVxdWlyZShpc2luc3RhbmNlKHN0YXRlLCBkaWN0KSBhbmQgc3RhdGUuZ2V0KCdmb3JtYXQnKSA9PSAnbGVib3gtYWdlbnQtcHVibGljYXRpb24tdjEnIGFuZCBzdGF0ZS5nZXQoJ3Njb3BlJykgPT0gc2NvcGUsICdSRUNPVkVSWV9TQ09QRV9NSVNNQVRDSCcpCiAgICAgICAgcmVxdWlyZShzdGF0ZS5nZXQoJ3N0YXR1cycpIGluICgncGVuZGluZycsICdwdWJsaXNoZWQnLCAncmV2b2tlZCcpLCAnUkVDT1ZFUllfU1RBVEUnKQogICAgICAgIHJlcXVpcmUoc3RhdGVbJ3N0YXR1cyddICE9ICdyZXZva2VkJywgJ1JFQ09WRVJZX1JFVk9LRUQnKQogICAgcmVtb3RlID0gcmVjb3ZlcnlfcmVtb3RlKHRva2VuLCB0b29scywgcmlkKQogICAgaWYgc3RhdGUgaXMgTm9uZToKICAgICAgICBzdGF0ZSA9IHsnZm9ybWF0JzogJ2xlYm94LWFnZW50LXB1YmxpY2F0aW9uLXYxJywgJ3Njb3BlJzogc2NvcGUsICdzdGF0dXMnOiAncHVibGlzaGVkJywKICAgICAgICAgICAgICAgICAncmVwb3NpdG9yeV9pZCc6IHJlbW90ZVsncmVwb3NpdG9yeV9pZCddLCAnYnJhbmNoJzogcmVtb3RlWydicmFuY2gnXSwgJ2dlbmVyYXRpb24nOiAwfQogICAgICAgIHMudmFsdWVbJ3JlY292ZXJ5X3B1YmxpY2F0aW9uJ10gPSBzdGF0ZQogICAgcmVxdWlyZShyZW1vdGVbJ3JlcG9zaXRvcnlfaWQnXSA9PSBzdGF0ZVsncmVwb3NpdG9yeV9pZCddIGFuZCByZW1vdGVbJ2JyYW5jaCddID09IHN0YXRlWydicmFuY2gnXSwgJ1JFQ09WRVJZX1JFTU9URV9TQ09QRV9DSEFOR0VEJykKICAgIGRlZiBkZW55X3Jldm9rZWQob2JzZXJ2ZWQpOgogICAgICAgIGlmIG9ic2VydmVkWydkYXRhJ10uc3RhcnRzd2l0aChiJ0xFQk9YLVJFVk9LRUQtVjEnKToKICAgICAgICAgICAgc3RhdGVbJ3N0YXR1cyddID0gJ3Jldm9rZWQnOyBzLnNhdmUoKSAgIyBSZXRhaW4gYWxsIHBlbmRpbmcgZXhhY3QgYnl0ZXM7IHBlcm1hbmVudCBsb2NhbCBkZW5pYWwuCiAgICAgICAgICAgIHJhaXNlIFN0b3AoJ1JFQ09WRVJZX1JFVk9LRUQnKQogICAgZGVueV9yZXZva2VkKHJlbW90ZSkKICAgIGlmIHN0YXRlLmdldCgnb3V0Ym94Jyk6CiAgICAgICAgIyBBbiBhbWJpZ3VvdXMgcHJldmlvdXMgd3JpdGUgY2FuIE9OTFkgcmVjb25jaWxlIGl0cyBvcmlnaW5hbCBieXRlcyBhbmQgb3JpZ2luYWwgQ0FTIGJhc2VsaW5lLgogICAgICAgIHRhcmdldCA9IHVuYjY0KHN0YXRlWydvdXRib3gnXSkKICAgICAgICByZXF1aXJlKGhhc2hsaWIuc2hhMjU2KHRhcmdldCkuaGV4ZGlnZXN0KCkgPT0gc3RhdGUuZ2V0KCdvdXRib3hfc2hhMjU2JyksICdSRUNPVkVSWV9PVVRCT1hfQ09SUlVQVCcpCiAgICBlbHNlOgogICAgICAgIG9sZCA9IGxvYWRfanNvbihyZWNvdmVyeV9jaXBoZXIoa2V5LCByZW1vdGVbJ2RhdGEnXSwgVHJ1ZSksIGxpbWl0PTE2XzAwMCkKICAgICAgICByZXF1aXJlKGlzaW5zdGFuY2Uob2xkLCBkaWN0KSBhbmQgb2xkLmdldCgndicpID09IDEgYW5kIHJlY292ZXJ5X2xlZ2FjeV9vd25lZChjLCBzLCBvbGQsIHJlbW90ZVsnZGF0YSddKQogICAgICAgICAgICAgICAgYW5kIG9sZC5nZXQoJ3JlcG8nKSA9PSBjWydyZXBvc2l0b3J5J10gYW5kIG9sZC5nZXQoJ3Rvb2xzJykgPT0gdG9vbHMKICAgICAgICAgICAgICAgIGFuZCAoJ3Njb3BlJyBub3QgaW4gb2xkIG9yIG9sZFsnc2NvcGUnXSA9PSBzY29wZSksICdSRUNPVkVSWV9PV05FUl9VTlZFUklGSUVEJykKICAgICAgICBpZiBvbGQuZ2V0KCd0b2tlbicpID09IHRva2VuIGFuZCBvbGQuZ2V0KCdzY29wZScpID09IHNjb3BlOgogICAgICAgICAgICByZXR1cm4gJ3B1Ymxpc2hlZCcKICAgICAgICByZXF1aXJlKHN0YXRlWydnZW5lcmF0aW9uJ10gPCAxMjgsICdSRUNPVkVSWV9HRU5FUkFUSU9OX0xJTUlUJykKICAgICAgICBwYXlsb2FkID0geyd2JzogMSwgJ3JlcG8nOiBjWydyZXBvc2l0b3J5J10sICd0b29scyc6IHRvb2xzLCAndG9rZW4nOiB0b2tlbiwgJ3JlbmV3ZWQnOiBpbnQodGltZS50aW1lKCkpLCAnYnknOiAnYWdlbnQnLCAnc2NvcGUnOiBzY29wZX0KICAgICAgICB0YXJnZXQgPSByZWNvdmVyeV9jaXBoZXIoa2V5LCBjYW5vbmljYWwocGF5bG9hZCkpCiAgICAgICAgc3RhdGUudXBkYXRlKHN0YXR1cz0ncGVuZGluZycsIGdlbmVyYXRpb249c3RhdGVbJ2dlbmVyYXRpb24nXSArIDEsIG91dGJveD1iNjQodGFyZ2V0KSwKICAgICAgICAgICAgICAgICAgICAgb3V0Ym94X3NoYTI1Nj1oYXNobGliLnNoYTI1Nih0YXJnZXQpLmhleGRpZ2VzdCgpLCBiYXNlbGluZV9zaGE9cmVtb3RlWydzaGEnXSkKICAgICAgICBzLnNhdmUoKSAgIyBObyBuZXR3b3JrIG11dGF0aW9uIHVudGlsIHRoZSBleGFjdCBjaXBoZXJ0ZXh0IGFuZCBiYXNlbGluZSByZWFjaCBkdXJhYmxlIHByaXZhdGUgc3RvcmFnZS4KICAgIGRlZiBhY2tub3dsZWRnZShvYnNlcnZlZCk6CiAgICAgICAgcmVxdWlyZShvYnNlcnZlZFsncmVwb3NpdG9yeV9pZCddID09IHN0YXRlWydyZXBvc2l0b3J5X2lkJ10gYW5kIG9ic2VydmVkWydicmFuY2gnXSA9PSBzdGF0ZVsnYnJhbmNoJ10sICdSRUNPVkVSWV9SRU1PVEVfU0NPUEVfQ0hBTkdFRCcpCiAgICAgICAgZGVueV9yZXZva2VkKG9ic2VydmVkKQogICAgICAgIGlmIG9ic2VydmVkWydkYXRhJ10gIT0gdGFyZ2V0OgogICAgICAgICAgICByZXR1cm4gRmFsc2UKICAgICAgICBzdGF0ZS51cGRhdGUoc3RhdHVzPSdwdWJsaXNoZWQnLCBwdWJsaXNoZWRfc2hhPW9ic2VydmVkWydzaGEnXSkKICAgICAgICBzdGF0ZS5wb3AoJ291dGJveCcsIE5vbmUpOyBzdGF0ZS5wb3AoJ2Jhc2VsaW5lX3NoYScsIE5vbmUpOyBzLnNhdmUoKQogICAgICAgIHJldHVybiBUcnVlCiAgICBpZiBhY2tub3dsZWRnZShyZW1vdGUpOgogICAgICAgIHJldHVybiAncHVibGlzaGVkJwogICAgcmVxdWlyZShyZW1vdGVbJ3NoYSddID09IHN0YXRlLmdldCgnYmFzZWxpbmVfc2hhJyksICdSRUNPVkVSWV9DT05URU5UX0NPTkZMSUNUJykKICAgIHMuc2F2ZSgpICAjIEFsc28gY292ZXJzIGEgcHJldmlvdXMgcGVyc2lzdGVuY2UgZmFpbHVyZTsgbmV2ZXIgcHVibGlzaCBvbmx5IGluLW1lbW9yeSBpbnRlbnQuCiAgICBib2R5ID0geydtZXNzYWdlJzogJ2xlYm94OiByZW5ldyAnICsgcmlkWzo4XSwgJ2JyYW5jaCc6IHN0YXRlWydicmFuY2gnXSwgJ3NoYSc6IHN0YXRlWydiYXNlbGluZV9zaGEnXSwgJ2NvbnRlbnQnOiBiNjQodGFyZ2V0KX0KICAgIHRyeToKICAgICAgICByZWNvdmVyeV9odHRwKHRva2VuLCAnL3JlcG9zLyVzL2NvbnRlbnRzL3JlY292ZXJ5LyVzLmJpbicgJSAodG9vbHMsIHJpZCksIGJvZHkpCiAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgICMgVGhlIHJlcXVlc3QgbWF5IGhhdmUgY29tbWl0dGVkLiBPbmUgcmVhZC1vbmx5IHJlY29uY2lsaWF0aW9uOyBOTyBibGluZCByZXRyeSBvciByZXNlYWwuCiAgICAgICAgcGFzcwogICAgb2JzZXJ2ZWQgPSByZWNvdmVyeV9yZW1vdGUodG9rZW4sIHRvb2xzLCByaWQpCiAgICBpZiBhY2tub3dsZWRnZShvYnNlcnZlZCk6CiAgICAgICAgcmV0dXJuICdwdWJsaXNoZWQnCiAgICByZXR1cm4gJ3BlbmRpbmcnICAjIEV2ZW4gMjAwLzIwMSBpcyBub3QgYW4gYWNrbm93bGVkZ2VtZW50IHdpdGhvdXQgZXhhY3QgcmVhZGJhY2suCgoKZGVmIHJlbWVtYmVyX3JlY292ZXJ5X2NvbnRleHQoYywgcyk6CiAgICAjIFNlY3JldC1iZWFyaW5nIGNvbnRleHQgYmVsb25ncyBvbmx5IGluIHRoaXMgb3JpZ2luYWwgc2Vzc2lvbidzIG93bmVyLXByaXZhdGUgc3RhdGUsIG5ldmVyIGFuIGluc3RhbGwgcmVjZWlwdC4KICAgIHJlYywgdG9vbHMgPSBvcy5lbnZpcm9uLmdldCgnTEVCT1hfUkVDT1ZFUllfU0VMRicsICcnKSwgb3MuZW52aXJvbi5nZXQoJ0xFQk9YX1RPT0xTJywgJycpCiAgICBpZiBub3QgcmVjOgogICAgICAgIHJldHVybgogICAgcmVjb3Zlcnlfa2V5KHJlYykKICAgIHJlcXVpcmUocmUuZnVsbG1hdGNoKHInW0EtWmEtejAtOV1bQS1aYS16MC05LV0qL1tBLVphLXowLTkuXy1dKycsIHRvb2xzKQogICAgICAgICAgICBhbmQgdG9vbHMuc3BsaXQoJy8nKVsxXSBub3QgaW4gKCcuJywgJy4uJykgYW5kIG9zLmVudmlyb24uZ2V0KCdMRUJPWF9SRVBPJywgY1sncmVwb3NpdG9yeSddKSA9PSBjWydyZXBvc2l0b3J5J10sICdSRUNPVkVSWV9TQ09QRV9NSVNNQVRDSCcpCiAgICBjb250ZXh0ID0geydyZWNvdmVyeSc6IHJlYywgJ3Rvb2xzJzogdG9vbHN9CiAgICBwcmV2aW91cyA9IHMudmFsdWUuZ2V0KCdyZWNvdmVyeV9jb250ZXh0JykKICAgIHJlcXVpcmUocHJldmlvdXMgaXMgTm9uZSBvciBwcmV2aW91cyA9PSBjb250ZXh0LCAnUkVDT1ZFUllfU0NPUEVfTUlTTUFUQ0gnKQogICAgaWYgcHJldmlvdXMgaXMgTm9uZToKICAgICAgICBzLnZhbHVlWydyZWNvdmVyeV9jb250ZXh0J10gPSBjb250ZXh0CiAgICAgICAgcy5zYXZlKCkKCgpkZWYgcmV0cnlfcmVjb3ZlcnlfcHVibGljYXRpb24oYywgcyk6CiAgICB0b2tlbiA9IHMudmFsdWUuZ2V0KCdyZW5ld2VkX3Rva2VuJykKICAgIGlmIG5vdCB0b2tlbjoKICAgICAgICByZXR1cm4KICAgIHRyeToKICAgICAgICByZXN1bHQgPSByZXNlYWxfcmVjb3ZlcnkodG9rZW4sIGMsIHMpCiAgICAgICAgaWYgcmVzdWx0ICE9ICdub3RfY29uZmlndXJlZCc6CiAgICAgICAgICAgIHByaW50KGpzb24uZHVtcHMoeydzdGF0dXMnOiAncmVjb3ZlcnlfJyArIHJlc3VsdH0pLCBmaWxlPXN5cy5zdGRlcnIpCiAgICBleGNlcHQgRXhjZXB0aW9uIGFzIGVycm9yOgogICAgICAgICMgRG8gbm90IGluY2x1ZGUgYSB0b2tlbi1iZWFyaW5nIEhUVFAgZXhjZXB0aW9uLCBVUkwgb3IgZGVjcnlwdGVkIHBheWxvYWQgaW4gZGlhZ25vc3RpY3MuCiAgICAgICAgY29kZSA9IHN0cihlcnJvcikgaWYgaXNpbnN0YW5jZShlcnJvciwgU3RvcCkgYW5kIHJlLmZ1bGxtYXRjaChyJ1JFQ09WRVJZX1tBLVpfXSsnLCBzdHIoZXJyb3IpKSBlbHNlICdSRUNPVkVSWV9OT1RfQ09ORklSTUVEJwogICAgICAgIHByaW50KGpzb24uZHVtcHMoeydzdGF0dXMnOiAncmVjb3Zlcnlfbm90X2NvbmZpcm1lZCcsICdjb2RlJzogY29kZX0pLCBmaWxlPXN5cy5zdGRlcnIpCgpkZWYgY2xvc2VkX21hcmtlcihjLCBzLCBib3gsIHRpcD1Ob25lKToKICAgICMgVGhlIGRlc2t0b3Agd3JpdGVzIHNlcnZlci5jbG9zZWQuanNvbiB3aGVuIGl0IHN0b3BzIG9yIHJlLWJpbmRzIHRoaXMgc2Vzc2lvbi4gUGxhaW50ZXh0LCBzbWFsbCwgY2hlY2tlZCBvbiBldmVyeSBwb2xsIHNvIGEKICAgICMgc3VwZXJzZWRlZCBBZ2VudCBsZWFybnMgd2l0aGluIG9uZSBmZXRjaCBpbnN0ZWFkIG9mIHdhaXRpbmcgb3V0IGl0cyB0aW1lb3V0LgogICAgdHJ5OgogICAgICAgIHJhdyA9IGJveC5hdCh0aXAsIG1lc3NhZ2VfcGF0aChjLCAnY2xvc2VkJywgJ3NlcnZlcicpKSBpZiB0aXAgZWxzZSBib3gucmVhZCgnY2xvc2VkJywgJ3NlcnZlcicpCiAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgIHJldHVybiBOb25lCiAgICBpZiByYXcgaXMgTm9uZToKICAgICAgICByZXR1cm4gTm9uZQogICAgdHJ5OgogICAgICAgIGluZm8gPSBsb2FkX2pzb24ocmF3LCBsaW1pdD00MDk2KQogICAgICAgIGlmIGluZm8uZ2V0KCdmb3JtYXQnKSAhPSAnc2N4LXNlc3Npb24tY2xvc2VkLXYxJyBvciBpbmZvLmdldCgnYmluZGluZycpICE9IGNbJ2JpbmRpbmcnXToKICAgICAgICAgICAgcmV0dXJuIE5vbmUKICAgICAgICByZXR1cm4gaW5mbwogICAgZXhjZXB0IEV4Y2VwdGlvbjoKICAgICAgICByZXR1cm4gTm9uZQoKZGVmIHJlcG9ydF9jbG9zZWQocywgaW5mbywgcGVuZGluZz1Ob25lKToKICAgIHMudmFsdWVbJ2Nsb3NlZCddID0geydyZWFzb24nOiBpbmZvLmdldCgncmVhc29uJywgJycpLCAnY2xvc2VkX2F0JzogaW5mby5nZXQoJ2Nsb3NlZF9hdCcsIDApfQogICAgcy52YWx1ZVsnYWN0aXZlJ10gPSBGYWxzZQogICAgcy5zYXZlKCkKICAgIG91dCA9IHsnc3RhdHVzJzogJ3Nlc3Npb25fY2xvc2VkX2J5X2Rlc2t0b3AnLCAncmVhc29uJzogaW5mby5nZXQoJ3JlYXNvbicsICcnKSwKICAgICAgICAgICAnbmV4dCc6IGluZm8uZ2V0KCduZXh0JywgJ1JlLWpvaW46IHJ1biAgcHl0aG9uMyAubGVib3gvYWdlbnQucHkgam9pbiAgaW4gdGhlIHJlcG9zaXRvcnkuJyl9CiAgICBpZiBwZW5kaW5nOgogICAgICAgIG91dFsnb3BlcmF0aW9uX2lkJ10gPSBwZW5kaW5nWydpZCddCiAgICAgICAgb3V0Wydub3RlJ10gPSAnVGhlIHBlbmRpbmcgb3BlcmF0aW9uIHdpbGwgbmV2ZXIgYmUgYW5zd2VyZWQgb24gdGhpcyBjaGFubmVsOyBpdHMgcHJvY2Vzc2luZyBzdGF0ZSBpcyB1bmtub3duLCBkbyBub3QgcmVzdWJtaXQgYmxpbmRseS4nCiAgICBwcmludChqc29uLmR1bXBzKG91dCwgZW5zdXJlX2FzY2lpPUZhbHNlLCBpbmRlbnQ9MiksIGZpbGU9c3lzLnN0ZGVycikKICAgIHN5cy5leGl0KDMpCgpkZWYgdmVyaWZ5X3BlbmRpbmdfcmVxdWVzdChjLCBwZW5kaW5nLCBib3gsIHRpcCk6CiAgICAiIiJCaW5kIHJlc3BvbnNlIGFjY2VwdGFuY2UgdG8gZXhhY3QgcHVibGlzaGVkIGJ5dGVzLCBub3QganVzdCBhIHJldXNlZCBvcGVyYXRpb24gSUQuCiAgICBGYWlscyBiZWZvcmUgYW55IHJlY292ZXJ5IHB1YmxpY2F0aW9uLCByZW5ld2FsLCByZXNwb25zZSBkZWNyeXB0LCBzYXZlIG9yIHBlbmRpbmcgY2xlYXIuCiAgICBOZXZlciByZXBhaXJzL3Jlc2VhbHMvcmVwbGF5cyBhIHJlcXVlc3QgYW5kIG5ldmVyIHRyZWF0cyBwbGFpbnRleHQgZXF1YWxpdHkgYXMgYWRtaXNzaW9uLgogICAgIiIiCiAgICByZXF1aXJlKGlzaW5zdGFuY2UocGVuZGluZywgZGljdCkgYW5kIGlzaW5zdGFuY2UocGVuZGluZy5nZXQoJ2lkJyksIHN0cikKICAgICAgICAgICAgYW5kIHJlLmZ1bGxtYXRjaChyJ29wLVswLTldezZ9JywgcGVuZGluZ1snaWQnXSkKICAgICAgICAgICAgYW5kIHBlbmRpbmcuZ2V0KCdtZXRob2QnKSBpbiAoJ2luaXRpYWxpemUnLCAndG9vbHMvbGlzdCcsICd0b29scy9jYWxsJyksICdQRU5ESU5HX1JFUVVFU1RfSU5WQUxJRCcpCiAgICBlbnYgPSBwZW5kaW5nLmdldCgnZW52ZWxvcGUnKQogICAgcmVxdWlyZShpc2luc3RhbmNlKGVudiwgZGljdCkgYW5kIHNldChlbnYpID09IHsnaGVhZGVyJywgJ25vbmNlJywgJ2NpcGhlcnRleHQnLCAndGFnJ30sICdQRU5ESU5HX1JFUVVFU1RfSU5WQUxJRCcpCiAgICBoID0gZW52LmdldCgnaGVhZGVyJykKICAgIHJlcXVpcmUoaXNpbnN0YW5jZShoLCBkaWN0KSBhbmQgc2V0KGgpID09IHNldChwaW5zKGMpKSB8IHsncmVxdWVzdF9pZCcsICdkaXJlY3Rpb24nLCAnZXhwaXJlcyd9LCAnUEVORElOR19SRVFVRVNUX0lOVkFMSUQnKQogICAgcmVxdWlyZShhbGwoaFtrXSA9PSB2IGZvciBrLCB2IGluIHBpbnMoYykuaXRlbXMoKSkgYW5kIGhbJ3JlcXVlc3RfaWQnXSA9PSBwZW5kaW5nWydpZCddCiAgICAgICAgICAgIGFuZCBoWydkaXJlY3Rpb24nXSA9PSAncmVxJywgJ1BFTkRJTkdfUkVRVUVTVF9CSU5ESU5HJykKICAgIHJlcXVpcmUodHlwZShoWydleHBpcmVzJ10pIGlzIGludCBhbmQgaFsnZXhwaXJlcyddID4gMAogICAgICAgICAgICBhbmQgYWxsKGlzaW5zdGFuY2UoZW52W2tdLCBzdHIpIGZvciBrIGluICgnbm9uY2UnLCAnY2lwaGVydGV4dCcsICd0YWcnKSksICdQRU5ESU5HX1JFUVVFU1RfSU5WQUxJRCcpCiAgICB0cnk6CiAgICAgICAgZm9yIGZpZWxkLCBsZW5ndGggaW4gKCgnbm9uY2UnLCAxMiksICgndGFnJywgMTYpLCAoJ2NpcGhlcnRleHQnLCBOb25lKSk6CiAgICAgICAgICAgIGRlY29kZWQgPSB1bmI2NChlbnZbZmllbGRdLCBsZW5ndGgpCiAgICAgICAgICAgIHJlcXVpcmUobGVuKGRlY29kZWQpIDw9IDY1NTM2IGFuZCBiNjQoZGVjb2RlZCkgPT0gZW52W2ZpZWxkXSwgJ1BFTkRJTkdfUkVRVUVTVF9JTlZBTElEJykKICAgIGV4Y2VwdCAoVmFsdWVFcnJvciwgVHlwZUVycm9yLCBTdG9wKToKICAgICAgICByYWlzZSBTdG9wKCdQRU5ESU5HX1JFUVVFU1RfSU5WQUxJRCcpIGZyb20gTm9uZQogICAgZXhwZWN0ZWQgPSBjYW5vbmljYWwoZW52KQogICAgcmVxdWlyZShsZW4oZXhwZWN0ZWQpIDw9IExJTUlULCAnUEVORElOR19SRVFVRVNUX0lOVkFMSUQnKQogICAgaWYgdGlwIGlzIE5vbmU6CiAgICAgICAgcmV0dXJuICAjIFNoYXBlL2JpbmRpbmcgcHJlZmxpZ2h0IG9ubHk7IG5vIHRyYW5zcG9ydCBvciBzdGF0ZSBhY2Nlc3MuCiAgICBwdWJsaXNoZWQgPSBib3guYXQodGlwLCBtZXNzYWdlX3BhdGgoYywgJ3JlcXVlc3QnLCBwZW5kaW5nWydpZCddKSkKICAgIHJlcXVpcmUoaXNpbnN0YW5jZShwdWJsaXNoZWQsIGJ5dGVzKSBhbmQgMCA8IGxlbihwdWJsaXNoZWQpIDw9IExJTUlULCAnUEVORElOR19SRVFVRVNUX1VOVkVSSUZJRUQnKQogICAgcmVxdWlyZShobWFjLmNvbXBhcmVfZGlnZXN0KGhhc2hsaWIuc2hhMjU2KHB1Ymxpc2hlZCkuZGlnZXN0KCksIGhhc2hsaWIuc2hhMjU2KGV4cGVjdGVkKS5kaWdlc3QoKSksCiAgICAgICAgICAgICdQRU5ESU5HX1JFUVVFU1RfQ09ORkxJQ1Q6IG9yaWdpbmFsIHBlbmRpbmcgcHJlc2VydmVkOyBkbyBub3QgcmVzdWJtaXQgb3Igc3Vic3RpdHV0ZSBhbm90aGVyIHJlcXVlc3QnKQoKZGVmIHN0YXR1cyhjLCBzLCBib3gsIHdhaXQ9MCk6CiAgICBwZW5kaW5nID0gcy52YWx1ZS5nZXQoJ3BlbmRpbmcnKQogICAgaWYgcGVuZGluZyBpcyBOb25lOgogICAgICAgIHJldHJ5X3JlY292ZXJ5X3B1YmxpY2F0aW9uKGMsIHMpCiAgICAgICAgaW5mbyA9IGNsb3NlZF9tYXJrZXIoYywgcywgYm94KQogICAgICAgIGlmIGluZm86CiAgICAgICAgICAgIHJlcG9ydF9jbG9zZWQocywgaW5mbykKICAgICAgICBwcmludChqc29uLmR1bXBzKHMudmFsdWUuZ2V0KCdsYXN0X3JlcGx5JywgeydzdGF0dXMnOiAnbm9fcGVuZGluZ19yZXF1ZXN0J30pLCBlbnN1cmVfYXNjaWk9RmFsc2UsIGluZGVudD0yKSkKICAgICAgICByZXR1cm4KICAgIHZlcmlmeV9wZW5kaW5nX3JlcXVlc3QoYywgcGVuZGluZywgYm94LCBOb25lKQogICAgdW50aWwgPSB0aW1lLm1vbm90b25pYygpICsgd2FpdAogICAgYXR0ZW1wdCA9IDAKICAgIHJlY292ZXJ5X2NoZWNrZWQgPSBGYWxzZQogICAgd2hpbGUgVHJ1ZToKICAgICAgICB0aXAgPSBib3guZmV0Y2goKQogICAgICAgIHJlcXVpcmUoaXNpbnN0YW5jZSh0aXAsIHN0cikgYW5kIGJvb2wodGlwKSwgJ1BFTkRJTkdfUkVRVUVTVF9VTlZFUklGSUVEJykKICAgICAgICB2ZXJpZnlfcGVuZGluZ19yZXF1ZXN0KGMsIHBlbmRpbmcsIGJveCwgdGlwKQogICAgICAgIGlmIG5vdCByZWNvdmVyeV9jaGVja2VkOgogICAgICAgICAgICByZXRyeV9yZWNvdmVyeV9wdWJsaWNhdGlvbihjLCBzKQogICAgICAgICAgICByZWNvdmVyeV9jaGVja2VkID0gVHJ1ZQogICAgICAgIHJhdyA9IGJveC5hdCh0aXAsIG1lc3NhZ2VfcGF0aChjLCAncmVzcG9uc2UnLCBwZW5kaW5nWydpZCddKSkKICAgICAgICBpZiByYXcgaXMgTm9uZToKICAgICAgICAgICAgYXBwbHlfdG9rZW5fcmVuZXcoYywgcywgYm94LCB0aXApCiAgICAgICAgICAgIGluZm8gPSBjbG9zZWRfbWFya2VyKGMsIHMsIGJveCwgdGlwKQogICAgICAgICAgICBpZiBpbmZvOgogICAgICAgICAgICAgICAgcmVwb3J0X2Nsb3NlZChzLCBpbmZvLCBwZW5kaW5nKQogICAgICAgIGlmIHJhdyBpcyBub3QgTm9uZToKICAgICAgICAgICAgcmVwbHkgPSBvcGVuX3Jlc3BvbnNlKGMsIHMsIHBlbmRpbmdbJ2lkJ10sIGxvYWRfanNvbihyYXcpKQogICAgICAgICAgICBpZiBwZW5kaW5nWydtZXRob2QnXSA9PSAnaW5pdGlhbGl6ZScgYW5kIGlzaW5zdGFuY2UocmVwbHksIGRpY3QpIGFuZCAncmVzdWx0JyBpbiByZXBseToKICAgICAgICAgICAgICAgIHMudmFsdWVbJ2luaXRpYWxpemVkJ10gPSBUcnVlCiAgICAgICAgICAgIHMudmFsdWVbJ2xhc3RfcmVwbHknXSA9IHJlcGx5CiAgICAgICAgICAgIGRlbCBzLnZhbHVlWydwZW5kaW5nJ10KICAgICAgICAgICAgcy5zYXZlKCkKICAgICAgICAgICAgcHJpbnQoanNvbi5kdW1wcyhyZXBseSwgZW5zdXJlX2FzY2lpPUZhbHNlLCBpbmRlbnQ9MikpCiAgICAgICAgICAgIHJldHVybgogICAgICAgIGlmIHRpbWUubW9ub3RvbmljKCkgPj0gdW50aWw6CiAgICAgICAgICAgIHByaW50KGpzb24uZHVtcHMoeydzdGF0dXMnOiAncmVzcG9uc2Vfbm90X3JlY2VpdmVkJywgJ29wZXJhdGlvbl9pZCc6IHBlbmRpbmdbJ2lkJ10sCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICduZXh0JzogJ1VzZSBzdGF0dXMgdG8gcmVjb25jaWxlIHRoaXMgb3JpZ2luYWwgb3BlcmF0aW9uLiBObyByZXNwb25zZSBoYXMgYmVlbiByZWNlaXZlZDsgdGhpcyBkb2VzIG5vdCBwcm92ZSBwZW5kaW5nIHBlcm1pc3Npb24gYXBwcm92YWwuIFByb2Nlc3Npbmcgc3RhdGUgaXMgdW5rbm93bjsgZG8gbm90IHN1Ym1pdCB0aGUgb3BlcmF0aW9uIGFnYWluLid9KSkKICAgICAgICAgICAgcmV0dXJuCiAgICAgICAgdGltZS5zbGVlcChtaW4ocG9sbF9kZWxheShhdHRlbXB0KSwgbWF4KDAuMCwgdW50aWwgLSB0aW1lLm1vbm90b25pYygpKSkgb3IgMSkKICAgICAgICBhdHRlbXB0ICs9IDEKCmRlZiByZXF1ZXN0KGMsIHMsIGJveCwgbWV0aG9kLCBhcmdzLCB3YWl0PTkwKToKICAgIHJlcXVpcmUobm90IHMudmFsdWUuZ2V0KCdwZW5kaW5nJyksICdUaGVyZSBpcyBhbiB1bnJlc29sdmVkIHJlcXVlc3QuIFJ1biBzdGF0dXMgaW5zdGVhZCBvZiBzdWJtaXR0aW5nIGFub3RoZXIgb3BlcmF0aW9uLicpCiAgICBhcHBseV90b2tlbl9yZW5ldyhjLCBzLCBib3gpCiAgICBpZiBzLnZhbHVlLmdldCgnY2xvc2VkJyk6CiAgICAgICAgcmVwb3J0X2Nsb3NlZChzLCB7J3JlYXNvbic6IHMudmFsdWVbJ2Nsb3NlZCddLmdldCgncmVhc29uJywgJycpLCAnbmV4dCc6ICdUaGlzIHNlc3Npb24gd2FzIGNsb3NlZCBieSB0aGUgZGVza3RvcC4gUmUtam9pbjogcnVuICBweXRob24zIC5sZWJveC9hZ2VudC5weSBqb2luICBpbiB0aGUgcmVwb3NpdG9yeS4nfSkKICAgIHJlcXVpcmUocy52YWx1ZS5nZXQoJ2FjdGl2ZScpLCAnUGFpcmluZyBpcyBub3QgYWN0aXZlLicpCiAgICByZXF1aXJlKG1ldGhvZCA9PSAnaW5pdGlhbGl6ZScgb3Igcy52YWx1ZS5nZXQoJ2luaXRpYWxpemVkJyksICdJbml0aWFsaXplIG11c3QgY29tcGxldGUgYmVmb3JlIHRvb2wgY2FsbHMuJykKICAgIHJlcXVpcmUoY1snbWF4X29wZXJhdGlvbnMnXSA9PSAwIG9yIHMudmFsdWVbJ25leHQnXSA8IGNbJ21heF9vcGVyYXRpb25zJ10sICdTZXNzaW9uIG1lc3NhZ2UgbGltaXQgcmVhY2hlZDsgcmVxdWVzdCBhIG5ldyBzZXNzaW9uLicpCiAgICByZXF1aXJlKHMudmFsdWVbJ25leHQnXSA8IDFfMDAwXzAwMCwgJ1Nlc3Npb24gbWVzc2FnZSBsaW1pdCByZWFjaGVkOyByZXF1ZXN0IGEgbmV3IHNlc3Npb24uJykKICAgIG9wID0gZiJvcC17cy52YWx1ZVsnbmV4dCddOjA2ZH0iCiAgICBlbnYgPSBzZWFsKGMsIHMsIG9wLCBjYW5vbmljYWwoeydvcGVyYXRpb25faWQnOiBvcCwgJ21ldGhvZCc6IG1ldGhvZCwgJ2FyZ3VtZW50cyc6IGFyZ3N9KSkKICAgIHMudmFsdWVbJ25leHQnXSArPSAxCiAgICBzLnZhbHVlWydwZW5kaW5nJ10gPSB7J2lkJzogb3AsICdtZXRob2QnOiBtZXRob2QsICdlbnZlbG9wZSc6IGVudn0KICAgIHMuc2F2ZSgpICAjIFBlcnNpc3QgZXhhY3QgY2lwaGVydGV4dCBCRUZPUkUgcHVibGlzaGluZyBhbnl0aGluZy4KICAgIGJveC5jcmVhdGUoJ3JlcXVlc3QnLCBvcCwgY2Fub25pY2FsKGVudikpCiAgICBzdGF0dXMoYywgcywgYm94LCB3YWl0PXdhaXQpCgpkZWYgYWN0aXZhdGUoYywgcywgYm94LCBhcHByb3ZlZCwgd2FpdD0wKToKICAgIHJlcXVpcmUoJ2tleXMnIGluIHMudmFsdWUsICdSdW4gY29ubmVjdCBmaXJzdC4nKQogICAgaWYgcy52YWx1ZS5nZXQoJ3BlbmRpbmcnKToKICAgICAgICByZXR1cm4gc3RhdHVzKGMsIHMsIGJveCwgd2FpdCkKICAgIGlmIHMudmFsdWUuZ2V0KCdpbml0aWFsaXplZCcpOgogICAgICAgIHByaW50KCdBbHJlYWR5IGluaXRpYWxpemVkLiBVc2UgbGlzdCwgY2FsbCBvciBzdGF0dXMuJyk7IHJldHVybgogICAgIyBUaGUgbG9jYWwgbWFjaGluZSBwdWJsaXNoZXMgY29uZmlybWF0aW9uL3NlcnZlciBvbmx5IGFmdGVyIHRoZSB1c2VyIChvciB0aGUgdHJ1c3RlZC1yZXBvc2l0b3J5IHNldHRpbmcpIGNvbmZpcm1lZCB0aGUgcGFpcmluZy4KICAgIHVudGlsID0gdGltZS5tb25vdG9uaWMoKSArIG1heCgwLCB3YWl0KQogICAgYXR0ZW1wdCA9IDAKICAgIHdoaWxlIFRydWU6CiAgICAgICAgcmF3ID0gYm94LnJlYWQoJ2NvbmZpcm1hdGlvbicsICdzZXJ2ZXInKQogICAgICAgIGlmIHJhdyBpcyBOb25lOgogICAgICAgICAgICBpbmZvID0gY2xvc2VkX21hcmtlcihjLCBzLCBib3gpCiAgICAgICAgICAgIGlmIGluZm86CiAgICAgICAgICAgICAgICByZXBvcnRfY2xvc2VkKHMsIGluZm8pCiAgICAgICAgaWYgcmF3IGlzIG5vdCBOb25lIG9yIHRpbWUubW9ub3RvbmljKCkgPj0gdW50aWw6CiAgICAgICAgICAgIGJyZWFrCiAgICAgICAgdGltZS5zbGVlcChtaW4ocG9sbF9kZWxheShhdHRlbXB0KSwgbWF4KDAuMCwgdW50aWwgLSB0aW1lLm1vbm90b25pYygpKSkgb3IgMSkKICAgICAgICBhdHRlbXB0ICs9IDEKICAgIHJlcXVpcmUocmF3IGlzIG5vdCBOb25lLCAnV2FpdGluZyBmb3IgbG9jYWwgY29uZmlybWF0aW9uLiBSdW4gYWN0aXZhdGUgLS13YWl0IDkwMCBhZ2FpbjsgZG8gbm90IHN0YXJ0IHRvb2xzIHlldC4nKQogICAgY29uZmlybWF0aW9uID0gbG9hZF9qc29uKHJhdykKICAgIGV4YWN0KGNvbmZpcm1hdGlvbiwgWydyb2xlJywgJ2JpbmRpbmcnLCAnZXBvY2gnLCAndGFnJ10pCiAgICByZXF1aXJlKGNvbmZpcm1hdGlvblsncm9sZSddID09ICdzZXJ2ZXInIGFuZCBjb25maXJtYXRpb25bJ2JpbmRpbmcnXSA9PSBjWydiaW5kaW5nJ10gYW5kIGNvbmZpcm1hdGlvblsnZXBvY2gnXSA9PSBjWydlcG9jaCddLCAnQ29uZmlybWF0aW9uIGlkZW50aXR5IG1pc21hdGNoLicpCiAgICBrZXlzID0gdW5iNjQocy52YWx1ZVsna2V5cyddLCAxMjgpCiAgICB0cmFuc2NyaXB0ID0gdW5iNjQocy52YWx1ZVsndHJhbnNjcmlwdCddLCAzMikKICAgIHJlcXVpcmUoaG1hYy5jb21wYXJlX2RpZ2VzdCh1bmI2NChjb25maXJtYXRpb25bJ3RhZyddLCAzMiksIGhtYWMuZGlnZXN0KGtleXNbOTY6MTI4XSwgdHJhbnNjcmlwdCwgJ3NoYTI1NicpKSwgJ0NvbmZpcm1hdGlvbiBmYWlsZWQuJykKICAgIGJveC5jcmVhdGUoJ2NvbmZpcm1hdGlvbicsICdjbGllbnQnLCBjYW5vbmljYWwoeydyb2xlJzogJ2NsaWVudCcsICdiaW5kaW5nJzogY1snYmluZGluZyddLCAnZXBvY2gnOiBjWydlcG9jaCddLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAndGFnJzogYjY0KGhtYWMuZGlnZXN0KGtleXNbNjQ6OTZdLCB0cmFuc2NyaXB0LCAnc2hhMjU2JykpfSkpCiAgICBzLnZhbHVlWydhY3RpdmUnXSA9IFRydWUKICAgIHMuc2F2ZSgpCiAgICByZXF1ZXN0KGMsIHMsIGJveCwgJ2luaXRpYWxpemUnLCB7fSwgd2FpdD1tYXgoOTAsIHdhaXQpKQoKIyBJbmRlcGVuZGVudCByZXN1bHQtY29udHJvbCBzdGF0ZS4gVGhpcyBuZXZlciBzYXZlcyBvciBlZGl0cyBQcml2YXRlU3RhdGUudmFsdWUsIHBlbmRpbmcgb3IgYnVzaW5lc3MgY291bnRlcnMuCmRlZiBfcmVzdWx0X2NvbnRyb2xfbG9hZChjLCBzKToKICAgIHBhdGggPSBzLnJvb3QgLyAncmVzdWx0LXJlYWQuanNvbicKICAgIHNjb3BlID0gaGFzaGxpYi5zaGEyNTYoY2Fub25pY2FsKGMpKS5oZXhkaWdlc3QoKQogICAgdmFsdWUgPSBfc3RhdGVfanNvbihfc3RhdGVfcmVhZChwYXRoKSkgaWYgcGF0aC5leGlzdHMoKSBlbHNlIHsnc2NvcGUnOiBzY29wZSwgJ25leHQnOiAwfQogICAgcmVxdWlyZShpc2luc3RhbmNlKHZhbHVlLCBkaWN0KSBhbmQgdmFsdWUuZ2V0KCdzY29wZScpID09IHNjb3BlIGFuZCB0eXBlKHZhbHVlLmdldCgnbmV4dCcpKSBpcyBpbnQKICAgICAgICAgICAgYW5kIDAgPD0gdmFsdWVbJ25leHQnXSA8PSAxMDAwMDAwLCAnUkVTVUxUX0NPTlRST0xfU1RBVEUnKQogICAgcmV0dXJuIHBhdGgsIHZhbHVlCgoKZGVmIF9yZXN1bHRfY29udHJvbF9zYXZlKHBhdGgsIHZhbHVlKToKICAgIHJhdyA9IGNhbm9uaWNhbCh2YWx1ZSkKICAgIHJlcXVpcmUobGVuKHJhdykgPD0gNDAwMDAwLCAnUkVTVUxUX0NPTlRST0xfTElNSVQnKQogICAgX3N0YXRlX3dyaXRlKHBhdGgsIHJhdykKCgpkZWYgX3Jlc3VsdF9yZXBseShjLCBzLCBvcGVyYXRpb24sIGVudik6CiAgICAjIEF1dGhlbnRpY2F0ZSBiZWZvcmUgZGVjaWRpbmcgd2hldGhlciBhbiBleHBpcmVkIENPTlRST0wgcmVjZWlwdCBjYW4gYWR2YW5jZSB0aGUgcmVhZCBjdXJzb3IuCiAgICAjIE9yZGluYXJ5IG9wZW5fcmVzcG9uc2UgYW5kIGJ1c2luZXNzIFRUTC9yZXBsYXkgcnVsZXMgc3RheSB1bmNoYW5nZWQuCiAgICByZXF1aXJlKHJlLmZ1bGxtYXRjaChyJ3JyZWFkLVswLTldezZ9Jywgb3BlcmF0aW9uKSwgJ1JFU1VMVF9RVUVSWV9JRCcpCiAgICBleGFjdChlbnYsIFsnaGVhZGVyJywgJ25vbmNlJywgJ2NpcGhlcnRleHQnLCAndGFnJ10pCiAgICBoID0gZW52WydoZWFkZXInXTsgZXhhY3QoaCwgbGlzdChwaW5zKGMpKSArIFsncmVxdWVzdF9pZCcsICdkaXJlY3Rpb24nLCAnZXhwaXJlcyddKQogICAgcmVxdWlyZShhbGwoaFtrXSA9PSB2IGZvciBrLCB2IGluIHBpbnMoYykuaXRlbXMoKSkgYW5kIGhbJ3JlcXVlc3RfaWQnXSA9PSBvcGVyYXRpb24KICAgICAgICAgICAgYW5kIGhbJ2RpcmVjdGlvbiddID09ICdyZXMnIGFuZCB0eXBlKGhbJ2V4cGlyZXMnXSkgaXMgaW50IGFuZCBoWydleHBpcmVzJ10gPiAwLCAnUkVTVUxUX1JFUExZX0JJTkRJTkcnKQogICAgXywgXywgXywgXywgQUVTR0NNID0gY3J5cHRvKCkKICAgIGNpcGhlciA9IHVuYjY0KGVudlsnY2lwaGVydGV4dCddKTsgcmVxdWlyZShsZW4oY2lwaGVyKSA8PSA2NTUzNiwgJ1JFU1VMVF9SRVBMWV9TSVpFJykKICAgIHBsYWluID0gQUVTR0NNKHVuYjY0KHMudmFsdWVbJ2tleXMnXSwgMTI4KVszMjo2NF0pLmRlY3J5cHQodW5iNjQoZW52Wydub25jZSddLCAxMiksIGNpcGhlciArIHVuYjY0KGVudlsndGFnJ10sIDE2KSwgY2Fub25pY2FsKGgpKQogICAgYm9keSA9IGxvYWRfanNvbihwbGFpbiwgbGltaXQ9NjU1MzYpOyBleGFjdChib2R5LCBbJ3Byb2plY3RfaWQnLCAncmVwbHknXSkKICAgIHJlcXVpcmUoYm9keVsncHJvamVjdF9pZCddID09IGNbJ3Byb2plY3RfaWQnXSwgJ1JFU1VMVF9SRVBMWV9QUk9KRUNUJykKICAgIHJldHVybiBib2R5WydyZXBseSddLCB0aW1lLnRpbWUoKSAtIDEgPD0gaFsnZXhwaXJlcyddIDw9IHRpbWUudGltZSgpICsgMTgwMQoKCmRlZiBfb3JpZ2luYWxfcmVzdWx0X2RlY29kZShjLCBwYXlsb2FkKToKICAgIHJlcXVpcmUobGVuKHBheWxvYWQpIDw9IDY1NTM2LCAnUkVTVUxUX1BBWUxPQURfU0laRScpCiAgICBpZiBwYXlsb2FkLnN0YXJ0c3dpdGgoYidTQ1gyWlwwJyk6CiAgICAgICAgZGVjb2RlciA9IHpsaWIuZGVjb21wcmVzc29iaigpCiAgICAgICAgcGF5bG9hZCA9IGRlY29kZXIuZGVjb21wcmVzcyhwYXlsb2FkWzY6XSwgMTAwMDAwMSkKICAgICAgICByZXF1aXJlKGxlbihwYXlsb2FkKSA8PSAxMDAwMDAwIGFuZCBkZWNvZGVyLmVvZiBhbmQgbm90IGRlY29kZXIudW51c2VkX2RhdGEgYW5kIG5vdCBkZWNvZGVyLnVuY29uc3VtZWRfdGFpbCwKICAgICAgICAgICAgICAgICdSRVNVTFRfQ09NUFJFU1NFRF9MSU1JVCcpCiAgICBib2R5ID0gbG9hZF9qc29uKHBheWxvYWQsIGxpbWl0PTEwMDAwMDApOyBleGFjdChib2R5LCBbJ3Byb2plY3RfaWQnLCAncmVwbHknXSkKICAgIHJlcXVpcmUoYm9keVsncHJvamVjdF9pZCddID09IGNbJ3Byb2plY3RfaWQnXSwgJ1JFU1VMVF9PUklHSU5BTF9QUk9KRUNUJykKICAgIHJldHVybiBib2R5WydyZXBseSddCgoKZGVmIHJlc3VsdF9yZWFkKGMsIHMsIGJveCwgd2FpdD0wKToKICAgIHJlcXVpcmUocy52YWx1ZS5nZXQoJ2FjdGl2ZScpIGFuZCBzLnZhbHVlLmdldCgna2V5cycpLCAnT3JpZ2luYWwgcGFpcmVkIGlkZW50aXR5IHJlcXVpcmVkOyBkbyBub3QgY3JlYXRlIGFub3RoZXIgaWRlbnRpdHkuJykKICAgIHBlbmRpbmcgPSBzLnZhbHVlLmdldCgncGVuZGluZycpCiAgICByZXF1aXJlKGlzaW5zdGFuY2UocGVuZGluZywgZGljdCkgYW5kIHJlLmZ1bGxtYXRjaChyJ29wLVswLTldezZ9JywgcGVuZGluZy5nZXQoJ2lkJywgJycpKSwgJ05vIG9yaWdpbmFsIHBlbmRpbmcgb3BlcmF0aW9uIHRvIHJlYWQuJykKICAgIG9yaWdpbmFsID0gY2Fub25pY2FsKHBlbmRpbmdbJ2VudmVsb3BlJ10pCiAgICBvcmlnaW5hbF9oYXNoID0gaGFzaGxpYi5zaGEyNTYob3JpZ2luYWwpLmhleGRpZ2VzdCgpLnVwcGVyKCkKICAgIG5vbmNlID0gcGVuZGluZ1snZW52ZWxvcGUnXVsnbm9uY2UnXTsgdW5iNjQobm9uY2UsIDEyKQogICAgaCA9IHBlbmRpbmdbJ2VudmVsb3BlJ11bJ2hlYWRlciddCiAgICByZXF1aXJlKGFsbChoW2tdID09IHYgZm9yIGssIHYgaW4gcGlucyhjKS5pdGVtcygpKSBhbmQgaFsncmVxdWVzdF9pZCddID09IHBlbmRpbmdbJ2lkJ10gYW5kIGhbJ2RpcmVjdGlvbiddID09ICdyZXEnLCAnUkVTVUxUX09SSUdJTkFMX0JJTkRJTkcnKQogICAgcGF0aCwgY29udHJvbCA9IF9yZXN1bHRfY29udHJvbF9sb2FkKGMsIHMpCiAgICByZXF1aXJlKGNvbnRyb2wuZ2V0KCdvcmlnaW5hbF9zaGEyNTYnLCBvcmlnaW5hbF9oYXNoKSA9PSBvcmlnaW5hbF9oYXNoLCAnUkVTVUxUX09SSUdJTkFMX0NIQU5HRUQnKQogICAgY29udHJvbFsnb3JpZ2luYWxfc2hhMjU2J10gPSBvcmlnaW5hbF9oYXNoCiAgICBkZWFkbGluZSA9IHRpbWUubW9ub3RvbmljKCkgKyBtYXgoMCwgbWluKHdhaXQsIDE4MDApKQogICAgIyBBdCBtb3N0IHNpeHRlZW4gcmV0aXJlZCBjb250cm9sIGdlbmVyYXRpb25zIHBlciBpbnZvY2F0aW9uLCBpbmRlcGVuZGVudCBvZiBidXNpbmVzcyBudW1iZXJpbmcuCiAgICBmb3IgXyBpbiByYW5nZSgxNik6CiAgICAgICAgaWYgJ3BlbmRpbmcnIG5vdCBpbiBjb250cm9sOgogICAgICAgICAgICByZXF1aXJlKGNvbnRyb2xbJ25leHQnXSA8IDEwMDAwMDAsICdSRVNVTFRfUVVFUllfTElNSVQnKQogICAgICAgICAgICBvZmZzZXQgPSBsZW4odW5iNjQoY29udHJvbC5nZXQoJ2RhdGEnLCAnJykpKQogICAgICAgICAgICBpZiBjb250cm9sLmdldCgnY29tcGxldGUnKToKICAgICAgICAgICAgICAgICMgRWFjaCBleHBsaWNpdCBuZXcgcmVhZCBtdXN0IHJlLWNoZWNrIENVUlJFTlQgYXV0aG9yaXphdGlvbiwgbm90IHNlcnZlIGEgc3RhbGUgY2FjaGVkIHJlc3VsdC4KICAgICAgICAgICAgICAgIGZvciBrZXkgaW4gKCdkYXRhJywgJ3RvdGFsJywgJ3Jlc3VsdF9zaGEyNTYnLCAnYXV0aG9yaXphdGlvbl92ZXJzaW9uJywgJ2NvbXBsZXRlJyk6CiAgICAgICAgICAgICAgICAgICAgY29udHJvbC5wb3Aoa2V5LCBOb25lKQogICAgICAgICAgICAgICAgb2Zmc2V0ID0gMAogICAgICAgICAgICBxID0gZGljdChwcm90b2NvbD0nc2N4LXJlc3VsdC1yZWFkLXYxJywgcHJvamVjdF9pZD1jWydwcm9qZWN0X2lkJ10sIHJlcG9zaXRvcnk9Y1sncmVwb3NpdG9yeSddLAogICAgICAgICAgICAgICAgICAgICByZXBvX2lkPWNbJ3JlcG9faWQnXSwgYnJhbmNoPWNbJ2JyYW5jaCddLCBiaW5kaW5nPWNbJ2JpbmRpbmcnXSwgZXBvY2g9Y1snZXBvY2gnXSwKICAgICAgICAgICAgICAgICAgICAgb3BlcmF0aW9uX2lkPXBlbmRpbmdbJ2lkJ10sIHJlcXVlc3Rfc2hhMjU2PW9yaWdpbmFsX2hhc2gsIHJlcXVlc3Rfbm9uY2U9bm9uY2UsCiAgICAgICAgICAgICAgICAgICAgIGNoYWxsZW5nZT1zZWNyZXRzLnRva2VuX2hleCgzMiksIG9mZnNldD1vZmZzZXQsCiAgICAgICAgICAgICAgICAgICAgIGF1dGhvcml6YXRpb25fdmVyc2lvbj1jb250cm9sLmdldCgnYXV0aG9yaXphdGlvbl92ZXJzaW9uJywgJycpKQogICAgICAgICAgICByZXF1aXJlKG9mZnNldCBpbiAoMCwgMzI3NjgpLCAnUkVTVUxUX1BBR0VfT0ZGU0VUJykKICAgICAgICAgICAgb3AgPSAncnJlYWQtJTA2ZCcgJSBjb250cm9sWyduZXh0J10KICAgICAgICAgICAgIyBJc29sYXRlZCBub25jZSBib29ra2VlcGluZzogc2VhbCBuZXZlciBtdXRhdGVzIG9yIHNhdmVzIG9yaWdpbmFsIGlkZW50aXR5IHN0YXRlIGhlcmUuCiAgICAgICAgICAgIGNsYXNzIENvbnRyb2xJZGVudGl0eTogcGFzcwogICAgICAgICAgICBkZXRhY2hlZCA9IENvbnRyb2xJZGVudGl0eSgpOyBkZXRhY2hlZC52YWx1ZSA9IGRpY3Qocy52YWx1ZSkKICAgICAgICAgICAgZGV0YWNoZWQudmFsdWVbJ3R4X25vbmNlcyddID0gbGlzdChzLnZhbHVlLmdldCgndHhfbm9uY2VzJywgW10pKSArIGxpc3QoY29udHJvbC5nZXQoJ3R4X25vbmNlcycsIFtdKSkKICAgICAgICAgICAgZW52ID0gc2VhbChjLCBkZXRhY2hlZCwgb3AsIGNhbm9uaWNhbChxKSwgbGlmZXRpbWU9MTIwKQogICAgICAgICAgICBjb250cm9sWyd0eF9ub25jZXMnXSA9IChjb250cm9sLmdldCgndHhfbm9uY2VzJywgW10pICsgW2Vudlsnbm9uY2UnXV0pWy04MTkyOl0KICAgICAgICAgICAgY29udHJvbFsncGVuZGluZyddID0geydpZCc6IG9wLCAncXVlcnknOiBxLCAnZW52ZWxvcGUnOiBlbnZ9CiAgICAgICAgICAgIF9yZXN1bHRfY29udHJvbF9zYXZlKHBhdGgsIGNvbnRyb2wpICAjIE11c3QgYmUgZHVyYWJsZSBiZWZvcmUgY3JlYXRpbmcgYW55dGhpbmcgb24gR2l0SHViLgogICAgICAgIGFjdGl2ZSA9IGNvbnRyb2xbJ3BlbmRpbmcnXTsgb3AgPSBhY3RpdmVbJ2lkJ107IHEgPSBhY3RpdmVbJ3F1ZXJ5J107IGVudiA9IGFjdGl2ZVsnZW52ZWxvcGUnXQogICAgICAgIHJlcXVpcmUob3AgPT0gJ3JyZWFkLSUwNmQnICUgY29udHJvbFsnbmV4dCddIGFuZCBxWydyZXF1ZXN0X3NoYTI1NiddID09IG9yaWdpbmFsX2hhc2gKICAgICAgICAgICAgICAgIGFuZCBxWydyZXF1ZXN0X25vbmNlJ10gPT0gbm9uY2UgYW5kIHFbJ29wZXJhdGlvbl9pZCddID09IHBlbmRpbmdbJ2lkJ10sICdSRVNVTFRfQ09OVFJPTF9NSVNNQVRDSCcpCiAgICAgICAgcmF3X3F1ZXJ5ID0gY2Fub25pY2FsKGVudikKICAgICAgICB0aXAgPSBib3guZmV0Y2goKQogICAgICAgIHJhd19yZXBseSA9IGJveC5hdCh0aXAsIG1lc3NhZ2VfcGF0aChjLCAncmVzdWx0cmVwbHknLCBvcCkpCiAgICAgICAgaWYgcmF3X3JlcGx5IGlzIG5vdCBOb25lOgogICAgICAgICAgICByZXBseSwgZnJlc2ggPSBfcmVzdWx0X3JlcGx5KGMsIHMsIG9wLCBsb2FkX2pzb24ocmF3X3JlcGx5KSkKICAgICAgICAgICAgZXhhY3QocmVwbHksIFsncHJvdG9jb2wnLCAncXVlcnknLCAncXVlcnlfc2hhMjU2JywgJ3N0YXR1cycsICdhdXRob3JpemF0aW9uX3ZlcnNpb24nLCAndG90YWwnLCAncmVzdWx0X3NoYTI1NicsICdjaHVuayddKQogICAgICAgICAgICByZXF1aXJlKHJlcGx5Wydwcm90b2NvbCddID09ICdzY3gtcmVzdWx0LXJlYWQtdjEnIGFuZCByZXBseVsncXVlcnknXSA9PSBxCiAgICAgICAgICAgICAgICAgICAgYW5kIHJlcGx5WydxdWVyeV9zaGEyNTYnXSA9PSBoYXNobGliLnNoYTI1NihyYXdfcXVlcnkpLmhleGRpZ2VzdCgpLnVwcGVyKCksICdSRVNVTFRfUFJPT0ZfTUlTTUFUQ0gnKQogICAgICAgICAgICAjIEF1dGhlbnRpY2F0ZWQgZXhwaXJlZCByZXBsaWVzIG9ubHkgcmV0aXJlIGNvbnRyb2wgc2xvdHM7IG5ldmVyIGV4cG9zZSB0aGVpciBwYXlsb2FkLgogICAgICAgICAgICBpZiBub3QgZnJlc2g6CiAgICAgICAgICAgICAgICBjb250cm9sLnBvcCgncGVuZGluZycpOyBjb250cm9sWyduZXh0J10gKz0gMQogICAgICAgICAgICAgICAgX3Jlc3VsdF9jb250cm9sX3NhdmUocGF0aCwgY29udHJvbCk7IGNvbnRpbnVlCiAgICAgICAgICAgIGlmIHJlcGx5WydzdGF0dXMnXSAhPSAnYXZhaWxhYmxlJzoKICAgICAgICAgICAgICAgIHJlcXVpcmUocmVwbHlbJ3N0YXR1cyddIGluICgndW5hdXRob3JpemVkJywgJ2F1dGhvcml6YXRpb25fY2hhbmdlZCcsICdxdWVyeV9leHBpcmVkJywgJ29yaWdpbmFsX3JlcXVlc3RfbWlzc2luZycsCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAnbm90X2ZvdW5kJywgJ2NvbmZsaWN0JywgJ3Vua25vd24nLCAnYXJjaGl2ZV9pbnZhbGlkJywgJ2FyY2hpdmVfbGltaXQnLCAnb2Zmc2V0X2ludmFsaWQnKQogICAgICAgICAgICAgICAgICAgICAgICBhbmQgcmVwbHlbJ2NodW5rJ10gPT0gJycgYW5kIHJlcGx5Wyd0b3RhbCddID09IDAgYW5kIHJlcGx5WydyZXN1bHRfc2hhMjU2J10gPT0gJycKICAgICAgICAgICAgICAgICAgICAgICAgYW5kIHJlcGx5WydhdXRob3JpemF0aW9uX3ZlcnNpb24nXSA9PSAnJywgJ1JFU1VMVF9ORUdBVElWRV9QUk9PRicpCiAgICAgICAgICAgICAgICBjb250cm9sLnBvcCgncGVuZGluZycpOyBjb250cm9sWyduZXh0J10gKz0gMQogICAgICAgICAgICAgICAgZm9yIGtleSBpbiAoJ2RhdGEnLCAndG90YWwnLCAncmVzdWx0X3NoYTI1NicsICdhdXRob3JpemF0aW9uX3ZlcnNpb24nLCAnY29tcGxldGUnKTogY29udHJvbC5wb3Aoa2V5LCBOb25lKQogICAgICAgICAgICAgICAgX3Jlc3VsdF9jb250cm9sX3NhdmUocGF0aCwgY29udHJvbCkKICAgICAgICAgICAgICAgIGFuc3dlciA9IHsnc3RhdHVzJzogcmVwbHlbJ3N0YXR1cyddLCAnb3BlcmF0aW9uX2lkJzogcGVuZGluZ1snaWQnXSwgJ3BlbmRpbmdfcHJlc2VydmVkJzogVHJ1ZSwgJ2J1c2luZXNzX3JlcGxheWVkJzogRmFsc2V9CiAgICAgICAgICAgICAgICBwcmludChqc29uLmR1bXBzKGFuc3dlciwgZW5zdXJlX2FzY2lpPUZhbHNlKSk7IHJldHVybiBhbnN3ZXIKICAgICAgICAgICAgcmVxdWlyZShpc2luc3RhbmNlKHJlcGx5WydhdXRob3JpemF0aW9uX3ZlcnNpb24nXSwgc3RyKSBhbmQgMCA8IGxlbihyZXBseVsnYXV0aG9yaXphdGlvbl92ZXJzaW9uJ10pIDw9IDI1NgogICAgICAgICAgICAgICAgICAgIGFuZCB0eXBlKHJlcGx5Wyd0b3RhbCddKSBpcyBpbnQgYW5kIDAgPD0gcmVwbHlbJ3RvdGFsJ10gPD0gNjU1MzYKICAgICAgICAgICAgICAgICAgICBhbmQgaXNpbnN0YW5jZShyZXBseVsncmVzdWx0X3NoYTI1NiddLCBzdHIpIGFuZCByZS5mdWxsbWF0Y2gocidbQS1GMC05XXs2NH0nLCByZXBseVsncmVzdWx0X3NoYTI1NiddKSwgJ1JFU1VMVF9QQUdFX01FVEFEQVRBJykKICAgICAgICAgICAgZGF0YSA9IHVuYjY0KGNvbnRyb2wuZ2V0KCdkYXRhJywgJycpKTsgY2h1bmsgPSB1bmI2NChyZXBseVsnY2h1bmsnXSkKICAgICAgICAgICAgcmVxdWlyZShxWydvZmZzZXQnXSA9PSBsZW4oZGF0YSkgYW5kIGxlbihkYXRhKSA8PSByZXBseVsndG90YWwnXQogICAgICAgICAgICAgICAgICAgIGFuZCBsZW4oY2h1bmspID09IG1pbigzMjc2OCwgcmVwbHlbJ3RvdGFsJ10gLSBsZW4oZGF0YSkpLCAnUkVTVUxUX1BBR0VfTEVOR1RIJykKICAgICAgICAgICAgZm9yIGtleSBpbiAoJ3RvdGFsJywgJ3Jlc3VsdF9zaGEyNTYnLCAnYXV0aG9yaXphdGlvbl92ZXJzaW9uJyk6CiAgICAgICAgICAgICAgICByZXF1aXJlKGtleSBub3QgaW4gY29udHJvbCBvciBjb250cm9sW2tleV0gPT0gcmVwbHlba2V5XSwgJ1JFU1VMVF9QQUdFX0NPTkZMSUNUJykKICAgICAgICAgICAgICAgIGNvbnRyb2xba2V5XSA9IHJlcGx5W2tleV0KICAgICAgICAgICAgZGF0YSArPSBjaHVuawogICAgICAgICAgICBjb250cm9sWydkYXRhJ10gPSBiNjQoZGF0YSkKICAgICAgICAgICAgY29udHJvbC5wb3AoJ3BlbmRpbmcnKTsgY29udHJvbFsnbmV4dCddICs9IDEKICAgICAgICAgICAgaWYgbGVuKGRhdGEpID09IHJlcGx5Wyd0b3RhbCddOgogICAgICAgICAgICAgICAgcmVxdWlyZShoYXNobGliLnNoYTI1NihkYXRhKS5oZXhkaWdlc3QoKS51cHBlcigpID09IHJlcGx5WydyZXN1bHRfc2hhMjU2J10sICdSRVNVTFRfVE9UQUxfSEFTSCcpCiAgICAgICAgICAgICAgICByZXN1bHQgPSBfb3JpZ2luYWxfcmVzdWx0X2RlY29kZShjLCBkYXRhKQogICAgICAgICAgICAgICAgY29udHJvbFsnY29tcGxldGUnXSA9IFRydWUKICAgICAgICAgICAgICAgIF9yZXN1bHRfY29udHJvbF9zYXZlKHBhdGgsIGNvbnRyb2wpCiAgICAgICAgICAgICAgICBhbnN3ZXIgPSB7J3N0YXR1cyc6ICdvcmlnaW5hbF9yZXN1bHRfcmVhZCcsICdvcGVyYXRpb25faWQnOiBwZW5kaW5nWydpZCddLCAncmVwbHknOiByZXN1bHQsCiAgICAgICAgICAgICAgICAgICAgICAgICAgJ3BlbmRpbmdfcHJlc2VydmVkJzogVHJ1ZSwgJ2J1c2luZXNzX3JlcGxheWVkJzogRmFsc2V9CiAgICAgICAgICAgICAgICBwcmludChqc29uLmR1bXBzKGFuc3dlciwgZW5zdXJlX2FzY2lpPUZhbHNlLCBpbmRlbnQ9MikpOyByZXR1cm4gYW5zd2VyCiAgICAgICAgICAgIF9yZXN1bHRfY29udHJvbF9zYXZlKHBhdGgsIGNvbnRyb2wpCiAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgaWYgdGltZS50aW1lKCkgPiBlbnZbJ2hlYWRlciddWydleHBpcmVzJ106CiAgICAgICAgICAgICMgUHVibGlzaCBhbiB1bnNlbnQgZXhwaXJlZCBxdWVyeSBFWEFDVExZIHNvIHRoZSBob3N0IGNhbiBhdXRoZW50aWNhdGUgYW5kIHJldGlyZSB0aGUgc2xvdC4KICAgICAgICAgICAgIyBOZXZlciBsZWF2ZSBhIHNlcXVlbnRpYWwgaG9sZSwgYW5kIG5ldmVyIHJlLXNlYWwgdGhpcyBJRC4KICAgICAgICAgICAgb2NjdXBpZWQgPSBib3guYXQodGlwLCBtZXNzYWdlX3BhdGgoYywgJ3Jlc3VsdHF1ZXJ5Jywgb3ApKQogICAgICAgICAgICByZXF1aXJlKG9jY3VwaWVkIGlzIE5vbmUgb3Igb2NjdXBpZWQgPT0gcmF3X3F1ZXJ5LCAnUkVTVUxUX1FVRVJZX09DQ1VQSUVEJykKICAgICAgICAgICAgaWYgb2NjdXBpZWQgaXMgTm9uZTogYm94LmNyZWF0ZSgncmVzdWx0cXVlcnknLCBvcCwgcmF3X3F1ZXJ5KQogICAgICAgICAgICAjIEFuIGF1dGhlbnRpY2F0ZWQgb3JpZ2luYWwgcXVlcnkgcGVyc2lzdGVkIGJ5IHVzIGNhbiBiZSBhYmFuZG9uZWQgT05MWSBpbiB0aGlzIGNvbnRyb2wgbGFuZS4KICAgICAgICAgICAgY29udHJvbC5wb3AoJ3BlbmRpbmcnKTsgY29udHJvbFsnbmV4dCddICs9IDEKICAgICAgICAgICAgX3Jlc3VsdF9jb250cm9sX3NhdmUocGF0aCwgY29udHJvbCk7IGNvbnRpbnVlCiAgICAgICAgb2NjdXBpZWQgPSBib3guYXQodGlwLCBtZXNzYWdlX3BhdGgoYywgJ3Jlc3VsdHF1ZXJ5Jywgb3ApKQogICAgICAgIHJlcXVpcmUob2NjdXBpZWQgaXMgTm9uZSBvciBvY2N1cGllZCA9PSByYXdfcXVlcnksICdSRVNVTFRfUVVFUllfT0NDVVBJRUQnKQogICAgICAgIGlmIG9jY3VwaWVkIGlzIE5vbmU6IGJveC5jcmVhdGUoJ3Jlc3VsdHF1ZXJ5Jywgb3AsIHJhd19xdWVyeSkKICAgICAgICBpZiB0aW1lLm1vbm90b25pYygpID49IGRlYWRsaW5lOgogICAgICAgICAgICBhbnN3ZXIgPSB7J3N0YXR1cyc6ICdyZXN1bHRfcmVhZF9wZW5kaW5nJywgJ29wZXJhdGlvbl9pZCc6IHBlbmRpbmdbJ2lkJ10sICdwZW5kaW5nX3ByZXNlcnZlZCc6IFRydWUsICdidXNpbmVzc19yZXBsYXllZCc6IEZhbHNlfQogICAgICAgICAgICBwcmludChqc29uLmR1bXBzKGFuc3dlciwgZW5zdXJlX2FzY2lpPUZhbHNlKSk7IHJldHVybiBhbnN3ZXIKICAgICAgICB0aW1lLnNsZWVwKG1pbigyLCBtYXgoMCwgZGVhZGxpbmUgLSB0aW1lLm1vbm90b25pYygpKSkpCiAgICBhbnN3ZXIgPSB7J3N0YXR1cyc6ICdyZXN1bHRfcmVhZF9wZW5kaW5nJywgJ29wZXJhdGlvbl9pZCc6IHBlbmRpbmdbJ2lkJ10sICdwZW5kaW5nX3ByZXNlcnZlZCc6IFRydWUsICdidXNpbmVzc19yZXBsYXllZCc6IEZhbHNlfQogICAgcHJpbnQoanNvbi5kdW1wcyhhbnN3ZXIsIGVuc3VyZV9hc2NpaT1GYWxzZSkpOyByZXR1cm4gYW5zd2VyCgoKIyBFeHBsaWNpdCBuZXctd29yayBlbnRyeSBvbmx5LiBOZXZlciBpbnZva2VkIGJ5IHN0YXR1cywgaGVhcnRiZWF0IG9yIGJhY2tncm91bmQgcmV0cmllcy4KZGVmIGVuc3VyZV9hY3RpdmUoYywgcywgYm94LCB3YWl0PTE4MCk6CiAgICByZXF1aXJlKHMudmFsdWUuZ2V0KCdhY3RpdmUnKSBhbmQgcy52YWx1ZS5nZXQoJ2tleXMnKSwgJ0FDVElWQVRJT05fT1JJR0lOQUxfSURFTlRJVFlfUkVRVUlSRUQnKQogICAgcmVxdWlyZShub3Qgcy52YWx1ZS5nZXQoJ3BlbmRpbmcnKSwgJ0FDVElWQVRJT05fUEVORElOR19QUkVTRVJWRUQ6IHF1ZXJ5IHRoZSBvcmlnaW5hbCBvcGVyYXRpb247IGRvIG5vdCBzd2l0Y2ggb3IgcmVwbGF5JykKICAgIHBhdGggPSBzLnJvb3QgLyAnYWN0aXZhdGlvbi5qc29uJwogICAgc2NvcGUgPSBoYXNobGliLnNoYTI1NihjYW5vbmljYWwoYykpLmhleGRpZ2VzdCgpCiAgICBjb250cm9sID0gX3N0YXRlX2pzb24oX3N0YXRlX3JlYWQocGF0aCkpIGlmIHBhdGguZXhpc3RzKCkgZWxzZSB7J3Njb3BlJzogc2NvcGUsICduZXh0JzogMCwgJ3BoYXNlJzogJ3Byb2JlJ30KICAgIHJlcXVpcmUoY29udHJvbC5nZXQoJ3Njb3BlJykgPT0gc2NvcGUgYW5kIHR5cGUoY29udHJvbC5nZXQoJ25leHQnKSkgaXMgaW50IGFuZCAwIDw9IGNvbnRyb2xbJ25leHQnXSA8IDEwMDAwMDAsICdBQ1RJVkFUSU9OX1NUQVRFJykKICAgIGlmIGNvbnRyb2wuZ2V0KCdwaGFzZScpID09ICdkb25lJzogY29udHJvbFsncGhhc2UnXSA9ICdwcm9iZScKICAgIGRlYWRsaW5lID0gdGltZS5tb25vdG9uaWMoKSArIG1heCgwLCBtaW4od2FpdCwgMzAwKSkKICAgIHdoaWxlIFRydWU6CiAgICAgICAgaWYgJ3BlbmRpbmcnIG5vdCBpbiBjb250cm9sOgogICAgICAgICAgICBwaGFzZSA9IGNvbnRyb2wuZ2V0KCdwaGFzZScsICdwcm9iZScpCiAgICAgICAgICAgIHJlcXVpcmUocGhhc2UgaW4gKCdwcm9iZScsICdjbGFpbScsICdjb25maXJtJyksICdBQ1RJVkFUSU9OX1BIQVNFJykKICAgICAgICAgICAgcSA9IGRpY3QocHJvdG9jb2w9J3NjeC1hY3RpdmF0aW9uLXYxJywgcHJvamVjdF9pZD1jWydwcm9qZWN0X2lkJ10sIGFjdGlvbj0nY2xhaW0nIGlmIHBoYXNlID09ICdjbGFpbScgZWxzZSAncHJvYmUnLAogICAgICAgICAgICAgICAgICAgICBjaGFsbGVuZ2U9c2VjcmV0cy50b2tlbl9oZXgoMzIpLCBnZW5lcmF0aW9uPWNvbnRyb2wuZ2V0KCdnZW5lcmF0aW9uJywgJycpIGlmIHBoYXNlID09ICdjbGFpbScgZWxzZSAnJywKICAgICAgICAgICAgICAgICAgICAgYXV0aG9yaXphdGlvbl92ZXJzaW9uPWNvbnRyb2wuZ2V0KCdncmFudCcsICcnKSBpZiBwaGFzZSA9PSAnY2xhaW0nIGVsc2UgJycpCiAgICAgICAgICAgIG9wID0gJ2FjdC0lMDZkJyAlIGNvbnRyb2xbJ25leHQnXQogICAgICAgICAgICBjbGFzcyBDb250cm9sSWRlbnRpdHk6IHBhc3MKICAgICAgICAgICAgZGV0YWNoZWQgPSBDb250cm9sSWRlbnRpdHkoKTsgZGV0YWNoZWQudmFsdWUgPSBkaWN0KHMudmFsdWUpCiAgICAgICAgICAgIGRldGFjaGVkLnZhbHVlWyd0eF9ub25jZXMnXSA9IGxpc3Qocy52YWx1ZS5nZXQoJ3R4X25vbmNlcycsIFtdKSkgKyBsaXN0KGNvbnRyb2wuZ2V0KCd0eF9ub25jZXMnLCBbXSkpCiAgICAgICAgICAgIGVudiA9IHNlYWwoYywgZGV0YWNoZWQsIG9wLCBjYW5vbmljYWwocSksIGxpZmV0aW1lPTE4MCkKICAgICAgICAgICAgY29udHJvbFsndHhfbm9uY2VzJ10gPSAoY29udHJvbC5nZXQoJ3R4X25vbmNlcycsIFtdKSArIFtlbnZbJ25vbmNlJ11dKVstODE5MjpdCiAgICAgICAgICAgIGNvbnRyb2xbJ3BlbmRpbmcnXSA9IGRpY3QoaWQ9b3AsIHF1ZXJ5PXEsIGVudmVsb3BlPWVudiwgcGhhc2U9cGhhc2UpCiAgICAgICAgICAgIF9yZXN1bHRfY29udHJvbF9zYXZlKHBhdGgsIGNvbnRyb2wpCiAgICAgICAgaXRlbSA9IGNvbnRyb2xbJ3BlbmRpbmcnXTsgb3AsIHEsIGVudiwgcGhhc2UgPSBpdGVtWydpZCddLCBpdGVtWydxdWVyeSddLCBpdGVtWydlbnZlbG9wZSddLCBpdGVtWydwaGFzZSddCiAgICAgICAgcmVxdWlyZShvcCA9PSAnYWN0LSUwNmQnICUgY29udHJvbFsnbmV4dCddIGFuZCBxWydwcm9qZWN0X2lkJ10gPT0gY1sncHJvamVjdF9pZCddLCAnQUNUSVZBVElPTl9QRU5ESU5HX1NDT1BFJykKICAgICAgICByYXcgPSBjYW5vbmljYWwoZW52KTsgdGlwID0gYm94LmZldGNoKCkKICAgICAgICBvYnNlcnZlZCA9IGJveC5hdCh0aXAsIG1lc3NhZ2VfcGF0aChjLCAnYWN0aXZhdGlvbnJlcGx5Jywgb3ApKQogICAgICAgIGlmIG9ic2VydmVkIGlzIG5vdCBOb25lOgogICAgICAgICAgICByZXBseSwgZnJlc2ggPSBfYWN0aXZhdGlvbl9yZXBseShjLCBzLCBvcCwgbG9hZF9qc29uKG9ic2VydmVkKSkKICAgICAgICAgICAgZXhhY3QocmVwbHksIFsncHJvdG9jb2wnLCAncXVlcnknLCAncXVlcnlfc2hhMjU2JywgJ3N0YXR1cycsICdnZW5lcmF0aW9uJywgJ2F1dGhvcml6YXRpb25fdmVyc2lvbicsICdjdXJyZW50J10pCiAgICAgICAgICAgIHJlcXVpcmUocmVwbHlbJ3Byb3RvY29sJ10gPT0gJ3NjeC1hY3RpdmF0aW9uLXYxJyBhbmQgcmVwbHlbJ3F1ZXJ5J10gPT0gcQogICAgICAgICAgICAgICAgICAgIGFuZCByZXBseVsncXVlcnlfc2hhMjU2J10gPT0gaGFzaGxpYi5zaGEyNTYocmF3KS5oZXhkaWdlc3QoKS51cHBlcigpCiAgICAgICAgICAgICAgICAgICAgYW5kIHR5cGUocmVwbHlbJ2N1cnJlbnQnXSkgaXMgYm9vbCwgJ0FDVElWQVRJT05fUFJPT0ZfTUlTTUFUQ0gnKQogICAgICAgICAgICBjb250cm9sLnBvcCgncGVuZGluZycpOyBjb250cm9sWyduZXh0J10gKz0gMQogICAgICAgICAgICBnb29kID0gZnJlc2ggYW5kIHJlcGx5WydzdGF0dXMnXSBpbiAoJ2F2YWlsYWJsZScsICdzZWxlY3RlZCcpCiAgICAgICAgICAgIGlmIG5vdCBnb29kOgogICAgICAgICAgICAgICAgY29udHJvbFsncGhhc2UnXSA9ICdkb25lJzsgX3Jlc3VsdF9jb250cm9sX3NhdmUocGF0aCwgY29udHJvbCkKICAgICAgICAgICAgICAgIHJhaXNlIFN0b3AoJ0FDVElWQVRJT05fTk9UX0dSQU5URUQ6ICcgKyAocmVwbHlbJ3N0YXR1cyddIGlmIGZyZXNoIGVsc2UgJ2V4cGlyZWQnKSArICc7IG5vIGJ1c2luZXNzIGRpc3BhdGNoZWQnKQogICAgICAgICAgICByZXF1aXJlKGlzaW5zdGFuY2UocmVwbHlbJ2dlbmVyYXRpb24nXSwgc3RyKSBhbmQgcmUuZnVsbG1hdGNoKHInW0EtRjAtOV17NjR9JywgcmVwbHlbJ2dlbmVyYXRpb24nXSkKICAgICAgICAgICAgICAgICAgICBhbmQgaXNpbnN0YW5jZShyZXBseVsnYXV0aG9yaXphdGlvbl92ZXJzaW9uJ10sIHN0cikgYW5kIDAgPCBsZW4ocmVwbHlbJ2F1dGhvcml6YXRpb25fdmVyc2lvbiddKSA8PSAyNTYsICdBQ1RJVkFUSU9OX1BST09GX0ZJRUxEUycpCiAgICAgICAgICAgIGlmIHBoYXNlIGluICgncHJvYmUnLCAnY29uZmlybScpIGFuZCByZXBseVsnY3VycmVudCddOgogICAgICAgICAgICAgICAgY29udHJvbFsncGhhc2UnXSA9ICdkb25lJzsgX3Jlc3VsdF9jb250cm9sX3NhdmUocGF0aCwgY29udHJvbCkKICAgICAgICAgICAgICAgIHByaW50KCdPTkxJTkVfU0VMRUNURUQ6IG9yaWdpbmFsIGlkZW50aXR5IHNlbGVjdGVkOyBtb2RlbCBhY3Rpdml0eSBhbmQgYnVzaW5lc3Mgc3VjY2VzcyBhcmUgbm90IGltcGxpZWQnLCBmbHVzaD1UcnVlKQogICAgICAgICAgICAgICAgcmV0dXJuCiAgICAgICAgICAgIGlmIHBoYXNlID09ICdjb25maXJtJzoKICAgICAgICAgICAgICAgIGNvbnRyb2xbJ3BoYXNlJ10gPSAnZG9uZSc7IF9yZXN1bHRfY29udHJvbF9zYXZlKHBhdGgsIGNvbnRyb2wpCiAgICAgICAgICAgICAgICByYWlzZSBTdG9wKCdBQ1RJVkFUSU9OX1NVUEVSU0VERUQ6IGFub3RoZXIgaWRlbnRpdHkgYmVjYW1lIGN1cnJlbnQ7IG5vIGF1dG9tYXRpYyByZWNsYWltJykKICAgICAgICAgICAgY29udHJvbFsncGhhc2UnXSA9ICdjbGFpbScgaWYgcGhhc2UgPT0gJ3Byb2JlJyBlbHNlICdjb25maXJtJwogICAgICAgICAgICBjb250cm9sWydnZW5lcmF0aW9uJ10sIGNvbnRyb2xbJ2dyYW50J10gPSByZXBseVsnZ2VuZXJhdGlvbiddLCByZXBseVsnYXV0aG9yaXphdGlvbl92ZXJzaW9uJ10KICAgICAgICAgICAgX3Jlc3VsdF9jb250cm9sX3NhdmUocGF0aCwgY29udHJvbCkKICAgICAgICAgICAgY29udGludWUKICAgICAgICBvY2N1cGllZCA9IGJveC5hdCh0aXAsIG1lc3NhZ2VfcGF0aChjLCAnYWN0aXZhdGlvbnF1ZXJ5Jywgb3ApKQogICAgICAgIHJlcXVpcmUob2NjdXBpZWQgaXMgTm9uZSBvciBvY2N1cGllZCA9PSByYXcsICdBQ1RJVkFUSU9OX1FVRVJZX09DQ1VQSUVEJykKICAgICAgICBpZiBvY2N1cGllZCBpcyBOb25lOiBib3guY3JlYXRlKCdhY3RpdmF0aW9ucXVlcnknLCBvcCwgcmF3KQogICAgICAgIGlmIHRpbWUudGltZSgpID4gZW52WydoZWFkZXInXVsnZXhwaXJlcyddOgogICAgICAgICAgICAjIFJldGlyZSBleGFjdCBleHBpcmVkIGNvbnRyb2wgYnl0ZXM7IG5ldmVyIHJlc2VhbCBvciByZXBsYWNlIGEgcGVuZGluZyBidXNpbmVzcyByZXF1ZXN0LgogICAgICAgICAgICBjb250cm9sLnBvcCgncGVuZGluZycpOyBjb250cm9sWyduZXh0J10gKz0gMTsgY29udHJvbFsncGhhc2UnXSA9ICdkb25lJwogICAgICAgICAgICBfcmVzdWx0X2NvbnRyb2xfc2F2ZShwYXRoLCBjb250cm9sKQogICAgICAgICAgICByYWlzZSBTdG9wKCdBQ1RJVkFUSU9OX0VYUElSRUQ6IG9yaWdpbmFsIGNvbnRyb2wgcmVxdWVzdCBwcmVzZXJ2ZWQ7IG5vIGF1dG9tYXRpYyBmcmVzaCBjbGFpbScpCiAgICAgICAgaWYgdGltZS5tb25vdG9uaWMoKSA+PSBkZWFkbGluZToKICAgICAgICAgICAgcmFpc2UgU3RvcCgnQUNUSVZBVElPTl9XQUlUSU5HOiBjb250cm9sIHJlcXVlc3Qgc2F2ZWQ7IHJldHJ5IHRoZSBzYW1lIGVudHJ5LCBub3QgYSBidXNpbmVzcyBvcGVyYXRpb24nKQogICAgICAgIHRpbWUuc2xlZXAobWluKDUsIG1heCgwLCBkZWFkbGluZSAtIHRpbWUubW9ub3RvbmljKCkpKSkKCgpkZWYgX2FjdGl2YXRpb25fcmVwbHkoYywgcywgb3BlcmF0aW9uLCBlbnYpOgogICAgcmVxdWlyZShyZS5mdWxsbWF0Y2gocidhY3QtWzAtOV17Nn0nLCBvcGVyYXRpb24pLCAnQUNUSVZBVElPTl9JRCcpCiAgICBleGFjdChlbnYsIFsnaGVhZGVyJywgJ25vbmNlJywgJ2NpcGhlcnRleHQnLCAndGFnJ10pCiAgICBoID0gZW52WydoZWFkZXInXTsgZXhhY3QoaCwgbGlzdChwaW5zKGMpKSArIFsncmVxdWVzdF9pZCcsICdkaXJlY3Rpb24nLCAnZXhwaXJlcyddKQogICAgcmVxdWlyZShhbGwoaFtrXSA9PSB2IGZvciBrLCB2IGluIHBpbnMoYykuaXRlbXMoKSkgYW5kIGhbJ3JlcXVlc3RfaWQnXSA9PSBvcGVyYXRpb24KICAgICAgICAgICAgYW5kIGhbJ2RpcmVjdGlvbiddID09ICdyZXMnIGFuZCB0eXBlKGhbJ2V4cGlyZXMnXSkgaXMgaW50IGFuZCBoWydleHBpcmVzJ10gPiAwLCAnQUNUSVZBVElPTl9SRVBMWV9TQ09QRScpCiAgICBfLCBfLCBfLCBfLCBBRVNHQ00gPSBjcnlwdG8oKQogICAgY2lwaGVydGV4dCA9IHVuYjY0KGVudlsnY2lwaGVydGV4dCddKTsgcmVxdWlyZShsZW4oY2lwaGVydGV4dCkgPD0gMTIwMDAsICdBQ1RJVkFUSU9OX1JFUExZX0xJTUlUJykKICAgIHBsYWluID0gQUVTR0NNKHVuYjY0KHMudmFsdWVbJ2tleXMnXSwgMTI4KVszMjo2NF0pLmRlY3J5cHQodW5iNjQoZW52Wydub25jZSddLCAxMiksIGNpcGhlcnRleHQgKyB1bmI2NChlbnZbJ3RhZyddLCAxNiksIGNhbm9uaWNhbChoKSkKICAgIGJvZHkgPSBsb2FkX2pzb24ocGxhaW4sIGxpbWl0PTEyMDAwKTsgZXhhY3QoYm9keSwgWydwcm9qZWN0X2lkJywgJ3JlcGx5J10pCiAgICByZXF1aXJlKGJvZHlbJ3Byb2plY3RfaWQnXSA9PSBjWydwcm9qZWN0X2lkJ10sICdBQ1RJVkFUSU9OX1BST0pFQ1QnKQogICAgcmV0dXJuIGJvZHlbJ3JlcGx5J10sIHRpbWUudGltZSgpIC0gMSA8PSBoWydleHBpcmVzJ10gPD0gdGltZS50aW1lKCkgKyAxODAxCgoKZGVmIG1haW4oKToKICAgIHBhcnNlciA9IGFyZ3BhcnNlLkFyZ3VtZW50UGFyc2VyKGRlc2NyaXB0aW9uPSdVc2UgYW4gZXhwbGljaXRseSBhdXRob3JpemVkIFNodW5Db2RleCBjb2xsYWJvcmF0aW9uIHNlc3Npb24uJykKICAgIHBhcnNlci5hZGRfYXJndW1lbnQoJy0tY29uZmlnJywgdHlwZT1QYXRoLCBkZWZhdWx0PVBhdGgoX19maWxlX18pLnJlc29sdmUoKS53aXRoX25hbWUoJ2Nvbm5lY3Rpb24uanNvbicpKQogICAgcGFyc2VyLmFkZF9hcmd1bWVudCgnLS1yZXBvJywgdHlwZT1QYXRoLCBkZWZhdWx0PVBhdGguY3dkKCkpCiAgICBzdWIgPSBwYXJzZXIuYWRkX3N1YnBhcnNlcnMoZGVzdD0nY29tbWFuZCcsIHJlcXVpcmVkPVRydWUpCiAgICBzdWIuYWRkX3BhcnNlcignY29ubmVjdCcpOyBzdWIuYWRkX3BhcnNlcignbGlzdCcpCiAgICBvbmxpbmVfcGFyc2VyID0gc3ViLmFkZF9wYXJzZXIoJ2Vuc3VyZS1hY3RpdmUnKTsgb25saW5lX3BhcnNlci5hZGRfYXJndW1lbnQoJy0td2FpdCcsIHR5cGU9aW50LCBkZWZhdWx0PTE4MCkKICAgIHN0YXR1c19wYXJzZXIgPSBzdWIuYWRkX3BhcnNlcignc3RhdHVzJyk7IHN0YXR1c19wYXJzZXIuYWRkX2FyZ3VtZW50KCctLXdhaXQnLCB0eXBlPWludCwgZGVmYXVsdD0wLCBoZWxwPSdzZWNvbmRzIHRvIGtlZXAgcG9sbGluZyBmb3IgdGhlIHBlbmRpbmcgcmVzcG9uc2UnKQogICAgcmVzdWx0X3BhcnNlciA9IHN1Yi5hZGRfcGFyc2VyKCdyZXN1bHQtcmVhZCcpOyByZXN1bHRfcGFyc2VyLmFkZF9hcmd1bWVudCgnLS13YWl0JywgdHlwZT1pbnQsIGRlZmF1bHQ9MCkKICAgIGFjdGl2YXRlX3BhcnNlciA9IHN1Yi5hZGRfcGFyc2VyKCdhY3RpdmF0ZScpOyBhY3RpdmF0ZV9wYXJzZXIuYWRkX2FyZ3VtZW50KCctLWFwcHJvdmVkJywgYWN0aW9uPSdzdG9yZV90cnVlJykKICAgIGFjdGl2YXRlX3BhcnNlci5hZGRfYXJndW1lbnQoJy0td2FpdCcsIHR5cGU9aW50LCBkZWZhdWx0PTAsIGhlbHA9J3NlY29uZHMgdG8gcG9sbCBmb3IgdGhlIGxvY2FsIGNvbmZpcm1hdGlvbiBiZWZvcmUgZ2l2aW5nIHVwJykKICAgIGNhbGxfcGFyc2VyID0gc3ViLmFkZF9wYXJzZXIoJ2NhbGwnKTsgY2FsbF9wYXJzZXIuYWRkX2FyZ3VtZW50KCduYW1lJyk7IGNhbGxfcGFyc2VyLmFkZF9hcmd1bWVudCgnLS1hcmd1bWVudHMnLCBkZWZhdWx0PSd7fScpCiAgICBjYWxsX3BhcnNlci5hZGRfYXJndW1lbnQoJy0td2FpdCcsIHR5cGU9aW50LCBkZWZhdWx0PTkwLCBoZWxwPSdzZWNvbmRzIHRvIHdhaXQgZm9yIHRoZSByZXNwb25zZSBiZWZvcmUgcmV0dXJuaW5nIHJlc3BvbnNlX25vdF9yZWNlaXZlZCcpCiAgICBvcHRzID0gcGFyc2VyLnBhcnNlX2FyZ3MoKQogICAgYyA9IGxvYWRfanNvbihvcHRzLmNvbmZpZy5yZWFkX2J5dGVzKCkpOyB2YWxpZGF0ZV9jb25maWcoYykKICAgICMgVHJhbnNwb3J0OiBnaXQgd2hlbiBhdmFpbGFibGUgKEFyZW5hLXN0eWxlIHNhbmRib3hlcyk7IG90aGVyd2lzZSB0aGUgR2l0SHViIFJFU1QgQVBJIHdpdGggYSB0b2tlbiBmcm9tIHRoZSBlbnZpcm9ubWVudC4KICAgIHRyYW5zcG9ydCA9IG9zLmVudmlyb24uZ2V0KCdMRUJPWF9UUkFOU1BPUlQnLCBvcy5lbnZpcm9uLmdldCgnU0NYX1RSQU5TUE9SVCcsICcnKSkuc3RyaXAoKS5sb3dlcigpCiAgICB0b2tlbl9lbnYgPSBvcy5lbnZpcm9uLmdldCgnTEVCT1hfVE9LRU5fRU5WJywgb3MuZW52aXJvbi5nZXQoJ1NDWF9UT0tFTl9FTlYnLCAnR0lUSFVCX1RPS0VOJykpCiAgICBoYXZlX2dpdCA9IHNodXRpbC53aGljaCgnZ2l0JykgaXMgbm90IE5vbmUKICAgIHVzZV9hcGkgPSB0cmFuc3BvcnQgPT0gJ2FwaScgb3IgKG5vdCBoYXZlX2dpdCBhbmQgdHJhbnNwb3J0ICE9ICdnaXQnKQogICAgaWYgdXNlX2FwaToKICAgICAgICB0b2tlbiA9IG9zLmVudmlyb24uZ2V0KHRva2VuX2VudiwgJycpCiAgICAgICAgcmVwbyA9IFBhdGgob3B0cy5yZXBvKS5yZXNvbHZlKCkKICAgIGVsc2U6CiAgICAgICAgIyBFc3RhYmxpc2ggd29ya3RyZWUgYm91bmRhcnkgd2l0aG91dCBydW5uaW5nIGhvb2tzIG9yIGNoZWNrb3V0IGZpbHRlcnMuCiAgICAgICAgcmVzdWx0ID0gc3VicHJvY2Vzcy5ydW4oWydnaXQnLCAnLWMnLCAnY29yZS5mc21vbml0b3I9ZmFsc2UnLCAnLUMnLCBzdHIob3B0cy5yZXBvKSwgJ3Jldi1wYXJzZScsICctLXNob3ctdG9wbGV2ZWwnXSwKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBzdGRvdXQ9c3VicHJvY2Vzcy5QSVBFLCBzdGRlcnI9c3VicHJvY2Vzcy5QSVBFLCB0aW1lb3V0PTEwLCBjaGVjaz1GYWxzZSkKICAgICAgICByZXF1aXJlKHJlc3VsdC5yZXR1cm5jb2RlID09IDAsICdSdW4gZnJvbSB0aGUgYXV0aG9yaXplZCBHaXQgcmVwb3NpdG9yeS4nKQogICAgICAgIHJlcG8gPSBQYXRoKHJlc3VsdC5zdGRvdXQuZGVjb2RlKCkuc3RyaXAoKSkucmVzb2x2ZSgpCiAgICBib3VuZGFyeSA9IHN0YXRlX3JlcG9fYm91bmRhcnkocmVwbykgaWYgdXNlX2FwaSBlbHNlIHJlcG8KICAgIHRyeToKICAgICAgICByb290ID0gcGVyc2lzdGVudF9zZXNzaW9uX2RpcmVjdG9yeShjLCBib3VuZGFyeSkKICAgIGV4Y2VwdCAoU3RhdGVMb2NhdGlvbkVycm9yLCBPU0Vycm9yKSBhcyBlcnJvcjoKICAgICAgICByYWlzZSBTdG9wKCdQcml2YXRlIHN0YXRlIHJlY292ZXJ5IHVuYXZhaWxhYmxlOiAnICsgKHN0cihlcnJvcikgaWYgaXNpbnN0YW5jZShlcnJvciwgU3RhdGVMb2NhdGlvbkVycm9yKSBlbHNlIHR5cGUoZXJyb3IpLl9fbmFtZV9fKSkKICAgIGlmIG9wdHMuY29tbWFuZCBub3QgaW4gKCdjb25uZWN0JywgJ2FjdGl2YXRlJyk6CiAgICAgICAgcmVxdWlyZSgocm9vdCAvICdzdGF0ZS5qc29uJykuZXhpc3RzKCksICdPcmlnaW5hbCBwcml2YXRlIHN0YXRlIGlzIG1pc3Npbmc7IHByZXNlcnZlIHBlbmRpbmcgZXZpZGVuY2UgYW5kIHJlY292ZXIgdGhlIG9yaWdpbmFsIGlkZW50aXR5LCBkbyBub3QgY3JlYXRlIGEgbmV3IHNlc3Npb24uJykKICAgICMgQ29uc3RydWN0L3NhdmUgc3RhdGUgb25seSBhZnRlciBob2xkaW5nIHRoZSBwZXItc2Vzc2lvbiBleGNsdXNpdmUgbG9jay4KICAgIHJlcXVpcmUobm90IHJvb3QuaXNfc3ltbGluaygpIGFuZCAoYm91bmRhcnkgaXMgTm9uZSBvciBub3Qgcm9vdC5yZXNvbHZlKCkuaXNfcmVsYXRpdmVfdG8oYm91bmRhcnkpKSwgJ1ByaXZhdGUgc3RhdGUgbG9jYXRpb24gaXMgdW5zYWZlLicpCiAgICByb290Lm1rZGlyKG1vZGU9MG83MDAsIGV4aXN0X29rPVRydWUpOyBvcy5jaG1vZChyb290LCAwbzcwMCkKICAgIHdpdGggZXhjbHVzaXZlKHJvb3QpOgogICAgICAgIHMgPSBQcml2YXRlU3RhdGUocm9vdCwgYywgYm91bmRhcnkpCiAgICAgICAgaWYgb3B0cy5jb21tYW5kIG5vdCBpbiAoJ3Jlc3VsdC1yZWFkJywgJ2Vuc3VyZS1hY3RpdmUnKTogcmVtZW1iZXJfcmVjb3ZlcnlfY29udGV4dChjLCBzKQogICAgICAgIGlmIHMudmFsdWUuZ2V0KCdyZW5ld2VkX3Rva2VuJyk6CiAgICAgICAgICAgIHRva2VuID0gcy52YWx1ZVsncmVuZXdlZF90b2tlbiddCiAgICAgICAgICAgIHJlcXVpcmUoaXNpbnN0YW5jZSh0b2tlbiwgc3RyKSBhbmQgOCA8PSBsZW4odG9rZW4pIDw9IDQwOTYgYW5kIG5vdCBhbnkoeC5pc3NwYWNlKCkgZm9yIHggaW4gdG9rZW4pLCAnSW52YWxpZCBzYXZlZCByZW5ld2FsIHRva2VuLicpCiAgICAgICAgICAgIG9zLmVudmlyb25bdG9rZW5fZW52XSA9IHRva2VuCiAgICAgICAgICAgIG9zLmVudmlyb25bJ0dJVEhVQl9UT0tFTiddID0gdG9rZW4KICAgICAgICAgICAgb3MuZW52aXJvblsnTEVCT1hfVE9LRU4nXSA9IHRva2VuCiAgICAgICAgaWYgdXNlX2FwaToKICAgICAgICAgICAgcmVxdWlyZSh0b2tlbiwgJ09yaWdpbmFsIGFjY2VzcyBjcmVkZW50aWFsIHVuYXZhaWxhYmxlOyBwcmVzZXJ2ZSBvcmlnaW5hbCBpZGVudGl0eSBhbmQgcGVuZGluZyBvcGVyYXRpb24uJykKICAgICAgICBib3ggPSBBcGlNYWlsYm94KGMsIHRva2VuKSBpZiB1c2VfYXBpIGVsc2UgR2l0TWFpbGJveChyZXBvLCBjLCBzLnJvb3QpCiAgICAgICAgaWYgb3B0cy5jb21tYW5kID09ICdjb25uZWN0JzogY29ubmVjdChjLCBzLCBib3gpCiAgICAgICAgZWxpZiBvcHRzLmNvbW1hbmQgPT0gJ2FjdGl2YXRlJzogYWN0aXZhdGUoYywgcywgYm94LCBvcHRzLmFwcHJvdmVkLCBvcHRzLndhaXQpCiAgICAgICAgZWxpZiBvcHRzLmNvbW1hbmQgPT0gJ3N0YXR1cyc6IHN0YXR1cyhjLCBzLCBib3gsIHdhaXQ9bWF4KDAsIG9wdHMud2FpdCkpCiAgICAgICAgZWxpZiBvcHRzLmNvbW1hbmQgPT0gJ3Jlc3VsdC1yZWFkJzogcmVzdWx0X3JlYWQoYywgcywgYm94LCB3YWl0PW1heCgwLCBvcHRzLndhaXQpKQogICAgICAgIGVsaWYgb3B0cy5jb21tYW5kID09ICdlbnN1cmUtYWN0aXZlJzogZW5zdXJlX2FjdGl2ZShjLCBzLCBib3gsIHdhaXQ9bWF4KDAsIG9wdHMud2FpdCkpCiAgICAgICAgZWxpZiBvcHRzLmNvbW1hbmQgPT0gJ2xpc3QnOiByZXF1ZXN0KGMsIHMsIGJveCwgJ3Rvb2xzL2xpc3QnLCB7fSkKICAgICAgICBlbHNlOgogICAgICAgICAgICByZXF1aXJlKHJlLmZ1bGxtYXRjaChyJ1tBLVphLXpdW0EtWmEtejAtOV9dezAsOTl9Jywgb3B0cy5uYW1lKSwgJ0ludmFsaWQgdG9vbCBuYW1lLicpCiAgICAgICAgICAgIGFyZ3MgPSBsb2FkX2pzb24ob3B0cy5hcmd1bWVudHMuZW5jb2RlKCkpCiAgICAgICAgICAgIHJlcXVpcmUoaXNpbnN0YW5jZShhcmdzLCBkaWN0KSwgJ1Rvb2wgYXJndW1lbnRzIG11c3QgYmUgYW4gb2JqZWN0LicpCiAgICAgICAgICAgIHJlcXVlc3QoYywgcywgYm94LCAndG9vbHMvY2FsbCcsIHsnbmFtZSc6IG9wdHMubmFtZSwgJ2FyZ3VtZW50cyc6IGFyZ3N9LCB3YWl0PW1heCgwLCBvcHRzLndhaXQpKQoKaWYgX19uYW1lX18gPT0gJ19fbWFpbl9fJzoKICAgIHRyeToKICAgICAgICBtYWluKCkKICAgIGV4Y2VwdCAoU3RvcCwgU3RhdGVMb2NhdGlvbkVycm9yKSBhcyBleDoKICAgICAgICBwcmludChqc29uLmR1bXBzKHsnc3RhdHVzJzogJ3N0b3BwZWQnLCAnbWVzc2FnZSc6IHN0cihleCl9LCBlbnN1cmVfYXNjaWk9RmFsc2UpLCBmaWxlPXN5cy5zdGRlcnIpOyBzeXMuZXhpdCgyKQogICAgZXhjZXB0IChLZXlib2FyZEludGVycnVwdCwgRXhjZXB0aW9uKToKICAgICAgICAjIE5ldmVyIHByaW50IHJhdyBHaXQgc3RkZXJyLCBjcmVkZW50aWFsLWJlYXJpbmcgVVJMcywgcHJpdmF0ZSBrZXkgc3RhdGUgb3IgdHJhY2ViYWNrIGxvY2Fscy4KICAgICAgICBwcmludChqc29uLmR1bXBzKHsnc3RhdHVzJzogJ25vdF9jb25maXJtZWQnLCAnbWVzc2FnZSc6ICdUaGUgb3BlcmF0aW9uIHdhcyBpbnRlcnJ1cHRlZCBvciByZWplY3RlZC4gS2VlcCBwcml2YXRlIHN0YXRlIGFuZCBpbnNwZWN0IHN0YXR1czsgZG8gbm90IHJlcGVhdCBhIHBvc3NpYmx5IGRpc3BhdGNoZWQgb3BlcmF0aW9uLid9KSwgZmlsZT1zeXMuc3RkZXJyKQogICAgICAgIHN5cy5leGl0KDMpCg=='
EMBEDDED_CLIENT_SHA256 = 'e3e38dab52de828dca1d90aa297321dc89574d33118f73dd0c74de76b5c4fa80'


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
