"""Pemetaan (mapping) data kader sistem DPW ke format formulir createKTA Digital NasDem DPP."""
import re
from datetime import date, datetime
from typing import Tuple

AGAMA_MAP = {
    "islam": "islam",
    "kristen": "kristen",
    "kristen protestan": "kristen",
    "protestan": "kristen",
    "katolik": "katholik",
    "katholik": "katholik",
    "hindu": "hindu",
    "buddha": "budha",
    "budha": "budha",
    "khonghucu": "kong hu cu",
    "konghucu": "kong hu cu",
    "kong hu cu": "kong hu cu",
    "aliran kepercayaan": "aliran kepercayaan",
}

STATUS_KAWIN_MAP = {
    "belum kawin": "B",
    "belum": "B",
    "b": "B",
    "kawin": "S",
    "sudah kawin": "S",
    "s": "S",
    "cerai hidup": "P",
    "cerai mati": "P",
    "pernah kawin": "P",
    "p": "P",
}

PENDIDIKAN_MAP = {
    "lain-lain": "0",
    "tidak/belum sekolah": "1",
    "sd": "2",
    "smp": "3",
    "sma": "4",
    "smu": "4",
    "slta": "4",
    "smk": "5",
    "d1": "6",
    "diploma i": "6",
    "d2": "7",
    "diploma ii": "7",
    "d3": "8",
    "diploma iii": "8",
    "d4": "9",
    "diploma iv": "9",
    "s1": "10",
    "sarjana": "10",
    "s2": "11",
    "pasca sarjana": "11",
    "s3": "12",
    "doktoral": "12",
}

JOB_MAP = {
    "belum/tidak bekerja": "Belum/Tidak Bekerja",
    "tidak bekerja": "Belum/Tidak Bekerja",
    "karyawan swasta": "SWASTA",
    "swasta": "SWASTA",
    "wiraswasta": "Wiraswasta",
    "wirausaha": "Wiraswasta",
    "pengusaha": "PENGUSAHA",
    "pegawai negeri sipil": "PNS",
    "pns": "PNS",
    "asn": "PNS",
    "tni": "TNI",
    "polri": "POLRI",
    "kepolisian ri": "POLRI",
    "petani": "PETANI",
    "petani/pekebun": "Petani / Pekebun",
    "petani / pekebun": "Petani / Pekebun",
    "peternak": "Peternak",
    "nelayan": "NELAYAN",
    "nelayan/perikanan": "Nelayan / Perikanan",
    "mengurus rumah tangga": "RT",
    "ibu rumah tangga": "RT",
    "irt": "RT",
    "rt": "RT",
    "pelajar/mahasiswa": "PELAJAR",
    "pelajar": "PELAJAR",
    "mahasiswa": "PELAJAR",
    "pensiunan": "Pensiunan",
    "pedagang": "Pedagang",
    "perdagangan": "Perdagangan",
    "karyawan bumn": "Karyawan BUMN",
    "karyawan bumd": "Karyawan BUMD",
    "karyawan honorer": "Karyawan Honorer",
    "buruh harian lepas": "Buruh Harian Lepas",
    "sopir": "Sopir",
    "guru": "Guru",
    "dosen": "Dosen",
    "dokter": "Dokter",
    "perawat": "Perawat",
    "bidan": "Bidan",
    "pengacara": "Pengacara",
    "notaris": "Notaris",
    "pendeta": "Pendeta",
    "pastor": "Pastur",
    "pastur": "Pastur",
    "wartawan": "Wartawan",
}

ROMAN_TO_WORD = [
    (r"\bVIII\b", "DELAPAN"),
    (r"\bVII\b", "TUJUH"),
    (r"\bVI\b", "ENAM"),
    (r"\bIV\b", "EMPAT"),
    (r"\bIX\b", "SEMBILAN"),
    (r"\bV\b", "LIMA"),
    (r"\bIII\b", "TIGA"),
    (r"\bII\b", "DUA"),
    (r"\bI\b", "SATU"),
    (r"\bX\b", "SEPULUH"),
    (r"\b1\b", "SATU"),
    (r"\b2\b", "DUA"),
    (r"\b3\b", "TIGA"),
    (r"\b4\b", "EMPAT"),
    (r"\b5\b", "LIMA"),
]


