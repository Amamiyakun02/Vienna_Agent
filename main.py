import os
import re
import jwt
import json
import html
from datetime import datetime, timezone
from fastapi import FastAPI, Body, BackgroundTasks, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from dotenv import load_dotenv
from services import AgentEngine, GeminiAgentEngine
from services.github_service import sync_github_repositories
from utils.prompt_builder import build_prompt
from typing import Optional, List
from pydantic import BaseModel
from services import get_or_create_session, get_messages, get_session, save_message
from services.mongo_service import products_col, brands_col, categories_col, product_variants_col
from bson import ObjectId

from fastapi.staticfiles import StaticFiles
load_dotenv()
JWT_SECRET = os.getenv("JWT_SECRET", "super-secret-jwt-key-aimer-future-2026-06-02")
JWT_ALGORITHM = "HS256"
app = FastAPI()

# Mount folder public untuk menyimpan & memutar anime secara lokal
os.makedirs("public/anime", exist_ok=True)
app.mount("/public", StaticFiles(directory="public"), name="public")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://amamiyakun02.github.io",
        "https://smart-guide-ai-agent.vercel.app",
        "https://lina-deals.vercel.app",
    ],
    allow_origin_regex=r"https://.*\.trycloudflare\.com",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

agent_robin = AgentEngine(persona_file="agents/robin.json")
agent_lina = AgentEngine(persona_file="agents/Lina.json")
agent_gemini = GeminiAgentEngine(persona_file="agents/robin.json")

@app.on_event("startup")
async def startup_event():
    import asyncio
    from services.agent_service import get_mcp_tools
    # Pre-fetch and cache MCP tools concurrently during server startup
    asyncio.create_task(get_mcp_tools())

from routers.admin_api import router as admin_router
app.include_router(admin_router)

from routers.auth_api import router as auth_router
app.include_router(auth_router)

from routers.pdf_api import router as pdf_router
app.include_router(pdf_router)

from routers.anime_api import router as anime_router
app.include_router(anime_router)



# ──────────────────────────────────────────────
# GLOBAL EXCEPTION HANDLERS (Manual CORS headers injection)
# ──────────────────────────────────────────────
from fastapi import Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException

def add_cors_headers(request: Request, response: JSONResponse) -> JSONResponse:
    origin = request.headers.get("origin")
    if origin:
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
    print("[CRITICAL ERROR] Unhandled exception occurred:")
    print(error_trace)
    response = JSONResponse(
        status_code=500,
        content={
            "detail": f"Internal Server Error: {str(exc)}",
            "type": exc.__class__.__name__,
            "traceback": error_trace
        }
    )
    return add_cors_headers(request, response)

