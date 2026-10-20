from app import create_app, db
from app.models import AppUser, Member

app = create_app()
with app.app_context():
    print("=== APP USERS ===")
    users = AppUser.query.all()
    for u in users:
        print(f"User: id={u.id}, username={u.username}, role={u.role}, dpd_access={u.dpd_access}, active={u.is_active}")

    print("\n=== SAMPLE MEMBERS ===")
    sample = Member.query.limit(5).all()
    for m in sample:
        print(f"Member: nik={m.nik}, nama={m.nama_lengkap}, dpd={m.dpd}, dpc={m.dpc}, status={m.status}")
