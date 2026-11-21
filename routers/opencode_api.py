"""Router OpenCode — opencode serve sebagai core agentic AI backend.

Engine lama (Gemini/Mercury/DeepSeek lewat process_chat di main.py) TIDAK
diubah dan TIDAK dihapus; router ini lapisan pengendali tipis di depan
`opencode serve` (harness agent yang sudah jadi: loop, tool calling,
session, permission). Backend hanya meneruskan prompt dan mengambil hasil.

Kontrak opencode serve v2 (terverifikasi live di laptop, v2.2.2):
- Semua endpoint di bawah /api/* dan wajib Basic Auth (user "opencode",
  password dari env OPENCODE_SERVER_PASSWORD — sama dengan password yang
  dipakai start-opencode-serve.sh dari .env backend).
- POST /api/session {title?, agent?, model?} -> {data: {id, ...}}
- POST /api/session/{id}/prompt {text} -> prompt masuk inbox, agent jalan async
- POST /api/experimental/session/{id}/wait -> blokir sampai sesi idle
- GET  /api/session/{id}/message -> {data: [pesan...]} (assistant punya
  content[] berisi part {type: "text"|"tool"|...}; pesan type "idle"
  membawa outcome succeeded/failed + error terstruktur)

POST /chat menunggu jawaban final (prompt -> wait -> ambil pesan).
POST /chat/stream meneruskan event SSE serve (GET /api/event) ke klien:
event V2 berbentuk {type, data:{sessionID, ...}} — yang diteruskan
"session.text.delta" (data.delta), "session.tool.input.started"
(data.name), ditutup "session.execution.succeeded"/"failed". Format
keluaran meniru SSE process_chat: {"text": ...} per potongan,
{"meta": {...}} untuk penanda, {"done": true, ...} penutup,
{"error": ...} bila gagal.
"""

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, File, Header, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/v1/opencode", tags=["OpenCode Agent"])

# Dua instance serve TERISOLASI sebagai core (paket opencode-core/):
# publik = agent anonim pengunjung (terkunci, sandbox, Mercury);
# admin = agent dashboard (nyaris penuh, Lyber). Serve PRIBADI user
# tidak lagi dipakai backend. Di VPS admin memakai port 4096 (default
# OPENCODE_BASE_URL); di laptop port itu ditempati serve pribadi,
# jadi admin laptop diarahkan lewat env OPENCODE_ADMIN_BASE_URL.
BASE_URL = os.getenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096").rstrip("/")
PUBLIC_BASE_URL = os.getenv("OPENCODE_PUBLIC_BASE_URL", "http://127.0.0.1:4097").rstrip("/")
ADMIN_BASE_URL = os.getenv("OPENCODE_ADMIN_BASE_URL", "").strip().rstrip("/") or BASE_URL
PASSWORD = os.getenv("OPENCODE_SERVER_PASSWORD", "")
DEFAULT_AGENT = os.getenv("OPENCODE_AGENT", "vienna")
PUBLIC_AGENT = os.getenv("OPENCODE_PUBLIC_AGENT", "public")
ADMIN_AGENT = os.getenv("OPENCODE_ADMIN_AGENT", "admin")
# Model default sesi chat lewat API ini: Mercury — model agent publik
# sesuai arsitektur (agent pribadi memakai model gratis opencode yang
# kuota bersamanya bisa habis sewaktu-waktu). Ganti lewat env OPENCODE_MODEL.
DEFAULT_MODEL = os.getenv("OPENCODE_MODEL", "").strip() or "inception/mercury-3"  # format "providerID/modelID"
ADMIN_MODEL = os.getenv("OPENCODE_ADMIN_MODEL", "").strip() or "lyber/kimi-k31-pro-abliterated"
# Limit khusus role publik (anonim) — menahan pemborosan token saat
# diekspos ke internet. Acuan ukuran: satu giliran tanya-jawab profil
# normal ≈ 10,5 ribu token (input ±9 ribu: system + instruksi + katalog
# tool MCP ikut terkirim tiap giliran) dan ±$0,0013. Semua bisa diatur
# via env; 0 = nonaktif.
PUBLIC_MAX_PROMPT_CHARS = int(os.getenv("OPENCODE_PUBLIC_MAX_PROMPT_CHARS", "4000"))
PUBLIC_MAX_TURNS = int(os.getenv("OPENCODE_PUBLIC_MAX_TURNS", "30"))
PUBLIC_MAX_SESSION_TOKENS = int(os.getenv("OPENCODE_PUBLIC_MAX_SESSION_TOKENS", "250000"))
PUBLIC_MAX_TURN_TOKENS = int(os.getenv("OPENCODE_PUBLIC_MAX_TURN_TOKENS", "80000"))
TIMEOUT_S = float(os.getenv("OPENCODE_TIMEOUT_S", "600"))
# Tenggat role admin jauh lebih longgar: pekerjaan berkas yang sah bisa
# melewati 10 menit, dan tenggat stream juga meng-abort giliran di
# serve — jawaban yang ter-abort tidak pernah masuk riwayat sesi,
# sehingga pemulihan riwayat oleh frontend pun tidak bisa menolong.
TIMEOUT_S_ADMIN = float(os.getenv("OPENCODE_TIMEOUT_S_ADMIN", "1800"))


def _deadline_s(role: str) -> float:
    """Tenggat total satu giliran per role (detik)."""
    return TIMEOUT_S_ADMIN if role == "admin" else TIMEOUT_S
AUTH = ("opencode", PASSWORD) if PASSWORD else None


def _db_model_default() -> str | None:
    """Model bawaan pilihan admin di dashboard (tabel ai_providers).

    Registry dashboard adalah sumber kebenaran pilihan model publik;
    env DEFAULT_MODEL hanya cadangan bila DB belum diatur / kosong.
    """
    try:
        from services import dashboard_store as _store

        return _store.default_ai_model_ref()
    except Exception:
        return None


def _db_admin_model_default() -> str | None:
    """Model bawaan agent admin pilihan dashboard (is_default_admin)."""
    try:
        from services import dashboard_store as _store

        return _store.default_admin_ai_model_ref()
    except Exception:
        return None


def _target(role: str | None) -> tuple[str, str, str, str]:
    """(role, base_url, agent_default, model_default) untuk satu role.

    Default SELALU publik (hak paling kecil); admin hanya bila diminta
    persis "admin" — dan role admin digerbang _admin_gate di bawah.
    Model bawaan kedua role dibaca dari registry dashboard lebih dulu
    (pilihan admin), env hanya cadangan.
    """
    if (role or "").strip().lower() == "admin":
        return "admin", ADMIN_BASE_URL, ADMIN_AGENT, _db_admin_model_default() or ADMIN_MODEL
    return "public", PUBLIC_BASE_URL, PUBLIC_AGENT, _db_model_default() or DEFAULT_MODEL


