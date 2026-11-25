"""Agregasi dashboard dihitung di database (GROUP BY), bukan di browser."""
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, jsonify, request
from sqlalchemy import and_, case, func, or_

from app.auth import current_claims, login_required, super_admin_required
from app.cache import get_or_compute
from app.extensions import db
from app.models import (
    AppUser,
    AuditLog,
    DeletionRequest,
    InventoryItem,
    Member,
    Position,
    StructureSlot,
    SupportMessage,
)
from app.utils import apply_scope, sanitize_date

bp = Blueprint("stats", __name__)


def _scoped(*cols):
    return apply_scope(db.session.query(*cols), current_claims())


@bp.get("/stats/summary")
@login_required
def summary():
    claims = current_claims()

    def _compute():
        by_status = dict(
            apply_scope(db.session.query(Member.status, func.count()), claims).group_by(Member.status).all()
        )
        total = sum(by_status.values())
        aktif = by_status.get("Aktif", 0)

        per_dpd = (
            apply_scope(
                db.session.query(
                    Member.dpd,
                    func.count().label("total"),
                    func.sum(case((Member.status == "Aktif", 1), else_=0)).label("aktif"),
                ),
                claims,
            )
            .group_by(Member.dpd)
            .order_by(Member.dpd)
            .all()
        )
        pengurus_cnt = apply_scope(db.session.query(func.count()), claims).filter(Member.is_pengurus.is_(True)).scalar()
        return {
            "total": total,
            "aktif": aktif,
            "nonAktif": total - aktif,
            "activeRate": round(aktif * 100 / total, 2) if total else 0,
            "byStatus": by_status,
            "pengurus": pengurus_cnt,
            "perDpd": [{"dpd": d, "total": t, "aktif": int(a or 0), "nonAktif": t - int(a or 0)} for d, t, a in per_dpd],
        }

    return jsonify(get_or_compute("stats", claims, "summary", ttl_seconds=10.0, compute_fn=_compute))


@bp.get("/stats/recruitment")
@login_required
def recruitment():
    """Laju rekrutmen: kartu hari ini/7/30 hari, tren harian, dan top kader perekrut."""
    claims = current_claims()
    today = date.today()
    days = request.args.get("days", 30, type=int)
    start = sanitize_date(request.args.get("from")) or (today - timedelta(days=days - 1))
    end = sanitize_date(request.args.get("to")) or today
    subkey = f"rec:{start.isoformat()}:{end.isoformat()}"

    def _compute():
        def count_since(d):
            return apply_scope(db.session.query(func.count()), claims).filter(Member.tanggal_bergabung >= d).scalar()

        daily = (
            apply_scope(db.session.query(Member.tanggal_bergabung, func.count()), claims)
            .filter(Member.tanggal_bergabung.between(start, end))
            .group_by(Member.tanggal_bergabung)
            .order_by(Member.tanggal_bergabung)
            .all()
        )
        top = (
            apply_scope(
                db.session.query(Member.kader_perekrut_nama, Member.kader_perekrut_jabatan, func.count().label("n")),
                claims,
            )
            .filter(Member.kader_perekrut_nama.isnot(None), Member.tanggal_bergabung.between(start, end))
            .group_by(Member.kader_perekrut_nama, Member.kader_perekrut_jabatan)
            .order_by(func.count().desc())
            .limit(20)
            .all()
        )
        return {
            "today": count_since(today),
            "last7": count_since(today - timedelta(days=6)),
            "last30": count_since(today - timedelta(days=29)),
            "daily": [{"date": d.isoformat(), "count": c} for d, c in daily],
            "topRecruiters": [{"nama": n, "jabatan": j, "count": c} for n, j, c in top],
        }

    return jsonify(get_or_compute("stats", claims, subkey, ttl_seconds=10.0, compute_fn=_compute))


