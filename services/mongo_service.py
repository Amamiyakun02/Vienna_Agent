import os
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from bson import ObjectId
import uuid
import json
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from .redis_service import redis_client

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# REDIS SERIALIZATION HELPERS
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def serialize_for_cache(data) -> str:
    """Sederhanakan data dict/list MongoDB menjadi JSON String untuk disimpan di Redis."""
    if data is None:
        return ""
    
    def transform(val):
        if isinstance(val, ObjectId):
            return {"__objectid__": str(val)}
        elif isinstance(val, datetime):
            return {"__datetime__": val.isoformat()}
        elif isinstance(val, list):
            return [transform(item) for item in val]
        elif isinstance(val, dict):
            return {k: transform(v) for k, v in val.items()}
        return val

    transformed_data = transform(data)
    return json.dumps(transformed_data)

def deserialize_from_cache(json_str: str):
    """Mengembalikan JSON String dari Redis menjadi dict/list dengan tipe data asli (ObjectId & datetime)."""
    if not json_str:
        return None
    
    raw_data = json.loads(json_str)
    
    def restore(val):
        if isinstance(val, dict):
            if "__datetime__" in val:
                dt_str = val["__datetime__"]
                try:
                    if dt_str.endswith('Z'):
                        dt_str = dt_str.replace('Z', '+00:00')
                    return datetime.fromisoformat(dt_str)
                except Exception:
                    return dt_str
            if "__objectid__" in val:
                try:
                    return ObjectId(val["__objectid__"])
                except Exception:
                    return val["__objectid__"]
            return {k: restore(v) for k, v in val.items()}
        elif isinstance(val, list):
            return [restore(item) for item in val]
        return val
        
    return restore(raw_data)


load_dotenv()
MONGO_USER = os.getenv("MONGO_USER")
MONGO_PASS = os.getenv("MONGO_PASS")
DB_NAME = os.getenv("MONGO_DB_NAME")

mongo_client = AsyncIOMotorClient(
    f"mongodb+srv://{MONGO_USER}:{MONGO_PASS}@aimer.wngbxyv.mongodb.net/?retryWrites=true&w=majority&appName=aimer"
)
db = mongo_client[DB_NAME]

# ──────────────────────────────────────────────
# CORE COLLECTIONS
# ──────────────────────────────────────────────
users_col            = db["users"]
documents_col        = db["documents"]
sessions_col         = db["chatsessions"]
messages_col         = db["messages"]
feedbacks_col        = db["feedbacks"]

# ──────────────────────────────────────────────
# PERSONAL ASSISTANT PORTFOLIO ALIASES
# ──────────────────────────────────────────────
pa_users_col     = users_col
pa_sessions_col  = sessions_col
pa_messages_col  = messages_col
pa_documents_col = documents_col

# ──────────────────────────────────────────────
# PRIVATE AI AGENT DEVICE & APP COLLECTIONS
# ──────────────────────────────────────────────
devices_col = db["devices"]
device_app_inventory_col = db["device_app_inventory"]



# ======================
# SESSION HANDLERS
# ======================

async def get_session_by_id(session_id: str, is_portfolio: bool = False) -> dict:
    """
    Mengambil data session berdasarkan session_id dari Redis Cache atau MongoDB.
    """
    cache_key = f"pa:session:{session_id}" if is_portfolio else f"session:{session_id}"
    col = pa_sessions_col if is_portfolio else sessions_col
    try:
        cached = await redis_client.get(cache_key)
        if cached:
            return deserialize_from_cache(cached)
    except Exception as e:
        print(f"[REDIS WARN] Failed to get session cache: {e}")

    session_data = await col.find_one({"session_id": session_id}, {"_id": 0})
    if session_data:
        try:
            await redis_client.setex(cache_key, 3600, serialize_for_cache(session_data))
        except Exception as e:
            print(f"[REDIS WARN] Failed to set session cache: {e}")
    return session_data or {}


