"""Amounts and deadlines found in the text of a document (German and English).

extract(text, doc_date) -> {"amounts": [...], "deadlines": [...], "notes": [...]}
Every hit carries a score; the first entry of each list is the best guess.
No AI needed – this runs in milliseconds, so the whole archive can be scanned.
"""
import calendar
import datetime as dt
import re

MONTHS = {
    "januar": 1, "jan": 1, "january": 1, "februar": 2, "feb": 2, "february": 2, "märz": 3, "maerz": 3, "mär": 3,
    "mrz": 3, "march": 3, "mar": 3, "april": 4, "apr": 4, "mai": 5, "may": 5, "juni": 6, "jun": 6, "june": 6,
    "juli": 7, "jul": 7, "july": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "oktober": 10,
    "okt": 10, "october": 10, "oct": 10, "november": 11, "nov": 11, "dezember": 12, "dez": 12, "december": 12, "dec": 12,
}
NUMWORDS = {"ein": 1, "eine": 1, "einem": 1, "einer": 1, "eines": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5,
            "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "zwölf": 12, "vierzehn": 14, "one": 1,
            "two": 2, "three": 3, "four": 4, "six": 6, "fourteen": 14, "thirty": 30, "dreißig": 30}

# ------------------------------------------------------------------ amounts

AMOUNT_RE = re.compile(
    r"(?P<pre>€|EUR|Euro)?\s?(?P<sign>[-–])?\s?"
    r"(?<![\d.,])(?P<num>\d{1,3}(?:[. ' ]\d{3})+,\d{2}|\d+,\d{2}|\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2})"
    r"(?!\d|[.,]\d)(?:\s?(?P<post>€|EUR|Euro|EURO))?", re.I)

AMOUNT_WORDS = [
    # (pattern, score, label)
    (r"gesamtbetrag|rechnungsbetrag|endbetrag|zahlbetrag|zahlungsbetrag|zu zahlen(?:der betrag)?|"
     r"gesamtsumme|summe brutto|bruttobetrag|gesamt brutto|brutto gesamt|amount due|total due|grand total|"
     r"offener betrag|offene forderung|forderungsbetrag|nachzahlung|zahllast|abzubuchen|abbuchungsbetrag|"
     r"überweisungsbetrag|zahlungsaufforderung", 6, None),
    (r"erstattung|guthaben|gutschrift|auszahlungsbetrag|rückzahlung|refund", 5, "refund"),
    (r"gesamt|total|summe|endsumme|betrag|brutto|kaufpreis|preis|kosten|gebühr|beitrag|miete|abschlag|rate|"
     r"monatsbeitrag|jahresbeitrag|honorar|vergütung|gehalt|auszahlung|netto-?verdienst|auszahlungsbetrag", 3, None),
]
AMOUNT_BAD = re.compile(r"mwst|ust\b|umsatzsteuer|mehrwertsteuer|steuer \d|vat|zwischensumme|subtotal|netto(?!-?verdienst)|"
                        r"rabatt|skonto|% |\d+\s?%|iban|bic|konto|kunden-?nr|rechnungs-?nr|steuer-?nr|telefon|tel\.|fax", re.I)
PERIODS = [(re.compile(r"monatlich|mtl\.|pro monat|je monat|/\s?monat|per month|monthly|im monat", re.I), "month"),
           (re.compile(r"vierteljährlich|quartal|pro quartal|quarterly", re.I), "quarter"),
           (re.compile(r"jährlich|pro jahr|je jahr|/\s?jahr|per year|annual|jahresbeitrag", re.I), "year")]


