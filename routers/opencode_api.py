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

shortcut: jawaban dikumpulkan sesudah sesi idle (bukan streaming SSE
seperti engine lama). Cukup untuk v1; upgrade ke passthrough
GET /api/event (SSE) bila frontend butuh token-by-token.
"""

import os

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/v1/opencode", tags=["OpenCode Agent"])

BASE_URL = os.getenv("OPENCODE_BASE_URL", "http://127.0.0.1:4096").rstrip("/")
PASSWORD = os.getenv("OPENCODE_SERVER_PASSWORD", "")
DEFAULT_AGENT = os.getenv("OPENCODE_AGENT", "vienna")
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


async def _create_session(body: SessionIn) -> str:
    payload: dict = {"agent": body.agent or DEFAULT_AGENT}
    if body.title:
        payload["title"] = body.title
    ref = _model_ref(body.model)
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
