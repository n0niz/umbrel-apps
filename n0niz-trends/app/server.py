"""Spending Trends: monthly and year-over-year views of Firefly III data.

The server reads transactions from the Firefly III API with a personal access
token and hands the browser compact rows (month, type, amount, category,
budget, tags, payee). All grouping and charting happens in the browser.
Standard library only.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_FILE = DATA_DIR / "config.json"
PORT = int(os.environ.get("PORT", "8080"))
CACHE_SECONDS = 600

DEFAULTS = {
    "firefly_url": os.environ.get("FIREFLY_URL", "http://firefly-iii_server_1:8080"),
    "token": os.environ.get("FIREFLY_TOKEN", ""),
}

cache = {}
cache_lock = threading.Lock()


def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update({k: v for k, v in json.loads(CONFIG_FILE.read_text()).items() if v})
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg))
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_FILE)


def firefly_get(cfg, path, params):
    url = cfg["firefly_url"].rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {cfg['token']}",
                                               "Accept": "application/vnd.api+json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def fetch_rows(cfg, start, end):
    """All withdrawals and deposits between start and end as compact rows."""
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
                    payee = t.get("destination_name") if t["type"] == "withdrawal" else t.get("source_name")
                    rows.append([
                        t["date"][:7],
                        "e" if t["type"] == "withdrawal" else "i",
                        round(abs(float(t.get("amount") or 0)), 2),
                        t.get("category_name") or "",
                        t.get("budget_name") or "",
                        t.get("tags") or [],
                        payee or "",
                        cur,
                    ])
            pages = res.get("meta", {}).get("pagination", {}).get("total_pages", 1)
            if page >= pages:
                break
            page += 1
    return {"rows": rows, "currencies": currencies, "start": start, "end": end,
            "fetched": int(time.time())}


class Handler(BaseHTTPRequestHandler):
    server_version = "SpendingTrends/1.0"

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

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            return self._send(200, (APP_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/config":
            cfg = load_config()
            return self._json(200, {"firefly_url": cfg["firefly_url"], "token_set": bool(cfg["token"])})
        if url.path == "/api/data":
            cfg = load_config()
            if not cfg["token"]:
                return self._json(400, {"error": "No Firefly token set yet. Open Settings."})
            start = q.get("start", [f"{date.today().year - 2}-01-01"])[0]
            end = q.get("end", [date.today().isoformat()])[0]
            key = (start, end)
            with cache_lock:
                hit = cache.get(key)
            if hit and time.time() - hit["fetched"] < CACHE_SECONDS and "refresh" not in q:
                return self._json(200, hit)
            try:
                data = fetch_rows(cfg, start, end)
            except urllib.error.HTTPError as e:
                msg = "Firefly rejected the token (401)." if e.code == 401 else f"Firefly error {e.code}."
                return self._json(502, {"error": msg})
            except Exception as e:
                return self._json(502, {"error": f"Cannot reach Firefly at {cfg['firefly_url']}: {e}"})
            with cache_lock:
                cache.clear()
                cache[key] = data
            return self._json(200, data)
        self._json(404, {"error": "not found"})

    def do_PUT(self):
        if self.path != "/api/config":
            return self._json(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._json(400, {"error": "invalid json"})
        cfg = load_config()
        if body.get("firefly_url"):
            cfg["firefly_url"] = body["firefly_url"].strip()
        if body.get("token"):
            cfg["token"] = body["token"].strip()
        try:
            firefly_get(cfg, "/api/v1/about", {})
        except urllib.error.HTTPError as e:
            return self._json(400, {"error": "Token rejected by Firefly." if e.code == 401 else f"Firefly error {e.code}"})
        except Exception as e:
            return self._json(400, {"error": f"Cannot reach Firefly: {e}"})
        save_config(cfg)
        with cache_lock:
            cache.clear()
        self._json(200, {"ok": True})


if __name__ == "__main__":
    print(f"Spending Trends on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