async def keep_alive_spaces():
    import httpx
    import asyncio
    urls = [
        "https://amamiya-kun-removebg.hf.space/",
        "https://amamiya-kun-palmscan.hf.space/"
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

@app.on_event("startup")
async def startup_event():
    import asyncio
    try:
        from services.rag_ingestion_service import initialize_qdrant_collections
        await initialize_qdrant_collections()
    except Exception as e:
        print(f"[ERROR] Error during Qdrant startup initialization: {e}")
    
    # Run the keep-alive background task
    asyncio.create_task(keep_alive_spaces())

@app.on_event("shutdown")
async def shutdown_event():
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

@app.get("/")
async def root():
    return {"message": "Hello World"}


# ---------------------------------------------------------------------------
# Quick-Prompt Suggestions — dapat diedit di sini tanpa menyentuh frontend
# ---------------------------------------------------------------------------
QUICK_PROMPTS_ID = [
    {
        "id": "gaming",
        "icon": "🎮",
        "title": "Rekomendasi HP Gaming",
        "description": "Cari smartphone performa tinggi untuk gaming budget di bawah 5 juta.",
        "prompt": "Bisa rekomendasikan smartphone untuk gaming dengan budget di bawah 5 juta?",
        "color": "indigo",
    },
    {
        "id": "flagship",
        "icon": "⚖️",
        "title": "Bandingkan Flagship",
        "description": "Perbandingan spesifikasi antara iPhone 15 Pro dan Samsung Galaxy S24 Ultra.",
        "prompt": "Apa perbedaan spesifikasi dan keunggulan antara iPhone 15 Pro dengan Samsung Galaxy S24 Ultra?",
        "color": "emerald",
    },
    {
        "id": "budget",
        "icon": "💸",
        "title": "HP Terbaik 2 Jutaan",
        "description": "Cari smartphone terbaik dan terkini dengan budget maksimal 2 juta.",
        "prompt": "Rekomendasikan HP terbaik yang ada di toko dengan budget maksimal 2 juta rupiah.",
        "color": "violet",
    },
    {
        "id": "camera",
        "icon": "📸",
        "title": "HP Kamera Terbaik",
        "description": "Smartphone dengan kamera terbaik untuk foto dan video profesional.",
        "prompt": "HP apa yang punya kamera terbaik di toko Aimer untuk fotografi dan video?",
        "color": "rose",
    },
]

QUICK_PROMPTS_EN = [
    {
        "id": "gaming",
        "icon": "🎮",
        "title": "Gaming Phone Picks",
        "description": "Find high-performance smartphones for gaming under 5 million IDR.",
        "prompt": "Can you recommend a smartphone for gaming with a budget under 5 million IDR?",
        "color": "indigo",
    },
    {
        "id": "flagship",
        "icon": "⚖️",
        "title": "Compare Flagships",
        "description": "Compare specs between iPhone 15 Pro and Samsung Galaxy S24 Ultra.",
        "prompt": "What are the spec differences and advantages between iPhone 15 Pro and Samsung Galaxy S24 Ultra?",
        "color": "emerald",
    },
    {
        "id": "budget",
        "icon": "💸",
        "title": "Best Phone under 2M",
        "description": "Find the best and latest smartphones with a maximum budget of 2 million IDR.",
        "prompt": "Recommend the best phone available in the store with a maximum budget of 2 million IDR.",
        "color": "violet",
    },
    {
        "id": "camera",
        "icon": "📸",
        "title": "Best Camera Phone",
        "description": "Smartphones with the best camera for professional photos and videos.",
        "prompt": "Which phone has the best camera in the Aimer store for photography and video?",
        "color": "rose",
    },
]

@app.get("/v1/agent/quick-prompts")
async def get_quick_prompts(lang: Optional[str] = "id"):
    """
    Mengembalikan daftar quick-prompt suggestion yang ditampilkan di halaman awal chatbot.
    Data dapat dikonfigurasi langsung di variabel QUICK_PROMPTS pada main.py.
    """
    if lang == "en":
        return {"prompts": QUICK_PROMPTS_EN}
    return {"prompts": QUICK_PROMPTS_ID}


@app.get("/v1/products/batch")
async def get_products_batch(ids: str):
    """
    Mengambil batch data produk dari MongoDB Atlas berdasarkan daftar ID terpisah koma,
    lalu me-resolve brand, kategori, spesifikasi, dan warna HEX untuk dikirimkan ke frontend.
    """
    id_list = [i.strip() for i in ids.split(",") if i.strip()]
    if not id_list:
        return {"items": []}

    mongo_ids = []
    for pid in id_list:
        try:
            if len(pid) == 24:
                mongo_ids.append(ObjectId(pid))
            else:
                mongo_ids.append(pid)
        except Exception:
            mongo_ids.append(pid)

    try:
        cursor = products_col.find({"_id": {"$in": mongo_ids}})
        raw_products = await cursor.to_list(length=len(mongo_ids))
    except Exception as e:
        return {"items": [], "error": f"Database query failed: {e}"}

    # Urutkan hasil agar sesuai dengan urutan ID yang di-request oleh user
    product_map = {str(p["_id"]): p for p in raw_products}
    ordered_products = [product_map[pid] for pid in id_list if pid in product_map]

    items = []

    def _format_rupiah(price: float) -> str:
        try:
            return f"Rp {int(price):,}".replace(",", ".")
        except Exception:
            return str(price)

    def _safe_str(val) -> str:
        if val is None:
            return ""
        if isinstance(val, list):
            return ", ".join(str(v) for v in val if v)
        return str(val)

    async def _resolve_name(col, field_id: str) -> str:
        try:
            if len(field_id) == 24:
                doc = await col.find_one({"_id": ObjectId(field_id)})
            else:
                doc = None
            if not doc:
                doc = await col.find_one({"$or": [{"name": field_id}, {"slug": field_id}]})
            return doc.get("name", field_id) if doc else field_id
        except Exception:
            return field_id

    for p in ordered_products:
        pid_str = str(p.get("_id", ""))
        brand_id = _safe_str(p.get("brand_id", ""))
        brand_name = await _resolve_name(brands_col, brand_id) if brand_id else "Unknown"

        cat_id = _safe_str(p.get("category_id", ""))
        cat_name = await _resolve_name(categories_col, cat_id) if cat_id else ""

        specs_raw = p.get("specs") or {}
        specs = {
            "screen":    _safe_str(specs_raw.get("display") or specs_raw.get("screen", "-")),
            "processor": _safe_str(specs_raw.get("processor", "-")),
            "camera":    _safe_str(specs_raw.get("camera_main") or specs_raw.get("camera", "-")),
            "battery":   _safe_str(specs_raw.get("battery", "-")),
        }

        tags = []
        if cat_name:
            tags.append(cat_name)
        if specs_raw.get("five_g") or specs_raw.get("5g"):
            tags.append("5G")
        if specs_raw.get("nfc"):
            tags.append("NFC")
        if not tags:
            tags.append("Toko Aimer")

        base_price = p.get("base_price", 0)
        price_str = _format_rupiah(base_price)

        images = p.get("images") or []
        image_url = images[0] if images else (
            f"https://placehold.co/300x300/e2e8f0/475569?text={p.get('name','Product')[:12].replace(' ','+')}"
        )

        colors = []
        try:
            v_filter = {"$or": [{"product_id": pid_str}, {"product_id": p.get("_id")}]}
            v_cursor = product_variants_col.find(v_filter).limit(6)
            variants = await v_cursor.to_list(length=6)
            seen_colors = set()
            for v in variants:
                color_name = v.get("color", "")
                if color_name and color_name not in seen_colors:
                    seen_colors.add(color_name)
                    hex_map = {
                        "black": "#1C1C1E", "hitam": "#1C1C1E",
                        "white": "#F5F5F7", "putih": "#F5F5F7",
                        "blue": "#2E3B4E", "biru": "#2E3B4E",
                        "red": "#C41E3A", "merah": "#C41E3A",
                        "green": "#1A6B3A", "hijau": "#1A6B3A",
                        "gold": "#C8A951", "emas": "#C8A951",
                        "silver": "#C0C0C0", "abu": "#8E8E93",
                        "gray": "#8E8E93", "grey": "#8E8E93",
                        "purple": "#6B4FA0", "ungu": "#6B4FA0",
                        "yellow": "#F2D06B", "kuning": "#F2D06B",
                        "pink": "#F4A7C3", "titanium": "#A8A7A3"
                    }
                    hex_color = hex_map.get(color_name.lower(), "#8E8E93")
                    colors.append({"name": color_name, "hex": hex_color})
        except Exception:
            pass

        if not colors:
            colors = [{"name": "Default", "hex": "#8E8E93"}]

        items.append({
            "id":           pid_str,
            "name":         p.get("name", "Unknown Product"),
            "brand":        brand_name,
            "price":        price_str,
            "rating":       round(float(p.get("avg_rating", 0) or 0), 1),
            "reviewsCount": int(p.get("total_reviews", 0) or 0),
            "specs":        specs,
            "tags":         tags,
            "image":        image_url,
            "colors":       colors
        })

    return {"items": items}


class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    user_id: str
    session_id: Optional[str] = None  # bisa kosong jika sesi baru
    messages: List[ChatMessage]
    lang: Optional[str] = "id"

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
    ts = msg["timestamp"]

    # Jika string → parse
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            ts = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")

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
        sender = "User" if msg["sender"] == "user" else "Agent"
        lines.append(f"[{time_str}] {sender:10}: {msg['content']}")
    return "\n".join(lines)

@app.post("/v1/assistant/chat")
async def assistant_chat(payload: ChatRequest = Body(...)):
    return await process_chat(payload, agent_robin)

@app.post("/v1/agent/chat")
async def agent_chat(
    payload: ChatRequest = Body(...),
    authorization: Optional[str] = Header(None)
):
    # Jika header Authorization dikirimkan, verifikasi token JWT
    if authorization:
        try:
            if not authorization.startswith("Bearer "):
                raise HTTPException(status_code=401, detail="Format header otorisasi harus Bearer <token>")
            
            token = authorization.split(" ")[1]
            try:
                decoded = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
                user_id = decoded.get("id")
                if not user_id:
                    raise HTTPException(status_code=401, detail="Token tidak valid: ID Pengguna tidak ditemukan.")
                
                # Periksa status aktif user di Redis/MongoDB untuk keamanan tambahan
                from services.redis_service import redis_client
                from services.mongo_service import users_col
                from bson import ObjectId
                try:
                    cache_key = f"user_status:{user_id}"
                    cached_status = await redis_client.get(cache_key)
                    if cached_status:
                        status_data = json.loads(cached_status)
                        is_active = status_data.get("is_active", True)
                        exists = status_data.get("exists", True)
                        if not exists:
                            raise HTTPException(status_code=401, detail="Akun tidak ditemukan.")
                        if not is_active:
                            raise HTTPException(status_code=403, detail="Akun Anda dinonaktifkan oleh administrator.")
                    else:
                        user_doc = await users_col.find_one({"_id": ObjectId(user_id)})
                        if not user_doc:
                            await redis_client.setex(cache_key, 300, json.dumps({"exists": False}))
                            raise HTTPException(status_code=401, detail="Akun tidak ditemukan.")
                        
                        is_active = user_doc.get("is_active", True)
                        await redis_client.setex(cache_key, 300, json.dumps({"exists": True, "is_active": is_active}))
                        
                        if not is_active:
                            raise HTTPException(status_code=403, detail="Akun Anda dinonaktifkan oleh administrator.")
                except HTTPException as he:
                    raise he
                except Exception:
                    # Abaikan error format ObjectId agar tidak memblokir sesi tamu
                    pass
                    
            except jwt.ExpiredSignatureError:
                raise HTTPException(status_code=401, detail="Sesi masuk Anda telah kadaluarsa. Silakan masuk kembali.")
            except jwt.InvalidTokenError as e:
                raise HTTPException(status_code=401, detail=f"Otentikasi gagal: {e}")
        except HTTPException as he:
            raise he
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"Otorisasi gagal: {str(e)}")

    return await process_chat(payload, agent_lina)

