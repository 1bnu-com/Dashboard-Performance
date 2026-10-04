"""Tes refresh.py tanpa internet: Web App & Meta dipalsukan dengan server lokal.

Jalankan: python3 -m unittest discover tests
"""
import datetime as dt
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refresh  # noqa: E402

ACTION = refresh.DEFAULT_CONTACT_ACTION

SUMMARY = {
    'ok': True, 'generatedAt': '2026-10-04T08:00:00+07:00', 'today': '2026-10-04', 'tz': 'Asia/Jakarta',
    'config': {'produkUtama': ['NOVIA']},
    'byDay': {
        '2026-09-29': {'omzet': 1000000, 'trx': 3, 'qty': 3, 'O': 2, 'F': 0, 'R': 1,
                       'omzetO': 600000, 'omzetF': 0, 'omzetR': 400000},
    },
    'byWeek': {
        '2026-09-28': {'omzet': 1000000, 'trx': 3, 'qty': 3, 'O': 2, 'F': 0, 'R': 1,
                       'omzetO': 600000, 'omzetF': 0, 'omzetR': 400000},
    },
    'byMonth': {
        '2026-09': {'omzet': 1000000, 'trx': 3, 'qty': 3, 'O': 2, 'F': 0, 'R': 1,
                    'omzetO': 600000, 'omzetF': 0, 'omzetR': 400000},
    },
    'nonUtama': {}, 'repeatPulse': {'R': 1, 'basisOF': 2, 'pct': 50.0},
    'kohort': {}, 'vipCount': 0, 'statusCount': {'DITERIMA': 3}, 'dataQuality': {'jumlah': 0, 'contoh': []},
}

META_ROWS = {
    '2026-09-29': {'spend': '200000', 'actions': [{'action_type': ACTION, 'value': '8'},
                                                  {'action_type': 'link_click', 'value': '99'}]},
    '2026-09-30': {'spend': '50000', 'actions': [{'action_type': ACTION, 'value': '2'}]},
}


class Fake(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        Fake.calls.append((u.path, q))
        if u.path == '/exec':
            if q.get('key') != 'rahasia':
                return self._send(200, {'ok': False, 'error': 'unauthorized'})
            return self._send(200, SUMMARY)
        if u.path.endswith('/insights'):
            if q.get('access_token') != 'tok':
                return self._send(400, {'error': {'message': 'Invalid OAuth access token'}})
            tr = json.loads(q['time_range'])
            rows = [dict(v, date_start=d, date_stop=d) for d, v in sorted(META_ROWS.items())
                    if tr['since'] <= d <= tr['until']]
            # halaman pertama 1 baris + paging.next, untuk menguji paginasi
            if 'after' not in q and len(rows) > 1:
                nq = dict(q, after='x')
                nxt = 'http://127.0.0.1:%d%s?%s' % (self.server.server_port, u.path, urllib.parse.urlencode(nq))
                return self._send(200, {'data': rows[:1], 'paging': {'next': nxt}})
            return self._send(200, {'data': rows[1:] if 'after' in q else rows})
        self._send(404, {})


class RefreshTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(('127.0.0.1', 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = 'http://127.0.0.1:%d' % cls.srv.server_port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Fake.calls.clear()
        self.out = os.path.join(tempfile.mkdtemp(), 'data', 'dashboard.json')
        self.enterContext(mock.patch.object(refresh, 'META_BASE', self.base))
        self.enterContext(mock.patch.object(refresh.time, 'sleep', lambda s: None))

    def env(self, **kw):
        e = {'SALES_SYNC_URL': self.base + '/exec', 'SALES_SYNC_KEY': 'rahasia'}
        e.update(kw)
        return mock.patch.dict(os.environ, e, clear=True)

    def test_tanpa_meta(self):
        with self.env():
            self.assertEqual(refresh.main(['--out', self.out]), 0)
        d = json.load(open(self.out))
        self.assertFalse(d['meta']['available'])
        m = d['byMonth']['2026-09']
        self.assertEqual(m['closing'], 2)
        self.assertIsNone(m['closingPct'])
        self.assertEqual(d['repeatPulse']['pct'], 50.0)

    def test_dengan_meta(self):
        with self.env(META_ACCESS_TOKEN='tok', META_AD_ACCOUNT_ID='123'):
            refresh.main(['--out', self.out])
        d = json.load(open(self.out))
        self.assertTrue(d['meta']['available'])
        self.assertEqual(d['meta']['since'], '2026-09-01')
        m = d['byMonth']['2026-09']
        self.assertEqual(m['contacts'], 10)          # 8 + 2, link_click tidak dihitung
        self.assertEqual(m['spend'], 250000)
        self.assertEqual(m['closingPct'], 20.0)      # 2 ÷ 10
        self.assertEqual(m['biayaClosing'], 125000)
        self.assertEqual(m['roas'], 4.0)
        # hari hanya-Meta tetap muncul dengan penjualan 0
        self.assertEqual(d['byDay']['2026-09-30']['trx'], 0)
        self.assertEqual(d['byDay']['2026-09-30']['contacts'], 2)
        self.assertEqual(d['byWeek']['2026-09-28']['contacts'], 10)
        # token tidak bocor ke output
        self.assertNotIn('tok"', open(self.out).read())

    def test_key_salah_tidak_menulis(self):
        with self.env(SALES_SYNC_KEY='salah'):
            with self.assertRaisesRegex(refresh.RefreshError, 'unauthorized'):
                refresh.main(['--out', self.out])
        self.assertFalse(os.path.exists(self.out))

    def test_meta_gagal_file_lama_utuh(self):
        os.makedirs(os.path.dirname(self.out))
        with open(self.out, 'w') as f:
            f.write('{"lama": true}')
        with self.env(META_ACCESS_TOKEN='salah', META_AD_ACCOUNT_ID='act_123'):
            with self.assertRaisesRegex(refresh.RefreshError, 'Invalid OAuth') as cm:
                refresh.main(['--out', self.out])
        self.assertNotIn('salah', str(cm.exception))
        self.assertEqual(json.load(open(self.out)), {'lama': True})

    def test_meta_range_maks_36_bulan(self):
        s = {'byMonth': {'2020-01': {}}}
        since, until = refresh.meta_range(s, dt.date(2026, 10, 4))
        self.assertEqual(since, dt.date(2023, 10, 1))
        self.assertEqual(until, dt.date(2026, 10, 4))

    def test_potongan_90_hari(self):
        seen = []
        with mock.patch.object(refresh, 'fetch_meta_chunk', lambda t, a, s, u, *r: seen.append((s, u)) or {}):
            refresh.fetch_meta('t', 'a', dt.date(2026, 1, 1), dt.date(2026, 6, 30), ACTION, 'v')
        self.assertEqual(seen[0], (dt.date(2026, 1, 1), dt.date(2026, 3, 31)))
        self.assertEqual(seen[-1][1], dt.date(2026, 6, 30))
        for (a, b), (c, _) in zip(seen, seen[1:]):
            self.assertEqual(c, b + dt.timedelta(days=1))


if __name__ == '__main__':
    unittest.main()
