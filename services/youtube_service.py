"""Shim kompatibilitas — implementasi pindah ke services/media_service.py.

Endpoint lama (/v1/tools/youtube/*, /api/youtube/*) tetap jalan lewat modul ini
dan terkunci ke platform youtube. Kode baru memakai services.media_service.
"""

from __future__ import annotations

from typing import Any

from services import media_service as _m

# Nama lama tetap bisa diimpor.
YouTubeFormat = _m.MediaFormat
YouTubeError = _m.MediaError
InvalidYouTubeURLError = _m.InvalidMediaURLError
VideoNotFoundError = _m.MediaNotFoundError
VideoTooLongError = _m.MediaTooLongError
FileTooLargeError = _m.FileTooLargeError
DownloadFailedError = _m.DownloadFailedError
ALLOWED_HOSTS = _m.PLATFORM_HOSTS["youtube"]
MAX_DURATION_SEC = _m.MAX_DURATION_SEC
MAX_FILE_BYTES = _m.MAX_FILE_BYTES
YTDLP_TIMEOUT_SEC = _m.YTDLP_TIMEOUT_SEC
QUALITY_CHOICES = _m.QUALITY_CHOICES

is_shorts_url = _m.is_shorts_url
sanitize_filename = _m.sanitize_filename
content_type_for = _m.content_type_for
download_filename = _m.download_filename


def extract_video_id(url: str) -> str | None:
    raw = _m.extract_media_id("youtube", url)
    return None if raw.startswith("url_") else raw


def is_allowed_url(url: str) -> bool:
    return _m.detect_platform(url) == "youtube" and extract_video_id(url) is not None


def validate_url_or_raise(url: str) -> str:
    platform, media_id = _m.validate_url_or_raise(url)
    if platform != "youtube":
        raise InvalidYouTubeURLError(
            "URL bukan YouTube — hanya tautan YouTube (termasuk Shorts) yang "
            "didukung di endpoint ini. Pakai /v1/tools/downloader untuk "
            "Instagram/Threads/Facebook."
        )
    vid = extract_video_id(url)
    if not vid:
        raise InvalidYouTubeURLError("Tidak dapat menemukan ID video dari URL.")
    return vid


def cache_filename(video_id: str, fmt: str, quality: str) -> str:
    return _m.cache_filename("youtube", video_id, fmt, quality)


async def get_video_info(url: str) -> dict[str, Any]:
    validate_url_or_raise(url)
    info = await _m.get_media_info(url)
    return {
        "video_id": info["id"],
        **{k: v for k, v in info.items() if k != "id"},
    }


async def download_video(url: str, fmt: str = "mp4", quality: str = "720"):
    validate_url_or_raise(url)
    path, hit, info = await _m.download_media(url, fmt, quality)  # type: ignore[arg-type]
    return path, hit, {"video_id": info.get("id"), **info}


def cache_status() -> dict[str, Any]:
    return _m.cache_status()
