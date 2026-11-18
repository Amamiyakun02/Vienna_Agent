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


# ── Modul 5: Users (spec §7; diskusi skema 8 Nov 2026) ──
# Password datang sebagai plaintext HANYA di badan request HTTPS lokal
# ini, langsung di-hash bcrypt di store, tidak pernah dilog dan tidak
# pernah dikembalikan. Reset password = endpoint terpisah.

_PROJECT_RE = _re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")
_USERNAME_RE = _re.compile(r"^[A-Za-z0-9_.-]{3,50}$")
_EMAIL_RE = _re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserCreateIn(BaseModel):
    project: str
    username: str
    email: str
    password: str
    role: Literal["admin", "user"] = "user"
    status: Literal["active", "suspended"] = "active"


class UserUpdateIn(BaseModel):
    project: str
    username: str
    email: str
    role: Literal["admin", "user"] = "user"
    status: Literal["active", "suspended"] = "active"


class ResetPasswordIn(BaseModel):
    password: str


def _validate_user_fields(project: str, username: str, email: str) -> dict:
    project = project.strip().lower()
    username = username.strip()
    email = email.strip()
    if not _PROJECT_RE.match(project):
        raise HTTPException(
            status_code=422,
            detail="Slug project tidak valid (huruf kecil/angka/strip, 2-61 karakter).",
        )
    if not _USERNAME_RE.match(username):
        raise HTTPException(
            status_code=422,
            detail="Username tidak valid (3-50 karakter: huruf, angka, titik, strip, garis bawah).",
        )
    if len(email) > 200 or not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="Format email tidak valid.")
    return {"project": project, "username": username, "email": email}


