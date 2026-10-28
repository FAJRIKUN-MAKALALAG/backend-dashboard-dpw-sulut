"""
Modul konfigurasi logging terpusat untuk API Backend NasDem DPW Sulut.
Menyediakan logging otomatis untuk setiap HTTP Request/Response,
pencatatan mendalam untuk error 4xx (Client Error) & 5xx (Server Error),
serta penyimpanan berkas log berputar (RotatingFileHandler) di logs/api.log dan logs/error.log.
"""

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import time
import traceback
import uuid

from flask import Flask, g, has_request_context, jsonify, request


# ANSI Color Codes untuk terminal console
class ConsoleColors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"
    BLUE = "\033[34m"
    GRAY = "\033[90m"


class ColoredConsoleFormatter(logging.Formatter):
    """Formatter kustom dengan warna untuk terminal agar memudahkan identifikasi status."""

    def format(self, record):
        levelname = record.levelname
        msg = record.getMessage()

        if record.levelno >= logging.ERROR:
            color = ConsoleColors.RED + ConsoleColors.BOLD
            prefix = "[ERROR]"
        elif record.levelno >= logging.WARNING:
            color = ConsoleColors.YELLOW + ConsoleColors.BOLD
            prefix = "[WARN ]"
        elif record.levelno >= logging.INFO:
            color = ConsoleColors.CYAN
            prefix = "[INFO ]"
        else:
            color = ConsoleColors.GRAY
            prefix = "[DEBUG]"

        timestamp = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        formatted = f"{ConsoleColors.GRAY}[{timestamp}]{ConsoleColors.RESET} {color}{prefix}{ConsoleColors.RESET} {msg}"

        if record.exc_info:
            formatted += f"\n{self.formatException(record.exc_info)}"

        return formatted


def _sanitize_value(key: str, val):
    """Menyaring nilai rahasia (password/token) dan meringkas string gambar base64 yang panjang."""
    low_key = str(key).lower()
    if any(secret_word in low_key for secret_word in ("password", "token", "secret", "authorization")):
        return "***PROTECTED***"

    if isinstance(val, str):
        if val.startswith("data:image/") or len(val) > 250:
            preview = val[:40].replace("\n", "")
            return f"[String len={len(val)}: '{preview}...']"
        return val

    if isinstance(val, dict):
        return {k: _sanitize_value(k, v) for k, v in val.items()}

    if isinstance(val, list):
        if len(val) > 10:
            sample = [_sanitize_value(f"item_{i}", item) for i, item in enumerate(val[:5])]
            return sample + [f"... dan {len(val) - 5} item lainnya"]
        return [_sanitize_value(f"item_{i}", item) for i, item in enumerate(val)]

    return val


def get_sanitized_request_body():
    """Mengambil dan menyaring body request untuk dicatat ke dalam log tanpa membocorkan rahasia."""
    try:
        if request.is_json:
            json_data = request.get_json(silent=True)
            if json_data is not None:
                sanitized = _sanitize_value("root", json_data)
                return json.dumps(sanitized, ensure_ascii=False)
        elif request.form:
            form_dict = request.form.to_dict()
            sanitized = _sanitize_value("form", form_dict)
            return json.dumps(sanitized, ensure_ascii=False)
        elif request.data:
            length = len(request.data)
            return f"[Raw Data {length} bytes]"
    except Exception as e:
        return f"[Gagal membaca body: {e}]"
    return None


def get_current_actor():
    """Mendapatkan identitas user/role yang sedang melakukan request (jika ada)."""
    try:
        from .auth import current_claims
        claims = current_claims()
        if claims and isinstance(claims, dict):
            role = claims.get("role", "UNKNOWN")
            nik = claims.get("nik") or claims.get("sub") or "-"
            name = claims.get("nama") or claims.get("name") or "-"
            return f"{role} (NIK: {nik}, {name})"
    except Exception:
        pass
    return "Anonim / Publik"


