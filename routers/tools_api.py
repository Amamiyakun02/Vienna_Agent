"""Endpoint khusus tool-tools WebPortofolio/Vienna Agent.

Primary (baru, extensible):
    GET  /v1/tools/                  → daftar tool tersedia
    GET  /v1/tools/youtube/info      → metadata / preview
    POST /v1/tools/youtube/download  → unduh mp4/mp3 (streaming + cache)
    GET  /v1/tools/youtube/download  → varian GET (untuk <a href> langsung)
    GET  /v1/tools/youtube/status    → statistik cache

Alias legacy sesuai spek rancangan (YouTube Downloader §4):
    GET  /api/youtube/info
    POST /api/youtube/download

Tool berikutnya (mis. tiktok downloader, pdf tools, dsb.) tinggal tambah
section baru di router ini — tidak perlu prefix/router baru per tool.
"""

from __future__ import annotations

import asyncio
import time
import urllib.parse
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from services import youtube_service as yt
from services import media_service as md

router = APIRouter(prefix="/v1/tools", tags=["Tools"])
legacy_router = APIRouter(prefix="/api/youtube", tags=["Tools (legacy alias)"])
legacy_downloader_router = APIRouter(prefix="/api/downloader", tags=["Tools (legacy alias)"])

CHUNK_SIZE = 1024 * 1024  # 1 MB

# Rate limit: download 10/menit/IP (spek §6), info 30/menit/IP.
DL_LIMIT_PER_MIN = 10
INFO_LIMIT_PER_MIN = 30
_RATE_MEM: dict[str, list[float]] = {}
_RATE_GUARD = asyncio.Lock()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


async def _check_rate(request: Request, scope: str, limit: int) -> None:
    ip = _client_ip(request)
    key = f"rl:tools:{scope}:{ip}"
    # Coba Redis dulu (fail-open bila Redis mati).
    try:
        from services.redis_service import redis_client as _rc

        n = await _rc.incr(key)
        if n == 1:
            await _rc.expire(key, 65)
        if n > limit:
            raise HTTPException(status_code=429, detail="Rate limit terlampaui (10 request/menit). Coba lagi sebentar.")
        return
    except HTTPException:
        raise
    except Exception:
        pass
    # Fallback memori lokal.
    now = time.time()
    async with _RATE_GUARD:
        hits = [t for t in _RATE_MEM.get(key, []) if now - t < 60]
        if len(hits) >= limit:
            raise HTTPException(status_code=429, detail="Rate limit terlampaui (10 request/menit). Coba lagi sebentar.")
        hits.append(now)
        _RATE_MEM[key] = hits


def _http_from_yt_error(exc: Exception) -> HTTPException:
    status = getattr(exc, "status_code", 500)
    if status not in (400, 401, 404, 429, 413):
        status = 500
    return HTTPException(status_code=status, detail=str(exc))


def _safe_disposition(filename: str) -> str:
    safe = "".join(c for c in filename if c not in '\r\n"').strip() or "video.mp4"
    quoted = urllib.parse.quote(safe)
    return f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quoted}"


async def _file_streamer(path: Path):
    loop = asyncio.get_running_loop()
    with open(path, "rb") as f:
        while True:
            chunk = await loop.run_in_executor(None, f.read, CHUNK_SIZE)
            if not chunk:
                break
            yield chunk


async def _serve_download(url: str, fmt: str, quality: str, request: Request) -> StreamingResponse:
    await _check_rate(request, "youtube-dl", DL_LIMIT_PER_MIN)
    fmt = (fmt or "mp4").lower()
    if fmt not in ("mp4", "mp3"):
        raise HTTPException(status_code=400, detail="Format harus 'mp4' atau 'mp3'.")
    try:
        path, hit, info = await yt.download_video(url, fmt, quality)  # type: ignore[arg-type]
    except Exception as exc:
        raise _http_from_yt_error(exc) from exc
    # Jangan log URL lengkap (spek §6) — cukup video_id.
    print(f"[TOOLS/youtube] download id={info.get('video_id')} fmt={fmt} cache={'HIT' if hit else 'MISS'}")
    title = info.get("title") or info.get("video_id") or "youtube_video"
    filename = yt.download_filename(title, fmt)  # type: ignore[arg-type]
    return StreamingResponse(
        _file_streamer(path),
        media_type=yt.content_type_for(fmt),  # type: ignore[arg-type]
        headers={
            "Content-Disposition": _safe_disposition(filename),
            "X-Cache": "HIT" if hit else "MISS",
        },
    )


