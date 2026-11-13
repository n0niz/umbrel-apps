"""Doc Sorter: setup page + gateway in front of the sorting engine.

/_setup            setup page (Paperless login, model, review mode)
/_setup/api/...    JSON endpoints used by the setup page
everything else    proxied to the engine (review UI) once it is configured
"""
import json
import os
import shlex
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = os.environ.get("DATA_DIR", "/data")
CFG_DIR = os.path.join(DATA, "cfg")
CFG = os.path.join(DATA, "config.json")
ENV = os.path.join(CFG_DIR, "engine.env")
PAPERLESS = os.environ.get("PAPERLESS_URL", "http://paperless_webserver_1:8000").rstrip("/")
OLLAMA = os.environ.get("OLLAMA_URL", "http://ollama_ollama_1:11434").rstrip("/")
ENGINE_HOST = os.environ.get("ENGINE_HOST", "n0niz-doc-sorter_engine_1")
ENGINE_PORT = int(os.environ.get("ENGINE_PORT", "8080"))
PAPERLESS_PORT = os.environ.get("PAPERLESS_PUBLIC_PORT", "2349")
PL_HOST = "localhost"
# the engine reaches Paperless through this relay, which fixes the Host header
RELAY_PORT = int(os.environ.get("RELAY_PORT", "8001"))
RELAY_URL = os.environ.get("RELAY_URL", "http://n0niz-doc-sorter_web_1:%d" % RELAY_PORT)
HERE = os.path.dirname(os.path.abspath(__file__))

TAGS = {
    "review": "sortieren",           # documents waiting in the review queue
    "auto": "sortieren-auto",        # documents sorted without asking
    "done": "sortiert",              # set after automatic sorting
    "fail": "sortieren-fehler",      # could not be processed
    "ocr": "ki-ocr",                 # re-read the scan with a vision model
    "ocr_done": "ki-ocr-fertig",
}
WORKFLOW_NAME = "Doc Sorter: neue Dokumente"
THINKING = ("qwen3", "deepseek-r1", "qwq", "gpt-oss", "magistral")

PULL = {"model": "", "status": "", "done": 0, "total": 0, "error": "", "running": False}


def load_cfg():
    try:
        with open(CFG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cfg(c):
    os.makedirs(DATA, exist_ok=True)
    tmp = CFG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f, indent=1)
    os.replace(tmp, CFG)


def http_json(url, data=None, headers=None, method=None, timeout=20, form=False):
    h = dict(headers or {})
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
    h.setdefault("Accept", "application/json")
    if url.startswith(PAPERLESS):
        h["Host"] = PL_HOST  # Django rejects host names with "_" (paperless_webserver_1)
        if h["Accept"] == "application/json" and "/api/" in url:
            h["Accept"] = "application/json; version=%d" % api_version()
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


API_VER = {"v": 0, "t": 0}


def api_version():
    """Newest REST API version this Paperless accepts (3.x refuses requests without one)."""
    if API_VER["v"] and time.time() - API_VER["t"] < 3600:
        return API_VER["v"]
    for v in range(12, 4, -1):
        req = urllib.request.Request(PAPERLESS + "/api/", headers={"Host": PL_HOST, "Accept": "application/json; version=%d" % v})
        try:
            urllib.request.urlopen(req, timeout=6).read()
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception:
            return API_VER["v"] or 9
        if code != 406:
            API_VER.update(v=v, t=time.time())
            return v
    return 9


def pl(path, token, data=None, method=None):
    return http_json(PAPERLESS + path, data, {"Authorization": "Token " + token}, method)


def pl_all(path, token):
    out, url = [], PAPERLESS + path + ("&" if "?" in path else "?") + "page_size=100"
    while url:
        d = http_json(url, None, {"Authorization": "Token " + token})
        out += d.get("results", [])
        nxt = d.get("next")
        url = nxt.replace(nxt.split("/api/")[0], PAPERLESS) if nxt else None
    return out


def err_text(e):
    if isinstance(e, urllib.error.HTTPError):
        try:
            import re
            txt = re.sub(r"<[^>]+>", " ", e.read().decode(errors="replace"))
            return f"HTTP {e.code}: " + " ".join(txt.split())[:160]
        except Exception:
            return f"HTTP {e.code}"
    return str(getattr(e, "reason", e))


