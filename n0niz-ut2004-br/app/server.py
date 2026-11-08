"""UT2004 Bombing Run dashboard for Umbrel.

Live status over the UT2004 query protocol (UDP game port + 1), settings in
/data/settings.env, restart through a flag file the server container watches,
and the classic xWebAdmin proxied under /ServerAdmin with automatic login.
"""
import base64
import json
import os
import re
import socket
import struct
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR", "/data"))
SETTINGS = DATA / "settings.env"
RESTART_FLAG = DATA / ".restart"
SERVER_HOST = os.environ.get("SERVER_HOST", "n0niz-ut2004-br_server_1")
QUERY_PORT = int(os.environ.get("QUERY_PORT", "7778"))
WEBADMIN = os.environ.get("WEBADMIN_URL", f"http://{SERVER_HOST}:8080")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
CONNECT_HOST = os.environ.get("CONNECT_HOST", "umbrel-1")
VERSION = os.environ.get("APP_VERSION", "dev")

# Settings the UI can edit: key -> (label, kind, help)
FIELDS = {
    "SERVER_NAME": ("Server name", "text", ""),
    "MAP_NAME": ("Start map", "map", "Loaded when the server starts"),
    "BOTS": ("Bots", "number", "Server fills up with bots to this many players, 0 = off"),
    "MAX_PLAYERS": ("Max players", "number", ""),
    "GOAL_SCORE": ("Goal score", "number", "Empty = UT2004 default"),
    "TIME_LIMIT": ("Time limit (min)", "number", "Empty = UT2004 default"),
    "MUTATORS": ("Mutators", "text", "Comma-separated, e.g. UTCompv18a.MutUTComp"),
    "TICKRATE": ("Tick rate", "number", "Fair-Gamers BR: 41"),
    "MAP_REPEAT_LIMIT": ("Map repeat limit", "number", ""),
    "IDLE_KICK_SECONDS": ("Idle kick (s)", "number", ""),
    "GAME_PASSWORD": ("Join password", "text", "Empty = open server"),
    "PUBLIC_LISTING": ("Public listing", "bool", "Needs a public IPv4 address"),
}


# ---------------------------------------------------------------- query ----

def _str(buf, i):
    """UT2004 query string: length byte (incl. NUL); high bit = UCS-2."""
    n = buf[i]
    i += 1
    if n & 0x80:
        n = (n & 0x7F) * 2
        raw = buf[i:i + n].decode("utf-16-le", "replace")
    else:
        raw = buf[i:i + n].decode("latin-1")
    i += n
    raw = raw.rstrip("\x00")
    raw = re.sub("\x1b...", "", raw, flags=re.S)  # colour codes
    return raw, i


def _int(buf, i):
    return struct.unpack_from("<i", buf, i)[0], i + 4


