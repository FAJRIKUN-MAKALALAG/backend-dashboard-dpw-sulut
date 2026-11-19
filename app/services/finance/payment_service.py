"""Logika transaksi setoran fraksi, autodebet kolektif, pembatalan aman, dan pengiriman ke DPP."""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app.cache import invalidate_all_data_cache
from app.extensions import db
from app.models_finance import (
    FinanceAutodebetHistory,
    FinanceDppRemittance,
    FinanceDuesPayment,
    FinanceLegislator,
    FinanceTariffRule,
    FinanceTransaction,
)
from app.services.finance.helpers import (
    FinanceServiceError,
    current_year_month,
    format_month_label,
    is_valid_ym,
    log_finance_audit,
    parse_date_safe,
    process_proof_image,
)
from app.services.finance.serializers import (
    get_tariff_for_month,
    remittance_to_dict,
    transaction_to_dict,
)


def record_autodebet_transition(
    leg: FinanceLegislator,
    new_active: bool,
    start_month: Optional[str],
    end_month: Optional[str],
    notes: Optional[str],
    actor: str,
):
    """Mencatat histori mulai / berhenti autodebet setiap kali status autodebet anggota diubah."""
    ym_now = current_year_month()
    s_month = start_month if (start_month and is_valid_ym(start_month)) else (leg.autodebet_start_month or ym_now)
    e_month = end_month if (end_month and is_valid_ym(end_month)) else None

    if new_active:
        leg.is_autodebet_active = True
        leg.autodebet_start_month = s_month
        leg.autodebet_end_month = None
        active_entries = [h for h in leg.autodebet_histories if h.is_active]
        if active_entries:
            active_entries[0].start_month = s_month
            active_entries[0].end_month = None
            if notes is not None:
                active_entries[0].notes = notes
            active_entries[0].updated_by = actor
        else:
            db.session.add(
                FinanceAutodebetHistory(
                    id=f"ADH-{uuid.uuid4().hex[:12].upper()}",
                    legislator_id=leg.id,
                    start_month=s_month,
                    end_month=None,
                    is_active=True,
                    notes=notes or "Autodebet diaktifkan",
                    updated_by=actor,
                )
            )
    else:
        closed_end = e_month or ym_now
        had_autodebet = bool(leg.is_autodebet_active or leg.autodebet_histories)
        leg.is_autodebet_active = False
        if had_autodebet:
            leg.autodebet_start_month = s_month
            leg.autodebet_end_month = closed_end
        open_entries = [h for h in leg.autodebet_histories if h.is_active]
        if open_entries:
            for h in open_entries:
                h.is_active = False
                h.end_month = closed_end
                if notes:
                    h.notes = notes
                h.updated_by = actor
        elif e_month and s_month:
            db.session.add(
                FinanceAutodebetHistory(
                    id=f"ADH-{uuid.uuid4().hex[:12].upper()}",
                    legislator_id=leg.id,
                    start_month=s_month,
                    end_month=closed_end,
                    is_active=False,
                    notes=notes or "Riwayat autodebet (non-aktif)",
                    updated_by=actor,
                )
            )


def _reconcile_affected_transactions(tx_ids: Set[str]) -> None:
    """Menyelaraskan nominal transaksi buku kas dengan sisa baris iuran setelah pembatalan/hapus."""
    for tx_id in tx_ids:
        if not tx_id:
            continue
        tx = db.session.get(FinanceTransaction, tx_id)
        if not tx:
            continue
        remaining = FinanceDuesPayment.query.filter_by(transaction_id=tx_id).all()
        if not remaining:
            db.session.delete(tx)
            continue
        new_total = sum(int(p.amount_dpw or 0) + int(p.amount_dpp or 0) for p in remaining)
        if new_total <= 0:
            db.session.delete(tx)
        else:
            tx.amount = new_total


