"""Mesin Deep Learning OCR e-KTP berbasis RapidOCR (PP-OCRv4 ONNX Runtime)."""
import logging
import time
from typing import Any, Dict

from app.ktp_scanner.image_preprocessor import preprocess_ktp_for_rapidocr
from app.ktp_scanner.spatial_parser import parse_ktp_spatial_data

logger = logging.getLogger("nasdem.ktp_scanner")

_rapid_ocr_instance = None


def get_rapid_ocr_engine():
    """Memuat instance tunggal (singleton) RapidOCR di memori server agar pemindaian berikutnya instan."""
    global _rapid_ocr_instance
    if _rapid_ocr_instance is None:
        from rapidocr_onnxruntime import RapidOCR

        logger.info("[KTP-SCANNER] Menginisialisasi mesin Deep Learning RapidOCR (PP-OCRv4 ONNX)...")
        _rapid_ocr_instance = RapidOCR()
    return _rapid_ocr_instance


def scan_ktp_image_with_rapidocr(image_source) -> Dict[str, Any]:
    """Menjalankan pipeline penuh pemindaian e-KTP: OpenCV Preprocessing -> RapidOCR ONNX -> Spatial Parser."""
    t0 = time.perf_counter()
    (
        ocr_main_img,
        ocr_nik_strip,
        standardized_ktp_data_url,
        cropped_face_data_url,
        quality_report,
    ) = preprocess_ktp_for_rapidocr(image_source)

    engine = get_rapid_ocr_engine()

    # 1. Baca citra utama e-KTP (CLAHE + Masking Pasfoto, tanpa angle cls karena sudah landscape)
    main_result, _ = engine(ocr_main_img, use_det=True, use_cls=False, use_rec=True)
    main_ocr_items = main_result or []

    # 2. Cek apakah NIK 16 digit valid sudah terbaca dari citra utama;
    #    jika belum lengkap/valid, jalankan juga pemindaian fokus tinggi pada strip NIK (1.75x)
    preliminary = parse_ktp_spatial_data(main_ocr_items, [])
    nik_strip_items = []
    if len(preliminary.get("nik") or "") < 16:
        nik_result, _ = engine(ocr_nik_strip, use_det=True, use_cls=False, use_rec=True)
        nik_strip_items = nik_result or []
        extracted = parse_ktp_spatial_data(main_ocr_items, nik_strip_items)
    else:
        extracted = preliminary
    extracted["croppedFaceDataUrl"] = cropped_face_data_url

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    logger.info(
        f"[KTP-SCANNER] Selesai membaca e-KTP dalam {elapsed_ms} ms | "
        f"NIK='{extracted.get('nik')}' | Nama='{extracted.get('namaLengkap')}' | "
        f"Confidence={extracted.get('confidence')}%"
    )

    return {
        "success": True,
        "engine": "RapidOCR-PPOCRv4-ONNX",
        "elapsedMs": elapsed_ms,
        "standardizedKtpDataUrl": standardized_ktp_data_url,
        "croppedFaceDataUrl": cropped_face_data_url,
        "quality": quality_report,
        "extracted": extracted,
    }
