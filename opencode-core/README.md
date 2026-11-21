# OpenCode Serve sebagai Core AI Agent — Vienna_Agent

Prinsip: **opencode serve adalah agent-nya** (loop, tool calling,
session, compaction, permission semua bawaan). Backend Vienna_Agent
tetap hanya jembatan tipis yang sudah ada (`routers/opencode_api.py`).
Yang belum ada selama ini bukan kode — tapi **file config per
instance**. Folder ini isinya file itu.

## Peta: kebutuhan → mekanisme bawaan (tanpa bikin dari nol)

| Kebutuhan | Mekanisme opencode | Diatur di |
|---|---|---|
| Agent loop, tool calling, streaming | `opencode serve` (API `/api/session`, `/api/event`) | — (sudah jalan v1) |
| Memory sesi (riwayat chat) | Session persisten di SQLite opencode; backend cukup simpan `session_id` per user | otomatis |
| Memory konteks panjang | Compaction otomatis (ringkas + simpan ekor percakapan) | `compaction` di jsonc |
| Memory statis (profil, aturan) | File instruksi dimuat ke setiap sesi baru | `instructions` + `.opencode/instructions/*.md` |
| Memory dinamis (fakta antar-sesi) | SENGAJA tidak dipasang untuk instance project (publik & admin) — keputusan user 21 Nov 2026: akses memory/vault hanya milik instance pribadi user | — |
| Pengetahuan dokumen | RAG Qdrant, sudah jadi tool MCP | MCP server |
| Tools eksternal | MCP server lokal (stdio) / remote (HTTP) | blok `mcp.servers` di jsonc |
| Tools bawaan (read/edit/bash/web) | Permission per agent: `allow`/`deny` per aksi+resource | `agents.<nama>.permissions` |
| Model ganti-ganti | Provider OpenAI-compatible didaftarkan sekali; Mercury & Lyber termasuk | blok `providers` di jsonc |
| Pemisahan publik vs admin | **Dua instance serve**: beda port, folder kerja, config, dan DB sesi (`XDG_DATA_HOME`) | `start-*.sh` |

## Dua instance (keputusan arsitektur 15 Nov 2026)

| | Publik | Admin |
|---|---|---|
| Port | 4097 | 4096 (yang dipakai router sekarang) |
| Folder kerja | `~/apps/agent-public/` (sandbox kosong + `output/`) | `~/apps/Vienna_Agent/` |
| Agent / model | `public` / Mercury 3 (Inception) | `admin` / Kimi K3.1 Abliterated (Lyber) |
| Shell & edit | shell ditolak total; edit hanya `output/**` | hampir semua boleh; `.env` tetap ditolak dibaca, `sudo` ditolak |
| MCP | `vienna`, `codemode: false` | `vienna`, `codemode: false` |
| Gerbang | rate-limit & filter di backend | login dashboard + approval + audit di backend/dashboard |

Kenapa `codemode: false` penting: default V2 membungkus tool MCP ke
tool `execute` (eksekusi kode). Untuk agent anonim itu melanggar
aturanmu sendiri — dia hanya boleh menjalankan program yang sudah
ada di MCP server, bukan mengeksekusi program buatannya.

Kenapa tidak ada `ask`: serve dipanggil backend secara headless;
`ask` menggantungkan request sampai timeout karena tidak ada klien
yang menjawab prompt persetujuan. Di headless hanya `allow`/`deny`.

## Cara pasang (sekali jalan, per mesin)

