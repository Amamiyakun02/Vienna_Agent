import os
os.environ["OPENBLAS_NUM_THREADS"] = "1"
import re
import jwt
import json
import html
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, Body, BackgroundTasks, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from dotenv import load_dotenv
from services import AgentEngine, GeminiAgentEngine, MercuryAgentEngine, DeepSeekAgentEngine
from services.github_service import sync_github_repositories
from utils.prompt_builder import build_prompt
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from services import get_or_create_session, get_messages, get_session, save_message, is_portfolio_persona, create_session, archive_session, count_session_messages
from services.summary_service import maybe_rollup_summary
from bson import ObjectId
from fastapi import WebSocket, WebSocketDisconnect
from services.connection_manager import manager
from services.mongo_service import devices_col, device_app_inventory_col
from services.redis_service import redis_client


from fastapi.staticfiles import StaticFiles
load_dotenv()
DEFAULT_JWT_SECRET = "super-secret-jwt-key-aimer-future-2026-06-02"
JWT_SECRET = os.getenv("JWT_SECRET", DEFAULT_JWT_SECRET)
JWT_ALGORITHM = "HS256"

_DEFAULT_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3100",
    "http://127.0.0.1:3100",
    "https://amamiyakun02.github.io",
    "https://smart-guide-ai-agent.vercel.app",
    "https://lina-deals.vercel.app",
]
# Deploy: tambah origin via env CORS_ALLOW_ORIGINS (comma-separated)
# agar tak perlu edit kode tiap ada frontend baru.
_EXTRA_CORS = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()]
CORS_ALLOW_ORIGINS = list(dict.fromkeys(_DEFAULT_CORS_ORIGINS + _EXTRA_CORS))
CORS_ALLOW_ORIGIN_REGEX = r"https://.*\.trycloudflare\.com"


def _is_origin_allowed(origin: str) -> bool:
    if origin in CORS_ALLOW_ORIGINS:
        return True
    return re.match(CORS_ALLOW_ORIGIN_REGEX, origin) is not None


@asynccontextmanager
async def lifespan(app: FastAPI):
    if JWT_SECRET == DEFAULT_JWT_SECRET:
        print("[SECURITY WARNING] JWT_SECRET not set — using insecure default. Set JWT_SECRET in .env!")
    # Pre-fetch and cache MCP tools concurrently during server startup
    try:
        from services.agent_service import get_mcp_tools
        asyncio.create_task(get_mcp_tools())
    except Exception as e:
        print(f"[ERROR] Failed to schedule MCP tools pre-fetch: {e}")
    try:
        from services.rag_ingestion_service import initialize_qdrant_collections
        await initialize_qdrant_collections()
    except Exception as e:
        print(f"[ERROR] RAG/Qdrant init gagal — retrieval nonaktif: {e}")
    try:
        from services.mongo_service import ensure_mongo_indexes
        await ensure_mongo_indexes()
    except Exception as e:
        print(f"[ERROR] Error ensuring Mongo indexes: {e}")
    # System Dashboard: buat tabel PostgreSQL + seed registry project.
    try:
        from services.dashboard_store import init_dashboard_db
        await asyncio.to_thread(init_dashboard_db)
    except Exception as e:
        print(f"[ERROR] Init database dashboard gagal — modul data nonaktif: {e}")

    # Warmup koneksi agar pesan pertama tidak membayar TLS handshake (~6s).
    try:
        from services.mongo_service import mongo_client as _mc
        await asyncio.wait_for(_mc.admin.command("ping"), timeout=15)
        print("[WARMUP] Mongo ping OK.")
    except Exception as e:
        print(f"[WARMUP WARN] Mongo ping gagal: {e}")
    try:
        from services.redis_service import redis_client as _rc
        await asyncio.wait_for(_rc.ping(), timeout=10)
        print("[WARMUP] Redis ping OK.")
    except Exception as e:
        print(f"[WARMUP WARN] Redis ping gagal: {e}")

    async def _warm_tool_embeddings():
        # Embed tools di background (one-time, detik-an) agar chat pertama
        # langsung hangat. Saat limit: cepat gagal via cooldown, dicoba lagi
        # otomatis di pesan berikutnya.
        try:
            from services.agent_service import get_mcp_tools, _prepare_tool_embeddings
            tools = await get_mcp_tools()
            if tools:
                await _prepare_tool_embeddings(tools)
                print("[WARMUP] Tool embeddings siap.")
        except Exception as e:
            print(f"[WARMUP WARN] Tool embeddings gagal: {e}")
    asyncio.create_task(_warm_tool_embeddings())

    # Run the keep-alive background task (cancellable on shutdown)
    keep_alive_task = asyncio.create_task(keep_alive_spaces())
    yield
    keep_alive_task.cancel()
    try:
        await keep_alive_task
    except asyncio.CancelledError:
        pass
    print("[SHUTDOWN] Closing database and cache connections...")
    try:
        from services.redis_service import close_redis_connection
        await close_redis_connection()
        print("[SHUTDOWN] Redis connection closed successfully.")
    except Exception as e:
        print(f"[SHUTDOWN ERROR] Failed to close Redis: {e}")

    try:
        from services.mongo_service import mongo_client
        mongo_client.close()
        print("[SHUTDOWN] MongoDB connection closed successfully.")
    except Exception as e:
        print(f"[SHUTDOWN ERROR] Failed to close MongoDB: {e}")