@app.post("/v1/gemini/chat")
async def gemini_chat(payload: ChatRequest = Body(...)):
    return await process_chat(payload, agent_gemini)

async def process_chat(payload: ChatRequest, current_agent: AgentEngine | GeminiAgentEngine):
    try:
        messages = payload.messages or []
        user_id = payload.user_id
        session_id = payload.session_id

        if not messages:
            return {"error": "Tidak ada pesan dalam permintaan."}

        # ======================
        # SESSION
        # ======================
        session = await get_or_create_session(
            user_id=user_id,
            session_id=session_id
        )

        session_id = session["session_id"]

        # ======================
        # MEMORY
        # ======================
        conversation = await get_messages(session_id=session_id, limit=30)
        history_messages = format_conversation(conversation) or []



        # ======================
        # RAG RETRIEVAL (QDRANT SEMANTIC SEARCH)
        # ======================
        retrieval = [""]
        try:
            from services.rag_service import search_documents
            retrieval_contexts = await search_documents(messages[-1].content, limit=3)
            if retrieval_contexts:
                retrieval = retrieval_contexts
        except Exception as e:
            print(f"[RAG SEARCH WARNING] Failed to retrieve context: {e}")

        # Fetch complete registered user info if logged in (not anonymous)
        user_info = None
        user_id_str = session.get("user_id")
        if user_id_str and not str(user_id_str).startswith("guest"):
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
                    user_info = {
                        "name": user_doc.get("name"),
                        "phone": user_doc.get("phone"),
                        "email": user_doc.get("email"),
                    }
            except Exception as e:
                print(f"[ERROR] Failed to fetch user info for prompt: {e}")

        prompt = build_prompt(
            session=session,
            history=history_messages,
            retrieval=retrieval,
            new_input=messages[-1].content,
            user_info=user_info
        )
        
        # ======================
        # STREAMING WRAPPER
        # ======================
        async def streaming_wrapper():
            full_response = ""

            try:
                async for chunk in current_agent.chat(prompt, lang=payload.lang, session_id=session_id):

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

            except Exception as e:
                # ======================
                # ERROR STREAM KE CLIENT
                # ======================
                try:
                    yield f"data: {json.dumps({'error': str(e)})}\n\n"
                except Exception:
                    yield "data: {\"error\": \"stream_error\"}\n\n"

            finally:
                # ======================
                # SAVE KE DATABASE
                # ======================
                try:
                    # simpan hanya LAST user message
                    if messages:
                        last_msg = messages[-1]

                        await save_message(
                            session_id=session_id,
                            sender=getattr(last_msg, "role", "user"),
                            content=getattr(last_msg, "content", ""),
                        )

                    # simpan response AI
                    if full_response.strip():
                        await save_message(
                            session_id=session_id,
                            sender="assistant",
                            content=full_response,
                        )

                except Exception as db_error:
                    # aman dari crash karena encoding
                    try:
                        safe_err = str(db_error).encode("utf-8", errors="ignore").decode()
                        print("DB ERROR:", safe_err)
                    except Exception:
                        print("DB ERROR: unknown")

        return StreamingResponse(
            streaming_wrapper(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )

    except Exception as e:
        return {"error": str(e)}


# FONNTE_API_TOKEN = os.getenv("WHATSAPP_GATEWAY_TOKEN")
# async def send_fonnte_message(target: str, message: str):
#     url = "https://api.fonnte.com/send"
#     headers = {
#         "Authorization": FONNTE_API_TOKEN,
#         "Content-Type": "application/json"
#     }
#     payload = {
#         "target": target,
#         "message": message
#     }
#     async with httpx.AsyncClient() as client:
#         response = await client.post(url, headers=headers, json=payload)
#         print("Fonnte response:", response.json())
#         return response.json()

# @app.api_route("/webhook", methods=["POST", "GET"])
# async def get_webhook(request: Request):
#     if request.method == "GET":
#         return JSONResponse(content={"status": "Webhook aktif"})
#     try:
#         data = await request.json()
#         print("Webhook received:", data)

#         sender = data.get("sender")
#         message = data.get("message", "").strip()

#         if not sender or not message:
#             return JSONResponse(status_code=400, content={"error": "sender/message kosong"})

#         user_id = sender  # anggap nomor WA sebagai user_id
#         session_id = str(uuid4())

#         # === Simpan session baru ===
#         sessions_col.insert_one({
#             "session_id": session_id,
#             "user_id": user_id,
#             "created_at": datetime.now()
#         })