def ollama_models():
    d = http_json(OLLAMA + "/api/tags", timeout=8)
    return sorted(m["name"] for m in d.get("models", []))


def ensure_tags(token):
    have = {t["name"].lower(): t["id"] for t in pl_all("/api/tags/", token)}
    ids = {}
    for key, name in TAGS.items():
        if name.lower() not in have:
            t = pl("/api/tags/", token, {"name": name, "matching_algorithm": 0, "is_inbox_tag": False})
            have[name.lower()] = t["id"]
        ids[key] = have[name.lower()]
    return ids


def ensure_workflow(token, mode, tag_ids):
    """Paperless workflow: every newly added document gets the review (or auto) tag."""
    wfs = pl_all("/api/workflows/", token)
    mine = [w for w in wfs if w.get("name") == WORKFLOW_NAME]
    if mode == "off":
        for w in mine:
            pl(f"/api/workflows/{w['id']}/", token, {"enabled": False}, "PATCH")
        return "Workflow switched off."
    tag = tag_ids["review"] if mode == "review" else tag_ids["auto"]
    body = {
        "name": WORKFLOW_NAME,
        "order": 90,
        "enabled": True,
        "triggers": [{"type": 2, "sources": [1, 2, 3], "filter_filename": None}],
        "actions": [{"type": 1, "assign_tags": [tag]}],
    }
    for w in mine:
        pl(f"/api/workflows/{w['id']}/", token, None, "DELETE")
    try:  # 4 = upload in the web interface (newer Paperless); older versions only know 1-3
        pl("/api/workflows/", token, dict(body, triggers=[dict(body["triggers"][0], sources=[1, 2, 3, 4])]))
    except urllib.error.HTTPError:
        pl("/api/workflows/", token, body)
    return "Workflow set: new documents get the tag '%s'." % (TAGS["review"] if mode == "review" else TAGS["auto"])


def q(v):
    return shlex.quote(str(v))


