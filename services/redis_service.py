import os
import redis.asyncio as redis
from dotenv import load_dotenv

# Load .env
load_dotenv()

REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT"))
REDIS_USERNAME = os.getenv("REDIS_USERNAME")
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")

# Create Redis connection
# Akan menggunakan authentication berdasarkan username & password jika tersedia.
redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    username=REDIS_USERNAME,
    password=REDIS_PASSWORD,
    decode_responses=True
)

async def get_redis_client():
    """
    Returns the async Redis client instance.
    """
    return redis_client

async def close_redis_connection():
    """
    Closes the Redis connection.
    """
    await redis_client.close()
