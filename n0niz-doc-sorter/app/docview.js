/* Doc Sorter – document view (like the Paperless detail page) + insights.

   Left: Details (all fields incl. custom fields) · Content · Metadata · Notes
   Middle: PDF preview
   Right: AI suggestion · 💶 amount · ⏰ deadline (read from the text, save as fields
          "Betrag"/"Frist", calendar file) · 🔗 connections tree (case file, suggested
          connections, folders + suggested folder)
   Keys: Esc close · Ctrl+S save · ←/→ previous/next document (outside text fields). */
(function () {
  const DS = window.DS;
  const { fmt, money, dueHtml, amtHtml, KIND, iso, today } = DS;
  let ST = null; // {id, list, data, dirty, conn}
  const docById = (id) => DOCS.find((d) => d.id === id);
  const typeName = (id) => (byId(M.types, id) || {}).name || "";
  const corrName = (id) => (byId(M.corrs, id) || {}).name || "";
  const size = (b) => (b == null ? "–" : b > 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.round(b / 1024) + " KB");
  const CUR = ["EUR", "USD", "GBP", "CHF"];

  // ================================================================ open / close / navigation
  function listFor(id) {
    const since = iso(new Date(today() - 14 * 864e5));
    const fresh = DOCS.filter((d) => (d.added || "") >= since).sort((a, b) => (b.added || "").localeCompare(a.added || "")).map((d) => d.id);
    if (fresh.includes(id)) return { ids: fresh, name: "new" };
    return { ids: DOCS.map((d) => d.id), name: "all" };
  }
  async function open(id, opts = {}) {
    if (ST && ST.id === id && $("#dv")) { await refreshSide(); return; }
    if (ST && ST.dirty && !confirm("Discard unsaved changes?")) return;
    const list = opts.list ? { ids: opts.list, name: opts.listName || "list" } : listFor(id);
    ST = { id, list, dirty: false, data: null };
    OPEN = id;
    let el = $("#dv");
    if (!el) { el = document.createElement("div"); el.id = "dv"; document.body.appendChild(el); }
    clearTimeout(el._ct);
    const was = el.classList.contains("open");
    el.className = "dv open" + (was ? " stay" : "");
    const pos = list.ids.indexOf(id);
    el.innerHTML = `<div class="dvsheet"><div class="dvtop"><button class="ib" id="dvX" title="Close (Esc)">←</button>
        <input id="dvTitle" class="dvtitle" value="${esc((docById(id) || {}).title || "")}" placeholder="Title">
        <span class="dvpos">${pos >= 0 ? `${pos + 1} / ${list.ids.length}${list.name === "new" ? " new" : ""}` : ""}</span>
        <button class="ib" id="dvPrev" title="Previous (←)" ${pos <= 0 ? "disabled" : ""}>‹</button><button class="ib" id="dvNext" title="Next (→)" ${pos < 0 || pos >= list.ids.length - 1 ? "disabled" : ""}>›</button>
        <a class="ib" id="dvDl" href="/_lib/download/${id}" title="Download original">⬇</a>
        <a class="ib" target="_blank" href="${esc((M.paperless || "") + "/documents/" + id + "/details")}" title="Open in Paperless">Paperless ↗</a>
        <button class="btn sec" id="dvDiscard" disabled>Discard</button><button class="btn" id="dvSave" disabled title="Ctrl+S">Save</button></div>
      <div class="dvbody"><section class="dvl"><div class="dvtabs"><button data-t="details" class="on">Details</button><button data-t="content">Content</button><button data-t="meta">Metadata</button><button data-t="notes">Notes</button></div><div class="dvpanel" id="dvPanel"><p class="thint">Loading…</p></div></section>
        <section class="dvc"><iframe id="dvFrame" title="Preview" src="/_lib/preview/${id}"></iframe></section>
        <section class="dvr" id="dvSide"><p class="thint">Loading…</p></section></div></div>`;
    el.onmousedown = (e) => { if (e.target === el) close(); };
    el.tabIndex = -1;
    const fr = $("#dvFrame");
    // the PDF viewer grabs the keyboard focus when it loads – give it back so Esc and ←/→ keep working
    let over = false; const t0 = Date.now();
    fr.onmouseenter = () => (over = true); fr.onmouseleave = () => (over = false);
    const back = () => { if (!over && document.activeElement === fr && Date.now() - t0 < 5000) el.focus({ preventScroll: true }); };
    window.addEventListener("blur", () => setTimeout(back, 0), { once: true });
    [80, 400, 1200, 2500].forEach((ms) => setTimeout(back, ms));
    el.focus({ preventScroll: true });
    $("#dvX").onclick = close;
    $("#dvPrev").onclick = () => step(-1);
    $("#dvNext").onclick = () => step(1);
    $("#dvSave").onclick = save;
    $("#dvDiscard").onclick = () => { ST.dirty = false; const i = ST.id; ST = null; open(i, { list: list.ids, listName: list.name }); };
    $("#dvTitle").oninput = dirty;
    $$(".dvtabs button", el).forEach((b) => (b.onclick = () => tab(b.dataset.t)));
    document.body.classList.add("dvopen");
    try {
      ST.data = await api(`/_lib/api/doc/${id}/full`);
    } catch (e) { $("#dvPanel").innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }
    if (!ST || ST.id !== id) return;
    $("#dvTitle").value = ST.data.doc.title || "";
    tab("details");
    side($("#dvSide"), id, { full: true });
  }
  function close() {
    if (ST && ST.dirty && !confirm("Discard unsaved changes?")) return;
    ST = null; OPEN = null;
    const el = $("#dv");
    if (el) { el.className = "dv closing"; clearTimeout(el._ct); el._ct = setTimeout(() => { if (!ST) { el.className = "dv"; el.innerHTML = ""; } }, 230); }
    document.body.classList.remove("dvopen");
  }
  function step(d) {
    if (!ST) return;
    const i = ST.list.ids.indexOf(ST.id), n = ST.list.ids[i + d];
    if (n === undefined) return;
    if (ST.dirty && !confirm("Discard unsaved changes?")) return;
    const L = ST.list; ST.dirty = false; ST = null;
    open(n, { list: L.ids, listName: L.name });
  }
  function dirty() { if (!ST) return; ST.dirty = true; $("#dvSave").disabled = false; $("#dvDiscard").disabled = false; }
  document.addEventListener("keydown", (e) => {
    if (!ST || !$("#dv.open")) return;
    if (e.key === "Escape" && !$(".menu")) { e.preventDefault(); return close(); }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); return save(); }
    if (/INPUT|TEXTAREA|SELECT/.test(e.target.tagName) || e.target.isContentEditable) return;
    if (e.key === "ArrowLeft" && !e.target.closest("#dvConn")) step(-1);
    if (e.key === "ArrowRight" && !e.target.closest("#dvConn")) step(1);
  }, true);

  // ================================================================ tabs
  function tab(t) {
    $$(".dvtabs button").forEach((b) => b.classList.toggle("on", b.dataset.t === t));
    const P = $("#dvPanel"), D = ST.data.doc;
    if (ST.form && t !== "details") ST.form = collect();
    if (t === "details") return details(P);
    if (t === "content") {
      P.innerHTML = `<div class="crow"><input id="dvFind" placeholder="Find in text…"><button class="btn sec" id="dvCopy">Copy</button></div><pre class="dvtext" id="dvText"></pre>`;
      const paint = () => { const q = $("#dvFind").value.trim(); let h = esc(D.content || "(no text recognized)"); if (q.length > 1) { const rx = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"); h = h.replace(rx, (m) => `<mark>${m}</mark>`); } $("#dvText").innerHTML = h; const m = $("#dvText mark"); if (m) m.scrollIntoView({ block: "center" }); };
      $("#dvFind").oninput = paint; paint();
      $("#dvCopy").onclick = () => { navigator.clipboard && navigator.clipboard.writeText(D.content || ""); toast("Text copied"); };
      return;
    }
    if (t === "meta") {
      const m = ST.data.meta || {};
      const rows = [["Original file", D.original_file_name], ["Archived file", D.archived_file_name], ["Stored as", m.media_filename], ["Type", D.mime_type || m.original_mime_type], ["Pages", D.page_count],
        ["Original size", size(m.original_size)], ["Archive size", m.has_archive_version ? size(m.archive_size) : "–"], ["Checksum (MD5)", m.original_checksum], ["Language", m.lang],
        ["Created", fmt(D.created_date || D.created)], ["Added", (D.added || "").replace("T", " ").slice(0, 16)], ["Modified", (D.modified || "").replace("T", " ").slice(0, 16)], ["Owner", D.owner != null ? "#" + D.owner : "–"], ["ID", D.id]];
      const extra = (m.original_metadata || []).slice(0, 60);
      P.innerHTML = `<table class="mt">${rows.map(([k, v]) => `<tr><th>${k}</th><td>${esc(v == null || v === "" ? "–" : v)}</td></tr>`).join("")}</table>`
        + (extra.length ? `<details class="mtx"><summary>File metadata (${extra.length})</summary><table class="mt">${extra.map((x) => `<tr><th>${esc(x.prefix ? x.prefix + ":" : "")}${esc(x.key)}</th><td>${esc(String(x.value).slice(0, 200))}</td></tr>`).join("")}</table></details>` : "");
      return;
    }
    if (t === "notes") return notes(P, ST.data.doc.notes || []);
  }

  // ================================================================ details form
  function details(P) {
    const D = ST.data.doc, f = ST.form || {
      asn: D.archive_serial_number ?? "", created: (D.created_date || D.created || "").slice(0, 10), corr: D.correspondent, type: D.document_type, path: D.storage_path,
      tags: [...(D.tags || [])], cfs: (D.custom_fields || []).map((c) => ({ field: c.field, value: c.value })),
    };
    ST.form = f;
    const opt = (arr, cur, none = "–") => `<option value="">${none}</option>` + arr.slice().sort((a, b) => a.name.localeCompare(b.name)).map((x) => `<option value="${x.id}" ${x.id === cur ? "selected" : ""}>${esc(x.name)}</option>`).join("");
    P.innerHTML = `<div class="frm">
      <div class="row2"><label>Date<input type="date" id="fCreated" value="${esc(f.created)}"></label><label>Archive serial number (ASN)<input type="number" id="fAsn" value="${esc(f.asn)}" placeholder="–"></label></div>
      <label>Correspondent<div class="sel2"><select id="fCorr">${opt(M.corrs, f.corr)}</select><button class="ib" data-new="corr" title="New correspondent">+</button></div></label>
      <label>Document type<div class="sel2"><select id="fType">${opt(M.types, f.type)}</select><button class="ib" data-new="type" title="New document type">+</button></div></label>
      <label>Storage path<select id="fPath">${opt(M.paths || [], f.path, "Default")}</select></label>
      <label>Folders / tags</label><div class="tags" id="fTags"></div>
      <h5>Custom fields</h5><div id="fCfs"></div>
      <div class="sel2"><select id="fAddCf"><option value="">+ Add field…</option>${(M.fields || []).map((x) => `<option value="${x.id}">${esc(x.name)} (${esc(x.type)})</option>`).join("")}</select></div>
    </div>`;
    const tagsPaint = () => {
      $("#fTags").innerHTML = f.tags.map((t) => { const tg = byId(M.tags, t) || {}; return `<span class="chip"${tg.color ? ` style="border-color:${esc(tg.color)}"` : ""}>${isFolder(t) ? "📁 " : "🏷 "}${esc(tg.name || "#" + t)}<button data-rm="${t}" title="Remove">×</button></span>`; }).join("") + `<button class="chip add" id="fAddTag">+ add</button>`;
      $$("[data-rm]", $("#fTags")).forEach((b) => (b.onclick = () => { f.tags = f.tags.filter((x) => x !== +b.dataset.rm); tagsPaint(); dirty(); }));
      $("#fAddTag").onclick = (e) => pickMenu(e.target, "tag", (tid) => { if (!f.tags.includes(tid)) f.tags.push(tid); tagsPaint(); dirty(); });
    };
    tagsPaint();
    cfsPaint(f);
    $("#fAddCf").onchange = (e) => { const id = +e.target.value; e.target.value = ""; if (id && !f.cfs.some((c) => c.field === id)) { f.cfs.push({ field: id, value: null }); cfsPaint(f); dirty(); } };
    ["fCreated", "fAsn", "fCorr", "fType", "fPath"].forEach((i) => ($("#" + i).oninput = $("#" + i).onchange = () => { Object.assign(f, collectBasics()); dirty(); }));
    $$("[data-new]", P).forEach((b) => (b.onclick = async () => {
      const kind = b.dataset.new, name = prompt(kind === "corr" ? "New correspondent" : "New document type");
      if (!name || !name.trim()) return;
      try { const r = await api(`/_lib/api/create/${kind}`, { name: name.trim() }); (kind === "corr" ? M.corrs : M.types).push({ id: r.id, name: r.name, count: 0 }); f[kind === "corr" ? "corr" : "type"] = r.id; details(P); dirty(); } catch (e) { toast(e.message, 5000); }
    }));
  }
  function collectBasics() {
    return { created: $("#fCreated").value, asn: $("#fAsn").value, corr: +$("#fCorr").value || null, type: +$("#fType").value || null, path: +$("#fPath").value || null };
  }
  function cfDef(id) { return (M.fields || []).find((x) => x.id === id) || { id, name: "#" + id, type: "string", extra: {} }; }
  function cfsPaint(f) {
    const box = $("#fCfs");
    if (!f.cfs.length) { box.innerHTML = `<p class="thint">No custom fields on this document.</p>`; return; }
    box.innerHTML = f.cfs.map((c, i) => {
      const d = cfDef(c.field), v = c.value;
      let inp;
      if (d.type === "boolean") inp = `<input type="checkbox" data-cf="${i}" ${v ? "checked" : ""}>`;
      else if (d.type === "date") inp = `<input type="date" data-cf="${i}" value="${esc(v || "")}">`;
      else if (d.type === "integer" || d.type === "float") inp = `<input type="number" step="${d.type === "integer" ? 1 : "any"}" data-cf="${i}" value="${esc(v ?? "")}">`;
      else if (d.type === "monetary") { const m = /^([A-Z]{3})?(-?[\d.]+)$/.exec(String(v ?? "")) || []; const cur = m[1] || (d.extra && d.extra.default_currency) || "EUR"; inp = `<span class="mon"><select data-cfc="${i}">${CUR.concat(CUR.includes(cur) ? [] : [cur]).map((x) => `<option ${x === cur ? "selected" : ""}>${x}</option>`).join("")}</select><input type="number" step="0.01" data-cf="${i}" value="${esc(m[2] || "")}"></span>`; }
      else if (d.type === "select") { const opts = (d.extra && d.extra.select_options) || []; inp = `<select data-cf="${i}"><option value="">–</option>${opts.map((o, j) => { const id = typeof o === "object" ? o.id : String(j), lb = typeof o === "object" ? o.label : o; return `<option value="${esc(id)}" ${String(v) === String(id) ? "selected" : ""}>${esc(lb)}</option>`; }).join("")}</select>`; }
      else if (d.type === "documentlink") inp = `<span class="tags">${(v || []).map((x) => `<span class="chip">${esc((docById(x) || {}).title || "#" + x)}<button data-cfl="${i}" data-x="${x}">×</button></span>`).join("")}<button class="chip add" data-cfadd="${i}">+ link</button></span>`;
      else if (d.type === "longtext") inp = `<textarea data-cf="${i}" rows="3">${esc(v || "")}</textarea>`;
      else inp = `<input type="${d.type === "url" ? "url" : "text"}" data-cf="${i}" value="${esc(v ?? "")}">`;
      return `<div class="cfr"><span class="cfn" title="${esc(d.type)}">${esc(d.name)}</span>${inp}<button class="ib" data-cfrm="${i}" title="Remove field">×</button></div>`;
    }).join("");
    $$("[data-cf],[data-cfc]", box).forEach((el) => (el.oninput = el.onchange = () => { readCfs(f); dirty(); }));
    $$("[data-cfrm]", box).forEach((b) => (b.onclick = () => { f.cfs.splice(+b.dataset.cfrm, 1); cfsPaint(f); dirty(); }));
    $$("[data-cfl]", box).forEach((b) => (b.onclick = () => { const c = f.cfs[+b.dataset.cfl]; c.value = (c.value || []).filter((x) => x !== +b.dataset.x); cfsPaint(f); dirty(); }));
    $$("[data-cfadd]", box).forEach((b) => (b.onclick = (e) => pickDoc(e.target, (id) => { const c = f.cfs[+b.dataset.cfadd]; c.value = [...new Set([...(c.value || []), id])]; cfsPaint(f); dirty(); })));
  }
  function readCfs(f) {
    f.cfs.forEach((c, i) => {
      const d = cfDef(c.field), el = $(`[data-cf="${i}"]`);
      if (!el) return;
      if (d.type === "boolean") c.value = el.checked;
      else if (d.type === "integer") c.value = el.value === "" ? null : parseInt(el.value, 10);
      else if (d.type === "float") c.value = el.value === "" ? null : parseFloat(el.value);
      else if (d.type === "monetary") c.value = el.value === "" ? null : $(`[data-cfc="${i}"]`).value + (+el.value).toFixed(2);
      else c.value = el.value === "" ? null : el.value;
    });
  }
  function collect() {
    const f = ST.form;
    if ($("#fCreated")) { Object.assign(f, collectBasics()); readCfs(f); }
    return f;
  }
  async function save() {
    if (!ST || !ST.dirty) return;
    const f = collect(), id = ST.id;
    const body = { title: $("#dvTitle").value.trim() || ST.data.doc.title, created: f.created || undefined, archive_serial_number: f.asn === "" ? null : +f.asn,
      correspondent: f.corr, document_type: f.type, storage_path: f.path, tags: f.tags, custom_fields: f.cfs.filter((c) => c.value !== undefined).map((c) => ({ field: c.field, value: c.value })) };
    $("#dvSave").disabled = true; $("#dvSave").textContent = "Saving…";
    try {
      await api(`/_lib/api/doc/${id}`, body, "PATCH");
      toast("✓ Saved");
      ST.dirty = false; ST.form = null;
      ST.data = await api(`/_lib/api/doc/${id}/full`);
      $("#dvDiscard").disabled = true;
      tab("details"); await reloadDocs(); refreshSide();
    } catch (e) { toast(e.message, 7000); $("#dvSave").disabled = false; }
    $("#dvSave").textContent = "Save";
  }
  function notes(P, L) {
    P.innerHTML = `<div class="notes">${L.length ? L.slice().reverse().map((n) => `<div class="note"><div class="nh"><b>${esc((n.user && (n.user.username || n.user.first_name)) || "")}</b> <span class="thint">${esc((n.created || "").replace("T", " ").slice(0, 16))}</span><button class="ib" data-del="${n.id}" title="Delete note">×</button></div><div>${esc(n.note).replace(/\n/g, "<br>")}</div></div>`).join("") : `<p class="thint">No notes yet.</p>`}</div>
      <textarea id="nText" rows="3" placeholder="Write a note…"></textarea><button class="btn" id="nAdd">Add note</button>`;
    $("#nAdd").onclick = async () => { const t = $("#nText").value.trim(); if (!t) return; try { ST.data.doc.notes = await api(`/_lib/api/doc/${ST.id}/notes`, { note: t }); notes(P, ST.data.doc.notes); } catch (e) { toast(e.message, 5000); } };
    $$("[data-del]", P).forEach((b) => (b.onclick = async () => { if (!confirm("Delete this note?")) return; try { ST.data.doc.notes = await api(`/_lib/api/doc/${ST.id}/notes/delete`, { id: +b.dataset.del }); notes(P, ST.data.doc.notes); } catch (e) { toast(e.message, 5000); } }));
  }
  function pickDoc(anchor, cb) {
    $$(".menu").forEach((m) => m.remove());
    const m = document.createElement("div"); m.className = "menu"; m.innerHTML = `<input placeholder="Search documents…"><div class="list"></div>`;
    document.body.appendChild(m);
    const r = anchor.getBoundingClientRect(); m.style.left = Math.min(r.left, innerWidth - 270) + "px"; m.style.top = Math.min(r.bottom + 6, innerHeight - 370) + "px";
    const inp = $("input", m), list = $(".list", m);
    const draw = () => { const q = inp.value.trim().toLowerCase(); list.innerHTML = DOCS.filter((d) => d.id !== (ST && ST.id) && (!q || docText(d).includes(q))).slice(0, 40).map((d) => `<div class="it" data-id="${d.id}">${esc(d.title)} <span class="thint">${fmt(d.created)}</span></div>`).join(""); $$(".it", list).forEach((el) => (el.onclick = () => { m.remove(); cb(+el.dataset.id); })); };
    inp.oninput = draw; draw(); inp.focus();
    inp.onkeydown = (e) => { if (e.key === "Escape") m.remove(); if (e.key === "Enter") { const el = $(".it", list); if (el) el.click(); } };
    setTimeout(() => document.addEventListener("mousedown", function off(e) { if (!m.contains(e.target)) { m.remove(); document.removeEventListener("mousedown", off); } }), 0);
  }

  // ================================================================ insights (right column / tree pane)
  async function side(box, id, o = {}) {
    let data = o.full && ST && ST.id === id ? ST.data : null;
    if (!data) {
      box.innerHTML = `<p class="thint">Loading…</p>`;
      try { data = await api(`/_lib/api/doc/${id}/full`); } catch (e) { box.innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }
      if (!box.isConnected) return;
    }
    const D = data.doc, fx = data.facts || {}, ff = data.fact_fields || {}, ai = AI.results[id] || data.ai, aiErr = AI.errors[id];
    const cfm = {}; (D.custom_fields || []).forEach((c) => (cfm[c.field] = c.value));
    const savedA = ff.amount && cfm[ff.amount] ? (/^([A-Z]{3})?(-?[\d.]+)$/.exec(String(cfm[ff.amount])) || []) : null;
    const savedAmt = savedA && savedA[2] ? { v: +savedA[2], c: savedA[1] || "EUR" } : null, savedDue = ff.due && cfm[ff.due] ? String(cfm[ff.due]).slice(0, 10) : null;
    const queued = AI.queue.includes(id) || AI.busy === id;
    let h = "";
    if (!o.full) {
      h += `<div class="pdoc"><img src="/_lib/thumb/${id}" class="pth" data-open="${id}" title="Open document view"><div><h3>${esc(D.title)}</h3>
        <p class="thint">${fmt(D.created_date || D.created)}${D.document_type ? " · " + esc(typeName(D.document_type)) : ""}${D.correspondent ? " · " + esc(corrName(D.correspondent)) : ""}</p>
        <div class="tags">${(D.tags || []).filter(isFolder).map((t) => `<span class="chip">📁 ${esc(tagName(t))}</span>`).join("") || '<span class="thint">not in a folder</span>'}</div>
        <div class="irow" style="margin-top:8px"><button class="btn" data-open="${id}">Open document view</button></div></div></div>`;
    }
    // AI
    h += `<div class="icard"><div class="ch"><b>✨ AI</b>${ai ? `<span class="thint">${ai.secs || ""}s</span>` : ""}</div>`;
    if (ai) {
      const row = (k, label, val, isNew) => val ? `<label class="ck"><input type="checkbox" data-f="${k}" checked><span>${label}: <b>${val}</b>${isNew ? ' <span class="new">new</span>' : ""}</span></label>` : "";
      h += row("title", "Title", esc(ai.title)) + row("type", "Type", ai.type && esc(ai.type.name), ai.type && !ai.type.id) + row("folders", "Folders", ai.folders && ai.folders.length && ai.folders.map((x) => esc(x.name) + (x.id ? "" : ' <span class="new">new</span>')).join(", "))
        + row("corr", "From", ai.corr && esc(ai.corr.name), ai.corr && !ai.corr.id) + row("date", "Date", ai.date && fmt(ai.date)) + row("amount", "Amount", ai.amount && money(ai.amount)) + row("due", "Deadline", ai.due && fmt(ai.due.d) + (ai.due.why ? " – " + esc(ai.due.why) : ""))
        + `<div class="irow"><button class="btn ai" id="xApplyAI">✓ Apply ticked</button><button class="btn sec" id="xDropAI">Discard</button></div>`;
    } else h += `<p class="thint">${queued ? "✨ The AI is reading this document…" : aiErr ? "Failed: " + esc(aiErr) : "Title, type, folder, sender, date, amount and deadline in one request."}</p>${queued ? "" : `<button class="btn ai" id="xAI">✨ Recognize</button>`}`;
    h += `</div>`;
    // amount
    const amts = fx.amounts || [];
    h += `<div class="icard"><div class="ch"><b>💶 Amount</b>${savedAmt ? `<span class="ok">saved: ${money(savedAmt)}</span>` : amts.length ? `<span class="thint">found in the text</span>` : ""}</div>`;
    if (amts.length || ai && ai.amount) {
      const sel = savedAmt ? -1 : 0;
      h += `<div class="opts">${amts.slice(0, 4).map((a, i) => `<label class="opt"><input type="radio" name="amt" value="${i}" ${i === sel ? "checked" : ""}><span><b>${a.kind === "refund" ? "↩ " : ""}${money(a)}</b> <span class="thint">${esc(a.label ? a.label.charAt(0).toUpperCase() + a.label.slice(1) : "")}${a.p ? " · " + (a.p === "month" ? "monthly" : a.p === "year" ? "yearly" : "quarterly") : ""}${a.kind === "refund" ? " · refund/credit" : ""}</span><br><small>“${esc(a.snip)}”</small></span></label>`).join("")}
        ${ai && ai.amount ? `<label class="opt"><input type="radio" name="amt" value="ai"><span><b>${money(ai.amount)}</b> <span class="thint">✨ AI</span></span></label>` : ""}</div>`;
    } else h += `<p class="thint">No amount found in the text.</p>`;
    h += `<div class="crow"><span class="mon"><select id="xCur">${CUR.map((x) => `<option ${x === ((savedAmt && savedAmt.c) || (amts[0] && amts[0].c) || "EUR") ? "selected" : ""}>${x}</option>`).join("")}</select><input type="number" step="0.01" id="xAmt" placeholder="0.00" value="${savedAmt ? savedAmt.v.toFixed(2) : amts[0] ? Math.abs(amts[0].v).toFixed(2) : ""}"></span><button class="btn" id="xSaveAmt">${savedAmt ? "Update" : "Save as “Betrag”"}</button>${savedAmt ? `<button class="ib" id="xDelAmt" title="Remove">×</button>` : ""}</div></div>`;
    // deadline
    const dls = fx.deadlines || [];
    h += `<div class="icard"><div class="ch"><b>⏰ Deadline</b>${savedDue ? `<span class="ok">saved: ${fmt(savedDue)}</span>` : dls.length ? `<span class="thint">found in the text</span>` : ""}</div>`;
    if (dls.length || ai && ai.due) {
      const sel = savedDue ? -1 : 0;
      h += `<div class="opts">${dls.slice(0, 4).map((d, i) => `<label class="opt"><input type="radio" name="due" value="${i}" ${i === sel ? "checked" : ""}><span>${dueHtml({ d: d.d, k: d.kind, s: !(savedDue === d.d) })} <span class="thint">${esc(KIND[d.kind] || d.kind)}${d.rel ? " · " + esc(d.rel) : ""}</span><br><small>“${esc(d.snip)}”</small></span></label>`).join("")}
        ${ai && ai.due ? `<label class="opt"><input type="radio" name="due" value="ai"><span>${dueHtml({ d: ai.due.d })} <span class="thint">✨ AI${ai.due.why ? " · " + esc(ai.due.why) : ""}</span></span></label>` : ""}</div>`;
    } else h += `<p class="thint">No deadline found in the text.</p>`;
    if ((fx.notes || []).length) h += `<p class="thint">${fx.notes.map(esc).join(" · ")}</p>`;
    const dueVal = savedDue || (dls[0] && dls[0].d) || "";
    h += `<div class="crow"><input type="date" id="xDue" value="${esc(dueVal)}"><button class="btn" id="xSaveDue">${savedDue ? "Update" : "Save as “Frist”"}</button>${savedDue ? `<button class="ib" id="xDelDue" title="Remove">×</button>` : ""}<button class="ib" id="xIcs" title="Calendar file (.ics) with a reminder 3 days before" ${dueVal ? "" : "disabled"}>📅</button></div></div>`;
    // connections
    h += `<div class="icard"><div class="ch"><b>🔗 Connections</b><span class="thint">expand for details</span></div><div class="conn tuiwrap" id="${o.full ? "dvConn" : "pConn"}"></div></div>`;
    box.innerHTML = h;
    bind(box, id, { D, fx, ai, savedAmt, savedDue, o });
    conn(box.querySelector(o.full ? "#dvConn" : "#pConn"), id, D);
  }
  function bind(box, id, c) {
    const q = (s) => box.querySelector(s);
    $$("[data-open]", box).forEach((el) => (el.onclick = () => open(+el.dataset.open)));
    if (q("#xAI")) q("#xAI").onclick = async () => { await recognize([id]); refreshSide(box, id, c.o); };
    if (q("#xApplyAI")) q("#xApplyAI").onclick = async () => {
      try { await api(`/_lib/api/ai/apply/${id}`, { suggestion: c.ai, fields: $$("[data-f]:checked", box).map((x) => x.dataset.f) }); delete AI.results[id]; toast("✓ AI suggestion applied"); await reloadDocs(); if (ST && ST.id === id) { ST.form = null; ST.data = await api(`/_lib/api/doc/${id}/full`); $("#dvTitle").value = ST.data.doc.title; tab("details"); } refreshSide(box, id, c.o); } catch (e) { toast(e.message, 6000); }
    };
    if (q("#xDropAI")) q("#xDropAI").onclick = async () => { AI = await api("/_lib/api/ai/clear", { ids: [id] }); refreshSide(box, id, c.o); };
    $$('input[name="amt"]', box).forEach((r) => (r.onchange = () => { const a = r.value === "ai" ? c.ai.amount : c.fx.amounts[+r.value]; q("#xAmt").value = Math.abs(a.v).toFixed(2); q("#xCur").value = a.c || "EUR"; }));
    $$('input[name="due"]', box).forEach((r) => (r.onchange = () => { q("#xDue").value = r.value === "ai" ? c.ai.due.d : c.fx.deadlines[+r.value].d; q("#xIcs").disabled = false; }));
    q("#xSaveAmt").onclick = async () => {
      const v = parseFloat(q("#xAmt").value); if (isNaN(v)) return toast("Enter an amount");
      const pick = $('input[name="amt"]:checked', box), a = pick && pick.value !== "ai" ? c.fx.amounts[+pick.value] : null;
      await DS.saveFact(id, "amount", { v: a && a.kind === "refund" ? -Math.abs(v) : v, c: q("#xCur").value }); afterFact(box, id, c);
    };
    if (q("#xDelAmt")) q("#xDelAmt").onclick = async () => { await DS.run("💶 Amount removed", [[`/_lib/api/doc/${id}/facts`, { amount: null }]], [[`/_lib/api/doc/${id}/facts`, { amount: c.savedAmt }]]); afterFact(box, id, c); };
    q("#xSaveDue").onclick = async () => { const v = q("#xDue").value; if (!v) return toast("Pick a date"); await DS.saveFact(id, "due", v); afterFact(box, id, c); };
    if (q("#xDelDue")) q("#xDelDue").onclick = async () => { await DS.run("⏰ Deadline removed", [[`/_lib/api/doc/${id}/facts`, { due: null }]], [[`/_lib/api/doc/${id}/facts`, { due: c.savedDue }]]); afterFact(box, id, c); };
    q("#xDue").oninput = () => (q("#xIcs").disabled = !q("#xDue").value);
    q("#xIcs").onclick = () => ics(c.D, q("#xDue").value, ($('input[name="due"]:checked', box) || {}).value, c);
  }
  async function afterFact(box, id, c) {
    if (ST && ST.id === id) { ST.data = await api(`/_lib/api/doc/${id}/full`); ST.form = null; if ($(".dvtabs .on") && $(".dvtabs .on").dataset.t === "details" && !ST.dirty) tab("details"); }
    refreshSide(box, id, c.o);
  }
  function ics(D, date, pick, c) {
    if (!date) return;
    const k = pick && pick !== "ai" && c.fx.deadlines[+pick] ? KIND[c.fx.deadlines[+pick].kind] : "Deadline";
    const d = date.replace(/-/g, ""), nd = new Date(date + "T00:00"); nd.setDate(nd.getDate() + 1);
    const amt = c.savedAmt ? money(c.savedAmt) : c.fx.amount ? money(c.fx.amount) : "";
    const escI = (s) => String(s).replace(/[\\;,]/g, (m) => "\\" + m).replace(/\n/g, "\\n");
    const txt = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Doc Sorter//EN", "BEGIN:VEVENT", `UID:docsorter-${D.id}-${d}@umbrel`, `DTSTAMP:${new Date().toISOString().replace(/[-:]/g, "").slice(0, 15)}Z`,
      `DTSTART;VALUE=DATE:${d}`, `DTEND;VALUE=DATE:${iso(nd).replace(/-/g, "")}`, `SUMMARY:${escI(k.charAt(0).toUpperCase() + k.slice(1) + ": " + D.title)}`,
      `DESCRIPTION:${escI((amt ? amt + " – " : "") + (M.paperless || "") + "/documents/" + D.id + "/details")}`, "BEGIN:VALARM", "TRIGGER:-P3D", "ACTION:DISPLAY", `DESCRIPTION:${escI(D.title)}`, "END:VALARM", "END:VEVENT", "END:VCALENDAR"].join("\r\n");
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([txt], { type: "text/calendar" })); a.download = `${D.title.replace(/[^\w\däöüÄÖÜß -]/g, "").slice(0, 60) || "deadline"}.ics`; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }
  function refreshSide(box, id, o) {
    box = box || $("#dvSide") || $("#tpane"); id = id || (ST && ST.id); o = o || { full: !!(ST && box && box.id === "dvSide") };
    if (box && id) return side(box, id, o);
  }

  // ---------------------------------------------------------------- connections tree
  const REL = {};
  async function conn(el, id, D) {
    if (!el) return;
    const comp = DS.components().get(id), me = docById(id) || { id, title: D.title, tags: D.tags || [], links: [] };
    const folderIds = (D.tags || []).filter(isFolder);
    const ctx = folderIds[0] || null;
    const info = (d, extra) => `<div class="info"><div class="irow">${(d.tags || []).filter(isFolder).map((t) => `<span class="chip">📁 ${esc(tagName(t))}</span>`).join("") || '<span class="mut">not in a folder</span>'}${d.amount ? `<span class="chip">${amtHtml(d.amount)}</span>` : ""}${d.due ? `<span class="chip">${dueHtml(d.due)}</span>` : ""}</div><div class="snip" data-snip="${d.id}">${DS.SNIP[d.id] !== undefined ? esc(DS.SNIP[d.id]) : "…"}</div>${extra || ""}</div>`;
    const docN = (d, k, o = {}) => ({
      key: k, type: "doc", id: d.id, cls: "tdoc" + (d.id === id ? " me" : ""), icon: `<img class="th" loading="lazy" src="/_lib/thumb/${d.id}" alt="">`,
      label: esc(d.title) + (d.id === id ? ' <span class="bd">this document</span>' : ""), detail: [o.pre, fmt(d.created), typeName(d.document_type), corrName(d.correspondent)].filter(Boolean).map((x) => (x === o.pre ? x : esc(x))).join(" · ") + (d.amount ? " · " + amtHtml(d.amount) : "") + (d.due ? " " + dueHtml(d.due) : ""),
      actions: o.actions || "", kids: d.id === id ? undefined : () => [{ key: "i" + k, html: info(d), id: d.id }],
    });
    const linked = new Set(comp ? comp.docs.map((d) => d.id) : []);
    let rel = REL[id];
    const build = () => {
      const top = [];
      if (comp) top.push({ key: "cg", type: "group", cls: "tgrp", icon: `<span class="ic">🗂</span>`, label: "Case file", count: comp.docs.length, open: true, detail: `${fmt(comp.docs[0].created).slice(3)} → ${fmt(comp.docs[comp.docs.length - 1].created).slice(3)} · oldest first`,
        kids: () => comp.docs.map((d) => docN(d, "c" + d.id, { actions: d.id !== id && (me.links || []).includes(d.id) ? `<button data-a="unlink" title="Disconnect from this document">✂</button>` : "" })) });
      const sug = (rel || []).filter((r) => !linked.has(r.id) && r.id !== id).slice(0, 10);
      top.push({ key: "sg", type: "group", cls: "tgrp", icon: `<span class="ic">💡</span>`, label: "Suggested connections", count: rel ? sug.length : "…", open: true,
        detail: rel ? (sug.length ? "similar documents – 🔗 connect, 📁 put into the same folder" : "nothing similar found") : "looking for similar documents…",
        kids: () => sug.map((r, i) => { const d = docById(r.id) || r; return docN(d, "s" + d.id, { pre: `<span class="sim" title="Rank by similarity">#${i + 1}</span>`, actions: `<button data-a="link" title="Connect with this document">🔗</button><button data-a="together" title="Put both into the same folder">📁</button>` }); }) });
      top.push({ key: "fg", type: "group", cls: "tgrp", icon: `<span class="ic">📁</span>`, label: "Folders", count: folderIds.length, open: true, detail: folderIds.length ? folderIds.map(tagName).map(esc).join(", ") : "not in a folder yet",
        kids: () => [
          ...folderIds.map((t) => { const ds = docsIn(t).filter((d) => d.id !== id); return { key: "f" + t, type: "folder", id: t, cls: "tfold", icon: `<span class="dot" style="background:${esc((byId(M.tags, t) || {}).color || "#64748b")}"></span>`, label: esc(tagName(t)), count: ds.length, detail: "other documents in this folder", kids: () => ds.slice(0, 40).map((d) => docN(d, "fd" + t + ":" + d.id)), empty: "Only this document." }; }),
          { key: "fs", type: "suggest", cls: "tsug", icon: `<span class="ic">💡</span>`, label: "Suggested folder", detail: `<span id="fsug">…</span>`, actions: "" },
        ] });
      return top;
    };
    const T = window.TreeUI(el, {
      onOpen: (n) => (n.type === "doc" && n.id !== id ? open(n.id) : T.toggle(n.key)),
      onAction: async (n, b) => {
        const a = b.dataset.a, other = docById(n.id);
        if (a === "link") await DS.connect({ kind: "doc", ids: [n.id], from: null }, me, null, true);
        if (a === "unlink") await DS.run(`✂ “${other.title}” disconnected`, [["/_lib/api/link", { a: id, b: n.id, on: false }]], [["/_lib/api/link", { a: id, b: n.id }]]);
        if (a === "together") {
          const mine = (me.tags || []).filter(isFolder), theirs = (other.tags || []).filter(isFolder);
          if (mine.length) await DS.moveTo({ kind: "doc", ids: [n.id], from: null }, mine[0], true);
          else if (theirs.length) await DS.moveTo({ kind: "doc", ids: [id], from: null }, theirs[0], true);
          else {
            const name = prompt("Neither is in a folder yet. Name for a new folder:", corrName(me.correspondent) || me.title);
            if (!name || !name.trim()) return;
            const t = await api("/_lib/api/create/tag", { name: name.trim() });
            await DS.run(`📁 “${name.trim()}” with both documents`, [["/_lib/api/bulk", { ids: [id, n.id], add_tags: [t.id] }]], [["/_lib/api/bulk", { ids: [id, n.id], remove_tags: [t.id] }]]);
          }
        }
        if (a === "move" || a === "also") await DS.moveTo({ kind: "doc", ids: [id], from: a === "move" ? (me.tags || []).filter(isFolder)[0] || null : null }, +b.dataset.t, a === "also");
        refreshSide(el.closest("#dvSide,#tpane"), id, { full: !!el.closest("#dvSide") });
      },
      afterRender: (TT, scope) => {
        $$("[data-snip]", scope || el).forEach(async (s) => {
          const did = +s.dataset.snip;
          if (DS.SNIP[did] === undefined) { try { const x = await api(`/_lib/api/doc/${did}`); DS.SNIP[did] = (x.content || "").replace(/\s+/g, " ").trim().slice(0, 280); } catch (e) { DS.SNIP[did] = ""; } }
          s.textContent = DS.SNIP[did] || "(no text recognized)";
        });
      },
    });
    T.render(build());
    suggestedFolder(el, id, me, T);
    if (!rel) {
      try { rel = REL[id] = (await api(`/_lib/api/related/${id}`)).related; } catch (e) { rel = REL[id] = []; }
      if (!el.isConnected) return;
      T.render(build());
      suggestedFolder(el, id, me, T);
    }
  }
  function suggestedFolder(el, id, me, T) {
    DS.classOf(id).then((s) => {
      const x = el.querySelector("#fsug"); if (!x) return;
      if (!s) { x.textContent = "no suggestion yet – sort a few similar documents first"; return; }
      if ((me.tags || []).includes(s.id)) { x.innerHTML = `✓ already in <b>${esc(s.name)}</b>, where the most similar documents are`; return; }
      x.innerHTML = `<b>${esc(s.name)}</b> · ${Math.round(s.score * 100)}% sure`;
      const r = T.el("fs") && T.el("fs").querySelector(":scope > .tr");
      if (r && !r.querySelector(".ac")) { const ac = document.createElement("span"); ac.className = "ac"; ac.innerHTML = `<button data-a="move" data-t="${s.id}" title="Move into this folder">Move here</button><button data-a="also" data-t="${s.id}" title="Add, keep the other folders">+ Also</button>`; r.appendChild(ac); }
    });
  }

  // ================================================================ public
  window.DocView = {
    open, close,
    pane: (el, id) => side(el, id, { full: false }),
    openNew: () => {
      const since = iso(new Date(today() - 14 * 864e5));
      const ids = DOCS.filter((d) => (d.added || "") >= since).sort((a, b) => (b.added || "").localeCompare(a.added || "")).map((d) => d.id);
      if (!ids.length) return toast("No new documents in the last 14 days");
      const first = ids.find((i) => !((docById(i) || {}).tags || []).some(isFolder)) || ids[0];
      open(first, { list: ids, listName: "new" });
    },
  };
  window.openDoc = (id) => open(id);
  window.closeDoc = close;
})();
