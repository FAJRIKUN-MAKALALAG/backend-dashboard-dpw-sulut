"""Mesin OCR e-KTP dengan Dukungan Google Cloud Document AI & Red-Channel Word-Segmented RapidOCR (ONNX)."""
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from app.ktp_scanner.image_preprocessor import (
    crop_direct_nik_line,
    extract_red_channel_bgr,
    preprocess_ktp_for_rapidocr,
    segment_word_crops_from_line,
)
from app.ktp_scanner.spatial_parser import _clean_person_name, parse_ktp_spatial_data

logger = logging.getLogger("nasdem.ktp_scanner")

_rapid_ocr_instance = None
_docai_cooldown_until: float = 0.0


def get_rapid_ocr_engine():
    """Memuat instance tunggal (singleton) RapidOCR di memori server agar pemindaian berikutnya instan."""
    global _rapid_ocr_instance
    if _rapid_ocr_instance is None:
        from rapidocr_onnxruntime import RapidOCR

        logger.info("[KTP-SCANNER] Menginisialisasi mesin Deep Learning RapidOCR (PP-OCRv4 ONNX)...")
        _rapid_ocr_instance = RapidOCR()
    return _rapid_ocr_instance


def _extract_rec_text(rec_result: Any) -> str:
    """Mengambil teks dari hasil panggilan RapidOCR dengan use_det=False."""
    if not rec_result or not isinstance(rec_result, list):
        return ""
    first = rec_result[0]
    if not first:
        return ""
    if isinstance(first[0], str):
        return first[0].strip()
    if len(first) > 1 and isinstance(first[1], str):
        return first[1].strip()
    return ""


def scan_ktp_image_with_rapidocr(image_source) -> Dict[str, Any]:
    """Menjalankan pipeline pemindaian e-KTP Multi-Channel Presisi Tinggi:
    1. Pass RGB Alami (1400x884) + Pass Kanal Merah Murni (Red-Channel Cyan Suppression).
    2. Direct Full-Line Recognition (use_det=False) pada jalur 16 digit NIK.
    3. Word-Level Segmentation (use_det=False per kata) pada kotak nilai Nama Lengkap
       agar huruf tidak terganggu garis gelombang biru dan spasi antar-kata akurat 100%.
    """
    t0 = time.perf_counter()
    (
        ocr_main_img,
        ocr_nik_strip,
        standardized_ktp_data_url,
        cropped_face_data_url,
        quality_report,
    ) = preprocess_ktp_for_rapidocr(image_source)

    engine = get_rapid_ocr_engine()

    # 1. Baca citra Kanal Merah Murni (bebas gelombang cyan latar e-KTP) & citra RGB standar
    red_bgr = extract_red_channel_bgr(ocr_main_img)
    red_result, _ = engine(red_bgr, use_det=True, use_cls=False, use_rec=True)
    red_items = red_result or []

    rgb_result, _ = engine(ocr_main_img, use_det=True, use_cls=False, use_rec=True)
    rgb_items = rgb_result or []

    # 2. Cari baris NIK & baris Nama pada Kanal Merah untuk Direct Recognition & Word Segmentation
    direct_nik_items: List[Any] = []
    segmented_nama: Optional[str] = None

    for box, txt, conf in red_items:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x1, x2, y1, y2 = int(min(xs)), int(max(xs)), int(min(ys)), int(max(ys))

        # Direct full-line NIK recognition tanpa DBNet agar digit tengah NIK tidak pernah terputus
        if ("NIK" in txt.upper() or (125 <= y1 <= 220 and x1 < 200)) and not direct_nik_items:
            nik_line_bgr = crop_direct_nik_line(ocr_main_img, y1, y2)
            nik_rec, _ = engine(nik_line_bgr, use_det=False, use_cls=False, use_rec=True)
            nik_txt = _extract_rec_text(nik_rec)
            if nik_txt:
                direct_nik_items.append(
                    [[[310, y1], [980, y1], [980, y2], [310, y2]], nik_txt, 0.96]
                )

        # Word-level segmentation pada kotak nilai Nama Lengkap (y sekitar 210..295, x > 240)
        if segmented_nama is None and x1 > 240 and 205 <= y1 <= 295:
            # Pastikan bukan baris Tempat/Tgl Lahir (tidak mengandung 4 digit tahun)
            if not re.search(r"(?:19|20)\d{2}", txt):
                word_crops = segment_word_crops_from_line(ocr_main_img, x1, y1, x2, y2)
                if word_crops:
                    words: List[str] = []
                    for w_crop in word_crops:
                        w_rec, _ = engine(w_crop, use_det=False, use_cls=False, use_rec=True)
                        w_txt = _extract_rec_text(w_rec)
                        if w_txt:
                            words.append(w_txt)
                    if words:
                        candidate = _clean_person_name(" ".join(words))
                        if len(candidate) >= 3:
                            segmented_nama = candidate

    # 3. Jalankan Spatial Parser (menggabungkan Direct NIK + Red-Channel + RGB + Word-Segmented Nama)
    extracted = parse_ktp_spatial_data(
        red_items,
        direct_nik_items,
        rgb_items,
        segmented_nama=segmented_nama,
    )

    if len(extracted.get("nik") or "") < 16:
        nik_result, _ = engine(ocr_nik_strip, use_det=True, use_cls=False, use_rec=True)
        nik_strip_items = (nik_result or []) + direct_nik_items
        extracted = parse_ktp_spatial_data(
            red_items,
            nik_strip_items,
            rgb_items,
            segmented_nama=segmented_nama,
        )

    extracted["croppedFaceDataUrl"] = cropped_face_data_url

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        f"[KTP-SCANNER-RAPID] Selesai membaca e-KTP dalam {elapsed_ms} ms | "
        f"NIK='{extracted.get('nik')}' | Nama='{extracted.get('namaLengkap')}' | "
        f"Confidence={extracted.get('confidence')}%"
    )

    return {
        "success": True,
        "engine": "RapidOCR-PPOCRv4-RedChannel-WordSeg",
        "elapsedMs": elapsed_ms,
        "standardizedKtpDataUrl": standardized_ktp_data_url,
        "croppedFaceDataUrl": cropped_face_data_url,
        "quality": quality_report,
        "extracted": extracted,
    }


