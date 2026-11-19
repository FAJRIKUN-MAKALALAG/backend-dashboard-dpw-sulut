"""Route endpoints master anggota fraksi/dewan dan aturan tarif daerah."""

from __future__ import annotations

import uuid

from flask import jsonify, request
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app.auth import current_claims, finance_access_required
from app.cache import invalidate_all_data_cache
from app.extensions import db
from app.models_finance import FinanceDuesPayment, FinanceLegislator, FinanceTariffRule
from app.routes.finance import bp
from app.services.finance import (
    is_valid_ym,
    legislator_to_dict,
    log_finance_audit,
    record_autodebet_transition,
    tariff_to_dict,
)


@bp.get("/finance/legislators")
@finance_access_required
def list_legislators():
    ref_month = (request.args.get("month") or "").strip() or None
    include_inactive = request.args.get("includeInactive", "false").lower() == "true"

    rules = FinanceTariffRule.query.all()
    q = FinanceLegislator.query.options(selectinload(FinanceLegislator.autodebet_histories))
    if not include_inactive:
        q = q.filter(FinanceLegislator.is_active.is_(True))
    rows = q.order_by(
        FinanceLegislator.tingkat.desc(),
        FinanceLegislator.urutan.asc(),
        FinanceLegislator.nama_lengkap.asc(),
    ).all()

    daerah_list = sorted({r.daerah for r in rows})
    return jsonify(
        items=[legislator_to_dict(r, rules=rules, ref_month=ref_month) for r in rows],
        daerahList=daerah_list,
    )


@bp.post("/finance/legislators")
@finance_access_required
def create_legislator():
    claims = current_claims()
    actor = claims.get("adminName") or "Wakil Bendahara"
    data = request.get_json(silent=True) or {}

    nama = str(data.get("namaLengkap") or "").strip()
    tingkat = str(data.get("tingkat") or "KAB_KOTA").strip().upper()
    daerah = str(data.get("daerah") or "").strip()
    if not nama or not daerah:
        return jsonify(error="Nama lengkap dan daerah wajib diisi."), 400
    if tingkat not in ("PROVINSI", "KAB_KOTA"):
        return jsonify(error="Tingkat harus PROVINSI atau KAB_KOTA."), 400

    max_urutan = (
        db.session.query(func.coalesce(func.max(FinanceLegislator.urutan), 0))
        .filter(FinanceLegislator.daerah == daerah)
        .scalar()
        or 0
    )

    leg = FinanceLegislator(
        id=f"LEG-{uuid.uuid4().hex[:10].upper()}",
        nama_lengkap=nama,
        tingkat=tingkat,
        daerah=daerah,
        urutan=int(data.get("urutan") or (max_urutan + 1)),
        is_active=True,
        is_autodebet_active=False,
        catatan=str(data.get("catatan") or "").strip() or None,
        member_nik=str(data.get("memberNik") or "").strip() or None,
    )
    db.session.add(leg)
    db.session.flush()

    if data.get("isAutodebetActive"):
        record_autodebet_transition(
            leg,
            True,
            data.get("autodebetStartMonth"),
            None,
            data.get("autodebetNotes") or "Autodebet aktif saat penambahan anggota fraksi",
            actor,
        )

    log_finance_audit("CREATE", nama, f"Tambah anggota fraksi {tingkat} {daerah}", actor)
    db.session.commit()
    invalidate_all_data_cache()
    rules = FinanceTariffRule.query.all()
    return jsonify(legislator_to_dict(leg, rules=rules)), 201


@bp.put("/finance/legislators/<leg_id>")
@finance_access_required
def update_legislator(leg_id: str):
    claims = current_claims()
    actor = claims.get("adminName") or "Wakil Bendahara"
    leg = FinanceLegislator.query.options(
        selectinload(FinanceLegislator.autodebet_histories)
    ).filter_by(id=leg_id).first()
    if not leg:
        return jsonify(error="Anggota fraksi tidak ditemukan."), 404

    data = request.get_json(silent=True) or {}
    if "namaLengkap" in data and str(data["namaLengkap"]).strip():
        leg.nama_lengkap = str(data["namaLengkap"]).strip()
    if "tingkat" in data and str(data["tingkat"]).upper() in ("PROVINSI", "KAB_KOTA"):
        leg.tingkat = str(data["tingkat"]).upper()
    if "daerah" in data and str(data["daerah"]).strip():
        leg.daerah = str(data["daerah"]).strip()
    if "urutan" in data and data["urutan"] is not None:
        leg.urutan = int(data["urutan"])
    if "isActive" in data:
        leg.is_active = bool(data["isActive"])
    if "catatan" in data:
        leg.catatan = str(data["catatan"] or "").strip() or None
    if "memberNik" in data:
        leg.member_nik = str(data["memberNik"] or "").strip() or None

    if any(k in data for k in ("isAutodebetActive", "autodebetStartMonth", "autodebetEndMonth")):
        new_active = bool(data.get("isAutodebetActive", leg.is_autodebet_active))
        record_autodebet_transition(
            leg,
            new_active,
            data.get("autodebetStartMonth"),
            data.get("autodebetEndMonth"),
            data.get("autodebetNotes"),
            actor,
        )

    log_finance_audit("UPDATE", leg.nama_lengkap, f"Perbarui data fraksi / status autodebet {leg.daerah}", actor)
    db.session.commit()
    invalidate_all_data_cache()
    rules = FinanceTariffRule.query.all()
    return jsonify(legislator_to_dict(leg, rules=rules))


