"""Folder sync: a shared Windows folder mirrored one way into Paperless.

- New file: uploaded to Paperless with the tag "windows" and its Windows sub-folders as Doc Sorter
  folders (Wohnen\\Miete -> Wohnen > Miete). Paperless's own workflow then lets the sorter fill in
  title, type, sender and date.
- Changed file: uploaded as a new version of the same Paperless document, so tags, folders,
  connections, notes and saved amounts stay.
- Renamed or moved file: title and folder follow (recognised by its content, nothing is re-uploaded).
- Deleted file: stays in Paperless as a backup and gets the tag "windows-entfernt".
- Nothing is ever written to the Windows folder. A document you delete in Paperless is not
  uploaded again.

The share is read with smbprotocol (pure Python), installed into the data folder on first use.
"""
import hashlib
import importlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import server as S

STATE_FILE = os.path.join(S.DATA, "foldersync.json")
PYLIB = os.path.join(S.DATA, "pylib")
if PYLIB not in sys.path:
    sys.path.insert(0, PYLIB)

TAG_SYNC, TAG_GONE = "windows", "windows-entfernt"
SYS_TAGS = {TAG_SYNC, TAG_GONE}
EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".gif", ".txt"}
SKIP = re.compile(r"^(~\$|\.|thumbs\.db$|desktop\.ini$|\$recycle\.bin$|system volume information$)", re.I)
MAX_BYTES = 150 * 1024 * 1024
PER_RUN = 25          # uploads per run – a big first import goes in steps, Paperless keeps up
STABLE_AFTER = 120    # a file untouched this long counts as finished; newer ones must look the same twice

LOCK = threading.Lock()
WAKE = threading.Event()
RUN = {"busy": False, "phase": "", "last": 0, "error": "", "warn": "", "listed": 0, "more": False, "installing": False}


def L():
    import library
    return library


def conf():
    return S.load_cfg().get("sync") or {}


# ---------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
            st.setdefault("files", {})
            st.setdefault("seen", {})
            return st
    except Exception:
        return {"files": {}, "seen": {}, "src": ""}


ST = load_state()


def save_state():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ST, f)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- sources
def parse_unc(s):
    s = (s or "").strip().replace("/", "\\")
    m = re.match(r"^\\\\([^\\]+)\\([^\\]+)(?:\\(.*))?$", s)
    if not m:
        raise ValueError("Write the folder like \\\\PC-NAME\\Share (or \\\\192.168.178.20\\Share\\Subfolder)")
    return m.group(1), m.group(2), (m.group(3) or "").strip("\\")


def smb_module():
    try:
        import smbclient
        return smbclient
    except ImportError:
        pass
    RUN["installing"] = True
    try:
        os.makedirs(PYLIB, exist_ok=True)
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir",
                            "--target", PYLIB, "smbprotocol>=1.13"], capture_output=True, text=True, timeout=900)
        if r.returncode:
            raise RuntimeError("Could not install SMB support: " + (r.stderr or r.stdout).strip()[-300:])
        importlib.invalidate_caches()
        import smbclient
        return smbclient
    finally:
        RUN["installing"] = False


class Smb:
    def __init__(self, c):
        self.host, self.share, self.sub = parse_unc(c.get("unc"))
        self.base = f"\\\\{self.host}\\{self.share}" + (f"\\{self.sub}" if self.sub else "")
        self.cli = smb_module()
        self.cli.register_session(self.host, username=c.get("user") or None, password=c.get("pass") or None,
                                  port=int(c.get("port") or 445), connection_timeout=15)
        self.label = self.base

    def walk(self):
        out = {}

        def rec(path, rel, depth):
            for e in self.cli.scandir(path):
                n = e.name
                if n in (".", "..") or SKIP.match(n):
                    continue
                if e.is_dir():
                    if depth < 12:
                        rec(path + "\\" + n, rel + n + "/", depth + 1)
                elif e.is_file():
                    st = e.stat()
                    out[rel + n] = [int(st.st_size), int(st.st_mtime)]
        rec(self.base, "", 0)
        return out

    def read(self, rel):
        with self.cli.open_file(self.base + "\\" + rel.replace("/", "\\"), mode="rb") as f:
            return f.read()


