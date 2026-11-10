"""Serialisasi snake_case (DB) <-> camelCase (frontend), sanitasi, dan scoping wilayah."""
from datetime import date, datetime

from sqlalchemy import func, or_, select

from .models import AppUser, Member, Position
from .services.image_storage import resolve_stored_image_url

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


def make_position_id(nik, tingkat, dpd, dpc, dprt, wilayah, jabatan=None) -> str:
    """ID deterministik 1 kursi jabatan: orang (NIK) + badan organisasi + nama jabatan.

    Menyertakan jabatan memungkinkan seorang kader (1 NIK) memegang lebih dari satu jabatan
    (rangkap jabatan) dalam badan organisasi yang sama secara sah tanpa saling menimpa.
    """
    import hashlib

    parts = [nik, tingkat, dpd, dpc, dprt, wilayah, jabatan or ""]
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
        values["nik"],
        tingkat,
        pos.get("dpd"),
        pos["dpc"],
        pos["dprt"],
        pos.get("wilayah_penugasan"),
        pos.get("jabatan"),
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
        if p.dpd:
            member.dpd = p.dpd
        if p.dpc and p.dpc != "-":
            member.dpc = p.dpc
        if p.dprt and p.dprt != "-":
            member.dprt = p.dprt
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


def is_acceptable_nik(nik: str) -> bool:
    """NIK angka yang diterima sistem (standar 16 digit, toleransi 8-20 digit untuk impor belum lengkap)."""
    s = str(nik or "").strip()
    return bool(s.isdigit() and 8 <= len(s) <= 20)


def is_subsequence_digits(short_s: str, long_s: str) -> bool:
    """True bila seluruh digit short_s muncul secara berurutan di dalam long_s."""
    it = iter(long_s)
    return all(ch in it for ch in short_s)


def find_member_by_nik_or_incomplete_match(clean_ident: str):
    """Cari Member berdasarkan NIK/e-KTA persis, atau pencocokan aman NIK KTP 16 digit ke NIK SK <16 digit.

    Mengembalikan tuple `(member, should_auto_heal_nik)`:
    - `should_auto_heal_nik = True` hanya bila pengguna memasukkan 16 digit angka valid dan cocok tunggal
      dengan 1 pengurus di database yang NIK tersimpannya masih belum 16 digit akibat salah ketik Excel.
    """
    ident_str = str(clean_ident or "").strip()
    if not ident_str:
        return None, False

    ident_low = ident_str.lower()
    exact = Member.query.filter(
        (func.lower(Member.nik) == ident_low)
        | (func.lower(Member.nomor_ekta) == ident_low)
    ).first()
    if exact:
        return exact, False

    # Fallback cerdas khusus bila input adalah 16 digit NIK KTP asli namun di DB tercatat kurang/lebih 1-2 digit
    if ident_str.isdigit() and len(ident_str) == 16 and not is_generated_nik(ident_str):
        candidates = Member.query.filter(
            Member.nik.like(f"{ident_str[:6]}%"),
            func.length(Member.nik) != 16,
        ).all()

        matched = []
        for cand in candidates:
            cnik = (cand.nik or "").strip()
            if not cnik.isdigit() or is_generated_nik(cnik):
                continue
            if 12 <= len(cnik) < 16 and (16 - len(cnik)) <= 2 and is_subsequence_digits(cnik, ident_str):
                matched.append(cand)
            elif 16 < len(cnik) <= 18 and (len(cnik) - 16) <= 2 and is_subsequence_digits(ident_str, cnik):
                matched.append(cand)

        if len(matched) == 1:
            return matched[0], True

        # Jika ada >1 kandidat di kecamatan yang sama, perketat ke 12 digit awal (Kode Wilayah + Tgl Lahir KTP)
        strict_12 = [c for c in matched if (c.nik or "")[:12] == ident_str[:12]]
        if len(strict_12) == 1:
            return strict_12[0], True

    return None, False


