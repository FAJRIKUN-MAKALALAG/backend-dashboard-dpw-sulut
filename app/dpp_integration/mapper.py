"""Pemetaan (mapping) data kader sistem DPW ke format formulir createKTA Digital NasDem DPP."""
import re
from datetime import date, datetime
from typing import Dict, Tuple

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
    "tni": "TNI",
    "polri": "POLRI",
    "kepolisian ri": "POLRI",
    "petani": "PETANI",
    "petani/pekebun": "Petani / Pekebun",
    "peternak": "Peternak",
    "nelayan": "NELAYAN",
    "nelayan/perikanan": "Nelayan / Perikanan",
    "mengurus rumah tangga": "RT",
    "ibu rumah tangga": "RT",
    "irt": "RT",
    "pelajar/mahasiswa": "PELAJAR",
    "pelajar": "PELAJAR",
    "mahasiswa": "PELAJAR",
    "pensiunan": "Pensiunan",
    "pedagang": "Pedagang",
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


def map_job(val: str) -> str:
    v = (val or "").strip().lower()
    return JOB_MAP.get(v, "Wiraswasta")


def format_birth_date(val) -> str:
    """Format tanggal lahir ke format d-m-Y yang diminta DPP (misal: 15-08-1985)."""
    if not val:
        return "01-01-1990"
    if isinstance(val, (date, datetime)):
        return val.strftime("%d-%m-%Y")
    s = str(val).strip()
    # Jika YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        parts = s.split("-")
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    # Jika DD-MM-YYYY atau DD/MM/YYYY
    m = re.match(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$", s)
    if m:
        return f"{int(m.group(1)):02d}-{int(m.group(2)):02d}-{m.group(3)}"
    return "01-01-1990"


def parse_rt_rw(val: str) -> Tuple[str, str]:
    """Ekstrak nilai RT dan RW (misal '002/004' -> ('2', '4'))."""
    s = str(val or "").strip()
    m = re.search(r"(\d{1,3})\s*[/|-]\s*(\d{1,3})", s)
    if m:
        rt = str(int(m.group(1)))
        rw = str(int(m.group(2)))
        return rt, rw
    return "0", "0"


def generate_dpp_email(nik: str, custom_email: str = None) -> str:
    """Jika kader belum memiliki email resmi, buatkan alias terpusat menggunakan NIK."""
    if custom_email and "@" in custom_email and "." in custom_email:
        clean = custom_email.strip().lower()
        if not clean.endswith("@example.com"):
            return clean
    clean_nik = re.sub(r"\D", "", str(nik or "")) or "kader"
    return f"ektasulut+{clean_nik}@gmail.com"


def generate_dpp_password(nik: str, tgl_lahir: str = None) -> str:
    """Password default kader baru di portal DPP: NasDem@{6digit}."""
    clean_nik = re.sub(r"\D", "", str(nik or ""))
    suffix = clean_nik[-6:] if len(clean_nik) >= 6 else "2029"
    return f"NasDem@{suffix}"
