"""Parser Spasial Berbasis Koordinat Bounding-Box (X, Y) & Koreksi Kemiringan (De-Skew) untuk e-KTP Indonesia."""
import re
from typing import Any, Dict, List, Optional, Tuple

from app.ktp_scanner.sulut_regions_ref import (
    SULUT_NIK_PREFIX_TO_DPD,
    decode_nik_info,
    heal_nik_with_cross_fields,
    infer_sulut_prefix_from_header,
    match_standard_pekerjaan,
    normalize_ocr_digits,
)


def _box_metrics(box: List[List[float]]) -> Tuple[float, float, float, float, float, float, float]:
    xs = [pt[0] for pt in box]
    ys = [pt[1] for pt in box]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    x_center = (x_min + x_max) / 2.0
    y_center = (y_min + y_max) / 2.0
    height = max(1.0, y_max - y_min)
    return x_min, x_max, y_min, y_max, x_center, y_center, height


def _estimate_text_slope(parsed_boxes: List[Dict[str, Any]]) -> float:
    """Menghitung kemiringan rata-rata (slope dy/dx) dari kotak-kotak teks horizontal pada e-KTP."""
    slopes: List[float] = []
    for b in parsed_boxes:
        box = b.get("raw_box")
        if not box or len(box) < 2:
            continue
        dx = float(box[1][0] - box[0][0])
        dy = float(box[1][1] - box[0][1])
        if dx >= 75.0:
            slope = dy / dx
            if -0.18 <= slope <= 0.18:
                slopes.append(slope)
    if not slopes:
        return 0.0
    slopes.sort()
    return slopes[len(slopes) // 2]


def cluster_boxes_into_rows(ocr_items: List[Any]) -> List[Dict[str, Any]]:
    """Mengelompokkan bounding box hasil OCR menjadi baris-baris horizontal dengan kompensasi kemiringan (slope)."""
    parsed_boxes: List[Dict[str, Any]] = []
    for item in ocr_items or []:
        if not item or len(item) < 2:
            continue
        box = item[0]
        text = str(item[1] or "").strip()
        conf = float(item[2]) if len(item) > 2 and item[2] is not None else 0.85
        if not text:
            continue
        x_min, x_max, y_min, y_max, x_center, y_center, height = _box_metrics(box)
        parsed_boxes.append(
            {
                "raw_box": box,
                "text": text,
                "conf": conf,
                "x_min": x_min,
                "x_max": x_max,
                "y_min": y_min,
                "y_max": y_max,
                "x_center": x_center,
                "y_center": y_center,
                "height": height,
            }
        )

    slope = _estimate_text_slope(parsed_boxes)
    for b in parsed_boxes:
        b["y_norm"] = b["y_center"] - slope * (b["x_center"] - 500.0)

    parsed_boxes.sort(key=lambda b: b["y_norm"])

    rows: List[Dict[str, Any]] = []
    for b in parsed_boxes:
        placed = False
        for row in rows:
            tol = max(16.0, row["avg_height"] * 0.65)
            if abs(b["y_norm"] - row["y_norm"]) <= tol:
                row["boxes"].append(b)
                n = len(row["boxes"])
                row["y_norm"] = sum(x["y_norm"] for x in row["boxes"]) / n
                row["y_center"] = sum(x["y_center"] for x in row["boxes"]) / n
                row["avg_height"] = sum(x["height"] for x in row["boxes"]) / n
                placed = True
                break
        if not placed:
            rows.append(
                {
                    "y_norm": b["y_norm"],
                    "y_center": b["y_center"],
                    "avg_height": b["height"],
                    "boxes": [b],
                }
            )

    for row in rows:
        row["boxes"].sort(key=lambda x: x["x_min"])
        # Filter kotak titik dua ':' tunggal yang sering terbaca sebagai huruf 'i', 'l', '1', '|'
        filtered_right_boxes = []
        for x in row["boxes"]:
            box_w = x["x_max"] - x["x_min"]
            txt_clean = x["text"].strip()
            is_colon_noise = (
                240 <= x["x_min"] <= 365
                and box_w < 32
                and txt_clean in (":", ";", ".", "-", "i", "l", "1", "I", "|", "!")
            )
            if is_colon_noise:
                continue
            if x["x_min"] >= 210 or x["x_max"] >= 340:
                filtered_right_boxes.append(x)

        row["line_text"] = " ".join(x["text"] for x in row["boxes"]).strip()
        row["right_value_text"] = " ".join(x["text"] for x in filtered_right_boxes).strip()
        row["right_conf"] = (
            sum(x["conf"] for x in filtered_right_boxes) / len(filtered_right_boxes)
            if filtered_right_boxes
            else 0.0
        )

    return rows


def _strip_label(line: str, pattern: str) -> str:
    cleaned = re.sub(pattern, "", line, flags=re.IGNORECASE)
    # Buang karakter titik dua ':' atau salah baca titik dua ('i ', 'l ', '1 ', '| ') di awal nilai
    cleaned = re.sub(r"^[\s:;.\-=_/\\|!]+", "", cleaned)
    cleaned = re.sub(r"^(?:[il1|!]\s+)(?=[A-Z])", "", cleaned)
    cleaned = re.sub(r"^[il1|!](?=[A-Z]{2,})", "", cleaned)
    cleaned = re.sub(r"[\s:;.\-=_/\\|!]+$", "", cleaned)
    return cleaned.strip()


def _digit_to_alpha_in_words(text: str) -> str:
    """Mengubah angka yang terselip di dalam kata huruf (misal MANAD0 -> MANADO, FAJR1 -> FAJRI)
    tanpa mengubah angka yang berdiri sendiri.
    """
    if not text:
        return ""
    mapping = {"0": "O", "1": "I", "4": "A", "5": "S", "8": "B", "6": "G"}

    def _repl(m: re.Match) -> str:
        ch = m.group(0)
        return mapping.get(ch, ch)

    return re.sub(r"(?<=[A-Za-z])[014586]|[014586](?=[A-Za-z])", _repl, text)


def _clean_person_name(raw: str) -> str:
    if not raw:
        return ""
    # 1. Bersihkan artefak titik dua ':' di awal sebelum konversi angka
    s = raw.strip()
    s = re.sub(r"^[\s:;.\-=_/\\|!]+", "", s)
    s = re.sub(r"^(?:[il1|!]\s+)(?=[A-Za-z])", "", s)
    s = re.sub(r"^[il1|!](?=[A-Z]{2,})", "", s)

    # 2. Hapus label KTP yang mungkin ikut terbawa di awal/akhir baris
    s = re.sub(
        r"^.*?\b(NAMA|NANA|NARNA|NAMO|NEMA|MAMA|HAMA|NAM4|NAWA|NMA|KAMA)\b[\s:;.\-=_/\\|!1il]*",
        "",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"\b(PROVINSI|KABUPATEN|KOTA|NIK|NAMA|TEMPAT|TGL|LAHIR|JENIS|KELAMIN|GOL|DARAH|ALAMAT|AGAMA)\b.*$",
        "",
        s,
        flags=re.IGNORECASE,
    )

    # 3. Konversi angka yang terjepit di dalam kata huruf
    s = _digit_to_alpha_in_words(s)

    # 4. Pertahankan huruf, spasi, titik, koma, tanda petik, dan tanda hubung nama
    s = re.sub(r"[^a-zA-Z\s.,'\-]", " ", s)
    s = re.sub(r"^[\s.,'\-]+|[\s.,'\-]+$", "", s)
    s = re.sub(r"\s+", " ", s).strip().upper()
    return s


def extract_best_nik_from_ocr(
    main_rows: List[Dict[str, Any]],
    nik_strip_items: List[Any],
) -> Tuple[str, int]:
    """Mencari kandidat 16 digit NIK terbaik dari strip khusus NIK dan baris utama."""
    candidates: List[str] = []
    nik_row_idx = -1

    # 1. Dari strip khusus NIK (diperbesar 1.75x)
    strip_rows = cluster_boxes_into_rows(nik_strip_items)
    for r in strip_rows:
        line = r["line_text"]
        cleaned = re.sub(r"\b(NIK|N1K|HIK|MIK|PROVINSI|KABUPATEN|KOTA)\b", " ", line, flags=re.IGNORECASE)
        norm = normalize_ocr_digits(cleaned)
        for m in re.findall(r"\d[\d\s]{12,20}\d", norm):
            digits = re.sub(r"\D", "", m)
            if len(digits) >= 14:
                candidates.append(digits[:16])

    # 2. Dari baris utama
    for idx, r in enumerate(main_rows):
        line = r["line_text"]
        has_nik_word = bool(re.search(r"\b(NIK|N1K|HIK|MIK)\b", line, re.IGNORECASE))
        cleaned = re.sub(r"\b(NIK|N1K|HIK|MIK)\b", " ", line, flags=re.IGNORECASE)
        norm = normalize_ocr_digits(cleaned)
        matches = re.findall(r"\d[\d\s]{12,20}\d", norm)
        if matches:
            if nik_row_idx == -1:
                nik_row_idx = idx
            for m in matches:
                digits = re.sub(r"\D", "", m)
                if len(digits) >= 14:
                    candidates.append(digits[:16])
        elif has_nik_word and nik_row_idx == -1:
            nik_row_idx = idx

    if not candidates:
        return "", nik_row_idx

    best_nik = candidates[0]
    best_score = -1
    for cand in candidates:
        score = 0
        if len(cand) == 16:
            score += 50
        elif len(cand) == 15:
            score += 25
        if cand.startswith("71"):
            score += 30
        if cand[:4] in SULUT_NIK_PREFIX_TO_DPD:
            score += 35
        info = decode_nik_info(cand)
        if info.get("isValidFormat"):
            score += 45
        if score > best_score:
            best_score = score
            best_nik = cand

    return best_nik[:16], nik_row_idx


def _extract_name_from_row(row: Dict[str, Any]) -> Tuple[str, float]:
    """Mengekstrak nama kader dari sebuah baris, mengutamakan kotak di kolom nilai (sebelah kanan titik dua)."""
    # Ambil kotak yang murni berada di kolom nilai (x_min >= 235)
    pure_value_boxes = [
        b for b in row["boxes"]
        if b["x_min"] >= 235 and not (b["x_max"] - b["x_min"] < 32 and b["text"].strip() in (":", ";", ".", "-", "i", "l", "1", "I", "|", "!"))
    ]
    if pure_value_boxes:
        val_text = " ".join(b["text"] for b in pure_value_boxes).strip()
        avg_conf = sum(b["conf"] for b in pure_value_boxes) / len(pure_value_boxes)
        cleaned = _clean_person_name(val_text)
        if len(cleaned) >= 2:
            return cleaned, avg_conf

    # Jika label "Nama" dan isi tergabung dalam 1 kotak panjang
    line = row["line_text"]
    val = _strip_label(
        line,
        r"^.*?\b(NAMA|NANA|NARNA|NAMO|NEMA|MAMA|HAMA|NAM4|NAWA|NMA|KAMA)\b",
    )
    cleaned = _clean_person_name(val)
    avg_conf = sum(b["conf"] for b in row["boxes"]) / max(1, len(row["boxes"]))
    return cleaned, avg_conf


def parse_ktp_spatial_data(
    main_ocr_items: List[Any],
    nik_strip_items: List[Any],
    biodata_strip_items: Optional[List[Any]] = None,
    segmented_nama: Optional[str] = None,
) -> Dict[str, Any]:
    """Mengekstrak seluruh field e-KTP menggunakan koordinat spasial (X, Y) & validasi silang NIK."""
    rows = cluster_boxes_into_rows(main_ocr_items)
    raw_nik, nik_row_idx = extract_best_nik_from_ocr(rows, nik_strip_items)

    nama_lengkap = _clean_person_name(segmented_nama) if segmented_nama else ""
    nama_conf = 0.98 if nama_lengkap else 0.0
    tempat_lahir = ""
    tanggal_lahir = ""
    jenis_kelamin = ""
    alamat = ""
    rt_rw = ""
    kel_desa = ""
    kecamatan = ""
    kab_kota = ""
    agama = ""
    status_perkawinan = ""
    pekerjaan = ""

    ttl_row_idx = -1
    alamat_row_idx = -1

    for idx, row in enumerate(rows):
        line = row["line_text"]
        right_val = row["right_value_text"]
        upper = line.upper()

        # Header Kabupaten / Kota (mendukung teks tersambung seperti KABUPATENMINAHASASELATAN)
        if not kab_kota and ("KABUPATEN" in upper or "KOTA" in upper) and "PROVINSI" not in upper:
            prefix = infer_sulut_prefix_from_header(upper)
            if prefix and prefix in SULUT_NIK_PREFIX_TO_DPD:
                kab_kota = SULUT_NIK_PREFIX_TO_DPD[prefix]
            else:
                m_kab = re.search(r"(KABUPATEN|KOTA)\s*[A-Z\s]+", upper)
                if m_kab:
                    kab_kota = m_kab.group(0).strip().title()

        # Nama Lengkap
        if (
            not nama_lengkap
            and re.search(r"\b(NAMA|NANA|NARNA|NAMO|NEMA|MAMA|HAMA|NAM4|NAWA|NMA|KAMA)\b", upper)
            and not re.search(r"AGAMA|KECAMATAN|KEWARGANEGARAAN", upper)
        ):
            cand_name, cand_conf = _extract_name_from_row(row)
            if len(cand_name) >= 2:
                nama_lengkap = cand_name
                nama_conf = cand_conf

        # Tempat / Tgl Lahir (mendukung format tanggal tanpa pemisah seperti 30-032005 atau 30032005)
        has_ttl_label = bool(
            re.search(r"(TEMPAT|TEMPAL|TMP|TGL|TGI|LAHIR|LAHL|TTL)", upper)
        )
        norm_line = normalize_ocr_digits(line)
        date_match = re.search(
            r"(\d{2})[\s\-/.,]*(\d{2})[\s\-/.,]*((?:19|20)\d{2})",
            norm_line,
        )
        if (not tempat_lahir or not tanggal_lahir) and (has_ttl_label or date_match):
            ttl_row_idx = idx
            val = (
                _strip_label(
                    line,
                    r"^.*?(LAHIR|LAHL|TGL\s*LAHIR|TGI\s*LAHIR|TEMPAT\s*/?\s*TGL\s*LAHIR|TEMPAT|TTL)",
                )
                if has_ttl_label
                else (right_val or line)
            )
            val_norm = normalize_ocr_digits(val)
            dm = re.search(
                r"(\d{2})[\s\-/.,]*(\d{2})[\s\-/.,]*((?:19|20)\d{2})",
                val_norm,
            )
            if dm:
                d_str, m_str, y_str = dm.group(1), dm.group(2), dm.group(3)
                if 1 <= int(d_str) <= 31 and 1 <= int(m_str) <= 12:
                    tanggal_lahir = f"{y_str}-{m_str}-{d_str}"
                before_date = val[: dm.start()]
                before_date = re.sub(
                    r"^.*?(LAHIR|TGL|TEMPAT)[:\s]*", "", before_date, flags=re.IGNORECASE
                )
                before_date = _digit_to_alpha_in_words(before_date)
                before_date = re.sub(r"[^a-zA-Z\s]", " ", before_date)
                before_date = re.sub(r"\s+", " ", before_date).strip()
                if len(before_date) >= 2:
                    tempat_lahir = before_date.title()
            elif has_ttl_label:
                parts = re.split(r"[,:]", val)
                cand_place = _digit_to_alpha_in_words(parts[0] if parts else "")
                cand_place = re.sub(r"[^a-zA-Z\s]", "", cand_place).strip()
                if len(cand_place) >= 2:
                    tempat_lahir = cand_place.title()

        # Jenis Kelamin (mendukung LAKAKI / LAKI-LAKI / PEREMPUAN)
        if not jenis_kelamin and re.search(
            r"(JENIS\s*KELAMIN|KELAMIN|KELAM1N|LAKI|LAK[A-Z\-]*KI|PEREMPUAN|PERENPUAN|WANITA|PRIA)",
            upper,
        ):
            if re.search(r"PEREMPUAN|PERENPUAN|WANITA", upper):
                jenis_kelamin = "Perempuan"
            elif re.search(r"LAKI|LAK[A-Z\-]*KI|PRIA", upper):
                jenis_kelamin = "Laki-laki"

        # Alamat
        if not alamat and re.search(r"\b(ALAMAT|ALANAT|ALAMAL|ALA\s*MAT)\b", upper):
            alamat_row_idx = idx
            val = _strip_label(line, r"^.*?\b(ALAMAT|ALANAT|ALAMAL|ALA\s*MAT)\b")
            val = re.sub(r"\bGOL\.?\s*DARAH.*$", "", val, flags=re.IGNORECASE).strip()
            if len(val) < 2 and right_val:
                val = right_val
            if len(val) >= 2:
                alamat = val.title()

        # RT / RW
        if not rt_rw and (
            re.search(r"(RT\s*/?\s*RW|RTRW|RTIRW|BTIRW|RT\s+RW)", upper)
            or re.search(r"\b00\d\s*[/|-]\s*00\d\b", norm_line)
        ):
            rt_m = re.search(r"(\d{1,3})\s*[/|-]\s*(\d{1,3})", norm_line)
            if rt_m:
                r_num = int(rt_m.group(1))
                w_num = int(rt_m.group(2))
                if r_num >= 100 and str(r_num).endswith("00"):
                    r_num = 0
                if w_num >= 100 and str(w_num).endswith("00"):
                    w_num = 0
                rt_rw = f"{r_num:03d}/{w_num:03d}"

        # Kel/Desa
        if not kel_desa and re.search(
            r"(KEL\s*/?\s*DESA|KEI\s*/?\s*DESA|KEL\s*/?\s*DOSA|KELURAHAN|DESA|KEVDESA)",
            upper,
        ):
            val = _strip_label(
                line,
                r"^.*?(KEL\s*/?\s*DESA|KEI\s*/?\s*DESA|KEL\s*/?\s*DOSA|KELURAHAN|DESA|KEVDESA)",
            )
            val = re.sub(r"[^a-zA-Z0-9\s]", " ", val)
            val = re.sub(r"\s+", " ", val).strip()
            if len(val) >= 2:
                kel_desa = val.title()

        # Kecamatan (mendukung Kecainatan / Kecaatan / Kecanatan / Kecamatan)
        if not kecamatan and re.search(
            r"(KECAMATAN|KECANATAN|KECAMALAN|KECA[A-Z]{1,6}N|KECA\s*MATAN|KEC\.)", upper
        ):
            val = _strip_label(
                line,
                r"^.*?(KECAMATAN|KECANATAN|KECAMALAN|KECA[A-Z]{1,6}N|KECA\s*MATAN|KEC\.)",
            )
            val = re.sub(r"[^a-zA-Z0-9\s]", " ", val)
            val = re.sub(r"\s+", " ", val).strip()
            if len(val) >= 2:
                kecamatan = val.title()

        # Agama
        if not agama and re.search(
            r"(AGAMA|AGANA|AQAMA|ISLAM|1SLAM|KRISTEN|KR1STEN|PROTESTAN|KATOLIK|KATHOLIK|HINDU|BUDDHA|BUDHA|KHONGHUCU|KONGHUCU)",
            upper,
        ):
            if re.search(r"KRISTEN|KR1STEN|PROTESTAN", upper):
                agama = "Kristen Protestan"
            elif re.search(r"KATOLIK|KATHOLIK", upper):
                agama = "Katolik"
            elif re.search(r"ISLAM|1SLAM|ISLAN", upper):
                agama = "Islam"
            elif "HINDU" in upper:
                agama = "Hindu"
            elif re.search(r"BUDDHA|BUDHA", upper):
                agama = "Buddha"
            elif re.search(r"KHONGHUCU|KONGHUCU", upper):
                agama = "Khonghucu"

        # Status Perkawinan
        if not status_perkawinan and re.search(
            r"(PERKAWINAN|PERKAW1NAN|KAWIN|KAW1N|CERAI)", upper
        ):
            if re.search(r"BELUM|BLN|BLM", upper):
                status_perkawinan = "Belum Kawin"
            elif re.search(r"CERAI\s*HIDUP", upper):
                status_perkawinan = "Cerai Hidup"
            elif re.search(r"CERAI\s*MATI", upper):
                status_perkawinan = "Cerai Mati"
            elif re.search(r"KAWIN|KAW1N", upper):
                status_perkawinan = "Kawin"

        # Pekerjaan
        if not pekerjaan and re.search(
            r"(PEKERJAAN|PEKERIAAN|PEKERLAAN|PEKERJ4AN)", upper
        ):
            val = _strip_label(line, r"^.*?(PEKERJAAN|PEKERIAAN|PEKERLAAN|PEKERJ4AN)")
            matched_job = match_standard_pekerjaan(val or right_val)
            if matched_job:
                pekerjaan = matched_job

    # Fallback Spasial untuk Nama Lengkap (Baris di antara NIK dan TTL pada kolom nilai kanan)
    if not nama_lengkap:
        search_indices = []
        if nik_row_idx >= 0 and ttl_row_idx > nik_row_idx:
            search_indices = list(range(nik_row_idx + 1, ttl_row_idx))
        elif ttl_row_idx > 0:
            search_indices = [ttl_row_idx - 1]
        elif nik_row_idx >= 0 and nik_row_idx + 1 < len(rows):
            search_indices = [nik_row_idx + 1]

        for c_idx in search_indices:
            if 0 <= c_idx < len(rows):
                cand_name, cand_conf = _extract_name_from_row(rows[c_idx])
                if len(cand_name) >= 3 and not re.search(r"\d{4,}", rows[c_idx]["line_text"]):
                    nama_lengkap = cand_name
                    nama_conf = cand_conf
                    break

    # Ensemble / Perbandingan dengan Pass Kedua jika segmented_nama belum tersedia
    if biodata_strip_items and not segmented_nama:
        bio_rows = cluster_boxes_into_rows(biodata_strip_items)
        for b_idx, b_row in enumerate(bio_rows):
            b_upper = b_row["line_text"].upper()
            if (
                re.search(r"\b(NAMA|NANA|NARNA|NAMO|NEMA|MAMA|HAMA|NAM4|NAWA|NMA|KAMA)\b", b_upper)
                and not re.search(r"AGAMA|KECAMATAN|KEWARGANEGARAAN", b_upper)
            ):
                strip_name, strip_conf = _extract_name_from_row(b_row)
                if len(strip_name) >= 3:
                    # Pilih hasil strip resolusi tinggi jika nama utama kosong, lebih pendek/terpotong, atau conf strip lebih unggul
                    if (
                        not nama_lengkap
                        or strip_conf >= nama_conf - 0.02
                        or len(strip_name) > len(nama_lengkap)
                    ):
                        nama_lengkap = strip_name
                        nama_conf = max(nama_conf, strip_conf)
                break
        else:
            # Jika label "Nama" tidak terbaca pada strip, cek baris pertama di atas baris TTL pada strip
            for b_idx, b_row in enumerate(bio_rows):
                if re.search(r"\b(TEMPAT|TGL|LAHIR)\b", b_row["line_text"].upper()) and b_idx > 0:
                    strip_name, strip_conf = _extract_name_from_row(bio_rows[b_idx - 1])
                    if len(strip_name) >= 3 and (not nama_lengkap or strip_conf > nama_conf + 0.03):
                        nama_lengkap = strip_name
                    break

    # Fallback Pekerjaan dari kamus baku jika label "Pekerjaan" tidak terbaca
    if not pekerjaan:
        for row in rows:
            line = row["line_text"]
            if re.search(
                r"\b(WIRASWASTA|KARYAWAN|MENGURUS\s*RUMAH\s*TANGGA|PELAJAR|MAHASISWA|PEGAWAI\s*NEGERI|PETANI|PEKEBUN|NELAYAN|BELUM\s*/?\s*TIDAK\s*BEKERJA|PENSIUNAN|PENDETA|GURU|SOPIR|PEDAGANG)\b",
                line,
                re.IGNORECASE,
            ):
                pekerjaan = match_standard_pekerjaan(line)
                if pekerjaan:
                    break

    # Koreksi Silang (Cross-Healing) antara NIK, Tanggal Lahir, Jenis Kelamin, dan Header Kab/Kota
    healed_nik = heal_nik_with_cross_fields(raw_nik, tanggal_lahir, jenis_kelamin, kab_kota)
    nik_info = decode_nik_info(healed_nik)

    if nik_info.get("isValidFormat"):
        nik_dob = nik_info.get("tanggalLahir")
        if not tanggal_lahir and nik_dob:
            tanggal_lahir = nik_dob
        elif tanggal_lahir and nik_dob and tanggal_lahir[4:] == nik_dob[4:] and tanggal_lahir[:4] != nik_dob[:4]:
            # Jika tanggal & bulan cocok tetapi tahun di TTL selisih 1 digit (misal 1998 vs 1988), gunakan tahun dari NIK
            tanggal_lahir = nik_dob
        if not jenis_kelamin and nik_info.get("jenisKelamin"):
            jenis_kelamin = nik_info["jenisKelamin"]
        if not tempat_lahir and nik_info.get("tempatLahirSuggestion"):
            tempat_lahir = nik_info["tempatLahirSuggestion"]
        if not kab_kota and nik_info.get("dpdName"):
            kab_kota = nik_info["dpdName"]

    addr_parts = []
    if alamat:
        addr_parts.append(alamat)
    if rt_rw:
        addr_parts.append(f"RT/RW {rt_rw}")
    if kel_desa:
        addr_parts.append(f"Kel/Desa {kel_desa}")
    if kecamatan:
        addr_parts.append(f"Kec. {kecamatan}")
    alamat_lengkap = ", ".join(addr_parts)

    raw_text_lines = [r["line_text"] for r in rows]
    raw_text_combined = "\n".join(raw_text_lines)

    confidence = 0
    if len(healed_nik) == 16:
        confidence += 35
    elif len(healed_nik) >= 12:
        confidence += 15
    if nama_lengkap:
        confidence += 25
    if tanggal_lahir:
        confidence += 10
    if jenis_kelamin:
        confidence += 10
    if alamat or kel_desa or kecamatan:
        confidence += 10
    if agama or pekerjaan:
        confidence += 10

    return {
        "nik": healed_nik,
        "namaLengkap": nama_lengkap,
        "tempatLahir": tempat_lahir,
        "tanggalLahir": tanggal_lahir,
        "jenisKelamin": jenis_kelamin,
        "alamatLengkap": alamat_lengkap,
        "rtRw": rt_rw,
        "kelDesa": kel_desa,
        "kecamatan": kecamatan,
        "kabKota": kab_kota,
        "agama": agama,
        "statusPerkawinan": status_perkawinan,
        "pekerjaan": pekerjaan,
        "confidence": min(100, confidence),
        "rawText": raw_text_combined,
    }
