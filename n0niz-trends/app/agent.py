"""Booking agent and monthly news flash for Spending Trends.

Categorizer: learns from the bookings you categorised and tagged yourself
(after Firefly's own rules ran) and fills in what is still missing:
  * payee seen before      -> category / tags you usually give it   (tag auto-categorized)
  * unknown payee          -> local LLM picks one of your categories (tag ai-categorized,
                              or ai-unsure with a suggestion when not confident)
  * category clearly odd   -> tag ai-check with a note, never overwritten

News flash: exact figures for a month (changes against last month, the 12-month
average and last year, new payees, subscriptions that started, stopped or changed
price, biggest expenses). The LLM only turns those facts into a few sentences.
Standard library only.
"""

import json
import re
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import date, timedelta

TAG_AI, TAG_UNSURE, TAG_CHECK, TAG_HIST = "ai-categorized", "ai-unsure", "ai-check", "auto-categorized"
OWN_TAGS = {TAG_AI, TAG_UNSURE, TAG_CHECK, TAG_HIST}
NOT_LEARNED_TAGS = OWN_TAGS | {"check", "krak-card"}   # tags that describe the card or a to-do, not the payee
OLLAMA_DEFAULTS = ["http://ollama_ollama_1:11434", "http://ollama_server_1:11434", "http://ollama_app_1:11434"]
MODEL_PREF = ["qwen2.5", "qwen3", "llama3.2", "llama3.1", "gemma3", "gemma2", "mistral", "phi"]


# ------------------------------------------------------------------ Ollama ---
def _post(url, body, timeout=240):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def ollama_models(base):
    with urllib.request.urlopen(base.rstrip("/") + "/api/tags", timeout=8) as r:
        return [m["name"] for m in json.loads(r.read()).get("models", [])]


def find_ollama(preferred=""):
    errs = []
    for base in [preferred] + OLLAMA_DEFAULTS:
        if not base:
            continue
        try:
            return base.rstrip("/"), ollama_models(base)
        except Exception as e:  # noqa: BLE001
            errs.append(f"{base}: {e}")
    raise RuntimeError("Ollama not reachable. Tried " + "; ".join(errs))


def pick_model(models, wanted=""):
    if wanted and wanted in models:
        return wanted
    for p in MODEL_PREF:
        for m in models:
            if m.startswith(p):
                return m
    return models[0] if models else ""


def chat(base, model, prompt, as_json=False):
    body = {"model": model, "stream": False, "options": {"temperature": 0.2 if not as_json else 0},
            "messages": [{"role": "user", "content": prompt}]}
    if as_json:
        body["format"] = "json"
    return _post(base + "/api/chat", body)["message"]["content"]


# ----------------------------------------------------------------- Firefly ---
def journals(get, start, end):
    out = []
    for kind in ("withdrawal", "deposit"):
        page = 1
        while True:
            res = get("/api/v1/transactions", {"type": kind, "start": start, "end": end, "page": page, "limit": 500})
            data = res.get("data", [])
            for g in data:
                for j in g["attributes"]["transactions"]:
                    if j.get("type") in ("withdrawal", "deposit"):
                        out.append((g["id"], j))
            pg = res.get("meta", {}).get("pagination", {}) or {}
            per = int(pg.get("per_page") or 0)
            if not data or (page >= int(pg.get("total_pages") or 1) and not (per and len(data) >= per)):
                break
            page += 1
    return out


def categories(get):
    names, page = [], 1
    while True:
        res = get("/api/v1/categories", {"page": page, "limit": 200})
        data = res.get("data", [])
        names += [c["attributes"]["name"] for c in data]
        if not data or page >= int(res.get("meta", {}).get("pagination", {}).get("total_pages") or 1):
            return names
        page += 1


def counterpart(j):
    return (j.get("destination_name") if j["type"] == "withdrawal" else j.get("source_name")) or ""


STOP = {"gmbh", "ag", "kg", "co", "se", "markt", "sagt", "danke", "fil", "filiale", "de", "com", "www", "ek", "e", "k", "gbr", "ug", "sarl", "sca", "et", "cie"}


def payee_key(name):
    s = re.sub(r"[^a-zäöüß ]", " ", (name or "").lower())
    words = [w for w in s.split() if len(w) > 1 and w not in STOP]
    return " ".join(words[:2])


