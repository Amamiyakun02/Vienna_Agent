# API Key untuk OpenCode Serve di VPS

Prinsip: **key TIDAK ditulis di `opencode.jsonc`** (file itu aman
di-commit) dan tidak ditempel di chat. Key hidup sebagai environment
variable proses serve, dari SATU sumber yang sudah disepakati:
file `.env` backend di VPS.

```text
/home/amamiya/apps/Vienna_Agent/.env   ← isi key di sini (izin 600)
        │  EnvironmentFile= (unit systemd opencode-admin/-public)
        ▼
proses opencode serve membaca env: LYBER_API_KEY,
MERCURY_API_KEY / INCEPTION_API_KEY → provider lyber & inception
di opencode.jsonc merujuk NAMA variabel itu lewat field "env"
```

## Variabel yang dibutuhkan

| Variabel | Untuk | Catatan |
|---|---|---|
| `LYBER_API_KEY` | agent admin (Lyber) | sudah terisi di .env laptop |
| `MERCURY_API_KEY` | agent publik (Mercury) | nama alternatif resmi: `INCEPTION_API_KEY` — pakai SATU nama yang sama seperti di .env laptopmu |
| `OPENCODE_SERVER_PASSWORD` | auth Basic serve | bukan API key, tapi satu rumah; backend membaca variabel yang sama |

Provider bawaan opencode (bila mau dipakai juga) memakai nama
standarnya masing-masing di file yang sama: `OPENROUTER_API_KEY`,
`GOOGLE_GENERATIVE_AI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`DEEPSEEK_API_KEY` — pola persis seperti `start-opencode-serve.sh`
di laptop.

## Langkah

1. Cek variabel mana yang sudah ada di .env VPS — perintah ini
   hanya mencetak ADA/KOSONG, nilainya tidak tampil:

   ```bash
   cd ~/apps/Vienna_Agent
   for k in LYBER_API_KEY MERCURY_API_KEY INCEPTION_API_KEY OPENCODE_SERVER_PASSWORD; do
     if grep -q "^$k=." .env; then echo "$k: ADA"; else echo "$k: KOSONG"; fi
   done
   ```

2. Isi yang KOSONG: `nano .env`, tambahkan baris `NAMA=nilai`.
   Nilai diambil dari .env laptop (buka sendiri di laptop,
   salin baris yang sama). Jangan kirim nilai key lewat chat.

3. Kunci izin file: `chmod 600 .env`

4. Key berlaku saat serve start berikutnya:
   - Lewat systemd (pola final): `sudo systemctl restart opencode-admin opencode-public`
   - Tes manual sebelum systemd terpasang:
     `set -a; source .env; set +a` lalu jalankan `opencode serve ...`
     di terminal yang sama.

5. Verifikasi TANPA membocorkan key: serve jalan →
   `GET /api/model` menampilkan provider `inception` & `lyber`;
   bukti final = satu chat pendek per instance menjawab.
   Output langkah 1 (ADA/KOSONG) aman ditempel ke Vienna.

## Kenapa bukan `opencode auth login` / `/connect`

Itu jalur interaktif untuk TUI di mesin pribadi — kredensialnya
tersimpan di `~/.local/share/opencode/auth.json`. Di VPS headless
dengan service systemd, env dari .env adalah jalur yang benar:
satu sumber untuk backend + dua serve sekaligus, dan key tidak
tersebar ke file lain.
