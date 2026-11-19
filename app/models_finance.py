"""Model database modul Keuangan DPW NasDem Sulawesi Utara (Iuran Fraksi, DPP, Kas Bank & Operasional)."""

from sqlalchemy import func

from app.extensions import db


class FinanceLegislator(db.Model):
    """Master anggota dewan / fraksi (Provinsi & Kabupaten/Kota) wajib iuran partai."""

    __tablename__ = "finance_legislators"

    id = db.Column(db.String(50), primary_key=True)
    nama_lengkap = db.Column(db.String(255), nullable=False)
    tingkat = db.Column(db.String(20), nullable=False, default="KAB_KOTA", index=True)  # PROVINSI | KAB_KOTA
    daerah = db.Column(db.String(120), nullable=False, index=True)
    urutan = db.Column(db.Integer, default=1, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    is_autodebet_active = db.Column(db.Boolean, nullable=False, default=False, index=True)
    autodebet_start_month = db.Column(db.String(7), nullable=True)  # YYYY-MM
    autodebet_end_month = db.Column(db.String(7), nullable=True)  # YYYY-MM
    catatan = db.Column(db.Text)
    member_nik = db.Column(db.String(50), nullable=True, index=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    autodebet_histories = db.relationship(
        "FinanceAutodebetHistory",
        back_populates="legislator",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="FinanceAutodebetHistory.start_month.desc()",
        lazy="select",
    )
    dues_payments = db.relationship(
        "FinanceDuesPayment",
        back_populates="legislator",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="select",
    )


class FinanceAutodebetHistory(db.Model):
    """Catatan historis periode mulai & berakhir autodebet setiap anggota fraksi."""

    __tablename__ = "finance_autodebet_histories"

    id = db.Column(db.String(50), primary_key=True)
    legislator_id = db.Column(
        db.String(50),
        db.ForeignKey("finance_legislators.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    start_month = db.Column(db.String(7), nullable=False)  # YYYY-MM
    end_month = db.Column(db.String(7), nullable=True)  # YYYY-MM atau NULL jika masih aktif
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    notes = db.Column(db.Text)
    updated_by = db.Column(db.String(150))
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())

    legislator = db.relationship("FinanceLegislator", back_populates="autodebet_histories")


class FinanceTariffRule(db.Model):
    """Master tarif iuran DPW & DPP per daerah beserta rentang bulan berlaku."""

    __tablename__ = "finance_tariff_rules"

    id = db.Column(db.String(80), primary_key=True)
    tingkat = db.Column(db.String(20), nullable=False, default="KAB_KOTA", index=True)
    daerah = db.Column(db.String(120), nullable=False, index=True)
    effective_from = db.Column(db.String(7), nullable=False, index=True)  # YYYY-MM
    effective_to = db.Column(db.String(7), nullable=True, index=True)  # YYYY-MM atau NULL
    amount_dpw = db.Column(db.BigInteger, nullable=False, default=0)
    amount_dpp = db.Column(db.BigInteger, nullable=False, default=0)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class FinanceDppRemittance(db.Model):
    """Batch pengiriman dana kewajiban dari Kas Bank DPW ke DPP NasDem Pusat."""

    __tablename__ = "finance_dpp_remittances"

    id = db.Column(db.String(60), primary_key=True)
    remittance_date = db.Column(db.Date, nullable=False, index=True)
    period_label = db.Column(db.String(100))
    total_dpp_amount = db.Column(db.BigInteger, nullable=False, default=0)
    bank_fee = db.Column(db.BigInteger, nullable=False, default=0)
    total_transferred = db.Column(db.BigInteger, nullable=False, default=0)
    recipient_account = db.Column(db.String(255))
    reference_no = db.Column(db.String(120))
    proof_url = db.Column(db.Text)
    notes = db.Column(db.Text)
    is_historical = db.Column(db.Boolean, nullable=False, default=False)
    created_by = db.Column(db.String(150))
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())

    payments = db.relationship(
        "FinanceDuesPayment",
        back_populates="dpp_remittance",
        lazy="select",
    )


class FinanceTransaction(db.Model):
    """Buku kas arus kas Rekening Bank DPW (BANK_DPW) dan Kas Operasional Kantor (PETTY_CASH)."""

    __tablename__ = "finance_transactions"

    id = db.Column(db.String(60), primary_key=True)
    tx_date = db.Column(db.Date, nullable=False, index=True)
    account_type = db.Column(db.String(20), nullable=False, default="BANK_DPW", index=True)  # BANK_DPW | PETTY_CASH
    direction = db.Column(db.String(10), nullable=False, default="IN", index=True)  # IN | OUT
    category = db.Column(db.String(100), nullable=False, default="Lainnya", index=True)
    description = db.Column(db.Text, nullable=False)
    amount = db.Column(db.BigInteger, nullable=False, default=0)
    proof_url = db.Column(db.Text)
    related_remittance_id = db.Column(db.String(60), nullable=True, index=True)
    related_batch_id = db.Column(db.String(60), nullable=True, index=True)
    is_historical = db.Column(db.Boolean, nullable=False, default=False)
    created_by = db.Column(db.String(150))
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class FinanceDuesPayment(db.Model):
    """Catatan setoran iuran fraksi per anggota dewan per bulan (DPW + Titipan DPP)."""

    __tablename__ = "finance_dues_payments"

    id = db.Column(db.String(60), primary_key=True)
    legislator_id = db.Column(
        db.String(50),
        db.ForeignKey("finance_legislators.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    period_month = db.Column(db.String(7), nullable=False, index=True)  # YYYY-MM
    payment_date = db.Column(db.Date, nullable=False, index=True)
    amount_dpw = db.Column(db.BigInteger, nullable=False, default=0)
    amount_dpp = db.Column(db.BigInteger, nullable=False, default=0)
    payment_method = db.Column(db.String(20), nullable=False, default="MANUAL", index=True)  # MANUAL | AUTODEBET
    dpp_remittance_id = db.Column(
        db.String(60),
        db.ForeignKey("finance_dpp_remittances.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    transaction_id = db.Column(
        db.String(60),
        db.ForeignKey("finance_transactions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    proof_url = db.Column(db.Text)
    notes = db.Column(db.Text)
    is_historical = db.Column(db.Boolean, nullable=False, default=False)
    created_by = db.Column(db.String(150))
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    legislator = db.relationship("FinanceLegislator", back_populates="dues_payments")
    dpp_remittance = db.relationship("FinanceDppRemittance", back_populates="payments")

    __table_args__ = (
        db.Index("idx_finance_dues_leg_month", "legislator_id", "period_month"),
    )
