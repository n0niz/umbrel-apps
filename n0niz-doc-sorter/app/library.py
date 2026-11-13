"""Library view: timeline of all Paperless documents, folders (tags), document types and
one-request AI recognition with Ollama."""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import server as S

AI_FILE = os.path.join(S.DATA, "ai_results.json")
AI = {"queue": [], "busy": None, "results": {}, "errors": {}, "done": 0, "started": 0}
AI_LOCK = threading.Lock()
THINK_FAMILIES = S.THINKING


def token():
    return S.load_cfg().get("token", "")


def pget(path, timeout=30):
    return S.http_json(S.PAPERLESS + path, None, {"Authorization": "Token " + token()}, timeout=timeout)


def psend(path, data, method="POST"):
    return S.http_json(S.PAPERLESS + path, data, {"Authorization": "Token " + token()}, method, timeout=60)


def all_pages(path):
    out, url = [], S.PAPERLESS + path
    while url:
        d = S.http_json(url, None, {"Authorization": "Token " + token()}, timeout=60)
        out += d.get("results", [])
        nxt = d.get("next")
        url = (S.PAPERLESS + "/api/" + nxt.split("/api/", 1)[1]) if nxt else None
    return out


def meta():
    tags = all_pages("/api/tags/?page_size=500")
    hidden = {n.lower() for n in S.TAGS.values()}
    return {
        "tags": [{"id": t["id"], "name": t["name"], "parent": t.get("parent"), "color": t.get("color"),
                  "count": t.get("document_count", 0), "system": t["name"].lower() in hidden,
                  "inbox": bool(t.get("is_inbox_tag"))} for t in tags],
        "types": [{"id": t["id"], "name": t["name"], "count": t.get("document_count", 0)}
                  for t in all_pages("/api/document_types/?page_size=500")],
        "corrs": [{"id": t["id"], "name": t["name"], "count": t.get("document_count", 0)}
                  for t in all_pages("/api/correspondents/?page_size=500")],
        "paperless": S.load_cfg().get("public_url", ""),
    }


def docs():
    rows = all_pages("/api/documents/?page_size=250&ordering=-created"
                     "&fields=id,title,created,added,tags,document_type,correspondent,page_count,original_file_name")
    for r in rows:
        r["created"] = (r.get("created") or "")[:10]
        r["added"] = (r.get("added") or "")[:10]
    return rows


def bulk(body):
    ids = [int(i) for i in body.get("ids", [])]
    if not ids:
        raise ValueError("no documents selected")
    done = []
    if body.get("add_tags") or body.get("remove_tags"):
        psend("/api/documents/bulk_edit/", {"documents": ids, "method": "modify_tags",
                                             "parameters": {"add_tags": body.get("add_tags", []), "remove_tags": body.get("remove_tags", [])}})
        done.append("tags")
    if "document_type" in body:
        psend("/api/documents/bulk_edit/", {"documents": ids, "method": "set_document_type",
                                             "parameters": {"document_type": body["document_type"]}})
        done.append("type")
    if "correspondent" in body:
        psend("/api/documents/bulk_edit/", {"documents": ids, "method": "set_correspondent",
                                             "parameters": {"correspondent": body["correspondent"]}})
        done.append("correspondent")
    return {"ok": True, "changed": done}


def create(kind, body):
    name = (body.get("name") or "").strip()
    if not name:
        raise ValueError("name missing")
    path = {"tag": "/api/tags/", "type": "/api/document_types/", "corr": "/api/correspondents/"}[kind]
    data = {"name": name, "matching_algorithm": 6}  # "Auto": Paperless learns from what you sort
    if kind == "tag" and body.get("parent"):
        data["parent"] = int(body["parent"])
    try:
        r = psend(path, data)
    except urllib.error.HTTPError as e:
        if e.code == 400 and kind == "tag" and "parent" in data:  # Paperless without nested tags
            data.pop("parent")
            r = psend(path, data)
        else:
            raise
    return {"id": r["id"], "name": r["name"], "parent": r.get("parent")}


def update_doc(did, body):
    data = {}
    for k in ("title", "document_type", "correspondent", "tags", "created"):
        if k in body:
            data[k] = body[k]
    r = psend(f"/api/documents/{int(did)}/", data, "PATCH")
    with AI_LOCK:
        AI["results"].pop(str(did), None)
        save_ai()
    return {"ok": True, "id": r.get("id")}


# ---------------------------------------------------------------- AI recognition

def save_ai():
    try:
        with open(AI_FILE + ".tmp", "w") as f:
            json.dump(AI["results"], f)
        os.replace(AI_FILE + ".tmp", AI_FILE)
    except OSError:
        pass