async def create_session(user_id: str, session_id: str = None, platform: str = "web", context: dict = None, role: str = "customer", is_portfolio: bool = False):
    """
    Membuat session baru untuk user_id tertentu.
    - Kalau `session_id` tidak diberikan â†’ auto-generate.
    - `context` default kosong.
    - `platform` default "web".
    """
    now = datetime.now(timezone.utc)
    col = pa_sessions_col if is_portfolio else sessions_col

    if not session_id:
        session_id = f"sess_{now.strftime('%Y%m%d_%H%M%S%f')}"

    if context is None:
        if is_portfolio:
            context = {
                "last_message": "",
                "summary": ""
            }
        else:
            context = {
                "last_message": "",
                "summary": "",
                "keywords": [],
                "current_intent": "browsing",
                "interested_products": [],
                "last_booking_id": None
            }

    if is_portfolio:
        customer_info = {
            "name": None,
            "email": None
        }
    else:
        customer_info = {
            "name": None,
            "phone": None,
            "email": None
        }
        if user_id and not str(user_id).startswith("guest"):
            try:
                user_doc = None
                try:
                    user_doc = await users_col.find_one({"_id": ObjectId(user_id)})
                except Exception:
                    pass
                if not user_doc:
                    user_doc = await users_col.find_one({"_id": user_id})
                
                if user_doc:
                    customer_info = {
                        "name": user_doc.get("name"),
                        "phone": user_doc.get("phone"),
                        "email": user_doc.get("email")
                    }
            except Exception as e:
                print(f"[WARNING] Failed to fetch user info for new session: {e}")

    new_data = {
        "_id": ObjectId(),
        "session_id": session_id,
        "user_id": user_id,
        "role": role,
        "platform": platform,
        "customer_info": customer_info,
        "context": context,
        "is_active": True,
        "created_at": now,
        "last_activity": now,
        "updated_at": now,
    }

    await col.insert_one(new_data)

    return {
        "status": "created",
        "session_id": session_id,
        "updated_at": now.isoformat()
    }


async def get_or_create_session(user_id: str, session_id: str = None, role: str = "customer", is_portfolio: bool = False):
    """
    Mengambil session berdasarkan `session_id` atau `user_id` dengan asinkron Redis Caching.
    """
    col = pa_sessions_col if is_portfolio else sessions_col

    async def sync_customer_info(session_doc: dict):
        if is_portfolio:
            return
        uid = session_doc.get("user_id")
        if uid and not str(uid).startswith("guest"):
            try:
                user_doc = None
                try:
                    user_doc = await users_col.find_one({"_id": ObjectId(uid)})
                except Exception:
                    pass
                if not user_doc:
                    user_doc = await users_col.find_one({"_id": uid})
                
                if user_doc:
                    new_cust = {
                        "name": user_doc.get("name"),
                        "phone": user_doc.get("phone"),
                        "email": user_doc.get("email")
                    }
                    old_cust = session_doc.get("customer_info", {})
                    if not old_cust or old_cust.get("name") != new_cust["name"] or old_cust.get("phone") != new_cust["phone"] or old_cust.get("email") != new_cust["email"]:
                        session_doc["customer_info"] = new_cust
                        await col.update_one(
                            {"session_id": session_doc["session_id"]},
                            {"$set": {"customer_info": new_cust}}
                        )
            except Exception as e:
                print(f"[WARNING] Failed to sync customer_info for session: {e}")

    if session_id:
        cache_key = f"pa:session:{session_id}" if is_portfolio else f"session:{session_id}"
        try:
            cached = await redis_client.get(cache_key)
            if cached:
                session_obj = deserialize_from_cache(cached)
                if session_obj and ( "role" not in session_obj or session_obj.get("role") != role ):
                    # Cache is stale in terms of role, delete it and load from DB
                    try:
                        await redis_client.delete(cache_key)
                    except Exception:
                        pass
                else:
                    return session_obj
        except Exception as e:
            print(f"[REDIS WARN] Failed to get session cache: {e}")

        existing = await col.find_one({"session_id": session_id})
        if existing:
            if "role" not in existing or existing.get("role") != role:
                existing["role"] = role
                await col.update_one({"session_id": session_id}, {"$set": {"role": role}})
            if not is_portfolio:
                await sync_customer_info(existing)
            try:
                await redis_client.setex(cache_key, 3600, serialize_for_cache(existing))
            except Exception as e:
                print(f"[REDIS WARN] Failed to set session cache: {e}")
            return existing

    existing = await col.find_one({"user_id": user_id})
    if existing:
        sess_id = existing.get("session_id")
        if sess_id:
            cache_key = f"pa:session:{sess_id}" if is_portfolio else f"session:{sess_id}"
            if "role" not in existing or existing.get("role") != role:
                existing["role"] = role
                await col.update_one({"user_id": user_id}, {"$set": {"role": role}})
                try:
                    await redis_client.delete(cache_key)
                except Exception:
                    pass
            if not is_portfolio:
                await sync_customer_info(existing)
            try:
                await redis_client.setex(cache_key, 3600, serialize_for_cache(existing))
            except Exception as e:
                print(f"[REDIS WARN] Failed to set session cache: {e}")
        return existing

    new_session = await create_session(user_id, session_id=session_id, role=role, is_portfolio=is_portfolio)
    created_sess = await col.find_one({"session_id": new_session["session_id"]})
    if created_sess:
        cache_key = f"pa:session:{new_session['session_id']}" if is_portfolio else f"session:{new_session['session_id']}"
        try:
            await redis_client.setex(cache_key, 3600, serialize_for_cache(created_sess))
        except Exception as e:
            print(f"[REDIS WARN] Failed to set session cache: {e}")
    return created_sess


