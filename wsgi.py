import os
import sys

# Pastikan direktori root backend selalu berada di urutan pertama sys.path (penting untuk Gunicorn / VPS)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

from app import create_app

app = create_app()

if __name__ == "__main__":
    is_prod = os.getenv("FLASK_ENV") == "production"
    port = int(os.getenv("PORT", "5000"))
    # threaded=True wajib aktif agar ratusan request bersamaan tidak saling memblokir
    app.run(host="0.0.0.0", port=port, debug=not is_prod, threaded=True)
