import re
import uuid

from flask import Blueprint, jsonify, request
from flask_jwt_extended import create_access_token, get_jwt_identity
from sqlalchemy import func
from werkzeug.security import check_password_hash, generate_password_hash

from config import get_config
from app.auth import (
    clear_user_presence,
    current_claims,
    invalidate_user_auth_state,
    login_required,
    touch_user_presence,
)
from app.cache import invalidate_all_data_cache
from app.rate_limit import rate_limit
from app.extensions import db
from app.models import AppUser, Member, Position
from app.services.referral_service import is_ketua_position
from app.utils import (
    DELEGATED_ADMIN_ROLES,
    DPC_ROLES,
    DPD_ROLES,
    DPRT_ROLES,
    DPW_ROLES,
    TERRITORIAL_ROLES,
    find_member_by_nik_or_incomplete_match,
    rename_member_nik_safely,
)

bp = Blueprint("auth", __name__)


def _is_member_ketua_at_level(member: Member | None, target_level: str) -> bool:
    if not member:
        return False
    norm_target = "DPRT" if target_level in ("DPRT", "RANTING") else target_level
    if member.positions:
        for p in member.positions:
            p_lvl = (p.tingkat_kepengurusan or "").strip().upper()
            p_norm = "DPRT" if p_lvl in ("DPRT", "RANTING") else p_lvl
            if p_norm == norm_target and is_ketua_position(p.jabatan or ""):
                return True
    return is_ketua_position(member.jabatan or "")


def determine_role(member_or_tingkat, member: Member = None) -> str:
    """Memetakan status kepengurusan ke level hak akses (Role) sistem 3-lapis.
    - Ketua resmi ber-SK mendapat role KETUA_DPW/DPD/DPC/DPRT.
    - Pengurus struktural ber-SK non-Ketua mendapat role PENGURUS_DPW/DPD/DPC/DPRT.
    - Anggota biasa tanpa SK resmi mendapatkan role 'ANGGOTA'."""
    m = member_or_tingkat if isinstance(member_or_tingkat, Member) else member
    tingkat = member_or_tingkat.tingkat_kepengurusan if isinstance(member_or_tingkat, Member) else member_or_tingkat

    t = (tingkat or "").strip().upper()
    has_sk = False
    if m:
        has_sk = bool(m.is_pengurus or (m.nomor_sk and str(m.nomor_sk).strip()))
    else:
        has_sk = t in ("DPW", "DPD", "DPC", "DPRT", "RANTING")

    if not has_sk or t in ("BUKAN PENGURUS", "ANGGOTA", "KADER", "-", ""):
        return "ANGGOTA"

    is_ketua = _is_member_ketua_at_level(m, t)

    if t == "DPW":
        return "KETUA_DPW" if is_ketua else "PENGURUS_DPW"
    elif t == "DPD":
        return "KETUA_DPD" if is_ketua else "PENGURUS_DPD"
    elif t == "DPC":
        return "KETUA_DPC" if is_ketua else "PENGURUS_DPC"
    elif t in ("DPRT", "RANTING"):
        return "KETUA_DPRT" if is_ketua else "PENGURUS_DPRT"
    return "ANGGOTA"


def _normalize_app_user_role(u: AppUser) -> str:
    role = (u.role or "ANGGOTA").strip().upper()
    if role == "INVENTORY":
        return "ADMIN_INVENTORY"
    if role in ("ADMIN_DPW", "ADMIN_DPD", "ADMIN_DPC", "ADMIN_DPRT"):
        if is_ketua_position(u.jabatan or "") and not u.delegated_for_ketua_nik:
            return role.replace("ADMIN_", "KETUA_", 1)
    return role


