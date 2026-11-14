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

import facts as FX
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
        "paths": [{"id": t["id"], "name": t["name"]} for t in safe_pages("/api/storage_paths/?page_size=500")],
        "fields": [{"id": f["id"], "name": f["name"], "type": f.get("data_type"), "extra": f.get("extra_data") or {}}
                   for f in safe_pages("/api/custom_fields/?page_size=500")],
        "fact_fields": find_fact_fields(),
        "paperless": S.load_cfg().get("public_url", ""),
    }


def safe_pages(path):
    try:
        return all_pages(path)
    except Exception:
        return []


def docs():
    rows = all_pages("/api/documents/?page_size=250&ordering=-created"
                     "&fields=id,title,created,added,tags,document_type,correspondent,page_count,original_file_name,custom_fields")
    lf = find_link_field()
    ff = find_fact_fields()
    for r in rows:
        r["created"] = (r.get("created") or "")[:10]
        r["added"] = (r.get("added") or "")[:10]
        cfs = r.pop("custom_fields", None) or []
        r["links"] = next(([int(x) for x in (cf.get("value") or [])] for cf in cfs if lf and cf.get("field") == lf), [])
        cfm = {cf.get("field"): cf.get("value") for cf in cfs}
        amt = money(cfm.get(ff.get("amount")))
        due = cfm.get(ff.get("due"))
        sug = FACTS.get(str(r["id"])) or {}
        if amt:
            r["amount"] = amt
        elif sug.get("a"):
            r["amount"] = dict(sug["a"], s=1)
        if due:
            r["due"] = {"d": str(due)[:10]}
        elif sug.get("d"):
            r["due"] = dict(sug["d"], s=1)
    return rows


def money(v):
    """Paperless monetary value ('EUR123.45' or 123.45) -> {"v": 123.45, "c": "EUR"}"""
    if v in (None, ""):
        return None
    m = re.fullmatch(r"([A-Z]{3})?(-?\d+(?:\.\d+)?)", str(v))
    if not m:
        return None
    return {"v": float(m.group(2)), "c": m.group(1) or "EUR"}


AMOUNT_FIELD, DUE_FIELD = "Betrag", "Frist"
FF = {"v": {}, "t": 0}


def find_fact_fields(fresh=False):
    if not fresh and FF["v"] and time.time() - FF["t"] < 600:
        return FF["v"]
    out = {}
    for f in safe_pages("/api/custom_fields/?page_size=500"):
        if f["name"] == AMOUNT_FIELD and f.get("data_type") == "monetary":
            out["amount"] = f["id"]
        if f["name"] == DUE_FIELD and f.get("data_type") == "date":
            out["due"] = f["id"]
    FF.update(v=out, t=time.time())
    return out


def fact_field(kind):
    ff = find_fact_fields(fresh=True)
    if ff.get(kind):
        return ff[kind]
    name, typ = (AMOUNT_FIELD, "monetary") if kind == "amount" else (DUE_FIELD, "date")
    fid = psend("/api/custom_fields/", {"name": name, "data_type": typ, "extra_data": {"default_currency": "EUR"} if typ == "monetary" else {}})["id"]
    FF["v"][kind] = fid
    return fid


def set_fields(did, values):
    """values: {field_id: value or None (= remove)} – keeps all other custom fields of the document."""
    d = pget(f"/api/documents/{int(did)}/?fields=id,custom_fields")
    cfs = {cf["field"]: cf.get("value") for cf in (d.get("custom_fields") or [])}
    for fid, v in values.items():
        if v is None:
            cfs.pop(fid, None)
        else:
            cfs[fid] = v
    psend(f"/api/documents/{int(did)}/", {"custom_fields": [{"field": k, "value": v} for k, v in cfs.items()]}, "PATCH")


def save_facts(did, body):
    vals = {}
    if "amount" in body:
        a = body["amount"]
        vals[fact_field("amount")] = None if not a else "%s%.2f" % ((a.get("c") or "EUR").upper()[:3], float(a["v"]))
    if "due" in body:
        vals[fact_field("due")] = body["due"] or None
    if vals:
        set_fields(did, vals)
    return {"ok": True}


# ---------------------------------------------------------------- facts: background scan of all documents

FACTS_FILE = os.path.join(S.DATA, "facts.json")
FACTS = {}
FACTS_STATE = {"running": False, "done": 0, "total": 0, "last": 0}


def load_facts():
    global FACTS
    try:
        with open(FACTS_FILE) as f:
            FACTS = json.load(f)
    except (OSError, ValueError):
        FACTS = {}


def save_facts_cache():
    try:
        with open(FACTS_FILE + ".tmp", "w") as f:
            json.dump(FACTS, f)
        os.replace(FACTS_FILE + ".tmp", FACTS_FILE)
    except OSError:
        pass


