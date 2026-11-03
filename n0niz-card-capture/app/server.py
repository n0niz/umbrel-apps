"""Card Capture: turn card payment notifications into Firefly III transactions.

An automation app on the phone (MacroDroid, Tasker, ...) forwards each payment
notification to POST /api/hook?key=SECRET. The text is parsed for amount,
currency and merchant and booked into Firefly III as a withdrawal from a
dedicated asset account (default "Krak Card"). Anything that cannot be parsed
safely lands in a review list in the web interface.

Standard library only.
"""

import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_FILE = DATA_DIR / "config.json"
EVENTS_FILE = DATA_DIR / "events.json"
PORT = int(os.environ.get("PORT", "8080"))
MAX_EVENTS = 1000
DEDUP_SECONDS = 15 * 60

DEFAULTS = {
    "firefly_url": os.environ.get("FIREFLY_URL", "http://firefly-iii_server_1:8080"),
    "token": os.environ.get("FIREFLY_TOKEN", ""),
    "account": "Krak Card",
    "currency": "EUR",
    "tag": "krak-card",
    "secret": "",
    "require": "",          # optional regex the text must match to be considered
    "ignore": r"declined|abgelehnt|failed|fehlgeschlagen|top.?up|aufgeladen|deposit|einzahlung",
}

lock = threading.Lock()


# --------------------------------------------------------------- storage ---
def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    except (OSError, ValueError):
        pass
    if not cfg["secret"]:
        cfg["secret"] = secrets.token_urlsafe(18)
        save_config(cfg)
    return cfg


def save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_FILE)


def load_events():
    try:
        return json.loads(EVENTS_FILE.read_text())
    except (OSError, ValueError):
        return []


def save_events(events):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = EVENTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(events[-MAX_EVENTS:], ensure_ascii=False, indent=1))
    os.replace(tmp, EVENTS_FILE)


# --------------------------------------------------------------- parsing ---
CUR_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "₺": "TRY", "฿": "THB", "¥": "JPY", "CHF": "CHF"}
CODE = r"(EUR|USD|GBP|CHF|THB|TRY|JPY|PLN|CZK|HUF|SEK|NOK|DKK|AUD|CAD)"
NUM = r"(\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
AMOUNT_PATTERNS = [
    re.compile(r"([€$£₺฿¥])\s?" + NUM),                 # €12,34
    re.compile(NUM + r"\s?([€$£₺฿¥])"),                 # 12,34 €
    re.compile(CODE + r"\s?" + NUM, re.I),              # EUR 12.34
    re.compile(NUM + r"\s?" + CODE, re.I),              # 12.34 EUR
]
REFUND = re.compile(r"refund|erstatt|gutschrift|returned|zurückerstatt|reversal|storniert", re.I)
# note: Krak adds "…Bitcoin-Cashback erhalten" to every payment, so cashback must NOT mean refund
GENERIC_TITLES = re.compile(r"^(krak|kraken|payment|zahlung|card payment|kartenzahlung|transaction|"
                            r"transaktion|purchase|einkauf|google wallet|wallet|neue? zahlung)\b", re.I)
MERCHANT_PATTERNS = [
    re.compile(r"\b(?:at|bei|an|to|für|for|from|von|@|merchant:|händler:)\s+(.+?)(?=\s+(?:for|für|with|mit|on|am|was|wurde|via|using|um|bezahlt|gezahlt|getätigt|abgebucht|paid|spent)\b|[.!,;(]|$)", re.I),
    re.compile(r"^(.+?)\s*[:\-–]\s*$"),
]


def to_number(s):
    s = s.replace(" ", "").replace(" ", "")
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(",", ".")
    elif "," in s:
        head, tail = s.rsplit(",", 1)
        s = (head.replace(",", "") + "." + tail) if len(tail) in (1, 2) else s.replace(",", "")
    elif "." in s:
        head, tail = s.rsplit(".", 1)
        if len(tail) == 3 and head.isdigit():
            s = head + tail                      # 1.234 -> thousands
    return float(s)


def find_amount(text):
    for pat in AMOUNT_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        a, b = m.group(1), m.group(2)
        cur_raw, num = (a, b) if not re.match(r"\d", a) else (b, a)
        cur = CUR_SYMBOLS.get(cur_raw, cur_raw.upper())
        try:
            return to_number(num), cur, m.span()
        except ValueError:
            continue
    return None, None, None


def clean_merchant(s):
    s = re.sub(r"\s+", " ", s.replace('"', "")).strip(" \t-–:.,;'")
    s = re.sub(r"\b(?:successful(?:ly)?|erfolgreich|approved|genehmigt)\b", "", s, flags=re.I).strip(" -–:.,")
    return s[:80]


