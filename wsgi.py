import os
from app import create_app

app = create_app()

if __name__ == "__main__":
    is_prod = os.getenv("FLASK_ENV") == "production"
    port = int(os.getenv("PORT", "5000"))
    # threaded=True wajib aktif agar ratusan request bersamaan tidak saling memblokir
    app.run(host="0.0.0.0", port=port, debug=not is_prod, threaded=True)
