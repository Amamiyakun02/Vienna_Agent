# Checklist Deploy ke VPS — opencode serve (hasil tes lokal)

Prinsip: yang dikirim ke VPS adalah **hasil akhir tes lokal**, bukan
seluruh isi laptop. Tata letak VPS: `~/apps/Vienna_Agent` dan
`~/apps/MCPServer` bersebelahan, backend di Docker, serve di host.

## 0. Prasyarat di VPS (cek dulu, bukan kiriman)

- [ ] **Binary opencode versi yang lolos tes di laptop (2.2.2).**
      Di VPS terakhir tercatat 1.19.5 (bawaan snapshot) — format
      config V2 (`agents`, `mcp.servers`, `permissions` array)
      tidak terbaca penuh oleh 1.19.x. Cek: `~/.opencode/bin/opencode --version`.
      Bila masih 1.19.x: ganti binary-nya dengan versi laptop
      (unduh rilis yang sama di VPS, atau kirim file binary-nya).
- [ ] Docker + tumpukan backend jalan (compose 5 service).
- [ ] `.env` VPS di `/home/amamiya/apps/Vienna_Agent/.env` terisi
      (diisi DI VPS, nilainya beda dari laptop): `OPENCODE_SERVER_PASSWORD`,
      `MERCURY_API_KEY` (atau `INCEPTION_API_KEY`), `LYBER_API_KEY`,
      `OPENCODE_PUBLIC_BASE_URL=http://host.docker.internal:4097`
      (untuk container backend; dari host: `127.0.0.1:4097`).

## 1. Yang DIKIRIM dari lokal (inti opencode serve)

| File lokal (hasil tes) | Tujuan di VPS |
|---|---|
| `opencode-core/public/opencode.jsonc` | `~/apps/agent-public/opencode.jsonc` |
| `opencode-core/admin/opencode.jsonc` | `~/apps/Vienna_Agent/opencode.jsonc` |
| `opencode-core/shared-instructions/profile-amamiya.md` | `~/apps/agent-public/.opencode/instructions/` DAN `~/apps/Vienna_Agent/.opencode/instructions/` |
| `opencode-core/shared-instructions/memory-protocol.md` | sama, ke kedua folder `instructions/` |
| `opencode-core/vps/opencode-admin.service` | `/etc/systemd/system/opencode-admin.service` |
| `opencode-core/vps/opencode-public.service` | `/etc/systemd/system/opencode-public.service` |

Folder baru yang dibuat di VPS (sekali):
`~/apps/agent-public/` + `output/` + `.opencode/instructions/`.

Aktifkan: `sudo systemctl daemon-reload && sudo systemctl enable --now opencode-admin opencode-public`

## 2. Yang dikirim HANYA bila berubah selama tes lokal

- [ ] `routers/opencode_api.py` (bila router jadi dwibahasa-instance:
      tambah `OPENCODE_PUBLIC_BASE_URL`) — bagian dari sinkronisasi
      project Vienna_Agent seperti biasa (rsync + dry-run).
- [ ] `docker-compose.yml` — perubahan satu blok: service `mcp`
      tambah `ports: ["127.0.0.1:8001:8001"]` agar serve di host
      bisa menjangkau MCP. Tanpa ini blok `mcp.servers` di config
      tidak akan tersambung di VPS (di laptop tesnya beda jalur).
- [ ] Kode `MCPServer` — bila selama tes ditambah tool
      `memory_save`/`memory_recall`; folder ini memang wajib ada
      di `~/apps/MCPServer` untuk build container mcp.
- [ ] Perubahan lain di Vienna_Agent selama tes (commit seperti biasa).

## 3. Yang TIDAK dikirim (sengaja)

- `.env` laptop — VPS punya nilainya sendiri; jangan ditimpa.
- Database sesi lokal (`~/.local/share/opencode/opencode.db`,
  folder `.data/`) — berisi sesi lokal/pribadi; VPS mulai bersih.
- `start-public.sh` / `start-admin.sh` — untuk tes lokal saja;
  di VPS perannya digantikan unit systemd di folder ini.
- `node_modules`, `.next`, `.venv`, `__pycache__` — seperti biasa.
- Folder `opencode-core` utuh — di VPS yang hidup hanya file hasil
  akhirnya (tabel no. 1); folder ini arsip sumber di laptop/server.

## 4. Verifikasi di VPS (setelah kirim)

```bash
systemctl is-active opencode-admin opencode-public
curl -s -u opencode:$OPENCODE_SERVER_PASSWORD http://127.0.0.1:4096/api/model/default  # lyber/...
curl -s -u opencode:$OPENCODE_SERVER_PASSWORD http://127.0.0.1:4097/api/model/default  # inception/...
# lalu 1 chat pendek per instance lewat backend :8000
```

## 5. Jembatan container → serve (WAJIB di VPS, terverifikasi 19 Nov 2026)

Backend di container menjangkau serve lewat `host.docker.internal`
= IP gateway Docker (172.x), **bukan** loopback host. Serve yang
hanya mendengarkan `127.0.0.1` tidak akan terjangkau dari container
(gejala: Assistant membalas "opencode serve tidak terjangkau",
curl dari container = `000`, dari host = `401`). Karena itu:

- Kedua unit mendengarkan `0.0.0.0` (publik langsung di unit-nya;
  admin lewat `OPENCODE_ADMIN_HOSTNAME` yang dibaca
  `opencode-core/start-admin.sh`, bawaan tetap `127.0.0.1` untuk
  laptop).
- UFW VPS membuka port ini HANYA untuk subnet Docker:
  `sudo ufw allow from 172.16.0.0/12 to any port 4096,4097 proto tcp`
  Dari internet 4096/4097 tetap tertutup (UFW default deny) —
  verifikasi dari luar wajib sesudah perubahan bind.
- Cek jembatan: `docker compose exec backend curl -s -o /dev/null -w '%{http_code}' http://host.docker.internal:4097/api/model/default`
  → `401` berarti tembus (serve meminta autentikasi).
