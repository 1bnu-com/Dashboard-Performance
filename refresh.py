#!/usr/bin/env python3
"""refresh.py — gabungkan ringkasan Sales Sync NOVIA + kontak Meta → data/dashboard.json

Alur:
  1. Ambil ringkasan agregat dari Web App Apps Script (?key=..). TANPA nama/HP.
  2. (Opsional) Ambil kontak WhatsApp + spend harian dari Meta Ads Insights API.
  3. Hitung per hari / minggu (Senin) / bulan:
       closingPct  = (O + F) ÷ kontak Meta × 100
       biayaClosing = spend ÷ (O + F)
       roas        = omzet ÷ spend
  4. Tulis data/dashboard.json secara atomik (file lama tidak rusak kalau gagal).

ENV (wajib):
  SALES_SYNC_URL     URL /exec Web App (tanpa ?key)
  SALES_SYNC_KEY     SECRET_KEY dari menu "📊 Dashboard → 2. Buat / lihat Secret Key"

ENV (opsional — kalau kosong, closing%/biaya/ROAS = null):
  META_ACCESS_TOKEN    token dengan izin ads_read
  META_AD_ACCOUNT_ID   angka ID akun iklan (boleh dengan/tanpa awalan act_)
  META_CONTACT_ACTION  action_type yang dihitung sebagai "kontak"
                       (default: onsite_conversion.messaging_conversation_started_7d)
  META_API_VERSION     default v23.0

Kalau Meta dikonfigurasi tapi gagal, script berhenti TANPA menulis file,
supaya dashboard tidak tiba-tiba menampilkan closing% kosong.

Pakai:
  python3 refresh.py                       # tulis data/dashboard.json
  python3 refresh.py --out x.json
  python3 refresh.py --summary-file s.json # pakai JSON ringkasan lokal (tes, tanpa internet)
"""

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_OUT = os.path.join('data', 'dashboard.json')
DEFAULT_CONTACT_ACTION = 'onsite_conversion.messaging_conversation_started_7d'
DEFAULT_META_VERSION = 'v23.0'
META_BASE = 'https://graph.facebook.com'
META_MAX_BULAN_MUNDUR = 36          # Insights API hanya menyimpan ±37 bulan
HTTP_TIMEOUT = 120                  # Apps Script bisa lambat kalau Sheet besar
SALES_FIELDS = ('omzet', 'trx', 'qty', 'O', 'F', 'R', 'omzetO', 'omzetF', 'omzetR')


class RefreshError(Exception):
    pass


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def http_get_json(url, params, label, retries=3):
    """GET + parse JSON. Pesan error tidak pernah memuat URL (berisi key/token)."""
    full = url
    if params:
        full += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(full, headers={'User-Agent': 'dashboard-refresh/1'})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                body = r.read().decode('utf-8')
            try:
                return json.loads(body)
            except ValueError:
                # Apps Script mengembalikan halaman HTML kalau deploy/akses salah
                raise RefreshError(label + ': respons bukan JSON (cek deployment Web App: akses "Anyone")')
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                detail = json.loads(e.read().decode('utf-8')).get('error', {}).get('message', '')
            except Exception:
                pass
            last = RefreshError(('%s: HTTP %s %s' % (label, e.code, detail)).strip())
            if e.code < 500 and e.code != 429:
                raise last
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = RefreshError('%s: %s' % (label, getattr(e, 'reason', e)))
        if attempt < retries - 1:
            time.sleep(2 ** (attempt + 1))
    raise last


# ---------------------------------------------------------------------------
# SUMBER DATA
# ---------------------------------------------------------------------------

def fetch_sales(url, key):
    data = http_get_json(url, {'key': key}, 'Sales Sync')
    if not isinstance(data, dict) or not data.get('ok'):
        raise RefreshError('Sales Sync: ' + str((data or {}).get('error', 'respons tidak valid')))
    return data


def fetch_meta(token, account, since, until, action, version):
    """Kembalikan {'yyyy-mm-dd': {'contacts': int, 'spend': float}} per hari.

    Diambil per potongan 90 hari: rentang panjang dengan time_increment=1
    sering ditolak Meta ("reduce the amount of data")."""
    out = {}
    start = since
    while start <= until:
        end = min(start + dt.timedelta(days=89), until)
        out.update(fetch_meta_chunk(token, account, start, end, action, version))
        start = end + dt.timedelta(days=1)
    return out


def fetch_meta_chunk(token, account, since, until, action, version):
    acc = account if account.startswith('act_') else 'act_' + account
    url = '%s/%s/%s/insights' % (META_BASE, version, acc)
    params = {
        'access_token': token,
        'level': 'account',
        'fields': 'spend,actions',
        'time_increment': 1,
        'time_range': json.dumps({'since': since.isoformat(), 'until': until.isoformat()}),
        'limit': 500,
    }
    out = {}
    while True:
        page = http_get_json(url, params, 'Meta Insights')
        if 'error' in page:
            raise RefreshError('Meta Insights: ' + str(page['error'].get('message', page['error'])))
        for row in page.get('data', []):
            contacts = 0
            for a in row.get('actions') or []:
                if a.get('action_type') == action:
                    contacts += int(float(a.get('value', 0)))
            out[row['date_start']] = {'contacts': contacts, 'spend': float(row.get('spend') or 0)}
        nxt = (page.get('paging') or {}).get('next')
        if not nxt:
            return out
        # URL "next" sudah memuat semua parameter (termasuk token)
        url, params = nxt, {}


