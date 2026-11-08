"""Router System Dashboard — gerbang admin (spec vault 5 Okt 2026, §9).

Tahap fondasi (5 Nov 2026): login + verifikasi token saja; endpoint
modul (projects/logs/integrations/contents/users/agent) menyusul
bertahap dan WAJIB lewat require_admin.

Kredensial admin TIDAK disimpan di kode/database — dibaca dari
environment backend:
  DASHBOARD_ADMIN_USER      username admin
  DASHBOARD_ADMIN_PASSWORD  password admin
  DASHBOARD_SECRET_KEY      kunci penanda token sesi
Token sesi stateless: payload base64url "user.exp" + HMAC-SHA256,
kedaluwarsa 12 jam, dikirim klien sebagai header X-Admin-Token.
"""

import base64
import hashlib
import hmac
import os
import time

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/v1/dashboard", tags=["dashboard"])

TOKEN_TTL_SECONDS = 12 * 3600


def _secret() -> bytes:
    return os.getenv("DASHBOARD_SECRET_KEY", "").encode("utf-8")


def _issue_token(user: str) -> str:
    payload = f"{user}.{int(time.time()) + TOKEN_TTL_SECONDS}"
    sig = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    body = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    return f"{body}.{sig}"


def _verify_token(token: str | None) -> str | None:
    if not token or "." not in token or not _secret():
        return None
    body, _, sig = token.rpartition(".")
    try:
        payload = base64.urlsafe_b64decode(body.encode("ascii")).decode("utf-8")
    except Exception:
        return None
    expected = hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    user, _, exp = payload.rpartition(".")
    if not user or not exp.isdigit() or int(exp) < int(time.time()):
        return None
    return user


async def require_admin(x_admin_token: str | None = Header(default=None)) -> str:
    """Dependency gerbang semua endpoint modul dashboard."""
    user = _verify_token(x_admin_token)
    if not user:
        raise HTTPException(status_code=401, detail="Token admin tidak valid atau sudah kedaluwarsa.")
    return user


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def dashboard_login(payload: LoginRequest):
    env_user = os.getenv("DASHBOARD_ADMIN_USER", "")
    env_pass = os.getenv("DASHBOARD_ADMIN_PASSWORD", "")
    if not env_user or not env_pass or not _secret():
        raise HTTPException(
            status_code=503,
            detail="Login dashboard belum dikonfigurasi di backend (env admin belum diisi).",
        )
    ok = hmac.compare_digest(payload.username, env_user) and hmac.compare_digest(
        payload.password, env_pass
    )
    if not ok:
        raise HTTPException(status_code=401, detail="Username atau password salah.")
    return {
        "token": _issue_token(env_user),
        "token_type": "admin",
        "expires_in": TOKEN_TTL_SECONDS,
    }


@router.get("/me")
async def dashboard_me(admin: str = Depends(require_admin)):
    return {"ok": True, "user": admin}


# ── Modul 1: Projects (spec §3) ─────────────────────────────
# Registry project di PostgreSQL (services/dashboard_store.py).
# Semua endpoint bergerbang require_admin; hapus = soft delete.

from typing import Literal

from sqlalchemy.orm import Session

from services import dashboard_store as store


class ProjectIn(BaseModel):
    name: str
    description: str | None = None
    tech_stack: str | None = None
    local_path: str | None = None
    repo_url: str | None = None
    status: Literal["idea", "active", "maintenance", "archived"] = "active"
    links: dict = {}
    demo_video_url: str | None = None
    demo_video_thumbnail: str | None = None


