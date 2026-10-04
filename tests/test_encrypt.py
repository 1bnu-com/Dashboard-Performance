"""Tes encrypt_data.py, termasuk dekripsi dengan WebCrypto (Node) seperti di browser.

Jalankan: python3 -m unittest discover tests
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import encrypt_data as E  # noqa: E402

PW = 'kata-sandi-panjang-untuk-tes-123'
DATA = {'generatedAt': '2026-10-04T08:00:00+07:00', 'byMonth': {'2026-09': {'omzet': 1000000, 'closingPct': 20.0}},
        'catatan': 'ümlaut & emoji 📊'}

# Sama persis dengan cara dashboard (browser) membuka file .enc
NODE_DECRYPT = r"""
const fs = require('fs');
const env = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const b = s => Uint8Array.from(Buffer.from(s, 'base64'));
(async () => {
  const s = globalThis.crypto.subtle;
  const base = await s.importKey('raw', new TextEncoder().encode(process.argv[2]), 'PBKDF2', false, ['deriveKey']);
  const key = await s.deriveKey({name: 'PBKDF2', hash: 'SHA-256', salt: b(env.salt), iterations: env.iter},
                                base, {name: 'AES-GCM', length: 256}, false, ['decrypt']);
  const pt = await s.decrypt({name: 'AES-GCM', iv: b(env.iv)}, key, b(env.ct));
  process.stdout.write(new TextDecoder().decode(pt));
})().catch(e => { console.error(String(e)); process.exit(1); });
"""


class EncryptTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.src = os.path.join(self.dir, 'dashboard.json')
        self.dst = os.path.join(self.dir, 'data', 'dashboard.json.enc')
        with open(self.src, 'w', encoding='utf-8') as f:
            json.dump(DATA, f, ensure_ascii=False)
        self.enterContext(mock.patch.dict(os.environ, {'DASHBOARD_PASSWORD': PW}))

    def test_bolak_balik(self):
        env = E.encrypt(DATA, PW, iterations=1000)
        self.assertEqual(E.decrypt(env, PW), DATA)
        self.assertNotIn('1000000', json.dumps(env))

    def test_password_salah(self):
        env = E.encrypt(DATA, PW, iterations=1000)
        with self.assertRaisesRegex(E.EncryptError, 'Password salah'):
            E.decrypt(env, PW + 'x')

    def test_password_pendek_ditolak(self):
        with mock.patch.dict(os.environ, {'DASHBOARD_PASSWORD': 'pendek'}):
            with self.assertRaisesRegex(E.EncryptError, 'minimal'):
                E.main([self.src, self.dst])
        self.assertFalse(os.path.exists(self.dst))

    def test_tidak_ditulis_ulang_kalau_data_sama(self):
        E.main([self.src, self.dst])
        pertama = open(self.dst).read()
        # hanya generatedAt yang berubah → file lama dibiarkan
        with open(self.src, 'w', encoding='utf-8') as f:
            json.dump(dict(DATA, generatedAt='2026-10-04T11:00:00+07:00'), f)
        E.main([self.src, self.dst])
        self.assertEqual(open(self.dst).read(), pertama)
        # data berubah → ditulis ulang
        with open(self.src, 'w', encoding='utf-8') as f:
            json.dump(dict(DATA, catatan='baru'), f)
        E.main([self.src, self.dst])
        self.assertNotEqual(open(self.dst).read(), pertama)
        with open(self.dst) as f:
            self.assertEqual(E.decrypt(json.load(f), PW)['catatan'], 'baru')

    @unittest.skipUnless(shutil.which('node'), 'node tidak ada')
    def test_bisa_dibuka_webcrypto(self):
        E.main([self.src, self.dst])
        out = subprocess.run(['node', '-e', NODE_DECRYPT, self.dst, PW], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), DATA)


if __name__ == '__main__':
    unittest.main()
