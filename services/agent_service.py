import os
os.environ["OPENBLAS_NUM_THREADS"] = "1"
from openai import AsyncOpenAI
import json
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from google import genai
from google.genai import types
from datetime import datetime

from utils import build_instruction
from services.connection_manager import manager
from services.mongo_service import devices_col, redis_client, device_app_inventory_col, users_col
from services.firebase_service import send_fcm_data_message

# MCP SERVICE
MCP_URL = os.getenv("MCP_URL", "https://agentmcp-service-579f62a8.fastapicloud.dev/mcp")

# Cache global untuk menyimpan daftar tools dari MCP server
_mcp_tools_cache = None
_mcp_tools_lock = asyncio.Lock()

DEVICE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Buka aplikasi secara umum di HP Android user menggunakan nama package. Gunakan ini HANYA untuk sekadar membuka aplikasi, BUKAN untuk memutar musik/media.",
            "parameters": {
                "type": "object",
                "properties": {
                    "package_name": {
                        "type": "string",
                        "description": "Nama package aplikasi Android (contoh: 'com.whatsapp', 'com.google.android.gm')."
                    }
                },
                "required": ["package_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Melihat daftar berkas/file di folder penyimpanan HP Android user.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Membaca isi berkas/file teks dari HP Android user menggunakan URI path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uri": {
                        "type": "string",
                        "description": "URI path berkas (contoh: content://com.android.providers.downloads.documents/document/raw%3A%2Fstorage%2Femulated%2F0%2FDownload%2Fnotes.txt)."
                    }
                },
                "required": ["uri"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_latest_photo",
            "description": "Mengambil info URI foto terakhir yang ada di galeri/camera roll HP Android user.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "click_text",
            "description": "Mensimulasikan klik pada teks antarmuka (UI) tertentu yang tampil di layar HP Android user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Teks tombol atau elemen UI yang ingin diklik."
                    }
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "input_text",
            "description": "Memasukkan teks ke kolom input yang sedang aktif/fokus di HP Android user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "Teks yang ingin dimasukkan."
                    }
                },
                "required": ["value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_installed_apps",
            "description": "Melihat daftar aplikasi yang terinstal di HP Android user beserta package name-nya.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_screen",
            "description": "Membaca semua teks, tombol, dan elemen UI yang sedang tampil di layar HP Android saat ini.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "spotify_get_current_song",
            "description": "Melihat lagu apa yang sedang diputar oleh user di Spotify saat ini (judul, artis, album, status playing/paused).",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    # {
    #     "type": "function",
    #     "function": {
    #         "name": "spotify_get_playlists",
    #         "description": "Melihat daftar playlist Spotify milik user.",
    #         "parameters": {
    #             "type": "object",
    #             "properties": {}
    #         }
    #     }
    # },
    # {
    #     "type": "function",
    #     "function": {
    #         "name": "spotify_get_top_artists",
    #         "description": "Melihat daftar artis atau musisi yang paling sering didengarkan oleh user di Spotify.",
    #         "parameters": {
    #             "type": "object",
    #             "properties": {}
    #         }
    #     }
    # },
    # {
    #     "type": "function",
    #     "function": {
    #         "name": "spotify_get_recent_tracks",
    #         "description": "Melihat riwayat lagu-lagu yang baru saja diputar oleh user di Spotify.",
    #         "parameters": {
    #             "type": "object",
    #             "properties": {}
    #         }
    #     }
    # },
    {
        "type": "function",
        "function": {
            "name": "spotify_play_music",
            "description": "Memutar musik KHUSUS di Spotify (Play). Gunakan ini jika user meminta memutar atau mencari lagu/musik. Bisa dilampirkan query berupa URI atau judul lagu.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Judul lagu, nama artis, atau URI Spotify. Kosongkan jika hanya ingin me-resume lagu yang dipause."
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "spotify_pause_music",
            "description": "Menjeda (Pause) musik yang sedang berputar di Spotify.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "spotify_next_track",
            "description": "Melompati (Next/Skip) ke lagu berikutnya di Spotify.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "spotify_previous_track",
            "description": "Kembali ke lagu sebelumnya di Spotify.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    }
]

DEVICE_TOOL_NAMES = {"open_app", "list_files", "read_file", "get_latest_photo", "click_text", "input_text", "list_installed_apps", "read_screen", "spotify_get_current_song", "spotify_get_playlists", "spotify_get_top_artists", "spotify_get_recent_tracks", "spotify_play_music", "spotify_pause_music", "spotify_next_track", "spotify_previous_track"}

async def execute_device_tool(user_id: str, tool_name: str, tool_args: dict, tool_call_id: str) -> str:
    if not user_id:
        return json.dumps({"status": "failed", "message": "Missing user_id parameter for device tool execution."})
        
    if tool_name == "list_installed_apps":
        inventory = await device_app_inventory_col.find_one({"user_id": user_id})
        if inventory and "apps" in inventory:
            apps_list = [{"label": app.get("label"), "package_name": app.get("packageName")} for app in inventory["apps"]]
            return json.dumps({"status": "success", "apps": apps_list})
        else:
            return json.dumps({"status": "failed", "message": "Inventory aplikasi belum disinkronkan."})

    action_type = tool_name
    target = None
    value = None
    
    if tool_name == "open_app":
        target = tool_args.get("package_name")
    elif tool_name == "click_text":
        action_type = "click"
        target = tool_args.get("text")
    elif tool_name == "input_text":
        value = tool_args.get("value")
    elif tool_name == "read_file":
        target = tool_args.get("uri")
    elif tool_name == "read_screen":
        action_type = "read_screen"
    elif tool_name == "spotify_play_music":
        action_type = "play_music"
        value = tool_args.get("query", "")
    elif tool_name == "spotify_pause_music":
        action_type = "pause_music"
    elif tool_name == "spotify_next_track":
        action_type = "next_track"
    elif tool_name == "spotify_previous_track":
        action_type = "previous_track"
    elif tool_name == "spotify_get_current_song":
        action_type = "get_current_song"
    # Dummy data untuk tools yang memerlukan Spotify Web API (belum aktif)
    elif tool_name in ["spotify_get_playlists", "spotify_get_top_artists", "spotify_get_recent_tracks"]:
        try:
            if tool_name == "spotify_get_playlists":
                playlists = ["My Favorite Lofi", "Coding Focus", "Top Hits 2026", "Anime OSTs"]
                return json.dumps({"status": "success", "result": playlists})
            elif tool_name == "spotify_get_top_artists":
                artists = ["Rick Astley", "Lofi Girl", "Aimer", "Vaundy", "YOASOBI"]
                return json.dumps({"status": "success", "result": artists})
            elif tool_name == "spotify_get_recent_tracks":
                tracks = [
                    "Idol - YOASOBI | Tautan: https://open.spotify.com/track/7mhwwez139P9TGBvQ58z1s",
                    "Never Gonna Give You Up - Rick Astley | Tautan: https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
                    "Zankyou Sanka - Aimer | Tautan: https://open.spotify.com/track/11bFwKACw8lB9vW5yA17C0"
                ]
                return json.dumps({"status": "success", "result": tracks})
        except Exception as e:
            return json.dumps({"status": "failed", "message": f"Error mengakses Spotify: {str(e)}"})
        
    payload = {
        "tool_call_id": tool_call_id,
        "action": action_type,
        "params": {k: v for k, v in {"target": target, "value": value}.items() if v is not None}
    }
    payload_str = json.dumps(payload)
    
    # 1. Kirim via WebSocket manager
    ws_sent = await manager.send_to_user(user_id, payload_str)
    
    # 2. Jika WebSocket offline, gunakan FCM push data
    if not ws_sent:
        print(f"[DEVICE TOOL] WebSocket offline untuk {user_id}. Mengirim FCM wake-up trigger.")
        device_doc = await devices_col.find_one({"user_id": user_id})
        if device_doc and device_doc.get("fcm_token"):
            fcm_token = device_doc["fcm_token"]
            send_fcm_data_message(fcm_token, {"payload": payload_str})
        else:
            return json.dumps({"status": "failed", "message": "Device WebSocket offline dan tidak ada token FCM terdaftar."})
            
    # 3. Tunggu respon hasil di Redis
    timeout = 30  # detik
    start_time = datetime.now()
    while (datetime.now() - start_time).total_seconds() < timeout:
        result_data = await redis_client.get(f"tool_result:{tool_call_id}")
        if result_data:
            await redis_client.delete(f"tool_result:{tool_call_id}")
            res_json = json.loads(result_data)
            
            status = res_json.get("status", "failed")
            if status == "success":
                # client return value can be mapping or in result field
                result_map = res_json.get("result") or res_json
                clean_result = {k: v for k, v in result_map.items() if k not in ["tool_call_id", "status"]}
                
                # Untuk spotify_play_music: instruksi agar AI menyertakan info lagu di respons
                if tool_name == "spotify_play_music":
                    query_display = value if value else "musik"
                    clean_result["instruction"] = f"Musik '{query_display}' sedang diputar di Spotify. Beritahu user bahwa lagunya sudah diputar."
                    
                return json.dumps({"status": "success", "result": clean_result})
            else:
                return json.dumps({"status": "failed", "message": res_json.get("message", "Gagal mengeksekusi aksi di HP.")})
        await asyncio.sleep(0.5)
        
    return json.dumps({"status": "failed", "message": "Batas waktu (timeout) terlampaui saat menunggu respon HP Android."})

async def get_mcp_tools():
    global _mcp_tools_cache
    if _mcp_tools_cache is not None:
        return _mcp_tools_cache

    async with _mcp_tools_lock:
        if _mcp_tools_cache is not None:
            return _mcp_tools_cache
            
        print("[MCP CACHE] Fetching tools from MCP server for the first time...")
        try:
            async with streamablehttp_client(MCP_URL) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    mcp_tools_resp = await session.list_tools()
                    tools = []
                    for t in mcp_tools_resp.tools:
                        tools.append({
                            "type": "function",
                            "function": {
                               "name": t.name,
                               "description": t.description,
                               "parameters": t.inputSchema
                            }
                        })
                    _mcp_tools_cache = tools + DEVICE_TOOLS
                    print(f"[MCP CACHE] Successfully cached {len(_mcp_tools_cache)} tools (including device tools)!")
                    return _mcp_tools_cache
        except Exception as e:
            print(f"[MCP CACHE ERROR] Failed to fetch tools from MCP server: {e}")
            return DEVICE_TOOLS


_embedding_provider = None  # "openai" atau "gemini"
_tool_embeddings_cache = {}  # Map: tool_name -> list[float]
_tool_embeddings_lock = asyncio.Lock()

async def _get_text_embedding(text: str) -> list[float]:
    global _embedding_provider
    openai_key = os.getenv("OPENAI_API_KEY")
    gemini_key = os.getenv("GEMINI_API_KEY")
    
    if _embedding_provider is None:
        if openai_key:
            _embedding_provider = "openai"
        elif gemini_key:
            _embedding_provider = "gemini"
            
    if _embedding_provider == "openai" and openai_key:
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                res = await client.post(
                    "https://api.openai.com/v1/embeddings",
                    headers={
                        "Authorization": f"Bearer {openai_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "input": text,
                        "model": "text-embedding-3-small"
                    },
                    timeout=5.0
                )
                res.raise_for_status()
                return res.json()["data"][0]["embedding"]
        except Exception as e:
            print(f"[SEMANTIC FILTER WARN] Gagal mendapatkan OpenAI embedding: {e}")
            if gemini_key:
                _embedding_provider = "gemini"
                _tool_embeddings_cache.clear()  # Bersihkan cache agar re-embed dengan Gemini
                # Lanjutkan dengan mencoba mengambil embedding menggunakan Gemini
                try:
                    import httpx
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent?key={gemini_key}"
                    async with httpx.AsyncClient() as client:
                        res = await client.post(
                            url,
                            json={
                                "model": "models/gemini-embedding-001",
                                "content": {
                                    "parts": [{"text": text}]
                                }
                            },
                            timeout=5.0
                        )
                        res.raise_for_status()
                        return res.json()["embedding"]["values"]
                except Exception as ge:
                    print(f"[SEMANTIC FILTER WARN] Gagal mendapatkan Gemini embedding setelah fallback: {ge}")
            
    if (_embedding_provider == "gemini" or gemini_key) and gemini_key:
        try:
            import httpx
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent?key={gemini_key}"
            async with httpx.AsyncClient() as client:
                res = await client.post(
                    url,
                    json={
                        "model": "models/gemini-embedding-001",
                        "content": {
                            "parts": [{"text": text}]
                        }
                    },
                    timeout=5.0
                )
                res.raise_for_status()
                return res.json()["embedding"]["values"]
        except Exception as e:
            print(f"[SEMANTIC FILTER WARN] Gagal mendapatkan Gemini embedding: {e}")
            
    return []

async def _prepare_tool_embeddings(all_tools: list):
    global _tool_embeddings_cache
    missing_tools = [
        t for t in all_tools
        if (t["function"]["name"] if "function" in t else t.get("name")) not in _tool_embeddings_cache
    ]
    if not missing_tools:
        return
        
    async with _tool_embeddings_lock:
        tasks = []
        names = []
        for t in missing_tools:
            name = t["function"]["name"] if "function" in t else t.get("name")
            if name in _tool_embeddings_cache:
                continue
            desc = t["function"]["description"] if "function" in t else t.get("description", "")
            text_to_embed = f"tool name: {name}\ndescription: {desc}"
            tasks.append(_get_text_embedding(text_to_embed))
            names.append(name)
            
        if not tasks:
            return
            
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for name, emb in zip(names, results):
            if isinstance(emb, list) and emb:
                _tool_embeddings_cache[name] = emb

def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    import math
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot_product = sum(x * y for x, y in zip(v1, v2))
    norm_v1 = math.sqrt(sum(x * x for x in v1))
    norm_v2 = math.sqrt(sum(x * x for x in v2))
    if norm_v1 == 0.0 or norm_v2 == 0.0:
        return 0.0
    return dot_product / (norm_v1 * norm_v2)

async def _filter_relevant_tools(prompt: str, all_tools: list, always_include_device_tools: bool = False) -> list:
    """Filter tools based on semantic similarity of prompt and tool description."""
    # 1. Dapatkan embedding dari prompt
    prompt_emb = await _get_text_embedding(prompt)
    if not prompt_emb:
        print("[SEMANTIC FILTER] Bypassing filter karena kegagalan pembuatan prompt embedding.")
        return all_tools
        
    # 2. Pastikan embedding untuk seluruh tools sudah siap di cache
    await _prepare_tool_embeddings(all_tools)
    
    # 3. Hitung similarity untuk masing-masing tool
    scored_tools = []
    
    # Tools wajib (system / general tools)
    always_include_names = {
        "get_current_time", "calculate", "update_session_context", 
        "waktu_sekarang", "info_sistem"
    }
    
    if always_include_device_tools:
        always_include_names.update([
            "open_app", "list_files", "read_file", "get_latest_photo", 
            "click_text", "input_text", "list_installed_apps", "read_screen"
        ])
        
    for t in all_tools:
        name = t["function"]["name"] if "function" in t else t.get("name")
        if name in always_include_names:
            scored_tools.append((t, 1.0))
            continue
            
        tool_emb = _tool_embeddings_cache.get(name)
        if not tool_emb:
            scored_tools.append((t, 0.25))
            continue
            
        score = cosine_similarity(prompt_emb, tool_emb)
        scored_tools.append((t, score))
        
    # Urutkan berdasarkan skor tertinggi
    scored_tools.sort(key=lambda x: x[1], reverse=True)
    
    # Hitung threshold dinamis berdasarkan skor tertinggi tools non-wajib
    non_always_include_scores = [
        score for t, score in scored_tools 
        if (t["function"]["name"] if "function" in t else t.get("name")) not in always_include_names
    ]
    max_non_always_score = max(non_always_include_scores) if non_always_include_scores else 0.0
    
    # Threshold dihitung dinamis (skor tertinggi dikurangi margin 0.10) dengan batas minimal 0.25
    threshold = max(max_non_always_score - 0.10, 0.25)
    filtered = []
    
    for t, score in scored_tools:
        name = t["function"]["name"] if "function" in t else t.get("name")
        if score >= threshold or name in always_include_names:
            filtered.append(t)
            
    if len(filtered) < 8:
        for t, score in scored_tools:
            if t not in filtered:
                filtered.append(t)
            if len(filtered) >= 8:
                break
                
    print(f"[SEMANTIC FILTER] Optimalisasi Aktif! Memangkas context window: {len(filtered)} dari {len(all_tools)} tools dimuat. (Threshold: {threshold:.4f}, Max Score: {max_non_always_score:.4f})")
    return filtered

# ---------------------------------------------------------------------------
# ROLE-BASED ACCESS CONTROL (RBAC) â€” Safety Guard untuk Sales
# ---------------------------------------------------------------------------
# Koleksi yang boleh dibaca (READ) oleh sales
_SALES_READABLE_COLLECTIONS = {"brands", "categories", "products", "product_variants", "bookings"}

# Tools yang DILARANG KERAS dipanggil oleh sales (operasi write/admin)
_SALES_BLOCKED_TOOLS = {"db_tambah", "db_tambah_banyak", "db_hapus", "scrape_gadget", "db_index_products"}

def _check_sales_authorization(tool_name: str, tool_args: dict) -> tuple[bool, str]:
    """
    Periksa apakah tool call diizinkan untuk role 'sales'.
    Returns: (is_authorized, error_message)
    """
    # 1. Blokir tools write yang dilarang keras
    if tool_name in _SALES_BLOCKED_TOOLS:
        return False, (
            f"Otorisasi ditolak: Akun Sales tidak memiliki izin untuk menggunakan operasi '{tool_name}'. "
            "Operasi penambahan, penghapusan data, scraping, dan indexing hanya dapat dilakukan oleh Superadmin."
        )

    # 2. Untuk db_update â€” hanya boleh pada koleksi 'bookings'
    if tool_name == "db_update":
        collection = tool_args.get("collection", "")
        if collection != "bookings":
            return False, (
                f"Otorisasi ditolak: Akun Sales hanya diizinkan mengubah data pada koleksi 'bookings'. "
                f"Perubahan pada koleksi '{collection}' memerlukan otorisasi Superadmin."
            )

    # 3. Untuk operasi read (db_cari, db_hitung, db_agregasi) â€” batasi koleksi
    if tool_name in {"db_cari", "db_hitung", "db_agregasi"}:
        collection = tool_args.get("collection", "")
        if collection and collection not in _SALES_READABLE_COLLECTIONS:
            return False, (
                f"Otorisasi ditolak: Akun Sales tidak diizinkan mengakses koleksi '{collection}'. "
                f"Koleksi yang diizinkan: {', '.join(sorted(_SALES_READABLE_COLLECTIONS))}."
            )

    # 4. db_list_collections dan db_ping â€” diizinkan (read-only system info)
    # 5. cari_produk, show_product_card, buat_booking â€” diizinkan
    # 6. waktu_sekarang, info_sistem, update_session_context, get_user_profile â€” diizinkan

    return True, ""


# OPENAI API
class AgentEngine:
    def __init__(self, persona_file="agents/kurisu.json"):
        self.gpt = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        with open(persona_file, "r", encoding="utf-8") as person:
            self._agent_personality = json.load(person)

        self._agent_instruction = build_instruction(self._agent_personality)

    async def chat(self, prompt: str, lang: str = "id", session_id: str = None, user_id: str = None, admin_role: str = None):
        lang_instruction = (
            "IMPORTANT: You MUST respond in English. All explanations, suggestions, and conversation MUST be in English."
            if lang == "en" else
            "IMPORTANT: Anda HARUS merespons dalam Bahasa Indonesia. Semua penjelasan, saran, dan percakapan HARUS dalam Bahasa Indonesia."
        )
        system_content = f"{self._agent_instruction}\n\n{lang_instruction}"

        is_portfolio = self._agent_personality.get("assistant_id") == "robin_ai_assistant"
        # 1. Ambil tools dari cache global dan filter dengan heuristik (0ms overhead)
        all_tools = await get_mcp_tools()
        tools = await _filter_relevant_tools(prompt, all_tools, always_include_device_tools=is_portfolio) if all_tools else []

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": prompt}
        ]
        
        # 2. Jalankan Agentic Loop (Mendukung tool calling berulang dengan streaming)
        while True:
            response = await self.gpt.chat.completions.create(
                model="gpt-5.4-mini-2026-03-17",
                messages=messages,
                tools=tools if tools else None,
                stream=True
            )
            
            active_tool_calls = {}
            current_content = ""
            async for chunk in response:
                choice = chunk.choices[0]
                delta = choice.delta
                
                # 1. Stream content to client
                if hasattr(delta, "content") and delta.content:
                    current_content += delta.content
                    yield f"data: {json.dumps({'text': delta.content})}\n\n"
                    
                # 2. Accumulate tool call delta if present
                if hasattr(delta, "tool_calls") and delta.tool_calls:
                    for tc in delta.tool_calls:
                        index = tc.index
                        if index not in active_tool_calls:
                            active_tool_calls[index] = {
                                "id": tc.id or "",
                                "name": tc.function.name or "",
                                "arguments": tc.function.arguments or ""
                            }
                        else:
                            if tc.id:
                                active_tool_calls[index]["id"] = tc.id
                            if tc.function.name:
                                active_tool_calls[index]["name"] = tc.function.name
                            if tc.function.arguments:
                                active_tool_calls[index]["arguments"] += tc.function.arguments

            # Jika tidak ada panggilan tool, loop selesai
            if not active_tool_calls:
                break
                
            # Format ke format standard tool_calls list
            formatted_tool_calls = []
            for idx, tc in sorted(active_tool_calls.items()):
                formatted_tool_calls.append({
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"]
                    }
                })
                
            # Tambahkan pesan asisten ke riwayat percakapan
            messages.append({
                "role": "assistant",
                "content": current_content if current_content else None,
                "tool_calls": formatted_tool_calls
            })
            
            # Hubungkan ke MCP Server secara dinamis hanya saat ada kebutuhan pemanggilan tool nyata
            non_device_calls = [tc for tc in formatted_tool_calls if tc["function"]["name"] not in DEVICE_TOOL_NAMES]
            device_calls = [tc for tc in formatted_tool_calls if tc["function"]["name"] in DEVICE_TOOL_NAMES]
            
            # Jalankan device tools terlebih dahulu tanpa perlu koneksi MCP
            for tc in device_calls:
                tool_name = tc["function"]["name"]
                args_str = tc["function"]["arguments"] or "{}"
                
                try:
                    tool_args = json.loads(args_str)
                except Exception:
                    tool_args = {}
                    
                print(f"[DEVICE TOOL] Calling tool: '{tool_name}' with args: {tool_args}")
                
                is_authorized = True
                if admin_role == "sales":
                    is_authorized, auth_error = _check_sales_authorization(tool_name, tool_args)
                    if not is_authorized:
                        result_text = f"Error: {auth_error}"
                
                if is_authorized:
                    try:
                        result_text = await execute_device_tool(user_id, tool_name, tool_args, tc["id"])
                    except Exception as e:
                        result_text = f"Error executing tool: {str(e)}"
                else:
                    result_text = f"Error: {auth_error}"
                    
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": tool_name,
                    "content": result_text
                })

            if non_device_calls:
                try:
                    async with streamablehttp_client(MCP_URL) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            
                            for tc in non_device_calls:
                                tool_name = tc["function"]["name"]
                                args_str = tc["function"]["arguments"] or "{}"
                                
                                try:
                                    tool_args = json.loads(args_str)
                                except Exception:
                                    tool_args = {}
                                    
                                if session_id and (tool_name == "buat_booking" or "session_id" in tool_args):
                                    tool_args["session_id"] = session_id

                                print(f"[OPENAI MCP] Calling tool: '{tool_name}' with args: {tool_args}")
                                
                                is_authorized = True
                                if admin_role == "sales":
                                    is_authorized, auth_error = _check_sales_authorization(tool_name, tool_args)
                                    if not is_authorized:
                                        result_text = f"Error: {auth_error}"
                                
                                if is_authorized:
                                    try:
                                        result = await session.call_tool(tool_name, tool_args)
                                        # Ekstrak konten teks dari hasil tool eksekusi
                                        result_text = ""
                                        if hasattr(result, "content") and result.content:
                                            for part in result.content:
                                                if hasattr(part, "text") and part.text:
                                                    result_text += part.text
                                                elif isinstance(part, dict) and part.get("type") == "text":
                                                    result_text += part.get("text", "")
                                                else:
                                                    result_text += str(part)
                                        else:
                                            result_text = str(result)
                                    except Exception as e:
                                        result_text = f"Error executing tool: {str(e)}"
                                    
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tc["id"],
                                    "name": tool_name,
                                    "content": result_text
                                })
                except Exception as conn_err:
                    print(f"[OPENAI MCP CONNECTION ERROR] Failed to connect for tool calling: {conn_err}")
                    for tc in non_device_calls:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "name": tc["name"],
                            "content": f"Failed to connect to MCP server for execution: {conn_err}"
                        })

            # Invalidate cache Redis setelah all tools selesai dijalankan
            if session_id:
                from services.redis_service import redis_client
                is_portfolio = self._agent_personality.get("assistant_id") == "robin_ai_assistant"
                sess_key = f"pa:session:{session_id}" if is_portfolio else f"session:{session_id}"
                msg_key = f"pa:messages:{session_id}" if is_portfolio else f"messages:{session_id}"
                try:
                    await redis_client.delete(sess_key)
                    await redis_client.delete(msg_key)
                    print(f"[REDIS] Cache invalidated for session {session_id} after tool calls.")
                except Exception as redis_err:
                    print(f"[REDIS WARN] Failed to delete session cache: {redis_err}")