class Local:
    """A folder on the Umbrel itself (also used by the tests)."""

    def __init__(self, c):
        self.root = c["local"]
        if not os.path.isdir(self.root):
            raise FileNotFoundError(self.root)
        self.label = self.root

    def walk(self):
        out = {}
        for d, dirs, files in os.walk(self.root):
            dirs[:] = [x for x in dirs if not SKIP.match(x)]
            for n in files:
                if SKIP.match(n):
                    continue
                p = os.path.join(d, n)
                st = os.stat(p)
                out[os.path.relpath(p, self.root).replace(os.sep, "/")] = [int(st.st_size), int(st.st_mtime)]
        return out

    def read(self, rel):
        with open(os.path.join(self.root, rel), "rb") as f:
            return f.read()


def source(c):
    return Local(c) if c.get("local") else Smb(c)


def wanted(rel):
    return os.path.splitext(rel)[1].lower() in EXTS


def nice_err(e):
    t = f"{type(e).__name__}: {e}"
    low = t.lower()
    if "logonfailure" in low or "logon failure" in low or "status_logon_failure" in low:
        return "Windows did not accept the user name or password (with a Microsoft account: the e-mail address and the account password, not the PIN)."
    if "bad_network_name" in low or "badnetworkname" in low:
        return "The PC answers, but there is no share with that name – check the name after \\\\PC-NAME\\."
    if "access_denied" in low or "accessdenied" in low:
        return "Windows refused access – give this user at least Read permission on the share."
    if "gaierror" in low or "name or service not known" in low or "no address associated" in low or "nodename nor servname" in low:
        return "The PC name was not found in the network – use its IP address instead, e.g. \\\\192.168.178.20\\Share."
    if "timed out" in low or "timeout" in low or "refused" in low or "unreachable" in low or "no route" in low:
        return "The PC is not reachable – is it switched on (not asleep), on the same network, with file sharing allowed (network set to Private)?"
    if "object_name_not_found" in low or "object_path_not_found" in low or "filenotfound" in low:
        return "The sub-folder was not found on the share."
    if isinstance(e, ValueError):
        return str(e)[:300]
    return t[:300]


