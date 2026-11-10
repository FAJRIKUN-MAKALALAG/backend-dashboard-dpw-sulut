"""Klien otomatisasi portal Digital NasDem DPP untuk penerbitan e-KTA resmi terpusat."""
import base64
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import requests

from .mapper import (
    format_birth_date,
    generate_dpp_email,
    generate_dpp_password,
    map_agama,
    map_gender,
    map_job,
    map_status_kawin,
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


def _clean_str(val: str) -> str:
    return re.sub(r"[^A-Z0-9\s]", "", (val or "").upper()).strip()


class DppClient:
    """Klien sesi terautentikasi ke portal DPP Partai NasDem."""

    def __init__(self):
        self.base_url = (os.getenv("DPP_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.email = os.getenv("DPP_ADMIN_EMAIL", "gabrielasenduk01@gmail.com")
        self.password = os.getenv("DPP_ADMIN_PASSWORD", "NasDem2029")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            }
        )
        self.last_login_at: float = 0
        self._wilayah_cache: Dict[str, Any] = {}

    def is_configured(self) -> bool:
        return bool(self.email and self.password)

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
        """Memastikan sesi masih aktif; jika sesi kadaluarsa (> 45 menit), lakukan login ulang."""
        if time.time() - self.last_login_at < 2700 and self.last_login_at > 0:
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
    ) -> Tuple[str, str, str]:
        """Mencocokkan nama teritorial DPW ke kode wilayah resmi yang dipakai dropdown form DPP."""
        clean_nik = re.sub(r"\D", "", nik or "")
        kab_id = clean_nik[:4] if clean_nik[:4] in SULUT_KABUPATEN_MAP else ""

        if not kab_id and kabupaten_name:
            c_kab = _clean_str(kabupaten_name)
            for kid, kname in SULUT_KABUPATEN_MAP.items():
                if kname in c_kab or c_kab in kname:
                    kab_id = kid
                    break

        if not kab_id:
            kab_id = "7171"  # Default Kota Manado jika tidak teridentifikasi

        kec_id = ""
        kel_id = ""

        # Cari Kecamatan
        kec_list = self.get_kecamatan_list(kab_id)
        if kec_list and kecamatan_name:
            c_kec = _clean_str(kecamatan_name)
            for k in kec_list:
                item_name = _clean_str(k.get("name", ""))
                if item_name == c_kec or item_name in c_kec or c_kec in item_name:
                    kec_id = k.get("id_wilayah", "")
                    break

        # Fallback kecamatan pertama jika nama tidak cocok persis
        if not kec_id and kec_list:
            kec_id = kec_list[0].get("id_wilayah", "")

        # Cari Kelurahan
        if kec_id:
            kel_list = self.get_kelurahan_list(kec_id)
            if kel_list and kelurahan_name:
                c_kel = _clean_str(kelurahan_name)
                for v in kel_list:
                    item_name = _clean_str(v.get("name", ""))
                    if item_name == c_kel or item_name in c_kel or c_kel in item_name:
                        kel_id = v.get("id_wilayah", "")
                        break
            if not kel_id and kel_list:
                kel_id = kel_list[0].get("id_wilayah", "")

        return kab_id, kec_id, kel_id

    def _prepare_image_bytes(self, image_source) -> Optional[bytes]:
        """Mendapatkan bytes citra dari string Base64 Data URL, path lokal, atau bytes mentah."""
        if not image_source:
            return None
        if isinstance(image_source, (bytes, bytearray)):
            return bytes(image_source)
        if isinstance(image_source, str):
            s = image_source.strip()
            if ";base64," in s:
                s = s.split(";base64,", 1)[1]
                return base64.b64decode(s)
            if s.startswith("/api/uploads/"):
                base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
                full_path = os.path.join(base_dir, s.replace("/api/", ""))
                if os.path.isfile(full_path):
                    with open(full_path, "rb") as f:
                        return f.read()
            if len(s) > 500:
                try:
                    return base64.b64decode(s)
                except Exception:
                    pass
        return None

    def create_kta_for_member(
        self,
        member_dict: Dict[str, Any],
        foto_ktp_source=None,
        foto_profil_source=None,
    ) -> Dict[str, Any]:
        """Mendaftarkan anggota baru ke portal DPP dan menghasilkan Nomor e-KTA resmi."""
        if not self.ensure_authenticated():
            return {
                "success": False,
                "error": "Gagal terhubung atau login ke portal Digital NasDem DPP.",
            }

        nik = str(member_dict.get("nik") or "").strip()
        nama = str(member_dict.get("namaLengkap") or member_dict.get("nama_lengkap") or "").strip()
        tempat_lahir = str(member_dict.get("tempatLahir") or member_dict.get("tempat_lahir") or "MANADO").strip()
        raw_tgl = member_dict.get("tanggalLahir") or member_dict.get("tanggal_lahir")
        tgl_lahir = format_birth_date(raw_tgl)

        gender = map_gender(member_dict.get("jenisKelamin") or member_dict.get("jenis_kelamin"))
        agama = map_agama(member_dict.get("agama"))
        job = map_job(member_dict.get("pekerjaan"))
        kawin = map_status_kawin(member_dict.get("statusPerkawinan") or member_dict.get("status_perkawinan"))
        gol_darah = member_dict.get("golonganDarah") or member_dict.get("gol_darah") or ""

        dpd = member_dict.get("dpd") or ""
        dpc = member_dict.get("dpc") or ""
        dprt = member_dict.get("dprt") or ""
        kab_id, kec_id, kel_id = self.resolve_wilayah_ids(dpd, dpc, dprt, nik)

        rt_val, rw_val = parse_rt_rw(member_dict.get("rtRw") or member_dict.get("rt_rw") or "000/000")
        alamat = (
            member_dict.get("alamatLengkap")
            or member_dict.get("alamat_lengkap")
            or f"Kel. {dprt}, Kec. {dpc}"
        ).strip()

        no_hp = str(member_dict.get("nomorHP") or member_dict.get("nomor_hp") or "08123456789").strip()
        if no_hp in ("-", "", "None"):
            no_hp = "08123456789"

        custom_email = member_dict.get("email")
        dpp_email = generate_dpp_email(nik, custom_email)
        dpp_password = generate_dpp_password(nik, tgl_lahir)

        form_fields = {
            "lokasinya": "Indonesia",
            "ktp": nik,
            "nama": nama,
            "tempat_lahir": tempat_lahir,
            "tgl_lahir": tgl_lahir,
            "jenis_kelamin": gender,
            "agama": agama,
            "job": job,
            "status_perkawinan": kawin,
            "pendidikan": "10",  # Default Sarjana / Umum
            "gol_darah": gol_darah,
            "provinsi": "71",  # SULAWESI UTARA
            "kabupaten": kab_id,
            "kecamatan": kec_id,
            "kelurahan": kel_id,
            "rtNumber": rt_val,
            "rwNumber": rw_val,
            "alamat_domisili": alamat,
            "no_hp": no_hp,
            "email": dpp_email,
            "password": dpp_password,
            "sayap": "",
        }

        # Dapatkan bytes foto KTP & foto profil
        ktp_bytes = self._prepare_image_bytes(
            foto_ktp_source or member_dict.get("fotoKtpUrl") or member_dict.get("foto_ktp_url")
        )
        profil_bytes = self._prepare_image_bytes(
            foto_profil_source or member_dict.get("fotoProfilUrl") or member_dict.get("foto_profil_url")
        )

        files = {}
        if ktp_bytes:
            files["foto_ktp"] = (f"{nik}_ktp.jpg", ktp_bytes, "image/jpeg")
        if profil_bytes:
            files["foto_profil"] = (f"{nik}_profil.jpg", profil_bytes, "image/jpeg")

        try:
            submit_url = f"{self.base_url}/index?page=createKTA"
            logger.info(f"[DPP-SUBMIT] Mengirim permohonan e-KTA untuk NIK {nik} ('{nama}') ke DPP Digital NasDem...")
            resp = self.session.post(submit_url, data=form_fields, files=files, timeout=30, allow_redirects=True)

            resp_text = resp.text or ""
            resp_url = resp.url or ""

            # 1. Cek apakah ada nomor KTA di URL atau response text
            # Format nomor e-KTA DPP adalah 16 digit angka (misal: 2574055961675481)
            found_kta = None

            m_nokta = re.search(r"nokta=([0-9]{14,18})", resp_url) or re.search(r"nokta=([0-9]{14,18})", resp_text)
            if m_nokta:
                found_kta = m_nokta.group(1)

            if not found_kta:
                m_saved = re.search(r"saved_ktas/([0-9]{14,18})\.(png|jpg)", resp_text)
                if m_saved:
                    found_kta = m_saved.group(1)

            if not found_kta:
                # Periksa apakah ada pesan sukses atau KTA PDF
                m_pdf = re.search(r"ktapdf\?nokta=([0-9]{14,18})", resp_text)
                if m_pdf:
                    found_kta = m_pdf.group(1)

            # Jika berhasil menemukan Nomor e-KTA resmi dari DPP
            if found_kta:
                logger.info(f"[DPP-SUCCESS] e-KTA RESMI BERHASIL DITERBITKAN DPP: {found_kta} untuk NIK {nik}")
                return {
                    "success": True,
                    "nomor_ekta": found_kta,
                    "email": dpp_email,
                    "message": f"e-KTA Resmi DPP berhasil diterbitkan: {found_kta}",
                }

            # Periksa jika NIK sudah pernah terdaftar di DPP sebelumnya
            if "sudah terdaftar" in resp_text.lower() or "nik sudah ada" in resp_text.lower():
                logger.info(f"[DPP-ALREADY-EXISTS] NIK {nik} sudah tercatat di sistem DPP.")
                return {
                    "success": True,
                    "alreadyRegistered": True,
                    "message": "NIK sudah tercatat dalam basis data DPP Pusat.",
                }

            # Periksa error lain
            err_match = re.search(r"Swal\.fire\([^)]*text:\s*['\"]([^'\"]+)['\"]", resp_text)
            err_msg = err_match.group(1) if err_match else "Gagal memproses permohonan e-KTA di portal DPP."
            logger.warning(f"[DPP-WARNING] Respons DPP untuk NIK {nik}: {err_msg}")
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