def create_manual_payment_tx(data: Dict[str, Any], actor: str) -> Dict[str, Any]:
    """Catat setoran fraksi (1 atau multi-bulan sekaligus) dan pecah Kas DPW vs Titipan DPP + Bank DPW."""
    leg_id = str(data.get("legislatorId") or "").strip()
    leg = db.session.get(FinanceLegislator, leg_id)
    if not leg:
        raise FinanceServiceError("Anggota dewan tidak ditemukan.", 404)

    raw_months = data.get("periodMonths") or ([data.get("periodMonth")] if data.get("periodMonth") else [])
    months = sorted({str(m).strip() for m in raw_months if is_valid_ym(str(m).strip())})
    if not months:
        raise FinanceServiceError("Pilih minimal 1 bulan iuran (YYYY-MM).", 400)

    payment_date = parse_date_safe(data.get("paymentDate"))
    mode = str(data.get("mode") or "BOTH").upper()
    method = "AUTODEBET" if str(data.get("paymentMethod") or "").upper() == "AUTODEBET" else "MANUAL"
    notes = str(data.get("notes") or "").strip()

    rules = FinanceTariffRule.query.all()
    batch_id = f"PAYB-{uuid.uuid4().hex[:10].upper()}"
    proof_url = process_proof_image(data.get("proofUrl"), f"pay_{leg.id}_{batch_id}")

    custom_dpw_total = max(0, int(data.get("customAmountDpw") or 0))
    custom_dpp_total = max(0, int(data.get("customAmountDpp") or 0))

    created_payments: List[FinanceDuesPayment] = []
    total_dpw = 0
    total_dpp = 0

    for idx, ym in enumerate(months):
        t_dpw, t_dpp = get_tariff_for_month(rules, leg.tingkat, leg.daerah, ym)
        if mode == "CUSTOM":
            amt_dpw = custom_dpw_total if idx == 0 else 0
            amt_dpp = custom_dpp_total if idx == 0 else 0
        elif mode == "DPW_ONLY":
            amt_dpw, amt_dpp = t_dpw, 0
        elif mode == "DPP_ONLY":
            amt_dpw, amt_dpp = 0, t_dpp
        else:
            amt_dpw, amt_dpp = t_dpw, t_dpp

        if amt_dpw < 0 or amt_dpp < 0 or (amt_dpw == 0 and amt_dpp == 0):
            continue

        total_dpw += amt_dpw
        total_dpp += amt_dpp
        created_payments.append(
            FinanceDuesPayment(
                id=f"PAY-{uuid.uuid4().hex[:12].upper()}",
                legislator_id=leg.id,
                period_month=ym,
                payment_date=payment_date,
                amount_dpw=amt_dpw,
                amount_dpp=amt_dpp,
                payment_method=method,
                proof_url=proof_url or None,
                notes=notes or f"Setoran {format_month_label(ym)}",
                is_historical=False,
                created_by=actor,
            )
        )

    if not created_payments:
        raise FinanceServiceError("Nominal DPW maupun DPP harus lebih besar dari Rp0.", 400)

    grand_total = total_dpw + total_dpp
    period_labels = ", ".join(format_month_label(m) for m in months)
    tx_id = f"TX-{uuid.uuid4().hex[:12].upper()}"
    tx = FinanceTransaction(
        id=tx_id,
        tx_date=payment_date,
        account_type="BANK_DPW",
        direction="IN",
        category="Autodebet Fraksi" if method == "AUTODEBET" else "Iuran Fraksi",
        description=(
            f"Setoran {leg.nama_lengkap} ({leg.daerah}) [{period_labels}] — "
            f"DPW Rp{total_dpw:,} + DPP Rp{total_dpp:,}"
        ).replace(",", "."),
        amount=grand_total,
        proof_url=proof_url or None,
        related_batch_id=batch_id,
        is_historical=False,
        created_by=actor,
    )
    # Wajib flush induk FinanceTransaction sebelum insert child FinanceDuesPayment agar FK PostgreSQL terpenuhi
    db.session.add(tx)
    db.session.flush()
    for p in created_payments:
        p.transaction_id = tx_id
        db.session.add(p)

    log_finance_audit("CREATE", leg.nama_lengkap, f"Setoran iuran {period_labels}: total Rp{grand_total:,}", actor)
    db.session.commit()
    invalidate_all_data_cache()

    return {
        "ok": True,
        "createdCount": len(created_payments),
        "totalDpw": total_dpw,
        "totalDpp": total_dpp,
        "grandTotal": grand_total,
        "transaction": transaction_to_dict(tx, locked_tx_ids={tx_id}),
    }