def load_ai():
    try:
        with open(AI_FILE) as f:
            AI["results"] = json.load(f)
    except (OSError, ValueError):
        AI["results"] = {}


def norm(s):
    return re.sub(r"[^a-z0-9äöüß]", "", (s or "").lower())


def match(name, items):
    """name -> existing item (exact, then contained), or None."""
    n = norm(name)
    if not n:
        return None
    for it in items:
        if norm(it["name"]) == n:
            return it
    for it in items:
        m = norm(it["name"])
        if len(m) >= 4 and (m in n or n in m):
            return it
    return None


def classify(did):
    cfg = S.load_cfg()
    model = cfg.get("model")
    if not model:
        raise RuntimeError("no model chosen in the setup")
    d = pget(f"/api/documents/{int(did)}/?fields=id,title,content,created,tags,document_type,correspondent")
    m = meta()
    tags = [t for t in m["tags"] if not t["system"] and not t["inbox"]]
    by_id = {t["id"]: t for t in m["tags"]}

    def path(t):
        p, seen = t["name"], set()
        while t.get("parent") and t["parent"] in by_id and t["parent"] not in seen:
            seen.add(t["parent"])
            t = by_id[t["parent"]]
            p = t["name"] + " > " + p
        return p
    tag_paths = {path(t): t for t in tags}
    limit = int(cfg.get("limit") or 1500)
    content = (d.get("content") or "").strip()
    if limit:
        content = content[: limit * 4]  # ~4 characters per token
    if not content:
        raise RuntimeError("document has no text (OCR empty)")
    lang = cfg.get("language", "German")
    prompt = f"""Sort this scanned document for a personal document archive. The document is probably in {lang}.

Existing document types: {", ".join(t["name"] for t in m["types"]) or "(none yet)"}
Existing folders (tags): {", ".join(tag_paths) or "(none yet)"}
Existing correspondents: {", ".join(c["name"] for c in m["corrs"][:300]) or "(none yet)"}

Answer with JSON only:
{{"title": "short {lang} title, e.g. 'Mietvertrag Wohnung' or 'Rechnung Stromabschlag März'",
 "document_type": "one existing type; only if none fits, a new short {lang} type such as Vertrag, Rechnung, Bescheid, Lohnabrechnung, Zeugnis",
 "folders": ["0 to 3 existing folders that fit, written exactly as listed"],
 "new_folder": "only if no existing folder fits: one short new folder name, else empty",
 "correspondent": "sender or company/authority the document is from (existing name if it matches)",
 "date": "YYYY-MM-DD date the document was issued, not today's date"}}

<document>
{content}
</document>"""
    body = {"model": model, "stream": False, "format": "json", "keep_alive": "30m",
            "messages": [{"role": "user", "content": prompt}],
            "options": {"temperature": 0, "num_ctx": int(cfg.get("ctx") or 4096)}}
    think = cfg.get("think", "auto")
    if think == "false" or (think == "auto" and model.split(":")[0].lower().startswith(THINK_FAMILIES)):
        body["think"] = False
    t0 = time.time()
    r = S.http_json(S.OLLAMA + "/api/chat", body, timeout=int(cfg.get("timeout", 600)))
    raw = (r.get("message") or {}).get("content", "")
    try:
        out = json.loads(raw)
    except ValueError:
        mm = re.search(r"\{.*\}", raw, re.S)
        out = json.loads(mm.group(0)) if mm else {}
    res = {"secs": round(time.time() - t0, 1), "title": (out.get("title") or "").strip()[:128]}
    t = match(out.get("document_type"), m["types"])
    res["type"] = {"id": t["id"], "name": t["name"]} if t else ({"id": None, "name": out["document_type"].strip()[:60]} if (out.get("document_type") or "").strip() else None)
    c = match(out.get("correspondent"), m["corrs"])
    res["corr"] = {"id": c["id"], "name": c["name"]} if c else ({"id": None, "name": out["correspondent"].strip()[:80]} if (out.get("correspondent") or "").strip() else None)
    folders = []
    for f in (out.get("folders") or [])[:3]:
        if not isinstance(f, str):
            continue
        hit = tag_paths.get(f.strip()) or match(f.split(">")[-1], tags)
        if hit and hit["id"] not in [x["id"] for x in folders]:
            folders.append({"id": hit["id"], "name": hit["name"]})
    nf = (out.get("new_folder") or "").strip()
    if nf and not match(nf, tags) and (cfg.get("new_tags") or not folders):
        folders.append({"id": None, "name": nf[:60]})
    res["folders"] = folders
    dt = str(out.get("date") or "")
    res["date"] = dt if re.fullmatch(r"(19|20)\d\d-[01]\d-[0-3]\d", dt) else None
    return res


