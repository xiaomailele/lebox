#!/usr/bin/env python3
# lebox bootstrap · lebox-boot-v7
# Public, reviewable, standard library only. What it does, in order:
#   1. If this sandbox can already read the private repository (git with existing GitHub auth), continue with that.
#   2. Otherwise obtain a user token through GitHub's official Device Flow for the App identified by --client-id.
#      With --pair the device code was pre-issued by the repository owner's local client and approved by the owner on
#      github.com/login/device, so nothing has to be relayed; without --pair this script prints a code for the user to enter.
#   3. Check that the token sees nothing beyond the collaboration repository and the public tools repository; stop otherwise.
#   4. Download agent.py from the public tools repository, verify its SHA-256 against --agent-sha, then run  agent.py join .
# Verified tools are staged in a private temporary directory. Durable identity is managed by agent.py.
# A trusted instruction pins this boot and the complete immutable release manifest, including recovery code.
import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request

VERSION = 'lebox-boot-v7'
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


RELEASE_FILES = ('boot.py', 'agent.py', 'lebox_recovery.py', 'README.md')
_release = None
_release_tools = ''
_release_id = ''
_tool_directory = ''


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('duplicate field')
        obj[key] = value
    return obj


def configure_release(tools, release_id, agent_sha):
    """The release id is the SHA-256 of the exact manifest bytes, supplied by trusted instructions."""
    global _release, _release_tools, _release_id
    _release = None
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', tools) or any(x in ('.', '..') for x in tools.split('/')):
        stop('工具仓库标识无效')
    if not re.fullmatch(r'[0-9a-f]{64}', release_id) or not re.fullmatch(r'[0-9a-fA-F]{64}', agent_sha):
        stop('RELEASE_PIN_REQUIRED：缺少可信版本清单或客户端哈希', '保留原身份；使用已验证的新版本说明，不跳过校验')
    st, raw = http('https://raw.githubusercontent.com/%s/HEAD/releases/%s/manifest.json' % (tools, release_id))
    if st != 200 or len(raw) > 16384 or hashlib.sha256(raw).hexdigest() != release_id:
        stop('RELEASE_MANIFEST_MISMATCH：版本清单不可用或校验失败', '不运行其他版本、不改写旧脚本')
    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(value, dict) or set(value) != {'format', 'files'} or value['format'] != 'lebox-tool-release-v1':
            raise ValueError('format')
        files = value['files']
        if not isinstance(files, dict) or set(files) != set(RELEASE_FILES):
            raise ValueError('files')
        for entry in files.values():
            if not isinstance(entry, dict) or set(entry) != {'sha256', 'bytes'} or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256']):
                raise ValueError('digest')
            if type(entry['bytes']) is not int or not 0 < entry['bytes'] <= 2_000_000:
                raise ValueError('size')
        if files['agent.py']['sha256'] != agent_sha.lower():
            raise ValueError('agent pin')
        with open(__file__, 'rb') as file:
            own = file.read(2_000_001)
        if len(own) != files['boot.py']['bytes'] or hashlib.sha256(own).hexdigest() != files['boot.py']['sha256']:
            raise ValueError('boot pin')
    except (ValueError, TypeError, KeyError, OSError):
        stop('RELEASE_MANIFEST_INVALID：版本清单与已校验启动器不一致')
    _release, _release_tools, _release_id = files, tools, release_id