app = FastAPI(lifespan=lifespan)

# Kompres respons JSON besar (mis. batch_list 1500 entri ~200KB -> ~20KB)
app.add_middleware(GZipMiddleware, minimum_size=1000)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_origin_regex=CORS_ALLOW_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Session-ID", "X-Cache", "X-Platform", "Content-Disposition"],
)

agent_vienna = AgentEngine(persona_file="agents/vienna.json")
agent_gemini = GeminiAgentEngine(persona_file="agents/vienna.json")
agent_mercury = MercuryAgentEngine(persona_file="agents/vienna.json")
agent_deepseek = DeepSeekAgentEngine(persona_file="agents/vienna.json")


from routers.auth_api import router as auth_router
app.include_router(auth_router)

from routers.pdf_api import router as pdf_router
app.include_router(pdf_router)

from routers.anime_api import router as anime_router
app.include_router(anime_router)

from routers.spotify_api import router as spotify_router
app.include_router(spotify_router)

from routers.admin import router as admin_router
app.include_router(admin_router)

from routers.gemini_live import router as gemini_live_router
app.include_router(gemini_live_router)

from routers.tools_api import router as tools_router, legacy_router as youtube_legacy_router, legacy_downloader_router as downloader_legacy_router
app.include_router(tools_router)
app.include_router(youtube_legacy_router)
app.include_router(downloader_legacy_router)

from routers.dashboard_api import public_router as public_projects_router
from routers.dashboard_api import contents_public_router
from routers.dashboard_api import router as dashboard_router
app.include_router(dashboard_router)
app.include_router(public_projects_router)
app.include_router(contents_public_router)

from routers.opencode_api import router as opencode_router
app.include_router(opencode_router)


# ──────────────────────────────────────────────
# GLOBAL EXCEPTION HANDLERS (Manual CORS headers injection)
# ──────────────────────────────────────────────
from fastapi import Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException

def add_cors_headers(request: Request, response: JSONResponse) -> JSONResponse:
    # Only echo back origins we actually trust — never reflect arbitrary origins
    # with credentials enabled.
    origin = request.headers.get("origin")
    if origin and _is_origin_allowed(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "*"
    return response

@app.exception_handler(StarletteHTTPException)
@app.exception_handler(FastAPIHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    response = JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )
    return add_cors_headers(request, response)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import traceback
    error_trace = traceback.format_exc()
    # Full traceback stays server-side only — never leak internals to clients.
    print("[CRITICAL ERROR] Unhandled exception occurred:")
    print(error_trace)
    response = JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error"}
    )
    return add_cors_headers(request, response)

async def keep_alive_spaces():
    import httpx
    urls = [
        "https://amamiya-kun-removebg.hf.space/",
        "https://amamiya-kun-viennago.hf.space/",
        # "https://amamiya-kun-palmscan.hf.space/"
    ]
    await asyncio.sleep(10)  # Wait 10 seconds for main server startup to settle
    while True:
        try:
            async with httpx.AsyncClient() as client:
                for url in urls:
                    try:
                        res = await client.get(url, timeout=10.0)
                        print(f"[KEEP-ALIVE] Pinged {url} - Status: {res.status_code}")
                    except Exception as e:
                        print(f"[KEEP-ALIVE WARNING] Failed to ping {url}: {e}")
        except Exception as e:
            print(f"[KEEP-ALIVE WARNING] HTTP client initialization failed: {e}")
        await asyncio.sleep(1500)  # 25 minutes interval

@app.post("/v1/github/sync")
async def trigger_github_sync(background_tasks: BackgroundTasks):
    """
    Trigger manual synchronization of GitHub repositories in the background.
    """
    background_tasks.add_task(sync_github_repositories)
    return {
        "status": "success",
        "message": "GitHub synchronization has been started in the background."
    }


# ──────────────────────────────────────────────
# PRIVATE AI AGENT DEVICE API & WEBSOCKET ENDPOINTS
# ──────────────────────────────────────────────

class RegisterDeviceRequest(BaseModel):
    user_id: str
    device_id: str
    fcm_token: str

class RegisterDeviceResponse(BaseModel):
    status: str
    message: str

@app.post("/v1/device/register", response_model=RegisterDeviceResponse)
async def register_device(payload: RegisterDeviceRequest):
    try:
        await devices_col.update_one(
            {"user_id": payload.user_id, "device_id": payload.device_id},
            {"$set": {
                "fcm_token": payload.fcm_token,
                "updated_at": datetime.now(timezone.utc)
            }},
            upsert=True
        )
        return {"status": "success", "message": "Device token registered successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to register device: {str(e)}")

class ActionResultRequest(BaseModel):
    tool_call_id: str
    status: str
    result: Optional[Dict[str, Any]] = None
    message: Optional[str] = None