async def _serve_info(url: str, request: Request) -> dict:
    await _check_rate(request, "youtube-info", INFO_LIMIT_PER_MIN)
    try:
        info = await yt.get_video_info(url)
    except Exception as exc:
        raise _http_from_yt_error(exc) from exc
    if info.get("too_long"):
        raise HTTPException(
            status_code=400,
            detail=f"Video terlalu panjang ({info.get('duration_formatted')} > 30:00). Maks 30 menit.",
        )
    return {"status": "success", **info}


# ── Models ──────────────────────────────────────────────


class YouTubeDownloadRequest(BaseModel):
    url: str = Field(..., description="Tautan YouTube (watch / youtu.be / shorts / embed / live)")
    format: Literal["mp4", "mp3"] = Field("mp4", description="mp4 = video, mp3 = audio saja")
    quality: str = Field("720", description="mp4: 360|480|720|1080|best. mp3: diabaikan.")


class DownloaderRequest(BaseModel):
    url: str = Field(..., description="Tautan YouTube / Instagram / Threads / Facebook")
    format: Literal["mp4", "mp3"] = Field("mp4", description="mp4 = video, mp3 = audio saja")
    quality: str = Field("720", description="mp4: 360|480|720|1080|best. mp3: diabaikan.")


# ── Primary routes: /v1/tools/... ───────────────────────


@router.get("/", summary="Daftar tool tersedia")
async def list_tools():
    return {
        "status": "success",
        "tools": [
            {
                "name": "youtube-downloader",
                "description": "Unduh video/Shorts YouTube sebagai mp4 atau mp3.",
                "routes": [
                    "GET /v1/tools/youtube/info?url=...",
                    "POST /v1/tools/youtube/download",
                    "GET /v1/tools/youtube/download?url=...&format=mp4&quality=720",
                    "GET /v1/tools/youtube/status",
                ],
            },
            {
                "name": "video-downloader",
                "description": "Unduh video YouTube, Instagram, Threads, Facebook (deteksi otomatis).",
                "routes": [
                    "GET /v1/tools/downloader/info?url=...",
                    "POST /v1/tools/downloader/download",
                    "GET /v1/tools/downloader/download?url=...&format=mp4&quality=720",
                    "GET /v1/tools/downloader/platforms",
                ],
            },
        ],
    }


@router.get("/youtube/info", summary="Preview metadata YouTube")
async def youtube_info(request: Request, url: str = Query(..., description="Tautan YouTube")):
    return await _serve_info(url, request)


@router.post("/youtube/download", summary="Unduh video/audio YouTube (POST)")
async def youtube_download_post(payload: YouTubeDownloadRequest, request: Request):
    return await _serve_download(payload.url, payload.format, payload.quality, request)


@router.get("/youtube/download", summary="Unduh video/audio YouTube (GET)")
async def youtube_download_get(
    request: Request,
    url: str = Query(..., description="Tautan YouTube"),
    format: str = Query("mp4", description="mp4 atau mp3"),
    quality: str = Query("720", description="360|480|720|1080|best"),
):
    return await _serve_download(url, format, quality, request)


@router.get("/youtube/status", summary="Status cache YouTube downloader")
async def youtube_status():
    return {"status": "success", **yt.cache_status()}


# ── Generic routes: /v1/tools/downloader/... (multi-platform) ──


