"""Serialisasi snake_case (DB) <-> camelCase (frontend), sanitasi, dan scoping wilayah."""
from datetime import date, datetime

from sqlalchemy import or_, select

from .models import Member, Position

STATUS_VALUES = (
    "Aktif",
    "Non-Aktif (Meninggal)",
    "Non-Aktif (Pindah Partai/Mundur)",
    "Non-Aktif (Pindah Domisili)",
)

# kolom DB -> nama field di frontend (src/types/index.ts)
MEMBER_FIELDS = {
    "nik": "nik",
    "nomor_ekta": "nomorEKTA",
    "nama_lengkap": "namaLengkap",
    "nomor_hp": "nomorHP",
    "email": "email",
    "sosmed": "sosmed",
    "dpd": "dpd",
    "dpc": "dpc",
    "dprt": "dprt",
    "is_pengurus": "isPengurus",
    "tingkat_kepengurusan": "tingkatKepengurusan",
    "wilayah_penugasan": "wilayahPenugasan",
    "nomor_sk": "nomorSK",
    "tanggal_sk": "tanggalSK",
    "kode_referral": "kodeReferral",
    "jabatan": "jabatan",
    "status": "status",
    "tanggal_bergabung": "tanggalBergabung",
    "catatan_mutasi": "catatanMutasi",
    "terakhir_diubah": "terakhirDiubah",
    "diubah_oleh": "diubahOleh",
    "foto_profil_url": "fotoProfilUrl",
    "foto_ktp_url": "fotoKtpUrl",
    "jenis_kelamin": "jenisKelamin",
    "tempat_lahir": "tempatLahir",
    "tanggal_lahir": "tanggalLahir",
    "alamat_lengkap": "alamatLengkap",
    "agama": "agama",
    "pekerjaan": "pekerjaan",
    "status_ktp_verified": "statusKtpVerified",
    "kader_perekrut_kode_referral": "kaderPerekrutKodeReferral",
    "kader_perekrut_nama": "kaderPerekrutNama",
    "kader_perekrut_nik": "kaderPerekrutNik",
    "kader_perekrut_ekta": "kaderPerekrutEkta",
    "kader_perekrut_jabatan": "kaderPerekrutJabatan",
    "ketua_langsung_nik": "ketuaLangsungNik",
    "ketua_langsung_nama": "ketuaLangsungNama",
    "ketua_langsung_jabatan": "ketuaLangsungJabatan",
    "ketua_langsung_tingkat": "ketuaLangsungTingkat",
    "urutan": "urutan",
}
CAMEL_TO_DB = {v: k for k, v in MEMBER_FIELDS.items()}
DATE_COLS = {"tanggal_sk", "tanggal_bergabung", "tanggal_lahir"}
HEAVY_COLS = {"foto_profil_url", "foto_ktp_url"}  # tidak dikirim di list (hemat bandwidth)
WRITABLE_COLS = set(MEMBER_FIELDS) - {"terakhir_diubah"}

# ---------------- Jabatan struktural (tabel positions) ----------------
POSITION_FIELDS = {
    "id": "id",
    "tingkat_kepengurusan": "tingkatKepengurusan",
    "dpd": "dpd",
    "dpc": "dpc",
    "dprt": "dprt",
    "wilayah_penugasan": "wilayahPenugasan",
    "jabatan": "jabatan",
    "nomor_sk": "nomorSK",
    "tanggal_sk": "tanggalSK",
    "urutan": "urutan",
}
POSITION_COLS = [c for c in POSITION_FIELDS if c != "id"]
STRUCTURAL_LEVELS = ("DPW", "DPD", "DPC", "DPRt")
_LEVEL_RANK = {"DPW": 0, "DPD": 1, "DPC": 2, "DPRt": 3}
# Kolom ringkasan jabatan utama di tabel members (dihitung otomatis dari positions)
SUMMARY_COLS = (
    "is_pengurus",
    "tingkat_kepengurusan",
    "wilayah_penugasan",
    "jabatan",
    "nomor_sk",
    "tanggal_sk",
    "urutan",
)


