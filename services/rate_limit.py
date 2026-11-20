"""Rate limit sederhana di memori untuk endpoint kredensial.

Sengaja tanpa dependensi baru: jendela geser per kunci (scope + IP +
identitas) disimpan sebagai deque timestamp. Backend berjalan satu
proses uvicorn, jadi memori proses cukup; bila suatu hari jalan
multi-worker, batas ini berlaku per worker (tetap membatasi, hanya
tidak global).

IP klien: backend selalu berada di belakang proxy milik sendiri
(Caddy di VPS) atau diakses langsung (laptop), jadi entri pertama
X-Forwarded-For dipakai bila ada; selain itu alamat koneksi langsung.
"""

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

_hits: dict[str, deque] = defaultdict(deque)


def client_ip(request: Request) -> str:
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if xff:
        return xff
    return request.client.host if request.client else "?"


def enforce(request: Request, scope: str, identity: str, limit: int, window_seconds: int) -> None:
    """Lempar 429 bila (scope, IP, identity) melewati limit dalam jendela."""
    key = f"{scope}:{client_ip(request)}:{identity.strip().lower()}"
    now = time.time()
    q = _hits[key]
    while q and q[0] <= now - window_seconds:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(
            status_code=429,
            detail="Terlalu banyak percobaan. Coba lagi beberapa menit lagi.",
        )
    q.append(now)
