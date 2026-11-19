"""Route endpoints antrean titipan DPP, batch pengiriman ke DPP Pusat, dan pembatalan batch."""

from __future__ import annotations

from collections import defaultdict
from typing import Dict

from flask import jsonify, request
from sqlalchemy.orm import selectinload

from app.auth import current_claims, finance_access_required
from app.models_finance import FinanceDppRemittance, FinanceDuesPayment
from app.routes.finance import bp
from app.services.finance import (
    create_dpp_remittance_tx,
    delete_dpp_remittance_tx,
    format_month_label,
    remittance_to_dict,
)


@bp.get("/finance/dpp-remittances/pending")
@finance_access_required
def get_pending_dpp_remittances():
    rows = (
        FinanceDuesPayment.query.options(selectinload(FinanceDuesPayment.legislator))
        .filter(
            FinanceDuesPayment.amount_dpp > 0,
            FinanceDuesPayment.dpp_remittance_id.is_(None),
        )
        .order_by(FinanceDuesPayment.payment_date.asc(), FinanceDuesPayment.period_month.asc())
        .all()
    )

    items = []
    by_daerah: Dict[str, int] = defaultdict(int)
    total = 0

    for p in rows:
        amt = int(p.amount_dpp or 0)
        daerah = p.legislator.daerah if p.legislator else "-"
        tingkat = p.legislator.tingkat if p.legislator else "-"
        nama = p.legislator.nama_lengkap if p.legislator else p.legislator_id
        total += amt
        by_daerah[daerah] += amt
        items.append(
            {
                "id": p.id,
                "legislatorId": p.legislator_id,
                "namaLengkap": nama,
                "tingkat": tingkat,
                "daerah": daerah,
                "periodMonth": p.period_month,
                "periodLabel": format_month_label(p.period_month),
                "paymentDate": p.payment_date.isoformat() if p.payment_date else None,
                "amountDpp": amt,
                "amountDpw": int(p.amount_dpw or 0),
                "paymentMethod": p.payment_method,
            }
        )

    return jsonify(
        items=items,
        totalPendingDpp=total,
        count=len(items),
        byDaerah=[{"daerah": k, "totalAmount": v} for k, v in sorted(by_daerah.items(), key=lambda x: -x[1])],
    )


@bp.get("/finance/dpp-remittances")
@finance_access_required
def list_dpp_remittances():
    rows = (
        FinanceDppRemittance.query.options(
            selectinload(FinanceDppRemittance.payments).selectinload(FinanceDuesPayment.legislator)
        )
        .order_by(FinanceDppRemittance.remittance_date.desc(), FinanceDppRemittance.created_at.desc())
        .all()
    )
    return jsonify(items=[remittance_to_dict(r, include_items=True) for r in rows])


@bp.post("/finance/dpp-remittances")
@finance_access_required
def create_dpp_remittance():
    claims = current_claims()
    actor = claims.get("adminName") or "Wakil Bendahara"
    data = request.get_json(silent=True) or {}
    return jsonify(create_dpp_remittance_tx(data, actor)), 201


@bp.delete("/finance/dpp-remittances/<remittance_id>")
@finance_access_required
def delete_dpp_remittance(remittance_id: str):
    claims = current_claims()
    actor = claims.get("adminName") or "Wakil Bendahara"
    return jsonify(delete_dpp_remittance_tx(remittance_id, actor)), 200
