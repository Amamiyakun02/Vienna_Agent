"""Lapisan data System Dashboard — PostgreSQL via SQLAlchemy.

Spec vault "System Dashboard" §2-3: data UTAMA dashboard disimpan di
PostgreSQL (keputusan user 5 Nov 2026); MongoDB hanya memory AI agent.
Koneksi dari env DATABASE_URL (format postgresql+psycopg2://...).

Modul pertama: registry `projects` (di-seed dari [[Peta Project]]).
Tabel dibuat via create_all saat startup (init_dashboard_db dipanggil
dari lifespan main.py, ter-guarded — backend tetap hidup bila DB mati).
"""

import os
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

_engine = None
_Session = None


def _get_engine():
    global _engine, _Session
    if _engine is None:
        url = os.getenv("DATABASE_URL", "")
        if not url:
            return None
        _engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 4},
        )
        _Session = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def dashboard_session():
    """FastAPI dependency: sesi DB dashboard, 503 bila belum diatur."""
    from fastapi import HTTPException

    if _get_engine() is None:
        raise HTTPException(
            status_code=503,
            detail="Database dashboard belum dikonfigurasi (DATABASE_URL kosong).",
        )
    db = _Session()
    try:
        yield db
    finally:
        db.close()


def _now():
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    tech_stack = Column(String(300), nullable=True)
    local_path = Column(String(500), nullable=True)
    repo_url = Column(String(500), nullable=True)
    status = Column(String(20), nullable=False, default="active")
    links = Column(JSON, nullable=False, default=dict)
    demo_video_url = Column(String(500), nullable=True)
    demo_video_thumbnail = Column(String(500), nullable=True)
    # Top Project: pin unggulan yang tampil di section Projects portofolio
    # (keputusan user 6 Nov 2026 — maks FEATURED_LIMIT, bisa diurutkan).
    is_featured = Column(Boolean, nullable=False, default=False)
    featured_order = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


PROJECT_STATUSES = ("idea", "active", "maintenance", "archived")

# Seed awal dari [[Peta Project]] (spec §3) — hanya saat tabel kosong.
_SEED = [
    {
        "name": "WebPortofolio",
        "description": "Website portofolio utama — frontend Next.js (design Wonder) + tool pages yang memanggil Vienna Agent.",
        "tech_stack": "Next.js + React + TypeScript",
        "local_path": "~/Desktop/wonder-background",
        "status": "active",
    },
    {
        "name": "Vienna Agent",
        "description": "Backend FastAPI asisten AI (backend utama): chat multi-engine, RAG, tool pages, dan System Dashboard.",
        "tech_stack": "Python + FastAPI",
        "local_path": "~/Desktop/Vienna_Agent",
        "status": "active",
    },
    {
        "name": "MCPServer",
        "description": "MCP tools server untuk agent (Agent MCP, 31 tools terverifikasi).",
        "tech_stack": "Python + FastMCP",
        "local_path": "~/Desktop/MCPServer",
        "repo_url": "https://github.com/Amamiyakun02/MCPServer",
        "status": "active",
    },
    {
        "name": "RadjaWeddingApps",
        "description": "Fullstack wedding app (frontend + backend).",
        "tech_stack": "FastAPI + Vite/React",
        "local_path": "~/Desktop/RadjaWeddingApps",
        "status": "maintenance",
    },
    {
        "name": "Qdrant Server",
        "description": "Infra vector database shared (cluster Qdrant Cloud 'ViennaAgent') untuk RAG beberapa project.",
        "tech_stack": "Qdrant Cloud",
        "local_path": None,
        "status": "maintenance",
    },
    {
        "name": "AI Dev Company",
        "description": "Platform multi-agent OpenCode + orchestrator (control plane, worker ephemeral).",
        "tech_stack": "OpenCode + FastAPI",
        "local_path": None,
        "status": "idea",
    },
]


def project_to_dict(p: Project) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "tech_stack": p.tech_stack,
        "local_path": p.local_path,
        "repo_url": p.repo_url,
        "status": p.status,
        "links": p.links or {},
        "demo_video_url": p.demo_video_url,
        "demo_video_thumbnail": p.demo_video_thumbnail,
        "is_featured": bool(p.is_featured),
        "featured_order": p.featured_order,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


