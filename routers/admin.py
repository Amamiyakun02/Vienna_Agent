from fastapi import APIRouter, HTTPException, Query, Body, Path
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from bson import ObjectId
from pydantic import BaseModel

from services.mongo_service import users_col, sessions_col, messages_col, documents_col, feedbacks_col

router = APIRouter(prefix="/v1/admin", tags=["Admin Management"])

def serialize_mongo(doc):
    if not doc:
        return None
    if "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc

# =======================
# USERS
# =======================
@router.get("/users")
async def get_users(limit: int = 100, skip: int = 0):
    cursor = users_col.find().sort("created_at", -1).skip(skip).limit(limit)
    users = await cursor.to_list(length=limit)
    return [serialize_mongo(u) for u in users]

@router.post("/users")
async def create_user(payload: dict = Body(...)):
    doc = {**payload, "created_at": datetime.now(timezone.utc)}
    res = await users_col.insert_one(doc)
    doc["_id"] = str(res.inserted_id)
    return doc

@router.put("/users/{user_id}")
async def update_user(user_id: str, payload: dict = Body(...)):
    try:
        obj_id = ObjectId(user_id)
        if "_id" in payload:
            del payload["_id"]
        res = await users_col.update_one({"_id": obj_id}, {"$set": payload})
        if res.matched_count == 0:
            raise HTTPException(404, "User not found")
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(400, str(e))

@router.delete("/users/{user_id}")
async def delete_user(user_id: str):
    try:
        obj_id = ObjectId(user_id)
        res = await users_col.delete_one({"_id": obj_id})
        if res.deleted_count == 0:
            raise HTTPException(404, "User not found")
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(400, str(e))

# =======================
# CHAT SESSIONS
# =======================
@router.get("/chatsessions")
async def get_sessions(limit: int = 100, skip: int = 0):
    cursor = sessions_col.find().sort("created_at", -1).skip(skip).limit(limit)
    sessions = await cursor.to_list(length=limit)
    return [serialize_mongo(s) for s in sessions]

@router.delete("/chatsessions/{session_id}")
async def delete_session(session_id: str):
    res = await sessions_col.delete_one({"session_id": session_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Session not found")
    # optionally delete associated messages
    await messages_col.delete_many({"session_id": session_id})
    return {"status": "success"}

# =======================
# MESSAGES
# =======================
@router.get("/messages")
async def get_all_messages(session_id: Optional[str] = None, limit: int = 100, skip: int = 0):
    query = {}
    if session_id:
        query["session_id"] = session_id
    cursor = messages_col.find(query).sort("timestamp", -1).skip(skip).limit(limit)
    msgs = await cursor.to_list(length=limit)
    return [serialize_mongo(m) for m in msgs]

# =======================
# DOCUMENTS
# =======================
@router.get("/documents")
async def get_documents(limit: int = 100, skip: int = 0):
    # Documents might use 'timestamp' or 'created_at' depending on how it's populated.
    # Usually we just return list
    cursor = documents_col.find().skip(skip).limit(limit)
    docs = await cursor.to_list(length=limit)
    return [serialize_mongo(d) for d in docs]

@router.post("/documents")
async def create_document(payload: dict = Body(...)):
    doc = {**payload, "created_at": datetime.now(timezone.utc)}
    res = await documents_col.insert_one(doc)
    doc["_id"] = str(res.inserted_id)
    return doc

@router.put("/documents/{doc_id}")
async def update_document(doc_id: str, payload: dict = Body(...)):
    try:
        obj_id = ObjectId(doc_id)
        if "_id" in payload:
            del payload["_id"]
        res = await documents_col.update_one({"_id": obj_id}, {"$set": payload})
        if res.matched_count == 0:
            raise HTTPException(404, "Document not found")
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(400, str(e))

@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: str):
    try:
        obj_id = ObjectId(doc_id)
        res = await documents_col.delete_one({"_id": obj_id})
        if res.deleted_count == 0:
            raise HTTPException(404, "Document not found")
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(400, str(e))

# =======================
# FEEDBACKS
# =======================
@router.get("/feedbacks")
async def get_feedbacks(limit: int = 100, skip: int = 0):
    cursor = feedbacks_col.find().sort("created_at", -1).skip(skip).limit(limit)
    feedbacks = await cursor.to_list(length=limit)
    return [serialize_mongo(f) for f in feedbacks]

@router.post("/feedbacks")
async def create_feedback(payload: dict = Body(...)):
    doc = {**payload, "created_at": datetime.now(timezone.utc)}
    res = await feedbacks_col.insert_one(doc)
    doc["_id"] = str(res.inserted_id)
    return doc

@router.put("/feedbacks/{feedback_id}")
async def update_feedback(feedback_id: str, payload: dict = Body(...)):
    try:
        obj_id = ObjectId(feedback_id)
        if "_id" in payload:
            del payload["_id"]
        res = await feedbacks_col.update_one({"_id": obj_id}, {"$set": payload})
        if res.matched_count == 0:
            raise HTTPException(404, "Feedback not found")
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(400, str(e))

@router.delete("/feedbacks/{feedback_id}")
async def delete_feedback(feedback_id: str):
    try:
        obj_id = ObjectId(feedback_id)
        res = await feedbacks_col.delete_one({"_id": obj_id})
        if res.deleted_count == 0:
            raise HTTPException(404, "Feedback not found")
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(400, str(e))

# =======================
# STATS
# =======================
@router.get("/stats")
async def get_stats():
    users_count = await users_col.count_documents({})
    docs_count = await documents_col.count_documents({})
    sessions_count = await sessions_col.count_documents({})
    messages_count = await messages_col.count_documents({})
    
    return {
        "counts": {
            "users": users_count,
            "documents": docs_count,
            "chatsessions": sessions_count,
            "messages": messages_count,
            "qdrant_indexed": docs_count
        }
    }

@router.get("/stats/message-activity")
async def get_message_activity():
    pipeline = [
        {
            "$group": {
                "_id": {
                    "year": {"$year": "$timestamp"},
                    "month": {"$month": "$timestamp"}
                },
                "messages": {"$sum": 1},
                "agent_replies": {
                    "$sum": {"$cond": [{"$eq": ["$sender", "assistant"]}, 1, 0]}
                }
            }
        },
        {"$sort": {"_id.year": 1, "_id.month": 1}},
        {"$limit": 12}
    ]
    cursor = messages_col.aggregate(pipeline)
    results = await cursor.to_list(length=12)
    
    formatted = []
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for r in results:
        # In case $timestamp is missing or not parseable, _id could be null
        if r["_id"] is None or "month" not in r["_id"] or r["_id"]["month"] is None:
            continue
        idx = r["_id"]["month"] - 1
        if 0 <= idx < 12:
            formatted.append({
                "month": month_names[idx],
                "messages": r["messages"],
                "agent_replies": r["agent_replies"]
            })
    return formatted
