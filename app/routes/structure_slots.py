"""Endpoint untuk manajemen Tabel Kursi Jabatan Teritorial (`structure_slots`).

Menyimpan seluruh kursi formasi jabatan (DPW, DPD, DPC, DPRt) di database PostgreSQL,
termasuk kursi yang masih kosong/lowong (`member_nik = NULL`) maupun kursi tambahan (+ Baris),
serta mendukung 1 kader (`member_nik`) memegang lebih dari 1 jabatan (rangkap jabatan).
"""
import re
from flask import Blueprint, jsonify, request
from sqlalchemy.orm import selectinload

from ..auth import current_claims, login_required
from ..cache import invalidate_all_data_cache
from ..extensions import db
from ..models import Member, Position, StructureSlot
from ..services.structure_templates import get_structure_template, make_slot_id
from ..utils import in_scope, make_position_id, refresh_member_summary

bp = Blueprint("structure_slots", __name__)

_TABLE_VERIFIED = False


def ensure_structure_slots_table_exists():
    """Memastikan tabel `positions` dan `structure_slots` beserta index-nya sudah ada di PostgreSQL
    menggunakan klausa `IF NOT EXISTS` agar tidak gagal/rollback ketika index tabel lain sudah ada.
    """
    global _TABLE_VERIFIED
    if _TABLE_VERIFIED:
        return
    try:
        db.session.execute(
            db.text(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    id VARCHAR(40) PRIMARY KEY,
                    member_nik VARCHAR(50) NOT NULL REFERENCES members(nik) ON DELETE CASCADE,
                    tingkat_kepengurusan VARCHAR(30) NOT NULL,
                    dpd VARCHAR(100) NOT NULL,
                    dpc VARCHAR(100) NOT NULL,
                    dprt VARCHAR(100) NOT NULL,
                    wilayah_penugasan VARCHAR(100),
                    jabatan VARCHAR(150),
                    nomor_sk VARCHAR(100),
                    tanggal_sk DATE,
                    urutan INTEGER,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_positions_member_nik ON positions (member_nik);
                CREATE INDEX IF NOT EXISTS idx_positions_wilayah ON positions (dpd, dpc, dprt, tingkat_kepengurusan);

                CREATE TABLE IF NOT EXISTS structure_slots (
                    id VARCHAR(100) PRIMARY KEY,
                    tingkat_kepengurusan VARCHAR(30) NOT NULL,
                    dpd VARCHAR(100) NOT NULL,
                    dpc VARCHAR(100) NOT NULL DEFAULT '-',
                    dprt VARCHAR(100) NOT NULL DEFAULT '-',
                    wilayah_penugasan VARCHAR(100) NOT NULL,
                    urutan INTEGER NOT NULL DEFAULT 1,
                    jabatan VARCHAR(150) NOT NULL,
                    is_baku BOOLEAN NOT NULL DEFAULT TRUE,
                    member_nik VARCHAR(50),
                    nomor_sk VARCHAR(100),
                    tanggal_sk DATE,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                ALTER TABLE structure_slots DROP CONSTRAINT IF EXISTS structure_slots_member_nik_fkey;
                CREATE INDEX IF NOT EXISTS idx_structure_slots_territory ON structure_slots (tingkat_kepengurusan, dpd, dpc, dprt);
                CREATE INDEX IF NOT EXISTS idx_structure_slots_member_nik ON structure_slots (member_nik);
                """
            )
        )
        db.session.commit()
        _TABLE_VERIFIED = True
    except Exception:
        db.session.rollback()
        raise



def _normalize_territory_params(tingkat: str, dpd: str, dpc: str, dprt: str):
    t = (tingkat or "DPD").strip()
    if t.upper() == "DPRT":
        t = "DPRt"
    else:
        t = t.upper()

    clean_dpd = (dpd or "Sulawesi Utara").strip()
    if t == "DPW":
        clean_dpd = "Sulawesi Utara"
        clean_dpc = "Wilayah DPW"
        clean_dprt = "Pusat/Umum"
    elif t == "DPD":
        clean_dpc = "Umum (DPD)"
        clean_dprt = "Pusat/Umum"
    elif t == "DPC":
        clean_dpc = (dpc or "Umum (DPD)").strip()
        clean_dprt = "Pusat/Umum"
    else:
        clean_dpc = (dpc or "Umum (DPD)").strip()
        clean_dprt = (dprt or "Pusat").strip()

    return t, clean_dpd, clean_dpc, clean_dprt


def _clean_job_key(jabatan: str) -> str:
    s = (jabatan or "").lower().strip()
    s = s.replace("bidang", "bid").replace("&", "dan")
    s = re.sub(r"\(.*?\)", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def _suborg_category(sub_org: str, jabatan: str = "") -> str:
    combined = f"{sub_org or ''} {jabatan or ''}".lower()
    if "pertimbangan" in combined or "wantim" in combined:
        return "PERTIMBANGAN"
    if "pakar" in combined or "wankar" in combined:
        return "PAKAR"
    return "PIMPINAN"


def _suborg_sort_order(sub_org: str) -> int:
    cat = _suborg_category(sub_org)
    if cat == "PERTIMBANGAN":
        return 1
    if cat == "PAKAR":
        return 2
    return 3


def _clean_region_key(name: str) -> str:
    s = (name or "").lower().strip()
    for prefix in (
        "kabupaten ",
        "kab. ",
        "kab ",
        "kota ",
        "dpd ",
        "kecamatan ",
        "kec. ",
        "kec ",
        "dpc ",
        "desa ",
        "kelurahan ",
        "kel. ",
        "kel ",
        "dprt ",
    ):
        if s.startswith(prefix):
            s = s[len(prefix) :].strip()
    return re.sub(r"[^a-z0-9]", "", s)


def _extract_core_sql_token(val: str) -> str:
    s = (val or "").strip().lower()
    for prefix in (
        "kabupaten ",
        "kab. ",
        "kab ",
        "kota ",
        "kotamadya ",
        "dpd ",
        "kecamatan ",
        "kec. ",
        "kec ",
        "dpc ",
        "desa ",
        "kelurahan ",
        "kel. ",
        "kel ",
        "dprt ",
    ):
        if s.startswith(prefix):
            s = s[len(prefix) :].strip()
    return s


def _query_territory_positions(tingkat: str, dpd: str, dpc: str, dprt: str):
    q = Position.query.filter(Position.tingkat_kepengurusan == tingkat)
    if tingkat == "DPW":
        return q.order_by(Position.urutan.asc().nullslast()).all()

    core_dpd_sql = _extract_core_sql_token(dpd)
    if core_dpd_sql:
        q = q.filter(Position.dpd.ilike(f"%{core_dpd_sql}%"))

    if tingkat in ("DPC", "DPRt"):
        core_dpc_sql = _extract_core_sql_token(dpc)
        if core_dpc_sql:
            q = q.filter(Position.dpc.ilike(f"%{core_dpc_sql}%"))

    candidate_pos = q.order_by(Position.urutan.asc().nullslast()).all()

    target_dpd = _clean_region_key(dpd)
    target_dpc = _clean_region_key(dpc)
    target_dprt = _clean_region_key(dprt)

    matched = []
    for p in candidate_pos:
        if target_dpd and _clean_region_key(p.dpd) != target_dpd:
            continue
        if tingkat in ("DPC", "DPRt") and target_dpc and _clean_region_key(p.dpc) != target_dpc:
            continue
        if tingkat == "DPRt" and target_dprt and _clean_region_key(p.dprt) != target_dprt:
            continue
        matched.append(p)
    return matched


def ensure_territory_slots_initialized(tingkat: str, dpd: str, dpc: str, dprt: str):
    """Memastikan seluruh kursi jabatan untuk wilayah ini sudah tercatat di tabel `structure_slots` DB.

    Jika wilayah ini belum memiliki baris di `structure_slots`, fungsi ini otomatis membuat seluruh
    kursi baku (`member_nik = NULL` jika lowong) dan memasangkan pengurus yang sudah ada di `positions`.
    """
    ensure_structure_slots_table_exists()
    t, c_dpd, c_dpc, c_dprt = _normalize_territory_params(tingkat, dpd, dpc, dprt)

    existing_slots = (
        StructureSlot.query.filter(
            StructureSlot.tingkat_kepengurusan == t,
            db.func.lower(StructureSlot.dpd) == c_dpd.lower(),
            db.func.lower(StructureSlot.dpc) == c_dpc.lower(),
            db.func.lower(StructureSlot.dprt) == c_dprt.lower(),
        )
        .order_by(StructureSlot.urutan.asc())
        .all()
    )

    existing_positions = _query_territory_positions(t, c_dpd, c_dpc, c_dprt)

    if not existing_slots:
        template = get_structure_template(t)
        created_slots = []
        for tpl in template:
            sub_org = tpl["subOrg"]
            no = tpl["no"]
            jab = tpl["jabatan"]
            slot_id = make_slot_id(t, c_dpd, c_dpc, c_dprt, sub_org, no, jab)
            existing_obj = db.session.get(StructureSlot, slot_id)
            if existing_obj:
                created_slots.append(existing_obj)
            else:
                slot_obj = StructureSlot(
                    id=slot_id,
                    tingkat_kepengurusan=t,
                    dpd=c_dpd,
                    dpc=c_dpc,
                    dprt=c_dprt,
                    wilayah_penugasan=sub_org,
                    urutan=no,
                    jabatan=jab,
                    is_baku=True,
                    member_nik=None,
                )
                db.session.add(slot_obj)
                created_slots.append(slot_obj)

        # Pasangkan pengurus yang sudah ada di tabel `positions` ke dalam `created_slots`
        used_slot_ids = set()
        unmatched_positions = []

        for pos in existing_positions:
            pos_cat = _suborg_category(pos.wilayah_penugasan, pos.jabatan)
            pos_jab_key = _clean_job_key(pos.jabatan)
            matched_slot = None

            # 1. Cari kecocokan nama jabatan pada dewan yang sama
            for s in created_slots:
                if s.id in used_slot_ids:
                    continue
                if _suborg_category(s.wilayah_penugasan) != pos_cat:
                    continue
                s_jab_key = _clean_job_key(s.jabatan)
                if s_jab_key == pos_jab_key or (pos_jab_key and (pos_jab_key in s_jab_key or s_jab_key in pos_jab_key)):
                    matched_slot = s
                    break

            # 2. Fallback nomor urut pada dewan yang sama jika jabatan belum terisi
            if not matched_slot and pos.urutan:
                for s in created_slots:
                    if s.id in used_slot_ids:
                        continue
                    if _suborg_category(s.wilayah_penugasan) == pos_cat and s.urutan == pos.urutan:
                        matched_slot = s
                        break

            if matched_slot:
                matched_slot.member_nik = pos.member_nik
                if not matched_slot.is_baku and pos.jabatan:
                    matched_slot.jabatan = pos.jabatan
                matched_slot.nomor_sk = pos.nomor_sk
                matched_slot.tanggal_sk = pos.tanggal_sk
                used_slot_ids.add(matched_slot.id)
            else:
                unmatched_positions.append(pos)

        # 3. Kursi tambahan (di luar kuota standar) tetap disimpan sebagai baris kursi resmi di DB
        for extra_pos in unmatched_positions:
            sub_org = extra_pos.wilayah_penugasan or (
                "DEWAN PIMPINAN WILAYAH"
                if t == "DPW"
                else "III. DEWAN PIMPINAN DAERAH"
                if t == "DPD"
                else "III. DEWAN PIMPINAN CABANG"
                if t == "DPC"
                else "DEWAN PIMPINAN RANTING"
            )
            same_sub_count = sum(1 for s in created_slots if s.wilayah_penugasan == sub_org)
            next_no = same_sub_count + 1
            extra_jab = extra_pos.jabatan or "Pengurus"
            extra_id = make_slot_id(
                t, c_dpd, c_dpc, c_dprt, sub_org, next_no, f"{extra_jab}_{extra_pos.member_nik}"
            )
            existing_extra = db.session.get(StructureSlot, extra_id)
            if existing_extra:
                existing_extra.member_nik = extra_pos.member_nik
                existing_extra.nomor_sk = extra_pos.nomor_sk
                existing_extra.tanggal_sk = extra_pos.tanggal_sk
                created_slots.append(existing_extra)
            else:
                extra_slot = StructureSlot(
                    id=extra_id,
                    tingkat_kepengurusan=t,
                    dpd=c_dpd,
                    dpc=c_dpc,
                    dprt=c_dprt,
                    wilayah_penugasan=sub_org,
                    urutan=next_no,
                    jabatan=extra_jab,
                    is_baku=False,
                    member_nik=extra_pos.member_nik,
                    nomor_sk=extra_pos.nomor_sk,
                    tanggal_sk=extra_pos.tanggal_sk,
                )
                db.session.add(extra_slot)
                created_slots.append(extra_slot)

        db.session.commit()
        existing_slots = created_slots
    else:
        # 1. Bersihkan slot yang pengurusnya sudah dihapus atau digantikan di tabel `positions`
        template = get_structure_template(t)
        template_jab_map = {
            (_suborg_category(tpl["subOrg"]), tpl["no"]): tpl["jabatan"]
            for tpl in template
        }
        active_pos_keys = {
            (
                (p.member_nik or "").strip(),
                _suborg_category(p.wilayah_penugasan, p.jabatan),
                _clean_job_key(p.jabatan),
            )
            for p in existing_positions
            if p.member_nik
        }
        active_nik_cats = {
            (
                (p.member_nik or "").strip(),
                _suborg_category(p.wilayah_penugasan, p.jabatan),
            )
            for p in existing_positions
            if p.member_nik
        }

        changed = False
        retained_slots = []
        for s in existing_slots:
            if s.member_nik:
                s_nik = s.member_nik.strip()
                s_cat = _suborg_category(s.wilayah_penugasan)
                s_key = (s_nik, s_cat, _clean_job_key(s.jabatan))
                if s_key not in active_pos_keys and (s_nik, s_cat) not in active_nik_cats:
                    if s.is_baku:
                        s.member_nik = None
                        s.nomor_sk = None
                        s.tanggal_sk = None
                        s.jabatan = template_jab_map.get((s_cat, s.urutan), s.jabatan)
                        retained_slots.append(s)
                    else:
                        db.session.delete(s)
                    changed = True
                    continue
            retained_slots.append(s)
        existing_slots = retained_slots

        # 2. Sinkronkan jika ada posisi baru hasil import yang belum masuk ke slot wilayah ini
        assigned_pairs = {
            (
                (s.member_nik or "").strip(),
                _suborg_category(s.wilayah_penugasan),
                _clean_job_key(s.jabatan),
            )
            for s in existing_slots
            if s.member_nik
        }
        for pos in existing_positions:
            pair_key = (
                (pos.member_nik or "").strip(),
                _suborg_category(pos.wilayah_penugasan, pos.jabatan),
                _clean_job_key(pos.jabatan),
            )
            if pair_key in assigned_pairs:
                continue

            pos_cat = _suborg_category(pos.wilayah_penugasan, pos.jabatan)
            pos_jab_key = _clean_job_key(pos.jabatan)
            vacant_match = None
            for s in existing_slots:
                if s.member_nik:
                    continue
                if _suborg_category(s.wilayah_penugasan) != pos_cat:
                    continue
                s_jab_key = _clean_job_key(s.jabatan)
                if s_jab_key == pos_jab_key or (pos_jab_key and (pos_jab_key in s_jab_key or s_jab_key in pos_jab_key)):
                    vacant_match = s
                    break

            if vacant_match:
                vacant_match.member_nik = pos.member_nik
                if not vacant_match.is_baku and pos.jabatan:
                    vacant_match.jabatan = pos.jabatan
                vacant_match.nomor_sk = pos.nomor_sk
                vacant_match.tanggal_sk = pos.tanggal_sk
                assigned_pairs.add(pair_key)
                changed = True
            else:
                sub_org = pos.wilayah_penugasan or (
                    "DEWAN PIMPINAN WILAYAH"
                    if t == "DPW"
                    else "III. DEWAN PIMPINAN DAERAH"
                    if t == "DPD"
                    else "III. DEWAN PIMPINAN CABANG"
                    if t == "DPC"
                    else "DEWAN PIMPINAN RANTING"
                )
                same_sub_count = sum(1 for s in existing_slots if s.wilayah_penugasan == sub_org)
                next_no = same_sub_count + 1
                extra_jab = pos.jabatan or "Pengurus"
                extra_id = make_slot_id(
                    t, c_dpd, c_dpc, c_dprt, sub_org, next_no, f"{extra_jab}_{pos.member_nik}"
                )
                if not db.session.get(StructureSlot, extra_id):
                    extra_slot = StructureSlot(
                        id=extra_id,
                        tingkat_kepengurusan=t,
                        dpd=c_dpd,
                        dpc=c_dpc,
                        dprt=c_dprt,
                        wilayah_penugasan=sub_org,
                        urutan=next_no,
                        jabatan=extra_jab,
                        is_baku=False,
                        member_nik=pos.member_nik,
                        nomor_sk=pos.nomor_sk,
                        tanggal_sk=pos.tanggal_sk,
                    )
                    db.session.add(extra_slot)
                    existing_slots.append(extra_slot)
                    assigned_pairs.add(pair_key)
                    changed = True

        if changed:
            db.session.commit()
        else:
            existing_slots.sort(key=lambda s: (_suborg_sort_order(s.wilayah_penugasan), s.urutan or 999))
            return existing_slots

    # Ambil ulang baris segar dari DB apabila terjadi commit agar bebas dari ObjectDeletedError
    fresh_slots = (
        StructureSlot.query.filter(
            StructureSlot.tingkat_kepengurusan == t,
            db.func.lower(StructureSlot.dpd) == c_dpd.lower(),
            db.func.lower(StructureSlot.dpc) == c_dpc.lower(),
            db.func.lower(StructureSlot.dprt) == c_dprt.lower(),
        )
        .order_by(StructureSlot.urutan.asc())
        .all()
    )
    fresh_slots.sort(key=lambda s: (_suborg_sort_order(s.wilayah_penugasan), s.urutan or 999))
    return fresh_slots


def _load_lightweight_member_map(niks: list) -> dict:
    if not niks:
        return {}
    rows = (
        db.session.query(
            Member.nik,
            Member.nama_lengkap,
            Member.nomor_ekta,
            Member.email,
            Member.nomor_hp,
            Member.sosmed,
        )
        .filter(Member.nik.in_(niks))
        .all()
    )
    return {r.nik: r for r in rows}


def slot_to_grid_dict(slot: StructureSlot, member_map: dict) -> dict:
    m = member_map.get(slot.member_nik) if slot.member_nik else None
    has_member = bool(slot.member_nik and m)
    return {
        "id": slot.id,
        "no": slot.urutan,
        "subOrg": slot.wilayah_penugasan,
        "jabatan": slot.jabatan,
        "isBaku": slot.is_baku,
        "namaLengkap": m.nama_lengkap if has_member else "",
        "nik": m.nik if has_member else "",
        "nomorEKTA": (m.nomor_ekta or "") if has_member else "",
        "email": (m.email or "") if has_member else "",
        "nomorHP": (m.nomor_hp or "") if has_member else "",
        "sosmed": (m.sosmed or "") if has_member else "",
        "fotoProfilUrl": "",
        "isExisting": has_member,
        "originalNik": m.nik if has_member else "",
    }


@bp.get("/structure-slots")
@login_required
def get_territory_structure_slots():
    """Mengambil daftar lengkap kursi jabatan (terisi maupun kosong/lowong) dari database untuk wilayah tertentu."""
    tingkat = request.args.get("tingkat", "DPD")
    dpd = request.args.get("dpd", "Sulawesi Utara")
    dpc = request.args.get("dpc", "")
    dprt = request.args.get("dprt", "")

    slots = ensure_territory_slots_initialized(tingkat, dpd, dpc, dprt)
    niks = list({s.member_nik for s in slots if s.member_nik})
    member_map = _load_lightweight_member_map(niks)

    grid_rows = [slot_to_grid_dict(s, member_map) for s in slots]
    filled_count = sum(1 for r in grid_rows if r["namaLengkap"])
    return jsonify(
        slots=grid_rows,
        total=len(grid_rows),
        filled=filled_count,
        vacant=len(grid_rows) - filled_count,
    )


@bp.post("/structure-slots/sync")
@login_required
def sync_territory_structure_slots():
    """Menyimpan/memperbarui susunan kursi jabatan suatu wilayah di tabel `structure_slots`."""
    claims = current_claims()
    data = request.get_json(silent=True) or {}
    tingkat = data.get("tingkat", "DPD")
    dpd = data.get("dpd", "Sulawesi Utara")
    dpc = data.get("dpc", "")
    dprt = data.get("dprt", "")
    incoming_rows = data.get("rows")

    if not isinstance(incoming_rows, list):
        return jsonify(error="Field 'rows' wajib berupa array"), 400

    t, c_dpd, c_dpc, c_dprt = _normalize_territory_params(tingkat, dpd, dpc, dprt)
    if not in_scope(claims, c_dpd, c_dpc, c_dprt):
        return jsonify(error="Wilayah di luar cakupan akses akun Anda"), 403

    ensure_structure_slots_table_exists()

    # Bersihkan slot lama di wilayah ini lalu simpan formasi terbaru (termasuk yang kosong/lowong & tambahan).
    # Catatan: Tabel `members` dan `positions` dikelola oleh endpoint `/api/members/bulk` & `/api/members/bulk-delete`
    # sehingga tidak terjadi deadlock transaksi paralel.
    StructureSlot.query.filter(
        StructureSlot.tingkat_kepengurusan == t,
        db.func.lower(StructureSlot.dpd) == c_dpd.lower(),
        db.func.lower(StructureSlot.dpc) == c_dpc.lower(),
        db.func.lower(StructureSlot.dprt) == c_dprt.lower(),
    ).delete(synchronize_session=False)

    saved_slots = []
    seen_slot_ids = set()

    for idx, r in enumerate(incoming_rows):
        sub_org = (r.get("subOrg") or "III. DEWAN PIMPINAN DAERAH").strip()
        no = int(r.get("no") or (idx + 1))
        jabatan = (r.get("jabatan") or "Pengurus").strip()
        is_baku = bool(r.get("isBaku", True))
        nama = (r.get("namaLengkap") or "").strip()
        nik = (r.get("nik") or "").strip()

        member_nik = nik if (nama and nama != "-" and nik and nik != "-") else None

        slot_id = make_slot_id(
            t, c_dpd, c_dpc, c_dprt, sub_org, no, f"{jabatan}_{idx}" if not is_baku else jabatan
        )
        while slot_id in seen_slot_ids:
            slot_id = f"{slot_id}_{idx}"
        seen_slot_ids.add(slot_id)

        slot_obj = StructureSlot(
            id=slot_id,
            tingkat_kepengurusan=t,
            dpd=c_dpd,
            dpc=c_dpc,
            dprt=c_dprt,
            wilayah_penugasan=sub_org,
            urutan=no,
            jabatan=jabatan,
            is_baku=is_baku,
            member_nik=member_nik,
        )
        db.session.add(slot_obj)
        saved_slots.append(slot_obj)

    db.session.commit()
    invalidate_all_data_cache()

    niks = list({s.member_nik for s in saved_slots if s.member_nik})
    member_map = _load_lightweight_member_map(niks)

    grid_rows = [slot_to_grid_dict(s, member_map) for s in saved_slots]
    return jsonify(slots=grid_rows, total=len(grid_rows), success=True)


@bp.post("/structure-slots/reset")
@login_required
def reset_territory_structure_slots():
    """Memulihkan kursi jabatan wilayah kembali ke formasi baku standar di database."""
    claims = current_claims()
    data = request.get_json(silent=True) or {}
    tingkat = data.get("tingkat", "DPD")
    dpd = data.get("dpd", "Sulawesi Utara")
    dpc = data.get("dpc", "")
    dprt = data.get("dprt", "")

    t, c_dpd, c_dpc, c_dprt = _normalize_territory_params(tingkat, dpd, dpc, dprt)
    if not in_scope(claims, c_dpd, c_dpc, c_dprt):
        return jsonify(error="Wilayah di luar cakupan akses akun Anda"), 403

    ensure_structure_slots_table_exists()

    StructureSlot.query.filter(
        StructureSlot.tingkat_kepengurusan == t,
        db.func.lower(StructureSlot.dpd) == c_dpd.lower(),
        db.func.lower(StructureSlot.dpc) == c_dpc.lower(),
        db.func.lower(StructureSlot.dprt) == c_dprt.lower(),
    ).delete(synchronize_session=False)
    db.session.commit()

    slots = ensure_territory_slots_initialized(t, c_dpd, c_dpc, c_dprt)
    niks = list({s.member_nik for s in slots if s.member_nik})
    member_map = _load_lightweight_member_map(niks)

    grid_rows = [slot_to_grid_dict(s, member_map) for s in slots]
    return jsonify(slots=grid_rows, total=len(grid_rows), success=True)