# GOOGLE GEMINI API (Menggunakan Google GenAI SDK resmi)
class GeminiAgentEngine:
    def __init__(self, persona_file="agents/kurisu.json"):
        self.client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        with open(persona_file, "r", encoding="utf-8") as person:
            self._agent_personality = json.load(person)

        self._agent_instruction = build_instruction(self._agent_personality)

    async def chat(self, prompt: str, lang: str = "id", session_id: str = None, user_id: str = None, admin_role: str = None):
        lang_instruction = (
            "IMPORTANT: You MUST respond in English. All explanations, suggestions, and conversation MUST be in English."
            if lang == "en" else
            "IMPORTANT: Anda HARUS merespons dalam Bahasa Indonesia. Semua penjelasan, saran, dan percakapan HARUS dalam Bahasa Indonesia."
        )
        system_content = f"{self._agent_instruction}\n\n{lang_instruction}"

        is_portfolio = self._agent_personality.get("assistant_id") == "robin_ai_assistant"
        # 1. Ambil tools dari cache global dan filter dengan heuristik (0ms overhead)
        all_tools = await get_mcp_tools()
        tools = await _filter_relevant_tools(prompt, all_tools, always_include_device_tools=is_portfolio) if all_tools else []

        # Konversi format tools dari OpenAI format ke Google GenAI Tool format
        genai_tools = []
        if tools:
            declarations = []
            for t in tools:
                if t.get("type") == "function":
                    func_data = t["function"]
                    declarations.append(
                        types.FunctionDeclaration(
                            name=func_data["name"],
                            description=func_data["description"],
                            parameters=func_data["parameters"]
                        )
                    )
            if declarations:
                genai_tools.append(types.Tool(function_declarations=declarations))

        # Mulai percakapan terstruktur
        contents = [
            types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
        ]
        
        # 2. Jalankan Agentic Loop (Mendukung tool calling berulang dengan streaming)
        while True:
            response_stream = await self.client.aio.models.generate_content_stream(
                model="gemini-3.1-flash-lite",
                contents=contents,
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=0,  # untuk menonaktifkan thinking model
                    ),
                    system_instruction=system_content,
                    tools=genai_tools if genai_tools else None,
                )
            )
            current_content = ""
            current_tool_calls = []
            model_parts = []
            
            async for chunk in response_stream:
                # 1. Stream content to client (only if chunk has text to prevent warnings)
                has_text = False
                if chunk.candidates and chunk.candidates[0].content and chunk.candidates[0].content.parts:
                    for part in chunk.candidates[0].content.parts:
                        if part.text:
                            has_text = True
                
                if has_text and chunk.text:
                    current_content += chunk.text
                    yield f"data: {json.dumps({'text': chunk.text})}\n\n"
                    
                # 2. Collect parts and function calls
                if chunk.candidates and chunk.candidates[0].content and chunk.candidates[0].content.parts:
                    for part in chunk.candidates[0].content.parts:
                        model_parts.append(part)
                        if part.function_call:
                            current_tool_calls.append(part.function_call)

            # Jika tidak ada panggilan tool, loop selesai
            if not current_tool_calls:
                break
                
            # Tambahkan pesan asisten (panggilan tool dengan thought_signature) ke riwayat percakapan
            contents.append(types.Content(role="model", parts=model_parts))
            
            # Hubungkan ke MCP Server untuk mengeksekusi tools secara dinamis
            non_device_calls = [fc for fc in current_tool_calls if fc.name not in DEVICE_TOOL_NAMES]
            device_calls = [fc for fc in current_tool_calls if fc.name in DEVICE_TOOL_NAMES]
            
            tool_response_parts = []
            
            # Jalankan device tools terlebih dahulu tanpa perlu koneksi MCP
            for fc in device_calls:
                tool_name = fc.name
                # Ensure tool_args is a standard Python dict (some SDK versions return Struct/Map)
                try:
                    tool_args = dict(fc.args) if fc.args else {}
                except Exception:
                    tool_args = {}
                
                print(f"[DEVICE TOOL] Calling tool: '{tool_name}' with args: {tool_args}")
                
                is_authorized = True
                if admin_role == "sales":
                    is_authorized, auth_error = _check_sales_authorization(tool_name, tool_args)
                    if not is_authorized:
                        result_text = f"Error: {auth_error}"
                
                if is_authorized:
                    try:
                        import uuid
                        tool_call_id = str(uuid.uuid4())
                        result_text = await execute_device_tool(user_id, tool_name, tool_args, tool_call_id)
                    except Exception as e:
                        result_text = f"Error executing tool: {str(e)}"
                else:
                    result_text = f"Error: {auth_error}"
                
                tool_response_parts.append(
                    types.Part.from_function_response(
                        name=tool_name,
                        response={"result": result_text}
                    )
                )

            if non_device_calls:
                try:
                    async with streamablehttp_client(MCP_URL) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            
                            for fc in non_device_calls:
                                tool_name = fc.name
                                try:
                                    tool_args = dict(fc.args) if fc.args else {}
                                except Exception:
                                    tool_args = {}
                                
                                if session_id and (tool_name == "buat_booking" or "session_id" in tool_args):
                                    tool_args["session_id"] = session_id

                                print(f"[GEMINI SDK MCP] Calling tool: '{tool_name}' with args: {tool_args}")
                                
                                is_authorized = True
                                if admin_role == "sales":
                                    is_authorized, auth_error = _check_sales_authorization(tool_name, tool_args)
                                    if not is_authorized:
                                        result_text = f"Error: {auth_error}"
                                
                                if is_authorized:
                                    try:
                                        result = await session.call_tool(tool_name, tool_args)
                                        # Ekstrak konten teks dari hasil tool eksekusi
                                        result_text = ""
                                        if hasattr(result, "content") and result.content:
                                            for part in result.content:
                                                if hasattr(part, "text") and part.text:
                                                    result_text += part.text
                                                elif isinstance(part, dict) and part.get("type") == "text":
                                                    result_text += part.get("text", "")
                                                else:
                                                    result_text += str(part)
                                        else:
                                            result_text = str(result)
                                    except Exception as e:
                                        result_text = f"Error executing tool: {str(e)}"
                                    
                                tool_response_parts.append(
                                    types.Part.from_function_response(
                                        name=tool_name,
                                        response={"result": result_text}
                                    )
                                )
                except Exception as conn_err:
                    import traceback
                    print(f"[GEMINI SDK MCP CONNECTION ERROR] Failed to connect for tool calling:\n{traceback.format_exc()}")
                    for fc in non_device_calls:
                        tool_response_parts.append(
                            types.Part.from_function_response(
                                name=fc.name,
                                response={"result": f"Failed to connect to MCP server for execution: {conn_err}"}
                            )
                        )

            # Invalidate cache Redis setelah all tools selesai dijalankan
            if session_id:
                from services.redis_service import redis_client
                is_portfolio = self._agent_personality.get("assistant_id") == "robin_ai_assistant"
                sess_key = f"pa:session:{session_id}" if is_portfolio else f"session:{session_id}"
                msg_key = f"pa:messages:{session_id}" if is_portfolio else f"messages:{session_id}"
                try:
                    await redis_client.delete(sess_key)
                    await redis_client.delete(msg_key)
                    print(f"[REDIS] Cache invalidated for session {session_id} after tool calls.")
                except Exception as redis_err:
                    print(f"[REDIS WARN] Failed to delete session cache: {redis_err}")
            
            # Tambahkan hasil eksekusi tool dari user ke percakapan
            contents.append(types.Content(role="user", parts=tool_response_parts))


            