def create_autodebet_batch_tx(data: Dict[str, Any], actor: str) -> Dict[str, Any]:
    """1-Klik Catat Autodebet Bulanan per wilayah (Provinsi, Minsel, Boltim, dll)."""
    period_month = str(data.get("periodMonth") or current_year_month()).strip()
    if not is_valid_ym(period_month):
        raise FinanceServiceError("Bulan autodebet harus berformat YYYY-MM.", 400)

    daerah = str(data.get("daerah") or "").strip()
    legislator_ids = [str(x).strip() for x in (data.get("legislatorIds") or []) if str(x).strip()]
    include_dpw = bool(data.get("includeDpw", True))
    include_dpp = bool(data.get("includeDpp", True))
    payment_date = parse_date_safe(data.get("paymentDate"))
    notes = str(data.get("notes") or "").strip()

    q = FinanceLegislator.query.filter(
        FinanceLegislator.is_active.is_(True),
        FinanceLegislator.is_autodebet_active.is_(True),
    )
    if legislator_ids:
        q = q.filter(FinanceLegislator.id.in_(legislator_ids))
    elif daerah:
        q = q.filter(func.lower(FinanceLegislator.daerah) == daerah.lower())
    else:
        raise FinanceServiceError("Pilih daerah atau daftar anggota autodebet.", 400)

    targets = q.order_by(FinanceLegislator.urutan.asc()).all()
    if not targets:
        raise FinanceServiceError("Tidak ditemukan anggota autodebet aktif untuk wilayah tersebut.", 404)

    rules = FinanceTariffRule.query.all()
    existing_month_pays = FinanceDuesPayment.query.filter(
        FinanceDuesPayment.legislator_id.in_([t.id for t in targets]),
        FinanceDuesPayment.period_month == period_month,
    ).all()
    paid_map: Dict[str, Dict[str, int]] = defaultdict(lambda: {"dpw": 0, "dpp": 0})
    for p in existing_month_pays:
        paid_map[p.legislator_id]["dpw"] += int(p.amount_dpw or 0)
        paid_map[p.legislator_id]["dpp"] += int(p.amount_dpp or 0)

    batch_id = f"AD-{period_month}-{uuid.uuid4().hex[:8].upper()}"
    proof_url = process_proof_image(data.get("proofUrl"), f"autodebet_{batch_id}")

    created_payments: List[FinanceDuesPayment] = []
    skipped_names: List[str] = []
    total_dpw = 0
    total_dpp = 0

    for leg in targets:
        dpw_t, dpp_t = get_tariff_for_month(rules, leg.tingkat, leg.daerah, period_month)
        prev = paid_map[leg.id]
        add_dpw = max(0, dpw_t - prev["dpw"]) if include_dpw else 0
        add_dpp = max(0, dpp_t - prev["dpp"]) if include_dpp else 0
        if add_dpw == 0 and add_dpp == 0:
            skipped_names.append(leg.nama_lengkap)
            continue

        total_dpw += add_dpw
        total_dpp += add_dpp
        created_payments.append(
            FinanceDuesPayment(
                id=f"PAY-{uuid.uuid4().hex[:12].upper()}",
                legislator_id=leg.id,
                period_month=period_month,
                payment_date=payment_date,
                amount_dpw=add_dpw,
                amount_dpp=add_dpp,
                payment_method="AUTODEBET",
                proof_url=proof_url or None,
                notes=notes or f"Autodebet Kolektif {format_month_label(period_month)}",
                is_historical=False,
                created_by=actor,
            )
        )

    if not created_payments:
        raise FinanceServiceError(
            f"Seluruh anggota autodebet {daerah or ''} bulan {format_month_label(period_month)} sudah tercatat lunas.",
            409,
        )

    grand_total = total_dpw + total_dpp
    region_label = daerah or targets[0].daerah
    tx_id = f"TX-{uuid.uuid4().hex[:12].upper()}"
    tx = FinanceTransaction(
        id=tx_id,
        tx_date=payment_date,
        account_type="BANK_DPW",
        direction="IN",
        category="Autodebet Fraksi",
        description=(
            f"Autodebet {region_label} ({len(created_payments)} org) {format_month_label(period_month)} "
            f"— DPW Rp{total_dpw:,} + Titipan DPP Rp{total_dpp:,}"
        ).replace(",", "."),
        amount=grand_total,
        proof_url=proof_url or None,
        related_batch_id=batch_id,
        is_historical=False,
        created_by=actor,
    )
    # Wajib flush induk FinanceTransaction sebelum insert child FinanceDuesPayment agar FK PostgreSQL terpenuhi
    db.session.add(tx)
    db.session.flush()
    for p in created_payments:
        p.transaction_id = tx_id
        db.session.add(p)

    log_finance_audit(
        "CREATE",
        f"Autodebet {region_label}",
        f"Catat autodebet {len(created_payments)} org bulan {period_month}: total Rp{grand_total:,}",
        actor,
    )
    db.session.commit()
    invalidate_all_data_cache()

    return {
        "ok": True,
        "createdCount": len(created_payments),
        "skippedCount": len(skipped_names),
        "skippedNames": skipped_names,
        "totalDpw": total_dpw,
        "totalDpp": total_dpp,
        "grandTotal": grand_total,
        "transaction": transaction_to_dict(tx, locked_tx_ids={tx_id}),
    }


