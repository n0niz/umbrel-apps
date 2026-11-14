/* Doc Sorter – smooth tree component (TreeUI) + the document tree.

   TreeUI: nested nodes, children are built only when a node is opened and slide open
   (CSS grid-rows transition). Each row has a title line and a small detail line.
   Keyboard: ↑↓ move, → open, ← close/parent, Enter = open document, Space = expand.
   Drag & drop with auto-open when you hover a closed node while dragging.

   Document tree: New · Deadlines · Folders > case files > documents · Not in a folder.
   Drag a document onto a document = connect (and file it alongside), onto a folder = move
   (Ctrl/⌥ = also), onto "Not in a folder" = take it out; folders onto folders = nest. U = undo. */
(function () {
  // ================================================================ helpers
  const fmt = (d) => (d || "").slice(0, 10).split("-").reverse().join(".");
  const money = (a) => a ? (a.s ? "≈ " : "") + new Intl.NumberFormat("de-DE", { style: "currency", currency: a.c || "EUR" }).format(a.v) + (a.p === "month" ? "/mo" : a.p === "year" ? "/yr" : a.p === "quarter" ? "/qtr" : "") : "";
  const today = () => new Date(new Date().toDateString());
  const iso = (x) => { const z = new Date(x - x.getTimezoneOffset() * 6e4); return z.toISOString().slice(0, 10); };
  function rel(d, k) {
    if (!d) return "";
    const days = Math.round((new Date(d + "T00:00") - today()) / 864e5);
    if (days < 0 && k === "sug") return days === -1 ? "yesterday" : `${-days} days ago`;
    if (days < 0 && k === "contract") return -days < 60 ? `ended ${-days} days ago` : -days < 400 ? `ended ${Math.round(-days / 30)} months ago` : "ended";
    if (days === 0) return "today";
    if (days === 1) return "tomorrow";
    if (days === -1) return "yesterday";
    if (days < 0) return `${-days} days overdue`;
    if (days < 14) return `in ${days} days`;
    if (days < 70) return `in ${Math.round(days / 7)} weeks`;
    return `in ${Math.round(days / 30)} months`;
  }
  const dueCls = (d, k, sug) => { const days = Math.round((new Date(d + "T00:00") - today()) / 864e5); return days < 0 ? (k === "contract" || sug ? "past" : "over") : days <= 7 ? "soon" : ""; };
  const KIND = { payment: "payment due", debit: "direct debit", objection: "objection deadline", contract: "contract / validity ends", deadline: "deadline", appointment: "appointment" };
  const dueHtml = (u) => u ? `<span class="due ${dueCls(u.d, u.k, u.s)}${u.s ? " sug" : ""}" title="${u.s ? "Found in the text – not saved yet" : "Deadline"}${u.k ? " · " + esc(KIND[u.k] || u.k) : ""}">⏰ ${u.s ? "≈ " : ""}${fmt(u.d)} · ${rel(u.d, u.s && u.k !== "contract" ? "sug" : u.k)}</span>` : "";
  const amtHtml = (a) => a ? `<span class="amt${a.s ? " sug" : ""}${a.k === "refund" ? " refund" : ""}" title="${a.s ? "Found in the text – not saved yet" : "Amount"}${a.k === "refund" ? " · refund / credit" : ""}">${a.k === "refund" ? "↩ " : ""}${money(a)}</span>` : "";
  window.DS = Object.assign(window.DS || {}, { fmt, money, rel, dueHtml, amtHtml, dueCls, KIND, iso, today });

  // ================================================================ TreeUI
  function TreeUI(root, o) {
    const T = { root, o, nodes: new Map(), sel: null, drag: null, hoverT: null };
    root.classList.add("tui");
    root.tabIndex = 0;
    const after = (scope) => (o.afterRender || (() => {}))(T, scope);

    function rowHtml(n, depth) {
      const can = n.kids !== undefined && !n.leaf;
      return `<div class="tn${n.open && can ? " open" : ""}${n.cls ? " " + n.cls : ""}" data-k="${esc(n.key)}" style="--d:${depth}" role="treeitem"${can ? ` aria-expanded="${!!n.open}"` : ""}>`
        + `<div class="tr${T.sel === n.key ? " sel" : ""}"${n.drag ? ' draggable="true"' : ""}>`
        + `<span class="car">${can ? "<i></i>" : ""}</span>${n.icon || ""}`
        + `<div class="tx"><div class="t1"><span class="lb">${n.label}</span>${n.badges || ""}</div>${n.detail ? `<div class="t2">${n.detail}</div>` : ""}</div>`
        + (n.actions ? `<span class="ac">${n.actions}</span>` : "") + (n.count !== undefined ? `<span class="cnt">${n.count}</span>` : "")
        + `</div>${can ? `<div class="tk"><div class="tki"${n.open ? ' data-built="1"' : ""}>${n.open ? kidsHtml(n, depth + 1) : ""}</div></div>` : ""}</div>`;
    }
    function kids(n) { n._k = typeof n.kids === "function" ? n.kids() : n.kids || []; return n._k; }
    function kidsHtml(n, depth) {
      const L = kids(n), max = n._shown || n.max || 150;
      let h = L.slice(0, max).map((c) => { T.nodes.set(c.key, c); c._parent = n.key; return c.html ? `<div class="tn tinfo" data-k="${esc(c.key)}" style="--d:${depth}">${c.html}</div>` : rowHtml(c, depth); }).join("");
      if (L.length > max) h += `<div class="tn tmore" data-more="${esc(n.key)}" style="--d:${depth}"><div class="tr"><span class="car"></span><div class="tx"><div class="t2">+ ${L.length - max} more – click to show</div></div></div></div>`;
      if (!L.length && n.empty) h = `<div class="tn tinfo" style="--d:${depth}"><div class="tempty">${n.empty}</div></div>`;
      return h;
    }
    T.render = function (top) {
      const st = root.scrollTop;
      T.nodes.clear();
      top.forEach((n) => T.nodes.set(n.key, n));
      root.classList.add("noanim");
      root.innerHTML = top.map((n) => rowHtml(n, 0)).join("");
      root.scrollTop = st;
      requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove("noanim")));
      after(root);
    };
    const nodeEl = (k) => root.querySelector(`.tn[data-k="${CSS.escape(k)}"]`);
    T.el = nodeEl;
    T.toggle = function (k, force) {
      const n = T.nodes.get(k), el = nodeEl(k);
      if (!n || !el || n.kids === undefined || n.leaf) return;
      const open = force !== undefined ? force : !el.classList.contains("open");
      if (open === el.classList.contains("open")) return;
      n.open = open;
      (o.onToggle || (() => {}))(n, open);
      const inner = el.querySelector(":scope > .tk > .tki");
      if (open) {
        if (!inner.dataset.built) { inner.innerHTML = kidsHtml(n, +el.style.getPropertyValue("--d") + 1); inner.dataset.built = "1"; after(inner); }
        requestAnimationFrame(() => { el.classList.add("open"); el.setAttribute("aria-expanded", "true"); });
      } else { el.classList.remove("open"); el.setAttribute("aria-expanded", "false"); }
    };
    T.select = function (k, scroll) {
      T.sel = k;
      root.querySelectorAll(".tr.sel").forEach((x) => x.classList.remove("sel"));
      const el = nodeEl(k);
      if (el) { const r = el.querySelector(":scope > .tr"); if (r) { r.classList.add("sel"); if (scroll) r.scrollIntoView({ block: "nearest" }); } }
      (o.onSelect || (() => {}))(T.nodes.get(k));
    };
    T.visible = () => [...root.querySelectorAll(".tn > .tr")].filter((r) => { const p = r.parentElement.parentElement; return !(p && p.closest(".tn:not(.open)")) && !r.parentElement.classList.contains("tmore"); }).map((r) => r.parentElement);

    // ---- events (delegated)
    root.addEventListener("click", (e) => {
      const more = e.target.closest("[data-more]");
      if (more) { const n = T.nodes.get(more.dataset.more); n._shown = (n._shown || n.max || 150) + 300; const el = nodeEl(n.key); const inner = el.querySelector(":scope > .tk > .tki"); inner.innerHTML = kidsHtml(n, +el.style.getPropertyValue("--d") + 1); after(inner); return; }
      const tn = e.target.closest(".tn"); if (!tn || tn.classList.contains("tinfo")) return;
      const n = T.nodes.get(tn.dataset.k); if (!n) return;
      const btn = e.target.closest(".ac button");
      if (btn) return (o.onAction || (() => {}))(n, btn, e);
      if (e.target.closest(".car")) return T.toggle(n.key);
      if ((o.onClick || (() => false))(n, e)) return;
      if (T.sel === n.key && n.kids !== undefined && !n.leaf) T.toggle(n.key);
      T.select(n.key);
    });
    root.addEventListener("dblclick", (e) => { const tn = e.target.closest(".tn"); if (!tn || tn.classList.contains("tinfo") || e.target.closest(".ac,.car")) return; const n = T.nodes.get(tn.dataset.k); if (n && o.onOpen) o.onOpen(n); });
    root.addEventListener("keydown", (e) => {
      if (/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
      const vis = T.visible(), i = vis.findIndex((el) => el.dataset.k === T.sel), n = T.nodes.get(T.sel);
      const go = (el) => el && T.select(el.dataset.k, true);
      if (e.key === "ArrowDown") { e.preventDefault(); return go(i < 0 ? vis[0] : vis[Math.min(vis.length - 1, i + 1)]); }
      if (e.key === "ArrowUp") { e.preventDefault(); return go(vis[Math.max(0, i - 1)]); }
      if (!n) return;
      const el = nodeEl(n.key), can = n.kids !== undefined && !n.leaf;
      if (e.key === "ArrowRight") { e.preventDefault(); if (can && !el.classList.contains("open")) return T.toggle(n.key, true); return go(vis[i + 1]); }
      if (e.key === "ArrowLeft") { e.preventDefault(); if (can && el.classList.contains("open")) return T.toggle(n.key, false); const p = el.parentElement.closest(".tn"); return p && go(p); }
      if (e.key === " ") { e.preventDefault(); return can && T.toggle(n.key); }
      if (e.key === "Enter") { e.preventDefault(); return o.onOpen ? o.onOpen(n) : T.toggle(n.key); }
      (o.onKey || (() => {}))(e, n);
    });
    // drag & drop
    root.addEventListener("dragstart", (e) => {
      const tn = e.target.closest(".tn"); const n = tn && T.nodes.get(tn.dataset.k);
      if (!n || !n.drag) return e.preventDefault();
      T.drag = typeof n.drag === "function" ? n.drag() : n.drag;
      window.DS.drag = T.drag;
      e.dataTransfer.effectAllowed = "copyMove";
      try { e.dataTransfer.setData("text/plain", n.key); } catch (err) { /* ignore */ }
      const g = document.createElement("div"); g.className = "ghost"; g.textContent = T.drag.label || "1 item";
      document.body.appendChild(g); e.dataTransfer.setDragImage(g, 12, 12); setTimeout(() => g.remove(), 0);
      root.classList.add("dragging");
    });
    root.addEventListener("dragend", () => { T.drag = null; window.DS.drag = null; root.classList.remove("dragging"); root.querySelectorAll(".drop").forEach((x) => x.classList.remove("drop")); clearTimeout(T.hoverT); });
    root.addEventListener("dragover", (e) => {
      const d = T.drag || window.DS.drag; const tn = e.target.closest(".tn"); const n = tn && T.nodes.get(tn.dataset.k);
      if (!d || !n || !n.drop || !(n.dropOk ? n.dropOk(d) : true)) return;
      e.preventDefault(); e.dataTransfer.dropEffect = e.ctrlKey || e.altKey ? "copy" : "move";
      const r = tn.querySelector(":scope > .tr");
      if (r && !r.classList.contains("drop")) {
        root.querySelectorAll(".drop").forEach((x) => x.classList.remove("drop")); r.classList.add("drop");
        clearTimeout(T.hoverT);
        if (n.kids !== undefined && !n.leaf && n.type !== "doc" && !tn.classList.contains("open")) T.hoverT = setTimeout(() => T.toggle(n.key, true), 650);
      }
    });
    root.addEventListener("drop", (e) => {
      const d = T.drag || window.DS.drag; const tn = e.target.closest(".tn"); const n = tn && T.nodes.get(tn.dataset.k);
      root.querySelectorAll(".drop").forEach((x) => x.classList.remove("drop")); clearTimeout(T.hoverT);
      if (!d || !n || !n.drop || !(n.dropOk ? n.dropOk(d) : true)) return;
      e.preventDefault(); T.drag = null; window.DS.drag = null;
      n.drop(d, e.ctrlKey || e.altKey);
    });
    return T;
  }
  window.TreeUI = TreeUI;

  // ================================================================ document tree model
  const OPENK = "docsorter.tree.open2";
  let OPEN;
  try { OPEN = new Set(JSON.parse(localStorage.getItem(OPENK) || '["g:new","g:due","g:folders","g:unfiled"]')); } catch (e) { OPEN = new Set(["g:new", "g:due", "g:folders", "g:unfiled"]); }
  const saveOpen = () => { try { localStorage.setItem(OPENK, JSON.stringify([...OPEN])); } catch (e) { /* ignore */ } };
  let TREE = null, UNDO = [], CLS = {};
  const SNIP = {};
  const docById = (id) => DOCS.find((d) => d.id === id);
  const typeName = (id) => (byId(M.types, id) || {}).name || "";
  const corrName = (id) => (byId(M.corrs, id) || {}).name || "";

  function linkMap() {
    const L = new Map(), add = (a, b) => { if (!L.has(a)) L.set(a, new Set()); L.get(a).add(b); };
    DOCS.forEach((d) => (d.links || []).forEach((x) => { if (docById(x)) { add(d.id, x); add(x, d.id); } }));
    return L;
  }
  function components() {
    const L = linkMap(), comp = new Map(), seen = new Set();
    DOCS.forEach((d) => {
      if (seen.has(d.id) || !L.has(d.id)) return;
      const st = [d.id], mem = []; seen.add(d.id);
      while (st.length) { const x = st.pop(); mem.push(x); (L.get(x) || []).forEach((y) => { if (!seen.has(y)) { seen.add(y); st.push(y); } }); }
      const docs = mem.map(docById).filter(Boolean).sort((a, b) => (a.created || "").localeCompare(b.created || "") || a.id - b.id);
      const c = { root: docs[0].id, docs };
      docs.forEach((m) => comp.set(m.id, c));
    });
    return comp;
  }
  function sumHtml(docs) {
    const one = docs.filter((d) => d.amount && !d.amount.p && d.amount.k !== "refund");
    if (one.length < 2) return "";
    const s = one.reduce((a, d) => a + d.amount.v, 0), sug = one.some((d) => d.amount.s);
    return `<span class="amt${sug ? " sug" : ""}" title="Sum of the one-off amounts${sug ? " (incl. amounts found in the text, not saved yet)" : ""}">Σ ${money({ v: s, c: "EUR" })}</span>`;
  }
  const nextDue = (docs) => { const t = iso(today()); const L = docs.filter((d) => d.due && d.due.d >= t).sort((a, b) => a.due.d.localeCompare(b.due.d)); return L[0] ? dueHtml(L[0].due) : ""; };

  function docNode(d, ctx, extra = {}) {
    const key = `d:${ctx}:${d.id}${extra.suffix || ""}`, nf = (d.tags || []).filter(isFolder).length, ai = AI.results[d.id];
    const detail = [fmt(d.created), typeName(d.document_type), corrName(d.correspondent)].filter(Boolean).map(esc).join(" · ")
      + (d.amount ? " · " + amtHtml(d.amount) : "") + (d.due ? " " + dueHtml(d.due) : "");
    return {
      key, type: "doc", id: d.id, ctx, cls: (extra.cls || "tdoc") + (SEL.has(d.id) ? " multi" : ""),
      icon: `<img class="th" loading="lazy" src="/_lib/thumb/${d.id}" alt="">`,
      label: esc(d.title || d.original_file_name || "#" + d.id), detail,
      badges: (extra.elsewhere ? `<span class="bd" title="Connected here, filed in another folder">↗</span>` : "") + (nf > 1 ? `<span class="bd" title="In ${nf} folders">×${nf}</span>` : "") + (ai ? `<span class="bd ai" title="AI suggestion waiting">✨</span>` : "") + ((d.links || []).length && !extra.inCase ? `<span class="bd" title="Connected documents">🔗${d.links.length}</span>` : ""),
      actions: (extra.inCase ? `<button data-a="cut" title="Take out of this case file">✂</button>` : "") + `<button data-a="open" title="Open document view (Enter)">↗</button>`,
      open: OPEN.has(key), kids: () => [{ key: "i:" + key, html: infoHtml(d), id: d.id }],
      drag: () => { const ids = SEL.has(d.id) ? [...SEL] : [d.id]; return { kind: "doc", ids, from: typeof ctx === "number" ? ctx : null, label: ids.length > 1 ? ids.length + " documents" : d.title }; },
      drop: (dr, also) => connect(dr, d, typeof ctx === "number" ? ctx : null, also),
      dropOk: (dr) => dr.kind === "doc" && !dr.ids.includes(d.id),
    };
  }
  function infoHtml(d) {
    const fol = (d.tags || []).filter(isFolder).map((t) => `<span class="chip">📁 ${esc(tagName(t))}</span>`).join("") || `<span class="mut">not in a folder</span>`;
    const facts = (d.amount ? `<span class="chip">${amtHtml(d.amount)}${d.amount.s ? ` <button class="lnk" data-save="amount" data-id="${d.id}" title="Save as field “Betrag” in Paperless">✓ save</button>` : ""}</span>` : "")
      + (d.due ? `<span class="chip">${dueHtml(d.due)}${d.due.s ? ` <button class="lnk" data-save="due" data-id="${d.id}" title="Save as field “Frist” in Paperless">✓ save</button>` : ""}</span>` : "");
    return `<div class="info"><div class="irow">${fol}${facts}</div><div class="snip" data-snip="${d.id}">${SNIP[d.id] !== undefined ? esc(SNIP[d.id]) : "…"}</div>
      <div class="irow"><button class="mini" data-act="open" data-id="${d.id}">Open document view</button><button class="mini ai" data-act="ai" data-id="${d.id}">✨ Recognize</button><button class="mini" data-act="classify" data-id="${d.id}">📁 Suggest folder</button></div></div>`;
  }
  function itemsOf(docs, ctx, C) {
    const used = new Set(), out = [];
    docs.slice().sort((a, b) => (b.created || "").localeCompare(a.created || "")).forEach((d) => {
      if (used.has(d.id)) return;
      const c = C.COMP.get(d.id);
      if (c) { c.docs.forEach((m) => used.add(m.id)); if (c.docs.some(C.match)) out.push({ c, date: c.docs[c.docs.length - 1].created || "" }); }
      else if (C.match(d)) out.push({ d, date: d.created || "" });
    });
    out.sort((a, b) => b.date.localeCompare(a.date));
    return out.map((it) => (it.d ? docNode(it.d, ctx) : caseNode(it.c, ctx)));
  }
  function caseNode(c, ctx) {
    const root = c.docs[0], last = c.docs[c.docs.length - 1], key = `c:${ctx}:${c.root}`;
    const types = [...new Set(c.docs.map((d) => typeName(d.document_type)).filter(Boolean))].slice(0, 3).join(" → ");
    return {
      key, type: "case", root: c.root, ctx, cls: "tcase", icon: `<span class="ic">🗂</span>`, label: esc(root.title), count: c.docs.length,
      detail: `case file · ${fmt(root.created).slice(3)} → ${fmt(last.created).slice(3)}${types ? " · " + esc(types) : ""} ${sumHtml(c.docs)} ${nextDue(c.docs)}`,
      open: OPEN.has(key) || !!Q,
      kids: () => c.docs.map((m) => docNode(m, ctx, { suffix: ":c", inCase: true, cls: "tdoc incase", elsewhere: typeof ctx === "number" && !(m.tags || []).includes(ctx) })),
      drop: (dr, also) => connect(dr, root, typeof ctx === "number" ? ctx : null, also),
      dropOk: (dr) => dr.kind === "doc",
    };
  }
  function folderNode(t, C) {
    const key = "f:" + t.id, ds = C.inFolder(t.id), subs = C.kidsOf[t.id] || [];
    const nCase = new Set(ds.map((d) => C.COMP.get(d.id)).filter(Boolean)).size;
    return {
      key, type: "folder", id: t.id, cls: "tfold", icon: `<span class="dot" style="background:${esc(t.color || "#64748b")}"></span>`,
      label: esc(t.name), count: ds.length,
      detail: `${ds.length} document${ds.length === 1 ? "" : "s"}${nCase ? ` · ${nCase} case file${nCase > 1 ? "s" : ""}` : ""}${subs.length ? ` · ${subs.length} subfolder${subs.length > 1 ? "s" : ""}` : ""} ${sumHtml(ds)} ${nextDue(ds)}`,
      open: OPEN.has(key) || (!!Q && ds.some(C.match)), actions: `<button data-a="sub" title="New subfolder">+</button>`,
      empty: "Empty – drop documents here",
      kids: () => [...subs.filter((s) => !Q || C.inFolder(s.id).some(C.match) || s.name.toLowerCase().includes(Q.toLowerCase())).map((s) => folderNode(s, C)), ...itemsOf(C.direct[t.id] || [], t.id, C)],
      drag: { kind: "folder", id: t.id, label: "📁 " + t.name },
      drop: (dr, also) => (dr.kind === "folder" ? nest(dr.id, t.id) : moveTo(dr, t.id, also)),
      dropOk: (dr) => dr.kind === "doc" || (dr.kind === "folder" && dr.id !== t.id && !descIds(dr.id).has(t.id) && byId(M.tags, dr.id).parent !== t.id),
    };
  }

  function model() {
    const words = (Q || "").toLowerCase().split(/\s+/).filter(Boolean);
    const match = (d) => !words.length || words.every((w) => docText(d).includes(w));
    const fold = folders(), kidsOf = {}, direct = {}, COMP = components(), cache = {};
    fold.forEach((t) => { const p = t.parent && isFolder(t.parent) ? t.parent : 0; (kidsOf[p] = kidsOf[p] || []).push(t); });
    Object.values(kidsOf).forEach((l) => l.sort((a, b) => a.name.localeCompare(b.name)));
    DOCS.forEach((d) => (d.tags || []).filter(isFolder).forEach((t) => (direct[t] = direct[t] || []).push(d)));
    const inFolder = (tid) => cache[tid] || (cache[tid] = (() => { const ids = descIds(tid); return DOCS.filter((d) => (d.tags || []).some((t) => ids.has(t))); })());
    const C = { kidsOf, direct, COMP, match, inFolder };
    const t0 = today(), since = iso(new Date(t0 - 14 * 864e5)), fromDue = iso(new Date(t0 - 30 * 864e5)), now = iso(t0);
    void fromDue;
    const fresh = DOCS.filter((d) => (d.added || "") >= since && match(d)).sort((a, b) => (b.added || "").localeCompare(a.added || ""));
    const dues = DOCS.filter((d) => d.due && (d.due.s ? d.due.d >= now : d.due.d >= fromDue) && match(d)).sort((a, b) => a.due.d.localeCompare(b.due.d));
    const unf = DOCS.filter((d) => !(d.tags || []).some(isFolder));
    const over = dues.filter((d) => d.due.d < now && !d.due.s && d.due.k !== "contract").length, nxt = dues.find((d) => d.due.d >= now);
    return [
      { key: "g:new", type: "group", cls: "tgrp", icon: `<span class="ic">📥</span>`, label: "New", count: fresh.length,
        detail: fresh.length ? `added in the last 14 days · ${fresh.filter((d) => !(d.tags || []).some(isFolder)).length} not in a folder yet` : "nothing new in the last 14 days",
        open: OPEN.has("g:new"), kids: () => fresh.map((d) => docNode(d, "new")), empty: "Nothing new." },
      { key: "g:due", type: "group", cls: "tgrp", icon: `<span class="ic">⏰</span>`, label: "Deadlines", count: dues.length,
        detail: dues.length ? `${over ? `<span class="due over">${over} overdue</span> · ` : ""}${nxt ? `next: ${esc(nxt.title)} ${dueHtml(nxt.due)}` : ""}` : "none found – they are read from the text, or set the field “Frist”",
        open: OPEN.has("g:due"), kids: () => dues.map((d) => docNode(d, "due")), empty: "No deadlines." },
      { key: "g:folders", type: "group", cls: "tgrp", icon: `<span class="ic">📚</span>`, label: "Folders", count: fold.length,
        detail: `${DOCS.length - unf.length} of ${DOCS.length} documents filed · ${new Set([...COMP.values()]).size} case files`,
        open: OPEN.has("g:folders") || !!Q, actions: `<button data-a="newf" title="New folder">+ folder</button>`, empty: "No folders yet – click “+ folder”.",
        kids: () => (kidsOf[0] || []).filter((t) => !Q || inFolder(t.id).some(match) || t.name.toLowerCase().includes(Q.toLowerCase())).map((t) => folderNode(t, C)),
        drop: (dr) => nest(dr.id, null), dropOk: (dr) => dr.kind === "folder" && !!byId(M.tags, dr.id).parent },
      { key: "g:unfiled", type: "group", cls: "tgrp", icon: `<span class="ic">📂</span>`, label: "Not in a folder", count: unf.length,
        detail: unf.length ? "drag them onto a folder – or onto a document they belong to" : "everything is filed 🎉",
        open: OPEN.has("g:unfiled") || !!Q, kids: () => itemsOf(unf, "unfiled", C), max: 100, empty: "Everything is filed.",
        drop: (dr) => takeOut(dr), dropOk: (dr) => dr.kind === "doc" && !!dr.from },
    ];
  }

  // ================================================================ actions with undo
  async function run(label, ops, inverse) {
    try {
      for (const [p, b] of ops) await api(p, b);
      UNDO.push({ label, inverse }); if (UNDO.length > 30) UNDO.shift();
      SEL.clear(); refreshSel();
      toastUndo(label); await reloadDocs();
      return true;
    } catch (e) { toast(e.message, 6000); return false; }
  }
  function toastUndo(label) {
    const t = $("#toast");
    t.innerHTML = `${esc(label)} <button id="undoBtn" class="undo">Undo</button>`;
    t.style.display = "block"; clearTimeout(t._t); t._t = setTimeout(() => (t.style.display = "none"), 7000);
    $("#undoBtn").onclick = undo;
  }
  async function undo() {
    const u = UNDO.pop(); if (!u) return toast("Nothing to undo");
    try { for (const [p, b] of u.inverse) await api(p, b); toast("Undone: " + u.label); await reloadDocs(); } catch (e) { toast(e.message, 6000); }
  }
  const tagOps = (ids, add, remove) => [["/_lib/api/bulk", { ids, add_tags: add, remove_tags: remove }]];
  const nm = (ids) => (ids.length > 1 ? ids.length + " documents" : "“" + ((docById(ids[0]) || {}).title || "") + "”");
  function connect(dr, target, ctx, also) {
    const ids = dr.ids.filter((i) => i !== target.id);
    if (!ids.length) return;
    const ops = ids.map((i) => ["/_lib/api/link", { a: i, b: target.id }]), inv = ids.map((i) => ["/_lib/api/link", { a: i, b: target.id, on: false }]);
    const notThere = ctx ? ids.filter((i) => !((docById(i) || {}).tags || []).includes(ctx)) : [];
    if (ctx && notThere.length) { const rm = !also && dr.from && dr.from !== ctx ? [dr.from] : []; ops.push(...tagOps(notThere, [ctx], rm)); inv.push(...tagOps(notThere, rm, [ctx])); }
    // keep the resulting case file open: its first document is the oldest of all involved
    const comp = components(), all = new Map();
    [target.id, ...ids].forEach((i) => ((comp.get(i) || { docs: [docById(i)] }).docs || []).forEach((d) => d && all.set(d.id, d)));
    const oldest = [...all.values()].sort((a, b) => (a.created || "").localeCompare(b.created || "") || a.id - b.id)[0];
    if (oldest) { OPEN.add(`c:${ctx || "unfiled"}:${oldest.id}`); saveOpen(); }
    return run(`🔗 ${nm(ids)} connected with “${target.title}”${ctx && notThere.length ? " in " + tagName(ctx) : ""}`, ops, inv);
  }
  function moveTo(dr, tid, also) {
    const ids = dr.ids, rm = !also && dr.from && dr.from !== tid ? [dr.from] : [];
    const add = ids.filter((i) => !((docById(i) || {}).tags || []).includes(tid));
    OPEN.add("f:" + tid); saveOpen();
    return run(`${nm(ids)} ${rm.length ? "moved" : "added"} to “${tagName(tid)}”`, tagOps(ids, [tid], rm), tagOps(ids, rm, add.length ? [tid] : []));
  }
  const takeOut = (dr) => run(`${nm(dr.ids)} taken out of “${tagName(dr.from)}”`, tagOps(dr.ids, [], [dr.from]), tagOps(dr.ids, [dr.from], []));
  function nest(src, to) {
    const t = byId(M.tags, src);
    if (to) { OPEN.add("f:" + to); saveOpen(); }
    return run(`“${t.name}” → ${to ? "“" + tagName(to) + "”" : "top level"}`, [[`/_lib/api/folder/${src}/edit`, { parent: to }]], [[`/_lib/api/folder/${src}/edit`, { parent: t.parent || null }]]);
  }
  function cut(id) {
    const d = docById(id), links = (d.links || []).filter(docById);
    if (links.length) run(`✂ “${d.title}” taken out of its case file`, links.map((x) => ["/_lib/api/link", { a: id, b: x, on: false }]), links.map((x) => ["/_lib/api/link", { a: id, b: x }]));
  }
  function saveFact(id, kind, value) {
    const d = docById(id) || {};
    const v = value !== undefined ? value : kind === "amount" ? d.amount && { v: d.amount.v, c: d.amount.c } : d.due && d.due.d;
    if (!v) return;
    const old = kind === "amount" ? (d.amount && !d.amount.s ? { v: d.amount.v, c: d.amount.c } : null) : (d.due && !d.due.s ? d.due.d : null);
    return run(kind === "amount" ? `💶 ${money({ v: v.v, c: v.c })} saved as “Betrag” for “${d.title}”` : `⏰ ${fmt(v)} saved as “Frist” for “${d.title}”`, [[`/_lib/api/doc/${id}/facts`, { [kind]: v }]], [[`/_lib/api/doc/${id}/facts`, { [kind]: old }]]);
  }
  async function classOf(id) {
    if (!(id in CLS)) { try { Object.assign(CLS, await api("/_lib/api/suggest_folders", { ids: [id] })); } catch (e) { CLS[id] = null; } }
    return CLS[id];
  }
  async function classify(id, from) {
    const s = await classOf(id);
    if (!s) return toast("No suggestion yet – there are no similar documents in folders");
    if (((docById(id) || {}).tags || []).includes(s.id)) return toast(`Already in “${s.name}”, where the most similar documents are`);
    moveTo({ kind: "doc", ids: [id], from }, s.id, false);
  }
  function newFolder(parent, anchorKey) {
    const el = TREE.el(anchorKey); if (!el) return;
    const inp = document.createElement("input");
    inp.className = "newin"; inp.placeholder = (parent ? `Subfolder of “${tagName(parent)}”` : "New folder") + " – Enter";
    el.querySelector(":scope > .tr").after(inp); inp.focus();
    inp.onkeydown = async (e) => {
      e.stopPropagation();
      if (e.key === "Escape") return inp.remove();
      if (e.key !== "Enter" || !inp.value.trim()) return;
      try { const t = await api("/_lib/api/create/tag", { name: inp.value.trim(), parent: parent || null }); OPEN.add(parent ? "f:" + parent : "g:folders"); saveOpen(); toast(`Folder “${t.name}” created – drag documents onto it`); await reloadDocs(); } catch (err) { toast(err.message, 5000); }
    };
    inp.onblur = () => setTimeout(() => inp.remove(), 150);
  }

  // ================================================================ render
  function renderTree() {
    $("#years").innerHTML = "";
    if (!TREE || !$("#tlist")) {
      $("#main").innerHTML = `<div class="tree"><div class="tleft"><div class="thead"><b>Document tree</b><span class="thint">drag a document onto a document to connect · onto a folder to move (Ctrl/⌥ = also) · ↑↓←→ Space Enter · C = suggest folder · U = undo</span></div><div id="tlist"></div></div><div class="tright" id="tpane"></div></div>`;
      TREE = TreeUI($("#tlist"), {
        onToggle: (n, open) => { open ? OPEN.add(n.key) : OPEN.delete(n.key); saveOpen(); },
        onSelect: (n) => paintPane(n),
        onOpen: (n) => (n.type === "doc" ? openDoc(n.id) : TREE.toggle(n.key)),
        onClick: (n, e) => {
          if (n.type !== "doc" || !(e.ctrlKey || e.metaKey || e.shiftKey)) { if (SEL.size) { SEL.clear(); paintMulti(); refreshSel(); } return false; }
          if (e.shiftKey && TREE.sel) {
            const vis = TREE.visible().map((el) => TREE.nodes.get(el.dataset.k)), a = vis.findIndex((x) => x && x.key === TREE.sel), b = vis.indexOf(n);
            vis.slice(Math.min(a, b), Math.max(a, b) + 1).filter((x) => x && x.type === "doc").forEach((x) => SEL.add(x.id));
          } else { const cur = TREE.nodes.get(TREE.sel); if (!SEL.size && cur && cur.type === "doc" && cur !== n) SEL.add(cur.id); SEL.has(n.id) ? SEL.delete(n.id) : SEL.add(n.id); }
          paintMulti(); refreshSel(); return true;
        },
        onAction: (n, b) => {
          const a = b.dataset.a;
          if (a === "open") openDoc(n.id);
          if (a === "cut") cut(n.id);
          if (a === "sub") newFolder(n.id, n.key);
          if (a === "newf") newFolder(null, n.key);
        },
        onKey: (e, n) => {
          const k = e.key.toLowerCase();
          if (k === "u") undo();
          if (k === "c" && n.type === "doc") classify(n.id, typeof n.ctx === "number" ? n.ctx : null);
        },
        afterRender: (T, scope) => bindInfo(scope || $("#tlist")),
      });
    }
    const keep = TREE.sel;
    TREE.render(model());
    if (keep && TREE.nodes.has(keep)) TREE.select(keep); else { TREE.sel = null; paintPane(null); }
    if (document.activeElement === document.body) $("#tlist").focus({ preventScroll: true });
  }
  function paintMulti() { $$("#tlist .tn").forEach((el) => { const n = TREE.nodes.get(el.dataset.k); if (n && n.type === "doc") el.classList.toggle("multi", SEL.has(n.id)); }); }
  function bindInfo(scope) {
    $$("[data-snip]", scope).forEach(async (el) => {
      const id = +el.dataset.snip;
      if (SNIP[id] === undefined) {
        try { const d = await api(`/_lib/api/doc/${id}`); SNIP[id] = (d.content || "").replace(/\s+/g, " ").trim().slice(0, 280); } catch (e) { SNIP[id] = ""; }
      }
      el.textContent = SNIP[id] ? SNIP[id] + (SNIP[id].length >= 280 ? " …" : "") : "(no text recognized)";
    });
    $$("[data-save]", scope).forEach((b) => (b.onclick = (e) => { e.stopPropagation(); saveFact(+b.dataset.id, b.dataset.save); }));
    $$("[data-act]", scope).forEach((b) => (b.onclick = (e) => {
      e.stopPropagation(); const id = +b.dataset.id, n = TREE && TREE.nodes.get(TREE.sel);
      if (b.dataset.act === "open") openDoc(id);
      if (b.dataset.act === "ai") recognize([id]);
      if (b.dataset.act === "classify") classify(id, n && typeof n.ctx === "number" ? n.ctx : null);
    }));
  }

  // ================================================================ right pane
  async function paintPane(n) {
    const pane = $("#tpane"); if (!pane) return;
    if (!n || n.type === "group") return overview(pane);
    if (n.type === "folder") {
      const t = byId(M.tags, n.id), ds = docsIn(n.id);
      pane.innerHTML = `<h3><span class="dot" style="background:${esc(t.color || "#64748b")}"></span> ${esc(t.name)}</h3><p class="thint">${ds.length} documents incl. subfolders ${sumHtml(ds)} ${nextDue(ds)}</p>
        <div class="minis">${ds.slice(0, 10).map((d) => `<img src="/_lib/thumb/${d.id}" data-open="${d.id}" title="${esc(d.title)}">`).join("")}</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:10px"><button class="btn sec" id="pOpenF">Open folder page</button></div>
        <h4>Probably belongs here</h4><div id="pCand"><p class="thint">Looking…</p></div>`;
      $("#pOpenF").onclick = () => { VIEW = "fold"; F = { kind: "tag", id: n.id }; render(); };
      $$("[data-open]", pane).forEach((im) => (im.onclick = () => openDoc(+im.dataset.open)));
      let L = [];
      try { L = (await api(`/_lib/api/folder/${n.id}/candidates`)).docs; } catch (e) { /* ignore */ }
      if (!TREE || TREE.sel !== n.key) return;
      const inside = descIds(n.id);
      L = L.map((x) => docById(x.id) && Object.assign({}, docById(x.id), { score: x.score })).filter((d) => d && !(d.tags || []).some((t) => inside.has(t))).slice(0, 8);
      $("#pCand").innerHTML = L.length ? L.map((d) => `<div class="rel" draggable="true" data-rel="${d.id}"><img src="/_lib/thumb/${d.id}" data-open="${d.id}" alt=""><div class="tt"><b>${esc(d.title)}</b><small>${fmt(d.created)} · ${Math.round(d.score * 100)}% similar</small></div><button data-addto="${d.id}">+ Add</button></div>`).join("") : `<p class="thint">Nothing similar outside this folder.</p>`;
      $$("[data-addto]", pane).forEach((b) => (b.onclick = () => moveTo({ kind: "doc", ids: [+b.dataset.addto], from: null }, n.id, true)));
      $$("[data-open]", pane).forEach((im) => (im.onclick = () => openDoc(+im.dataset.open)));
      $$("[data-rel]", pane).forEach((el) => { el.ondragstart = () => { window.DS.drag = { kind: "doc", ids: [+el.dataset.rel], from: null, label: "1 document" }; }; });
      return;
    }
    const id = n.type === "case" ? n.root : n.id;
    if (window.DocView) window.DocView.pane(pane, id, { ctx: typeof n.ctx === "number" ? n.ctx : null });
  }
  async function overview(pane) {
    const unf = DOCS.filter((d) => !(d.tags || []).some(isFolder)).length, cases = new Set([...components().values()]).size;
    const from = iso(new Date(today() - 30 * 864e5)), now = iso(today());
    const dues = DOCS.filter((d) => d.due && (d.due.s ? d.due.d >= now : d.due.d >= from)).sort((a, b) => a.due.d.localeCompare(b.due.d)).slice(0, 8);
    const sugA = DOCS.filter((d) => d.amount && d.amount.s).length, sugD = DOCS.filter((d) => d.due && d.due.s).length;
    pane.innerHTML = `<h3>Overview</h3><div class="stats"><div><b>${DOCS.length}</b>documents</div><div><b>${folders().length}</b>folders</div><div><b>${cases}</b>case files</div><div><b>${unf}</b>not in a folder</div></div>
      <h4>⏰ Next deadlines</h4>${dues.length ? dues.map((d) => `<div class="rel" data-open="${d.id}"><img src="/_lib/thumb/${d.id}" alt=""><div class="tt"><b>${esc(d.title)}</b><small>${dueHtml(d.due)} ${d.amount ? amtHtml(d.amount) : ""}</small></div></div>`).join("") : `<p class="thint">No deadlines found yet.</p>`}
      ${sugA + sugD ? `<p class="thint">≈ = read from the text, not saved yet (${sugA} amounts, ${sugD} deadlines). Open a document to check and save it.</p>` : ""}
      <h4>💡 Suggested case files</h4><div id="pCases"><p class="thint">Looking for documents that belong together…</p></div>`;
    $$("[data-open]", pane).forEach((el) => (el.onclick = () => openDoc(+el.dataset.open)));
    let G = [];
    try { G = (await api("/_lib/api/cases")).groups; } catch (e) { /* ignore */ }
    if (!$("#pCases") || (TREE && TREE.sel && TREE.nodes.get(TREE.sel) && TREE.nodes.get(TREE.sel).type !== "group")) return;
    let hide; try { hide = new Set(JSON.parse(localStorage.getItem("docsorter.hidecase") || "[]")); } catch (e) { hide = new Set(); }
    G = G.filter((g) => !hide.has(g.ids.join(",")));
    $("#pCases").innerHTML = G.length ? G.map((g, i) => `<div class="cprop"><div class="minis">${g.ids.map((id) => `<img src="/_lib/thumb/${id}" data-g="${i}" data-x="${id}" title="${esc((docById(id) || {}).title || "")} – click to leave out, double-click to open">`).join("")}</div>
        <div><b>${esc(g.name)}</b> · ${g.ids.length} documents <span class="thint">${esc(g.span[0])} – ${esc(g.span[1])}</span></div>
        <div style="display:flex;gap:6px;margin-top:6px"><button class="btn" data-mk="${i}">🗂 Connect</button><button class="btn sec" data-hide="${i}">Not related</button></div></div>`).join("") : `<p class="thint">No suggestions right now.</p>`;
    $$("[data-x]", pane).forEach((im) => { im.onclick = () => im.classList.toggle("off"); im.ondblclick = () => openDoc(+im.dataset.x); });
    $$("[data-mk]", pane).forEach((b) => (b.onclick = () => {
      const off = new Set($$(`[data-g="${b.dataset.mk}"].off`, pane).map((x) => +x.dataset.x)), ids = G[+b.dataset.mk].ids.filter((x) => !off.has(x));
      if (ids.length < 2) return toast("Keep at least two documents");
      run(`🗂 Case file “${(docById(ids[0]) || {}).title}” with ${ids.length} documents`, [["/_lib/api/cases", { ids }]], ids.slice(1).map((i) => ["/_lib/api/link", { a: i, b: ids[0], on: false }]));
    }));
    $$("[data-hide]", pane).forEach((b) => (b.onclick = () => { hide.add(G[+b.dataset.hide].ids.join(",")); try { localStorage.setItem("docsorter.hidecase", JSON.stringify([...hide])); } catch (e) { /* ignore */ } overview(pane); }));
  }

  window.renderTree = () => { if (!$("#tlist")) TREE = null; renderTree(); };
  window.treeUndo = undo;
  Object.assign(window.DS, { run, connect, moveTo, saveFact, classOf, components, docById, SNIP });
})();
