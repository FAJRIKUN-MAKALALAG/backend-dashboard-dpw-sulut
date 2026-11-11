"""Klien otomatisasi portal Digital NasDem DPP untuk penerbitan e-KTA resmi terpusat."""
import base64
import http.cookiejar
import io
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple
try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None
    ImageDraw = None

try:
    import requests
except ImportError:
    requests = None


class _StdlibResponse:
    def __init__(self, status_code: int, content: bytes, url: str):
        self.status_code = status_code
        self.content = content
        self.url = url
        self.text = content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)


class _StdlibSession:
    """Fallback HTTP session berbasis library bawaan Python (urllib + CookieJar) jika 'requests' belum terpasang di Docker VPS."""

    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.headers: Dict[str, str] = {}

    def get(self, url: str, timeout: int = 20) -> _StdlibResponse:
        req = urllib.request.Request(url, headers=dict(self.headers), method="GET")
        with self.opener.open(req, timeout=timeout) as resp:
            return _StdlibResponse(resp.getcode(), resp.read(), resp.geturl())

    def post(
        self,
        url: str,
        data: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Tuple[str, bytes, str]]] = None,
        timeout: int = 25,
        allow_redirects: bool = True,
        **kwargs,
    ) -> _StdlibResponse:
        if "json" in kwargs and json_body is None:
            json_body = kwargs["json"]
        hdrs = dict(self.headers)
        body_bytes = b""

        if files:
            boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
            hdrs["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            parts: List[bytes] = []
            for k, v in (data or {}).items():
                parts.append(
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8")
                )
            for field_name, (filename, file_bytes, mime_type) in files.items():
                header = (
                    f"--{boundary}\r\n"
                    f"Content-Disposition: form-data; name=\"{field_name}\"; filename=\"{filename}\"\r\n"
                    f"Content-Type: {mime_type}\r\n\r\n"
                ).encode("utf-8")
                parts.append(header + file_bytes + b"\r\n")
            parts.append(f"--{boundary}--\r\n".encode("utf-8"))
            body_bytes = b"".join(parts)
        elif json_body is not None:
            hdrs["Content-Type"] = "application/json"
            body_bytes = json.dumps(json_body).encode("utf-8")
        elif data is not None:
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
            body_bytes = urllib.parse.urlencode(data).encode("utf-8")

        req = urllib.request.Request(url, data=body_bytes, headers=hdrs, method="POST")
        with self.opener.open(req, timeout=timeout) as resp:
            return _StdlibResponse(resp.getcode(), resp.read(), resp.geturl())

from app.dpp_integration.mapper import (
    format_birth_date,
    generate_dpp_email,
    generate_dpp_password,
    map_agama,
    map_gender,
    map_job,
    map_pendidikan,
    map_status_kawin,
    normalize_wilayah_Token,
    parse_rt_rw,
)

logger = logging.getLogger("nasdem.dpp")

DEFAULT_BASE_URL = "https://digital.partainasdem.id/v1"

# Peta Kode Kabupaten BPS Sulawesi Utara (Provinsi 71) ke DPP id_wilayah
SULUT_KABUPATEN_MAP = {
    "7101": "BOLAANG MONGONDOW",
    "7102": "MINAHASA",
    "7103": "KEPULAUAN SANGIHE",
    "7104": "KEPULAUAN TALAUD",
    "7105": "MINAHASA SELATAN",
    "7106": "MINAHASA UTARA",
    "7107": "MINAHASA TENGGARA",
    "7108": "BOLAANG MONGONDOW UTARA",
    "7109": "KEP. SIAU TAGULANDANG BIARO",
    "7110": "BOLAANG MONGONDOW TIMUR",
    "7111": "BOLAANG MONGONDOW SELATAN",
    "7171": "KOTA MANADO",
    "7172": "KOTA BITUNG",
    "7173": "KOTA TOMOHON",
    "7174": "KOTA KOTAMOBAGU",
}


class DppClient:
    """Klien sesi terautentikasi ke portal DPP Partai NasDem."""

    def __init__(self):
        self.base_url = (os.getenv("DPP_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.email = os.getenv("DPP_ADMIN_EMAIL", "gabrielasenduk01@gmail.com")
        self.password = os.getenv("DPP_ADMIN_PASSWORD", "NasDem2029")
        if requests:
            self.session = requests.Session()
        else:
            self.session = _StdlibSession()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            }
        )
        self.last_login_at: float = 0
        self._wilayah_cache: Dict[str, Any] = {}

    def is_configured(self) -> bool:
        return bool(self.session and self.email and self.password)

    def login(self) -> bool:
        """Melakukan login ke portal DPP dan menyimpan session cookie."""
        if not self.is_configured():
            logger.warning("[DPP-LOGIN] Kredensial DPP_ADMIN_EMAIL / DPP_ADMIN_PASSWORD belum diatur.")
            return False

        try:
            login_url = f"{self.base_url}/login"
            r1 = self.session.get(login_url, timeout=15)
            csrf_match = re.search(r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']', r1.text)
            csrf_token = csrf_match.group(1) if csrf_match else ""

            payload = {
                "email": self.email,
                "password": self.password,
                "csrf_token": csrf_token,
            }
            r2 = self.session.post(login_url, data=payload, timeout=20, allow_redirects=True)

            if "overview" in r2.url or "createKTA" in r2.text or "Digital Siber" in r2.text:
                self.last_login_at = time.time()
                logger.info(f"[DPP-LOGIN-SUCCESS] Berhasil login sebagai Admin DPP: '{self.email}'")
                return True
            else:
                logger.error(f"[DPP-LOGIN-FAILED] Gagal login ke portal DPP. Status: {r2.status_code}, URL: {r2.url}")
                return False
        except Exception as exc:
            logger.error(f"[DPP-LOGIN-ERROR] Terjadi kendala koneksi ke server DPP: {exc}", exc_info=True)
            return False

    def ensure_authenticated(self) -> bool:
        """Memastikan sesi masih aktif; jika sesi kadaluarsa (> 40 menit), lakukan login ulang."""
        if time.time() - self.last_login_at < 2400 and self.last_login_at > 0:
            return True
        return self.login()

    def check_ktp_exists(self, nik: str) -> bool:
        """Mengecek apakah NIK sudah terdaftar di database e-KTA DPP."""
        if not self.ensure_authenticated():
            return False
        try:
            url = f"{self.base_url}/check_ktp.php"
            resp = self.session.post(url, json={"ktp": str(nik).strip()}, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return bool(data.get("exists"))
        except Exception as exc:
            logger.warning(f"[DPP-CHECK-KTP] Gagal mengecek NIK {nik} ke DPP: {exc}")
        return False

    def check_email_exists(self, email: str) -> bool:
        """Mengecek apakah Email sudah terdaftar di database e-KTA DPP."""
        if not self.ensure_authenticated():
            return False
        try:
            url = f"{self.base_url}/check_email.php"
            resp = self.session.post(url, json={"email": str(email).strip()}, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return bool(data.get("exists"))
        except Exception as exc:
            logger.warning(f"[DPP-CHECK-EMAIL] Gagal mengecek email {email} ke DPP: {exc}")
        return False

    def get_kabupaten_list(self, provinsi_id: str = "71") -> List[Dict[str, str]]:
        cache_key = f"kab_{provinsi_id}"
        if cache_key in self._wilayah_cache:
            return self._wilayah_cache[cache_key]
        if not self.ensure_authenticated():
            return [{"id_wilayah": k, "name": v} for k, v in SULUT_KABUPATEN_MAP.items()]
        try:
            url = f"{self.base_url}/get_data.php"
            resp = self.session.post(
                url,
                data={"provinsi_id": str(provinsi_id).strip(), "level": "kabupaten"},
                timeout=12,
            )
            if resp.status_code == 200:
                items = resp.json() or []
                if isinstance(items, list) and items:
                    self._wilayah_cache[cache_key] = items
                    return items
        except Exception as exc:
            logger.warning(f"[DPP-WILAYAH] Gagal mengambil daftar kabupaten: {exc}")
        return [{"id_wilayah": k, "name": v} for k, v in SULUT_KABUPATEN_MAP.items()]

    def get_kecamatan_list(self, kabupaten_id: str) -> List[Dict[str, str]]:
        cache_key = f"kec_{kabupaten_id}"
        if cache_key in self._wilayah_cache:
            return self._wilayah_cache[cache_key]
        if not self.ensure_authenticated():
            return []
        try:
            url = f"{self.base_url}/get_data.php"
            resp = self.session.post(
                url,
                data={"kabupaten_id": str(kabupaten_id).strip(), "level": "kecamatan"},
                timeout=12,
            )
            if resp.status_code == 200:
                items = resp.json() or []
                if isinstance(items, list):
                    self._wilayah_cache[cache_key] = items
                    return items
        except Exception as exc:
            logger.warning(f"[DPP-WILAYAH] Gagal mengambil daftar kecamatan untuk kab {kabupaten_id}: {exc}")
        return []

    def get_kelurahan_list(self, kecamatan_id: str) -> List[Dict[str, str]]:
        cache_key = f"kel_{kecamatan_id}"
        if cache_key in self._wilayah_cache:
            return self._wilayah_cache[cache_key]
        if not self.ensure_authenticated():
            return []
        try:
            url = f"{self.base_url}/get_data.php"
            resp = self.session.post(
                url,
                data={"kecamatan_id": str(kecamatan_id).strip(), "level": "kelurahan"},
                timeout=12,
            )
            if resp.status_code == 200:
                items = resp.json() or []
                if isinstance(items, list):
                    self._wilayah_cache[cache_key] = items
                    return items
        except Exception as exc:
            logger.warning(f"[DPP-WILAYAH] Gagal mengambil daftar kelurahan untuk kec {kecamatan_id}: {exc}")
        return []

    def resolve_wilayah_ids(
        self,
        kabupaten_name: str,
        kecamatan_name: str,
        kelurahan_name: str,
        nik: str = "",
        kab_id_override: str = None,
        kec_id_override: str = None,
        kel_id_override: str = None,
    ) -> Tuple[str, str, str]:
        """Mencocokkan nama teritorial DPW ke kode wilayah resmi yang dipakai dropdown form DPP."""
        if kab_id_override and kec_id_override and kel_id_override:
            return str(kab_id_override).strip(), str(kec_id_override).strip(), str(kel_id_override).strip()

        kab_id = str(kab_id_override or "").strip()

        # 1. Prioritaskan nama Kabupaten/Kota (DPD) yang dipilih pengguna
        if not kab_id and kabupaten_name:
            c_kab = normalize_wilayah_Token(kabupaten_name)
            # Cek exact match terlebih dahulu, lalu substring match terpanjang
            best_kid = ""
            best_len = 0
            for kid, kname in SULUT_KABUPATEN_MAP.items():
                norm_k = normalize_wilayah_Token(kname)
                if norm_k == c_kab:
                    best_kid = kid
                    break
                if (norm_k in c_kab or c_kab in norm_k) and len(norm_k) > best_len:
                    best_kid = kid
                    best_len = len(norm_k)
            if best_kid:
                kab_id = best_kid

        # 2. Fallback dari 4 digit awal NIK jika nama DPD kosong/tidak dikenali
        if not kab_id:
            clean_nik = re.sub(r"\D", "", nik or "")
            if clean_nik[:4] in SULUT_KABUPATEN_MAP:
                kab_id = clean_nik[:4]
            else:
                kab_id = "7171"  # Default Kota Manado

        kec_id = str(kec_id_override or "").strip()
        kel_id = str(kel_id_override or "").strip()

        # 3. Cari Kecamatan
        kec_list = self.get_kecamatan_list(kab_id)
        if not kec_id and kec_list and kecamatan_name:
            c_kec = normalize_wilayah_Token(kecamatan_name)
            for k in kec_list:
                item_name = normalize_wilayah_Token(k.get("name", ""))
                if item_name == c_kec:
                    kec_id = k.get("id_wilayah", "")
                    break
            if not kec_id:
                for k in kec_list:
                    item_name = normalize_wilayah_Token(k.get("name", ""))
                    if item_name and c_kec and (item_name in c_kec or c_kec in item_name):
                        kec_id = k.get("id_wilayah", "")
                        break

        if not kec_id and kec_list:
            kec_id = kec_list[0].get("id_wilayah", "")

        # 4. Cari Kelurahan (mendukung konversi angka Romawi I -> SATU, II -> DUA, dst.)
        if kec_id and not kel_id:
            kel_list = self.get_kelurahan_list(kec_id)
            if kel_list and kelurahan_name:
                c_kel = normalize_wilayah_Token(kelurahan_name)
                for v in kel_list:
                    item_name = normalize_wilayah_Token(v.get("name", ""))
                    if item_name == c_kel:
                        kel_id = v.get("id_wilayah", "")
                        break
                if not kel_id:
                    for v in kel_list:
                        item_name = normalize_wilayah_Token(v.get("name", ""))
                        if item_name and c_kel and (item_name in c_kel or c_kel in item_name):
                            kel_id = v.get("id_wilayah", "")
                            break
            if not kel_id and kel_list:
                kel_id = kel_list[0].get("id_wilayah", "")

        return kab_id, kec_id, kel_id

    def _prepare_jpeg_bytes(self, image_source, kind: str = "ktp") -> bytes:
        """Mengonversi sumber gambar apa pun (Base64, WebP, PNG, path lokal) ke JPEG RGB murni.
        Jika sumber gambar kosong, menghasilkan gambar placeholder JPEG agar syarat required DPP terpenuhi.
        """
        raw_bytes: Optional[bytes] = None
        if isinstance(image_source, (bytes, bytearray)):
            raw_bytes = bytes(image_source)
        elif isinstance(image_source, str) and image_source.strip():
            s = image_source.strip()
            if ";base64," in s:
                try:
                    raw_bytes = base64.b64decode(s.split(";base64,", 1)[1])
                except Exception:
                    pass
            elif s.startswith("/api/uploads/") or s.startswith("uploads/"):
                base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
                rel_path = s.replace("/api/", "").lstrip("/\\")
                full_path = os.path.join(base_dir, rel_path)
                if os.path.isfile(full_path):
                    with open(full_path, "rb") as f:
                        raw_bytes = f.read()
            elif len(s) > 300:
                try:
                    raw_bytes = base64.b64decode(s)
                except Exception:
                    pass

        if raw_bytes:
            try:
                img = Image.open(io.BytesIO(raw_bytes))
                if img.mode != "RGB":
                    img = img.convert("RGB")
                # Batasi dimensi maksimum 1400px seperti cropper di web DPP
                max_dim = 1400
                if max(img.width, img.height) > max_dim:
                    scale = max_dim / float(max(img.width, img.height))
                    img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
                out = io.BytesIO()
                img.save(out, format="JPEG", quality=88)
                return out.getvalue()
            except Exception as exc:
                logger.warning(f"[DPP-IMG-CONVERT] Gagal mengonversi citra {kind} ke JPEG, menggunakan fallback: {exc}")

        # Buat placeholder JPEG bersih jika foto belum diunggah
        if Image is not None and ImageDraw is not None:
            w, h = (800, 500) if kind == "ktp" else (400, 500)
            bg = (235, 242, 252) if kind == "ktp" else (26, 50, 130)
            img = Image.new("RGB", (w, h), bg)
            draw = ImageDraw.Draw(img)
            draw.rectangle([15, 15, w - 15, h - 15], outline=(180, 195, 220), width=3)
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=85)
            return out.getvalue()
        return raw_bytes or b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xd9"

    def fetch_kta_card_image(self, nokta: str) -> Optional[bytes]:
        """Mengambil citra PNG kartu fisik e-KTA langsung dari server DPP (`cekbikinkta.php`)."""
        clean_kta = re.sub(r"\D", "", str(nokta or ""))
        if not clean_kta or not self.ensure_authenticated():
            return None
        try:
            url = f"{self.base_url}/cekbikinkta.php?nokta={clean_kta}"
            resp = self.session.get(url, timeout=20)
            if resp.status_code == 200 and len(resp.content) > 1000:
                return resp.content
        except Exception as exc:
            logger.warning(f"[DPP-CARD-FETCH] Gagal mengunduh gambar kartu e-KTA {clean_kta}: {exc}")
        return None

    def create_kta_for_member(
        self,
        member_dict: Dict[str, Any],
        foto_ktp_source=None,
        foto_profil_source=None,
    ) -> Dict[str, Any]:
        """Mendaftarkan anggota baru ke portal DPP dan mengekstrak Nomor e-KTA resmi 16-digit."""
        if not self.ensure_authenticated():
            return {
                "success": False,
                "error": "Gagal terhubung atau login ke portal Digital NasDem DPP.",
            }

        nik = re.sub(r"\D", "", str(member_dict.get("nik") or "").strip())
        if len(nik) < 9 or len(nik) > 16:
            return {
                "success": False,
                "error": "NIK harus terdiri dari 9 hingga 16 digit angka yang valid.",
            }

        nama = str(member_dict.get("namaLengkap") or member_dict.get("nama_lengkap") or "").strip()
        if not nama:
            return {
                "success": False,
                "error": "Nama Lengkap wajib diisi untuk penerbitan e-KTA DPP.",
            }

        tempat_lahir = str(member_dict.get("tempatLahir") or member_dict.get("tempat_lahir") or "MANADO").strip() or "MANADO"
        raw_tgl = member_dict.get("tanggalLahir") or member_dict.get("tanggal_lahir")
        tgl_lahir = format_birth_date(raw_tgl, nik=nik)

        gender = map_gender(member_dict.get("jenisKelamin") or member_dict.get("jenis_kelamin"))
        agama = map_agama(member_dict.get("agama"))
        job = map_job(member_dict.get("pekerjaan") or member_dict.get("job"))
        kawin = map_status_kawin(member_dict.get("statusPerkawinan") or member_dict.get("status_perkawinan"))
        pendidikan = map_pendidikan(member_dict.get("pendidikan"))
        gol_darah = str(member_dict.get("golonganDarah") or member_dict.get("gol_darah") or "O").strip().upper()
        if gol_darah not in ("A", "B", "AB", "O"):
            gol_darah = "O"
        sayap = str(member_dict.get("sayap") or "").strip()

        dpd = member_dict.get("dpd") or ""
        dpc = member_dict.get("dpc") or ""
        dprt = member_dict.get("dprt") or ""
        kab_id, kec_id, kel_id = self.resolve_wilayah_ids(
            dpd,
            dpc,
            dprt,
            nik=nik,
            kab_id_override=member_dict.get("kabupatenId"),
            kec_id_override=member_dict.get("kecamatanId"),
            kel_id_override=member_dict.get("kelurahanId"),
        )

        rt_val, rw_val = parse_rt_rw(
            member_dict.get("rtRw") or member_dict.get("rt_rw"),
            rt_override=member_dict.get("rtNumber"),
            rw_override=member_dict.get("rwNumber"),
        )
        alamat = (
            member_dict.get("alamatLengkap")
            or member_dict.get("alamat_lengkap")
            or f"Kel. {dprt or 'Umum'}, Kec. {dpc or 'Umum'}, {dpd or 'Sulawesi Utara'}"
        ).strip()

        no_hp = re.sub(r"[^0-9+]", "", str(member_dict.get("nomorHP") or member_dict.get("nomor_hp") or ""))
        if not no_hp or len(no_hp) < 8:
            no_hp = "081234567890"

        custom_email = member_dict.get("email")
        dpp_email = generate_dpp_email(nik, custom_email)
        # Jika email sudah dipakai orang lain di DPP, gunakan email berbasis NIK+timestamp
        if self.check_email_exists(dpp_email):
            dpp_email = f"ektasulut{nik}{int(time.time()) % 1000}@gmail.com"

        dpp_password = generate_dpp_password(nik, member_dict.get("password"))

        form_fields = {
            "lokasinya": "Indonesia",
            "negara": "",
            "ktp": nik,
            "nama": nama,
            "tempat_lahir": tempat_lahir,
            "tgl_lahir": tgl_lahir,
            "jenis_kelamin": gender,
            "agama": agama,
            "job": job,
            "status_perkawinan": kawin,
            "pendidikan": pendidikan,
            "gol_darah": gol_darah,
            "provinsi": "71",
            "kabupaten": kab_id,
            "kecamatan": kec_id,
            "kelurahan": kel_id,
            "rtNumber": rt_val,
            "rwNumber": rw_val,
            "alamat_domisili": alamat,
            "no_hp": no_hp,
            "email": dpp_email,
            "password": dpp_password,
            "sayap": sayap,
        }

        ktp_bytes = self._prepare_jpeg_bytes(
            foto_ktp_source or member_dict.get("fotoKtpUrl") or member_dict.get("foto_ktp_url"),
            kind="ktp",
        )
        profil_bytes = self._prepare_jpeg_bytes(
            foto_profil_source or member_dict.get("fotoProfilUrl") or member_dict.get("foto_profil_url"),
            kind="profil",
        )

        files = {
            "foto_ktp": ("cropped_inputKtp.jpg", ktp_bytes, "image/jpeg"),
            "foto_profil": ("cropped_inputProfil.jpg", profil_bytes, "image/jpeg"),
        }

        try:
            submit_url = f"{self.base_url}/index?page=createKTA"
            logger.info(
                f"[DPP-SUBMIT] Mengirim form createKTA ke DPP untuk NIK {nik} ('{nama}'), "
                f"Wilayah={kab_id}/{kec_id}/{kel_id}, Email={dpp_email}"
            )
            resp = self.session.post(submit_url, data=form_fields, files=files, timeout=35, allow_redirects=True)
            resp_text = resp.text or ""
            resp_url = resp.url or ""

            # 1. Tangkap JavaScript redirect: window.location.replace("index?page=account\/overview&id=...")
            overview_id = None
            js_redir = re.search(
                r'window\.location\.replace\(["\']([^"\']*account\\?/overview[^"\']*)["\']\)',
                resp_text,
            )
            if js_redir:
                rel_overview = js_redir.group(1).replace("\\/", "/").lstrip("/")
                id_match = re.search(r"[?&]id=([^&#]+)", rel_overview)
                if id_match:
                    overview_id = id_match.group(1)
                overview_url = f"{self.base_url}/{rel_overview}"
                logger.info(f"[DPP-REDIRECT] Mengikuti halaman overview kader DPP: {overview_url}")
                r_ov = self.session.get(overview_url, timeout=25)
                if r_ov.status_code == 200 and r_ov.text:
                    resp_text = r_ov.text
                    resp_url = r_ov.url

            # 2. Ekstrak Nomor e-KTA resmi 16-digit dari halaman overview / response
            found_kta = None
            patterns = [
                r"cekbikinkta\.php\?nokta=([0-9]{12,20})",
                r"E-KTA:\s*<b>([0-9]{12,20})</b>",
                r"saved_ktas/([0-9]{12,20})\.(?:png|jpg)",
                r"ktapdf\?nokta=([0-9]{12,20})",
                r"nokta=([0-9]{12,20})",
            ]
            for pat in patterns:
                m_kta = re.search(pat, resp_text, re.IGNORECASE) or re.search(pat, resp_url, re.IGNORECASE)
                if m_kta:
                    found_kta = m_kta.group(1)
                    break

            if found_kta:
                logger.info(f"[DPP-SUCCESS] e-KTA RESMI BERHASIL DITERBITKAN DPP: {found_kta} untuk NIK {nik}")
                card_b64 = None
                card_bytes = self.fetch_kta_card_image(found_kta)
                if card_bytes:
                    card_b64 = "data:image/png;base64," + base64.b64encode(card_bytes).decode("ascii")

                return {
                    "success": True,
                    "nomor_ekta": found_kta,
                    "overview_id": overview_id,
                    "email": dpp_email,
                    "password": dpp_password,
                    "kabupaten_id": kab_id,
                    "kecamatan_id": kec_id,
                    "kelurahan_id": kel_id,
                    "kta_card_base64": card_b64,
                    "message": f"Berhasil menerbitkan e-KTA Resmi DPP Digital NasDem: {found_kta}",
                }

            # 3. Cek apakah NIK ternyata sudah terdaftar di DPP
            if self.check_ktp_exists(nik):
                logger.info(f"[DPP-VERIFIED-EXISTS] NIK {nik} terkonfirmasi sudah terdaftar di sistem DPP.")
                return {
                    "success": True,
                    "alreadyRegistered": True,
                    "email": dpp_email,
                    "message": f"NIK {nik} sudah terdaftar aktif di database Digital NasDem Pusat.",
                }

            err_match = re.search(r"Swal\.fire\([^)]*text:\s*['\"]([^'\"]+)['\"]", resp_text)
            err_msg = err_match.group(1) if err_match else "Server DPP menolak permohonan. Periksa kelengkapan NIK dan biodata."
            logger.warning(f"[DPP-WARNING] Gagal menerbitkan e-KTA untuk NIK {nik}: {err_msg}")
            return {
                "success": False,
                "error": err_msg,
            }

        except Exception as exc:
            logger.error(f"[DPP-SUBMIT-ERROR] Gagal mengirim data e-KTA ke DPP: {exc}", exc_info=True)
            return {
                "success": False,
                "error": f"Kendala komunikasi dengan server DPP: {str(exc)}",
            }


# Singleton instance
_dpp_client_instance: Optional[DppClient] = None


def get_dpp_client() -> DppClient:
    global _dpp_client_instance
    if _dpp_client_instance is None:
        _dpp_client_instance = DppClient()
    return _dpp_client_instance