def claims_for(u: AppUser) -> dict:
    dpd_acc = u.dpd_access or "ALL"
    dpc_acc = u.dpc_access or "ALL"
    dprt_acc = u.dprt_access or "ALL"

    normalized_role = _normalize_app_user_role(u)

    # Peran DPC: dpc_access WAJIB spesifik ke kecamatannya, tidak boleh ALL
    if normalized_role in DPC_ROLES:
        dprt_acc = "ALL"  # Membawahi seluruh DPRt di kecamatannya
        if not dpc_acc or dpc_acc in ("ALL", "Umum (DPD)", "-", ""):
            pos_dpc = Position.query.filter(
                Position.member_nik == u.member_nik,
                Position.tingkat_kepengurusan == "DPC"
            ).first() if u.member_nik else None
            if pos_dpc and pos_dpc.dpc and pos_dpc.dpc not in ("ALL", "Umum (DPD)", "-", ""):
                dpc_acc = pos_dpc.dpc
                u.dpc_access = pos_dpc.dpc
                if pos_dpc.jabatan:
                    u.jabatan = pos_dpc.jabatan
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
            else:
                m = Member.query.filter(Member.nik == u.member_nik).first() if u.member_nik else None
                if m and m.dpc and m.dpc not in ("ALL", "Umum (DPD)", "-", ""):
                    dpc_acc = m.dpc
                    u.dpc_access = m.dpc
                    try:
                        db.session.commit()
                    except Exception:
                        db.session.rollback()

    # Peran DPRt: dpc_access dan dprt_access WAJIB spesifik
    elif normalized_role in DPRT_ROLES:
        if not dprt_acc or dprt_acc in ("ALL", "Pusat/Umum", "-", "") or not dpc_acc or dpc_acc in ("ALL", "Umum (DPD)", "-", ""):
            pos_dprt = Position.query.filter(
                Position.member_nik == u.member_nik,
                Position.tingkat_kepengurusan == "DPRt"
            ).first() if u.member_nik else None
            if pos_dprt:
                if pos_dprt.dpc and pos_dprt.dpc not in ("ALL", "Umum (DPD)", "-", ""):
                    dpc_acc = pos_dprt.dpc
                    u.dpc_access = pos_dprt.dpc
                if pos_dprt.dprt and pos_dprt.dprt not in ("ALL", "Pusat/Umum", "-", ""):
                    dprt_acc = pos_dprt.dprt
                    u.dprt_access = pos_dprt.dprt
                if pos_dprt.jabatan:
                    u.jabatan = pos_dprt.jabatan
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()

    # Peran DPD membawahi SELURUH DPC dan DPRt di DPD-nya
    elif normalized_role in DPD_ROLES:
        dpc_acc = "ALL"
        dprt_acc = "ALL"
    elif normalized_role in (("SUPER_ADMIN", "ADMIN_INVENTORY", "WAKIL_BENDAHARA") + DPW_ROLES):
        dpd_acc = "ALL"
        dpc_acc = "ALL"
        dprt_acc = "ALL"

    # Hanya Super Admin dan Pengurus struktural ber-SK / Delegasi yang berwenang menambahkan anggota baru
    is_structural_actor = bool(normalized_role in (("SUPER_ADMIN",) + TERRITORIAL_ROLES))

    default_jabatan = (
        "Admin Inventaris Kantor DPW Sulut"
        if normalized_role == "ADMIN_INVENTORY"
        else (
            "Wakil Bendahara DPW Sulut"
            if normalized_role == "WAKIL_BENDAHARA"
            else ("Anggota Biasa" if normalized_role == "ANGGOTA" else f"Pengurus {normalized_role}")
        )
    )

    return {
        "role": normalized_role,
        "dpdAccess": dpd_acc,
        "dpcAccess": dpc_acc,
        "dprtAccess": dprt_acc,
        "adminName": u.full_name,
        "adminNik": u.member_nik or u.username,
        "username": u.username,
        "jabatan": u.jabatan or default_jabatan,
        "canAddMembers": is_structural_actor,
        "hasSk": is_structural_actor,
        "isDelegatedAdmin": normalized_role in DELEGATED_ADMIN_ROLES,
        "delegatedForKetuaNik": u.delegated_for_ketua_nik,
        "tokenVersion": int(getattr(u, "token_version", 1) or 1),
    }


