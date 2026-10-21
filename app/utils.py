"""Serialisasi snake_case (DB) <-> camelCase (frontend), sanitasi, dan scoping wilayah."""
from datetime import date, datetime

from .models import Member

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


def member_to_dict(m: Member, include_photos=True) -> dict:
    out = {}
    for col, camel in MEMBER_FIELDS.items():
        if not include_photos and col in HEAVY_COLS:
            continue
        v = getattr(m, col)
        if v is None and camel not in ("nik", "namaLengkap"):
            continue
        out[camel] = _iso(v)
    out.setdefault("nomorHP", "-")
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
    for col, key in (("dpd", "dpdAccess"), ("dpc", "dpcAccess"), ("dprt", "dprtAccess")):
        val = claims.get(key)
        if val and val != "ALL" and hasattr(model, col):
            query = query.filter(getattr(model, col) == val)
    return query


def in_scope(claims: dict, dpd=None, dpc=None, dprt=None) -> bool:
    if is_global(claims):
        return True
    for have, key in ((dpd, "dpdAccess"), (dpc, "dpcAccess"), (dprt, "dprtAccess")):
        want = claims.get(key)
        if want and want != "ALL" and have != want:
            return False
    return True