# ---------------------------------------------------------------- paperless
def multipart_post(path, fields, filename, data):
    b = "----docsorter" + uuid.uuid4().hex
    body = io.BytesIO()
    for k, v in fields:
        body.write(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        body.write(str(v).encode("utf-8"))
        body.write(b"\r\n")
    fn = re.sub(r'["\r\n]', "_", filename)
    body.write(f'--{b}\r\nContent-Disposition: form-data; name="document"; filename="{fn}"\r\n'
               f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8"))
    body.write(data)
    body.write(f"\r\n--{b}--\r\n".encode())
    h = {"Authorization": "Token " + L().token(), "Content-Type": f"multipart/form-data; boundary={b}",
         "Host": S.PL_HOST, "Accept": "application/json; version=%d" % S.api_version()}
    req = urllib.request.Request(S.PAPERLESS + path, data=body.getvalue(), headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read()
    v = json.loads(raw) if raw else ""
    return v if isinstance(v, str) else str(v.get("task_id") or v.get("id") or v)


def task_state(tid):
    r = L().pget("/api/tasks/?task_id=" + urllib.parse.quote(tid))
    items = r if isinstance(r, list) else r.get("results", [])
    if not items:
        return "pending", None, None, ""
    t = items[0]
    st = str(t.get("status") or "").lower()
    ids = t.get("related_document_ids") or ([t["related_document"]] if t.get("related_document") else [])
    rd = t.get("result_data")
    dup = rd.get("duplicate_of") if isinstance(rd, dict) else None
    if not dup and t.get("duplicate_documents"):
        d0 = t["duplicate_documents"][0]
        dup = d0.get("id") if isinstance(d0, dict) else d0
    msg = ""
    if isinstance(rd, dict):
        msg = rd.get("error") or rd.get("message") or rd.get("result") or ""
    msg = msg or t.get("result") or (rd if isinstance(rd, str) else "")
    return st, (int(ids[0]) if ids else None), (int(dup) if dup else None), str(msg)[:300]


def doc_exists(did):
    try:
        L().pget(f"/api/documents/{int(did)}/?fields=id")
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def by_checksum(sha):
    r = L().pget("/api/documents/?page_size=1&fields=id&checksum__iexact=" + sha)
    res = r.get("results") or []
    return int(res[0]["id"]) if res else None


def load_tags():
    return {t["name"].lower(): t for t in L().all_pages("/api/tags/?page_size=500")}


def tag_id(name, parent, tags):
    t = tags.get(name.lower())
    if t:
        return t["id"]
    data = {"name": name, "matching_algorithm": 0}  # never let Paperless add these by itself
    if parent:
        data["parent"] = parent
    try:
        r = L().psend("/api/tags/", data)
    except urllib.error.HTTPError as e:
        if e.code == 400 and parent:
            data.pop("parent")
            r = L().psend("/api/tags/", data)
        else:
            raise
    tags[name.lower()] = r
    L().META["t"] = 0
    return r["id"]


def folder_tag(rel, tags):
    """Windows sub-folders as nested Doc Sorter folders; returns the deepest one (or None)."""
    if not conf().get("folders", True):
        return None
    parts = [p.strip() for p in rel.split("/")[:-1] if p.strip()]
    parent, prev = None, ""
    for i, part in enumerate(parts):
        name = part[:120]
        t = tags.get(name.lower())
        if t and i > 0 and (t.get("parent") or None) != parent:
            name = f"{prev} {part}"[:120]  # "2024" exists elsewhere -> "Miete 2024"
        parent, prev = tag_id(name, parent, tags), part
    return parent


def modify_tags(did, add=(), remove=()):
    add, remove = [t for t in add if t], [t for t in remove if t]
    if add or remove:
        L().psend("/api/documents/bulk_edit/", {"documents": [int(did)], "method": "modify_tags",
                                                 "parameters": {"add_tags": add, "remove_tags": remove}})


def title_of(rel):
    stem = os.path.splitext(rel.rsplit("/", 1)[-1])[0]
    return re.sub(r"[_]+", " ", stem).strip()[:128] or stem[:128]


# ---------------------------------------------------------------- one run
def finish_task(rel, e, tags):
    st, did, dup, msg = task_state(e["task"])
    if st in ("pending", "started", "queued", "received", ""):
        return
    kind = e.pop("kind", "new")
    e.pop("task", None)
    if st == "success" and (did or kind == "version"):
        if kind == "new":
            e["doc"] = did
        e["err"] = ""
        e["at"] = int(time.time())
        L().mark_dirty([e.get("doc")])
        return
    if dup or "duplicate" in msg.lower():  # already in Paperless: adopt that document
        did = dup or by_checksum(e.get("sha", ""))
        if did and kind == "new":
            e["doc"], e["err"] = did, ""
            modify_tags(did, [tags_sync(tags), e.get("ftag")])
            L().mark_dirty([did])
            return
    e["err"] = msg or "Paperless could not import the file"
    e["tries"] = e.get("tries", 0) + 1


def tags_sync(tags):
    return tag_id(TAG_SYNC, None, tags)


def upload_new(rel, e, data, tags):
    ftag = folder_tag(rel, tags)
    fields = [("title", title_of(rel)), ("tags", tags_sync(tags))] + ([("tags", ftag)] if ftag else [])
    e.update(task=multipart_post("/api/documents/post_document/", fields, rel.rsplit("/", 1)[-1], data),
             kind="new", ftag=ftag, name=title_of(rel), err="")


def upload_version(rel, e, data):
    label = "Windows " + time.strftime("%d.%m.%Y %H:%M")
    e.update(task=multipart_post(f"/api/documents/{int(e['doc'])}/update_version/", [("version_label", label)],
                                 rel.rsplit("/", 1)[-1], data), kind="version", err="")


def relocate(old, new, e, tags):
    """Same content under a new name or folder: title and folder follow."""
    did = e.get("doc")
    if did:
        if old.rsplit("/", 1)[0:-1] != new.rsplit("/", 1)[0:-1] or not e.get("ftag"):
            nt = folder_tag(new, tags)
            if nt != e.get("ftag"):
                modify_tags(did, [nt], [e.get("ftag")])
                e["ftag"] = nt
        if title_of(old) != title_of(new):
            L().psend(f"/api/documents/{int(did)}/", {"title": title_of(new)}, "PATCH")
            e["name"] = title_of(new)
        if e.get("gone"):
            modify_tags(did, [], [tag_id(TAG_GONE, None, tags)])
        L().mark_dirty([did])
    e["gone"], e["miss"] = False, 0


def run_once():
    c = conf()
    files, seen = ST["files"], ST["seen"]
    RUN.update(busy=True, phase="reading the folder", warn="", more=False)
    try:
        src = source(c)
        listing = {k: v for k, v in src.walk().items() if wanted(k)}
    except Exception as ex:  # noqa: BLE001
        RUN.update(busy=False, phase="", error=nice_err(ex), last=time.time())
        return
    if ST.get("src") != src.label:  # another folder than before: start a fresh map, Paperless keeps everything
        if ST.get("src"):
            files.clear()
            seen.clear()
        ST["src"] = src.label
    RUN.update(error="", listed=len(listing), phase="checking")
    now = time.time()
    tags = load_tags()

    # 1) uploads Paperless has finished meanwhile
    for rel, e in list(files.items()):
        if e.get("task"):
            try:
                finish_task(rel, e, tags)
            except Exception as ex:  # noqa: BLE001
                e["err"] = nice_err(ex)

    present = [r for r, e in files.items() if not e.get("gone")]
    missing = [r for r in present if r not in listing]
    if present and not listing:
        RUN.update(busy=False, phase="", last=now, error="The folder looks empty – is the PC on and the share reachable? Nothing was changed in Paperless.")
        return
    mass = len(missing) > 5 and len(missing) > 0.5 * len(present)
    if mass:
        RUN["warn"] = f"{len(missing)} files are missing at once – not marking them as deleted until they stay away (was the folder moved?)."

    def stable(rel, meta):
        return seen.get(rel) == meta or now - meta[1] > STABLE_AFTER

    def read(rel, meta):
        if meta[0] > MAX_BYTES:
            raise ValueError(f"larger than {MAX_BYTES // 1048576} MB")
        data = src.read(rel)
        return data, hashlib.sha256(data).hexdigest()

    budget = PER_RUN
    new, tries = [], {}
    # 2) known files: changed content -> new version
    for rel, meta in listing.items():
        e = files.get(rel)
        if e is None:
            if stable(rel, meta):
                new.append(rel)
            continue
        if e.get("ignored") or e.get("task"):
            continue
        if e.get("gone") or e.get("miss"):
            if e.get("gone") and e.get("doc"):
                try:
                    modify_tags(e["doc"], [], [tag_id(TAG_GONE, None, tags)])
                except Exception:  # noqa: BLE001
                    pass
            e["gone"], e["miss"] = False, 0
        if [e.get("size"), e.get("mtime")] == meta or not stable(rel, meta):
            if e.get("err") and not e.get("doc") and e.get("tries", 0) < 3 and budget > 0:
                new.append(rel)  # retry a failed first upload
                tries[rel] = files.pop(rel).get("tries", 0)
            continue
        if budget <= 0:
            RUN["more"] = True
            continue
        try:
            data, sha = read(rel, meta)
            if sha == e.get("sha"):
                e["size"], e["mtime"] = meta
                continue
            if e.get("doc") and doc_exists(e["doc"]):
                upload_version(rel, e, data)
            elif e.get("doc"):
                e.update(ignored=True, err="deleted in Paperless – not uploaded again")
            else:
                upload_new(rel, e, data, tags)
            e.update(sha=sha, size=meta[0], mtime=meta[1])
            budget -= 1
            save_state()
        except Exception as ex:  # noqa: BLE001
            e["err"] = nice_err(ex)

    # 3) new paths: moved / renamed / copied / already in Paperless / really new
    for rel in new:
        meta = listing[rel]
        if budget <= 0:
            RUN["more"] = True
            break
        try:
            data, sha = read(rel, meta)
        except Exception as ex:  # noqa: BLE001
            files[rel] = {"size": meta[0], "mtime": meta[1], "err": nice_err(ex), "tries": tries.get(rel, 0) + 1}
            continue
        RUN["phase"] = "importing " + rel
        try:
            moved = next((r for r in list(files) if r not in listing and files[r].get("sha") == sha), None)
            if moved:
                e = files.pop(moved)
                relocate(moved, rel, e, tags)
                e.update(size=meta[0], mtime=meta[1])
                files[rel] = e
            else:
                twin = next((r for r in files if r in listing and files[r].get("sha") == sha and files[r].get("doc")), None)
                e = {"sha": sha, "size": meta[0], "mtime": meta[1], "tries": tries.get(rel, 0)}
                if twin:  # the same file in two places: one document, both folders
                    e.update(doc=files[twin]["doc"], copy_of=twin, ftag=folder_tag(rel, tags))
                    modify_tags(e["doc"], [e["ftag"]])
                else:
                    did = by_checksum(sha)
                    if did:  # already in Paperless (uploaded by hand before)
                        e.update(doc=did, ftag=folder_tag(rel, tags), name=title_of(rel))
                        modify_tags(did, [tags_sync(tags), e["ftag"]])
                        L().mark_dirty([did])
                    else:
                        upload_new(rel, e, data, tags)
                        budget -= 1
                files[rel] = e
            save_state()
        except Exception as ex:  # noqa: BLE001
            files[rel] = {"sha": sha, "size": meta[0], "mtime": meta[1], "err": nice_err(ex), "tries": tries.get(rel, 0) + 1}

    # 4) gone from Windows: keep in Paperless, mark it (after two checks, never on a mass disappearance)
    docs_present = {files[r].get("doc") for r in files if r in listing}
    for rel in [r for r in files if r not in listing]:
        e = files[rel]
        if not e.get("doc"):
            if not e.get("task"):
                files.pop(rel)
            continue
        if e.get("gone"):
            continue
        e["miss"] = e.get("miss", 0) + 1
        if e["miss"] >= 2 and not mass:
            try:
                if e["doc"] not in docs_present:
                    modify_tags(e["doc"], [tag_id(TAG_GONE, None, tags)])
                    L().mark_dirty([e["doc"]])
                e["gone"] = True
            except Exception as ex:  # noqa: BLE001
                e["err"] = nice_err(ex)

    ST["seen"] = {k: v for k, v in listing.items()}
    ST["last"] = now
    save_state()
    RUN.update(busy=False, phase="", last=now)


def loop():
    time.sleep(8)
    while True:
        try:
            if conf().get("on") and L().token():
                with LOCK:
                    run_once()
        except Exception as ex:  # noqa: BLE001
            RUN.update(busy=False, error=nice_err(ex))
        pending = RUN.get("more") or any(e.get("task") for e in ST["files"].values())
        WAKE.wait(10 if pending else max(30, int(conf().get("every") or 60)))
        WAKE.clear()


# ---------------------------------------------------------------- api
def status():
    c, files = conf(), ST["files"]
    vals = list(files.values())
    synced = sum(1 for e in vals if e.get("doc") and not e.get("task") and not e.get("gone"))
    pending = sum(1 for e in vals if e.get("task"))
    failed = [{"file": r, "error": e["err"]} for r, e in files.items() if e.get("err") and not e.get("task")]
    return {
        "on": bool(c.get("on")), "unc": c.get("unc", ""), "user": c.get("user", ""), "has_pass": bool(c.get("pass")),
        "folders": c.get("folders", True), "every": int(c.get("every") or 60),
        "files": len(files), "synced": synced, "pending": pending, "gone": sum(1 for e in vals if e.get("gone")),
        "waiting": max(0, RUN.get("listed", 0) - len([r for r in files if not files[r].get("gone")])),
        "failed": failed[:20], "failed_n": len(failed),
        "busy": RUN["busy"], "phase": RUN["phase"], "error": RUN["error"], "warn": RUN["warn"],
        "installing": RUN["installing"], "last": ST.get("last") or RUN.get("last") or 0,
    }


def merged(body):
    c = dict(conf())
    for k in ("unc", "user"):
        if k in body:
            c[k] = str(body[k] or "").strip()
    if body.get("pass"):
        c["pass"] = body["pass"]
    if "folders" in body:
        c["folders"] = bool(body["folders"])
    if "every" in body:
        c["every"] = max(30, int(body.get("every") or 60))
    if "on" in body:
        c["on"] = bool(body["on"])
    if body.get("local"):  # tests / a folder on the Umbrel
        c["local"] = body["local"]
    elif "unc" in body:
        c.pop("local", None)
    return c


def test(c):
    try:
        if not c.get("local"):
            parse_unc(c.get("unc"))
        src = source(c)
        listing = src.walk()
    except Exception as ex:  # noqa: BLE001
        return False, {"error": nice_err(ex)}
    docs = [k for k in listing if wanted(k)]
    return True, {"ok": True, "found": len(docs), "skipped": len(listing) - len(docs),
                  "folders": len({k.rsplit("/", 1)[0] for k in docs if "/" in k}), "sample": sorted(docs)[:6]}


def handle(h, path, body):
    if path == "/_lib/api/sync/status":
        return h.send(200, status())
    if path == "/_lib/api/sync/test":
        ok, res = test(merged(body))
        return h.send(200 if ok else 400, res)
    if path == "/_lib/api/sync/save":
        c = merged(body)
        if c.get("on"):
            ok, res = test(c)
            if not ok:
                return h.send(400, res)
        else:
            res = {"ok": True}
        cfg = S.load_cfg()
        cfg["sync"] = c
        S.save_cfg(cfg)
        try:
            import smbclient
            smbclient.reset_connection_cache()
        except Exception:  # noqa: BLE001
            pass
        WAKE.set()
        return h.send(200, res)
    if path == "/_lib/api/sync/run":
        for e in ST["files"].values():
            if e.get("err") and not e.get("ignored"):
                e["tries"] = 0
        WAKE.set()
        return h.send(200, {"ok": True})
    return h.send(404, {"error": "not found"})


threading.Thread(target=loop, daemon=True).start()
