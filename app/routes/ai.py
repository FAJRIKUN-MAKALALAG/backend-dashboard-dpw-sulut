"""Pembersihan & clustering data pengurus memakai Google Gemini (dipanggil dari server).

API key Gemini disimpan di .env backend (GEMINI_API_KEY) dan tidak pernah dikirim ke browser.
Jika key belum diisi / Gemini gagal, endpoint mengembalikan success=false sehingga
frontend memakai normalizer lokal sebagai cadangan.
"""
import json
import os
import urllib.error
import urllib.request

from flask import Blueprint, jsonify, request

from ..auth import login_required

bp = Blueprint("ai", __name__)

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_ROWS_PER_REQUEST = 100

TAXONOMY = """
PANDUAN TAKSONOMI RESMI PARTAI NASDEM SULUT:
1. 15 DPD KABUPATEN / KOTA BAKU:
   - Manado, Minahasa, Minahasa Selatan, Minahasa Utara, Minahasa Tenggara,
   - Tomohon, Bitung, Kotamobagu, Bolaang Mongondow, Bolaang Mongondow Utara,
   - Bolaang Mongondow Selatan, Bolaang Mongondow Timur,
   - Kepulauan Sangihe, Kepulauan Talaud, Kepulauan Siau Tagulandang Biaro (Sitaro).

2. SUB-ORGANISASI RESMI:
   - "I. DEWAN PERTIMBANGAN" (atau "DEWAN PERTIMBANGAN WILAYAH" untuk DPW)
   - "II. DEWAN PAKAR" (atau "DEWAN PAKAR WILAYAH" untuk DPW)
   - "III. DEWAN PIMPINAN DAERAH" (atau "DEWAN PIMPINAN WILAYAH" untuk DPW, "DEWAN PIMPINAN CABANG" untuk DPC)

3. 36 JABATAN BAKU STRUKTURAL DEWAN PIMPINAN:
   - Ketua, Sekretaris, Bendahara.
   - Para Wakil Ketua Bidang: Bappilu, OKK, Kaderisasi & Pendidikan Politik, Hubungan Legislatif, Hubungan Eksekutif, Hubungan Sayap & Badan, Penggalangan Komunitas, Pemilih Pemula/Milenial, Digital & Siber, Media & Komunikasi Publik, UMKM, Ekonomi, Agama & Masyarakat Adat, Tenaga Kerja, Kesehatan, Perempuan & Anak, Pendidikan & Kebudayaan, Hukum & HAM, Pariwisata & Industri Kreatif, Pertanian/Peternakan/Desa, Maritim, Pemuda & Olahraga, Lingkungan Hidup, dll.
"""

SYSTEM_PROMPT = f"""
Anda adalah AI Spesialis Normalisasi & Clustering Data Kepengurusan Partai NasDem Sulawesi Utara.
Tugas Anda adalah membaca daftar pengurus dan merapikannya sesuai taksonomi resmi:

{TAXONOMY}

ATURAN PENTING:
1. PISAHKAN DEWAN:
   - Kata kunci "Pertimbangan" / "Wankar" -> subOrg = "I. DEWAN PERTIMBANGAN", clusterGroup = "WANKAR".
   - Kata kunci "Pakar" / "Wangan" -> subOrg = "II. DEWAN PAKAR", clusterGroup = "WANGAN".
   - Sisanya -> subOrg = "III. DEWAN PIMPINAN DAERAH" (atau tingkat wilayah yang sesuai), clusterGroup = "PIMPINAN".
2. NORMALISASI JABATAN:
   - Petakan singkatan ke nama jabatan resmi baku (misal "Wkl Ketua Bid OKK" -> "Wakil Ketua Bidang Organisasi, Keanggotaan, dan Kaderisasi (OKK)").
   - Cocokkan dengan 36 slot baku, set "isMatched": true jika cocok, false jika jabatan kustom.
3. RAPIKAN NAMA:
   - Format nama pengurus rapi (huruf besar atau title case profesional), pisahkan gelar jika ada.
4. KEMBALIKAN HANYA JSON DENGAN FORMAT:
   {{
     "items": [
       {{
         "no": 1,
         "subOrg": "I. DEWAN PERTIMBANGAN",
         "jabatan": "Ketua",
         "canonicalJabatan": "Ketua",
         "isMatched": true,
         "namaLengkap": "NAMA LENGKAP",
         "nik": "7105...",
         "clusterGroup": "WANKAR",
         "aiCorrection": "Dipisahkan ke Dewan Pertimbangan"
       }}
     ]
   }}
"""


def _stats(items):
    def has(item, word, group):
        return word in (item.get("subOrg") or "").lower() or item.get("clusterGroup") == group

    return {
        "total": len(items),
        "pertimbanganCount": sum(1 for i in items if has(i, "pertimbangan", "WANKAR")),
        "pakarCount": sum(1 for i in items if has(i, "pakar", "WANGAN")),
        "pimpinanCount": sum(1 for i in items if has(i, "pimpinan", "PIMPINAN")),
        "matchedCount": sum(1 for i in items if i.get("isMatched")),
        "correctedCount": sum(1 for i in items if i.get("aiCorrection")),
    }


@bp.post("/ai/clean")
@login_required
def ai_clean():
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return jsonify(success=False, message="GEMINI_API_KEY belum diatur di .env backend"), 503

    data = request.get_json(silent=True) or {}
    rows = data.get("rawRows")
    ctx = data.get("territoryContext") or {}
    if not isinstance(rows, list) or not rows:
        return jsonify(success=False, message="rawRows wajib berisi daftar pengurus"), 400
    if len(rows) > MAX_ROWS_PER_REQUEST:
        return jsonify(success=False, message=f"Maksimal {MAX_ROWS_PER_REQUEST} baris per request"), 413

    user_prompt = (
        f"Wilayah: Tingkat {ctx.get('level', '-')} - {ctx.get('dpd', '-')}\n"
        f"Data Pengurus:\n{json.dumps(rows, ensure_ascii=False, indent=2)}\n"
    )
    model = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
    body = json.dumps(
        {
            "contents": [{"role": "user", "parts": [{"text": SYSTEM_PROMPT + "\n\n" + user_prompt}]}],
            "generationConfig": {"response_mime_type": "application/json", "temperature": 0.1},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        GEMINI_ENDPOINT.format(model=model),
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
    except urllib.error.HTTPError as exc:
        return jsonify(success=False, isFallback=True, message=f"Gemini API error {exc.code}, menggunakan parser lokal"), 200
    except (urllib.error.URLError, TimeoutError):
        return jsonify(success=False, isFallback=True, message="Koneksi Gemini timeout/offline, menggunakan parser cerdas lokal"), 200
    except (KeyError, IndexError, ValueError):
        return jsonify(success=False, isFallback=True, message="Format respons Gemini tidak standar, menggunakan parser lokal"), 200

    items = parsed.get("items") or parsed.get("pengurus") or (parsed if isinstance(parsed, list) else [])
    return jsonify(
        success=True,
        items=items,
        stats=_stats(items),
        message=f"Gemini AI berhasil membersihkan dan mengelompokkan {len(items)} kader pengurus!",
    )