@bp.get("/stats/territory")
@login_required
def territory():
    """Drilldown teritorial: tanpa param -> per DPD; ?dpd= -> per DPC; ?dpd=&dpc= -> per DPRt."""
    claims = current_claims()
    dpd, dpc = request.args.get("dpd"), request.args.get("dpc")
    start, end = sanitize_date(request.args.get("from")), sanitize_date(request.args.get("to"))
    subkey = f"terr:{dpd or ''}:{dpc or ''}:{start or ''}:{end or ''}"

    def _compute():
        if dpd and dpc:
            level, col = "dprt", Member.dprt
        elif dpd:
            level, col = "dpc", Member.dpc
        else:
            level, col = "dpd", Member.dpd

        q = apply_scope(
            db.session.query(col, func.count(), func.sum(case((Member.status == "Aktif", 1), else_=0))),
            claims,
        )
        if dpd:
            q = q.filter(Member.dpd == dpd)
        if dpc:
            q = q.filter(Member.dpc == dpc)
        if start:
            q = q.filter(Member.tanggal_bergabung >= start)
        if end:
            q = q.filter(Member.tanggal_bergabung <= end)
        rows = q.group_by(col).order_by(func.count().desc()).all()
        return {
            "level": level,
            "items": [{"name": n, "total": t, "aktif": int(a or 0), "nonAktif": t - int(a or 0)} for n, t, a in rows],
        }

    return jsonify(get_or_compute("stats", claims, subkey, ttl_seconds=10.0, compute_fn=_compute))


@bp.get("/stats/reconciliation")
@login_required
def reconciliation():
    """Rekonsiliasi NIK & e-KTA: NIK tidak valid, duplikat e-KTA, dan nomor non-standar."""
    claims = current_claims()

    def _compute():
        invalid_nik = apply_scope(db.session.query(func.count()), claims).filter(~Member.nik.op("~")(r"^\d{16}$")).scalar()
        non_standard = (
            apply_scope(db.session.query(func.count()), claims)
            .filter(
                (Member.nomor_ekta.is_(None))
                | ~(Member.nomor_ekta.like("ND-%") | Member.nomor_ekta.like("KT-%"))
            )
            .scalar()
        )
        dup = (
            apply_scope(db.session.query(Member.nomor_ekta, func.count().label("n")), claims)
            .filter(Member.nomor_ekta.isnot(None))
            .group_by(Member.nomor_ekta)
            .having(func.count() > 1)
            .limit(100)
            .all()
        )
        return {
            "invalidNik": invalid_nik,
            "nonStandardEkta": non_standard,
            "duplicateEkta": [{"nomorEkta": e, "count": n} for e, n in dup],
        }

    return jsonify(get_or_compute("stats", claims, "reconciliation", ttl_seconds=10.0, compute_fn=_compute))


def _role_category_key(role: str | None) -> str:
    r = (role or "").strip().upper()
    if r == "SUPER_ADMIN":
        return "superAdmin"
    if r.startswith("KETUA_"):
        return "ketua"
    if r in ("ADMIN_DPW", "ADMIN_DPD", "ADMIN_DPC", "ADMIN_DPRT"):
        return "adminDelegasi"
    if r.startswith("PENGURUS_"):
        return "pengurus"
    return "anggotaKhusus"


def _normalize_structure_level(raw_level: str | None) -> str:
    lvl = (raw_level or "").strip().upper()
    if lvl in ("DPRT", "RANTING"):
        return "DPRt"
    if lvl in ("DPW", "DPD", "DPC"):
        return lvl
    return ""


