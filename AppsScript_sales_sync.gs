/**
 * AppsScript_sales_sync.gs — Sales Sync NOVIA untuk Performance Dashboard
 * Blueprint v1 (bagian 4 langkah 3 + bagian 5a).
 *
 * ENDPOINT (semua WAJIB pakai &key=SECRET_KEY):
 *   ?key=..               → ringkasan agregat TANPA nama/HP (dibaca refresh.py)
 *   ?key=..&fu2=1         → feed checklist follow-up + VIP (dipakai CS, berisi nama/HP)
 *   ?key=..&mark=1&order=NO_ORDER&stage=D1|D3|D7|REPEAT   → tombol ✓ (nulis timestamp)
 *   tambah &undo=1 pada mark untuk membatalkan centang
 *
 * KEPUTUSAN BISNIS (dikonfirmasi pemilik):
 *   - Produk: NOVIA saja. Durasi stok dibaca dari tab Panduan ("Durasi stok per box (hari)").
 *   - Status BATAL & RETUR: keluar dari omzet, closing, repeat, kohort, dan checklist.
 *   - PC = pembelian internal, selalu dikecualikan.
 *   - Kolom "Sales" = omzet rupiah. Nama sales/advertiser di kolom "NAMA SALES".
 */

const CFG = {
  SHEET_SALES: 'Sales',
  SHEET_PANDUAN: 'Panduan',
  TZ: 'Asia/Jakarta',
  PRODUK_UTAMA: ['NOVIA'],
  DURASI: { NOVIA: 60 },                       // hari per box; NOVIA ditimpa nilai di tab Panduan
  DURASI_LABEL_PANDUAN: 'DURASI STOK PER BOX (HARI)',
  STATUS_KELUAR: ['BATAL', 'RETUR'],
  OFR_VALID: ['O', 'F', 'R'],                  // PC tidak termasuk → dikecualikan
  GRACE_KOHORT: 15,                            // hari setelah stok habis sebelum dinilai
  REPEAT_SEBELUM_HABIS: 8,                     // FU repeat muncul H-8 sebelum stok habis
  NYANGKUT_HARI: 14,                           // tanpa TGL TERIMA > 14 hari → seksi nyangkut
  PULSE_MIN_UMUR: 30,                          // pembagi Repeat %: transaksi O+F berumur > 30 hari
  FU_MAX_TELAT: 14,                            // tahap FU telat > 14 hari tidak ditampilkan (dihitung "kedaluwarsa")
  VIP_TOP: 20,
  TES_HARI_INI: ''                             // KOSONGKAN di produksi. Isi 'yyyy-mm-dd' hanya untuk tes.
};

const FU_STAGES = [
  { key: 'D1', col: 'FU D1 SELESAI', offset: 0 },
  { key: 'D3', col: 'FU D3 SELESAI', offset: 2 },
  { key: 'D7', col: 'FU D7 SELESAI', offset: 6 },
  { key: 'REPEAT', col: 'FU REPEAT SELESAI', offset: null }
];

const H = {
  ORDER: 'NO ORDER', STATUS: 'STATUS ORDER', TGL: 'TANGGAL', NAME: 'NAME', HP: 'NO HP',
  RESI: 'TRACKING LIST', PRODUK: 'PRODUCT', DETAIL: 'PRODUCT DETAIL', OFR: 'O/F/R',
  QTY: 'QTY', SALES: 'SALES', TERIMA: 'TGL TERIMA', CS: 'NAMA CS', NAMA_SALES: 'NAMA SALES'
};
const WAJIB = [H.ORDER, H.STATUS, H.TGL, H.NAME, H.HP, H.RESI, H.PRODUK, H.DETAIL, H.OFR, H.QTY, H.SALES];

/* =========================================================================
 * WEB APP
 * ========================================================================= */

