"""File cache generik untuk tool-tools Vienna Agent (YouTube downloader, dst).

Satu class reusable: LRU berdasarkan mtime + batas ukuran & jumlah file,
plus lock per key agar request konkuren untuk key yang sama hanya
mengunduh sekali (yang lain menunggu lalu membaca dari cache).

Dipakai oleh services/youtube_service.py dan tool lain di masa depan
via get_cache(name).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

# Default mengikuti spek "YouTube Downloader": maks 2 GB / 50 file.
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_FILES = 50

# Root cache bisa dioverride via env DATA_DIR (default ./data).
DATA_ROOT = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))


class FileCache:
    def __init__(
        self,
        base_dir: Path | str,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_files: int = DEFAULT_MAX_FILES,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.max_files = max_files
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def lock_for(self, key: str) -> asyncio.Lock:
        """Lock per key — request konkuren untuk key sama antre di sini."""
        async with self._locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._key_locks[key] = lock
            return lock

    def path_for(self, filename: str) -> Path:
        # Cegah path traversal dari key/filename.
        safe = Path(filename).name
        return self.base_dir / safe

    def exists(self, filename: str) -> bool:
        p = self.path_for(filename)
        return p.is_file() and p.stat().st_size > 0

    def touch(self, path: Path) -> None:
        now = time.time()
        try:
            os.utime(path, (now, now))
        except OSError:
            pass

    def total_bytes(self) -> int:
        total = 0
        for f in self.base_dir.iterdir():
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                continue
        return total

    def evict_if_needed(self) -> None:
        """Evict file tertua (mtime) sampai di bawah batas ukuran & jumlah."""
        try:
            files = [f for f in self.base_dir.iterdir() if f.is_file()]
        except OSError:
            return
        files.sort(key=lambda f: f.stat().st_mtime if f.exists() else 0)

        total = sum(f.stat().st_size for f in files if f.exists())
        while (len(files) > self.max_files or total > self.max_bytes) and files:
            oldest = files.pop(0)
            try:
                total -= oldest.stat().st_size
                oldest.unlink()
            except OSError:
                continue

    def stats(self) -> dict:
        try:
            files = [f for f in self.base_dir.iterdir() if f.is_file()]
        except OSError:
            files = []
        return {
            "dir": str(self.base_dir),
            "files": len(files),
            "bytes": sum(f.stat().st_size for f in files if f.exists()),
            "max_files": self.max_files,
            "max_bytes": self.max_bytes,
        }


_cache_registry: dict[str, FileCache] = {}


def get_cache(name: str = "default", **kwargs) -> FileCache:
    """Ambil (atau buat) cache bernama — tiap tool pakai subdir sendiri.

    Contoh: get_cache("youtube") -> <data>/youtube_cache/
    """
    if name not in _cache_registry:
        base = DATA_ROOT / f"{name}_cache"
        _cache_registry[name] = FileCache(base, **kwargs)
    return _cache_registry[name]
