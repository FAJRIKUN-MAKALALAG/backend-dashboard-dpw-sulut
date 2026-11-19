"""Migrasi data historis 2024-2026 dari 3 file Excel keuangan DPW NasDem Sulawesi Utara.

Jalankan dari root backend (`backend-dashboard-dpw`):
    python -m scripts.migrate_finance_excel
"""

from __future__ import annotations

import calendar
import hashlib
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import openpyxl

_BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_ROOT_DIR = os.path.abspath(os.path.join(_BASE_DIR, ".."))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

from app import create_app, db  # noqa: E402
from app.models_finance import (  # noqa: E402
    FinanceAutodebetHistory,
    FinanceDppRemittance,
    FinanceDuesPayment,
    FinanceLegislator,
    FinanceTariffRule,
    FinanceTransaction,
)

FINANCE_DIR = os.path.join(_ROOT_DIR, "data keuangan")
FILE_DPW = os.path.join(FINANCE_DIR, "IURAN DPW SAMPAI 26 OKT 2026.xlsx")
FILE_DPP = os.path.join(FINANCE_DIR, "SULUT SEPT 2024 - 26 OKTOBER 2026 (DPP).xlsx")
FILE_JULI = os.path.join(FINANCE_DIR, "LAPORAN KEUANGAN BULAN JULI 2025.xlsx")

# Urutan tetap 26 kolom bulan dari Sept 2024 s/d Okt 2026 di kedua file Iuran DPW & DPP
MONTH_COLUMNS: List[str] = [
    "2024-09", "2024-10", "2024-11", "2024-12",
    "2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06",
    "2025-07", "2025-08", "2025-09", "2025-10", "2025-11", "2025-12",
    "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06",
    "2026-07", "2026-08", "2026-09", "2026-10",
]
MIGRATION_START_MONTH = "2026-01"

REGION_CANONICAL_MAP: Dict[str, str] = {
    "PROVINSI SULUT": "Provinsi Sulawesi Utara",
    "KOTA MANADO": "Kota Manado",
    "KAB. MINAHASA UTARA": "Kabupaten Minahasa Utara",
    "KOTA BITUNG": "Kota Bitung",
    "KAB. MINAHASA": "Kabupaten Minahasa",
    "KAB. MINAHASA SELATAN": "Kabupaten Minahasa Selatan",
    "KAB. MINAHASA TENGGARA": "Kabupaten Minahasa Tenggara",
    "KAB. KEPL. SANGIHE": "Kabupaten Kepulauan Sangihe",
    "KAB. KEPL. SITARO": "Kabupaten Kepulauan Siau Tagulandang Biaro",
    "KAB. KEPL. TALAUD": "Kabupaten Kepulauan Talaud",
    "KAB. BOLAANG MONGONDOW TIMUR": "Kabupaten Bolaang Mongondow Timur",
    "KAB. BOLAANG MONGONDOW": "Kabupaten Bolaang Mongondow",
    "KAB. BOLAANG MONGONDOW SELATAN": "Kabupaten Bolaang Mongondow Selatan",
    "KAB. BOLSEL": "Kabupaten Bolaang Mongondow Selatan",
    "KOTA KOTAMOBAGU": "Kota Kotamobagu",
}


def canonical_region(raw: str) -> str:
    up = " ".join(str(raw or "").upper().split())
    return REGION_CANONICAL_MAP.get(up, str(raw or "").strip())


def parse_money(val) -> Tuple[int, bool]:
    """Mengembalikan (nominal_int, is_autodebet_text)."""
    if val is None:
        return (0, False)
    if isinstance(val, (int, float)):
        return (max(0, int(round(float(val)))), False)
    s = str(val).strip()
    if not s:
        return (0, False)
    if "AUTODEB" in s.upper():
        return (0, True)
    digits = re.sub(r"[^0-9]", "", s)
    return (int(digits), False) if digits else (0, False)