_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,28}[A-Za-z0-9]$")


def _validate_custom_username(raw_username: str) -> tuple[str | None, str | None]:
    clean = (raw_username or "").strip()
    if len(clean) < 4 or len(clean) > 30:
        return None, "Username harus terdiri dari 4 hingga 30 karakter."
    if not _USERNAME_RE.match(clean):
        return (
            None,
            "Username hanya boleh berisi huruf, angka, titik (.), garis bawah (_), atau tanda hubung (-) tanpa spasi.",
        )
    return clean, None


def _resolve_authenticated_app_user(claims: dict) -> AppUser | None:
    uid = get_jwt_identity()
    user = None
    if uid:
        try:
            user = db.session.get(AppUser, uuid.UUID(str(uid)))
        except (ValueError, TypeError):
            user = None
    if user is None:
        ident = (claims.get("adminNik") or claims.get("username") or "").strip()
        if ident:
            user = AppUser.query.filter(
                (AppUser.member_nik == ident) | (func.lower(AppUser.username) == ident.lower())
            ).first()
    return user


def resolve_territory_access(member: Member, user_role: str):
    dpd = member.dpd or "ALL"
    dpc = member.dpc or "ALL"
    dprt = member.dprt or "ALL"
    jabatan = member.jabatan or f"Pengurus {member.tingkat_kepengurusan}"

    if user_role in DPC_ROLES:
        pos_dpc = next(
            (
                p
                for p in member.positions
                if p.tingkat_kepengurusan == "DPC" and p.dpc and p.dpc not in ("ALL", "Umum (DPD)", "-", "")
            ),
            None,
        )
        if pos_dpc:
            dpc = pos_dpc.dpc
            if pos_dpc.jabatan:
                jabatan = pos_dpc.jabatan
        elif dpc in ("Umum (DPD)", "-", ""):
            dpc = "ALL"
    elif user_role in DPRT_ROLES:
        pos_dprt = next(
            (
                p
                for p in member.positions
                if p.tingkat_kepengurusan == "DPRt" and p.dprt and p.dprt not in ("ALL", "Pusat/Umum", "-", "")
            ),
            None,
        )
        if pos_dprt:
            if pos_dprt.dpc and pos_dprt.dpc not in ("ALL", "Umum (DPD)", "-", ""):
                dpc = pos_dprt.dpc
            if pos_dprt.dprt and pos_dprt.dprt not in ("ALL", "Pusat/Umum", "-", ""):
                dprt = pos_dprt.dprt
            if pos_dprt.jabatan:
                jabatan = pos_dprt.jabatan

    return dpd, dpc, dprt, jabatan


