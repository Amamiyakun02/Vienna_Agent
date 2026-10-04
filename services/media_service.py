"""Downloader media multi-platform via yt-dlp: YouTube, Instagram, Threads, Facebook.

Sesuai spek vault "Video Downloader Multi-Platform" (§1-§6):
- Deteksi platform otomatis dari URL + whitelist domain per platform (anti-SSRF).
- Satu pola untuk semua platform: unduh sekali ke file cache
  (dedup key {platform}:{media_id}:{format}:{quality}), client terima via streaming.
- Cookies opsional per platform untuk konten login-walled (IG/FB):
  taruh file Netscape di <data>/cookies/<platform>.txt (jangan di-commit).
- Batasan: durasi maks 30 mnt, ukuran maks 500 MB, timeout 10 mnt.

Modul ini implementasi generik; services/youtube_service.py tinggal shim
kompatibilitas di atasnya (endpoint lama /v1/tools/youtube + /api/youtube
tetap jalan dan terkunci ke platform youtube).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any, Literal

from services.file_cache import get_cache

MediaFormat = Literal["mp4", "mp3"]
Platform = Literal["youtube", "instagram", "threads", "facebook"]

PLATFORM_HOSTS: dict[str, set[str]] = {
    "youtube": {
        "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
        "youtu.be", "www.youtu.be",
        "youtube-nocookie.com", "www.youtube-nocookie.com",
    },
    "instagram": {"instagram.com", "www.instagram.com", "m.instagram.com"},
    "threads": {"threads.com", "www.threads.com", "threads.net", "www.threads.net"},
    "facebook": {
        "facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com",
        "fb.watch", "www.fb.watch",
    },
}

HOST_TO_PLATFORM: dict[str, str] = {
    host: platform for platform, hosts in PLATFORM_HOSTS.items() for host in hosts
}

MAX_DURATION_SEC = 30 * 60  # 30 menit
MAX_FILE_BYTES = 500 * 1024 * 1024  # 500 MB
YTDLP_TIMEOUT_SEC = 10 * 60  # 10 menit

QUALITY_CHOICES = ("360", "480", "720", "1080", "best")

_YT_ID_RES = [
    re.compile(r"[?&]v=([A-Za-z0-9_-]{6,})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{6,})"),
    re.compile(r"/shorts/([A-Za-z0-9_-]{6,})"),
    re.compile(r"/embed/([A-Za-z0-9_-]{6,})"),
    re.compile(r"/live/([A-Za-z0-9_-]{6,})"),
    re.compile(r"/v/([A-Za-z0-9_-]{6,})"),
]

# Cookies: <data>/cookies/{instagram,facebook,threads,youtube}.txt (format Netscape,
# mis. diekspor via ekstensi "Get cookies.txt"). Tidak di-commit, nilainya tak di-log.
# NOTE: getenv string kosong harus difallback SEBELUM Path() — Path("") == Path(".")
# yang truthy dan membuat `or` tidak pernah jatuh ke default.
_explicit_cookies_dir = (os.getenv("DOWNLOADER_COOKIES_DIR") or "").strip()
if _explicit_cookies_dir:
    COOKIES_DIR = Path(_explicit_cookies_dir)
else:
    _data_dir = (os.getenv("DATA_DIR") or "").strip() or Path(__file__).resolve().parent.parent / "data"
    COOKIES_DIR = Path(_data_dir) / "cookies"

# Cookies via env (wajib di cloud — filesystem ephemeral, file manual hilang
# tiap redeploy). Isi Netscape cookies.txt, satu env per platform:
#   YOUTUBE_COOKIES / INSTAGRAM_COOKIES / THREADS_COOKIES / FACEBOOK_COOKIES
# File di server (data/cookies/<platform>.txt) tetap didukung untuk lokal.
_COOKIES_ENV = {
    "youtube": "YOUTUBE_COOKIES",
    "instagram": "INSTAGRAM_COOKIES",
    "threads": "THREADS_COOKIES",
    "facebook": "FACEBOOK_COOKIES",
}


def _materialize_env_cookies() -> None:
    """Tulis cookies dari env ke COOKIES_DIR bila isi berubah.

    Idempoten & murah: tulis ulang hanya bila konten beda. Nilai env
    TIDAK pernah di-log (kredensial login YouTube/IG).
    """
    try:
        COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for platform, env_name in _COOKIES_ENV.items():
        content = (os.getenv(env_name, "") or "").strip()
        if not content:
            continue
        target = COOKIES_DIR / f"{platform}.txt"
        try:
            existing = target.read_text(encoding="utf-8", errors="ignore") if target.is_file() else ""
            if existing != content:
                target.write_text(content, encoding="utf-8")
        except OSError:
            continue


class MediaError(Exception):
    status_code = 500


class InvalidMediaURLError(MediaError):
    status_code = 400


class LoginRequiredError(MediaError):
    status_code = 401


class MediaNotFoundError(MediaError):
    status_code = 404


class MediaTooLongError(MediaError):
    status_code = 400


class FileTooLargeError(MediaError):
    status_code = 413


class DownloadFailedError(MediaError):
    status_code = 500


def detect_platform(url: str) -> Platform | None:
    """Platform dari hostname, atau None bila di luar whitelist."""
    try:
        host = (urllib.parse.urlparse((url or "").strip()).hostname or "").lower()
    except Exception:
        return None
    plat = HOST_TO_PLATFORM.get(host)
    return plat  # type: ignore[return-value]


def _extract_youtube_id(url: str) -> str | None:
    for rx in _YT_ID_RES:
        m = rx.search(url)
        if m:
            return m.group(1).split("?")[0].split("&")[0].split("/")[0]
    try:
        parts = urllib.parse.urlparse(url)
        segs = [s for s in parts.path.split("/") if s]
        if parts.hostname in ("youtu.be", "www.youtu.be") and segs:
            cand = re.sub(r"[^A-Za-z0-9_-]", "", segs[0])
            return cand or None
    except Exception:
        pass
    return None


def _extract_shortcode(url: str) -> str | None:
    """Shortcode IG (/reel/, /p/, /tv/) atau segmen akhir path."""
    try:
        parts = urllib.parse.urlparse((url or "").strip())
        segs = [s for s in parts.path.split("/") if s]
        if not segs:
            return None
        for key in ("reel", "reels", "p", "tv"):
            if key in segs:
                i = segs.index(key)
                if i + 1 < len(segs):
                    cand = re.sub(r"[^A-Za-z0-9_-]", "", segs[i + 1])
                    if cand:
                        return cand
        cand = re.sub(r"[^A-Za-z0-9_.-]", "", segs[-1])
        return cand[:64] or None
    except Exception:
        return None


def _extract_fb_id(url: str) -> str | None:
    try:
        parts = urllib.parse.urlparse((url or "").strip())
        qs = urllib.parse.parse_qs(parts.query)
        if qs.get("v"):
            cand = re.sub(r"\D", "", qs["v"][0])
            if cand:
                return cand
        segs = [s for s in parts.path.split("/") if s]
        if not segs:
            return None
        if parts.hostname in ("fb.watch", "www.fb.watch"):
            cand = re.sub(r"[^A-Za-z0-9_-]", "", segs[-1])
            return cand or None
        for key in ("reel", "watch", "share", "v", "videos"):
            if key in segs:
                i = segs.index(key)
                rest = [s for s in segs[i + 1 :] if s not in ("v", "r")]
                if rest:
                    cand = re.sub(r"[^A-Za-z0-9_.-]", "", rest[-1])
                    if cand:
                        return cand[:64]
        cand = re.sub(r"[^A-Za-z0-9_.-]", "", segs[-1])
        return cand[:64] or None
    except Exception:
        return None


def _extract_threads_id(url: str) -> str | None:
    try:
        parts = urllib.parse.urlparse((url or "").strip())
        segs = [s for s in parts.path.split("/") if s]
        if not segs:
            return None
        if "post" in segs:
            i = segs.index("post")
            if i + 1 < len(segs):
                cand = re.sub(r"[^A-Za-z0-9_-]", "", segs[i + 1])
                if cand:
                    return cand
        cand = re.sub(r"[^A-Za-z0-9_.-]", "", segs[-1])
        return cand[:64] or None
    except Exception:
        return None


def extract_media_id(platform: Platform, url: str) -> str:
    """ID media untuk cache key. Fallback: hash URL (tetap dedup URL sama)."""
    extractors = {
        "youtube": _extract_youtube_id,
        "instagram": _extract_shortcode,
        "facebook": _extract_fb_id,
        "threads": _extract_threads_id,
    }
    raw = (extractors[platform])(url) or ""
    if raw:
        return raw[:64]
    digest = hashlib.sha256(((url or "").strip()).encode()).hexdigest()[:16]
    return f"url_{digest}"


def validate_url_or_raise(url: str) -> tuple[Platform, str]:
    """Return (platform, media_id); raise InvalidMediaURLError bila tak didukung."""
    if not url or not isinstance(url, str):
        raise InvalidMediaURLError("URL wajib diisi.")
    platform = detect_platform(url)
    if platform is None:
        raise InvalidMediaURLError(
            "URL tidak valid — platform yang didukung: YouTube, Instagram, "
            "Threads, dan Facebook."
        )
    media_id = extract_media_id(platform, url)
    if platform == "youtube" and (len(media_id) < 6 or media_id.startswith("url_")):
        raise InvalidMediaURLError("Tidak dapat menemukan ID video dari URL YouTube.")
    return platform, media_id


def is_shorts_url(url: str) -> bool:
    return "/shorts/" in (url or "")


def sanitize_filename(name: str, fallback: str = "video") -> str:
    base = "".join(c if c.isalnum() or c in (" ", "-", "_", ".") else "_" for c in (name or ""))
    base = re.sub(r"\s+", " ", base).strip().strip(".") or fallback
    return base[:100]


def cache_filename(platform: Platform, media_id: str, fmt: str, quality: str) -> str:
    ext = "mp3" if fmt == "mp3" else "mp4"
    q = quality if fmt == "mp4" else "audio"
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", media_id)[:32] or "media"
    return f"{platform}_{safe_id}_{fmt}_{q}.{ext}"


def cookiefile_for(platform: Platform) -> str | None:
    """Path cookie file bila ada (per platform, lalu generik). None bila tak ada."""
    candidates = [COOKIES_DIR / f"{platform}.txt", COOKIES_DIR / "cookies.txt"]
    for cand in candidates:
        try:
            if cand.is_file() and cand.stat().st_size > 0:
                return str(cand)
        except OSError:
            continue
    return None


def cookies_status() -> dict[str, bool]:
    return {p: cookiefile_for(p) is not None for p in ("youtube", "instagram", "threads", "facebook")}


def _ffmpeg_path() -> str | None:
    import shutil

    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return exe
    except Exception:
        pass
    return None


def _has_ffmpeg() -> bool:
    return _ffmpeg_path() is not None


def _format_selector(platform: Platform, fmt: MediaFormat, quality: str) -> str:
    if fmt == "mp3":
        return "bestaudio/best"
    if platform == "youtube" and _has_ffmpeg():
        if quality == "best":
            return "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b/best"
        q = re.sub(r"\D", "", quality or "") or "720"
        return (
            f"bv*[height<={q}][ext=mp4]+ba[ext=m4a]/b[height<={q}][ext=mp4]/"
            f"bv*[height<={q}]+ba/b[height<={q}]/b/best"
        )
    # Non-YouTube (biasanya sudah progresif) atau tanpa ffmpeg:
    # single-file agar tak butuh merge.
    if quality == "best":
        return "b[ext=mp4]/b/best"
    q = re.sub(r"\D", "", quality or "") or "720"
    return f"b[height<={q}][ext=mp4]/b[height<={q}]/b[ext=mp4]/b/best"


def _has_curl_cffi() -> bool:
    try:
        import curl_cffi  # noqa: F401
        return True
    except ImportError:
        return False


def _base_ydl_opts(platform: Platform) -> dict[str, Any]:
    # Cookies dari env (cloud) dimaterialisasi dulu tiap request — murah & idempoten.
    _materialize_env_cookies()
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
        "cookiefile": None,
        "writeinfojson": False,
        "writethumbnail": False,
        "writesubtitles": False,
    }
    if platform == "youtube":
        # IP datacenter cloud sering kena "Sign in to confirm you're not a bot"
        # pada player web. Urutan fallback: coba klien mobile/TV dulu yang
        # jarang di-challenge, web paling terakhir.
        opts["extractor_args"] = {
            "youtube": {"player_client": ["android", "ios", "tv", "web"]},
        }
        # TLS fingerprint ala Chrome asli (butuh paket curl-cffi).
        # yt-dlp baru wajib objek ImpersonateTarget (string mentah crash di init).
        if _has_curl_cffi():
            try:
                from yt_dlp.networking.impersonate import ImpersonateTarget
                opts["impersonate"] = ImpersonateTarget(client="chrome", os="windows")
            except Exception:
                pass
    # Proxy opsional (mis. VPS Indonesia) untuk konten yang diblokir
    # di region server cloud: isi env DOWNLOADER_PROXY, mis.
    # "http://user:pass@103.x.x.x:8080" atau "socks5://127.0.0.1:1080".
    _proxy = (os.getenv("DOWNLOADER_PROXY") or "").strip()
    if _proxy:
        opts["proxy"] = _proxy
    ffmpeg = _ffmpeg_path()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    cookiefile = cookiefile_for(platform)
    if cookiefile:
        opts["cookiefile"] = cookiefile
    return opts


def _map_ytdlp_error(exc: Exception, platform: Platform, media_id: str) -> MediaError:
    msg = str(exc)
    low = msg.lower()
    if any(k in low for k in ("login", "log in", "logged in", "cookies", "cookie", "checkpoint")):
        return LoginRequiredError(
            f"Konten {platform} ini butuh login / privat (id={media_id}). "
            "Di server: isi env YOUTUBE_COOKIES (format Netscape cookies.txt) "
            "atau pasang file data/cookies/<platform>.txt lalu coba lagi."
        )
    if any(k in low for k in ("private", "not available", "404", "removed", "deleted", "not found")):
        return MediaNotFoundError(f"Media tidak ditemukan / privat / dihapus ({platform} id={media_id}).")
    if any(k in low for k in ("claimed content", "copyright", "track claimed")):
        return MediaNotFoundError(
            f"Video diblokir pemegang hak cipta di region server ({platform} id={media_id}). "
            "Coba dari jaringan/region lain, atau set env DOWNLOADER_PROXY ke proxy region yang diizinkan."
        )
    if any(k in low for k in ("region", "blocked", "country", "geo")):
        return MediaNotFoundError(f"Media dibatasi wilayah / region-blocked ({platform} id={media_id}).")
    if "timed out" in low or "timeout" in low:
        return DownloadFailedError(f"Unduhan timeout ({platform} id={media_id}). Coba lagi sebentar.")
    if "ffmpeg" in low:
        return DownloadFailedError(
            "ffmpeg tidak tersedia di server sehingga konversi gagal. "
            "Coba format mp4 atau hubungi admin."
        )
    detail = msg.strip().splitlines()[0][:300] if msg.strip() else "unduhan gagal"
    return DownloadFailedError(f"Unduhan gagal ({platform} id={media_id}): {detail}")


def _extract_info_sync(url: str, platform: Platform) -> dict[str, Any]:
    from yt_dlp import YoutubeDL

    with YoutubeDL(_base_ydl_opts(platform)) as ydl:
        return ydl.extract_info(url, download=False) or {}


def _fmt_duration(sec: int) -> str:
    m, s = divmod(max(0, sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


async def get_media_info(url: str) -> dict[str, Any]:
    """Metadata untuk preview: platform, judul, durasi, thumbnail, kualitas."""
    platform, media_id = validate_url_or_raise(url)
    try:
        info = await asyncio.to_thread(_extract_info_sync, url, platform)
    except Exception as exc:
        raise _map_ytdlp_error(exc, platform, media_id) from exc

    duration = int(info.get("duration") or 0)
    formats_raw: list[dict[str, Any]] = info.get("formats") or []
    heights = sorted({int(f.get("height") or 0) for f in formats_raw if f.get("height")})
    if platform == "youtube":
        qualities = [str(h) for h in heights if h in (360, 480, 720, 1080)] or ["best"]
    else:
        qualities = [str(h) for h in heights if h > 0] or ["best"]
    if "best" not in qualities:
        qualities.append("best")

    return {
        "platform": platform,
        "id": media_id,
        "title": info.get("title") or media_id,
        "duration": duration,
        "duration_formatted": _fmt_duration(duration),
        "uploader": info.get("uploader") or info.get("channel") or info.get("uploader_id") or "",
        "thumbnail": info.get("thumbnail") or "",
        "is_shorts": is_shorts_url(url),
        "is_live": bool(info.get("is_live")),
        "too_long": duration > MAX_DURATION_SEC,
        "max_duration": MAX_DURATION_SEC,
        "available_formats": ["mp4", "mp3"],
        "available_qualities": qualities,
        "needs_login": False,
    }


def _download_sync(url: str, platform: Platform, fmt: MediaFormat, quality: str, dest: Path) -> Path:
    """Blocking yt-dlp download — dijalankan via asyncio.to_thread."""
    from yt_dlp import YoutubeDL

    dest.parent.mkdir(parents=True, exist_ok=True)
    for suffix in (".part", ".ytdl", ".temp"):
        try:
            Path(str(dest) + suffix).unlink(missing_ok=True)
        except OSError:
            pass

    stem = str(dest.with_suffix(""))
    opts: dict[str, Any] = {
        **_base_ydl_opts(platform),
        "format": _format_selector(platform, fmt, quality),
        "outtmpl": stem + ".%(ext)s",
        "merge_output_format": "mp4" if fmt == "mp4" else None,
        "overwrites": True,
    }
    if fmt == "mp3":
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise DownloadFailedError("yt-dlp tidak mengembalikan info unduhan.")

    if dest.is_file():
        return dest
    cands = sorted(dest.parent.glob(dest.stem + ".*"), key=lambda p: p.stat().st_size, reverse=True)
    cands = [p for p in cands if p.suffix.lower() not in (".part", ".ytdl", ".temp", ".json")]
    if not cands:
        raise DownloadFailedError("File hasil unduhan tidak ditemukan.")
    final = cands[0]
    if final != dest and dest.suffix.lower() in (".mp4", ".mp3") and final.suffix.lower() == dest.suffix.lower():
        try:
            final.rename(dest)
            return dest
        except OSError:
            return final
    return final


async def download_media(
    url: str, fmt: MediaFormat = "mp4", quality: str = "720"
) -> tuple[Path, bool, dict[str, Any]]:
    """Unduh (atau ambil dari cache). Return (path_file, dari_cache, info_ringkas)."""
    fmt = (fmt or "mp4").lower()
    if fmt not in ("mp4", "mp3"):
        raise InvalidMediaURLError("Format harus 'mp4' atau 'mp3'.")
    quality = (quality or ("audio" if fmt == "mp3" else "720")).lower()
    if fmt == "mp4" and quality not in QUALITY_CHOICES:
        raise InvalidMediaURLError(f"Quality mp4 harus salah satu {QUALITY_CHOICES}.")
    if fmt == "mp3" and not _has_ffmpeg():
        raise DownloadFailedError("ffmpeg belum terinstal di server sehingga konversi mp3 belum bisa.")

    platform, media_id = validate_url_or_raise(url)
    cache = get_cache("downloader")
    filename = cache_filename(platform, media_id, fmt, quality)
    dest = cache.path_for(filename)

    lock = await cache.lock_for(f"{platform}:{media_id}:{fmt}:{quality}")
    async with lock:
        if cache.exists(filename):
            cache.touch(dest)
            return dest, True, {"platform": platform, "id": media_id, "cached": True}

        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(_extract_info_sync, url, platform), timeout=120
            )
        except asyncio.TimeoutError as exc:
            raise DownloadFailedError(f"Mengambil info media timeout ({platform} id={media_id}).") from exc
        except MediaError:
            raise
        except Exception as exc:
            raise _map_ytdlp_error(exc, platform, media_id) from exc

        duration = int((info or {}).get("duration") or 0)
        if duration > MAX_DURATION_SEC:
            raise MediaTooLongError(
                f"Media terlalu panjang ({_fmt_duration(duration)} > 30:00). Maks 30 menit."
            )

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(_download_sync, url, platform, fmt, quality, dest),
                timeout=YTDLP_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError as exc:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            raise DownloadFailedError(f"Unduhan melebihi timeout 10 menit ({platform} id={media_id}).") from exc
        except MediaError:
            raise
        except Exception as exc:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            raise _map_ytdlp_error(exc, platform, media_id) from exc

        try:
            size = result.stat().st_size
        except OSError:
            raise DownloadFailedError("File hasil unduhan hilang setelah proses.")
        if size <= 0:
            try:
                result.unlink(missing_ok=True)
            except OSError:
                pass
            raise DownloadFailedError("Hasil unduhan kosong.")
        if size > MAX_FILE_BYTES:
            try:
                result.unlink(missing_ok=True)
            except OSError:
                pass
            raise FileTooLargeError("Hasil unduhan melebihi 500 MB.")

        cache.evict_if_needed()
        title = (info or {}).get("title") or media_id
        return result, False, {"platform": platform, "id": media_id, "title": title, "cached": False}


def content_type_for(fmt: MediaFormat) -> str:
    return "audio/mpeg" if fmt == "mp3" else "video/mp4"


def download_filename(title: str, fmt: MediaFormat) -> str:
    ext = "mp3" if fmt == "mp3" else "mp4"
    return f"{sanitize_filename(title)}.{ext}"


def supported_platforms() -> list[dict[str, Any]]:
    cookies = cookies_status()
    examples = {
        "youtube": "https://www.youtube.com/watch?v=...",
        "instagram": "https://www.instagram.com/reel/...",
        "threads": "https://www.threads.com/@user/post/...",
        "facebook": "https://www.facebook.com/reel/...",
    }
    return [
        {
            "platform": p,
            "hosts": sorted(PLATFORM_HOSTS[p]),
            "example": examples[p],
            "cookies_configured": cookies[p],
        }
        for p in ("youtube", "instagram", "threads", "facebook")
    ]


def cache_status() -> dict[str, Any]:
    c = get_cache("downloader")
    extra = {
        "ffmpeg_available": _has_ffmpeg(),
        "max_duration_sec": MAX_DURATION_SEC,
        "max_file_bytes": MAX_FILE_BYTES,
        "cookies": cookies_status(),
        "cookies_dir": str(COOKIES_DIR),
    }
    return {**c.stats(), **extra}