@bp.delete("/finance/legislators/<leg_id>")
@finance_access_required
def delete_legislator(leg_id: str):
    claims = current_claims()
    actor = claims.get("adminName") or "Wakil Bendahara"
    leg = db.session.get(FinanceLegislator, leg_id)
    if not leg:
        return jsonify(error="Anggota fraksi tidak ditemukan."), 404

    has_payments = FinanceDuesPayment.query.filter_by(legislator_id=leg_id).count() > 0
    nama = leg.nama_lengkap
    if has_payments:
        leg.is_active = False
        leg.is_autodebet_active = False
        msg = "Anggota fraksi dinonaktifkan agar histori setoran tetap terjaga."
    else:
        db.session.delete(leg)
        msg = "Anggota fraksi dihapus permanen."

    log_finance_audit("DELETE", nama, msg, actor)
    db.session.commit()
    invalidate_all_data_cache()
    return jsonify(ok=True, message=msg)


# ---------------- Master Tarif Per Daerah ----------------
@bp.get("/finance/tariffs")
@finance_access_required
def list_tariffs():
    rows = FinanceTariffRule.query.order_by(
        FinanceTariffRule.tingkat.desc(),
        FinanceTariffRule.daerah.asc(),
        FinanceTariffRule.effective_from.asc(),
    ).all()
    return jsonify(items=[tariff_to_dict(r) for r in rows])


@bp.post("/finance/tariffs")
@finance_access_required
def create_tariff():
    claims = current_claims()
    actor = claims.get("adminName") or "Wakil Bendahara"
    data = request.get_json(silent=True) or {}

    tingkat = str(data.get("tingkat") or "KAB_KOTA").strip().upper()
    daerah = str(data.get("daerah") or "").strip()
    eff_from = str(data.get("effectiveFrom") or "").strip()
    eff_to = str(data.get("effectiveTo") or "").strip() or None
    amount_dpw = int(data.get("amountDpw") or 0)
    amount_dpp = int(data.get("amountDpp") or 0)

    if not daerah or not is_valid_ym(eff_from):
        return jsonify(error="Daerah dan bulan mulai berlaku (YYYY-MM) wajib valid."), 400
    if eff_to and (not is_valid_ym(eff_to) or eff_to < eff_from):
        return jsonify(error="Bulan selesai tidak valid atau lebih kecil dari bulan mulai."), 400
    if amount_dpw < 0 or amount_dpp < 0:
        return jsonify(error="Nominal tarif tidak boleh negatif."), 400

    rule = FinanceTariffRule(
        id=f"TRF-{uuid.uuid4().hex[:12].upper()}",
        tingkat=tingkat if tingkat in ("PROVINSI", "KAB_KOTA") else "KAB_KOTA",
        daerah=daerah,
        effective_from=eff_from,
        effective_to=eff_to,
        amount_dpw=amount_dpw,
        amount_dpp=amount_dpp,
        notes=str(data.get("notes") or "").strip() or None,
    )
    db.session.add(rule)
    log_finance_audit("CREATE", f"Tarif {daerah}", f"Tarif baru {eff_from}: DPW {amount_dpw}, DPP {amount_dpp}", actor)
    db.session.commit()
    invalidate_all_data_cache()
    return jsonify(tariff_to_dict(rule)), 201


@bp.put("/finance/tariffs/<rule_id>")
@finance_access_required
def update_tariff(rule_id: str):
    claims = current_claims()
    actor = claims.get("adminName") or "Wakil Bendahara"
    rule = db.session.get(FinanceTariffRule, rule_id)
    if not rule:
        return jsonify(error="Aturan tarif tidak ditemukan."), 404

    data = request.get_json(silent=True) or {}
    if "effectiveFrom" in data and is_valid_ym(data["effectiveFrom"]):
        rule.effective_from = data["effectiveFrom"].strip()
    if "effectiveTo" in data:
        eff_to = str(data["effectiveTo"] or "").strip() or None
        if eff_to and (not is_valid_ym(eff_to) or eff_to < rule.effective_from):
            return jsonify(error="Bulan selesai tidak valid."), 400
        rule.effective_to = eff_to
    if "amountDpw" in data:
        rule.amount_dpw = max(0, int(data["amountDpw"] or 0))
    if "amountDpp" in data:
        rule.amount_dpp = max(0, int(data["amountDpp"] or 0))
    if "notes" in data:
        rule.notes = str(data["notes"] or "").strip() or None

    log_finance_audit("UPDATE", f"Tarif {rule.daerah}", f"Ubah tarif {rule.effective_from}", actor)
    db.session.commit()
    invalidate_all_data_cache()
    return jsonify(tariff_to_dict(rule))


@bp.delete("/finance/tariffs/<rule_id>")
@finance_access_required
def delete_tariff(rule_id: str):
    rule = db.session.get(FinanceTariffRule, rule_id)
    if not rule:
        return jsonify(error="Aturan tarif tidak ditemukan."), 404
    db.session.delete(rule)
    db.session.commit()
    invalidate_all_data_cache()
    return jsonify(ok=True)