@bp.post("/auth/login")
@rate_limit(10, 60, scope="ip", key_prefix="auth_login")
def login():
    """
    Login sistem Partai NasDem Sulut (ketat berbasis NIK / Nomor e-KTA / Username):
    - Setiap akun aktif di AppUser diverifikasi ketat terhadap hash kata sandi pribadinya.
    - Tidak ada kata sandi bawaan universal (seperti 'nasdem123') yang dapat melewati kata sandi pengguna.
    - Pengurus yang belum memiliki akun aktif di AppUser harus melakukan aktivasi mandiri terlebih dahulu.
    """
    data = request.get_json(silent=True) or {}
    raw_ident = (data.get("username") or "").strip()
    ident = raw_ident.lower()
    clean_ident = raw_ident.replace(" ", "").replace("-", "").strip()
    clean_ident_low = clean_ident.lower()
    password = (data.get("password") or "").strip()

    if not ident or not password:
        return jsonify(error="NIK dan kata sandi wajib diisi"), 400

    # 1. Cari langsung di tabel AppUser (akun yang sudah aktif)
    user = AppUser.query.filter(
        (func.lower(AppUser.username) == ident)
        | (func.lower(AppUser.username) == clean_ident_low)
        | (func.lower(AppUser.member_nik) == ident)
        | (func.lower(AppUser.member_nik) == clean_ident_low)
        | (func.lower(AppUser.nomor_ekta) == ident)
        | (func.lower(AppUser.nomor_ekta) == clean_ident_low)
    ).first()

    if user:
        if not user.is_active:
            return jsonify(error="Akun dinonaktifkan oleh Super Administrator. Hubungi DPW."), 403

        if not user.password_hash or not check_password_hash(user.password_hash, password):
            return jsonify(
                error="Kata sandi salah. Silakan periksa kembali kata sandi Anda."
            ), 401

        claims = claims_for(user)
        touch_user_presence(str(user.id), force=True)
        token = create_access_token(
            identity=str(user.id),
            additional_claims=claims,
            expires_delta=get_config().JWT_ACCESS_TOKEN_EXPIRES,
        )
        return jsonify(token=token, session=claims)

    # 2. Cari di tabel Member (termasuk pencocokan aman 16 digit NIK KTP ke NIK SK <16 digit)
    member, should_heal_nik = find_member_by_nik_or_incomplete_match(clean_ident)

    if not member:
        return jsonify(error="NIK atau Nomor e-KTA tidak ditemukan di database kepengurusan Partai NasDem Sulut"), 404

    # Cek apakah Member ini sebenarnya sudah punya akun AppUser pada NIK lamanya (<16 digit)
    existing_member_user = AppUser.query.filter(
        (AppUser.member_nik == member.nik) | (func.lower(AppUser.username) == member.nik.lower())
    ).first()

    if existing_member_user:
        if not existing_member_user.is_active:
            return jsonify(error="Akun dinonaktifkan oleh Super Administrator. Hubungi DPW."), 403

        if not existing_member_user.password_hash or not check_password_hash(
            existing_member_user.password_hash, password
        ):
            return jsonify(
                error="Kata sandi salah. Silakan periksa kembali kata sandi Anda."
            ), 401

        target_user_id = existing_member_user.id
        if should_heal_nik:
            rename_member_nik_safely(db.session, member, clean_ident)
            invalidate_all_data_cache()
        db.session.commit()
        refreshed_user = db.session.get(AppUser, target_user_id)
        claims = claims_for(refreshed_user)
        touch_user_presence(str(refreshed_user.id), force=True)
        token = create_access_token(
            identity=str(refreshed_user.id),
            additional_claims=claims,
            expires_delta=get_config().JWT_ACCESS_TOKEN_EXPIRES,
        )
        return jsonify(token=token, session=claims)

    # 3. Member belum pernah diaktivasi di AppUser -> wajib aktivasi melalui tab Aktivasi Pengurus
    return jsonify(
        error="Akun untuk NIK ini belum diaktivasi. Silakan gunakan tab 'Aktivasi Pengurus' terlebih dahulu untuk membuat kata sandi pribadi Anda."
    ), 401


def _mask_person_name(name: str) -> str:
    """Menyamarkan nama lengkap pada endpoint publik (mis. 'Budi Santoso' -> 'Bu*** Sa***')."""
    words = [w for w in str(name or "").strip().split() if w]
    if not words:
        return "Pengurus Terdaftar"
    masked = []
    for w in words:
        keep = 2 if len(w) >= 4 else 1
        masked.append(f"{w[:keep]}***")
    return " ".join(masked)


def _normalize_person_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


def _resolve_activation_challenge(member: Member) -> tuple[str, str, str]:
    """Menentukan tantangan verifikasi identitas saat aktivasi akun pengurus."""
    digits = re.sub(r"\D", "", str(getattr(member, "nomor_hp", "") or ""))
    if len(digits) >= 8 and len(set(digits)) > 1:
        hint = f"{digits[:4]}-****-XXXX" if len(digits) >= 8 else "****-XXXX"
        return (
            "PHONE_LAST4",
            digits[-4:],
            f"Masukkan 4 digit terakhir Nomor HP terdaftar ({hint})",
        )
    return (
        "FULL_NAME",
        _normalize_person_name(member.nama_lengkap),
        "Masukkan Nama Lengkap Anda sesuai data SK kepengurusan",
    )


