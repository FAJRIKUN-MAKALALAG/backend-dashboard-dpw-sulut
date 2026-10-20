"""Membuat tabel (jika belum ada) dan Super Admin awal.

Jalankan:  python -m scripts.seed_admin
"""
import os

from werkzeug.security import generate_password_hash

from app import create_app
from app.extensions import db
from app.models import AppUser

app = create_app()

with app.app_context():
    db.create_all()
    username = os.getenv("SEED_ADMIN_USERNAME", "7106033003050001")
    password = os.getenv("SEED_ADMIN_PASSWORD")
    if not password:
        raise SystemExit("Set SEED_ADMIN_PASSWORD di .env terlebih dahulu.")

    user = AppUser.query.filter_by(username=username).first() or AppUser(username=username)
    user.password_hash = generate_password_hash(password)
    user.full_name = "Super Administrator DPW NasDem"
    user.member_nik = None
    user.nomor_ekta = "KT-7106-2024-0001"
    user.role = "SUPER_ADMIN"
    user.dpd_access = user.dpc_access = user.dprt_access = "ALL"
    user.jabatan = "Super Administrator DPW Partai NasDem Sulut"
    user.is_active = True
    db.session.add(user)
    db.session.commit()
    print(f"Super Admin '{username}' siap digunakan.")
