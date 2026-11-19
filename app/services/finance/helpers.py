"""Utilitas bersama, konstanta, dan exception standar modul keuangan DPW."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any, List, Optional

from app.extensions import db
from app.models import AuditLog
from app.services.image_storage import is_base64_data_uri, optimize_and_save_image

logger = logging.getLogger("nasdem.finance")

MONTH_NAMES_SHORT = [
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "Mei",
    "Jun",
    "Jul",
    "Agu",
    "Sep",
    "Okt",
    "Nov",
    "Des",
]

HISTORICAL_START_MONTH = "2026-01"
HISTORICAL_EXCEL_END_MONTH = "2026-10"


class FinanceServiceError(Exception):
    """Kesalahan bisnis terstruktur dengan HTTP status code untuk modul keuangan."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def ensure_finance_tables_exists():
    """Memastikan seluruh tabel modul keuangan tersedia di PostgreSQL."""
    try:
        db.create_all()
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.warning(f"Inisialisasi tabel keuangan dilewati: {exc}")


def current_year_month() -> str:
    return datetime.now().strftime("%Y-%m")


def is_valid_ym(ym: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(ym or "").strip()))


def format_month_label(ym: str) -> str:
    if not is_valid_ym(ym):
        return str(ym or "")
    y, m = ym.split("-")
    return f"{MONTH_NAMES_SHORT[int(m)]} {y}"


def list_months_range(start_ym: str, end_ym: str) -> List[str]:
    if not is_valid_ym(start_ym) or not is_valid_ym(end_ym):
        return []
    sy, sm = map(int, start_ym.split("-"))
    ey, em = map(int, end_ym.split("-"))
    if (sy, sm) > (ey, em):
        return []
    out: List[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def parse_date_safe(val: Any, fallback: Optional[date] = None) -> date:
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    s = str(val or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return fallback or date.today()


def process_proof_image(raw_proof: Optional[str], identifier: str) -> str:
    """Menyimpan gambar bukti transfer/nota terkompresi WebP agar tidak membebani PostgreSQL."""
    if not raw_proof:
        return ""
    val = str(raw_proof).strip()
    if is_base64_data_uri(val) or (
        len(val) > 500 and not val.startswith("http") and not val.startswith("/api/uploads/")
    ):
        return optimize_and_save_image(val, category="finance", identifier=identifier, max_dimension=1400, quality=82)
    return val


def log_finance_audit(action: str, subject: str, reason: str, actor: str):
    """Mencatat jejak aktivitas keuangan ke AuditLog tanpa menggagalkan transaksi utama."""
    try:
        db.session.add(
            AuditLog(
                member_nik="-",
                member_name=str(subject or "-")[:255],
                action=str(action or "UPDATE")[:50],
                previous_status=None,
                new_status=None,
                reason=reason,
                actor=(actor or "Bendahara")[:100],
                dpd="DPW Sulawesi Utara",
            )
        )
    except Exception as exc:
        logger.debug(f"Gagal mencatat audit keuangan: {exc}")


NON_DPW_EXPENSE_CATEGORIES = {"Setoran ke DPP", "Penarikan ke Kas Operasional"}


def is_real_dpw_expense(
    direction,
    category,
    related_remittance_id,
    related_batch_id,
) -> bool:
    """True bila transaksi adalah pengeluaran riil DPW (bukan setoran DPP atau transfer antar kas internal)."""
    if str(direction or "").upper() != "OUT":
        return False
    if related_remittance_id:
        return False
    if str(related_batch_id or "").startswith("XFER-"):
        return False
    if str(category or "").strip() in NON_DPW_EXPENSE_CATEGORIES:
        return False
    return True