def make_position_id(nik, tingkat, dpd, dpc, dprt, wilayah) -> str:
    """ID deterministik 1 kursi jabatan: orang (NIK) + badan organisasi (tingkat/wilayah/sub-organisasi).

    Jabatan sengaja TIDAK ikut dalam id: seseorang hanya bisa menduduki satu kursi per badan organisasi,
    sehingga mengubah jabatan = memperbarui kursi yang sama (tidak menjadi baris ganda).
    """
    import hashlib

    parts = [nik, tingkat, dpd, dpc, dprt, wilayah]
    raw = "|".join(str(p or "").strip().lower() for p in parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def position_values_from_row(values: dict):
    """Ambil data jabatan dari 1 baris flat (payload frontend). None bila baris ini bukan jabatan struktural."""
    tingkat = values.get("tingkat_kepengurusan")
    if tingkat not in STRUCTURAL_LEVELS or not values.get("is_pengurus"):
        return None
    pos = {c: values.get(c) for c in POSITION_COLS}
    pos["dpc"] = pos.get("dpc") or "-"
    pos["dprt"] = pos.get("dprt") or "-"
    pos["member_nik"] = values["nik"]
    pos["id"] = make_position_id(
        values["nik"], tingkat, pos.get("dpd"), pos["dpc"], pos["dprt"], pos.get("wilayah_penugasan")
    )
    return pos


def primary_position(positions):
    """Jabatan utama = tingkat tertinggi (DPW>DPD>DPC>DPRt), lalu urutan terkecil."""
    if not positions:
        return None
    return sorted(
        positions,
        key=lambda p: (
            _LEVEL_RANK.get(p.tingkat_kepengurusan, 9),
            p.urutan if p.urutan is not None else 10**9,
            p.wilayah_penugasan or "",
        ),
    )[0]


def refresh_member_summary(member):
    """Hitung ulang kolom ringkasan jabatan utama pada Member dari daftar positions-nya."""
    p = primary_position(list(member.positions))
    if p:
        member.is_pengurus = True
        member.tingkat_kepengurusan = p.tingkat_kepengurusan
        member.wilayah_penugasan = p.wilayah_penugasan
        member.jabatan = p.jabatan or "Pengurus"
        member.nomor_sk = p.nomor_sk
        member.tanggal_sk = p.tanggal_sk
        member.urutan = p.urutan
    else:
        member.is_pengurus = False
        member.tingkat_kepengurusan = "Bukan Pengurus"
        member.wilayah_penugasan = None
        member.jabatan = "Anggota Biasa"
        member.nomor_sk = None
        member.tanggal_sk = None
        member.urutan = None


def is_generated_nik(nik: str) -> bool:
    """NIK sementara buatan sistem (bukan NIK KTP asli)."""
    import re

    nik = nik or ""
    return bool(re.match(r"^0{6,}", nik) or re.match(r"^71\d{2}(11|22|33|44|55|99)26\d{8}$", nik))



def sanitize_date(value):
    if not value:
        return None
    if isinstance(value, (date, datetime)):
        return value if isinstance(value, date) and not isinstance(value, datetime) else value.date()
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _iso(v):
    return v.isoformat() if isinstance(v, (date, datetime)) else v


def member_to_dict(m: Member, include_photos=True, include_positions=True) -> dict:
    out = {}
    for col, camel in MEMBER_FIELDS.items():
        if not include_photos and col in HEAVY_COLS:
            continue
        v = getattr(m, col)
        if v is None and camel not in ("nik", "namaLengkap"):
            continue
        out[camel] = _iso(v)
    out.setdefault("nomorHP", "-")
    if include_positions:
        out["positions"] = [
            position_to_dict(p)
            for p in sorted(
                m.positions,
                key=lambda p: (_LEVEL_RANK.get(p.tingkat_kepengurusan, 9), p.urutan if p.urutan is not None else 10**9),
            )
        ]
    return out


def position_to_dict(p) -> dict:
    out = {}
    for col, camel in POSITION_FIELDS.items():
        v = getattr(p, col)
        if v is None:
            continue
        out[camel] = _iso(v)
    return out


def payload_to_member_values(data: dict) -> dict:
    """Terima payload camelCase ATAU snake_case -> dict kolom DB yang valid."""
    values = {}
    for key, val in data.items():
        col = CAMEL_TO_DB.get(key, key)
        if col not in WRITABLE_COLS:
            continue
        if col in DATE_COLS:
            val = sanitize_date(val)
        elif isinstance(val, str):
            val = val.strip() or None
        values[col] = val
    if "nik" in values and values["nik"] is not None:
        values["nik"] = str(values["nik"]).strip()
    for col in ("dpc", "dprt"):
        if col in values and not values[col]:
            values[col] = "-"
    return values


def row_to_dict(obj, fields: dict) -> dict:
    out = {}
    for col, camel in fields.items():
        v = getattr(obj, col)
        out[camel] = str(v) if col == "id" else _iso(v)
    return out


# ---------------- Scoping RBAC ----------------
def is_global(claims: dict) -> bool:
    return claims.get("role") in ("SUPER_ADMIN", "ADMIN_DPW") and claims.get("dpdAccess", "ALL") == "ALL"


def apply_scope(query, claims: dict, model=Member):
    if is_global(claims):
        return query
    role = claims.get("role")

    # 1. DPD filter: berlaku untuk ADMIN_DPD, ADMIN_DPC, ADMIN_DPRT
    dpd_val = claims.get("dpdAccess")
    if dpd_val and dpd_val != "ALL" and hasattr(model, "dpd"):
        query = query.filter(getattr(model, "dpd") == dpd_val)

    # 2. DPC filter: hanya membatasi ADMIN_DPC dan ADMIN_DPRT
    # ADMIN_DPD berhak melihat SELURUH DPC di wilayah DPD-nya
    if role in ("ADMIN_DPC", "ADMIN_DPRT"):
        dpc_val = claims.get("dpcAccess")
        if dpc_val and dpc_val not in ("ALL", "Umum (DPD)", "-", "") and hasattr(model, "dpc"):
            query = query.filter(getattr(model, "dpc") == dpc_val)

    # 3. DPRt filter: hanya membatasi ADMIN_DPRT
    # ADMIN_DPD dan ADMIN_DPC berhak melihat SELURUH DPRt di bawah wilayahnya
    if role == "ADMIN_DPRT":
        dprt_val = claims.get("dprtAccess")
        if dprt_val and dprt_val not in ("ALL", "Pusat/Umum", "-", "") and hasattr(model, "dprt"):
            query = query.filter(getattr(model, "dprt") == dprt_val)

    return query


def in_scope(claims: dict, dpd=None, dpc=None, dprt=None) -> bool:
    if is_global(claims):
        return True
    role = claims.get("role")

    dpd_want = claims.get("dpdAccess")
    if dpd_want and dpd_want != "ALL" and dpd != dpd_want:
        return False

    if role in ("ADMIN_DPC", "ADMIN_DPRT"):
        dpc_want = claims.get("dpcAccess")
        if dpc_want and dpc_want not in ("ALL", "Umum (DPD)", "-", "") and dpc != dpc_want:
            return False

    if role == "ADMIN_DPRT":
        dprt_want = claims.get("dprtAccess")
        if dprt_want and dprt_want not in ("ALL", "Pusat/Umum", "-", "") and dprt != dprt_want:
            return False

    return True


def member_scope_query(claims: dict):
    """Query Member dalam cakupan akun: domisili di wilayahnya ATAU memegang jabatan di wilayahnya."""
    q = Member.query
    if is_global(claims):
        return q
    in_positions = apply_scope(select(Position.member_nik), claims, Position)
    in_domicile = apply_scope(select(Member.nik), claims, Member)
    return q.filter(or_(Member.nik.in_(in_positions), Member.nik.in_(in_domicile)))


def member_in_scope(claims: dict, m: Member) -> bool:
    if in_scope(claims, m.dpd, m.dpc, m.dprt):
        return True
    return any(in_scope(claims, p.dpd, p.dpc, p.dprt) for p in m.positions)