def update(put, gid, j, category=None, tags=None, note=None):
    payload = {"transaction_journal_id": j["transaction_journal_id"]}
    if tags is not None:
        payload["tags"] = sorted(set(tags))
    if category is not None:
        payload["category_name"] = category
    if note:
        old = (j.get("notes") or "").strip()
        payload["notes"] = (old + "\n" if old else "") + note
    put(f"/api/v1/transactions/{gid}", {"apply_rules": False, "fire_webhooks": False, "transactions": [payload]})


# -------------------------------------------------------------- categorize ---
def learn(hist):
    """payee -> Counter(category), Counter(tag), number of your own bookings."""
    cats, tags, n = defaultdict(Counter), defaultdict(Counter), Counter()
    for _, j in hist:
        jt = set(j.get("tags") or [])
        if jt & {TAG_AI, TAG_UNSURE}:
            continue                                  # never learn from the agent's own guesses
        k = (j["type"], payee_key(counterpart(j)))
        n[k] += 1
        if j.get("category_name"):
            cats[k][j["category_name"]] += 1
        for t in jt - NOT_LEARNED_TAGS:
            tags[k][t] += 1
    return cats, tags, n


def ask_llm(base, model, j, cats, examples):
    kind = "expense" if j["type"] == "withdrawal" else "income"
    ex = "\n".join(f"- {p} -> {c}" for p, c in examples[:50])
    prompt = f"""You sort bank and card bookings of a household in Germany into budget categories.
Pick exactly one category from this list, spelled exactly as written:
{json.dumps(cats, ensure_ascii=False)}

How this household categorised other payees before:
{ex or "- (no history yet)"}

Booking ({kind}):
payee: {counterpart(j)}
description: {j.get("description", "")}
amount: {abs(float(j.get("amount") or 0)):.2f} {j.get("currency_code", "EUR")}

Answer only with JSON: {{"category": "<one name from the list or null>", "confidence": <0..1>, "reason": "<max 12 words>"}}"""
    try:
        ans = json.loads(chat(base, model, prompt, as_json=True))
    except ValueError:
        return None, 0.0, "unreadable answer"
    cat = ans.get("category")
    match = next((c for c in cats if cat and c.lower() == str(cat).lower()), None)
    try:
        conf = float(ans.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    return match, conf, str(ans.get("reason") or "")[:120]


def run(get, put, days=3, dry=False, min_conf=0.75, llm=None, history_days=730, progress=None):
    """llm: (base, model) or None. Returns (stats, actions)."""
    today = date.today()
    cats = categories(get)
    hist = journals(get, (today - timedelta(days=history_days)).isoformat(), today.isoformat())
    lc, lt, ln = learn(hist)
    examples = [(p, c.most_common(1)[0][0]) for (_, p), c in sorted(lc.items(), key=lambda kv: -sum(kv[1].values())) if p]
    start = (today - timedelta(days=days)).isoformat()
    todo = [(g, j) for g, j in hist if j["date"][:10] >= start]
    stats, actions = Counter(), []
    for n_done, (gid, j) in enumerate(todo):
        if progress:
            progress(n_done + 1, len(todo))
        tags = set(j.get("tags") or [])
        k = (j["type"], payee_key(counterpart(j)))
        hc, ht, hn = lc.get(k, Counter()), lt.get(k, Counter()), ln.get(k, 0)
        act = {"date": j["date"][:10], "payee": counterpart(j), "amount": round(float(j["amount"]) * (-1 if j["type"] == "withdrawal" else 1), 2),
               "gid": gid, "jid": j["transaction_journal_id"], "account": (j.get("source_name") if j["type"] == "withdrawal" else j.get("destination_name")) or "",
               "description": (j.get("description") or "")[:100], "t": int(time.time())}
        # tags you always give this payee (at least 2 earlier bookings, 70 % of them)
        own_n = hn - (0 if tags & {TAG_AI, TAG_UNSURE} else 1)
        add_tags = {t for t, c in ht.items() if own_n >= 2 and c - (1 if t in tags else 0) >= 0.7 * own_n} - tags
        if j.get("category_name"):
            others = hc.copy()
            others[j["category_name"]] -= 1
            others = +others
            n, (top, top_n) = sum(others.values()), (others.most_common(1)[0] if others else (None, 0))
            if TAG_CHECK not in tags and n >= 3 and top != j["category_name"] and top_n / n >= 0.8 and others[j["category_name"]] == 0:
                if not dry:
                    update(put, gid, j, tags=tags | {TAG_CHECK} | add_tags, note=f"[agent] Usually '{top}' for this payee ({top_n} of {n}), here '{j['category_name']}'.")
                actions.append({**act, "action": "check", "category": j["category_name"], "suggest": top, "tags": sorted(add_tags)})
                stats["check"] += 1
            elif add_tags:
                if not dry:
                    update(put, gid, j, tags=tags | add_tags)
                actions.append({**act, "action": "tags", "category": j["category_name"], "tags": sorted(add_tags)})
                stats["tags"] += 1
            else:
                stats["ok"] += 1
            continue
        if tags & OWN_TAGS:
            stats["seen"] += 1
            continue
        hn_c = sum(hc.values())
        top, top_n = hc.most_common(1)[0] if hc else (None, 0)
        if hn_c >= 2 and top_n / hn_c >= 0.7:
            if not dry:
                update(put, gid, j, category=top, tags=tags | add_tags | {TAG_HIST})
            actions.append({**act, "action": "history", "category": top, "tags": sorted(add_tags), "conf": round(top_n / hn_c, 2),
                            "reason": f"{top_n} of {hn_c} earlier bookings"})
            stats["history"] += 1
            continue
        if not llm:
            stats["left"] += 1
            continue
        try:
            cat, conf, why = ask_llm(llm[0], llm[1], j, cats, examples)
        except Exception as e:  # noqa: BLE001
            actions.append({**act, "action": "error", "reason": str(e)[:160]})
            stats["error"] += 1
            continue
        if cat and conf >= min_conf:
            if not dry:
                update(put, gid, j, category=cat, tags=tags | add_tags | {TAG_AI}, note=f"[agent] {cat} ({conf:.0%}): {why}")
            actions.append({**act, "action": "ai", "category": cat, "conf": round(conf, 2), "reason": why, "tags": sorted(add_tags)})
            stats["ai"] += 1
        else:
            if not dry:
                update(put, gid, j, tags=tags | add_tags | {TAG_UNSURE}, note=f"[agent] unsure, maybe '{cat or '?'}' ({conf:.0%}): {why}")
            actions.append({**act, "action": "unsure", "suggest": cat, "conf": round(conf, 2), "reason": why, "tags": sorted(add_tags)})
            stats["unsure"] += 1
    stats["categories"] = len(cats)
    stats["learned"] = len(lc)
    return dict(stats), actions, cats


def correct(get, put, gid, jid, category):
    """Your correction: set the category, drop the agent's tags. Future runs learn from it."""
    res = get(f"/api/v1/transactions/{gid}", {})
    for j in res["data"]["attributes"]["transactions"]:
        if str(j["transaction_journal_id"]) == str(jid):
            tags = set(j.get("tags") or []) - OWN_TAGS
            update(put, gid, j, category=category if category is not None else j.get("category_name"), tags=tags)
            return True
    return False


# -------------------------------------------------------------------- news ---
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]


