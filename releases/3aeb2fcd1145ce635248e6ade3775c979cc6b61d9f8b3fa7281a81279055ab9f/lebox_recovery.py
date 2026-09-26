# lebox recovery envelope · LBR1 — standard library only (hmac/hashlib), so boot.py can decrypt before any pip install.
#   key K (32 bytes, random, lives only in the chat as LEBOX_RECOVERY=lebox1-<rid>-<b64url(K)>)
#   enc = HMAC-SHA256(K, "lebox-recovery-enc") ; mac = HMAC-SHA256(K, "lebox-recovery-mac")
#   keystream block i = HMAC-SHA256(enc, nonce || i.to_bytes(4,'big'))  (CTR over HMAC)
#   envelope = b"LBR1" || nonce(16) || ciphertext || HMAC-SHA256(mac, b"LBR1" || nonce || ciphertext)[:32]
import base64, hashlib, hmac, secrets

MAGIC = b'LBR1'

def _b64u(b): return base64.urlsafe_b64encode(b).rstrip(b'=').decode()
def _unb64u(s): return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))

def new_key(): return secrets.token_bytes(32)

def format_recovery(rid, key): return 'lebox1-%s-%s' % (rid, _b64u(key))

def parse_recovery(text):
    text = text.strip()
    if text.startswith('LEBOX_RECOVERY='): text = text[len('LEBOX_RECOVERY='):].strip()
    parts = text.split('-', 2)
    if len(parts) != 3 or parts[0] != 'lebox1' or len(parts[1]) != 32: raise ValueError('not a lebox recovery string')
    key = _unb64u(parts[2])
    if len(key) != 32: raise ValueError('bad key length')
    return parts[1], key

def key_id(key): return hashlib.sha256(b'lebox-recovery-id' + key).hexdigest()

def _subkeys(key):
    return hmac.new(key, b'lebox-recovery-enc', hashlib.sha256).digest(), hmac.new(key, b'lebox-recovery-mac', hashlib.sha256).digest()

def _stream(enc, nonce, n):
    out = b''; i = 0
    while len(out) < n:
        out += hmac.new(enc, nonce + i.to_bytes(4, 'big'), hashlib.sha256).digest(); i += 1
    return out[:n]

def seal(key, plaintext):
    enc, mac = _subkeys(key); nonce = secrets.token_bytes(16)
    ct = bytes(a ^ b for a, b in zip(plaintext, _stream(enc, nonce, len(plaintext))))
    tag = hmac.new(mac, MAGIC + nonce + ct, hashlib.sha256).digest()
    return MAGIC + nonce + ct + tag

def open_(key, envelope):
    if len(envelope) < 4 + 16 + 32 or envelope[:4] != MAGIC: raise ValueError('bad envelope')
    nonce, ct, tag = envelope[4:20], envelope[20:-32], envelope[-32:]
    enc, mac = _subkeys(key)
    if not hmac.compare_digest(tag, hmac.new(mac, MAGIC + nonce + ct, hashlib.sha256).digest()): raise ValueError('tag mismatch')
    return bytes(a ^ b for a, b in zip(ct, _stream(enc, nonce, len(ct))))

if __name__ == '__main__':
    k = new_key(); env = seal(k, b'{"token":"ghu_x"}'); assert open_(k, env) == b'{"token":"ghu_x"}'
    rid = '0123456789abcdef0123456789abcdef'; s = format_recovery(rid, k); assert parse_recovery(s) == (rid, k)
    print('ok', s[:20], key_id(k)[:16])
