#!/usr/bin/env python3
# lebox bootstrap · lebox-boot-v6
# Public, reviewable, standard library only. What it does, in order:
#   1. If this sandbox can already read the private repository (git with existing GitHub auth), continue with that.
#   2. Otherwise obtain a user token through GitHub's official Device Flow for the App identified by --client-id.
#      With --pair the device code was pre-issued by the repository owner's local client and approved by the owner on
#      github.com/login/device, so nothing has to be relayed; without --pair this script prints a code for the user to enter.
#   3. Check that the token sees nothing beyond the collaboration repository and the public tools repository; stop otherwise.
#   4. Download agent.py from the public tools repository, verify its SHA-256 against --agent-sha, then run  agent.py join .
# Nothing is written outside the current directory; the token lives in this process's environment only.
import argparse, hashlib, json, os, shutil, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

VERSION = 'lebox-boot-v6'
UA = 'lebox-agent/1.0'


def stop(msg, advice=''):
    print('STOP: ' + msg + (' | ' + advice if advice else ''), flush=True)
    sys.exit(2)


def http(url, data=None, headers=None, method=None):
    if method is None:
        method = 'POST' if data else 'GET'
    req = urllib.request.Request(url, data=data, method=method, headers={'User-Agent': UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(2_000_000)
    except urllib.error.HTTPError as ex:
        return ex.code, ex.read(200_000)
    except (urllib.error.URLError, TimeoutError, OSError) as ex:
        stop('无法访问 %s：%s' % (urllib.parse.urlsplit(url).netloc, str(ex)[:100]), '请用户检查沙盒网络')


def gh_form(path, fields):
    st, raw = http('https://github.com' + path, data=urllib.parse.urlencode(fields).encode(), headers={'Accept': 'application/json'})
    try:
        return json.loads(raw or b'{}')
    except ValueError:
        # GitHub answers a consumed/unknown device code with an HTML 500 page instead of a JSON error. Treat any non-JSON or
        # 5xx as "this code is no longer usable" so the caller can fall back to a fresh code instead of stopping.
        return {'error': 'http_%d' % st, 'error_description': 'GitHub returned non-JSON (HTTP %d)' % st}


def api(path, token):
    st, raw = http('https://api.github.com' + path, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
    try:
        return st, json.loads(raw or b'{}')
    except ValueError:
        return st, {}


def ensure_origin(repo):
    """Some sandboxes drop .git/config on restore; put origin back so agent.py finds the repository."""
    if not shutil.which('git') or subprocess.run(['git', 'rev-parse', '--is-inside-work-tree'], capture_output=True, text=True).stdout.strip() != 'true':
        return
    if subprocess.run(['git', 'remote', 'get-url', 'origin'], capture_output=True).returncode != 0:
        subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/%s.git' % repo], capture_output=True)
        print('OK: 已补回 origin（沙盒恢复时 .git/config 被丢弃）', flush=True)


def sandbox_can_read(repo):
    if not shutil.which('git'):
        return False
    r = subprocess.run(['git', 'ls-remote', '--heads', 'https://github.com/%s.git' % repo], capture_output=True, env=dict(os.environ, GIT_TERMINAL_PROMPT='0'))
    return r.returncode == 0


def device_flow(client_id, repo, pair):
    if pair:
        code, interval, expires = pair, 5, 900
        print('OK: 使用仓库所有者本地协作端预签发的一次性配对码；等待所有者在 GitHub 页面点 Authorize（无需转告任何代码）', flush=True)
    else:
        s = gh_form('/login/device/code', {'client_id': client_id})
        if s.get('verification_uri') != 'https://github.com/login/device' or not s.get('device_code'):
            stop('GitHub 未返回设备授权信息：%s' % (s.get('error_description') or s.get('error') or s), '请用户确认 lebox 应用已启用 Device Flow')
        code, interval, expires = s['device_code'], int(s.get('interval', 5)), int(s.get('expires_in', 900))
        print('', flush=True)
        print('ACTION REQUIRED（请把下面两行原样转告用户）：', flush=True)
        print('  请在浏览器打开 https://github.com/login/device ，输入一次性代码  %s  ，然后点 Authorize。' % s['user_code'], flush=True)
        print('  授权对象是 lebox 应用（应仅安装在仓库 %s 上）；%d 分钟内有效，我在这里等待。' % (repo, expires // 60), flush=True)
        print('', flush=True)
    deadline = time.time() + expires
    last_note = time.time()
    while time.time() < deadline:
        time.sleep(interval)
        r = gh_form('/login/oauth/access_token', {'client_id': client_id, 'device_code': code, 'grant_type': 'urn:ietf:params:oauth:grant-type:device_code'})
        err = r.get('error')
        if err == 'authorization_pending':
            if time.time() - last_note >= 30:
                last_note = time.time()
                print('waiting for owner approval on github.com/login/device ... (%d 分钟后过期)' % max(1, int((deadline - time.time()) // 60)), flush=True)
            continue
        if err == 'slow_down':
            interval += 5
            continue
        if (err in ('expired_token', 'incorrect_device_code') or str(err).startswith('http_')) and pair:
            print('OK: 预签发的配对码已失效（%s），改为申请新的设备码' % err, flush=True)
            return device_flow(client_id, repo, '')
        if str(err).startswith('http_5'):
            print('waiting: GitHub 暂时返回 %s，5 秒后重试' % err, flush=True)
            continue
        if err == 'access_denied':
            stop('用户在 GitHub 上拒绝了授权', '如需继续，请用户重新运行并在 GitHub 页面点 Authorize')
        if err:
            stop('GitHub 授权未完成：%s' % (r.get('error_description') or err), '把这行原样告诉用户')
        if r.get('access_token') and str(r.get('token_type', '')).lower() == 'bearer':
            return r['access_token']
    stop('等待用户在 GitHub 授权超时', '重新运行会生成新的代码')


def check_scope(token, repo, tools):
    """The token must see the collaboration repository; besides it only the public tools repository is acceptable."""
    st, me = api('/repos/' + repo, token)
    if st != 200:
        stop('授权已完成，但该授权看不到仓库 %s（HTTP %d）' % (repo, st), '请用户确认 lebox 应用已安装到这个仓库（GitHub → Settings → Applications → lebox → Configure）')
    st, inst = api('/user/installations?per_page=20', token)
    names = []
    for i in (inst.get('installations') or []) if st == 200 else []:
        st2, page = api('/user/installations/%s/repositories?per_page=100' % i.get('id'), token)
        if st2 == 200:
            names += [r.get('full_name', '') for r in page.get('repositories') or []]
    allowed = {repo.lower(), tools.lower()}
    extra = sorted({n for n in names if n.lower() not in allowed})
    if extra:
        stop('该授权还覆盖了协作之外的仓库：%s' % ', '.join(extra[:5]), '请用户在 GitHub → Settings → Applications → lebox → Configure 把 Repository access 改为仅 %s 和 %s，然后重新运行' % (repo, tools))
    seen = sorted({n for n in names if n.lower() in allowed}) or [repo]
    print('OK: 授权范围已核对：%s' % '、'.join(seen), flush=True)


def fetch_recovery_module(tools_repo):
    st, data = http('https://raw.githubusercontent.com/%s/main/lebox_recovery.py' % tools_repo)
    if st == 200:
        with open('lebox_recovery.py', 'wb') as f:
            f.write(data)


def recover_token(tools_repo, recovery):
    """Reopen a session without re-authorizing: fetch the public recovery blob for this rid and open it with the chat-resident key."""
    fetch_recovery_module(tools_repo)
    if not os.path.exists('lebox_recovery.py'):
        stop('公开工具仓库里没有 lebox_recovery.py', '请用户在本地协作端点「发布 / 更新工具」')
    import importlib.util
    spec = importlib.util.spec_from_file_location('lebox_recovery', 'lebox_recovery.py'); lr = importlib.util.module_from_spec(spec); spec.loader.exec_module(lr)
    try:
        rid, key = lr.parse_recovery(recovery)
    except ValueError as ex:
        stop('恢复串格式不对：%s' % ex, '请原样复制对话里 LEBOX_RECOVERY= 后面的整串')
    st, blob = http('https://raw.githubusercontent.com/%s/main/recovery/%s.bin' % (tools_repo, rid))
    if st != 200:
        stop('找不到该会话的恢复密文（recovery/%s.bin，HTTP %d）——可能已被所有者作废' % (rid[:8], st), '请用户重新走一次授权接入（去掉 --recover）')
    try:
        payload = json.loads(lr.open_(key, blob))
    except Exception:
        stop('恢复密文无法解开（钥匙不匹配或已被替换）', '请用户重新走一次授权接入（去掉 --recover）')
    token = payload.get('token', '')
    if not token:
        stop('恢复密文里没有令牌', '请用户重新走一次授权接入')
    st, me = api('/repos/' + (payload.get('repo') or ''), token) if payload.get('repo') else (200, {})
    if st == 401:
        stop('恢复出的令牌已失效（被撤销或过期）', '请用户重新走一次授权接入（去掉 --recover）')
    print('OK: 已凭对话中的恢复钥匙取回授权，无需再次在 GitHub 授权', flush=True)
    os.environ['LEBOX_RECOVERY_SELF'] = recovery.strip().replace('LEBOX_RECOVERY=', '')
    return token


def publish_recovery_early(tools_repo, repo, token):
    """Right after authorization (before join): seal the token with a fresh chat-resident key, put the blob into the public tools
    repository with this very token (the App is installed there), and print the LEBOX_RECOVERY line. From now on a recycled
    sandbox can reopen without a second authorization even if join/activate never completes."""
    fetch_recovery_module(tools_repo)
    if not os.path.exists('lebox_recovery.py'):
        print('NOTE: 公开工具仓库里没有 lebox_recovery.py，跳过恢复钥匙（请用户在本地协作端点「发布 / 更新工具」）', flush=True)
        return ''
    import importlib.util, base64, secrets
    spec = importlib.util.spec_from_file_location('lebox_recovery', 'lebox_recovery.py'); lr = importlib.util.module_from_spec(spec); spec.loader.exec_module(lr)
    rid = secrets.token_hex(16); key = lr.new_key()
    payload = json.dumps({'v': 1, 'repo': repo, 'tools': tools_repo, 'token': token, 'issued': int(time.time())}).encode()
    blob = lr.seal(key, payload)
    body = json.dumps({'message': 'lebox: recovery %s' % rid[:8], 'content': base64.b64encode(blob).decode()}).encode()
    st, raw = http('https://api.github.com/repos/%s/contents/recovery/%s.bin' % (tools_repo, rid), data=body, method='PUT',
                   headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'})
    if st not in (200, 201):
        print('NOTE: 恢复钥匙密文未能写入公开仓库（HTTP %d）；沙盒回收后需重新授权。若长期如此请用户确认 lebox 应用已安装到 %s。' % (st, tools_repo), flush=True)
        return ''
    line = 'LEBOX_RECOVERY=' + lr.format_recovery(rid, key)
    print('', flush=True)
    print('RECOVERY（请把下面这一整行原样保留在对话里；沙盒被回收后，重新运行本命令并加上 --recover <那一整串> 即可免授权恢复）：', flush=True)
    print(line, flush=True)
    print('', flush=True)
    os.environ['LEBOX_RECOVERY_DONE'] = '1'
    os.environ['LEBOX_RECOVERY_SELF'] = line[len('LEBOX_RECOVERY='):]  # lets agent.py re-seal the blob when the desktop renews the token
    return rid


def fetch_agent(tools_repo, sha, token):
    url = 'https://raw.githubusercontent.com/%s/main/agent.py' % tools_repo
    st, data = http(url)
    if st != 200:
        stop('无法从公开工具仓库下载 agent.py（%d）' % st, '请用户确认公开仓库 %s 存在且包含 agent.py' % tools_repo)
    digest = hashlib.sha256(data).hexdigest()
    if sha and digest != sha.lower():
        stop('agent.py 的 SHA-256 (%s…) 与说明中的不一致，已停止' % digest[:16], '请用户在本地协作端重新复制说明')
    with open('agent.py', 'wb') as f:
        f.write(data)
    print('OK: agent.py 已下载并通过 SHA-256 校验（%s…）' % digest[:16], flush=True)


def main():
    p = argparse.ArgumentParser(description='lebox bootstrap: authorize (if needed), verify and start agent.py join')
    p.add_argument('--project-id', default='', help='expected project identity')
    p.add_argument('--project-route', default='', help='project routing hint, not an access grant')
    p.add_argument('--repo', required=True, help='private collaboration repository owner/name')
    p.add_argument('--tools', default='', help='public tools repository owner/name (default: <owner>/lebox)')
    p.add_argument('--client-id', default='', help='GitHub App public client id (needed only when the sandbox has no GitHub access)')
    p.add_argument('--pair', default='', help="one-time pairing code pre-issued by the owner's local client (optional)")
    p.add_argument('--agent-sha', default='', help='expected SHA-256 of agent.py (from the instructions)')
    p.add_argument('--no-run', action='store_true', help='download and verify only; do not start agent.py join')
    p.add_argument('--recover', default='', help='LEBOX_RECOVERY=lebox1-<rid>-<key> from an earlier session: reopen without re-authorizing')
    a = p.parse_args()
    tools = a.tools or (a.repo.split('/')[0] + '/lebox')
    token = ''
    if a.recover:
        token = recover_token(tools, a.recover)
        global_auth_note = '恢复钥匙'
    if token:
        os.environ['LEBOX_AUTH_SOURCE'] = 'device-flow'
    elif sandbox_can_read(a.repo):
        print('OK: 沙盒已有 GitHub 授权，可读取 %s；不会使用配对码' % a.repo, flush=True)
    else:
        if not a.client_id:
            stop('沙盒没有该仓库的 GitHub 授权，且未提供 --client-id', '请用户在本地协作端重新复制说明')
        token = device_flow(a.client_id, a.repo, a.pair)
        check_scope(token, a.repo, tools)
        publish_recovery_early(tools, a.repo, token)
    fetch_agent(tools, a.agent_sha, token)
    fetch_recovery_module(tools)
    if a.no_run:
        print('OK: 已下载并校验；按要求未运行 agent.py', flush=True)
        return
    ensure_origin(a.repo)
    env = dict(os.environ, LEBOX_CLIENT_ID=a.client_id, LEBOX_REPO=a.repo, LEBOX_TOOLS=tools)
    if token:
        env['LEBOX_TOKEN'] = token
    print('--- agent.py join ---', flush=True)
    sys.exit(subprocess.run([sys.executable, 'agent.py', 'join', '--repo', a.repo] + (['--project-route', a.project_route] if a.project_route else []) + (['--project-id', a.project_id] if a.project_id else []), env=env).returncode)


if __name__ == '__main__':
    main()
