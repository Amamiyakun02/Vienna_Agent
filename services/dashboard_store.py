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
