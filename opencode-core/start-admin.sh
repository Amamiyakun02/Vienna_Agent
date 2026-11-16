#!/usr/bin/env bash
# Instance ADMIN opencode serve — port 4096 (default router backend).
# Sama seperti start-opencode-serve.sh lama, bedanya hanya eksplisit:
# cwd = project Vienna_Agent (agar opencode.jsonc admin terbaca) dan
# DB sesi memakai lokasi default (sesi lama tidak hilang).
set -euo pipefail

BACKEND_DIR="${BACKEND_DIR:-$HOME/apps/Vienna_Agent}"

set -a
# shellcheck disable=SC1091
source "$BACKEND_DIR/.env"
set +a

cd "$BACKEND_DIR"
exec opencode serve --hostname 127.0.0.1 --port 4096
