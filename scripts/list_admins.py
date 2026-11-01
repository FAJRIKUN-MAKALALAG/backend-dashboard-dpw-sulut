"""Script untuk menarik dan menampilkan daftar seluruh akun Admin dari database.

Menampilkan:
1. Akun Admin yang sudah terdaftar/aktif di tabel `app_users` (SUPER_ADMIN, ADMIN_DPW, ADMIN_DPD, ADMIN_DPC, ADMIN_DPRT).
2. Daftar Ketua (DPW, DPD, DPC, DPRt) di tabel `members` yang memiliki akses penuh Dashboard & Matriks Struktur.
3. Ringkasan seluruh Pengurus Struktural Ber-SK per Role.

Cara menjalankan:
  python -m scripts.list_admins
  atau dengan URL database / API VPS kustom:
  python -m scripts.list_admins --db-url "postgresql://postgres:PasswordSuperAman123@IP_VPS:5432/dpw_sulut"
  python -m scripts.list_admins --api-url "https://domain-api-vps-anda.com/api"
"""
import argparse
import os
import sys
import urllib.request
import json

# Pastikan root folder backend masuk ke sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def is_ketua_utama(jabatan: str) -> bool:
    if not jabatan:
        return False
    j = jabatan.strip().lower()
    return j.startswith("ketua") and "wakil" not in j


