# Protokol Memory (ingatan dinamis agent)

Opencode TIDAK punya ingatan jangka panjang bawaan antar-sesi selain
riwayat sesi itu sendiri. Protokol ini menutup celah itu TANPA kode
agent baru — seluruhnya lewat tool MCP `vienna` yang sudah ada /
ditambah di MCP server, bukan di backend.

## Bila MCP server punya tool memory (mis. `memory_recall` / `memory_save`)

1. Awal percakapan dengan pengunjung yang kembali / menyebut konteks
   lama: panggil `memory_recall` dengan topiknya sebelum menjawab.
2. Saat pengunjung menyampaikan fakta awet tentang dirinya (nama,
   preferensi, proyeknya): panggil `memory_save` SETELAH menjawab,
   satu fakta per panggilan, tanpa data sensitif (password, token,
   data pembayaran tidak pernah disimpan).
3. Bila hasil recall bertentangan dengan ucapan terbaru pengunjung,
   ucapan terbaru yang menang — dan perbarui memory-nya.

Kunci memory WAJIB mengandung id pengunjung/sesi dari konteks
(`user_id`), jangan pernah mencampur memory antar-pengunjung.

## Bila tool memory BELUM ada (fallback nol-kode)

- Tulis fakta awet ke file `memory/notes.md` di workspace agent
  (khusus instance admin; instance publik folder output-nya tidak
  dibaca ulang sebagai instruksi, jadi fallback ini tidak berlaku
  untuk publik — publik memang sebaiknya tanpa ingatan lintas
  pengunjung).
- File itu dimuat ulang sebagai instruksi hanya bila ditambahkan ke
  array `instructions` di opencode.jsonc dan sesi BARU dibuat.

## Yang TIDAK perlu dibangun

- Jangan membuat tabel/loop memory sendiri di backend: riwayat
  percakapan = session opencode, ringkasan konteks panjang =
  compaction opencode, fakta awet = MCP memory / file di atas,
  pengetahuan dokumen = RAG Qdrant yang sudah ada di MCP server.
