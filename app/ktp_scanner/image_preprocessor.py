"""Pra-pemrosesan citra e-KTP menggunakan OpenCV (Red-Channel Cyan Suppression, Word Segmentation, & Zonal Cropping)."""
from __future__ import annotations

import base64
from typing import Any, Dict, List, Tuple

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


def extract_red_channel_bgr(std_bgr: np.ndarray) -> np.ndarray:
    """Mengekstrak Kanal Merah (Red Channel) murni sebagai BGR 3-channel.

    Pada fisik e-KTP Indonesia, latar belakang gelombang biru/cyan memantulkan cahaya merah
    hampir seterang warna putih, sedangkan teks huruf hitam menyerap cahaya merah.
    Kanal merah murni menghilangkan garis gelombang biru yang sering memotong huruf Nama (misal G -> C, R -> IR).
    """
    _, _, r_ch = cv2.split(std_bgr)
    r_bgr = cv2.cvtColor(r_ch, cv2.COLOR_GRAY2BGR)
    target_h, target_w = std_bgr.shape[:2]
    # Tutup hanya area tanda tangan di pojok kanan bawah
    r_bgr[int(target_h * 0.65) :, int(target_w * 0.74) :] = 255
    return r_bgr


def segment_word_crops_from_line(
    std_bgr: np.ndarray,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
) -> List[np.ndarray]:
    """Memotong kotak nilai baris (seperti Nama Lengkap) menjadi potongan per-kata (word crops)
    menggunakan morfologi penutupan horizontal pada Kanal Merah.

    Hal ini mengatasi kelemahan model OCR yang sering menggabungkan kata tanpa spasi
    (misal 'FAJRIKUNMAKALALAG' -> ['FAJRIKUN', 'MAKALALAG'], 'FELTYYUNITAFRANS' -> ['FELTY', 'YUNITA', 'FRANS']).
    """
    h, w = std_bgr.shape[:2]
    _, _, r_ch = cv2.split(std_bgr)

    cy1 = max(0, y1 - 4)
    cy2 = min(h, y2 + 4)
    cx1 = max(0, x1 - 4)
    cx2 = min(w, x2 + 6)
    crop_r = r_ch[cy1:cy2, cx1:cx2]
    if crop_r.size == 0 or crop_r.shape[0] < 10 or crop_r.shape[1] < 20:
        return []

    _, bw = cv2.threshold(crop_r, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    # Kernel horizontal 9x5 menyatukan huruf-huruf di dalam 1 kata, namun tetap memisahkan antar-kata (jarak >= 10px)
    k_word = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 5))
    closed = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, k_word)

    cnts, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    word_boxes = []
    for c in cnts:
        wx, wy, ww, wh = cv2.boundingRect(c)
        if ww >= 15 and wh >= 11:
            word_boxes.append((wx, wy, ww, wh))

    if not word_boxes:
        return []

    word_boxes.sort(key=lambda b: b[0])
    bg_val = int(np.median(crop_r))
    crops: List[np.ndarray] = []

    for wx, wy, ww, wh in word_boxes:
        w_slice = crop_r[:, max(0, wx - 3) : min(crop_r.shape[1], wx + ww + 3)]
        w_pad = cv2.copyMakeBorder(
            w_slice, 8, 8, 10, 10, cv2.BORDER_CONSTANT, value=bg_val
        )
        crops.append(cv2.cvtColor(w_pad, cv2.COLOR_GRAY2BGR))

    return crops


def crop_direct_nik_line(std_bgr: np.ndarray, y1: int, y2: int) -> np.ndarray:
    """Memotong jalur penuh 16 digit NIK pada Kanal Merah untuk dibaca langsung oleh Recognizer
    tanpa melalui DBNet Detector (mencegah digit tengah NIK terputus/hilang).
    """
    h, w = std_bgr.shape[:2]
    _, _, r_ch = cv2.split(std_bgr)
    ny1 = max(0, y1 - 10)
    ny2 = min(h, y2 + 10)
    nx1 = int(w * 0.22)
    nx2 = int(w * 0.70)
    nik_line = r_ch[ny1:ny2, nx1:nx2]
    nik_clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4)).apply(nik_line)
    return cv2.cvtColor(nik_clahe, cv2.COLOR_GRAY2BGR)


def preprocess_ktp_for_rapidocr(image_source) -> Tuple[np.ndarray, np.ndarray, str, str, Dict]:
    """Menstandarkan citra e-KTP dan menyiapkan matriks optimal bagi RapidOCR & Google Document AI.

    Returns:
      - `ocr_main_img`: Citra standar 1400x884 BGR alami (tanpa distorsi rotasi palsu atau over-sharpening).
      - `ocr_nik_strip`: Citra khusus area baris NIK yang diperbesar 1.75x dengan penghapusan latar cyan.
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
    standardized_ktp_data_url = encode_bgr_to_data_url(std_bgr, "image/jpeg", 90)

    # 1. Potong Pasfoto Wajah dari Sisi Kanan e-KTP
    fx1 = int(target_w * 0.715)
    fy1 = int(target_h * 0.21)
    fx2 = int(target_w * 0.96)
    fy2 = int(target_h * 0.77)
    face_crop = cv2.resize(std_bgr[fy1:fy2, fx1:fx2], (300, 380), interpolation=cv2.INTER_CUBIC)
    cropped_face_data_url = encode_bgr_to_data_url(face_crop, "image/jpeg", 88)

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

    # 3. Citra Utama: Gunakan std_bgr alami dengan penutupan area tanda tangan kanan bawah
    ocr_main_img = std_bgr.copy()
    ocr_main_img[int(target_h * 0.65) :, int(target_w * 0.74) :] = 255

    # 4. Citra Khusus Strip NIK (Diperbesar 1.75x pada Kanal Merah)
    nik_y1 = int(target_h * 0.08)
    nik_y2 = int(target_h * 0.32)
    nik_x1 = int(target_w * 0.02)
    nik_x2 = int(target_w * 0.74)
    nik_crop = std_bgr[nik_y1:nik_y2, nik_x1:nik_x2]
    _, _, r_nik = cv2.split(nik_crop)
    nik_clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(6, 6)).apply(r_nik)
    nik_upscaled = cv2.resize(
        nik_clahe,
        (int(nik_crop.shape[1] * 1.75), int(nik_crop.shape[0] * 1.75)),
        interpolation=cv2.INTER_CUBIC,
    )
    ocr_nik_strip = cv2.cvtColor(nik_upscaled, cv2.COLOR_GRAY2BGR)

    return (
        ocr_main_img,
        ocr_nik_strip,
        standardized_ktp_data_url,
        cropped_face_data_url,
        quality_report,
    )