def month_end_date(ym: str) -> date:
    if ym == "2026-10":
        return date(2026, 10, 26)
    y, m = map(int, ym.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    return date(y, m, last_day)


def slug_id(prefix: str, daerah: str, idx: int) -> str:
    short = hashlib.sha1(daerah.lower().encode("utf-8")).hexdigest()[:6].upper()
    clean = re.sub(r"^(KABUPATEN|KOTA|PROVINSI)\s+", "", daerah.upper()).replace(" ", "")
    tag = re.sub(r"[^A-Z0-9]", "", clean)[:8] or "SULUT"
    return f"{prefix}-{tag}-{short}-{idx:03d}"


def normalize_person_key(name: str) -> str:
    return re.sub(r"[^a-z]", "", str(name or "").lower())


def load_dpw_excel():
    wb = openpyxl.load_workbook(FILE_DPW, data_only=True)
    legislators = []  # list of dict
    ws_prov = wb["PROVINSI"]
    prov_tariffs = [parse_money(ws_prov.cell(2, col).value)[0] for col in range(2, 28)]

    urutan = 0
    for r in range(4, ws_prov.max_row + 1):
        nama = str(ws_prov.cell(r, 1).value or "").strip()
        if not nama or nama.upper() == "NAMA":
            continue
        urutan += 1
        vals = [parse_money(ws_prov.cell(r, col).value) for col in range(2, 28)]
        legislators.append(
            {
                "tingkat": "PROVINSI",
                "daerah": "Provinsi Sulawesi Utara",
                "urutan": urutan,
                "nama": nama,
                "dpw_cells": vals,
                "dpw_tariffs": prov_tariffs,
            }
        )

    ws_kab = wb["KAB KOTA"]
    kab_tariffs = [parse_money(ws_kab.cell(3, col).value)[0] for col in range(2, 28)]
    current_region = ""
    for r in range(4, ws_kab.max_row + 1):
        c0 = str(ws_kab.cell(r, 1).value or "").strip()
        if not c0:
            continue
        if re.match(r"^(KOTA|KAB\.)", c0, re.I):
            current_region = canonical_region(c0)
            continue
        if c0.upper() in ("NAMA", "TOTAL"):
            continue
        urutan += 1
        vals = [parse_money(ws_kab.cell(r, col).value) for col in range(2, 28)]
        legislators.append(
            {
                "tingkat": "KAB_KOTA",
                "daerah": current_region,
                "urutan": urutan,
                "nama": c0,
                "dpw_cells": vals,
                "dpw_tariffs": kab_tariffs,
            }
        )
    return legislators


def load_dpp_excel():
    wb = openpyxl.load_workbook(FILE_DPP, data_only=True)
    ws = wb["Sheet1"]
    current_region = "Provinsi Sulawesi Utara"
    current_tingkat = "PROVINSI"
    active_tariffs = [0] * 26
    dpp_by_person: Dict[Tuple[str, str], List[int]] = {}
    tariffs_by_region: Dict[str, List[int]] = {}

    for r in range(1, ws.max_row + 1):
        c0 = str(ws.cell(r, 1).value or "").strip()
        row_vals = [parse_money(ws.cell(r, col).value)[0] for col in range(2, 28)]

        if "PROVINSI" in c0.upper() and "IURAN" in c0.upper():
            current_tingkat = "PROVINSI"
            current_region = "Provinsi Sulawesi Utara"
            continue
        if "KABUPATEN" in c0.upper() and "IURAN" in c0.upper():
            current_tingkat = "KAB_KOTA"
            continue
        if re.match(r"^(KOTA|KAB\.)", c0, re.I):
            current_region = canonical_region(c0)
            tariffs_by_region[current_region] = list(active_tariffs)
            continue
        if not c0 and any(v > 0 for v in row_vals):
            # Baris tarif di atas blok daerah (untuk Provinsi ada sebelum nama, untuk Kab/Kota sebelum header daerah)
            active_tariffs = row_vals
            if current_tingkat == "PROVINSI":
                tariffs_by_region["Provinsi Sulawesi Utara"] = list(active_tariffs)
            continue
        if not c0 or c0.upper() in ("NAMA", "TOTAL"):
            continue

        dpp_by_person[(current_region, normalize_person_key(c0))] = row_vals

    return dpp_by_person, tariffs_by_region


def build_tariff_rules(legs_data, tariffs_dpp_by_region) -> List[FinanceTariffRule]:
    """Menyusun aturan tarif DPW + DPP per daerah & per perubahan nominal lintas bulan."""
    region_dpw = {}
    region_tingkat = {}
    for item in legs_data:
        region_dpw[item["daerah"]] = item["dpw_tariffs"]
        region_tingkat[item["daerah"]] = item["tingkat"]

    rules: List[FinanceTariffRule] = []
    cols_2026 = [(idx, ym) for idx, ym in enumerate(MONTH_COLUMNS) if ym >= MIGRATION_START_MONTH]
    for daerah, dpw_list in region_dpw.items():
        tingkat = region_tingkat[daerah]
        dpp_list = tariffs_dpp_by_region.get(daerah, [0] * len(MONTH_COLUMNS))
        # Gabungkan menjadi segmen periode dengan pasangan (dpw, dpp) yang identik mulai 2026-01
        segments = []
        for idx, ym in cols_2026:
            pair = (dpw_list[idx], dpp_list[idx])
            if not segments or segments[-1]["pair"] != pair:
                segments.append({"from": ym, "to": ym, "pair": pair})
            else:
                segments[-1]["to"] = ym

        for s_idx, seg in enumerate(segments):
            is_last = s_idx == len(segments) - 1
            eff_to = None if is_last else seg["to"]
            rules.append(
                FinanceTariffRule(
                    id=slug_id("TRF", daerah, s_idx + 1),
                    tingkat=tingkat,
                    daerah=daerah,
                    effective_from=seg["from"],
                    effective_to=eff_to,
                    amount_dpw=seg["pair"][0],
                    amount_dpp=seg["pair"][1],
                    notes="Migrasi tarif Excel DPW & DPP (2026)",
                )
            )
    return rules


def parse_juli_date(raw_val, default_year: int = 2025) -> date:
    if isinstance(raw_val, datetime):
        d = raw_val.date()
        return date(default_year, d.month, d.day) if d.year == 2024 else d
    if isinstance(raw_val, date):
        return date(default_year, raw_val.month, raw_val.day) if raw_val.year == 2024 else raw_val
    s = str(raw_val or "").strip()
    if not s:
        return date(2025, 7, 1)
    for fmt in ("%d-%b-%y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s, fmt).date()
            return date(default_year, d.month, d.day) if (d.year == 2024 and "Jul" in s) else d
        except ValueError:
            continue
    return date(2025, 7, 1)


def infer_op_category(desc: str) -> str:
    low = desc.lower()
    if "gaji" in low:
        return "Gaji Staf & Satpam"
    if "listrik" in low or "wifi" in low or "genset" in low or "solar" in low or "lift" in low:
        return "Listrik, Wifi & Utilitas"
    if "transport" in low or "bensin" in low or "tiket" in low:
        return "Transport & Perjalanan"
    if "makan" in low or "aqua" in low or "tisu" in low or "kopi" in low or "leminerale" in low:
        return "Konsumsi & Perlengkapan Kantor"
    if "bendera" in low or "mesin" in low or "sapu" in low or "engsel" in low or "tukang" in low or "pasir" in low:
        return "Perawatan & Aset Kantor"
    if "setor iuran" in low or "cicilan" in low or "pengembalian" in low:
        return "Penyesuaian & Kewajiban Lain"
    return "Operasional Kantor"


def load_juli_2025_operational_transactions() -> List[FinanceTransaction]:
    wb = openpyxl.load_workbook(FILE_JULI, data_only=True)
    ws_op = wb["Operational"]
    txs: List[FinanceTransaction] = []

    for r in range(5, ws_op.max_row + 1):
        uraian = str(ws_op.cell(r, 2).value or "").strip()
        debit = parse_money(ws_op.cell(r, 3).value)[0]
        credit = parse_money(ws_op.cell(r, 4).value)[0]
        if not uraian or uraian.lower() in ("pengeluaran", "total penarikan", "total pengeluaran", "sisa saldo"):
            continue
        tx_d = parse_juli_date(ws_op.cell(r, 1).value, default_year=2025)

        if debit > 0 and ("penarikan uang dpw" in uraian.lower() or "sisah saldo juni" in uraian.lower()):
            txs.append(
                FinanceTransaction(
                    id=f"HIST-OP-IN-{r:03d}",
                    tx_date=date(2025, 7, 17) if "penarikan" in uraian.lower() and tx_d == date(2025, 7, 1) else tx_d,
                    account_type="PETTY_CASH",
                    direction="IN",
                    category="Penerimaan dari Rekening DPW" if "penarikan" in uraian.lower() else "Saldo Awal Kas Operasional",
                    description=f"[Juli 2025] {uraian}",
                    amount=debit,
                    is_historical=True,
                    created_by="Migrasi Excel Juli 2025",
                )
            )
            # Penarikan bank untuk operasional (selain item Iuran DPP yang sudah tercatat di remitansi DPP)
            if "penarikan" in uraian.lower() and debit != 49_500_000:
                txs.append(
                    FinanceTransaction(
                        id=f"HIST-BK-WD-{r:03d}",
                        tx_date=tx_d,
                        account_type="BANK_DPW",
                        direction="OUT",
                        category="Penarikan ke Kas Operasional",
                        description=f"[Juli 2025] {uraian} (Kas Operasional)",
                        amount=debit,
                        is_historical=True,
                        created_by="Migrasi Excel Juli 2025",
                    )
                )
        elif credit > 0:
            txs.append(
                FinanceTransaction(
                    id=f"HIST-OP-OUT-{r:03d}",
                    tx_date=tx_d,
                    account_type="PETTY_CASH",
                    direction="OUT",
                    category=infer_op_category(uraian),
                    description=f"[Juli 2025] {uraian}",
                    amount=credit,
                    is_historical=True,
                    created_by="Migrasi Excel Juli 2025",
                )
            )

    # Koreksi item Bank Juli 2025 dari sheet Saldo (Biaya Admin & Pajak = OUT, Bunga Tabungan = IN)
    # Catatan: Saldo awal Juni tidak ditambahkan ke BANK_DPW agar tidak menumpuk ganda di atas pemasukan 2024-09..2025-06
    ws_saldo = wb["Saldo"]
    for r in range(5, ws_saldo.max_row + 1):
        uraian = str(ws_saldo.cell(r, 2).value or "").strip()
        col_in = parse_money(ws_saldo.cell(r, 3).value)[0]
        col_out = parse_money(ws_saldo.cell(r, 4).value)[0]
        low = uraian.lower()
        tx_d = parse_juli_date(ws_saldo.cell(r, 1).value, default_year=2025)
        if "biaya admin" in low or "pajak" in low:
            amt = col_in or col_out
            if amt > 0:
                txs.append(
                    FinanceTransaction(
                        id=f"HIST-BK-FEE-{r:02d}",
                        tx_date=tx_d,
                        account_type="BANK_DPW",
                        direction="OUT",
                        category="Biaya Admin & Pajak Bank",
                        description=f"[Juli 2025] {uraian} (terkoreksi ke pengeluaran bank)",
                        amount=amt,
                        is_historical=True,
                        created_by="Migrasi Excel Juli 2025",
                    )
                )
        elif "bunga tabungan" in low:
            amt = col_out or col_in
            if amt > 0:
                txs.append(
                    FinanceTransaction(
                        id=f"HIST-BK-INT-{r:02d}",
                        tx_date=tx_d,
                        account_type="BANK_DPW",
                        direction="IN",
                        category="Bunga Bank",
                        description=f"[Juli 2025] {uraian} (terkoreksi ke pemasukan bank)",
                        amount=amt,
                        is_historical=True,
                        created_by="Migrasi Excel Juli 2025",
                    )
                )

    return txs


def run_migration():
    app = create_app()
    with app.app_context():
        db.create_all()

        # Bersihkan data migrasi sebelumnya agar idempoten saat dijalankan ulang
        FinanceDuesPayment.query.delete()
        FinanceAutodebetHistory.query.delete()
        FinanceTransaction.query.delete()
        FinanceDppRemittance.query.delete()
        FinanceTariffRule.query.delete()
        FinanceLegislator.query.delete()
        db.session.flush()

        legs_data = load_dpw_excel()
        dpp_by_person, tariffs_dpp_by_region = load_dpp_excel()
        rules = build_tariff_rules(legs_data, tariffs_dpp_by_region)
        for rule in rules:
            db.session.add(rule)
        db.session.flush()

        total_hist_dpp = 0
        total_hist_dpw = 0
        monthly_bank_in: Dict[str, Tuple[int, int]] = defaultdict(lambda: (0, 0))

        # Batch remitansi historis 2026 agar antrean DPP baru mulai bersih Rp0
        hist_rem = FinanceDppRemittance(
            id="HIST-DPP-2026",
            remittance_date=date(2026, 10, 26),
            period_label="Jan 2026 - Okt 2026 (Migrasi Excel)",
            total_dpp_amount=0,
            bank_fee=0,
            total_transferred=0,
            recipient_account="Rekening DPP Partai NasDem Pusat",
            reference_no="HIST-MIGRASI-2026",
            notes="Rekonsiliasi penuh setoran historis ke DPP Jan–Okt 2026 sesuai file Excel DPP.",
            is_historical=True,
            created_by="Migrasi Excel 2026",
        )
        db.session.add(hist_rem)
        db.session.flush()

        for item in legs_data:
            daerah = item["daerah"]
            tingkat = item["tingkat"]
            nama = item["nama"]
            leg_id = slug_id("LEG", daerah, item["urutan"])

            # Deteksi sel "AUTODEBET" di Excel DPW
            auto_months = [MONTH_COLUMNS[i] for i, (_, is_auto) in enumerate(item["dpw_cells"]) if is_auto]

            # Aturan status autodebet sesuai konfirmasi lapangan terkini:
            # - Provinsi Sulut (6 org): AKTIF sejak 2024-09
            # - Kab. Minahasa Selatan (4 org): AKTIF sejak bulan autodebet pertama di Excel (2024-10)
            # - Kab. Bolaang Mongondow Timur (8 org): AKTIF mulai periode berjalan (2026-11)
            # - Kota Bitung (4 org): TIDAK AKTIF lagi; simpan periode mulai -> berhenti aktual per orang
            is_auto_now = False
            ad_start = None
            ad_end = None
            ad_note = None

            if daerah == "Provinsi Sulawesi Utara":
                is_auto_now = True
                ad_start = "2024-09"
                ad_note = "Autodebet fraksi Provinsi (6 anggota) aktif"
            elif daerah == "Kabupaten Minahasa Selatan":
                is_auto_now = True
                ad_start = auto_months[0] if auto_months else "2024-10"
                ad_note = "Autodebet Kab. Minahasa Selatan aktif sejak Okt 2024"
            elif daerah == "Kabupaten Bolaang Mongondow Timur":
                is_auto_now = True
                ad_start = "2026-11"
                ad_note = "Autodebet Kab. Boltim aktif mulai Nov 2026 (histori Sep 2024–Okt 2026 manual)"
            elif auto_months:
                is_auto_now = False
                ad_start = auto_months[0]
                ad_end = auto_months[-1]
                ad_note = f"Riwayat autodebet {ad_start} s/d {ad_end}, beralih ke setoran manual setelahnya"

            leg = FinanceLegislator(
                id=leg_id,
                nama_lengkap=nama,
                tingkat=tingkat,
                daerah=daerah,
                urutan=item["urutan"],
                is_active=True,
                is_autodebet_active=is_auto_now,
                autodebet_start_month=ad_start,
                autodebet_end_month=ad_end,
                catatan=ad_note,
            )
            db.session.add(leg)
            db.session.flush()

            if ad_start:
                db.session.add(
                    FinanceAutodebetHistory(
                        id=f"ADH-{leg_id}-01",
                        legislator_id=leg_id,
                        start_month=ad_start,
                        end_month=ad_end if not is_auto_now else None,
                        is_active=is_auto_now,
                        notes=ad_note,
                        updated_by="Migrasi Excel 2026",
                    )
                )

            dpp_cells = dpp_by_person.get((daerah, normalize_person_key(nama)), [0] * 26)
            dpp_tariffs = tariffs_dpp_by_region.get(daerah, [0] * 26)

            for m_idx, ym in enumerate(MONTH_COLUMNS):
                if ym < MIGRATION_START_MONTH:
                    continue
                raw_dpw, is_auto_cell = item["dpw_cells"][m_idx]
                dpw_amt = item["dpw_tariffs"][m_idx] if is_auto_cell else raw_dpw
                dpp_amt = dpp_cells[m_idx]

                # Catatan khusus Minsel: Excel mengosongkan Sep-Okt 2026 di akhir file saat cutoff 26 Okt,
                # tidak dibuatkan pembayaran palsu jika sel kosong; tetap ikut sel Excel apa adanya.
                if dpw_amt == 0 and dpp_amt == 0:
                    continue

                if is_auto_cell:
                    method = "AUTODEBET"
                elif daerah == "Provinsi Sulawesi Utara" and ym >= "2024-09":
                    method = "AUTODEBET"
                else:
                    method = "MANUAL"

                pay_date = month_end_date(ym)
                total_hist_dpw += dpw_amt
                total_hist_dpp += dpp_amt
                prev_dpw_m, prev_dpp_m = monthly_bank_in[ym]
                monthly_bank_in[ym] = (prev_dpw_m + dpw_amt, prev_dpp_m + dpp_amt)

                db.session.add(
                    FinanceDuesPayment(
                        id=f"HPAY-{leg_id}-{ym}",
                        legislator_id=leg_id,
                        period_month=ym,
                        payment_date=pay_date,
                        amount_dpw=dpw_amt,
                        amount_dpp=dpp_amt,
                        payment_method=method,
                        dpp_remittance_id=hist_rem.id if dpp_amt > 0 else None,
                        notes="Autodebet tercatat penuh (koreksi sel Excel)" if is_auto_cell else "Migrasi histori Excel 2026",
                        is_historical=True,
                        created_by="Migrasi Excel 2026",
                    )
                )

        hist_rem.total_dpp_amount = total_hist_dpp
        hist_rem.total_transferred = total_hist_dpp

        # Masukkan arus kas masuk & keluar DPP bulanan (2026-01..2026-10) ke buku BANK_DPW (tanpa data 2024-2025)
        for ym, (sum_dpw, sum_dpp) in sorted(monthly_bank_in.items()):
            tot = sum_dpw + sum_dpp
            if tot > 0:
                db.session.add(
                    FinanceTransaction(
                        id=f"HIST-DUES-IN-{ym}",
                        tx_date=month_end_date(ym),
                        account_type="BANK_DPW",
                        direction="IN",
                        category="Iuran Fraksi",
                        description=f"[Historis {ym}] Total Setoran Fraksi (Porsi DPW Rp{sum_dpw:,} + Titipan DPP Rp{sum_dpp:,})".replace(",", "."),
                        amount=tot,
                        is_historical=True,
                        created_by="Migrasi Excel 2026",
                    )
                )
            if sum_dpp > 0:
                db.session.add(
                    FinanceTransaction(
                        id=f"HIST-DPP-OUT-{ym}",
                        tx_date=month_end_date(ym),
                        account_type="BANK_DPW",
                        direction="OUT",
                        category="Setoran ke DPP",
                        description=f"[Historis {ym}] Pengiriman Kewajiban ke DPP Pusat Rp{sum_dpp:,}".replace(",", "."),
                        amount=sum_dpp,
                        related_remittance_id=hist_rem.id,
                        is_historical=True,
                        created_by="Migrasi Excel 2026",
                    )
                )

        db.session.commit()

        print("=== MIGRASI KEUANGAN EXCEL SELESAI ===")
        print(f"Total Legislator      : {FinanceLegislator.query.count()}")
        print(f"Total Aturan Tarif    : {FinanceTariffRule.query.count()}")
        print(f"Total Hist Autodebet  : {FinanceAutodebetHistory.query.count()}")
        print(f"Total Pembayaran Iuran: {FinanceDuesPayment.query.count()}")
        print(f"Total DPW Terkumpul   : Rp{total_hist_dpw:,}".replace(",", "."))
        print(f"Total DPP Tersalurkan : Rp{total_hist_dpp:,}".replace(",", "."))
        print(f"Total Transaksi Kas   : {FinanceTransaction.query.count()}")


if __name__ == "__main__":
    run_migration()