def slim(r):
    a, d = r.get("amount"), r.get("deadline")
    return {"a": {"v": a["v"], "c": a["c"], "p": a.get("p"), "k": a.get("kind")} if a else None,
            "d": {"d": d["d"], "k": d["kind"]} if d else None}


def scan_facts():
    if FACTS_STATE["running"] or not token():
        return
    FACTS_STATE["running"] = True
    try:
        rows = all_pages("/api/documents/?page_size=250&fields=id,modified")
        todo = [r["id"] for r in rows if (FACTS.get(str(r["id"])) or {}).get("m") != r.get("modified")]
        FACTS_STATE.update(total=len(todo), done=0)
        mods = {r["id"]: r.get("modified") for r in rows}
        for i in range(0, len(todo), 40):
            part = todo[i:i + 40]
            for d in all_pages(f"/api/documents/?page_size=40&id__in={','.join(map(str, part))}&fields=id,content,created"):
                r = FX.extract(d.get("content") or "", (d.get("created") or "")[:10])
                FACTS[str(d["id"])] = dict(slim(r), m=mods.get(d["id"]))
            FACTS_STATE["done"] = min(len(todo), i + 40)
        live = {str(r["id"]) for r in rows}
        for k in list(FACTS):
            if k not in live:
                FACTS.pop(k)
        save_facts_cache()
        FACTS_STATE["last"] = time.time()
    finally:
        FACTS_STATE["running"] = False


def facts_loop():
    time.sleep(15)
    while True:
        try:
            scan_facts()
        except Exception as e:
            print("facts scan:", e, flush=True)
        time.sleep(600)


# ---------------------------------------------------------------- full document (like the Paperless detail page)

def full_doc(did):
    did = int(did)
    d = pget(f"/api/documents/{did}/?full_perms=true")
    try:
        md = pget(f"/api/documents/{did}/metadata/")
    except Exception:
        md = None
    r = FX.extract(d.get("content") or "", (d.get("created") or d.get("created_date") or "")[:10])
    FACTS[str(did)] = dict(slim(r), m=d.get("modified"))
    with AI_LOCK:
        ai = AI["results"].get(str(did))
        err = AI["errors"].get(str(did))
    return {"doc": d, "meta": md, "facts": r, "fact_fields": find_fact_fields(), "ai": ai, "ai_error": err,
            "link_field": find_link_field()}


def notes(did, body=None, delete=None):
    if delete:
        S.http_json(S.PAPERLESS + f"/api/documents/{int(did)}/notes/?id={int(delete)}", None,
                    {"Authorization": "Token " + token()}, "DELETE")
    elif body and (body.get("note") or "").strip():
        psend(f"/api/documents/{int(did)}/notes/", {"note": body["note"].strip()})
    return pget(f"/api/documents/{int(did)}/notes/")


LF = {"id": None, "t": 0}


def find_link_field():
    if LF["id"] and time.time() - LF["t"] < 600:
        return LF["id"]
    try:
        for f in all_pages("/api/custom_fields/?page_size=200"):
            if f["name"] == LINK_FIELD:
                LF.update(id=f["id"], t=time.time())
                return f["id"]
    except Exception:
        pass
    return None


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
    for k in ("title", "document_type", "correspondent", "tags", "created", "storage_path", "archive_serial_number", "custom_fields"):
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
 "date": "YYYY-MM-DD date the document was issued, not today's date",
 "amount": "the main amount of money the document is about (total to pay, refund, monthly price) as a number like 123.45, or null",
 "deadline": "YYYY-MM-DD of the most important deadline (payment due, objection, contract end, appointment), or null",
 "deadline_reason": "a few words what the deadline is for, or empty"}}

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
    try:
        av = float(str(out.get("amount")).replace(",", ".")) if out.get("amount") not in (None, "", "null") else None
    except ValueError:
        av = None
    res["amount"] = {"v": round(av, 2), "c": "EUR"} if av else None
    dl = str(out.get("deadline") or "")
    res["due"] = {"d": dl, "why": (out.get("deadline_reason") or "")[:60]} if re.fullmatch(r"(19|20)\d\d-[01]\d-[0-3]\d", dl) else None
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
    fx = {}
    if "amount" in pick and res.get("amount"):
        fx["amount"] = res["amount"]
    if "due" in pick and res.get("due"):
        fx["due"] = res["due"]["d"]
    if fx:
        save_facts(did, fx)
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


CASE_CACHE = {"t": 0, "v": []}