def normalize_wilayah_Token(name: str) -> str:
    """Normalisasi nama wilayah termasuk angka Romawi (misal 'Girian Weru I' -> 'GIRIAN WERU SATU')."""
    s = re.sub(r"[^A-Z0-9\s]", " ", (name or "").upper()).strip()
    for prefix in ("KABUPATEN ", "KAB ", "KOTA ", "KECAMATAN ", "KEC ", "KELURAHAN ", "KEL ", "DESA "):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
    for pat, repl in ROMAN_TO_WORD:
        s = re.sub(pat, repl, s)
    return re.sub(r"\s+", " ", s).strip()


def map_gender(val: str) -> str:
    v = (val or "").strip().upper()
    if v.startswith("P") or "WANITA" in v or "PEREMPUAN" in v:
        return "P"
    return "L"


def map_agama(val: str) -> str:
    v = (val or "").strip().lower()
    return AGAMA_MAP.get(v, "kristen")


def map_status_kawin(val: str) -> str:
    v = (val or "").strip().lower()
    return STATUS_KAWIN_MAP.get(v, "B")


def map_pendidikan(val: str) -> str:
    v = (val or "").strip().lower()
    if v in ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"):
        return v
    return PENDIDIKAN_MAP.get(v, "4")


def map_job(val: str) -> str:
    if not val:
        return "Wiraswasta"
    v = val.strip().lower()
    if v in JOB_MAP:
        return JOB_MAP[v]
    return "Wiraswasta"


def format_birth_date(val, nik: str = "") -> str:
    """Format tanggal lahir ke format d-m-Y yang diminta DPP (misal: 30-03-2005)."""
    if val:
        if isinstance(val, (date, datetime)):
            return val.strftime("%d-%m-%Y")
        s = str(val).strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            parts = s.split("-")
            return f"{parts[2]}-{parts[1]}-{parts[0]}"
        m = re.match(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$", s)
        if m:
            return f"{int(m.group(1)):02d}-{int(m.group(2)):02d}-{m.group(3)}"

    # Fallback cerdas ekstrak dari 16 digit NIK jika tanggal lahir kosong
    clean_nik = re.sub(r"\D", "", str(nik or ""))
    if len(clean_nik) == 16:
        try:
            dd = int(clean_nik[6:8])
            mm = int(clean_nik[8:10])
            yy = int(clean_nik[10:12])
            if dd > 40:
                dd -= 40
            year = 2000 + yy if yy <= 25 else 1900 + yy
            if 1 <= dd <= 31 and 1 <= mm <= 12:
                return f"{dd:02d}-{mm:02d}-{year}"
        except Exception:
            pass
    return "01-01-1990"


def parse_rt_rw(val: str, rt_override: str = None, rw_override: str = None) -> Tuple[str, str]:
    """Ekstrak nilai RT dan RW sesuai pilihan dropdown DPP ('0'..'999')."""
    if rt_override is not None and rw_override is not None:
        try:
            return str(max(0, int(rt_override))), str(max(0, int(rw_override)))
        except ValueError:
            pass
    s = str(val or "").strip()
    m = re.search(r"(\d{1,3})\s*[/|-]\s*(\d{1,3})", s)
    if m:
        rt = int(m.group(1))
        rw = int(m.group(2))
        return str(rt if rt > 0 else 1), str(rw if rw > 0 else 1)
    return "1", "1"


def generate_dpp_email(nik: str, custom_email: str = None) -> str:
    """Hasilkan email valid tanpa karakter '+' agar lolos validasi ketat portal DPP."""
    if custom_email and "@" in custom_email and "." in custom_email:
        clean = custom_email.strip().lower().replace("+", "")
        if not clean.endswith("@example.com") and len(clean) > 5:
            return clean
    clean_nik = re.sub(r"\D", "", str(nik or "")) or "kader"
    return f"ektasulut{clean_nik}@gmail.com"


def generate_dpp_password(nik: str, custom_password: str = None) -> str:
    """Password default kader baru di portal DPP (minimal 8 karakter): NasDem@{6digit}."""
    if custom_password and len(str(custom_password).strip()) >= 8:
        return str(custom_password).strip()
    clean_nik = re.sub(r"\D", "", str(nik or ""))
    suffix = clean_nik[-6:] if len(clean_nik) >= 6 else "2029"
    return f"NasDem@{suffix}"
