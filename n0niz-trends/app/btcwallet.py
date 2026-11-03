"""Watch-only Bitcoin wallets from xpub/ypub/zpub keys or output descriptors.

Derives addresses locally (BIP32 public derivation, no private keys involved) and
reads their history from a Mempool/Esplora-style REST API, normally the Mempool
app on the same Umbrel. Supports single-sig (legacy, nested and native SegWit,
Taproot) and multisig descriptors (wsh/sh-wsh with multi or sortedmulti).
Standard library only.
"""

import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.request

# ------------------------------------------------------------- secp256k1 ---
P = 2**256 - 2**32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % P == 0:
        return None
    if a == b:
        lam = 3 * a[0] * a[0] * pow(2 * a[1], -1, P) % P
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], -1, P) % P
    x = (lam * lam - a[0] - b[0]) % P
    return x, (lam * (a[0] - x) - a[1]) % P


def _mul(k, pt=G):
    r = None
    while k:
        if k & 1:
            r = _add(r, pt)
        pt = _add(pt, pt)
        k >>= 1
    return r


def _lift(x, odd):
    y = pow((pow(x, 3, P) + 7) % P, (P + 1) // 4, P)
    if (y & 1) != odd:
        y = P - y
    return x, y


def _ser(pt):
    return bytes([2 + (pt[1] & 1)]) + pt[0].to_bytes(32, "big")


def _deser(b):
    return _lift(int.from_bytes(b[1:], "big"), b[0] == 3)


# ---------------------------------------------------------------- hashing ---
def _ripemd160_py(data):
    # compact pure-Python RIPEMD-160 (OpenSSL 3 often hides ripemd160)
    def f(j, x, y, z):
        return (x ^ y ^ z if j < 16 else (x & y) | (~x & z) if j < 32 else (x | ~y) ^ z if j < 48
                else (x & z) | (y & ~z) if j < 64 else x ^ (y | ~z)) & 0xFFFFFFFF
    K1 = [0, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E]
    K2 = [0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0]
    R1 = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8,
          3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12, 1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2,
          4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13]
    R2 = [5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12, 6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2,
          15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13, 8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14,
          12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11]
    S1 = [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8, 7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12,
          11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5, 11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12,
          9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6]
    S2 = [8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6, 9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11,
          9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5, 15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8,
          8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11]
    rol = lambda x, n: ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF  # noqa: E731
    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
    ml = len(data) * 8
    data = data + b"\x80" + b"\x00" * ((55 - len(data)) % 64) + ml.to_bytes(8, "little")
    for off in range(0, len(data), 64):
        X = [int.from_bytes(data[off + 4 * i:off + 4 * i + 4], "little") for i in range(16)]
        al, bl, cl, dl, el = h
        ar, br, cr, dr, er = h
        for j in range(80):
            t = rol((al + f(j, bl, cl, dl) + X[R1[j]] + K1[j // 16]) & 0xFFFFFFFF, S1[j]) + el & 0xFFFFFFFF
            al, el, dl, cl, bl = el, dl, rol(cl, 10), bl, t
            t = rol((ar + f(79 - j, br, cr, dr) + X[R2[j]] + K2[j // 16]) & 0xFFFFFFFF, S2[j]) + er & 0xFFFFFFFF
            ar, er, dr, cr, br = er, dr, rol(cr, 10), br, t
        t = (h[1] + cl + dr) & 0xFFFFFFFF
        h[1] = (h[2] + dl + er) & 0xFFFFFFFF
        h[2] = (h[3] + el + ar) & 0xFFFFFFFF
        h[3] = (h[4] + al + br) & 0xFFFFFFFF
        h[4] = (h[0] + bl + cr) & 0xFFFFFFFF
        h[0] = t
    return b"".join(x.to_bytes(4, "little") for x in h)


def ripemd160(b):
    try:
        return hashlib.new("ripemd160", b).digest()
    except (ValueError, TypeError):
        return _ripemd160_py(b)


def hash160(b):
    return ripemd160(hashlib.sha256(b).digest())


def tagged(tag, msg):
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + msg).digest()


# -------------------------------------------------------------- encodings ---
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58check_decode(s):
    n = 0
    for c in s:
        if c not in B58:
            raise ValueError("not a valid key (unexpected character)")
        n = n * 58 + B58.index(c)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    raw = b"\x00" * (len(s) - len(s.lstrip("1"))) + raw
    body, chk = raw[:-4], raw[-4:]
    if hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] != chk:
        raise ValueError("checksum wrong - is the key complete?")
    return body


def b58check_encode(b):
    b = b + hashlib.sha256(hashlib.sha256(b).digest()).digest()[:4]
    n, s = int.from_bytes(b, "big"), ""
    while n:
        n, r = divmod(n, 58)
        s = B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + s


BECH = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def segwit_address(version, program, hrp="bc"):
    acc, bits, data = 0, 0, [version]
    for b in program:
        acc = (acc << 8) | b
        bits += 8
        while bits >= 5:
            bits -= 5
            data.append((acc >> bits) & 31)
    if bits:
        data.append((acc << (5 - bits)) & 31)
    const = 1 if version == 0 else 0x2BC830A3
    hv = [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]
    pm = _polymod(hv + data + [0] * 6) ^ const
    return hrp + "1" + "".join(BECH[d] for d in data + [(pm >> 5 * (5 - i)) & 31 for i in range(6)])


# ------------------------------------------------------------------ BIP32 ---
VERSIONS = {
    "0488b21e": "pkh", "049d7cb2": "sh-wpkh", "04b24746": "wpkh",      # xpub ypub zpub
    "0295b43f": "sh-wsh", "02aa7ed3": "wsh",                          # Ypub Zpub (multisig)
}


class ExtKey:
    def __init__(self, s):
        raw = b58check_decode(s)
        if len(raw) != 78:
            raise ValueError("not an extended public key")
        ver = raw[:4].hex()
        if ver not in VERSIONS:
            if ver in ("043587cf", "044a5262", "045f1cf6", "024289ef", "02575483"):
                raise ValueError("this is a testnet key")
            if ver in ("0488ade4", "049d7878", "04b2430c"):
                raise ValueError("this is a PRIVATE key - never paste that anywhere. Use the xpub/zpub instead")
            raise ValueError("unknown key type")
        self.kind = VERSIONS[ver]
        self.chain = raw[13:45]
        self.key = raw[45:]
        if self.key[0] not in (2, 3):
            raise ValueError("not a public key")
        self.text = s

    def child(self, i):
        if i >= 2**31:
            raise ValueError("hardened step after the xpub is impossible")
        I = hmac.new(self.chain, self.key + i.to_bytes(4, "big"), hashlib.sha512).digest()
        il = int.from_bytes(I[:32], "big")
        pt = _add(_mul(il), _deser(self.key))
        c = object.__new__(ExtKey)
        c.kind, c.chain, c.key, c.text = self.kind, I[32:], _ser(pt), None
        return c

    def derive(self, path):
        k = self
        for i in path:
            k = k.child(i)
        return k


# ------------------------------------------------------------ descriptors ---
KEY_RE = re.compile(r"^(?:\[[0-9a-fA-F]{8}(?:/[0-9]+['hH]?)*\])?([1-9A-HJ-NP-Za-km-z]{100,120})((?:/[^,)]*)?)$")
TYPE_NAMES = {"pkh": "Legacy (1…)", "sh-wpkh": "Nested SegWit (3…)", "wpkh": "Native SegWit (bc1q…)",
              "tr": "Taproot (bc1p…)", "wsh": "Multisig, native (bc1q…)", "sh-wsh": "Multisig, nested (3…)"}


def _parse_key(txt):
    m = KEY_RE.match(txt.strip())
    if not m:
        raise ValueError("could not read the key in the descriptor")
    k = ExtKey(m.group(1))
    path = [p for p in m.group(2).split("/") if p]
    fixed, chains = [], None
    for n, p in enumerate(path):
        if p == "*":
            break
        mm = re.fullmatch(r"<(\d+);(\d+)>", p)
        if mm:
            chains = [int(mm.group(1)), int(mm.group(2))]
            continue
        if p.endswith(("'", "h", "H")):
            raise ValueError("hardened derivation after the xpub is impossible")
        if chains is None and n == len(path) - 2 and path[-1] == "*":
            chains = [int(p)]
            continue
        fixed.append(int(p))
    if fixed:
        k = k.derive(fixed)
    return k, chains


def parse_wallet(text, force_type=""):
    """Turn a pasted xpub/ypub/zpub or descriptor into a derivation spec."""
    s = re.sub(r"\s+", "", (text or "")).split("#")[0]
    if not s:
        raise ValueError("empty")
    spec = {"keys": [], "type": None, "k": 1, "sorted": True, "chains": [0, 1]}
    if "(" not in s:
        k = ExtKey(s)
        spec["keys"] = [k]
        spec["type"] = force_type or ("wpkh" if k.kind == "wsh" else "sh-wpkh" if k.kind == "sh-wsh" else k.kind)
        if spec["type"] in ("wsh", "sh-wsh"):
            raise ValueError("a single Zpub/Ypub is one key of a multisig - paste the wallet's output descriptor from Sparrow instead")
        return spec
    m = re.fullmatch(r"(sh\(wsh|wsh)\((sortedmulti|multi)\((\d+),(.+)\)\)\)?", s)
    if m:
        spec["type"] = "sh-wsh" if m.group(1).startswith("sh") else "wsh"
        spec["sorted"] = m.group(2) == "sortedmulti"
        spec["k"] = int(m.group(3))
        chains = None
        for part in m.group(4).split(","):
            k, ch = _parse_key(part)
            spec["keys"].append(k)
            chains = chains or ch
        if spec["k"] > len(spec["keys"]):
            raise ValueError("threshold larger than the number of keys")
    else:
        for pat, typ in ((r"wpkh\((.+)\)", "wpkh"), (r"sh\(wpkh\((.+)\)\)", "sh-wpkh"), (r"pkh\((.+)\)", "pkh"), (r"tr\((.+)\)", "tr")):
            mm = re.fullmatch(pat, s)
            if mm:
                if typ == "tr" and "," in mm.group(1):
                    raise ValueError("Taproot with script paths is not supported")
                k, chains = _parse_key(mm.group(1))
                spec["keys"], spec["type"] = [k], typ
                break
        else:
            raise ValueError("descriptor type not supported (use wpkh, sh(wpkh), pkh, tr or wsh(sortedmulti))")
    if chains and len(chains) == 2:
        spec["chains"] = chains
    elif chains:
        spec["chains"] = sorted({chains[0], 1 - chains[0] if chains[0] in (0, 1) else chains[0]})
    return spec


def address(spec, chain, index):
    t = spec["type"]
    pubs = [k.derive([chain, index]).key for k in spec["keys"]]
    if t == "pkh":
        return b58check_encode(b"\x00" + hash160(pubs[0]))
    if t == "wpkh":
        return segwit_address(0, hash160(pubs[0]))
    if t == "sh-wpkh":
        return b58check_encode(b"\x05" + hash160(b"\x00\x14" + hash160(pubs[0])))
    if t == "tr":
        pt = _deser(pubs[0])
        x = pt[0].to_bytes(32, "big")
        q = _add(_lift(pt[0], False), _mul(int.from_bytes(tagged("TapTweak", x), "big")))
        return segwit_address(1, q[0].to_bytes(32, "big"))
    if t in ("wsh", "sh-wsh"):
        keys = sorted(pubs) if spec["sorted"] else pubs
        n = len(keys)
        script = bytes([0x50 + spec["k"]]) + b"".join(b"\x21" + k for k in keys) + bytes([0x50 + n, 0xAE])
        h = hashlib.sha256(script).digest()
        if t == "wsh":
            return segwit_address(0, h)
        return b58check_encode(b"\x05" + hash160(b"\x00\x20" + h))
    raise ValueError("unknown type")


def describe(spec):
    t = TYPE_NAMES.get(spec["type"], spec["type"])
    if spec["type"] in ("wsh", "sh-wsh"):
        t = f"{spec['k']}-of-{len(spec['keys'])} " + t
    return t


# --------------------------------------------------------------- Mempool ---
def _short(path):
    return re.sub(r"(bc1\w{6})\w+(\w{4})|([13]\w{5})\w{20,}(\w{4})", lambda m: (m.group(1) or m.group(3)) + "…" + (m.group(2) or m.group(4)), path)


def _get(base, path, timeout=30, tries=3):
    req = urllib.request.Request(base.rstrip("/") + path, headers={"User-Agent": "spending-trends"})
    for n in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
            break
        except urllib.error.HTTPError as e:
            # Mempool answers 500 while Electrs is busy or still indexing: wait and retry
            if e.code < 500 or n == tries - 1:
                why = e.read()[:160].decode(errors="replace").strip()
                raise RuntimeError(f"Mempool {e.code} on {_short(path)}" + (f" ({why})" if why else "")) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if n == tries - 1:
                raise RuntimeError(f"Mempool timeout/unreachable on {_short(path)}: {e}") from None
        time.sleep(3 * (n + 1))
    try:
        return json.loads(body)
    except ValueError:
        return body.decode().strip()


def find_backend(candidates):
    errs = []
    for base in candidates:
        if not base:
            continue
        try:
            h = _get(base, "/blocks/tip/height", timeout=8, tries=1)
            if str(h).strip().isdigit():
                return base.rstrip("/")
            errs.append(f"{base}: unexpected answer")
        except Exception as e:  # noqa: BLE001
            errs.append(f"{base}: {e}")
    raise RuntimeError("Mempool not reachable. Tried " + "; ".join(errs))


def address_txs(base, addr):
    out, seen, last = [], set(), None
    for _ in range(400):
        page = _get(base, f"/address/{addr}/txs" + (f"/chain/{last}" if last else ""))
        new = [t for t in page if t["txid"] not in seen]
        if not new:
            break
        for t in new:
            seen.add(t["txid"])
            out.append(t)
        confirmed = [t for t in page if t.get("status", {}).get("confirmed")]
        if not confirmed or len(confirmed) < 25:
            break
        last = confirmed[-1]["txid"]
    return out


def scan(spec, base, cache, gap=20, progress=None):
    """Walk receive and change chains until `gap` unused addresses in a row.
    cache: {"addr": {address: {"c": chain, "i": index, "n": tx_count}}, "tx": {txid: tx}}"""
    addrs = cache.setdefault("addr", {})
    txs = cache.setdefault("tx", {})
    by_index = {(v["c"], v["i"]): a for a, v in addrs.items()}
    funded = spent = 0
    for chain in spec["chains"]:
        i, unused = 0, 0
        while unused < gap:
            a = by_index.get((chain, i)) or address(spec, chain, i)
            info = _get(base, f"/address/{a}")
            cs, ms = info.get("chain_stats", {}), info.get("mempool_stats", {})
            n = cs.get("tx_count", 0) + ms.get("tx_count", 0)
            funded += cs.get("funded_txo_sum", 0) + ms.get("funded_txo_sum", 0)
            spent += cs.get("spent_txo_sum", 0) + ms.get("spent_txo_sum", 0)
            old = addrs.get(a, {}).get("n")
            addrs[a] = {"c": chain, "i": i, "n": n}
            if n:
                unused = 0
                if n != old or ms.get("tx_count", 0):
                    for t in address_txs(base, a):
                        txs[t["txid"]] = t
            else:
                unused += 1
            i += 1
            if progress:
                progress(chain, i)
    mine = set(addrs)
    rows = []
    for txid, t in txs.items():
        recv = sum(o.get("value", 0) for o in t.get("vout", []) if o.get("scriptpubkey_address") in mine)
        sent = sum((v.get("prevout") or {}).get("value", 0) for v in t.get("vin", [])
                   if (v.get("prevout") or {}).get("scriptpubkey_address") in mine)
        if not recv and not sent:
            continue
        st = t.get("status", {})
        rows.append({"txid": txid, "t": int(st.get("block_time") or time.time()), "conf": bool(st.get("confirmed")),
                     "net": recv - sent, "fee": t.get("fee", 0) if sent else 0})
    rows.sort(key=lambda r: r["t"])
    used = sum(1 for v in addrs.values() if v["n"])
    return {"balance": funded - spent, "txs": rows, "used": used}