def case_proposals():
    """Documents of the same sender that resemble each other and aren't connected yet -> 'make a case file?'"""
    if time.time() - CASE_CACHE["t"] < 300:
        return CASE_CACHE["v"]
    rows = docs()
    corrs = {c["id"]: c["name"] for c in all_pages("/api/correspondents/?page_size=500")}
    groups = {}
    for d in rows:
        if d.get("correspondent"):
            groups.setdefault(d["correspondent"], []).append(d)
    out, calls = [], 0
    for cid, ds in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        free = [d for d in ds if not d.get("links")]
        if len(free) < 2 or calls > 80:
            continue
        ids = {d["id"] for d in ds}
        # union-find over "similar and same sender"
        parent = {d["id"]: d["id"] for d in free}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for d in free[:15]:
            calls += 1
            for r in related(d["id"], 6)[:4]:
                if r["id"] in parent and r["id"] in ids:
                    parent[find(r["id"])] = find(d["id"])
        clusters = {}
        for d in free:
            clusters.setdefault(find(d["id"]), []).append(d)
        for cl in clusters.values():
            if 2 <= len(cl) <= 15:
                cl.sort(key=lambda d: d.get("created") or "")
                out.append({"name": corrs.get(cid, "?"), "title": cl[0].get("title"), "ids": [d["id"] for d in cl],
                            "span": [(cl[0].get("created") or "")[:7], (cl[-1].get("created") or "")[:7]]})
    out.sort(key=lambda g: -len(g["ids"]))
    CASE_CACHE.update(t=time.time(), v=out[:10])
    return CASE_CACHE["v"]


def make_case(ids):
    ids = [int(i) for i in ids]
    root = ids[0]
    for i in ids[1:]:
        link(i, root)
    CASE_CACHE["t"] = 0
    return {"ok": True}


def link_field():
    fid = find_link_field()
    if fid:
        return fid
    fid = psend("/api/custom_fields/", {"name": LINK_FIELD, "data_type": "documentlink"})["id"]
    LF.update(id=fid, t=time.time())
    return fid


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
    CASE_CACHE["t"] = 0
    for x, y in ((a, b), (b, a)):
        cur = links_of(x)
        new = (cur + [y]) if on and y not in cur else [i for i in cur if i != y] if not on else cur
        if new != cur:
            set_links(x, new)
    return {"ok": True}


# ---------------------------------------------------------------- HTTP

def stream_file(h, path, ctype_default, attach=False):
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
        cd = r.getheader("Content-Disposition")
        h.send_header("Content-Disposition", cd if attach else cd.replace("attachment", "inline"))
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
    m = re.fullmatch(r"/_lib/(thumb|preview|download)/(\d+)", path)
    if m:
        kind, did = m.groups()
        if kind == "download":
            return stream_file(h, f"/api/documents/{did}/download/?original=true", "application/octet-stream", attach=True)
        return stream_file(h, f"/api/documents/{did}/{kind}/", "image/webp" if kind == "thumb" else "application/pdf")
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
        if path == "/_lib/api/cases" and h.command == "POST":
            return h.send(200, make_case(body.get("ids") or []))
        if path == "/_lib/api/cases":
            return h.send(200, {"groups": case_proposals()})
        if path == "/_lib/api/link":
            return h.send(200, link(int(body["a"]), int(body["b"]), body.get("on", True)))
        if path == "/_lib/api/bulk":
            return h.send(200, bulk(body))
        m = re.fullmatch(r"/_lib/api/create/(tag|type|corr)", path)
        if m:
            return h.send(200, create(m.group(1), body))
        m = re.fullmatch(r"/_lib/api/doc/(\d+)/(full|facts|notes|notes/delete|fields)", path)
        if m:
            did, op = m.group(1), m.group(2)
            if op == "full":
                return h.send(200, full_doc(did))
            if op == "facts":
                return h.send(200, save_facts(did, body))
            if op == "fields":
                set_fields(did, {int(k): v for k, v in (body.get("values") or {}).items()})
                return h.send(200, {"ok": True})
            if op == "notes/delete":
                return h.send(200, notes(did, delete=body.get("id")))
            return h.send(200, notes(did, body if h.command == "POST" else None))
        if path == "/_lib/api/facts/scan":
            threading.Thread(target=scan_facts, daemon=True).start()
            return h.send(200, FACTS_STATE)
        if path == "/_lib/api/facts/state":
            return h.send(200, FACTS_STATE)
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
            new = all_pages(f"/api/documents/?page_size=100&added__date__gte={since}&document_type__isnull=true&fields=id")
            ids = [d["id"] for d in new if d["id"] not in seen and str(d["id"]) not in AI["results"]]
            if ids:
                seen.update(ids)
                ai_enqueue(ids)
        except Exception:
            pass
        time.sleep(270)


load_ai()
load_facts()
threading.Thread(target=ai_worker, daemon=True).start()
threading.Thread(target=facts_loop, daemon=True).start()
threading.Thread(target=auto_scan, daemon=True).start()
