"""Blueprint REST API Modul Keuangan DPW NasDem Sulawesi Utara."""

from __future__ import annotations

from flask import Blueprint, jsonify

from app.services.finance import FinanceServiceError

bp = Blueprint("finance", __name__)


@bp.errorhandler(FinanceServiceError)
def handle_finance_service_error(err: FinanceServiceError):
    return jsonify(error=err.message), err.status_code


# Daftarkan seluruh sub-router modul keuangan ke Blueprint `bp`
from app.routes.finance import (  # noqa: E402, F401
    cashbook_routes,
    master_routes,
    payment_routes,
    remittance_routes,
    summary_routes,
)