def ai_worker():
    while True:
        with AI_LOCK:
            did = AI["queue"].pop(0) if AI["queue"] else None
            AI["busy"] = did
        if did is None:
            time.sleep(1)
            continue
        try:
            res = classify(did)
            with AI_LOCK:
                AI["results"][str(did)] = res
                AI["errors"].pop(str(did), None)
                save_ai()
        except Exception as e:
            with AI_LOCK:
                AI["errors"][str(did)] = S.err_text(e)
        with AI_LOCK:
            AI["done"] += 1
            AI["busy"] = None


def ai_enqueue(ids):
    with AI_LOCK:
        if not AI["queue"] and AI["busy"] is None:
            AI["done"], AI["started"] = 0, time.time()
        for i in ids:
            i = int(i)
            if i not in AI["queue"] and i != AI["busy"]:
                AI["queue"].append(i)
                AI["errors"].pop(str(i), None)
    return ai_state()


def ai_state():
    with AI_LOCK:
        return {"queue": list(AI["queue"]), "busy": AI["busy"], "done": AI["done"],
                "results": AI["results"], "errors": AI["errors"]}


def ai_clear(ids):
    with AI_LOCK:
        for i in ids:
            AI["results"].pop(str(i), None)
            AI["errors"].pop(str(i), None)
        if not ids:
            AI["queue"].clear()
        save_ai()
    return ai_state()


def apply_ai(did, body):
    """Write accepted suggestions; creates new types/folders/correspondents on the way."""
    res = body.get("suggestion") or {}
    pick = set(body.get("fields") or ["title", "type", "folders", "corr", "date"])
    cur = pget(f"/api/documents/{int(did)}/?fields=id,tags")
    data = {}
    if "title" in pick and res.get("title"):
        data["title"] = res["title"]
    if "type" in pick and res.get("type"):
        data["document_type"] = res["type"]["id"] or create("type", {"name": res["type"]["name"]})["id"]
    if "corr" in pick and res.get("corr"):
        data["correspondent"] = res["corr"]["id"] or create("corr", {"name": res["corr"]["name"]})["id"]
    if "folders" in pick and res.get("folders"):
        tags = list(cur.get("tags") or [])
        for f in res["folders"]:
            tid = f["id"] or create("tag", {"name": f["name"]})["id"]
            if tid not in tags:
                tags.append(tid)
        data["tags"] = tags
    if "date" in pick and res.get("date"):
        data["created"] = res["date"]
    if data:
        psend(f"/api/documents/{int(did)}/", data, "PATCH")
    with AI_LOCK:
        AI["results"].pop(str(did), None)
        save_ai()
    return {"ok": True, "changed": list(data)}


# ---------------------------------------------------------------- folders, similarity, links

REL_CACHE = {}
LINK_FIELD = "Gehört zu"
DOC_FIELDS = "id,title,created,tags,document_type,correspondent,original_file_name"


def related(did, n=8):
    key = (int(did), n)
    hit = REL_CACHE.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        rows = pget(f"/api/documents/?more_like_id={int(did)}&page_size={n}&fields={DOC_FIELDS}").get("results", [])
    except urllib.error.HTTPError:
        rows = []
    out = [dict(r, created=(r.get("created") or "")[:10]) for r in rows if r.get("id") != int(did)]
    REL_CACHE[key] = (time.time(), out)
    return out


def folder_tags():
    hidden = {n.lower() for n in S.TAGS.values()}
    return {t["id"]: t for t in all_pages("/api/tags/?page_size=500") if t["name"].lower() not in hidden and not t.get("is_inbox_tag")}


def suggest_folders(ids):
    """For documents without folder: vote over the folders of their most similar documents."""
    tags = folder_tags()
    out = {}
    for did in ids[:30]:
        votes = {}
        rel = related(did, 8)
        for rank, r in enumerate(rel):
            for t in r.get("tags") or []:
                if t in tags:
                    votes[t] = votes.get(t, 0) + 1.0 / (1 + rank * 0.5)
        if votes:
            best = max(votes, key=votes.get)
            total = sum(1.0 / (1 + i * 0.5) for i in range(len(rel))) or 1
            out[str(did)] = {"id": best, "name": tags[best]["name"], "score": round(min(1, votes[best] / total * 1.6), 2)}
        else:
            out[str(did)] = None
    return out


