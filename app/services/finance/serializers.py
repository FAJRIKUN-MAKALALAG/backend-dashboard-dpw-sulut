"""Serializer JSON dan lookup aturan tarif untuk entitas modul keuangan."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from app.models_finance import (
    FinanceAutodebetHistory,
    FinanceDppRemittance,
    FinanceDuesPayment,
    FinanceLegislator,
    FinanceTariffRule,
    FinanceTransaction,
)
from app.services.finance.helpers import current_year_month, format_month_label, is_valid_ym
from app.services.image_storage import resolve_stored_image_url


def get_tariff_for_month(
    rules: List[FinanceTariffRule],
    tingkat: str,
    daerah: str,
    month_str: str,
) -> Tuple[int, int]:
    """Mengambil tarif (amount_dpw, amount_dpp) yang berlaku pada bulan `month_str` untuk daerah tertentu."""
    t_norm = str(tingkat or "").strip().upper()
    d_norm = str(daerah or "").strip().lower()
    matched = [
        r
        for r in rules
        if str(r.tingkat or "").strip().upper() == t_norm
        and str(r.daerah or "").strip().lower() == d_norm
        and r.effective_from <= month_str
        and (not r.effective_to or month_str <= r.effective_to)
    ]
    if not matched:
        return (0, 0)
    matched.sort(key=lambda r: (r.effective_from, r.updated_at or datetime.min), reverse=True)
    top = matched[0]
    return int(top.amount_dpw or 0), int(top.amount_dpp or 0)


def autodebet_history_to_dict(h: FinanceAutodebetHistory) -> Dict[str, Any]:
    return {
        "id": h.id,
        "legislatorId": h.legislator_id,
        "startMonth": h.start_month,
        "endMonth": h.end_month,
        "isActive": bool(h.is_active),
        "notes": h.notes or "",
        "updatedBy": h.updated_by or "",
        "createdAt": h.created_at.isoformat() if h.created_at else None,
    }


def tariff_to_dict(r: FinanceTariffRule) -> Dict[str, Any]:
    return {
        "id": r.id,
        "tingkat": r.tingkat,
        "daerah": r.daerah,
        "effectiveFrom": r.effective_from,
        "effectiveTo": r.effective_to,
        "amountDpw": int(r.amount_dpw or 0),
        "amountDpp": int(r.amount_dpp or 0),
        "totalObligation": int(r.amount_dpw or 0) + int(r.amount_dpp or 0),
        "notes": r.notes or "",
        "createdAt": r.created_at.isoformat() if r.created_at else None,
        "updatedAt": r.updated_at.isoformat() if r.updated_at else None,
    }


def legislator_to_dict(
    leg: FinanceLegislator,
    rules: Optional[List[FinanceTariffRule]] = None,
    ref_month: Optional[str] = None,
) -> Dict[str, Any]:
    month_key = ref_month if (ref_month and is_valid_ym(ref_month)) else current_year_month()
    dpw_tariff, dpp_tariff = (
        get_tariff_for_month(rules or [], leg.tingkat, leg.daerah, month_key)
        if rules is not None
        else (0, 0)
    )
    histories = [autodebet_history_to_dict(h) for h in (leg.autodebet_histories or [])]
    return {
        "id": leg.id,
        "namaLengkap": leg.nama_lengkap,
        "tingkat": leg.tingkat,
        "daerah": leg.daerah,
        "urutan": int(leg.urutan or 1),
        "isActive": bool(leg.is_active),
        "isAutodebetActive": bool(leg.is_autodebet_active),
        "autodebetStartMonth": leg.autodebet_start_month,
        "autodebetEndMonth": leg.autodebet_end_month,
        "autodebetHistories": histories,
        "catatan": leg.catatan or "",
        "memberNik": leg.member_nik or "",
        "currentTariffDpw": dpw_tariff,
        "currentTariffDpp": dpp_tariff,
        "currentTariffTotal": dpw_tariff + dpp_tariff,
    }


def payment_to_dict(p: FinanceDuesPayment) -> Dict[str, Any]:
    dpw = int(p.amount_dpw or 0)
    dpp = int(p.amount_dpp or 0)
    return {
        "id": p.id,
        "legislatorId": p.legislator_id,
        "namaLengkap": p.legislator.nama_lengkap if p.legislator else p.legislator_id,
        "tingkat": p.legislator.tingkat if p.legislator else "",
        "daerah": p.legislator.daerah if p.legislator else "",
        "periodMonth": p.period_month,
        "periodLabel": format_month_label(p.period_month),
        "paymentDate": p.payment_date.isoformat() if p.payment_date else None,
        "amountDpw": dpw,
        "amountDpp": dpp,
        "totalAmount": dpw + dpp,
        "paymentMethod": p.payment_method,
        "dppRemittanceId": p.dpp_remittance_id,
        "transactionId": p.transaction_id,
        "proofUrl": resolve_stored_image_url(p.proof_url) if p.proof_url else "",
        "notes": p.notes or "",
        "isHistorical": bool(p.is_historical),
        "canDelete": p.dpp_remittance_id is None,
        "createdBy": p.created_by or "",
        "createdAt": p.created_at.isoformat() if p.created_at else None,
    }


def transaction_to_dict(
    tx: FinanceTransaction,
    locked_tx_ids: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    lock_reason: Optional[str] = None
    if tx.is_historical:
        lock_reason = "HISTORICAL"
    elif tx.related_remittance_id:
        lock_reason = "DPP_REMITTANCE"
    elif locked_tx_ids and tx.id in locked_tx_ids:
        lock_reason = "DUES_PAYMENT"

    return {
        "id": tx.id,
        "txDate": tx.tx_date.isoformat() if tx.tx_date else None,
        "accountType": tx.account_type,
        "direction": tx.direction,
        "category": tx.category,
        "description": tx.description,
        "amount": int(tx.amount or 0),
        "proofUrl": resolve_stored_image_url(tx.proof_url) if tx.proof_url else "",
        "relatedRemittanceId": tx.related_remittance_id,
        "relatedBatchId": tx.related_batch_id,
        "isHistorical": bool(tx.is_historical),
        "canDelete": lock_reason is None,
        "lockReason": lock_reason,
        "createdBy": tx.created_by or "",
        "createdAt": tx.created_at.isoformat() if tx.created_at else None,
    }


def remittance_to_dict(rem: FinanceDppRemittance, include_items: bool = False) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "id": rem.id,
        "remittanceDate": rem.remittance_date.isoformat() if rem.remittance_date else None,
        "periodLabel": rem.period_label or "",
        "totalDppAmount": int(rem.total_dpp_amount or 0),
        "bankFee": int(rem.bank_fee or 0),
        "totalTransferred": int(rem.total_transferred or 0),
        "recipientAccount": rem.recipient_account or "",
        "referenceNo": rem.reference_no or "",
        "proofUrl": resolve_stored_image_url(rem.proof_url) if rem.proof_url else "",
        "notes": rem.notes or "",
        "isHistorical": bool(rem.is_historical),
        "itemCount": len(rem.payments or []),
        "createdBy": rem.created_by or "",
        "createdAt": rem.created_at.isoformat() if rem.created_at else None,
    }
    if include_items:
        data["items"] = [
            {
                "id": p.id,
                "legislatorId": p.legislator_id,
                "namaLengkap": p.legislator.nama_lengkap if p.legislator else p.legislator_id,
                "daerah": p.legislator.daerah if p.legislator else "",
                "tingkat": p.legislator.tingkat if p.legislator else "",
                "periodMonth": p.period_month,
                "paymentDate": p.payment_date.isoformat() if p.payment_date else None,
                "amountDpp": int(p.amount_dpp or 0),
                "paymentMethod": p.payment_method,
            }
            for p in (rem.payments or [])
        ]
    return data
