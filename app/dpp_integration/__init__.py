"""Modul Integrasi Otomatisasi e-KTA Digital NasDem DPP."""
from .routes import bp as dpp_bp
from .client import get_dpp_client

__all__ = ["dpp_bp", "get_dpp_client"]
