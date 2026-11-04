"""
Migrasi Position ID:
Memperbarui ID posisi lama (hash 6 parameter tanpa jabatan) ke format baru
(hash 7 parameter menyertakan nama jabatan) agar mendukung rangkap jabatan
tanpa tabrakan ID.
"""
from app import create_app
from app.extensions import db
from app.models import Position
from app.utils import make_position_id


def migrate_positions():
    app = create_app()
    with app.app_context():
        positions = Position.query.all()
        print(f"Memeriksa {len(positions)} baris posisi...")
        updated = 0
        seen_ids = set()

        for p in positions:
            new_id = make_position_id(
                p.member_nik,
                p.tingkat_kepengurusan,
                p.dpd,
                p.dpc,
                p.dprt,
                p.wilayah_penugasan,
                p.jabatan,
            )
            if new_id != p.id and new_id not in seen_ids:
                p.id = new_id
                seen_ids.add(new_id)
                updated += 1
            else:
                seen_ids.add(p.id)

        if updated > 0:
            db.session.commit()
            print(f"Berhasil memperbarui {updated} ID posisi ke format baru!")
        else:
            print("Semua ID posisi sudah mutakhir atau tidak memerlukan migrasi.")


if __name__ == "__main__":
    migrate_positions()
