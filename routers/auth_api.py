import os
import hashlib
import base64
import urllib.request
import json
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, status
from bson import ObjectId
from pydantic import BaseModel
import jwt

# Cryptography primitives for Google RSA JWK signature verification
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

from models.schemas import UserRegisterRequest, UserLoginRequest, SessionMigrateRequest
from services.mongo_service import users_col, get_user_by_email, migrate_anonymous_session

router = APIRouter(prefix="/v1/auth", tags=["Authentication"])

JWT_SECRET = os.getenv("JWT_SECRET", "super-secret-jwt-key-aimer-future-2026-06-02")
JWT_ALGORITHM = "HS256"

class GoogleLoginRequest(BaseModel):
    credential: str

# ──────────────────────────────────────────────
# JWT & CRYPTOGRAPHY HELPERS
# ──────────────────────────────────────────────

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Generate signed JWT access token for our application."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(days=7)  # 7 days expiration
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)

def hash_password(password: str) -> str:
    """Hash password using PBKDF2-HMAC-SHA256 with a random salt."""
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt,
        100000  # 100k iterations is cryptographically secure
    )
    return f"pbkdf2:sha256:100000${salt.hex()}${key.hex()}"

def verify_password(password: str, hashed: str) -> bool:
    """Verify a password against a PBKDF2 hash."""
    try:
        parts = hashed.split('$')
        if len(parts) != 3:
            return False
        algo_info, salt_hex, key_hex = parts
        salt = bytes.fromhex(salt_hex)
        expected_key = bytes.fromhex(key_hex)
        
        algo_parts = algo_info.split(':')
        iterations = int(algo_parts[2])
        
        actual_key = hashlib.pbkdf2_hmac(
            'sha256',
            password.encode('utf-8'),
            salt,
            iterations
        )
        return actual_key == expected_key
    except Exception:
        return False

async def verify_firebase_id_token(token: str) -> dict:
    """Verify Firebase ID Token signature, audience, and issuer."""
    # 1. Extract kid from unverified header
    try:
        headers = jwt.get_unverified_header(token)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Token Firebase tidak valid: {e}")
        
    kid = headers.get("kid")
    if not kid:
        raise HTTPException(status_code=400, detail="Token Firebase tidak memiliki header 'kid'")

    # 2. Fetch Firebase public certificates (x509 format)
    try:
        from services.redis_service import redis_client
        # Try fetching from Redis first
        cached_certs = await redis_client.get("firebase_public_certs")
        if cached_certs:
            certs = json.loads(cached_certs)
        else:
            req = urllib.request.Request(
                "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com",
                headers={"User-Agent": "Aimer-Agentic-App"}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                headers_info = response.info()
                # Parse Cache-Control to get max-age
                cache_control = headers_info.get("Cache-Control", "")
                max_age = 21600 # default fallback 6 hours
                if "max-age=" in cache_control:
                    try:
                        parts = cache_control.split("max-age=")
                        max_age = int(parts[1].split(",")[0].strip())
                    except Exception:
                        pass
                
                certs_data = response.read().decode()
                certs = json.loads(certs_data)
                
                # Store in Redis with TTL
                await redis_client.setex("firebase_public_certs", max_age, certs_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal mengunduh sertifikat publik Firebase: {e}")

    # 3. Find certificate for kid
    cert_pem = certs.get(kid)
    if not cert_pem:
        raise HTTPException(status_code=401, detail="Key ID (kid) tidak valid atau telah kadaluarsa.")

    # 4. Decode & Verify
    project_id = "aimer-project1"
    expected_iss = f"https://securetoken.google.com/{project_id}"
    
    try:
        from cryptography.x509 import load_pem_x509_certificate
        cert_obj = load_pem_x509_certificate(cert_pem.encode(), default_backend())
        public_key = cert_obj.public_key()

        decoded = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=project_id,
            issuer=expected_iss,
            options={
                "verify_aud": True,
                "verify_iss": True,
                "verify_exp": True
            }
        )
        return decoded
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Sesi masuk Google Firebase telah kadaluarsa.")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Otentikasi Firebase gagal: {e}")

# ──────────────────────────────────────────────
# ENDPOINTS
# ──────────────────────────────────────────────

