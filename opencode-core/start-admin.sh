#!/usr/bin/env bash
# Instance ADMIN opencode serve — direktori kerja KHUSUS
# <project>/admin-workspace: agent berakses penuh di dalamnya dan
# ditolak menyentuh sisa project (permission external_directory di
# admin/opencode.jsonc). Config dibaca dari file repo itu juga
# (OPENCODE_CONFIG), jadi publish dashboard (blok providers + model
# agent admin) langsung berlaku sesudah restart — tidak ada salinan
# config kedua. Password & API key dari .env backend (tidak dicetak).
set -euo pipefail

BACKEND_DIR="${BACKEND_DIR:-$HOME/apps/Vienna_Agent}"
WORKSPACE_DIR="$BACKEND_DIR/admin-workspace"
PORT="${OPENCODE_ADMIN_PORT:-4096}"
# Alamat dengar serve. Bawaan 127.0.0.1 (laptop: backend bare-metal).
# Di VPS backend hidup di container dan menjangkau serve lewat
# gateway Docker (host.docker.internal = IP 172.x, BUKAN loopback),
# jadi unit systemd VPS menyetel OPENCODE_ADMIN_HOSTNAME=0.0.0.0 —
# aman karena UFW VPS hanya membuka 4096/4097 untuk subnet Docker
# (172.16.0.0/12); dari internet port ini tetap tertutup.
HOST="${OPENCODE_ADMIN_HOSTNAME:-127.0.0.1}"

export PATH="$HOME/.opencode/bin:$PATH"

set -a
# shellcheck disable=SC1091
source "$BACKEND_DIR/.env"
set +a

mkdir -p "$WORKSPACE_DIR/.opencode/instructions" "$WORKSPACE_DIR/.data"
cp "$BACKEND_DIR/opencode-core/shared-instructions/"*.md \
  "$WORKSPACE_DIR/.opencode/instructions/"
# WAJIB: workspace harus repo git-nya sendiri. Tanpa ini opencode
# menganggap root project = repo Vienna_Agent di atasnya, dan
# permission external_directory tidak berlaku (terbukti lolos baca
# file project di luar workspace pada uji pertama, 18 Nov 2026).
if [ ! -d "$WORKSPACE_DIR/.git" ]; then
  git init -q "$WORKSPACE_DIR"
fi
# WAJIB: .data harus diabaikan repo workspace. Di dalamnya hidup
# XDG_DATA_HOME opencode termasuk folder snapshot-nya sendiri;
# tanpa baris ini snapshot menelan .data (dan dirinya sendiri)
# tiap langkah sampai membengkak 47 GB dan menyumbat disk
# (kejadian 21 Nov 2026: /home 100% penuh, giliran agent tertahan
# menit-menit di git update-index snapshot).
touch "$WORKSPACE_DIR/.gitignore"
grep -qx '.data/' "$WORKSPACE_DIR/.gitignore" || echo '.data/' >> "$WORKSPACE_DIR/.gitignore"

export OPENCODE_CONFIG="$BACKEND_DIR/opencode-core/admin/opencode.jsonc"
# Pisahkan DB sesi admin dari sesi publik/pribadi:
export XDG_DATA_HOME="$WORKSPACE_DIR/.data"
cd "$WORKSPACE_DIR"
exec opencode serve --hostname "$HOST" --port "$PORT"