def rename_member_nik_safely(session, member: Member, new_nik: str) -> Member:
    """Perbarui Primary Key `members.nik` beserta `positions` dan akun `app_users` tanpa melanggar FK PostgreSQL."""
    old_nik = str(member.nik or "").strip()
    clean_new = str(new_nik or "").strip()
    if not old_nik or not clean_new or old_nik == clean_new:
        return member

    # Pastikan NIK baru belum dipakai orang lain
    conflict = session.get(Member, clean_new)
    if conflict and conflict.nik != old_nik:
        return member

    pos_snapshots = [
        {c: getattr(p, c) for c in POSITION_COLS}
        for p in list(member.positions or [])
    ]

    # 1. Lepas sementara positions lama agar FK members.nik tidak memblokir UPDATE PK di PostgreSQL
    session.query(Position).filter(Position.member_nik == old_nik).delete(synchronize_session=False)
    session.flush()

    # 2. Update PK pada tabel members dan referensi terkait
    session.execute(
        Member.__table__.update()
        .where(Member.__table__.c.nik == old_nik)
        .values(nik=clean_new)
    )
    session.query(Member).filter(Member.kader_perekrut_nik == old_nik).update(
        {Member.kader_perekrut_nik: clean_new}, synchronize_session=False
    )
    session.query(Member).filter(Member.ketua_langsung_nik == old_nik).update(
        {Member.ketua_langsung_nik: clean_new}, synchronize_session=False
    )

    # 3. Sinkronkan akun login AppUser agar pengurus langsung login dengan NIK baru tanpa putus sandi
    linked_users = session.query(AppUser).filter(
        (AppUser.member_nik == old_nik) | (func.lower(AppUser.username) == old_nik.lower())
    ).all()
    username_taken = (
        session.query(AppUser)
        .filter(func.lower(AppUser.username) == clean_new.lower())
        .first()
    )
    for u in linked_users:
        u.member_nik = clean_new
        if u.username.strip().lower() == old_nik.lower() and (not username_taken or username_taken.id == u.id):
            u.username = clean_new

    # 4. Masukkan kembali positions dengan member_nik & hash id yang baru
    for snap in pos_snapshots:
        snap["member_nik"] = clean_new
        snap["dpc"] = snap.get("dpc") or "-"
        snap["dprt"] = snap.get("dprt") or "-"
        snap["id"] = make_position_id(
            clean_new,
            snap.get("tingkat_kepengurusan"),
            snap.get("dpd"),
            snap["dpc"],
            snap["dprt"],
            snap["wilayah_penugasan"],
            snap.get("jabatan"),
        )
        session.add(Position(**snap))

    session.flush()
    session.expire_all()
    return session.get(Member, clean_new)



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
        v = getattr(m, col)
        if col in HEAVY_COLS and v:
            v = resolve_stored_image_url(v)
        if not include_photos and col in HEAVY_COLS:
            # Jika berupa URL fisik WebP ringkas (< 500 byte), tetap sertakan
            if v and isinstance(v, str) and not v.startswith("data:image/") and len(v) < 500:
                out[camel] = _iso(v)
            continue
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
    """Super Admin dan Admin DPW selalu memiliki akses global penuh ke seluruh wilayah Sulawesi Utara."""
    role = claims.get("role")
    if role in ("SUPER_ADMIN", "ADMIN_DPW"):
        return True
    return claims.get("dpdAccess") == "ALL"


def clean_region_name(name: str) -> str:
    """Membersihkan imbuhan administratif seperti Kabupaten, Kota, Kec., Desa untuk pencocokan yang akurat."""
    if not name:
        return ""
    import re
    n = re.sub(
        r"^(kabupaten|kab\.|kab|kota|kotamadya|kecamatan|kec\.|kec|desa|kelurahan|kel\.|des)\s+",
        "",
        str(name).strip(),
        flags=re.IGNORECASE,
    ).strip().lower()

    aliases = {
        "bolmong": "bolaang mongondow",
        "bm": "bolaang mongondow",
        "bolmut": "bolaang mongondow utara",
        "bolsel": "bolaang mongondow selatan",
        "boltim": "bolaang mongondow timur",
        "minsel": "minahasa selatan",
        "minut": "minahasa utara",
        "mitra": "minahasa tenggara",
        "sitaro": "kepulauan siau tagulandang biaro",
        "sangihe": "kepulauan sangihe",
        "talaud": "kepulauan talaud",
    }
    if n in aliases:
        n = aliases[n]
    elif "tagulandang" in n or "sitaro" in n:
        n = "kepulauan siau tagulandang biaro"

    n = re.sub(r"[^a-z0-9]", " ", n)
    return " ".join(n.split()).upper()



def apply_scope(query, claims: dict, model=Member):
    if is_global(claims):
        return query
    role = claims.get("role")

    # Anggota biasa tanpa SK HANYA memiliki akses ke data pribadinya sendiri
    if role == "ANGGOTA":
        my_nik = str(claims.get("adminNik") or "")
        if hasattr(model, "nik"):
            return query.filter(getattr(model, "nik") == my_nik)
        elif hasattr(model, "member_nik"):
            return query.filter(getattr(model, "member_nik") == my_nik)
        return query.filter(False)

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
    if role == "ANGGOTA":
        return False

    dpd_want = claims.get("dpdAccess")
    if dpd_want and dpd_want != "ALL":
        if clean_region_name(dpd) != clean_region_name(dpd_want):
            return False

    if role in ("ADMIN_DPC", "ADMIN_DPRT"):
        dpc_want = claims.get("dpcAccess")
        if dpc_want and dpc_want not in ("ALL", "Umum (DPD)", "-", ""):
            if clean_region_name(dpc) != clean_region_name(dpc_want):
                return False

    if role == "ADMIN_DPRT":
        dprt_want = claims.get("dprtAccess")
        if dprt_want and dprt_want not in ("ALL", "Pusat/Umum", "-", ""):
            if clean_region_name(dprt) != clean_region_name(dprt_want):
                return False

    return True


def member_scope_query(claims: dict):
    """Query Member dalam cakupan akun: domisili di wilayahnya ATAU memegang jabatan di wilayahnya."""
    q = Member.query
    if is_global(claims):
        return q
    if claims.get("role") == "ANGGOTA":
        my_nik = str(claims.get("adminNik") or "")
        return q.filter(Member.nik == my_nik)
    in_positions = apply_scope(select(Position.member_nik), claims, Position)
    in_domicile = apply_scope(select(Member.nik), claims, Member)
    return q.filter(or_(Member.nik.in_(in_positions), Member.nik.in_(in_domicile)))


def member_in_scope(claims: dict, m: Member) -> bool:
    if claims.get("role") == "ANGGOTA":
        return str(m.nik) == str(claims.get("adminNik") or "")
    if in_scope(claims, m.dpd, m.dpc, m.dprt):
        return True
    return any(in_scope(claims, p.dpd, p.dpc, p.dprt) for p in m.positions)
