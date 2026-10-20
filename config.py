"""Konfigurasi terpusat backend & database (dibaca dari environment / .env)."""
import os
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv()


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
    # Connection pool untuk beban data besar
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_size": _int("DB_POOL_SIZE", 10),
        "max_overflow": _int("DB_MAX_OVERFLOW", 20),
        "pool_recycle": _int("DB_POOL_RECYCLE", 1800),
        "pool_pre_ping": True,
    }


class AppConfig(DatabaseConfig):
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "dev-jwt-secret-change-me")
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=_int("JWT_ACCESS_HOURS", 12))
    CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()]

    MAX_PAGE_SIZE = _int("MAX_PAGE_SIZE", 500)
    BULK_CHUNK_SIZE = _int("BULK_CHUNK_SIZE", 1000)
    BULK_MAX_ROWS = _int("BULK_MAX_ROWS", 20000)
    # Foto base64 bisa besar; izinkan body hingga 50 MB untuk import massal
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024
    JSON_SORT_KEYS = False

    PROVINCE_NAME = "Sulawesi Utara"
    PROVINCE_CODE = "71"


class DevelopmentConfig(AppConfig):
    DEBUG = True


class ProductionConfig(AppConfig):
    DEBUG = False


def get_config():
    return ProductionConfig if os.getenv("FLASK_ENV") == "production" else DevelopmentConfig
