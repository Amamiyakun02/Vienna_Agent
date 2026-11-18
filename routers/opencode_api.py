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
import time

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/v1/opencode", tags=["OpenCode Agent"])

BASE_URL = os.getenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096").rstrip("/")
PASSWORD = os.getenv("OPENCODE_SERVER_PASSWORD", "")
DEFAULT_AGENT = os.getenv("OPENCODE_AGENT", "vienna")
DEFAULT_MODEL = os.getenv("OPENCODE_MODEL", "").strip()  # format "providerID/modelID"
TIMEOUT_S = float(os.getenv("OPENCODE_TIMEOUT_S", "600"))
AUTH = ("opencode", PASSWORD) if PASSWORD else None


async def _oc(method: str, path: str, timeout: float = 30.0, **kwargs):
    """Satu pintu ke opencode serve; error diterjemahkan ke HTTP backend."""
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, auth=AUTH, timeout=timeout) as client:
            resp = await client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"opencode serve tidak terjangkau di {BASE_URL} — nyalakan ~/start-opencode-serve.sh ({exc})",
        )
    if resp.status_code >= 400:
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
    model: str | None = None  # format "providerID/modelID"; hanya dipakai saat sesi baru dibuat
    hitl: bool = False  # True = klien bisa menjawab form/permission agent (lihat /chat/stream)


class FormReplyIn(BaseModel):
    answer: dict  # peta key field -> nilai, sesuai definisi form dari event form.created


class PermissionReplyIn(BaseModel):
    decision: str  # "once" | "always" | "reject" (enum Permission.Reply di serve)
    message: str | None = None


async def _create_session(body: SessionIn) -> str:
    payload: dict = {"agent": body.agent or DEFAULT_AGENT}
    if body.title:
        payload["title"] = body.title
    ref = _model_ref(body.model or DEFAULT_MODEL)
    if ref:
        payload["model"] = ref
    data = await _oc("POST", "/api/session", json=payload)
    return data["data"]["id"]


@router.get("/status")
async def status():
    """Kesehatan jembatan backend -> opencode serve."""
    try:
        data = await _oc("GET", "/api/model/default", timeout=10.0)
    except HTTPException as exc:
        return {"status": "unreachable", "base_url": BASE_URL, "detail": exc.detail}
    model = (data or {}).get("data") or {}
    return {
        "status": "ok",
        "base_url": BASE_URL,
        "default_agent": DEFAULT_AGENT,
        "default_model": model.get("id"),
        "auth_configured": bool(PASSWORD),
    }


@router.get("/models")
async def list_models():
    """Katalog model opencode serve, dikelompokkan per provider.

    API key platform lain diisi user di .env backend (nama env standar
    tiap provider) dan diteruskan ke serve oleh start-opencode-serve.sh;
    model provider itu otomatis bisa dipilih lewat field "model"
    ("providerID/modelID") pada POST /chat atau env OPENCODE_MODEL.
    """
    data = await _oc("GET", "/api/model")
    providers: dict[str, list[str]] = {}
    for m in (data or {}).get("data", []):
        providers.setdefault(m.get("providerID", "?"), []).append(m.get("id"))
    return {
        "status": "success",
        "total": sum(len(v) for v in providers.values()),
        "default_model": DEFAULT_MODEL or None,
        "providers": {k: sorted(v) for k, v in sorted(providers.items())},
    }


@router.get("/sessions")
async def list_sessions():
    data = await _oc("GET", "/api/session")
    sessions = [
        {"id": s.get("id"), "title": s.get("title"), "agent": s.get("agent"), "time": s.get("time")}
        for s in (data or {}).get("data", [])
    ]
    return {"status": "success", "sessions": sessions}


@router.post("/sessions")
async def create_session(body: SessionIn):
    session_id = await _create_session(body)
    return {"status": "success", "session_id": session_id}


@router.get("/sessions/{session_id}/messages")
async def session_messages(session_id: str):
    data = await _oc("GET", f"/api/session/{session_id}/message")
    return {"status": "success", "messages": (data or {}).get("data", [])}