def folder_candidates(tag_id, limit=24):
    """Documents not in the folder that resemble the ones inside."""
    ids = descendants(int(tag_id))
    inside = all_pages(f"/api/documents/?page_size=100&tags__id__in={','.join(map(str, ids))}&fields=id,tags")
    have = {d["id"] for d in inside}
    score = {}
    rows = {}
    for d in inside[:12]:
        for rank, r in enumerate(related(d["id"], 10)):
            if r["id"] in have:
                continue
            score[r["id"]] = score.get(r["id"], 0) + 1.0 / (1 + rank * 0.4)
            rows[r["id"]] = r
    best = sorted(score, key=score.get, reverse=True)[:limit]
    top = max(score.values()) if score else 1
    return [dict(rows[i], score=round(score[i] / top, 2)) for i in best]


def descendants(tid):
    tags = all_pages("/api/tags/?page_size=500")
    out, grew = {tid}, True
    while grew:
        grew = False
        for t in tags:
            if t.get("parent") in out and t["id"] not in out:
                out.add(t["id"])
                grew = True
    return out


def folder_edit(tid, body):
    data = {k: body[k] for k in ("name", "color", "parent") if k in body}
    if "parent" in data and data["parent"] in descendants(int(tid)):
        raise ValueError("a folder can't go inside itself")
    return psend(f"/api/tags/{int(tid)}/", data, "PATCH")


def folder_delete(tid):
    S.http_json(S.PAPERLESS + f"/api/tags/{int(tid)}/", None, {"Authorization": "Token " + token()}, "DELETE")
    return {"ok": True}


def folder_merge(src, dst):
    docs = all_pages(f"/api/documents/?page_size=250&tags__id__all={int(src)}&fields=id")
    if docs:
        psend("/api/documents/bulk_edit/", {"documents": [d["id"] for d in docs], "method": "modify_tags",
                                             "parameters": {"add_tags": [int(dst)], "remove_tags": [int(src)]}})
    for t in all_pages("/api/tags/?page_size=500"):
        if t.get("parent") == int(src):
            psend(f"/api/tags/{t['id']}/", {"parent": int(dst)}, "PATCH")
    folder_delete(src)
    return {"ok": True, "moved": len(docs)}


def proposals():
    """Unfiled documents from the same sender -> 'make a folder?'"""
    tags = folder_tags()
    corrs = {c["id"]: c["name"] for c in all_pages("/api/correspondents/?page_size=500")}
    groups = {}
    for d in docs():
        if d.get("correspondent") and not any(t in tags for t in d.get("tags") or []):
            groups.setdefault(d["correspondent"], []).append(d["id"])
    names = {t["name"].lower() for t in tags.values()}
    out = [{"name": corrs.get(c, "?"), "ids": ids} for c, ids in groups.items()
           if len(ids) >= 3 and corrs.get(c, "").lower() not in names]
    return sorted(out, key=lambda g: -len(g["ids"]))[:8]


def link_field():
    for f in all_pages("/api/custom_fields/?page_size=200"):
        if f["name"] == LINK_FIELD:
            return f["id"]
    return psend("/api/custom_fields/", {"name": LINK_FIELD, "data_type": "documentlink"})["id"]


def links_of(did):
    fid = link_field()
    d = pget(f"/api/documents/{int(did)}/?fields=id,custom_fields")
    for cf in d.get("custom_fields") or []:
        if cf.get("field") == fid:
            return [int(x) for x in (cf.get("value") or [])]
    return []


def set_links(did, ids):
    fid = link_field()
    d = pget(f"/api/documents/{int(did)}/?fields=id,custom_fields")
    cfs = [cf for cf in (d.get("custom_fields") or []) if cf.get("field") != fid]
    cfs.append({"field": fid, "value": sorted(set(int(i) for i in ids if int(i) != int(did)))})
    psend(f"/api/documents/{int(did)}/", {"custom_fields": cfs}, "PATCH")


def link(a, b, on=True):
    """Connect two documents both ways (Paperless shows the link field on each)."""
    for x, y in ((a, b), (b, a)):
        cur = links_of(x)
        new = (cur + [y]) if on and y not in cur else [i for i in cur if i != y] if not on else cur
        if new != cur:
            set_links(x, new)
    return {"ok": True}


# ---------------------------------------------------------------- HTTP

