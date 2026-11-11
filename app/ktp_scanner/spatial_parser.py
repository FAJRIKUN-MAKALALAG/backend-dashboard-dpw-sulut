"""Parser Spasial Berbasis Koordinat Bounding-Box (X, Y) untuk e-KTP Indonesia."""
import re
from typing import Any, Dict, List, Optional, Tuple

from app.ktp_scanner.sulut_regions_ref import (
    SULUT_NIK_PREFIX_TO_DPD,
    decode_nik_info,
    heal_nik_with_cross_fields,
    match_standard_pekerjaan,
    normalize_ocr_digits,
)


def _box_metrics(box: List[List[float]]) -> Tuple[float, float, float, float, float, float]:
    xs = [pt[0] for pt in box]
    ys = [pt[1] for pt in box]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    y_center = (y_min + y_max) / 2.0
    height = max(1.0, y_max - y_min)
    return x_min, x_max, y_min, y_max, y_center, height


def cluster_boxes_into_rows(ocr_items: List[Any]) -> List[Dict[str, Any]]:
    """Mengelompokkan bounding box hasil RapidOCR menjadi baris-baris horizontal berdasarkan koordinat Y."""
    parsed_boxes = []
    for item in ocr_items or []:
        if not item or len(item) < 2:
            continue
        box = item[0]
        text = str(item[1] or "").strip()
        conf = float(item[2]) if len(item) > 2 and item[2] is not None else 0.85
        if not text:
            continue
        x_min, x_max, y_min, y_max, y_center, height = _box_metrics(box)
        parsed_boxes.append(
            {
                "text": text,
                "conf": conf,
                "x_min": x_min,
                "x_max": x_max,
                "y_min": y_min,
                "y_max": y_max,
                "y_center": y_center,
                "height": height,
            }
        )

    parsed_boxes.sort(key=lambda b: b["y_center"])

    rows: List[Dict[str, Any]] = []
    for b in parsed_boxes:
        placed = False
        for row in rows:
            tol = max(15.0, row["avg_height"] * 0.58)
            if abs(b["y_center"] - row["y_center"]) <= tol:
                row["boxes"].append(b)
                n = len(row["boxes"])
                row["y_center"] = sum(x["y_center"] for x in row["boxes"]) / n
                row["avg_height"] = sum(x["height"] for x in row["boxes"]) / n
                placed = True
                break
        if not placed:
            rows.append(
                {
                    "y_center": b["y_center"],
                    "avg_height": b["height"],
                    "boxes": [b],
                }
            )

    for row in rows:
        row["boxes"].sort(key=lambda x: x["x_min"])
        row["line_text"] = " ".join(x["text"] for x in row["boxes"]).strip()
        # Ambil teks yang berada di kolom isi (sebelah kanan label, x_min >= 185 pada kanvas 1400px)
        right_boxes = [x["text"] for x in row["boxes"] if x["x_min"] >= 185]
        row["right_value_text"] = " ".join(right_boxes).strip()

    return rows


def _strip_label(line: str, pattern: str) -> str:
    cleaned = re.sub(pattern, "", line, flags=re.IGNORECASE)
    cleaned = re.sub(r"^[\s:;.\-=_/\\|]+", "", cleaned)
    cleaned = re.sub(r"[\s:;.\-=_/\\|]+$", "", cleaned)
    return cleaned.strip()


def _digit_to_alpha_in_words(text: str) -> str:
    """Mengubah angka yang terselip di dalam kata huruf (misal MANAD0 -> MANADO, W1RASWASTA -> WIRASWASTA)."""
    return (
        (text or "")
        .replace("0", "O")
        .replace("1", "I")
        .replace("4", "A")
        .replace("5", "S")
        .replace("8", "B")
    )