def verified_tool(name):
    if _release is None or name not in RELEASE_FILES or not _tool_directory:
        stop('RELEASE_NOT_VERIFIED：禁止加载未校验工具')
    st, data = http('https://raw.githubusercontent.com/%s/HEAD/releases/%s/%s' % (_release_tools, _release_id, name))
    expected = _release[name]
    if st != 200 or len(data) != expected['bytes'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
        stop('RELEASE_FILE_MISMATCH：%s 未通过版本校验' % name, '不使用遗留本地副本，不回退到 main 根目录')
    path = os.path.join(_tool_directory, name)
    # Fresh private directory; reject any pre-existing object instead of overwriting caller files.
    with open(path, 'xb') as file:
        os.chmod(path, 0o600)
        file.write(data)
    return path


def fetch_recovery_module(tools_repo):
    if tools_repo != _release_tools or _release is None:
        stop('RELEASE_NOT_VERIFIED：恢复模块缺少可信来源')
    path = os.path.join(_tool_directory, 'lebox_recovery.py')
    if not os.path.exists(path):
        return verified_tool('lebox_recovery.py')
    with open(path, 'rb') as file:
        data = file.read(2_000_001)
    expected = _release['lebox_recovery.py']
    if len(data) != expected['bytes'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
        stop('RELEASE_FILE_MISMATCH：恢复模块被更改')
    return path


def load_recovery_module(path):
    import types
    with open(path, 'rb') as file:
        raw = file.read(2_000_001)
    expected = _release['lebox_recovery.py'] if _release is not None else {}
    if len(raw) != expected.get('bytes') or hashlib.sha256(raw).hexdigest() != expected.get('sha256'):
        stop('RELEASE_FILE_MISMATCH：禁止执行未校验恢复模块')
    module = types.ModuleType('verified_lebox_recovery')
    exec(compile(raw, path, 'exec'), module.__dict__)
    return module


def recover_token(tools_repo, recovery, repository):
    """Reopen a session without re-authorizing: fetch the public recovery blob for this rid and open it with the chat-resident key."""
    recovery_path = fetch_recovery_module(tools_repo)
    if not os.path.exists(recovery_path):
        stop('公开工具仓库里没有 lebox_recovery.py', '请用户在本地协作端点「发布 / 更新工具」')
    lr = load_recovery_module(recovery_path)
    try:
        rid, key = lr.parse_recovery(recovery)
    except ValueError as ex:
        stop('恢复串格式不对：%s' % ex, '请原样复制对话里 LEBOX_RECOVERY= 后面的整串')
    st, blob = http('https://raw.githubusercontent.com/%s/HEAD/recovery/%s.bin' % (tools_repo, rid))
    if st != 200:
        stop('恢复资料暂不可读取（HTTP %d）' % st, '保留原身份；区分网络、限流、访问和明确撤销，不重新接入')
    if blob.startswith(b'LEBOX-REVOKED-V1'):
        stop('RECOVERY_REVOKED：恢复材料已永久撤销', '保留原身份和未决操作；不重新发布该 RID')
    try:
        payload = json.loads(lr.open_(key, blob))
    except Exception:
        stop('恢复密文无法验证', '保留原资料，不创建替代身份')
    if not isinstance(payload, dict) or str(payload.get('repo', '')).lower() != repository.lower() or str(payload.get('tools', '')).lower() != tools_repo.lower():
        stop('RECOVERY_TARGET_MISMATCH：恢复资料不属于指定仓库')
    token = payload.get('token', '')
    if not isinstance(token, str) or not 1 <= len(token) <= 4096 or any(c.isspace() for c in token):
        stop('恢复资料不完整', '保留原身份，不自动重新配对')
    st, me = api('/repos/' + (payload.get('repo') or ''), token) if payload.get('repo') else (200, {})
    if st != 200:
        stop('原恢复资料的仓库访问尚未验证（HTTP %d）' % st, '保留原身份；等待合法续期或处理真实访问问题，不重新配对')
    print('OK: 已凭对话中的恢复钥匙取回授权，无需再次在 GitHub 授权', flush=True)
    os.environ['LEBOX_RECOVERY_SELF'] = recovery.strip().replace('LEBOX_RECOVERY=', '')
    return token


def publish_recovery_early(tools_repo, repo, token):
    """Right after authorization (before join): seal the token with a fresh chat-resident key, put the blob into the public tools
    repository with this very token (the App is installed there), and print the LEBOX_RECOVERY line. From now on a recycled
    sandbox can reopen without a second authorization even if join/activate never completes."""
    recovery_path = fetch_recovery_module(tools_repo)
    if not os.path.exists(recovery_path):
        print('NOTE: 公开工具仓库里没有 lebox_recovery.py，跳过恢复钥匙（请用户在本地协作端点「发布 / 更新工具」）', flush=True)
        return ''
    import base64, secrets
    lr = load_recovery_module(recovery_path)
    rid = secrets.token_hex(16); key = lr.new_key()
    payload = json.dumps({'v': 1, 'repo': repo, 'tools': tools_repo, 'token': token, 'issued': int(time.time()), 'by': 'agent'}).encode()
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
    if _release is None or tools_repo != _release_tools or sha.lower() != _release['agent.py']['sha256']:
        stop('RELEASE_NOT_VERIFIED：客户端固定哈希不匹配')
    path = verified_tool('agent.py')
    print('OK: agent.py 已通过固定版本与 SHA-256 校验', flush=True)
    return path


def main():
    p = argparse.ArgumentParser(description='lebox bootstrap: verify an immutable release, restore access, then resume/join')
    p.add_argument('--project-id', default='', help='expected project identity')
    p.add_argument('--project-route', default='', help='routing hint, not an access grant')
    p.add_argument('--repo', required=True)
    p.add_argument('--tools', default='')
    p.add_argument('--client-id', default='')
    p.add_argument('--pair', default='')
    p.add_argument('--agent-sha', required=True)
    p.add_argument('--release-id', required=True, help='trusted SHA-256 of immutable manifest bytes')
    p.add_argument('--no-run', action='store_true', help='verify/download only: no authorization, pairing or repository writes')
    p.add_argument('--recover', default='')
    p.add_argument('--upgrade-client', default='', metavar='ORIGINAL_CONNECTION_JSON', help='install verified client for the original identity; no authorization/join/state migration')
    a = p.parse_args()
    if a.upgrade_client and (a.no_run or a.recover or a.pair):
        p.error('--upgrade-client must not be combined with authorization/recovery/no-run modes')
    tools = a.tools or (a.repo.split('/')[0] + '/lebox')
    configure_release(tools, a.release_id, a.agent_sha)
    global _tool_directory
    with tempfile.TemporaryDirectory(prefix='lebox-verified-') as directory:
        _tool_directory = directory
        agent = fetch_agent(tools, a.agent_sha, '')
        fetch_recovery_module(tools)
        if a.no_run:
            print('OK: 版本清单、启动器、客户端与恢复模块校验通过；未授权、未运行、未写仓库', flush=True)
            return
        if a.upgrade_client:
            args = [sys.executable, agent, 'upgrade-client', a.upgrade_client, '--repo', a.repo]
            if a.project_id: args += ['--project-id', a.project_id]
            sys.exit(subprocess.run(args).returncode)
        token = ''
        if a.recover:
            token = recover_token(tools, a.recover, a.repo)
            os.environ['LEBOX_AUTH_SOURCE'] = 'device-flow'
        elif sandbox_can_read(a.repo):
            os.environ['LEBOX_AUTH_SOURCE'] = 'sandbox'
            print('OK: 使用平台已有 GitHub 授权，不替换平台凭据或固定分支', flush=True)
        else:
            if not a.client_id:
                stop('平台仓库访问未验证；未提供 B 接入凭据', '保留原状态，检查原平台连接；不自动新增 A 凭据')
            token = device_flow(a.client_id, a.repo, a.pair)
            check_scope(token, a.repo, tools)
            os.environ['LEBOX_AUTH_SOURCE'] = 'device-flow'
            publish_recovery_early(tools, a.repo, token)
        ensure_origin(a.repo)
        env = dict(os.environ, LEBOX_CLIENT_ID=a.client_id, LEBOX_REPO=a.repo, LEBOX_TOOLS=tools,
                   LEBOX_RELEASE_ID=a.release_id)
        if token:
            env['LEBOX_TOKEN'] = token
        print('--- verified agent.py join/resume ---', flush=True)
        args = [sys.executable, agent, 'join', '--repo', a.repo]
        if a.project_route: args += ['--project-route', a.project_route]
        if a.project_id: args += ['--project-id', a.project_id]
        sys.exit(subprocess.run(args, env=env).returncode)


if __name__ == '__main__':
    main()
