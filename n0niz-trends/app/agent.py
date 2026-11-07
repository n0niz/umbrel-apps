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


# payment services: the real shop is in the description, so learn those per description
GENERIC = {"paypal", "klarna", "sumup", "visa", "mastercard", "stripe", "adyen", "lastschrift", "kartenzahlung", "zahlung",
           "amazon payments", "google", "apple", "unzer", "computop", "mollie", "payone", "sofort", "giropay", "wero", "curve"}


def booking_key(j):
    """(type, payee) – for payment services (PayPal, Klarna, SumUp …) the shop from the description is added."""
    p = payee_key(counterpart(j))
    if p and (p in GENERIC or p.split()[0] in GENERIC):
        d = re.sub(r"(?i)\b(paypal|klarna|sumup|ihr einkauf bei|einkauf bei|zahlung an|purchase at|europe|s a r l|et cie|s c a)\b", " ", j.get("description") or "")
        d = payee_key(re.sub(r"[0-9]{4,}", " ", d))
        if d:
            return (j["type"], f"{p.split()[0]}|{d}")
    return (j["type"], p)


def first_word(name):
    return (payee_key(name).split() or [""])[0]


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
        k = booking_key(j)
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
        k = booking_key(j)
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


def facts(rows, month, currency="EUR", upto_day=None, bills=None, savings=()):
    """rows: Spending Trends rows. month: 'YYYY-MM'. Exact numbers only.
    savings: categories that count as saving/investing (e.g. Crypto), not as spending or income."""
    y, m = map(int, month.split("-"))
    rows = [r for r in rows if r[7] == currency and not (len(r) > 11 and r[11])]
    sav = set(savings or ())
    saved = defaultdict(float)
    for r in rows:
        if r[3] in sav and not (upto_day and r[0] == month and int(r[9][8:10]) > upto_day):
            saved[r[0]] += r[2] if r[1] == "e" else -r[2]
    rows = [r for r in rows if r[3] not in sav]
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
    f["saved"] = round(saved.get(month, 0), 2)
    f["saved_avg12"] = round(sum(saved.get(k, 0) for k in last12) / len(have12), 2) if have12 else 0
    f["savings_categories"] = sorted(sav)
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
    # Firefly subscriptions are the truth; the pattern search above only gives hints
    # for payees that are not linked to a subscription in Firefly.
    billed = {first_word(r[6]) for r in rows if len(r) > 12 and r[12]}
    hint = lambda lst: [x for x in lst if first_word(x["payee"]) not in billed]  # noqa: E731
    f["recurring_hint"] = hint(started)[:4]
    # newly found subscriptions that are not in Firefly yet: detectable at the end of this month,
    # not yet at the end of last month
    f["undiscovered_new"] = []
    if bills is not None:
        end = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
        if upto_day:
            end = date(y, m, upto_day)
        prev_end = date(y, m, 1) - timedelta(days=1)
        allr = [r for r in rows if r[9] <= end.isoformat()]
        now_found = undiscovered(allr, bills, end, currency, savings)
        before = {x["key"] for x in undiscovered([r for r in allr if r[9] <= prev_end.isoformat()], bills, prev_end, currency, savings)}
        f["undiscovered_new"] = [{"payee": x["name"], "amount": x["typical"], "freq": x["freq"], "count": x["count"], "first": x["first"]}
                                 for x in now_found if x["key"] not in before][:4]
        if f["undiscovered_new"]:
            new_keys = {first_word(x["payee"]) for x in f["undiscovered_new"]}
            f["recurring_hint"] = [x for x in f["recurring_hint"] if first_word(x["payee"]) not in new_keys]
    f["subscriptions_started"] = f["subscriptions_stopped"] = []
    f["price_changes"] = hint(changed)[:4]
    f["bills"] = None
    if bills is not None:
        paid_now = defaultdict(float)
        for r in rows:
            if len(r) > 12 and r[12] and r[0] == month and r[1] == "e":
                paid_now[r[12]] += r[2]
        active = [b for b in bills if b["active"]]
        due = [b for b in active if b["pay_dates"]]
        missed = [b["name"] for b in due if not paid_now.get(b["name"]) and not f["partial"]]
        over = [{"name": b["name"], "paid": round(paid_now[b["name"]], 2), "max": b["max"]}
                for b in active if paid_now.get(b["name"]) and b["max"] and paid_now[b["name"]] > b["max"] + 0.01]
        new = [b["name"] for b in bills if b["created"][:7] == month]
        f["bills"] = {"active": len(active), "due": len(due), "paid": len(paid_now), "paid_sum": round(sum(paid_now.values()), 2),
                      "missed": missed[:5], "over": over[:5], "new": new[:5]}
    else:
        f["subscriptions_stopped"] = stopped[:5]
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
    if f.get("saved"):
        cats = ", ".join(f["savings_categories"])
        avg_s = f" (Schnitt {_eur(f['saved_avg12'])})" if de and f.get("saved_avg12") else f" (usually {_eur(f['saved_avg12'])})" if f.get("saved_avg12") else ""
        out.append(f"Gespart und angelegt ({cats}): {_eur(f['saved'])}{avg_s}." if de else f"Saved and invested ({cats}): {_eur(f['saved'])}{avg_s}.")
    if f["spent_last_year"]:
        out.append(f"Vor einem Jahr: {_eur(f['spent_last_year'])} ({pct(f['spent'], f['spent_last_year'])})." if de else f"Same month last year: {_eur(f['spent_last_year'])} ({pct(f['spent'], f['spent_last_year'])}).")
    for c in f["category_up"]:
        out.append(f"{c['category']} stieg auf {_eur(c['now'])} (Schnitt {_eur(c['avg'])})." if de else f"{c['category']} up to {_eur(c['now'])} (usually {_eur(c['avg'])}).")
    for c in f["category_down"]:
        out.append(f"{c['category']} sank auf {_eur(c['now'])} (Schnitt {_eur(c['avg'])})." if de else f"{c['category']} down to {_eur(c['now'])} (usually {_eur(c['avg'])}).")
    b = f.get("bills")
    if b and b["active"]:
        out.append((f"Abos in Firefly: {b['paid']} bezahlt, zusammen {_eur(b['paid_sum'])}." if de else f"Subscriptions in Firefly: {b['paid']} paid, {_eur(b['paid_sum'])} together."))
        for n in b["missed"]:
            out.append(f"Abo „{n}“ war fällig, wurde aber nicht bezahlt." if de else f"Subscription “{n}” was due but not paid.")
        for o in b["over"]:
            out.append(f"Abo „{o['name']}“ kostete {o['paid']:.2f} €, mehr als die eingetragenen {o['max']:.2f} €.".replace(".", ",").replace(",“", ".“") if de
                       else f"Subscription “{o['name']}” cost {o['paid']:.2f} €, more than the {o['max']:.2f} € set in Firefly.")
        for n in b["new"]:
            out.append(f"Neu in Firefly angelegt: Abo „{n}“." if de else f"New subscription set up in Firefly: “{n}”.")
    FQ = {"weekly": ("wöchentlich", "weekly"), "monthly": ("monatlich", "monthly"), "quarterly": ("vierteljährlich", "quarterly"),
          "half-year": ("halbjährlich", "every six months"), "yearly": ("jährlich", "yearly")}
    for s in f.get("undiscovered_new", []):
        fq = FQ.get(s["freq"], (s["freq"], s["freq"]))
        out.append(f"Unentdecktes Abo: {s['payee']} bucht {fq[0]} etwa {_eur(s['amount'])} ({s['count']} Zahlungen seit {s['first'][8:10]}.{s['first'][5:7]}.) – in Firefly noch kein Abo." if de
                   else f"Undiscovered subscription: {s['payee']} charges about {_eur(s['amount'])} {fq[1]} ({s['count']} payments since {s['first']}) – not set up in Firefly yet.")
    for s in f.get("recurring_hint", []):
        out.append(f"Sieht regelmäßig aus: {s['payee']} {_eur(s['amount'])} drei Monate in Folge – in Firefly noch kein Abo." if de
                   else f"Looks recurring: {s['payee']} {_eur(s['amount'])} three months in a row – not set up as a subscription in Firefly yet.")
    for s in f["subscriptions_stopped"]:
        out.append(f"{s['payee']} ({_eur(s['amount'])}/Monat) kam diesen Monat nicht – gekündigt?" if de else f"{s['payee']} ({_eur(s['amount'])}/month) did not charge this month – cancelled?")
    for s in f["price_changes"]:
        out.append(f"Betrag geändert: {s['payee']} von {s['before']:.2f} € auf {s['now']:.2f} €.".replace(".", ",").rstrip(",") + "." if de else f"Price change: {s['payee']} from {s['before']:.2f} € to {s['now']:.2f} €.")
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


