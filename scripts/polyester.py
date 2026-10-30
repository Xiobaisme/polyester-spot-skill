#!/usr/bin/env python3
"""Polyester TESTNET spot CLI untuk skill Hermes.

Perintah (semua output JSON):
  markets                     daftar pair ENABLED (default quote USDT)
  scan   [--top N]            ranking pair dari trade terbaru (volume, perubahan, aktivitas)
  price  SYMBOL               harga terakhir + aturan pair
  rate-limits                 publik: GET /v1/rate-limits (sanity check)
  orderbook SYMBOL [--depth]  publik: GET /v1/orderbook/{symbol}
  buy    SYMBOL --usdt X      beli market (atau --qty Q)  [--dry-run]
  sell   SYMBOL --qty Q       jual market  (atau --usdt X)  [--dry-run]
  balance/orders/cancel/preview/modify/history/open/trading-rate-limits   privat (fail-closed s.d. signing selesai)

Hanya stdlib. Base URL dikunci ke TESTNET.
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, getcontext

getcontext().prec = 40

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR_ENV = os.path.join(SKILL_DIR, ".env")

STABLES = {"USDT", "USDC"}
DATA_DIR = os.path.expanduser("~/.polyester-spot")
TRADE_LOG = os.path.join(DATA_DIR, "trades.jsonl")


def _load_env_file():
    # Sumber utama .env di folder skill (di samping script), cadangan di DATA_DIR.
    # Konvensi: line pertama yang menang, jadi skill folder dibaca paling akhir (paling kuat).
    paths = [
        SKILL_DIR_ENV,
        os.path.join(DATA_DIR, ".env"),
    ]
    for path in paths:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")


_load_env_file()

BASE_URL = os.environ.get("POLYESTER_BASE_URL", "https://api.testnet.polyester.com")
assert "testnet" in BASE_URL, "hanya testnet yang diizinkan"


# Batas keras (bisa diubah lewat env). Agent bebas milih size, tapi tidak boleh lewat ini.
MAX_ORDER_USDT = Decimal(os.environ.get("POLYESTER_MAX_ORDER_USDT", "25"))
MAX_ORDERS_PER_DAY = int(os.environ.get("POLYESTER_MAX_ORDERS_PER_DAY", "5000"))


def out(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    sys.exit(code)


def fail(error, detail=None, code=1):
    out({"ok": False, "error": error, "detail": detail}, code)


# --------------------------------------------------------------------------
# HTTP publik
# --------------------------------------------------------------------------
def http_get(path, params=None):
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "hermes-polyester-spot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        fail("HTTP_ERROR", {"status": e.code, "path": path, "body": e.read().decode()[:500]})
    except Exception as e:  # noqa: BLE001
        fail("NETWORK_ERROR", {"path": path, "msg": str(e)})


def http_post(path, body=None):
    """POST JSON publik (endpoint privat lewat signed_request, bukan ini)."""
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method="POST",
        headers={"Accept": "*/*", "Content-Type": "application/json", "User-Agent": "hermes-polyester-spot/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        fail("HTTP_ERROR", {"status": e.code, "path": path, "body": e.read().decode()[:500]})
    except Exception as e:  # noqa: BLE001
        fail("NETWORK_ERROR", {"path": path, "msg": str(e)})


def get_config():
    # POLYESTER_CONFIG_FILE dipakai untuk tes offline saja
    f = os.environ.get("POLYESTER_CONFIG_FILE")
    if f:
        with open(f) as fh:
            return json.load(fh)
    return http_get("/v1/spot/config")


def pair_map(cfg):
    return {p["symbol"]: p for p in cfg.get("pairs", []) if "symbol" in p}


def get_trades(symbol, limit=100):
    d = http_get(f"/v1/spot/markets/{symbol}/trades", {"limit": limit})
    return d.get("trades", [])


def get_candles(symbol, timeframe="1m", limit=5):
    """Lilin OHLC dari GET /v1/spot/markets/{symbol}/candles.
    Format candle: [openTimeSec, open, high, low, close, baseVolume, quoteVolume]."""
    d = http_get(f"/v1/spot/markets/{symbol}/candles", {"timeframe": timeframe, "limit": limit})
    return d.get("candles", [])


def parse_ts(s):
    # RFC3339 dengan nanodetik -> potong ke mikrodetik
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        frac = ""
        i = 0
        while i < len(rest) and rest[i].isdigit():
            frac += rest[i]
            i += 1
        s = f"{head}.{frac[:6].ljust(6, '0')}{rest[i:]}"
    return datetime.fromisoformat(s)


# --------------------------------------------------------------------------
# Logika pair, scan, validasi order
# --------------------------------------------------------------------------
def candidates(cfg, quote="USDT"):
    res = []
    for p in cfg["pairs"]:
        if p.get("status") != "ENABLED" or p.get("delistingAt"):
            continue
        if p["quoteAsset"] != quote:
            continue
        if p["baseAsset"] in STABLES:  # pair stablecoin tidak ada trennya
            continue
        res.append(p)
    return res


def trade_stats(pair, trades):
    if not trades:
        return None
    last = Decimal(trades[0]["price"])
    first = Decimal(trades[-1]["price"])
    qv = sum(Decimal(t["price"]) * Decimal(t["qty"]) for t in trades)
    buy_qv = sum(Decimal(t["price"]) * Decimal(t["qty"]) for t in trades if t.get("side") == "BUY")
    span = (parse_ts(trades[0]["executedAt"]) - parse_ts(trades[-1]["executedAt"])).total_seconds()
    min_cost = max(Decimal(pair["minNotionalQuote"]), Decimal(pair["minQtyBase"]) * last)
    return {
        "symbol": pair["symbol"],
        "last": str(last),
        "change_pct_in_sample": float((last - first) / first * 100) if first else None,
        "quote_volume_in_sample": float(qv),
        "buy_share": float(buy_qv / qv) if qv else None,
        "trades_in_sample": len(trades),
        "sample_span_sec": span,
        "trades_per_min": (len(trades) / span * 60) if span > 0 else None,
        "min_order_cost_usdt": str(min_cost),
        "default_slippage_bps": pair["defaultMarketSlippageBpsBuy"],
        "last_trade_at": trades[0]["executedAt"],
    }


def floor_to(value, step):
    step = Decimal(step)
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def normalize_order(pair, side, last, qty=None, usdt=None):
    """Bulatkan & validasi. Return dict order siap kirim atau fail()."""
    if pair["status"] != "ENABLED":
        fail("PAIR_DISABLED", pair["symbol"])
    if pair["quoteAsset"] not in STABLES:
        fail("QUOTE_NOT_SUPPORTED", "skill ini hanya trade pair quote USDT/USDC")
    if (qty is None) == (usdt is None):
        fail("ARGS", "isi tepat salah satu: --qty atau --usdt")

    last = Decimal(last)
    if qty is None:
        qty = Decimal(usdt) / last
    qty = floor_to(Decimal(qty), pair["stepSize"])

    if qty < Decimal(pair["minQtyBase"]):
        fail("BELOW_MIN_QTY", {"qty": str(qty), "minQtyBase": pair["minQtyBase"]})
    notional = qty * last
    if notional < Decimal(pair["minNotionalQuote"]):
        fail("BELOW_MIN_NOTIONAL", {"notional": str(notional), "min": pair["minNotionalQuote"]})
    if notional > MAX_ORDER_USDT:
        fail("ABOVE_MAX_ORDER", {"notional": str(notional), "cap": str(MAX_ORDER_USDT),
                                 "hint": "kecilkan size, cap di env POLYESTER_MAX_ORDER_USDT"})
    slip = pair["defaultMarketSlippageBpsBuy" if side == "BUY" else "defaultMarketSlippageBpsSell"]
    return {
        "symbol": pair["symbol"],
        "symbolId": pair["symbolId"],
        "side": side,
        "type": "MARKET_IOC",
        "qty": format(qty.normalize(), "f"),
        "ref_price": str(last),
        "est_notional_quote": str(notional.quantize(Decimal("0.0001"))),
        "max_slippage_bps": slip,
    }


def orders_today():
    if not os.path.exists(TRADE_LOG):
        return 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    n = 0
    with open(TRADE_LOG) as f:
        for line in f:
            if line.startswith('{"day": "%s"' % today):
                n += 1
    return n


def log_trade(entry):
    os.makedirs(DATA_DIR, exist_ok=True)
    entry = {"day": datetime.now(timezone.utc).strftime("%Y-%m-%d"), **entry}
    with open(TRADE_LOG, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# --------------------------------------------------------------------------
# AUTH — signing Ed25519 (sesuai tes_api.py / SDK resmi, proven HTTP 200).
# Rumus string yang di-sign (wajib newline, \n):
#   f"{timestamp_ms}\n{METHOD}\n{PATH}\n{QUERY}\n{sha256_hex(BODY)}"
# - timestamp: milidetik (ms).
# - QUERY = canonical_query(url) (sisa kosong kalau tidak ada query).
# - Signature: hex (bukan base64).
# Key dari env: POLYESTER_API_KEY_ID, POLYESTER_API_PRIVATE_KEY, POLYESTER_ACCOUNT_ID.
# --------------------------------------------------------------------------
import hashlib  # noqa: E402
from urllib.parse import parse_qsl, quote, urlsplit

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    _ED25519_AVAILABLE = True
except Exception:  # noqa: BLE001
    _ED25519_AVAILABLE = False

ORDER_TYPE_OBJECTS = {
    # tipe order = nama key object di dalam `order`.
    # marketIoc sudah verified; tambah limitGtc/limitIoc/postOnly begitu docs masuk.
    "marketIoc": {"clientRefPrice": "<string>", "maxSlippageTicks": "<number>"},
}


def _not_configured(what):
    fail("AUTH_NOT_CONFIGURED", f"{what} belum diimplementasi. Baca references/api.md.", code=2)


def _env(what):
    return os.environ.get(what) or _not_configured(what)


def _private_key():
    if not _ED25519_AVAILABLE:
        _not_configured("modul cryptography tidak terpasang (pip install cryptography)")
    hexkey = _env("POLYESTER_API_PRIVATE_KEY")
    try:
        raw = bytes.fromhex(hexkey)
    except ValueError:
        fail("BAD_PRIVATE_KEY", "POLYESTER_API_PRIVATE_KEY harus hex 64 karakter")
    if len(raw) != 32:
        fail("BAD_PRIVATE_KEY", f"panjang key {len(raw)} byte, harus 32 (hex 64 char)")
    return Ed25519PrivateKey.from_private_bytes(raw)


def canonical_query(url: str) -> str:
    """Canonical Query dari SDK asli (persis tes_api.py)."""
    pairs = []
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        pairs.append(f"{quote(key, safe='')}={quote(value, safe='')}")
    pairs.sort()
    return "&".join(pairs)


def sign_request(method, url, body_bytes=b""):
    """Header privat (persis tes_api.py). url = FULL URL (base + path + query)."""
    timestamp_ms = str(int(time.time() * 1000))
    path = urlsplit(url).path or "/"
    query = canonical_query(url)
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    string_to_sign = f"{timestamp_ms}\n{method.upper()}\n{path}\n{query}\n{body_hash}"
    sig = _private_key().sign(string_to_sign.encode("utf-8")).hex()
    return {
        "X-API-KEY-ID": _env("POLYESTER_API_KEY_ID"),
        "X-API-TIMESTAMP": timestamp_ms,
        "X-API-SIGNATURE": sig,
        "Accept": "*/*",
    }


def signed_request(method, path, params=None, body=None):
    """Endpoint privat: tanda tangan Ed25519 (Rumus newline SDK), kirim via urllib."""
    query_string = ""
    if params:
        query_string = urllib.parse.urlencode(params)
    full_url = f"{BASE_URL}{path}"
    if query_string:
        full_url += f"?{query_string}"
    raw_body = json.dumps(body).encode() if body is not None else b""
    headers = sign_request(method, full_url, raw_body)
    if body is not None:
        headers["Content-Type"] = "application/json"
    headers["User-Agent"] = "hermes-polyester-spot/1.0"
    req = urllib.request.Request(
        full_url,
        data=raw_body if body is not None else None,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err_raw = e.read().decode()[:500]
        try:
            err_json = json.loads(err_raw)
        except Exception:
            err_json = {}
        # Retry sekali dengan timestamp baru bila server anggap skew.
        # Guard global agar retry berhenti (max 1x per proses module).
        if err_json.get("code") == "TIMESTAMP_SKEW" and not globals().get("_skew_retried", False):
            globals()["_skew_retried"] = True
            time.sleep(0.5)
            return signed_request(method, path, params=params, body=body)
        fail("HTTP_ERROR", {"status": e.code, "path": full_url, "body": err_raw})
    except Exception as e:  # noqa: BLE001
        fail("NETWORK_ERROR", {"path": full_url, "msg": str(e)})


# --------------------------------------------------------------------------
# Perintah
# --------------------------------------------------------------------------
def cmd_markets(a):
    cfg = get_config()
    ps = candidates(cfg, a.quote)
    out({"ok": True, "quote": a.quote, "count": len(ps), "pairs": [
        {"symbol": p["symbol"], "minQtyBase": p["minQtyBase"], "minNotionalQuote": p["minNotionalQuote"],
         "stepSize": p["stepSize"], "tickSize": p["tickSize"],
         "slippage_bps": p["defaultMarketSlippageBpsBuy"]} for p in ps]})


def cmd_scan(a):
    cfg = get_config()
    rows = []
    for p in candidates(cfg, a.quote):
        st = trade_stats(p, get_trades(p["symbol"], a.limit))
        if st:
            rows.append(st)
        time.sleep(0.15)
    rows.sort(key=lambda r: r["quote_volume_in_sample"], reverse=True)
    out({"ok": True, "note": "statistik dari sampel trade terakhir, bukan volume 24 jam",
         "scanned": len(rows), "top": rows[: a.top]})


def cmd_price(a):
    cfg = get_config()
    pm = pair_map(cfg)
    if a.symbol not in pm:
        fail("UNKNOWN_SYMBOL", a.symbol)
    st = trade_stats(pm[a.symbol], get_trades(a.symbol, 50))
    if not st:
        fail("NO_TRADES", a.symbol)
    out({"ok": True, **st, "rules": {k: pm[a.symbol][k] for k in
         ("status", "minQtyBase", "minNotionalQuote", "stepSize", "tickSize")}})


def cmd_order(a, side):
    cfg = get_config()
    pm = pair_map(cfg)
    if a.symbol not in pm:
        fail("UNKNOWN_SYMBOL", a.symbol)
    trades = get_trades(a.symbol, 5)
    if not trades:
        fail("NO_TRADES", "tidak ada harga acuan untuk " + a.symbol)
    last = trades[0]["price"]
    order = normalize_order(pm[a.symbol], side, last,
                            qty=Decimal(a.qty) if a.qty else None,
                            usdt=Decimal(a.usdt) if a.usdt else None)
    if a.dry_run:
        out({"ok": True, "dry_run": True, "order": order})
    if orders_today() >= MAX_ORDERS_PER_DAY:
        fail("DAILY_ORDER_LIMIT", MAX_ORDERS_PER_DAY)
    resp = signed_request("POST", "/v1/orders", body=build_create_body(order))
    log_trade({"ts": time.time(), "order": order, "response": resp})
    out({"ok": True, "order": order, "response": resp})


def _orderbook_ask(symbol):
    """Harga ask terbaik dari GET /v1/orderbook/{symbol}?depth=1."""
    ob = http_get(f"/v1/orderbook/{symbol}", {"depth": 1})
    asks = ob.get("asks") or []
    if not asks:
        fail("NO_ASK", f"orderbook {symbol} tidak punya asks")
    return Decimal(asks[0][0])


def cmd_buy(a):
    """buy <SYMBOL> <QTY>: ask dari orderbook → clientRefPrice = ask×1.01 → POST /v1/orders."""
    ask = _orderbook_ask(a.symbol)
    # +1% biar pasti nerima fill
    ref = (ask * Decimal("1.01")).quantize(Decimal("0.000001"))
    order = {"symbol": a.symbol, "side": "BUY", "qty": a.qty, "ref_price": str(ref)}
    if orders_today() >= MAX_ORDERS_PER_DAY:
        fail("DAILY_ORDER_LIMIT", MAX_ORDERS_PER_DAY)
    body = build_create_body(order)
    resp = signed_request("POST", "/v1/orders", body=body)
    log_trade({"ts": time.time(), "order": order, "response": resp})
    out({"ok": True, "order": body, "response": resp})


def cmd_rate_limits(a):
    out({"ok": True, "publik": True, "endpoint": "GET /v1/rate-limits", "data": http_get("/v1/rate-limits")})


def cmd_orderbook(a):
    # publik, sanity check
    data = http_get(f"/v1/orderbook/{a.symbol}", {"depth": a.depth})
    out({"ok": True, "publik": True, "symbol": a.symbol, "data": data})


def cmd_symbols(a):
    cfg = get_config()
    pm = pair_map(cfg)
    rows = []
    for sym, p in sorted(pm.items()):
        rows.append({
            "symbol": sym,
            "baseAsset": p.get("baseAsset"),
            "quoteAsset": p.get("quoteAsset"),
            "status": p.get("status"),
            "minQtyBase": p.get("minQtyBase"),
            "minNotionalQuote": p.get("minNotionalQuote"),
            "stepSize": p.get("stepSize"),
            "tickSize": p.get("tickSize"),
        })
    out({"ok": True, "publik": True, "endpoint": "GET /v1/spot/config", "count": len(rows), "pairs": rows})


def cmd_candles(a):
    """candles SYMBOL [--timeframe 1m|5m|15m|1h|4h|1d] [--limit N] — data OHLC."""
    candles = get_candles(a.symbol, a.timeframe, a.limit)
    rows = []
    for c in candles:
        rows.append({
            "time": c[0], "open": c[1], "high": c[2], "low": c[3], "close": c[4],
            "baseVolume": c[5], "quoteVolume": c[6],
        })
    out({"ok": True, "publik": True, "symbol": a.symbol, "timeframe": a.timeframe, "count": len(rows), "candles": rows})


def build_create_body(order):
    """Bungkus order ke body POST /v1/orders. Field wajib dari contoh dashboard."""
    return {
        # "subaccountId": _env("POLYESTER_ACCOUNT_ID"),
        "order": {
            "symbol": order["symbol"],
            "side": order["side"],
            "baseQty": order["qty"],
            "clientOrderId": "hermes-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"),
            "feeAsset": "QUOTE",
            "selfTradePreventionMode": "EXPIRE_MAKER",
            "marketIoc": {
                "clientRefPrice": order["ref_price"],
                "maxSlippageTicks": 10000,
            },
        },
    }


def cmd_auth_only(a):
    _not_configured(a.cmd)


def cmd_trading_rate_limits(a):
    resp = signed_request("GET", "/v1/trading/rate-limits")  # tanpa query — server baca dari API Key
    out({"ok": True, "privat": True, "endpoint": "GET /v1/trading/rate-limits", "data": resp})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("markets"); s.add_argument("--quote", default="USDT"); s.set_defaults(fn=cmd_markets)
    s = sub.add_parser("scan"); s.add_argument("--quote", default="USDT")
    s.add_argument("--limit", type=int, default=100); s.add_argument("--top", type=int, default=5); s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("price"); s.add_argument("symbol"); s.set_defaults(fn=cmd_price)
    s = sub.add_parser("rate-limits"); s.set_defaults(fn=cmd_rate_limits)
    s = sub.add_parser("symbols"); s.set_defaults(fn=cmd_symbols)
    s = sub.add_parser("candles"); s.add_argument("symbol"); s.add_argument("--timeframe", default="1m"); s.add_argument("--limit", type=int, default=20); s.set_defaults(fn=cmd_candles)
    s = sub.add_parser("orderbook"); s.add_argument("symbol"); s.add_argument("--depth", type=int, default=1); s.set_defaults(fn=cmd_orderbook)
    s = sub.add_parser("buy"); s.add_argument("symbol"); s.add_argument("qty"); s.set_defaults(fn=cmd_buy)
    for side in ("sell",):
        s = sub.add_parser(side); s.add_argument("symbol")
        s.add_argument("--qty"); s.add_argument("--usdt"); s.add_argument("--dry-run", action="store_true")
        s.set_defaults(fn=lambda a, sd=side.upper(): cmd_order(a, sd))
    s = sub.add_parser("trading-rate-limits"); s.set_defaults(fn=cmd_trading_rate_limits)
    for name in ("balance", "orders", "cancel", "preview", "modify", "history", "open"):
        s = sub.add_parser(name); s.add_argument("rest", nargs="*"); s.set_defaults(fn=cmd_auth_only)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