def stream_file(h, path, ctype_default):
    import http.client
    u = urllib.parse.urlsplit(S.PAPERLESS)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=120)
    conn.request("GET", path, headers={"Host": S.PL_HOST, "Authorization": "Token " + token(),
                                       "Accept": "application/json; version=%d" % S.api_version()})
    r = conn.getresponse()
    h.send_response(r.status)
    h.send_header("Content-Type", r.getheader("Content-Type") or ctype_default)
    if r.getheader("Content-Length"):
        h.send_header("Content-Length", r.getheader("Content-Length"))
    if r.getheader("Content-Disposition"):
        h.send_header("Content-Disposition", r.getheader("Content-Disposition").replace("attachment", "inline"))
    h.send_header("Cache-Control", "private, max-age=86400")
    h.end_headers()
    while True:
        b = r.read(65536)
        if not b:
            break
        h.wfile.write(b)
    conn.close()


def handle(h, path):
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(h.path).query)
    if not token():
        return h.send(409, {"error": "Doc Sorter is not connected to Paperless yet – open the setup."})
    m = re.fullmatch(r"/_lib/(thumb|preview)/(\d+)", path)
    if m:
        kind, did = m.groups()
        return stream_file(h, f"/api/documents/{did}/{'thumb' if kind == 'thumb' else 'preview'}/",
                           "image/webp" if kind == "thumb" else "application/pdf")
    body = json.loads(h.body() or b"{}") if h.command in ("POST", "PATCH") else {}
    try:
        if path == "/_lib/api/meta":
            return h.send(200, meta())
        if path == "/_lib/api/docs":
            return h.send(200, {"docs": docs()})
        m = re.fullmatch(r"/_lib/api/related/(\d+)", path)
        if m:
            d = pget(f"/api/documents/{m.group(1)}/?fields=id")
            return h.send(200, {"related": related(d["id"], 10), "links": links_of(d["id"])})
        if path == "/_lib/api/suggest_folders":
            return h.send(200, suggest_folders([int(i) for i in body.get("ids", [])]))
        m = re.fullmatch(r"/_lib/api/folder/(\d+)/(candidates|edit|delete|merge)", path)
        if m:
            tid, op = int(m.group(1)), m.group(2)
            if op == "candidates":
                return h.send(200, {"docs": folder_candidates(tid)})
            if op == "edit":
                return h.send(200, folder_edit(tid, body))
            if op == "delete":
                return h.send(200, folder_delete(tid))
            return h.send(200, folder_merge(tid, int(body["into"])))
        if path == "/_lib/api/proposals":
            return h.send(200, {"groups": proposals()})
        if path == "/_lib/api/link":
            return h.send(200, link(int(body["a"]), int(body["b"]), body.get("on", True)))
        if path == "/_lib/api/bulk":
            return h.send(200, bulk(body))
        m = re.fullmatch(r"/_lib/api/create/(tag|type|corr)", path)
        if m:
            return h.send(200, create(m.group(1), body))
        m = re.fullmatch(r"/_lib/api/doc/(\d+)", path)
        if m and h.command == "PATCH":
            return h.send(200, update_doc(m.group(1), body))
        if m:
            return h.send(200, pget(f"/api/documents/{m.group(1)}/?fields=id,title,created,tags,document_type,correspondent,content,original_file_name,page_count"))
        if path == "/_lib/api/ai" and h.command == "POST":
            return h.send(200, ai_enqueue(body.get("ids") or []))
        if path == "/_lib/api/ai":
            return h.send(200, ai_state())
        if path == "/_lib/api/ai/clear":
            return h.send(200, ai_clear(body.get("ids") or []))
        m = re.fullmatch(r"/_lib/api/ai/apply/(\d+)", path)
        if m:
            return h.send(200, apply_ai(m.group(1), body))
        return h.send(404, {"error": "not found"})
    except urllib.error.HTTPError as e:
        return h.send(502, {"error": "Paperless: " + S.err_text(e)})
    except Exception as e:
        return h.send(400, {"error": str(e)})


def auto_scan():
    """New documents (added in the last 3 days, no type yet) get an AI suggestion by themselves."""
    seen = set()
    while True:
        time.sleep(30)
        try:
            if not token() or not S.load_cfg().get("model") or S.load_cfg().get("lib_auto") is False:
                continue
            since = time.strftime("%Y-%m-%d", time.localtime(time.time() - 3 * 86400))
            new = all_pages(f"/api/documents/?page_size=100&added__date__gte={since}&document_type__isnull=1&fields=id")
            ids = [d["id"] for d in new if d["id"] not in seen and str(d["id"]) not in AI["results"]]
            if ids:
                seen.update(ids)
                ai_enqueue(ids)
        except Exception:
            pass
        time.sleep(270)


load_ai()
threading.Thread(target=ai_worker, daemon=True).start()
threading.Thread(target=auto_scan, daemon=True).start()