def scan_ktp_image(image_source) -> Dict[str, Any]:
    """Fungsi gerbang utama (Dispatcher):

    1. Memeriksa kredensial Google Cloud Document AI (*.json).
    2. Jika tersedia dan tidak sedang dalam cooldown akibat Billing belum aktif, jalankan Google Cloud Document AI.
    3. Jika Google Cloud Document AI gagal (misal Billing belum diaktifkan di Google Cloud Console),
       otomatis fallback ke mesin Red-Channel Word-Segmented RapidOCR.
    """
    global _docai_cooldown_until
    prefer_engine = os.getenv("KTP_OCR_PREFER_ENGINE", "google").lower()

    if prefer_engine != "rapidocr" and time.time() >= _docai_cooldown_until:
        try:
            from app.ktp_scanner.google_docai_engine import (
                find_service_account_path,
                scan_ktp_image_with_google_docai,
            )

            cred_path = find_service_account_path()
            if cred_path:
                logger.info(f"[KTP-SCANNER] Menggunakan Google Cloud Document AI ({os.path.basename(cred_path)})...")
                return scan_ktp_image_with_google_docai(image_source)
        except Exception as exc:
            err_str = str(exc)
            if "BILLING_DISABLED" in err_str or "403" in err_str:
                _docai_cooldown_until = time.time() + 180.0
                logger.warning(
                    "[KTP-SCANNER] Google Cloud Billing belum diaktifkan pada project 'nasddem-ocr-ktp' (403 BILLING_DISABLED). "
                    "Beralih ke Red-Channel Word-Segmented RapidOCR..."
                )
            else:
                logger.warning(
                    f"[KTP-SCANNER] Gagal memindai dengan Google Document AI ({exc}), otomatis fallback ke RapidOCR...",
                    exc_info=True,
                )

    return scan_ktp_image_with_rapidocr(image_source)
