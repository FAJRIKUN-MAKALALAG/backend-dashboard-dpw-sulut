"""Modul Khusus Pemindaian e-KTP (Deep Learning RapidOCR ONNX + Spatial Coordinate Parser)."""
from app.ktp_scanner.routes import bp as ktp_scanner_bp
from app.ktp_scanner.ocr_engine import scan_ktp_image_with_rapidocr

__all__ = ["ktp_scanner_bp", "scan_ktp_image_with_rapidocr"]