def _validate_password(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(
            status_code=422, detail="Password minimal 8 karakter."
        )
    if len(password.encode("utf-8")) > 72:
        raise HTTPException(
            status_code=422,
            detail="Password terlalu panjang (maks 72 byte, batas bcrypt).",
        )


def _user_or_404(db: Session, user_id: int):
    u = store.get_user(db, user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="User tidak ditemukan.")
    return u


@router.get("/users")
async def list_users(
    project: str | None = None,
    status: str | None = None,
    q: str | None = None,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    rows = store.list_users(db, project=project, status=status, q=q)
    projects = sorted({u.project for u in store.list_users(db)})
    return {
        "items": [store.user_to_dict(u) for u in rows],
        "total": len(rows),
        "projects": projects,
    }


@router.post("/users", status_code=201)
async def create_user(
    payload: UserCreateIn,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    fields = _validate_user_fields(payload.project, payload.username, payload.email)
    _validate_password(payload.password)
    if store.find_user(db, fields["project"], fields["username"]) is not None:
        raise HTTPException(
            status_code=409,
            detail="Username sudah dipakai di project ini.",
        )
    data = {**fields, "role": payload.role, "status": payload.status}
    u = store.create_user(db, data, payload.password)
    return store.user_to_dict(u)


@router.get("/users/{user_id}")
async def get_user(
    user_id: int,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    return store.user_to_dict(_user_or_404(db, user_id))


@router.put("/users/{user_id}")
async def update_user(
    user_id: int,
    payload: UserUpdateIn,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    u = _user_or_404(db, user_id)
    fields = _validate_user_fields(payload.project, payload.username, payload.email)
    clash = store.find_user(db, fields["project"], fields["username"])
    if clash is not None and clash.id != u.id:
        raise HTTPException(
            status_code=409,
            detail="Username sudah dipakai di project ini.",
        )
    data = {**fields, "role": payload.role, "status": payload.status}
    return store.user_to_dict(store.update_user(db, u, data))


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    u = _user_or_404(db, user_id)
    store.soft_delete_user(db, u)
    return {"ok": True, "deleted_id": user_id}


@router.post("/users/{user_id}/reset-password")
async def reset_user_password(
    user_id: int,
    payload: ResetPasswordIn,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    u = _user_or_404(db, user_id)
    _validate_password(payload.password)
    store.reset_user_password(db, u, payload.password)
    return {"ok": True, "user_id": user_id}


@router.post("/users/{user_id}/suspend")
async def suspend_user(
    user_id: int,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    u = _user_or_404(db, user_id)
    return store.user_to_dict(store.update_user(db, u, {"status": "suspended"}))


@router.post("/users/{user_id}/activate")
async def activate_user(
    user_id: int,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    u = _user_or_404(db, user_id)
    return store.user_to_dict(store.update_user(db, u, {"status": "active"}))


# ── Modul 4: Contents (spec §6; diskusi skema 8 Nov 2026) ──
# Admin: CRUD penuh bergerbang token. Publik: router terpisah tanpa
# token yang HANYA menyajikan konten published (draft tidak bocor —
# slug draft menjawab 404 di sisi publik).

_SLUG_RE = _re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ContentIn(BaseModel):
    type: Literal["project", "blog", "paper", "research"] = "blog"
    title: str
    slug: str | None = None
    summary: str | None = None
    body: str | None = None
    tags: list = []
    thumbnail: str | None = None
    status: Literal["draft", "published"] = "draft"


def _slugify(title: str) -> str:
    s = _re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:200] or "konten"


def _unique_slug(db: Session, base: str, exclude_id: int | None = None) -> str:
    slug = base
    n = 2
    while True:
        found = store.find_content_by_slug(db, slug)
        if found is None or (exclude_id is not None and found.id == exclude_id):
            return slug
        slug = f"{base[:205]}-{n}"
        n += 1


def _content_or_404(db: Session, content_id: int):
    c = store.get_content(db, content_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Konten tidak ditemukan.")
    return c


def _validate_content(payload: ContentIn, db: Session, exclude_id: int | None = None) -> dict:
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Judul wajib diisi.")
    if len(title) > 200:
        raise HTTPException(status_code=422, detail="Judul maksimal 200 karakter.")
    slug_in = (payload.slug or "").strip().lower()
    if slug_in:
        if len(slug_in) > 220 or not _SLUG_RE.match(slug_in):
            raise HTTPException(
                status_code=422,
                detail="Slug tidak valid (huruf kecil/angka/strip, tanpa spasi).",
            )
        clash = store.find_content_by_slug(db, slug_in)
        if clash is not None and clash.id != exclude_id:
            raise HTTPException(
                status_code=409, detail="Slug sudah dipakai konten lain."
            )
        slug = slug_in
    else:
        slug = _unique_slug(db, _slugify(title), exclude_id=exclude_id)
    tags = []
    for t in payload.tags or []:
        ts = str(t).strip()
        if ts:
            tags.append(ts[:50])
    if len(tags) > 20:
        raise HTTPException(status_code=422, detail="Maksimal 20 tag.")
    thumb = (payload.thumbnail or "").strip() or None
    if thumb and not (
        thumb.startswith("http://")
        or thumb.startswith("https://")
        or thumb.startswith("/")
    ):
        raise HTTPException(
            status_code=422,
            detail="Thumbnail harus URL http(s) atau path diawali /.",
        )
    return {
        "type": payload.type,
        "title": title,
        "slug": slug,
        "summary": (payload.summary or "").strip() or None,
        "body": payload.body,
        "tags": tags,
        "thumbnail": thumb,
        "status": payload.status,
    }


@router.get("/contents")
async def list_contents(
    type: str | None = None,
    status: str | None = None,
    q: str | None = None,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    rows = store.list_contents(db, type=type, status=status, q=q)
    return {"items": [store.content_to_dict(c) for c in rows], "total": len(rows)}


@router.post("/contents", status_code=201)
async def create_content(
    payload: ContentIn,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    data = _validate_content(payload, db)
    c = store.create_content(db, data)
    return store.content_to_dict(c)


@router.get("/contents/{content_id}")
async def get_content(
    content_id: int,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    return store.content_to_dict(_content_or_404(db, content_id))


@router.put("/contents/{content_id}")
async def update_content(
    content_id: int,
    payload: ContentIn,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    c = _content_or_404(db, content_id)
    data = _validate_content(payload, db, exclude_id=c.id)
    return store.content_to_dict(store.update_content(db, c, data))


@router.delete("/contents/{content_id}")
async def delete_content(
    content_id: int,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    c = _content_or_404(db, content_id)
    store.soft_delete_content(db, c)
    return {"ok": True, "deleted_id": content_id}


contents_public_router = APIRouter(prefix="/v1/contents", tags=["public-contents"])


@contents_public_router.get("")
def public_contents(
    type: str | None = None,
    db: Session = Depends(store.dashboard_session),
):
    rows = store.list_published_contents(db, type=type)
    return {
        "items": [store.content_to_public_dict(c) for c in rows],
        "total": len(rows),
    }


@contents_public_router.get("/{slug}")
def public_content_by_slug(
    slug: str,
    db: Session = Depends(store.dashboard_session),
):
    c = store.get_published_content_by_slug(db, slug)
    if c is None:
        raise HTTPException(status_code=404, detail="Konten tidak ditemukan.")
    return store.content_to_public_dict(c)


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


# ── Modul AI Providers (addendum spec 14 Nov 2026; dibangun 18 Nov) ──
# Registry platform AI ala halaman "API providers": CRUD base_url,
# daftar model, model default. API key TIDAK PERNAH disimpan di DB —
# yang disimpan hanya NAMA variabel di .env backend (auth_env_var);
# API hanya mengembalikan boolean auth_env_present.

_PROVIDER_KEY_RE = _re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class AiModelIn(BaseModel):
    id: str
    name: str | None = None


class AiProviderIn(BaseModel):
    name: str
    provider_key: str
    adapter: Literal["builtin", "openai-compatible"] = "openai-compatible"
    package: str | None = None
    base_url: str | None = None
    auth_env_var: str | None = None
    models: list[AiModelIn] = []
    default_model: str | None = None
    is_default: bool = False
    status: Literal["active", "inactive"] = "active"
    notes: str | None = None


def _ai_provider_or_404(db: Session, provider_id: int):
    p = store.get_ai_provider(db, provider_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Provider AI tidak ditemukan.")
    return p


def _validate_ai_provider(payload: AiProviderIn, db: Session, exclude_id: int | None = None) -> dict:
    if not payload.name.strip():
        raise HTTPException(status_code=422, detail="Nama provider wajib diisi.")
    key = payload.provider_key.strip()
    if not _PROVIDER_KEY_RE.match(key):
        raise HTTPException(
            status_code=422,
            detail="provider_key harus slug huruf kecil/angka, boleh '-' dan '_' (mis. 'openrouter').",
        )
    if store.get_ai_provider_by_key(db, key, exclude_id=exclude_id) is not None:
        raise HTTPException(status_code=409, detail=f"provider_key '{key}' sudah dipakai provider lain.")
    base_url = (payload.base_url or "").strip() or None
    if payload.adapter == "openai-compatible":
        if not base_url:
            raise HTTPException(status_code=422, detail="base_url wajib untuk adapter openai-compatible.")
        if not (payload.package or "").strip():
            raise HTTPException(
                status_code=422,
                detail="package wajib untuk adapter openai-compatible (mis. '@opencode/ai/providers/openai-compatible').",
            )
    if base_url:
        parsed = _urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HTTPException(status_code=422, detail="base_url harus URL http(s) yang valid.")
    env_name = (payload.auth_env_var or "").strip() or None
    if env_name and not _ENV_NAME_RE.match(env_name):
        raise HTTPException(
            status_code=422,
            detail="auth_env_var harus nama variabel environment yang valid (huruf/angka/garis bawah).",
        )
    models: list[dict] = []
    seen: set[str] = set()
    for m in payload.models or []:
        mid = (m.id or "").strip()
        if not mid:
            raise HTTPException(status_code=422, detail="Setiap model wajib punya id non-kosong.")
        if mid in seen:
            raise HTTPException(status_code=422, detail=f"Model '{mid}' duplikat dalam satu provider.")
        seen.add(mid)
        models.append({"id": mid, "name": (m.name or "").strip() or mid})
    default_model = (payload.default_model or "").strip() or None
    if default_model and default_model not in seen:
        raise HTTPException(status_code=422, detail="default_model harus salah satu id di daftar models.")
    data = payload.model_dump()
    data.update(
        {
            "name": payload.name.strip(),
            "provider_key": key,
            "package": (payload.package or "").strip() or None,
            "base_url": base_url,
            "auth_env_var": env_name,
            "models": models,
            "default_model": default_model,
            "notes": (payload.notes or "").strip() or None,
        }
    )
    return data


@router.get("/ai-providers")
async def list_ai_providers(
    status: str | None = None,
    q: str | None = None,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    rows = store.list_ai_providers(db, status=status, q=q)
    return {"items": [store.ai_provider_to_dict(p) for p in rows], "total": len(rows)}


@router.post("/ai-providers", status_code=201)
async def create_ai_provider(
    payload: AiProviderIn,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    data = _validate_ai_provider(payload, db)
    return store.ai_provider_to_dict(store.create_ai_provider(db, data))


@router.get("/ai-providers/{provider_id}")
async def get_ai_provider(
    provider_id: int,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    return store.ai_provider_to_dict(_ai_provider_or_404(db, provider_id))


@router.put("/ai-providers/{provider_id}")
async def update_ai_provider(
    provider_id: int,
    payload: AiProviderIn,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    p = _ai_provider_or_404(db, provider_id)
    data = _validate_ai_provider(payload, db, exclude_id=provider_id)
    return store.ai_provider_to_dict(store.update_ai_provider(db, p, data))


@router.delete("/ai-providers/{provider_id}")
async def delete_ai_provider(
    provider_id: int,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    p = _ai_provider_or_404(db, provider_id)
    store.soft_delete_ai_provider(db, p)
    return {"ok": True, "deleted_id": provider_id}


@router.post("/ai-providers/{provider_id}/test")
async def test_ai_provider(
    provider_id: int,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    """Tes koneksi: builtin = provider_key ada di katalog serve;
    openai-compatible = GET {base_url}/models (Bearer dari env bila
    auth_env_var terisi). Hasil dicatat ke last_tested_at/last_test_ok."""
    p = _ai_provider_or_404(db, provider_id)
    start = time.perf_counter()

    def _latency() -> int:
        return int((time.perf_counter() - start) * 1000)

    if p.adapter == "builtin":
        base = os.getenv("OPENCODE_ADMIN_BASE_URL") or os.getenv(
            "OPENCODE_BASE_URL", "http://127.0.0.1:4096"
        )
        password = os.getenv("OPENCODE_SERVER_PASSWORD", "")
        auth = ("opencode", password) if password else None
        try:
            async with _httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(base.rstrip("/") + "/api/model", auth=auth)
            if resp.status_code >= 400:
                store.record_ai_provider_test(db, p, False)
                return {
                    "ok": False,
                    "status_code": resp.status_code,
                    "latency_ms": _latency(),
                    "error": f"Serve opencode menjawab HTTP {resp.status_code}.",
                }
            ids = {(m or {}).get("providerID") for m in (resp.json().get("data") or [])}
            ok = p.provider_key in ids
            store.record_ai_provider_test(db, p, ok)
            return {
                "ok": ok,
                "status_code": resp.status_code,
                "latency_ms": _latency(),
                "error": None if ok else f"Provider '{p.provider_key}' tidak ada di katalog serve.",
            }
        except Exception as e:
            store.record_ai_provider_test(db, p, False)
            return {
                "ok": False,
                "status_code": None,
                "latency_ms": _latency(),
                "error": _mask_secrets(str(e))[:200] or "Serve opencode tidak terjangkau.",
            }

    headers: dict = {}
    if p.auth_env_var:
        value = os.getenv(p.auth_env_var)
        if not value:
            store.record_ai_provider_test(db, p, False)
            return {
                "ok": False,
                "status_code": None,
                "latency_ms": None,
                "error": f"Env var {p.auth_env_var} tidak terisi di environment backend.",
            }
        headers["Authorization"] = f"Bearer {value}"
    try:
        async with _httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(p.base_url.rstrip("/") + "/models", headers=headers)
        ok = resp.status_code < 400
        store.record_ai_provider_test(db, p, ok)
        return {
            "ok": ok,
            "status_code": resp.status_code,
            "latency_ms": _latency(),
            "error": None if ok else f"Server menjawab HTTP {resp.status_code}.",
        }
    except Exception as e:
        store.record_ai_provider_test(db, p, False)
        return {
            "ok": False,
            "status_code": None,
            "latency_ms": _latency(),
            "error": _mask_secrets(str(e))[:200] or "Koneksi gagal.",
        }


# ── Publish: database -> config opencode ──
# Admin memilih provider default + modelnya lewat CRUD di atas; publish
# menerbitkan SEMUA provider aktif ke blok "providers" kedua config
# opencode-core dan memasang model pilihan itu ke agent publik. DB =
# sumber kebenaran, file config = artefak (komentar di dalam blok
# providers akan tergantikan hasil generate; catatan pindah ke kolom
# notes di DB). Instance serve harus direstart sesudah publish.

import json as _json
from pathlib import Path as _Path

_PROJECT_ROOT = _Path(__file__).resolve().parents[1]
_CONFIG_FILES = {
    "public": _PROJECT_ROOT / "opencode-core" / "public" / "opencode.jsonc",
    "admin": _PROJECT_ROOT / "opencode-core" / "admin" / "opencode.jsonc",
}


def _jsonc_block_span(text: str, key: str, start: int = 0) -> tuple[int, int]:
    """(indeks '{', indeks '}') dari blok objek untuk "key": { ... }.

    Scanner sadar string & komentar // — URL dan teks bebas tidak
    mengacaukan hitungan kurung. ValueError bila tidak ketemu.
    """
    idx = text.index(f'"{key}"', start)
    brace = text.index("{", idx)
    depth = 0
    i = brace
    in_str = False
    in_comment = False
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_comment:
            if ch == "\n":
                in_comment = False
        elif in_str:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "/" and nxt == "/":
            in_comment = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return brace, i
        i += 1
    raise ValueError(f"blok '{key}' tidak tertutup")


def _providers_from_db(rows) -> dict:
    providers: dict = {}
    for p in rows:
        if p.adapter != "openai-compatible" or p.status != "active":
            continue
        entry: dict = {"name": p.name}
        if p.auth_env_var:
            entry["env"] = [p.auth_env_var]
        if p.package:
            entry["package"] = p.package
        if p.base_url:
            entry["settings"] = {"baseURL": p.base_url}
        entry["models"] = {
            m["id"]: {
                "name": m.get("name") or m["id"],
                "limit": {"context": 128000, "output": 8192},
            }
            for m in (p.models or [])
        }
        providers[p.provider_key] = entry
    # Urutan stabil menurut provider_key — hasil generate deterministik,
    # diff config di git tetap bersih antar-publish.
    return dict(sorted(providers.items()))


def _render_providers_block(providers: dict) -> str:
    body = _json.dumps(providers, indent=2, ensure_ascii=False)
    lines = body.splitlines()
    return "\n".join([lines[0]] + ["  " + line for line in lines[1:]])


def _set_model_value(text: str, model_ref: str, agent: str | None) -> str:
    """Ganti nilai "model" pertama: di level atas (agent=None) atau di
    dalam blok agents.<agent>."""
    if agent is None:
        bound = text.index('"providers"')
        head, tail = text[:bound], text[bound:]
        head = _re.sub(r'"model"\s*:\s*"[^"]*"', f'"model": "{model_ref}"', head, count=1)
        return head + tail
    a0, a1 = _jsonc_block_span(text, "agents")
    g0, g1 = _jsonc_block_span(text, agent, a0)
    block = _re.sub(
        r'"model"\s*:\s*"[^"]*"', f'"model": "{model_ref}"', text[g0 : g1 + 1], count=1
    )
    return text[:g0] + block + text[g1 + 1 :]


@router.post("/ai-providers/publish")
async def publish_ai_providers(
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    """Terbitkan registry DB ke config opencode-core + salinan live.

    Blok providers kedua config ditulis ulang dari DB; model agent
    publik (dan model level-atas config publik) dipasang ke
    provider/model default pilihan admin. Config admin hanya menerima
    blok providers — model agent admin tetap pilihannya sendiri.
    """
    rows = store.list_ai_providers(db)
    default = next((p for p in rows if p.is_default and p.status == "active"), None)
    if default is None or not default.default_model:
        raise HTTPException(
            status_code=422,
            detail="Belum ada provider default aktif dengan default_model — pilih dulu lewat CRUD.",
        )
    model_ref = f"{default.provider_key}/{default.default_model}"
    providers = _providers_from_db(rows)
    block = _render_providers_block(providers)

    targets: list[tuple[str, _Path]] = [
        ("public", _CONFIG_FILES["public"]),
        ("admin", _CONFIG_FILES["admin"]),
    ]
    live_dir = os.getenv("OPENCODE_PUBLIC_DIR", "").strip()
    if live_dir:
        targets.append(("public-live", _Path(live_dir).expanduser() / "opencode.jsonc"))

    written: list[str] = []
    skipped: list[str] = []
    for label, path in targets:
        if not path.exists():
            skipped.append(f"{label}: {path} tidak ada")
            continue
        try:
            text = path.read_text(encoding="utf-8")
            s, e = _jsonc_block_span(text, "providers")
            text = text[:s] + block + text[e + 1 :]
            if label != "admin":
                text = _set_model_value(text, model_ref, agent=None)
                text = _set_model_value(text, model_ref, agent="public")
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Gagal memproses {path}: {_mask_secrets(str(exc))[:200]}",
            )
        if label == "public-live":
            path.with_suffix(".jsonc.bak").write_text(
                path.read_text(encoding="utf-8"), encoding="utf-8"
            )
        path.write_text(text, encoding="utf-8")
        written.append(str(path))
    return {
        "ok": True,
        "model": model_ref,
        "provider_default": default.provider_key,
        "providers_diterbitkan": sorted(providers),
        "files": written,
        "dilewati": skipped,
        "catatan": (
            "Instance opencode harus direstart agar config baru terbaca "
            "(laptop: ~/start-opencode-public.sh; VPS: systemctl restart opencode-public). "
            "Sesi publik yang sudah berjalan ikut pindah model pada pesan berikutnya lewat router."
        ),
    }


# ── Modul AI Agent: daftar sesi opencode (metadata + audit) ──
# Baris di tabel ai_sessions ditulis router opencode tiap giliran
# selesai; di sini hanya dibaca/dihapus. Transkrip lengkap tetap
# diambil dari serve lewat GET /v1/opencode/sessions/{id}/messages
# (role admin di sana juga tergerbang token yang sama).


@router.get("/ai-sessions")
async def list_ai_sessions(
    role: str | None = None,
    q: str | None = None,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    rows = store.list_ai_sessions(db, role=role, q=q)
    return {"items": [store.ai_session_to_dict(s) for s in rows], "total": len(rows)}


@router.get("/ai-sessions/{row_id}")
async def get_ai_session(
    row_id: int,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    s = store.get_ai_session(db, row_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Sesi tidak ditemukan.")
    return store.ai_session_to_dict(s)


@router.delete("/ai-sessions/{row_id}")
async def delete_ai_session(
    row_id: int,
    admin: str = Depends(require_admin),
    db: Session = Depends(store.dashboard_session),
):
    s = store.get_ai_session(db, row_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Sesi tidak ditemukan.")
    store.soft_delete_ai_session(db, s)
    return {"ok": True, "deleted_id": row_id}