# --- Lampiran file hasil kerja agent -------------------------------------
# File yang dibuat/diubah agent di direktori kerjanya dikembalikan ke
# klien di akhir giliran (kunci "files" pada event done / respons chat):
# pengunjung menerima HASILNYA, bukan cuma cerita agent. Jalur file
# ditangkap dari event session.tool.called (input tool write/edit) —
# bukan snapshot folder, supaya file sesi lain yang berjalan bersamaan
# tidak ikut terbawa. Isi dibatasi agar SSE tidak meledak; file biner /
# terlalu besar dikembalikan sebagai metadata saja (content_omitted).
FILE_RETURN_MAX_BYTES = 65536
_FILE_TOOLS = {"write", "edit"}


def _role_root(role: str) -> Path | None:
    """Direktori kerja instance serve untuk satu role, dari sisi backend.

    Publik: OPENCODE_PUBLIC_DIR (di VPS diisi compose, menunjuk bind
    mount folder agent publik di host); cadangan ~/agent-public (letak
    bawaan laptop). Admin: OPENCODE_ADMIN_DIR; cadangan folder
    admin-workspace/ di repo ini (di container VPS = /app/admin-workspace,
    bind mount di compose). None bila foldernya tidak ada — fitur
    lampiran mati diam-diam, chat tetap jalan.
    """
    if role == "admin":
        env = os.getenv("OPENCODE_ADMIN_DIR", "").strip()
        root = Path(env) if env else Path(__file__).resolve().parents[1] / "admin-workspace"
    else:
        env = os.getenv("OPENCODE_PUBLIC_DIR", "").strip()
        root = Path(env) if env else Path.home() / "agent-public"
    return root if root.is_dir() else None


def _collect_files(role: str, raw_paths: list[str]) -> list[dict]:
    """Baca isi file-file yang disentuh agent pada giliran ini.

    Jalur dari tool adalah jalur absolut MESIN HOST serve; backend di
    container melihat folder yang sama lewat bind mount di jalur lain.
    Karena itu, bila jalur apa adanya tidak ditemukan, coba gabungkan
    potongan ekor jalurnya ke root role sampai ketemu — lalu wajibkan
    hasil akhir tetap di dalam root (tidak boleh keluar sandbox).
    """
    root = _role_root(role)
    if root is None:
        return []
    root_real = root.resolve()
    out: list[dict] = []
    seen: set[Path] = set()
    for raw in raw_paths:
        if not raw:
            continue
        p = Path(str(raw))
        candidates = [p] if p.is_absolute() else [root_real / p]
        if p.is_absolute():
            parts = p.parts
            candidates += [root_real.joinpath(*parts[i:]) for i in range(1, min(len(parts), 7))]
        resolved = None
        for cand in candidates:
            try:
                rp = cand.resolve()
            except Exception:
                continue
            if rp.is_file() and root_real in rp.parents:
                resolved = rp
                break
        if resolved is None or resolved in seen:
            continue
        seen.add(resolved)
        try:
            size = resolved.stat().st_size
            data = resolved.read_bytes() if size <= FILE_RETURN_MAX_BYTES else None
        except Exception:
            continue
        entry = {
            "name": resolved.name,
            "path": resolved.relative_to(root_real).as_posix(),
            "size": size,
            "content": None,
        }
        if data is None:
            entry["content_omitted"] = "too_large"
        else:
            try:
                entry["content"] = data.decode("utf-8")
            except UnicodeDecodeError:
                entry["content_omitted"] = "binary"
        out.append(entry)
    return out


# --- Lampiran unggahan user untuk agent admin ----------------------------
# Arah kebalikan files[]: user mengunggah berkas lewat POST
# /chat/upload?role=admin; backend menyimpannya di uploads/<id>/ DI DALAM
# ruang kerja admin, lalu id-nya dikirim kembali sebagai "attachments"
# pada body chat — backend menambahkan blok konteks ke prompt supaya
# agent membaca berkas itu dengan tool file-nya di giliran yang sama.
# HANYA admin: pengunjung anonim tidak boleh menulis apa pun ke disk.
UPLOAD_MAX_FILES = 5
UPLOAD_MAX_FILE_BYTES = 10 * 1024 * 1024
UPLOAD_MAX_TOTAL_BYTES = 25 * 1024 * 1024
_UPLOADS: dict[str, dict] = {}  # id -> {path, name, size, mime}


def _safe_upload_name(name: str | None) -> str:
    """Nama berkas aman untuk disk: basename saja, karakter aman."""
    base = Path((name or "").replace("\\", "/")).name.strip()
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).lstrip(".")
    return base or "file"


def _resolve_upload(uid: str) -> Path | None:
    """Path berkas unggahan dari id-nya; None bila tak dikenal.

    Id wajib berformat uuid (sekaligus penahan path traversal).
    Pemetaan utama in-memory; cadangan memindai folder uploads/<id>/
    di disk supaya id tetap berlaku sesudah backend restart.
    """
    try:
        uuid.UUID(str(uid))
    except (ValueError, AttributeError, TypeError):
        return None
    entry = _UPLOADS.get(str(uid))
    if entry and Path(entry["path"]).is_file():
        return Path(entry["path"])
    root = _role_root("admin")
    if root is None:
        return None
    folder = root / "uploads" / str(uid)
    if folder.is_dir():
        files = sorted(p for p in folder.iterdir() if p.is_file())
        if files:
            return files[0]
    return None


def _attachment_block(role: str, ids: list[str] | None) -> str:
    """Blok konteks lampiran untuk prompt; "" bila tak ada yang sah.

    Id tak dikenal diabaikan diam-diam (chat tidak boleh gagal hanya
    karena satu lampiran basi), tapi tercatat di log backend.
    """
    if role != "admin" or not ids:
        return ""
    root = _role_root("admin")
    if root is None:
        return ""
    lines = []
    for uid in ids:
        path = _resolve_upload(uid)
        if path is None:
            print(f"[OPENCODE] lampiran tak dikenal diabaikan: {uid}", flush=True)
            continue
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except Exception:
            rel = f"uploads/{uid}/{path.name}"
        lines.append(
            f"[Berkas terlampir dari pengguna — tersimpan di ruang kerja: {rel}. "
            "Baca berkas itu bila relevan dengan perintah.]"
        )
    return "\n\n" + "\n".join(lines) if lines else ""


def _sweep_old_uploads() -> None:
    """Hapus folder uploads/ berusia > 7 hari (jalan sekali saat startup).

    Unggahan yatim (diunggah tapi tak pernah terkirim) tidak menumpuk
    selamanya. Best-effort total: tidak boleh menggagalkan startup.
    """
    try:
        root = _role_root("admin")
        if root is None:
            return
        up = root / "uploads"
        if not up.is_dir():
            return
        cutoff = time.time() - 7 * 86400
        for child in up.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass


