"""Modul Integrasi Otomatisasi e-KTA Digital NasDem DPP."""
from app.dpp_integration.routes import bp as dpp_bp
from app.dpp_integration.client import get_dpp_client

__all__ = ["dpp_bp", "get_dpp_client"]

