-- ========================================================================
-- SKEMA POSTGRESQL MURNI (tanpa RLS / realtime Supabase) - Shadow DB DPW Sulut
-- Jalankan: psql "$DATABASE_URL" -f database/schema.sql
-- Skema ini identik dengan app/models.py (alternatif selain db.create_all()).
-- ========================================================================
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- pencarian nama cepat (ILIKE '%...%')

CREATE TABLE IF NOT EXISTS members (
    nik VARCHAR(50) PRIMARY KEY,
    nomor_ekta VARCHAR(50),
    nama_lengkap VARCHAR(255) NOT NULL,
    nomor_hp VARCHAR(50),
    email VARCHAR(150),
    sosmed VARCHAR(150),
    dpd VARCHAR(100) NOT NULL,
    dpc VARCHAR(100) NOT NULL,
    dprt VARCHAR(100) NOT NULL,
    is_pengurus BOOLEAN DEFAULT FALSE,
    tingkat_kepengurusan VARCHAR(30) DEFAULT 'Bukan Pengurus',
    wilayah_penugasan VARCHAR(100),
    nomor_sk VARCHAR(100),
    tanggal_sk DATE,
    kode_referral VARCHAR(50),
    jabatan VARCHAR(150) DEFAULT 'Anggota Biasa',
    status VARCHAR(50) DEFAULT 'Aktif',
    tanggal_bergabung DATE DEFAULT CURRENT_DATE,
    catatan_mutasi TEXT,
    terakhir_diubah TIMESTAMPTZ DEFAULT NOW(),
    diubah_oleh VARCHAR(100),
    foto_profil_url TEXT,
    foto_ktp_url TEXT,
    jenis_kelamin VARCHAR(20),
    tempat_lahir VARCHAR(100),
    tanggal_lahir DATE,
    alamat_lengkap TEXT,
    agama VARCHAR(50),
    pekerjaan VARCHAR(100),
    status_ktp_verified BOOLEAN DEFAULT TRUE,
    kader_perekrut_nama VARCHAR(255),
    kader_perekrut_nik VARCHAR(16),
    kader_perekrut_ekta VARCHAR(50),
    kader_perekrut_jabatan VARCHAR(150),
    urutan INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_members_urutan ON members (urutan);
CREATE INDEX IF NOT EXISTS idx_members_dpd ON members (dpd);
CREATE INDEX IF NOT EXISTS idx_members_dpc ON members (dpc);
CREATE INDEX IF NOT EXISTS idx_members_dprt ON members (dprt);
CREATE INDEX IF NOT EXISTS idx_members_status ON members (status);
CREATE INDEX IF NOT EXISTS idx_members_is_pengurus ON members (is_pengurus);
CREATE INDEX IF NOT EXISTS idx_members_nomor_ekta ON members (nomor_ekta);
CREATE INDEX IF NOT EXISTS idx_members_tgl_gabung ON members (tanggal_bergabung);
CREATE INDEX IF NOT EXISTS idx_members_perekrut ON members (kader_perekrut_nama);
CREATE INDEX IF NOT EXISTS idx_members_wilayah ON members (dpd, dpc, dprt);
CREATE INDEX IF NOT EXISTS idx_members_nama_trgm ON members USING gin (nama_lengkap gin_trgm_ops);

CREATE TABLE IF NOT EXISTS audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    member_nik VARCHAR(50) NOT NULL,
    member_name VARCHAR(255) NOT NULL,
    action VARCHAR(50) NOT NULL,
    previous_status VARCHAR(50),
    new_status VARCHAR(50),
    reason TEXT NOT NULL,
    actor VARCHAR(100) NOT NULL,
    dpd VARCHAR(100) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_logs_timestamp ON audit_logs (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_dpd ON audit_logs (dpd);
CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs (action);

CREATE TABLE IF NOT EXISTS deletion_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    member_nik VARCHAR(50) NOT NULL,
    member_name VARCHAR(255) NOT NULL,
    dpd VARCHAR(100) NOT NULL,
    dpc VARCHAR(100) NOT NULL,
    dprt VARCHAR(100) NOT NULL,
    requested_by VARCHAR(100) NOT NULL,
    requested_at TIMESTAMPTZ DEFAULT NOW(),
    reason TEXT NOT NULL,
    status VARCHAR(30) DEFAULT 'PENDING',
    reviewed_by VARCHAR(100),
    reviewed_at TIMESTAMPTZ,
    review_notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_deletion_status ON deletion_requests (status);

CREATE TABLE IF NOT EXISTS app_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    member_nik VARCHAR(50) REFERENCES members(nik) ON DELETE SET NULL,
    nomor_ekta VARCHAR(50),
    username VARCHAR(100) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,      -- hash (werkzeug), BUKAN plaintext
    full_name VARCHAR(255) NOT NULL,
    role VARCHAR(30) NOT NULL DEFAULT 'ADMIN_DPD',
    dpd_access VARCHAR(100) DEFAULT 'ALL',
    dpc_access VARCHAR(100) DEFAULT 'ALL',
    dprt_access VARCHAR(100) DEFAULT 'ALL',
    jabatan VARCHAR(150),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_app_users_member_nik ON app_users (member_nik);
CREATE INDEX IF NOT EXISTS idx_app_users_role ON app_users (role);