@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register_user(payload: UserRegisterRequest):
    """
    Registers a new member (customer) to the gadget store.
    Encrypts password securely and returns a JWT access token.
    """
    email_clean = payload.email.strip().lower()
    
    existing = await get_user_by_email(email_clean)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email sudah terdaftar. Silakan masuk menggunakan akun Anda."
        )
        
    hashed = hash_password(payload.password)
    
    new_user = {
        "_id": ObjectId(),
        "name": payload.name.strip(),
        "email": email_clean,
        "phone": payload.phone.strip() if payload.phone else None,
        "password_hash": hashed,
        "role": "customer",
        "avatar_url": f"https://api.dicebear.com/7.x/bottts/svg?seed={payload.name.strip()}",
        "preferences": {
            "preferred_brands": [],
            "budget_range": None,
            "preferred_categories": []
        },
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc)
    }
    
    await users_col.insert_one(new_user)
    
    # Generate our JWT token
    token_data = {
        "id": str(new_user["_id"]),
        "email": new_user["email"],
        "role": new_user["role"]
    }
    access_token = create_access_token(token_data)
    
    return {
        "status": "success",
        "message": "Registrasi akun member berhasil!",
        "access_token": access_token,
        "user": {
            "id": str(new_user["_id"]),
            "name": new_user["name"],
            "email": new_user["email"],
            "phone": new_user["phone"],
            "role": new_user["role"],
            "avatar_url": new_user["avatar_url"]
        }
    }

@router.post("/login")
async def login_user(payload: UserLoginRequest):
    """
    Authenticates a user using email & password and returns a JWT access token.
    """
    email_clean = payload.email.strip().lower()
    
    user = await get_user_by_email(email_clean)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email atau kata sandi Anda salah."
        )
        
    is_valid = verify_password(payload.password, user.get("password_hash", ""))
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email atau kata sandi Anda salah."
        )
        
    if not user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akun Anda dinonaktifkan oleh administrator."
        )
        
    # Generate our JWT token
    token_data = {
        "id": str(user["_id"]),
        "email": user["email"],
        "role": user["role"]
    }
    access_token = create_access_token(token_data)
    
    return {
        "status": "success",
        "message": "Login berhasil! Selamat datang kembali.",
        "access_token": access_token,
        "user": {
            "id": str(user["_id"]),
            "name": user["name"],
            "email": user["email"],
            "phone": user.get("phone"),
            "role": user["role"],
            "avatar_url": user.get("avatar_url")
        }
    }

@router.post("/google")
async def login_google(payload: GoogleLoginRequest):
    """
    Authenticates Google credential ID Token and logs/registers user.
    """
    # 1. Verify Google token signature & contents (via Firebase ID Token)
    google_data = await verify_firebase_id_token(payload.credential)
    
    email = google_data.get("email", "").strip().lower()
    name = google_data.get("name", "").strip()
    picture = google_data.get("picture", "")
    
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Otentikasi Google tidak mengembalikan data email yang valid."
        )
        
    # 2. Check if user already exists
    user = await get_user_by_email(email)
    
    if not user:
        # User does not exist -> Auto Register
        new_user = {
            "_id": ObjectId(),
            "name": name,
            "email": email,
            "phone": None,
            "password_hash": "",  # Empty for Google Sign-In
            "role": "customer",
            "avatar_url": picture or f"https://api.dicebear.com/7.x/bottts/svg?seed={name}",
            "is_active": True,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc)
        }
        await users_col.insert_one(new_user)
        user = new_user
        msg = "Registrasi & Login Akun Google berhasil!"
    else:
        # Check active state
        if not user.get("is_active", True):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Akun Anda dinonaktifkan oleh administrator."
            )
        msg = "Login via Google berhasil! Selamat datang kembali."
        
    # 3. Generate our JWT Token
    token_data = {
        "id": str(user["_id"]),
        "email": user["email"],
        "role": user["role"]
    }
    access_token = create_access_token(token_data, expires_delta=timedelta(days=365))
    
    return {
        "status": "success",
        "message": msg,
        "access_token": access_token,
        "user": {
            "id": str(user["_id"]),
            "name": user["name"],
            "email": user["email"],
            "phone": user.get("phone"),
            "role": user["role"],
            "avatar_url": user.get("avatar_url")
        }
    }

@router.post("/migrate-session")
async def migrate_session(payload: SessionMigrateRequest):
    """
    Migrates chat sessions from anonymous status to a registered customer.
    """
    if not payload.temp_session_id or not payload.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Sesi sementara dan User ID tidak boleh kosong."
        )
        
    result = await migrate_anonymous_session(
        temp_session_id=payload.temp_session_id,
        user_id=payload.user_id
    )
    
    if result["status"] == "error":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result["message"]
        )
        
    return result