async def get_session(session_id: str, is_portfolio: bool = False) -> Optional[dict]:
    """Ambil data session berdasarkan session_id dari Redis Cache atau MongoDB."""
    cache_key = f"pa:session:{session_id}" if is_portfolio else f"session:{session_id}"
    col = pa_sessions_col if is_portfolio else sessions_col
    try:
        cached = await redis_client.get(cache_key)
        if cached:
            return deserialize_from_cache(cached)
    except Exception as e:
        print(f"[REDIS WARN] Failed to get session cache: {e}")

    session_data = await col.find_one(
        {"session_id": session_id},
        {"_id": 0}
    )
    if session_data:
        try:
            await redis_client.setex(cache_key, 3600, serialize_for_cache(session_data))
        except Exception as e:
            print(f"[REDIS WARN] Failed to set session cache: {e}")
    return session_data


async def save_session(session_data: dict, is_portfolio: bool = False):
    """
    Simpan atau update session di MongoDB dan Redis Cache.
    """
    col = pa_sessions_col if is_portfolio else sessions_col
    session_data["updated_at"] = datetime.now(timezone.utc).isoformat()
    await col.update_one(
        {"session_id": session_data["session_id"]},
        {"$set": session_data},
        upsert=True
    )
    
    cache_key = f"pa:session:{session_data['session_id']}" if is_portfolio else f"session:{session_data['session_id']}"
    try:
        await redis_client.setex(cache_key, 3600, serialize_for_cache(session_data))
    except Exception as e:
        print(f"[REDIS WARN] Failed to set session cache: {e}")


# ======================
# MESSAGE HANDLERS
# ======================

async def get_messages(session_id: str, limit: int = 50, is_portfolio: bool = False) -> List[dict]:
    """Ambil pesan terakhir dari session tertentu dengan asinkron Redis Caching."""
    cache_key = f"pa:messages:{session_id}" if is_portfolio else f"messages:{session_id}"
    col = pa_messages_col if is_portfolio else messages_col
    try:
        cached = await redis_client.get(cache_key)
        if cached:
            messages = deserialize_from_cache(cached)
            if isinstance(messages, list):
                return messages[-limit:]
    except Exception as e:
        print(f"[REDIS WARN] Failed to get messages cache: {e}")

    cursor = col.find({"session_id": session_id}).sort("timestamp", -1).limit(limit)
    messages = await cursor.to_list(length=limit)
    ordered_messages = list(reversed(messages))
    
    try:
        await redis_client.setex(cache_key, 3600, serialize_for_cache(ordered_messages))
    except Exception as e:
        print(f"[REDIS WARN] Failed to set messages cache: {e}")
    return ordered_messages


