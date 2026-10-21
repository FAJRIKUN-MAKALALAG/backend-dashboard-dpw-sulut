"""Layanan Logika Kode Referral dan Atribusi Hierarkis Ketua Langsung.
Sesuai Aturan Resmi:
1. Setiap pengurus memiliki kode referral unik.
2. Pengurus cukup menggunakan kode miliknya sendiri untuk merekrut anggota.
3. Sistem otomatis mengetahui ketua langsung pengurus tanpa input manual.
4. Setiap perekrutan mencatat dua pihak: Perekrut Langsung dan Ketua Langsung.
5. Rekapitulasi hierarkis berlaku di seluruh tingkat (DPRt, DPC, DPD, DPW).
"""
import random
import string
from sqlalchemy import func, or_
from ..extensions import db
from ..models import Member, AppUser


def is_ketua_position(jabatan: str) -> bool:
    """Mengecek apakah jabatan merupakan Ketua (bukan Wakil Ketua / Wk)."""
    if not jabatan:
        return False
    j = jabatan.strip().lower()
    if j.startswith("wakil") or j.startswith("wk.") or "wakil ketua" in j:
        return False
    return j.startswith("ketua")


def find_ketua_dprt(dpd: str, dpc: str, dprt: str):
    """Mencari Ketua DPRt di tingkat Desa/Kelurahan."""
    if not dprt or dprt in ("-", "Pusat/Umum"):
        return None
    return (
        Member.query.filter(
            Member.dpd == dpd,
            Member.dpc == dpc,
            Member.dprt == dprt,
            Member.tingkat_kepengurusan == "DPRt",
            Member.urutan == 1,
        ).first()
        or Member.query.filter(
            Member.dpd == dpd,
            Member.dpc == dpc,
            Member.dprt == dprt,
            Member.tingkat_kepengurusan == "DPRt",
            Member.jabatan.ilike("Ketua%"),
            ~Member.jabatan.ilike("Wakil%"),
        ).first()
    )


def find_ketua_dpc(dpd: str, dpc: str):
    """Mencari Ketua DPC di tingkat Kecamatan."""
    if not dpc or dpc in ("-", "Pusat/Umum"):
        return None
    return (
        Member.query.filter(
            Member.dpd == dpd,
            Member.dpc == dpc,
            Member.tingkat_kepengurusan == "DPC",
            Member.urutan == 1,
            Member.wilayah_penugasan.ilike("%PIMPINAN CABANG%"),
        ).first()
        or Member.query.filter(
            Member.dpd == dpd,
            Member.dpc == dpc,
            Member.tingkat_kepengurusan == "DPC",
            Member.urutan == 1,
        ).first()
        or Member.query.filter(
            Member.dpd == dpd,
            Member.dpc == dpc,
            Member.tingkat_kepengurusan == "DPC",
            Member.jabatan.ilike("Ketua%"),
            ~Member.jabatan.ilike("Wakil%"),
        ).first()
    )


def find_ketua_dpd(dpd: str):
    """Mencari Ketua DPD di tingkat Kabupaten/Kota."""
    if not dpd:
        return None
    return (
        Member.query.filter(
            Member.dpd == dpd,
            Member.tingkat_kepengurusan == "DPD",
            Member.urutan == 1,
            Member.wilayah_penugasan.ilike("%PIMPINAN DAERAH%"),
        ).first()
        or Member.query.filter(
            Member.dpd == dpd,
            Member.tingkat_kepengurusan == "DPD",
            Member.urutan == 1,
        ).first()
        or Member.query.filter(
            Member.dpd == dpd,
            Member.tingkat_kepengurusan == "DPD",
            Member.jabatan.ilike("Ketua%"),
            ~Member.jabatan.ilike("Wakil%"),
        ).first()
    )


def find_ketua_dpw():
    """Mencari Ketua DPW Partai NasDem Provinsi Sulawesi Utara."""
    return (
        Member.query.filter(
            Member.tingkat_kepengurusan == "DPW",
            Member.urutan == 1,
            Member.wilayah_penugasan.ilike("%PIMPINAN WILAYAH%"),
        ).first()
        or Member.query.filter(
            Member.tingkat_kepengurusan == "DPW",
            Member.jabatan.ilike("Ketua%"),
            ~Member.jabatan.ilike("Wakil%"),
        ).first()
        or Member.query.filter(
            Member.dpd.ilike("%Sulawesi Utara%"),
            Member.jabatan.ilike("Ketua%"),
            ~Member.jabatan.ilike("Wakil%"),
        ).first()
    )