function doGet(e) {
  const p = (e && e.parameter) || {};
  try {
    if (!cekKey_(p.key)) return json_({ ok: false, error: 'unauthorized' });
    if (p.mark) return json_(mark_(p));
    if (p.fu2) return json_(fu2_());
    return json_(summary_());
  } catch (err) {
    return json_({ ok: false, error: String((err && err.message) || err) });
  }
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function cekKey_(k) {
  const s = PropertiesService.getScriptProperties().getProperty('SECRET_KEY');
  return !!s && !!k && String(k) === s;
}

/* =========================================================================
 * MENU & SETUP (dijalankan sekali oleh pemilik Sheet)
 * ========================================================================= */

function onOpen() {
  SpreadsheetApp.getUi().createMenu('📊 Dashboard')
    .addItem('1. Rapikan Sheet (setup)', 'setupSheet')
    .addItem('2. Buat / lihat Secret Key', 'setupSecretKey')
    .addSeparator()
    .addItem('Cek kualitas data', 'cekKualitasData')
    .addToUi();
}

function setupSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const log = [];

  ss.setSpreadsheetTimeZone(CFG.TZ);
  log.push('Time zone → ' + ss.getSpreadsheetTimeZone());

  let localeOk = false;
  ['in_ID', 'id_ID', 'id'].forEach(function (loc) {
    if (localeOk) return;
    try { ss.setSpreadsheetLocale(loc); localeOk = /^(in|id)/i.test(ss.getSpreadsheetLocale()); } catch (e) {}
  });
  log.push(localeOk ? 'Locale → ' + ss.getSpreadsheetLocale() + ' (format tanggal dd/mm/yyyy)'
                    : '⚠️ Locale gagal diset otomatis. Set manual: File → Settings → Locale: Indonesia');

  const sh = ss.getSheetByName(CFG.SHEET_SALES);
  if (!sh) throw new Error('Tab "' + CFG.SHEET_SALES + '" tidak ditemukan');

  // Kolom NAMA SALES + pindahkan isi non-angka dari kolom Sales
  let hdr = headerMap_(sh);
  if (!(H.NAMA_SALES in hdr)) {
    const c = sh.getLastColumn() + 1;
    sh.getRange(1, c).setValue(H.NAMA_SALES);
    log.push('Kolom NAMA SALES ditambahkan di kolom ' + c);
    hdr = headerMap_(sh);
  }
  const n = sh.getLastRow() - 1;
  if (n > 0) {
    const sCol = hdr[H.SALES], nCol = hdr[H.NAMA_SALES];
    const sv = sh.getRange(2, sCol, n, 1).getValues();
    const nv = sh.getRange(2, nCol, n, 1).getValues();
    let pindah = 0;
    for (let i = 0; i < n; i++) {
      const a = angka_(sv[i][0]);
      if (sv[i][0] !== '' && isNaN(a)) {
        if (nv[i][0] === '') nv[i][0] = String(sv[i][0]).trim();
        sv[i][0] = '';
        pindah++;
      }
    }
    if (pindah) {
      sh.getRange(2, sCol, n, 1).setValues(sv);
      sh.getRange(2, nCol, n, 1).setValues(nv);
      log.push(pindah + ' isi teks di kolom Sales dipindah ke NAMA SALES (kolom Sales dikosongkan → isi omzet rupiah)');
    }
  }
  const maxR = Math.max(sh.getMaxRows() - 1, 1);
  sh.getRange(2, hdr[H.SALES], maxR, 1).setNumberFormat('#,##0');
  sh.getRange(2, hdr[H.HP], maxR, 1).setNumberFormat('@');
  log.push('Format: Sales = angka ribuan, NO HP = teks');

  pastikanKolomFU_(sh);
  log.push('Kolom FU D1/D3/D7/REPEAT dicek');

  tampil_('Setup selesai', log.join('\n'));
}

function setupSecretKey() {
  const props = PropertiesService.getScriptProperties();
  let k = props.getProperty('SECRET_KEY');
  if (!k) {
    k = (Utilities.getUuid() + Utilities.getUuid()).replace(/-/g, '').slice(0, 40);
    props.setProperty('SECRET_KEY', k);
  }
  tampil_('Secret Key', 'Simpan baik-baik, jangan dibagikan di chat publik:\n\n' + k +
    '\n\nURL lengkap = URL /exec Web App + ?key=' + k);
}

