"""Kosongkan data anggota lalu buat ulang tabel `members` (1 baris per NIK) dan `positions` (jabatan).

Jalankan:  python -m scripts.reset_members_schema
PERINGATAN: menghapus SELURUH data members & positions. Tabel app_users, audit_logs, deletion_requests tidak disentuh.
"""
from sqlalchemy import text

from app import create_app
from app.extensions import db

app = create_app()

with app.app_context():
    db.session.execute(text("ALTER TABLE IF EXISTS app_users DROP CONSTRAINT IF EXISTS app_users_member_nik_fkey"))
    db.session.execute(text("DROP TABLE IF EXISTS positions CASCADE"))
    db.session.execute(text("DROP TABLE IF EXISTS members CASCADE"))
    db.session.commit()
    db.create_all()
    print("Tabel members & positions dibuat ulang (kosong).")
