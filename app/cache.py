"""Thread-safe in-memory TTL cache + Single-Flight request coalescing (anti Thundering Herd).

Dirancang khusus agar saat 100+ pengguna login atau membuka halaman secara bersamaan:
1. Permintaan identik dalam cakupan hak akses wilayah (RBAC scope) yang sama digabungkan (Single-Flight),
   sehingga PostgreSQL hanya dieksekusi 1 kali sementara pengguna lain membaca hasil dari RAM (< 1 ms).
2. Setiap operasi tulis (tambah, ubah, hapus, import, mutasi status, permohonan hapus) langsung
   memanggil `invalidate_all_data_cache()` sehingga data selalu 100% akurat dan real-time.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from .utils import is_global

MAX_CACHE_ENTRIES = 512

_store_lock = threading.Lock()
_store: OrderedDict[str, tuple[float, Any]] = OrderedDict()
_inflight_locks: dict[str, threading.Lock] = {}


def scope_key_from_claims(claims: dict) -> str:
    """Membentuk identitas cakupan data berdasarkan klaim JWT pengguna agar aman secara RBAC."""
    if not claims:
        return "ANON"
    if is_global(claims):
        return "GLOBAL"
    role = claims.get("role") or "UNKNOWN"
    if role == "ANGGOTA":
        return f"ANGGOTA:{claims.get('adminNik') or ''}"
    dpd = claims.get("dpdAccess") or ""
    if role == "ADMIN_DPD":
        return f"DPD:{dpd}"
    dpc = claims.get("dpcAccess") or ""
    if role == "ADMIN_DPC":
        return f"DPC:{dpd}:{dpc}"
    dprt = claims.get("dprtAccess") or ""
    if role == "ADMIN_DPRT":
        return f"DPRT:{dpd}:{dpc}:{dprt}"
    return f"OTHER:{role}:{dpd}:{dpc}:{dprt}:{claims.get('adminNik') or ''}"


def _get_valid_unlocked(full_key: str, now: float) -> tuple[bool, Any]:
    entry = _store.get(full_key)
    if entry is None:
        return False, None
    expires_at, value = entry
    if now >= expires_at:
        _store.pop(full_key, None)
        return False, None
    _store.move_to_end(full_key)
    return True, value


def get_or_compute(
    namespace: str,
    claims: dict,
    subkey: str,
    ttl_seconds: float,
    compute_fn: Callable[[], Any],
) -> Any:
    """Ambil hasil dari cache RAM atau hitung satu kali saja (Single-Flight) jika cache kosong/kedaluwarsa."""
    if ttl_seconds <= 0:
        return compute_fn()

    scope_sig = scope_key_from_claims(claims)
    full_key = f"{namespace}|{scope_sig}|{subkey}"
    now = time.monotonic()

    with _store_lock:
        hit, val = _get_valid_unlocked(full_key, now)
        if hit:
            return val
        key_lock = _inflight_locks.get(full_key)
        if key_lock is None:
            key_lock = threading.Lock()
            _inflight_locks[full_key] = key_lock

    with key_lock:
        now2 = time.monotonic()
        with _store_lock:
            hit, val = _get_valid_unlocked(full_key, now2)
            if hit:
                return val

        computed = compute_fn()

        with _store_lock:
            _store[full_key] = (time.monotonic() + ttl_seconds, computed)
            _store.move_to_end(full_key)
            while len(_store) > MAX_CACHE_ENTRIES:
                _store.popitem(last=False)
            # Bersihkan key_lock jika tidak ada thread lain yang antre
            _inflight_locks.pop(full_key, None)

        return computed


def invalidate_namespace(*namespaces: str) -> None:
    """Menghapus entri cache pada namespace tertentu."""
    prefixes = tuple(f"{ns}|" for ns in namespaces)
    with _store_lock:
        keys_to_remove = [k for k in _store if k.startswith(prefixes)]
        for k in keys_to_remove:
            _store.pop(k, None)


def invalidate_all_data_cache() -> None:
    """Menghapus seluruh cache data (dipanggil setiap ada perubahan database)."""
    with _store_lock:
        _store.clear()