function cekKualitasData() {
  const D = muat_();
  const w = D.warn;
  tampil_('Kualitas data — ' + w.length + ' masalah',
    w.length ? w.slice(0, 40).map(function (x) { return 'Baris ' + x.row + ' (' + (x.order || '-') + '): ' + x.masalah; }).join('\n')
             : 'Tidak ada masalah ✅');
}

function tampil_(judul, isi) {
  try { SpreadsheetApp.getUi().alert(judul, isi, SpreadsheetApp.getUi().ButtonSet.OK); }
  catch (e) { Logger.log(judul + '\n' + isi); }
}

/* =========================================================================
 * HELPER
 * ========================================================================= */

function norm_(v) { return String(v == null ? '' : v).trim().toUpperCase(); }

function headerMap_(sh) {
  const h = sh.getRange(1, 1, 1, Math.max(sh.getLastColumn(), 1)).getValues()[0];
  const m = {};
  h.forEach(function (x, i) { const k = norm_(x); if (k && !(k in m)) m[k] = i + 1; });
  return m; // 1-based
}

function pastikanKolomFU_(sh) {
  let m = headerMap_(sh);
  FU_STAGES.forEach(function (s) {
    if (!(s.col in m)) {
      const c = sh.getLastColumn() + 1;
      sh.getRange(1, c).setValue(s.col);
      m = headerMap_(sh);
    }
  });
  return m;
}

function dayNum_(y, m, d) { return Math.round(Date.UTC(y, m - 1, d) / 86400000); }
function ymd_(n) { return new Date(n * 86400000).toISOString().slice(0, 10); }
function senin_(n) { return n - (((n + 3) % 7) + 7) % 7; }

function hariIni_() {
  const s = CFG.TES_HARI_INI || Utilities.formatDate(new Date(), CFG.TZ, 'yyyy-MM-dd');
  const a = s.split('-').map(Number);
  return dayNum_(a[0], a[1], a[2]);
}

/** null = kosong, NaN = tidak terbaca, angka = hari sejak epoch (tanggal WIB). */
function parseTgl_(v, tz) {
  if (v === '' || v == null) return null;
  if (Object.prototype.toString.call(v) === '[object Date]') {
    if (isNaN(v.getTime())) return NaN;
    const a = Utilities.formatDate(v, tz || CFG.TZ, 'yyyy-MM-dd').split('-').map(Number);
    return dayNum_(a[0], a[1], a[2]);
  }
  const s = String(v).trim();
  let m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
  if (m && +m[2] <= 12 && +m[3] <= 31) return dayNum_(+m[1], +m[2], +m[3]);
  m = s.match(/^(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{4})$/);      // teks dd/mm/yyyy (format Indonesia)
  if (m && +m[2] <= 12 && +m[1] <= 31) return dayNum_(+m[3], +m[2], +m[1]);
  return NaN;
}

/** null = kosong, NaN = bukan angka (mis. nama orang). "Rp 500.000" → 500000 */
function angka_(v) {
  if (typeof v === 'number') return v;
  const s = String(v == null ? '' : v).trim();
  if (!s) return null;
  const t = s.replace(/^rp\.?\s*/i, '');
  if (!/\d/.test(t) || /[a-z]/i.test(t)) return NaN;
  return Number(t.replace(/[^\d]/g, ''));
}

function hp_(v) {
  let s = String(v == null ? '' : v).replace(/\D/g, '');
  if (s.indexOf('62') === 0) s = '0' + s.slice(2);
  else if (s.indexOf('8') === 0) s = '0' + s;
  return s;
}

function durasi_(D, produk) { return D.durasi[produk] || D.durasi[CFG.PRODUK_UTAMA[0]] || 60; }

/* =========================================================================
 * MUAT DATA
 * ========================================================================= */