def setup_logger(app: Flask):
    """Mengonfigurasi logging terpusat pada aplikasi Flask."""
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    log_dir = os.path.join(base_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)

    api_log_file = os.path.join(log_dir, "api.log")
    error_log_file = os.path.join(log_dir, "error.log")

    # Standard Formatter untuk File Log
    file_formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1. Console Handler (Output ke Terminal dengan Warna)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(ColoredConsoleFormatter())

    # 2. File Handler: api.log (Merekam semua aktivitas info, warning, error) - Max 10MB per file, 5 backup
    api_file_handler = RotatingFileHandler(
        api_log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    api_file_handler.setLevel(logging.INFO)
    api_file_handler.setFormatter(file_formatter)

    # 3. File Handler: error.log (Khusus mencatat WARNING dan ERROR untuk kemudahan debugging)
    error_file_handler = RotatingFileHandler(
        error_log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    error_file_handler.setLevel(logging.WARNING)
    error_file_handler.setFormatter(file_formatter)

    # Logger aplikasi
    api_logger = logging.getLogger("api")
    api_logger.setLevel(logging.INFO)
    api_logger.handlers = []
    api_logger.addHandler(console_handler)
    api_logger.addHandler(api_file_handler)
    api_logger.addHandler(error_file_handler)
    api_logger.propagate = False

    # Root logger juga mendapatkan file handlers agar exception dari library/SQLAlchemy tertangkap
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(console_handler)
    root_logger.addHandler(error_file_handler)

    # Simpan logger di objek app
    app.api_logger = api_logger

    # ================= HOOKS REQUEST & RESPONSE =================

    @app.before_request
    def log_request_start():
        request._start_time = time.time()
        g.request_id = uuid.uuid4().hex[:8]

    @app.after_request
    def log_request_end(response):
        # Abaikan logging berlebihan untuk static files / favicon jika bukan error
        if request.path.startswith("/static") and response.status_code < 400:
            return response

        start_time = getattr(request, "_start_time", None)
        duration_ms = round((time.time() - start_time) * 1000, 1) if start_time else 0.0

        # Tambahkan header diagnostik ke response
        req_id = getattr(g, "request_id", "-")
        response.headers["X-Request-ID"] = req_id
        response.headers["X-Response-Time"] = f"{duration_ms}ms"

        method = request.method
        path = request.path
        query = request.query_string.decode("utf-8") if request.query_string else ""
        full_path = f"{path}?{query}" if query else path
        status_code = response.status_code
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "-")
        actor = get_current_actor()

        # Format pesan log sesuai status HTTP
        if status_code < 400:
            # 2xx Success & 3xx Redirection
            api_logger.info(
                f"[API-OK] {method} {full_path} -> {status_code} ({duration_ms}ms) | IP: {ip} | User: {actor}"
            )
        elif 400 <= status_code < 500:
            # 4xx Client Error (400, 401, 403, 404, 409, 422, dll.)
            body_summary = get_sanitized_request_body()
            res_preview = ""
            try:
                if response.is_json:
                    res_data = response.get_json()
                    res_preview = json.dumps(res_data, ensure_ascii=False)
                elif response.data:
                    res_preview = response.data[:300].decode("utf-8", errors="replace")
            except Exception:
                pass

            msg_lines = [
                f"[API-WARN {status_code}] {method} {full_path} ({duration_ms}ms)",
                f"  -> IP: {ip} | User: {actor} | Req-ID: {req_id}",
            ]
            if body_summary:
                msg_lines.append(f"  -> Request Body: {body_summary}")
            if res_preview:
                msg_lines.append(f"  -> Response Error: {res_preview}")

            api_logger.warning("\n".join(msg_lines))
        else:
            # 5xx Server Error (500, 502, 503, dll.)
            body_summary = get_sanitized_request_body()
            res_preview = ""
            try:
                if response.is_json:
                    res_preview = json.dumps(response.get_json(), ensure_ascii=False)
                elif response.data:
                    res_preview = response.data[:300].decode("utf-8", errors="replace")
            except Exception:
                pass

            msg_lines = [
                f"[API-ERROR {status_code}] {method} {full_path} ({duration_ms}ms)",
                f"  -> IP: {ip} | User: {actor} | Req-ID: {req_id}",
            ]
            if body_summary:
                msg_lines.append(f"  -> Request Body: {body_summary}")
            if res_preview:
                msg_lines.append(f"  -> Response: {res_preview}")

            api_logger.error("\n".join(msg_lines))

        return response

    return api_logger
