"""Pra-pemrosesan citra e-KTP menggunakan OpenCV (CLAHE, Cyan Suppression, Auto-Rotate, dan Zonal Cropping)."""
from __future__ import annotations

import base64
from typing import Any, Dict, Tuple

try:
    import cv2
    import numpy as np
except Exception:
    cv2 = None  # type: ignore
    np = None  # type: ignore


def decode_image_to_bgr(image_source) -> Any:
    """Mendekode string Base64 Data URI atau bytes menjadi matriks BGR OpenCV."""
    if cv2 is None or np is None:
        raise RuntimeError("Modul opencv-python-headless / numpy belum terpasang di server.")
    if isinstance(image_source, str):
        raw_str = image_source.strip()
        if ";base64," in raw_str:
            raw_str = raw_str.split(";base64,", 1)[1]
        img_bytes = base64.b64decode(raw_str)
    elif hasattr(image_source, "read"):
        img_bytes = image_source.read()
    elif isinstance(image_source, (bytes, bytearray)):
        img_bytes = bytes(image_source)
    else:
        raise ValueError("Format sumber gambar tidak didukung")

    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError("Gagal mendekode berkas gambar e-KTP")
    return bgr


def encode_bgr_to_data_url(bgr: np.ndarray, mime: str = "image/jpeg", quality: int = 88) -> str:
    if mime == "image/png":
        ok, buf = cv2.imencode(".png", bgr)
    else:
        ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise ValueError("Gagal mengenkode gambar hasil pemrosesan")
    b64 = base64.b64encode(buf.tobytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def preprocess_ktp_for_rapidocr(image_source) -> Tuple[np.ndarray, np.ndarray, str, str, Dict]:
    """Menstandarkan citra e-KTP dan menyiapkan matriks optimal bagi RapidOCR (PP-OCRv4).

    Returns:
      - `ocr_main_img`: Citra utama 1400x884 yang dipertajam dengan CLAHE & area pasfoto di-masking.
      - `ocr_nik_strip`: Citra khusus area baris NIK yang diperbesar 1.8x dengan penghapusan latar cyan.
      - `standardized_ktp_data_url`: Data URL gambar e-KTP standar warna.
      - `cropped_face_data_url`: Data URL potongan pasfoto wajah kader dari sisi kanan KTP.
      - `quality_report`: Laporan kualitas gambar (pencahayaan, ketajaman, resolusi).
    """
    raw_bgr = decode_image_to_bgr(image_source)
    src_h, src_w = raw_bgr.shape[:2]

    was_auto_rotated = src_h > int(src_w * 1.08)
    if was_auto_rotated:
        raw_bgr = cv2.rotate(raw_bgr, cv2.ROTATE_90_COUNTERCLOCKWISE)
        src_h, src_w = raw_bgr.shape[:2]

    target_w, target_h = 1400, 884
    std_bgr = cv2.resize(raw_bgr, (target_w, target_h), interpolation=cv2.INTER_CUBIC)
    standardized_ktp_data_url = encode_bgr_to_data_url(std_bgr, "image/jpeg", 88)

    # 1. Potong Pasfoto Wajah dari Sisi Kanan e-KTP
    fx1 = int(target_w * 0.715)
    fy1 = int(target_h * 0.21)
    fx2 = int(target_w * 0.96)
    fy2 = int(target_h * 0.77)
    face_crop = cv2.resize(std_bgr[fy1:fy2, fx1:fx2], (300, 380), interpolation=cv2.INTER_CUBIC)
    cropped_face_data_url = encode_bgr_to_data_url(face_crop, "image/jpeg", 86)

    # 2. Evaluasi Kualitas Gambar (Brightness, Glare, Laplacian Sharpness)
    gray = cv2.cvtColor(std_bgr, cv2.COLOR_BGR2GRAY)
    avg_lum = float(np.mean(gray))
    glare_ratio = float(np.mean(gray > 245))
    text_roi_gray = gray[40 : target_h - 40, 30 : int(target_w * 0.71)]
    laplacian_var = float(cv2.Laplacian(text_roi_gray, cv2.CV_64F).var())

    brightness_status = (
        "TOO_DARK"
        if avg_lum < 58
        else "TOO_BRIGHT"
        if (avg_lum > 228 or glare_ratio > 0.30)
        else "GOOD"
    )
    sharpness_status = "BLURRY" if laplacian_var < 55.0 else "GOOD"
    resolution_status = "LOW" if max(src_w, src_h) < 500 else "GOOD"

    warnings = []
    tips = []
    if brightness_status == "TOO_DARK":
        warnings.append("Pencahayaan KTP tampak agak gelap.")
        tips.append("Nyalakan tombol Flash / Senter di kamera atau pindahkan ke tempat terang.")
    elif brightness_status == "TOO_BRIGHT":
        warnings.append("Terdeteksi pantulan cahaya berlebih pada permukaan KTP.")
        tips.append("Miringkan sedikit kartu KTP agar pantulan lampu tidak menutupi angka NIK.")

    if sharpness_status == "BLURRY":
        warnings.append("Fokus gambar terdeteksi agak buram (blur).")
        tips.append("Tahan kamera dengan stabil hingga fokus mengunci sebelum memotret.")

    if resolution_status == "LOW":
        warnings.append("Resolusi gambar KTP cukup rendah.")
        tips.append("Dekatkan kamera agar kartu KTP memenuhi bingkai panduan.")

    if was_auto_rotated:
        tips.append("Foto vertikal otomatis diputar ke posisi mendatar (landscape) standar e-KTP.")

    score = 100
    if brightness_status != "GOOD":
        score -= 20
    if sharpness_status == "BLURRY":
        score -= 25
    if resolution_status == "LOW":
        score -= 20

    quality_report = {
        "isValid": score >= 55,
        "score": max(0, score),
        "isLandscape": True,
        "wasAutoRotated": was_auto_rotated,
        "brightnessStatus": brightness_status,
        "sharpnessStatus": sharpness_status,
        "resolutionStatus": resolution_status,
        "warnings": warnings,
        "tips": tips,
    }

    # 3. Bangun Citra Utama untuk RapidOCR (CLAHE pada kanal L + Unsharp Mask + Masking Pasfoto)
    lab = cv2.cvtColor(std_bgr, cv2.COLOR_BGR2LAB)
    l_chan, a_chan, b_chan = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.4, tileGridSize=(8, 8))
    l_enhanced = clahe.apply(l_chan)
    enhanced_bgr = cv2.cvtColor(cv2.merge((l_enhanced, a_chan, b_chan)), cv2.COLOR_LAB2BGR)

    blurred = cv2.GaussianBlur(enhanced_bgr, (0, 0), 1.4)
    ocr_main_img = cv2.addWeighted(enhanced_bgr, 1.55, blurred, -0.55, 0)

    # Tutup area pasfoto & tanda tangan di sisi kanan bawah dengan warna putih bersih
    # agar tidak menghasilkan kotak teks palsu pada kolom Pekerjaan / Agama / Status
    mask_x1 = int(target_w * 0.72)
    mask_y1 = int(target_h * 0.20)
    ocr_main_img[mask_y1:, mask_x1:] = 255

    # 4. Bangun Citra Khusus Strip NIK (Diperbesar 1.8x + Red-Channel Cyan Suppression)
    # Pada e-KTP, latar biru/cyan memiliki nilai Kanal Merah (R) yang kontras terhadap teks hitam
    nik_y1 = int(target_h * 0.08)
    nik_y2 = int(target_h * 0.34)
    nik_x1 = int(target_w * 0.02)
    nik_x2 = int(target_w * 0.74)
    nik_crop = std_bgr[nik_y1:nik_y2, nik_x1:nik_x2]

    b_c, g_c, r_c = cv2.split(nik_crop)
    # Angkat warna latar biru/cyan menjadi terang sambil mempertahankan huruf hitam NIK
    cyan_suppressed = np.where(
        (b_c.astype(np.int16) > r_c.astype(np.int16) + 14) & (b_c > 90),
        np.clip(b_c.astype(np.int16) + 30, 0, 255),
        np.round(0.7 * r_c + 0.2 * g_c + 0.1 * b_c),
    ).astype(np.uint8)

    nik_clahe = cv2.createCLAHE(clipLimit=2.8, tileGridSize=(6, 6)).apply(cyan_suppressed)
    nik_upscaled = cv2.resize(
        nik_clahe,
        (int(nik_crop.shape[1] * 1.75), int(nik_crop.shape[0] * 1.75)),
        interpolation=cv2.INTER_CUBIC,
    )
    nik_blur = cv2.GaussianBlur(nik_upscaled, (0, 0), 1.2)
    nik_sharp = cv2.addWeighted(nik_upscaled, 1.6, nik_blur, -0.6, 0)
    ocr_nik_strip = cv2.cvtColor(nik_sharp, cv2.COLOR_GRAY2BGR)

    return (
        ocr_main_img,
        ocr_nik_strip,
        standardized_ktp_data_url,
        cropped_face_data_url,
        quality_report,
    )

