"""Embedding untuk RAG Vienna Agent — provider bisa diganti via env.

Provider:
  EMBEDDING_PROVIDER=gemini  (default) -> text-embedding-004, 768 dimensi,
                               gratis via GEMINI_API_KEY yang sama dengan chat.
  EMBEDDING_PROVIDER=openai  -> text-embedding-3-small, 1536 dimensi,
                               butuh OPENAI_API_KEY ber-credits.

PENTING: dimensi + ruang vektor terikat ke provider. Query dan isi Qdrant
HARUS satu provider — campur OpenAI/Gemini menghasilkan similarity sampah
atau ditolak Qdrant (dimensi beda). Ganti provider = recreate koleksi
(diurus otomatis oleh initialize_qdrant_collections) + re-ingest dokumen.
"""

import os
import time

# ---------------------------------------------------------------------------
# Konfigurasi provider
# ---------------------------------------------------------------------------
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "gemini").strip().lower()
GEMINI_EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "text-embedding-004")

# Dimensi vektor per provider — dipakai saat membuat koleksi Qdrant.
EMBEDDING_DIM = 768 if EMBEDDING_PROVIDER == "gemini" else 1536


def _cooldown_seconds(env_name: str, default: float) -> float:
    try:
        return float(os.getenv(env_name, str(default)))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# OpenAI (legacy — dipakai bila EMBEDDING_PROVIDER=openai)
# ---------------------------------------------------------------------------
from openai import OpenAI

openai_api_key = os.getenv("OPENAI_API_KEY")

# Tanpa retry: credits habis — 429 harus gagal cepat, bukan backoff SDK
# yang bikin tiap pesan chat nunggu detik-an.
openai_client = (
    OpenAI(api_key=openai_api_key, timeout=5.0, max_retries=0)
    if openai_api_key else None
)

OPENAI_EMB_COOLDOWN_S = _cooldown_seconds("OPENAI_EMB_COOLDOWN_S", 60)
_openai_dead_until = 0.0  # monotonic — jeda bila OpenAI limit/habis


def gpt_embedding(text: str, model_name: str = "text-embedding-3-small") -> list[float]:
    """Embedding via OpenAI (1536-dim). Dipakai bila EMBEDDING_PROVIDER=openai."""
    if openai_client is None:
        return []
    global _openai_dead_until
    if time.monotonic() < _openai_dead_until:
        return []  # masih cooldown — hemat waktu selama kredit habis
    try:
        result = openai_client.embeddings.create(
            model=model_name,
            input=text,
            encoding_format="float",
        )
        _openai_dead_until = 0.0  # pulih — reset cooldown
        return result.data[0].embedding
    except Exception as e:
        _openai_dead_until = time.monotonic() + OPENAI_EMB_COOLDOWN_S
        print(f"[ERROR] Error saat membuat embedding OpenAI: {e}")
        return []


# ---------------------------------------------------------------------------
# Gemini (default — gratis, satu key dengan chat)
# ---------------------------------------------------------------------------
GEMINI_EMB_COOLDOWN_S = _cooldown_seconds("GEMINI_EMB_COOLDOWN_S", 60)
_gemini_dead_until = 0.0  # monotonic — jeda bila Gemini limit/error
_gemini_client = None


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return None
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


def gemini_embedding(text: str, model_name: str = "") -> list[float]:
    """Embedding via Gemini, selalu EMBEDDING_DIM (768) dimensi.

    Model dari env GEMINI_EMBEDDING_MODEL; model Gemini baru berdimensi
    asli 3072, jadi output diminta 768 via output_dimensionality lalu
    dinormalisasi — koleksi Qdrant Vienna Agent berdimensi 768.

    Gagal cepat + cooldown seperti versi OpenAI: bila limit/error,
    return [] agar chat lanjut tanpa konteks (degradasi aman).
    """
    global _gemini_dead_until
    if time.monotonic() < _gemini_dead_until:
        return []  # masih cooldown
    client = _get_gemini_client()
    if client is None:
        return []
    try:
        from google.genai import types

        result = client.models.embed_content(
            model=model_name or GEMINI_EMBEDDING_MODEL,
            contents=text,
            config=types.EmbedContentConfig(output_dimensionality=EMBEDDING_DIM),
        )
        _gemini_dead_until = 0.0  # pulih — reset cooldown
        values = list(result.embeddings[0].values)
        # Output dipangkas ke EMBEDDING_DIM (768) agar cocok dengan koleksi
        # Qdrant; sesuai anjuran Google, vektor hasil pemangkasan dimensi
        # dinormalisasi ulang (L2) agar cosine similarity tetap benar.
        norm = sum(v * v for v in values) ** 0.5
        if norm > 0:
            values = [v / norm for v in values]
        return values
    except Exception as e:
        _gemini_dead_until = time.monotonic() + GEMINI_EMB_COOLDOWN_S
        print(f"[ERROR] Error saat membuat embedding Gemini: {e}")
        return []


# ---------------------------------------------------------------------------
# Dispatcher — SATU pintu untuk query RAG + ingestion.
# ---------------------------------------------------------------------------
def embed_text(text: str) -> list[float]:
    """Embedding sesuai EMBEDDING_PROVIDER aktif."""
    if EMBEDDING_PROVIDER == "gemini":
        return gemini_embedding(text)
    return gpt_embedding(text)
