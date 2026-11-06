"""Spending Trends: home dashboard, reports, trends and a Kraken Bitcoin view.

Firefly III is read with a personal access token; transactions go to the
browser as compact rows and all grouping and charting happens there.
Kraken is read with a read-only API key (query funds + query ledger). BTC
ledger entries are synced in a background thread, matched with their euro
side, and cached in /data so only new entries are fetched later.
Standard library only.
"""

import agent
import base64
import btcwallet
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
CASHBACK_FILE = DATA_DIR / "cashback_booked.json"
STRIKE_FILE = DATA_DIR / "strike.json"
STRIKE_API = "https://api.strike.me/v1"
PRICES_FILE = DATA_DIR / "btc_prices.json"
WALLETS_FILE = DATA_DIR / "wallets.json"
MEMPOOL_DEFAULTS = [os.environ.get("MEMPOOL_URL", ""), "http://mempool_web_1:3006/api", "http://mempool_api_1:8999/api/v1"]
PORT = int(os.environ.get("PORT", "8080"))
CACHE_SECONDS = 600
KRAKEN_API = os.environ.get("KRAKEN_API", "https://api.kraken.com")
BINANCE_API = os.environ.get("BINANCE_API", "https://api.binance.com")
STORES = {"layout", "reports", "prefs", "subignore", "trendviews"}
AGENT_FILE = DATA_DIR / "agent.json"
NEWS_DIR = DATA_DIR / "news"
SYNC_EVERY = 6 * 3600
KRAKEN_PAUSE = float(os.environ.get("KRAKEN_PAUSE", "2.5"))

