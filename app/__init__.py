from flask import Flask, jsonify
from flask_cors import CORS

from config import get_config
from .extensions import db, jwt, migrate


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(get_config())

    db.init_app(app)
    migrate.init_app(app, db)
    jwt.init_app(app)
    CORS(app, resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}})

    from . import models  # noqa: F401  (registrasi model untuk migrasi)
    from .routes import register_blueprints

    register_blueprints(app)

    @app.get("/api/health")
    def health():
        try:
            db.session.execute(db.text("SELECT 1"))
            return jsonify(status="ok", database="up")
        except Exception as exc:  # pragma: no cover
            return jsonify(status="error", database=str(exc)), 503

    @app.errorhandler(404)
    def not_found(_):
        return jsonify(error="Endpoint tidak ditemukan"), 404

    @app.errorhandler(413)
    def too_large(_):
        return jsonify(error="Ukuran request terlalu besar"), 413

    @jwt.unauthorized_loader
    def _unauth(msg):
        return jsonify(error="Token tidak ditemukan / tidak valid", detail=msg), 401

    @jwt.expired_token_loader
    def _expired(_h, _p):
        return jsonify(error="Sesi berakhir, silakan login kembali"), 401

    return app