```bash
# 1. Instance publik = folder baru
mkdir -p ~/apps/agent-public/output ~/apps/agent-public/.opencode/instructions
cp opencode-core/public/opencode.jsonc ~/apps/agent-public/
cp opencode-core/shared-instructions/*.md ~/apps/agent-public/.opencode/instructions/

# 2. Instance admin = config di akar project + instruksi
cp opencode-core/admin/opencode.jsonc ~/apps/Vienna_Agent/
mkdir -p ~/apps/Vienna_Agent/.opencode/instructions
cp opencode-core/shared-instructions/*.md ~/apps/Vienna_Agent/.opencode/instructions/

# 3. MCP harus terjangkau dari host (serve jalan di host, bukan docker)
#    docker-compose mcp saat ini TANPA port host. Tambahkan di service mcp:
#      ports: ["127.0.0.1:8001:8001"]
#    ATAU pakai varian stdio yang sudah dikomentari di opencode.jsonc publik.

# 4. Jalankan (password serve dari .env backend, seperti sekarang)
./opencode-core/start-admin.sh    # terminal/service 1 — port 4096
./opencode-core/start-public.sh   # terminal/service 2 — port 4097
```

Di laptop untuk tes: ganti `~/apps/` dengan `~/Desktop/`
(`BACKEND_DIR` dan `PUBLIC_DIR` bisa di-override sebagai env).

## Yang berubah di backend (langkah berikutnya, belum dikerjakan)

Router sekarang mengenal satu `OPENCODE_BASE_URL` + satu password.
Untuk dua instance, perubahan terkecilnya:

1. Samakan dulu `OPENCODE_SERVER_PASSWORD` untuk kedua instance
   (satu .env) → router tidak perlu logika password per instance.
2. Tambah `OPENCODE_PUBLIC_BASE_URL=http://127.0.0.1:4097` dan pilih
   base URL dari nama agent (`public` → publik, selain itu admin).
   ±20 baris di `routers/opencode_api.py`, tidak ada tabel baru.
3. Frontend Assistant memanggil agent `public` seperti sekarang;
   modul AI Agent dashboard memanggil agent `admin`.

## Wajib diverifikasi sebelum live (jujur, belum dites)

1. **Limit konteks Mercury & Lyber** di jsonc masih placeholder
   128000 — tanpa angka yang benar, compaction bisa terlalu cepat /
   terlambat. Cek docs resmi kedua provider.
2. **Config provider custom SUDAH lolos uji muat live dua kali**:
   di server Vienna (opencode v2.1.4) dan di laptop pada v2.3.0
   (16 Nov 2026, setelah laptop di-upgrade dari 2.2.2): serve dengan
   config publik menjawab model default `inception/mercury-3` dan
   agent `public` termuat; config admin menjawab
   `lyber/kimi-k31-pro-abliterated` dan agent `admin` termuat.
   Catatan dari tes laptop: agent `vienna` dari config GLOBAL ikut
   terbaca di kedua instance — karena itu backend publik WAJIB
   mengunci nama agent ke `public` dan tidak meneruskan pilihan
   agent dari request pengunjung. Yang BELUM dites: panggilan chat
   sungguhan ke API Mercury/Lyber lewat opencode (butuh key aktif
   di env serve). Tes pertama cukup satu chat per instance:
   `GET /v1/opencode/models` harus menampilkan `inception` &
   `lyber`, lalu `POST /v1/opencode/chat` satu prompt pendek.
   Mercury tetap mati total setelah **28 Des 2026** — config ini
   membuat penggantinya cukup ganti satu baris `model` +
   blok provider, bukan ganti kode.
3. **Tool memory di MCP server**: SENGAJA tidak dibangun untuk
   instance project (keputusan user 21 Nov 2026) — protokol memory
   lama sudah dicabut dari instruksi bersama dan prompt kedua
   agent. Akses memory/vault hanya untuk instance pribadi user;
   kebutuhan serupa di masa depan akan datang sebagai fitur skill
   custom tersendiri, bukan protokol diam-diam.
4. Format config ini V2 (serve server v2.1.4 / laptop v2.2.2).
   Instalasi V1 lama (1.18.x, backup 26 Okt) memakai format lain
   (`agent`, `mcp` langsung, `permission` objek) dan akan mengabaikan
   sebagian file ini.
