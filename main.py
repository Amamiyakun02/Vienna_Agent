import os
os.environ["OPENBLAS_NUM_THREADS"] = "1"
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
from typing import Optional, List, Dict, Any
from pydantic import BaseModel
from services import get_or_create_session, get_messages, get_session, save_message
from bson import ObjectId
from fastapi import WebSocket, WebSocketDisconnect
from services.connection_manager import manager
from services.mongo_service import devices_col, device_app_inventory_col
from services.redis_service import redis_client


from fastapi.staticfiles import StaticFiles
load_dotenv()
JWT_SECRET = os.getenv("JWT_SECRET", "super-secret-jwt-key-aimer-future-2026-06-02")
JWT_ALGORITHM = "HS256"
app = FastAPI()

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

agent_vienna = AgentEngine(persona_file="agents/vienna.json")
agent_gemini = GeminiAgentEngine(persona_file="agents/vienna.json")

@app.on_event("startup")
async def startup_event():
    import asyncio
    from services.agent_service import get_mcp_tools
    # Pre-fetch and cache MCP tools concurrently during server startup
    asyncio.create_task(get_mcp_tools())


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
    return {"message": "Hello World"}


class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    user_id: str
    session_id: Optional[str] = None  # bisa kosong jika sesi baru
    messages: List[ChatMessage]
    lang: Optional[str] = "id"
    role: Optional[str] = None  # role admin aktif (superadmin/sales)

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
    return await process_chat(payload, agent_vienna)



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

        # Check if the agent is Vienna (robin_ai_assistant)
        is_portfolio = False
        if hasattr(current_agent, "_agent_personality") and current_agent._agent_personality:
            is_portfolio = current_agent._agent_personality.get("assistant_id") == "robin_ai_assistant"

        # ======================
        # SESSION
        # ======================
        role = payload.role
        if not role:
            if user_id.startswith("admin_"):
                role = "superadmin"
            else:
                role = "customer"

        session = await get_or_create_session(
            user_id=user_id,
            session_id=session_id,
            role=role,
            is_portfolio=is_portfolio
        )

        session_id = session["session_id"]

        # ======================
        # MEMORY
        # ======================
        conversation = await get_messages(session_id=session_id, limit=30, is_portfolio=is_portfolio)
        history_messages = format_conversation(conversation) or []



        # ======================
        # RAG RETRIEVAL (QDRANT SEMANTIC SEARCH)
        # ======================
        retrieval = [""]
        try:
            from services.rag_service import search_documents
            collection_to_search = "portfolio_document_chunk" if is_portfolio else "document_chunk"
            retrieval_contexts = await search_documents(messages[-1].content, limit=3, collection_name=collection_to_search)
            if retrieval_contexts:
                retrieval = retrieval_contexts
        except Exception as e:
            print(f"[RAG SEARCH WARNING] Failed to retrieve context: {e}")

        # Fetch complete registered user info if logged in (not anonymous)
        user_info = None
        user_id_str = session.get("user_id")
        if not is_portfolio and user_id_str and not str(user_id_str).startswith("guest"):
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

        # Fetch device app inventory if available
        app_inventory = None
        if user_id:
            try:
                inventory_doc = await device_app_inventory_col.find_one({"user_id": user_id})
                if inventory_doc and "apps" in inventory_doc:
                    app_inventory = inventory_doc["apps"]
            except Exception as e:
                print(f"[ERROR] Failed to fetch device app inventory: {e}")

        prompt = build_prompt(
            session=session,
            history=history_messages,
            retrieval=retrieval,
            new_input=messages[-1].content,
            user_info=user_info,
            admin_role=payload.role,
            app_inventory=app_inventory
        )
        
        # ======================
        # STREAMING WRAPPER
        # ======================
        async def streaming_wrapper():
            full_response = ""

            try:
                async for chunk in current_agent.chat(prompt, lang=payload.lang, session_id=session_id, user_id=user_id, admin_role=payload.role):

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
                            is_portfolio=is_portfolio
                        )

                    # simpan response AI
                    if full_response.strip():
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