# Status yang boleh tampil ke publik (idea = belum jadi, archived = pensiun).
PUBLIC_STATUSES = ("active", "maintenance")
FEATURED_LIMIT = 4


def project_to_public_dict(p: Project) -> dict:
    """Bentuk aman-publik: TANPA local_path dan data internal lain."""
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "tech_stack": p.tech_stack,
        "repo_url": p.repo_url,
        "status": p.status,
        "links": p.links or {},
        "demo_video_url": p.demo_video_url,
        "demo_video_thumbnail": p.demo_video_thumbnail,
        "is_featured": bool(p.is_featured),
        "featured_order": p.featured_order,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def featured_projects(db, public_only: bool = False):
    """Project ter-pin, terurut featured_order (null terakhir), lalu nama."""
    query = db.query(Project).filter(
        Project.deleted_at.is_(None), Project.is_featured.is_(True)
    )
    if public_only:
        query = query.filter(Project.status.in_(PUBLIC_STATUSES))
    return query.order_by(
        Project.featured_order.is_(None),
        Project.featured_order.asc(),
        Project.name.asc(),
    ).all()


def public_projects(db):
    """Semua project publik: unggulan dulu (sesuai urutan pin), lalu terbaru."""
    rows = (
        db.query(Project)
        .filter(
            Project.deleted_at.is_(None),
            Project.status.in_(PUBLIC_STATUSES),
        )
        .all()
    )
    rows.sort(
        key=lambda p: (
            0 if p.is_featured else 1,
            p.featured_order if p.featured_order is not None else 999,
            -(p.updated_at.timestamp() if p.updated_at else 0),
        )
    )
    return rows


def _renumber_featured(db):
    """Padatkan featured_order jadi 1..n mengikuti urutan saat ini."""
    for i, p in enumerate(featured_projects(db), start=1):
        p.featured_order = i


def set_featured(db, p: Project, on: bool) -> Project:
    if on and not p.is_featured:
        if len(featured_projects(db)) >= FEATURED_LIMIT:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=422,
                detail=f"Maksimal {FEATURED_LIMIT} Top Project. Lepas pin salah satu dulu.",
            )
        p.is_featured = True
        p.featured_order = None  # di-renumber di bawah (masuk urutan akhir)
        db.commit()
        _renumber_featured(db)
    elif not on and p.is_featured:
        p.is_featured = False
        p.featured_order = None
        db.commit()
        _renumber_featured(db)
    db.commit()
    db.refresh(p)
    return p


def reorder_featured(db, ids: list[int]):
    """Tetapkan urutan pin persis mengikuti daftar id yang diberikan."""
    current = {p.id: p for p in featured_projects(db)}
    if set(ids) != set(current.keys()):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=422,
            detail="Daftar id harus persis berisi semua Top Project saat ini.",
        )
    for i, pid in enumerate(ids, start=1):
        current[pid].featured_order = i
    db.commit()
    return featured_projects(db)


def init_dashboard_db() -> bool:
    """Buat tabel dashboard + seed projects bila kosong. Aman dipanggil
    berulang; False bila DATABASE_URL belum diatur / DB tak terjangkau."""
    if _get_engine() is None:
        print("[DASHBOARD] DATABASE_URL belum diatur — modul data dashboard nonaktif.")
        return False
    Base.metadata.create_all(_engine)
    # Migrasi ringan untuk tabel lama (create_all tidak menambah kolom):
    # pastikan kolom featured ada.
    cols = {c["name"] for c in inspect(_engine).get_columns("projects")}
    with _engine.begin() as conn:
        if "is_featured" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE projects ADD COLUMN is_featured "
                    "BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
        if "featured_order" not in cols:
            conn.execute(
                text("ALTER TABLE projects ADD COLUMN featured_order INTEGER")
            )
    db = _Session()
    try:
        if db.query(Project).count() == 0:
            for row in _SEED:
                db.add(Project(links={}, **row))
            db.commit()
            print(f"[DASHBOARD] Tabel projects dibuat + seed {len(_SEED)} project dari Peta Project.")
        else:
            print("[DASHBOARD] Tabel dashboard siap (projects sudah terisi).")
        if db.query(Integration).count() == 0:
            for row in _INTEGRATION_SEED:
                db.add(Integration(headers={}, **row))
            db.commit()
            print(f"[DASHBOARD] Tabel integrations dibuat + seed {len(_INTEGRATION_SEED)} integrasi infra.")
    finally:
        db.close()
    return True