# ---------------------------------------------------------------------------
# HITUNG
# ---------------------------------------------------------------------------

def senin(d):
    return d - dt.timedelta(days=d.weekday())


def meta_range(summary, today):
    """Mulai dari bulan transaksi paling awal, maksimal 36 bulan ke belakang."""
    months = sorted((summary.get('byMonth') or {}).keys())
    start = dt.date.fromisoformat(months[0] + '-01') if months else today.replace(day=1)
    y, m = today.year, today.month - META_MAX_BULAN_MUNDUR
    while m <= 0:
        y, m = y - 1, m + 12
    return max(start, dt.date(y, m, 1)), today


def group_meta(daily):
    by = {'byDay': {}, 'byWeek': {}, 'byMonth': {}}
    for day, v in daily.items():
        d = dt.date.fromisoformat(day)
        for bucket, k in (('byDay', day), ('byWeek', senin(d).isoformat()), ('byMonth', day[:7])):
            a = by[bucket].setdefault(k, {'contacts': 0, 'spend': 0.0})
            a['contacts'] += v['contacts']
            a['spend'] += v['spend']
    return by


def pct(a, b):
    return round(a / b * 100, 1) if b else None


def ratio(a, b, nd=2):
    return round(a / b, nd) if b else None


def merge_period(sales, meta, meta_ok):
    """Gabung satu granularitas (byDay/byWeek/byMonth). Periode hanya-Meta tetap muncul."""
    out = {}
    for k in sorted(set(sales) | set(meta)):
        s = dict(sales.get(k) or {f: 0 for f in SALES_FIELDS})
        closing = s.get('O', 0) + s.get('F', 0)
        s['closing'] = closing
        if meta_ok:
            m = meta.get(k) or {'contacts': 0, 'spend': 0.0}
            s['contacts'] = m['contacts']
            s['spend'] = round(m['spend'], 2)
            s['closingPct'] = pct(closing, m['contacts'])
            s['biayaClosing'] = ratio(m['spend'], closing, 0)
            s['roas'] = ratio(s.get('omzet', 0), m['spend'])
        else:
            s.update(contacts=None, spend=None, closingPct=None, biayaClosing=None, roas=None)
        out[k] = s
    return out


def build(summary, meta_daily, meta_info, now):
    meta_ok = meta_daily is not None
    meta_by = group_meta(meta_daily) if meta_ok else {'byDay': {}, 'byWeek': {}, 'byMonth': {}}
    out = {
        'generatedAt': now.isoformat(timespec='seconds'),
        'today': summary.get('today'),
        'sales': {'generatedAt': summary.get('generatedAt'), 'tz': summary.get('tz')},
        'meta': meta_info,
        'config': summary.get('config'),
    }
    for bucket in ('byDay', 'byWeek', 'byMonth'):
        out[bucket] = merge_period(summary.get(bucket) or {}, meta_by[bucket], meta_ok)
    for k in ('nonUtama', 'repeatPulse', 'kohort', 'vipCount', 'statusCount', 'dataQuality'):
        out[k] = summary.get(k)
    return out


def write_atomic(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.dashboard-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=False)
            f.write('\n')
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description='Refresh data dashboard dari Sales Sync + Meta Ads')
    ap.add_argument('--out', default=DEFAULT_OUT)
    ap.add_argument('--summary-file', help='pakai JSON ringkasan lokal, bukan Web App')
    args = ap.parse_args(argv)
    env = os.environ.get

    if args.summary_file:
        with open(args.summary_file, encoding='utf-8') as f:
            summary = json.load(f)
    else:
        if not env('SALES_SYNC_URL') or not env('SALES_SYNC_KEY'):
            raise RefreshError('SALES_SYNC_URL dan SALES_SYNC_KEY wajib diisi')
        summary = fetch_sales(env('SALES_SYNC_URL'), env('SALES_SYNC_KEY'))

    today = dt.date.fromisoformat(summary['today']) if summary.get('today') else dt.date.today()
    action = env('META_CONTACT_ACTION') or DEFAULT_CONTACT_ACTION
    meta_daily = None
    if env('META_ACCESS_TOKEN') and env('META_AD_ACCOUNT_ID'):
        since, until = meta_range(summary, today)
        meta_daily = fetch_meta(env('META_ACCESS_TOKEN'), env('META_AD_ACCOUNT_ID'), since, until,
                                action, env('META_API_VERSION') or DEFAULT_META_VERSION)
        meta_info = {'available': True, 'since': since.isoformat(), 'until': until.isoformat(),
                     'contactAction': action, 'days': len(meta_daily)}
    else:
        meta_info = {'available': False, 'reason': 'META_ACCESS_TOKEN / META_AD_ACCOUNT_ID belum diisi'}

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=7)))   # WIB
    out = build(summary, meta_daily, meta_info, now)
    write_atomic(args.out, out)

    dq = (summary.get('dataQuality') or {}).get('jumlah', 0)
    print('OK → %s | %d hari, %d bulan | Meta: %s | masalah data: %d' % (
        args.out, len(out['byDay']), len(out['byMonth']),
        '%d hari' % len(meta_daily) if meta_daily is not None else 'tidak dipakai', dq))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except RefreshError as e:
        print('GAGAL: %s' % e, file=sys.stderr)
        sys.exit(1)