@app.post("/v1/device/action-result")
async def device_action_result(payload: ActionResultRequest):
    try:
        # Save result to Redis for agent waiting loop
        await redis_client.set(f"tool_result:{payload.tool_call_id}", payload.model_dump_json(), ex=60)
        return {"status": "success", "message": "Action result relayed."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to relay action result: {str(e)}")

@app.websocket("/v1/assistant/ws")
async def websocket_endpoint(websocket: WebSocket):
    user_id = None
    try:
        await websocket.accept()
        init_data = await websocket.receive_text()
        init_json = json.loads(init_data)
        
        if init_json.get("type") == "register":
            user_id = init_json.get("user_id")
            display_name = init_json.get("display_name", "")
            device_id = init_json.get("device_id", "default_device")
            fcm_token = init_json.get("fcm_token")
            
            if not user_id:
                await websocket.send_text(json.dumps({"error": "Missing user_id"}))
                await websocket.close(1008)
                return
                
            await manager.connect(user_id, websocket)
            
            if fcm_token:
                await devices_col.update_one(
                    {"user_id": user_id, "device_id": device_id},
                    {"$set": {
                        "fcm_token": fcm_token,
                        "updated_at": datetime.now(timezone.utc)
                    }},
                    upsert=True
                )
            
            await websocket.send_text(json.dumps({"type": "registered", "status": "success"}))
            log_name = f"{display_name} ({user_id})" if display_name else user_id
            print(f"[WS] Registered connection for user {log_name} (device: {device_id})")
        else:
            await websocket.send_text(json.dumps({"error": "First message must be registration"}))
            await websocket.close(1008)
            return

        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            print(f"[WS] Message received from user {user_id}: {message}")
            
            if message.get("type") == "sync_inventory":
                apps = message.get("apps", [])
                device_id = message.get("device_id", "default_device")
                await device_app_inventory_col.update_one(
                    {"user_id": user_id, "device_id": device_id},
                    {"$set": {
                        "apps": apps,
                        "updated_at": datetime.now(timezone.utc)
                    }},
                    upsert=True
                )
                print(f"[WS] Synced {len(apps)} apps for user {user_id}")
                await websocket.send_text(json.dumps({
                    "type": "sync_inventory_response",
                    "status": "success"
                }))
                
            elif "tool_call_id" in message and "status" in message:
                tool_call_id = message["tool_call_id"]
                await redis_client.set(f"tool_result:{tool_call_id}", json.dumps(message), ex=60)
                print(f"[WS] Relayed result for tool call {tool_call_id} to Redis")
                
    except WebSocketDisconnect:
        if user_id:
            manager.disconnect(user_id, websocket)
    except Exception as e:
        print(f"[WS ERROR] Error in WebSocket connection: {e}")
        if user_id:
            manager.disconnect(user_id, websocket)
        try:
            await websocket.close()
        except Exception:
            pass

@app.get("/")
async def root():
    return {"status": "ok", "service": "vienna-agent"}


@app.get("/health")
async def health():
    """Healthcheck untuk FastAPI Cloud / Railway / Render / LB.

    Selalu 200 bila proses hidup — TANPA menyentuh DB eksternal
    agar orchestrator tidak me-restart saat Mongo/Redis ngadat.
    """
    return {"status": "ok"}


class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    user_id: str
    session_id: Optional[str] = None  # bisa kosong jika sesi baru
    messages: List[ChatMessage]
    lang: Optional[str] = "id"
    role: Optional[str] = None  # kompatibilitas saja — DIABAIKAN, role ditentukan server

# Hygiene input user: cegah spam/gibberish mengotori memory & prompt.
USER_INPUT_MAX_CHARS = int(os.getenv("USER_INPUT_MAX_CHARS", "4000"))


def sanitize_user_input(raw: str) -> tuple[str, bool]:
    """Bersihkan input user. Return (teks_bersih, boleh_disimpan).

    - Truncate 4000 char agar prompt tidak jebol oleh paste raksasa.
    - Gibberish (tanpa huruf/angka) tetap dijawab tapi TIDAK disimpan ke DB.
    """
    text = (raw or "").strip()
    if len(text) > USER_INPUT_MAX_CHARS:
        text = text[:USER_INPUT_MAX_CHARS].rstrip() + " … [pesan dipotong]"
    saveable = len(text) >= 2 and any(c.isalnum() for c in text)
    return text, saveable


def detect_retry_burst(conversation: list) -> int:
    """Hitung pesan 'user' beruntun di ujung history (indikasi retry/spam kirim ulang)."""
    n = 0
    for m in reversed(conversation or []):
        if m.get("sender") == "user":
            n += 1
        else:
            break
    return n


# Pola jailbreak/injeksi umum (ID/EN). Dicek SEBELUM panggil LLM:
# serangan umum ditolak instan — tanpa bakar kuota, tanpa racuni memory.
_JAILBREAK_PATTERNS = [
    r"abaikan\s+(semua\s+)?instruksi",
    r"lupakan\s+(semua\s+)?instruksi",
    r"ignore\s+(all\s+)?(previous|prior|above|your)\s+instructions",
    r"forget\s+(all\s+)?(previous|prior|your)\s+instructions",
    r"disregard\s+(all\s+)?(previous|prior|your|safety|filter|guardrail)\s+\w*",
    r"system\s*prompt",
    r"tampilkan\s+(system\s*prompt|instruksi\s*(sistem|rahasia|internal|asli))",
    r"tulis\s+ulang\s+(instruksi|system)",
    r"reveal\s+(your\s+)?(system\s*prompt|instructions|internal)",
    r"repeat\s+(your\s+)?(system\s*prompt|instructions)",
    r"jailbreak|jail\s*break",
    r"kamu\s+sekarang\s+(adalah|bukan)|anda\s+sekarang\s+(adalah|bukan)",
    r"you\s+are\s+now\s+(a|an|not|no\s+longer|dan|without|above|beyond|unrestricted|jailbroken|free)",
    r"developer\s*mode|mode\s*developer|god\s*mode|mode\s*dewa",
    r"disable\s+(safety|filter|guardrail|restriction)",
    r"nonaktifkan\s+(keamanan|filter|batasan|pengaman)",
    r"bypass\s+(safety|filter|restriction|keamanan)",
    r"prompt\s*injection",
]

_compiled_jailbreak = [re.compile(p, re.IGNORECASE) for p in _JAILBREAK_PATTERNS]

# Pola PEKA huruf besar/kecil — dicek terpisah TANPA IGNORECASE.
# "DAN" (Do Anything Now) selalu ditulis serba kapital oleh penyerang,
# sedangkan "dan" adalah kata sambung Bahasa Indonesia yang sangat umum:
# mencocokkan \bDAN\b secara case-insensitive memblokir hampir semua
# kalimat Indonesia (false positive massal, diperbaiki 9 Nov 2026).
_JAILBREAK_PATTERNS_CS = [
    r"\bDAN\b",
]

_compiled_jailbreak_cs = [re.compile(p) for p in _JAILBREAK_PATTERNS_CS]


def detect_jailbreak(text: str) -> bool:
    """True bila input cocok pola injeksi/jailbreak umum."""
    t = text or ""
    return any(rx.search(t) for rx in _compiled_jailbreak) or any(
        rx.search(t) for rx in _compiled_jailbreak_cs
    )


def _jailbreak_refusal(lang: Optional[str]) -> str:
    if (lang or "id").lower().startswith("en"):
        return ("I can't follow instructions that override my core behavior. "
                "I'm Vienna, Amamiya's AI assistant — ask me about his skills, projects, or how to reach him.")
    return ("Aku tidak bisa mengikuti instruksi yang menimpa perilaku intiku. "
            "Aku Vienna, asisten AI Amamiya — tanya tentang keahliannya, proyeknya, atau cara menghubunginya.")


# Rate limit chat (lindungi kuota free tier dari burn).
# Default: 20 req/menit + 150 req/hari per IP (override via env).
CHAT_RL_PER_MIN = int(os.getenv("CHAT_RL_PER_MIN", "20"))
CHAT_RL_PER_DAY = int(os.getenv("CHAT_RL_PER_DAY", "150"))


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip() or "unknown"
    return (request.client.host if request.client else "unknown")


async def _check_chat_rate_limit(request: Request) -> Optional[str]:
    """Return None bila lolos; label scope bila limit ("min"/"day"). Redis mati -> fail-open."""
    try:
        ip = _client_ip(request)
        now = datetime.now(timezone.utc)
        from services.redis_service import redis_client as _rc
        for scope, key, ttl, cap in (
            ("min", f"rl:chat:min:{ip}:{now.strftime('%Y%m%d%H%M')}", 70, CHAT_RL_PER_MIN),
            ("day", f"rl:chat:day:{ip}:{now.strftime('%Y%m%d')}", 87000, CHAT_RL_PER_DAY),
        ):
            n = await _rc.incr(key)
            if n == 1:
                await _rc.expire(key, ttl)
            if n > cap:
                return scope
        return None
    except Exception as e:
        print(f"[RL WARN] Rate limiter fail-open: {e}")
        return None
# Budget history prompt (override via env). Menjaga prompt kecil & fokus:
# summary meng-cover konteks lama, history hanya jendela terakhir.
HISTORY_MSG_LIMIT = int(os.getenv("HISTORY_MSG_LIMIT", "12"))
HISTORY_PER_MSG_CHARS = int(os.getenv("HISTORY_PER_MSG_CHARS", "1500"))
HISTORY_TOTAL_CHARS = int(os.getenv("HISTORY_TOTAL_CHARS", "12000"))


def budget_history(messages: list) -> list:
    """Potong history agar hemat token: truncate per pesan + cap total char.

    Pesan paling baru dipertahankan utuh; yang lama dipotong dari ujung depan.
    """
    if not messages:
        return []
    budgeted = []
    for m in messages:
        content = m.get("content", "") or ""
        if len(content) > HISTORY_PER_MSG_CHARS:
            content = content[:HISTORY_PER_MSG_CHARS].rstrip() + " …"
        budgeted.append({**m, "content": content})
    total = sum(len(m.get("content", "")) for m in budgeted)
    while len(budgeted) > 1 and total > HISTORY_TOTAL_CHARS:
        dropped = budgeted.pop(0)
        total -= len(dropped.get("content", ""))
    return budgeted

def clean_content(s: str, max_chars: int = None) -> str:
    """Bersihkan whitespace/gandaan dan decode entity; potong jika terlalu panjang."""
    if not isinstance(s, str):
        return ""
    # decode HTML entities if ada, hapus leading/trailing whitespace
    s = html.unescape(s).strip()
    # ganti multiple whitespace (newline/tab/spasi) menjadi satu spasi
    s = re.sub(r'\s+', ' ', s)
    if max_chars and len(s) > max_chars:
        return s[:max_chars].rstrip() + " …"
    return s

def normalize_timestamp(msg):
    ts = msg.get("timestamp", datetime.now(timezone.utc))

    # Jika string → parse
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            try:
                ts = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                ts = datetime.now(timezone.utc)

    if not isinstance(ts, datetime):
        ts = datetime.now(timezone.utc)

    # Jika naive (tanpa tz) → tambahkan UTC
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    return {**msg, "timestamp": ts}

def format_conversation(messages):
    if not messages:  # kosong atau None
        return "No conversation yet."

    normalized = [normalize_timestamp(m) for m in messages]
    sorted_msgs = sorted(normalized, key=lambda x: x["timestamp"])

    lines = []
    for msg in sorted_msgs:
        time_str = msg["timestamp"].strftime("%H:%M:%S")
        sender = "User" if msg.get("sender") == "user" else "Agent"
        lines.append(f"[{time_str}] {sender:10}: {msg.get('content', '')}")
    return "\n".join(lines)

# NOTE (2026-09-29): endpoint OpenAI/GPT DINONAKTIFKAN sementara — credits habis.
# Frontend dikunci ke Gemini saja. Untuk re-enable: uncomment blok di bawah.
# @app.post("/v1/assistant/chat")
# async def assistant_chat(payload: ChatRequest = Body(...)):
#     return await process_chat(payload, agent_vienna)



@app.post("/v1/gemini/chat")
async def gemini_chat(payload: ChatRequest = Body(...), request: Request = None):
    return await process_chat(payload, agent_gemini, request=request)


@app.post("/v1/mercury/chat")
@app.post("/v1/inception/chat")
async def mercury_chat(payload: ChatRequest = Body(...), request: Request = None):
    """Chat via Inception Labs Mercury (OpenAI-compatible).

    Butuh MERCURY_API_KEY (atau INCEPTION_API_KEY). Model default `mercury-3`,
    fallback `mercury-2.5` — bisa diatur lewat env MERCURY_MODEL dsb.
    """
    return await process_chat(payload, agent_mercury, request=request)


@app.post("/v1/deepseek/chat")
@app.post("/v1/larprouter/chat")
async def deepseek_chat(payload: ChatRequest = Body(...), request: Request = None):
    """Chat via DeepSeek usando LARP Router (API OpenAI-compatible)."""
    return await process_chat(payload, agent_deepseek, request=request)


@app.get("/v1/engines")
async def list_engines():
    """Daftar engine chat yang tersedia + status konfigurasi API key."""
    return {
        "status": "success",
        "engines": [
            {
                "id": "gemini",
                "label": "Google Gemini",
                "model": os.getenv("GEMINI_CHAT_MODEL", "gemini-3.1-flash-lite"),
                "endpoint": "/v1/gemini/chat",
                "available": bool(os.getenv("GEMINI_API_KEY")),
            },
            {
                "id": "mercury",
                "label": "Inception Labs Mercury",
                "provider": getattr(agent_mercury, "provider", "mercury"),
                "model": getattr(agent_mercury, "model_name", "mercury-3"),
                "endpoint": "/v1/mercury/chat",
                "available": bool(getattr(agent_mercury, "available", False)),
            },
            {
                "id": "deepseek",
                "label": "DeepSeek via LARP Router",
                "provider": getattr(agent_deepseek, "provider", "larprouter"),
                "model": getattr(agent_deepseek, "model_name", "deepseek-v4.1-flash"),
                "endpoint": "/v1/deepseek/chat",
                "available": bool(getattr(agent_deepseek, "available", False)),
            },
            {
                "id": "opencode",
                "label": "OpenCode Agent (core harness)",
                "provider": "opencode-serve",
                "model": os.getenv("OPENCODE_MODEL", "deepseek-v4.1-flash-free"),
                "endpoint": "/v1/opencode/chat",
                "available": bool(os.getenv("OPENCODE_SERVER_PASSWORD")),
            },
        ],
    }


def _format_model_entry(model_id: str, owned_by: str, root: str, available: bool, tools: bool = True, vision: bool = False):
    return {
        "id": model_id,
        "object": "model",
        "created": 1720000000,
        "owned_by": owned_by,
        "root": root,
        "parent": None,
        "available": available,
        "permission": [],
        "capabilities": {
            "tools": tools,
            "vision": vision,
        },
    }


@app.get("/v1/models")
@app.get("/models")
async def list_models():
    """Endpoint kompatibilitas OpenAI untuk discovery model (/v1/models)."""
    gemini_model = os.getenv("GEMINI_CHAT_MODEL", "gemini-3.1-flash-lite")
    mercury_model = getattr(agent_mercury, "model_name", os.getenv("MERCURY_MODEL", "mercury-3"))
    deepseek_model = getattr(agent_deepseek, "model_name", os.getenv("LARPROUTER_MODEL", "deepseek-v4.1-flash"))
    gemini_avail = bool(os.getenv("GEMINI_API_KEY"))
    mercury_avail = bool(getattr(agent_mercury, "available", False) or os.getenv("MERCURY_API_KEY") or os.getenv("INCEPTION_API_KEY"))
    deepseek_avail = bool(getattr(agent_deepseek, "available", False) or os.getenv("LARPROUTER_API_KEY"))

    data = [
        _format_model_entry("gemini", "google", "gemini", gemini_avail, tools=True, vision=True),
        _format_model_entry("mercury", "inception", "mercury", mercury_avail, tools=True, vision=False),
        _format_model_entry("deepseek", "larprouter", "deepseek", deepseek_avail, tools=True, vision=False),
        _format_model_entry("vienna", "vienna-agent", "vienna", True, tools=True, vision=True),
    ]
    if gemini_model not in ("gemini", ""):
        data.append(_format_model_entry(gemini_model, "google", "gemini", gemini_avail, tools=True, vision=True))
    if mercury_model not in ("mercury", ""):
        data.append(_format_model_entry(mercury_model, "inception", "mercury", mercury_avail, tools=True, vision=False))
    if deepseek_model not in ("deepseek", ""):
        data.append(_format_model_entry(deepseek_model, "larprouter", "deepseek", deepseek_avail, tools=True, vision=False))

    return {
        "object": "list",
        "data": data,
    }


@app.get("/v1/models/{model_id}")
@app.get("/models/{model_id}")
async def get_model(model_id: str):
    """Endpoint kompatibilitas OpenAI untuk detail satu model."""
    models_resp = await list_models()
    for m in models_resp["data"]:
        if m["id"].lower() == model_id.lower():
            return m
    return _format_model_entry(model_id, "vienna-agent", model_id, True, tools=True, vision=False)


def _role_for_user(user_id: str) -> str:
    return "superadmin" if user_id.startswith("admin_") else "customer"


def _engine_for(name: Optional[str]):
    # OpenAI/GPT masih nonaktif (credits habis). Gemini tetap default;
    # Mercury dipilih eksplisit via engine="mercury" (atau nama modelnya).
    normalized = (name or "").strip().lower()
    if normalized in ("mercury", "inception", "mercury-3", "mercury-2.5", "mercury-2", "mercury-voice"):
        return agent_mercury
    if normalized in ("deepseek", "larprouter", "deepseek-v4.1-flash"):
        return agent_deepseek
    return agent_gemini


class NewSessionRequest(BaseModel):
    user_id: str
    engine: Optional[str] = "gemini"  # "gemini" (default), "mercury", atau "deepseek"
    session_id: Optional[str] = None  # bila diisi: arsipkan session ini spesifik


@app.post("/v1/assistant/new-session")
async def assistant_new_session(payload: NewSessionRequest = Body(...), request: Request = None):
    """Mulai session baru (tombol Clear Chat). Session lama diarsip, tidak dihapus."""
    if request is not None:
        limited = await _check_chat_rate_limit(request)
        if limited:
            raise HTTPException(status_code=429, detail="Terlalu banyak permintaan, coba lagi sebentar.")
    agent = _engine_for(payload.engine)
    is_pf = is_portfolio_persona(getattr(agent, "_agent_personality", None))
    try:
        if payload.session_id:
            await archive_session(payload.session_id, is_portfolio=is_pf)
        else:
            # Arsipkan session implicit aktif agar tidak dipakai lagi
            from services.mongo_service import sessions_col as _col
            old = await _col.find_one({"user_id": payload.user_id, "is_active": True})
            if old:
                await archive_session(old["session_id"], is_portfolio=is_pf)
    except Exception as e:
        print(f"[SESSION WARN] Archive on new-session skipped: {e}")
    role = _role_for_user(payload.user_id)
    created = await create_session(payload.user_id, role=role, is_portfolio=is_pf)
    return {"status": "success", "session_id": created["session_id"]}


@app.get("/v1/assistant/sessions")
async def assistant_list_sessions(user_id: str, engine: Optional[str] = "gemini", limit: int = 20, session_id: Optional[str] = None):
    """Daftar session milik user (riwayat percakapan), terbaru dulu.

    Wajib bukti kepemilikan: session_id milik user tersebut. Mencegah
    enumerasi riwayat chat orang lain bermodal user_id.
    """
    agent = _engine_for(engine)
    is_pf = is_portfolio_persona(getattr(agent, "_agent_personality", None))
    if not session_id:
        raise HTTPException(status_code=403, detail="session_id milik user wajib disertakan.")
    try:
        from services.mongo_service import sessions_col as _col, pa_sessions_col as _pa_col
        col = _pa_col if is_pf else _col
        proof = await col.find_one({"session_id": session_id, "user_id": user_id}, {"_id": 0, "session_id": 1})
        if not proof:
            raise HTTPException(status_code=403, detail="session_id tidak cocok dengan user_id.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to verify session: {e}")
    try:
        cursor = col.find({"user_id": user_id}).sort("last_activity", -1).limit(max(1, min(limit, 50)))
        docs = await cursor.to_list(length=50)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list sessions: {e}")
    items = []
    for d in docs:
        sid = d.get("session_id")
        ctx = d.get("context") or {}
        items.append({
            "session_id": sid,
            "is_active": d.get("is_active", True),
            "last_activity": (d.get("last_activity") or d.get("updated_at")),
            "last_message": ctx.get("last_message") or "-",
            "message_count": await count_session_messages(sid, is_portfolio=is_pf),
        })
    return {"status": "success", "sessions": items}



async def process_chat(payload: ChatRequest, current_agent: AgentEngine | GeminiAgentEngine | MercuryAgentEngine | DeepSeekAgentEngine, request: Request = None):
    try:
        messages = payload.messages or []
        user_id = payload.user_id
        session_id = payload.session_id

        if not messages:
            raise HTTPException(status_code=400, detail="Tidak ada pesan dalam permintaan.")

        # Rate limit duluan (lindungi kuota) — sebelum kerja berat apa pun.
        if request is not None:
            limited = await _check_chat_rate_limit(request)
            if limited:
                raise HTTPException(status_code=429, detail="Terlalu banyak permintaan, coba lagi sebentar.")

        # Layar anti-jailbreak: tolak instan TANPA panggil LLM (hemat kuota,
        # deterministik, tidak meracuni memory).
        _raw_last = (messages[-1].content or "")[:USER_INPUT_MAX_CHARS]
        if detect_jailbreak(_raw_last):
            print(f"[SECURITY] Jailbreak attempt blocked (user={user_id}).")
            return {"response": _jailbreak_refusal(payload.lang), "blocked": "jailbreak"}

        # Portfolio = persona Vienna (vienna_ai_assistant) atau legacy robin_ai_assistant
        is_portfolio = False
        if hasattr(current_agent, "_agent_personality") and current_agent._agent_personality:
            is_portfolio = is_portfolio_persona(current_agent._agent_personality)

        # ======================
        # SESSION
        # ======================
        # Role SELALU dari server (user_id) — abaikan payload.role dari client.
        # Mencegah eskalasi: pengunjung anonim tidak bisa mengaku "superadmin".
        role = _role_for_user(user_id)

        session = await get_or_create_session(
            user_id=user_id,
            session_id=session_id,
            role=role,
            is_portfolio=is_portfolio
        )

        session_id = session["session_id"]

        # ======================
        # INPUT HYGIENE (anti spam/gibberish mengotori memory)
        # ======================
        user_text, input_saveable = sanitize_user_input(messages[-1].content)

        # ======================
        # MEMORY + RAG + LOOKUP (paralel — independen satu sama lain,
        # hemat ~0.5-1s dibanding sekuensial sebelum token pertama)
        # ======================
        user_id_str = session.get("user_id")
        collection_to_search = "portfolio_document_chunk" if is_portfolio else "document_chunk"

        async def _load_conversation():
            return await get_messages(session_id=session_id, limit=HISTORY_MSG_LIMIT, is_portfolio=is_portfolio)

        async def _load_retrieval():
            try:
                from services.rag_service import search_documents
                contexts = await search_documents(user_text, limit=3, collection_name=collection_to_search)
                return contexts if contexts else [""]
            except Exception as e:
                print(f"[RAG SEARCH WARNING] Failed to retrieve context: {e}")
                return [""]

        async def _load_user_info():
            if is_portfolio or not user_id_str or str(user_id_str).startswith("guest"):
                return None
            try:
                from services.mongo_service import users_col
                from bson import ObjectId

                user_doc = None
                try:
                    user_doc = await users_col.find_one({"_id": ObjectId(user_id_str)})
                except Exception:
                    pass

                if not user_doc:
                    user_doc = await users_col.find_one({"_id": user_id_str})

                if user_doc:
                    return {
                        "name": user_doc.get("name"),
                        "phone": user_doc.get("phone"),
                        "email": user_doc.get("email"),
                    }
            except Exception as e:
                print(f"[ERROR] Failed to fetch user info for prompt: {e}")
            return None

        async def _load_inventory():
            if not user_id:
                return None
            try:
                inventory_doc = await device_app_inventory_col.find_one({"user_id": user_id})
                if inventory_doc and "apps" in inventory_doc:
                    return inventory_doc["apps"]
            except Exception as e:
                print(f"[ERROR] Failed to fetch device app inventory: {e}")
            return None

        conversation, retrieval, user_info, app_inventory = await asyncio.gather(
            _load_conversation(), _load_retrieval(), _load_user_info(), _load_inventory()
        )
        history_messages = format_conversation(budget_history(conversation))
        if not history_messages:
            history_messages = "No conversation yet."

        # Duplikat kirim ulang (retry jaringan / double-tap): sudah tersimpan, jawab saja.
        saved_users = [m.get("content") or "" for m in conversation if m.get("sender") == "user"]
        if saved_users and user_text == saved_users[-1]:
            input_saveable = False
        # Retry burst: beri catatan agar model tidak menganggapnya penekanan.
        burst = detect_retry_burst(conversation)
        retry_note = ""
        if burst >= 2:
            retry_note = (
                f"\n\n[SYSTEM NOTE: user mengirim {burst + 1} pesan beruntun tanpa jeda jawaban "
                "(kemungkinan retry jaringan). Jawab sekali saja, jangan anggap sebagai penekanan.]"
            )



        # (RAG retrieval, user info, dan device inventory sudah dimuat paralel di atas.)

        prompt = build_prompt(
            session=session,
            history=history_messages,
            retrieval=retrieval,
            new_input=user_text + retry_note,
            user_info=user_info,
            admin_role=role,
            app_inventory=app_inventory
        )
        print(f"[PROMPT] session={session_id} history_chars={len(history_messages)} prompt_chars={len(prompt)}")
        
        # ======================
        # STREAMING WRAPPER
        # ======================
        async def streaming_wrapper():
            full_response = ""
            stream_meta = {}

            try:
                async for chunk in current_agent.chat(prompt, lang=payload.lang, session_id=session_id, user_id=user_id, admin_role=role):

                    text_part = ""

                    # ======================
                    # PARSE SSE CHUNK
                    # ======================
                    if isinstance(chunk, str) and chunk.startswith("data:"):
                        raw = chunk[len("data:"):].strip()

                        if raw == "[DONE]":
                            break

                        try:
                            data = json.loads(raw)
                            if isinstance(data.get("meta"), dict):
                                # Metadata internal engine (model/usage) — tidak
                                # diteruskan ke client; dipakai untuk event done.
                                stream_meta.update(data["meta"])
                                continue
                            text_part = data.get("text", "")
                        except Exception:
                            text_part = ""
                    else:
                        text_part = str(chunk)

                    # ======================
                    # ACCUMULATE RESPONSE
                    # ======================
                    if text_part:
                        full_response += text_part

                    # ======================
                    # STREAM KE CLIENT
                    # ======================
                    yield chunk

                # Terminal event: usage + RAG sources. Frontend lama mengabaikan
                # field asing (hanya baca `text`), frontend baru memakai `usage`
                # sebagai token asli dan `sources` sebagai sitasi.
                try:
                    rag_chunks = [r for r in (retrieval or []) if r and r.strip()]
                    usage = {
                        "model": stream_meta.get("model") or getattr(current_agent, "last_model_used", "gemini-3.1-flash-lite"),
                        "provider": getattr(current_agent, "provider", "gemini"),
                        "prompt_chars": len(prompt),
                        "history_chars": len(history_messages),
                        "response_chars": len(full_response),
                        "prompt_tokens_est": max(1, len(prompt) // 4),
                        "response_tokens_est": max(1, len(full_response) // 4),
                        "rag_chunks": len(rag_chunks),
                    }
                    # Token usage asli dari event meta engine (Mercury). Fallback
                    # ke atribut engine untuk engine lama (Gemini = estimasi).
                    real_usage = stream_meta.get("usage")
                    if not isinstance(real_usage, dict) or not real_usage:
                        real_usage = getattr(current_agent, "last_usage", None)
                    if isinstance(real_usage, dict) and real_usage:
                        usage.update({
                            k: v for k, v in real_usage.items()
                            if k in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens")
                        })
                    sources_preview = [s[:500] for s in rag_chunks[:3]]
                    yield f"data: {json.dumps({'done': True, 'usage': usage, 'sources': sources_preview})}\n\n"
                except Exception as term_err:
                    print(f"[STREAM WARN] Failed to emit usage event: {term_err}")

            except Exception as e:
                # ======================
                # ERROR STREAM KE CLIENT
                # ======================
                try:
                    err_text = str(e) or type(e).__name__  # jangan kirim error kosong
                    yield f"data: {json.dumps({'error': err_text})}\n\n"
                except Exception:
                    yield "data: {\"error\": \"stream_error\"}\n\n"

            finally:
                # ======================
                # SAVE KE DATABASE
                # ======================
                try:
                    # simpan hanya LAST user message (lewati bila gibberish/duplikat)
                    if messages and input_saveable:
                        last_msg = messages[-1]

                        await save_message(
                            session_id=session_id,
                            sender=getattr(last_msg, "role", "user"),
                            content=user_text,
                            is_portfolio=is_portfolio
                        )

                    # simpan response AI (lewati bila input tidak disimpan)
                    if input_saveable and full_response.strip():
                        await save_message(
                            session_id=session_id,
                            sender="assistant",
                            content=full_response,
                            is_portfolio=is_portfolio
                        )

                except Exception as db_error:
                    # aman dari crash karena encoding
                    try:
                        safe_err = str(db_error).encode("utf-8", errors="ignore").decode()
                        print("DB ERROR:", safe_err)
                    except Exception:
                        print("DB ERROR: unknown")

                # Rolling summary: fire-and-forget, tidak menghambat response.
                # maybe_rollup_summary no-op kecuali pesan menyentuh kelipatan 20.
                try:
                    asyncio.create_task(maybe_rollup_summary(session_id, is_portfolio=is_portfolio))
                except Exception as e:
                    print(f"[SUMMARY WARN] Failed to schedule rollup: {e}")

        return StreamingResponse(
            streaming_wrapper(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "X-Session-ID": session_id,
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    import uvicorn

    _port = int(os.getenv("PORT", "8000"))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=_port,
        proxy_headers=True,
    )
