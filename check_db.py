from app import create_app, db

app = create_app()
with app.app_context():
    tables = [t[0] for t in db.session.execute(db.text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")).fetchall()]
    print("TABLES FOUND:", tables)
    for t in tables:
        try:
            cnt = db.session.execute(db.text(f'SELECT count(*) FROM "{t}"')).scalar()
            print(f"  {t}: {cnt} rows")
        except Exception as e:
            print(f"  {t}: Error {e}")

    cols = [c[0] for c in db.session.execute(db.text("SELECT column_name FROM information_schema.columns WHERE table_name='members'")).fetchall()]
    print("\nCOLUMNS IN members TABLE:")
    print(cols)
    print("\nIS 'urutan' PRESENT IN members?:", "urutan" in cols)