def list_projects(db, status: str | None = None, q: str | None = None):
    query = db.query(Project).filter(Project.deleted_at.is_(None))
    if status:
        query = query.filter(Project.status == status)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            (Project.name.ilike(like))
            | (Project.description.ilike(like))
            | (Project.tech_stack.ilike(like))
        )
    return query.order_by(Project.updated_at.desc()).all()


def get_project(db, project_id: int):
    return (
        db.query(Project)
        .filter(Project.id == project_id, Project.deleted_at.is_(None))
        .first()
    )


def create_project(db, data: dict) -> Project:
    p = Project(**data)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def update_project(db, p: Project, data: dict) -> Project:
    for key, value in data.items():
        setattr(p, key, value)
    db.commit()
    db.refresh(p)
    return p


def soft_delete_project(db, p: Project) -> None:
    p.deleted_at = _now()
    was_featured = bool(p.is_featured)
    p.is_featured = False
    p.featured_order = None
    db.commit()
    if was_featured:
        _renumber_featured(db)
        db.commit()


# ── Modul 3: Integrations (spec §5; diskusi skema 8 Nov 2026) ──
# v1: kredensial HANYA referensi env var (auth_env_var = NAMA variabel;
# nilai dibaca backend dari environment saat test koneksi, tidak pernah
# disimpan di DB dan tidak pernah dikembalikan API). Kolom secret
# terenkripsi Fernet DITUNDA sesuai keputusan diskusi 8 Nov 2026.


class Integration(Base):
    __tablename__ = "integrations"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    kind = Column(String(20), nullable=False, default="rest")
    base_url = Column(String(500), nullable=False)
    auth_type = Column(String(20), nullable=False, default="none")
    auth_env_var = Column(String(200), nullable=True)
    headers = Column(JSON, nullable=False, default=dict)
    notes = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="active")
    last_tested_at = Column(DateTime(timezone=True), nullable=True)
    last_test_ok = Column(Boolean, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


INTEGRATION_KINDS = ("rest", "webhook", "mcp", "other")
INTEGRATION_AUTH_TYPES = ("none", "api_key", "bearer", "basic", "oauth")
INTEGRATION_STATUSES = ("active", "inactive")

# Seed awal: layanan infra milik user sendiri (dari Peta Project) —
# hanya saat tabel kosong.
_INTEGRATION_SEED = [
    {
        "name": "Vienna Agent (backend ini)",
        "kind": "rest",
        "base_url": "http://127.0.0.1:8000/",
        "auth_type": "none",
        "notes": "Health check backend utama.",
    },
    {
        "name": "MCPServer lokal",
        "kind": "mcp",
        "base_url": "http://127.0.0.1:8001/",
        "auth_type": "none",
        "notes": "Container agentmcp-server di laptop.",
    },
    {
        "name": "Qdrant lokal",
        "kind": "rest",
        "base_url": "http://127.0.0.1:6333/",
        "auth_type": "none",
        "notes": "Container qdrant_server di laptop (vector DB RAG).",
    },
]


def integration_to_dict(i: Integration) -> dict:
    """Bentuk API: TANPA nilai secret apa pun — hanya nama env var dan
    status keberadaannya di environment backend (boolean)."""
    env_name = i.auth_env_var or None
    return {
        "id": i.id,
        "name": i.name,
        "kind": i.kind,
        "base_url": i.base_url,
        "auth_type": i.auth_type,
        "auth_env_var": env_name,
        "auth_env_present": bool(env_name and os.getenv(env_name)),
        "headers": i.headers or {},
        "notes": i.notes,
        "status": i.status,
        "last_tested_at": i.last_tested_at.isoformat() if i.last_tested_at else None,
        "last_test_ok": i.last_test_ok,
        "created_at": i.created_at.isoformat() if i.created_at else None,
        "updated_at": i.updated_at.isoformat() if i.updated_at else None,
    }


def list_integrations(db, status: str | None = None, q: str | None = None):
    query = db.query(Integration).filter(Integration.deleted_at.is_(None))
    if status:
        query = query.filter(Integration.status == status)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            (Integration.name.ilike(like))
            | (Integration.base_url.ilike(like))
            | (Integration.notes.ilike(like))
        )
    return query.order_by(Integration.updated_at.desc()).all()


