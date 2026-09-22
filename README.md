# lebox

lebox lets an AI Agent running anywhere (any sandbox with Python) collaborate with a project on the repository owner's own
computer, using **a private GitHub repository as the only channel**. No tunnels, no public endpoints, no shared secrets in chat.

## What is in this repository

| File | Purpose |
|---|---|
| `boot.py` | Tiny bootstrap (standard library only). Gets GitHub authorization if the sandbox has none, verifies `agent.py` by SHA-256, starts it. |
| `agent.py` | Launcher: pushes a join request on a dedicated branch, waits for the owner's client to publish the session bundle, verifies it file by file, sets up the encrypted mailbox and starts the collaboration client. |

Both files are published and updated automatically by the owner's local client; their SHA-256 values are printed in every
set of instructions so a reviewer can verify what will run.

## How a session starts

1. The Agent runs `boot.py --repo <owner>/<private-repo> [--client-id … --pair …] --agent-sha …`.
2. **Authorization** — one of:
   * the sandbox already has GitHub access (e.g. platforms that inject the user's GitHub login): nothing else happens;
   * otherwise GitHub **Device Flow** with the owner's GitHub App (`--client-id`). With `--pair`, the owner already approved a
     pre-issued code from a popup on their computer; without it, `boot.py` prints an 8-character code for the owner to enter at
     <https://github.com/login/device>. The token is issued by GitHub directly to the sandbox; it is never sent anywhere else.
3. `boot.py` checks that the token can see **only** the collaboration repository and stops otherwise.
4. `agent.py join` pushes an empty commit `lebox: join` on a dedicated branch. The owner's client, which watches the repository,
   creates a session folder `.lebox/session-<id>/` on that branch containing a bootstrap bundle.
5. `agent.py` verifies the bundle (every file hashed), unpacks it **outside** the repository, and performs an ECDH key exchange
   with the owner's client. From then on requests and responses are AES-GCM sealed JSON files in the session folder; the
   repository only ever holds ciphertext, the bundle, and the public scripts.

## What it does not do

* Never modifies `main`/`master` or any file outside `.lebox/` on the session branch.
* Never reads credentials other than the one GitHub authorization it was given; the token stays in process memory and git's
  in-memory credential cache.
* Never contacts any host other than `github.com`, `api.github.com`, `raw.githubusercontent.com`.
* Every tool call the Agent makes is executed on the owner's computer under the owner's own approval rules.

## Security model in one paragraph

Authorization = the owner's GitHub App installed on exactly one private repository with *Contents: read/write*. Identity of a
session = a branch on that repository plus an ECDH pairing that the owner's client confirms. A leaked token is worth at most
"read/write ciphertext in that one repository until the owner revokes it". A leaked pairing code is worth nothing unless the
owner approves it on github.com. Reviewers can read both scripts here and compare hashes before running anything.

## Verifying before running

```
curl -fsSLO https://raw.githubusercontent.com/<owner>/lebox/main/boot.py && sha256sum boot.py
curl -fsSLO https://raw.githubusercontent.com/<owner>/lebox/main/agent.py && sha256sum agent.py
```
Compare with the values in the instructions you received.