class UpdateProfileRequest(BaseModel):
    user_id: str
    phone: str
    name: Optional[str] = None
    password: Optional[str] = None


@router.post("/update-profile")
async def update_profile(payload: UpdateProfileRequest):
    """
    Updates registered customer's name and WhatsApp number.
    """
    try:
        user_oid = ObjectId(payload.user_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ID Pengguna tidak valid."
        )

    # Check if user exists
    user = await users_col.find_one({"_id": user_oid})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pengguna tidak ditemukan."
        )

    phone_clean = payload.phone.strip()
    if not phone_clean:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nomor WhatsApp tidak boleh kosong."
        )

    update_data = {
        "phone": phone_clean,
        "updated_at": datetime.now(timezone.utc)
    }
    
    if payload.name and payload.name.strip():
        update_data["name"] = payload.name.strip()

    if payload.password and payload.password.strip():
        update_data["password_hash"] = hash_password(payload.password.strip())

    await users_col.update_one({"_id": user_oid}, {"$set": update_data})

    # Fetch updated user
    updated_user = await users_col.find_one({"_id": user_oid})

    return {
        "status": "success",
        "message": "Profil berhasil diperbarui!",
        "user": {
            "id": str(updated_user["_id"]),
            "name": updated_user["name"],
            "email": updated_user["email"],
            "phone": updated_user.get("phone"),
            "role": updated_user["role"],
            "avatar_url": updated_user.get("avatar_url")
        }
    }


class ResetPasswordWaRequest(BaseModel):
    email: str


@router.post("/reset-password-wa")
async def reset_password_wa(payload: ResetPasswordWaRequest):
    """
    Generates a temporary 8-character password, updates it in MongoDB,
    and sends it to the user's registered WhatsApp number via Fonnte.
    """
    email_clean = payload.email.strip().lower()
    
    # 1. Find user in MongoDB
    user = await get_user_by_email(email_clean)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Email tidak terdaftar."
        )
        
    # 2. Check if user is superadmin or sales
    if user.get("role") not in ["superadmin", "sales"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses ditolak: Hanya staf yang dapat meminta reset password."
        )
        
    # 3. Get registered WhatsApp/phone number
    phone = user.get("phone")
    if not phone:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nomor WhatsApp tidak terdaftar untuk akun ini. Silakan hubungi Administrator Utama."
        )
        
    phone_clean = phone.strip()
    if phone_clean.startswith("0"):
        phone_clean = "62" + phone_clean[1:]
        
    # 4. Generate random temporary password
    import random
    import string
    import httpx
    
    temp_pass = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
    hashed = hash_password(temp_pass)
    
    # Update password in database
    await users_col.update_one(
        {"_id": user["_id"]},
        {"$set": {"password_hash": hashed, "updated_at": datetime.now(timezone.utc)}}
    )
    
    # 5. Send WhatsApp message via Fonnte
    message = (
        f"Halo {user.get('name')},\n\n"
        f"Anda telah meminta reset password untuk masuk ke Dashboard IRIN CELLULAR.\n\n"
        f"Password sementara Anda adalah: {temp_pass}\n\n"
        f"Silakan masuk menggunakan password tersebut dan segera ganti kata sandi Anda di menu Manajemen Data demi keamanan akun Anda."
    )
    
    fonnte_token = os.getenv("WHATSAPP_GATEWAY_TOKEN")
    if not fonnte_token:
        print("[ERROR] Token Fonnte (WHATSAPP_GATEWAY_TOKEN) tidak ditemukan di environment.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Sistem WhatsApp Gateway tidak terkonfigurasi di server."
        )
        
    url = "https://api.fonnte.com/send"
    headers = {
        "Authorization": fonnte_token
    }
    payload_data = {
        "target": phone_clean,
        "message": message
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, data=payload_data, timeout=15.0)
            res_json = response.json()
            if not res_json.get("status"):
                print(f"[ERROR] Fonnte API failure: {res_json}")
                raise Exception(res_json.get("reason", "Gagal mengirim pesan via Fonnte"))
    except Exception as e:
        print(f"[ERROR] Gagal mengirim pesan WA via Fonnte: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gagal mengirim pesan WhatsApp reset password: {str(e)}"
        )
        
    return {
        "status": "success",
        "message": f"Password sementara berhasil dikirim ke nomor WhatsApp Anda ({phone})."
    }