def _project_or_404(db: Session, project_id: int):
    p = store.get_project(db, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Project tidak ditemukan.")
    return p


@router.get("/projects")
async def list_projects(
    status: str | None = None,
    q: str | None = None,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    rows = store.list_projects(db, status=status, q=q)
    return {"items": [store.project_to_dict(p) for p in rows], "total": len(rows)}


@router.post("/projects", status_code=201)
async def create_project(
    payload: ProjectIn,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    if not payload.name.strip():
        raise HTTPException(status_code=422, detail="Nama project wajib diisi.")
    p = store.create_project(db, payload.model_dump())
    return store.project_to_dict(p)


@router.get("/projects/{project_id}")
async def get_project(
    project_id: int,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    return store.project_to_dict(_project_or_404(db, project_id))


@router.put("/projects/{project_id}")
async def update_project(
    project_id: int,
    payload: ProjectIn,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    if not payload.name.strip():
        raise HTTPException(status_code=422, detail="Nama project wajib diisi.")
    p = _project_or_404(db, project_id)
    return store.project_to_dict(store.update_project(db, p, payload.model_dump()))


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: int,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    p = _project_or_404(db, project_id)
    store.soft_delete_project(db, p)
    return {"ok": True, "deleted_id": project_id}


class FeaturedIn(BaseModel):
    featured: bool


@router.post("/projects/{project_id}/featured")
async def set_project_featured(
    project_id: int,
    payload: FeaturedIn,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    """Pin/lepas Top Project (maks 4 — tampil di section Projects portofolio)."""
    p = _project_or_404(db, project_id)
    return store.project_to_dict(store.set_featured(db, p, payload.featured))


class ReorderIn(BaseModel):
    ids: list[int]


@router.post("/projects/reorder-featured")
async def reorder_featured(
    payload: ReorderIn,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    rows = store.reorder_featured(db, payload.ids)
    return {"items": [store.project_to_dict(p) for p in rows]}


# ── Endpoint PUBLIK projects (tanpa token) ──────────────────
# Konsumen: section Projects + halaman /projects di portofolio.
# Hanya field aman-publik (project_to_public_dict — tanpa local_path)
# dan hanya status active/maintenance.

public_router = APIRouter(prefix="/v1/projects", tags=["public-projects"])


@public_router.get("/featured")
def public_featured(db: Session = Depends(store.dashboard_session)):
    rows = store.featured_projects(db, public_only=True)
    return {
        "items": [store.project_to_public_dict(p) for p in rows],
        "total": len(rows),
    }


@public_router.get("")
def public_list(db: Session = Depends(store.dashboard_session)):
    rows = store.public_projects(db)
    return {
        "items": [store.project_to_public_dict(p) for p in rows],
        "total": len(rows),
    }


# ── Modul 3: Integrations (spec §5; diskusi skema 8 Nov 2026) ─
# v1: kredensial HANYA referensi env var — nilai secret dibaca backend
# dari environment saat test koneksi; tidak disimpan di DB, tidak pernah
# dikembalikan API (hanya boolean keberadaannya). Header kustom yang
# namanya sensitif (authorization/api-key/token) ditampilkan ter-mask
# dan nilai "***" saat PUT berarti "pertahankan nilai lama".

import re as _re
from urllib.parse import urlparse as _urlparse

import httpx as _httpx

_SENSITIVE_HEADERS = {"authorization", "x-api-key", "api-key", "x-auth-token", "token"}
_ENV_NAME_RE = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class IntegrationIn(BaseModel):
    name: str
    kind: Literal["rest", "webhook", "mcp", "other"] = "rest"
    base_url: str
    auth_type: Literal["none", "api_key", "bearer", "basic", "oauth"] = "none"
    auth_env_var: str | None = None
    headers: dict = {}
    notes: str | None = None
    status: Literal["active", "inactive"] = "active"


def _integration_or_404(db: Session, integration_id: int):
    i = store.get_integration(db, integration_id)
    if i is None:
        raise HTTPException(status_code=404, detail="Integrasi tidak ditemukan.")
    return i


def _validate_integration(payload: IntegrationIn) -> dict:
    if not payload.name.strip():
        raise HTTPException(status_code=422, detail="Nama integrasi wajib diisi.")
    parsed = _urlparse(payload.base_url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(
            status_code=422,
            detail="base_url harus URL http(s) yang valid (cegah SSRF, spec §9).",
        )
    env_name = (payload.auth_env_var or "").strip() or None
    if env_name and not _ENV_NAME_RE.match(env_name):
        raise HTTPException(
            status_code=422,
            detail="auth_env_var harus nama variabel environment yang valid (huruf/angka/garis bawah).",
        )
    headers = {}
    for k, v in (payload.headers or {}).items():
        key = str(k).strip()
        if key:
            headers[key] = str(v)
    data = payload.model_dump()
    data["name"] = payload.name.strip()
    data["base_url"] = payload.base_url.strip()
    data["auth_env_var"] = env_name
    data["headers"] = headers
    return data


def _mask_headers(headers: dict) -> dict:
    return {
        k: ("***" if k.lower() in _SENSITIVE_HEADERS and v else v)
        for k, v in (headers or {}).items()
    }


def _integration_out(i) -> dict:
    d = store.integration_to_dict(i)
    d["headers"] = _mask_headers(d.get("headers") or {})
    return d


@router.get("/integrations")
async def list_integrations(
    status: str | None = None,
    q: str | None = None,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    rows = store.list_integrations(db, status=status, q=q)
    return {"items": [_integration_out(i) for i in rows], "total": len(rows)}


@router.post("/integrations", status_code=201)
async def create_integration(
    payload: IntegrationIn,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    data = _validate_integration(payload)
    i = store.create_integration(db, data)
    return _integration_out(i)


@router.get("/integrations/{integration_id}")
async def get_integration(
    integration_id: int,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    return _integration_out(_integration_or_404(db, integration_id))


@router.put("/integrations/{integration_id}")
async def update_integration(
    integration_id: int,
    payload: IntegrationIn,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    i = _integration_or_404(db, integration_id)
    data = _validate_integration(payload)
    # Nilai "***" pada header sensitif = pertahankan nilai tersimpan.
    old_headers = i.headers or {}
    for k, v in list(data["headers"].items()):
        if v == "***" and k.lower() in _SENSITIVE_HEADERS and k in old_headers:
            data["headers"][k] = old_headers[k]
    return _integration_out(store.update_integration(db, i, data))


@router.delete("/integrations/{integration_id}")
async def delete_integration(
    integration_id: int,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    i = _integration_or_404(db, integration_id)
    store.soft_delete_integration(db, i)
    return {"ok": True, "deleted_id": integration_id}


def _resolve_auth_headers(i):
    """Header auth dari env var. Nilai secret hanya hidup di dict ini
    selama request test — tidak dilog, tidak masuk respons."""
    headers = dict(i.headers or {})
    if i.auth_type == "none":
        return headers, None
    if not i.auth_env_var:
        return headers, f"auth_type '{i.auth_type}' butuh auth_env_var, tapi kolomnya kosong."
    value = os.getenv(i.auth_env_var)
    if not value:
        return headers, f"Env var {i.auth_env_var} tidak terisi di environment backend."
    if i.auth_type == "api_key":
        headers.setdefault("X-API-Key", value)
    elif i.auth_type in ("bearer", "oauth"):
        headers.setdefault("Authorization", f"Bearer {value}")
    elif i.auth_type == "basic":
        headers.setdefault(
            "Authorization",
            "Basic " + base64.b64encode(value.encode()).decode(),
        )
    return headers, None


@router.post("/integrations/{integration_id}/test")
async def test_integration(
    integration_id: int,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    i = _integration_or_404(db, integration_id)
    headers, err = _resolve_auth_headers(i)
    if err:
        store.record_integration_test(db, i, False)
        return {"ok": False, "error": err, "latency_ms": None}
    start = time.perf_counter()
    try:
        async with _httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(i.base_url, headers=headers)
        latency = int((time.perf_counter() - start) * 1000)
        ok = resp.status_code < 400
        store.record_integration_test(db, i, ok)
        return {
            "ok": ok,
            "status_code": resp.status_code,
            "latency_ms": latency,
            "error": None if ok else f"Server menjawab HTTP {resp.status_code}.",
        }
    except Exception as e:  # koneksi gagal/timeout/dll — pesan wajib ter-mask
        latency = int((time.perf_counter() - start) * 1000)
        store.record_integration_test(db, i, False)
        return {
            "ok": False,
            "status_code": None,
            "latency_ms": latency,
            "error": _mask_secrets(str(e))[:200] or "Koneksi gagal.",
        }


# ── Modul 2: System Log + pantau sistem (spec §4) ───────────
# vault   : parse ~/agent_knowledge/_log.md per entri (read-only).
# backend : tail log backend (~/vienna-backend.log) per baris.
# Semua teks yang keluar WAJIB lewat _mask_secrets (spec §4b).

import re
from pathlib import Path

_SECRET_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|key|token|password|passwd|secret|authorization)(\s*[:=]\s*)\S+"), r"\1\2***"),
    (re.compile(r"([?&](?:key|token|password|secret|api_key)=)[^&\s]+"), r"\1***"),
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "sk-***"),
    (re.compile(r"AIza[0-9A-Za-z_-]{20,}"), "***"),
    (re.compile(r"(?i)bearer\s+\S+"), "Bearer ***"),
]


def _mask_secrets(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _vault_log_path() -> Path:
    return Path(os.getenv("DASHBOARD_VAULT_LOG", "~/agent_knowledge/_log.md")).expanduser()


def _backend_log_path() -> Path:
    return Path(os.getenv("DASHBOARD_BACKEND_LOG", "~/vienna-backend.log")).expanduser()


_VAULT_HEAD = re.compile(r"^## \[([^\]]+)\]\s*([^|]+?)\s*\|\s*(.+)$", re.MULTILINE)


def _parse_vault_entries(text: str) -> list[dict]:
    matches = list(_VAULT_HEAD.finditer(text))
    entries = []
    for i, m in enumerate(matches):
        body = text[m.end(): matches[i + 1].start() if i + 1 < len(matches) else len(text)].strip()
        summary = body
        pages: list[str] = []
        sm = re.search(r"^Summary:\s*(.+)$", body, re.MULTILINE | re.DOTALL)
        if sm:
            summary = sm.group(1).strip()
        pm = re.search(r"^Pages touched:\s*(.+)$", body, re.MULTILINE)
        if pm:
            pages = [p.strip() for p in pm.group(1).split(",") if p.strip()]
        entries.append(
            {
                "timestamp": m.group(1).strip(),
                "category": m.group(2).strip(),
                "title": m.group(3).strip(),
                "summary": _mask_secrets(summary),
                "pages": pages,
            }
        )
    entries.reverse()  # file kronologis lama->baru; API terbaru dulu
    return entries


def _read_tail(path: Path, max_bytes: int) -> str:
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        return fh.read().decode("utf-8", errors="replace")


def _parse_backend_lines(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        upper = line.upper()
        if "ERROR" in upper or "CRITICAL" in upper or "TRACEBACK" in upper:
            level = "ERROR"
        elif "WARN" in upper:
            level = "WARNING"
        else:
            level = "INFO"
        rows.append({"level": level, "message": _mask_secrets(line.rstrip())})
    rows.reverse()  # terbaru dulu
    return rows


@router.get("/logs")
def dashboard_logs(
    source: str = "vault",
    level: str | None = None,
    category: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
    admin: str = Depends(require_admin),
):
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    if source == "backend":
        path = _backend_log_path()
        if not path.exists():
            return {"source": source, "available": False, "items": [], "total": 0}
        rows = _parse_backend_lines(_read_tail(path, 512_000))
        if level:
            rows = [r for r in rows if r["level"] == level.upper()]
    else:
        path = _vault_log_path()
        if not path.exists():
            return {"source": "vault", "available": False, "items": [], "total": 0}
        rows = _parse_vault_entries(_read_tail(path, 1_000_000))
        if category:
            rows = [r for r in rows if r["category"].lower() == category.lower()]
    if q:
        needle = q.lower()
        rows = [
            r
            for r in rows
            if needle in (r.get("message") or r.get("summary", "")).lower()
            or needle in r.get("title", "").lower()
        ]
    total = len(rows)
    return {
        "source": source,
        "available": True,
        "file": path.name,
        "items": rows[offset: offset + limit],
        "total": total,
    }


@router.get("/system")
def dashboard_system(admin: str = Depends(require_admin)):
    import platform
    import time as _time

    import psutil

    vm = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    boot = psutil.boot_time()
    return {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "cpu_count": psutil.cpu_count(),
        "memory": {
            "percent": vm.percent,
            "used_mb": round(vm.used / 1_048_576),
            "total_mb": round(vm.total / 1_048_576),
        },
        "disk": {
            "percent": disk.percent,
            "used_gb": round(disk.used / 1_073_741_824, 1),
            "total_gb": round(disk.total / 1_073_741_824, 1),
            "path": "/",
        },
        "uptime_seconds": int(_time.time() - boot),
        "boot_time": _time.strftime("%Y-%m-%dT%H:%M:%S", _time.localtime(boot)),
    }
