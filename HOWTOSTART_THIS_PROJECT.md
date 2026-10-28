# PANDUAN CARA MENJALANKAN PROJECT (FRONTEND & BACKEND)
**Dashboard Shadow Database & Teritorial DPW Partai NasDem Sulawesi Utara**

Dokumen ini berisi panduan lengkap langkah demi langkah untuk menjalankan Backend dan Frontend di komputer lokal (Development) maupun persiapan di VPS (Production).

---

## DAFTAR ISI
1. [Prasyarat Sistem (Prerequisites)](#1-prasyarat-sistem-prerequisites)
2. [Langkah 1: Menjalankan Backend (Flask REST API)](#langkah-1-menjalankan-backend-flask-rest-api)
3. [Langkah 2: Menjalankan Frontend (React + Vite)](#langkah-2-menjalankan-frontend-react--vite)
4. [Akun Login Default (Super Admin)](#akun-login-default-super-admin)
5. [Daftar Endpoint Penting Backend](#daftar-endpoint-penting-backend)
6. [Solusi Masalah Umum (Troubleshooting)](#solusi-masalah-umum-troubleshooting)

---

## 1. Prasyarat Sistem (Prerequisites)

Pastikan perangkat Anda sudah terinstall:
* **Python 3.10+** (Rekomendasi: Python 3.12)
* **Node.js 18+** dan **npm**
* **Git**

---

## Langkah 1: Menjalankan Backend (Flask REST API)

Backend bertugas melayani seluruh request data ke database PostgreSQL, autentikasi JWT, audit log, dan normalisasi AI Gemini.

### 1.1 Buka Terminal 1 dan Masuk ke Folder Backend:
```powershell
cd "c:\FAJRIKUN\PROJECT KERJAAN\Nasdem\backend-dashboard-dpw"
```

### 1.2 Aktifkan Python Virtual Environment (venv):
* **Windows PowerShell**:
  ```powershell
  .\venv\Scripts\Activate.ps1
  ```
  *(Jika muncul error execution policy di PowerShell, jalankan: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` lalu ulangi).*
* **Windows CMD**:
  ```cmd
  venv\Scripts\activate.bat
  ```
* **Linux / Mac**:
  ```bash
  source venv/bin/activate
  ```

### 1.3 Pastikan Dependencies Terpasang:
*(Hanya perlu dijalankan jika baru pertama kali atau ada penambahan package)*
```bash
pip install -r requirements.txt
```

### 1.4 Periksa File Konfigurasi `.env`:
Pastikan file `.env` di dalam folder `backend-dashboard-dpw` sudah terisi:
```env
FLASK_ENV=development
SECRET_KEY=kunci-rahasia-aplikasi-anda
JWT_SECRET_KEY=kunci-jwt-rahasia-anda
JWT_ACCESS_HOURS=12
CORS_ORIGINS=http://localhost:3000,http://localhost:5173,http://127.0.0.1:3000,http://127.0.0.1:5173

# Koneksi Database PostgreSQL:
DATABASE_URL=postgresql://postgres.fansmsmlfzuvnktyjlef:DPWsulut115@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres

# Seed Super Admin
SEED_ADMIN_USERNAME=7106033003050001
SEED_ADMIN_PASSWORD=NasDem15

# Google Gemini Flash AI (Pembersihan Data)
GEMINI_API_KEY=AQ.Ab8RN6Iy6Ow-Unsnry7bEyvVPY380gGDEWFvpXkCpmPFzZDnSA
GEMINI_MODEL=gemini-flash-latest
```

### 1.5 Buat / Reset Akun Super Admin (Jika Diperlukan):
```bash
python -m scripts.seed_admin
```

### 1.6 Jalankan Server Backend:
```bash
python wsgi.py
```
> **Backend Aktif di:** `http://localhost:5000`  
> Cek status backend di browser: `http://localhost:5000/api/health` (harus menampilkan `status: ok, database: up`).

### 1.7 Memantau Log API & Debugging Error:
Setiap pemanggilan API otomatis tercatat secara mendalam dan terpusat:
* **Terminal Console:** Menampilkan log berwarna dengan status HTTP, endpoint URL, durasi eksekusi (ms), dan IP pengakses secara *realtime*.
* **Berkas `logs/api.log`:** Menyimpan seluruh riwayat pemanggilan API (INFO, WARNING, ERROR) dengan rotasi otomatis (maks 10MB x 5 file).
* **Berkas `logs/error.log`:** Khusus merekam kegagalan request (status 4xx & 5xx) beserta rincian:
  * Parameter URL & Body JSON request yang dikirimkan klien.
  * Pesan error yang dikembalikan server.
  * Full Python Stack Traceback jika terjadi unhandled error / bug.
* **Endpoint Diagnostik (Super Admin):**
  * `GET /api/logs/recent?type=error&limit=100` (melihat 100 baris error terakhir langsung via API / browser).

---

## Langkah 2: Menjalankan Frontend (React + Vite)

Frontend adalah antarmuka web modern untuk admin mengelola kader, struktur teritorial, cetak KTA, dan import matriks.

### 2.1 Buka Terminal 2 Baru dan Masuk ke Folder Frontend:
```powershell
cd "c:\FAJRIKUN\PROJECT KERJAAN\Nasdem\Dashboard-DPW-Sulut"
```

### 2.2 Pastikan Dependencies Frontend Terpasang:
*(Hanya perlu dijalankan pertama kali atau jika package diubah)*
```bash
npm install
```

### 2.3 Periksa File Konfigurasi `.env`:
Pastikan file `.env` di folder `Dashboard-DPW-Sulut` mengarah ke Backend API:
```env
VITE_API_URL=http://localhost:5000/api
```

### 2.4 Jalankan Server Development Frontend:
```bash
npm run dev
```
> **Frontend Aktif di:** `http://localhost:5173`  
> Buka browser dan buka alamat: `http://localhost:5173`

---

## Akun Login Default (Super Admin)

Gunakan akun berikut di halaman Login:
* **Username / NIK**: `7106033003050001`
* **Password**: `NasDem15`
* **Role**: `SUPER_ADMIN` (Akses penuh ke 15 Kabupaten/Kota se-Sulawesi Utara)

---

## Ringkasan Perintah Cepat (Quick Start)

| Komponen | Folder | Perintah Terminal | URL Lokal |
| :--- | :--- | :--- | :--- |
| **Backend API** | `backend-dashboard-dpw` | `.\venv\Scripts\Activate.ps1`<br>`python wsgi.py` | `http://localhost:5000/api` |
| **Frontend UI** | `Dashboard-DPW-Sulut` | `npm run dev` | `http://localhost:5173` |

---

## Daftar Endpoint Penting Backend

* `GET /api/health` - Cek kesehatan backend dan koneksi database.
* `POST /api/auth/login` - Login admin & pembuatan token JWT.
* `GET /api/members` - Ambil daftar anggota/kader (didukung pencarian & filter wilayah).
* `POST /api/members` - Tambah / simpan anggota.
* `POST /api/members/bulk` - Import massal pengurus dari Excel matriks.
* `POST /api/members/bulk-delete` - Reset formasi kepengurusan per wilayah.
* `POST /api/ai/clean` - AI Normalizer & Clustering (Google Gemini Flash).
* `GET /api/audit-logs` - Riwayat log aktivitas dan perubahan status anggota.
* `GET /api/deletion-requests` - Daftar pengajuan hapus bertingkat.
* `GET /api/users` - Manajemen akun pengguna (Khusus Super Admin).

---

## Solusi Masalah Umum (Troubleshooting)

1. **Error: `Script execution is disabled on this system` saat mengaktifkan venv:**
   Jalankan perintah ini di PowerShell:
   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   ```
2. **Error: Port 5000 sudah dipakai:**
   Ubah port di baris terakhir file `wsgi.py` atau matikan proses python lama di Task Manager.
3. **Frontend tidak bisa login (Network Error):**
   Pastikan Terminal 1 (Backend) sudah berjalan dan menampilkan pesan server aktif di `http://localhost:5000`.
4. **Pindah Database ke VPS:**
   Saat deploy ke VPS lokal, Anda cukup mengubah `DATABASE_URL` di file `.env` backend menjadi:
   `DATABASE_URL=postgresql+psycopg2://nama_user:password@localhost:5432/nama_database`
   dan import struktur tabel menggunakan file `database/schema.sql`.
