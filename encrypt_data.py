#!/usr/bin/env python3
"""encrypt_data.py — enkripsi dashboard.json supaya aman di-commit ke repo PUBLIK.

Repo ini publik: data/dashboard.json (omzet, spend, ROAS, kohort) TIDAK boleh
di-commit dalam bentuk biasa. File ini mengubahnya jadi data/dashboard.json.enc
yang hanya bisa dibuka dengan password dashboard, di browser (WebCrypto).

Format .enc (JSON):
  {"v": 1, "kdf": "PBKDF2-SHA256", "iter": 600000,
   "salt": b64(16 byte), "iv": b64(12 byte), "ct": b64(ciphertext + tag GCM 16 byte)}
  kunci = PBKDF2-SHA256(password, salt, iter) → 32 byte → AES-256-GCM.
  Cara buka di browser: crypto.subtle.importKey('raw', pw, 'PBKDF2') →
  deriveKey({name:'PBKDF2', hash:'SHA-256', salt, iterations}, …, {name:'AES-GCM', length:256}) →
  decrypt({name:'AES-GCM', iv}, key, ct).

Kalau isinya sama dengan file .enc lama (selain generatedAt), file lama dibiarkan
supaya tidak ada commit kosong tiap jadwal.

ENV: DASHBOARD_PASSWORD (min. 16 karakter — file .enc bisa diunduh siapa saja
     dan ditebak offline, jadi password harus panjang & acak)

Pakai:
  python3 encrypt_data.py IN.json OUT.json.enc
  python3 encrypt_data.py --decrypt OUT.json.enc     # cek isi (print ke stdout)
"""

import argparse
import base64
import json
import os
import sys
import tempfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITER = 600000
MIN_PASSWORD = 16
ABAIKAN_SAAT_BANDING = ('generatedAt',)   # berubah tiap run, bukan perubahan data


class EncryptError(Exception):
    pass


def _key(password, salt, iterations):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(password.encode('utf-8'))


def encrypt(obj, password, iterations=ITER):
    salt, iv = os.urandom(16), os.urandom(12)
    plain = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    ct = AESGCM(_key(password, salt, iterations)).encrypt(iv, plain, None)
    b64 = lambda b: base64.b64encode(b).decode('ascii')
    return {'v': 1, 'kdf': 'PBKDF2-SHA256', 'iter': iterations, 'salt': b64(salt), 'iv': b64(iv), 'ct': b64(ct)}


def decrypt(env, password):
    if env.get('v') != 1:
        raise EncryptError('Versi format .enc tidak dikenal: %r' % env.get('v'))
    d = lambda k: base64.b64decode(env[k])
    try:
        plain = AESGCM(_key(password, d('salt'), int(env['iter']))).decrypt(d('iv'), d('ct'), None)
    except InvalidTag:
        raise EncryptError('Password salah atau file .enc rusak')
    return json.loads(plain.decode('utf-8'))


def _tanpa_volatile(obj):
    return {k: v for k, v in obj.items() if k not in ABAIKAN_SAAT_BANDING}


def sama_dengan_lama(obj, path, password):
    """True kalau file .enc lama berisi data yang sama. Password lama beda → dianggap berubah."""
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding='utf-8') as f:
            lama = decrypt(json.load(f), password)
    except (EncryptError, ValueError, KeyError):
        return False
    return _tanpa_volatile(lama) == _tanpa_volatile(obj)


def write_atomic(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.enc-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f)
            f.write('\n')
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(description='Enkripsi / dekripsi dashboard.json')
    ap.add_argument('--decrypt', action='store_true', help='buka file .enc dan print isinya')
    ap.add_argument('src')
    ap.add_argument('dst', nargs='?')
    args = ap.parse_args(argv)

    password = os.environ.get('DASHBOARD_PASSWORD', '')
    if len(password) < MIN_PASSWORD:
        raise EncryptError('DASHBOARD_PASSWORD wajib diisi, minimal %d karakter' % MIN_PASSWORD)

    with open(args.src, encoding='utf-8') as f:
        data = json.load(f)

    if args.decrypt:
        json.dump(decrypt(data, password), sys.stdout, ensure_ascii=False, indent=1)
        print()
        return 0

    if not args.dst:
        ap.error('OUT.json.enc wajib diisi')
    if sama_dengan_lama(data, args.dst, password):
        print('Tidak ada perubahan data → %s dibiarkan' % args.dst)
        return 0
    write_atomic(args.dst, encrypt(data, password))
    print('Terenkripsi → %s' % args.dst)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except EncryptError as e:
        print('GAGAL: %s' % e, file=sys.stderr)
        sys.exit(1)