def parse_num(s):
    s = s.replace(" ", "").replace("'", "").replace(" ", "")
    if re.search(r",\d{2}$", s):  # German 1.234,56
        s = s.replace(".", "").replace(",", ".")
    else:  # English 1,234.56
        s = s.replace(",", "")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def amounts(text):
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        for m in AMOUNT_RE.finditer(line):
            cur = m.group("pre") or m.group("post")
            v = parse_num(m.group("num"))
            if v is None or v == 0 or v > 5_000_000:
                continue
            before = line[max(0, m.start() - 70):m.start()]
            prev = lines[i - 1][-70:] if i and len(line[:m.start()].strip()) < 3 else ""
            ctx = (prev + " " + before).lower()
            score, label, kind = 0.0, None, None
            if cur:
                score += 2
            for pat, w, k in AMOUNT_WORDS:
                hit = re.search(pat, ctx, re.I)
                if hit:
                    score += w
                    label = label or hit.group(0)
                    kind = kind or k
                    break
            if AMOUNT_BAD.search(before[-35:]):
                score -= 4
            if not cur and not label:
                continue  # a bare "12,50" without € or a label is too often a measurement or a number
            period = next((p for rx, p in PERIODS if rx.search(line) or rx.search(ctx)), None)
            out.append({"v": -v if m.group("sign") else v, "c": "EUR" if (cur or "").upper() in ("€", "EUR", "EURO", "") else cur.upper(),
                        "label": (label or "").strip()[:40] or None, "kind": kind, "p": period, "score": score,
                        "snip": re.sub(r"\s+", " ", line.strip())[:120], "line": i})
    if not out:
        return []
    # totals are usually the largest labelled amount – small bonus for size, larger for appearing late in the text
    top = max(abs(a["v"]) for a in out)
    n = max(len(lines), 1)
    for a in out:
        a["score"] += (abs(a["v"]) / top) * 1.5 + (a["line"] / n) * 0.8
    out.sort(key=lambda a: -a["score"])
    seen, res = set(), []
    for a in out:
        k = (a["v"], a["c"])
        if k in seen:
            continue
        seen.add(k)
        a.pop("line", None)
        a["score"] = round(a["score"], 1)
        res.append(a)
    return res[:6]


# ------------------------------------------------------------------ deadlines

DATE_RES = [
    re.compile(r"(?<![\d.])(?P<d>[0-3]?\d)\.\s?(?P<m>[01]?\d)\.\s?(?P<y>(?:19|20)?\d\d)(?![\d])"),
    re.compile(r"(?<!\d)(?P<y>(?:19|20)\d\d)-(?P<m>[01]\d)-(?P<d>[0-3]\d)(?!\d)"),
    re.compile(r"(?<!\d)(?P<d>[0-3]?\d)\.?\s(?P<mn>[A-Za-zäÄ]{3,9})\.?\s(?P<y>(?:19|20)\d\d)", re.I),
    re.compile(r"(?P<mn>[A-Za-z]{3,9})\.?\s(?P<d>[0-3]?\d)(?:st|nd|rd|th)?,?\s(?P<y>(?:19|20)\d\d)", re.I),
]
DEADLINE_WORDS = [
    # (pattern, score, kind, label)
    (r"zahlbar bis|zahlbar spätestens|bitte (?:überweisen|zahlen) sie .{0,40}bis|fällig (?:am|zum|bis)|fälligkeit(?:sdatum)?|"
     r"zahlungsziel|zahlungsfrist|due date|payment due|pay by|zu zahlen bis|bis spätestens|spätestens (?:am|bis)", 7, "payment", "Payment due"),
    (r"abbuchung (?:am|erfolgt)|lastschrift(?:einzug)? (?:am|zum)|wird (?:am|zum) .{0,20}abgebucht|einzug am", 5, "debit", "Direct debit"),
    (r"einspruch|widerspruch|rechtsbehelf|klagefrist|beschwerde", 6, "objection", "Objection deadline"),
    (r"kündbar (?:zum|bis)|kündigung (?:zum|bis)|kündigungs(?:frist|termin)|vertragsende|endet am|endet zum|läuft .{0,10}bis|"
     r"laufzeit bis|mindestlaufzeit|befristet bis|gültig bis|ablauf(?:datum)?|valid until|expires?|verlängert sich", 5, "contract", "Contract / validity ends"),
    (r"frist|deadline|bis zum|bis spätestens|einzureichen bis|vorzulegen bis|rückmeldung bis|antwort bis|melden sie sich bis", 4, "deadline", "Deadline"),
    (r"termin|appointment|am .{0,15}um \d{1,2}[:.]\d\d|uhrzeit", 3, "appointment", "Appointment"),
]
DATE_BAD = re.compile(r"geburts|geb\.|born|datum der rechnung|rechnungsdatum|lieferdatum|leistungsdatum|leistungszeitraum|"
                      r"invoice date|ausgestellt|erstellt am|druckdatum|stand:|seit|vom\b|zeitraum|abrechnungszeitraum|kundennr", re.I)
REL_RE = re.compile(
    r"(?:innerhalb|binnen|within|in den nächsten)\s(?:von\s)?(?P<n>\d{1,3}|[a-zäöüß]+)\s(?P<u>tag|tage|tagen|werktag(?:e|en)?|woche|wochen|monat|monate|monaten|monats|days?|weeks?|months?)"
    r"|zahlungsziel:?\s(?P<n2>\d{1,3})\s(?:tage|days)|(?P<n3>\d{1,3})\stage\snetto|net\s(?P<n4>\d{1,3})", re.I)