_sweep_old_uploads()


def _admin_gate(role: str | None, x_admin_token: str | None) -> str | None:
    """Gerbang role admin: wajib token dashboard yang valid.

    Instance admin nyaris setara akses server — ia tidak boleh bisa
    dipanggil siapa pun yang sekadar bisa mencapai backend. Verifikasi
    memakai fungsi yang sama persis dengan gerbang dashboard
    (token HMAC 12 jam, header X-Admin-Token). Mengembalikan username
    admin (untuk audit) bila role admin; None untuk role publik.
    """
    if (role or "").strip().lower() != "admin":
        return None
    from routers.dashboard_api import _verify_token

    user = _verify_token(x_admin_token)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Role admin butuh token dashboard yang valid (header X-Admin-Token).",
        )
    return user


def _record_turn(
    session_id: str,
    role: str,
    agent: str | None,
    model: str | None,
    admin_user: str | None,
    prompt: str,
    usage: dict | None,
    tools: list[str],
) -> None:
    """Catat metadata sesi ke tabel ai_sessions (audit dashboard).

    Best-effort: fungsi store-nya sendiri tidak pernah melempar, dan
    pemanggil membungkusnya lagi — pencatatan tidak boleh mengganggu
    percakapan. usage adalah {cost, tokens} KUMULATIF dari serve.
    """
    try:
        from services import dashboard_store as _store

        usage = usage or {}
        _store.record_ai_session_turn(
            session_id=session_id,
            role=role,
            agent=agent,
            model=model,
            admin_user=admin_user,
            prompt=prompt,
            total_tokens=_tokens_total(usage.get("tokens")),
            total_cost=usage.get("cost") or 0.0,
            tools=tools,
        )
    except Exception:
        pass


def _split_tokens(tokens: dict | None) -> tuple[int, int, int, int]:
    """(input, output+reasoning, cache_read, total) dari objek tokens serve."""
    t = tokens or {}
    cache = t.get("cache") or {}
    return (
        int(t.get("input") or 0),
        int(t.get("output") or 0) + int(t.get("reasoning") or 0),
        int(cache.get("read") or 0),
        _tokens_total(t),
    )


def _usage_delta(start_usage: dict | None, end_usage: dict | None) -> dict:
    """Nilai SATU giliran = selisih usage KUMULATIF serve (akhir - awal).

    Serve hanya melaporkan kumulatif per sesi, jadi nilai giliran
    diturunkan sebagai selisih dari potret awal giliran. Semua komponen
    dijepit >= 0; potret akhir yang hilang berarti giliran dicatat 0.
    """
    s_in, s_out, s_cache, s_total = _split_tokens((start_usage or {}).get("tokens"))
    e_in, e_out, e_cache, e_total = _split_tokens((end_usage or {}).get("tokens"))
    s_cost = float((start_usage or {}).get("cost") or 0.0)
    e_cost = float((end_usage or {}).get("cost") or 0.0)
    return {
        "tokens_input": max(0, e_in - s_in),
        "tokens_output": max(0, e_out - s_out),
        "tokens_cache_read": max(0, e_cache - s_cache),
        "total_tokens": max(0, e_total - s_total),
        "total_cost": max(0.0, e_cost - s_cost),
    }


def _record_ai_turn_only(
    session_id: str,
    role: str,
    agent: str | None,
    model: str | None,
    admin_user: str | None,
    start_usage: dict | None,
    end_usage: dict | None,
    tools: list[str],
    status: str,
    duration_ms: int | None,
    files: list | None = None,
) -> None:
    """Catat SATU giliran ke tabel ai_turns (termasuk giliran error).

    Pasangan _record_turn (yang mengurus ai_sessions kumulatif dan
    hanya berjalan untuk giliran sukses — perilakunya tidak diubah).
    Sama-sama best-effort: tidak pernah melempar. files hanya diisi
    pemanggil pada giliran sukses (snapshot yang sama dengan yang
    dikirim ke klien), agar riwayat sesi bisa menampilkannya lagi.
    """
    try:
        from services import dashboard_store as _store

        _store.record_ai_turn(
            session_id=session_id,
            role=role,
            agent=agent,
            model=model,
            admin_user=admin_user,
            tools_used=tools,
            status=status,
            duration_ms=duration_ms,
            files=files,
            **_usage_delta(start_usage, end_usage),
        )
    except Exception:
        pass


async def _oc(method: str, path: str, timeout: float = 30.0, base: str | None = None, pass_404: bool = False, **kwargs):
    """Satu pintu ke opencode serve; error diterjemahkan ke HTTP backend.

    pass_404: untuk endpoint yang objeknya memang bisa tidak ada
    (sesi yang sudah dihapus), 404 dari serve diteruskan sebagai 404
    backend — bukan ditelan jadi 502 — supaya klien bisa membedakan
    "memang tidak ada" dari "serve bermasalah".
    """
    base_url = base or ADMIN_BASE_URL
    try:
        async with httpx.AsyncClient(base_url=base_url, auth=AUTH, timeout=timeout) as client:
            resp = await client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"opencode serve tidak terjangkau di {base_url} — nyalakan instance opencode-core terkait ({exc})",
        )
    if resp.status_code >= 400:
        if pass_404 and resp.status_code == 404:
            raise HTTPException(status_code=404, detail="Sesi tidak ditemukan di opencode serve.")
        raise HTTPException(
            status_code=502,
            detail=f"opencode serve menjawab {resp.status_code}: {resp.text[:300]}",
        )
    return resp.json() if resp.content else {}


def _model_ref(model: str | None) -> dict | None:
    """'providerID/modelID' -> Model.Ref opencode. None = model default agent."""
    if not model or "/" not in model:
        return None
    provider, model_id = model.split("/", 1)
    return {"id": model_id, "providerID": provider}


class SessionIn(BaseModel):
    title: str | None = None
    agent: str | None = None
    model: str | None = None  # format "providerID/modelID"


class ChatIn(BaseModel):
    prompt: str
    session_id: str | None = None
    agent: str | None = None
    model: str | None = None  # format "providerID/modelID"; sesi lama disamakan modelnya
    hitl: bool = False  # True = klien bisa menjawab form/permission agent (lihat /chat/stream)
    role: str = "public"  # "public" (default, instance anonim) | "admin" (instance dashboard)
    attachments: list[str] | None = None  # id unggahan dari POST /chat/upload (KHUSUS admin)


class FormReplyIn(BaseModel):
    answer: dict  # peta key field -> nilai, sesuai definisi form dari event form.created


