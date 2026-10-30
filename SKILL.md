---
name: polyester-spot
description: Trading spot (beli/jual) di Polyester TESTNET lewat REST API. Pakai saat user minta scan market, cek harga, atau buka/tutup posisi spot di Polyester testnet.
---

# Polyester Spot Trader (TESTNET ONLY)

Skill ini hanya untuk **testnet** (`https://api.testnet.polyester.com`). URL dikunci di script. Jangan pernah mencoba mainnet atau mengubah base URL.

## Aturan
- Agent boleh menentukan sendiri pair dan ukuran order, tapi **selalu lewat `scripts/polyester.py`**. Jangan menulis request sendiri.
- Hanya pair quote **USDT**. Pair BTC/USDC dilewati.
- Batas keras ada di script: nilai per order maksimal `POLYESTER_MAX_ORDER_USDT` (default 25) dan maksimal `POLYESTER_MAX_ORDERS_PER_DAY` (default 20) order per hari. Kalau kena batas, jangan cari jalan pintas, laporkan ke user.
- Jangan pernah menampilkan API key atau secret. Key hanya dibaca dari env: `POLYESTER_API_KEY`, `POLYESTER_API_SECRET`, `POLYESTER_SUBACCOUNT_ID`.
- Jangan retry order yang error lebih dari 1x. Order ganda lebih berbahaya daripada order yang gagal.

## Alur trading
1. `python scripts/polyester.py scan --top 5` untuk ranking pair dari trade terbaru. Pilih kandidat yang `trades_per_min` dan `quote_volume_in_sample` cukup tinggi. Hindari pair yang sepi karena slippage market order bisa 2% sampai 5%.
2. `python scripts/polyester.py price SYMBOL` untuk harga terakhir dan aturan pair.
3. Hitung ukuran sendiri, lalu uji dulu: `python scripts/polyester.py buy SYMBOL --usdt 10 --dry-run`. Script membulatkan qty ke `stepSize` dan menolak order di bawah minimum atau di atas batas.
4. Kirim: hilangkan `--dry-run`. Untuk jual: `sell SYMBOL --qty Q`.
5. Verifikasi dengan `orders` dan `balance`, lalu laporkan ke user: pair, side, qty, harga acuan, status.

## Perintah
- `markets` daftar pair ENABLED
- `scan [--top N] [--limit 100]` ranking aktivitas
- `price SYMBOL`
- `buy|sell SYMBOL (--usdt X | --qty Q) [--dry-run]`
- `balance`, `orders`, `cancel ID` butuh auth

## Kalau error
- `AUTH_NOT_CONFIGURED`: signing belum diisi di script. Baca `references/api.md`, jangan mengarang.
- `BELOW_MIN_QTY` / `BELOW_MIN_NOTIONAL`: naikkan size ke atas minimum pair.
- `ABOVE_MAX_ORDER`: kecilkan size.
- `HTTP_ERROR` 401/403: cek key dan timestamp, jangan coba berkali-kali.
- Hasil scan hanya dari sampel trade terakhir, bukan volume 24 jam. Jangan menyebutnya volume harian.