def _query(kind, timeout=1.0):
    """Send one query, collect all reply packets until a short silence."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    packets = []
    t0, first = time.monotonic(), None
    try:
        s.sendto(b"\x80\x00\x00\x00" + bytes([kind]), (SERVER_HOST, QUERY_PORT))
        while True:
            data, _ = s.recvfrom(65535)
            if len(data) >= 5 and data[4] == kind:
                packets.append(data[5:])
                if first is None:
                    first = round((time.monotonic() - t0) * 1000)
            s.settimeout(0.25)
    except (socket.timeout, OSError):
        pass
    finally:
        s.close()
    return packets, first


def query_status():
    info_p, ping = _query(0)
    if not info_p:
        return {"online": False}
    b, i = info_p[0], 0
    info = {}
    _, i = _int(b, i)                      # server id
    _, i = _str(b, i)                      # ip
    info["port"], i = _int(b, i)
    _, i = _int(b, i)                      # query port
    info["name"], i = _str(b, i)
    info["map"], i = _str(b, i)
    info["gametype"], i = _str(b, i)
    info["players"], i = _int(b, i)
    info["max_players"], i = _int(b, i)

    players = []
    for b in _query(2)[0]:
        i = 0
        while i + 4 <= len(b):
            try:
                pid, i = _int(b, i)
                name, i = _str(b, i)
                pping, i = _int(b, i)
                score, i = _int(b, i)
                stats, i = _int(b, i)
            except (IndexError, struct.error):
                break
            team = "red" if stats & 0x20000000 else "blue" if stats & 0x40000000 else None
            players.append({"id": pid, "name": name, "ping": pping, "score": score,
                            "team": team, "bot": pping == 0})

    rules = {}
    mutators = []
    for b in _query(1)[0]:
        i = 0
        while i < len(b):
            try:
                k, i = _str(b, i)
                v, i = _str(b, i)
            except IndexError:
                break
            if k.lower() == "mutator":
                mutators.append(v)
            else:
                rules[k] = v

    players.sort(key=lambda p: -p["score"])
    return {"online": True, "ping": ping, **info, "player_list": players,
            "rules": rules, "mutators": mutators}


# ------------------------------------------------------------- settings ----

def read_settings():
    vals = {}
    if SETTINGS.exists():
        for line in SETTINGS.read_text().splitlines():
            m = re.match(r"\s*([A-Z_][A-Z0-9_]*)=(.*)$", line)
            if m:
                v = m.group(2).strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                vals[m.group(1)] = v
    return vals


def write_settings(new):
    lines = ["# UT2004 Bombing Run settings, written by the dashboard.",
             "# Restart the server to apply. Values with spaces need quotes."]
    for key in FIELDS:
        v = str(new.get(key, "")).strip()
        if v == "":
            continue
        if "\n" in v or '"' in v or "$" in v or "`" in v or "\\" in v:
            raise ValueError(f"{FIELDS[key][0]}: characters \" $ ` \\ are not allowed")
        if key == "SERVER_NAME" and ("/" in v or "&" in v):
            raise ValueError("Server name: / and & are not allowed")
        lines.append(f'{key}="{v}"')
    # keep unknown keys the user added by hand
    for key, v in read_settings().items():
        if key not in FIELDS:
            lines.append(f'{key}="{v}"')
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(SETTINGS)


def defaults():
    return {k: os.environ.get(k, "") for k in FIELDS}


def list_maps():
    maps = set()
    for d in (DATA / "server" / "Maps", DATA / "addons" / "Maps"):
        if d.is_dir():
            for f in d.iterdir():
                if f.suffix.lower() == ".ut2" and f.stem.upper().startswith("BR-"):
                    maps.add(f.stem)
    return sorted(maps, key=str.lower)


# ----------------------------------------------------------------- http ----

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/status":
            return self._send(200, {**query_status(),
                                    "restarting": RESTART_FLAG.exists(),
                                    "connect": f"{CONNECT_HOST}:7777", "version": VERSION})
        if path == "/api/settings":
            return self._send(200, {"values": read_settings(), "defaults": defaults(),
                                    "fields": FIELDS, "maps": list_maps()})
        return self._proxy()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/settings":
            try:
                req = json.loads(self._body() or b"{}")
                write_settings(req.get("values", {}))
                if req.get("restart"):
                    RESTART_FLAG.touch()
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"ok": True})
        if path == "/api/restart":
            RESTART_FLAG.touch()
            return self._send(200, {"ok": True})
        return self._proxy()

    def _proxy(self):
        """Classic xWebAdmin, logged in automatically (Umbrel login sits in front)."""
        if not (self.path.startswith("/ServerAdmin") or self.path.startswith("/images")):
            return self._send(404, {"error": "not found"})
        body = self._body() if self.command == "POST" else None
        req = urllib.request.Request(WEBADMIN + self.path, data=body, method=self.command)
        tok = base64.b64encode(f"{ADMIN_USER}:{ADMIN_PASSWORD}".encode()).decode()
        req.add_header("Authorization", "Basic " + tok)
        if self.headers.get("Content-Type"):
            req.add_header("Content-Type", self.headers["Content-Type"])
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                code, ctype, data = r.status, r.headers.get("Content-Type", "text/html"), r.read()
        except urllib.error.HTTPError as e:
            code, ctype, data = e.code, e.headers.get("Content-Type", "text/html"), e.read()
        except OSError:
            return self._send(502, "<p>Classic web admin is not reachable (server starting?).</p>",
                              "text/html")
        self._send(code, data, ctype)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"UT2004 BR dashboard {VERSION} on :{port}, querying {SERVER_HOST}:{QUERY_PORT}",
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
