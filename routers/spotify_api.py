import os
import urllib.parse
import base64
from typing import Optional
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, HTMLResponse
import httpx
from bson import ObjectId

from services.mongo_service import pa_users_col

router = APIRouter(prefix="/v1/spotify", tags=["Spotify Integration"])

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
# Update to match your actual domain/port in production
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "https://myagentic-apps.fastapicloud.dev/v1/spotify/callback")

@router.get("/login")
async def spotify_login(request: Request, user_id: str = Query(..., description="The ID of the user starting the OAuth flow")):
    """
    Redirects the user to Spotify's authorization page.
    """
    if not SPOTIFY_CLIENT_ID:
        raise HTTPException(status_code=500, detail="SPOTIFY_CLIENT_ID not configured")

    scopes = [
        "user-read-currently-playing",
        "user-read-playback-state",
        "playlist-read-private",
        "playlist-read-collaborative",
        "user-top-read",
        "user-read-recently-played"
    ]
    
    # Simple state implementation containing user_id
    # In production, you'd want to store a secure random string and verify it
    import uuid
    state = f"{user_id}:::{uuid.uuid4().hex}"
    
    redirect_uri = str(request.base_url).rstrip("/") + "/v1/spotify/callback"
    # Force HTTPS for cloud environments that might drop the proxy protocol
    if "fastapicloud.dev" in redirect_uri and redirect_uri.startswith("http://"):
        redirect_uri = redirect_uri.replace("http://", "https://")
    
    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": " ".join(scopes),
        "show_dialog": "true"
    })
    
    return RedirectResponse(auth_url)

@router.get("/callback")
async def spotify_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    """
    Handles the callback from Spotify, exchanges code for tokens, and saves them.
    """
    if error:
        raise HTTPException(status_code=400, detail=f"Spotify Auth Error: {error}")
        
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")
        
    parts = state.split(":::")
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="Invalid state parameter")
        
    user_id = parts[0]
    
    # Convert user_id to ObjectId if possible
    try:
        if ObjectId.is_valid(user_id):
            user_query = {"_id": ObjectId(user_id)}
        else:
            user_query = {"$or": [{"_id": user_id}, {"email": user_id}]} # Fallbacks
    except Exception:
        user_query = {"_id": user_id}
        
    # Exchange code for token
    auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    
    # Dynamic redirect URI
    redirect_uri = str(request.base_url).rstrip("/") + "/v1/spotify/callback"
    if "fastapicloud.dev" in redirect_uri and redirect_uri.startswith("http://"):
        redirect_uri = redirect_uri.replace("http://", "https://")

    async with httpx.AsyncClient() as client:
        token_res = await client.post(
            "https://accounts.spotify.com/api/token",
            headers={"Authorization": f"Basic {b64_auth}"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri
            },
            timeout=10.0
        )
        
        if token_res.status_code != 200:
            raise HTTPException(
                status_code=500, 
                detail=f"Failed to get Spotify token: {token_res.text}"
            )
            
        token_data = token_res.json()
        
    # Save token to user's MongoDB record
    update_data = {
        "integrations.spotify.access_token": token_data.get("access_token"),
        "integrations.spotify.refresh_token": token_data.get("refresh_token"),
        "integrations.spotify.expires_in": token_data.get("expires_in"),
        "integrations.spotify.scope": token_data.get("scope"),
        "integrations.spotify.updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    }
    
    existing_user = await pa_users_col.find_one(user_query)
    
    if existing_user:
        result = await pa_users_col.update_one({"_id": existing_user["_id"]}, {"$set": update_data})
    else:
        upsert_query = {"_id": ObjectId(user_id)} if ObjectId.is_valid(user_id) else {"_id": user_id}
        result = await pa_users_col.update_one(upsert_query, {"$set": update_data}, upsert=True)
    
    success_html = f"""
    <html>
        <head>
            <title>Spotify Integration Success</title>
            <style>
                body {{ font-family: sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; background-color: #121212; color: #fff; }}
                .container {{ text-align: center; padding: 2rem; border-radius: 8px; background-color: #1DB954; }}
                h1 {{ margin: 0 0 1rem 0; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Integration Successful!</h1>
                <p>Your Spotify account has been successfully linked.</p>
                <p>You can close this window and return to the app.</p>
            </div>
        </body>
    </html>
    """
    
    return HTMLResponse(content=success_html)