function muat_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sh = ss.getSheetByName(CFG.SHEET_SALES);
  if (!sh) throw new Error('Tab "' + CFG.SHEET_SALES + '" tidak ditemukan');
  const tz = ss.getSpreadsheetTimeZone();
  const values = sh.getDataRange().getValues();
  const idx = {};
  values[0].forEach(function (h, i) { const k = norm_(h); if (k && !(k in idx)) idx[k] = i; });
  const kurang = WAJIB.filter(function (h) { return !(h in idx); });
  if (kurang.length) throw new Error('Kolom wajib tidak ada: ' + kurang.join(', '));

  // durasi stok: config, ditimpa tab Panduan
  const durasi = JSON.parse(JSON.stringify(CFG.DURASI));
  const pan = ss.getSheetByName(CFG.SHEET_PANDUAN);
  if (pan) {
    pan.getDataRange().getValues().forEach(function (r) {
      if (norm_(r[0]) === CFG.DURASI_LABEL_PANDUAN && Number(r[1]) > 0) durasi[CFG.PRODUK_UTAMA[0]] = Number(r[1]);
    });
  }

  const cell = function (r, h) { return h in idx ? r[idx[h]] : ''; };
  const rows = [], warn = [], seenOrder = {};
  const W = function (o, msg) { warn.push({ row: o.row, order: o.order, masalah: msg }); };

  for (let i = 1; i < values.length; i++) {
    const r = values[i];
    const order = String(cell(r, H.ORDER)).trim();
    if (!order && !String(cell(r, H.HP)).trim() && !String(cell(r, H.TGL)).trim()) continue; // baris kosong

    const qtyN = angka_(cell(r, H.QTY));
    const o = {
      row: i + 1, order: order,
      status: norm_(cell(r, H.STATUS)),
      tgl: parseTgl_(cell(r, H.TGL), tz),
      nama: String(cell(r, H.NAME)).trim(),
      hp: hp_(cell(r, H.HP)),
      produk: norm_(cell(r, H.PRODUK)),
      detail: String(cell(r, H.DETAIL)).trim(),
      ofr: norm_(cell(r, H.OFR)),
      qty: (qtyN > 0) ? qtyN : 1,
      sales: angka_(cell(r, H.SALES)),
      terima: parseTgl_(cell(r, H.TERIMA), tz),
      cs: String(cell(r, H.CS)).trim(),
      fu: {}
    };
    FU_STAGES.forEach(function (s) {
      const v = cell(r, s.col);
      o.fu[s.key] = (v === '' || v == null) ? null : v;
    });

    if (!order) W(o, 'NO ORDER kosong');
    else if (seenOrder[norm_(order)]) W(o, 'NO ORDER kembar dengan baris ' + seenOrder[norm_(order)]);
    else seenOrder[norm_(order)] = o.row;
    if (o.tgl == null) W(o, 'TANGGAL kosong');
    else if (isNaN(o.tgl)) W(o, 'TANGGAL tidak terbaca');
    if (isNaN(o.terima)) W(o, 'TGL TERIMA tidak terbaca');
    else if (o.terima != null && o.tgl != null && !isNaN(o.tgl) && o.terima < o.tgl) W(o, 'TGL TERIMA lebih awal dari TANGGAL order');
    if (o.ofr !== 'PC' && CFG.OFR_VALID.indexOf(o.ofr) < 0) W(o, 'O/F/R tidak dikenal: "' + o.ofr + '"');
    if (!o.hp) W(o, 'NO HP kosong');
    if (qtyN != null && !(qtyN > 0)) W(o, 'QTY bukan angka > 0 (dianggap 1)');

    o.keluar = CFG.STATUS_KELUAR.indexOf(o.status) >= 0;
    o.valid = !o.keluar && CFG.OFR_VALID.indexOf(o.ofr) >= 0 && o.tgl != null && !isNaN(o.tgl);
    o.utama = CFG.PRODUK_UTAMA.indexOf(o.produk) >= 0;
    if (o.valid) {
      if (o.sales == null) W(o, 'Sales (omzet) kosong — omzet dihitung 0');
      else if (isNaN(o.sales)) W(o, 'Sales berisi teks, bukan angka omzet — omzet dihitung 0');
    }
    o.omzet = (o.sales > 0) ? o.sales : 0;
    const start = (o.terima != null && !isNaN(o.terima)) ? o.terima : o.tgl;
    o.habis = (o.tgl != null && !isNaN(o.tgl)) ? start + durasi_({ durasi: durasi }, o.produk) * o.qty : null;
    rows.push(o);
  }

  // Urutkan order valid per pelanggan
  const valid = rows.filter(function (o) { return o.valid; })
    .sort(function (a, b) { return a.tgl - b.tgl || a.row - b.row; });
  const perHp = {};
  valid.forEach(function (o) { if (o.hp) (perHp[o.hp] = perHp[o.hp] || []).push(o); });

  // Aturan 5a.3: produk utama, ATAU R non-utama dari pelanggan yg sudah beli utama lebih awal
  Object.keys(perHp).forEach(function (hp) {
    let pertamaUtama = null;
    perHp[hp].forEach(function (o, i) {
      o.urutan = i;
      o.adaSesudah = i < perHp[hp].length - 1;
      if (o.ofr === 'R' && i === 0) W(o, 'Ditandai R tapi belum ada order sebelumnya dari NO HP ini');
      o.hitung = o.utama || (o.ofr === 'R' && pertamaUtama != null && pertamaUtama < o.tgl);
      if (o.utama && pertamaUtama == null) pertamaUtama = o.tgl;
    });
  });
  valid.forEach(function (o) {
    if (!o.hp) { o.hitung = o.utama; o.urutan = 0; o.adaSesudah = false; }
  });

  return { rows: rows, valid: valid, perHp: perHp, warn: warn, today: hariIni_(), durasi: durasi, tz: tz };
}

