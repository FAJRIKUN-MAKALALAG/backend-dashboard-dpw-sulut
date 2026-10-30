"""Endpoint untuk upload dan serving berkas fisik gambar (Foto KTP & Profil)."""
import logging
import os
from flask import Blueprint, jsonify, request, send_from_directory, current_app
from ..auth import login_required, current_claims
from ..services.image_storage import UPLOADS_DIR, optimize_and_save_image

bp = Blueprint("upload", __name__)
logger = logging.getLogger("nasdem.upload")


@bp.get("/uploads/<category>/<filename>")
def serve_uploaded_file(category: str, filename: str):
    """Menampilkan atau mengunduh berkas fisik foto KTP atau Profil."""
    cat = (category or "").lower().strip()
    if cat not in ("profile", "ktp"):
        return jsonify(error="Kategori berkas tidak valid"), 404

    target_dir = os.path.join(UPLOADS_DIR, cat)
    full_path = os.path.join(target_dir, filename)
    if not os.path.exists(full_path):
        logger.warning(f"[UPLOAD-SERVE-404] Berkas foto {cat} tidak ditemukan: {filename} di {target_dir}")
        return jsonify(error="Berkas tidak ditemukan"), 404

    # Cache browser 7 hari untuk efisiensi loading + header CORP & CORS
    resp = send_from_directory(target_dir, filename, max_age=86400 * 7)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    return resp


@bp.post("/upload")
@login_required
def upload_file():
    """Endpoint upload berkas mandiri untuk foto profil atau KTP."""
    claims = current_claims()
    category = request.form.get("category") or (request.json.get("category") if request.is_json else "profile")
    category = (category or "profile").lower().strip()

    actor_name = claims.get("adminName") or "kader"
    identifier = claims.get("adminNik") or "kader"

    logger.info(f"[UPLOAD-REQUEST] Menerima request upload Foto {category.upper()} dari '{actor_name}' (NIK/ID: {identifier})")

    # 1. Dari multipart/form-data
    if "file" in request.files:
        uploaded_file = request.files["file"]
        if not uploaded_file or uploaded_file.filename == "":
            logger.warning(f"[UPLOAD-EMPTY] File upload kosong dari '{actor_name}'")
            return jsonify(error="Tidak ada berkas yang dipilih"), 400
        try:
            url = optimize_and_save_image(
                uploaded_file,
                category=category,
                identifier=identifier,
            )
            logger.info(f"[UPLOAD-SUCCESS] Berkas Foto {category.upper()} berhasil disimpan via Form-Data: {url}")
            return jsonify(url=url, success=True)
        except Exception as e:
            logger.error(f"[UPLOAD-FAILED] Gagal memproses berkas Form-Data: {e}", exc_info=True)
            return jsonify(error=f"Gagal memproses gambar: {str(e)}"), 400

    # 2. Dari JSON body (data:image/...)
    data = request.get_json(silent=True) or {}
    image_data = data.get("image") or data.get("base64")
    if image_data:
        target_id = data.get("nik") or identifier
        try:
            url = optimize_and_save_image(
                image_data,
                category=category,
                identifier=target_id,
            )
            logger.info(f"[UPLOAD-SUCCESS] Berkas Foto {category.upper()} berhasil disimpan via Base64/JSON: {url} (ID: {target_id})")
            return jsonify(url=url, success=True)
        except Exception as e:
            logger.error(f"[UPLOAD-FAILED] Gagal memproses gambar Base64/JSON: {e}", exc_info=True)
            return jsonify(error=f"Gagal memproses gambar: {str(e)}"), 400

    logger.warning(f"[UPLOAD-NO-ATTACHMENT] Request upload dari '{actor_name}' tidak membawa lampiran file maupun data gambar")
    return jsonify(error="Lampiran berkas atau data gambar tidak ditemukan"), 400
