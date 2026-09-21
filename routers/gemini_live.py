import asyncio
import os
import json
import traceback
import base64
import websockets
from datetime import datetime, timezone
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types

# Import services and utils
from services import get_or_create_session, get_messages, save_message
from services.agent_service import get_mcp_tools, execute_device_tool, DEVICE_TOOL_NAMES, _check_sales_authorization
from services.mongo_service import device_app_inventory_col, users_col
from utils.prompt_builder import build_instruction, build_prompt
from bson import ObjectId

router = APIRouter()

cached_persona_data = None
cached_instruction = None

def format_conversation(messages):
    if not messages:
        return "No conversation yet."

    def normalize_timestamp(msg):
        ts = msg.get("timestamp", datetime.now(timezone.utc))
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except ValueError:
                ts = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return {**msg, "timestamp": ts}

    normalized = [normalize_timestamp(m) for m in messages]
    sorted_msgs = sorted(normalized, key=lambda x: x["timestamp"])

    lines = []
    for msg in sorted_msgs:
        time_str = msg["timestamp"].strftime("%H:%M:%S")
        sender = "User" if msg.get("sender") == "user" else "Agent"
        lines.append(f"[{time_str}] {sender:10}: {msg.get('content', '')}")
    return "\n".join(lines)

@router.websocket("/live-voice")
async def websocket_endpoint(client_ws: WebSocket):
    await client_ws.accept()
    print("[Gemini Live] Android client terhubung. Menunggu init payload...")

    gemini_api_key = os.getenv("GEMINI_API_KEY", "")
    if not gemini_api_key:
        print("[Gemini Live] GEMINI_API_KEY is not set.")
        await client_ws.close(code=1011, reason="GEMINI_API_KEY is missing")
        return

    user_id = None
    session_id = None
    role = "customer"

    # Tunggu pesan pertama sebagai inisialisasi
    try:
        init_data = await client_ws.receive_text()
        init_json = json.loads(init_data)
        if init_json.get("type") == "init":
            user_id = init_json.get("user_id")
            session_id = init_json.get("session_id")
            role = init_json.get("role", "customer")
            
            if not user_id:
                print("[Gemini Live] Init payload missing user_id.")
                await client_ws.close(code=1008, reason="Authentication required")
                return
                
            if session_id == "null" or session_id == "":
                session_id = None
            print(f"[Gemini Live] Init diterima. User: {user_id}, Session: {session_id}")
        else:
            print("[Gemini Live] Tidak menerima pesan init yang valid.")
            await client_ws.close(code=1008, reason="Init payload required")
            return
    except Exception as e:
        print(f"[Gemini Live] Gagal menerima init payload: {e}")
        return

    try:
        # 1. Bangun prompt/konteks awal
        is_portfolio = True # Sesuai dengan vienna.json
        session = await get_or_create_session(
            user_id=user_id,
            session_id=session_id,
            role=role,
            is_portfolio=is_portfolio
        )
        session_id = session["session_id"]
        
        conversation = await get_messages(session_id=session_id, limit=10, is_portfolio=is_portfolio)
        history_messages = format_conversation(conversation) or ""

        user_info = None
        if user_id:
            try:
                user_doc = None
                try:
                    user_doc = await users_col.find_one({"_id": ObjectId(user_id)})
                except:
                    user_doc = await users_col.find_one({"_id": user_id})
                if user_doc:
                    user_info = {"name": user_doc.get("name"), "phone": user_doc.get("phone"), "email": user_doc.get("email")}
            except Exception:
                pass

        app_inventory = None
        if user_id:
            try:
                inventory_doc = await device_app_inventory_col.find_one({"user_id": user_id})
                if inventory_doc and "apps" in inventory_doc:
                    app_inventory = inventory_doc["apps"]
                    print(f"[Gemini Live] Loaded {len(app_inventory)} apps from inventory for user {user_id}")
                else:
                    print(f"[Gemini Live] WARNING: No app inventory found for user_id='{user_id}'")
            except Exception as e:
                print(f"[Gemini Live] ERROR loading app inventory: {e}")

        # Load Persona
        global cached_persona_data, cached_instruction
        if cached_persona_data is None:
            try:
                with open("agents/vienna.json", "r", encoding="utf-8") as f:
                    cached_persona_data = json.load(f)
                cached_instruction = build_instruction(cached_persona_data)
            except Exception as e:
                print(f"[Gemini Live] Failed to load persona: {e}")
                cached_instruction = "Anda adalah Vienna."
        instruction = cached_instruction

        # RAG Placeholder
        retrieval = []

        context_prompt = build_prompt(
            session=session,
            history=history_messages,
            retrieval=retrieval,
            new_input="(Sesi Live Voice Dimulai)",
            user_info=user_info,
            admin_role=role,
            app_inventory=app_inventory
        )

        system_instruction_text = f"{instruction}\n\n[CONTEXT DARI SISTEM]\n{context_prompt}\n\nRESPOND IN id-ID. YOU MUST RESPOND UNMISTAKABLY IN id-ID."

        # Ambil tools dan konversi ke types SDK
        all_tools = await get_mcp_tools() or []
        function_declarations = []
        for t in all_tools:
            if "function" in t:
                fn = t["function"]
                function_declarations.append(
                    types.FunctionDeclaration(
                        name=fn.get("name"),
                        description=fn.get("description"),
                        parameters=fn.get("parameters")
                    )
                )
        
        # Tambahkan fungsi RAG search_documents
        function_declarations.append(
            types.FunctionDeclaration(
                name="search_documents",
                description="Pencarian ke database dokumen internal (RAG). Gunakan alat ini jika pengguna menanyakan informasi tentang panduan, referensi internal, atau portofolio.",
                parameters={
                    "type": "OBJECT",
                    "properties": {
                        "query": {
                            "type": "STRING",
                            "description": "Kata kunci pencarian."
                        }
                    },
                    "required": ["query"]
                }
            )
        )
        tools_list = [types.Tool(function_declarations=function_declarations)]

        # Inisialisasi client Google GenAI
        client = genai.Client(api_key=gemini_api_key)
        
        # Konfigurasi Sesi
        config = types.LiveConnectConfig(
            system_instruction=types.Content(parts=[types.Part.from_text(text=system_instruction_text)]),
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Aoede"
                    )
                )
            ),
            tools=tools_list if tools_list else None
        )

        print("[Gemini Live] Menghubungkan ke Google Gemini Live API via SDK...")
        async with client.aio.live.connect(model="gemini-3.1-flash-live-preview", config=config) as gemini_session:
            print("[Gemini Live] Terhubung ke Google Gemini Live API.")
            
            # Flag untuk menandakan koneksi masih aktif
            connection_alive = True
            
            # ===== HEARTBEAT TASK =====
            # Mengirim ping ke Android client setiap 25 detik untuk menjaga koneksi tetap hidup
            async def heartbeat():
                nonlocal connection_alive
                try:
                    while connection_alive:
                        await asyncio.sleep(25)
                        try:
                            await client_ws.send_text(json.dumps({"type": "ping"}))
                        except Exception:
                            connection_alive = False
                            break
                except asyncio.CancelledError:
                    pass
            
            heartbeat_task = asyncio.create_task(heartbeat())

            # ===== LOOP PENERIMA DARI GEMINI =====
            chunk_count_out = 0
            agent_text_chunk = ""

            async def receive_from_gemini():
                nonlocal chunk_count_out, agent_text_chunk, connection_alive
                try:
                    async for response in gemini_session.receive():
                        if not connection_alive:
                            break
                            
                        server_content = response.server_content
                        if server_content is not None:
                            model_turn = server_content.model_turn
                            if model_turn:
                                for part in model_turn.parts:
                                    # Tangkap Text Response
                                    if part.text:
                                        agent_text_chunk += part.text
                                        
                                    # Tangkap Audio Response
                                    if part.inline_data:
                                        base64_audio = base64.b64encode(part.inline_data.data).decode("utf-8")
                                        chunk_count_out += 1
                                        if chunk_count_out % 50 == 0:
                                            print(f"[Gemini Live] (Audio Out) chunk ke-{chunk_count_out}")
                                        try:
                                            await client_ws.send_text(json.dumps({"audio": base64_audio}))
                                        except Exception as send_err:
                                            print(f"[Gemini Live] Gagal mengirim audio ke Android: {send_err}")
                                            connection_alive = False
                                            return

                            if server_content.turn_complete and agent_text_chunk.strip():
                                # Fire-and-forget: simpan ke DB tanpa memblokir audio stream
                                text_to_save = agent_text_chunk.strip()
                                asyncio.create_task(_save_voice_turn(session_id, text_to_save, is_portfolio))
                                
                                # TAMBAHAN: Kirim Teks agar muncul di layar chat frontend
                                try:
                                    await client_ws.send_text(json.dumps({
                                        "type": "chat_message",
                                        "role": "user",
                                        "content": "[Audio Input]"
                                    }))
                                    await client_ws.send_text(json.dumps({
                                        "type": "chat_message",
                                        "role": "assistant",
                                        "content": text_to_save
                                    }))
                                except Exception as e:
                                    print(f"[Gemini Live] Gagal mengirim teks chat ke Android: {e}")

                                agent_text_chunk = ""

                        # Tangkap Function Call (Tool Execution)
                        tool_call = response.tool_call
                        if tool_call:
                            function_responses = []
                            for fc in tool_call.function_calls:
                                tool_name = fc.name
                                tool_args = fc.args or {}
                                call_id = fc.id
                                
                                print(f"[Gemini Live] Tool call: {tool_name}({tool_args})")
                                
                                # Execute tool
                                result_text = ""
                                if tool_name == "search_documents":
                                    query = tool_args.get("query", "")
                                    try:
                                        from services.rag_service import search_documents
                                        collection_to_search = "portfolio_document_chunk" if is_portfolio else "document_chunk"
                                        retrieval_contexts = await search_documents(query, limit=3, collection_name=collection_to_search)
                                        result_text = "\n".join(retrieval_contexts) if retrieval_contexts else "Data tidak ditemukan."
                                    except Exception as err:
                                        result_text = f"Error mencari dokumen: {err}"
                                elif tool_name in DEVICE_TOOL_NAMES:
                                    try:
                                        result_text = await execute_device_tool(user_id, tool_name, tool_args, call_id)
                                    except Exception as err:
                                        result_text = f"Error: {err}"
                                else:
                                    # MCP tools
                                    try:
                                        from mcp import ClientSession as MCPClientSession
                                        from mcp.client.streamable_http import streamablehttp_client
                                        MCP_URL = os.getenv("MCP_URL", "https://agentmcp-service-579f62a8.fastapicloud.dev/mcp")
                                        async with streamablehttp_client(MCP_URL) as (read, write, _):
                                            async with MCPClientSession(read, write) as mcp_session:
                                                await mcp_session.initialize()
                                                result = await mcp_session.call_tool(tool_name, tool_args)
                                                if hasattr(result, "content") and result.content:
                                                    for p in result.content:
                                                        if hasattr(p, "text") and p.text:
                                                            result_text += p.text
                                                        elif isinstance(p, dict) and p.get("type") == "text":
                                                            result_text += p.get("text", "")
                                                else:
                                                    result_text = str(result)
                                    except Exception as err:
                                        result_text = f"Failed to execute MCP tool: {err}"
                                        
                                print(f"[Gemini Live] Tool result ({tool_name}): {result_text[:120]}")
                                
                                # Coba parsing result_text menjadi JSON dictionary
                                try:
                                    result_obj = json.loads(result_text)
                                    if not isinstance(result_obj, dict):
                                        result_obj = {"result": result_text}
                                except Exception:
                                    result_obj = {"result": result_text}
                                
                                # Siapkan respons untuk alat bantu
                                function_responses.append(
                                    types.FunctionResponse(
                                        name=tool_name,
                                        id=call_id,
                                        response=result_obj
                                    )
                                )
                            
                            # Kirim hasil eksekusi kembali ke Gemini
                            if function_responses:
                                try:
                                    # Menggunakan types.LiveClientToolResponse untuk respons tool
                                    tool_response_content = types.LiveClientToolResponse(
                                        function_responses=function_responses
                                    )
                                    await gemini_session.send(input=tool_response_content)
                                except websockets.exceptions.ConnectionClosedError as e:
                                    print(f"[Gemini Live] Koneksi ke Gemini terputus saat kirim tool response: {e}")
                                    connection_alive = False
                                    break

                except Exception as e:
                    print(f"[Gemini Live] Error saat menerima data dari Gemini: {e}")
                    traceback.print_exc()
                    connection_alive = False

            gemini_task = asyncio.create_task(receive_from_gemini())

            # ===== LOOP PENERIMA DARI ANDROID =====
            try:
                chunk_count_in = 0
                while connection_alive:
                    data = await client_ws.receive_text()
                    try:
                        client_input = json.loads(data)
                        
                        # Handle pong dari Android (respons heartbeat)
                        if client_input.get("type") == "pong":
                            continue
                        
                        if "audio" in client_input:
                            chunk_count_in += 1
                            if chunk_count_in % 50 == 0:
                                print(f"[Gemini Live] (Audio In) chunk ke-{chunk_count_in}")
                            
                            audio_bytes = base64.b64decode(client_input["audio"])
                            
                            # Filter out absolute silence frames (sent by Android for echo suppression)
                            # Perfect silence ruins the Server VAD noise floor, causing it to never trigger again.
                            if not any(audio_bytes):
                                continue

                            try:
                                await gemini_session.send_realtime_input(
                                    audio=types.Blob(
                                        data=audio_bytes,
                                        mime_type="audio/pcm;rate=16000"
                                    )
                                )
                            except websockets.exceptions.ConnectionClosedError as e:
                                print(f"[Gemini Live] Koneksi ke Gemini terputus (ConnectionClosedError): {e}")
                                await _notify_client_error(client_ws, "Koneksi ke AI terputus. Silakan coba lagi.")
                                break
                        elif "realtimeInput" in client_input or "realtime_input" in client_input:
                             pass
                    except json.JSONDecodeError:
                        pass
            except WebSocketDisconnect:
                print("[Gemini Live] Client Android disconnect.")
            finally:
                connection_alive = False
                heartbeat_task.cancel()
                gemini_task.cancel()

    except Exception as e:
        print(f"[Gemini Live] Error utama: {e}")
        traceback.print_exc()
        await _notify_client_error(client_ws, f"Server error: {str(e)[:100]}")
        try:
            await client_ws.close()
        except:
            pass


# ===== HELPER FUNCTIONS =====

async def _save_voice_turn(session_id: str, agent_text: str, is_portfolio: bool):
    """Fire-and-forget task untuk menyimpan voice turn ke database tanpa memblokir audio stream."""
    try:
        await save_message(
            session_id=session_id,
            sender="user",
            content="[Audio Input]",
            message_type="audio",
            is_portfolio=is_portfolio
        )
        await save_message(
            session_id=session_id,
            sender="assistant",
            content=agent_text,
            message_type="audio",
            is_portfolio=is_portfolio
        )
        print(f"[Gemini Live] Voice context saved to DB for session {session_id}")
    except Exception as e:
        print(f"[Gemini Live] Gagal menyimpan voice context: {e}")


async def _notify_client_error(client_ws: WebSocket, message: str):
    """Kirim pesan error ke Android sebelum menutup koneksi."""
    try:
        await client_ws.send_text(json.dumps({"error": message}))
    except Exception:
        pass
