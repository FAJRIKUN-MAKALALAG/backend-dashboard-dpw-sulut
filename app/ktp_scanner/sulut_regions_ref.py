"""Referensi kode wilayah NIK Sulawesi Utara, kamus baku Dukcapil, dan algoritma koreksi silang NIK."""
import re
from datetime import date
from typing import Dict, Optional, Tuple

SULUT_NIK_PREFIX_TO_DPD: Dict[str, str] = {
    "7101": "Kabupaten Bolaang Mongondow",
    "7102": "Kabupaten Minahasa",
    "7103": "Kabupaten Kepulauan Sangihe",
    "7104": "Kabupaten Kepulauan Talaud",
    "7105": "Kabupaten Minahasa Selatan",
    "7106": "Kabupaten Minahasa Utara",
    "7107": "Kabupaten Minahasa Tenggara",
    "7108": "Kabupaten Bolaang Mongondow Utara",
    "7109": "Kabupaten Kepulauan Siau Tagulandang Biaro",
    "7110": "Kabupaten Bolaang Mongondow Timur",
    "7111": "Kabupaten Bolaang Mongondow Selatan",
    "7171": "Kota Manado",
    "7172": "Kota Bitung",
    "7173": "Kota Tomohon",
    "7174": "Kota Kotamobagu",
}

DPD_NAME_TO_NIK_PREFIX: Dict[str, str] = {
    v.upper(): k for k, v in SULUT_NIK_PREFIX_TO_DPD.items()
}

STANDARD_KTP_PEKERJAAN = [
    "Belum/Tidak Bekerja",
    "Mengurus Rumah Tangga",
    "Pelajar/Mahasiswa",
    "Pensiunan",
    "Pegawai Negeri Sipil",
    "TNI",
    "Kepolisian RI",
    "Perdagangan",
    "Petani/Pekebun",
    "Peternak",
    "Nelayan/Perikanan",
    "Industri",
    "Konstruksi",
    "Transportasi",
    "Karyawan Swasta",
    "Karyawan BUMN",
    "Karyawan BUMD",
    "Karyawan Honorer",
    "Buruh Harian Lepas",
    "Buruh Tani/Perkebunan",
    "Buruh Nelayan/Perikanan",
    "Buruh Peternakan",
    "Pembantu Rumah Tangga",
    "Tukang Cukur",
    "Tukang Listrik",
    "Tukang Batu",
    "Tukang Kayu",
    "Tukang Las/Pandai Besi",
    "Tukang Jahit",
    "Mekanik",
    "Seniman",
    "Pendeta",
    "Pastor",
    "Wartawan",
    "Ustadz/Mubaligh",
    "Juru Masak",
    "Dosen",
    "Guru",
    "Pengacara",
    "Notaris",
    "Arsitek",
    "Akuntan",
    "Konsultan",
    "Dokter",
    "Bidan",
    "Perawat",
    "Apoteker",
    "Pelaut",
    "Peneliti",
    "Sopir",
    "Perangkat Desa",
    "Kepala Desa",
    "Wiraswasta",
    "Wirausaha",
    "Pedagang",
]

OCR_DIGIT_MAP = str.maketrans({
    "O": "0",
    "o": "0",
    "Q": "0",
    "D": "0",
    "C": "0",
    "U": "0",
    "I": "1",
    "l": "1",
    "|": "1",
    "!": "1",
    "L": "1",
    "i": "1",
    "j": "1",
    "Z": "2",
    "z": "2",
    "E": "3",
    "A": "4",
    "S": "5",
    "s": "5",
    "$": "5",
    "G": "6",
    "b": "6",
    "T": "7",
    "?": "7",
    "J": "7",
    "B": "8",
    "&": "8",
    "g": "9",
    "q": "9",
})


def normalize_ocr_digits(text: str) -> str:
    return (text or "").translate(OCR_DIGIT_MAP)


def string_similarity(a: str, b: str) -> float:
    s1 = re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9\s]", "", (a or "").upper())).strip()
    s2 = re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9\s]", "", (b or "").upper())).strip()
    if not s1 or not s2:
        return 0.0
    if s1 == s2:
        return 1.0

    len1, len2 = len(s1), len(s2)
    max_len = max(len1, len2)
    dp = list(range(len2 + 1))
    for i in range(1, len1 + 1):
        prev = dp[0]
        dp[0] = i
        for j in range(1, len2 + 1):
            temp = dp[j]
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1, prev + cost)
            prev = temp
    return 1.0 - (dp[len2] / max_len)