def _last_full_month(month, partial):
    y, m = map(int, month.split("-"))
    return _mkey(y, m - 1) if partial else month


def year_stories(rows, month, currency="EUR", partial=False, savings=(), top=3, period=12):
    """The biggest changes, as newspaper stories. period = 12 (the last 12 finished months
    against the 12 before), 6 (6 against 6) or 1 (the last finished month against the
    average of the 12 months before it). Kinds:
      cat      a spending category got dearer or cheaper (with the payee that drove it,
               and for shopping-type categories whether it was more trips or a bigger basket)
      payee    a regular payment changed (rent, energy, insurance, salary …)
      new      a cost that did not exist before and now comes back every month
      gone     a regular cost that stopped
      income   income went up or down
    Scored by the change in euros per month; exact figures only."""
    end = _last_full_month(month, partial)
    y, m = map(int, end.split("-"))
    n = period if period in (1, 6, 12) else 12
    nb = 12 if n == 1 else n
    now_w = [_mkey(y, m - i) for i in range(n - 1, -1, -1)]           # oldest .. newest
    before_w = [_mkey(y, m - n - i) for i in range(nb - 1, -1, -1)]
    span = before_w + now_w
    sav = set(savings or ())
    rows = [r for r in rows if r[7] == currency and not (len(r) > 11 and r[11]) and r[3] not in sav and r[0] in span]
    have = {r[0] for r in rows}
    if sum(1 for k in before_w if k in have) < max(1, nb // 2) or sum(1 for k in now_w if k in have) < max(1, n // 2):
        return []
    fw = lambda name: (payee_key(name).split() or [""])[0]       # noqa: E731
    cat_m = defaultdict(lambda: defaultdict(float))
    cat_n = defaultdict(lambda: defaultdict(int))
    pay_m = defaultdict(lambda: defaultdict(float))
    pay_name, pay_cat, pay_kind = defaultdict(Counter), defaultdict(Counter), {}
    inc_m = defaultdict(float)
    for r in rows:
        k = fw(r[6] or r[10])
        if r[1] == "e":
            c = r[3] or "(no category)"
            cat_m[c][r[0]] += r[2]
            cat_n[c][r[0]] += 1
        else:
            inc_m[r[0]] += r[2]
        if k:
            key = (r[1], k)
            pay_m[key][r[0]] += r[2]
            pay_name[key][r[6] or r[10]] += 1
            pay_cat[key][r[3] or ""] += 1
    pm = lambda d, w: sum(d.get(k, 0) for k in w) / len(w)                       # noqa: E731
    active = lambda d, w: sum(1 for k in w if d.get(k))                            # noqa: E731
    series = lambda d: [round(d.get(k, 0), 2) for k in span]                       # noqa: E731
    cands = []
    for (kind, k), d in pay_m.items():
        name = pay_name[(kind, k)].most_common(1)[0][0]
        cat = pay_cat[(kind, k)].most_common(1)[0][0]
        a_now, a_bef = active(d, now_w), active(d, before_w)
        last3 = [d.get(x, 0) for x in now_w[-3:]]
        reg_b, reg_n = max(2, round(nb * 2 / 3)), max(1, round(n * 2 / 3))
        base = {"name": name, "key": k, "category": cat, "series": series(d), "income": kind == "i"}
        if n >= 3 and a_bef == 0 and a_now >= 3 and sum(1 for v in last3 if v) >= 2 and kind == "e":
            run = sum(last3) / sum(1 for v in last3 if v)
            first = next(x for x in now_w if d.get(x))
            cands.append({**base, "kind": "new", "score": run, "now": round(run, 2), "before": 0, "since": first,
                          "year_total": round(sum(d.get(x, 0) for x in now_w), 2)})
        elif a_bef >= max(nb // 2, 5 if n == 1 else 0) and not any(last3) and kind == "e" and (n >= 3 or a_bef >= 10):
            run = sum(d.get(x, 0) for x in before_w) / a_bef
            last = max(x for x in span if d.get(x))
            cands.append({**base, "kind": "gone", "score": run, "now": 0, "before": round(run, 2), "last": last})
        elif a_bef >= reg_b and a_now >= reg_n:                             # regular payment, compare per month paid
            now_v = sum(d.get(x, 0) for x in now_w) / a_now
            bef_v = sum(d.get(x, 0) for x in before_w) / a_bef
            dpm = pm(d, now_w) - pm(d, before_w)
            if abs(now_v - bef_v) >= 5 and abs(now_v - bef_v) / max(bef_v, 1) >= 0.05:
                cands.append({**base, "kind": "payee", "score": abs(dpm), "now": round(now_v, 2), "before": round(bef_v, 2),
                              "delta_pm": round(dpm, 2)})
    for c, d in cat_m.items():
        now_v, bef_v = pm(d, now_w), pm(d, before_w)
        dlt = now_v - bef_v
        if abs(dlt) < 15 or abs(dlt) / max(bef_v, 1) < 0.10:
            continue
        if n == 1 and (active(d, before_w) < 8 or not d.get(now_w[0])):
            continue                                                    # one month: only categories you pay every month
        drivers = []
        for (kind, k), pdm in pay_m.items():
            if kind == "e" and pay_cat[(kind, k)].most_common(1)[0][0] == (c if c != "(no category)" else ""):
                pd = pm(pdm, now_w) - pm(pdm, before_w)
                if pd * dlt > 0:
                    drivers.append((abs(pd), pay_name[(kind, k)].most_common(1)[0][0], k, round(pm(pdm, now_w), 2), round(pm(pdm, before_w), 2)))
        drivers.sort(reverse=True)
        n_now, n_bef = sum(cat_n[c].get(x, 0) for x in now_w) / len(now_w), sum(cat_n[c].get(x, 0) for x in before_w) / len(before_w)
        cands.append({"kind": "cat", "name": c, "category": c, "score": abs(dlt), "now": round(now_v, 2), "before": round(bef_v, 2),
                      "delta_pm": round(dlt, 2), "series": series(d), "income": False,
                      "drivers": [{"name": n, "key": k, "now": a, "before": b, "share": round(s / abs(dlt), 2)} for s, n, k, a, b in drivers[:2]],
                      "trips_now": round(n_now, 1), "trips_before": round(n_bef, 1),
                      "basket_now": round(now_v / n_now, 2) if n_now else 0, "basket_before": round(bef_v / n_bef, 2) if n_bef else 0})
    inc_now, inc_bef = pm(inc_m, now_w), pm(inc_m, before_w)
    if abs(inc_now - inc_bef) >= 50 and abs(inc_now - inc_bef) / max(inc_bef, 1) >= 0.03:
        cands.append({"kind": "income", "name": "Income", "category": "", "score": abs(inc_now - inc_bef) * 0.9, "now": round(inc_now, 2),
                      "before": round(inc_bef, 2), "delta_pm": round(inc_now - inc_bef, 2), "series": series(inc_m), "income": True})
    cands.sort(key=lambda s: -s["score"])
    picked, used_keys, used_cats = [], set(), set()
    for s in cands:
        if s["kind"] == "cat":
            main = s["drivers"][0] if s["drivers"] else None
            if s["category"] in used_cats or (main and main["key"] in used_keys and main["share"] >= 0.6):
                continue
            used_cats.add(s["category"])
            used_keys.update(dv["key"] for dv in s["drivers"][:1])
        elif s["kind"] == "income":
            if "income" in used_cats:
                continue
            used_cats.add("income")
        else:
            if s["key"] in used_keys:
                continue
            used_keys.add(s["key"])
            if s["income"]:
                used_cats.add("income")
        picked.append(s)
        if len(picked) == top:
            break
    for s in picked:
        s["good"] = (s.get("delta_pm", s["now"] - s["before"]) > 0) == s["income"]
        s.pop("key", None)
        for dv in s.get("drivers", []):
            dv.pop("key", None)
    for s in picked:
        s["period"] = n
    return {"stories": picked, "window": [now_w[0], now_w[-1]], "before": [before_w[0], before_w[-1]], "months": span, "period": n}


def story_text(s, lang="en"):
    """Deterministic headline, deck and copy for one story."""
    de = lang == "de"
    mon = lambda k: (MONATE if de else MONTHS)[int(k[5:7]) - 1] + " " + k[:4]                   # noqa: E731
    e = lambda v: _eur(v).replace(" €", " €")                                                   # noqa: E731
    pct = lambda a, b: f"{(a - b) / b * 100:+.0f} %" if b else ""                               # noqa: E731
    n, k = s["name"], s["kind"]
    yr = abs(s.get("delta_pm", s["now"] - s["before"])) * 12
    if k == "cat":
        up = s["delta_pm"] > 0
        head = (f"{n}: {e(s['delta_pm'])} mehr im Monat" if up else f"{n}: {e(-s['delta_pm'])} weniger im Monat") if de else \
               (f"{n} costs {e(s['delta_pm'])} more a month" if up else f"{n} costs {e(-s['delta_pm'])} less a month")
        deck = (f"Im Schnitt {e(s['now'])} statt {e(s['before'])} pro Monat ({pct(s['now'], s['before'])}), aufs Jahr {e(yr)} {'mehr' if up else 'weniger'}." if de else
                f"On average {e(s['now'])} instead of {e(s['before'])} a month ({pct(s['now'], s['before'])}), {e(yr)} {'more' if up else 'less'} over a year.")
        body = []
        if s.get("trips_before") and s.get("trips_now"):
            more_trips = s["trips_now"] > s["trips_before"] * 1.08
            bigger = s["basket_now"] > s["basket_before"] * 1.08
            fewer_trips = s["trips_now"] < s["trips_before"] * 0.92
            smaller = s["basket_now"] < s["basket_before"] * 0.92
            t = (f"{s['trips_now']:.0f} statt {s['trips_before']:.0f} Buchungen im Monat, im Schnitt {s['basket_now']:.2f} € statt {s['basket_before']:.2f} € pro Einkauf".replace(".", ",") if de else
                 f"{s['trips_now']:.0f} instead of {s['trips_before']:.0f} bookings a month, {s['basket_now']:.2f} € instead of {s['basket_before']:.2f} € each on average")
            why = ""
            if up and bigger and not more_trips:
                why = " – nicht öfter, sondern teurer eingekauft." if de else " – not more often, but each time costs more."
            elif up and more_trips and not bigger:
                why = " – öfter, nicht teurer." if de else " – more often, not dearer."
            elif not up and smaller and not fewer_trips:
                why = " – gleich oft, aber günstiger." if de else " – just as often, but cheaper each time."
            elif not up and fewer_trips and not smaller:
                why = " – seltener, nicht billiger." if de else " – less often, not cheaper."
            body.append(t + (why or "."))
        for dv in s.get("drivers", [])[:2]:
            if dv["share"] >= 0.25:
                body.append((f"Treiber: {dv['name']} mit {e(dv['now'])} statt {e(dv['before'])} im Monat." if de else
                             f"Main driver: {dv['name']}, {e(dv['now'])} instead of {e(dv['before'])} a month."))
    elif k == "payee":
        up = s["now"] > s["before"]
        if s["income"]:
            head = (f"{n} zahlt {e(abs(s['now'] - s['before']))} {'mehr' if up else 'weniger'}" if de else f"{n} pays {e(abs(s['now'] - s['before']))} {'more' if up else 'less'}")
        else:
            head = (f"{n} {'teurer' if up else 'günstiger'}: {e(s['now'])} statt {e(s['before'])}" if de else f"{n} {'up' if up else 'down'}: {e(s['now'])} instead of {e(s['before'])}")
        deck = (f"Regelmäßige Zahlung, im Schnitt pro Monat {pct(s['now'], s['before'])} – aufs Jahr {e(yr)} {'mehr' if up else 'weniger'}." if de else
                f"A regular payment, {pct(s['now'], s['before'])} per month on average – {e(yr)} {'more' if up else 'less'} over a year.")
        body = [f"Kategorie: {s['category']}." if de else f"Category: {s['category']}."] if s.get("category") else []
    elif k == "new":
        head = (f"Neuer Kostenpunkt: {n}" if de else f"New cost: {n}")
        deck = (f"Seit {mon(s['since'])} jeden Monat, zuletzt rund {e(s['now'])} im Monat – im Zeitraum davor gab es das noch nicht." if de else
                f"Every month since {mon(s['since'])}, lately about {e(s['now'])} a month – it did not exist in the period before.")
        body = [(f"Bisher {e(s['year_total'])} insgesamt" + (f", Kategorie {s['category']}." if s.get("category") else ".")) if de else
                (f"{e(s['year_total'])} so far" + (f", category {s['category']}." if s.get("category") else "."))]
    elif k == "gone":
        head = (f"{n} fällt weg" if de else f"{n} has stopped")
        deck = (f"Früher rund {e(s['before'])} im Monat, zuletzt bezahlt im {mon(s['last'])} – spart etwa {e(s['before'] * 12)} im Jahr." if de else
                f"Used to be about {e(s['before'])} a month, last paid in {mon(s['last'])} – that saves about {e(s['before'] * 12)} a year.")
        body = [f"Kategorie: {s['category']}." if de else f"Category: {s['category']}."] if s.get("category") else []
    else:  # income
        up = s["delta_pm"] > 0
        head = (f"Einnahmen {'steigen' if up else 'sinken'} um {e(abs(s['delta_pm']))} im Monat" if de else f"Income {'up' if up else 'down'} {e(abs(s['delta_pm']))} a month")
        deck = (f"Im Schnitt {e(s['now'])} statt {e(s['before'])} pro Monat ({pct(s['now'], s['before'])})." if de else
                f"On average {e(s['now'])} instead of {e(s['before'])} a month ({pct(s['now'], s['before'])}).")
        body = []
    return {"headline": head, "deck": deck, "body": body}


def news_text(f, lang, llm, stories=()):
    """Bullets always come straight from the exact facts. The model only writes a
    one- or two-sentence summary on top, so it cannot relabel or invent figures."""
    lines = headlines(f, lang)
    year = [f"{t['headline']}. {t['deck']}" for t in stories]
    if not llm:
        return None, lines
    de = lang == "de"
    prompt = (("Fasse den Monat eines Haushalts in EINEM oder ZWEI kurzen, lockeren Sätzen zusammen. "
               "Nur das Wichtigste, keine neuen Zahlen, keine Aufzählung, nichts umdeuten (ein neuer Zahlungsempfänger ist kein Abo). "
               "Keine Ratschläge.\n\nFakten:\n")
              if de else
              ("Sum up a household's month in ONE or TWO short, friendly sentences. Only the most important point, "
               "no new numbers, no list, do not reinterpret anything (a new payee is not a subscription). No advice.\n\nFacts:\n")) + "\n".join("- " + x for x in lines)
    if year:
        prompt += ("\n\nDie drei größten Veränderungen im Jahresvergleich (dürfen erwähnt werden):\n" if de else
                   "\n\nThe three biggest changes over the year (you may mention them):\n") + "\n".join("- " + x for x in year)
    if f["partial"]:
        prompt += "\n(" + ("Der Monat läuft noch." if de else "The month is not over yet.") + ")"
    try:
        txt = " ".join(chat(llm[0], llm[1], prompt).strip().split())
        txt = txt.lstrip("-•* ").strip('"')
        return (txt[:400] or None), lines
    except Exception:  # noqa: BLE001
        return None, lines


# ----------------------------------------------------- undiscovered subscriptions ---
# interval classes: (name, median gap in days, allowed deviation per gap in days)
_CLASSES = [("weekly", 7, 2), ("monthly", 30.4, 6), ("quarterly", 91, 12), ("half-year", 182, 20), ("yearly", 365, 30)]


def undiscovered(rows, bills=None, today=None, currency="EUR", savings=(), months=12):
    """Payments that come back at a steady interval with a steady amount over the last `months`
    months, but are not set up as a Firefly subscription yet. Strict on purpose: a restaurant you
    visit often has uneven gaps and amounts and stays out."""
    today = today or date.today()
    start = (today - timedelta(days=int(months * 30.5) + 20)).isoformat()
    sav = set(savings or ())
    exp = [r for r in rows if r[7] == currency and r[1] == "e" and r[9] >= start and r[9] <= today.isoformat()
           and not (len(r) > 11 and r[11]) and r[3] not in sav]
    # what Firefly already knows: per subscription the payee words and its amount range
    known = []
    for b in bills or []:
        keys = {first_word(b["name"])} | {first_word(r[6]) for r in rows if len(r) > 12 and r[12] == b["name"]}
        known.append((keys - {""}, b.get("min") or 0, b.get("max") or 0))
    linked = {first_word(r[6]) for r in rows if len(r) > 12 and r[12]}
    groups = defaultdict(list)
    for r in exp:
        if len(r) > 12 and r[12]:
            continue                                         # already linked to a subscription
        k = first_word(r[6]) or first_word(r[10])
        if k:
            groups[k].append(r)
    out = []
    for k, hits in groups.items():
        hits.sort(key=lambda r: r[9])
        # one payment per day at most (a split booking is one charge)
        days = {}
        for r in hits:
            days[r[9]] = days.get(r[9], 0) + r[2]
        ds = sorted(days)
        if len(ds) < 2:
            continue
        dd = [date.fromisoformat(x) for x in ds]
        gaps = [(b - a).days for a, b in zip(dd, dd[1:])]
        g = sorted(gaps)[len(gaps) // 2]
        cls = next((c for c in _CLASSES if abs(g - c[1]) <= c[2] * 1.5), None)
        if not cls:
            continue
        name, step, tol = cls
        need = {"weekly": 6, "monthly": 3, "quarterly": 3, "half-year": 2, "yearly": 2}[name]
        if len(ds) < need:
            continue
        regular = sum(abs(x - step) <= tol for x in gaps) / len(gaps)
        amts = [days[x] for x in ds]
        med = sorted(amts)[len(amts) // 2]
        steady = sum(abs(a - med) <= max(1.0, 0.1 * med) for a in amts) / len(amts)
        if regular < 0.75 or steady < 0.75 or med < 1:
            continue
        last = dd[-1]
        if (today - last).days > step + max(tol * 2, 10):
            continue                                         # stopped
        if any(k in keys and (not mx or mn * 0.85 <= med <= mx * 1.15) for keys, mn, mx in known):
            continue                                         # same payee and amount as a Firefly subscription
        cur = amts[-1]
        nxt = (last + timedelta(days=round(step))).isoformat()
        per_month = med / FREQ_MONTHS[name]
        out.append({"key": k, "name": hits[-1][6], "freq": name, "amount": round(cur, 2), "typical": round(med, 2),
                    "min": round(min(amts), 2), "max": round(max(amts), 2), "count": len(ds), "first": ds[0], "last": ds[-1],
                    "next": nxt, "per_month": round(per_month, 2), "per_year": round(per_month * 12, 2),
                    "category": hits[-1][3], "account": hits[-1][8], "dates": ds,
                    "also_linked": k in linked, "score": round((regular + steady) / 2, 2)})
    out.sort(key=lambda x: -x["per_year"])
    return out


# ------------------------------------------------------------- subscriptions ---
SUB_TAGS = {"subscription", "subscriptions", "abo", "abos", "abonnement"}
FREQ_MONTHS = {"weekly": 0.25, "monthly": 1, "quarterly": 3, "half-year": 6, "yearly": 12}


def subscriptions(rows, bills=None, today=None, currency="EUR", savings=()):
    """Everything that looks like a subscription: Firefly subscriptions, bookings tagged
    Subscription/Abo, and payees charging a similar amount at a regular interval.
    Savings categories (e.g. a BTC savings plan) are left out."""
    today = today or date.today()
    sav = set(savings or ())
    rows = [r for r in rows if r[7] == currency and r[1] == "e" and not (len(r) > 11 and r[11]) and r[3] not in sav]
    by_payee = defaultdict(list)
    for r in rows:
        by_payee[first_word(r[6]) or r[6]].append(r)
    out = {}

    def entry(key, name, hits, source, freq=None, expected=None, bill=None):
        hits = sorted(hits, key=lambda r: r[9])
        last = hits[-1] if hits else None
        months = sorted({r[0] for r in hits})
        if not freq:
            gaps = []
            ds = [date.fromisoformat(r[9]) for r in hits]
            for a, b in zip(ds, ds[1:]):
                if (b - a).days > 3:
                    gaps.append((b - a).days)
            g = sorted(gaps)[len(gaps) // 2] if gaps else 30
            freq = "yearly" if g > 300 else "half-year" if g > 150 else "quarterly" if g > 75 else "weekly" if g < 10 else "monthly"
        amount = last[2] if last else (bill["max"] if bill else 0)
        per_month = amount / FREQ_MONTHS.get(freq, 1)
        if last:
            step = {"weekly": 7, "monthly": 31, "quarterly": 92, "half-year": 183, "yearly": 366}.get(freq, 31)
            nxt = expected or (date.fromisoformat(last[9]) + timedelta(days=step)).isoformat()
            overdue = (today - date.fromisoformat(last[9])).days > step + 10
        else:
            nxt, overdue = expected, False
        prev = hits[-2][2] if len(hits) > 1 else None
        out[key] = {"name": name, "payee": last[6] if last else name, "source": source, "freq": freq,
                    "amount": round(amount, 2), "per_month": round(per_month, 2), "per_year": round(per_month * 12, 2),
                    "first": hits[0][9] if hits else None, "last": last[9] if last else None, "next": nxt, "count": len(hits), "months": len(months),
                    "category": last[3] if last else "", "account": last[8] if last else "",
                    "changed": round(amount - prev, 2) if prev is not None and abs(amount - prev) > max(0.5, 0.04 * prev) else 0,
                    "active": not overdue and (bool(last) or source == "firefly"),
                    "range": [bill["min"], bill["max"]] if bill else None}

    # 1) Firefly subscriptions: the bookings Firefly linked to them
    for b in bills or []:
        if not b.get("active", True):
            continue
        hits = [r for r in rows if len(r) > 12 and r[12] == b["name"]]
        exp = sorted(b.get("pay_dates") or [])
        entry("bill:" + b["name"], b["name"], hits, "firefly", b.get("freq") or None, exp[0] if exp else None, b)
    linked = {first_word(r[6]) for r in rows if len(r) > 12 and r[12]}
    # 2) tagged by you
    for key, hits in by_payee.items():
        tagged = [r for r in hits if {t.lower() for t in r[5]} & SUB_TAGS]
        if tagged and key not in linked:
            entry("tag:" + key, tagged[-1][6], tagged, "tag")
    # 3) pattern: similar amount in at least 3 of the last 6 months, or once a year twice
    six = {f"{(today.replace(day=1) - timedelta(days=31 * i)).strftime('%Y-%m')}" for i in range(0, 7)}
    for key, hits in by_payee.items():
        if key in linked or ("tag:" + key) in out or not key:
            continue
        recent = sorted([r for r in hits if r[0] in six], key=lambda r: r[9])
        if len({r[0] for r in recent}) >= 3:
            amts = [r[2] for r in recent]
            steady = lambda xs: len(xs) >= 3 and max(xs) - min(xs) <= max(1.0, 0.1 * max(xs))  # noqa: E731
            # steady over the last 3 payments, or steady before and the latest one changed price (by at most half)
            price_change = steady(amts[-4:-1]) and abs(amts[-1] - amts[-2]) <= 0.5 * amts[-2]
            if (steady(amts[-3:]) or price_change) and len(recent) <= len({r[0] for r in recent}) + 1:
                entry("pat:" + key, recent[-1][6], recent, "pattern")
    items = sorted(out.values(), key=lambda x: (not x["active"], -x["per_month"]))
    act = [x for x in items if x["active"]]
    return {"items": items, "per_month": round(sum(x["per_month"] for x in act), 2),
            "per_year": round(sum(x["per_year"] for x in act), 2), "active": len(act)}
