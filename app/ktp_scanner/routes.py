"""Endpoint REST API pemindaian e-KTP berbasis Deep Learning RapidOCR (ONNX)."""
import logging
from flask import Blueprint, jsonify, request
from app.auth import login_required
from app.ktp_scanner.ocr_engine import scan_ktp_image_with_rapidocr

bp = Blueprint("ktp_scanner", __name__)
logger = logging.getLogger("nasdem.ktp_scanner")


@bp.post("/ktp-scanner/scan")
@login_required
def scan_ktp_endpoint():
    """Menerima foto e-KTP (multipart file atau JSON base64) dan mengembalikan hasil ekstraksi terstruktur."""
    image_source = None

    if "file" in request.files:
        uploaded = request.files["file"]
        if uploaded and uploaded.filename:
            image_source = uploaded.read()

    if image_source is None:
        data = request.get_json(silent=True) or {}
        image_source = data.get("image") or data.get("base64") or data.get("fotoKtpUrl")

    if not image_source:
        return jsonify(success=False, error="Data gambar e-KTP tidak ditemukan pada request."), 400

    try:
        result = scan_ktp_image_with_rapidocr(image_source)
        return jsonify(result), 200
    except Exception as exc:
        logger.error(f"[KTP-SCANNER-ERROR] Gagal memproses scan e-KTP: {exc}", exc_info=True)
        return jsonify(
            success=False,
            error=f"Gagal membaca gambar e-KTP di server: {str(exc)}",
        ), 500

