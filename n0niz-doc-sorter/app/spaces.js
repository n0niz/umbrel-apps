/* Doc Sorter – Spaces: your top-level folders as life areas (Wohnen, Auto, Steuern …).
   Left: one tile per space with document count, type mix, case files and the next deadline.
   Right: the selected space – suggestions from "Not in a folder", case files as threads
   (documents connected via "Gehört zu", oldest first), loose documents, drop zone.
   Drag a document onto a space tile = move it there (Ctrl/⌥ = also), onto a case file = connect it. */
(function () {
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } return null; };
  const HUES = [212, 186, 150, 38, 262, 0, 330, 96, 24, 200, 276, 168];
  const hueOf = (s) => { let h = 0; for (const ch of String(s)) h = (h * 31 + ch.charCodeAt(0)) >>> 0; return HUES[h % HUES.length]; };
  const typeName = (id) => (byId(M.types, id) || {}).name || "";
  const corrName = (id) => (byId(M.corrs, id) || {}).name || "";
  const typeColor = (id) => (id ? `hsl(${hueOf(typeName(id))} 70% 62%)` : "rgb(255 255 255 / .28)");
  let OPENC;
  try { OPENC = new Set(JSON.parse(store("docsorter.spaces.open") || "[]")); } catch (e) { OPENC = new Set(); }
  const saveOpen = () => store("docsorter.spaces.open", JSON.stringify([...OPENC]));
  let SP = (() => { const v = store("docsorter.space"); return v === "u" ? "u" : v ? +v : null; })();
  let LOOSE_ALL = false, SUG_ALL = false, FSN = -1, LOOSE_OPEN = store("docsorter.spaces.loose") === "1";

  const parentFolder = (t) => t.parent && isFolder(t.parent) ? byId(M.tags, t.parent) : null;
  const spaceList = () => folders().filter((t) => !parentFolder(t));
  function topOf(tid) { let t = byId(M.tags, tid), n = 0; while (t && parentFolder(t) && n++ < 12) t = parentFolder(t); return t; }
  const short = (d) => DS.fmt(d);
  const monthYear = (d) => { if (!d) return ""; const [y, m] = d.split("-"); return MONTHS[+m - 1].slice(0, 3) + " " + y; };

  function sumOpen(docs) {
    const t0 = DS.iso(DS.today());
    const L = docs.filter((d) => d.amount && !d.amount.p && d.amount.k !== "refund" && d.due && d.due.d >= t0);
    return L.length ? L.reduce((a, d) => a + d.amount.v, 0) : null;
  }
  function nextDue(docs) {
    const t0 = DS.iso(DS.today());
    return docs.filter((d) => d.due && d.due.d >= t0).sort((a, b) => a.due.d.localeCompare(b.due.d))[0] || null;
  }
  function mixOf(docs) {
    const by = new Map();
    docs.forEach((d) => by.set(d.document_type || 0, (by.get(d.document_type || 0) || 0) + 1));
    const top = [...by.entries()].filter(([k]) => k).sort((a, b) => b[1] - a[1]).slice(0, 4);
    const rest = docs.length - top.reduce((a, [, n]) => a + n, 0);
    const seg = top.map(([k, n]) => ({ w: n, c: typeColor(k), t: typeName(k) + " " + n }));
    if (rest) seg.push({ w: rest, c: "rgb(255 255 255 / .22)", t: "other " + rest });
    return seg;
  }

  // ---------------------------------------------------------------- model
  function model() {
    const base = filtered(), comp = DS.components();
    const mk = (id, name, color, docs, extra = {}) => {
      const roots = new Map(), loose = [];
      docs.forEach((d) => { const c = comp.get(d.id); if (c) roots.set(c.root, c); else loose.push(d); });
      const cases = [...roots.values()].sort((a, b) => (b.docs[b.docs.length - 1].created || "").localeCompare(a.docs[a.docs.length - 1].created || ""));
      return Object.assign({ id, name, color, docs, cases, loose: loose.sort((a, b) => (b.created || "").localeCompare(a.created || "")), next: nextDue(docs), open: sumOpen(docs) }, extra);
    };
    const list = spaceList().map((t) => { const ids = descIds(t.id); return mk(t.id, t.name, t.color || "#64748b", base.filter((d) => (d.tags || []).some((x) => ids.has(x))), { subs: folders().filter((x) => x.parent === t.id) }); })
      .filter((s) => s.docs.length || !Q)
      .sort((a, b) => b.docs.length - a.docs.length || a.name.localeCompare(b.name));
    const un = mk("u", "Not in a folder", "#94a3b8", base.filter((d) => !(d.tags || []).some(isFolder)), { unsorted: true, subs: [] });
    return { list, un, all: base };
  }

  // ---------------------------------------------------------------- suggestions (similar documents' folder)
  function sugFor(S, un) {
    if (S.unsorted) return [];
    return un.docs.filter((d) => { const s = FS[d.id]; if (!s || s.score < 0.45) return false; const top = topOf(s.id); return top && top.id === S.id; })
      .sort((a, b) => FS[b.id].score - FS[a.id].score);
  }
  function fetchFS(un) {
    const ids = un.docs.slice(0, 150).map((d) => d.id).filter((i) => !(i in FS));
    if (!ids.length) return;
    const before = Object.keys(FS).length;
    loadFS(ids).then(() => { const n = Object.keys(FS).length; if (n !== before && n !== FSN && VIEW === "spaces") { FSN = n; renderSpaces(); } });
  }

  // ---------------------------------------------------------------- html
  function tileHtml(S, on) {
    const mix = mixOf(S.docs), tot = mix.reduce((a, s) => a + s.w, 0) || 1;
    const chips = S.unsorted ? [`${S.docs.length} to sort`] : S.cases.slice(0, 2).map((c) => c.docs[0].title).concat(S.cases.length > 2 ? ["+" + (S.cases.length - 2)] : []);
    const nx = S.next ? `<span class="spnext due">⏰ ${esc(short(S.next.due.d))} · ${esc(DS.rel(S.next.due.d))}</span>`
      : S.unsorted && !S.docs.length ? `<span class="spnext">all sorted 🎉</span>`
      : S.docs.length ? `<span class="spnext">latest ${esc(short(S.docs.reduce((m, d) => (d.created || "") > m ? d.created : m, "")))}</span>` : "";
    return `<button class="sptile${on ? " on" : ""}${S.unsorted ? " spun" : ""}" data-sp="${S.id}" aria-pressed="${on}" title="${S.unsorted ? "Documents that are in no folder yet" : "Drop documents here to move them into " + esc(S.name)}">
      <span class="sph"><i class="dot" style="background:${esc(S.color)}"></i><b>${esc(S.name)}</b><span class="n">${S.docs.length}</span></span>
      <span class="spmix" aria-hidden="true">${mix.map((s) => `<i style="flex:${s.w / tot};background:${s.c}" title="${esc(s.t)}"></i>`).join("")}</span>
      ${chips.length ? `<span class="spchips">${chips.map((c) => `<span>${esc(c)}</span>`).join("")}</span>` : ""}
      ${nx}</button>`;
  }
  function rowHtml(d, from, i, n, sug, lite) {
    const ty = typeName(d.document_type), co = corrName(d.correspondent);
    return `<li class="sprow" data-id="${d.id}" data-from="${from || ""}" draggable="true" title="Click: open · drag: onto a space or a case file">
      <span class="node${i === 0 ? " first" : ""}${i === n - 1 ? " last" : ""}" aria-hidden="true"><i style="background:${typeColor(d.document_type)}"></i></span>
      <span class="dt">${esc(short(d.created))}</span>
      <span class="tt"><b>${esc(d.title || d.original_file_name || "#" + d.id)}</b>${ty ? `<span class="ty">${esc(ty)}</span>` : ""}${co ? `<small>${esc(co)}</small>` : ""}</span>
      <span class="fx">${!lite && d.due ? DS.dueHtml(d.due) : ""}${!lite && d.amount ? DS.amtHtml(d.amount) : ""}${sug ? `<span class="spput" title="Similar documents are there">📁 ${esc(sug.name)} <button data-put="${d.id}" data-tid="${sug.id}">✓</button></span>` : ""}</span></li>`;
  }
  function caseHtml(c, S) {
    const first = c.docs[0], last = c.docs[c.docs.length - 1], key = String(c.root), open = OPENC.has(key) || !!Q;
    const senders = [...new Set(c.docs.map((d) => corrName(d.correspondent)).filter(Boolean))].slice(0, 2).join(", ");
    const one = c.docs.filter((d) => d.amount && !d.amount.p && d.amount.k !== "refund"), sum = one.reduce((a, d) => a + d.amount.v, 0);
    const nx = nextDue(c.docs), from = typeof S.id === "number" ? S.id : "";
    return `<div class="spcase${open ? " open" : ""}" data-root="${c.root}">
      <button class="spch" data-tog="${key}" aria-expanded="${open}">
        <span class="ic" aria-hidden="true">🗂</span>
        <span class="t"><b>${esc(first.title)}</b><small>${senders ? esc(senders) + " · " : ""}${c.docs.length} documents · ${esc(monthYear(first.created))} → ${esc(monthYear(last.created))}</small></span>
        <span class="thr" aria-hidden="true">${c.docs.slice(-8).map((d) => `<i style="background:${typeColor(d.document_type)}"></i>`).join("<s></s>")}</span>
        <span class="sum">${nx ? DS.dueHtml(nx.due) : ""}${one.length > 1 ? `<span class="amt">Σ ${DS.money({ v: sum, c: "EUR" })}</span>` : one.length ? DS.amtHtml(one[0].amount) : ""}</span>
        <span class="chev" aria-hidden="true">⌄</span></button>
      <div class="spbody"><div><ol class="spthread">${c.docs.map((d, i) => rowHtml(d, (d.tags || []).includes(from) ? from : "", i, c.docs.length)).join("")}</ol></div></div></div>`;
  }
  function paneHtml(S, un) {
    if (!S) return `<div class="empty">No spaces yet.<br><br>Spaces are your top-level folders – e.g. Wohnen, Auto, Arbeit, Steuern.<br>Create one on the left, then drag documents onto it.</div>`;
    const from = typeof S.id === "number" ? S.id : "";
    const sug = sugFor(S, un), sugShow = SUG_ALL ? sug : sug.slice(0, 3);
    const loose = LOOSE_ALL ? S.loose : S.loose.slice(0, 12);
    let h = `<div class="sphead"><div class="spname"><h2><i class="dot" style="background:${esc(S.color)}"></i>${esc(S.name)}</h2>
      ${S.subs && S.subs.length ? `<div class="spsubs">${S.subs.map((t) => `<button class="chip" data-sub="${t.id}" title="Open folder">📁 ${esc(t.name)} <span class="mut">${docsIn(t.id).length}</span></button>`).join("")}</div>` : ""}
      ${S.unsorted ? `<p class="mut">Documents that are in no folder yet. 📁 = folder of the most similar documents.</p>` : ""}</div>
      <dl class="spstats"><div><dt>Documents</dt><dd>${S.docs.length}</dd></div>${S.cases.length ? `<div><dt>Case files</dt><dd>${S.cases.length}</dd></div>` : ""}
      ${S.open != null ? `<div><dt>Open amount</dt><dd>${esc(DS.money({ v: S.open, c: "EUR" }))}</dd></div>` : ""}
      ${S.next ? `<div><dt>Next deadline</dt><dd class="due" title="${esc(S.next.title)}">${esc(short(S.next.due.d))}</dd></div>` : ""}</dl>
      ${!S.unsorted ? `<div class="spacts"><button class="lnk" id="spSub">+ Sub-folder</button><button class="lnk" id="spFolder">Folder settings</button></div>` : ""}</div>`;
    if (sug.length) {
      h += `<div class="spsug"><div class="spsugh"><span>✨ <b>${sug.length} document${sug.length > 1 ? "s" : ""}</b> not in a folder probably belong${sug.length > 1 ? "" : "s"} here</span>
        <button class="btn" id="spSugAll">Add ${sug.length > 1 ? "all " + sug.length : "it"}</button></div>
        <ol class="spthread">${sugShow.map((d, i) => rowHtml(d, "", i, sugShow.length, null, true).replace(`<span class="fx">`, `<span class="fx"><span class="spput">${Math.round(FS[d.id].score * 100)} % <button data-put="${d.id}" data-tid="${FS[d.id].id}" title="Put into ${esc(FS[d.id].name)}">✓ ${esc(FS[d.id].name)}</button></span>`)).join("")}</ol>
        ${sug.length > 3 ? `<button class="lnk" id="spSugMore">${SUG_ALL ? "show less" : "show all " + sug.length}</button>` : ""}</div>`;
    }
    if (S.unsorted) {
      const rows = (LOOSE_ALL ? S.docs : S.docs.slice(0, 40)).slice().sort((a, b) => (b.created || "").localeCompare(a.created || ""));
      h += `<h3 class="sec">To sort <small>${S.docs.length} · drag onto a space on the left</small></h3>
        <ol class="spthread flat">${rows.map((d, i) => rowHtml(d, "", i, rows.length, FS[d.id] && FS[d.id].score >= 0.3 ? FS[d.id] : null)).join("")}</ol>
        ${S.docs.length > 40 ? `<button class="lnk" id="spMore">${LOOSE_ALL ? "show less" : "show all " + S.docs.length}</button>` : ""}`;
      return h;
    }
    if (S.cases.length) h += `<h3 class="sec">Case files <small>${S.cases.length}</small></h3><div class="spcases">${S.cases.map((c) => caseHtml(c, S)).join("")}</div>`;
    if (S.loose.length) {
      const open = LOOSE_OPEN || !S.cases.length;
      h += `<button class="sptoggle${open ? " open" : ""}" id="spLoose" aria-expanded="${open}"><span class="chev" aria-hidden="true">›</span> Other documents <small>${S.loose.length}</small></button>`;
      if (open) h += `<ol class="spthread flat">${loose.map((d, i) => rowHtml(d, (d.tags || []).includes(from) ? from : "", i, loose.length)).join("")}</ol>${S.loose.length > 12 ? `<button class="lnk" id="spMore">${LOOSE_ALL ? "show less" : "show all " + S.loose.length}</button>` : ""}`;
    }
    h += `<div class="spdrop" data-dropsp="${S.id}">Drop documents here</div>`;
    return h;
  }

  // ---------------------------------------------------------------- render
  function renderSpaces() {
    const { list, un } = model();
    if (SP !== "u" && !list.some((s) => s.id === SP)) SP = list.length ? list[0].id : un.docs.length ? "u" : null;
    const S = SP === "u" ? un : list.find((s) => s.id === SP) || null;
    const total = list.reduce((a, s) => a + s.docs.length, 0);
    const main = $("#main"), keep = main.querySelector(".spr") ? main.querySelector(".spr").scrollTop : 0;
    main.innerHTML = `<div class="bar"><h2>${greeting()}</h2><span class="cnt">${DOCS.length} documents${list.length ? " in " + list.length + " space" + (list.length === 1 ? "" : "s") : ""}</span></div>
      ${welcomeHtml(un)}
      <div class="spw"><section class="spl" aria-label="Spaces"><div class="sptiles">${list.map((s) => tileHtml(s, s.id === SP)).join("")}${tileHtml(un, SP === "u")}
        <button class="sptile spnew" id="spNew"><span>＋</span><b>New space</b></button></div></section>
        <section class="spr" aria-label="${S ? esc(S.name) : "Space"}">${paneHtml(S, un)}</section></div>`;
    $("#years").innerHTML = "";
    const pane = $(".spr", main); if (keep) pane.scrollTop = keep;
    bind(main, S, un);
    fetchFS(un);
    void total;
  }

  function greeting() { const h = new Date().getHours(); return h < 5 ? "Hello" : h < 11 ? "Good morning" : h < 17 ? "Good afternoon" : h < 22 ? "Good evening" : "Hello"; }
  function welcomeHtml(un) {
    const t0 = DS.iso(DS.today()), t14 = DS.iso(new Date(DS.today().getTime() + 14 * 864e5)), wk = DS.iso(new Date(DS.today() - 7 * 864e5));
    const due = DOCS.filter((d) => d.due && d.due.d >= t0 && d.due.d <= t14).length, fresh = DOCS.filter((d) => (d.added || "") >= wk).length;
    const ch = [];
    if (due) ch.push(`<button class="wchip due" data-w="due">⏰ ${due} deadline${due > 1 ? "s" : ""} in the next 2 weeks</button>`);
    if (un.docs.length) ch.push(`<button class="wchip" data-w="sort">📥 ${un.docs.length} to sort</button>`);
    if (fresh) ch.push(`<button class="wchip" data-w="new">✨ ${fresh} new this week</button>`);
    return `<div class="welcome">${ch.length ? ch.join("") : `<span class="wchip calm">✓ Nothing needs you right now</span>`}</div>`;
  }
  function bind(main, S, un) {
    $$("[data-w]", main).forEach((b) => (b.onclick = () => {
      const w = b.dataset.w;
      if (w === "due") { VIEW = "time"; render(); }
      else if (w === "sort") { SP = "u"; store("docsorter.space", "u"); renderSpaces(); }
      else if (window.DocView) window.DocView.openNew();
    }));
    if ($("#spLoose", main)) $("#spLoose", main).onclick = () => { LOOSE_OPEN = !LOOSE_OPEN; store("docsorter.spaces.loose", LOOSE_OPEN ? "1" : "0"); renderSpaces(); };
    $$(".sptile[data-sp]", main).forEach((b) => {
      b.onclick = () => { const v = b.dataset.sp; SP = v === "u" ? "u" : +v; store("docsorter.space", String(SP)); LOOSE_ALL = SUG_ALL = false; renderSpaces(); const p = $(".spr"); if (p) { p.classList.remove("vin"); void p.offsetWidth; p.classList.add("vin"); } };
      b.ondragover = (e) => { const d = DS.drag; if (!d || d.kind !== "doc") return; e.preventDefault(); b.classList.add("drop"); e.dataTransfer.dropEffect = e.ctrlKey || e.altKey ? "copy" : "move"; };
      b.ondragleave = () => b.classList.remove("drop");
      b.ondrop = (e) => {
        e.preventDefault(); b.classList.remove("drop"); const d = DS.drag; DS.drag = null; if (!d) return;
        const v = b.dataset.sp;
        if (v === "u") { if (d.from) DS.takeOut(d); else toast("Already not in this space's folders"); }
        else DS.moveTo({ kind: "doc", ids: d.ids, from: d.from }, +v, e.ctrlKey || e.altKey);
      };
    });
    const nb = $("#spNew", main);
    if (nb) nb.onclick = () => askName(nb, "New space – e.g. Wohnen", null);
    if ($("#spSub", main)) $("#spSub", main).onclick = (e) => askName(e.currentTarget, `Sub-folder of “${S.name}”`, S.id);
    if ($("#spFolder", main)) $("#spFolder", main).onclick = () => { VIEW = "fold"; F = { kind: "tag", id: S.id }; render(); };
    $$("[data-sub]", main).forEach((b) => (b.onclick = () => { VIEW = "fold"; F = { kind: "tag", id: +b.dataset.sub }; render(); }));
    $$(".spch", main).forEach((b) => {
      b.onclick = () => { const k = b.dataset.tog, box = b.parentElement, on = !box.classList.contains("open"); box.classList.toggle("open", on); b.setAttribute("aria-expanded", on); on ? OPENC.add(k) : OPENC.delete(k); saveOpen(); };
      b.ondragover = (e) => { const d = DS.drag; if (!d || d.kind !== "doc") return; e.preventDefault(); b.classList.add("drop"); };
      b.ondragleave = () => b.classList.remove("drop");
      b.ondrop = (e) => {
        e.preventDefault(); b.classList.remove("drop"); const d = DS.drag; DS.drag = null; if (!d) return;
        const root = DS.docById(+b.parentElement.dataset.root); if (!root) return;
        OPENC.add(String(root.id)); saveOpen();
        DS.connect({ kind: "doc", ids: d.ids, from: d.from }, root, typeof S.id === "number" ? S.id : null, e.ctrlKey || e.altKey);
      };
    });
    $$(".sprow", main).forEach((r) => {
      const id = +r.dataset.id;
      r.onclick = (e) => {
        const put = e.target.closest("[data-put]");
        if (put) { e.stopPropagation(); return DS.moveTo({ kind: "doc", ids: [+put.dataset.put], from: null }, +put.dataset.tid, true); }
        if (e.target.closest(".lnk")) return;
        openDoc(id);
      };
      r.ondragstart = (e) => { const from = r.dataset.from ? +r.dataset.from : null; DS.drag = { kind: "doc", ids: [id], from, label: (DS.docById(id) || {}).title }; e.dataTransfer.effectAllowed = "copyMove"; try { e.dataTransfer.setData("text/plain", String(id)); } catch (err) { /* ignore */ } r.classList.add("dragging"); };
      r.ondragend = () => { r.classList.remove("dragging"); setTimeout(() => (DS.drag = null), 0); };
    });
    const dz = $(".spdrop", main);
    if (dz) {
      dz.ondragover = (e) => { if (!DS.drag) return; e.preventDefault(); dz.classList.add("drop"); };
      dz.ondragleave = () => dz.classList.remove("drop");
      dz.ondrop = (e) => { e.preventDefault(); dz.classList.remove("drop"); const d = DS.drag; DS.drag = null; if (d) DS.moveTo({ kind: "doc", ids: d.ids, from: d.from }, +dz.dataset.dropsp, e.ctrlKey || e.altKey); };
    }
    if ($("#spMore", main)) $("#spMore", main).onclick = () => { LOOSE_ALL = !LOOSE_ALL; renderSpaces(); };
    if ($("#spSugMore", main)) $("#spSugMore", main).onclick = () => { SUG_ALL = !SUG_ALL; renderSpaces(); };
    if ($("#spSugAll", main)) $("#spSugAll", main).onclick = () => {
      const sug = sugFor(S, un), by = {};
      sug.forEach((d) => (by[FS[d.id].id] = by[FS[d.id].id] || []).push(d.id));
      const ops = Object.entries(by).map(([tid, ids]) => ["/_lib/api/bulk", { ids, add_tags: [+tid], remove_tags: [] }]);
      const inv = Object.entries(by).map(([tid, ids]) => ["/_lib/api/bulk", { ids, add_tags: [], remove_tags: [+tid] }]);
      DS.run(`📁 ${sug.length} document${sug.length > 1 ? "s" : ""} filed into ${S.name}`, ops, inv);
    };
  }

  function askName(anchor, placeholder, parent) {
    const inp = document.createElement("input");
    inp.className = "newin spnewin"; inp.placeholder = placeholder + " – Enter";
    anchor.after(inp); inp.focus();
    inp.onkeydown = async (e) => {
      if (e.key === "Escape") return inp.remove();
      if (e.key !== "Enter" || !inp.value.trim()) return;
      try {
        const r = await api("/_lib/api/create/tag", { name: inp.value.trim(), parent });
        if (!parent) { SP = r.id; store("docsorter.space", String(r.id)); }
        toast(`Created “${r.name}” – drag documents onto it`);
        await reloadDocs();
      } catch (err) { toast(err.message, 5000); }
    };
    inp.onblur = () => setTimeout(() => inp.remove(), 150);
  }

  window.renderSpaces = renderSpaces;
})();