def _verify_activation_answer(member: Member, provided: str) -> bool:
    method, expected, _ = _resolve_activation_challenge(member)
    if not expected:
        return False
    if method == "PHONE_LAST4":
        digits = re.sub(r"\D", "", str(provided or ""))
        return len(digits) == 4 and digits == expected
    return _normalize_person_name(provided) == expected


@bp.post("/auth/check-pengurus")
@rate_limit(5, 60, scope="ip", key_prefix="auth_check")
def check_pengurus():
    """Memeriksa apakah NIK/e-KTA terdaftar sebagai pengurus resmi (data disamarkan dari scraping)."""
    data = request.get_json(silent=True) or {}
    ident = (data.get("nik") or "").strip()
    clean_ident = ident.replace(" ", "").replace("-", "").strip()
    if not clean_ident:
        return jsonify(error="Nomor NIK KTP atau e-KTA wajib diisi"), 400

    member, should_heal_nik = find_member_by_nik_or_incomplete_match(clean_ident)

    if not member:
        return jsonify(
            found=False,
            error="NIK / Nomor e-KTA tidak ditemukan dalam database kepengurusan Partai NasDem Sulut. Silakan hubungi Sekretariat DPW.",
        ), 404

    user = AppUser.query.filter(
        (AppUser.member_nik == member.nik) | (func.lower(AppUser.username) == member.nik.lower())
    ).first()

    effective_nik = clean_ident if should_heal_nik else member.nik
    v_method, _, v_prompt = _resolve_activation_challenge(member)
    return jsonify({
        "found": True,
        "nik": effective_nik,
        "nikAutoCorrectedFrom": member.nik if should_heal_nik else None,
        "namaLengkap": _mask_person_name(member.nama_lengkap),
        "jabatan": member.jabatan or "Pengurus",
        "tingkatKepengurusan": member.tingkat_kepengurusan or "DPC",
        "dpd": member.dpd,
        "verificationMethod": v_method,
        "verificationPrompt": v_prompt,
        "hasCustomPassword": user is not None and bool(user.password_hash),
        "defaultPasswordHint": "",
    })