def parse(title, text):
    """Return dict(amount, currency, merchant, kind) or raise ValueError(reason)."""
    full = f"{title}\n{text}".strip()
    amount, currency, span = find_amount(full)
    if amount is None:
        raise ValueError("no amount found")
    if amount <= 0:
        raise ValueError("amount is zero")
    kind = "deposit" if REFUND.search(full) else "withdrawal"
    rest = (full[:span[0]] + " " + full[span[1]:]) if span else full
    merchant = ""
    for line in [l for l in rest.splitlines() if l.strip()] or [rest]:
        for pat in MERCHANT_PATTERNS:
            m = pat.search(line)
            if m and clean_merchant(m.group(1)):
                merchant = clean_merchant(m.group(1))
                break
        if merchant:
            break
    if not merchant and title.strip() and not GENERIC_TITLES.search(title.strip()):
        t_amount, _, _ = find_amount(title)
        if t_amount is None:
            merchant = clean_merchant(title)
    return {"amount": round(amount, 2), "currency": currency, "merchant": merchant or "Card payment",
            "kind": kind}


# --------------------------------------------------------------- Firefly ---
def ff(cfg, method, path, body=None, params=None):
    url = cfg["firefly_url"].rstrip("/") + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {cfg['token']}",
                                          "Accept": "application/vnd.api+json",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def ensure_account(cfg):
    res = ff(cfg, "GET", "/api/v1/accounts", params={"type": "asset", "limit": 200})
    for a in res.get("data", []):
        if a["attributes"]["name"].lower() == cfg["account"].lower():
            return "exists"
    ff(cfg, "POST", "/api/v1/accounts", {"name": cfg["account"], "type": "asset",
                                          "account_role": "defaultAsset", "currency_code": cfg["currency"]})
    return "created"


def book(cfg, ev):
    p = ev["parsed"]
    base = {
        "type": p["kind"], "date": ev["received"], "amount": f"{p['amount']:.2f}",
        "description": p["merchant"], "currency_code": cfg["currency"],
        "tags": [cfg["tag"]] if cfg["tag"] else [], "external_id": "cc-" + ev["id"],
        "notes": f"{ev.get('title', '')}\n{ev.get('text', '')}".strip()[:1000],
    }
    if p["kind"] == "withdrawal":
        base.update(source_name=cfg["account"], destination_name=p["merchant"])
    else:
        base.update(source_name=p["merchant"], destination_name=cfg["account"])
    res = ff(cfg, "POST", "/api/v1/transactions",
             {"error_if_duplicate_hash": True, "apply_rules": True, "fire_webhooks": True, "transactions": [base]})
    return res.get("data", {}).get("id")


def try_book(cfg, ev):
    try:
        ev["firefly_id"] = book(cfg, ev)
        ev["status"], ev["reason"] = "booked", ""
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code == 422 and "uplicate" in body:
            ev["status"], ev["reason"] = "duplicate", "already in Firefly"
        else:
            ev["status"], ev["reason"] = "error", f"Firefly {e.code}: {body[:300]}"
    except Exception as e:
        ev["status"], ev["reason"] = "error", f"Firefly not reachable: {e}"


# --------------------------------------------------------------- handling ---
def handle_notification(title, text, app=""):
    cfg = load_config()
    now = datetime.now(timezone.utc).astimezone()
    fingerprint = hashlib.sha256(f"{title}|{text}".encode()).hexdigest()[:16]
    ev = {"id": f"{int(time.time() * 1000)}-{fingerprint[:6]}", "received": now.isoformat(timespec="seconds"),
          "title": title[:300], "text": text[:2000], "app": app[:80], "fp": fingerprint}
    with lock:
        events = load_events()
        recent = [e for e in events[-50:] if e.get("fp") == fingerprint
                  and time.time() - datetime.fromisoformat(e["received"]).timestamp() < DEDUP_SECONDS]
        if recent:
            return {"status": "repeat", "reason": "same notification within 15 minutes, ignored"}
        full = f"{title}\n{text}"
        if cfg["require"] and not re.search(cfg["require"], full, re.I):
            ev["status"], ev["reason"] = "ignored", "does not match the required pattern"
        elif cfg["ignore"] and re.search(cfg["ignore"], full, re.I):
            ev["status"], ev["reason"] = "ignored", "matches an ignore word"
        else:
            try:
                ev["parsed"] = parse(title, text)
                if ev["parsed"]["currency"] != cfg["currency"]:
                    ev["status"], ev["reason"] = "review", f"paid in {ev['parsed']['currency']}, enter the {cfg['currency']} amount"
                elif not cfg["token"]:
                    ev["status"], ev["reason"] = "review", "no Firefly token set yet"
                else:
                    try_book(cfg, ev)
            except ValueError as e:
                # payment notifications always carry an amount; without one it is most
                # likely a news/summary/security message, so it is only logged
                ev["status"] = "ignored" if str(e) == "no amount found" else "review"
                ev["reason"] = "no amount found (probably not a payment)" if ev["status"] == "ignored" else str(e)
        events.append(ev)
        save_events(events)
    return {"status": ev["status"], "reason": ev.get("reason", ""), "parsed": ev.get("parsed")}


