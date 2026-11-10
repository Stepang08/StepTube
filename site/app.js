/* StepTube front-end. Vanilla JS, no build step.
   All personal state (likes, history, watch later, notes, settings) lives in
   this browser's localStorage. Export/import in Settings. */
(() => {
  "use strict";

  const PAGE = 20;              // finite pages; "Show more" is an explicit choice
  const NEW_DAYS = 21;          // the "New" view only looks this far back
  const KEY = "steptube:v1";

  // ---------------------------------------------------------------- state
  const defaults = () => ({
    later: [], liked: [], history: [], watched: {}, notes: {}, muted: [], notInterested: [], recs: null, pos: {},
    settings: { thumbs: "muted", clickbait: false, theme: "auto", budget: 60, sort: "new", smart: true, searchSort: "rel",
                len: "any", lang: "all", subs: {}, rate: 1 },
    lastVisit: null, budget: { day: "", sec: 0 },
  });
  let S = defaults();
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) { const o = JSON.parse(raw); S = { ...S, ...o, settings: { ...S.settings, ...(o.settings || {}) } }; }
  } catch (_) { /* private mode etc. */ }
  let dirty = false;  // something changed since the last sync
  const rawSave = () => { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (_) {} };
  const save = () => { dirty = true; rawSave(); };

  // ---------------------------------------------------------------- sync (private Gist)
  const SYNC_KEY = "steptube:sync";
  let SY = { token: "", gistId: "", base: null, last: 0, err: "" };
  try { SY = { ...SY, ...(JSON.parse(localStorage.getItem(SYNC_KEY)) || {}) }; } catch (_) {}
  const saveSY = () => { try { localStorage.setItem(SYNC_KEY, JSON.stringify(SY)); } catch (_) {} };
  const syncData = () => JSON.parse(JSON.stringify({ later: S.later, liked: S.liked, muted: S.muted, notInterested: S.notInterested,
    watched: S.watched, notes: S.notes, pos: S.pos, settings: S.settings, history: S.history, budget: S.budget }));
  let syncing = false;
  async function syncNow(force = false) {
    if (!SY.token || syncing || !window.StepSync) return;
    syncing = true;
    try {
      if (!SY.gistId) { SY.gistId = await StepSync.findOrCreate(SY.token); saveSY(); }
      const remote = await StepSync.pull(SY.token, SY.gistId);
      const local = syncData(); dirty = false;      // no awaits between this snapshot and applying the merge
      // First sync on this device: treat untouched default settings as "unchanged", so the Gist's win.
      const merged = StepSync.merge3(SY.base || { settings: defaults().settings }, local, remote || {});
      const changedHere = JSON.stringify(merged) !== JSON.stringify(local);
      Object.assign(S, merged); S.settings = { ...defaults().settings, ...merged.settings };
      rawSave();
      if (!remote || force || JSON.stringify(merged) !== JSON.stringify(remote)) await StepSync.push(SY.token, SY.gistId, merged);
      SY.base = merged; SY.last = Date.now(); SY.err = ""; saveSY();
      if (changedHere && !location.hash.startsWith("#/v/")) { applySettings(); render(true); }
    } catch (e) {
      SY.err = e.message === "not-found" ? "The sync Gist was deleted. Disconnect and connect again." : String(e.message || e);
      if (e.message === "not-found") SY.gistId = "";
      saveSY();
    } finally { syncing = false; }
  }
  setInterval(() => { if (dirty && document.visibilityState === "visible") syncNow(); }, 60000);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden" ? dirty : true) syncNow(); });

  // Remember the previous visit so "New" can mean "new since last time".
  const prevVisit = S.lastVisit;
  S.lastVisit = new Date().toISOString(); save();

  let DB = { topics: [], channels: [], videos: [] };
  let byId = new Map(), chByKey = new Map();
  let COURSES = [], courseByKey = new Map(), courseOf = new Map();
  let shown = PAGE, sel = -1, lastRoute = "", player = null, tick = null;

  const $ = (s, el = document) => el.querySelector(s);
  const main = $("#main");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const today = () => new Date().toISOString().slice(0, 10);

  function applySettings() {
    document.body.dataset.thumbs = S.settings.thumbs;
    const t = S.settings.theme;
    if (t === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.dataset.theme = t;
  }

  // ---------------------------------------------------------------- data
  async function load() {
    try {
      const r = await fetch("data/videos.json", { cache: "no-cache" });
      DB = await r.json();
    } catch (e) {
      main.innerHTML = `<div class="note">Couldn't load <code>data/videos.json</code>. Run <code>python scripts/build.py</code> or wait for the nightly GitHub Action.</div>`;
      return;
    }
    byId = new Map(DB.videos.map((v) => [v.id, v]));
    chByKey = new Map(DB.channels.map((c) => [c.key, c]));
    window.StepSearch?.build(DB, chName);
    // Channels' own YouTube playlists (optional file), then series guessed from numbered titles.
    let PL = { playlists: [], extra: {} };
    try { const r = await fetch("data/playlists.json", { cache: "no-cache" }); if (r.ok) PL = await r.json(); } catch (_) {}
    for (const [id, x] of Object.entries(PL.extra || {})) if (!byId.has(id)) byId.set(id, { id, t: x.t, chn: x.chn, s: x.s, ch: "", p: "", xtra: 1 });
    const lists = (PL.playlists || []).map((p) => ({ key: `pl:${p.id}`, title: p.t, ch: p.ch, chn: chByKey.get(p.ch)?.name || p.ch,
      ids: p.ids.filter((id) => byId.has(id)), src: "playlist", pid: p.id, total: p.total || 0 })).filter((c) => c.ids.length);
    const inList = new Set(lists.flatMap((c) => c.ids));
    const guessed = (window.StepCourses ? StepCourses.build(DB.videos, chName) : [])
      .filter((c) => c.ids.filter((id) => inList.has(id)).length < 0.6 * c.ids.length).map((c) => ({ ...c, src: "series" }));
    COURSES = [...lists, ...guessed];
    courseByKey = new Map(COURSES.map((c) => [c.key, c]));
    courseOf = new Map();
    for (const c of COURSES) c.ids.forEach((id, i) => { if (!courseOf.has(id)) courseOf.set(id, [c, i]); });
    $("#tabs").innerHTML =
      `<a href="#/" data-tab="">New</a><a href="#/foryou" data-tab="foryou">For you</a>` +
      DB.topics.filter((t) => DB.videos.some((v) => v.tp === t.key)).map((t) => `<a href="#/t/${t.key}" data-tab="${t.key}">${esc(t.name)}</a>`).join("") +
      `<a href="#/playlists" data-tab="playlists">Playlists</a><a href="#/classics" data-tab="classics">Classics</a><a href="#/channels" data-tab="channels">Channels</a>`;
    const when = DB.generated ? new Date(DB.generated) : null;
    $("#meta").textContent = `${DB.videos.length} videos · ${DB.channels.length} channels` +
      (when ? ` · updated ${when.toLocaleDateString(undefined, { month: "short", day: "numeric" })}` : "");
    route();
    syncNow();
  }

  const chName = (v) => chByKey.get(v.ch)?.name || v.chn || v.ch || "";
  const visible = (v) => (S.settings.clickbait || !v.cb) && !S.muted.includes(v.ch);

  function fmtDate(p) {
    if (!p) return "";
    const d = new Date(p + "T00:00:00Z"), days = Math.floor((Date.now() - d) / 864e5);
    if (days < 1) return "today";
    if (days < 2) return "yesterday";
    if (days < 7) return `${days} days ago`;
    return d.toLocaleDateString(undefined, { year: d.getUTCFullYear() === new Date().getUTCFullYear() ? undefined : "numeric", month: "short", day: "numeric" });
  }

  function searchMode(q) {
    if (S.settings.smart === false) return "Keyword search (smart search is off in Settings).";
    if (StepSearch.isReady(q)) return "Smart search: by meaning and keywords, ranked like YouTube (relevance, views, your taste).";
    const st = StepSearch.status;
    if (st.mode === "loading") return `Loading the smart-search model… ${st.detail} (one-time download, about 120 MB).`;
    if (st.detail) return "Keyword search (smart search unavailable right now).";
    return "Keyword results · refining by meaning…";
  }
  function tokenizeFix(q, corrected) {
    let out = q.toLowerCase();
    for (const [from, to] of corrected) out = out.replace(from, to);
    return out;
  }

  const langOf = (v) => v.lang || (/[\u0530-\u058f]/.test(v.t) ? "hy" : /[\u0400-\u04ff]/.test(v.t) ? "ru" : "en");
  function fmtDur(sec) {
    sec = Math.round(sec); const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s2 = sec % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(s2).padStart(2, "0")}` : `${m}:${String(s2).padStart(2, "0")}`;
  }
  function passFilters(v) {
    const len = S.settings.len, lang = S.settings.lang;
    if (len && len !== "any") {
      if (!v.s) return false;
      const m = v.s / 60;
      if (len === "short" ? m > 15 : len === "mid" ? (m <= 15 || m > 60) : m <= 60) return false;
    }
    return !lang || lang === "all" || langOf(v) === lang;
  }
  function filterControls() {
    const o = (cur, k, n) => `<option value="${k}"${cur === k ? " selected" : ""}>${n}</option>`;
    const len = S.settings.len || "any", lang = S.settings.lang || "all";
    return `<label>Length <select id="flen">${o(len, "any", "Any")}${o(len, "short", "Up to 15 min")}${o(len, "mid", "15–60 min")}${o(len, "long", "Over 1 hour")}</select></label>
      <label>Language <select id="flang">${o(lang, "all", "All")}${o(lang, "en", "English")}${o(lang, "ru", "Русский")}${o(lang, "hy", "Հայերեն")}</select></label>`;
  }

  // Most viewed first; videos without a count go last; ties newest first.
  const byViews = (a, b) => (b.vc || 0) - (a.vc || 0) || (b.p || "").localeCompare(a.p || "");
  function fmtViews(n) {
    if (n >= 1e9) return `${+(n / 1e9).toFixed(1)}B views`;
    if (n >= 1e6) return `${+(n / 1e6).toFixed(1)}M views`;
    if (n >= 1e3) return `${n >= 1e5 ? Math.round(n / 1e3) : +(n / 1e3).toFixed(1)}K views`;
    return `${n} view${n === 1 ? "" : "s"}`;
  }

  function sortList(list, mode) {
    const l = list.slice();
    if (mode === "views") l.sort(byViews);
    else if (mode === "old") l.sort((a, b) => (a.p || "").localeCompare(b.p || ""));
    else if (mode === "shuffle") {
      // Stable per-day shuffle: calm discovery without a slot machine on refresh.
      let seed = [...today()].reduce((a, c) => a * 31 + c.charCodeAt(0), 7) >>> 0;
      const rnd = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 2 ** 32);
      for (let i = l.length - 1; i > 0; i--) { const j = Math.floor(rnd() * (i + 1)); [l[i], l[j]] = [l[j], l[i]]; }
    } else l.sort((a, b) => (b.p || "").localeCompare(a.p || ""));
    return l;
  }

  // ---------------------------------------------------------------- render helpers
  function card(v, i) {
    const later = S.later.includes(v.id), liked = S.liked.includes(v.id), seen = !!S.watched[v.id];
    return `<li class="card${seen ? " seen" : ""}" data-id="${v.id}" data-i="${i}">
      <a class="thumb" href="#/v/${v.id}" tabindex="-1" aria-hidden="true"><img loading="lazy" decoding="async" alt="" onerror="this.style.visibility='hidden'" src="https://i.ytimg.com/vi/${v.id}/mqdefault.jpg"></a>
      <div class="info">
        <h3 class="title"><a href="#/v/${v.id}">${esc(v.t)}</a></h3>
        <div class="byline">${chLink(v)}${v.p ? `<span class="dot">·</span>${fmtDate(v.p)}` : ""}${v.s ? `<span class="dot">·</span>${fmtDur(v.s)}` : ""}${showViews && v.vc ? `<span class="dot">·</span>${fmtViews(v.vc)}` : ""}${S.pos[v.id] && !seen ? `<span class="tag">resume at ${fmtDur(S.pos[v.id].t)}</span>` : ""}${v.ev ? `<span class="tag">classic</span>` : ""}${S.notes[v.id] ? `<span class="tag">note</span>` : ""}</div>
        ${v.d ? `<p class="desc">${esc(v.d)}</p>` : ""}
      </div>
      <div class="acts">
        <button class="icon" data-act="later" aria-pressed="${later}" title="Watch later (w)" aria-label="Watch later">${later ? "◆" : "◇"}</button>
        <button class="icon" data-act="like" aria-pressed="${liked}" title="Like (l)" aria-label="Like">${liked ? "♥" : "♡"}</button>
        <button class="icon" data-act="seen" aria-pressed="${seen}" title="Mark watched (x)" aria-label="Mark watched">${seen ? "✓" : "○"}</button>
        ${nopeMode ? `<button class="icon" data-act="nope" title="Not interested (n)" aria-label="Not interested">✕</button>` : ""}
      </div>
    </li>`;
  }

  let nopeMode = false;  // the For you page shows a "Not interested" button
  let showViews = false; // view counts only appear while a list is sorted by views
  function chLink(v) {
    if (v.ch) return `<a href="#/c/${encodeURIComponent(v.ch)}">${esc(chName(v))}</a>`;
    if (v.ccid) return `<a href="https://www.youtube.com/channel/${esc(v.ccid)}" target="_blank" rel="noopener">${esc(chName(v))} ↗</a>`;
    return esc(chName(v));
  }

  function listView({ title, sub = "", list, empty, note = "", pre = null, sortable = true, endText, tools = "", byViews: viewsSorted = false, filterable = true }) {
    showViews = viewsSorted || (sortable && S.settings.sort === "views");
    const sortSel = tools || (sortable ? `<label class="tools">Sort
      <select id="sort">${[["new", "Newest"], ["old", "Oldest"], ["views", "Most viewed"], ["shuffle", "Today's shuffle"]]
        .map(([k, n]) => `<option value="${k}"${S.settings.sort === k ? " selected" : ""}>${n}</option>`).join("")}</select></label>` : "");
    const total = list.length;
    if (filterable) list = list.filter(passFilters);
    const filtered = filterable && ((S.settings.len || "any") !== "any" || (S.settings.lang || "all") !== "all");
    const items = sortable ? sortList(list, S.settings.sort) : list;
    const page = items.slice(0, shown);
    const controls = sortSel || filterable ? `<div class="tools">${sortSel}${filterable ? filterControls() : ""}</div>` : "";
    main.innerHTML = `
      <div class="head"><div><h1>${esc(title)}</h1>${sub ? `<div class="sub">${sub}</div>` : ""}</div>${controls}</div>
      ${note ? `<div class="note">${note}</div>` : ""}
      ${pre ? pre() : ""}
      ${filtered ? `<div class="filt">Filters on: showing ${items.length} of ${total}. <button class="link" id="fclear">Clear filters</button></div>` : ""}
      ${items.length ? `<ul class="list">${page.map(card).join("")}</ul>` : `<p class="end">${filtered && total ? "Nothing matches these filters." : empty}</p>`}
      ${items.length > shown ? `<div class="more"><button class="btn" id="more">Show ${Math.min(PAGE, items.length - shown)} more <span style="color:var(--text-3)">(${items.length - shown} left)</span></button></div>`
        : items.length ? `<p class="end">${endText || "That's everything here."}</p>` : ""}`;
    $("#sort")?.addEventListener("change", (e) => { S.settings.sort = e.target.value; save(); shown = PAGE; render(); });
    $("#flen")?.addEventListener("change", (e) => { S.settings.len = e.target.value; save(); shown = PAGE; render(true); });
    $("#flang")?.addEventListener("change", (e) => { S.settings.lang = e.target.value; save(); shown = PAGE; render(true); });
    $("#fclear")?.addEventListener("click", () => { S.settings.len = "any"; S.settings.lang = "all"; save(); shown = PAGE; render(true); });
    $("#more")?.addEventListener("click", () => { shown += PAGE; render(true); });
    sel = Math.min(sel, main.querySelectorAll(".card").length - 1);
    markSel();
  }

  function budgetNote() {
    const b = S.settings.budget, used = S.budget.day === today() ? Math.round(S.budget.sec / 60) : 0;
    if (!b) return "";
    if (used >= b) return `You've watched ${used} minutes today, past your ${b}-minute budget. Maybe that's enough for today.`;
    return "";
  }

  // ---------------------------------------------------------------- views
  const views = {
    home() {
      const cutoff = new Date(Date.now() - NEW_DAYS * 864e5).toISOString().slice(0, 10);
      const list = DB.videos.filter((v) => visible(v) && !v.ev && v.ch && v.tp !== "review" && v.p >= cutoff && !S.watched[v.id]);
      const fresh = prevVisit ? list.filter((v) => v.p >= prevVisit.slice(0, 10)).length : 0;
      const sub = prevVisit
        ? (fresh ? `${fresh} posted since your last visit.` : "Nothing new since your last visit.")
        : "Unwatched videos from your channels, last three weeks.";
      const cont = Object.entries(S.pos).filter(([id, p]) => byId.has(id) && !S.watched[id] && p.t > 30)
        .sort((x, y) => y[1].at - x[1].at).slice(0, 3);
      const pre = cont.length ? () => `<h2 class="sec">Continue watching</h2><ul class="list">${cont.map(([id], i) => card(byId.get(id), i)).join("")}</ul><h2 class="sec">New</h2>` : null;
      listView({ title: "New", sub, list, sortable: true, note: budgetNote(), pre,
        empty: "You're all caught up. Go outside, or check the <a href='#/classics'>classics</a>.",
        endText: "You're caught up." });
    },
    topic(key) {
      const t = DB.topics.find((x) => x.key === key);
      if (!t) return views.notFound();
      let list = DB.videos.filter((v) => v.tp === key && visible(v));
      if (key === "claude") {
        const ccSort = S.settings.ccSort === "views" ? "views" : "new";
        const tools = `<label>Sort <select id="ccsort">
          <option value="new"${ccSort === "new" ? " selected" : ""}>Newest</option>
          <option value="views"${ccSort === "views" ? " selected" : ""}>Most viewed</option></select></label>`;
        listView({ title: t.name, tools, sortable: false, byViews: ccSort === "views",
          sub: `${list.length} videos about Claude Code, from any channel · found by YouTube search every hour. Shorts, live streams and clickbait are filtered out.`,
          list: sortList(list, ccSort), empty: "Nothing yet. The hourly search fills this in." });
        $("#ccsort").addEventListener("change", (e) => { S.settings.ccSort = e.target.value; save(); shown = PAGE; render(true); });
        return;
      }
      let pre = null;
      if (t.subs) {
        const cur = (S.settings.subs || {})[key] || "all";
        const count = (k) => list.filter((v) => chByKey.get(v.ch)?.sub === k).length;
        const all = list;
        if (cur !== "all") list = list.filter((v) => chByKey.get(v.ch)?.sub === cur);
        pre = () => `<div class="chips">${[["all", `All (${all.length})`], ...Object.entries(t.subs).map(([k, n]) => [k, `${n} (${all.filter((v) => chByKey.get(v.ch)?.sub === k).length})`])]
          .map(([k, n]) => `<button class="btn" data-sub="${k}" aria-pressed="${cur === k}">${esc(n)}</button>`).join("")}</div>`;
      }
      const note = key === "review"
        ? "These channels are on probation and don't appear on the New page. Watch a few, then move each to a real topic in <code>channels.yml</code> or delete its line. <a href='#/channels'>Channel list</a>."
        : budgetNote();
      listView({ title: t.name, sub: `${list.length} videos`, list, empty: "Nothing here yet.", note, pre });
      main.querySelectorAll("[data-sub]").forEach((b) => b.addEventListener("click", () => {
        S.settings.subs = { ...(S.settings.subs || {}), [key]: b.dataset.sub }; save(); shown = PAGE; render(true);
      }));
    },
    courses() { return views.playlists(); },
    playlists() {
      const info = (c) => {
        const done = c.ids.filter((id) => S.watched[id]).length;
        const last = Math.max(0, ...c.ids.map((id) => S.watched[id] || (S.pos[id] && S.pos[id].at) || 0));
        return { c, done, last, next: c.ids.find((id) => !S.watched[id]) };
      };
      const lang = S.settings.lang || "all";
      let rows = COURSES.filter((c) => !S.muted.includes(c.ch) && (lang === "all" || langOf(byId.get(c.ids[0])) === lang)).map(info);
      rows.sort((a, b) => (b.last > 0 && b.done < b.c.ids.length) - (a.last > 0 && a.done < a.c.ids.length) || b.last - a.last ||
        (a.c.src === "playlist" ? 0 : 1) - (b.c.src === "playlist" ? 0 : 1) || b.c.ids.length - a.c.ids.length);
      const o = (k, n) => `<option value="${k}"${lang === k ? " selected" : ""}>${n}</option>`;
      const started = rows.filter((r) => r.last > 0 && r.done < r.c.ids.length).length;
      const nPl = rows.filter((r) => r.c.src === "playlist").length;
      main.innerHTML = `<div class="head"><div><h1>Playlists</h1><div class="sub">${nPl} course-like playlists from your channels on YouTube, plus ${rows.length - nPl} series found by numbered titles${started ? ` · ${started} in progress` : ""}. Videos play in the creator's order, and your progress is tracked.</div></div>
        <div class="tools"><label>Language <select id="flang">${o("all", "All")}${o("en", "English")}${o("ru", "Русский")}${o("hy", "Հայերեն")}</select></label></div></div>
        ${rows.length ? `<ul class="list">${rows.slice(0, shown).map(({ c, done, next, last }) => `<li class="course">
          <div class="info"><h3 class="title"><a href="#/course/${encodeURIComponent(c.key)}">${esc(c.title)}</a></h3>
            <div class="byline">${esc(c.chn)}<span class="dot">·</span>${c.ids.length}${c.total > c.ids.length ? ` of ${c.total}` : ""} videos${done ? `<span class="dot">·</span>${done} watched` : ""}<span class="tag">${c.src === "playlist" ? "playlist" : "series"}</span></div>
            <div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="${c.ids.length}" aria-valuenow="${done}"><i style="width:${Math.round((100 * done) / c.ids.length)}%"></i></div></div>
          <div class="acts">${next ? `<a class="btn" href="#/v/${next}">${done || last ? "Continue" : "Start"}</a>` : `<span class="tag">finished</span>`}</div></li>`).join("")}</ul>` : `<p class="end">No playlists match.</p>`}
        ${rows.length > shown ? `<div class="more"><button class="btn" id="more">Show ${Math.min(PAGE, rows.length - shown)} more <span style="color:var(--text-3)">(${rows.length - shown} left)</span></button></div>` : rows.length ? `<p class="end">That's every playlist.</p>` : ""}`;
      $("#flang").addEventListener("change", (e) => { S.settings.lang = e.target.value; save(); shown = PAGE; render(true); });
      $("#more")?.addEventListener("click", () => { shown += PAGE; render(true); });
    },
    course(key) {
      const c = courseByKey.get(key);
      if (!c) return views.notFound();
      const done = c.ids.filter((id) => S.watched[id]).length, next = c.ids.find((id) => !S.watched[id]);
      listView({ title: c.title, sortable: false, filterable: false, list: c.ids.map((id) => byId.get(id)),
        sub: `${esc(c.chn)} · ${c.ids.length} videos in order${c.total > c.ids.length ? ` (first ${c.ids.length} of ${c.total})` : ""} · ${done} watched${c.pid ? ` · <a href="https://www.youtube.com/playlist?list=${esc(c.pid)}" target="_blank" rel="noopener">playlist on YouTube ↗</a>` : ""}${next ? ` · <a href="#/v/${next}">${done ? "Continue" : "Start"}: ${esc(byId.get(next).t)}</a>` : " · finished"}`,
        pre: () => `<div class="progress wide"><i style="width:${Math.round((100 * done) / c.ids.length)}%"></i></div>`,
        empty: "Empty course.", endText: "End of the course." });
    },
    async foryou() {
      const day = today();
      const head = (sub) => `<div class="head"><div><h1>For you</h1><div class="sub">${sub}</div></div></div>`;
      if (!S.recs || S.recs.day !== day || S.recs.v !== 1) {
        main.innerHTML = head("Picking today's videos…");
        const r = await StepSearch.recommend(S, { visible: (v) => visible(v) && v.tp !== "review" });
        S.recs = { v: 1, day, smart: r.smart, rows: r.rows.map((x) => ({ label: x.label, ids: x.ids })) };
        save();
        if (lastRoute !== "/foryou") return;  // user navigated away meanwhile
      }
      const rows = S.recs.rows.map((r) => ({ ...r, ids: r.ids.filter((id) => byId.has(id) && !S.notInterested.includes(id)) }))
        .filter((r) => r.ids.length);
      nopeMode = true; showViews = false;
      let i = 0;
      const body = rows.map((r) => `<h2 class="sec">${esc(r.label)}</h2><ul class="list">${r.ids.map((id) => card(byId.get(id), i++)).join("")}</ul>`).join("");
      main.innerHTML = head(
        "Picked once a day from what you've watched and liked, the way YouTube's home feed works, but only from your library. New picks tomorrow." +
        (S.recs.smart ? "" : " (Simple mode: the similarity index isn't available, so this uses your channels only.)")) +
        (rows.length ? body : `<p class="end">Nothing left for today. Watch something from <a href="#/">New</a> and come back tomorrow.</p>`) +
        (rows.length ? `<p class="end">That's today's picks.</p>` : "");
      nopeMode = false;
      sel = Math.min(sel, i - 1); markSel();
    },
    channel(key) {
      const c = chByKey.get(key);
      const list = DB.videos.filter((v) => v.ch === key && (S.settings.clickbait || !v.cb));
      const pls = COURSES.filter((x) => x.ch === key && x.src === "playlist");
      const pre = pls.length ? () => `<h2 class="sec">Playlists</h2><div class="plrow">${pls.map((p) => {
        const done = p.ids.filter((id) => S.watched[id]).length;
        return `<a class="plcard" href="#/course/${encodeURIComponent(p.key)}"><b>${esc(p.title)}</b><span>${p.ids.length} videos${done ? ` · ${done} watched` : ""}</span>
          <span class="progress"><i style="width:${Math.round((100 * done) / p.ids.length)}%"></i></span></a>`;
      }).join("")}</div><h2 class="sec">Videos</h2>` : null;
      listView({ title: c?.name || key, pre, sub: `${list.length} videos${pls.length ? ` · ${pls.length} playlists` : ""} · <a href="https://www.youtube.com/${c?.cid ? "channel/" + c.cid : "@" + encodeURIComponent(key)}" target="_blank" rel="noopener">on YouTube ↗</a>`, list, empty: "No videos stored for this channel yet." });
    },
    classics() {
      listView({ title: "Classics", sub: "Pinned forever, from <code>classics.yml</code>.", list: DB.videos.filter((v) => v.ev), empty: "Add evergreen videos to classics.yml." });
    },
    later() {
      listView({ title: "Watch later", filterable: false, sub: "Your queue, oldest first.", sortable: false, list: S.later.map((id) => byId.get(id)).filter(Boolean), empty: "Queue is empty. Press <kbd>w</kbd> on anything you'd like to come back to." });
    },
    liked() {
      listView({ title: "Liked", filterable: false, sortable: false, list: S.liked.slice().reverse().map((id) => byId.get(id)).filter(Boolean), empty: "Nothing liked yet." });
    },
    history() {
      const seenIds = new Set(), list = [];
      for (const h of S.history.slice().reverse()) if (!seenIds.has(h.id) && byId.get(h.id)) { seenIds.add(h.id); list.push(byId.get(h.id)); }
      listView({ title: "History", filterable: false, sub: list.length ? `<button class="link" id="clearH">Clear history</button>` : "", sortable: false, list, empty: "No history yet." });
      $("#clearH")?.addEventListener("click", () => { if (confirm("Clear watch history?")) { S.history = []; save(); render(); } });
    },
    search(q) {
      const smartOn = S.settings.smart !== false;
      let { list, corrected } = StepSearch.rank(q, { visible, S });
      if (S.settings.searchSort === "new") list = list.slice().sort((a, b) => (b.p || "").localeCompare(a.p || ""));
      else if (S.settings.searchSort === "views") list = list.slice().sort(byViews);
      const fix = corrected.length
        ? ` · showing results for <b>${esc(tokenizeFix(q, corrected))}</b>` : "";
      const tools = `<label class="tools">Sort <select id="ssort">
        <option value="rel"${!["new", "views"].includes(S.settings.searchSort) ? " selected" : ""}>Relevance</option>
        <option value="new"${S.settings.searchSort === "new" ? " selected" : ""}>Upload date</option>
        <option value="views"${S.settings.searchSort === "views" ? " selected" : ""}>Most viewed</option></select></label>`;
      listView({ title: `“${q}”`, list, sortable: false, tools, byViews: S.settings.searchSort === "views",
        sub: `${list.length >= 500 ? "500+" : list.length} result${list.length === 1 ? "" : "s"}${fix}<br><span id="smode">${searchMode(q)}</span>`,
        empty: "No matches in your library." });
      $("#ssort").addEventListener("change", (e) => { S.settings.searchSort = e.target.value; save(); shown = PAGE; render(true); });
      if (smartOn && !StepSearch.isReady(q)) {
        StepSearch.semantic(q, () => { const el = $("#smode"); if (el) el.textContent = searchMode(q); })
          .then((changed) => { if (lastRoute === "/s/" + q) changed ? render(true) : ($("#smode") && ($("#smode").textContent = searchMode(q))); });
      }
    },
    channels() {
      const count = {}; DB.videos.forEach((v) => (count[v.ch] = (count[v.ch] || 0) + 1));
      main.innerHTML = `<div class="head"><div><h1>Channels</h1><div class="sub">Your whole “algorithm”. Edit <code>channels.yml</code> to add or remove. Mute here to hide a channel on this device.</div></div></div>` +
        DB.topics.map((t) => {
          const cs = DB.channels.filter((c) => c.topic === t.key).sort((a, b) => a.name.localeCompare(b.name));
          if (!cs.length) return "";
          return `<h2 class="sec">${esc(t.name)}</h2><div class="grid2">${cs.map((c) => {
            const m = S.muted.includes(c.key);
            const av = c.av ? `<img class="av" src="data/avatars/${c.cid}.jpg" alt="" loading="lazy">` : `<span class="av"></span>`;
            return `<div class="chan${m ? " muted" : ""}"><span class="cn">${av}<a href="#/c/${encodeURIComponent(c.key)}">${esc(c.name)}</a> <span class="n">${count[c.key] || 0}</span></span>
              <button class="btn" data-mute="${esc(c.key)}" aria-pressed="${m}">${m ? "Muted" : "Mute"}</button></div>`;
          }).join("")}</div>`;
        }).join("");
      main.querySelectorAll("[data-mute]").forEach((b) => b.addEventListener("click", () => {
        const k = b.dataset.mute; S.muted = S.muted.includes(k) ? S.muted.filter((x) => x !== k) : [...S.muted, k]; save(); render(true);
      }));
    },
    settings() {
      const opt = (name, val, cur, label) => `<button class="btn" data-set="${name}" data-val="${val}" aria-pressed="${String(cur) === String(val)}">${label}</button>`;
      const used = S.budget.day === today() ? Math.round(S.budget.sec / 60) : 0;
      const cb = DB.videos.filter((v) => v.cb).length;
      main.innerHTML = `<div class="head"><h1>Settings</h1></div>
        <div class="set"><h3>Thumbnails</h3><p>Muted (grayscale) by default, so titles compete on substance rather than on faces and arrows.</p>
          <div class="ctl">${opt("thumbs", "muted", S.settings.thumbs, "Muted")}${opt("thumbs", "color", S.settings.thumbs, "Color")}${opt("thumbs", "off", S.settings.thumbs, "Off")}</div></div>
        <div class="set"><h3>Theme</h3><p>Warm dark or warm light. Auto follows your system.</p>
          <div class="ctl">${opt("theme", "auto", S.settings.theme, "Auto")}${opt("theme", "dark", S.settings.theme, "Dark")}${opt("theme", "light", S.settings.theme, "Light")}</div></div>
        <div class="set"><h3>Smart search</h3><p>Finds videos by meaning (in any language) as well as keywords, and ranks them like YouTube. Downloads a ~120 MB model once, the first time you search.</p>
          <div class="ctl">${opt("smart", true, S.settings.smart !== false, "On")}${opt("smart", false, S.settings.smart !== false, "Off")}</div></div>
        <div class="set"><h3>Clickbait-flagged titles</h3><p>${cb} video${cb === 1 ? "" : "s"} flagged by title heuristics (caps, “shocking”, “!!”, …).</p>
          <div class="ctl">${opt("clickbait", false, S.settings.clickbait, "Hide")}${opt("clickbait", true, S.settings.clickbait, "Show")}</div></div>
        <div class="set"><h3>Daily budget</h3><p>Minutes of playback per day before a gentle reminder. 0 = off. Today: ${used} min.</p>
          <div class="ctl"><input type="number" min="0" step="5" id="budget" value="${S.settings.budget}" aria-label="Daily minutes"></div></div>
        <div class="set"><h3>Sync across devices</h3>
          <p>${SY.token
            ? (SY.err ? `Sync problem: ${esc(SY.err)}` : `On. ${SY.last ? `Last synced ${new Date(SY.last).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.` : "Syncing…"} Likes, history, Watch later, notes, resume positions and settings are kept in a private GitHub Gist.`)
            : `Keeps likes, history, Watch later, notes and resume positions the same on your phone and computer, in a private GitHub Gist. <a href="https://github.com/settings/tokens/new?scopes=gist&description=StepTube%20sync" target="_blank" rel="noopener">Create a token with only the “gist” permission ↗</a>, paste it here, and do the same on each device.`}</p>
          <div class="ctl">${SY.token
            ? `<button class="btn" id="syncnow">Sync now</button><button class="btn" id="syncoff">Disconnect</button>`
            : `<input type="password" id="synctok" placeholder="GitHub token" autocomplete="off" aria-label="GitHub token"><button class="btn" id="syncon">Connect</button>`}</div></div>
        <div class="set"><h3>Your data</h3><p>Likes, history, queue, notes and settings live only in this browser. Back them up or move them to another device.</p>
          <div class="ctl"><button class="btn" id="exp">Export</button><label class="btn">Import<input type="file" id="imp" accept="application/json" hidden></label><button class="btn" id="wipe">Reset</button></div></div>`;
      main.querySelectorAll("[data-set]").forEach((b) => b.addEventListener("click", () => {
        let v = b.dataset.val; if (v === "true") v = true; else if (v === "false") v = false;
        S.settings[b.dataset.set] = v; save(); applySettings(); render(true);
      }));
      $("#syncon")?.addEventListener("click", async () => {
        const tok = $("#synctok").value.trim(); if (!tok) return;
        SY = { token: tok, gistId: "", base: null, last: 0, err: "" }; saveSY();
        await syncNow(true); render(true);
      });
      $("#syncnow")?.addEventListener("click", async () => { await syncNow(true); render(true); });
      $("#syncoff")?.addEventListener("click", () => { if (confirm("Stop syncing on this device? Your data stays here and in the Gist.")) { SY = { token: "", gistId: "", base: null, last: 0, err: "" }; saveSY(); render(true); } });
      $("#budget").addEventListener("change", (e) => { S.settings.budget = Math.max(0, parseInt(e.target.value, 10) || 0); save(); });
      $("#exp").addEventListener("click", () => {
        const a = document.createElement("a");
        a.href = URL.createObjectURL(new Blob([JSON.stringify(S, null, 1)], { type: "application/json" }));
        a.download = `steptube-${today()}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
      });
      $("#imp").addEventListener("change", async (e) => {
        const f = e.target.files[0]; if (!f) return;
        try { const o = JSON.parse(await f.text()); S = { ...defaults(), ...o, settings: { ...defaults().settings, ...(o.settings || {}) } }; save(); applySettings(); render(); alert("Imported."); }
        catch (_) { alert("That file doesn't look like a StepTube export."); }
      });
      $("#wipe").addEventListener("click", () => { if (confirm("Erase all local StepTube data on this device?")) { S = defaults(); save(); applySettings(); render(); } });
    },
    watch(id) {
      const v = byId.get(id) || { id, t: "Video", ch: "", p: "" };
      const later = S.later.includes(id), liked = S.liked.includes(id), seen = !!S.watched[id];
      const nextLater = S.later.filter((x) => x !== id).map((x) => byId.get(x)).find(Boolean);
      const [crs, ci] = courseOf.get(id) || [null, -1];
      const nextInCourse = crs && crs.ids[ci + 1] ? byId.get(crs.ids[ci + 1]) : null;
      const posKeys = Object.keys(S.pos);
      if (posKeys.length > 300) posKeys.sort((x, y) => S.pos[x].at - S.pos[y].at).slice(0, posKeys.length - 300).forEach((k) => delete S.pos[k]);
      main.innerHTML = `<article class="watch">
        <div class="player"><div id="yt"></div>
          <div class="done" id="done"><div><p>Finished. Good time to pause.</p><div class="row">
            <a class="btn primary" href="#/">Back to library</a>
            ${nextInCourse ? `<a class="btn" href="#/v/${nextInCourse.id}">Next in ${crs.src === "playlist" ? "playlist" : "series"}</a>` : ""}
            ${nextLater ? `<a class="btn" href="#/v/${nextLater.id}">Next in Watch later</a>` : ""}
            <button class="btn" id="replay">Replay</button></div></div></div>
        </div>
        <h1>${esc(v.t)}</h1>
        <div class="byline">${chLink(v)}${v.p ? `<span class="dot">·</span>${fmtDate(v.p)}` : ""}${v.s ? `<span class="dot">·</span>${fmtDur(v.s)}` : ""}</div>
        ${crs ? `<div class="byline crs">Part ${ci + 1} of ${crs.ids.length} in <a href="#/course/${encodeURIComponent(crs.key)}">${esc(crs.title)}</a>${nextInCourse ? `<span class="dot">·</span>next: <a href="#/v/${nextInCourse.id}">${esc(nextInCourse.t)}</a>` : ""}</div>` : ""}
        <div class="row">
          <button class="btn" data-act="later" aria-pressed="${later}">${later ? "◆ In Watch later" : "◇ Watch later"}</button>
          <button class="btn" data-act="like" aria-pressed="${liked}">${liked ? "♥ Liked" : "♡ Like"}</button>
          <button class="btn" data-act="seen" aria-pressed="${seen}">${seen ? "✓ Watched" : "○ Mark watched"}</button>
          <a class="btn" href="https://www.youtube.com/watch?v=${id}" target="_blank" rel="noopener">YouTube ↗</a>
        </div>
        ${v.d ? `<p class="desc">${esc(v.d)}</p>` : ""}
        <div class="notes"><label for="note">Your notes (saved locally, searchable)</label><textarea id="note" placeholder="What's worth remembering?">${esc(S.notes[id] || "")}</textarea></div>
        <div class="budget" id="budgetLine"></div>
      </article>`;
      main.dataset.id = id;
      const ta = $("#note");
      ta.addEventListener("input", () => { const t = ta.value.trim(); if (t) S.notes[id] = ta.value; else delete S.notes[id]; save(); });
      $("#replay").addEventListener("click", () => { $("#done").classList.remove("show"); player?.seekTo?.(0); player?.playVideo?.(); });
      S.history.push({ id, at: Date.now() }); if (S.history.length > 2000) S.history = S.history.slice(-2000); save();
      mountPlayer(id);
      updateBudgetLine();
      window.scrollTo(0, 0);
    },
    notFound() { main.innerHTML = `<p class="end">Nothing here. <a href="#/">Go home</a>.</p>`; },
  };

  // ---------------------------------------------------------------- player
  let ytReady = null;
  function loadYT() {
    if (ytReady) return ytReady;
    ytReady = new Promise((res) => {
      window.onYouTubeIframeAPIReady = res;
      const s = document.createElement("script"); s.src = "https://www.youtube.com/iframe_api"; s.onerror = () => res(null);
      document.head.appendChild(s);
    });
    return ytReady;
  }
  async function mountPlayer(id) {
    destroyPlayer();
    const ok = await loadYT();
    if (!$("#yt") || main.dataset.id !== id) return;
    if (!ok || !window.YT?.Player) {
      $("#yt").outerHTML = `<iframe src="https://www.youtube-nocookie.com/embed/${id}?rel=0&modestbranding=1&iv_load_policy=3" allow="encrypted-media; picture-in-picture; fullscreen" allowfullscreen title="Video player"></iframe>`;
      return;
    }
    player = new YT.Player("yt", {
      host: "https://www.youtube-nocookie.com", videoId: id,
      playerVars: { rel: 0, modestbranding: 1, iv_load_policy: 3, playsinline: 1, autoplay: 0, ...resumeAt(id) },
      events: {
        onReady: (e) => { const r = S.settings.rate; if (r && r !== 1) try { e.target.setPlaybackRate(r); } catch (_) {} },
        onPlaybackRateChange: (e) => { S.settings.rate = e.data; save(); },
        onStateChange: (e) => {
          clearInterval(tick); tick = null;
          if (e.data === YT.PlayerState.PLAYING) {
            tick = setInterval(() => {
              if (S.budget.day !== today()) S.budget = { day: today(), sec: 0 };
              S.budget.sec += 5; recordPos(id); save(); updateBudgetLine();
            }, 5000);
          }
          if (e.data === YT.PlayerState.PAUSED) { recordPos(id); save(); }
          if (e.data === YT.PlayerState.ENDED) {
            S.watched[id] = Date.now(); delete S.pos[id]; save();
            $("#done")?.classList.add("show");
            const b = main.querySelector('.row [data-act="seen"]'); if (b) { b.setAttribute("aria-pressed", "true"); b.textContent = "✓ Watched"; }
          }
        },
      },
    });
  }
  // Resume: start a few seconds before where you stopped (not for the first/last half minute).
  function resumeAt(id) {
    const p = S.pos[id];
    return p && p.t > 30 && (!p.d || p.t < p.d - 30) ? { start: Math.max(0, Math.floor(p.t) - 3) } : {};
  }
  function recordPos(id) {
    let t = 0, d = 0;
    try { t = player.getCurrentTime(); d = player.getDuration(); } catch (_) { return; }
    if (!(t > 0)) return;
    if (d && t / d > 0.95) {          // close enough to the end: count it as watched
      if (!S.watched[id]) { S.watched[id] = Date.now(); rerenderWatchButtons(id); }
      delete S.pos[id]; return;
    }
    S.pos[id] = { t: Math.round(t), d: Math.round(d || 0), at: Date.now() };
  }
  function destroyPlayer() { clearInterval(tick); tick = null; try { player?.destroy?.(); } catch (_) {} player = null; }
  function updateBudgetLine() {
    const el = $("#budgetLine"); if (!el) return;
    const b = S.settings.budget, used = S.budget.day === today() ? Math.floor(S.budget.sec / 60) : 0;
    el.textContent = b ? `${used} of ${b} minutes today${used >= b ? ". Budget reached." : ""}` : "";
  }

  // ---------------------------------------------------------------- actions
  function toggle(act, id) {
    if (act === "nope") {
      if (!S.notInterested.includes(id)) S.notInterested.push(id);
      if (S.notInterested.length > 500) S.notInterested = S.notInterested.slice(-500);
      save(); return;
    }
    if (act === "later") S.later = S.later.includes(id) ? S.later.filter((x) => x !== id) : [...S.later, id];
    if (act === "like") S.liked = S.liked.includes(id) ? S.liked.filter((x) => x !== id) : [...S.liked, id];
    if (act === "seen") { if (S.watched[id]) delete S.watched[id]; else { S.watched[id] = Date.now(); delete S.pos[id]; } }
    save();
  }
  main.addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]"); if (!b) return;
    const id = b.closest("[data-id]")?.dataset.id || main.dataset.id;
    if (!id) return;
    toggle(b.dataset.act, id);
    if (location.hash.startsWith("#/v/")) rerenderWatchButtons(id); else render(true);
  });
  function rerenderWatchButtons(id) {
    const set = (act, on, a, b) => { const el = main.querySelector(`.row [data-act="${act}"]`); if (el) { el.setAttribute("aria-pressed", on); el.textContent = on ? a : b; } };
    set("later", S.later.includes(id), "◆ In Watch later", "◇ Watch later");
    set("like", S.liked.includes(id), "♥ Liked", "♡ Like");
    set("seen", !!S.watched[id], "✓ Watched", "○ Mark watched");
  }

  // ---------------------------------------------------------------- router
  function render(keep = false) {
    const h = decodeURIComponent(location.hash.slice(1) || "/");
    if (!keep && h !== lastRoute) { shown = PAGE; sel = -1; }
    if (!h.startsWith("/v/")) { destroyPlayer(); delete main.dataset.id; }
    lastRoute = h;
    const y = keep ? window.scrollY : 0;
    const [, a, ...rest] = h.split("/"); const b = rest.join("/");
    const q = $("#q");
    if (a !== "s" && document.activeElement !== q) q.value = "";
    if (!a) views.home();
    else if (a === "t") views.topic(b);
    else if (a === "c") views.channel(b);
    else if (a === "v") views.watch(b);
    else if (a === "s") views.search(b);
    else if (a === "course") views.course(b);
    else if (views[a] && !["topic", "channel", "watch", "search", "course", "notFound"].includes(a)) views[a]();
    else views.notFound();
    if (keep) window.scrollTo(0, y);
    const tab = a === "t" ? b : !a ? "" : (a === "course" || a === "courses") ? "playlists" : a;
    document.querySelectorAll("#tabs a").forEach((x) => x.toggleAttribute("aria-current", x.dataset.tab === tab && a !== "v" && a !== "s"));
    document.querySelectorAll(".me a").forEach((x) => x.toggleAttribute("aria-current", x.dataset.nav === a));
    document.title = a === "v" ? `${byId.get(b)?.t || "Video"} · StepTube` : "StepTube";
  }
  const route = () => render();
  window.addEventListener("hashchange", route);

  let qTimer;
  $("#q").addEventListener("input", (e) => {
    const dl = $("#sugg");
    if (dl && window.StepSearch) dl.innerHTML = StepSearch.suggest(e.target.value, DB.channels).map((x) => `<option value="${esc(x)}">`).join("");
    clearTimeout(qTimer);
    qTimer = setTimeout(() => {
      const v = e.target.value.trim();
      const target = v ? `#/s/${encodeURIComponent(v)}` : "#/";
      if (location.hash !== target) history.replaceState(null, "", target), render();
    }, 180);
  });

  // ---------------------------------------------------------------- keyboard
  function cards() { return [...main.querySelectorAll(".card")]; }
  function markSel() {
    cards().forEach((c, i) => c.classList.toggle("sel", i === sel));
  }
  let gPending = false;
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName);
    if (e.key === "Escape") {
      if (typing) { document.activeElement.blur(); return; }
      if ($("#help").open) return;
      if (location.hash.startsWith("#/v/") || location.hash.startsWith("#/s/")) history.length > 1 ? history.back() : (location.hash = "#/");
      return;
    }
    if (typing) return;
    const cs = cards();
    if (gPending) {
      gPending = false;
      const m = { l: "#/later", h: "#/history", c: "#/channels", s: "#/settings", k: "#/liked", n: "#/", f: "#/foryou", u: "#/playlists" }[e.key];
      if (m) { location.hash = m; e.preventDefault(); }
      return;
    }
    switch (e.key) {
      case "/": e.preventDefault(); $("#q").focus(); $("#q").select(); break;
      case "?": $("#help").showModal(); break;
      case "g": gPending = true; setTimeout(() => (gPending = false), 1200); break;
      case "j": case "k": {
        if (!cs.length) return;
        sel = e.key === "j" ? Math.min(cs.length - 1, sel + 1) : Math.max(0, sel - 1);
        markSel(); cs[sel].scrollIntoView({ block: "nearest" }); break;
      }
      case "Enter": case "o": if (cs[sel]) location.hash = `#/v/${cs[sel].dataset.id}`; break;
      case "n": {
        if (location.hash !== "#/foryou" || !cs[sel]) break;
        toggle("nope", cs[sel].dataset.id); render(true); break;
      }
      case "w": case "l": case "x": {
        const act = { w: "later", l: "like", x: "seen" }[e.key];
        const id = location.hash.startsWith("#/v/") ? main.dataset.id : cs[sel]?.dataset.id;
        if (!id) return;
        toggle(act, id);
        if (location.hash.startsWith("#/v/")) rerenderWatchButtons(id); else render(true);
        break;
      }
      default:
        if (/^[0-9]$/.test(e.key)) {
          const n = +e.key;
          if (n === 0) location.hash = "#/";
          else if (DB.topics[n - 1]) location.hash = `#/t/${DB.topics[n - 1].key}`;
        }
    }
  });
  $("#helpBtn").addEventListener("click", () => $("#help").showModal());

  applySettings();
  load();
})();
