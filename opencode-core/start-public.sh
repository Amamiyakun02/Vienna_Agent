#!/usr/bin/env bash
# Instance PUBLIK opencode serve — port 4097, sandbox sendiri.
# Password & API key diambil dari .env backend (satu sumber, sama
# seperti start-opencode-serve.sh yang sudah ada). Isi .env tidak
# pernah dicetak skrip ini.
set -euo pipefail

BACKEND_DIR="${BACKEND_DIR:-$HOME/apps/Vienna_Agent}"
PUBLIC_DIR="${PUBLIC_DIR:-$HOME/apps/agent-public}"

set -a
# shellcheck disable=SC1091
source "$BACKEND_DIR/.env"
set +a

cd "$PUBLIC_DIR"
# Pisahkan DB sesi publik dari sesi admin/pribadi:
export XDG_DATA_HOME="$PUBLIC_DIR/.data"
exec opencode serve --hostname 127.0.0.1 --port 4097
