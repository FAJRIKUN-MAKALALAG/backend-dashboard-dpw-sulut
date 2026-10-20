# Backend Dashboard DPW NasDem Sulut (Flask + PostgreSQL)

Pengganti akses langsung Supabase dari browser. Frontend: `../Dashboard-DPW-Sulut`.

## Struktur
```
config.py            # Konfigurasi aplikasi & database (dari .env)
.env.example         # Template environment
database/schema.sql  # Skema PostgreSQL murni (+ index trigram)
app/models.py        # Model SQLAlchemy (members, audit_logs, deletion_requests, app_users)
app/routes/          # auth, members, audit, deletions, users, stats
scripts/seed_admin.py
```

## Menjalankan
```powershell
python -m venv venv; .\venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env        # isi DATABASE_URL, secret, SEED_ADMIN_PASSWORD
psql "<DATABASE_URL>" -f database/schema.sql   # atau: python -m scripts.seed_admin (db.create_all)
python -m scripts.seed_admin
python wsgi.py                # dev, port 5000
# produksi: gunicorn -w 4 -b 0.0.0.0:5000 wsgi:app
```

## Endpoint (prefix `/api`, JWT `Authorization: Bearer <token>`)
| Method | Path | Keterangan |
|---|---|---|
| POST | /auth/login | `{username,password}` -> token + session |
| GET | /auth/me | sesi saat ini |
| GET | /members | `page, per_page(<=500), q, search_by, status, dpd, dpc, dprt, is_pengurus` |
| GET/PUT | /members/{nik} | detail (dengan foto) / ubah |
| POST | /members | tambah (validasi NIK 16 digit, scope wilayah) |
| POST | /members/bulk | `{members:[...]}` upsert chunked, maks `BULK_MAX_ROWS` |
| POST | /members/{nik}/status | mutasi status + audit |
| DELETE | /members/{nik} | hapus langsung (Super Admin) |
| GET | /members/check-nik/{nik} | cek duplikat |
| GET/POST | /deletion-requests | ajukan/lihat |
| POST | /deletion-requests/{id}/review | `{decision: APPROVED/REJECTED, notes}` (Super Admin) |
| GET | /audit-logs | paginasi |
| GET/POST/PUT/DELETE | /users | kelola akun (Super Admin) |
| GET | /stats/summary, /stats/recruitment, /stats/territory, /stats/reconciliation | agregasi di DB |

## Catatan data besar
- Pagination & filter di server; kolom foto (base64) tidak ikut dikirim di list.
- Agregasi dashboard memakai `GROUP BY` + index; tidak ada lagi tarik semua baris ke browser.
- Bulk import memakai `INSERT ... ON CONFLICT` per chunk.
- RBAC (DPW/DPD/DPC/DPRt) ditegakkan di server lewat klaim JWT, bukan hanya di UI.
- Password di-hash (werkzeug). Akun lama di Supabase (plaintext) harus di-reset ulang.
- Foto sebaiknya dipindah ke object storage (S3/MinIO) dan kolom hanya menyimpan URL.

## Migrasi data dari Supabase
`pg_dump --data-only -t members -t audit_logs -t deletion_requests <supabase_url> | psql <DATABASE_URL>`
(tabel `app_users` dibuat ulang lewat seed/endpoint /users).
