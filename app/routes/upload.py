"""Endpoint untuk upload dan serving berkas fisik gambar (Foto KTP & Profil)."""
import os
from flask import Blueprint, jsonify, request, send_from_directory, current_app
from ..auth import login_required, current_claims
from ..services.image_storage import UPLOADS_DIR, optimize_and_save_image

bp = Blueprint("upload", __name__)


@bp.get("/uploads/<category>/<filename>")
def serve_uploaded_file(category: str, filename: str):
    """Menampilkan atau mengunduh berkas fisik foto KTP atau Profil."""
    cat = (category or "").lower().strip()
    if cat not in ("profile", "ktp"):
        return jsonify(error="Kategori berkas tidak valid"), 404

    target_dir = os.path.join(UPLOADS_DIR, cat)
    if not os.path.exists(os.path.join(target_dir, filename)):
        return jsonify(error="Berkas tidak ditemukan"), 404

    # Cache browser 7 hari untuk efisiensi loading
    return send_from_directory(target_dir, filename, max_age=86400 * 7)


@bp.post("/upload")
@login_required
def upload_file():
    """Endpoint upload berkas mandiri untuk foto profil atau KTP."""
    claims = current_claims()
    category = request.form.get("category") or request.json.get("category") if request.is_json else "profile"
    category = (category or "profile").lower().strip()

    identifier = claims.get("adminNik") or "kader"

    # 1. Dari multipart/form-data
    if "file" in request.files:
        uploaded_file = request.files["file"]
        if not uploaded_file or uploaded_file.filename == "":
            return jsonify(error="Tidak ada berkas yang dipilih"), 400
        try:
            url = optimize_and_save_image(
                uploaded_file,
                category=category,
                identifier=identifier,
            )
            return jsonify(url=url, success=True)
        except Exception as e:
            return jsonify(error=f"Gagal memproses gambar: {str(e)}"), 400

    # 2. Dari JSON body (data:image/...)
    data = request.get_json(silent=True) or {}
    image_data = data.get("image") or data.get("base64")
    if image_data:
        try:
            url = optimize_and_save_image(
                image_data,
                category=category,
                identifier=data.get("nik") or identifier,
            )
            return jsonify(url=url, success=True)
        except Exception as e:
            return jsonify(error=f"Gagal memproses gambar: {str(e)}"), 400

    return jsonify(error="Lampiran berkas atau data gambar tidak ditemukan"), 400