def extract_fields(raw, ctype, q):
    """Accept JSON, form data, query parameters or plain text from any automation app."""
    body = raw.decode(errors="replace")
    pick = lambda d, k: str((d.get(k) or [""])[0] if isinstance(d.get(k), list) else d.get(k) or "")
    if "title" in q or "text" in q:
        return pick(q, "title"), pick(q, "text"), pick(q, "app")
    try:
        d = json.loads(body)
        if isinstance(d, dict):
            return str(d.get("title", "")), str(d.get("text", "")), str(d.get("app", ""))
    except ValueError:
        pass
    if "form-urlencoded" in ctype:
        d = urllib.parse.parse_qs(body)
        return pick(d, "title"), pick(d, "text"), pick(d, "app")
    if body.lstrip().startswith("{"):
        # JSON broken by quotes inside the notification text: pull the fields out leniently
        grab = lambda k: (re.search(r'"%s"\s*:\s*"(.*?)"\s*(?:,\s*"\w+"\s*:|\}\s*$)' % k, body, re.S) or [None, ""])[1]
        return grab("title"), grab("text"), grab("app")
    return "", body, ""


# ------------------------------------------------------------------ HTTP ---
class Handler(BaseHTTPRequestHandler):
    server_version = "CardCapture/1.0"

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
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100_000:
            raise ValueError("too large")
        return self.rfile.read(n)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._send(200, (APP_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/config":
            cfg = load_config()
            out = {k: v for k, v in cfg.items() if k != "token"}
            out["token_set"] = bool(cfg["token"])
            return self._json(200, out)
        if url.path == "/api/events":
            return self._json(200, list(reversed(load_events()))[:200])
        self._json(404, {"error": "not found"})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/api/hook":
            cfg = load_config()
            key = q.get("key", [""])[0] or self.headers.get("X-Key", "")
            if not secrets.compare_digest(key, cfg["secret"]):
                return self._json(403, {"error": "wrong key"})
            try:
                raw = self._body()
            except ValueError:
                return self._json(413, {"error": "too large"})
            title, text, app = extract_fields(raw, self.headers.get("Content-Type", ""), q)
            if not (title or text).strip():
                return self._json(400, {"error": "empty notification"})
            return self._json(200, handle_notification(title, text, app))

        if url.path == "/api/test":
            data = json.loads(self._body() or b"{}")
            try:
                return self._json(200, {"ok": True, "parsed": parse(data.get("title", ""), data.get("text", ""))})
            except ValueError as e:
                return self._json(200, {"ok": False, "reason": str(e)})

        if url.path == "/api/rebook":
            data = json.loads(self._body() or b"{}")
            cfg = load_config()
            with lock:
                events = load_events()
                ev = next((e for e in events if e["id"] == data.get("id")), None)
                if not ev:
                    return self._json(404, {"error": "event not found"})
                if data.get("action") == "ignore":
                    ev["status"], ev["reason"] = "ignored", "ignored by you"
                else:
                    try:
                        amount = float(str(data["amount"]).replace(",", "."))
                    except (KeyError, ValueError):
                        return self._json(400, {"error": "invalid amount"})
                    ev["parsed"] = {"amount": round(amount, 2), "currency": cfg["currency"],
                                    "merchant": (data.get("merchant") or "Card payment").strip()[:80],
                                    "kind": "deposit" if data.get("kind") == "deposit" else "withdrawal"}
                    if not cfg["token"]:
                        return self._json(400, {"error": "Set the Firefly token in Settings first."})
                    try_book(cfg, ev)
                save_events(events)
            return self._json(200, ev)

        if url.path == "/api/secret":
            cfg = load_config()
            cfg["secret"] = secrets.token_urlsafe(18)
            save_config(cfg)
            return self._json(200, {"secret": cfg["secret"]})
        self._json(404, {"error": "not found"})

    def do_PUT(self):
        if self.path != "/api/config":
            return self._json(404, {"error": "not found"})
        try:
            data = json.loads(self._body() or b"{}")
        except ValueError:
            return self._json(400, {"error": "invalid json"})
        cfg = load_config()
        # never send the stored token to a new address: a changed URL needs the token again
        new_url = str(data.get("firefly_url", cfg["firefly_url"])).strip()
        if new_url.rstrip("/") != cfg["firefly_url"].rstrip("/") and not data.get("token"):
            return self._json(400, {"error": "You changed the Firefly address - paste the token again to confirm."})
        for k in ("firefly_url", "account", "currency", "tag", "require", "ignore"):
            if k in data:
                cfg[k] = str(data[k]).strip()
        if data.get("token"):
            cfg["token"] = data["token"].strip()
        for k in ("require", "ignore"):
            try:
                re.compile(cfg[k])
            except re.error as e:
                return self._json(400, {"error": f"{k}: invalid pattern ({e})"})
        if not cfg["account"]:
            return self._json(400, {"error": "Account name is required."})
        msg = ""
        if cfg["token"]:
            try:
                msg = f"Firefly connected, account “{cfg['account']}” {ensure_account(cfg)}."
            except urllib.error.HTTPError as e:
                return self._json(400, {"error": "Token rejected by Firefly." if e.code == 401 else f"Firefly error {e.code}"})
            except Exception as e:
                return self._json(400, {"error": f"Cannot reach Firefly: {e}"})
        save_config(cfg)
        self._json(200, {"ok": True, "message": msg})


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    load_config()
    print(f"Card Capture on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
