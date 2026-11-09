"""Script untuk membuat dan menginisialisasi tabel `structure_slots` (kursi jabatan) di PostgreSQL.

Menyimpan seluruh kursi baku DPW, 15 DPD Kabupaten/Kota, serta DPC/DPRt ke dalam database
meskipun kursi tersebut belum ada pengisinya (`member_nik = NULL`).

Cara jalankan:
    python scripts/seed_structure_slots.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app, db
from app.models import Position, StructureSlot
from app.routes.structure_slots import ensure_structure_slots_table_exists, ensure_territory_slots_initialized

SULUT_DPD_LIST = [
    "Kota Manado",
    "Kota Bitung",
    "Kota Tomohon",
    "Kota Kotamobagu",
    "Kabupaten Minahasa",
    "Kabupaten Minahasa Utara",
    "Kabupaten Minahasa Selatan",
    "Kabupaten Minahasa Tenggara",
    "Kabupaten Bolaang Mongondow",
    "Kabupaten Bolaang Mongondow Utara",
    "Kabupaten Bolaang Mongondow Timur",
    "Kabupaten Bolaang Mongondow Selatan",
    "Kabupaten Kepulauan Sangihe",
    "Kabupaten Kepulauan Talaud",
    "Kabupaten Kepulauan Siau Tagulandang Biaro",
]


def main():
    app = create_app()
    with app.app_context():
        ensure_structure_slots_table_exists()
        print("1. Menginisialisasi kursi jabatan DPW Sulawesi Utara...")
        dpw_slots = ensure_territory_slots_initialized("DPW", "Sulawesi Utara", "Wilayah DPW", "Pusat/Umum")
        print(f"   -> {len(dpw_slots)} kursi DPW tercatat di database.")

        print("2. Menginisialisasi kursi jabatan 15 DPD Kabupaten/Kota...")
        total_dpd_slots = 0
        for dpd_name in SULUT_DPD_LIST:
            slots = ensure_territory_slots_initialized("DPD", dpd_name, "Umum (DPD)", "Pusat/Umum")
            total_dpd_slots += len(slots)
        print(f"   -> {total_dpd_slots} kursi DPD tercatat di database.")

        print("3. Menginisialisasi kursi jabatan DPC & DPRt yang aktif di database...")
        dpc_territories = (
            db.session.query(Position.dpd, Position.dpc)
            .filter(Position.tingkat_kepengurusan == "DPC")
            .distinct()
            .all()
        )
        total_dpc_slots = 0
        for dpd_name, dpc_name in dpc_territories:
            if dpd_name and dpc_name and dpc_name != "-":
                slots = ensure_territory_slots_initialized("DPC", dpd_name, dpc_name, "Pusat/Umum")
                total_dpc_slots += len(slots)
        print(f"   -> {total_dpc_slots} kursi DPC tercatat di database.")

        dprt_territories = (
            db.session.query(Position.dpd, Position.dpc, Position.dprt)
            .filter(Position.tingkat_kepengurusan == "DPRt")
            .distinct()
            .all()
        )
        total_dprt_slots = 0
        for dpd_name, dpc_name, dprt_name in dprt_territories:
            if dpd_name and dpc_name and dprt_name and dprt_name != "-":
                slots = ensure_territory_slots_initialized("DPRt", dpd_name, dpc_name, dprt_name)
                total_dprt_slots += len(slots)
        print(f"   -> {total_dprt_slots} kursi DPRt tercatat di database.")

        total_in_db = StructureSlot.query.count()
        print(f"\nSelesai! Total {total_in_db} kursi jabatan kini tersimpan di tabel `structure_slots` PostgreSQL.")


if __name__ == "__main__":
    main()

