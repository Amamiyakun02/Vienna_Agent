import os
import requests
import time
from dotenv import load_dotenv

# Muat variabel lingkungan
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
# Default nama bucket penyimpanan Supabase adalah 'products'
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET")

def upload_file_to_supabase(file_bytes: bytes, file_name: str, content_type: str) -> str:
    """
    Mengunggah file (dalam byte) ke Supabase Storage dan mengembalikan URL publiknya.

    Args:
        file_bytes (bytes): Konten biner dari file.
        file_name (str): Nama file asli.
        content_type (str): Tipe MIME dari file (contoh: 'image/png').

    Returns:
        str: URL publik file yang diunggah.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("SUPABASE_URL dan SUPABASE_KEY tidak ditemukan di file .env")

    # Bersihkan nama file dari karakter aneh
    sanitized_name = "".join(c for c in file_name if c.isalnum() or c in "._-").strip()
    
    # Tambahkan keunikan agar nama file tidak bentrok di storage
    unique_name = f"{int(time.time())}_{sanitized_name}"
    
    # URL Endpoint Supabase Storage untuk upload objek:
    # POST /storage/v1/object/{bucket}/{filepath}
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{unique_name}"
    
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "ApiKey": SUPABASE_KEY,
        "Content-Type": content_type
    }
    
    try:
        # Melakukan upload biner secara langsung ke Supabase Storage REST API
        response = requests.post(upload_url, headers=headers, data=file_bytes)
        
        # Jika file sudah ada atau bucket belum dibuat, tangani error-nya
        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}: {response.text}")
            
        # Format URL publik Supabase Storage:
        # {SUPABASE_URL}/storage/v1/object/public/{bucket}/{filepath}
        public_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{unique_name}"
        return public_url
        
    except Exception as e:
        print(f"[Supabase Upload Error] Detail: {e}")
        raise Exception(f"Gagal mengunggah file ke Supabase Storage: {str(e)}")
