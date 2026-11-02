"""Spending Trends: home dashboard, reports, trends and a Kraken Bitcoin view.

Firefly III is read with a personal access token; transactions go to the
browser as compact rows and all grouping and charting happens there.
Kraken is read with a read-only API key (query funds + query ledger). BTC
ledger entries are synced in a background thread, matched with their euro
side, and cached in /data so only new entries are fetched later.
Standard library only.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_FILE = DATA_DIR / "config.json"
KRAKEN_FILE = DATA_DIR / "kraken.json"
PRICES_FILE = DATA_DIR / "btc_prices.json"
PORT = int(os.environ.get("PORT", "8080"))
CACHE_SECONDS = 600
KRAKEN_API = os.environ.get("KRAKEN_API", "https://api.kraken.com")
BINANCE_API = os.environ.get("BINANCE_API", "https://api.binance.com")
STORES = {"layout", "reports", "prefs"}
SYNC_EVERY = 6 * 3600
KRAKEN_PAUSE = float(os.environ.get("KRAKEN_PAUSE", "2.5"))

DEFAULTS = {
    "firefly_url": os.environ.get("FIREFLY_URL", "http://firefly-iii_server_1:8080"),
    "token": os.environ.get("FIREFLY_TOKEN", ""),
    "kraken_key": os.environ.get("KRAKEN_KEY", ""),
    "kraken_secret": os.environ.get("KRAKEN_SECRET", ""),
    "history_start": "",
}

cache = {}
cache_lock = threading.Lock()
file_lock = threading.Lock()


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update({k: v for k, v in json.loads(CONFIG_FILE.read_text()).items() if v})
    except (OSError, ValueError):
        pass
    return cfg


def write_json(path, obj, private=False):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with file_lock:
        tmp.write_text(json.dumps(obj, ensure_ascii=False))
        if private:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_config(cfg):
    write_json(CONFIG_FILE, cfg, private=True)


def http_json(url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "spending-trends/2.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ------------------------------------------------------------- Firefly ---
def firefly_get(cfg, path, params):
    url = cfg["firefly_url"].rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    return http_json(url, headers={"Authorization": f"Bearer {cfg['token']}",
                                   "Accept": "application/vnd.api+json"})


def history_start(cfg):
    s = cfg.get("history_start") or ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    return f"{date.today().year - 5}-01-01"


def fetch_rows(cfg, start, end):
    """Withdrawals and deposits as rows:
    [month, e|i, amount, category, budget, tags, payee, currency, account, date, description]"""
    rows, currencies = [], {}
    for kind in ("withdrawal", "deposit"):
        page = 1
        while True:
            res = firefly_get(cfg, "/api/v1/transactions",
                              {"type": kind, "start": start, "end": end, "page": page, "limit": 500})
            for group in res.get("data", []):
                for t in group.get("attributes", {}).get("transactions", []):
                    if t.get("type") not in ("withdrawal", "deposit"):
                        continue
                    cur = t.get("currency_code") or "?"
                    currencies[cur] = currencies.get(cur, 0) + 1
                    out = t["type"] == "withdrawal"
                    rows.append([
                        t["date"][:7],
                        "e" if out else "i",
                        round(abs(float(t.get("amount") or 0)), 2),
                        t.get("category_name") or "",
                        t.get("budget_name") or "",
                        t.get("tags") or [],
                        (t.get("destination_name") if out else t.get("source_name")) or "",
                        cur,
                        (t.get("source_name") if out else t.get("destination_name")) or "",
                        t["date"][:10],
                        (t.get("description") or "")[:120],
                    ])
            pages = res.get("meta", {}).get("pagination", {}).get("total_pages", 1)
            if page >= pages:
                break
            page += 1
    return {"rows": rows, "currencies": currencies, "start": start, "end": end,
            "fetched": int(time.time())}


def fetch_accounts(cfg, on_date=None):
    out, page = [], 1
    while True:
        params = {"type": "asset", "page": page, "limit": 200}
        if on_date:
            params["date"] = on_date
        res = firefly_get(cfg, "/api/v1/accounts", params)
        for a in res.get("data", []):
            at = a.get("attributes", {})
            out.append({
                "id": a.get("id"), "name": at.get("name") or "?",
                "role": at.get("account_role") or "",
                "balance": round(float(at.get("current_balance") or 0), 2),
                "currency": at.get("currency_code") or "EUR",
                "active": bool(at.get("active", True)),
                "net": at.get("include_net_worth", True) is not False,
            })
        if page >= res.get("meta", {}).get("pagination", {}).get("total_pages", 1):
            break
        page += 1
    return out


def month_ends(months):
    today = date.today()
    ends = [today]
    d = today.replace(day=1) - timedelta(days=1)
    while len(ends) < months:
        ends.append(d)
        d = d.replace(day=1) - timedelta(days=1)
    return [e.isoformat() for e in reversed(ends)]


def fetch_networth(cfg, months):
    dates = month_ends(months)
    today = date.today().isoformat()

    def one(d):
        key = ("acc", d)
        ttl = CACHE_SECONDS if d == today else 6 * 3600
        with cache_lock:
            hit = cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        accs = fetch_accounts(cfg, d)
        with cache_lock:
            cache[key] = (time.time(), accs)
        return accs

    with ThreadPoolExecutor(4) as ex:
        snaps = list(ex.map(one, dates))
    names = {}
    for i, accs in enumerate(snaps):
        for a in accs:
            row = names.setdefault(a["name"], {"name": a["name"], "currency": a["currency"],
                                               "net": a["net"], "active": a["active"],
                                               "values": [0.0] * len(dates)})
            row["values"][i] = a["balance"]
            row["active"], row["net"] = a["active"], a["net"]
    return {"dates": dates, "accounts": list(names.values()), "fetched": int(time.time())}


# -------------------------------------------------------------- Kraken ---
BTC_BASES = {"XXBT", "XBT"}
EUR_BASES = {"ZEUR", "EUR"}
NO_COUNTERPART = {"deposit", "withdrawal", "transfer", "staking", "reward", "dividend",
                  "credit", "earn", "custodytransfer", "airdrop"}
INTERNAL_SUB = re.compile(r"spot|staking|earn|futures|alloc|margin|bond|parachain", re.I)
REWARD_TYPES = {"staking", "reward", "dividend", "credit", "earn", "airdrop"}


def is_btc(asset):
    return (asset or "").split(".")[0] in BTC_BASES


def is_eur(asset):
    return (asset or "").split(".")[0] in EUR_BASES


def kraken_private(method, params, key, secret):
    path = f"/0/private/{method}"
    for attempt in range(7):
        data = {"nonce": str(int(time.time() * 1000)), **params}
        postdata = urllib.parse.urlencode(data)
        msg = path.encode() + hashlib.sha256((data["nonce"] + postdata).encode()).digest()
        sig = base64.b64encode(hmac.new(base64.b64decode(secret), msg, hashlib.sha512).digest()).decode()
        res = http_json(KRAKEN_API + path, data=postdata.encode(), timeout=30, headers={
            "API-Key": key, "API-Sign": sig,
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"})
        errs = res.get("error") or []
        if any("Rate limit" in e or "Too many" in e for e in errs):
            time.sleep(min(60, (15 + attempt * 10) * KRAKEN_PAUSE / 2.5))
            continue
        if errs:
            raise RuntimeError(", ".join(errs))
        return res["result"]
    raise RuntimeError("Kraken rate limit: gave up after several retries")


sync_state = {"running": False, "step": "", "done": 0, "total": 0, "error": "", "finished": 0}
sync_lock = threading.Lock()


def set_sync(**kw):
    with sync_lock:
        sync_state.update(kw)


def kraken_fetch_btc(cfg, st):
    """All BTC ledger entries since the last sync (newest first, paged by ofs)."""
    since = max(0, int(st.get("last", 0)) - 86400)
    filters = [st["filter"]] if st.get("filter") else ["XXBT,XBT.M,XBT.F,XBT.B,XBT.S", "XBT,XBT.M,XBT.F,XBT.B", ""]
    last_err = None
    for flt in filters:
        try:
            ofs, got = 0, {}
            while True:
                params = {"start": since, "ofs": ofs}
                if flt:
                    params["asset"] = flt
                res = kraken_private("Ledgers", params, cfg["kraken_key"], cfg["kraken_secret"])
                page = res.get("ledger", {})
                got.update({k: v for k, v in page.items() if is_btc(v.get("asset"))})
                ofs += len(page)
                set_sync(step=f"Reading BTC ledger ({ofs} of {res.get('count', '?')})")
                if not page or ofs >= int(res.get("count", 0)):
                    break
                time.sleep(KRAKEN_PAUSE)
            st["filter"] = flt
            return got
        except RuntimeError as e:
            last_err = e
            if "asset" not in str(e).lower() and "invalid" not in str(e).lower():
                raise
    raise last_err


def kraken_sync(cfg):
    if not (cfg.get("kraken_key") and cfg.get("kraken_secret")):
        return
    with sync_lock:
        if sync_state["running"]:
            return
        sync_state.update(running=True, error="", step="Starting", done=0, total=0)
    try:
        st = read_json(KRAKEN_FILE, {})
        st.setdefault("btc", {})
        st.setdefault("ctr", {})
        new = kraken_fetch_btc(cfg, st)
        for k, v in new.items():
            v["id"] = k
            st["btc"][k] = v
        if st["btc"]:
            st["last"] = max(float(v["time"]) for v in st["btc"].values())
        write_json(KRAKEN_FILE, st)

        # euro (or other) side of each BTC trade, looked up in a small time window
        todo = {}
        for v in st["btc"].values():
            if v.get("type") in NO_COUNTERPART or v["refid"] in st["ctr"]:
                continue
            todo.setdefault(v["refid"], float(v["time"]))
        windows = []
        for t in sorted(set(todo.values())):
            if windows and t - windows[-1][1] <= 5:
                windows[-1][1] = t
            else:
                windows.append([t, t])
        set_sync(total=len(windows), done=0)
        for i, (a, b) in enumerate(windows):
            set_sync(step=f"Matching euro side of trades ({i + 1} of {len(windows)})", done=i)
            res = kraken_private("Ledgers", {"start": int(a) - 3, "end": int(b) + 3},
                                 cfg["kraken_key"], cfg["kraken_secret"])
            found = {}
            for k, v in res.get("ledger", {}).items():
                if is_btc(v.get("asset")):
                    continue
                v["id"] = k
                found.setdefault(v["refid"], []).append(v)
            for ref, t in todo.items():
                if a - 1 <= t <= b + 1:
                    st["ctr"][ref] = found.get(ref, [])
            if i % 10 == 9:
                write_json(KRAKEN_FILE, st)
            time.sleep(KRAKEN_PAUSE)

        set_sync(step="Reading balance")
        try:
            st["balance"] = kraken_private("Balance", {}, cfg["kraken_key"], cfg["kraken_secret"])
            st["balance_at"] = int(time.time())
        except RuntimeError as e:
            st["balance_error"] = str(e)
        write_json(KRAKEN_FILE, st)

        set_sync(step="Updating price history")
        update_prices()
        set_sync(running=False, step="", finished=int(time.time()))
    except Exception as e:  # noqa: BLE001 - shown in the UI
        set_sync(running=False, step="", error=str(e), finished=int(time.time()))


def start_sync(cfg):
    threading.Thread(target=kraken_sync, args=(cfg,), daemon=True).start()


def sync_loop():
    while True:
        cfg = load_config()
        if cfg.get("kraken_key"):
            kraken_sync(cfg)
        time.sleep(SYNC_EVERY)


# ------------------------------------------------------- BTC prices ---
def update_prices():
    """Daily BTC/EUR closes: Binance (since 2020) plus Kraken OHLC (last 720 days)."""
    pr = read_json(PRICES_FILE, {"days": {}})
    days = pr["days"]
    start_ms = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    if days:
        last = max(days)
        start_ms = int(datetime.fromisoformat(last).replace(tzinfo=timezone.utc).timestamp() * 1000) - 3 * 86400000
    try:
        for _ in range(10):
            q = urllib.parse.urlencode({"symbol": "BTCEUR", "interval": "1d", "startTime": start_ms, "limit": 1000})
            kl = http_json(f"{BINANCE_API}/api/v3/klines?{q}", timeout=30)
            if not kl:
                break
            for k in kl:
                d = datetime.fromtimestamp(k[0] / 1000, timezone.utc).date().isoformat()
                days[d] = round(float(k[4]))
            if len(kl) < 1000:
                break
            start_ms = kl[-1][0] + 86400000
    except Exception:  # noqa: BLE001 - Kraken below is the fallback
        pass
    try:
        res = http_json(f"{KRAKEN_API}/0/public/OHLC?pair=XBTEUR&interval=1440", timeout=30)
        for rows in res.get("result", {}).values():
            if isinstance(rows, list):
                for k in rows:
                    d = datetime.fromtimestamp(int(k[0]), timezone.utc).date().isoformat()
                    days.setdefault(d, round(float(k[4])))
    except Exception:  # noqa: BLE001
        pass
    pr["updated"] = int(time.time())
    write_json(PRICES_FILE, pr)
    return pr


spot_cache = {"t": 0, "v": None}


def spot_price():
    if time.time() - spot_cache["t"] < 60 and spot_cache["v"]:
        return spot_cache["v"]
    try:
        res = http_json(f"{KRAKEN_API}/0/public/Ticker?pair=XBTEUR", timeout=15)
        v = float(next(iter(res["result"].values()))["c"][0])
        spot_cache.update(t=time.time(), v=v)
    except Exception:  # noqa: BLE001
        pass
    return spot_cache["v"]


def classify(st):
    """Group BTC ledger entries by refid and turn each group into one transaction."""
    groups = {}
    for v in st.get("btc", {}).values():
        groups.setdefault(v["refid"], []).append(v)
    tx, onk = [], 0.0
    for ref, entries in groups.items():
        all_e = entries + st.get("ctr", {}).get(ref, [])
        btc_amt = sum(float(e["amount"]) for e in entries)
        btc_fee = sum(float(e["fee"]) for e in entries)
        onk += btc_amt - btc_fee
        eur_net = sum(float(e["amount"]) - float(e["fee"]) for e in all_e if is_eur(e["asset"]))
        eur_fee = sum(float(e["fee"]) for e in all_e if is_eur(e["asset"]))
        other = {}
        for e in all_e:
            if not is_btc(e["asset"]) and not is_eur(e["asset"]):
                other[e["asset"]] = other.get(e["asset"], 0) + float(e["amount"]) - float(e["fee"])
        net = btc_amt - btc_fee
        t = min(float(e["time"]) for e in entries)
        types = sorted({e.get("type", "") for e in all_e})
        subtypes = sorted({e.get("subtype", "") for e in all_e if e.get("subtype")})
        row = {"t": int(t), "ref": ref, "ktype": "/".join(types), "sub": "/".join(subtypes),
               "btc": 0.0, "eur": 0.0, "feeEur": 0.0, "feeBtc": round(btc_fee, 8), "other": ""}
        if other:
            row["other"] = ", ".join(f"{a} {v:+.4f}" for a, v in other.items())
        kt = entries[0].get("type", "")
        if net > 0 and eur_net < 0:
            row.update(type="Buy", btc=net, eur=-eur_net)
            row["feeEur"] = eur_fee + (btc_fee * (-eur_net) / net if net else 0)
        elif net < 0 and eur_net > 0:
            row.update(type="Sell", btc=-net, eur=eur_net, feeEur=eur_fee)
        elif other and net > 0:
            row.update(type="Convert in", btc=net)
        elif other and net < 0:
            row.update(type="Convert out", btc=-net)
        elif kt == "deposit":
            row.update(type="Deposit", btc=net)
        elif kt == "withdrawal":
            row.update(type="Withdrawal", btc=-btc_amt)
        elif kt in ("transfer", "custodytransfer") and (net <= 0 or INTERNAL_SUB.search(row["sub"] or "")):
            row.update(type="Internal", btc=net)
        elif (kt in REWARD_TYPES or kt in ("transfer", "custodytransfer", "receive")) and net > 0:
            row.update(type="Reward", btc=net)
        else:
            row.update(type="Unmatched", btc=net)
        row["btc"] = round(row["btc"], 8)
        row["eur"] = round(row["eur"], 2)
        row["feeEur"] = round(row["feeEur"], 2)
        tx.append(row)
    tx.sort(key=lambda r: r["t"])
    return tx, onk


def price_series(tx):
    pr = read_json(PRICES_FILE, {"days": {}})
    anchors = dict(pr.get("days", {}))
    for r in tx:
        if r["type"] in ("Buy", "Sell") and r["btc"] > 0:
            d = datetime.fromtimestamp(r["t"], timezone.utc).date().isoformat()
            anchors.setdefault(d, round(r["eur"] / r["btc"]))
    if not anchors:
        return {"start": date.today().isoformat(), "px": []}
    if tx:
        first = datetime.fromtimestamp(tx[0]["t"], timezone.utc).date().isoformat()
    else:
        first = max(min(anchors), (date.today() - timedelta(days=730)).isoformat())
    d0 = date.fromisoformat(first)
    n = (date.today() - d0).days + 1
    known = sorted(((date.fromisoformat(k) - d0).days, v) for k, v in anchors.items() if k >= first)
    px = [None] * n
    for i, v in known:
        if 0 <= i < n:
            px[i] = v
    # linear fill between known days, flat at both ends
    idx = [i for i in range(n) if px[i] is not None]
    if not idx:
        return {"start": first, "px": []}
    for i in range(idx[0]):
        px[i] = px[idx[0]]
    for a, b in zip(idx, idx[1:]):
        for i in range(a + 1, b):
            px[i] = round(px[a] + (px[b] - px[a]) * (i - a) / (b - a))
    for i in range(idx[-1] + 1, n):
        px[i] = px[idx[-1]]
    return {"start": first, "px": px, "updated": pr.get("updated", 0)}


def btc_payload(cfg):
    st = read_json(KRAKEN_FILE, {})
    tx, onk = classify(st)
    bal = st.get("balance") or {}
    with sync_lock:
        sync = dict(sync_state)
    return {
        "configured": bool(cfg.get("kraken_key") and cfg.get("kraken_secret")),
        "tx": tx,
        "ledger_btc": round(onk, 8),
        "balance_btc": round(sum(float(v) for k, v in bal.items() if is_btc(k)), 8) if bal else None,
        "balance_at": st.get("balance_at", 0),
        "balance_error": st.get("balance_error", ""),
        "prices": price_series(tx),
        "spot": spot_price(),
        "sync": sync,
    }


# ------------------------------------------------------------- server ---
class Handler(BaseHTTPRequestHandler):
    server_version = "SpendingTrends/2.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

    def _body(self):
        try:
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return None

    def _firefly(self, fn):
        cfg = load_config()
        if not cfg["token"]:
            return self._json(400, {"error": "No Firefly token set yet. Open Settings."})
        try:
            return self._json(200, fn(cfg))
        except urllib.error.HTTPError as e:
            return self._json(502, {"error": "Firefly rejected the token (401)." if e.code == 401 else f"Firefly error {e.code}."})
        except Exception as e:  # noqa: BLE001
            return self._json(502, {"error": f"Cannot reach Firefly at {cfg['firefly_url']}: {e}"})

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        p = url.path
        if p in ("/", "/index.html"):
            return self._send(200, (APP_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        if p == "/api/config":
            cfg = load_config()
            return self._json(200, {"firefly_url": cfg["firefly_url"], "token_set": bool(cfg["token"]),
                                    "kraken_set": bool(cfg["kraken_key"] and cfg["kraken_secret"]),
                                    "history_start": history_start(cfg)})
        if p == "/api/data":
            def get(cfg):
                start = q.get("start", [history_start(cfg)])[0]
                end = q.get("end", [date.today().isoformat()])[0]
                key = ("rows", start, end)
                with cache_lock:
                    hit = cache.get(key)
                if hit and time.time() - hit["fetched"] < CACHE_SECONDS and "refresh" not in q:
                    return hit
                data = fetch_rows(cfg, start, end)
                with cache_lock:
                    for k in [k for k in cache if k[0] == "rows"]:
                        del cache[k]
                    cache[key] = data
                return data
            return self._firefly(get)
        if p == "/api/accounts":
            return self._firefly(lambda cfg: {"accounts": fetch_accounts(cfg)})
        if p == "/api/networth":
            months = max(2, min(180, int((q.get("months") or ["36"])[0] or 36)))
            if "refresh" in q:
                with cache_lock:
                    for k in [k for k in cache if k[0] == "acc"]:
                        del cache[k]
            return self._firefly(lambda cfg: fetch_networth(cfg, months))
        if p == "/api/btc":
            return self._json(200, btc_payload(load_config()))
        if p.startswith("/api/store/"):
            name = p.rsplit("/", 1)[-1]
            if name not in STORES:
                return self._json(404, {"error": "unknown store"})
            return self._json(200, {"value": read_json(DATA_DIR / f"store_{name}.json", None)})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/btc/sync":
            cfg = load_config()
            if not cfg.get("kraken_key"):
                return self._json(400, {"error": "No Kraken key set yet. Open Settings."})
            start_sync(cfg)
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})

    def do_PUT(self):
        body = self._body()
        if body is None:
            return self._json(400, {"error": "invalid json"})
        if self.path.startswith("/api/store/"):
            name = self.path.rsplit("/", 1)[-1]
            if name not in STORES:
                return self._json(404, {"error": "unknown store"})
            write_json(DATA_DIR / f"store_{name}.json", body.get("value"))
            return self._json(200, {"ok": True})
        if self.path != "/api/config":
            return self._json(404, {"error": "not found"})
        cfg = load_config()
        if body.get("firefly_url"):
            cfg["firefly_url"] = body["firefly_url"].strip()
        if body.get("token"):
            cfg["token"] = body["token"].strip()
        if "history_start" in body:
            hs = (body.get("history_start") or "").strip()
            if hs and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", hs):
                return self._json(400, {"error": "History start must look like 2021-01-01."})
            cfg["history_start"] = hs
        try:
            firefly_get(cfg, "/api/v1/about", {})
        except urllib.error.HTTPError as e:
            return self._json(400, {"error": "Token rejected by Firefly." if e.code == 401 else f"Firefly error {e.code}"})
        except Exception as e:  # noqa: BLE001
            return self._json(400, {"error": f"Cannot reach Firefly: {e}"})
        kraken_changed = False
        if body.get("kraken_key") or body.get("kraken_secret"):
            k = (body.get("kraken_key") or cfg.get("kraken_key") or "").strip()
            s = (body.get("kraken_secret") or cfg.get("kraken_secret") or "").strip()
            try:
                kraken_private("Balance", {}, k, s)
            except Exception as e:  # noqa: BLE001
                return self._json(400, {"error": f"Kraken rejected the key: {e}"})
            kraken_changed = (k, s) != (cfg.get("kraken_key"), cfg.get("kraken_secret"))
            cfg["kraken_key"], cfg["kraken_secret"] = k, s
        if body.get("kraken_remove"):
            cfg["kraken_key"] = cfg["kraken_secret"] = ""
        save_config(cfg)
        with cache_lock:
            cache.clear()
        if kraken_changed:
            start_sync(cfg)
        self._json(200, {"ok": True})


if __name__ == "__main__":
    print(f"Spending Trends on :{PORT}", flush=True)
    threading.Thread(target=sync_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
