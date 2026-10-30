# Polyester testnet API — catatan & referensi

Base URL testnet: `https://api.testnet.polyester.com` (dikunci di script)

## Env vars (dibaca script; jangan pernah dicetak ke chat/log)

| Var | Isi | Keterangan |
|---|---|---|
| `POLYESTER_BASE_URL` | `https://api.testnet.polyester.com` | Base URL (juga dikunci di script) |
| `POLYESTER_API_KEY_ID` | `ak_...` (dari dashboard "key id") | Header `X-API-KEY-ID` |
| `POLYESTER_API_SECRET` | private key Ed25519 (format TBD: hex/PEM/base64) | Bahan signing `X-API-SIGNATURE` |
| `POLYESTER_SUBACCOUNT_ID` | `<ACCOUNT_ID>` (string pendek, bukan snowflake) | Dipakai di body/query endpoint privat |
| `POLYESTER_MAX_ORDER_USDT` | `25` (default) | Cap nilai order |
| `POLYESTER_MAX_ORDERS_PER_DAY` | `20` (default) | Cap order/hari |

## Header auth (SEMUA endpoint privat) — CONFIRMED dari dashboard

```
X-API-KEY-ID: <api key id>
X-API-TIMESTAMP: <timestamp>
X-API-SIGNATURE: <signature>
Accept: */*
Content-Type: application/json   (khusus POST)
```

- Tidak ada header nonce terpisah. Hanya key id, timestamp, signature.
- Format timestamp & payload yang di-sign: **BELUM ADA → tandatangani hanya setelah docs masuk**.

## Endpoint

| Method | Path | Auth | Status |
|---|---|---|---|
| GET | `/v1/rate-limits` | publik | CONFIRMED |
| GET | `/v1/orderbook/{symbol}?depth=N` | publik | CONFIRMED |
| GET | `/v1/trading/rate-limits?subaccountId=...` | privat | CONFIRMED (tes signing pertama) |
| POST | `/v1/orders/preview` | privat | CONFIRMED (validasi tanpa eksekusi) |
| POST | `/v1/orders` | privat | CONFIRMED |
| POST | `/v1/orders/modify` | privat | CONFIRMED |
| POST | `/v1/orders/cancel` | privat | CONFIRMED |
| GET | `/v1/orders/open` | privat | CONFIRMED |
| GET | `/v1/orders/history` | privat | CONFIRMED |

## Schema POST /v1/orders (CONFIRMED dari contoh dashboard)

```json
{
  "subaccountId": "<ACCOUNT_ID>",
  "order": {
    "symbol": "BTC-USDT",
    "side": "BUY",
    "baseQty": "100000",
    "clientOrderId": "cli-ord-20250101-001",
    "feeAsset": "QUOTE",
    "selfTradePreventionMode": "EXPIRE_MAKER",
    "marketIoc": { "clientRefPrice": "0.005", "maxSlippageTicks": 1 },
    "attachedRisk": {
      "oco": false,
      "stopLoss":   { "child": { "marketIoc": {} }, "triggerPrice": "0.005" },
      "takeProfit": { "child": { "marketIoc": {} }, "triggerPrice": "0.005" }
    }
  }
}
```

Inferensi (belum semua terkonfirmasi docs):
- `baseQty`, `clientRefPrice`, `triggerPrice` → string; `maxSlippageTicks` → number.
- Tidak ada `type` top-level → tipe order = nama key object di dalam `order` (`marketIoc`, kemungkinan `limitGtc`, `limitIoc`, dll — **TBD**).
- `subaccountId` string alfanumerik pendek. Contoh pada docs milik orang lain diganti `<ACCOUNT_ID>`; subaccount nyata = `MDaHfjadD1T` (disimpan di .env). JANGAN gunakan contoh akun seseorang di kode.
- `attachedRisk` opsional (SL/TP via OCO).

## Cancel (POST /v1/orders/cancel) — flat body

```json
{ "orderId": "5USXJZmk", "subaccountId": "<ACCOUNT_ID>", "symbol": "BTC-USDT" }
```

## Modify (POST /v1/orders/modify) — flat body

```json
{
  "behavior": "AMEND_OR_REPLACE",
  "orderId": "5USXJZmk",
  "subaccountId": "<ACCOUNT_ID>",
  "symbol": "BTC-USDT",
  "newPrice": "0.005",
  "newQtyScaled": 100000,
  "newClientOrderId": "cli-ord-20250101-001",
  "newAttachedRisk": {},
  "requestId": "request_01J0EXAMPLE"
}
```

## GET /v1/orders/open & /v1/orders/history — query params

`subaccountId` (wajib), `symbol`, `side`, `limit`, `includeAttachedRisk`,
`includeAttachedRiskState`, `triggerId`. History tambahan: `startTime`/`endTime`
(format **nanosecond, 19 digit**, contoh `1735689600000000000`), `status`.

## TODO / MASIH KURANG (JANGAN ngarang — tunggu docs/dashboard)

1. **Skema signing Ed25519**: payload yang di-sign (raw body? `timestamp+method+path+body`?),
   di-hash SHA256 dulu atau langsung Ed25519? Encode hex atau base64?
2. **Timestamp format** (ms/µs/ns) + toleransi window.
3. **Endpoint balance** — kandidat belum dikonfirmasi: `/v1/accounts`, `/v1/portfolio`,
   `/v1/wallets`, `/v1/balances`, `/v1/subaccounts/{id}/balances`.
4. Contoh response success & error (untuk parsing).
5. Daftar lengkap order type object (`limitGtc`, `postOnly`, dsb.) + field wajib.
6. Struktur error (format `{code, message}`? mapping HTTP status?).