@router.post("/chat")
async def chat(body: ChatIn):
    """Kirim prompt ke agent opencode dan tunggu jawaban finalnya.

    Sesi & riwayat disimpan opencode sendiri (bukan Mongo backend) —
    itulah inti memakai harness-nya. Lanjutkan percakapan dengan
    mengembalikan session_id dari respons ke permintaan berikutnya.
    """
    session_id = body.session_id or await _create_session(
        SessionIn(agent=body.agent, model=body.model)
    )
    await _oc("POST", f"/api/session/{session_id}/prompt", json={"text": body.prompt})
    await _oc(
        "POST", f"/api/experimental/session/{session_id}/wait",
        json={}, timeout=TIMEOUT_S,
    )
    data = await _oc("GET", f"/api/session/{session_id}/message")
    messages = sorted(
        (data or {}).get("data", []),
        key=lambda m: (m.get("time") or {}).get("created", 0),
    )

    idle = next((m for m in reversed(messages) if m.get("type") == "idle"), None)
    if idle and idle.get("outcome") == "failed":
        error = (idle.get("error") or {}).get("message", "agent gagal tanpa pesan error")
        return {"status": "error", "session_id": session_id, "response": "", "error": error}

    # Satu giliran = banyak pesan assistant (satu per langkah agent);
    # yang relevan hanya yang sesudah pesan user terakhir.
    last_user_idx = max((i for i, m in enumerate(messages) if m.get("type") == "user"), default=-1)
    turn = [m for m in messages[last_user_idx + 1:] if m.get("type") == "assistant"]
    if not turn:
        return {"status": "error", "session_id": session_id, "response": "", "error": "Tidak ada jawaban assistant."}
    assistant = turn[-1]
    parts = assistant.get("content") or []
    text = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
    tools = [p.get("name") for m in turn for p in (m.get("content") or []) if p.get("type") == "tool"]
    model = (assistant.get("model") or {}).get("id")
    return {
        "status": "success",
        "session_id": session_id,
        "response": text,
        "tools_used": tools,
        "agent": assistant.get("agent"),
        "model": model,
    }


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@router.post("/chat/stream")
async def chat_stream(body: ChatIn):
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
    TIMEOUT_S lalu menutup dengan event error yang rapi, (3) menghentikan
    giliran seketika bila tool "question" muncul. Obat sebenarnya ada di
    config agent (agent serve dilarang memakai tool interaktif / effect
    "ask" — lihat opencode-core/), bukan di sini.
    """
    session_id = body.session_id or await _create_session(
        SessionIn(agent=body.agent, model=body.model)
    )

    async def gen():
        yield _sse({"meta": {"session_id": session_id}})
        tools_used: list[str] = []
        model_id: str | None = None
        usage = None
        timeout = httpx.Timeout(TIMEOUT_S, connect=10.0)
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(base_url=BASE_URL, auth=AUTH, timeout=timeout) as client:
                async def abort_best_effort():
                    try:
                        await client.post(f"/api/session/{session_id}/abort", json={})
                    except Exception:
                        pass

                async with client.stream("GET", "/api/event") as resp:
                    if resp.status_code >= 400:
                        yield _sse({"error": f"opencode serve menjawab {resp.status_code} untuk /api/event"})
                        return
                    pr = await client.post(
                        f"/api/session/{session_id}/prompt", json={"text": body.prompt}
                    )
                    if pr.status_code >= 400:
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
                    try:
                        while True:
                            try:
                                item = await asyncio.wait_for(queue.get(), timeout=15.0)
                            except asyncio.TimeoutError:
                                if time.monotonic() - started > TIMEOUT_S:
                                    await abort_best_effort()
                                    yield _sse({"error": f"agent tidak selesai dalam {int(TIMEOUT_S)} detik — giliran dihentikan"})
                                    return
                                yield ": hb\n\n"  # heartbeat: jaga aliran dari timeout proxy
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
                                yield _sse({"meta": {"hitl_form": form}})
                            elif etype == "form.cancelled" and body.hitl:
                                yield _sse({"meta": {"hitl_form_cancelled": data.get("id")}})
                            elif etype == "permission.asked" and body.hitl:
                                # Agent meminta persetujuan aksi (effect "ask").
                                # Klien menjawab lewat POST
                                # /sessions/{id}/permissions/{request_id}/reply.
                                yield _sse({"meta": {"hitl_permission": data}})
                            elif etype == "session.text.delta":
                                delta = data.get("delta") or ""
                                if delta:
                                    yield _sse({"text": delta})
                            elif etype == "session.tool.input.started":
                                name = data.get("name")
                                if name:
                                    tools_used.append(name)
                                    yield _sse({"meta": {"tool": name}})
                                    if name == "question" and not body.hitl:
                                        await abort_best_effort()
                                        yield _sse({"error": "agent meminta jawaban interaktif (tool question) — tidak bisa dijawab lewat jalur web ini; pakai agent yang dilarang memakai tool interaktif (lihat opencode-core/) atau kirim hitl=true dan jawab form-nya"})
                                        return
                            elif etype == "session.step.started":
                                model_id = (data.get("model") or {}).get("id") or model_id
                            elif etype == "session.usage.updated":
                                usage = {"cost": data.get("cost"), "tokens": data.get("tokens")}
                            elif etype == "session.execution.succeeded":
                                break
                            elif etype == "session.execution.failed":
                                raw = data.get("error")
                                err = raw.get("message") if isinstance(raw, dict) else (raw or "agent gagal")
                                yield _sse({"error": str(err)})
                                return
                    finally:
                        task.cancel()
        except httpx.HTTPError as exc:
            yield _sse({"error": f"aliran event opencode terputus: {exc}"})
            return
        yield _sse({
            "done": True,
            "session_id": session_id,
            "tools_used": tools_used,
            "model": model_id,
            "usage": usage,
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
async def list_forms(session_id: str):
    """Form pertanyaan agent yang masih tertunda di sesi ini."""
    data = await _oc("GET", f"/api/session/{session_id}/form")
    return {"status": "success", "forms": (data or {}).get("data", [])}


@router.post("/sessions/{session_id}/forms/{form_id}/reply")
async def reply_form(session_id: str, form_id: str, body: FormReplyIn):
    """Jawab form agent; agent melanjutkan gilirannya sesudah ini."""
    data = await _oc(
        "POST", f"/api/session/{session_id}/form/{form_id}/reply",
        json={"answer": body.answer},
    )
    return {"status": "success", "data": (data or {}).get("data")}


@router.delete("/sessions/{session_id}/forms/{form_id}")
async def cancel_form(session_id: str, form_id: str):
    """Batalkan form yang tertunda (agent menerima pembatalan)."""
    await _oc("DELETE", f"/api/session/{session_id}/form/{form_id}")
    return {"status": "success"}


@router.get("/sessions/{session_id}/permissions")
async def list_permissions(session_id: str):
    """Permintaan persetujuan aksi yang masih tertunda di sesi ini."""
    data = await _oc("GET", f"/api/session/{session_id}/permission")
    return {"status": "success", "permissions": (data or {}).get("data", [])}


@router.post("/sessions/{session_id}/permissions/{request_id}/reply")
async def reply_permission(session_id: str, request_id: str, body: PermissionReplyIn):
    """Jawab permintaan persetujuan: once (sekali), always (selalu), reject (tolak)."""
    if body.decision not in ("once", "always", "reject"):
        raise HTTPException(status_code=422, detail="decision harus 'once', 'always', atau 'reject'")
    payload: dict = {"decision": body.decision}
    if body.message:
        payload["message"] = body.message
    data = await _oc(
        "POST", f"/api/session/{session_id}/permission/{request_id}/reply",
        json=payload,
    )
    return {"status": "success", "data": (data or {}).get("data")}