/* =========================================================================
 * RINGKASAN AGREGAT (tanpa PII) — untuk refresh.py
 * ========================================================================= */

function summary_() {
  const D = muat_(), today = D.today;
  const baru = function () { return { omzet: 0, trx: 0, qty: 0, O: 0, F: 0, R: 0, omzetO: 0, omzetF: 0, omzetR: 0 }; };
  const byDay = {}, byWeek = {}, byMonth = {}, nonUtama = {};

  D.valid.forEach(function (o) {
    const day = ymd_(o.tgl), wk = ymd_(senin_(o.tgl)), mo = day.slice(0, 7);
    if (!o.hitung) {
      const x = nonUtama[mo] = nonUtama[mo] || { omzet: 0, trx: 0 };
      x.omzet += o.omzet; x.trx++;
      return;
    }
    [[byDay, day], [byWeek, wk], [byMonth, mo]].forEach(function (p) {
      const a = p[0][p[1]] = p[0][p[1]] || baru();
      a.omzet += o.omzet; a.trx++; a.qty += o.qty;
      a[o.ofr]++; a['omzet' + o.ofr] += o.omzet;
    });
  });

  // Repeat % (pulse)
  let nR = 0, nBasis = 0;
  D.valid.forEach(function (o) {
    if (!o.hitung) return;
    if (o.ofr === 'R') nR++;
    else if (today - o.tgl > CFG.PULSE_MIN_UMUR) nBasis++;
  });

  // Kohort per ORANG, per bulan akuisisi
  const kohort = {};
  let vip = 0;
  Object.keys(D.perHp).forEach(function (hp) {
    const list = D.perHp[hp].filter(function (o) { return o.hitung; });
    if (list.length >= 2) vip++;
    const ai = list.findIndex(function (o) { return o.ofr === 'O' || o.ofr === 'F'; });
    if (ai < 0) return;
    const acq = list[ai], mo = ymd_(acq.tgl).slice(0, 7);
    const k = kohort[mo] = kohort[mo] || { baru: 0, masihMinum: 0, balik: 0, hilang: 0, pctBalik: null };
    k.baru++;
    if (ai < list.length - 1) k.balik++;
    else if (today < acq.habis + CFG.GRACE_KOHORT) k.masihMinum++;
    else k.hilang++;
  });
  Object.keys(kohort).forEach(function (mo) {
    const k = kohort[mo], dinilai = k.balik + k.hilang;
    k.pctBalik = dinilai ? Math.round(k.balik / dinilai * 1000) / 10 : null;
  });

  const status = {};
  D.rows.forEach(function (o) { status[o.status || '(KOSONG)'] = (status[o.status || '(KOSONG)'] || 0) + 1; });

  return {
    ok: true,
    generatedAt: Utilities.formatDate(new Date(), CFG.TZ, "yyyy-MM-dd'T'HH:mm:ssXXX"),
    today: ymd_(today),
    tz: CFG.TZ,
    config: { produkUtama: CFG.PRODUK_UTAMA, durasiPerBox: D.durasi, statusKeluar: CFG.STATUS_KELUAR, graceKohort: CFG.GRACE_KOHORT },
    byDay: byDay, byWeek: byWeek, byMonth: byMonth,   // closing% = (O+F) bulan ÷ contact Meta (dihitung di refresh.py)
    nonUtama: nonUtama,
    repeatPulse: { R: nR, basisOF: nBasis, pct: nBasis ? Math.round(nR / nBasis * 1000) / 10 : null },
    kohort: kohort,
    vipCount: vip,
    statusCount: status,
    dataQuality: { jumlah: D.warn.length, contoh: D.warn.slice(0, 50) }   // hanya no baris + NO ORDER, tanpa nama/HP
  };
}

