"""Konfigurasi terpusat backend & database (dibaca dari environment / .env)."""
import os
from datetime import timedelta

from dotenv import load_dotenv

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BASE_DIR, ".env"))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


class DatabaseConfig:
    """Pengaturan koneksi database - dipisahkan dari konfigurasi aplikasi."""

    _raw_db = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://postgres:password@localhost:5432/dpw_sulut",
    )
    if _raw_db.startswith("postgresql://"):
        _raw_db = _raw_db.replace("postgresql://", "postgresql+psycopg2://", 1)
    SQLALCHEMY_DATABASE_URI = _raw_db
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Connection pool untuk beban data besar & 100+ concurrent users (aman untuk PostgreSQL / Supabase Pooler)
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_size": _int("DB_POOL_SIZE", 8),
        "max_overflow": _int("DB_MAX_OVERFLOW", 12),
        "pool_timeout": _int("DB_POOL_TIMEOUT", 30),
        "pool_recycle": _int("DB_POOL_RECYCLE", 1800),
        "pool_pre_ping": True,
        "connect_args": {"connect_timeout": 10},
    }


_PROD_ORIGINS = (
    "https://digital.nasdemsulut.io",
    "http://digital.nasdemsulut.io",
    "https://nasdemsulut.io",
    "https://www.nasdemsulut.io",
)

_DEV_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)


def _parse_cors_origins(include_dev: bool = False) -> list[str]:
    """Membangun daftar whitelist origin CORS yang ketat (tanpa wildcard '*')."""
    raw_cors = os.getenv("CORS_ORIGINS", "")
    parsed: list[str] = []
    for item in raw_cors.split(","):
        origin = item.strip().rstrip("/")
        if not origin or origin == "*":
            continue
        if origin not in parsed:
            parsed.append(origin)

    for prod_origin in _PROD_ORIGINS:
        if prod_origin not in parsed:
            parsed.append(prod_origin)

    if include_dev:
        for dev_origin in _DEV_ORIGINS:
            if dev_origin not in parsed:
                parsed.append(dev_origin)

    return parsed


class AppConfig(DatabaseConfig):
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "dev-jwt-secret-change-me")

    # Masa berlaku session login (JWT Token).
    # Default: 30 hari (720 jam) agar kader, pengurus wilayah, dan admin tidak sering ter-logout.
    # Dapat diatur lewat JWT_ACCESS_DAYS atau JWT_ACCESS_HOURS di .env.
    _jwt_days = _int("JWT_ACCESS_DAYS", 30)
    _jwt_hours = _int("JWT_ACCESS_HOURS", 0)
    if _jwt_hours > 0 and "JWT_ACCESS_DAYS" not in os.environ:
        JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=max(_jwt_hours, 24))
    else:
        JWT_ACCESS_TOKEN_EXPIRES = timedelta(days=_jwt_days if _jwt_days > 0 else 30)

    CORS_ORIGINS = _parse_cors_origins(include_dev=True)

    MAX_PAGE_SIZE = _int("MAX_PAGE_SIZE", 500)
    BULK_CHUNK_SIZE = _int("BULK_CHUNK_SIZE", 1000)
    BULK_MAX_ROWS = _int("BULK_MAX_ROWS", 20000)
    CACHE_TTL_SECONDS = _int("CACHE_TTL_SECONDS", 8)
    # Foto base64 bisa besar; izinkan body hingga 50 MB untuk import massal
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024
    JSON_SORT_KEYS = False

    PROVINCE_NAME = "Sulawesi Utara"
    PROVINCE_CODE = "71"


class DevelopmentConfig(AppConfig):
    DEBUG = True
    CORS_ORIGINS = _parse_cors_origins(include_dev=True)


_INSECURE_SECRET_VALUES = {
    "",
    "dev-secret-change-me",
    "dev-jwt-secret-change-me",
    "changeme",
    "secret",
}


class ProductionConfig(AppConfig):
    DEBUG = False
    CORS_ORIGINS = _parse_cors_origins(include_dev=False)


def validate_production_secrets(cfg_cls=ProductionConfig):
    """Fail-fast di mode produksi agar server tidak pernah berjalan dengan secret key default/lemah."""
    secret_key = (os.getenv("SECRET_KEY") or getattr(cfg_cls, "SECRET_KEY", "") or "").strip()
    jwt_secret = (os.getenv("JWT_SECRET_KEY") or getattr(cfg_cls, "JWT_SECRET_KEY", "") or "").strip()

    cfg_cls.SECRET_KEY = secret_key
    cfg_cls.JWT_SECRET_KEY = jwt_secret

    invalid = []
    if secret_key.lower() in _INSECURE_SECRET_VALUES or len(secret_key) < 16:
        invalid.append("SECRET_KEY")
    if jwt_secret.lower() in _INSECURE_SECRET_VALUES or len(jwt_secret) < 16:
        invalid.append("JWT_SECRET_KEY")

    if invalid:
        raise RuntimeError(
            "Konfigurasi produksi tidak aman: variabel "
            + ", ".join(invalid)
            + " wajib diatur di environment/.env minimal 16 karakter acak yang kuat."
        )


def get_config():
    if os.getenv("FLASK_ENV") == "production":
        ProductionConfig.CORS_ORIGINS = _parse_cors_origins(include_dev=False)
        validate_production_secrets(ProductionConfig)
        return ProductionConfig
    DevelopmentConfig.CORS_ORIGINS = _parse_cors_origins(include_dev=True)
    return DevelopmentConfig