def delete_single_payment_tx(payment_id: str, actor: str) -> Dict[str, Any]:
    """Menghapus 1 entri setoran iuran dan menyelaraskan mutasi rekening bank terkait."""
    p = db.session.get(FinanceDuesPayment, payment_id)
    if not p:
        raise FinanceServiceError("Data pembayaran tidak ditemukan.", 404)
    if p.dpp_remittance_id is not None:
        raise FinanceServiceError("Setoran sudah masuk batch pengiriman ke DPP dan tidak dapat dihapus.", 409)

    tx_id = p.transaction_id
    removed_total = int(p.amount_dpw or 0) + int(p.amount_dpp or 0)
    db.session.delete(p)
    db.session.flush()

    if tx_id:
        _reconcile_affected_transactions({tx_id})

    log_finance_audit("DELETE", payment_id, f"Hapus setoran iuran sebesar Rp{removed_total:,}", actor)
    db.session.commit()
    invalidate_all_data_cache()
    return {"ok": True, "removedTotal": removed_total}


def cancel_autodebet_payments_tx(
    period_month: str,
    daerah: str,
    legislator_id: str,
    only_autodebet: bool,
    actor: str,
) -> Dict[str, Any]:
    """Membatalkan catatan autodebet/setoran non-historis per wilayah atau per anggota pada 1 bulan."""
    ym = str(period_month or "").strip()
    if not is_valid_ym(ym):
        raise FinanceServiceError("Bulan yang akan dibatalkan harus berformat YYYY-MM.", 400)

    clean_daerah = str(daerah or "").strip()
    clean_leg_id = str(legislator_id or "").strip()
    if not clean_daerah and not clean_leg_id:
        raise FinanceServiceError("Pilih daerah atau anggota untuk membatalkan catatan autodebet.", 400)

    q = FinanceDuesPayment.query.filter(
        FinanceDuesPayment.period_month == ym,
        FinanceDuesPayment.is_historical.is_(False),
    )
    if only_autodebet:
        q = q.filter(FinanceDuesPayment.payment_method == "AUTODEBET")

    if clean_leg_id:
        q = q.filter(FinanceDuesPayment.legislator_id == clean_leg_id)
    else:
        q = q.join(
            FinanceLegislator, FinanceDuesPayment.legislator_id == FinanceLegislator.id
        ).filter(func.lower(FinanceLegislator.daerah) == clean_daerah.lower())

    matched = q.all()
    locked = [p for p in matched if p.dpp_remittance_id is not None]
    deletable = [p for p in matched if p.dpp_remittance_id is None]

    if not deletable:
        label = format_month_label(ym)
        if locked:
            raise FinanceServiceError(
                f"Setoran {label} sudah masuk batch pengiriman ke DPP. Batalkan batch pengiriman DPP terlebih dahulu sebelum membatalkan setoran ini.",
                409,
            )
        kind_label = "autodebet " if only_autodebet else ""
        raise FinanceServiceError(f"Tidak ada catatan {kind_label}yang dapat dibatalkan untuk {label}.", 404)

    removed_dpw = sum(int(p.amount_dpw or 0) for p in deletable)
    removed_dpp = sum(int(p.amount_dpp or 0) for p in deletable)
    removed_total = removed_dpw + removed_dpp
    affected_tx_ids: Set[str] = {p.transaction_id for p in deletable if p.transaction_id}

    for p in deletable:
        db.session.delete(p)
    db.session.flush()

    _reconcile_affected_transactions(affected_tx_ids)

    subject = clean_daerah or clean_leg_id
    log_finance_audit(
        "DELETE",
        f"Batal Autodebet {subject}",
        f"Batalkan {len(deletable)} entri bulan {ym}: total Rp{removed_total:,}",
        actor,
    )
    db.session.commit()
    invalidate_all_data_cache()

    return {
        "ok": True,
        "deletedCount": len(deletable),
        "skippedLockedCount": len(locked),
        "removedDpw": removed_dpw,
        "removedDpp": removed_dpp,
        "removedTotal": removed_total,
    }


