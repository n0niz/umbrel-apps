/* Doc Sorter – the tree as a zoomable node graph (infinite canvas).

   Archive ─ folders ─ subfolders / case files ─ documents, laid out left to right with
   curved connectors. Connected documents ("Gehört zu") get dashed arcs.
   · scroll / pinch = zoom (around the pointer), drag the background = pan
   · click the ◦ handle on a card = open / close its children
   · double-click a folder or case file = zoom into it, double-click a document = open it
   · drag a document onto a document = connect them (and file it in the same folder),
     onto a folder = move it there (Ctrl/⌥ = also), onto "Not in a folder" = take it out,
     a folder onto a folder = put it inside
   · keys: ← parent · → child · ↑↓ siblings · Space open/close · Enter open · + − zoom · 0 fit · U undo */
(function () {
  const DS = window.DS;
  const { fmt, money, dueHtml, amtHtml } = DS;
  const COLW = 330, GAPY = 12, LIMIT = 30;
  const DIM = { root: [230, 66], folder: [252, 66], cat: [252, 66], group: [252, 66], case: [264, 68], doc: [264, 76], more: [200, 40] };
  const store = (k, d) => { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch (e) { return d; } };
  const G = { x: 60, y: 60, k: 1, sel: null, focus: "root", open: new Set(store("docsorter.graph.open", ["root"])), limit: {},
    els: new Map(), edges: new Map(), labels: new Map(), nodes: new Map(), parent: new Map(), pos: new Map(), first: true, tween: null, drag: null,
    mode: store("docsorter.graph.mode", "tags") };
  const MODES = { tags: { label: "Tags" }, cats: { label: "Category", field: "document_type", none: "Without category", pre: "k:", list: () => M.types, verb: "set category", unverb: "remove category" },
    corrs: { label: "Sender", field: "correspondent", none: "Unknown sender", pre: "r:", list: () => M.corrs, verb: "set sender", unverb: "remove sender" } };
  const HUES = [212, 186, 150, 38, 262, 0, 330, 96, 24, 200, 276, 168];
  const hueOf = (s) => { let h = 0; for (const ch of String(s)) h = (h * 31 + ch.charCodeAt(0)) >>> 0; return HUES[h % HUES.length]; };
  const saveOpen = () => { try { localStorage.setItem("docsorter.graph.open", JSON.stringify([...G.open])); } catch (e) { /* ignore */ } };
  let VP = null, WORLD = null, SVG = null, LBL = null;
  const docById = (id) => DOCS.find((d) => d.id === id);
  const typeName = (id) => (byId(M.types, id) || {}).name || "";
  const corrName = (id) => (byId(M.corrs, id) || {}).name || "";

  // ================================================================ model
  function primaryFolder(d) {
    const fs = (d.tags || []).filter(isFolder).map((t) => byId(M.tags, t)).filter(Boolean).sort((a, b) => a.name.localeCompare(b.name));
    return fs.length ? fs[0].id : null;
  }
  function model() {
    const COMP = DS.components(), fold = folders(), kidsOf = {};
    fold.forEach((t) => { const p = t.parent && isFolder(t.parent) ? t.parent : 0; (kidsOf[p] = kidsOf[p] || []).push(t); });
    Object.values(kidsOf).forEach((l) => l.sort((a, b) => a.name.localeCompare(b.name)));
    const words = (Q || "").toLowerCase().split(/\s+/).filter(Boolean), match = (d) => !words.length || words.every((w) => docText(d).includes(w));
    const MD = MODES[G.mode] || MODES.tags, tags = G.mode === "tags";
    const groupOf = (d) => (tags ? primaryFolder(d) : d[MD.field] || null);
    const items = {}, seen = new Set();
    DOCS.forEach((d) => {
      const c = COMP.get(d.id);
      if (c) {
        if (seen.has(c)) return; seen.add(c);
        const home = groupOf(c.docs[0]) ?? (tags ? c.docs.map(groupOf).find((x) => x != null) : null) ?? "u";
        (items[home] = items[home] || []).push({ c, date: c.docs[c.docs.length - 1].created || "", hit: c.docs.some(match) });
      } else (items[groupOf(d) ?? "u"] = items[groupOf(d) ?? "u"] || []).push({ d, date: d.created || "", hit: match(d) });
    });
    Object.values(items).forEach((l) => l.sort((a, b) => b.date.localeCompare(a.date)));
    const searching = words.length > 0;
    const itemNodes = (home, pkey) => {
      let L = items[home] || [];
      if (searching) L = L.filter((x) => x.hit);
      const lim = G.limit[pkey] || LIMIT, out = L.slice(0, lim).map((x) => Object.assign(x.d ? docNode(x.d, home) : caseNode(x.c, home, match), { hit: searching && x.hit }));
      if (L.length > lim) out.push({ key: "m:" + pkey, type: "more", label: `+ ${L.length - lim} more`, sub: "click to show", parentKey: pkey });
      return out;
    };
    const subtreeDocs = (tid) => { const ids = descIds(tid); return DOCS.filter((d) => (d.tags || []).some((t) => ids.has(t))); };
    const folderNode = (t) => {
      const key = "f:" + t.id, subs = (kidsOf[t.id] || []).map(folderNode).filter(Boolean), its = itemNodes(t.id, key), ds = subtreeDocs(t.id);
      const hit = searching && (subs.some((s) => s.hit) || its.some((x) => x.type !== "more") || t.name.toLowerCase().includes(words.join(" ")));
      if (searching && !hit) return null;
      const nCase = (items[t.id] || []).filter((x) => x.c).length;
      return { key, type: "folder", id: t.id, color: t.color, label: t.name, count: ds.length, hit,
        sub: `${ds.length} doc${ds.length === 1 ? "" : "s"}${nCase ? ` · ${nCase} case file${nCase > 1 ? "s" : ""}` : ""}${subs.length ? ` · ${subs.length} sub` : ""}`, extra: sumNext(ds), kids: [...subs, ...its] };
    };
    if (!tags) {
      const groups = MD.list().slice().sort((a, b) => a.name.localeCompare(b.name)).map((t) => {
        const key = MD.pre + t.id, its = itemNodes(t.id, key), ds = DOCS.filter((d) => d[MD.field] === t.id);
        if (searching && !its.some((x) => x.type !== "more") && !t.name.toLowerCase().includes(words.join(" "))) return null;
        return { key, type: "cat", id: t.id, label: t.name, hue: hueOf(t.name), count: ds.length, hit: searching, kids: its,
          sub: ds.length ? `${ds.length} doc${ds.length === 1 ? "" : "s"}` : "empty – drop documents here", extra: sumNext(ds) };
      }).filter(Boolean);
      const unf = itemNodes("u", "g:u"), nd = DOCS.filter((d) => !d[MD.field]);
      const root = { key: "root", type: "root", label: "Archive", sub: `${DOCS.length} documents · by ${MD.label.toLowerCase()}`, kids: groups };
      if (unf.length || !searching) root.kids.push({ key: "g:u", type: "group", label: MD.none, count: nd.length, sub: nd.length ? "drag onto a " + MD.label.toLowerCase() : "all assigned", kids: unf, hit: searching && unf.length > 0 });
      return { root, searching };
    }
    const top = (kidsOf[0] || []).map(folderNode).filter(Boolean);
    const unf = itemNodes("u", "g:u"), unfDocs = DOCS.filter((d) => !(d.tags || []).some(isFolder));
    const root = { key: "root", type: "root", label: "Archive", sub: `${DOCS.length} documents · ${fold.length} folders`, kids: [...top] };
    if (unf.length || !searching) root.kids.push({ key: "g:u", type: "group", label: "Not in a folder", count: unfDocs.length, sub: unfDocs.length ? "drag onto a folder or a document" : "everything is filed", kids: unf, hit: searching && unf.length > 0 });
    return { root, searching };
  }
  function sumNext(docs) {
    const one = docs.filter((d) => d.amount && !d.amount.p && d.amount.k !== "refund"), t = DS.iso(DS.today());
    const nd = docs.filter((d) => d.due && d.due.d >= t).sort((a, b) => a.due.d.localeCompare(b.due.d))[0];
    return (one.length > 1 ? `<span class="amt">Σ ${money({ v: one.reduce((a, d) => a + d.amount.v, 0), c: "EUR" })}</span> ` : "") + (nd ? dueHtml(nd.due) : "");
  }
  function docNode(d, home) {
    return { key: "d:" + d.id, type: "doc", id: d.id, home, label: d.title || d.original_file_name || "#" + d.id, doc: d,
      sub: [fmt(d.created), typeName(d.document_type), corrName(d.correspondent)].filter(Boolean).join(" · "),
      extra: (d.amount ? amtHtml(d.amount) + " " : "") + (d.due ? dueHtml(d.due) : ""), nf: (d.tags || []).filter(isFolder).length };
  }
  function caseNode(c, home, match) {
    const r = c.docs[0], l = c.docs[c.docs.length - 1];
    return { key: "c:" + c.root, type: "case", root: c.root, home, label: r.title, count: c.docs.length,
      sub: `case file · ${fmt(r.created).slice(3)} → ${fmt(l.created).slice(3)}`, extra: sumNext(c.docs),
      kids: c.docs.map((d) => Object.assign(docNode(d, home), { inCase: true, hit: Q && match(d) })) };
  }

  // ================================================================ layout (tidy tree, left → right)
  function isOpen(n, searching) { return n.key === "root" || G.open.has(n.key) || (searching && n.hit); }
  function shift(n, dy) { n.y += dy; if (n._open) n.kids.forEach((c) => shift(c, dy)); }
  function lay(n, depth, top, searching) {
    const [w, h] = DIM[n.type]; n.w = w; n.h = h; n.x = depth * COLW; n.depth = depth;
    n._open = !!(n.kids && n.kids.length && isOpen(n, searching));
    if (!n._open) { n.y = top; return h; }
    let y = top;
    n.kids.forEach((c) => { G.parent.set(c.key, n.key); y += lay(c, depth + 1, y, searching) + GAPY; });
    const a = n.kids[0], b = n.kids[n.kids.length - 1];
    n.y = (a.y + a.h / 2 + b.y + b.h / 2) / 2 - h / 2;
    if (n.y < top) { const d = top - n.y; n.kids.forEach((c) => shift(c, d)); n.y = top; y += d; }
    return Math.max(y - GAPY - top, n.y + h - top);
  }
  function flat(n, out = []) { out.push(n); if (n._open) n.kids.forEach((c) => flat(c, out)); return out; }

  // ================================================================ render
  const ICON_FOLDER = (c) => `<svg viewBox="0 0 24 24" width="18" height="18"><path d="M3 6.5A2.5 2.5 0 0 1 5.5 4h3.6l2 2.2h7.4A2.5 2.5 0 0 1 21 8.7v8.8a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z" fill="${esc(c || "#64748b")}"/></svg>`;
  function cardHtml(n) {
    let ic;
    if (n.type === "root") ic = `<div class="gic groot">✦</div>`;
    else if (n.type === "folder") ic = `<div class="gic" style="--c:${esc(n.color || "#64748b")}">${ICON_FOLDER(n.color)}</div>`;
    else if (n.type === "cat") ic = `<div class="gic gcat" style="--h:${n.hue}">${G.mode === "corrs" ? esc((n.label || "?").trim().charAt(0).toUpperCase()) : "🏷"}</div>`;
    else if (n.type === "group") ic = `<div class="gic">${G.mode === "tags" ? "📂" : "∅"}</div>`;
    else if (n.type === "case") ic = `<div class="gic gcase">🗂</div>`;
    else if (n.type === "doc") ic = `<img class="gth" src="/_lib/thumb/${n.id}" alt="" draggable="false">`;
    else ic = "";
    const ai = n.type === "doc" && AI.results[n.id] ? `<span class="gbd ai">✨</span>` : "";
    const nf = n.type === "doc" && n.nf > 1 ? `<span class="gbd" title="In ${n.nf} folders">×${n.nf}</span>` : "";
    const lk = n.type === "doc" && (n.doc.links || []).length && !n.inCase ? `<span class="gbd">🔗${n.doc.links.length}</span>` : "";
    return `${ic}<div class="gtx"><b>${esc(n.label)}</b><span>${esc(n.sub || "")}</span>${n.extra ? `<em>${n.extra}</em>` : ""}</div>${ai}${nf}${lk}`
      + (n.kids && n.kids.length ? `<button class="ghd" title="${n._open ? "Close" : "Open"}">${n._open ? "−" : (n.count ?? n.kids.length)}</button>` : "");
  }
  const ePath = (x1, y1, x2, y2) => { const dx = Math.max(40, (x2 - x1) * 0.5); return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`; };
  const arcPath = (x1, y1, x2, y2) => { const b = 46 + Math.min(120, Math.abs(y2 - y1) * 0.25); return `M${x1},${y1} C${x1 + b},${y1} ${x2 + b},${y2} ${x2},${y2}`; };
  function setD(p, d, instant) { p.setAttribute("d", d); if (instant) { p.style.transition = "none"; p.style.d = `path("${d}")`; void p.getBoundingClientRect(); p.style.transition = ""; } else p.style.d = `path("${d}")`; }

  function draw() {
    if (!VP) return;
    const { root, searching } = model();
    G.parent.clear(); G.nodes.clear();
    lay(root, 0, 0, searching);
    const list = flat(root), keys = new Set(list.map((n) => n.key));
    list.forEach((n) => G.nodes.set(n.key, n));
    const sel = G.sel && G.nodes.has(G.sel) ? G.sel : null; G.sel = sel;
    const anc = new Set(); for (let k = sel; k; k = G.parent.get(k)) anc.add(k);
    const instant = G.first; G.first = false;
    // nodes
    list.forEach((n) => {
      let el = G.els.get(n.key); const fresh = !el;
      if (fresh) {
        el = document.createElement("div"); el.dataset.k = n.key; WORLD.appendChild(el); G.els.set(n.key, el);
        const p = G.nodes.get(G.parent.get(n.key)), from = p ? G.pos.get(p.key) || p : n;
        el.style.transition = "none"; el.style.transform = `translate(${from.x + (p ? p.w - 40 : 0)}px,${from.y}px) scale(.8)`; el.style.opacity = "0";
      }
      const html = cardHtml(n);
      if (el._h !== html) { el.innerHTML = html; el._h = html; }
      el.className = `gn g-${n.type}${n.key === sel ? " sel" : ""}${anc.has(n.key) && n.key !== sel ? " anc" : ""}${n.type === "doc" && SEL.has(n.id) ? " multi" : ""}${searching && n.type === "doc" && n.hit ? " hit" : ""}${searching && n.type === "doc" && !n.hit ? " dim" : ""}`;
      el.style.width = n.w + "px"; el.style.height = n.h + "px";
      const go = () => { el.style.transition = instant ? "none" : ""; el.style.transform = `translate(${n.x}px,${n.y}px)`; el.style.opacity = "1"; };
      if (fresh && !instant) requestAnimationFrame(() => requestAnimationFrame(go)); else go();
      G.pos.set(n.key, { x: n.x, y: n.y, w: n.w, h: n.h });
    });
    G.els.forEach((el, k) => {
      if (keys.has(k)) return;
      G.els.delete(k); G.pos.delete(k);
      let p = k; while (p && !keys.has(p)) p = G._oldParent && G._oldParent.get(p);
      const tp = p && G.nodes.get(p);
      el.style.pointerEvents = "none"; el.style.opacity = "0";
      if (tp) el.style.transform = `translate(${tp.x + tp.w - 60}px,${tp.y}px) scale(.8)`;
      setTimeout(() => el.remove(), 380);
    });
    G._oldParent = new Map(G.parent);
    // tree edges
    const want = new Map();
    list.forEach((n) => { const p = G.nodes.get(G.parent.get(n.key)); if (p) want.set("e:" + n.key, { d: ePath(p.x + p.w, p.y + p.h / 2, n.x, n.y + n.h / 2), cls: "ge" + (anc.has(n.key) ? " hl" : "") + (n.type === "more" ? " faint" : ""), from: p }); });
    // link arcs between visible connected documents
    const vis = new Map(list.filter((n) => n.type === "doc").map((n) => [n.id, n]));
    vis.forEach((a) => (a.doc.links || []).forEach((b) => {
      if (b <= a.id || !vis.has(b)) return;
      const B = vis.get(b), up = a.y < B.y ? a : B, dn = up === a ? B : a;
      want.set(`l:${a.id}-${b}`, { d: arcPath(up.x + up.w, up.y + up.h / 2, dn.x + dn.w, dn.y + dn.h / 2), cls: "gl" + (sel === "d:" + a.id || sel === "d:" + b ? " hl" : ""), label: true, mid: { x: Math.max(up.x, dn.x) + up.w + 30 + Math.min(60, Math.abs(dn.y - up.y) * 0.18), y: (up.y + dn.y + up.h) / 2 } });
    }));
    // a selected document that is in several folders: faint "also in" edges
    const sn = sel && G.nodes.get(sel);
    if (sn && sn.type === "doc") (sn.doc.tags || []).filter(isFolder).forEach((t) => { const f = G.nodes.get("f:" + t); if (f && G.parent.get(sel) !== f.key && !(G.parent.get(sel) || "").startsWith("c:")) want.set(`a:${t}`, { d: ePath(f.x + f.w, f.y + f.h / 2, sn.x, sn.y + sn.h / 2), cls: "ga" }); });
    want.forEach((w, k) => {
      let p = G.edges.get(k);
      if (!p) {
        p = document.createElementNS("http://www.w3.org/2000/svg", "path"); SVG.appendChild(p); G.edges.set(k, p);
        if (w.from && !instant) { const f = G.pos.get(w.from.key) || w.from; setD(p, ePath(f.x + f.w, f.y + f.h / 2, f.x + f.w + 1, f.y + f.h / 2), true); p.getBoundingClientRect(); }
        else if (!instant && k[0] !== "e") { p.style.opacity = "0"; requestAnimationFrame(() => (p.style.opacity = "")); }
      }
      p.setAttribute("class", w.cls); setD(p, w.d, instant);
      if (w.label) {
        let l = G.labels.get(k);
        if (!l) { l = document.createElement("div"); l.className = "glabel"; l.textContent = "connected"; LBL.appendChild(l); G.labels.set(k, l); }
        l.style.transform = `translate(${w.mid.x}px,${w.mid.y}px) translate(-50%,-50%)`; l.classList.toggle("hl", w.cls.includes("hl"));
      }
    });
    G.edges.forEach((p, k) => { if (!want.has(k)) { p.style.opacity = "0"; G.edges.delete(k); setTimeout(() => p.remove(), 300); } });
    G.labels.forEach((l, k) => { if (!want.has(k)) { l.remove(); G.labels.delete(k); } });
    crumbs();
    if (instant) fit("root", true, true);
  }

  // ================================================================ pan / zoom
  function apply() {
    WORLD.style.transform = `translate(${G.x}px,${G.y}px) scale(${G.k})`;
    const s = 24 * G.k; VP.style.backgroundSize = `${s}px ${s}px`; VP.style.backgroundPosition = `${G.x}px ${G.y}px`;
    VP.classList.toggle("lod1", G.k < 0.55); VP.classList.toggle("lod2", G.k < 0.3);
    const z = $("#gZoom"); if (z) z.textContent = Math.round(G.k * 100) + "%";
  }
  const clampK = (k) => Math.max(0.12, Math.min(2.2, k));
  function zoomAt(k, cx, cy, animate) {
    k = clampK(k); const nx = cx - ((cx - G.x) * k) / G.k, ny = cy - ((cy - G.y) * k) / G.k;
    animate ? tween(nx, ny, k) : (Object.assign(G, { x: nx, y: ny, k }), apply());
  }
  function tween(x, y, k, ms = 520) {
    cancelAnimationFrame(G.tween);
    const s = { x: G.x, y: G.y, k: G.k }, t0 = performance.now(), ease = (t) => 1 - Math.pow(1 - t, 4);
    const step = (now) => { const t = Math.min(1, (now - t0) / ms), e = ease(t); G.x = s.x + (x - s.x) * e; G.y = s.y + (y - s.y) * e; G.k = s.k + (k - s.k) * e; apply(); if (t < 1) G.tween = requestAnimationFrame(step); };
    G.tween = requestAnimationFrame(step);
  }
  function bounds(key) {
    const out = { x1: Infinity, y1: Infinity, x2: -Infinity, y2: -Infinity }, n0 = G.nodes.get(key);
    if (!n0) return null;
    const walk = (n) => { out.x1 = Math.min(out.x1, n.x); out.y1 = Math.min(out.y1, n.y); out.x2 = Math.max(out.x2, n.x + n.w + (n.type === "doc" ? 90 : 0)); out.y2 = Math.max(out.y2, n.y + n.h); if (n._open) n.kids.forEach(walk); };
    walk(n0); return out;
  }
  function fit(key = "root", instant, comfort) {
    const b = bounds(key); if (!b) return;
    const r = VP.getBoundingClientRect(), pad = 70;
    let k = clampK(Math.min((r.width - pad * 2) / (b.x2 - b.x1), (r.height - pad * 2) / (b.y2 - b.y1), 1.15));
    let x = (r.width - (b.x2 - b.x1) * k) / 2 - b.x1 * k, y = (r.height - (b.y2 - b.y1) * k) / 2 - b.y1 * k;
    if (comfort && k < 0.62) {
      // too much to read at once: open readable, with the node itself at the left edge, vertically centred
      const n = G.nodes.get(key); k = 0.62;
      x = 60 - b.x1 * k; y = r.height / 2 - (n.y + n.h / 2) * k;
    }
    G.focus = key;
    if (instant) { Object.assign(G, { x, y, k }); apply(); } else tween(x, y, k);
    crumbs();
  }
  function centerOn(key) {
    const n = G.nodes.get(key); if (!n) return;
    const r = VP.getBoundingClientRect(), sx = G.x + n.x * G.k, sy = G.y + n.y * G.k;
    if (sx > 40 && sy > 40 && sx + n.w * G.k < r.width - 40 && sy + n.h * G.k < r.height - 40) return;
    tween(r.width / 2 - (n.x + n.w / 2) * G.k, r.height / 2 - (n.y + n.h / 2) * G.k, G.k, 420);
  }
  function zoomInto(key) {
    const n = G.nodes.get(key); if (!n) return;
    if (n.kids && n.kids.length && !n._open) { G.open.add(key); saveOpen(); draw(); }
    setTimeout(() => fit(key, false, true), 30);
  }
  function crumbs() {
    const c = $("#gCrumbs"); if (!c) return;
    const path = []; for (let k = G.focus; k; k = G.parent.get(k)) { const n = G.nodes.get(k); if (n) path.unshift(n); }
    c.innerHTML = path.map((n, i) => `<button data-z="${esc(n.key)}"${i === path.length - 1 ? ' class="on"' : ""}>${n.type === "root" ? "✦ Archive" : esc(n.label)}</button>`).join("<i>›</i>");
    $$("[data-z]", c).forEach((b) => (b.onclick = () => fit(b.dataset.z)));
  }

  // ================================================================ selection + actions
  function catPane(n) {
    const pane = $("#tpane"); if (!pane) return;
    const MD = MODES[G.mode], ds = DOCS.filter((d) => (n.type === "group" ? !d[MD.field] : d[MD.field] === n.id));
    pane.innerHTML = `<h3><span class="dot" style="background:hsl(${n.hue || 210} 70% 60%)"></span> ${esc(n.label)}</h3><p class="thint">${ds.length} documents ${sumNext(ds)}</p>
      <div class="minis">${ds.slice(0, 12).map((d) => `<img src="/_lib/thumb/${d.id}" data-open="${d.id}" title="${esc(d.title)}">`).join("")}</div>
      <p class="thint" style="margin-top:12px">Drag documents onto “${esc(n.label)}” in the tree – or from the Documents list – to ${n.type === "group" ? MD.unverb : MD.verb}.</p>
      ${n.type === "cat" && G.mode === "cats" ? `<button class="btn sec" id="pCatOpen">Open in Category</button>` : ""}`;
    $$("[data-open]", pane).forEach((im) => (im.onclick = () => openDoc(+im.dataset.open)));
    if ($("#pCatOpen")) $("#pCatOpen").onclick = () => { CATSEL = n.id; VIEW = "cats"; render(); };
  }
  function select(key, scroll, keepTab) {
    G.sel = key;
    $$(".gn.sel, .gn.anc", WORLD).forEach((x) => x.classList.remove("sel", "anc"));
    draw();
    if (scroll) centerOn(key);
    const n = G.nodes.get(key);
    if (!keepTab && window.DocPanel && n && n.type !== "root") window.DocPanel.tab("info");
    if (n && (n.type === "cat" || (n.type === "group" && G.mode !== "tags"))) return catPane(n);
    if (DS.paintPane) DS.paintPane(n ? (n.type === "doc" ? { type: "doc", id: n.id, key, ctx: typeof n.home === "number" ? n.home : null } : n.type === "case" ? { type: "case", root: n.root, key, ctx: typeof n.home === "number" ? n.home : null } : n.type === "folder" ? { type: "folder", id: n.id, key } : null) : null);
  }
  function toggle(key, force) {
    const n = G.nodes.get(key); if (!n || !n.kids || !n.kids.length || key === "root") return;
    const open = force !== undefined ? force : !G.open.has(key);
    open ? G.open.add(key) : G.open.delete(key); saveOpen(); draw();
    if (open) setTimeout(() => { const b = bounds(key), r = VP.getBoundingClientRect(); if (b && (G.x + b.x2 * G.k > r.width || G.y + b.y2 * G.k > r.height)) centerOn(n.kids[Math.min(2, n.kids.length - 1)].key); }, 40);
  }
  function homeOf(n) { return G.mode === "tags" && typeof n.home === "number" ? n.home : null; }
  function setField(ids, value, name) {
    const MD = MODES[G.mode], f = MD.field, by = {};
    ids.forEach((i) => { const d = docById(i); const old = d ? d[f] || null : null; (by[old] = by[old] || []).push(i); });
    const inv = Object.entries(by).map(([old, L]) => ["/_lib/api/bulk", { ids: L, [f]: old === "null" ? null : +old }]);
    const nm = ids.length > 1 ? ids.length + " documents" : "“" + ((docById(ids[0]) || {}).title || "") + "”";
    return DS.run(value ? `${nm} → ${MD.label.toLowerCase()} “${name}”` : `${nm}: ${MD.unverb}`, [["/_lib/api/bulk", { ids, [f]: value }]], inv);
  }
  function dropKind(drag, t) {
    if (!t || t.key === drag.key) return null;
    if (drag.kind === "doc") {
      if (t.type === "doc" && !drag.ids.includes(t.id)) return { label: "connect", run: (also) => DS.connect({ kind: "doc", ids: drag.ids, from: drag.from }, docById(t.id), homeOf(t), also) };
      if (t.type === "case") return { label: "add to case file", run: (also) => DS.connect({ kind: "doc", ids: drag.ids, from: drag.from }, docById(t.root), homeOf(t), also) };
      if (t.type === "folder") return { label: "move into", run: (also) => DS.moveTo({ kind: "doc", ids: drag.ids, from: drag.from }, t.id, also) };
      if (t.type === "cat") return { label: MODES[G.mode].verb, run: () => setField(drag.ids, t.id, t.label) };
      if (t.type === "group" && G.mode !== "tags") return { label: MODES[G.mode].unverb, run: () => setField(drag.ids, null) };
      if (t.type === "group" && drag.from) return { label: "take out", run: () => DS.run(`Taken out of “${tagName(drag.from)}”`, [["/_lib/api/bulk", { ids: drag.ids, remove_tags: [drag.from] }]], [["/_lib/api/bulk", { ids: drag.ids, add_tags: [drag.from] }]]) };
    }
    if (drag.kind === "folder") {
      if (t.type === "folder" && t.id !== drag.id && !descIds(drag.id).has(t.id)) return { label: "put inside", run: () => DS.nest(drag.id, t.id) };
      if (t.type === "root" && (byId(M.tags, drag.id) || {}).parent) return { label: "top level", run: () => DS.nest(drag.id, null) };
    }
    return null;
  }

  // ================================================================ pointer: pan, click, drag & drop
  function world(e) { const r = VP.getBoundingClientRect(); return { x: (e.clientX - r.left - G.x) / G.k, y: (e.clientY - r.top - G.y) / G.k, sx: e.clientX - r.left, sy: e.clientY - r.top }; }
  function bind() {
    VP.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = VP.getBoundingClientRect(), cx = e.clientX - r.left, cy = e.clientY - r.top;
      const mouse = e.deltaMode === 1 || (e.deltaX === 0 && Math.abs(e.deltaY) >= 40 && Number.isInteger(e.deltaY));
      if (e.ctrlKey || mouse) zoomAt(G.k * Math.exp(-e.deltaY * (e.ctrlKey ? 0.012 : 0.0016)), cx, cy);
      else { G.x -= e.deltaX; G.y -= e.deltaY; apply(); }
    }, { passive: false });
    let P = null;
    VP.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || e.target.closest(".gtool")) return;
      cancelAnimationFrame(G.tween);
      const el = e.target.closest(".gn"), n = el && G.nodes.get(el.dataset.k);
      P = { x: e.clientX, y: e.clientY, gx: G.x, gy: G.y, n, el, moved: false, handle: !!e.target.closest(".ghd"), id: e.pointerId };
      VP.setPointerCapture(e.pointerId);
    });
    VP.addEventListener("pointermove", (e) => {
      if (!P) return;
      const dx = e.clientX - P.x, dy = e.clientY - P.y;
      if (!P.moved && Math.hypot(dx, dy) < 5) return;
      if (!P.moved) {
        P.moved = true;
        if (P.n && !P.handle && (P.n.type === "doc" || P.n.type === "folder")) {
          const w = world(e), ids = P.n.type === "doc" ? (SEL.has(P.n.id) ? [...SEL] : [P.n.id]) : null;
          G.drag = { key: P.n.key, kind: P.n.type, id: P.n.id, ids, from: P.n.type === "doc" && !P.n.inCase ? homeOf(P.n) : P.n.type === "doc" ? homeOf(P.n) : null, el: P.el, ox: w.x - P.n.x, oy: w.y - P.n.y, n: P.n };
          P.el.classList.add("dragging"); VP.classList.add("isdrag");
          if (ids && ids.length > 1) P.el.setAttribute("data-count", ids.length);
        } else VP.classList.add("panning");
      }
      if (G.drag) dragMove(e); else { G.x = P.gx + dx; G.y = P.gy + dy; apply(); }
    });
    const end = (e) => {
      if (!P) return;
      const p = P; P = null; VP.classList.remove("panning");
      try { VP.releasePointerCapture(p.id); } catch (err) { /* ignore */ }
      if (G.drag) return dragEnd(e);
      if (p.moved) return;
      // double click (own detection: with pointer capture the browser sends dblclick to the canvas)
      const now = performance.now(), k = p.n ? p.n.key : "bg", dbl = G.lastClick && G.lastClick.k === k && now - G.lastClick.t < 380 && !p.handle;
      G.lastClick = dbl ? null : { k, t: now };
      if (dbl) {
        if (!p.n) return fit("root");
        if (p.n.type === "doc") return openDoc(p.n.id);
        if (p.n.type !== "more") return zoomInto(p.n.key);
      }
      if (!p.n) { if (SEL.size) { SEL.clear(); refreshSel(); draw(); } return; }
      if (p.handle) return toggle(p.n.key);
      if (p.n.type === "more") { G.limit[p.n.parentKey] = (G.limit[p.n.parentKey] || LIMIT) + 60; return draw(); }
      if (p.n.type === "doc" && (e.ctrlKey || e.metaKey || e.shiftKey)) {
        const cur = G.nodes.get(G.sel); if (!SEL.size && cur && cur.type === "doc" && cur !== p.n) SEL.add(cur.id);
        SEL.has(p.n.id) ? SEL.delete(p.n.id) : SEL.add(p.n.id); refreshSel(); return draw();
      }
      if (SEL.size) { SEL.clear(); refreshSel(); }
      select(p.n.key);
    };
    VP.addEventListener("pointerup", end); VP.addEventListener("pointercancel", end);
    VP.addEventListener("keydown", keys);
  }
  let autoPan = null;
  function dragMove(e) {
    const D = G.drag, w = world(e), r = VP.getBoundingClientRect();
    D.el.style.transition = "none"; D.el.style.transform = `translate(${w.x - D.ox}px,${w.y - D.oy}px) rotate(1.2deg) scale(1.03)`;
    const under = document.elementFromPoint(e.clientX, e.clientY), tel = under && under.closest(".gn"), t = tel && G.nodes.get(tel.dataset.k);
    const k = dropKind(D, t);
    $$(".gn.tgt", WORLD).forEach((x) => x !== tel && x.classList.remove("tgt"));
    let pv = $("#gPrev"), pl = $("#gPrevL");
    if (k) {
      tel.classList.add("tgt"); D.target = { t, k };
      const sx = w.x - D.ox, sy = w.y - D.oy + D.n.h / 2, tx = t.x + (t.type === "doc" || t.type === "case" ? t.w : 0), ty = t.y + t.h / 2;
      const d = t.type === "doc" || t.type === "case" ? arcPath(Math.max(sx + D.n.w, tx), sy, tx, ty) : ePath(t.x + t.w, ty, sx, sy);
      pv.setAttribute("d", d); pv.style.opacity = 1;
      pl.textContent = k.label + (D.ids && D.ids.length > 1 ? ` (${D.ids.length})` : ""); pl.style.opacity = 1;
      pl.style.transform = `translate(${(sx + tx) / 2 + 40}px,${(sy + ty) / 2}px) translate(-50%,-50%)`;
    } else { D.target = null; pv.style.opacity = 0; pl.style.opacity = 0; }
    // auto-pan near the edges
    const ex = e.clientX - r.left, ey = e.clientY - r.top, edge = 46;
    const vx = ex < edge ? 12 : ex > r.width - edge ? -12 : 0, vy = ey < edge ? 12 : ey > r.height - edge ? -12 : 0;
    cancelAnimationFrame(autoPan);
    if (vx || vy) { const last = { clientX: e.clientX, clientY: e.clientY }; const loop = () => { if (!G.drag) return; G.x += vx; G.y += vy; apply(); dragMove(last); }; autoPan = requestAnimationFrame(loop); }
  }
  async function dragEnd(e) {
    cancelAnimationFrame(autoPan);
    const D = G.drag; G.drag = null; VP.classList.remove("isdrag");
    D.el.classList.remove("dragging"); D.el.removeAttribute("data-count"); D.el.style.transition = "";
    $$(".gn.tgt", WORLD).forEach((x) => x.classList.remove("tgt"));
    $("#gPrev").style.opacity = 0; $("#gPrevL").style.opacity = 0;
    if (D.target) {
      const t = D.target.t;
      if (t.type === "folder") G.open.add(t.key);
      if ((t.type === "doc" || t.type === "case") && homeOf(t) != null) G.open.add("f:" + homeOf(t));
      if (t.type === "case") G.open.add(t.key);
      saveOpen();
      const ok = await D.target.k.run(e.ctrlKey || e.altKey);
      if (ok === false) return draw();
      // keep the result in view: open the case file the document now belongs to, then select it
      const after = D.kind === "doc" ? D.ids[0] : null;
      if (after) { const c = DS.components().get(after); if (c) G.open.add("c:" + c.root); saveOpen(); draw(); }
      const key = after ? "d:" + after : D.key;
      if (G.nodes.has(key)) select(key, true);
    } else draw();
  }

  // ---- dragging in from the documents list
  function extDrag(e, ids, label) {
    if (!VP || !VP.isConnected) return;
    const ghost = document.createElement("div"); ghost.className = "gghost"; ghost.textContent = label; document.body.appendChild(ghost);
    G.drag = { kind: "doc", ids, from: null, ext: true, ghost };
    VP.classList.add("isdrag"); document.body.classList.add("extdrag");
    const mv = (ev) => extMove(ev), up = (ev) => { removeEventListener("pointermove", mv); removeEventListener("pointerup", up); removeEventListener("pointercancel", up); extEnd(ev); };
    addEventListener("pointermove", mv); addEventListener("pointerup", up); addEventListener("pointercancel", up);
    extMove(e);
  }
  function extMove(e) {
    const D = G.drag; if (!D || !D.ext) return;
    D.ghost.style.transform = `translate(${e.clientX + 14}px,${e.clientY + 10}px)`;
    const r = VP.getBoundingClientRect(), inside = e.clientX > r.left && e.clientX < r.right && e.clientY > r.top && e.clientY < r.bottom;
    const under = inside && document.elementFromPoint(e.clientX, e.clientY), tel = under && under.closest(".gn"), t = tel && G.nodes.get(tel.dataset.k);
    const k = dropKind(D, t), pv = $("#gPrev"), pl = $("#gPrevL");
    $$(".gn.tgt", WORLD).forEach((x) => x !== tel && x.classList.remove("tgt"));
    if (k) {
      tel.classList.add("tgt"); D.target = { t, k };
      const w = world(e), tx = t.x + (t.type === "doc" || t.type === "case" ? t.w : 0), ty = t.y + t.h / 2;
      pv.setAttribute("d", t.type === "doc" || t.type === "case" ? arcPath(Math.max(w.x, tx), w.y, tx, ty) : ePath(t.x + t.w, ty, w.x, w.y)); pv.style.opacity = 1;
      pl.textContent = k.label + (D.ids.length > 1 ? ` (${D.ids.length})` : ""); pl.style.opacity = 1;
      pl.style.transform = `translate(${(w.x + tx) / 2 + 30}px,${(w.y + ty) / 2}px) translate(-50%,-50%)`;
      D.ghost.classList.add("ok");
    } else { D.target = null; pv.style.opacity = 0; pl.style.opacity = 0; D.ghost.classList.remove("ok"); }
    cancelAnimationFrame(autoPan);
    if (inside) {
      const ex = e.clientX - r.left, ey = e.clientY - r.top, edge = 46, vx = ex < edge ? 12 : ex > r.width - edge ? -12 : 0, vy = ey < edge ? 12 : ey > r.height - edge ? -12 : 0;
      if (vx || vy) { const last = { clientX: e.clientX, clientY: e.clientY }; autoPan = requestAnimationFrame(() => { if (!G.drag) return; G.x += vx; G.y += vy; apply(); extMove(last); }); }
    }
  }
  async function extEnd(e) {
    cancelAnimationFrame(autoPan);
    const D = G.drag; G.drag = null; if (!D) return;
    D.ghost.remove(); VP.classList.remove("isdrag"); document.body.classList.remove("extdrag");
    $$(".gn.tgt", WORLD).forEach((x) => x.classList.remove("tgt")); $("#gPrev").style.opacity = 0; $("#gPrevL").style.opacity = 0;
    if (!D.target) return;
    const t = D.target.t;
    if (t.type === "folder" || t.type === "cat" || t.type === "case") G.open.add(t.key);
    if ((t.type === "doc" || t.type === "case") && homeOf(t) != null) G.open.add("f:" + homeOf(t));
    saveOpen();
    const ok = await D.target.k.run(e.ctrlKey || e.altKey);
    if (ok === false) return;
    reveal(D.ids[0], true);
  }
  // open the path to a document and select it
  function reveal(id, keepTab) {
    const d = docById(id); if (!d || !VP) return;
    const c = DS.components().get(id), MD = MODES[G.mode], root = c ? c.docs[0] : d;
    if (G.mode === "tags") {
      let f = primaryFolder(root) ?? (c ? c.docs.map(primaryFolder).find((x) => x != null) : null);
      if (f == null) G.open.add("g:u");
      for (let t = f; t; t = (byId(M.tags, t) || {}).parent) if (isFolder(t)) G.open.add("f:" + t);
    } else G.open.add(root[MD.field] ? MD.pre + root[MD.field] : "g:u");
    if (c) G.open.add("c:" + c.root);
    saveOpen(); draw();
    if (!G.nodes.has("d:" + id)) {
      // beyond the first entries of a big group: show more until it is visible
      const p = [...G.nodes.values()].find((n) => n.type === "more"); if (p) { G.limit[p.parentKey] = 100000; draw(); }
    }
    if (G.nodes.has("d:" + id)) select("d:" + id, true, keepTab);
  }

  // keyboard
  function keys(e) {
    if (/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
    const n = G.nodes.get(G.sel), r = VP.getBoundingClientRect();
    if (e.key === "+" || e.key === "=") { e.preventDefault(); return zoomAt(G.k * 1.25, r.width / 2, r.height / 2, true); }
    if (e.key === "-") { e.preventDefault(); return zoomAt(G.k / 1.25, r.width / 2, r.height / 2, true); }
    if (e.key === "0" || e.key.toLowerCase() === "f") { e.preventDefault(); return fit(n && e.key.toLowerCase() === "f" ? n.key : "root"); }
    if (e.key.toLowerCase() === "u") return window.treeUndo && window.treeUndo();
    if (!n) { if (e.key.startsWith("Arrow")) { e.preventDefault(); select("root", true); } return; }
    const p = G.nodes.get(G.parent.get(n.key)), sib = p ? p.kids : [n], i = sib.indexOf(n);
    if (e.key === "ArrowLeft") { e.preventDefault(); if (n._open && n.key !== "root") return toggle(n.key, false); if (p) select(p.key, true); }
    if (e.key === "ArrowRight") { e.preventDefault(); if (n.kids && n.kids.length) { if (!n._open) return toggle(n.key, true); select(n.kids[0].key, true); } }
    if (e.key === "ArrowDown") { e.preventDefault(); if (sib[i + 1]) select(sib[i + 1].key, true); }
    if (e.key === "ArrowUp") { e.preventDefault(); if (sib[i - 1]) select(sib[i - 1].key, true); }
    if (e.key === " ") { e.preventDefault(); toggle(n.key); }
    if (e.key === "Enter") { e.preventDefault(); n.type === "doc" ? openDoc(n.id) : zoomInto(n.key); }
  }

  // ================================================================ view
  function renderGraph() {
    $("#years").innerHTML = "";
    if (!VP || !VP.isConnected) {
      $("#main").innerHTML = `<div class="tree"><div class="tleft gwrap">
          <div class="gview" id="gview" tabindex="0"><div class="gworld" id="gworld"><svg class="gsvg" id="gsvg" width="1" height="1"><path id="gPrev" class="gprev"/></svg><div class="glbls" id="glbls"><div class="glabel prev" id="gPrevL"></div></div></div></div>
          <div class="gtool gtl"><div class="tabs small" id="gMode"><button data-m="graph" class="on">Graph</button><button data-m="list">List</button></div>
            <div class="tabs small" id="gGroup">${Object.entries(MODES).map(([k, m]) => `<button data-g="${k}"${G.mode === k ? ' class="on"' : ""}>${m.label}</button>`).join("")}</div><div class="gcrumbs" id="gCrumbs"></div></div>
          <div class="gtool gtr"><button class="ib" id="gOut" title="Zoom out (−)">−</button><span id="gZoom">100%</span><button class="ib" id="gIn" title="Zoom in (+)">+</button><button class="ib" id="gFit" title="Show everything (0)">Fit</button><button class="ib" id="gColl" title="Close all">Collapse</button></div>
          <div class="gtool gbl thint">drag a document onto a document to <b>connect</b> · onto a folder to <b>move</b> (Ctrl/⌥ = also) · double-click to zoom in · scroll to zoom · drag the background to pan</div>
        </div><div class="tright tside" id="tside"></div></div>`;
      VP = $("#gview"); WORLD = $("#gworld"); SVG = $("#gsvg"); LBL = $("#glbls");
      G.els.clear(); G.edges.clear(); G.labels.clear(); G.first = true;
      bind();
      $("#gIn").onclick = () => { const r = VP.getBoundingClientRect(); zoomAt(G.k * 1.3, r.width / 2, r.height / 2, true); };
      $("#gOut").onclick = () => { const r = VP.getBoundingClientRect(); zoomAt(G.k / 1.3, r.width / 2, r.height / 2, true); };
      $("#gFit").onclick = () => fit("root");
      $("#gColl").onclick = () => { G.open = new Set(["root"]); saveOpen(); G.sel = null; draw(); setTimeout(() => fit("root"), 40); };
      $$("#gMode button").forEach((b) => (b.onclick = () => { try { localStorage.setItem("docsorter.treemode", b.dataset.m); } catch (e) { /* ignore */ } VP = null; render(); }));
      $$("#gGroup button").forEach((b) => (b.onclick = () => {
        if (G.mode === b.dataset.g) return;
        G.mode = b.dataset.g; try { localStorage.setItem("docsorter.graph.mode", JSON.stringify(G.mode)); } catch (e) { /* ignore */ }
        $$("#gGroup button").forEach((x) => x.classList.toggle("on", x === b)); segThumb();
        G.sel = null; G.focus = "root"; draw(); setTimeout(() => fit("root", false, true), 40); DS.paintPane && DS.paintPane(null); window.DocPanel.refresh();
      }));
      window.DocPanel.mount(false);
      requestAnimationFrame(() => segThumb && segThumb());
      VP.focus({ preventScroll: true });
    }
    draw(); apply();
    window.DocPanel.refresh();
    if (!G.sel) DS.paintPane && DS.paintPane(null);
  }
  window.renderGraph = renderGraph;
  window.Graph = { fit, select, zoomInto, G, extDrag, reveal };

  // ================================================================ documents panel (right side): find documents and drag them onto the tree
  const DP = { tab: store("docsorter.side", "docs"), q: "", f: "all", limit: 120, list: false };
  const FILTERS = [["all", "All"], ["unfiled", "Not in a folder"], ["new", "New"], ["notype", "No category"], ["due", "Deadline"]];
  function dpFiltered() {
    const words = DP.q.toLowerCase().split(/\s+/).filter(Boolean), since = DS.iso(new Date(DS.today() - 14 * 864e5));
    return DOCS.filter((d) => {
      if (DP.f === "unfiled" && (d.tags || []).some(isFolder)) return false;
      if (DP.f === "new" && (d.added || "") < since) return false;
      if (DP.f === "notype" && d.document_type) return false;
      if (DP.f === "due" && !d.due) return false;
      return !words.length || words.every((w) => docText(d).includes(w));
    }).sort((a, b) => (DP.f === "new" ? (b.added || "").localeCompare(a.added || "") : DP.f === "due" ? (a.due.d || "").localeCompare(b.due.d || "") : (b.created || "").localeCompare(a.created || "")));
  }
  function dpRows() {
    const L = dpFiltered(), box = $("#dpList"); if (!box) return;
    const what = DP.list || G.mode === "tags" ? "a folder to <b>file</b> it" : G.mode === "cats" ? "a category to <b>set</b> it" : "a sender to <b>set</b> it";
    $("#dpHint").innerHTML = `Drag a document onto the tree: onto a document to <b>connect</b>, onto ${what}. ${DP.list ? "" : "Click = show in tree · "}double-click = open · Ctrl-click = select several.`;
    $("#dpCount").textContent = `${L.length} document${L.length === 1 ? "" : "s"}`;
    box.innerHTML = L.slice(0, DP.limit).map((d) => {
      const fol = (d.tags || []).filter(isFolder).map(tagName);
      return `<div class="dprow${SEL.has(d.id) ? " multi" : ""}" data-id="${d.id}"${DP.list ? ' draggable="true"' : ""}><img class="th" loading="lazy" src="/_lib/thumb/${d.id}" alt="" draggable="false">
        <div class="dptx"><b>${esc(d.title || "#" + d.id)}</b><span>${fmt(d.created)}${d.document_type ? " · " + esc(typeName(d.document_type)) : ""}${fol.length ? " · 📁 " + esc(fol.join(", ")) : ""}</span>${d.amount || d.due ? `<em>${d.amount ? amtHtml(d.amount) : ""} ${d.due ? dueHtml(d.due) : ""}</em>` : ""}</div><i class="grip">⋮⋮</i></div>`;
    }).join("") + (L.length > DP.limit ? `<button class="mini" id="dpMore">Show ${Math.min(200, L.length - DP.limit)} more</button>` : "") + (!L.length ? `<p class="thint" style="padding:12px">No documents.</p>` : "");
    if ($("#dpMore")) $("#dpMore").onclick = () => { DP.limit += 200; dpRows(); };
    $$(".dprow", box).forEach((row) => {
      const id = +row.dataset.id, ids = () => (SEL.has(id) ? [...SEL] : [id]), d = docById(id);
      if (DP.list) {
        row.ondragstart = (e) => { const L = ids(); window.DS.drag = { kind: "doc", ids: L, from: null, label: L.length > 1 ? L.length + " documents" : d.title }; e.dataTransfer.effectAllowed = "copyMove"; try { e.dataTransfer.setData("text/plain", String(id)); } catch (err) { /* ignore */ } };
        row.ondragend = () => (window.DS.drag = null);
      } else {
        row.onpointerdown = (e) => {
          if (e.button !== 0) return;
          const sx = e.clientX, sy = e.clientY;
          const mv = (ev) => { if (Math.hypot(ev.clientX - sx, ev.clientY - sy) > 5) { cleanup(); const L = ids(); extDrag(ev, L, L.length > 1 ? L.length + " documents" : d.title); } };
          const up = () => cleanup();
          const cleanup = () => { removeEventListener("pointermove", mv); removeEventListener("pointerup", up); };
          addEventListener("pointermove", mv); addEventListener("pointerup", up);
        };
      }
      let last = 0;
      row.onclick = (e) => {
        if (e.ctrlKey || e.metaKey || e.shiftKey) { SEL.has(id) ? SEL.delete(id) : SEL.add(id); row.classList.toggle("multi", SEL.has(id)); refreshSel(); return; }
        const now = Date.now(); if (now - last < 380) return openDoc(id); last = now;
        if (!DP.list && window.Graph) reveal(id, true);
      };
    });
  }
  function dpTab(t) {
    DP.tab = t; try { localStorage.setItem("docsorter.side", JSON.stringify(t)); } catch (e) { /* ignore */ }
    if (!$("#tdocs")) return;
    $("#tdocs").hidden = t !== "docs"; $("#tpane").hidden = t !== "info";
    $$("#pTabs button").forEach((b) => b.classList.toggle("on", b.dataset.p === t)); segThumb();
  }
  window.DocPanel = {
    mount(list) {
      DP.list = !!list;
      const side = $("#tside"); if (!side) return;
      side.innerHTML = `<div class="tabs small ptabs" id="pTabs"><button data-p="docs">Documents</button><button data-p="info">Details</button></div>
        <div class="tdocs" id="tdocs"><div class="dphead"><input id="dpQ" placeholder="Find a document…" value="${esc(DP.q)}"><span class="thint" id="dpCount"></span></div>
          <div class="dpchips">${FILTERS.map(([k, l]) => `<button class="chip${DP.f === k ? " on" : ""}" data-f="${k}">${l}</button>`).join("")}</div>
          <p class="thint dphint" id="dpHint"></p>
          <div class="dplist" id="dpList"></div></div><div class="tpane" id="tpane"></div>`;
      $$("#pTabs button").forEach((b) => (b.onclick = () => dpTab(b.dataset.p)));
      let qt; $("#dpQ").oninput = (e) => { clearTimeout(qt); qt = setTimeout(() => { DP.q = e.target.value.trim(); DP.limit = 120; dpRows(); }, 120); };
      $$(".dpchips .chip").forEach((c) => (c.onclick = () => { DP.f = c.dataset.f; DP.limit = 120; $$(".dpchips .chip").forEach((x) => x.classList.toggle("on", x === c)); dpRows(); }));
      dpTab(DP.tab); dpRows();
    },
    refresh() { if ($("#dpList") && document.activeElement !== $("#dpQ")) dpRows(); else if ($("#dpList")) dpRows(); },
    tab: dpTab,
  };
})();
