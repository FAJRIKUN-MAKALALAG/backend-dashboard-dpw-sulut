"""Rate limiter in-memory thread-safe berbasis sliding window untuk proteksi anti brute-force & anti spam."""
from __future__ import annotations

from collections import deque
from functools import wraps
import logging
import os
import threading
import time
from typing import Deque

from flask import jsonify, make_response, request
from flask_jwt_extended import get_jwt, verify_jwt_in_request

logger = logging.getLogger("nasdem.rate_limit")

MAX_BUCKETS = 10000
_lock = threading.Lock()
_buckets: dict[str, Deque[float]] = {}
_bucket_windows: dict[str, int] = {}


def reset_rate_limits() -> None:
    """Membersihkan seluruh bucket rate limit (dipakai untuk unit test)."""
    with _lock:
        _buckets.clear()
        _bucket_windows.clear()


def _is_enabled() -> bool:
    raw = os.getenv("RATE_LIMIT_ENABLED", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _get_client_ip() -> str:
    xff = request.headers.get("X-Forwarded-For", "").strip()
    if xff:
        first_ip = xff.split(",")[0].strip()
        if first_ip:
            return first_ip
    xri = request.headers.get("X-Real-IP", "").strip()
    if xri:
        return xri
    return (request.remote_addr or "unknown").strip()


def _resolve_identity(scope: str) -> str:
    if scope == "user_or_ip":
        try:
            verify_jwt_in_request(optional=True)
            claims = get_jwt()
            if claims:
                ident = claims.get("sub") or claims.get("adminNik") or claims.get("username")
                if ident:
                    return f"user:{ident}"
        except Exception:
            pass
    return f"ip:{_get_client_ip()}"


def _prune_buckets_unlocked(now: float) -> None:
    stale_keys = []
    for k, q in _buckets.items():
        win = _bucket_windows.get(k, 60)
        while q and (now - q[0]) >= win:
            q.popleft()
        if not q:
            stale_keys.append(k)
    for k in stale_keys:
        _buckets.pop(k, None)
        _bucket_windows.pop(k, None)

    # Jika masih penuh, pangkas bucket paling lama
    while len(_buckets) >= MAX_BUCKETS:
        oldest_key = next(iter(_buckets))
        _buckets.pop(oldest_key, None)
        _bucket_windows.pop(oldest_key, None)


def rate_limit(
    limit: int,
    window_seconds: int = 60,
    *,
    scope: str = "ip",
    key_prefix: str = "",
):
    """Decorator rate limit sliding-window per IP atau per User JWT."""

    def decorator(fn):
        prefix = key_prefix or f"{fn.__module__}.{fn.__name__}"

        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not _is_enabled() or limit <= 0 or window_seconds <= 0:
                return fn(*args, **kwargs)

            identity = _resolve_identity(scope)
            bucket_key = f"{prefix}:{identity}"
            now = time.monotonic()

            with _lock:
                if len(_buckets) >= MAX_BUCKETS:
                    _prune_buckets_unlocked(now)

                bucket = _buckets.get(bucket_key)
                if bucket is None:
                    bucket = deque()
                    _buckets[bucket_key] = bucket
                _bucket_windows[bucket_key] = window_seconds

                while bucket and (now - bucket[0]) >= window_seconds:
                    bucket.popleft()

                if len(bucket) >= limit:
                    retry_after = max(1, int(window_seconds - (now - bucket[0])) + 1)
                    logger.warning(
                        f"[RATE-LIMIT] {bucket_key} melebihi batas {limit}/{window_seconds}s"
                    )
                    resp = make_response(
                        jsonify(
                            error=(
                                f"Terlalu banyak percobaan dalam waktu singkat. "
                                f"Coba lagi dalam {retry_after} detik."
                            ),
                            retryAfter=retry_after,
                        ),
                        429,
                    )
                    resp.headers["Retry-After"] = str(retry_after)
                    resp.headers["X-RateLimit-Limit"] = str(limit)
                    resp.headers["X-RateLimit-Remaining"] = "0"
                    return resp

                bucket.append(now)
                remaining = max(0, limit - len(bucket))

            resp = make_response(fn(*args, **kwargs))
            resp.headers.setdefault("X-RateLimit-Limit", str(limit))
            resp.headers.setdefault("X-RateLimit-Remaining", str(remaining))
            return resp

        return wrapper

    return decorator