def create_dpp_remittance_tx(data: Dict[str, Any], actor: str) -> Dict[str, Any]:
    """Kirim batch dana titipan DPP dari Rekening Bank DPW ke Rekening DPP Pusat."""
    payment_ids = [str(x).strip() for x in (data.get("paymentIds") or []) if str(x).strip()]
    if not payment_ids:
        raise FinanceServiceError("Pilih minimal 1 setoran DPP yang akan dikirim.", 400)

    rows = FinanceDuesPayment.query.filter(
        FinanceDuesPayment.id.in_(payment_ids),
        FinanceDuesPayment.amount_dpp > 0,
        FinanceDuesPayment.dpp_remittance_id.is_(None),
    ).all()
    if not rows:
        raise FinanceServiceError(
            "Tidak ada item valid yang siap dikirim ke DPP (mungkin sudah dikirim sebelumnya).",
            409,
        )

    total_dpp = sum(int(p.amount_dpp or 0) for p in rows)
    bank_fee = max(0, int(data.get("bankFee") or 0))
    total_transferred = total_dpp + bank_fee
    rem_date = parse_date_safe(data.get("remittanceDate"))
    rem_id = f"REM-{uuid.uuid4().hex[:10].upper()}"
    proof_url = process_proof_image(data.get("proofUrl"), f"dpp_{rem_id}")

    months_in_batch = sorted({p.period_month for p in rows})
    auto_label = (
        format_month_label(months_in_batch[0])
        if len(months_in_batch) == 1
        else f"{format_month_label(months_in_batch[0])} - {format_month_label(months_in_batch[-1])}"
    )
    period_label = str(data.get("periodLabel") or auto_label).strip()

    rem = FinanceDppRemittance(
        id=rem_id,
        remittance_date=rem_date,
        period_label=period_label,
        total_dpp_amount=total_dpp,
        bank_fee=bank_fee,
        total_transferred=total_transferred,
        recipient_account=str(data.get("recipientAccount") or "Rekening DPP Partai NasDem Pusat").strip(),
        reference_no=str(data.get("referenceNo") or "").strip() or None,
        proof_url=proof_url or None,
        notes=str(data.get("notes") or "").strip() or None,
        is_historical=False,
        created_by=actor,
    )
    # Flush parent remittance lebih dulu sebelum mengaitkan child payments.dpp_remittance_id
    db.session.add(rem)
    db.session.flush()

    for p in rows:
        p.dpp_remittance_id = rem.id

    tx = FinanceTransaction(
        id=f"TX-{uuid.uuid4().hex[:12].upper()}",
        tx_date=rem_date,
        account_type="BANK_DPW",
        direction="OUT",
        category="Setoran ke DPP",
        description=(
            f"Pengiriman Iuran ke DPP Pusat ({len(rows)} setoran, periode {period_label}) "
            f"— Pokok Rp{total_dpp:,} + Admin Rp{bank_fee:,}"
        ).replace(",", "."),
        amount=total_transferred,
        proof_url=proof_url or None,
        related_remittance_id=rem.id,
        is_historical=False,
        created_by=actor,
    )
    db.session.add(tx)

    log_finance_audit(
        "CREATE",
        f"Remitansi DPP {period_label}",
        f"Kirim DPP Rp{total_dpp:,} (+ admin Rp{bank_fee:,})",
        actor,
    )
    db.session.commit()
    invalidate_all_data_cache()

    rem_loaded = (
        FinanceDppRemittance.query.options(
            selectinload(FinanceDppRemittance.payments).selectinload(FinanceDuesPayment.legislator)
        )
        .filter_by(id=rem.id)
        .first()
    )
    return remittance_to_dict(rem_loaded or rem, include_items=True)


def delete_dpp_remittance_tx(remittance_id: str, actor: str) -> Dict[str, Any]:
    """Membatalkan batch pengiriman DPP non-historis agar setoran kembali ke antrean titipan."""
    rem = (
        FinanceDppRemittance.query.options(selectinload(FinanceDppRemittance.payments))
        .filter_by(id=remittance_id)
        .first()
    )
    if not rem:
        raise FinanceServiceError("Batch pengiriman DPP tidak ditemukan.", 404)
    if rem.is_historical:
        raise FinanceServiceError("Batch historis hasil migrasi Excel tidak dapat dibatalkan.", 409)

    released_count = len(rem.payments or [])
    for p in rem.payments or []:
        p.dpp_remittance_id = None

    linked_txs = FinanceTransaction.query.filter_by(related_remittance_id=rem.id).all()
    for tx in linked_txs:
        db.session.delete(tx)

    label = rem.period_label or rem.id
    amount = int(rem.total_dpp_amount or 0)
    db.session.delete(rem)

    log_finance_audit(
        "DELETE",
        f"Batal Remitansi DPP {label}",
        f"Batalkan pengiriman DPP Rp{amount:,} ({released_count} setoran dilepaskan kembali)",
        actor,
    )
    db.session.commit()
    invalidate_all_data_cache()

    return {"ok": True, "releasedCount": released_count}