def match_standard_pekerjaan(raw_val: str) -> str:
    cleaned = re.sub(
        r"\b(KEWARGANEGARAAN|WNI|BERLAKU|SEUMUR|HIDUP|GOL|DARAH)\b.*$",
        "",
        raw_val or "",
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"[^a-zA-Z/\s.-]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) < 2:
        return ""

    upper = cleaned.upper()
    if re.search(r"PNS|PEGAWAI\s*NEGERI", upper):
        return "Pegawai Negeri Sipil"
    if re.search(r"RUMAH\s*TANGGA|IRT|MENGURUS", upper):
        return "Mengurus Rumah Tangga"
    if re.search(r"PELAJAR|MAHASISWA", upper):
        return "Pelajar/Mahasiswa"
    if re.search(r"BELUM.*BEKERJA|TIDAK.*BEKERJA", upper):
        return "Belum/Tidak Bekerja"
    if "WIRASWASTA" in upper:
        return "Wiraswasta"
    if "WIRAUSAHA" in upper:
        return "Wirausaha"
    if "SWASTA" in upper:
        return "Karyawan Swasta"
    if re.search(r"PETANI|PEKEBUN", upper):
        return "Petani/Pekebun"
    if "NELAYAN" in upper:
        return "Nelayan/Perikanan"
    if "HONORER" in upper:
        return "Karyawan Honorer"

    best_job = ""
    best_score = 0.0
    for job in STANDARD_KTP_PEKERJAAN:
        score = string_similarity(cleaned, job)
        if score > best_score:
            best_score = score
            best_job = job

    if best_score >= 0.64:
        return best_job
    return cleaned.title()


def infer_sulut_prefix_from_header(kab_kota_text: str) -> Optional[str]:
    if not kab_kota_text:
        return None
    upper = kab_kota_text.upper()
    for dpd_upper, code in DPD_NAME_TO_NIK_PREFIX.items():
        short_name = re.sub(r"^(KABUPATEN|KOTA)\s+", "", dpd_upper)
        if short_name in upper or string_similarity(short_name, upper) >= 0.75:
            return code
    return None


def decode_nik_info(nik: str) -> dict:
    clean = re.sub(r"\D", "", nik or "")
    if len(clean) != 16:
        return {"isValidFormat": False}

    kab_code = clean[:4]
    dpd_name = SULUT_NIK_PREFIX_TO_DPD.get(kab_code)
    tempat_suggestion = (
        re.sub(r"^(Kabupaten|Kota)\s+", "", dpd_name, flags=re.IGNORECASE)
        if dpd_name
        else None
    )

    try:
        dd = int(clean[6:8])
        mm = int(clean[8:10])
        yy = int(clean[10:12])
    except ValueError:
        return {"isValidFormat": False, "dpdName": dpd_name}

    jenis_kelamin = None
    if 41 <= dd <= 71:
        jenis_kelamin = "Perempuan"
        dd -= 40
    elif 1 <= dd <= 31:
        jenis_kelamin = "Laki-laki"

    tanggal_lahir = None
    if 1 <= dd <= 31 and 1 <= mm <= 12:
        curr_yy = date.today().year % 100
        full_year = 2000 + yy if yy <= curr_yy - 15 else 1900 + yy
        tanggal_lahir = f"{full_year:04d}-{mm:02d}-{dd:02d}"

    return {
        "isValidFormat": bool(jenis_kelamin and tanggal_lahir),
        "dpdName": dpd_name,
        "tempatLahirSuggestion": tempat_suggestion,
        "tanggalLahir": tanggal_lahir,
        "jenisKelamin": jenis_kelamin,
    }


def heal_nik_with_cross_fields(
    raw_nik: str,
    tanggal_lahir: str,
    jenis_kelamin: str,
    kab_kota_text: str,
) -> str:
    """Memperbaiki digit NIK yang tertukar oleh OCR menggunakan informasi Kabupaten/Kota, Tanggal Lahir & Jenis Kelamin."""
    clean = re.sub(r"\D", "", normalize_ocr_digits(raw_nik or ""))
    if len(clean) < 14:
        return clean

    clean = clean[:16]
    header_prefix = infer_sulut_prefix_from_header(kab_kota_text)

    # 1. Perbaiki 4 digit awal (Kode Provinsi 71 + Kab/Kota Sulut)
    if len(clean) == 16:
        if header_prefix and clean[:4] != header_prefix:
            # Jika selisih hanya 1-2 digit di 4 angka awal, gunakan prefix resmi dari header KTP
            diff_count = sum(1 for a, b in zip(clean[:4], header_prefix) if a != b)
            if diff_count <= 2:
                clean = header_prefix + clean[4:]
        elif not clean.startswith("71"):
            possible = "71" + clean[2:4]
            if possible in SULUT_NIK_PREFIX_TO_DPD:
                clean = possible + clean[4:]

    # 2. Perbaiki digit 7-12 (DDMMYY) jika tanggal_lahir (YYYY-MM-DD) terbaca jelas dari baris TTL
    if len(clean) == 16 and tanggal_lahir and re.match(r"^\d{4}-\d{2}-\d{2}$", tanggal_lahir):
        parts = tanggal_lahir.split("-")
        yy_str = parts[0][2:4]
        mm_str = parts[1]
        dd_num = int(parts[2])

        nik_dd_raw = int(clean[6:8]) if clean[6:8].isdigit() else 0
        is_female = jenis_kelamin == "Perempuan" or (not jenis_kelamin and 41 <= nik_dd_raw <= 71)
        expected_dd = f"{dd_num + 40:02d}" if is_female else f"{dd_num:02d}"
        expected_ddmmyy = f"{expected_dd}{mm_str}{yy_str}"

        current_ddmmyy = clean[6:12]
        if current_ddmmyy != expected_ddmmyy:
            diff_digits = sum(1 for a, b in zip(current_ddmmyy, expected_ddmmyy) if a != b)
            # Jika digit 7-12 saat ini tidak valid ATAU hanya beda 1-3 digit akibat salah baca OCR, koreksi!
            decoded_curr = decode_nik_info(clean)
            if not decoded_curr.get("isValidFormat") or diff_digits <= 3:
                clean = clean[:6] + expected_ddmmyy + clean[12:]

    return clean