def generate_unique_referral_code(member: Member) -> str:
    """
    Menghasilkan kode referral unik untuk pengurus.
    Format: NSD-[TINGKAT]-[4 DIGIT HEX/ALFNUM]
    Contoh: NSD-DPD-7K2X, NSD-DPC-9B41, NSD-KDR-8M1Q
    """
    if member.kode_referral and len(member.kode_referral.strip()) >= 5:
        return member.kode_referral.strip().upper()

    tingkat = (member.tingkat_kepengurusan or "KDR").upper().strip()
    prefix = f"NSD-{tingkat[:3]}"

    chars = string.ascii_uppercase + string.digits
    # Hapus karakter ambigu seperti O, 0, I, 1
    chars = chars.replace("O", "").replace("0", "").replace("I", "").replace("1", "")

    for _ in range(50):
        suffix = "".join(random.choices(chars, k=4))
        code = f"{prefix}-{suffix}"
        exists = Member.query.filter(func.upper(Member.kode_referral) == code).first()
        if not exists:
            member.kode_referral = code
            db.session.commit()
            return code

    # Fallback dengan timestamp jika tabrakan terus
    code = f"{prefix}-{random.randint(1000, 9999)}"
    member.kode_referral = code
    db.session.commit()
    return code


def find_direct_leader(member: Member) -> dict:
    """
    Menemukan Ketua Langsung secara hierarkis berdasarkan struktur organisasi:
    - DPRt Non-Ketua -> Ketua DPRt (Fallback: Ketua DPC)
    - DPRt Ketua -> Ketua DPC
    - DPC Non-Ketua -> Ketua DPC (Fallback: Ketua DPD)
    - DPC Ketua -> Ketua DPD
    - DPD Non-Ketua -> Ketua DPD (Fallback: Ketua DPW)
    - DPD Ketua -> Ketua DPW
    - DPW Non-Ketua -> Ketua DPW
    - DPW Ketua -> Ketua Umum DPP Partai NasDem
    """
    if not member:
        return {
            "nik": "-",
            "nama": "Ketua DPW Partai NasDem Sulawesi Utara",
            "jabatan": "Ketua DPW",
            "tingkat": "DPW",
            "kodeReferral": "NSD-DPW-SULUT",
        }

    tingkat = (member.tingkat_kepengurusan or "").upper().strip()
    is_ketua = is_ketua_position(member.jabatan or "")

    leader = None

    if tingkat == "DPRT":
        if is_ketua:
            leader = find_ketua_dpc(member.dpd, member.dpc) or find_ketua_dpd(member.dpd)
        else:
            leader = find_ketua_dprt(member.dpd, member.dpc, member.dprt) or find_ketua_dpc(member.dpd, member.dpc) or find_ketua_dpd(member.dpd)

    elif tingkat == "DPC":
        if is_ketua:
            leader = find_ketua_dpd(member.dpd) or find_ketua_dpw()
        else:
            leader = find_ketua_dpc(member.dpd, member.dpc) or find_ketua_dpd(member.dpd) or find_ketua_dpw()

    elif tingkat == "DPD":
        if is_ketua:
            leader = find_ketua_dpw()
        else:
            leader = find_ketua_dpd(member.dpd) or find_ketua_dpw()

    elif tingkat == "DPW":
        if is_ketua:
            return {
                "nik": "0000000000000000",
                "nama": "Ketua Umum DPP Partai NasDem",
                "jabatan": "Ketua Umum DPP",
                "tingkat": "DPP",
                "kodeReferral": "NSD-DPP-SURYA",
            }
        else:
            leader = find_ketua_dpw()

    else:
        # Anggota biasa / belum pengurus: cari ketua terdekat dari wilayah domisilinya
        leader = (
            find_ketua_dprt(member.dpd, member.dpc, member.dprt)
            or find_ketua_dpc(member.dpd, member.dpc)
            or find_ketua_dpd(member.dpd)
            or find_ketua_dpw()
        )

    if leader and leader.nik != member.nik:
        leader_code = leader.kode_referral
        if not leader_code or len(leader_code.strip()) < 4:
            leader_code = generate_unique_referral_code(leader)
        return {
            "nik": leader.nik,
            "nama": leader.nama_lengkap,
            "jabatan": leader.jabatan or f"Ketua {leader.tingkat_kepengurusan}",
            "tingkat": leader.tingkat_kepengurusan or "DPD",
            "kodeReferral": leader_code,
        }

    # Fallback standar jika ketua langsung belum terisi di database
    target_tingkat = "DPD" if tingkat in ("DPC", "DPRT") else "DPW"
    return {
        "nik": "-",
        "nama": f"Ketua {target_tingkat} NasDem {member.dpd if target_tingkat == 'DPD' else 'Sulawesi Utara'}",
        "jabatan": f"Ketua {target_tingkat}",
        "tingkat": target_tingkat,
        "kodeReferral": f"NSD-{target_tingkat[:3]}-KETUA",
    }