class PermissionReplyIn(BaseModel):
    decision: str  # "once" | "always" | "reject" (enum Permission.Reply di serve)
    message: str | None = None


async def _create_session(body: SessionIn, base: str, agent_default: str, model_default: str) -> str:
    payload: dict = {"agent": body.agent or agent_default}
    if body.title:
        payload["title"] = body.title
    ref = _model_ref(body.model or model_default)
    if ref:
        payload["model"] = ref
    data = await _oc("POST", "/api/session", json=payload, base=base)
    return data["data"]["id"]


async def _sync_session_model(session_id: str, model: str | None, base: str, model_default: str) -> None:
    """Samakan model sesi lama dengan model pilihan/default (best-effort).

    Model sesi opencode menempel sejak sesi dibuat; tanpa ini, sesi lama
    yang dibuat dengan model lain (mis. model gratis yang kuotanya habis)
    akan terus gagal walau model default sudah diganti.
    """
    ref = _model_ref(model or model_default)
    if not ref:
        return
    try:
        await _oc("POST", f"/api/session/{session_id}/model", json={"model": ref}, timeout=10.0, base=base)
    except Exception:
        pass


def _tokens_total(tokens: dict | None) -> int:
    """Total token dari objek tokens serve (input+output+reasoning+cache).

    Token cache ikut dihitung penuh: provider memang menagihnya lebih
    murah, tetapi limit ini soal total konsumsi sesi/giliran — dan tanpa
    cache pun angka inilah yang benar-benar diproses model.
    """
    t = tokens or {}
    cache = t.get("cache") or {}
    return (
        int(t.get("input") or 0)
        + int(t.get("output") or 0)
        + int(t.get("reasoning") or 0)
        + int(cache.get("read") or 0)
        + int(cache.get("write") or 0)
    )


async def _ensure_session(body: ChatIn) -> tuple[str, str, str, str, str, dict | None]:
    """(session_id, base, role, agent_default, model_default, session_data).

    Sesi baru dibuat di instance sesuai role. Sesi lama dipakai ulang
    (modelnya disamakan; session_data-nya dikembalikan untuk cek limit);
    sesi lama yang TIDAK dikenal instance tujuan — mis. dibuat di serve
    pribadi sebelum isolasi — diganti sesi baru diam-diam supaya klien
    tidak nyangkut pada id yang yatim.
    """
    role, base, agent_default, model_default = _target(body.role)
    if body.session_id:
        try:
            data = await _oc("GET", f"/api/session/{body.session_id}", timeout=10.0, base=base)
        except HTTPException:
            sid = await _create_session(
                SessionIn(agent=body.agent, model=body.model), base, agent_default, model_default
            )
            return sid, base, role, agent_default, model_default, None
        await _sync_session_model(body.session_id, body.model, base, model_default)
        return body.session_id, base, role, agent_default, model_default, (data or {}).get("data")
    sid = await _create_session(
        SessionIn(agent=body.agent, model=body.model), base, agent_default, model_default
    )
    return sid, base, role, agent_default, model_default, None


async def _public_limit_error(body: ChatIn, base: str, session_id: str, session_data: dict | None) -> str | None:
    """Pesan penolakan bila limit role publik tercapai; None bila aman.

    Hanya dipanggil untuk role publik — admin (user sendiri) tak dibatasi.
    """
    if PUBLIC_MAX_PROMPT_CHARS and len(body.prompt) > PUBLIC_MAX_PROMPT_CHARS:
        return f"Pesan terlalu panjang (maksimal {PUBLIC_MAX_PROMPT_CHARS} karakter). Persingkat lalu coba lagi."
    if session_data is None:
        return None  # sesi baru: belum ada pemakaian
    if PUBLIC_MAX_SESSION_TOKENS and _tokens_total(session_data.get("tokens")) >= PUBLIC_MAX_SESSION_TOKENS:
        return "Sesi ini sudah mencapai batas pemakaian. Silakan mulai sesi baru."
    if PUBLIC_MAX_TURNS:
        data = await _oc("GET", f"/api/session/{session_id}/message", timeout=10.0, base=base)
        turns = sum(1 for m in (data or {}).get("data", []) if m.get("type") == "user")
        if turns >= PUBLIC_MAX_TURNS:
            return "Sesi ini sudah mencapai batas jumlah pesan. Silakan mulai sesi baru."
    return None


@router.get("/status")
async def status():
    """Kesehatan jembatan backend -> KEDUA instance opencode-core."""
    async def probe(base: str) -> dict:
        try:
            data = await _oc("GET", "/api/model/default", timeout=10.0, base=base)
        except HTTPException as exc:
            return {"status": "unreachable", "base_url": base, "detail": exc.detail}
        model = (data or {}).get("data") or {}
        return {"status": "ok", "base_url": base, "default_model": model.get("id")}

    public = await probe(PUBLIC_BASE_URL)
    admin = await probe(ADMIN_BASE_URL)
    return {
        "status": public["status"],
        "base_url": PUBLIC_BASE_URL,
        "default_agent": PUBLIC_AGENT,
        "default_model": public.get("default_model"),
        "auth_configured": bool(PASSWORD),
        "instances": {"public": public, "admin": admin},
    }


@router.get("/models")
async def list_models(role: str = "public", x_admin_token: str | None = Header(default=None)):
    """Katalog model satu instance opencode-core (?role=public|admin),
    dikelompokkan per provider. Model bisa dipilih lewat field "model"
    ("providerID/modelID") pada POST /chat."""
    _admin_gate(role, x_admin_token)
    _, base, _, model_default = _target(role)
    data = await _oc("GET", "/api/model", base=base)
    providers: dict[str, list[str]] = {}
    for m in (data or {}).get("data", []):
        providers.setdefault(m.get("providerID", "?"), []).append(m.get("id"))
    return {
        "status": "success",
        "total": sum(len(v) for v in providers.values()),
        "default_model": model_default or None,
        "providers": {k: sorted(v) for k, v in sorted(providers.items())},
    }


@router.get("/sessions")
async def list_sessions(role: str = "public", x_admin_token: str | None = Header(default=None)):
    _admin_gate(role, x_admin_token)
    _, base, _, _ = _target(role)
    data = await _oc("GET", "/api/session", base=base)
    sessions = [
        {"id": s.get("id"), "title": s.get("title"), "agent": s.get("agent"), "time": s.get("time")}
        for s in (data or {}).get("data", [])
    ]
    return {"status": "success", "sessions": sessions}


@router.post("/sessions")
async def create_session(body: SessionIn, role: str = "public", x_admin_token: str | None = Header(default=None)):
    _admin_gate(role, x_admin_token)
    _, base, agent_default, model_default = _target(role)
    session_id = await _create_session(body, base, agent_default, model_default)
    return {"status": "success", "session_id": session_id}


