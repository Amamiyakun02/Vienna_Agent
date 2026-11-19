# Deploy Vienna_Agent ke VPS

Arsitektur produksi (keputusan 13–15 Nov 2026):

- **Core AI agent = opencode serve di HOST VPS** (bukan container):
  dua instance systemd — **admin** port 4096 (agent `admin`, model Lyber
  Kimi K3.1 Abliterated, folder kerja `admin-workspace/` di dalam
  project ini) dan **publik** port 4097 (agent `public`, model Mercury,
  sandbox `~/apps/agent-public/`).
  Config & unit-nya di folder `opencode-core/` (lihat README-nya).
- **Backend + pendukung = Docker Compose** (`docker-compose.yml`):
  backend (hanya `127.0.0.1:8000`), mcp, postgres (DB dashboard),
  qdrant (vector DB), redis — selain backend semua jaringan internal.
  Backend menjangkau opencode admin via `host.docker.internal:4096`
  dan publik via `host.docker.internal:4097`. Folder `opencode-core/`
  dan `../agent-public/` di-bind-mount ke container backend supaya
  publish dashboard menulis file yang benar-benar dibaca serve di
  host; service mcp menerbitkan `127.0.0.1:8001` (localhost saja)
  supaya opencode di host bisa menjangkau tool MCP-nya.
- **MongoDB tetap cloud (Atlas)** — bukan container.
- Frontend di Vercel; akses publik ke backend nanti via reverse proxy
  ke `api.amamiya.web.id` (di luar dokumen ini).

## Prasyarat VPS

1. Docker Engine + Compose plugin terpasang & aktif.
2. opencode **2.3.0** di `~/.opencode/bin/opencode` (samakan versi
   laptop–VPS; config V2 tidak terbaca penuh oleh 1.19.x).
3. Git + SSH key GitHub untuk clone/pull repo privat.

## Tata letak (WAJIB bersebelahan)

```text
~/apps/
├── Vienna_Agent/     ← repo ini
├── MCPServer/        ← repo MCP (build context ../MCPServer di compose)
└── agent-public/     ← sandbox instance publik (dibuat unit/script)
```

## Langkah deploy

1. Clone/pull kedua repo ke `~/apps/`.
2. Buat `.env` dari `.env.example`, isi NILAI SENDIRI (jangan commit):
   - Wajib: `MONGO_USER`, `MONGO_PASS`, `MONGO_DB_NAME` (Atlas),
     `POSTGRES_PASSWORD` (buat baru khusus VPS, karakter URL-safe),
     `DASHBOARD_ADMIN_USER`, `DASHBOARD_ADMIN_PASSWORD`,
     `DASHBOARD_SECRET_KEY`,
     `MERCURY_API_KEY` (atau `INCEPTION_API_KEY`), `LYBER_API_KEY`,
     `OPENCODE_SERVER_PASSWORD` (stabil; samakan untuk backend & serve),
     `OPENCODE_AGENT=admin`,
     `CORS_ALLOW_ORIGINS=https://amamiya.web.id,https://www.amamiya.web.id`
   - MCPServer punya .env SENDIRI di foldernya (MONGODB_URI dsb.) —
     buat juga dari contoh di repo MCPServer, chmod 600.
   - Lalu: `chmod 600 .env` untuk keduanya.
3. Pastikan folder kerja agent admin sudah ada di host SEBELUM compose
   pertama kali naik: `mkdir -p ~/apps/Vienna_Agent/admin-workspace`
   (folder ini di-bind-mount ke backend untuk lampiran file hasil kerja
   agent; bila Docker yang membuatnya, folder jadi milik root dan serve
   di host tidak bisa menulis ke dalamnya).
4. Naikkan tumpukan: `docker compose up -d --build`
5. Pasang opencode serve: ikuti `opencode-core/vps/DEPLOY-VPS.md`
   (unit systemd `opencode-admin` + `opencode-public`; keduanya membaca
   `.env` yang sama lewat `EnvironmentFile=`) lalu
   `sudo systemctl enable --now opencode-admin opencode-public`.
6. Verifikasi:
   - `docker compose ps` — backend up, postgres & redis healthy.
   - `curl http://127.0.0.1:8000/health` → `{"status":"ok"}`
   - `curl http://127.0.0.1:8000/v1/engines` — lyber & opencode available.
   - `curl http://127.0.0.1:8000/v1/opencode/status` — serve admin terhubung.

## Update berikutnya

```bash
cd ~/apps/Vienna_Agent && git pull && docker compose up -d --build
sudo systemctl restart opencode-admin opencode-public   # bila config opencode-core berubah
```

## Keamanan (jangan dilanggar)

- Port 8000/4096/4097 TIDAK dibuka ke publik — semua bind localhost.
  Pemegang `OPENCODE_SERVER_PASSWORD` = akses eksekusi penuh ke mesin.
- `.env` tidak pernah masuk git (sudah di-`.gitignore`); nilai key hanya
  ada di `.env` tiap mesin. Database dashboard hanya menyimpan NAMA
  variabel key (`auth_env_var`), bukan nilainya.