@bp.post("/auth/activate-pengurus")
@rate_limit(5, 60, scope="ip", key_prefix="auth_activate")
def activate_pengurus():
    """Aktivasi akun pengurus atau buat kata sandi baru mandiri (wajib verifikasi identitas + opsional username)."""
    data = request.get_json(silent=True) or {}
    ident = (data.get("nik") or "").strip()
    clean_ident = ident.replace(" ", "").replace("-", "").strip()
    new_password = (data.get("newPassword") or "").strip()
    raw_username = (data.get("username") or "").strip()
    verification_value = (data.get("verificationValue") or "").strip()

    if not clean_ident:
        return jsonify(error="Nomor NIK KTP wajib diisi"), 400
    if not verification_value:
        return jsonify(error="Verifikasi identitas (4 digit akhir HP atau Nama Lengkap) wajib diisi"), 400
    if not new_password or len(new_password) < 6:
        return jsonify(error="Kata sandi baru minimal 6 karakter"), 400

    chosen_username = None
    if raw_username:
        chosen_username, username_err = _validate_custom_username(raw_username)
        if username_err:
            return jsonify(error=username_err), 400

    member, should_heal_nik = find_member_by_nik_or_incomplete_match(clean_ident)

    if not member:
        return jsonify(error="Data pengurus dengan NIK tersebut tidak ditemukan di database"), 404

    # Cegah pengambilalihan akun jika AppUser sudah aktif dan sudah memiliki kata sandi
    existing_user = AppUser.query.filter(
        (AppUser.member_nik == member.nik) | (func.lower(AppUser.username) == member.nik.lower())
    ).first()
    if existing_user and existing_user.password_hash:
        return jsonify(
            error="Akun ini sudah aktif dan memiliki kata sandi. Silakan masuk melalui tab Login, ubah kata sandi dari menu Profil Saya setelah login, atau hubungi Super Admin DPW jika lupa kata sandi."
        ), 409

    if not _verify_activation_answer(member, verification_value):
        return jsonify(
            error="Verifikasi identitas tidak cocok. Periksa kembali data Anda atau hubungi Super Admin DPW."
        ), 403

    if chosen_username:
        username_conflict_query = AppUser.query.filter(
            (func.lower(AppUser.username) == chosen_username.lower())
            | (func.lower(AppUser.member_nik) == chosen_username.lower())
        )
        if existing_user:
            username_conflict_query = username_conflict_query.filter(AppUser.id != existing_user.id)
        if username_conflict_query.first():
            return jsonify(error="Username sudah digunakan oleh akun lain. Silakan pilih username lain."), 409

    if should_heal_nik:
        member = rename_member_nik_safely(db.session, member, clean_ident)
        invalidate_all_data_cache()

    # Cari ulang AppUser bila ada (mis. belum punya password_hash)
    user = AppUser.query.filter(
        (AppUser.member_nik == member.nik) | (func.lower(AppUser.username) == member.nik.lower())
    ).first()

    user_role = determine_role(member)
    dpd_val, dpc_val, dprt_val, jab_val = resolve_territory_access(member, user_role)

    if user:
        user.member_nik = member.nik
        if chosen_username:
            user.username = chosen_username
        user.password_hash = generate_password_hash(new_password)
        # Jangan menimpa nama admin custom bila sudah diatur khusus oleh Super Admin
        if not user.full_name:
            user.full_name = member.nama_lengkap
        # Pertahankan peran dan isolasi wilayah jika sudah diatur secara khusus oleh Super Admin
        if not user.role or user.role == "ANGGOTA":
            user.role = user_role
            user.dpd_access = dpd_val
            user.dpc_access = dpc_val
            user.dprt_access = dprt_val
            user.jabatan = jab_val
        user.is_active = True
    else:
        user = AppUser(
            member_nik=member.nik,
            nomor_ekta=member.nomor_ekta,
            username=chosen_username or member.nik,
            password_hash=generate_password_hash(new_password),
            full_name=member.nama_lengkap,
            role=user_role,
            dpd_access=dpd_val,
            dpc_access=dpc_val,
            dprt_access=dprt_val,
            jabatan=jab_val,
            is_active=True,
        )
        db.session.add(user)

    db.session.commit()

    claims = claims_for(user)
    touch_user_presence(str(user.id), force=True)
    token = create_access_token(
        identity=str(user.id),
        additional_claims=claims,
        expires_delta=get_config().JWT_ACCESS_TOKEN_EXPIRES,
    )
    return jsonify(
        token=token,
        session=claims,
        message=f"Akun pengurus berhasil diaktivasi untuk {member.nama_lengkap} ({member.jabatan})!",
    )


@bp.post("/auth/change-username")
@login_required
def change_username():
    """Mengatur atau memperbarui username login akun sendiri agar tidak perlu mengetik NIK."""
    data = request.get_json(silent=True) or {}
    raw_username = (data.get("username") or "").strip()
    clean_username, username_err = _validate_custom_username(raw_username)
    if username_err:
        return jsonify(error=username_err), 400

    claims = current_claims()
    user = _resolve_authenticated_app_user(claims)
    if user is None:
        return jsonify(error="Akun pengguna tidak ditemukan."), 404

    conflict = AppUser.query.filter(
        AppUser.id != user.id,
        (func.lower(AppUser.username) == clean_username.lower())
        | (func.lower(AppUser.member_nik) == clean_username.lower()),
    ).first()
    if conflict:
        return jsonify(error="Username sudah digunakan oleh akun lain. Silakan pilih username lain."), 409

    user.username = clean_username
    db.session.commit()

    updated_claims = claims_for(user)
    token = create_access_token(
        identity=str(user.id),
        additional_claims=updated_claims,
        expires_delta=get_config().JWT_ACCESS_TOKEN_EXPIRES,
    )
    return jsonify(
        token=token,
        session=updated_claims,
        message=f"Username login berhasil disimpan sebagai '{clean_username}'.",
    )