def _strip_attachment_block(text: str) -> str:
    """Buang blok konteks lampiran (ditambahkan _attachment_block ke
    prompt) dari teks user untuk tampilan riwayat — frontend mencocokkan
    riwayat dengan salinan lokalnya lewat teks asli user."""
    marker = "\n\n[Berkas terlampir dari pengguna"
    idx = text.find(marker)
    return text[:idx].rstrip() if idx >= 0 else text


def _history_shape(raw: list) -> list:
    """Samakan riwayat sesi mentah serve dengan kontrak klien dashboard:
    [{info: {role}, parts: [{type, text}]}] dalam urutan KRONOLOGIS.

    Serve V2 mengembalikan pesan {type, content/text} dengan urutan
    TERBARU dulu, diselingi pesan penanda "idle" dan langkah assistant
    kosong — bentuk mentah itu tidak terbaca parser riwayat frontend
    (info/parts kronologis), sehingga riwayat sesi tampil kosong.
    Di sini: balik urutan, buang penanda & langkah kosong, petakan
    user/assistant, sisakan part teks + reasoning assistant.
    """
    out = []
    for m in reversed(raw or []):
        t = m.get("type")
        if t == "user":
            text = _strip_attachment_block((m.get("text") or "").strip())
            if text:
                out.append({"info": {"role": "user"},
                            "parts": [{"type": "text", "text": text}]})
        elif t == "assistant":
            parts = [
                {"type": p.get("type"), "text": p.get("text")}
                for p in (m.get("content") or [])
                if p.get("type") in ("text", "reasoning") and (p.get("text") or "").strip()
            ]
            if parts:
                out.append({"info": {"role": "assistant"}, "parts": parts})
    return out


def _attach_turn_files(shaped: list, turn_files: list) -> None:
    """Tempelkan files[] audit (ai_turns) ke pesan assistant terakhir
    tiap giliran pada riwayat hasil _history_shape.

    Pemetaan menurut urutan giliran: pesan user membuka giliran baru
    dan baris ai_turns per sesi juga kronologis, jadi giliran ke-n
    di riwayat berpasangan dengan baris ke-n. Satu giliran bisa
    menghasilkan beberapa langkah assistant di serve — files selalu
    menempel di langkah TERAKHIR giliran itu. Giliran tanpa file
    (None/kosong) tidak mendapat field files sama sekali.
    """
    seg = -1
    last_asst = None
    for m in shaped:
        if m["info"]["role"] == "user":
            if last_asst is not None and 0 <= seg < len(turn_files) and turn_files[seg]:
                last_asst["files"] = turn_files[seg]
            seg += 1
            last_asst = None
        else:
            last_asst = m
    if last_asst is not None and 0 <= seg < len(turn_files) and turn_files[seg]:
        last_asst["files"] = turn_files[seg]


@router.get("/sessions/{session_id}/messages")
async def session_messages(session_id: str, role: str = "public", x_admin_token: str | None = Header(default=None)):
    _admin_gate(role, x_admin_token)
    _, base, _, _ = _target(role)
    data = await _oc("GET", f"/api/session/{session_id}/message", base=base, pass_404=True)
    shaped = _history_shape((data or {}).get("data", []))
    # Serve tidak menyimpan file hasil agent — snapshot-nya hidup di
    # audit ai_turns (kolom files). Tempelkan kembali di sini supaya
    # kartu file awet di riwayat; kegagalan baca audit tidak boleh
    # menggagalkan riwayat itu sendiri.
    try:
        from services import dashboard_store as _store

        _attach_turn_files(shaped, _store.list_ai_turn_files(session_id, role))
    except Exception:
        pass
    return {"status": "success", "messages": shaped}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, role: str = "public", x_admin_token: str | None = Header(default=None)):
    """Hapus sesi di opencode serve; riwayat chat-nya ikut hilang.

    Audit Postgres (ai_sessions/ai_turns) SENGAJA tidak disentuh — itu
    log audit, bukan riwayat chat. Jalur serve terverifikasi dari
    OpenAPI-nya: DELETE /api/session/{id} -> 204; sesi tak dikenal ->
    404, yang diteruskan sebagai 404 (bukan 502 seperti _oc, yang
    memang untuk jalur data) supaya UI bisa membedakan "sudah hilang"
    dari "serve bermasalah".
    """
    _admin_gate(role, x_admin_token)
    _, base, _, _ = _target(role)
    try:
        async with httpx.AsyncClient(base_url=base, auth=AUTH, timeout=30.0) as client:
            resp = await client.delete(f"/api/session/{session_id}")
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"opencode serve tidak terjangkau di {base} ({exc})",
        )
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Sesi tidak ditemukan di opencode serve.")
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"opencode serve menjawab {resp.status_code}: {resp.text[:300]}",
        )
    print(f"[OPENCODE] sesi dihapus session={session_id} role={role}", flush=True)
    return {"status": "success"}


@router.post("/chat/upload")
async def chat_upload(
    role: str = "admin",
    files: list[UploadFile] = File(...),
    x_admin_token: str | None = Header(default=None),
):
    """Unggah lampiran untuk agent ADMIN (multipart, field "files").

    Berkas disimpan di uploads/<uuid>/ di dalam ruang kerja admin agar
    tool file agent bisa membacanya pada giliran chat berikutnya (id
    dikirim kembali lewat field "attachments" di body chat). Batas:
    maks 5 berkas, 10 MB per berkas, 25 MB total — pelanggaran = 413
    dan seluruh unggahan dibatalkan. Publik/anonim tidak dilayani.
    """
    if (role or "").strip().lower() != "admin":
        raise HTTPException(status_code=403, detail="Lampiran hanya tersedia untuk role admin.")
    _admin_gate("admin", x_admin_token)
    root = _role_root("admin")
    if root is None:
        raise HTTPException(status_code=503, detail="Ruang kerja admin tidak tersedia di backend.")
    if len(files) > UPLOAD_MAX_FILES:
        raise HTTPException(
            status_code=413,
            detail=f"Terlalu banyak berkas (maksimal {UPLOAD_MAX_FILES} per unggahan).",
        )
    uploads_dir = root / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    out: list[dict] = []
    total = 0
    try:
        for f in files:
            uid = str(uuid.uuid4())
            folder = uploads_dir / uid
            folder.mkdir(parents=True, exist_ok=False)
            created.append(folder)
            dest = folder / _safe_upload_name(f.filename)
            size = 0
            with dest.open("wb") as fh:
                while True:
                    chunk = await f.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    total += len(chunk)
                    if size > UPLOAD_MAX_FILE_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"Berkas '{f.filename}' melebihi batas 10 MB per berkas.",
                        )
                    if total > UPLOAD_MAX_TOTAL_BYTES:
                        raise HTTPException(
                            status_code=413, detail="Total unggahan melebihi batas 25 MB."
                        )
                    fh.write(chunk)
            mime = f.content_type or "application/octet-stream"
            shown = f.filename or dest.name
            _UPLOADS[uid] = {"path": str(dest), "name": shown, "size": size, "mime": mime}
            out.append({"id": uid, "name": shown, "size": size, "mime": mime})
    except HTTPException:
        for folder in created:
            shutil.rmtree(folder, ignore_errors=True)
        raise
    print(f"[OPENCODE] unggah admin: {len(out)} berkas, total {total} byte", flush=True)
    return {"files": out}