def _mkey(y, m):
    while m < 1:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    return f"{y}-{m:02d}"


def _eur(v):
    s = f"{abs(v):,.0f}".replace(",", ".")
    return ("−" if v < 0 else "") + s + " €"


def facts(rows, month, currency="EUR", upto_day=None):
    """rows: Spending Trends rows. month: 'YYYY-MM'. Exact numbers only."""
    y, m = map(int, month.split("-"))
    rows = [r for r in rows if r[7] == currency and not (len(r) > 11 and r[11])]
    exp, inc = defaultdict(float), defaultdict(float)
    by_cat, by_payee, by_tag = defaultdict(lambda: defaultdict(float)), defaultdict(lambda: defaultdict(list)), defaultdict(lambda: defaultdict(float))
    biggest = []
    for r in rows:
        mk = r[0]
        if upto_day and mk == month and int(r[9][8:10]) > upto_day:
            continue
        if r[1] == "e":
            exp[mk] += r[2]
            by_cat[r[3] or "(no category)"][mk] += r[2]
            by_payee[r[6] or "?"][mk].append(r[2])
            for t in r[5]:
                if t not in NOT_LEARNED_TAGS and t != "krak-card":
                    by_tag[t][mk] += r[2]
            if mk == month:
                biggest.append((r[2], r[6] or r[10], r[3], r[9]))
        else:
            inc[mk] += r[2]
    prev, ly = _mkey(y, m - 1), _mkey(y - 1, m)
    last12 = [_mkey(y, m - i) for i in range(1, 13)]
    have12 = [k for k in last12 if exp.get(k)]
    avg = lambda d: sum(d.get(k, 0) for k in have12) / len(have12) if have12 else 0  # noqa: E731
    f = {"month": month, "partial": bool(upto_day), "spent": round(exp[month], 2), "income": round(inc[month], 2),
         "net": round(inc[month] - exp[month], 2), "spent_prev": round(exp.get(prev, 0), 2), "spent_last_year": round(exp.get(ly, 0), 2),
         "spent_avg12": round(avg(exp), 2), "months_of_history": len(have12)}
    f["savings_rate"] = round(f["net"] / f["income"], 3) if f["income"] else None
    ytd = sum(v for k, v in exp.items() if k[:4] == str(y) and k <= month)
    ytd_ly = sum(v for k, v in exp.items() if k[:4] == str(y - 1) and k <= _mkey(y - 1, m))
    f["ytd"], f["ytd_last_year"] = round(ytd, 2), round(ytd_ly, 2)
    # category movers against the 12-month average
    movers = []
    for c, d in by_cat.items():
        now, a = d.get(month, 0), avg(d)
        if have12 and abs(now - a) >= 25 and (a == 0 or abs(now - a) / a >= 0.25):
            movers.append({"category": c, "now": round(now, 2), "avg": round(a, 2), "change": round(now - a, 2)})
    movers.sort(key=lambda x: -abs(x["change"]))
    f["category_up"] = [x for x in movers if x["change"] > 0][:3]
    f["category_down"] = [x for x in movers if x["change"] < 0][:3]
    f["top_categories"] = sorted(({"category": c, "amount": round(d.get(month, 0), 2)} for c, d in by_cat.items() if d.get(month)), key=lambda x: -x["amount"])[:5]
    # new payees: never seen in the months before, compared by the first real word of the
    # name so "Vodafone GmbH" and "Vodafone West GmbH" count as the same payee.
    # Only reported once there are at least 3 earlier months to compare with.
    first_word = lambda name: (payee_key(name).split() or [""])[0]  # noqa: E731
    seen_before = {first_word(p) for p, d in by_payee.items() if any(d.get(k) for k in last12)}
    newp = []
    if len(have12) >= 3:
        for p, d in by_payee.items():
            if d.get(month) and first_word(p) and first_word(p) not in seen_before:
                newp.append({"payee": p, "amount": round(sum(d[month]), 2), "count": len(d[month])})
    f["new_payees"] = sorted(newp, key=lambda x: -x["amount"])[:5]
    # subscriptions: same payee, similar amount, 3 months in a row
    started, stopped, changed = [], [], []
    for p, d in by_payee.items():
        series = [d.get(_mkey(y, m - i), []) for i in range(0, 5)]   # month, -1, -2, -3, -4
        def one(lst):
            return lst[0] if len(lst) == 1 else None
        a0, a1, a2, a3 = (one(s) for s in series[:4])
        steady = lambda xs: all(x is not None for x in xs) and max(xs) - min(xs) <= max(1.0, 0.1 * max(xs))  # noqa: E731
        if steady([a1, a2, a3]):
            if not series[0] and not f["partial"]:
                stopped.append({"payee": p, "amount": round(a1, 2)})
            elif a0 is not None and abs(a0 - a1) > max(0.5, 0.04 * a1):
                changed.append({"payee": p, "before": round(a1, 2), "now": round(a0, 2)})
        elif steady([a0, a1, a2]) and not series[3] and not series[4]:
            started.append({"payee": p, "amount": round(a0, 2)})
    f["subscriptions_started"], f["subscriptions_stopped"], f["price_changes"] = started[:5], stopped[:5], changed[:5]
    biggest.sort(reverse=True)
    f["biggest"] = [{"amount": round(a, 2), "payee": p, "category": c, "date": d} for a, p, c, d in biggest[:3]]
    tags = []
    for t, d in by_tag.items():
        now, a = d.get(month, 0), avg(d)
        if now >= 20 or a >= 20:
            tags.append({"tag": t, "now": round(now, 2), "avg": round(a, 2)})
    f["tags"] = sorted(tags, key=lambda x: -x["now"])[:6]
    return f