/* =========================================================================
 * FEED CHECKLIST FOLLOW-UP (&fu2) — berisi nama & HP, khusus CS
 * ========================================================================= */

function fu2_() {
  const D = muat_(), today = D.today;
  const due = [], nyangkut = [], beres = [];
  let kedaluwarsa = 0;

  const base = function (o) {
    return {
      row: o.row, order: o.order, nama: o.nama, hp: o.hp,
      wa: o.hp ? 'https://wa.me/62' + o.hp.slice(1) : '',
      detail: o.detail, qty: o.qty, ofr: o.ofr, cs: o.cs, status: o.status,
      tglOrder: ymd_(o.tgl), tglTerima: (o.terima != null && !isNaN(o.terima)) ? ymd_(o.terima) : ''
    };
  };

  D.valid.forEach(function (o) {
    // Belum ada TGL TERIMA → jangan hilang diam-diam
    if (o.terima == null || isNaN(o.terima)) {
      const umur = today - o.tgl;
      const b = base(o);
      if (o.status === 'DITERIMA') { b.alasan = 'Status DITERIMA tapi TGL TERIMA kosong'; b.umur = umur; nyangkut.push(b); }
      else if (umur > CFG.NYANGKUT_HARI) { b.alasan = 'Belum ada TGL TERIMA ' + umur + ' hari sejak order'; b.umur = umur; nyangkut.push(b); }
      return;
    }

    const jadwal = FU_STAGES.map(function (s) {
      return {
        key: s.key,
        due: s.offset != null ? o.terima + s.offset : o.habis - CFG.REPEAT_SEBELUM_HABIS,
        done: o.fu[s.key]
      };
    }).filter(function (j) {
      return !(j.key === 'REPEAT' && o.adaSesudah);   // sudah repeat → tahap REPEAT tidak perlu
    }).sort(function (a, b) { return a.due - b.due; });

    // ✅ Beres hari ini + kapan tahap berikutnya
    jadwal.forEach(function (j) {
      if (j.done && parseTgl_(j.done, CFG.TZ) === today) {
        const next = jadwal.filter(function (x) { return !x.done && x.due > j.due; })[0];
        const b = base(o);
        b.tahap = j.key;
        b.berikut = next ? { tahap: next.key, tanggal: ymd_(next.due), lagi: next.due - today } : null;
        beres.push(b);
      }
    });

    // Tahap aktif = tahap TERAKHIR yang sudah jatuh tempo
    const jatuh = jadwal.filter(function (j) { return j.due <= today; });
    if (!jatuh.length) return;
    const cur = jatuh[jatuh.length - 1];
    if (cur.done) return;
    const telat = today - cur.due;
    if (telat > CFG.FU_MAX_TELAT) { kedaluwarsa++; return; }
    const b = base(o);
    b.tahap = cur.key;
    b.jatuhTempo = ymd_(cur.due);
    b.telat = telat;
    b.terlewat = jatuh.slice(0, -1).filter(function (j) { return !j.done; }).map(function (j) { return j.key; });
    due.push(b);
  });

  due.sort(function (a, b) { return b.telat - a.telat || a.row - b.row; });
  nyangkut.sort(function (a, b) { return b.umur - a.umur; });

  // VIP: pelanggan ≥2 order, urut omzet
  const vip = Object.keys(D.perHp).map(function (hp) {
    const list = D.perHp[hp].filter(function (o) { return o.hitung; });
    if (list.length < 2) return null;
    const last = list[list.length - 1];
    return {
      nama: last.nama, hp: hp, wa: 'https://wa.me/62' + hp.slice(1), order: list.length,
      omzet: list.reduce(function (s, o) { return s + o.omzet; }, 0), terakhir: ymd_(last.tgl)
    };
  }).filter(Boolean).sort(function (a, b) { return b.omzet - a.omzet; }).slice(0, CFG.VIP_TOP);

  return { ok: true, today: ymd_(today), due: due, nyangkut: nyangkut, beres: beres, kedaluwarsa: kedaluwarsa, vip: vip };
}