def write_env(c, public_url):
    model = c["model"]
    think = c.get("think", "auto")
    if think == "auto":
        think = "false" if model.split(":")[0].lower().startswith(THINKING) else ""
    env = {
        "PAPERLESS_BASE_URL": RELAY_URL,
        "PAPERLESS_API_TOKEN": c["token"],
        "PAPERLESS_PUBLIC_URL": public_url,
        "LLM_PROVIDER": "ollama",
        "LLM_MODEL": model,
        "OLLAMA_HOST": OLLAMA,
        "OLLAMA_TIMEOUT_SECONDS": str(c.get("timeout", 600)),
        "LLM_LANGUAGE": c.get("language", "German"),
        "LLM_TEMPERATURE": "0",
        "LLM_REQUESTS_PER_MINUTE": "30",
        "CREATE_NEW_TAGS": "true" if c.get("new_tags") else "false",
        "MANUAL_TAG": TAGS["review"],
        "AUTO_TAG": TAGS["auto"],
        "AUTO_TAG_COMPLETE": TAGS["done"],
        "FAIL_TAG": TAGS["fail"],
        "AUTO_OCR_TAG": TAGS["ocr"],
        "PDF_OCR_COMPLETE_TAG": TAGS["ocr_done"],
        "AUTO_GENERATE_CUSTOM_FIELDS": "false",
        "LOG_LEVEL": "info",
    }
    if think:
        env["OLLAMA_THINK"] = think
    if c.get("ctx"):
        env["OLLAMA_CONTEXT_LENGTH"] = str(c["ctx"])
    if c.get("vision"):
        env.update({"OCR_PROVIDER": "llm", "VISION_LLM_PROVIDER": "ollama", "VISION_LLM_MODEL": c["vision"],
                    "OCR_PROCESS_MODE": "image", "PDF_SKIP_EXISTING_OCR": "false"})
    os.makedirs(CFG_DIR, exist_ok=True)
    tmp = ENV + ".tmp"
    with open(tmp, "w") as f:
        for k, v in env.items():
            f.write(f"{k}={q(v)}\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, ENV)


def pull_worker(model):
    PULL.update(model=model, status="starting", done=0, total=0, error="", running=True)
    try:
        req = urllib.request.Request(OLLAMA + "/api/pull", data=json.dumps({"model": model, "stream": True}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3600) as r:
            for line in r:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("error"):
                    PULL["error"] = d["error"]
                    break
                PULL["status"] = d.get("status", "")
                if d.get("total"):
                    PULL["total"], PULL["done"] = d["total"], d.get("completed", 0)
    except Exception as e:
        PULL["error"] = err_text(e)
    PULL["running"] = False
    if not PULL["error"]:
        PULL["status"] = "success"


def engine_up():
    try:
        c = http.client.HTTPConnection(ENGINE_HOST, ENGINE_PORT, timeout=3)
        c.request("GET", "/api/version")
        ok = c.getresponse().status < 500
        c.close()
        return ok
    except Exception:
        return False


def status():
    c = load_cfg()
    try:
        ver = open(os.path.join(HERE, "VERSION")).read().strip()
    except OSError:
        ver = "?"
    s = {"version": ver, "configured": bool(c.get("token") and c.get("model")), "user": c.get("user", ""), "model": c.get("model", ""),
         "vision": c.get("vision", ""), "mode": c.get("mode", "review"), "language": c.get("language", "German"),
         "think": c.get("think", "auto"), "ctx": c.get("ctx", ""), "new_tags": bool(c.get("new_tags")), "tags": TAGS}
    try:
        http_json(PAPERLESS + "/api/", timeout=6)
        s["paperless"] = "ok"
    except urllib.error.HTTPError as e:
        s["paperless"] = "ok" if e.code in (401, 403, 406) else err_text(e)
    except Exception as e:
        s["paperless"] = "not reachable – is the Paperless app installed and running? (" + err_text(e) + ")"
    if c.get("token"):
        try:
            pl("/api/tags/?page_size=1", c["token"])
            s["token"] = "ok"
        except Exception as e:
            s["token"] = err_text(e)
    try:
        s["models"] = ollama_models()
        s["ollama"] = "ok"
    except Exception as e:
        s["models"] = []
        s["ollama"] = "not reachable – is the Ollama app installed and running? (" + err_text(e) + ")"
    s["engine"] = engine_up() if s["configured"] else False
    s["pull"] = PULL
    return s


def do_save(body, host):
    c = load_cfg()
    token = (body.get("token") or "").strip()
    user = (body.get("user") or "").strip()
    pw = body.get("password") or ""
    if user and pw:
        try:
            token = http_json(PAPERLESS + "/api/token/", {"username": user, "password": pw}, form=True)["token"]
        except Exception as e:
            return 400, {"error": "Paperless login failed: " + err_text(e)}
        c["user"] = user
    if token:
        c["token"] = token
    if not c.get("token"):
        return 400, {"error": "Log in with your Paperless user, or paste an API token."}
    try:
        pl("/api/tags/?page_size=1", c["token"])
    except Exception as e:
        return 400, {"error": "Paperless does not accept the token: " + err_text(e)}
    model = (body.get("model") or c.get("model") or "").strip()
    if not model:
        return 400, {"error": "Choose a model."}
    c.update(model=model, vision=(body.get("vision") or "").strip(), mode=body.get("mode") or "review",
             language=(body.get("language") or "German").strip(), think=body.get("think") or "auto",
             ctx=int(body["ctx"]) if str(body.get("ctx") or "").isdigit() else "", new_tags=bool(body.get("new_tags")))
    msgs = []
    try:
        ids = ensure_tags(c["token"])
        msgs.append("Tags ready in Paperless.")
        try:
            msgs.append(ensure_workflow(c["token"], c["mode"], ids))
        except Exception as e:
            msgs.append("Could not set the Paperless workflow (%s) – create it by hand, see below." % err_text(e))
    except Exception as e:
        msgs.append("Could not create the tags (%s)." % err_text(e))
    save_cfg(c)
    hostname = (host or "umbrel.local").split(":")[0]
    write_env(c, f"http://{hostname}:{PAPERLESS_PORT}")
    msgs.append("Saved. The sorter restarts – give it 10 seconds.")
    return 200, {"ok": True, "messages": msgs}


HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "content-length", "host"}

SETUP_LINK = (b'<a href="/_setup" title="Doc Sorter setup" style="position:fixed;right:14px;bottom:14px;z-index:99999;'
              b'background:#1f2937;color:#fff;font:600 13px system-ui;padding:8px 12px;border-radius:999px;'
              b'text-decoration:none;box-shadow:0 2px 8px rgba(0,0,0,.3)">&#9881; Setup</a>')

WAIT_PAGE = """<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=5>
<title>Doc Sorter</title><body style="font:16px system-ui;background:#0f1115;color:#e5e7eb;display:grid;place-items:center;height:90vh">
<div style="text-align:center"><h2>Doc Sorter is starting…</h2><p>This page reloads by itself.</p>
<p style="color:#9ca3af">If it stays here for more than a minute, check the <a style="color:#60a5fa" href="/_setup">setup</a>.</p></div>"""


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        if not isinstance(body, (bytes, str)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def route(self):
        path = self.path.split("?")[0]
        if path == "/_setup" or path == "/_setup/":
            with open(os.path.join(HERE, "setup.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if path == "/_setup/api/status":
            return self.send(200, status())
        if path == "/_setup/api/save" and self.command == "POST":
            code, res = do_save(json.loads(self.body() or b"{}"), self.headers.get("Host"))
            return self.send(code, res)
        if path == "/_setup/api/pull" and self.command == "POST":
            m = (json.loads(self.body() or b"{}").get("model") or "").strip()
            if not m:
                return self.send(400, {"error": "no model"})
            if PULL["running"]:
                return self.send(409, {"error": "a download is already running"})
            threading.Thread(target=pull_worker, args=(m,), daemon=True).start()
            return self.send(200, {"ok": True})
        if path.startswith("/_setup"):
            return self.send(404, {"error": "not found"})
        if not load_cfg().get("model") or not os.path.exists(ENV):
            self.send_response(302)
            self.send_header("Location", "/_setup")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self.proxy()

    def proxy(self, host=None, port=None, host_header=None, rewrite=None):
        data = self.body()
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in HOP and k.lower() != "accept-encoding"}
        if host_header:
            hdrs["Host"] = host_header
            acc = next((k for k in hdrs if k.lower() == "accept"), None)
            if self.path.startswith("/api/") and (not acc or "version=" not in hdrs[acc]):
                if acc:
                    del hdrs[acc]
                hdrs["Accept"] = "application/json; version=%d" % api_version()
        try:
            conn = http.client.HTTPConnection(host or ENGINE_HOST, port or ENGINE_PORT, timeout=900)
            conn.request(self.command, self.path, body=data or None, headers=hdrs)
            r = conn.getresponse()
        except Exception:
            if self.command == "GET" and "text/html" in (self.headers.get("Accept") or ""):
                return self.send(503, WAIT_PAGE, "text/html; charset=utf-8")
            return self.send(503, {"error": "sorter engine is starting"})
        ctype = r.getheader("Content-Type") or ""
        self.send_response(r.status, r.reason)
        for k, v in r.getheaders():
            if k.lower() not in HOP:
                if rewrite and k.lower() == "location":
                    v = v.replace(rewrite[0].decode(), rewrite[1].decode())
                self.send_header(k, v)
        if rewrite and "json" in ctype:
            body = r.read().replace(rewrite[0], rewrite[1])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif rewrite is None and ctype.startswith("text/html") and self.command == "GET":
            body = r.read()
            i = body.lower().rfind(b"</body>")
            body = body[:i] + SETUP_LINK + body[i:] if i >= 0 else body + SETUP_LINK
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            n = r.getheader("Content-Length")
            if n:
                self.send_header("Content-Length", n)
            self.end_headers()
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        conn.close()

    def handle_one(self):
        try:
            self.route()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.send(500, {"error": str(e)})
            except Exception:
                pass

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = handle_one


class Relay(H):
    """Engine -> Paperless, with a Host header Django accepts; absolute links point back here."""

    def route(self):
        u = urllib.parse.urlsplit(PAPERLESS)
        return self.proxy(u.hostname, u.port or 80, PL_HOST,
                          (b"http://" + PL_HOST.encode() + b"/", RELAY_URL.encode() + b"/"))


def main():
    os.makedirs(CFG_DIR, exist_ok=True)
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", RELAY_PORT), Relay).serve_forever, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    print(f"Doc Sorter gateway on :{port}, engine {ENGINE_HOST}:{ENGINE_PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


if __name__ == "__main__":
    main()
