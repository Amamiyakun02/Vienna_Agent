# Tutorial: Reinstall / Upgrade OpenCode di VPS Vultr ke v2.2.2

Target: versi opencode di VPS **sama persis dengan laptop: v2.2.2**
(terverifikasi dari laptop 16 Nov 2026). VPS mulai dari 1.19.5
(bawaan snapshot). Semua perintah dijalankan **di VPS**, kecuali
Langkah 7 yang ditandai khusus dari laptop.

Login dulu:

```bash
ssh amamiya@vps.amamiya.web.id
```

## Langkah 1 — Cek kondisi awal

```bash
~/.opencode/bin/opencode --version
pgrep -af opencode
```

Harapan: versi masih `1.19.5`. Baris kedua: kalau ada proses
`opencode serve` yang jalan, catat — harus dimatikan dulu di
Langkah 2. Kalau kosong, langsung ke Langkah 3.

## Langkah 2 — Matikan serve yang masih jalan (bila ada)

Kalau serve dulu dinyalakan manual di terminal lain: fokus ke
terminal itu, tekan `Ctrl-C`. Kalau tidak ketemu terminalnya:

```bash
pkill -f "opencode serve"
pgrep -af opencode   # harus kosong sekarang
```

## Langkah 3 — Backup (jangan dilewati)

```bash
cp ~/.opencode/bin/opencode ~/.opencode/bin/opencode.bak-1.19.5
cp ~/.local/share/opencode/opencode.db ~/.local/share/opencode/opencode.db.bak-20261116
ls -lh ~/.opencode/bin/
```

File `.bak-1.19.5` = jalan pulang kalau upgrade bermasalah.

## Langkah 4 — Upgrade dua tahap

```bash
~/.opencode/bin/opencode upgrade
~/.opencode/bin/opencode upgrade --major
```

Tahap pertama menaikkan 1.19.5 ke 1.x terbaru, tahap kedua
(`--major`) melompat ke jalur V2. Resep yang sama terbukti di
server Vienna: 1.19.4 → 2.1.4. Butuh internet VPS (ada) dan
beberapa menit; jangan ditutup di tengah.

## Langkah 5 — Verifikasi versi

```bash
~/.opencode/bin/opencode --version
```

- Keluar `opencode v2.2.2` → **SELESAI**, lanjut Langkah 6.
- Keluar versi V2 lain (mis. lebih baru) → tetap bisa dipakai,
  tapi kalau mau sama persis dengan laptop, pakai Langkah 7.
- Masih 1.x / error → tempel outputnya ke Vienna, jangan diutak-atik.

## Langkah 6 — Tes serve singkat

```bash
~/.opencode/bin/opencode serve --hostname 127.0.0.1 --port 4096
```

Harapan: muncul peringatan password belum diset (normal untuk tes
singkat ini) lalu `opencode server listening on http://127.0.0.1:4096`.
Tekan `Ctrl-C`. Password beneran nanti datang dari `.env` backend
lewat unit systemd — bukan diset manual di sini.

## Langkah 7 — (Cadangan) Salin binary laptop apa adanya

Hanya bila Langkah 5 meleset dan ingin versi identik. Jalankan
**di laptop**:

```bash
scp ~/.opencode/bin/opencode amamiya@vps.amamiya.web.id:~/.opencode/bin/opencode.new
```

Lalu **di VPS**:

```bash
chmod +x ~/.opencode/bin/opencode.new
mv ~/.opencode/bin/opencode.new ~/.opencode/bin/opencode
~/.opencode/bin/opencode --version   # harus v2.2.2
```

Binary-nya ±248 MB; rute rumah → Singapura ±25 ms, wajar beberapa menit.

## Rollback (bila upgrade rusak)

```bash
cp ~/.opencode/bin/opencode.bak-1.19.5 ~/.opencode/bin/opencode
~/.opencode/bin/opencode --version   # kembali 1.19.5
```

## Sesudah ini

Baru deploy config: ikuti `DEPLOY-VPS.md` di folder yang sama
(kirim `opencode.jsonc` publik/admin, instruksi, unit systemd).
