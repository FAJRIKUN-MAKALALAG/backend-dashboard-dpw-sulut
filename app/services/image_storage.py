"""Layanan penyimpanan & kompresi berkas gambar fisik (Foto KTP & Profil).

Menghindarkan database PostgreSQL dari beban data Base64 raksasa (bloat)
dengan menyimpan berkas fisik terkompresi WebP di disk server, dan hanya
menyimpan URL link teks pendek di kolom database.
"""
import base64
import hashlib
import io
import logging
import os
import re
import time
from typing import Optional, Tuple
from PIL import Image

logger = logging.getLogger("nasdem.storage")

# Direktori penyimpanan lokal di server
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
PROFILE_DIR = os.path.join(UPLOADS_DIR, "profile")
KTP_DIR = os.path.join(UPLOADS_DIR, "ktp")

os.makedirs(PROFILE_DIR, exist_ok=True)
os.makedirs(KTP_DIR, exist_ok=True)


def get_category_dir(category: str) -> str:
    cat = (category or "").lower().strip()
    if cat in ("ktp", "idcard", "foto_ktp"):
        return KTP_DIR, "ktp"
    return PROFILE_DIR, "profile"


def _clean_identifier(identifier: Optional[str]) -> str:
    if not identifier:
        return f"kader_{int(time.time())}"
    return re.sub(r"[^a-zA-Z0-9_-]", "", str(identifier).strip()) or "kader"


def is_base64_data_uri(val: str) -> bool:
    if not isinstance(val, str):
        return False
    val = val.strip()
    return val.startswith("data:image/") and ";base64," in val


def decode_base64_image(base64_str: str) -> Tuple[bytes, str]:
    """Mengekstrak binary bytes dan ekstensi asli dari string Data URI base64."""
    if ";base64," in base64_str:
        header, encoded = base64_str.split(";base64,", 1)
        mime = header.split("data:image/")[-1].lower()
    else:
        mime = "jpeg"
        encoded = base64_str

    img_bytes = base64.b64decode(encoded)
    return img_bytes, mime


def optimize_and_save_image(
    image_source,
    category: str = "profile",
    identifier: Optional[str] = None,
    max_dimension: Optional[int] = None,
    quality: int = 82,
) -> str:
    """Mengompresi dan menyimpan gambar ke disk server dalam format WebP modern.

    :param image_source: Binary bytes, FileStorage objek Flask, atau string Base64
    :param category: 'profile' atau 'ktp'
    :param identifier: NIK kader atau ID unik
    :return: URL relatif yang disimpan di database, contoh: '/api/uploads/profile/7105_1729000.webp'
    """
    target_dir, cat_name = get_category_dir(category)
    clean_id = _clean_identifier(identifier)

    # 1. Dapatkan bytes gambar
    if isinstance(image_source, str):
        if is_base64_data_uri(image_source) or len(image_source) > 500:
            img_bytes, _ = decode_base64_image(image_source)
        else:
            # Jika sudah berupa URL, kembalikan apa adanya
            return image_source
    elif hasattr(image_source, "read"):
        img_bytes = image_source.read()
    elif isinstance(image_source, bytes):
        img_bytes = image_source
    else:
        raise ValueError("Format data gambar tidak didukung")

    if not img_bytes:
        raise ValueError("Data gambar kosong")

    # 2. Buka gambar dengan PIL
    img = Image.open(io.BytesIO(img_bytes))

    # Konversi color mode jika diperlukan
    if img.mode in ("RGBA", "LA") and cat_name == "ktp":
        # KTP lebih baik background putih jika ada transparansi
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        img = background
    elif img.mode != "RGB" and img.mode != "RGBA":
        img = img.convert("RGB")

    # 3. Resize cerdas proporsional
    default_max = 900 if cat_name == "profile" else 1400
    limit = max_dimension or default_max

    w, h = img.size
    if max(w, h) > limit:
        if w > h:
            new_w = limit
            new_h = int(h * (limit / w))
        else:
            new_h = limit
            new_w = int(w * (limit / h))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    # 4. Buat nama berkas unik
    content_hash = hashlib.md5(img_bytes[:2048]).hexdigest()[:8]
    filename = f"{clean_id}_{cat_name}_{content_hash}.webp"
    file_path = os.path.join(target_dir, filename)

    # 5. Simpan sebagai WebP terkompresi
    img.save(file_path, "WEBP", quality=quality, method=6)

    # 6. Hitung ukuran dan catat log sukses
    file_size_kb = os.path.getsize(file_path) / 1024
    url = f"/api/uploads/{cat_name}/{filename}"
    logger.info(
        f"[STORAGE-SAVED] Berhasil menyimpan berkas fisik Foto {cat_name.upper()} di disk server: "
        f"{filename} ({w}x{h} px, {file_size_kb:.1f} KB) | Path: {file_path} -> URL: {url} | ID: {clean_id}"
    )

    # 7. Kembalikan URL publik API
    return url


def intercept_and_store_member_photos(values: dict, default_nik: Optional[str] = None) -> dict:
    """Memeriksa apakah ada fotoProfilUrl atau fotoKtpUrl berupa Base64 di payload.

    Jika ada, otomatis simpan ke disk server sebagai file fisik WebP,
    lalu gantikan nilainya dengan URL singkat sebelum disimpan ke database.
    """
    nik = values.get("nik") or default_nik or "kader"

    # Proses foto profil
    foto_profil = values.get("foto_profil_url")
    if foto_profil and isinstance(foto_profil, str) and (is_base64_data_uri(foto_profil) or len(foto_profil) > 600):
        try:
            logger.info(f"[IMAGE-PROCESS] Mendeteksi data Foto Profil untuk NIK {nik}, memulai kompresi & penyimpanan fisik...")
            url = optimize_and_save_image(foto_profil, category="profile", identifier=nik)
            values["foto_profil_url"] = url
            logger.info(f"[IMAGE-SUCCESS] Foto Profil untuk NIK {nik} berhasil disimpan di server dan URL siap dicatat di DB: {url}")
        except Exception as e:
            logger.error(f"[IMAGE-ERROR] Gagal memproses berkas Foto Profil untuk NIK {nik}: {e}", exc_info=True)

    # Proses foto KTP
    foto_ktp = values.get("foto_ktp_url")
    if foto_ktp and isinstance(foto_ktp, str) and (is_base64_data_uri(foto_ktp) or len(foto_ktp) > 600):
        try:
            logger.info(f"[IMAGE-PROCESS] Mendeteksi data Foto KTP untuk NIK {nik}, memulai kompresi & penyimpanan fisik...")
            url = optimize_and_save_image(foto_ktp, category="ktp", identifier=nik)
            values["foto_ktp_url"] = url
            logger.info(f"[IMAGE-SUCCESS] Foto KTP untuk NIK {nik} berhasil disimpan di server dan URL siap dicatat di DB: {url}")
        except Exception as e:
            logger.error(f"[IMAGE-ERROR] Gagal memproses berkas Foto KTP untuk NIK {nik}: {e}", exc_info=True)

    return values