@bp.get("/stats/admin-panel")
@super_admin_required
def admin_panel_telemetry():
    """Telemetri angka real-time seluruh database non-keuangan khusus SUPER_ADMIN."""
    claims = current_claims()
    fresh = request.args.get("fresh") in ("1", "true", "yes")

    def _compute():
        now_utc = datetime.now(timezone.utc)
        now_local = datetime.now().astimezone()
        today_local_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        today_date = now_local.date()
        online_window_minutes = 3
        online_cutoff = now_utc - timedelta(minutes=online_window_minutes)

        # 1. Sesi & Pengguna Aktif (app_users)
        accounts_by_category = {
            "superAdmin": 0,
            "ketua": 0,
            "adminDelegasi": 0,
            "pengurus": 0,
            "anggotaKhusus": 0,
        }
        online_by_category = dict(accounts_by_category)
        total_accounts = 0
        active_accounts = 0
        online_now = 0
        active_today = 0

        user_rows = (
            db.session.query(
                AppUser.role,
                func.count().label("total"),
                func.sum(case((AppUser.is_active.is_(True), 1), else_=0)).label("active"),
                func.sum(
                    case(
                        (
                            and_(
                                AppUser.is_active.is_(True),
                                AppUser.last_seen_at.isnot(None),
                                AppUser.last_seen_at >= online_cutoff,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("online"),
                func.sum(
                    case(
                        (
                            and_(
                                AppUser.last_seen_at.isnot(None),
                                AppUser.last_seen_at >= today_local_start,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("today"),
            )
            .group_by(AppUser.role)
            .all()
        )
        for role_val, cnt_total, cnt_active, cnt_online, cnt_today in user_rows:
            t = int(cnt_total or 0)
            a = int(cnt_active or 0)
            o = int(cnt_online or 0)
            td = int(cnt_today or 0)
            total_accounts += t
            active_accounts += a
            online_now += o
            active_today += td
            cat = _role_category_key(role_val)
            accounts_by_category[cat] += t
            online_by_category[cat] += o

        # 2. Data Kader & Anggota Biasa (members)
        m_agg = (
            db.session.query(
                func.count().label("total"),
                func.sum(case((Member.is_pengurus.is_(True), 1), else_=0)).label("pengurus"),
                func.sum(case((Member.status == "Aktif", 1), else_=0)).label("aktif"),
                func.sum(case((Member.status.ilike("%Meninggal%"), 1), else_=0)).label("meninggal"),
                func.sum(
                    case(
                        (
                            or_(
                                Member.status.ilike("%Pindah Partai%"),
                                Member.status.ilike("%Mundur%"),
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("mundur_pindah_partai"),
                func.sum(case((Member.status.ilike("%Pindah Domisili%"), 1), else_=0)).label("pindah_domisili"),
                func.sum(
                    case(
                        (
                            and_(
                                Member.nomor_ekta.isnot(None),
                                func.trim(Member.nomor_ekta) != "",
                                func.trim(Member.nomor_ekta) != "-",
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("with_ekta"),
                func.sum(
                    case(
                        (
                            and_(
                                Member.foto_ktp_url.isnot(None),
                                func.trim(Member.foto_ktp_url) != "",
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("with_ktp_photo"),
                func.sum(
                    case(
                        (
                            and_(
                                Member.foto_profil_url.isnot(None),
                                func.trim(Member.foto_profil_url) != "",
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("with_profile_photo"),
                func.sum(case((Member.tanggal_bergabung >= today_date, 1), else_=0)).label("joined_today"),
                func.sum(
                    case((Member.tanggal_bergabung >= today_date - timedelta(days=6), 1), else_=0)
                ).label("joined_7d"),
                func.sum(
                    case((Member.tanggal_bergabung >= today_date - timedelta(days=29), 1), else_=0)
                ).label("joined_30d"),
                func.sum(
                    case(
                        (
                            or_(
                                and_(
                                    Member.kader_perekrut_nik.isnot(None),
                                    func.trim(Member.kader_perekrut_nik) != "",
                                ),
                                and_(
                                    Member.kader_perekrut_kode_referral.isnot(None),
                                    func.trim(Member.kader_perekrut_kode_referral) != "",
                                ),
                                and_(
                                    Member.kader_perekrut_nama.isnot(None),
                                    func.trim(Member.kader_perekrut_nama) != "",
                                ),
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("via_referral"),
            )
            .first()
        )
        m_total = int((m_agg.total if m_agg else 0) or 0)
        m_pengurus = int((m_agg.pengurus if m_agg else 0) or 0)
        m_anggota = max(0, m_total - m_pengurus)
        m_aktif = int((m_agg.aktif if m_agg else 0) or 0)
        m_non_aktif = max(0, m_total - m_aktif)
        m_with_ekta = int((m_agg.with_ekta if m_agg else 0) or 0)

        # 3. Cakupan Wilayah, Jabatan Struktural & Slot Struktur
        dpd_count = int(
            db.session.query(func.count(func.distinct(Member.dpd)))
            .filter(
                Member.dpd.isnot(None),
                ~func.trim(Member.dpd).in_(["", "-", "ALL", "Sulawesi Utara"]),
            )
            .scalar()
            or 0
        )
        dpc_count = int(
            db.session.query(func.count(func.distinct(Member.dpd + "::" + Member.dpc)))
            .filter(
                Member.dpd.isnot(None),
                Member.dpc.isnot(None),
                ~func.trim(Member.dpc).in_(["", "-", "ALL", "Umum (DPD)"]),
            )
            .scalar()
            or 0
        )
        dprt_count = int(
            db.session.query(func.count(func.distinct(Member.dpd + "::" + Member.dpc + "::" + Member.dprt)))
            .filter(
                Member.dpd.isnot(None),
                Member.dpc.isnot(None),
                Member.dprt.isnot(None),
                ~func.trim(Member.dprt).in_(["", "-", "ALL", "Pusat/Umum"]),
            )
            .scalar()
            or 0
        )

        per_dpd_rows = (
            db.session.query(
                Member.dpd,
                func.count().label("total"),
                func.sum(case((Member.is_pengurus.is_(True), 1), else_=0)).label("pengurus"),
                func.sum(case((Member.status == "Aktif", 1), else_=0)).label("aktif"),
            )
            .group_by(Member.dpd)
            .order_by(func.count().desc(), Member.dpd.asc())
            .all()
        )
        per_dpd = []
        for dpd_name, t_cnt, p_cnt, a_cnt in per_dpd_rows:
            t_val = int(t_cnt or 0)
            p_val = int(p_cnt or 0)
            per_dpd.append(
                {
                    "dpd": dpd_name or "-",
                    "total": t_val,
                    "pengurus": p_val,
                    "anggotaBiasa": max(0, t_val - p_val),
                    "aktif": int(a_cnt or 0),
                }
            )

        positions_by_level = {"DPW": 0, "DPD": 0, "DPC": 0, "DPRt": 0}
        positions_total = 0
        for raw_lvl, cnt in (
            db.session.query(Position.tingkat_kepengurusan, func.count())
            .group_by(Position.tingkat_kepengurusan)
            .all()
        ):
            c_val = int(cnt or 0)
            positions_total += c_val
            norm_lvl = _normalize_structure_level(raw_lvl)
            if norm_lvl in positions_by_level:
                positions_by_level[norm_lvl] += c_val

        slots_by_level = {
            lvl: {"total": 0, "filled": 0, "vacant": 0}
            for lvl in ("DPW", "DPD", "DPC", "DPRt")
        }
        slots_total = 0
        slots_filled = 0
        for raw_lvl, cnt_t, cnt_f in (
            db.session.query(
                StructureSlot.tingkat_kepengurusan,
                func.count().label("total"),
                func.sum(
                    case(
                        (
                            and_(
                                StructureSlot.member_nik.isnot(None),
                                func.trim(StructureSlot.member_nik) != "",
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("filled"),
            )
            .group_by(StructureSlot.tingkat_kepengurusan)
            .all()
        ):
            t_val = int(cnt_t or 0)
            f_val = int(cnt_f or 0)
            slots_total += t_val
            slots_filled += f_val
            norm_lvl = _normalize_structure_level(raw_lvl)
            if norm_lvl in slots_by_level:
                slots_by_level[norm_lvl]["total"] += t_val
                slots_by_level[norm_lvl]["filled"] += f_val
                slots_by_level[norm_lvl]["vacant"] = max(
                    0, slots_by_level[norm_lvl]["total"] - slots_by_level[norm_lvl]["filled"]
                )
        slots_vacant = max(0, slots_total - slots_filled)
        slots_fill_rate = round((slots_filled * 100.0) / slots_total, 1) if slots_total else 0.0

        # 4. Pencatatan Audit, Pengajuan Hapus, Support Chat & Inventaris (tanpa tabel keuangan)
        audit_agg = (
            db.session.query(
                func.count().label("total"),
                func.sum(case((AuditLog.timestamp >= today_local_start, 1), else_=0)).label("today"),
                func.sum(
                    case((AuditLog.timestamp >= today_local_start - timedelta(days=6), 1), else_=0)
                ).label("last7"),
            )
            .first()
        )
        audit_total = int((audit_agg.total if audit_agg else 0) or 0)
        audit_today = int((audit_agg.today if audit_agg else 0) or 0)
        audit_last7 = int((audit_agg.last7 if audit_agg else 0) or 0)
        audit_by_action = {
            str(act or "UNKNOWN"): int(cnt or 0)
            for act, cnt in db.session.query(AuditLog.action, func.count()).group_by(AuditLog.action).all()
        }

        del_counts = {"PENDING": 0, "APPROVED": 0, "REJECTED": 0}
        del_total = 0
        for st_val, cnt in (
            db.session.query(DeletionRequest.status, func.count()).group_by(DeletionRequest.status).all()
        ):
            c_val = int(cnt or 0)
            del_total += c_val
            st_key = str(st_val or "").strip().upper()
            if st_key in del_counts:
                del_counts[st_key] += c_val

        chat_agg = (
            db.session.query(
                func.count().label("total"),
                func.count(func.distinct(SupportMessage.thread_id)).label("threads"),
                func.sum(case((SupportMessage.is_read.is_(False), 1), else_=0)).label("unread"),
            )
            .first()
        )
        chat_total = int((chat_agg.total if chat_agg else 0) or 0)
        chat_threads = int((chat_agg.threads if chat_agg else 0) or 0)
        chat_unread = int((chat_agg.unread if chat_agg else 0) or 0)

        inv_by_floor = {str(floor): 0 for floor in range(1, 6)}
        inv_items = 0
        inv_units = 0
        inv_baik = 0
        inv_perbaikan = 0
        inv_rusak = 0
        for floor_val, c_items, s_units, s_baik, s_perb, s_rusak in (
            db.session.query(
                InventoryItem.lantai,
                func.count().label("items"),
                func.coalesce(func.sum(InventoryItem.jumlah), 0).label("units"),
                func.coalesce(func.sum(InventoryItem.jumlah_baik), 0).label("baik"),
                func.coalesce(func.sum(InventoryItem.jumlah_perbaikan), 0).label("perbaikan"),
                func.coalesce(func.sum(InventoryItem.jumlah_rusak), 0).label("rusak"),
            )
            .group_by(InventoryItem.lantai)
            .all()
        ):
            ci = int(c_items or 0)
            inv_items += ci
            inv_units += int(s_units or 0)
            inv_baik += int(s_baik or 0)
            inv_perbaikan += int(s_perb or 0)
            inv_rusak += int(s_rusak or 0)
            fl_key = str(int(floor_val or 0))
            if fl_key in inv_by_floor:
                inv_by_floor[fl_key] += ci

        tables_map = {
            "members": m_total,
            "positions": positions_total,
            "structure_slots": slots_total,
            "app_users": total_accounts,
            "audit_logs": audit_total,
            "deletion_requests": del_total,
            "support_messages": chat_total,
            "inventory_items": inv_items,
        }

        return {
            "serverTime": now_utc.isoformat(),
            "sessions": {
                "onlineWindowMinutes": online_window_minutes,
                "onlineNow": online_now,
                "activeToday": active_today,
                "totalAccounts": total_accounts,
                "activeAccounts": active_accounts,
                "inactiveAccounts": max(0, total_accounts - active_accounts),
                "onlineByCategory": online_by_category,
                "accountsByCategory": accounts_by_category,
            },
            "members": {
                "total": m_total,
                "pengurus": m_pengurus,
                "anggotaBiasa": m_anggota,
                "aktif": m_aktif,
                "nonAktif": m_non_aktif,
                "meninggal": int((m_agg.meninggal if m_agg else 0) or 0),
                "mundurPindahPartai": int((m_agg.mundur_pindah_partai if m_agg else 0) or 0),
                "pindahDomisili": int((m_agg.pindah_domisili if m_agg else 0) or 0),
                "withEkta": m_with_ekta,
                "withoutEkta": max(0, m_total - m_with_ekta),
                "withKtpPhoto": int((m_agg.with_ktp_photo if m_agg else 0) or 0),
                "withProfilePhoto": int((m_agg.with_profile_photo if m_agg else 0) or 0),
                "joinedToday": int((m_agg.joined_today if m_agg else 0) or 0),
                "joinedLast7Days": int((m_agg.joined_7d if m_agg else 0) or 0),
                "joinedLast30Days": int((m_agg.joined_30d if m_agg else 0) or 0),
                "viaReferral": int((m_agg.via_referral if m_agg else 0) or 0),
            },
            "territories": {
                "dpdCount": dpd_count,
                "dpcCount": dpc_count,
                "dprtCount": dprt_count,
                "positionsTotal": positions_total,
                "positionsByLevel": positions_by_level,
                "slots": {
                    "total": slots_total,
                    "filled": slots_filled,
                    "vacant": slots_vacant,
                    "fillRate": slots_fill_rate,
                    "byLevel": slots_by_level,
                },
                "perDpd": per_dpd,
            },
            "records": {
                "auditLogs": {
                    "total": audit_total,
                    "today": audit_today,
                    "last7Days": audit_last7,
                    "byAction": audit_by_action,
                },
                "deletionRequests": {
                    "total": del_total,
                    "pending": del_counts["PENDING"],
                    "approved": del_counts["APPROVED"],
                    "rejected": del_counts["REJECTED"],
                },
                "supportChat": {
                    "totalMessages": chat_total,
                    "totalThreads": chat_threads,
                    "unreadMessages": chat_unread,
                },
                "inventory": {
                    "totalItems": inv_items,
                    "totalUnits": inv_units,
                    "baik": inv_baik,
                    "perbaikan": inv_perbaikan,
                    "rusak": inv_rusak,
                    "byFloor": inv_by_floor,
                },
            },
            "database": {
                "tables": tables_map,
                "totalRecordsNonFinance": sum(tables_map.values()),
            },
        }

    if fresh:
        return jsonify(_compute())
    return jsonify(get_or_compute("stats", claims, "admin_panel", ttl_seconds=3.0, compute_fn=_compute))