def fetch_via_api(api_url: str, username: str, password: str):
    api_url = api_url.rstrip("/")
    if not api_url.endswith("/api"):
        api_url = f"{api_url}/api"

    print(f"[*] Menghubungi API VPS: {api_url} ...")
    login_req = urllib.request.Request(
        f"{api_url}/auth/login",
        data=json.dumps({"username": username, "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "NasDem-Admin-Fetcher/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(login_req, timeout=15) as resp:
        login_data = json.loads(resp.read().decode("utf-8"))

    token = login_data.get("token")
    if not token:
        raise RuntimeError(f"Gagal mendapatkan token dari API: {login_data}")

    users_req = urllib.request.Request(
        f"{api_url}/users",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "NasDem-Admin-Fetcher/1.0"},
        method="GET",
    )
    with urllib.request.urlopen(users_req, timeout=15) as resp:
        users_data = json.loads(resp.read().decode("utf-8"))

    items = users_data.get("items", [])
    print_app_users_list(items, source=f"API VPS ({api_url})")


def print_app_users_list(users: list, source: str):
    print("\n" + "=" * 120)
    print(f" 1. DAFTAR AKUN LOGIN TERDAFTAR DI TABEL `app_users` - Sumber: {source}")
    print("=" * 120)
    if not users:
        print(" (Belum ada data di tabel app_users)")
        return

    print(f" Total Akun di app_users: {len(users)}\n")
    header = f"{'No':<3} | {'Role':<12} | {'Username / Login':<18} | {'NIK Kader':<18} | {'Nama Lengkap':<30} | {'Jabatan':<25} | {'Cakupan Wilayah'}"
    print(header)
    print("-" * 120)

    for idx, u in enumerate(users, 1):
        role = u.get("role") or "-"
        uname = u.get("username") or "-"
        nik = u.get("memberNik") or u.get("member_nik") or "-"
        fname = (u.get("fullName") or u.get("full_name") or "-")[:30]
        jabatan = (u.get("jabatan") or "-")[:25]
        dpd = u.get("dpdAccess") or u.get("dpd_access") or "ALL"
        dpc = u.get("dpcAccess") or u.get("dpc_access") or "ALL"
        dprt = u.get("dprtAccess") or u.get("dprt_access") or "ALL"
        active = "Aktif" if u.get("isActive", u.get("is_active", True)) else "Nonaktif"
        wilayah = f"DPD: {dpd} | DPC: {dpc} | DPRt: {dprt} [{active}]"
        print(f"{idx:<3} | {role:<12} | {uname:<18} | {nik:<18} | {fname:<30} | {jabatan:<25} | {wilayah}")
    print("=" * 120)


def fetch_via_db(db_url: str = None):
    if db_url:
        os.environ["DATABASE_URL"] = db_url

    from app import create_app
    from app.models import AppUser, Member
    from app.routes.auth_routes import determine_role

    app = create_app()
    with app.app_context():
        uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
        masked_uri = uri.split("@")[-1] if "@" in uri else uri
        print(f"[*] Menghubungkan ke Database PostgreSQL: {masked_uri} ...")

        app_users = AppUser.query.order_by(AppUser.role, AppUser.full_name).all()
        user_dicts = [
            {
                "role": u.role,
                "username": u.username,
                "member_nik": u.member_nik,
                "full_name": u.full_name,
                "jabatan": u.jabatan,
                "dpd_access": u.dpd_access,
                "dpc_access": u.dpc_access,
                "dprt_access": u.dprt_access,
                "is_active": u.is_active,
            }
            for u in app_users
        ]
        print_app_users_list(user_dicts, source=f"PostgreSQL ({masked_uri})")

        # Cek juga pengurus ber-SK di tabel members yang memiliki hak akses Admin
        pengurus_members = (
            Member.query.filter(Member.is_pengurus.is_(True))
            .order_by(Member.tingkat_kepengurusan, Member.dpd, Member.dpc, Member.dprt, Member.nama_lengkap)
            .all()
        )
        print("\n" + "=" * 120)
        print(f" 2. RINGKASAN PENGURUS BER-SK (AUTO-ADMIN SAAT LOGIN NIK) DI TABEL `members`: {len(pengurus_members)} Orang")
        print("=" * 120)

        by_role = {}
        ketua_list = []
        for m in pengurus_members:
            r = determine_role(m)
            by_role.setdefault(r, []).append(m)
            if is_ketua_utama(m.jabatan):
                ketua_list.append((r, m))

        for r_name in ("ADMIN_DPW", "ADMIN_DPD", "ADMIN_DPC", "ADMIN_DPRT", "ANGGOTA"):
            m_list = by_role.get(r_name, [])
            if m_list:
                print(f" - {r_name:<12}: {len(m_list)} orang")

        print("\n" + "=" * 120)
        print(f" 3. DAFTAR PARA KETUA (DPW / DPD / DPC / DPRt) PEMEGANG AKSES PENUH WILAYAH ({len(ketua_list)} Orang)")
        print("=" * 120)
        print(f"{'No':<4} | {'Role':<12} | {'NIK (Username)':<18} | {'Pass Default':<12} | {'Nama Lengkap':<30} | {'Jabatan':<18} | {'Wilayah'}")
        print("-" * 120)
        for idx, (r, m) in enumerate(ketua_list, 1):
            default_pass = m.nik[-6:] if m.nik and len(m.nik) >= 6 else "nasdem123"
            wil = f"DPD: {m.dpd} | DPC: {m.dpc} | DPRt: {m.dprt}"
            print(f"{idx:<4} | {r:<12} | {m.nik:<18} | {default_pass:<12} | {m.nama_lengkap[:30]:<30} | {(m.jabatan or '-')[:18]:<18} | {wil}")
        print("=" * 120)


def main():
    parser = argparse.ArgumentParser(description="Tarik daftar Admin dari Database / API VPS NasDem Sulut")
    parser.add_argument("--db-url", help="Override DATABASE_URL PostgreSQL")
    parser.add_argument("--api-url", help="Tarik via REST API URL (misal: https://api.domain.com/api)")
    parser.add_argument("--username", default=os.getenv("SEED_ADMIN_USERNAME", "SUPERADMINDPW"))
    parser.add_argument("--password", default=os.getenv("SEED_ADMIN_PASSWORD", "NasDem15"))
    args = parser.parse_args()

    if args.api_url:
        fetch_via_api(args.api_url, args.username, args.password)
    else:
        fetch_via_db(args.db_url)


if __name__ == "__main__":
    main()