@bp.post("/auth/change-password")
@login_required
@rate_limit(6, 60, scope="user_or_ip", key_prefix="auth_chgpass")
def change_password():
    """Mengubah kata sandi akun sendiri dengan konfirmasi kata sandi lama."""
    data = request.get_json(silent=True) or {}
    old_password = (data.get("oldPassword") or "").strip()
    new_password = (data.get("newPassword") or "").strip()
    confirm_password = (data.get("confirmPassword") or "").strip()

    if not old_password or not new_password or not confirm_password:
        return jsonify(error="Kata sandi lama, kata sandi baru, dan konfirmasi kata sandi wajib diisi."), 400
    if len(new_password) < 6:
        return jsonify(error="Kata sandi baru minimal 6 karakter."), 400
    if new_password != confirm_password:
        return jsonify(error="Konfirmasi kata sandi baru tidak cocok."), 400
    if new_password == old_password:
        return jsonify(error="Kata sandi baru tidak boleh sama dengan kata sandi lama."), 400

    claims = current_claims()
    user = _resolve_authenticated_app_user(claims)

    if user is None:
        return jsonify(error="Akun pengguna tidak ditemukan."), 404

    if not user.password_hash or not check_password_hash(user.password_hash, old_password):
        return jsonify(error="Kata sandi lama yang Anda masukkan salah."), 400

    user.password_hash = generate_password_hash(new_password)
    user.token_version = int(getattr(user, "token_version", 1) or 1) + 1
    db.session.commit()
    invalidate_user_auth_state(str(user.id))

    updated_claims = claims_for(user)
    new_token = create_access_token(
        identity=str(user.id),
        additional_claims=updated_claims,
        expires_delta=get_config().JWT_ACCESS_TOKEN_EXPIRES,
    )
    return jsonify(
        token=new_token,
        session=updated_claims,
        message="Kata sandi berhasil diperbarui. Sesi lama di perangkat lain telah dicabut otomatis.",
    )


@bp.post("/auth/refresh")
@login_required
def refresh_session():
    """Memperpanjang masa aktif token sesi pengurus / admin."""
    claims = current_claims()
    user = _resolve_authenticated_app_user(claims)
    if user is not None:
        if not user.is_active:
            return jsonify(error="Akun dinonaktifkan oleh Super Administrator."), 403
        refreshed_claims = claims_for(user)
        token = create_access_token(
            identity=str(user.id),
            additional_claims=refreshed_claims,
            expires_delta=get_config().JWT_ACCESS_TOKEN_EXPIRES,
        )
        return jsonify(token=token, session=refreshed_claims)

    ident = claims.get("sub") or claims.get("adminNik") or "user"
    token = create_access_token(
        identity=str(ident),
        additional_claims=claims,
        expires_delta=get_config().JWT_ACCESS_TOKEN_EXPIRES,
    )
    return jsonify(token=token, session=claims)


@bp.post("/auth/logout")
@login_required
def logout_session():
    """Menandai sesi user langsung offline (`last_seen_at = NULL`) saat keluar."""
    clear_user_presence(str(get_jwt_identity() or ""))
    return jsonify(ok=True)


@bp.get("/auth/me")
@login_required
def me():
    c = current_claims()
    keys = (
        "role",
        "dpdAccess",
        "dpcAccess",
        "dprtAccess",
        "adminName",
        "adminNik",
        "username",
        "jabatan",
        "canAddMembers",
        "hasSk",
        "isDelegatedAdmin",
        "delegatedForKetuaNik",
    )
    return jsonify(session={k: c.get(k) for k in keys})