async def _serve_md_download(url: str, fmt: str, quality: str, request: Request) -> StreamingResponse:
    await _check_rate(request, "downloader-dl", DL_LIMIT_PER_MIN)
    fmt = (fmt or "mp4").lower()
    if fmt not in ("mp4", "mp3"):
        raise HTTPException(status_code=400, detail="Format harus 'mp4' atau 'mp3'.")
    try:
        path, hit, info = await md.download_media(url, fmt, quality)  # type: ignore[arg-type]
    except Exception as exc:
        raise _http_from_yt_error(exc) from exc
    # Jangan log URL lengkap (spek §6) — cukup platform + id.
    print(f"[TOOLS/downloader] download platform={info.get('platform')} id={info.get('id')} fmt={fmt} cache={'HIT' if hit else 'MISS'}")
    title = info.get("title") or info.get("id") or "video"
    filename = md.download_filename(title, fmt)  # type: ignore[arg-type]
    return StreamingResponse(
        _file_streamer(path),
        media_type=md.content_type_for(fmt),  # type: ignore[arg-type]
        headers={
            "Content-Disposition": _safe_disposition(filename),
            "X-Cache": "HIT" if hit else "MISS",
            "X-Platform": str(info.get("platform") or "unknown"),
        },
    )


async def _serve_md_info(url: str, request: Request) -> dict:
    await _check_rate(request, "downloader-info", INFO_LIMIT_PER_MIN)
    try:
        info = await md.get_media_info(url)
    except Exception as exc:
        raise _http_from_yt_error(exc) from exc
    if info.get("too_long"):
        raise HTTPException(
            status_code=400,
            detail=f"Media terlalu panjang ({info.get('duration_formatted')} > 30:00). Maks 30 menit.",
        )
    return {"status": "success", **info}


@router.get("/downloader/info", summary="Preview metadata (semua platform)")
async def downloader_info(request: Request, url: str = Query(..., description="Tautan YouTube / Instagram / Threads / Facebook")):
    return await _serve_md_info(url, request)


@router.post("/downloader/download", summary="Unduh video/audio (semua platform, POST)")
async def downloader_download_post(payload: DownloaderRequest, request: Request):
    return await _serve_md_download(payload.url, payload.format, payload.quality, request)


@router.get("/downloader/download", summary="Unduh video/audio (semua platform, GET)")
async def downloader_download_get(
    request: Request,
    url: str = Query(..., description="Tautan YouTube / Instagram / Threads / Facebook"),
    format: str = Query("mp4", description="mp4 atau mp3"),
    quality: str = Query("720", description="360|480|720|1080|best"),
):
    return await _serve_md_download(url, format, quality, request)


@router.get("/downloader/platforms", summary="Daftar platform + status cookies")
async def downloader_platforms():
    return {"status": "success", "platforms": md.supported_platforms()}


# ── Legacy alias: /api/youtube/... (sesuai spek rancangan §4) ──


@legacy_router.get("/info", summary="[alias] Preview metadata YouTube")
async def legacy_youtube_info(request: Request, url: str = Query(...)):
    return await _serve_info(url, request)


@legacy_router.post("/download", summary="[alias] Unduh video/audio YouTube")
async def legacy_youtube_download(payload: YouTubeDownloadRequest, request: Request):
    return await _serve_download(payload.url, payload.format, payload.quality, request)


@legacy_router.get("/download", summary="[alias] Unduh via GET")
async def legacy_youtube_download_get(
    request: Request,
    url: str = Query(...),
    format: str = Query("mp4"),
    quality: str = Query("720"),
):
    return await _serve_download(url, format, quality, request)


# ── Legacy alias: /api/downloader/... (sesuai spek rancangan §4) ──


@legacy_downloader_router.get("/info", summary="[alias] Preview metadata (semua platform)")
async def legacy_downloader_info(request: Request, url: str = Query(...)):
    return await _serve_md_info(url, request)


@legacy_downloader_router.post("/download", summary="[alias] Unduh video/audio (semua platform)")
async def legacy_downloader_download(payload: DownloaderRequest, request: Request):
    return await _serve_md_download(payload.url, payload.format, payload.quality, request)


@legacy_downloader_router.get("/download", summary="[alias] Unduh via GET (semua platform)")
async def legacy_downloader_download_get(
    request: Request,
    url: str = Query(...),
    format: str = Query("mp4"),
    quality: str = Query("720"),
):
    return await _serve_md_download(url, format, quality, request)


@legacy_downloader_router.get("/platforms", summary="[alias] Daftar platform")
async def legacy_downloader_platforms():
    return {"status": "success", "platforms": md.supported_platforms()}
