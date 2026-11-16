/* Doc Sorter – Timeline lanes.
   One lane per sender, case file or space. Documents sit where they are dated; a bar shows a
   running contract (contract-type documents, until their contract deadline if one is known);
   dashed amber lines are deadlines; dashed circles are documents that usually arrive at a regular
   rhythm (monthly, quarterly, yearly) and are expected next – red when one is overdue.
   Click a dot = details below, double-click = open. Drag a dot onto another lane = set sender /
   connect / move, depending on the grouping. The old photo grid stays available as "Grid". */
(function () {
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } return null; };
  const HUES = [212, 186, 150, 38, 262, 0, 330, 96, 24, 200, 276, 168];
  const hueOf = (s) => { let h = 0; for (const ch of String(s)) h = (h * 31 + ch.charCodeAt(0)) >>> 0; return HUES[h % HUES.length]; };
  const typeName = (id) => (byId(M.types, id) || {}).name || "";
  const corrName = (id) => (byId(M.corrs, id) || {}).name || "";
  const typeColor = (id) => (id ? `hsl(${hueOf(typeName(id))} 70% 62%)` : "rgb(255 255 255 / .45)");
  const DAY = 864e5;
  const ms = (s) => Date.parse(s + "T00:00:00Z");
  const isoOf = (v) => new Date(v).toISOString().slice(0, 10);
  const MON = MONTHS.map((m) => m.slice(0, 3));
  const L = {
    mode: store("docsorter.time.mode") || "lanes",
    group: store("docsorter.time.group") || "corr",
    zoom: store("docsorter.time.zoom") || "year",
    pred: store("docsorter.time.pred") !== "0",
    sel: null, all: false,
  };
  const ZOOM = { quarter: { label: "Quarter", back: 80, fwd: 40 }, year: { label: "1 year", back: 300, fwd: 70 }, two: { label: "2 years", back: 640, fwd: 90 }, all: { label: "All" } };
  const GROUPS = { corr: "Sender", case: "Case file", space: "Space" };
  const CONTRACT = /vertrag|contract|police|versicherungsschein|mitgliedschaft|abo\b|abonnement|subscription/i;

  // ---------------------------------------------------------------- lanes
  function lanesOf(docs) {
    const by = new Map(), add = (key, meta, d) => { if (!by.has(key)) by.set(key, Object.assign({ key, docs: [] }, meta)); by.get(key).docs.push(d); };
    if (L.group === "corr") docs.forEach((d) => add("r" + (d.correspondent || 0), { name: corrName(d.correspondent) || "Unknown sender", kind: "corr", id: d.correspondent || null }, d));
    else if (L.group === "case") {
      const comp = DS.components();
      docs.forEach((d) => { const c = comp.get(d.id); if (c) add("c" + c.root, { name: c.docs[0].title, kind: "case", id: c.root }, d); });
    } else {
      const tops = folders().filter((t) => !(t.parent && isFolder(t.parent)));
      docs.forEach((d) => {
        let hit = false;
        tops.forEach((t) => { if ((d.tags || []).some((x) => descIds(t.id).has(x))) { hit = true; add("s" + t.id, { name: t.name, kind: "space", id: t.id }, d); } });
        if (!hit) add("s0", { name: "Not in a folder", kind: "space", id: null }, d);
      });
    }
    return [...by.values()];
  }
  // regular rhythm per lane + document type → the next expected document
  function expectedOf(lane, t0) {
    const out = [], byT = new Map();
    lane.docs.forEach((d) => { if (d.created && d.document_type) { if (!byT.has(d.document_type)) byT.set(d.document_type, []); byT.get(d.document_type).push(d); } });
    byT.forEach((list, tid) => {
      const ds = [...new Set(list.map((d) => d.created))].sort().map(ms);
      if (ds.length < 3) return;
      const gaps = ds.slice(1).map((v, i) => (v - ds[i]) / DAY).slice(-8);
      const med = gaps.slice().sort((a, b) => a - b)[Math.floor(gaps.length / 2)];
      const P = [[26, 35, "monthly"], [84, 98, "quarterly"], [170, 196, "half-yearly"], [345, 385, "yearly"]].find(([lo, hi]) => med >= lo && med <= hi);
      if (!P) return;
      if (gaps.filter((g) => Math.abs(g - med) <= med * 0.22).length < gaps.length * 0.7) return;
      const last = ds[ds.length - 1];
      let nx = last + med * DAY;
      if (nx + med * 0.6 * DAY < t0) return; // the series has ended
      const missing = nx < t0 - 10 * DAY;
      out.push({ d: isoOf(nx), tid, every: P[2], missing, title: typeName(tid) });
      if (!missing) { nx += med * DAY; out.push({ d: isoOf(nx), tid, every: P[2], missing: false, title: typeName(tid) }); }
    });
    return out;
  }

  function range(docs, t0) {
    const Z = ZOOM[L.zoom] || ZOOM.year;
    if (L.zoom !== "all") return [t0 - Z.back * DAY, t0 + Z.fwd * DAY];
    const dates = docs.map((d) => d.created).filter(Boolean).sort();
    const a = dates.length ? ms(dates[0].slice(0, 7) + "-01") - 15 * DAY : t0 - 365 * DAY;
    return [Math.min(a, t0 - 90 * DAY), t0 + 90 * DAY];
  }
  function ticks(a, b) {
    const span = (b - a) / DAY, out = [], d0 = new Date(a);
    let y = d0.getUTCFullYear(), m = d0.getUTCMonth();
    const every = span > 1500 ? 12 : span > 500 ? 3 : 1;
    for (let i = 0; i < 400; i++) {
      const v = Date.UTC(y, m, 1);
      if (v > b) break;
      if (v >= a) {
        const lab = m % every === 0;
        out.push({ v, major: m === 0, label: lab ? (every === 12 ? String(y) : MON[m] + (m === 0 || !out.some((t) => t.label) ? " " + y : "")) : "" });
      }
      m++; if (m > 11) { m = 0; y++; }
    }
    if (span <= 130) for (let v = a; v <= b; v += 7 * DAY) out.push({ v, minor: true, label: "" });
    return out;
  }

  // ---------------------------------------------------------------- render
  const seg = (id, opts, cur) => `<div class="tabs mini" id="${id}">${Object.entries(opts).map(([k, v]) => `<button data-v="${k}" class="${k === cur ? "on" : ""}">${esc(typeof v === "string" ? v : v.label)}</button>`).join("")}</div>`;
  function renderLanes() {
    if (L.mode === "grid") {
      renderMain();
      const bar = $("#main .bar");
      if (bar) {
        bar.insertAdjacentHTML("beforeend", `<div class="lntools">${seg("lnMode", { lanes: "Lanes", grid: "Grid" }, "grid")}</div>`);
        $$("#lnMode button").forEach((b) => (b.onclick = () => { L.mode = b.dataset.v; store("docsorter.time.mode", L.mode); render(); }));
      }
      return;
    }
    const docs = filtered(), t0 = ms(DS.iso(DS.today()));
    const [a, b] = range(docs, t0), span = b - a, X = (v) => ((v - a) / span) * 100;
    const inR = (v) => v >= a && v <= b;
    let lanes = lanesOf(docs).map((ln) => {
      const sorted = ln.docs.slice().sort((p, q) => (p.created || "").localeCompare(q.created || ""));
      const vis = sorted.filter((d) => d.created && inR(ms(d.created)));
      const dense = vis.length > 40;
      const dots = vis.map((d) => ({ d, x: X(ms(d.created)), size: dense ? 8 : d.amount ? 14 : 11 }));
      // deadlines: everything still ahead; past ones only if you saved them (found-in-text ones that passed are noise)
      const marks = ln.docs.filter((d) => d.due && inR(ms(d.due.d)) && (ms(d.due.d) >= t0 - 3 * DAY || !d.due.s)).map((d) => ({ d, x: X(ms(d.due.d)), past: ms(d.due.d) < t0 - 3 * DAY }));
      // contracts: a newer contract in the same lane ends the previous one
      const cons = sorted.filter((d) => d.created && CONTRACT.test(typeName(d.document_type)));
      const bars = cons.map((d, i) => {
        const from = ms(d.created), nxt = cons[i + 1] ? ms(cons[i + 1].created) : null, end = d.due && d.due.k === "contract" ? ms(d.due.d) : null;
        let to = end || nxt || b; if (nxt && to > nxt) to = nxt;
        if (to <= a || from > b || to - from < DAY) return null;
        return { d, x: X(Math.max(from, a)), w: X(Math.min(to, b)) - X(Math.max(from, a)), open: !end && !nxt, cutL: from < a, cutR: (!end && !nxt) || to > b, until: to < b ? isoOf(to) : null };
      }).filter(Boolean).slice(-3);
      const exp = L.pred ? expectedOf(ln, t0).filter((e) => inR(ms(e.d))).map((e) => Object.assign(e, { x: X(ms(e.d)) })) : [];
      const xs = dots.map((p) => p.x);
      const up = ln.docs.filter((d) => d.due && ms(d.due.d) >= t0).sort((p, q) => p.due.d.localeCompare(q.due.d))[0];
      const last = sorted.length ? sorted[sorted.length - 1].created || "" : "";
      return Object.assign(ln, { sorted, dots, marks, bars, exp, up, last, lineX: xs.length ? Math.min(...xs) : 0, lineW: xs.length > 1 ? Math.max(...xs) - Math.min(...xs) : 0 });
    }).filter((ln) => ln.dots.length || ln.marks.length || ln.exp.length || ln.bars.length);
    lanes.sort((p, q) => (!!q.up - !!p.up) || (p.up && q.up ? p.up.due.d.localeCompare(q.up.due.d) : 0) || q.last.localeCompare(p.last));
    const total = lanes.length, shown = L.all ? lanes : lanes.slice(0, 25);
    const missing = lanes.reduce((n, ln) => n + ln.exp.filter((e) => e.missing).length, 0);
    const tk = ticks(a, b), tX = X(t0);
    const selDoc = L.sel && DS.docById(L.sel);

    const laneHtml = (ln) => `<div class="ln" data-lane="${esc(ln.key)}">
      <div class="lnl"><b title="${esc(ln.name)}">${esc(ln.name)}</b><small>${ln.docs.length} document${ln.docs.length === 1 ? "" : "s"}${ln.kind === "case" ? "" : ln.sorted.length ? " · since " + esc((ln.sorted[0].created || "").slice(0, 4)) : ""}</small>
        ${ln.up ? `<span class="lnup${ln.up.due.s ? " sug" : ""}">⏰ ${esc(DS.fmt(ln.up.due.d).slice(0, 6))} · ${esc(DS.rel(ln.up.due.d))}</span>` : ""}</div>
      <div class="lnt">
        ${ln.bars.map((br) => `<span class="lnbar${br.open ? " open" : ""}" style="left:${br.x}%;width:${Math.max(br.w, 0.4)}%;border-radius:${br.cutL ? 0 : 999}px ${br.cutR ? 0 : 999}px ${br.cutR ? 0 : 999}px ${br.cutL ? 0 : 999}px" title="${esc(br.d.title)}${br.open ? " · running" : br.until ? " · until " + DS.fmt(br.until) : ""}">${esc(br.d.title)}</span>`).join("")}
        ${ln.lineW ? `<span class="lnline" style="left:${ln.lineX}%;width:${ln.lineW}%"></span>` : ""}
        ${ln.marks.map((mk) => `<span class="lnmk${mk.past ? " past" : ""}${mk.d.due.s ? " sug" : ""}" style="left:${mk.x}%" title="${esc((DS.KIND[mk.d.due.k] || "deadline") + " · " + DS.fmt(mk.d.due.d) + " · " + mk.d.title + (mk.d.due.s ? " · found in the text, not saved yet" : ""))}"><i>${esc(DS.fmt(mk.d.due.d).slice(0, 6))}</i></span>`).join("")}
        ${ln.exp.map((e) => `<span class="lnexp${e.missing ? " miss" : ""}" style="left:${e.x}%" title="${esc((e.missing ? "Expected but not arrived: " : "Expected: ") + e.title + " ≈ " + DS.fmt(e.d) + " (" + e.every + ")")}"></span>`).join("")}
        ${ln.dots.map((p) => `<button class="lndot${L.sel === p.d.id ? " on" : ""}" data-id="${p.d.id}" draggable="true" style="left:${p.x}%;--s:${p.size}px;--c:${typeColor(p.d.document_type)}" aria-label="${esc((p.d.title || "") + " · " + DS.fmt(p.d.created))}" title="${esc((p.d.title || "") + " · " + DS.fmt(p.d.created) + (typeName(p.d.document_type) ? " · " + typeName(p.d.document_type) : "") + (p.d.amount ? " · " + DS.money(p.d.amount) : ""))}"><i></i></button>`).join("")}
      </div></div>`;

    $("#main").innerHTML = `<div class="bar"><h2>Timeline</h2><span class="cnt">${total} lane${total === 1 ? "" : "s"} · ${docs.length} documents${missing ? ` · <span class="lnmiss">${missing} expected document${missing > 1 ? "s" : ""} missing</span>` : ""}</span>
        <div class="lntools">${seg("lnMode", { lanes: "Lanes", grid: "Grid" }, "lanes")}${seg("lnGroup", GROUPS, L.group)}${seg("lnZoom", ZOOM, L.zoom)}
        <button class="hbtn lnpred${L.pred ? " on" : ""}" id="lnPred" aria-pressed="${L.pred}" title="Show documents that usually arrive at a regular rhythm">◌ Expected</button></div></div>
      <div class="lnlegend"><span><i class="lg lg-bar"></i>contract running</span><span><i class="lg lg-mk"></i>deadline</span><span><i class="lg lg-exp"></i>expected</span><span><i class="lg lg-miss"></i>expected, not arrived</span><span><i class="lg lg-big"></i>bigger dot = has an amount</span><span class="mut">dot colour = document type · drag a dot onto another lane to ${L.group === "corr" ? "change its sender" : L.group === "case" ? "connect it" : "move it"}</span></div>
      ${total ? `<div class="lnwrap"><div class="lngrid">
        <div class="ln lnaxis"><div class="lnl"><small>${esc(DS.fmt(isoOf(a)).slice(3))} – ${esc(DS.fmt(isoOf(b)).slice(3))}</small></div><div class="lnt">${tk.filter((t) => t.label && X(t.v) < 95).map((t) => `<span class="lntk${t.major ? " major" : ""}" style="left:${X(t.v)}%">${esc(t.label)}</span>`).join("")}<span class="lntoday" style="left:${tX}%">Today</span></div></div>
        <div class="lnov" aria-hidden="true">${tk.map((t) => `<i class="${t.major ? "major" : t.minor ? "minor" : ""}" style="left:${X(t.v)}%"></i>`).join("")}<b style="left:${tX}%"></b></div>
        ${shown.map(laneHtml).join("")}</div>
        ${total > 25 ? `<button class="btn sec lnall" id="lnAll">${L.all ? "Show fewer lanes" : `Show all ${total} lanes`}</button>` : ""}</div>`
        : `<div class="empty">${L.group === "case" ? "No case files yet – connect documents (drag one onto another in the Tree) and they show up here as lanes." : "Nothing in this time range."}</div>`}
      <div id="lnSel">${selDoc ? selHtml(selDoc, lanes) : `<p class="thint" style="margin:14px 4px">Click a dot to see the document here · double-click opens it.</p>`}</div>`;
    $("#years").innerHTML = "";
    bind(lanes);
  }

  function selHtml(d, lanes) {
    const ln = lanes.find((x) => x.docs.includes(d));
    const seq = ln ? ln.sorted : [];
    const i = seq.indexOf(d), prev = i > 0 ? seq[i - 1] : null, next = i >= 0 && i < seq.length - 1 ? seq[i + 1] : null;
    const one = (x) => x ? `<button class="lnj" data-jump="${x.id}">${esc(x.title)} <span class="mut">${esc(DS.fmt(x.created))}</span></button>` : `<span class="mut">—</span>`;
    return `<div class="lnsel">
      <img class="th" src="/_lib/thumb/${d.id}" alt="">
      <div class="lnsm"><small class="mut">${ln ? esc(GROUPS[L.group] + " · " + ln.name) : ""}</small><h3>${esc(d.title)}</h3>
        <div class="mut">${[DS.fmt(d.created), typeName(d.document_type), corrName(d.correspondent)].filter(Boolean).map(esc).join(" · ")}</div>
        <div class="irow">${d.amount ? DS.amtHtml(d.amount) : ""}${d.due ? DS.dueHtml(d.due) : ""}</div></div>
      <dl class="lnnb"><div><dt>Before, same lane</dt><dd>${one(prev)}</dd></div><div><dt>After, same lane</dt><dd>${one(next)}</dd></div></dl>
      <div class="lnsa"><button class="btn" id="lnOpen">Open document</button></div></div>`;
  }

  function bind(lanes) {
    const main = $("#main");
    const segBind = (id, fn) => $$("#" + id + " button", main).forEach((b) => (b.onclick = () => fn(b.dataset.v)));
    segBind("lnMode", (v) => { L.mode = v; store("docsorter.time.mode", v); render(); });
    segBind("lnGroup", (v) => { L.group = v; L.all = false; store("docsorter.time.group", v); renderLanes(); segThumb(); });
    segBind("lnZoom", (v) => { L.zoom = v; store("docsorter.time.zoom", v); renderLanes(); segThumb(); });
    $("#lnPred").onclick = () => { L.pred = !L.pred; store("docsorter.time.pred", L.pred ? "1" : "0"); renderLanes(); segThumb(); };
    if ($("#lnAll")) $("#lnAll").onclick = () => { L.all = !L.all; renderLanes(); segThumb(); };
    const pick = (id) => { L.sel = id; $$(".lndot.on", main).forEach((x) => x.classList.remove("on")); $$(`.lndot[data-id="${id}"]`, main).forEach((x) => x.classList.add("on")); const d = DS.docById(id); $("#lnSel").innerHTML = d ? selHtml(d, lanes) : ""; bindSel(); };
    const bindSel = () => {
      if ($("#lnOpen")) $("#lnOpen").onclick = () => openDoc(L.sel);
      $$("[data-jump]", main).forEach((b) => (b.onclick = () => { const id = +b.dataset.jump; pick(id); const el = $(`.lndot[data-id="${id}"]`, main); if (el) el.focus({ preventScroll: true }); }));
    };
    bindSel();
    let lastT = 0, lastId = null;
    $$(".lndot", main).forEach((el) => {
      const id = +el.dataset.id;
      el.onclick = () => { const now = Date.now(); if (lastId === id && now - lastT < 380) return openDoc(id); lastT = now; lastId = id; pick(id); };
      el.onkeydown = (e) => { if (e.key === "Enter" && e.shiftKey) { e.preventDefault(); openDoc(id); } };
      el.ondragstart = (e) => { const lane = el.closest(".ln").dataset.lane; DS.drag = { kind: "doc", ids: [id], from: lane[0] === "s" && +lane.slice(1) ? +lane.slice(1) : null, lane, label: (DS.docById(id) || {}).title }; e.dataTransfer.effectAllowed = "copyMove"; try { e.dataTransfer.setData("text/plain", String(id)); } catch (err) { /* ignore */ } };
      el.ondragend = () => setTimeout(() => (DS.drag = null), 0);
    });
    $$(".ln[data-lane]", main).forEach((row) => {
      const key = row.dataset.lane;
      row.ondragover = (e) => { const d = DS.drag; if (!d || d.kind !== "doc" || d.lane === key) return; if (key === "s0" && !d.from) return; e.preventDefault(); row.classList.add("drop"); };
      row.ondragleave = (e) => { if (!row.contains(e.relatedTarget)) row.classList.remove("drop"); };
      row.ondrop = (e) => {
        e.preventDefault(); row.classList.remove("drop"); const d = DS.drag; DS.drag = null; if (!d) return;
        const ln = lanes.find((x) => x.key === key); if (!ln) return;
        if (ln.kind === "corr") return setCorr(d.ids, ln.id, ln.name);
        if (ln.kind === "case") return DS.connect({ kind: "doc", ids: d.ids, from: null }, DS.docById(ln.id), null, true);
        if (ln.id) return DS.moveTo({ kind: "doc", ids: d.ids, from: d.from }, ln.id, e.ctrlKey || e.altKey);
        if (d.from) return DS.takeOut(d);
      };
    });
  }
  function setCorr(ids, cid, name) {
    const by = {};
    ids.forEach((i) => { const d = DS.docById(i); const old = d ? d.correspondent || null : null; (by[old] = by[old] || []).push(i); });
    const inv = Object.entries(by).map(([old, list]) => ["/_lib/api/bulk", { ids: list, correspondent: old === "null" ? null : +old }]);
    const nm = ids.length > 1 ? ids.length + " documents" : "“" + ((DS.docById(ids[0]) || {}).title || "") + "”";
    return DS.run(cid ? `✉ ${nm} → sender “${name}”` : `✉ ${nm}: sender removed`, [["/_lib/api/bulk", { ids, correspondent: cid }]], inv);
  }

  window.renderLanes = renderLanes;
  window.Lanes = { L };
})();