@router.post("/chat")
async def chat(body: ChatIn, x_admin_token: str | None = Header(default=None)):
    """Kirim prompt ke agent opencode dan tunggu jawaban finalnya.

    Sesi & riwayat disimpan opencode sendiri (bukan Mongo backend) —
    itulah inti memakai harness-nya. Lanjutkan percakapan dengan
    mengembalikan session_id dari respons ke permintaan berikutnya.
    """
    admin_user = _admin_gate(body.role, x_admin_token)
    session_id, base, role, agent_default, model_default, session_data = await _ensure_session(body)
    _log_chat(body, session_id, role, agent_default, model_default)
    if role == "public":
        limit_error = await _public_limit_error(body, base, session_id, session_data)
        if limit_error:
            print(f"[OPENCODE] limit publik session={session_id}: {limit_error}", flush=True)
            return {"status": "error", "session_id": session_id, "response": "", "error": limit_error}
    prompt_text = body.prompt + _attachment_block(role, body.attachments)
    started = time.monotonic()
    start_usage = {
        "cost": (session_data or {}).get("cost"),
        "tokens": (session_data or {}).get("tokens"),
    }
    await _oc("POST", f"/api/session/{session_id}/prompt", json={"text": prompt_text}, base=base)
    await _oc(
        "POST", f"/api/experimental/session/{session_id}/wait",
        json={}, timeout=_deadline_s(role), base=base,
    )
    data = await _oc("GET", f"/api/session/{session_id}/message", base=base)
    messages = sorted(
        (data or {}).get("data", []),
        key=lambda m: (m.get("time") or {}).get("created", 0),
    )

    # Satu giliran = banyak pesan assistant (satu per langkah agent);
    # yang relevan hanya yang sesudah pesan user terakhir.
    last_user_idx = max((i for i, m in enumerate(messages) if m.get("type") == "user"), default=-1)
    turn = [m for m in messages[last_user_idx + 1:] if m.get("type") == "assistant"]
    tools = [p.get("name") for m in turn for p in (m.get("content") or []) if p.get("type") == "tool"]
    file_paths = []
    for m in turn:
        for p in (m.get("content") or []):
            if p.get("type") == "tool" and p.get("name") in _FILE_TOOLS:
                inp = p.get("input") or (p.get("state") or {}).get("input") or {}
                fp = inp.get("path") or inp.get("filePath")
                if fp:
                    file_paths.append(fp)

    idle = next((m for m in reversed(messages) if m.get("type") == "idle"), None)
    if idle and idle.get("outcome") == "failed":
        error = (idle.get("error") or {}).get("message", "agent gagal tanpa pesan error")
        print(f"[OPENCODE] gagal session={session_id} error={str(error)[:160]}", flush=True)
        _record_ai_turn_only(
            session_id, role, body.agent or agent_default, None, admin_user,
            start_usage, None, tools, "error",
            int((time.monotonic() - started) * 1000),
        )
        return {"status": "error", "session_id": session_id, "response": "", "error": error}

    if not turn:
        _record_ai_turn_only(
            session_id, role, body.agent or agent_default, None, admin_user,
            start_usage, None, tools, "error",
            int((time.monotonic() - started) * 1000),
        )
        return {"status": "error", "session_id": session_id, "response": "", "error": "Tidak ada jawaban assistant."}
    assistant = turn[-1]
    parts = assistant.get("content") or []
    text = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
    model = (assistant.get("model") or {}).get("id")
    print(f"[OPENCODE] selesai session={session_id} model={model} tools={tools}", flush=True)
    usage = None
    try:
        sdata = await _oc("GET", f"/api/session/{session_id}", timeout=10.0, base=base)
        sd = (sdata or {}).get("data") or {}
        usage = {"cost": sd.get("cost"), "tokens": sd.get("tokens")}
    except Exception:
        pass
    _record_turn(
        session_id, role, assistant.get("agent") or (body.agent or agent_default),
        model, admin_user, prompt_text, usage, tools,
    )
    files = _collect_files(role, file_paths)
    _record_ai_turn_only(
        session_id, role, assistant.get("agent") or (body.agent or agent_default),
        model, admin_user, start_usage, usage, tools, "ok",
        int((time.monotonic() - started) * 1000),
        files=files,
    )
    return {
        "status": "success",
        "session_id": session_id,
        "response": text,
        "tools_used": tools,
        "agent": assistant.get("agent"),
        "model": model,
        "files": files,
    }


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _log_chat(body: ChatIn, session_id: str, role: str, agent_default: str, model_default: str) -> None:
    """Satu baris log per permintaan chat — router ini memang sengaja
    tidak mencatat apa pun sebelumnya, makanya request opencode tidak
    terlihat di log backend (beda dengan process_chat yang print [PROMPT])."""
    preview = " ".join(body.prompt.split())[:80]
    print(
        f"[OPENCODE] chat session={session_id} role={role} agent={body.agent or agent_default} "
        f"model={body.model or model_default} hitl={body.hitl} prompt={preview!r}",
        flush=True,
    )


