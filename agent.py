# lebox collaboration launcher · lebox-join-v13
# Lives in the repository at /.lebox/agent.py (maintained by the lebox desktop). Run from the repository working
# directory of an Arena conversation:  python .lebox/agent.py join
# It pushes a join request on the current arena/* branch, waits for the desktop to publish this session's bootstrap,
# verifies and unpacks it outside the repository, installs the one dependency, pairs and activates. Every failure prints one
# "STOP:" line meant to be relayed to the user verbatim. No tokens, cookies or keys are requested; only your existing git auth.
import hashlib, json, os, pathlib, re, shlex, shutil, subprocess, sys, tempfile, time

VERSION = 'lebox-join-v14'
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
    """Migrate only this sandbox/recovery route when original private state proves the same project."""
    if not PROJECT_ID:
        return None
    temp = pathlib.Path(tempfile.gettempdir())
    old = temp / ('lebox-route-' + hashlib.sha256(scope.encode()).hexdigest())
    marker = old / 'branch'
    if not marker.exists():
        return None
    if old.is_symlink() or marker.is_symlink() or old.resolve().is_relative_to(pathlib.Path.cwd().resolve()):
        stop('旧自动分支缓存位置不安全', '保留原资料并核对')
    branch = marker.read_text().strip()
    if not re.fullmatch(r'agent/[0-9a-f]{32}', branch):
        stop('旧自动分支缓存损坏', '不能自动替换原会话')
    matches = set()
    for file in temp.glob('lebox-tools-*/connection.json'):
        if file.is_symlink() or file.parent.is_symlink() or file.stat().st_size > 1_200_000:
            continue
        try:
            config = json.loads(file.read_bytes(), object_pairs_hook=unique)
            if config.get('project_id') != PROJECT_ID or config.get('repository', '').lower() != repo.lower() or config.get('branch') != branch:
                continue
            binding = config.get('binding', '')
            if not re.fullmatch(r'[0-9a-f]{32}', binding):
                raise ValueError()
            private = temp / ('scx-collaboration-' + binding)
            state_file = private / 'state.json'
            if private.is_symlink() or state_file.is_symlink() or not state_file.exists() or state_file.stat().st_size > 1_200_000:
                stop('找到旧项目会话，但原私有状态不可用', '先恢复原状态，或在桌面明确处理旧会话；未创建替代身份')
            state = json.loads(state_file.read_bytes(), object_pairs_hook=unique)
            digest = hashlib.sha256(json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            if state.get('config_hash') != digest or not state.get('keys') or not state.get('hello'):
                raise ValueError()
            if state.get('closed'):
                if state.get('pending'):
                    stop('原操作结果仍未确认', '保留原状态，只查询原结果')
                continue
            matches.add(binding)
        except (OSError, ValueError, KeyError, TypeError):
            stop('旧项目会话资料不完整', '保留原资料，不自动替换身份')
    if len(matches) > 1:
        stop('旧分支存在多个项目会话', '需要核对原身份，不按时间猜选')
    return branch if matches else None


def automatic_branch(repo):
    """Remember a transport branch, never credentials or an authenticated session identity."""
    import secrets
    scope = repo.lower() + '\n' + str(pathlib.Path.cwd().resolve()) + '\n' + os.environ.get('LEBOX_RECOVERY_SELF', '')
    legacy_scope = scope
    if PROJECT_ID:
        scope += '\nproject=' + PROJECT_ID
    key = hashlib.sha256(scope.encode()).hexdigest()
    root = pathlib.Path(tempfile.gettempdir()) / ('lebox-route-' + key)
    if root.is_symlink() or root.resolve().is_relative_to(pathlib.Path.cwd().resolve()):
        stop('自动分支缓存路径不安全', '请检查临时目录，不要删除原会话资料')
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    marker = root / 'branch'
    if marker.is_symlink():
        stop('自动分支缓存不可用', '请检查临时目录')
    preferred = legacy_project_branch(repo, legacy_scope) if not marker.exists() else None
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        branch = marker.read_text().strip()
        if not re.fullmatch(r'agent/[0-9a-f]{32}', branch):
            stop('自动分支缓存损坏', '保留原状态并核对，不自动创建替代会话')
        return branch
    with os.fdopen(fd, 'w') as file:
        branch = preferred or 'agent/' + secrets.token_hex(16)
        file.write(branch); file.flush(); os.fsync(file.fileno())
    return branch


def local_resume_candidate(branch, paths, read_raw):
    """Select only by an exact descriptor hash plus private cryptographic state, not branch name."""
    matches = []
    for path in paths:
        if not BOOTSTRAP.fullmatch(path):
            continue
        binding = path.split('/session-', 1)[1].split('/')[0]
        root = pathlib.Path(tempfile.gettempdir()) / ('scx-collaboration-' + binding)
        state_file = root / 'state.json'
        if not state_file.exists():
            continue
        if root.is_symlink() or state_file.is_symlink() or root.resolve().is_relative_to(pathlib.Path.cwd().resolve()):
            stop('旧会话私有状态位置不安全', '保留原状态，未发出新的接入请求')
        try:
            raw = read_raw(path)
            if len(raw) > 1_200_000 or state_file.stat().st_size > 1_200_000:
                raise ValueError()
            bundle = json.loads(raw, object_pairs_hook=unique)
            entry = bundle['files']['connection.json']
            content = entry['content'].encode('utf-8')
            if hashlib.sha256(content).hexdigest() != entry['sha256']:
                raise ValueError()
            config = json.loads(content, object_pairs_hook=unique)
            state = json.loads(state_file.read_bytes(), object_pairs_hook=unique)
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
        import importlib.util
        spec = importlib.util.spec_from_file_location('lebox_recovery', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lebox_recovery.py'))
        if spec is None or not os.path.exists(spec.origin):
            # boot.py downloads lebox_recovery.py next to agent.py; a bare agent.py run without it just skips recovery.
            return
        lr = importlib.util.module_from_spec(spec); spec.loader.exec_module(lr)
        import secrets as _s, base64 as _b, json as _j
        rid = _s.token_hex(16); key = lr.new_key()
        payload = _j.dumps({'v': 1, 'repo': API_REPO or REPO_HINT or '', 'tools': tools_repo_hint(tools), 'token': token, 'issued': int(time.time())}).encode()
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
        print('NOTE: 恢复钥匙登记跳过：%s' % str(ex)[:120], flush=True)


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
    if cmd == 'doctor':
        doctor(verbose=True)
        print('READY: 环境检查通过，可以运行 join', flush=True)
    elif cmd == 'join':
        join()
    elif cmd in ('-h', '--help', 'help'):
        print('usage: python .lebox/agent.py [doctor|join] [--branch <name>] [--repo owner/name] [--client-id <GitHub App client id>]   (' + VERSION + '; 无授权时走 GitHub Device Flow，由用户在 github.com/login/device 批准)')
    else:
        stop('未知命令 %r' % cmd, '只支持 doctor 和 join')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        stop('已中断', '')
    except RuntimeError as ex:
        stop('git 操作失败：' + str(ex)[:200], '把这行原样告诉用户')
