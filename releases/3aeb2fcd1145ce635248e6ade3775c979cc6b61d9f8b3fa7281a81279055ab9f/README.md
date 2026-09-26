# lebox

An Agent collaborates with the repository owner's desktop through a private GitHub
mailbox. GitHub is the product channel; a development management endpoint is not a
fallback transport. Access, business identity, message expiry and platform execution
availability are separate states.

## Immutable tool releases

The desktop publishes `releases/<manifest-sha256>/` in the public tools repository,
or `.lebox/releases/<manifest-sha256>/` for the platform-authenticated in-repository
route. Each release contains `boot.py`, `agent.py`, `lebox_recovery.py`, `README.md`
and `manifest.json`. The release id is SHA-256 of the exact manifest bytes. The
manifest pins the size and SHA-256 of each payload. Its expected hash must come from
a trusted instruction, not from an untrusted file downloaded alongside the code.

Publication is additive: it never updates legacy root scripts, `.lebox/agent.py`, or
previous releases. Existing content at a release path must match exactly; otherwise
publication stops. The publisher verifies all files at one immutable commit before
returning success. A partial publication is not a completed release. Existing pins
are not waived and old clients do not silently run new code.

New instructions pin the boot hash, release id and agent hash. Raw downloads use the
repository's default `HEAD`, but every executable is authenticated by its externally
pinned hash before use. Ref movement cannot authorize different bytes. Deleting a
release can make it unavailable; the client fails closed instead of using a root alias.

## Starting or resuming

1. Obtain trusted project-specific instructions. Download boot from the exact release
   path and compare its SHA-256 before execution.
2. Use the supplied `--release-id`, `--agent-sha`, `--repo`, `--tools` and project
   routing parameters. A route is a locator, never an authorization grant.
3. Boot verifies the manifest, itself, agent and recovery module before access recovery
   or code import. `--no-run` verifies/downloads only: no authorization, join, credential
   publication or business calls. Tools are staged in a fresh private temporary directory.
4. A uses platform-provided GitHub access and its existing fixed branch. B uses its
   existing issued access/recovery materials; Device Flow is for approved onboarding,
   not a substitute for recovering a missing original identity.
5. The launcher first attempts original-identity resume. New join is not a way around
   missing private state, stopped/revoked status, or an unresolved operation.

## Persistent state and recoverable caches

The managed launcher and client use a private non-excluded directory inside HOME
(default `.lebox-state`), outside Git worktrees. `LEBOX_STATE_HOME` or `SCX_STATE_HOME`
can explicitly select a valid root. Invalid roots do not fall back to `/tmp`.
Tools, extracted bundles and dependencies are rebuildable caches; identity keys,
sequence, pending requests and branch locators are not. Migration locks and validates
the original state, copies exact bytes and retains the old file and a digest receipt.
Conflict or missing identity evidence stops recovery, rather than creating a replacement.
Workspace snapshots remain platform-dependent, capacity-limited and best effort.

## Recovery materials

`LEBOX_RECOVERY=lebox1-<rid>-<key>` is sensitive access-recovery material. Do not publish
it, logs containing it, or plaintext tokens. A desktop-issued key is also held in the
desktop's protected registry; an Agent-issued key has a different recovery capability.
The public `recovery/<rid>.bin` contains an authenticated encrypted access payload.
Its repo/tools targets must match the requested targets before a recovered token is used.
Access recovery is not a full backup of private business identity or pending operations.

The existing desktop offline resealing path and reliable publication queues must be
validated separately. A failed/404 read is not proof of revocation. Do not delete
pending requests, skip TTL, repeat a business request, or promise unconditional recovery
when all legitimate private recovery factors have been lost.

## Security model and scope

Tool publication needs consent for its exact repository/target; a token's technical
write capability is not consent. Platform credentials are not exported or replaced.
Each business identity is bound to the original project/repository/branch/binding/epoch
and cryptographic state. GitHub access alone is not proof of that identity.
Stopped/revoked Agents must stay stopped. Remote tools remain subject to desktop approval.
No perpetual model execution, external wakeup, cross-machine DPAPI portability or
unconditional exactly-once outcome is promised. Fake-transport tests are not a real
sandbox-recycle, Windows private-state or production deployment acceptance test.

## 原身份可信客户端升级（不重新接入）

新发行包仍使用四项 payload 加不可变 manifest。桌面在构建发行内容时，将实际
`agent_collaboration.py` 的精确字节和 SHA-256 嵌入被 pin 的 `agent.py`；因此升级
不需要用过期令牌重新下载会话 bootstrap，也不需要换 binding/epoch。原始源码中
的空嵌入占位符不能用来升级，必须使用经完整发行 pin 校验的生成文件。

在**已校验的新版本** boot 命令上，以
`--upgrade-client /原工具目录/connection.json` 替代 `--recover`／配对参数；也可在
已校验的新发行 `agent.py` 上运行：

```text
python3 <已校验的新发行目录>/agent.py upgrade-client /原工具目录/connection.json --repo owner/repository --project-id 原项目ID
```