def headlines(f, lang="en"):
    """Deterministic one-liners - shown when no LLM is available, and given to it as the facts."""
    de = lang == "de"
    y, m = map(int, f["month"].split("-"))
    mon = (MONATE if de else MONTHS)[m - 1]
    out = []
    pct = lambda a, b: f"{(a - b) / b * 100:+.0f} %" if b else ""  # noqa: E731
    if f["spent_avg12"]:
        out.append((f"{mon}: {_eur(f['spent'])} ausgegeben, {pct(f['spent'], f['spent_avg12'])} gegenüber dem Schnitt der letzten {f['months_of_history']} Monate ({_eur(f['spent_avg12'])})."
                    if de else f"{mon}: spent {_eur(f['spent'])}, {pct(f['spent'], f['spent_avg12'])} against the {f['months_of_history']}-month average ({_eur(f['spent_avg12'])})."))
    else:
        out.append(f"{mon}: {_eur(f['spent'])} ausgegeben." if de else f"{mon}: spent {_eur(f['spent'])}.")
    if f["income"]:
        rate = f" ({f['savings_rate'] * 100:.0f} %)" if f["savings_rate"] is not None else ""
        out.append(f"Einnahmen {_eur(f['income'])}, übrig {_eur(f['net'])}{rate}." if de else f"Income {_eur(f['income'])}, left over {_eur(f['net'])}{rate}.")
    if f["spent_last_year"]:
        out.append(f"Vor einem Jahr: {_eur(f['spent_last_year'])} ({pct(f['spent'], f['spent_last_year'])})." if de else f"Same month last year: {_eur(f['spent_last_year'])} ({pct(f['spent'], f['spent_last_year'])}).")
    for c in f["category_up"]:
        out.append(f"{c['category']} stieg auf {_eur(c['now'])} (Schnitt {_eur(c['avg'])})." if de else f"{c['category']} up to {_eur(c['now'])} (usually {_eur(c['avg'])}).")
    for c in f["category_down"]:
        out.append(f"{c['category']} sank auf {_eur(c['now'])} (Schnitt {_eur(c['avg'])})." if de else f"{c['category']} down to {_eur(c['now'])} (usually {_eur(c['avg'])}).")
    for s in f["subscriptions_started"]:
        out.append(f"Neues Abo? {s['payee']} {_eur(s['amount'])} jetzt drei Monate in Folge." if de else f"New subscription? {s['payee']} {_eur(s['amount'])} three months in a row now.")
    for s in f["subscriptions_stopped"]:
        out.append(f"{s['payee']} ({_eur(s['amount'])}/Monat) kam diesen Monat nicht – gekündigt?" if de else f"{s['payee']} ({_eur(s['amount'])}/month) did not charge this month – cancelled?")
    for s in f["price_changes"]:
        out.append(f"Preisänderung: {s['payee']} von {s['before']:.2f} € auf {s['now']:.2f} €.".replace(".", ",").rstrip(",") + "." if de else f"Price change: {s['payee']} from {s['before']:.2f} € to {s['now']:.2f} €.")
    if f["new_payees"]:
        names = ", ".join(f"{p['payee']} ({_eur(p['amount'])})" for p in f["new_payees"][:3])
        out.append(f"Zum ersten Mal bezahlt: {names}." if de else f"First payment to: {names}.")
    if f["biggest"]:
        b = f["biggest"][0]
        out.append(f"Größte Ausgabe: {b['payee']} {_eur(b['amount'])}." if de else f"Biggest expense: {b['payee']} {_eur(b['amount'])}.")
    if f["ytd_last_year"]:
        out.append(f"Seit Januar {_eur(f['ytd'])} ausgegeben, Vorjahr bis {mon}: {_eur(f['ytd_last_year'])} ({pct(f['ytd'], f['ytd_last_year'])})." if de
                   else f"Year to date {_eur(f['ytd'])}, last year by {mon}: {_eur(f['ytd_last_year'])} ({pct(f['ytd'], f['ytd_last_year'])}).")
    return out


def news_text(f, lang, llm):
    """Bullets always come straight from the exact facts. The model only writes a
    one- or two-sentence summary on top, so it cannot relabel or invent figures."""
    lines = headlines(f, lang)
    if not llm:
        return None, lines
    de = lang == "de"
    prompt = (("Fasse den Monat eines Haushalts in EINEM oder ZWEI kurzen, lockeren Sätzen zusammen. "
               "Nur das Wichtigste, keine neuen Zahlen, keine Aufzählung, nichts umdeuten (ein neuer Zahlungsempfänger ist kein Abo). "
               "Keine Ratschläge.\n\nFakten:\n")
              if de else
              ("Sum up a household's month in ONE or TWO short, friendly sentences. Only the most important point, "
               "no new numbers, no list, do not reinterpret anything (a new payee is not a subscription). No advice.\n\nFacts:\n")) + "\n".join("- " + x for x in lines)
    if f["partial"]:
        prompt += "\n(" + ("Der Monat läuft noch." if de else "The month is not over yet.") + ")"
    try:
        txt = " ".join(chat(llm[0], llm[1], prompt).strip().split())
        txt = txt.lstrip("-•* ").strip('"')
        return (txt[:400] or None), lines
    except Exception:  # noqa: BLE001
        return None, lines
