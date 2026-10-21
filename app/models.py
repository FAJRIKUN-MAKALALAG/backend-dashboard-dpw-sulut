import uuid

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import UUID

from .extensions import db


def _uuid():
    return uuid.uuid4()


class Member(db.Model):
    __tablename__ = "members"

    nik = db.Column(db.String(50), primary_key=True)
    nomor_ekta = db.Column(db.String(50), index=True)
    nama_lengkap = db.Column(db.String(255), nullable=False)
    nomor_hp = db.Column(db.String(50))
    email = db.Column(db.String(150))
    sosmed = db.Column(db.String(150))

    dpd = db.Column(db.String(100), nullable=False, index=True)
    dpc = db.Column(db.String(100), nullable=False, index=True)
    dprt = db.Column(db.String(100), nullable=False, index=True)

    is_pengurus = db.Column(db.Boolean, default=False, index=True)
    tingkat_kepengurusan = db.Column(db.String(30), default="Bukan Pengurus")
    wilayah_penugasan = db.Column(db.String(100))
    nomor_sk = db.Column(db.String(100))
    tanggal_sk = db.Column(db.Date)
    kode_referral = db.Column(db.String(50))

    jabatan = db.Column(db.String(150), default="Anggota Biasa")
    status = db.Column(db.String(50), default="Aktif", index=True)
    tanggal_bergabung = db.Column(db.Date, server_default=func.current_date(), index=True)
    catatan_mutasi = db.Column(db.Text)
    terakhir_diubah = db.Column(db.DateTime(timezone=True), server_default=func.now())
    diubah_oleh = db.Column(db.String(100))

    foto_profil_url = db.Column(db.Text)
    foto_ktp_url = db.Column(db.Text)
    jenis_kelamin = db.Column(db.String(20))
    tempat_lahir = db.Column(db.String(100))
    tanggal_lahir = db.Column(db.Date)
    alamat_lengkap = db.Column(db.Text)
    agama = db.Column(db.String(50))
    pekerjaan = db.Column(db.String(100))
    status_ktp_verified = db.Column(db.Boolean, default=True)

    kader_perekrut_kode_referral = db.Column(db.String(50), index=True)
    kader_perekrut_nama = db.Column(db.String(255), index=True)
    kader_perekrut_nik = db.Column(db.String(50), index=True)
    kader_perekrut_ekta = db.Column(db.String(50))
    kader_perekrut_jabatan = db.Column(db.String(150))

    # Atribusi Ketua Langsung Otomatis (Hierarkis berdasarkan struktur organisasi)
    ketua_langsung_nik = db.Column(db.String(50), index=True)
    ketua_langsung_nama = db.Column(db.String(255))
    ketua_langsung_jabatan = db.Column(db.String(150))
    ketua_langsung_tingkat = db.Column(db.String(50))

    # Urutan tampil di matriks struktur (mengikuti urutan baris Excel saat import)
    urutan = db.Column(db.Integer, index=True)

    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (db.Index("idx_members_wilayah", "dpd", "dpc", "dprt"),)


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    timestamp = db.Column(db.DateTime(timezone=True), server_default=func.now(), index=True)
    member_nik = db.Column(db.String(50), nullable=False)
    member_name = db.Column(db.String(255), nullable=False)
    action = db.Column(db.String(50), nullable=False, index=True)
    previous_status = db.Column(db.String(50))
    new_status = db.Column(db.String(50))
    reason = db.Column(db.Text, nullable=False)
    actor = db.Column(db.String(100), nullable=False)
    dpd = db.Column(db.String(100), nullable=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())


class DeletionRequest(db.Model):
    __tablename__ = "deletion_requests"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    member_nik = db.Column(db.String(50), nullable=False)
    member_name = db.Column(db.String(255), nullable=False)
    dpd = db.Column(db.String(100), nullable=False)
    dpc = db.Column(db.String(100), nullable=False)
    dprt = db.Column(db.String(100), nullable=False)
    requested_by = db.Column(db.String(100), nullable=False)
    requested_at = db.Column(db.DateTime(timezone=True), server_default=func.now())
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), default="PENDING", index=True)
    reviewed_by = db.Column(db.String(100))
    reviewed_at = db.Column(db.DateTime(timezone=True))
    review_notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())


class AppUser(db.Model):
    __tablename__ = "app_users"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    member_nik = db.Column(db.String(50), db.ForeignKey("members.nik", ondelete="SET NULL"), index=True)
    nomor_ekta = db.Column(db.String(50))
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(30), nullable=False, default="ADMIN_DPD", index=True)
    dpd_access = db.Column(db.String(100), default="ALL")
    dpc_access = db.Column(db.String(100), default="ALL")
    dprt_access = db.Column(db.String(100), default="ALL")
    jabatan = db.Column(db.String(150))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