这是纯代码安装，不是 `join`：只读校验原连接和原私有身份，在持久私有根的
`verified-clients/` 下新建独立目录，输出 `LEBOX_CLIENT` 和升级校验记录。不会执行
旧脚本、修改旧 pin、切分支、复制令牌到工具目录、迁移真实 state、清除 pending、
发起配对或重放业务。旧私有身份缺失、冲突或不匹配时停止；不得改成新建身份。
升级成功不代表宿主已恢复或原结果已取回。

客户端续封不再导入当前目录/临近目录的任意恢复模块。续封先核对公有工具仓库、
固定提交、文件类型、大小与 blob SHA，永久撤销标记具有本地持久拒绝记录；未知
HTTP 结果只读核对。条件写之前保存精确密文及原 CAS 基线，重启重试不重新加密，
不覆盖其他所有者或不同 scope 的内容。**没有明确 Agent 所有权标识的旧恢复密文
只读保留，不猜测归属，不自动迁移**；这类历史资料的可信归属接续仍需宿主侧完成。

新客户端把已经获得的恢复上下文和续期令牌保存到原会话的 owner-private state，
以便正常进程重启后继续；其中包含秘密，仅应留在私有持久根，不放进升级目录、
Git 仓库、日志或对话附件。它不改变平台是否真正持久化私有根的限制，也不能让
丢失的原身份或已经撤销的访问自动恢复。普通响应仍严格检查 TTL；原结果认证只读
取回通道是后续独立验收项，不能通过禁用 TTL 或重发旧请求替代。

## 历史归属证明与续期游标

在原宿主仍有完整原 Agent 发布记录时，宿主可只读核对当前公有密文与记录中的
精确字节、项目、仓库 ID、分支、binding 和 epoch，并把归属证明放入原身份认证的
新鲜续期回包。客户端核对证明的全部字段与密文摘要后，才允许接续缺少 `by` 字段
的旧 Agent 材料。明确标为 desktop、另一所有者、撤销或摘要变化的材料绝不承接。
只有公开密文、RID、能解密的恢复钥匙，或缺失原宿主记录，均不能代替可信归属证据；
这类材料继续只读保留，不凭空创造历史，不要求重建身份来掩盖缺失。

宿主续期必须真正保存原身份和精确回包之后才写 GitHub，并在核对原字节后推进
游标。旧游标只能跳过经原会话密钥认证的续期槽位，不跳过未知占位；持久化未启用、
写入失败或当前绑定变化时停止发布。客户端可认证并跳过过期的续期槽位，但不会
安装其中的过期令牌或归属证明；普通业务响应 TTL 完全不变。所有这些都是控制面
续期处理，不提交业务请求，不推进业务 operation 序号，也不清除原 pending。


## 原结果认证只读取回（独立控制通道）

在原配对身份、原 pending 和当前有效项目授权仍存在时，可运行：

```text
python agent_collaboration.py --config <原 connection.json> --repo <原仓库> result-read --wait 90
```

必须通过可信不可变升级取得带此命令的客户端；不得覆盖现用 pin 脚本、重新连接/建身份或重发原工具请求。宿主也必须升级到实现该通道的版本；旧宿主不会回答新通道。此命令不初始化 MCP、不调用工具、不重新批准、不删除 pending、不推进业务序号。成功仅表示取得原保存结果，不代表自动解除 unknown 或允许下一次业务。

协议 `scx-result-read-v1` 使用独立 rread-NNNNNN 的 resultquery/resultreply 路径、原 ECDH 身份、全 scope/原请求 SHA256/原 nonce/新 challenge/页偏移绑定。宿主直接读取当前持久授权及版本，不能用 PreapproveConversation 或 tools/list 代替。原请求密文必须仍可读取且摘要/nonce 与原 DPAPI journal 一致；读取当前或 completed 归档，不调用 Begin/Delivered/执行工具。旧回包仅由私有 journal 解密后以新鲜认证分页返回；普通业务 Open 的 TTL 不变。

每页最多32768字节，原封装载荷最多65536字节，压缩解码最多1000000字节。客户端逐页检查完整 hash/长度/授权版本，不接受混页或过期 proof。每次新的读取均重新核验当前授权，不将旧缓存当作当前授权。not_found/unknown/conflict/unauthorized 等均为负结果，不得据此重做原操作。

控制查询与响应分别先在原私有目录 `result-read.json` / `result-read-<binding>.dpapi` 保存精确字节，再发布；失败或重启只对账同一密文，不覆盖占位。已过期的未发控制查询可按原字节发布以便宿主认证拒绝并退休该槽，绝不派发其原操作。授权撤销、版本改变、换绑或响应过期后未发数据响应不再发布；在HTTP写入前再次检查。已经发出的密文或在途网络请求无法通过后来的撤销追溯收回，此边界不是实时撤回承诺。

控制状态与原业务状态分离。私有控制文件丢失、归档损坏/过大/冲突、无法证明原请求等情况保持失败关闭，不能换 RID/身份或修补摘要绕过。超4096个完成归档时返回 archive_limit 而不是假装未找到。此功能不修复真实 GitHub 凭据过期，原身份实际恢复及正式部署仍须独立验收。