def get_integration(db, integration_id: int):
    return (
        db.query(Integration)
        .filter(Integration.id == integration_id, Integration.deleted_at.is_(None))
        .first()
    )


def create_integration(db, data: dict) -> Integration:
    i = Integration(**data)
    db.add(i)
    db.commit()
    db.refresh(i)
    return i


def update_integration(db, i: Integration, data: dict) -> Integration:
    for key, value in data.items():
        setattr(i, key, value)
    db.commit()
    db.refresh(i)
    return i


def soft_delete_integration(db, i: Integration) -> None:
    i.deleted_at = _now()
    db.commit()


def record_integration_test(db, i: Integration, ok: bool) -> None:
    i.last_tested_at = _now()
    i.last_test_ok = ok
    db.commit()


# ── Modul 5: Users (spec §7; diskusi skema 8 Nov 2026) ──
# Registry user per project (konsumen pertama: ViennaAssistantMobile).
# Password SELALU bcrypt — plaintext tidak pernah disimpan, hash tidak
# pernah dikembalikan API. Auth terpusat untuk aplikasi konsumen adalah
# pekerjaan lanjutan (catatan v1 spec); verify_user_credentials adalah
# fondasinya.

import bcrypt as _bcrypt


class AppUser(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    project = Column(String(100), nullable=False)
    username = Column(String(50), nullable=False)
    email = Column(String(200), nullable=False)
    password_hash = Column(String(200), nullable=False)
    role = Column(String(20), nullable=False, default="user")
    status = Column(String(20), nullable=False, default="active")
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


USER_ROLES = ("admin", "user")
USER_STATUSES = ("active", "suspended")


def hash_password(plain: str) -> str:
    return _bcrypt.hashpw(plain.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def user_to_dict(u: AppUser) -> dict:
    """Bentuk API: TANPA password_hash — tidak pernah, dalam keadaan
    apa pun."""
    return {
        "id": u.id,
        "project": u.project,
        "username": u.username,
        "email": u.email,
        "role": u.role,
        "status": u.status,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "updated_at": u.updated_at.isoformat() if u.updated_at else None,
    }


def list_users(db, project: str | None = None, status: str | None = None, q: str | None = None):
    query = db.query(AppUser).filter(AppUser.deleted_at.is_(None))
    if project:
        query = query.filter(AppUser.project == project)
    if status:
        query = query.filter(AppUser.status == status)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            (AppUser.username.ilike(like)) | (AppUser.email.ilike(like))
        )
    return query.order_by(AppUser.updated_at.desc()).all()


def get_user(db, user_id: int):
    return (
        db.query(AppUser)
        .filter(AppUser.id == user_id, AppUser.deleted_at.is_(None))
        .first()
    )


def find_user(db, project: str, username: str):
    return (
        db.query(AppUser)
        .filter(
            AppUser.project == project,
            AppUser.username == username,
            AppUser.deleted_at.is_(None),
        )
        .first()
    )


def create_user(db, data: dict, password_plain: str) -> AppUser:
    u = AppUser(password_hash=hash_password(password_plain), **data)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def update_user(db, u: AppUser, data: dict) -> AppUser:
    for key, value in data.items():
        setattr(u, key, value)
    db.commit()
    db.refresh(u)
    return u


def reset_user_password(db, u: AppUser, password_plain: str) -> AppUser:
    u.password_hash = hash_password(password_plain)
    db.commit()
    db.refresh(u)
    return u


def soft_delete_user(db, u: AppUser) -> None:
    u.deleted_at = _now()
    db.commit()


def verify_user_credentials(db, project: str, username: str, password: str) -> bool:
    """Fondasi auth aplikasi konsumen: True hanya bila user ada,
    berstatus active, dan password cocok. User suspended/gagal =
    False."""
    u = find_user(db, project, username)
    if u is None or u.status != "active":
        return False
    return verify_password(password, u.password_hash)


# ── Modul 4: Contents (spec §6; diskusi skema 8 Nov 2026) ──
# Artikel markdown untuk portofolio: type project/blog/paper/research.
# Admin mengelola semua status; publik HANYA membaca yang published
# (endpoint publik di router terpisah, tanpa token). Slug unik di
# antara baris yang tidak terhapus — dijaga level aplikasi (-> 409).


class Content(Base):
    __tablename__ = "contents"

    id = Column(Integer, primary_key=True)
    type = Column(String(20), nullable=False, default="blog")
    title = Column(String(200), nullable=False)
    slug = Column(String(220), nullable=False)
    summary = Column(Text, nullable=True)
    body = Column(Text, nullable=True)
    tags = Column(JSON, nullable=False, default=list)
    thumbnail = Column(String(500), nullable=True)
    status = Column(String(20), nullable=False, default="draft")
    published_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


CONTENT_TYPES = ("project", "blog", "paper", "research")
CONTENT_STATUSES = ("draft", "published")


def content_to_dict(c: Content) -> dict:
    return {
        "id": c.id,
        "type": c.type,
        "title": c.title,
        "slug": c.slug,
        "summary": c.summary,
        "body": c.body,
        "tags": c.tags or [],
        "thumbnail": c.thumbnail,
        "status": c.status,
        "published_at": c.published_at.isoformat() if c.published_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def content_to_public_dict(c: Content) -> dict:
    return {
        "id": c.id,
        "type": c.type,
        "title": c.title,
        "slug": c.slug,
        "summary": c.summary,
        "body": c.body,
        "tags": c.tags or [],
        "thumbnail": c.thumbnail,
        "published_at": c.published_at.isoformat() if c.published_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def list_contents(db, type: str | None = None, status: str | None = None, q: str | None = None):
    query = db.query(Content).filter(Content.deleted_at.is_(None))
    if type:
        query = query.filter(Content.type == type)
    if status:
        query = query.filter(Content.status == status)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            (Content.title.ilike(like))
            | (Content.summary.ilike(like))
            | (Content.slug.ilike(like))
        )
    return query.order_by(Content.updated_at.desc()).all()


def get_content(db, content_id: int):
    return (
        db.query(Content)
        .filter(Content.id == content_id, Content.deleted_at.is_(None))
        .first()
    )


def find_content_by_slug(db, slug: str):
    return (
        db.query(Content)
        .filter(Content.slug == slug, Content.deleted_at.is_(None))
        .first()
    )


def create_content(db, data: dict) -> Content:
    c = Content(**data)
    if c.status == "published" and c.published_at is None:
        c.published_at = _now()
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def update_content(db, c: Content, data: dict) -> Content:
    was_published = c.status == "published"
    for key, value in data.items():
        setattr(c, key, value)
    if c.status == "published" and not was_published and c.published_at is None:
        c.published_at = _now()
    db.commit()
    db.refresh(c)
    return c


def soft_delete_content(db, c: Content) -> None:
    c.deleted_at = _now()
    db.commit()


def list_published_contents(db, type: str | None = None):
    query = db.query(Content).filter(
        Content.deleted_at.is_(None), Content.status == "published"
    )
    if type:
        query = query.filter(Content.type == type)
    return query.order_by(Content.published_at.desc()).all()


def get_published_content_by_slug(db, slug: str):
    return (
        db.query(Content)
        .filter(
            Content.slug == slug,
            Content.status == "published",
            Content.deleted_at.is_(None),
        )
        .first()
    )
