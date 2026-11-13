/* Doc Sorter – document tree.
   Folders (Paperless tags) > case files (documents linked via "Gehört zu", oldest first) > documents.
   Drag a document onto a document = connect them (and file it in the same folder),
   onto a folder = move it there (hold Ctrl/⌥ to add it there as well), onto "Not in a folder" = take it out.
   Drag a folder onto a folder = put it inside. Keys: ↑↓ ←→ Enter, C = classify to the best folder, U = undo. */
(function () {
  const OPENK = "docsorter.tree.open";
  let OPENSET;
  try { OPENSET = new Set(JSON.parse(localStorage.getItem(OPENK) || '["f:root","f:unfiled"]')); } catch (e) { OPENSET = new Set(["f:root", "f:unfiled"]); }
  const saveOpen = () => { try { localStorage.setItem(OPENK, JSON.stringify([...OPENSET])); } catch (e) { /* ignore */ } };
  let TSEL = null, TDRAG = null, ROWS = [], UNDO = [], CLS = {};

  const fmt = (d) => (d || "").split("-").reverse().join(".");
  const typeName = (id) => (byId(M.types, id) || {}).name || "";
  const docById = (id) => DOCS.find((d) => d.id === id);

  function linkMap() {
    const L = new Map();
    const add = (a, b) => { if (!L.has(a)) L.set(a, new Set()); L.get(a).add(b); };
    DOCS.forEach((d) => (d.links || []).forEach((x) => { if (docById(x)) { add(d.id, x); add(x, d.id); } }));
    return L;
  }
  function components(L) {
    const comp = new Map(), seen = new Set();
    DOCS.forEach((d) => {
      if (seen.has(d.id) || !L.has(d.id)) return;
      const stack = [d.id], members = [];
      seen.add(d.id);
      while (stack.length) { const x = stack.pop(); members.push(x); (L.get(x) || []).forEach((y) => { if (!seen.has(y)) { seen.add(y); stack.push(y); } }); }
      const docs = members.map(docById).filter(Boolean).sort((a, b) => (a.created || "").localeCompare(b.created || "") || a.id - b.id);
      const c = { root: docs[0].id, docs };
      docs.forEach((m) => comp.set(m.id, c));
    });
    return comp;
  }

  // ------------------------------------------------------------ model -> visible rows
  function build() {
    const L = linkMap(), COMP = components(L), q = (Q || "").toLowerCase().split(/\s+/).filter(Boolean);
    const match = (d) => !q.length || q.every((w) => docText(d).includes(w));
    const fold = folders(), kids = {};
    fold.forEach((t) => (kids[t.parent && isFolder(t.parent) ? t.parent : 0] = kids[t.parent && isFolder(t.parent) ? t.parent : 0] || []).push(t));
    Object.values(kids).forEach((l) => l.sort((a, b) => a.name.localeCompare(b.name)));
    const direct = {};
    DOCS.forEach((d) => (d.tags || []).filter(isFolder).forEach((t) => (direct[t] = direct[t] || []).push(d)));
    const rows = [];
    const searching = q.length > 0;

    // items (single docs / case files) inside a container (folder id or "unfiled")
    function items(docs, ctx) {
      const used = new Set(), out = [];
      docs.slice().sort((a, b) => (b.created || "").localeCompare(a.created || "")).forEach((d) => {
        if (used.has(d.id)) return;
        const c = COMP.get(d.id);
        if (c) {
          c.docs.forEach((m) => used.add(m.id));
          const hit = c.docs.some(match);
          if (hit) out.push({ kind: "case", c, ctx, date: c.docs[c.docs.length - 1].created || "" });
        } else if (match(d)) out.push({ kind: "doc", d, ctx, date: d.created || "" });
      });
      return out.sort((a, b) => b.date.localeCompare(a.date));
    }
    function countIn(tid) { const ids = descIds(tid); return DOCS.filter((d) => (d.tags || []).some((t) => ids.has(t))).length; }
    function pushItems(list, depth, ctxFolder) {
      list.forEach((it) => {
        if (it.kind === "doc") rows.push({ key: `d:${ctxFolder}:${it.d.id}`, type: "doc", d: it.d, depth, folder: ctxFolder });
        else {
          const k = `c:${ctxFolder}:${it.c.root}`, open = OPENSET.has(k) || searching;
          rows.push({ key: k, type: "case", c: it.c, depth, folder: ctxFolder, open });
          if (open) it.c.docs.forEach((m, i) => rows.push({ key: `d:${ctxFolder}:${m.id}:c`, type: "doc", d: m, depth: depth + 1, folder: ctxFolder, inCase: it.c, first: i === 0, last: i === it.c.docs.length - 1, elsewhere: ctxFolder !== "unfiled" && !(m.tags || []).includes(ctxFolder) }));
        }
      });
    }
    function walk(parent, depth) {
      (kids[parent] || []).forEach((t) => {
        const own = items(direct[t.id] || [], t.id);
        const sub = [];
        // hide empty branches while searching
        const before = rows.length;
        const k = "f:" + t.id, open = OPENSET.has(k) || (searching && (own.length || hasHit(t.id)));
        if (searching && !own.length && !hasHit(t.id) && !t.name.toLowerCase().includes(q.join(" "))) return;
        rows.push({ key: k, type: "folder", t, depth, open, n: countIn(t.id) });
        if (open) { walk(t.id, depth + 1); pushItems(own, depth + 1, t.id); }
        void before; void sub;
      });
    }
    function hasHit(tid) {
      const ids = descIds(tid);
      return DOCS.some((d) => (d.tags || []).some((t) => ids.has(t)) && match(d));
    }
    rows.push({ key: "f:root", type: "root", label: "Folders", depth: 0, open: true, n: fold.length });
    walk(0, 1);
    const unf = DOCS.filter((d) => !(d.tags || []).some(isFolder));
    const uopen = OPENSET.has("f:unfiled") || searching;
    rows.push({ key: "f:unfiled", type: "unfiled", label: "Not in a folder", depth: 0, open: uopen, n: unf.length });
    if (uopen) pushItems(items(unf, "unfiled"), 1, "unfiled");
    return rows;
  }

  // ------------------------------------------------------------ render
  function rowHtml(r) {
    const pad = 10 + r.depth * 18, sel = TSEL === r.key ? " tsel" : "";
    const caret = (open) => `<span class="tcar">${open ? "▾" : "▸"}</span>`;
    if (r.type === "root" || r.type === "unfiled")
      return `<div class="trow troot${sel}" data-key="${r.key}" style="padding-left:${pad}px">${r.type === "root" ? "<span class='tcar'></span>" : caret(r.open)}<b>${r.type === "root" ? "📚" : "📥"} ${esc(r.label)}</b><span class="tn">${r.n}</span>${r.type === "root" ? `<button class="tadd" data-newf="0" title="New folder">+ folder</button>` : ""}</div>`;
    if (r.type === "folder")
      return `<div class="trow tfold${sel}" data-key="${r.key}" draggable="true" style="padding-left:${pad}px">${caret(r.open)}<span class="dot" style="background:${esc(r.t.color || "#64748b")}"></span><span class="tl">${esc(r.t.name)}</span><span class="tn">${r.n}</span><button class="tadd" data-newf="${r.t.id}" title="New subfolder">+</button></div>`;
    if (r.type === "case") {
      const root = r.c.docs[0], last = r.c.docs[r.c.docs.length - 1];
      return `<div class="trow tcase${sel}" data-key="${r.key}" style="padding-left:${pad}px">${caret(r.open)}<span class="tic">🗂</span><span class="tl">${esc(root.title)}</span><span class="tm">${r.c.docs.length} docs · ${fmt(root.created).slice(3)}–${fmt(last.created).slice(3)}</span></div>`;
    }
    const d = r.d, nf = (d.tags || []).filter(isFolder).length, ai = AI.results[d.id];
    const line = r.inCase ? `<span class="tline${r.first ? " f" : ""}${r.last ? " l" : ""}" style="left:${pad - 11}px"></span>` : "";
    return `<div class="trow tdoc${sel}${r.inCase ? " incase" : ""}" data-key="${r.key}" data-id="${d.id}" draggable="true" style="padding-left:${pad}px">${line}<img class="tth" loading="lazy" src="/_lib/thumb/${d.id}" alt=""><span class="tl">${esc(d.title || "#" + d.id)}</span>`
      + `<span class="tm">${fmt(d.created)}${d.document_type ? " · " + esc(typeName(d.document_type)) : ""}</span>`
      + (r.elsewhere ? `<span class="tb" title="Linked here, filed in another folder">↗</span>` : "")
      + (nf > 1 ? `<span class="tb" title="In ${nf} folders">×${nf}</span>` : "")
      + (ai ? `<span class="tb ai" title="AI suggestion waiting">✨</span>` : "")
      + (r.inCase ? `<button class="tact" data-cut="${d.id}" title="Take out of this case file">✂</button>` : "") + `</div>`;
  }

  function renderTree() {
    $("#years").innerHTML = "";
    ROWS = build();
    if (TSEL && !ROWS.some((r) => r.key === TSEL)) TSEL = null;
    $("#main").innerHTML = `<div class="tree"><div class="tleft"><div class="thead"><b>Document tree</b><span class="thint">drag a document onto a document to connect · onto a folder to move (Ctrl/⌥ = also) · keys ↑↓←→ C U</span></div><div id="tlist" tabindex="0">${ROWS.map(rowHtml).join("")}</div></div><div class="tright" id="tpane"></div></div>`;
    bindTree();
    paintPane();
  }

  function setSel(key, scroll) {
    TSEL = key;
    $$("#tlist .trow").forEach((el) => el.classList.toggle("tsel", el.dataset.key === key));
    if (scroll) { const el = $(`#tlist .trow[data-key="${CSS.escape(key)}"]`); if (el) el.scrollIntoView({ block: "nearest" }); }
    paintPane();
  }
  function toggle(key, force) {
    const open = force !== undefined ? force : !OPENSET.has(key);
    open ? OPENSET.add(key) : OPENSET.delete(key);
    saveOpen(); renderTree();
  }

  function bindTree() {
    const list = $("#tlist");
    $$(".trow", list).forEach((el) => {
      const r = ROWS.find((x) => x.key === el.dataset.key);
      el.onclick = (e) => {
        if (e.target.dataset.newf !== undefined) return newFolder(+e.target.dataset.newf, el);
        if (e.target.dataset.cut) return cut(+e.target.dataset.cut);
        if (e.target.classList.contains("tcar") || (r.type !== "doc" && TSEL === r.key)) return r.type === "root" ? null : toggle(r.key);
        if (r.type === "doc" && (e.ctrlKey || e.metaKey || e.shiftKey)) {
          if (e.shiftKey && TSEL) {
            const a = ROWS.findIndex((x) => x.key === TSEL), b = ROWS.indexOf(r);
            ROWS.slice(Math.min(a, b), Math.max(a, b) + 1).filter((x) => x.type === "doc").forEach((x) => SEL.add(x.d.id));
          } else {
            const cur = ROWS.find((x) => x.key === TSEL);
            if (!SEL.size && cur && cur.type === "doc" && cur !== r) SEL.add(cur.d.id);
            SEL.has(r.d.id) ? SEL.delete(r.d.id) : SEL.add(r.d.id);
          }
          paintMulti(); refreshSel(); return;
        }
        if (SEL.size) { SEL.clear(); paintMulti(); refreshSel(); }
        setSel(r.key);
      };
      el.ondblclick = () => { if (r.type === "doc") openDoc(r.d.id); else if (r.type !== "root") toggle(r.key); };
      el.ondragstart = (e) => {
        if (r.type === "doc") TDRAG = { kind: "doc", id: r.d.id, from: r.folder, ids: SEL.has(r.d.id) ? [...SEL] : [r.d.id] };
        else if (r.type === "folder") TDRAG = { kind: "folder", id: r.t.id };
        else return e.preventDefault();
        e.dataTransfer.effectAllowed = "copyMove";
        try { e.dataTransfer.setData("text/plain", String(TDRAG.id)); } catch (err) { /* ignore */ }
      };
      el.ondragend = () => { TDRAG = null; $$(".tdrop").forEach((x) => x.classList.remove("tdrop")); };
      el.ondragover = (e) => {
        if (!TDRAG || !dropOk(r)) return;
        e.preventDefault(); e.dataTransfer.dropEffect = e.ctrlKey || e.altKey ? "copy" : "move";
        el.classList.add("tdrop");
      };
      el.ondragleave = () => el.classList.remove("tdrop");
      el.ondrop = (e) => { e.preventDefault(); el.classList.remove("tdrop"); const d = TDRAG; TDRAG = null; if (d) dropTo(d, r, e.ctrlKey || e.altKey); };
    });
    list.onkeydown = keys;
    paintMulti();
    if (document.activeElement === document.body || !document.activeElement) list.focus({ preventScroll: true });
  }

  function paintMulti() { $$("#tlist .tdoc").forEach((el) => el.classList.toggle("tmulti", SEL.has(+el.dataset.id))); }

  function dropOk(r) {
    if (TDRAG.kind === "folder") return (r.type === "folder" && r.t.id !== TDRAG.id && !descIds(TDRAG.id).has(r.t.id)) || r.type === "root";
    if (r.type === "doc") return r.d.id !== TDRAG.id;
    return r.type === "folder" || r.type === "unfiled" || r.type === "case";
  }

  // ------------------------------------------------------------ actions (with undo)
  async function run(label, ops, inverse) {
    try {
      for (const [path, body] of ops) await api(path, body);
      UNDO.push({ label, inverse }); if (UNDO.length > 20) UNDO.shift();
      SEL.clear(); refreshSel();
      toastUndo(label);
      await reloadDocs();
    } catch (e) { toast(e.message, 6000); }
  }
  function toastUndo(label) {
    const t = $("#toast");
    t.innerHTML = `${esc(label)} <button id="undoBtn" style="margin-left:10px;background:none;border:1px solid #475569;border-radius:7px;color:#e5e7eb;padding:1px 8px">Undo</button>`;
    t.style.display = "block"; clearTimeout(t._t); t._t = setTimeout(() => (t.style.display = "none"), 6000);
    $("#undoBtn").onclick = undo;
  }
  async function undo() {
    const u = UNDO.pop();
    if (!u) return toast("Nothing to undo");
    try { for (const [path, body] of u.inverse) await api(path, body); toast("Undone: " + u.label); await reloadDocs(); } catch (e) { toast(e.message, 6000); }
  }
  const tagOps = (ids, add, remove) => [["/_lib/api/bulk", { ids, add_tags: add, remove_tags: remove }]];

  function dropTo(drag, r, also) {
    if (drag.kind === "folder") {
      const t = byId(M.tags, drag.id), to = r.type === "root" ? null : r.t.id;
      return run(`“${t.name}” → ${to ? "“" + tagName(to) + "”" : "top level"}`, [[`/_lib/api/folder/${drag.id}/edit`, { parent: to }]], [[`/_lib/api/folder/${drag.id}/edit`, { parent: t.parent || null }]]);
    }
    const ids = drag.ids, from = typeof drag.from === "number" ? drag.from : null;
    if (r.type === "doc" || r.type === "case") {
      // connect: link to the target document (or the case file's first document) and file it alongside
      const target = r.type === "doc" ? r.d : r.c.docs[0], ctx = typeof r.folder === "number" ? r.folder : null;
      const ops = ids.filter((i) => i !== target.id).map((i) => ["/_lib/api/link", { a: i, b: target.id }]);
      const inv = ids.filter((i) => i !== target.id).map((i) => ["/_lib/api/link", { a: i, b: target.id, on: false }]);
      const notThere = ctx ? ids.filter((i) => !(docById(i).tags || []).includes(ctx)) : [];
      if (ctx && notThere.length) {
        const rm = !also && from && from !== ctx ? [from] : [];
        ops.push(...tagOps(notThere, [ctx], rm)); inv.push(...tagOps(notThere, rm, [ctx]));
      }
      return run(`🔗 ${ids.length > 1 ? ids.length + " documents" : "“" + docById(ids[0]).title + "”"} connected with “${target.title}”${ctx && notThere.length ? " in " + tagName(ctx) : ""}`, ops, inv);
    }
    if (r.type === "folder") {
      const to = r.t.id, rm = !also && from && from !== to ? [from] : [];
      const add = ids.filter((i) => !(docById(i).tags || []).includes(to));
      return run(`${ids.length > 1 ? ids.length + " documents" : "“" + docById(ids[0]).title + "”"} ${rm.length ? "moved" : "added"} to “${r.t.name}”`, tagOps(ids, [to], rm), tagOps(ids, rm, add.length ? [to] : []));
    }
    if (r.type === "unfiled" && from) return run(`Taken out of “${tagName(from)}”`, tagOps(ids, [], [from]), tagOps(ids, [from], []));
  }
  function cut(id) {
    const d = docById(id), links = (d.links || []).filter(docById);
    if (!links.length) return;
    run(`✂ “${d.title}” taken out of its case file`, links.map((x) => ["/_lib/api/link", { a: id, b: x, on: false }]), links.map((x) => ["/_lib/api/link", { a: id, b: x }]));
  }
  function newFolder(parent, rowEl) {
    const inp = document.createElement("input");
    inp.className = "newin"; inp.placeholder = (parent ? `Subfolder of “${tagName(parent)}”` : "New folder") + " – Enter";
    inp.style.marginLeft = (parseInt(rowEl.style.paddingLeft) + 18) + "px";
    rowEl.after(inp); inp.focus();
    inp.onkeydown = async (e) => {
      e.stopPropagation();
      if (e.key === "Escape") return inp.remove();
      if (e.key !== "Enter" || !inp.value.trim()) return;
      try { const t = await api("/_lib/api/create/tag", { name: inp.value.trim(), parent: parent || null }); if (parent) OPENSET.add("f:" + parent); saveOpen(); TSEL = "f:" + t.id; toast(`Folder “${t.name}” created`); await reloadDocs(); } catch (err) { toast(err.message, 5000); }
    };
    inp.onblur = () => setTimeout(() => inp.remove(), 150);
  }
  async function classifySel() {
    const r = ROWS.find((x) => x.key === TSEL);
    if (!r || r.type !== "doc") return;
    const s = await classOf(r.d.id);
    if (!s) return toast("No suggestion – there are no similar documents in folders yet");
    dropTo({ kind: "doc", id: r.d.id, ids: [r.d.id], from: r.folder }, { type: "folder", t: byId(M.tags, s.id) }, false);
  }
  async function classOf(id) {
    if (!(id in CLS)) { try { Object.assign(CLS, await api("/_lib/api/suggest_folders", { ids: [id] })); } catch (e) { CLS[id] = null; } }
    return CLS[id];
  }

  function keys(e) {
    if (e.target.tagName === "INPUT") return;
    const i = ROWS.findIndex((r) => r.key === TSEL), r = ROWS[i];
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); const n = ROWS[Math.max(0, Math.min(ROWS.length - 1, (i < 0 ? -1 : i) + (e.key === "ArrowDown" ? 1 : -1)))]; if (n) setSel(n.key, true); return; }
    if (!r) return;
    if (e.key === "ArrowRight" && r.type !== "doc" && r.type !== "root" && !r.open) { e.preventDefault(); return toggle(r.key, true); }
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      if (r.type !== "doc" && r.type !== "root" && r.open) return toggle(r.key, false);
      for (let j = i - 1; j >= 0; j--) if (ROWS[j].depth < r.depth) return setSel(ROWS[j].key, true);
    }
    if (e.key === "Enter") { e.preventDefault(); if (r.type === "doc") openDoc(r.d.id); else if (r.type !== "root") toggle(r.key); }
    if (e.key.toLowerCase() === "c") classifySel();
    if (e.key.toLowerCase() === "u") undo();
  }

  // ------------------------------------------------------------ right pane
  function caseGraph(c, focus) {
    const n = c.docs.length, W = 380, H = 150, x = (i) => (n === 1 ? W / 2 : 55 + (i * (W - 110)) / (n - 1));
    const nodes = c.docs.map((d, i) => `<g class="gn" data-open="${d.id}"><circle cx="${x(i)}" cy="${i % 2 ? 98 : 58}" r="${d.id === focus ? 9 : 7}" fill="${d.id === focus ? "#facc15" : "#60a5fa"}"/><text x="${x(i)}" y="${i % 2 ? 122 : 40}" text-anchor="middle">${esc((typeName(d.document_type) || d.title || "").slice(0, 14))}</text><text x="${x(i)}" y="${i % 2 ? 134 : 28}" text-anchor="middle" class="gd">${fmt(d.created).slice(3)}</text></g>`).join("");
    const edges = c.docs.slice(1).map((d, i) => `<line x1="${x(i)}" y1="${i % 2 ? 98 : 58}" x2="${x(i + 1)}" y2="${(i + 1) % 2 ? 98 : 58}" stroke="#334155" stroke-width="2"/>`).join("");
    return `<svg viewBox="0 0 ${W} ${H}" class="graph">${edges}${nodes}</svg>`;
  }
  async function paintPane() {
    const pane = $("#tpane");
    if (!pane) return;
    const r = ROWS.find((x) => x.key === TSEL);
    if (!r || r.type === "root" || r.type === "unfiled") {
      const unf = DOCS.filter((d) => !(d.tags || []).some(isFolder)).length, cases = new Set([...components(linkMap()).values()]).size;
      pane.innerHTML = `<h3>Overview</h3><div class="stats"><div><b>${DOCS.length}</b>documents</div><div><b>${folders().length}</b>folders</div><div><b>${cases}</b>case files</div><div><b>${unf}</b>not in a folder</div></div>
        <p class="thint" style="margin-top:14px">A <b>case file</b> 🗂 is a group of connected documents, e.g. a contract with its invoices and the termination letter, sorted by date. Drag a document onto another one to start one.</p>`;
      return;
    }
    if (r.type === "folder") {
      const ds = docsIn(r.t.id);
      pane.innerHTML = `<h3><span class="dot" style="background:${esc(r.t.color || "#64748b")}"></span> ${esc(r.t.name)}</h3><p class="thint">${ds.length} documents incl. subfolders</p>
        <div class="minis">${ds.slice(0, 8).map((d) => `<img src="/_lib/thumb/${d.id}" data-open="${d.id}" title="${esc(d.title)}">`).join("")}</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:10px"><button class="btn sec" id="pOpenF">Open folder page</button></div>
        <h4>Probably belongs here</h4><div id="pCand"><p class="thint">Looking…</p></div>`;
      $("#pOpenF").onclick = () => { VIEW = "fold"; F = { kind: "tag", id: r.t.id }; render(); };
      bindOpen(pane);
      let L = [];
      try { L = (await api(`/_lib/api/folder/${r.t.id}/candidates`)).docs; } catch (e) { /* ignore */ }
      if (TSEL !== r.key) return;
      const inside = descIds(r.t.id);
      L = L.map((x) => docById(x.id) && Object.assign({}, docById(x.id), { score: x.score })).filter((d) => d && !(d.tags || []).some((t) => inside.has(t))).slice(0, 8);
      $("#pCand").innerHTML = L.length ? L.map((d) => relRow(d, `<button data-addto="${d.id}">+ Add</button>`, d.score)).join("") : `<p class="thint">Nothing similar outside this folder.</p>`;
      $$("[data-addto]", pane).forEach((b) => (b.onclick = () => dropTo({ kind: "doc", id: +b.dataset.addto, ids: [+b.dataset.addto], from: null }, r, true)));
      bindOpen(pane); bindRelDrag(pane);
      return;
    }
    const d = r.type === "doc" ? r.d : r.c.docs[0], c = r.type === "case" ? r.c : r.inCase || components(linkMap()).get(d.id);
    pane.innerHTML = `<div class="pdoc"><img src="/_lib/thumb/${d.id}" data-open="${d.id}" class="pth" title="Open"><div><h3>${esc(d.title)}</h3>
        <p class="thint">${fmt(d.created)}${d.document_type ? " · " + esc(typeName(d.document_type)) : ""}${d.correspondent ? " · " + esc((byId(M.corrs, d.correspondent) || {}).name || "") : ""}</p>
        <div class="tags">${(d.tags || []).filter(isFolder).map((t) => `<span class="chip">📁 ${esc(tagName(t))}</span>`).join("") || '<span class="thint">not in a folder</span>'}</div>
        <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap"><button class="btn sec" data-open="${d.id}">Open</button><button class="btn ai" id="pAI">✨ AI</button></div></div></div>
      ${c ? `<h4>🗂 Case file · ${c.docs.length} documents</h4>${caseGraph(c, d.id)}` : ""}
      <h4>Classify <small class="thint">folders of the most similar documents</small></h4><div id="pCls"><p class="thint">…</p></div>
      <h4>See also <small class="thint">drag onto the tree or click 🔗</small></h4><div id="pRel"><p class="thint">Looking…</p></div>`;
    bindOpen(pane);
    $("#pAI").onclick = () => recognize([d.id]);
    classOf(d.id).then((s) => {
      if (TSEL !== r.key) return;
      const already = s && (d.tags || []).includes(s.id);
      $("#pCls").innerHTML = already ? `<p class="thint">✓ Already in “${esc(s.name)}”, where the most similar documents are.</p>` : s ? `<div class="rel"><span style="font-size:20px">📁</span><div class="tt"><b>${esc(s.name)}</b><small>${Math.round(s.score * 100)}% sure</small></div><button id="pMove">Move here</button><button id="pAlso" title="Keep the other folders">+ Also</button></div>` : `<p class="thint">No suggestion yet.</p>`;
      if (s && !already) { $("#pMove").onclick = () => dropTo({ kind: "doc", id: d.id, ids: [d.id], from: r.folder }, { type: "folder", t: byId(M.tags, s.id) }, false); $("#pAlso").onclick = () => dropTo({ kind: "doc", id: d.id, ids: [d.id], from: r.folder }, { type: "folder", t: byId(M.tags, s.id) }, true); }
    });
    let rel = [];
    try { rel = (await api(`/_lib/api/related/${d.id}`)).related; } catch (e) { /* ignore */ }
    if (TSEL !== r.key) return;
    const linked = new Set((c ? c.docs.map((x) => x.id) : []));
    rel = rel.filter((x) => !linked.has(x.id)).map((x) => docById(x.id) || x).slice(0, 8);
    $("#pRel").innerHTML = rel.length ? rel.map((x) => relRow(x, `<button data-lnk="${x.id}" title="Connect">🔗</button>`)).join("") : `<p class="thint">No similar documents.</p>`;
    $$("[data-lnk]", pane).forEach((b) => (b.onclick = () => dropTo({ kind: "doc", id: +b.dataset.lnk, ids: [+b.dataset.lnk], from: null }, { type: "doc", d, folder: typeof r.folder === "number" ? r.folder : null }, true)));
    bindOpen(pane); bindRelDrag(pane);
  }
  function relRow(d, btns, score) {
    return `<div class="rel" draggable="true" data-rel="${d.id}"><img src="/_lib/thumb/${d.id}" data-open="${d.id}" alt=""><div class="tt"><b>${esc(d.title)}</b><small>${fmt(d.created)}${d.document_type ? " · " + esc(typeName(d.document_type)) : ""}${(d.tags || []).filter(isFolder).length ? " · 📁 " + esc((d.tags || []).filter(isFolder).map(tagName).join(", ")) : ""}${score ? " · " + Math.round(score * 100) + "%" : ""}</small></div>${btns}</div>`;
  }
  function bindOpen(root) { $$("[data-open]", root).forEach((el) => (el.onclick = () => openDoc(+el.dataset.open))); }
  function bindRelDrag(root) {
    $$("[data-rel]", root).forEach((el) => {
      el.ondragstart = (e) => { const id = +el.dataset.rel; TDRAG = { kind: "doc", id, ids: [id], from: null }; e.dataTransfer.effectAllowed = "copy"; };
      el.ondragend = () => { TDRAG = null; };
    });
  }

  window.renderTree = renderTree;
  window.treeUndo = undo;
})();