/* =========================================================================
 * TOMBOL ✓ (&mark) — nulis balik ke Sheet
 * ========================================================================= */

function mark_(p) {
  const order = String(p.order || '').trim();
  const stage = String(p.stage || '').trim().toUpperCase();
  const st = FU_STAGES.filter(function (s) { return s.key === stage; })[0];
  if (!order) return { ok: false, error: 'Parameter order kosong' };
  if (!st) return { ok: false, error: 'stage harus D1, D3, D7, atau REPEAT' };

  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    const sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(CFG.SHEET_SALES);
    const m = pastikanKolomFU_(sh);
    const last = sh.getLastRow();
    if (last < 2) return { ok: false, error: 'Sheet masih kosong' };

    const ids = sh.getRange(2, m[H.ORDER], last - 1, 1).getValues();
    const hits = [];
    ids.forEach(function (r, i) { if (norm_(r[0]) === norm_(order)) hits.push(i + 2); });
    if (!hits.length) return { ok: false, error: 'NO ORDER ' + order + ' tidak ditemukan' };
    if (hits.length > 1) {
      return { ok: false, error: 'NO ORDER ' + order + ' kembar di baris ' + hits.join(', ') + ' — perbaiki dulu di Sheet, tidak ada yang ditulis' };
    }

    const c = sh.getRange(hits[0], m[st.col]);
    if (p.undo) { c.clearContent(); return { ok: true, undo: true, row: hits[0], order: order, stage: st.key }; }
    const cur = c.getValue();
    if (cur !== '' && cur != null) return { ok: true, sudah: true, row: hits[0], order: order, stage: st.key, waktu: String(cur) };

    const ts = Utilities.formatDate(new Date(), CFG.TZ, "yyyy-MM-dd'T'HH:mm:ssXXX");
    c.setNumberFormat('@').setValue(ts);
    return { ok: true, row: hits[0], order: order, stage: st.key, waktu: ts };
  } finally {
    lock.releaseLock();
  }
}

/* =========================================================================
 * TES DI EDITOR (Run → tesLokal, lihat Execution log)
 * ========================================================================= */

function tesLokal() {
  Logger.log(JSON.stringify(summary_(), null, 2));
  Logger.log(JSON.stringify(fu2_(), null, 2));
}
