"""Endpoint untuk upload dan serving berkas fisik gambar (Foto KTP & Profil) dengan proteksi media sensitif."""
import logging
import os
from flask import Blueprint, jsonify, request, send_from_directory, Response
from flask_jwt_extended import get_jwt, verify_jwt_in_request
from app.auth import login_required, current_claims
from app.models import Member
from app.services.image_storage import (
    SENSITIVE_MEDIA_CATEGORIES,
    UPLOADS_DIR,
    optimize_and_save_image,
    sign_media_url,
    verify_media_signature,
)
from app.utils import TERRITORIAL_MANAGER_ROLES, member_in_scope

bp = Blueprint("upload", __name__)
logger = logging.getLogger("nasdem.upload")


def _is_safe_filename(filename: str) -> bool:
    if not filename:
        return False
    if "/" in filename or "\\" in filename or ".." in filename or "\x00" in filename:
        return False
    return os.path.basename(filename) == filename


@bp.get("/uploads/<category>/<filename>")
def serve_uploaded_file(category: str, filename: str):
    """Menampilkan atau mengunduh berkas fisik foto KTP, Profil, atau Inventaris."""
    cat = (category or "").lower().strip()
    if cat not in ("profile", "ktp", "inventory", "finance"):
        resp = Response(b"", status=404, mimetype="image/webp")
        resp.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    if not _is_safe_filename(filename):
        return jsonify(error="Nama berkas tidak valid"), 400

    is_sensitive = cat in SENSITIVE_MEDIA_CATEGORIES
    if is_sensitive:
        # Wajib memiliki tanda tangan HMAC-SHA256 valid ATAU JWT terautentikasi sesuai scope wilayah
        has_valid_sig = verify_media_signature(
            cat,
            filename,
            request.args.get("exp"),
            request.args.get("sig"),
        )
        if not has_valid_sig:
            claims = None
            try:
                verify_jwt_in_request(optional=True)
                claims = get_jwt() or None
            except Exception:
                claims = None

            if not claims:
                return jsonify(error="Akses berkas sensitif memerlukan autentikasi"), 401

            if cat == "ktp" and "_ktp_" in filename:
                file_nik = filename.split("_ktp_", 1)[0].strip()
                role = claims.get("role")
                my_nik = str(claims.get("adminNik") or "").strip()
                if role == "ANGGOTA" and file_nik != my_nik:
                    return jsonify(error="Akses foto KTP kader lain ditolak"), 403
                if file_nik.isdigit():
                    try:
                        m = Member.query.filter(Member.nik == file_nik).first()
                    except Exception:
                        m = None
                    if m and not member_in_scope(claims, m):
                        return jsonify(error="Foto KTP berada di luar cakupan wilayah Anda"), 403

    target_dir = os.path.join(UPLOADS_DIR, cat)
    full_path = os.path.join(target_dir, filename)
    if not os.path.isfile(full_path):
        # Jangan izinkan tebak prefix tanpa hash persis untuk kategori sensitif (KTP / Keuangan)
        prefix_match = None
        marker = f"_{cat}_"
        if not is_sensitive and marker in filename and os.path.isdir(target_dir):
            prefix = filename.split(marker)[0] + marker
            try:
                candidates = [
                    f
                    for f in os.listdir(target_dir)
                    if f.startswith(prefix)
                    and f.endswith(".webp")
                    and os.path.isfile(os.path.join(target_dir, f))
                ]
                if candidates:
                    candidates.sort(
                        key=lambda f: os.path.getmtime(os.path.join(target_dir, f)),
                        reverse=True,
                    )
                    prefix_match = candidates[0]
            except OSError:
                prefix_match = None

        if prefix_match:
            logger.info(
                f"[UPLOAD-SERVE-FALLBACK] {filename} dialihkan ke berkas fisik yang tersedia: {prefix_match}"
            )
            filename = prefix_match
        else:
            logger.warning(f"[UPLOAD-SERVE-404] Berkas foto {cat} tidak ditemukan: {filename} di {target_dir}")
            resp = Response(b"", status=404, mimetype="image/webp")
            resp.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
            resp.headers["X-Content-Type-Options"] = "nosniff"
            if is_sensitive:
                resp.headers["Cache-Control"] = "private, no-store"
            else:
                resp.headers["Cache-Control"] = "public, max-age=300"
            return resp

    if is_sensitive:
        resp = send_from_directory(target_dir, filename, max_age=0)
        resp.headers["Cache-Control"] = "private, no-store"
    else:
        resp = send_from_directory(target_dir, filename, max_age=86400 * 7)
    resp.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@bp.post("/upload")
@login_required
def upload_file():
    """Endpoint upload berkas mandiri untuk foto profil atau KTP."""
    claims = current_claims()
    category = request.form.get("category") or (request.json.get("category") if request.is_json else "profile")
    category = (category or "profile").lower().strip()

    role = claims.get("role")
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
            response_url = sign_media_url(url) if category in SENSITIVE_MEDIA_CATEGORIES else url
            logger.info(f"[UPLOAD-SUCCESS] Berkas Foto {category.upper()} berhasil disimpan via Form-Data: {url}")
            return jsonify(url=response_url, success=True)
        except Exception as e:
            logger.error(f"[UPLOAD-FAILED] Gagal memproses berkas Form-Data: {e}", exc_info=True)
            return jsonify(error=f"Gagal memproses gambar: {str(e)}"), 400

    # 2. Dari JSON body (data:image/...)
    data = request.get_json(silent=True) or {}
    image_data = data.get("image") or data.get("base64")
    if image_data:
        # Hanya pengelola teritorial yang boleh mengunggah atas nama NIK kader lain
        target_id = (data.get("nik") or identifier) if role in TERRITORIAL_MANAGER_ROLES else identifier
        try:
            url = optimize_and_save_image(
                image_data,
                category=category,
                identifier=target_id,
            )
            response_url = sign_media_url(url) if category in SENSITIVE_MEDIA_CATEGORIES else url
            logger.info(f"[UPLOAD-SUCCESS] Berkas Foto {category.upper()} berhasil disimpan via Base64/JSON: {url} (ID: {target_id})")
            return jsonify(url=response_url, success=True)
        except Exception as e:
            logger.error(f"[UPLOAD-FAILED] Gagal memproses gambar Base64/JSON: {e}", exc_info=True)
            return jsonify(error=f"Gagal memproses gambar: {str(e)}"), 400

    logger.warning(f"[UPLOAD-NO-ATTACHMENT] Request upload dari '{actor_name}' tidak membawa lampiran file maupun data gambar")
    return jsonify(error="Lampiran berkas atau data gambar tidak ditemukan"), 400
