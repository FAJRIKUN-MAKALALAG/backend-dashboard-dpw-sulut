import gzip
import os
from flask import Flask, jsonify, request, g
from flask_cors import CORS

from config import get_config
from .extensions import db, jwt, migrate
from .logging_config import setup_logger


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(get_config())

    # Inisialisasi sistem logging terpusat (Terminal Console + Rotating File logs/api.log & logs/error.log)
    logger = setup_logger(app)
    logger.info("Memulai Aplikasi NasDem DPW Sulut Backend...")

    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    cors_origins = app.config.get("CORS_ORIGINS", "*")
    CORS(
        app,
        resources={
            r"/*": {
                "origins": cors_origins,
                "methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                "allow_headers": ["Content-Type", "Authorization", "Accept", "X-Requested-With", "Origin"],
            }
        },
    )

    from . import models  # noqa: F401  (registrasi model untuk migrasi)
    from .routes import register_blueprints

    register_blueprints(app)

    with app.app_context():
        try:
            db.create_all()
        except Exception as exc:
            logger.warning(f"Inisialisasi tabel otomatis dilewati: {exc}")
        try:
            from .routes.structure_slots import ensure_structure_slots_table_exists
            ensure_structure_slots_table_exists()
        except Exception as exc:
            logger.warning(f"Inisialisasi tabel structure_slots dilewati: {exc}")

    @app.after_request
    def compress_json_response(response):
        """Kompresi Gzip otomatis (~85% lebih hemat bandwidth) untuk respons JSON berukuran >= 1 KB."""
        if (
            response.direct_passthrough
            or response.status_code < 200
            or response.status_code >= 300
            or "Content-Encoding" in response.headers
        ):
            return response

        accept_encoding = request.headers.get("Accept-Encoding", "")
        if "gzip" not in accept_encoding.lower():
            return response

        if response.mimetype not in ("application/json", "text/plain", "text/csv"):
            return response

        raw_data = response.get_data()
        if len(raw_data) < 1024:
            return response

        compressed = gzip.compress(raw_data, compresslevel=5)
        if len(compressed) >= len(raw_data):
            return response

        response.set_data(compressed)
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = str(len(compressed))
        vary = response.headers.get("Vary", "")
        if "Accept-Encoding" not in vary:
            response.headers["Vary"] = f"{vary}, Accept-Encoding".lstrip(", ")
        return response

    @app.get("/api/health")
    def health():
        try:
            db.session.execute(db.text("SELECT 1"))
            return jsonify(status="ok", database="up")
        except Exception as exc:  # pragma: no cover
            return jsonify(status="error", database=str(exc)), 503

    @app.get("/api/logs/recent")
    def get_recent_logs():
        """Endpoint diagnostik khusus Administrator untuk melihat baris log terbaru."""
        from .auth import current_claims
        claims = current_claims()
        if not claims or claims.get("role") not in ("SUPER_ADMIN", "ADMIN_DPW"):
            return jsonify(error="Akses ditolak: Hanya Administrator yang dapat melihat log"), 403

        log_type = request.args.get("type", "error")
        try:
            limit = min(int(request.args.get("limit", 100)), 500)
        except (ValueError, TypeError):
            limit = 100

        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        filename = "error.log" if log_type == "error" else "api.log"
        target_path = os.path.join(base_dir, "logs", filename)

        if not os.path.exists(target_path):
            return jsonify(lines=[], total=0, file=filename)

        try:
            with open(target_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            recent_lines = [l.rstrip("\r\n") for l in all_lines[-limit:]]
            return jsonify(lines=recent_lines, total=len(all_lines), file=filename)
        except Exception as err:
            return jsonify(error=f"Gagal membaca berkas log: {err}"), 500

    @app.errorhandler(404)
    def not_found(_):
        return jsonify(error="Endpoint tidak ditemukan", path=request.path), 404

    @app.errorhandler(413)
    def too_large(_):
        return jsonify(error="Ukuran request terlalu besar (maksimal 50MB)"), 413

    @app.errorhandler(500)
    @app.errorhandler(Exception)
    def handle_exception(e):
        try:
            db.session.rollback()
        except Exception:
            pass
        import traceback
        tb = traceback.format_exc()
        if hasattr(app, "api_logger"):
            app.api_logger.error(
                f"[UNHANDLED EXCEPTION] {request.method} {request.path}\n  -> Error: {e}\n{tb}"
            )
        return jsonify(
            error="Terjadi kesalahan server internal",
            detail=str(e),
            path=request.path,
            request_id=getattr(g, "request_id", None),
        ), 500

    @jwt.unauthorized_loader
    def _unauth(msg):
        return jsonify(error="Token tidak ditemukan / tidak valid", detail=msg), 401

    @jwt.expired_token_loader
    def _expired(_h, _p):
        return jsonify(error="Sesi berakhir, silakan login kembali"), 401

    class ApiPrefixMiddleware:
        def __init__(self, wsgi_app):
            self.wsgi_app = wsgi_app

        def __call__(self, environ, start_response):
            path = environ.get("PATH_INFO", "")
            if path and not path.startswith("/api") and not path.startswith("/static"):
                environ["PATH_INFO"] = "/api" + path
            return self.wsgi_app(environ, start_response)

    app.wsgi_app = ApiPrefixMiddleware(app.wsgi_app)

    return app