def _clean_person_name(raw: str) -> str:
    cleaned = _digit_to_alpha_in_words(raw)
    cleaned = re.sub(
        r"\b(PROVINSI|KABUPATEN|KOTA|NIK|NAMA|TEMPAT|TGL|LAHIR|JENIS|KELAMIN)\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"[^a-zA-Z\s.,']", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().upper()
    return cleaned


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


def parse_ktp_spatial_data(
    main_ocr_items: List[Any],
    nik_strip_items: List[Any],
) -> Dict[str, Any]:
    """Mengekstrak seluruh field e-KTP menggunakan koordinat spasial (X, Y) & validasi silang NIK."""
    rows = cluster_boxes_into_rows(main_ocr_items)
    raw_nik, nik_row_idx = extract_best_nik_from_ocr(rows, nik_strip_items)

    nama_lengkap = ""
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

        # Header Kabupaten / Kota
        if not kab_kota and re.search(r"\b(KABUPATEN|KOTA)\b", upper) and "PROVINSI" not in upper:
            m_kab = re.search(r"\b(KABUPATEN|KOTA)\s+[A-Z\s]+", upper)
            if m_kab:
                kab_kota = m_kab.group(0).strip().title()

        # Nama Lengkap
        if (
            not nama_lengkap
            and re.search(r"\b(NAMA|NANA|MAMA|HAMA|NAM4|NAWA)\b", upper)
            and not re.search(r"AGAMA|KECAMATAN|KEWARGANEGARAAN", upper)
        ):
            val = _strip_label(line, r"^.*?\b(NAMA|NANA|MAMA|HAMA|NAM4|NAWA)\b")
            if len(val) < 2 and right_val:
                val = _strip_label(right_val, r"^.*?\b(NAMA|NANA|MAMA)\b")
            cleaned_name = _clean_person_name(val)
            if len(cleaned_name) >= 2:
                nama_lengkap = cleaned_name

        # Tempat / Tgl Lahir
        has_ttl_label = bool(
            re.search(r"\b(TEMPAT|TEMPAL|TMP|TGL|TGI|LAHIR|LAHL|TTL)\b", upper)
        )
        norm_line = normalize_ocr_digits(line)
        date_match = re.search(
            r"(\d{2})\s*[-/.,\s]\s*(\d{2})\s*[-/.,\s]\s*((?:19|20)\d{2})",
            norm_line,
        )
        if (not tempat_lahir or not tanggal_lahir) and (has_ttl_label or date_match):
            ttl_row_idx = idx
            val = (
                _strip_label(
                    line,
                    r"^.*?\b(LAHIR|LAHL|TGL\s*LAHIR|TGI\s*LAHIR|TEMPAT\s*/?\s*TGL\s*LAHIR|TEMPAT|TTL)\b",
                )
                if has_ttl_label
                else (right_val or line)
            )
            val_norm = normalize_ocr_digits(val)
            dm = re.search(
                r"(\d{2})\s*[-/.,\s]\s*(\d{2})\s*[-/.,\s]\s*((?:19|20)\d{2})",
                val_norm,
            )
            if dm:
                d_str, m_str, y_str = dm.group(1), dm.group(2), dm.group(3)
                if 1 <= int(d_str) <= 31 and 1 <= int(m_str) <= 12:
                    tanggal_lahir = f"{y_str}-{m_str}-{d_str}"
                before_date = val[: dm.start()]
                before_date = re.sub(
                    r"^.*?\b(LAHIR|TGL|TEMPAT)\b[:\s]*", "", before_date, flags=re.IGNORECASE
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

        # Jenis Kelamin
        if not jenis_kelamin and re.search(
            r"\b(JENIS\s*KELAMIN|KELAMIN|KELAM1N|LAKI|PEREMPUAN|PERENPUAN|WANITA|PRIA)\b",
            upper,
        ):
            if re.search(r"PEREMPUAN|PERENPUAN|WANITA", upper):
                jenis_kelamin = "Perempuan"
            elif re.search(r"LAKI|PRIA", upper):
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
            re.search(r"\b(RT\s*/?\s*RW|RTRW|RTIRW|RT\s+RW)\b", upper)
            or re.search(r"\b00\d\s*[/|-]\s*00\d\b", norm_line)
        ):
            rt_m = re.search(r"(\d{1,3})\s*[/|-]\s*(\d{1,3})", norm_line)
            if rt_m:
                rt_rw = f"{int(rt_m.group(1)):03d}/{int(rt_m.group(2)):03d}"

        # Kel/Desa
        if not kel_desa and re.search(
            r"\b(KEL\s*/?\s*DESA|KEI\s*/?\s*DESA|KEL\s*/?\s*DOSA|KELURAHAN|DESA|KEVDESA)\b",
            upper,
        ):
            val = _strip_label(
                line,
                r"^.*?\b(KEL\s*/?\s*DESA|KEI\s*/?\s*DESA|KEL\s*/?\s*DOSA|KELURAHAN|DESA|KEVDESA)\b",
            )
            val = re.sub(r"[^a-zA-Z0-9\s]", " ", val)
            val = re.sub(r"\s+", " ", val).strip()
            if len(val) >= 2:
                kel_desa = val.title()

        # Kecamatan
        if not kecamatan and re.search(
            r"\b(KECAMATAN|KECANATAN|KECAMALAN|KECA\s*MATAN|KEC\.)\b", upper
        ):
            val = _strip_label(
                line,
                r"^.*?\b(KECAMATAN|KECANATAN|KECAMALAN|KECA\s*MATAN|KEC\.)\b",
            )
            val = re.sub(r"[^a-zA-Z0-9\s]", " ", val)
            val = re.sub(r"\s+", " ", val).strip()
            if len(val) >= 2:
                kecamatan = val.title()

        # Agama
        if not agama and re.search(
            r"\b(AGAMA|AGANA|AQAMA|ISLAM|1SLAM|KRISTEN|KR1STEN|PROTESTAN|KATOLIK|KATHOLIK|HINDU|BUDDHA|BUDHA|KHONGHUCU|KONGHUCU)\b",
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
            r"\b(PERKAWINAN|PERKAW1NAN|KAWIN|KAW1N|CERAI)\b", upper
        ):
            if re.search(r"BELUM|BLN", upper):
                status_perkawinan = "Belum Kawin"
            elif re.search(r"CERAI\s*HIDUP", upper):
                status_perkawinan = "Cerai Hidup"
            elif re.search(r"CERAI\s*MATI", upper):
                status_perkawinan = "Cerai Mati"
            elif re.search(r"KAWIN|KAW1N", upper):
                status_perkawinan = "Kawin"

        # Pekerjaan
        if not pekerjaan and re.search(
            r"\b(PEKERJAAN|PEKERIAAN|PEKERLAAN|PEKERJ4AN)\b", upper
        ):
            val = _strip_label(line, r"^.*?\b(PEKERJAAN|PEKERIAAN|PEKERLAAN|PEKERJ4AN)\b")
            matched_job = match_standard_pekerjaan(val or right_val)
            if matched_job:
                pekerjaan = matched_job

    # Fallback Spasial untuk Nama Lengkap (Baris tepat di atas TTL atau tepat di bawah NIK pada kolom kanan)
    if not nama_lengkap:
        cand_idx = (
            ttl_row_idx - 1
            if ttl_row_idx > 0
            else (nik_row_idx + 1 if 0 <= nik_row_idx + 1 < len(rows) else -1)
        )
        if 0 <= cand_idx < len(rows):
            raw_cand = rows[cand_idx]["right_value_text"] or rows[cand_idx]["line_text"]
            if ":" in raw_cand:
                raw_cand = raw_cand.split(":", 1)[1]
            cleaned = _clean_person_name(raw_cand)
            if len(cleaned) >= 3 and not re.search(r"\d{4,}", raw_cand):
                nama_lengkap = cleaned

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
        if not tanggal_lahir and nik_info.get("tanggalLahir"):
            tanggal_lahir = nik_info["tanggalLahir"]
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
