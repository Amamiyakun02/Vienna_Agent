"""Rolling conversation summary memakai Gemini Flash-Lite.

Dipanggil fire-and-forget setiap pesan tersimpan; hanya benar-benar memanggil
LLM tiap kelipatan SUMMARY_EVERY_MSGS pesan. Hasilnya ditulis ke
`session.context.summary` (+ keywords, last_message) sehingga prompt chat
cukup membawa summary + jendela pesan terakhir.
"""
import os
from datetime import datetime, timezone

SUMMARY_EVERY_MSGS = int(os.getenv("SUMMARY_EVERY_MSGS", "20"))
SUMMARY_MAX_CHARS = int(os.getenv("SUMMARY_MAX_CHARS", "800"))
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "gemini-3.1-flash-lite")
SUMMARY_FALLBACK_MODEL = os.getenv("SUMMARY_FALLBACK_MODEL", "gemini-3.5-flash-lite")

_SUMMARY_INSTRUCTION = (
    "Ringkas percakapan berikut dalam Bahasa Indonesia, maksimal "
    f"{SUMMARY_MAX_CHARS} karakter. Catat: fakta tentang user (nama, preferensi, "
    "kebutuhan), topik yang dibahas, dan status/keputusan terakhir. "
    "Abaikan sapaan basa-basi, typo, dan pesan gagal. Pakai kalimat padat.\n\n"
    "Format jawaban WAJIB:\n"
    "RINGKASAN: <ringkasan>\n"
    "KATA_KUNCI: <kata1, kata2, ...>"
)


def _parse_summary(text: str):
    """Ambil (summary, keywords) dari output model. Defensif terhadap format ngawur."""
    summary, keywords = text.strip(), []
    if "RINGKASAN:" in text:
        summary = text.split("RINGKASAN:", 1)[1]
        if "KATA_KUNCI:" in summary:
            summary, kw_part = summary.split("KATA_KUNCI:", 1)
            keywords = [k.strip() for k in kw_part.replace("\n", ",").split(",") if k.strip()][:10]
        summary = summary.strip()
    return summary[:SUMMARY_MAX_CHARS], keywords


async def maybe_rollup_summary(session_id: str, is_portfolio: bool = False) -> bool:
    """Rollup summary bila jumlah pesan menyentuh kelipatan berikutnya.

    Return True jika summary diperbarui. Tidak pernah raise (log saja) agar
    tidak mengganggu alur chat utama.
    """
    try:
        from services.mongo_service import (
            count_session_messages, get_messages,
            sessions_col, pa_sessions_col,
        )
        total = await count_session_messages(session_id, is_portfolio=is_portfolio)
        if total <= 0 or total % SUMMARY_EVERY_MSGS != 0:
            return False

        recent = await get_messages(session_id=session_id, limit=SUMMARY_EVERY_MSGS, is_portfolio=is_portfolio)
        if not recent:
            return False

        col = pa_sessions_col if is_portfolio else sessions_col
        session = await col.find_one({"session_id": session_id}, {"_id": 0}) or {}
        prev_summary = (session.get("context") or {}).get("summary") or "-"

        lines = []
        for m in recent:
            sender = "User" if m.get("sender") == "user" else "AI"
            content = (m.get("content") or "")[:1000]
            lines.append(f"{sender}: {content}")
        convo = "\n".join(lines)

        from google import genai
        from google.genai import types
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        summary_models = [SUMMARY_MODEL]
        if SUMMARY_FALLBACK_MODEL and SUMMARY_FALLBACK_MODEL != SUMMARY_MODEL:
            summary_models.append(SUMMARY_FALLBACK_MODEL)
        resp = None
        for sm in summary_models:
            try:
                resp = await client.aio.models.generate_content(
                    model=sm,
                    contents=f"RINGKASAN SEBELUMNYA:\n{prev_summary}\n\nPERCAKAPAN TERBARU:\n{convo}",
                    config=types.GenerateContentConfig(
                        thinking_config=types.ThinkingConfig(thinking_budget=0),
                        max_output_tokens=512,
                    ),
                )
                break
            except Exception as se:
                print(f"[SUMMARY WARN] model {sm} gagal ({se})")
                if sm == summary_models[-1]:
                    raise
                continue
        text = ((resp.text if resp else "") or "").strip()
        if not text:
            return False

        summary, keywords = _parse_summary(text)
        last_msg = (recent[-1].get("content") or "")[:500]
        await col.update_one(
            {"session_id": session_id},
            {"$set": {
                "context.summary": summary,
                "context.keywords": keywords,
                "context.last_message": last_msg,
                "updated_at": datetime.now(timezone.utc),
            }}
        )
        # Segarkan cache session agar summary baru langsung dipakai prompt berikutnya
        try:
            from services.mongo_service import get_session_by_id  # noqa: F401
            from services.redis_service import redis_client
            cache_key = f"pa:session:{session_id}" if is_portfolio else f"session:{session_id}"
            await redis_client.delete(cache_key)
        except Exception:
            pass
        print(f"[SUMMARY] Updated summary for session {session_id} ({total} msgs)")
        return True
    except Exception as e:
        print(f"[SUMMARY WARN] Rollup skipped: {e}")
        return False
