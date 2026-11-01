"""Konfigurasi Gunicorn Production untuk menangani 100+ pengguna aktif secara bersamaan.

Cara menjalankan di Linux VPS / Production:
    gunicorn -c gunicorn.conf.py wsgi:app
"""
import multiprocessing
import os

bind = f"0.0.0.0:{os.getenv('PORT', '5000')}"

# Gunakan worker moderat + multi-threading (gthread) agar:
# 1. Mampu melayani ratusan request paralel sekaligus
# 2. In-memory cache & DB connection pool per proses tetap hemat koneksi PostgreSQL/Supabase
_cpu = multiprocessing.cpu_count()
workers = int(os.getenv("WEB_CONCURRENCY", min(max(_cpu, 2), 4)))
worker_class = "gthread"
threads = int(os.getenv("GUNICORN_THREADS", 8))

timeout = 120
keepalive = 5
max_requests = 2000
max_requests_jitter = 200
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info")