async def save_message(session_id: str, sender: str, content: str, message_type: str = "text", metadata: dict = None, is_portfolio: bool = False):
    """Simpan message baru ke koleksi messages MongoDB dan bersihkan cache Redis."""
    col = pa_messages_col if is_portfolio else messages_col
    doc = {
        "session_id": session_id,
        "sender": sender,           # "user" | "agent" | "system"
        "content": content,
        "message_type": message_type,
        "metadata": metadata,
        "timestamp": datetime.now(timezone.utc)
    }
    await col.insert_one(doc)
    
    # Invalidate cache messages agar data di-fetch segar dari DB pada pemanggilan get_messages berikutnya
    cache_key = f"pa:messages:{session_id}" if is_portfolio else f"messages:{session_id}"
    try:
        await redis_client.delete(cache_key)
    except Exception as e:
        print(f"[REDIS WARN] Failed to delete messages cache: {e}")

# ======================
# AUTH & MIGRATE DB HANDLERS (NEW)
# ======================

async def get_user_by_email(email: str) -> Optional[dict]:
    """Mengambil data pengguna berdasarkan email dari MongoDB."""
    return await users_col.find_one({"email": email})

async def migrate_anonymous_session(temp_session_id: str, user_id: str) -> dict:
    """
    Memindahkan / menjahit riwayat sesi obrolan tamu ke user terdaftar baru.
    1. Mengubah user_id sesi di chatsessions MongoDB.
    2. Menghapus cache sesi lama di Redis agar terupdate dinamis.
    """
    # 1. Update session di MongoDB
    existing_session = await sessions_col.find_one({"session_id": temp_session_id})
    if not existing_session:
        return {"status": "success", "message": "Tidak ada riwayat sesi tamu untuk dimigrasikan."}

    # Jika sesi sudah diasosiasikan dengan user_id lain, batalkan demi keamanan
    if existing_session.get("user_id") and existing_session.get("user_id") != user_id:
        user_id_str = existing_session.get("user_id")
        if not str(user_id_str).startswith("guest"):
            return {"status": "error", "message": "Sesi ini sudah dimiliki oleh user lain."}

    # Fetch user info to populate customer_info
    customer_info = {
        "name": None,
        "phone": None,
        "email": None
    }
    try:
        user_doc = None
        try:
            user_doc = await users_col.find_one({"_id": ObjectId(user_id)})
        except Exception:
            pass
        if not user_doc:
            user_doc = await users_col.find_one({"_id": user_id})
        
        if user_doc:
            customer_info = {
                "name": user_doc.get("name"),
                "phone": user_doc.get("phone"),
                "email": user_doc.get("email")
            }
    except Exception as e:
        print(f"[WARNING] Failed to fetch user info for migration: {e}")

    # Jalankan update user_id di chatsessions
    await sessions_col.update_one(
        {"session_id": temp_session_id},
        {"$set": {
            "user_id": user_id,
            "customer_info": customer_info,
            "updated_at": datetime.now(timezone.utc)
        }}
    )

    # 2. Invalidate cache Redis sesi agar sync seketika
    cache_key = f"session:{temp_session_id}"
    try:
        await redis_client.delete(cache_key)
    except Exception as e:
        print(f"[REDIS WARN] Gagal menghapus cache sesi migrasi: {e}")

    # Ambil sesi terupdate untuk di-cache kembali
    updated_session = await sessions_col.find_one({"session_id": temp_session_id})
    if updated_session:
        try:
            await redis_client.setex(cache_key, 3600, serialize_for_cache(updated_session))
        except Exception as e:
            print(f"[REDIS WARN] Gagal menulis ulang cache sesi migrasi: {e}")

    return {"status": "success", "message": "Sesi obrolan berhasil dimigrasikan."}