DEFAULTS = {
    "firefly_url": os.environ.get("FIREFLY_URL", "http://firefly-iii_server_1:8080"),
    "token": os.environ.get("FIREFLY_TOKEN", ""),
    "kraken_key": os.environ.get("KRAKEN_KEY", ""),
    "kraken_secret": os.environ.get("KRAKEN_SECRET", ""),
    "history_start": "",
    "mempool_url": "",
    "wallets": [],
    "agent_auto": False,
    "agent_hour": 8,
    "agent_days": 3,
    "agent_min_conf": 0.75,
    "ollama_url": "",
    "ollama_model": "",
    "news_lang": "en",
    "savings_categories": ["Crypto"],
    # Krak card cashback (BTC sent from Krak to Kraken) booked in Firefly as deposits
    "cashback_book": False,
    "cashback_account": "",
    "cashback_from": "",
    "btc_balance_sync": True,
    # Strike: read-only API key; its BTC balance is shown on the Bitcoin page and can be kept in Firefly
    "strike_key": "",
    "strike_account": "",
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
def more_pages(res, page):
    """True while Firefly has another page. Does not trust total_pages alone: some
    versions cap the page size, so a full page always means "ask for the next one"."""
    pg = res.get("meta", {}).get("pagination", {}) or {}
    data = res.get("data", [])
    if not data:
        return False
    if page < int(pg.get("total_pages") or 1):
        return True
    per = int(pg.get("per_page") or 0)
    return bool(per) and len(data) >= per and page < 2000


def firefly_put(cfg, path, body):
    req = urllib.request.Request(cfg["firefly_url"].rstrip("/") + path, method="PUT", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {cfg['token']}", "Accept": "application/vnd.api+json",
                                          "Content-Type": "application/json", "User-Agent": "spending-trends/2.2"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def firefly_post(cfg, path, body):
    req = urllib.request.Request(cfg["firefly_url"].rstrip("/") + path, method="POST", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {cfg['token']}", "Accept": "application/vnd.api+json",
                                          "Content-Type": "application/json", "User-Agent": "spending-trends/2.8"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def berlin_now():
    """Local time in Germany without tzdata: CET, CEST from the last Sunday of March to the last Sunday of October."""
    u = datetime.now(timezone.utc)
    def last_sunday(month):
        d = datetime(u.year, month, 31, 1, tzinfo=timezone.utc)
        return d - timedelta(days=(d.weekday() + 1) % 7)
    off = 2 if last_sunday(3) <= u < last_sunday(10) else 1
    return (u + timedelta(hours=off)).replace(tzinfo=None)


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
    [month, e|i, amount, category, budget, tags, payee, currency, account, date, description, loan]
    loan = 1 when the money goes to (or comes from) a liability: a repayment or a payout, not spending."""
    rows, currencies = [], {}
    for kind in ("withdrawal", "deposit", "transfer"):
        page = 1
        while True:
            res = firefly_get(cfg, "/api/v1/transactions",
                              {"type": kind, "start": start, "end": end, "page": page, "limit": 500})
            for group in res.get("data", []):
                for t in group.get("attributes", {}).get("transactions", []):
                    if t.get("type") == "transfer":
                        # a transfer to or from a liability is a repayment or a payout: keep it as a loan
                        # row (never spending); transfers between your own accounts stay out
                        st, dt = (t.get("source_type") or "").lower(), (t.get("destination_type") or "").lower()
                        if dt in LIAB_TYPES and st not in LIAB_TYPES:
                            t = {**t, "type": "withdrawal"}
                        elif st in LIAB_TYPES and dt not in LIAB_TYPES:
                            t = {**t, "type": "deposit"}
                        else:
                            continue
                    if t.get("type") not in ("withdrawal", "deposit"):
                        continue
                    cur = t.get("currency_code") or "?"
                    amt = abs(float(t.get("amount") or 0))
                    # the bitcoin in it: a BTC booking's own amount, or "(12345 sats)" / "0.0001 BTC" in its note
                    btc = amt if cur == "BTC" else 0.0
                    if not btc:
                        nt = t.get("notes") or ""
                        mm = re.search(r"(\d[\d.,]*)\s*sats\b", nt, re.I)
                        if mm:
                            btc = float(re.sub(r"[.,]", "", mm.group(1))) / 1e8
                        else:
                            mm = re.search(r"(\d+[.,]\d+)\s*BTC\b", nt)
                            btc = float(mm.group(1).replace(",", ".")) if mm else 0.0
                    # booked in another currency (e.g. BTC cashback) with its euro value as foreign amount:
                    # count it in euros
                    if cur != "EUR" and t.get("foreign_currency_code") == "EUR" and t.get("foreign_amount"):
                        cur, amt = "EUR", abs(float(t["foreign_amount"]))
                    currencies[cur] = currencies.get(cur, 0) + 1
                    out = t["type"] == "withdrawal"
                    rows.append([
                        t["date"][:7],
                        "e" if out else "i",
                        round(amt, 2),
                        t.get("category_name") or "",
                        t.get("budget_name") or "",
                        t.get("tags") or [],
                        (t.get("destination_name") if out else t.get("source_name")) or "",
                        cur,
                        (t.get("source_name") if out else t.get("destination_name")) or "",
                        t["date"][:10],
                        (t.get("description") or "")[:120],
                        1 if ((t.get("destination_type") if out else t.get("source_type")) or "").lower() in LIAB_TYPES else 0,
                        t.get("bill_name") or t.get("subscription_name") or "",
                        round(btc, 8),
                    ])
            if not more_pages(res, page):
                break
            page += 1
    return {"rows": rows, "currencies": currencies, "start": start, "end": end,
            "fetched": int(time.time())}


LIAB_TYPES = {"loan", "debt", "mortgage", "liabilities", "liability"}


def fetch_accounts(cfg, on_date=None):
    """Asset accounts and liabilities. Debts you owe come back as negative balances."""
    out = []
    for kind in ("asset", "liabilities"):
        page = 1
        while True:
            params = {"type": kind, "page": page, "limit": 200}
            if on_date:
                params["date"] = on_date
            try:
                res = firefly_get(cfg, "/api/v1/accounts", params)
            except urllib.error.HTTPError:
                if kind == "asset":
                    raise
                break
            for a in res.get("data", []):
                at = a.get("attributes", {})
                bal = float(at.get("current_balance") or 0)
                is_liab = kind != "asset"
                if is_liab:
                    # "credit" = somebody owes you; anything else = you owe it
                    bal = abs(bal) if at.get("liability_direction") == "credit" else -abs(bal)
                out.append({
                    "id": a.get("id"), "name": at.get("name") or "?",
                    "role": at.get("account_role") or "",
                    "kind": "liability" if is_liab else "asset",
                    "ltype": at.get("liability_type") or "",
                    "balance": round(bal, 2),
                    "currency": at.get("currency_code") or "EUR",
                    "active": bool(at.get("active", True)),
                    "net": at.get("include_net_worth", True) is not False,
                })
            if not more_pages(res, page):
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
            row = names.setdefault(a["name"], {"name": a["name"], "currency": a["currency"], "kind": a["kind"],
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


def btc_sync(cfg):
    has_kraken = bool(cfg.get("kraken_key") and cfg.get("kraken_secret"))
    if not (has_kraken or cfg.get("wallets") or cfg.get("strike_key")):
        return
    with sync_lock:
        if sync_state["running"]:
            return
        sync_state.update(running=True, error="", step="Starting", done=0, total=0)
    errors = []
    if has_kraken:
        try:
            kraken_part(cfg)
        except Exception as e:  # noqa: BLE001 - shown in the UI
            errors.append(f"Kraken: {e}")
    if cfg.get("wallets"):
        try:
            wallet_part(cfg)
        except Exception as e:  # noqa: BLE001
            errors.append(f"Wallets: {e}")
    if cfg.get("strike_key"):
        try:
            strike_part(cfg)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e) if str(e).startswith("Strike") else f"Strike: {e}")
    set_sync(step="Updating price history")
    try:
        update_prices()
    except Exception as e:  # noqa: BLE001
        errors.append(f"Prices: {e}")
    if has_kraken and cfg.get("cashback_book") and cfg.get("token"):
        set_sync(step="Booking Krak cashback in Firefly")
        try:
            book_cashback(cfg)
        except Exception as e:  # noqa: BLE001
            errors.append(f"Cashback: {e}")
        if cfg.get("btc_balance_sync", True):
            set_sync(step="Setting the BTC balance in Firefly")
            try:
                sync_btc_balance(cfg)
            except Exception as e:  # noqa: BLE001
                errors.append(f"BTC balance: {e}")
    set_sync(running=False, step="", error=" · ".join(errors), finished=int(time.time()))


def cashback_rows(cfg):
    """BTC that came into Kraken as a reward (Krak card cashback), with your own corrections
    from the Bitcoin page applied (marked as reward / not a reward)."""
    tx, _ = classify(read_json(KRAKEN_FILE, {}))
    prefs = read_json(DATA_DIR / "store_prefs.json", None) or {}
    as_r, not_r = set(prefs.get("asReward") or []), set(prefs.get("notReward") or [])
    out = []
    for r in tx:
        if r["ref"] in not_r:
            continue
        if r["type"] == "Reward" or (r["ref"] in as_r and r["btc"] > 0):
            out.append(r)
    return out


def sync_btc_balance(cfg):
    """Keep the chosen BTC account in Firefly at your real Kraken BTC balance. Only its opening
    balance moves, so nothing shows up as income or spending; the cashback bookings stay as they are."""
    acc = (cfg.get("cashback_account") or "").strip()
    st = read_json(KRAKEN_FILE, {})
    bal = st.get("balance")
    if not acc or not bal:
        return None
    target = round(sum(float(v) for k, v in bal.items() if is_btc(k)), 8)
    return set_firefly_btc(cfg, acc, target, cfg.get("cashback_from") or f"{date.today().year}-01-01")


def set_firefly_btc(cfg, acc, target, since):
    """Set a BTC asset account in Firefly to `target` BTC by moving its opening balance."""
    a, page = None, 1
    while a is None:
        res = firefly_get(cfg, "/api/v1/accounts", {"type": "asset", "page": page, "limit": 200})
        a = next((x for x in res.get("data", []) if (x.get("attributes") or {}).get("name") == acc), None)
        if a is None and not more_pages(res, page):
            raise RuntimeError(f"there is no asset account “{acc}” in Firefly")
        page += 1
    at = a["attributes"]
    if at.get("currency_code") != "BTC":
        return None                                          # only a BTC account can hold the sats
    cur = float(at.get("current_balance") or 0)
    diff = round(target - cur, 8)
    if abs(diff) < 1e-8:
        return 0.0
    opening = float(at.get("opening_balance") or 0) + diff
    odate = str(at.get("opening_balance_date") or "")[:10] or (date.fromisoformat(since) - timedelta(days=1)).isoformat()
    firefly_put(cfg, f"/api/v1/accounts/{a['id']}", {"name": acc, "opening_balance": f"{opening:.8f}", "opening_balance_date": odate})
    return diff


def strike_get(key, path):
    return http_json(f"{STRIKE_API}/{path}", headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}, timeout=30)


def strike_btc(balances):
    """BTC held on Strike from GET /balances (a list with one entry per currency)."""
    for b in balances if isinstance(balances, list) else []:
        if str(b.get("currency", "")).upper() == "BTC":
            # "current"/"total" leave out BTC parked in Stacks, so those are added
            main = max(float(b.get("current") or 0), float(b.get("total") or 0))
            return round(main + sum(float(x.get("amount") or 0) for x in (b.get("stacks") or [])), 8)
    return 0.0


def strike_part(cfg):
    set_sync(step="Reading Strike balance")
    st = read_json(STRIKE_FILE, {})
    try:
        bal = strike_get(cfg["strike_key"], "balances")
        st.update(btc=strike_btc(bal), at=int(time.time()), error="", raw=bal)
    except urllib.error.HTTPError as e:
        st.update(error=f"Strike {e.code}: {e.read().decode(errors='replace')[:200]}")
    except Exception as e:  # noqa: BLE001
        st.update(error=f"Strike: {e}")
    write_json(STRIKE_FILE, st)
    if st.get("error"):
        raise RuntimeError(st["error"])
    acc = (cfg.get("strike_account") or "").strip()
    if acc and cfg.get("token") and st.get("at"):
        set_sync(step="Setting the Strike balance in Firefly")
        set_firefly_btc(cfg, acc, st["btc"], f"{date.today().year}-01-01")


def book_cashback(cfg):
    """Each cashback transfer once as a Firefly deposit: euro value of the day, BTC in the note,
    external id kraken-<refid> so nothing is ever booked twice."""
    acc = (cfg.get("cashback_account") or "").strip()
    if not acc:
        raise RuntimeError("choose the Firefly account for the cashback in Settings")
    since = cfg.get("cashback_from") or f"{date.today().year}-01-01"
    # an account kept in BTC gets the sats as the amount and the euro value as foreign amount;
    # a euro account gets the euro value
    acc_cur = next((a["currency"] for a in fetch_accounts(cfg) if a["name"] == acc), None)
    if acc_cur is None:
        raise RuntimeError(f"there is no asset account “{acc}” in Firefly")
    days = read_json(PRICES_FILE, {"days": {}}).get("days", {})
    done = read_json(CASHBACK_FILE, {})
    booked = 0
    for r in cashback_rows(cfg):
        d = datetime.fromtimestamp(r["t"], timezone.utc).date().isoformat()
        if d < since or r["ref"] in done:
            continue
        px = days.get(d) or days.get((date.fromisoformat(d) - timedelta(days=1)).isoformat())
        if not px:
            continue                                         # price of that day not known yet: next sync
        ext = f"kraken-{r['ref']}"
        found = firefly_get(cfg, "/api/v1/search/transactions", {"query": f'external_id_is:"{ext}"', "limit": 5}).get("data", [])
        if found:
            done[r["ref"]] = found[0].get("id")
            write_json(CASHBACK_FILE, done)
            continue
        eur = round(r["btc"] * float(px), 2)
        if eur < 0.01:
            continue
        t = {"type": "deposit", "date": datetime.fromtimestamp(r["t"], timezone.utc).isoformat(), "amount": f"{eur:.2f}",
             "description": "Krak cashback", "source_name": "Krak Cashback", "destination_name": acc,
             "external_id": ext, "notes": f"{r['btc']:.8f} BTC ({round(r['btc'] * 1e8)} sats) · Kraken ledger {r['ref']} · "
                                          f"valued at the BTC/EUR close of {d} ({float(px):,.0f} €)"}
        # category and tags: your own Firefly rules (apply_rules) decide
        if acc_cur == "BTC":
            t.update(amount=f"{r['btc']:.8f}", currency_code="BTC", foreign_amount=f"{eur:.2f}", foreign_currency_code="EUR")
        elif acc_cur != "EUR":
            raise RuntimeError(f"the account “{acc}” is kept in {acc_cur}; choose a euro or a BTC account")
        try:
            res = firefly_post(cfg, "/api/v1/transactions", {"error_if_duplicate_hash": True, "apply_rules": True, "transactions": [t]})
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            if "uplicate" in msg:
                done[r["ref"]] = "duplicate"
                write_json(CASHBACK_FILE, done)
                continue
            raise RuntimeError(f"Firefly {e.code}: {msg}")
        done[r["ref"]] = (res.get("data") or {}).get("id")
        write_json(CASHBACK_FILE, done)
        booked += 1
    if booked:
        with cache_lock:
            for k in [k for k in cache if k[0] == "rows"]:
                del cache[k]
    return booked


def wallet_part(cfg):
    base = btcwallet.find_backend([cfg.get("mempool_url")] + MEMPOOL_DEFAULTS)
    store = read_json(WALLETS_FILE, {})
    keep = {}
    for w in cfg.get("wallets", []):
        old = store.get(w["id"], {})
        rec = {"name": w["name"], "cache": old.get("cache", {}), "result": old.get("result"), "error": "", "at": old.get("at", 0)}
        try:
            spec = btcwallet.parse_wallet(w["desc"], w.get("type", ""))
            if old.get("desc_hash") != hashlib.sha256(w["desc"].encode()).hexdigest():
                rec["cache"] = {}
            rec["desc_hash"] = hashlib.sha256(w["desc"].encode()).hexdigest()
            prog = lambda c, i, n=w["name"]: set_sync(step=f"Scanning wallet {n} ({'change' if c else 'receive'} address {i})")  # noqa: E731
            rec["result"] = btcwallet.scan(spec, base, rec["cache"], progress=prog)
            rec["at"] = int(time.time())
        except Exception as e:  # noqa: BLE001
            rec["error"] = str(e)
        keep[w["id"]] = rec
        write_json(WALLETS_FILE, {**store, **keep})
    write_json(WALLETS_FILE, keep)
    bad = [f"{r['name']}: {r['error']}" for r in keep.values() if r["error"]]
    if bad:
        raise RuntimeError("; ".join(bad))


def kraken_part(cfg):
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



def start_sync(cfg):
    threading.Thread(target=btc_sync, args=(cfg,), daemon=True).start()


# --------------------------------------------------------------- agent ---
agent_lock = threading.Lock()
agent_state = {"running": False, "step": "", "error": ""}
llm_cache = {"t": 0, "v": None, "err": ""}


def llm_for(cfg, fresh=False):
    if not fresh and time.time() - llm_cache["t"] < 300:
        return llm_cache["v"]
    try:
        base, models = agent.find_ollama(cfg.get("ollama_url", ""))
        model = agent.pick_model(models, cfg.get("ollama_model", ""))
        llm_cache.update(t=time.time(), v=(base, model, models) if model else None,
                         err="" if model else "Ollama has no model yet. On the Umbrel run: sudo docker exec ollama_ollama_1 ollama pull qwen2.5:3b")
    except Exception as e:  # noqa: BLE001
        llm_cache.update(t=time.time(), v=None, err=str(e))
    return llm_cache["v"]


def agent_run(cfg, dry=False, days=None, auto=False):
    with agent_lock:
        if agent_state["running"]:
            return False
        agent_state.update(running=True, step="Starting", error="")
    st = read_json(AGENT_FILE, {})
    try:
        llm = llm_for(cfg, fresh=True)
        days = int(days or cfg.get("agent_days") or 3)
        stats, actions, cats = agent.run(
            lambda path, params: firefly_get(cfg, path, params), lambda path, body: firefly_put(cfg, path, body),
            days=days, dry=dry, min_conf=float(cfg.get("agent_min_conf") or 0.75), llm=llm[:2] if llm else None,
            progress=lambda i, n: agent_state.update(step=f"Looking at booking {i} of {n}"))
        for a in actions:
            a["dry"] = dry
        keep = [a for a in st.get("actions", []) if not (dry and a.get("dry"))]
        seen = {(a["jid"], a["action"]) for a in actions}
        keep = [a for a in keep if (a.get("jid"), a.get("action")) not in seen]
        st["actions"] = (actions + keep)[:400]
        st["categories"] = cats
        st["last"] = {"t": int(time.time()), "dry": dry, "days": days, "auto": auto, "stats": stats,
                      "model": llm[1] if llm else "", "llm_error": "" if llm else llm_cache["err"]}
        if auto:
            st["last_auto_day"] = berlin_now().date().isoformat()
        write_json(AGENT_FILE, st)
        if not dry:
            with cache_lock:
                cache.clear()
        agent_state.update(running=False, step="")
    except Exception as e:  # noqa: BLE001
        if auto:
            st["last_auto_day"] = berlin_now().date().isoformat()
            write_json(AGENT_FILE, st)
        agent_state.update(running=False, step="", error=str(e))
    return True


def fetch_bills(cfg, start, end):
    """Firefly subscriptions (API name: bills) with the dates they were expected in this range."""
    out, page = [], 1
    while True:
        res = firefly_get(cfg, "/api/v1/bills", {"start": start, "end": end, "page": page, "limit": 200})
        for b in res.get("data", []):
            a = b.get("attributes", {})
            out.append({"name": a.get("name") or "?", "active": a.get("active", True) is not False,
                        "min": float(a.get("amount_min") or 0), "max": float(a.get("amount_max") or 0),
                        "freq": a.get("repeat_freq") or "", "pay_dates": [str(x)[:10] for x in (a.get("pay_dates") or [])],
                        "paid": len(a.get("paid_dates") or []), "created": str(a.get("created_at") or "")[:10],
                        "skip": int(a.get("skip") or 0), "next": str(a.get("next_expected_match") or "")[:10],
                        "group": a.get("object_group_title") or "",
                        "date": str(a.get("date") or "")[:10], "end_date": str(a.get("end_date") or "")[:10],
                        "paid_dates": [str(x.get("date") or "")[:10] for x in (a.get("paid_dates") or []) if isinstance(x, dict)]})
        if not more_pages(res, page):
            return out
        page += 1


def news_for(cfg, month, refresh=False):
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    f = NEWS_DIR / f"{month}.json"
    now = berlin_now()
    current = month == now.strftime("%Y-%m")
    old = read_json(f, None)
    if old and (isinstance(old.get("text"), list) or "papers" not in old):
        old = None                                      # written before summaries (2.2.0) or before the front page (2.5.0)
    if old and not refresh and (not current or time.time() - old.get("t", 0) < 3600) and (old.get("text") or time.time() - old.get("t", 0) < 6 * 3600):
        return old
    y, m = map(int, month.split("-"))
    start = f"{y - 2}-{m:02d}-01"
    end = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).isoformat()
    data = fetch_rows(cfg, start, end)
    cur = max(data["currencies"], key=data["currencies"].get) if data["currencies"] else "EUR"
    try:
        bills = fetch_bills(cfg, f"{y}-{m:02d}-01", end)
    except Exception:  # noqa: BLE001 - news still works without them
        bills = None
    fx = agent.facts(data["rows"], month, cur, upto_day=now.day if current else None, bills=bills,
                     savings=[c for c in (cfg.get("savings_categories") or []) if c != "-"])
    lang = cfg.get("news_lang") or "en"
    sav = [c for c in (cfg.get("savings_categories") or []) if c != "-"]
    papers = {}
    for per in (1, 6, 12):
        p = agent.year_stories(data["rows"], month, cur, partial=current, savings=sav, period=per) or {"stories": [], "period": per}
        for s in p["stories"]:
            s.update(agent.story_text(s, lang))
        papers[str(per)] = p
    paper = papers["12"] if papers["12"]["stories"] else papers["6"]
    llm = llm_for(cfg)
    text, lines = agent.news_text(fx, lang, llm[:2] if llm else None, paper["stories"])
    out = {"month": month, "t": int(time.time()), "facts": fx, "headlines": lines, "text": text, "papers": papers, "lang": lang,
           "model": llm[1] if (llm and text) else "", "llm_error": "" if llm else llm_cache["err"], "partial": current}
    write_json(f, out)
    return out


def agent_loop():
    while True:
        try:
            cfg = load_config()
            if cfg.get("token"):
                now = berlin_now()
                st = read_json(AGENT_FILE, {})
                if cfg.get("agent_auto") and now.hour >= int(cfg.get("agent_hour") or 8) and st.get("last_auto_day") != now.date().isoformat():
                    agent_run(cfg, auto=True)
                prev = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
                got = read_json(NEWS_DIR / f"{prev}.json", None)
                if now.hour >= int(cfg.get("agent_hour") or 8) and (not got or (not got.get("text") and time.time() - got.get("t", 0) > 6 * 3600)):
                    news_for(cfg, prev, refresh=bool(got))
        except Exception as e:  # noqa: BLE001
            print("agent loop:", e, flush=True)
        time.sleep(300)


def sync_loop():
    while True:
        cfg = load_config()
        if cfg.get("kraken_key") or cfg.get("wallets") or cfg.get("strike_key"):
            btc_sync(cfg)
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


def price_series(tx, first_t=None):
    pr = read_json(PRICES_FILE, {"days": {}})
    anchors = dict(pr.get("days", {}))
    for r in tx:
        if r["type"] in ("Buy", "Sell") and r["btc"] > 0:
            d = datetime.fromtimestamp(r["t"], timezone.utc).date().isoformat()
            anchors.setdefault(d, round(r["eur"] / r["btc"]))
    if not anchors:
        return {"start": date.today().isoformat(), "px": []}
    ts = [r["t"] for r in tx[:1]] + ([first_t] if first_t else [])
    if ts:
        first = datetime.fromtimestamp(min(ts), timezone.utc).date().isoformat()
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
    wstore = read_json(WALLETS_FILE, {})
    wallets = []
    for w in cfg.get("wallets", []):
        rec = wstore.get(w["id"], {})
        res = rec.get("result") or {}
        wallets.append({"id": w["id"], "name": w["name"], "balance": res.get("balance", 0) / 1e8,
                        "txs": res.get("txs", []), "used": res.get("used", 0), "at": rec.get("at", 0),
                        "error": rec.get("error", "")})
    wts = [t["t"] for w in wallets for t in w["txs"]]
    return {
        "configured": bool(cfg.get("kraken_key") and cfg.get("kraken_secret")) or bool(wallets) or bool(cfg.get("strike_key")),
        "strike": ({k: v for k, v in read_json(STRIKE_FILE, {}).items() if k != "raw"} or {"btc": None}) if cfg.get("strike_key") else None,
        "kraken": bool(cfg.get("kraken_key") and cfg.get("kraken_secret")),
        "wallets": wallets,
        "tx": tx,
        "ledger_btc": round(onk, 8),
        "balance_btc": round(sum(float(v) for k, v in bal.items() if is_btc(k)), 8) if bal else None,
        "balance_at": st.get("balance_at", 0),
        "balance_error": st.get("balance_error", ""),
        "prices": price_series(tx, min(wts) if wts else None),
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
            wl = []
            for w in cfg.get("wallets", []):
                item = {"id": w["id"], "name": w["name"], "type": w.get("type", ""),
                        "masked": w["desc"][:12] + "…" + w["desc"][-6:]}
                try:
                    spec = btcwallet.parse_wallet(w["desc"], w.get("type", ""))
                    item.update(first=btcwallet.address(spec, spec["chains"][0], 0), kind=btcwallet.describe(spec))
                except ValueError as e:
                    item["error"] = str(e)
                wl.append(item)
            return self._json(200, {"firefly_url": cfg["firefly_url"], "token_set": bool(cfg["token"]),
                                    "wallets": wl, "mempool_url": cfg.get("mempool_url", ""),
                                    "savings_categories": cfg.get("savings_categories") or [],
                                    **{k: cfg.get(k, DEFAULTS[k]) for k in ("cashback_book", "cashback_account", "cashback_from", "btc_balance_sync")},
                                    "cashback_count": len(read_json(CASHBACK_FILE, {})),
                                    "kraken_set": bool(cfg["kraken_key"] and cfg["kraken_secret"]),
                                    "strike_set": bool(cfg.get("strike_key")), "strike_account": cfg.get("strike_account", ""),
                                    "history_start": history_start(cfg)})
        if p == "/api/data":
            def get(cfg):
                start = q.get("start", [history_start(cfg)])[0]
                end = q.get("end", [date.today().isoformat()])[0]
                key = ("rows", start, end)
                with cache_lock:
                    hit = cache.get(key)
                if hit and time.time() - hit["fetched"] < CACHE_SECONDS and "refresh" not in q:
                    return {**hit, "btc_eur": spot_price()}
                data = fetch_rows(cfg, start, end)
                with cache_lock:
                    for k in [k for k in cache if k[0] == "rows"]:
                        del cache[k]
                    cache[key] = data
                return {**data, "btc_eur": spot_price()}
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
        if p == "/api/agent":
            cfg = load_config()
            st = read_json(AGENT_FILE, {})
            llm = llm_for(cfg)
            with agent_lock:
                run_state = dict(agent_state)
            return self._json(200, {"state": run_state, "last": st.get("last"), "actions": st.get("actions", []),
                                    "categories": st.get("categories", []),
                                    "config": {k: cfg.get(k) for k in ("agent_auto", "agent_hour", "agent_days", "agent_min_conf", "ollama_url", "ollama_model", "news_lang")},
                                    "llm": {"ok": bool(llm), "base": llm[0] if llm else "", "model": llm[1] if llm else "",
                                            "models": llm[2] if llm else [], "error": "" if llm else llm_cache["err"]}})
        if p == "/api/undiscovered":
            def und(cfg):
                today = berlin_now().date()
                start = (today - timedelta(days=420)).isoformat()
                data = fetch_rows(cfg, start, today.isoformat())
                bills = [b for b in fetch_bills(cfg, start, today.isoformat()) if b["active"]]
                cur = max(data["currencies"], key=data["currencies"].get) if data["currencies"] else "EUR"
                sav = [c for c in (cfg.get("savings_categories") or []) if c != "-"]
                return {"items": agent.undiscovered(data["rows"], bills, today, cur, savings=sav), "currency": cur,
                        "firefly_subscriptions": len(bills)}
            return self._firefly(und)
        if p == "/api/subcal":
            # every active Firefly subscription with its schedule (start date, repeat, skip, end)
            # and the dates Firefly linked a payment to it in that year
            try:
                year = int((q.get("year") or [""])[0] or berlin_now().year)
            except ValueError:
                return self._json(400, {"error": "year must be a number"})

            def subcal(cfg):
                bills = fetch_bills(cfg, f"{year}-01-01", f"{year}-12-31")
                items = [{"name": b["name"], "group": b["group"], "freq": b["freq"], "skip": b["skip"],
                          "date": b["date"], "end_date": b["end_date"],
                          "amount": round((b["min"] + b["max"]) / 2, 2), "range": [b["min"], b["max"]],
                          "pay_dates": sorted(b["pay_dates"]), "paid_dates": sorted(b["paid_dates"]), "next": b["next"]}
                         for b in bills if b["active"]]
                return {"year": year, "items": items}
            return self._firefly(subcal)
        if p == "/api/news":
            now = berlin_now()
            month = (q.get("month") or [""])[0] or (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
            if month == "current":
                month = now.strftime("%Y-%m")
            if not re.fullmatch(r"\d{4}-\d{2}", month):
                return self._json(400, {"error": "month must look like 2026-10"})
            return self._firefly(lambda cfg: news_for(cfg, month, refresh="refresh" in q))
        if p == "/api/subscriptions":
            def subs(cfg):
                today = berlin_now().date()
                start = (today - timedelta(days=430)).isoformat()
                data = fetch_rows(cfg, start, today.isoformat())
                try:
                    bills = fetch_bills(cfg, today.replace(day=1).isoformat(), (today + timedelta(days=62)).isoformat())
                except Exception:  # noqa: BLE001
                    bills = []
                cur = max(data["currencies"], key=data["currencies"].get) if data["currencies"] else "EUR"
                sav = [c for c in (cfg.get("savings_categories") or []) if c != "-"]
                res = agent.subscriptions(data["rows"], bills, today, cur, savings=sav)
                # income of the last full month (salary and the like), to set fixed costs against
                last = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
                inc = [r for r in data["rows"] if r[1] == "i" and r[7] == cur and r[0] == last
                       and r[3] not in sav and not (len(r) > 11 and r[11])]
                res["income_avg"] = round(sum(r[2] for r in inc), 2)
                res["income_months"] = [last]
                res["firefly_subscriptions"] = len(bills)
                return res
            return self._firefly(subs)
        if p.startswith("/api/store/"):
            name = p.rsplit("/", 1)[-1]
            if name not in STORES:
                return self._json(404, {"error": "unknown store"})
            return self._json(200, {"value": read_json(DATA_DIR / f"store_{name}.json", None)})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path in ("/api/agent/run", "/api/agent/correct", "/api/agent/review"):
            cfg = load_config()
            body = self._body() or {}
            if not cfg.get("token"):
                return self._json(400, {"error": "No Firefly token set yet. Open Settings."})
            if self.path == "/api/agent/run":
                threading.Thread(target=agent_run, args=(cfg, bool(body.get("dry")), body.get("days")), daemon=True).start()
                return self._json(200, {"ok": True})
            st = read_json(AGENT_FILE, {})
            if self.path == "/api/agent/correct":
                try:
                    agent.correct(lambda path, params: firefly_get(cfg, path, params), lambda path, b: firefly_put(cfg, path, b),
                                  body["gid"], body["jid"], body.get("category"))
                except Exception as e:  # noqa: BLE001
                    return self._json(502, {"error": f"Firefly: {e}"})
                with cache_lock:
                    cache.clear()
            for a in st.get("actions", []):
                if str(a.get("jid")) == str(body.get("jid")):
                    a["reviewed"] = True
                    if self.path == "/api/agent/correct":
                        a["final"] = body.get("category")
            write_json(AGENT_FILE, st)
            return self._json(200, {"ok": True})
        if self.path == "/api/undiscovered/add":
            # set a found payment up as a Firefly subscription
            cfg = load_config()
            b = self._body() or {}
            if not cfg.get("token"):
                return self._json(400, {"error": "No Firefly token set yet. Open Settings."})
            try:
                mn, mx = float(b["amount_min"]), float(b["amount_max"])
                if b.get("repeat_freq") not in ("weekly", "monthly", "quarterly", "half-year", "yearly") or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", b.get("date", "")):
                    raise ValueError("bad interval or date")
                res = firefly_post(cfg, "/api/v1/bills", {"name": str(b["name"])[:255], "amount_min": f"{mn:.2f}", "amount_max": f"{max(mn, mx):.2f}",
                                                         "date": b["date"], "repeat_freq": b["repeat_freq"], "skip": 0, "active": True,
                                                         "currency_code": b.get("currency") or "EUR"})
            except urllib.error.HTTPError as e:
                return self._json(502, {"error": f"Firefly error {e.code}: {e.read().decode(errors='replace')[:300]}"})
            except (KeyError, ValueError) as e:
                return self._json(400, {"error": f"invalid request: {e}"})
            except Exception as e:  # noqa: BLE001
                return self._json(502, {"error": f"Cannot reach Firefly: {e}"})
            with cache_lock:
                cache.clear()
            return self._json(200, {"ok": True, "id": (res.get("data") or {}).get("id")})
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
            f = DATA_DIR / f"store_{name}.json"
            old, new = read_json(f, None), body.get("value")
            # an empty list never replaces several saved entries in one go (that is a failed load
            # on the page, not you deleting them one by one)
            if isinstance(old, list) and len(old) >= 2 and isinstance(new, list) and not new:
                return self._json(409, {"error": f"refusing to replace {len(old)} saved entries with an empty list – reload the page"})
            if f.exists():                                   # keep the last 30 versions in data/backups/
                bdir = DATA_DIR / "backups"
                bdir.mkdir(parents=True, exist_ok=True)
                (bdir / f"store_{name}-{time.strftime('%Y%m%d-%H%M%S')}.json").write_bytes(f.read_bytes())
                for b in sorted(bdir.glob(f"store_{name}-*.json"))[:-30]:
                    b.unlink(missing_ok=True)
            write_json(f, new)
            return self._json(200, {"ok": True})
        if self.path != "/api/config":
            return self._json(404, {"error": "not found"})
        cfg = load_config()
        # never send the stored token to a new address: a changed URL needs the token again
        if body.get("firefly_url") and body["firefly_url"].strip().rstrip("/") != cfg["firefly_url"].rstrip("/") and not body.get("token"):
            return self._json(400, {"error": "You changed the Firefly address - paste the token again to confirm."})
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
        strike_changed = False
        if body.get("strike_key"):
            sk = body["strike_key"].strip()
            try:
                strike_get(sk, "balances")
            except urllib.error.HTTPError as e:
                return self._json(400, {"error": f"Strike rejected the key ({e.code}). Give it the scope partner.balances.read."})
            except Exception as e:  # noqa: BLE001
                return self._json(400, {"error": f"Cannot reach Strike: {e}"})
            strike_changed = sk != cfg.get("strike_key")
            cfg["strike_key"] = sk
        if body.get("strike_remove"):
            cfg["strike_key"] = ""
            STRIKE_FILE.unlink(missing_ok=True)
        if "strike_account" in body:
            strike_changed = strike_changed or (body.get("strike_account") or "") != cfg.get("strike_account", "")
            cfg["strike_account"] = (body.get("strike_account") or "").strip()
        wallets_changed = False
        if isinstance(body.get("wallets"), list):
            old = {w["id"]: w for w in cfg.get("wallets", [])}
            new = []
            for i, w in enumerate(body["wallets"]):
                wid = w.get("id") or hashlib.sha256(f"{time.time()}{i}".encode()).hexdigest()[:10]
                desc = (w.get("desc") or "").strip() or old.get(wid, {}).get("desc", "")
                name = (w.get("name") or "").strip() or f"Wallet {i + 1}"
                typ = w.get("type") or ""
                try:
                    btcwallet.parse_wallet(desc, typ)
                except ValueError as e:
                    return self._json(400, {"error": f"Wallet “{name}”: {e}"})
                new.append({"id": wid, "name": name, "desc": desc, "type": typ})
            wallets_changed = [(w["id"], w["desc"], w["type"]) for w in new] != [(w["id"], w["desc"], w.get("type", "")) for w in cfg.get("wallets", [])]
            cfg["wallets"] = new
        for k, cast in (("agent_auto", bool), ("agent_hour", int), ("agent_days", int), ("agent_min_conf", float),
                        ("ollama_url", str), ("ollama_model", str), ("news_lang", str)):
            if k in body:
                try:
                    cfg[k] = cast(body[k]) if not isinstance(body[k], str) or cast is str else cast(float(body[k])) if cast is int else cast(body[k])
                except (TypeError, ValueError):
                    return self._json(400, {"error": f"bad value for {k}"})
                if k in ("ollama_url", "ollama_model"):
                    llm_cache["t"] = 0
        if isinstance(body.get("savings_categories"), list):
            cfg["savings_categories"] = [str(x) for x in body["savings_categories"]] or ["-"]
            with cache_lock:
                for k in [k for k in cache if k[0] == "news"]:
                    del cache[k]
        if "mempool_url" in body:
            cfg["mempool_url"] = (body.get("mempool_url") or "").strip()
        cashback_on = False
        if "cashback_book" in body:
            cf = (body.get("cashback_from") or "").strip()
            if cf and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cf):
                return self._json(400, {"error": "Cashback start date must look like 2026-01-01."})
            if body.get("cashback_book") and not (body.get("cashback_account") or "").strip():
                return self._json(400, {"error": "Choose the Firefly account the cashback should be booked to."})
            cashback_on = bool(body.get("cashback_book")) and not cfg.get("cashback_book")
            cfg["cashback_book"] = bool(body.get("cashback_book"))
            cfg["cashback_account"] = (body.get("cashback_account") or "").strip()
            cfg["cashback_from"] = cf
            cfg["btc_balance_sync"] = bool(body.get("btc_balance_sync", True))
        if body.get("kraken_remove"):
            cfg["kraken_key"] = cfg["kraken_secret"] = ""
        save_config(cfg)
        with cache_lock:
            cache.clear()
        if kraken_changed or wallets_changed or cashback_on or (strike_changed and cfg.get("strike_key")):
            start_sync(cfg)
        self._json(200, {"ok": True})


if __name__ == "__main__":
    print(f"Spending Trends on :{PORT}", flush=True)
    threading.Thread(target=sync_loop, daemon=True).start()
    threading.Thread(target=agent_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
