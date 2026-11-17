import logging
import uuid
from flask import Blueprint, jsonify, request
from sqlalchemy import text

from app.auth import current_claims, inventory_access_required
from app.extensions import db
from app.models import InventoryItem
from app.services.image_storage import (
    delete_physical_image,
    is_base64_data_uri,
    optimize_and_save_image,
    resolve_stored_image_url,
)

bp = Blueprint("inventory", __name__)
logger = logging.getLogger("nasdem.inventory")


def ensure_inventory_table_exists():
    """Memastikan tabel inventory_items tersedia dan bersih dari data dummy awal."""
    try:
        db.session.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS inventory_items (
                    id VARCHAR(50) PRIMARY KEY,
                    kode_barang VARCHAR(50) NOT NULL,
                    nama_barang VARCHAR(255) NOT NULL,
                    kategori VARCHAR(100) NOT NULL DEFAULT 'Furnitur & Mebel',
                    lantai INTEGER NOT NULL DEFAULT 1,
                    ruangan VARCHAR(150) NOT NULL DEFAULT 'Ruang Utama',
                    jumlah INTEGER NOT NULL DEFAULT 1,
                    jumlah_baik INTEGER NOT NULL DEFAULT 1,
                    jumlah_perbaikan INTEGER NOT NULL DEFAULT 0,
                    jumlah_rusak INTEGER NOT NULL DEFAULT 0,
                    satuan VARCHAR(50) NOT NULL DEFAULT 'Unit',
                    kondisi VARCHAR(50) NOT NULL DEFAULT 'Baik',
                    merk_spesifikasi VARCHAR(255),
                    keterangan TEXT,
                    penanggung_jawab VARCHAR(150),
                    foto_url TEXT,
                    diubah_oleh VARCHAR(150),
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                """
            )
        )
        # Hapus data dummy otomatis jika pernah ter-seed sebelumnya
        db.session.execute(
            text("DELETE FROM inventory_items WHERE diubah_oleh = 'Sistem Inventaris DPW'")
        )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.warning(f"Inisialisasi tabel inventory_items dilewati: {exc}")


def _item_to_dict(item: InventoryItem) -> dict:
    resolved_foto = resolve_stored_image_url(item.foto_url) if item.foto_url else ""
    return {
        "id": item.id,
        "kodeBarang": item.kode_barang,
        "namaBarang": item.nama_barang,
        "kategori": item.kategori,
        "lantai": int(item.lantai or 1),
        "ruangan": item.ruangan or "Ruang Utama",
        "jumlah": int(item.jumlah or 0),
        "jumlahBaik": int(item.jumlah_baik if item.jumlah_baik is not None else (item.jumlah or 0)),
        "jumlahPerbaikan": int(item.jumlah_perbaikan or 0),
        "jumlahRusak": int(item.jumlah_rusak or 0),
        "satuan": item.satuan or "Unit",
        "kondisi": item.kondisi or "Baik",
        "merkSpesifikasi": item.merk_spesifikasi or "",
        "keterangan": item.keterangan or "",
        "penanggungJawab": item.penanggung_jawab or "",
        "fotoUrl": resolved_foto or "",
        "diubahOleh": item.diubah_oleh or "",
        "createdAt": item.created_at.isoformat() if item.created_at else None,
        "updatedAt": item.updated_at.isoformat() if item.updated_at else None,
    }


def _derive_condition(jumlah_baik: int, jumlah_perbaikan: int, jumlah_rusak: int, explicit_kondisi: str = "") -> str:
    if jumlah_rusak > 0 and jumlah_baik == 0 and jumlah_perbaikan == 0:
        return "Rusak"
    if jumlah_perbaikan > 0 or jumlah_rusak > 0:
        return "Perlu Perbaikan"
    if explicit_kondisi in ("Baik", "Perlu Perbaikan", "Rusak"):
        return explicit_kondisi
    return "Baik"


def _process_inventory_photo(raw_foto: str, identifier: str) -> str:
    if not raw_foto or not isinstance(raw_foto, str):
        return ""
    raw_foto = raw_foto.strip()
    if is_base64_data_uri(raw_foto) or len(raw_foto) > 600:
        try:
            return optimize_and_save_image(raw_foto, category="inventory", identifier=identifier)
        except Exception as exc:
            logger.error(f"Gagal memproses foto inventaris {identifier}: {exc}")
            return ""
    return raw_foto


@bp.get("/inventory")
@inventory_access_required
def list_inventory():
    """Mengambil seluruh daftar barang inventaris gedung 5 lantai."""
    lantai_param = request.args.get("lantai")
    kategori_param = request.args.get("kategori")
    kondisi_param = request.args.get("kondisi")
    search_param = (request.args.get("search") or "").strip().lower()

    query = InventoryItem.query

    if lantai_param and lantai_param not in ("ALL", "0"):
        try:
            query = query.filter(InventoryItem.lantai == int(lantai_param))
        except ValueError:
            pass

    if kategori_param and kategori_param != "ALL":
        query = query.filter(InventoryItem.kategori == kategori_param)

    if kondisi_param and kondisi_param != "ALL":
        query = query.filter(InventoryItem.kondisi == kondisi_param)

    items = query.order_by(InventoryItem.lantai.asc(), InventoryItem.kode_barang.asc()).all()
    result = [_item_to_dict(it) for it in items]

    if search_param:
        result = [
            r
            for r in result
            if search_param in (r["namaBarang"] or "").lower()
            or search_param in (r["kodeBarang"] or "").lower()
            or search_param in (r["ruangan"] or "").lower()
            or search_param in (r["kategori"] or "").lower()
            or search_param in (r["merkSpesifikasi"] or "").lower()
        ]

    return jsonify(result)


@bp.post("/inventory")
@inventory_access_required
def create_inventory_item():
    """Menambahkan barang inventaris baru ke salah satu lantai (1-5)."""
    claims = current_claims()
    actor = claims.get("adminName") or claims.get("full_name") or "Admin DPW"

    data = request.get_json(silent=True) or {}
    nama_barang = (data.get("namaBarang") or data.get("nama_barang") or "").strip()
    if not nama_barang:
        return jsonify(error="Nama barang wajib diisi"), 400

    try:
        lantai = max(1, min(5, int(data.get("lantai") or 1)))
    except (ValueError, TypeError):
        lantai = 1

    item_id = (data.get("id") or f"inv-l{lantai}-{uuid.uuid4().hex[:8]}").strip()

    # Buat kode barang otomatis jika kosong
    kode_barang = (data.get("kodeBarang") or data.get("kode_barang") or "").strip()
    if not kode_barang:
        floor_count = InventoryItem.query.filter_by(lantai=lantai).count() + 1
        kode_barang = f"INV-L{lantai}-{floor_count:03d}"

    try:
        jumlah_baik = max(0, int(data.get("jumlahBaik", data.get("jumlah", 1))))
        jumlah_perbaikan = max(0, int(data.get("jumlahPerbaikan", 0)))
        jumlah_rusak = max(0, int(data.get("jumlahRusak", 0)))
    except (ValueError, TypeError):
        jumlah_baik, jumlah_perbaikan, jumlah_rusak = 1, 0, 0

    total_breakdown = jumlah_baik + jumlah_perbaikan + jumlah_rusak
    try:
        raw_jumlah = int(data.get("jumlah", total_breakdown))
    except (ValueError, TypeError):
        raw_jumlah = total_breakdown

    if total_breakdown > 0 and raw_jumlah != total_breakdown:
        jumlah = total_breakdown
    else:
        jumlah = max(0, raw_jumlah)
        if total_breakdown == 0 and jumlah > 0:
            jumlah_baik = jumlah

    kondisi = _derive_condition(jumlah_baik, jumlah_perbaikan, jumlah_rusak, data.get("kondisi") or "")
    foto_url = _process_inventory_photo(data.get("fotoUrl") or data.get("foto_url") or "", item_id)

    item = InventoryItem()
    item.id = item_id
    item.kode_barang = kode_barang
    item.nama_barang = nama_barang
    item.kategori = (data.get("kategori") or "Furnitur & Mebel").strip()
    item.lantai = lantai
    item.ruangan = (data.get("ruangan") or f"Ruang Lantai {lantai}").strip()
    item.jumlah = jumlah
    item.jumlah_baik = jumlah_baik
    item.jumlah_perbaikan = jumlah_perbaikan
    item.jumlah_rusak = jumlah_rusak
    item.satuan = (data.get("satuan") or "Unit").strip()
    item.kondisi = kondisi
    item.merk_spesifikasi = (data.get("merkSpesifikasi") or data.get("merk_spesifikasi") or "").strip()
    item.keterangan = (data.get("keterangan") or "").strip()
    item.penanggung_jawab = (data.get("penanggungJawab") or data.get("penanggung_jawab") or "").strip()
    item.foto_url = foto_url
    item.diubah_oleh = actor

    db.session.add(item)
    db.session.commit()
    return jsonify(_item_to_dict(item)), 201


@bp.put("/inventory/<item_id>")
@inventory_access_required
def update_inventory_item(item_id: str):
    """Memperbarui detail, lokasi lantai/ruangan, stok, atau foto barang inventaris."""
    claims = current_claims()
    actor = claims.get("adminName") or claims.get("full_name") or "Admin DPW"

    item = db.session.get(InventoryItem, item_id)
    if not item:
        return jsonify(error="Barang inventaris tidak ditemukan"), 404

    data = request.get_json(silent=True) or {}

    if "namaBarang" in data or "nama_barang" in data:
        nama = (data.get("namaBarang") or data.get("nama_barang") or "").strip()
        if nama:
            item.nama_barang = nama

    if "kodeBarang" in data or "kode_barang" in data:
        kode = (data.get("kodeBarang") or data.get("kode_barang") or "").strip()
        if kode:
            item.kode_barang = kode

    if "kategori" in data:
        item.kategori = (data.get("kategori") or "Furnitur & Mebel").strip()

    if "lantai" in data:
        try:
            item.lantai = max(1, min(5, int(data.get("lantai") or 1)))
        except (ValueError, TypeError):
            pass

    if "ruangan" in data:
        item.ruangan = (data.get("ruangan") or f"Ruang Lantai {item.lantai}").strip()

    if "satuan" in data:
        item.satuan = (data.get("satuan") or "Unit").strip()

    if "merkSpesifikasi" in data or "merk_spesifikasi" in data:
        item.merk_spesifikasi = (data.get("merkSpesifikasi") or data.get("merk_spesifikasi") or "").strip()

    if "keterangan" in data:
        item.keterangan = (data.get("keterangan") or "").strip()

    if "penanggungJawab" in data or "penanggung_jawab" in data:
        item.penanggung_jawab = (data.get("penanggungJawab") or data.get("penanggung_jawab") or "").strip()

    # Penanganan kuantitas & rincian kondisi
    has_breakdown = any(k in data for k in ("jumlahBaik", "jumlahPerbaikan", "jumlahRusak"))
    if has_breakdown:
        jb = max(0, int(data.get("jumlahBaik", item.jumlah_baik or 0)))
        jp = max(0, int(data.get("jumlahPerbaikan", item.jumlah_perbaikan or 0)))
        jr = max(0, int(data.get("jumlahRusak", item.jumlah_rusak or 0)))
        item.jumlah_baik = jb
        item.jumlah_perbaikan = jp
        item.jumlah_rusak = jr
        item.jumlah = jb + jp + jr
        item.kondisi = _derive_condition(jb, jp, jr, data.get("kondisi") or item.kondisi)
    elif "jumlah" in data:
        new_total = max(0, int(data.get("jumlah") or 0))
        diff = new_total - (item.jumlah or 0)
        item.jumlah = new_total
        item.jumlah_baik = max(0, (item.jumlah_baik or 0) + diff)
        if item.jumlah_baik + (item.jumlah_perbaikan or 0) + (item.jumlah_rusak or 0) != new_total:
            item.jumlah_baik = max(0, new_total - (item.jumlah_perbaikan or 0) - (item.jumlah_rusak or 0))
        item.kondisi = _derive_condition(
            item.jumlah_baik, item.jumlah_perbaikan or 0, item.jumlah_rusak or 0, data.get("kondisi") or item.kondisi
        )
    elif "kondisi" in data:
        item.kondisi = data.get("kondisi") or item.kondisi

    if "fotoUrl" in data or "foto_url" in data:
        raw_foto = data.get("fotoUrl") if "fotoUrl" in data else data.get("foto_url")
        if not raw_foto:
            if item.foto_url:
                delete_physical_image(item.foto_url)
            item.foto_url = ""
        else:
            item.foto_url = _process_inventory_photo(raw_foto, item.id)

    item.diubah_oleh = actor
    db.session.commit()
    return jsonify(_item_to_dict(item))


@bp.delete("/inventory/<item_id>")
@inventory_access_required
def delete_inventory_item(item_id: str):
    """Menghapus barang inventaris dari database beserta berkas fotonya."""
    item = db.session.get(InventoryItem, item_id)
    if not item:
        return jsonify(error="Barang inventaris tidak ditemukan"), 404

    if item.foto_url:
        delete_physical_image(item.foto_url)

    db.session.delete(item)
    db.session.commit()
    return jsonify(success=True, id=item_id)