def mkdate(y, m, d):
    y = int(y)
    if y < 100:
        y += 2000 if y < 70 else 1900
    try:
        return dt.date(y, int(m), int(d))
    except ValueError:
        return None


def add_months(d, n):
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def ctx_kind(ctx):
    for pat, w, kind, label in DEADLINE_WORDS:
        hit = re.search(pat, ctx, re.I)
        if hit:
            return w, kind, label, hit.group(0)
    return 0, None, None, None


def deadlines(text, doc_date=None):
    base = doc_date if isinstance(doc_date, dt.date) else None
    out, notes = [], []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        for rx in DATE_RES:
            for m in rx.finditer(line):
                g = m.groupdict()
                mon = g.get("m") or MONTHS.get((g.get("mn") or "").lower().rstrip("."))
                if not mon:
                    continue
                d = mkdate(g["y"], mon, g["d"])
                if not d or d.year < 1990 or d.year > 2100:
                    continue
                before = line[max(0, m.start() - 80):m.start()].lower()
                prev = lines[i - 1][-60:].lower() if i and len(line[:m.start()].strip()) < 3 else ""
                w, kind, label, word = ctx_kind(prev + " " + before)
                if not kind:
                    continue
                score = w
                if DATE_BAD.search(before[-30:]):
                    score -= 5
                if base:
                    if d < base and kind not in ("contract",):
                        score -= 4
                    if d == base:
                        score -= 3
                out.append({"d": d.isoformat(), "kind": kind, "label": label, "word": word, "score": score,
                            "snip": re.sub(r"\s+", " ", line.strip())[:120]})
        for m in REL_RE.finditer(line):
            n = m.group("n") or m.group("n2") or m.group("n3") or m.group("n4")
            n = int(n) if n.isdigit() else NUMWORDS.get(n.lower())
            if not n or n > 400:
                continue
            unit = (m.group("u") or "tage").lower()
            ctx = (lines[i - 1][-60:] + " " + line).lower() if i else line.lower()
            w, kind, label, word = ctx_kind(ctx)
            if not kind:
                if m.group("n2") or m.group("n3") or m.group("n4") or re.search(r"zahl|überweis|begleich|pay", ctx):
                    w, kind, label, word = 6, "payment", "Payment due", "zahlbar"
                else:
                    continue
            u = "day" if unit.startswith(("tag", "day")) else "working day" if unit.startswith("werk") else "week" if unit.startswith(("woch", "week")) else "month"
            span = f"{n} {u}{'s' if n != 1 else ''}"
            if not base:
                notes.append(f"{label}: {span}")
                continue
            if unit.startswith(("tag", "day", "werk")):
                d = base + dt.timedelta(days=n + (n // 5) * 2 if unit.startswith("werk") else n)
            elif unit.startswith(("woch", "week")):
                d = base + dt.timedelta(weeks=n)
            else:
                d = add_months(base, n)
            out.append({"d": d.isoformat(), "kind": kind, "label": label, "word": word, "score": w - 0.5,
                        "rel": f"{span} after the document date", "snip": re.sub(r"\s+", " ", line.strip())[:120]})
        km = re.search(r"kündigungsfrist(?:\s(?:von|beträgt))?:?\s(?P<n>\d{1,2}|[a-zäöü]+)\s(?P<u>monat\w*|woche\w*|tag\w*)", line, re.I)
        if km:
            nn = km.group("n")
            nn = int(nn) if nn.isdigit() else NUMWORDS.get(nn.lower(), nn)
            uu = "month" if km.group("u").lower().startswith("monat") else "week" if km.group("u").lower().startswith("woch") else "day"
            notes.append(f"Notice period: {nn} {uu}{'s' if nn != 1 else ''}")
    out.sort(key=lambda x: (-x["score"], x["d"]))
    seen, res = set(), []
    for x in out:
        if x["d"] in seen or x["score"] < 2:
            continue
        seen.add(x["d"])
        x["score"] = round(x["score"], 1)
        res.append(x)
    return res[:6], list(dict.fromkeys(notes))[:4]


def extract(text, doc_date=None):
    text = (text or "")[:60000]
    if isinstance(doc_date, str) and re.fullmatch(r"\d{4}-\d\d-\d\d", doc_date[:10]):
        doc_date = dt.date.fromisoformat(doc_date[:10])
    a = amounts(text)
    d, notes = deadlines(text, doc_date)
    return {"amounts": a, "deadlines": d, "notes": notes,
            "amount": a[0] if a and a[0]["score"] >= 4 else None,
            "deadline": d[0] if d and d[0]["score"] >= 4 else None}
