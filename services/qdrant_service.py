import os
from qdrant_client import AsyncQdrantClient
from dotenv import load_dotenv

# Load .env
load_dotenv()

QDRANT_URI = (os.getenv("QDRANT_URL") or "").strip()
# Qdrant lokal tanpa auth: API key boleh kosong. Cloud wajib diisi.
QDRANT_API_KEY = (os.getenv("QDRANT_API_KEY") or "").strip() or None

if not QDRANT_URI:
    raise ValueError("QDRANT_URL harus diset di dalam file .env")

# Initialize Qdrant Client
qdrant_client = AsyncQdrantClient(url=QDRANT_URI, api_key=QDRANT_API_KEY)

async def get_qdrant_client():
    """
    Returns the async Qdrant client instance.
    """
    return qdrant_client

async def close_qdrant_connection():
    """
    Closes the Qdrant connection.
    """
    await qdrant_client.close()
