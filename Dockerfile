# FastAPI Cloud / Railway / Render / VPS — satu Dockerfile untuk semua.
# Build: docker build -t vienna-agent .
# Run:   docker run -p 8000:8000 --env-file .env vienna-agent
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    PATH="/root/.local/bin:$PATH"

# ffmpeg wajib untuk yt-dlp/imageio-ffmpeg (downloader mp3/mp4).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install deps dulu (cache-friendly) — uv.lock di-commit agar reproducible.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Salin kode aplikasi.
COPY main.py ./
COPY agents/ ./agents/
COPY routers/ ./routers/
COPY services/ ./services/
COPY models/ ./models/
COPY utils/ ./utils/
COPY rag/ ./rag/
# Config opencode-core dibaca/ditulis fitur publish dashboard
# (routers/dashboard_api.py memakai path <root>/opencode-core/...).
# Di compose, folder ini di-bind-mount dari host menimpa salinan ini.
COPY opencode-core/ ./opencode-core/

# Cache downloader sifatnya ephemeral — siapkan dir kosongnya saja.
RUN mkdir -p /app/data && chmod 777 /app/data

EXPOSE 8000

# $PORT diisi otomatis oleh platform; default 8000 untuk lokal.
# --proxy-headers wajib di belakang reverse proxy (FastAPI Cloud/dll).
CMD ["sh", "-c", "uv run uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