@router.post("/chat/stream")
async def chat_stream(body: ChatIn, x_admin_token: str | None = Header(default=None)):
    """Versi streaming dari /chat: teks tampil per potongan (SSE).

    Berlangganan GET /api/event DULU, baru kirim prompt, supaya tidak
    ada delta awal yang terlewat. Aliran event serve itu global (semua
    sesi) — disaring di sini berdasarkan sessionID sesi ini.

    Ketahanan aliran (pelajaran insiden 18 Nov 2026): agent pribadi
    memakai tool interaktif "question" yang menunggu jawaban manusia —
    di serve headless jawaban itu tidak pernah datang, eksekusi macet
    tanpa event apa pun, dan aliran SSE mati diputus proxy/browser
    (ERR_INCOMPLETE_CHUNKED_ENCODING di frontend). Karena itu generator
    ini: (1) mengirim heartbeat komentar SSE tiap 15 dtk senyap supaya
    proxy tidak memutus aliran yang sah, (2) punya tenggat total
    per role (publik OPENCODE_TIMEOUT_S, admin OPENCODE_TIMEOUT_S_ADMIN)
    lalu menutup dengan event error yang rapi, (3) menghentikan
    giliran seketika bila tool "question" muncul. Obat sebenarnya ada di
    config agent (agent serve dilarang memakai tool interaktif / effect
    "ask" — lihat opencode-core/), bukan di sini.
    """
    admin_user = _admin_gate(body.role, x_admin_token)
    session_id, base, role, agent_default, model_default, session_data = await _ensure_session(body)
    _log_chat(body, session_id, role, agent_default, model_default)
    if role == "public":
        limit_error = await _public_limit_error(body, base, session_id, session_data)
        if limit_error:
            print(f"[OPENCODE] limit publik session={session_id}: {limit_error}", flush=True)

            async def limit_gen():
                yield _sse({"meta": {"session_id": session_id}})
                yield _sse({"error": limit_error})

            return StreamingResponse(
                limit_gen(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
    prompt_text = body.prompt + _attachment_block(role, body.attachments)
    turn_start_tokens = _tokens_total((session_data or {}).get("tokens")) if session_data else 0
    start_usage = {
        "cost": (session_data or {}).get("cost"),
        "tokens": (session_data or {}).get("tokens"),
    }

    async def gen():
        yield _sse({"meta": {"session_id": session_id}})
        tools_used: list[str] = []
        call_names: dict[str, str] = {}
        touched_paths: list[str] = []
        model_id: str | None = None
        usage = None
        deadline_s = _deadline_s(role)
        timeout = httpx.Timeout(deadline_s, connect=10.0)
        started = time.monotonic()

        def _rec_turn(status: str, files: list | None = None) -> None:
            _record_ai_turn_only(
                session_id, role, body.agent or agent_default, model_id,
                admin_user, start_usage, usage, tools_used, status,
                int((time.monotonic() - started) * 1000),
                files=files,
            )
        try:
            async with httpx.AsyncClient(base_url=base, auth=AUTH, timeout=timeout) as client:
                async def abort_best_effort():
                    try:
                        await client.post(f"/api/session/{session_id}/abort", json={})
                    except Exception:
                        pass

                async with client.stream("GET", "/api/event") as resp:
                    if resp.status_code >= 400:
                        _rec_turn("error")
                        yield _sse({"error": f"opencode serve menjawab {resp.status_code} untuk /api/event"})
                        return
                    pr = await client.post(
                        f"/api/session/{session_id}/prompt", json={"text": prompt_text}
                    )
                    if pr.status_code >= 400:
                        _rec_turn("error")
                        yield _sse({"error": f"prompt ditolak serve: {pr.status_code}"})
                        return

                    queue: asyncio.Queue = asyncio.Queue()

                    async def producer():
                        try:
                            async for line in resp.aiter_lines():
                                await queue.put(line)
                        except Exception as exc:  # diteruskan ke konsumen
                            await queue.put(exc)
                        finally:
                            await queue.put(None)

                    task = asyncio.create_task(producer())
                    # Heartbeat diukur dari terakhir kali DATA TERKIRIM ke
                    # klien, bukan dari aktivitas antrean. Serve rajin
                    # mengirim event yang tidak diteruskan ke klien
                    # (usage, langkah sesi lain, dsb.) — event semacam itu
                    # mengulur timer lama walau klien tidak menerima satu
                    # byte pun, dan insiden 21 Nov 2026 (browser putus pada
                    # ±98 dtk, ERR_INCOMPLETE_CHUNKED_ENCODING) menunjukkan
                    # celah sepi 25+ dtk di sisi klien. Kini: maksimal
                    # ±15 dtk tanpa kiriman, lalu komentar SSE ": ping"
                    # (diabaikan semua parser klien). Berlaku juga selama
                    # masa tunggu HITL — stream memang sepi menurut desain
                    # saat admin menjawab form.
                    last_sent = time.monotonic()

                    def _out(payload: str) -> str:
                        nonlocal last_sent
                        last_sent = time.monotonic()
                        return payload

                    try:
                        while True:
                            try:
                                wait = max(0.1, 15.0 - (time.monotonic() - last_sent))
                                item = await asyncio.wait_for(queue.get(), timeout=wait)
                            except asyncio.TimeoutError:
                                if time.monotonic() - started > deadline_s:
                                    await abort_best_effort()
                                    print(f"[OPENCODE] timeout session={session_id} role={role} setelah {int(deadline_s)} dtk", flush=True)
                                    _rec_turn("error")
                                    yield _sse({"error": f"agent tidak selesai dalam {int(deadline_s)} detik — giliran dihentikan"})
                                    return
                                yield _out(": ping\n\n")  # heartbeat: jaga aliran dari timeout proxy
                                continue
                            if item is None:
                                break  # aliran event serve berakhir
                            if isinstance(item, Exception):
                                raise item
                            line = item
                            if not line.startswith("data:"):
                                continue
                            try:
                                ev = json.loads(line[5:].strip())
                            except ValueError:
                                continue
                            data = ev.get("data") or {}
                            form = data.get("form") or {}
                            ev_session = data.get("sessionID") or form.get("sessionID")
                            if ev_session != session_id:
                                continue
                            etype = ev.get("type")
                            if etype == "form.created" and body.hitl:
                                # Agent bertanya (tool question = form serve).
                                # Teruskan definisinya; klien menjawab lewat
                                # POST /sessions/{id}/forms/{form_id}/reply dan
                                # agent melanjutkan di aliran yang sama.
                                yield _out(_sse({"meta": {"hitl_form": form}}))
                            elif etype == "form.cancelled" and body.hitl:
                                yield _out(_sse({"meta": {"hitl_form_cancelled": data.get("id")}}))
                            elif etype == "permission.asked" and body.hitl:
                                # Agent meminta persetujuan aksi (effect "ask").
                                # Klien menjawab lewat POST
                                # /sessions/{id}/permissions/{request_id}/reply.
                                yield _out(_sse({"meta": {"hitl_permission": data}}))
                            elif etype == "session.text.delta":
                                delta = data.get("delta") or ""
                                if delta:
                                    yield _out(_sse({"text": delta}))
                            elif etype == "session.tool.input.started":
                                name = data.get("name")
                                if name:
                                    if data.get("id"):
                                        call_names[data["id"]] = name
                                    tools_used.append(name)
                                    yield _out(_sse({"meta": {"tool": name}}))
                                    if name == "question" and not body.hitl:
                                        await abort_best_effort()
                                        print(f"[OPENCODE] dihentikan session={session_id}: tool question tanpa hitl", flush=True)
                                        _rec_turn("error")
                                        yield _sse({"error": "agent meminta jawaban interaktif (tool question) — tidak bisa dijawab lewat jalur web ini; pakai agent yang dilarang memakai tool interaktif (lihat opencode-core/) atau kirim hitl=true dan jawab form-nya"})
                                        return
                            elif etype == "session.tool.called":
                                # Input tool lengkap (event "started" hanya
                                # membawa nama). Untuk write/edit, tangkap
                                # jalur file-nya: di akhir giliran isi file
                                # dikembalikan ke klien sebagai lampiran.
                                if call_names.get(data.get("id")) in _FILE_TOOLS:
                                    inp = data.get("input") or {}
                                    fp = inp.get("path") or inp.get("filePath")
                                    if fp:
                                        touched_paths.append(fp)
                            elif etype == "session.step.started":
                                model_id = (data.get("model") or {}).get("id") or model_id
                            elif etype == "session.usage.updated":
                                usage = {"cost": data.get("cost"), "tokens": data.get("tokens")}
                                if (
                                    role == "public"
                                    and PUBLIC_MAX_TURN_TOKENS
                                    and _tokens_total(data.get("tokens")) - turn_start_tokens > PUBLIC_MAX_TURN_TOKENS
                                ):
                                    await abort_best_effort()
                                    print(f"[OPENCODE] limit per giliran session={session_id}", flush=True)
                                    _rec_turn("error")
                                    yield _sse({"error": "Giliran ini dihentikan: pemakaian token melebihi batas per giliran. Persingkat permintaanmu."})
                                    return
                            elif etype == "session.execution.succeeded":
                                break
                            elif etype == "session.execution.failed":
                                raw = data.get("error")
                                err = raw.get("message") if isinstance(raw, dict) else (raw or "agent gagal")
                                print(f"[OPENCODE] gagal session={session_id} error={str(err)[:160]}", flush=True)
                                _rec_turn("error")
                                yield _sse({"error": str(err)})
                                return
                    finally:
                        task.cancel()
        except httpx.HTTPError as exc:
            print(f"[OPENCODE] aliran terputus session={session_id}: {exc}", flush=True)
            _rec_turn("error")
            yield _sse({"error": f"aliran event opencode terputus: {exc}"})
            return
        files = _collect_files(role, touched_paths)
        print(
            f"[OPENCODE] selesai session={session_id} model={model_id} "
            f"tools={tools_used} files={len(files)} durasi={time.monotonic() - started:.1f}dtk",
            flush=True,
        )
        _record_turn(
            session_id, role, body.agent or agent_default, model_id,
            admin_user, prompt_text, usage, tools_used,
        )
        _rec_turn("ok", files)
        yield _sse({
            "done": True,
            "session_id": session_id,
            "tools_used": tools_used,
            "model": model_id,
            "usage": usage,
            "files": files,
        })

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- HITL: form (pertanyaan agent) & permission (persetujuan aksi) ---
# Pasangan event "form.created"/"permission.asked" yang diteruskan
# /chat/stream saat body hitl=true. Semua tipis di depan serve; auth
# serve tetap di backend, browser tidak pernah menyentuh password serve.

@router.get("/sessions/{session_id}/forms")
async def list_forms(session_id: str, role: str = "public", x_admin_token: str | None = Header(default=None)):
    """Form pertanyaan agent yang masih tertunda di sesi ini."""
    _admin_gate(role, x_admin_token)
    _, base, _, _ = _target(role)
    data = await _oc("GET", f"/api/session/{session_id}/form", base=base, pass_404=True)
    return {"status": "success", "forms": (data or {}).get("data", [])}


@router.post("/sessions/{session_id}/forms/{form_id}/reply")
async def reply_form(session_id: str, form_id: str, body: FormReplyIn, role: str = "public", x_admin_token: str | None = Header(default=None)):
    """Jawab form agent; agent melanjutkan gilirannya sesudah ini."""
    _admin_gate(role, x_admin_token)
    _, base, _, _ = _target(role)
    answer = dict(body.answer)
    # Normalisasi bentuk jawaban per tipe field: serve menolak scalar
    # untuk field multiselect ("Expected string array for form field")
    # — bentuk yang benar adalah urusan kontrak backend<->serve,
    # bukan beban klien. Gagal membaca form tidak menggagalkan jawaban.
    try:
        forms = await _oc("GET", f"/api/session/{session_id}/form", base=base)
        form = next((f for f in (forms or {}).get("data", []) if f.get("id") == form_id), None)
        if form:
            for field in form.get("fields") or []:
                key = field.get("key")
                if field.get("type") == "multiselect" and key in answer and not isinstance(answer[key], list):
                    answer[key] = [answer[key]]
    except HTTPException:
        pass
    data = await _oc(
        "POST", f"/api/session/{session_id}/form/{form_id}/reply",
        json={"answer": answer}, base=base,
    )
    print(f"[OPENCODE] HITL form dijawab session={session_id} form={form_id}", flush=True)
    return {"status": "success", "data": (data or {}).get("data")}


@router.delete("/sessions/{session_id}/forms/{form_id}")
async def cancel_form(session_id: str, form_id: str, role: str = "public", x_admin_token: str | None = Header(default=None)):
    """Batalkan form yang tertunda (agent menerima pembatalan)."""
    _admin_gate(role, x_admin_token)
    _, base, _, _ = _target(role)
    await _oc("DELETE", f"/api/session/{session_id}/form/{form_id}", base=base)
    return {"status": "success"}


@router.get("/sessions/{session_id}/permissions")
async def list_permissions(session_id: str, role: str = "public", x_admin_token: str | None = Header(default=None)):
    """Permintaan persetujuan aksi yang masih tertunda di sesi ini."""
    _admin_gate(role, x_admin_token)
    _, base, _, _ = _target(role)
    data = await _oc("GET", f"/api/session/{session_id}/permission", base=base, pass_404=True)
    return {"status": "success", "permissions": (data or {}).get("data", [])}


@router.post("/sessions/{session_id}/permissions/{request_id}/reply")
async def reply_permission(session_id: str, request_id: str, body: PermissionReplyIn, role: str = "public", x_admin_token: str | None = Header(default=None)):
    """Jawab permintaan persetujuan: once (sekali), always (selalu), reject (tolak)."""
    _admin_gate(role, x_admin_token)
    if body.decision not in ("once", "always", "reject"):
        raise HTTPException(status_code=422, detail="decision harus 'once', 'always', atau 'reject'")
    _, base, _, _ = _target(role)
    payload: dict = {"decision": body.decision}
    if body.message:
        payload["message"] = body.message
    data = await _oc(
        "POST", f"/api/session/{session_id}/permission/{request_id}/reply",
        json=payload, base=base,
    )
    print(f"[OPENCODE] HITL permission session={session_id} request={request_id} decision={body.decision}", flush=True)
    return {"status": "success", "data": (data or {}).get("data")}
