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
    later: [], liked: [], history: [], watched: {}, notes: {}, muted: [],
    settings: { thumbs: "muted", clickbait: false, theme: "auto", budget: 60, sort: "new" },
    lastVisit: null, budget: { day: "", sec: 0 },
  });
  let S = defaults();
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) { const o = JSON.parse(raw); S = { ...S, ...o, settings: { ...S.settings, ...(o.settings || {}) } }; }
  } catch (_) { /* private mode etc. */ }
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (_) {} };

  // Remember the previous visit so "New" can mean "new since last time".
  const prevVisit = S.lastVisit;
  S.lastVisit = new Date().toISOString(); save();

  let DB = { topics: [], channels: [], videos: [] };
  let byId = new Map(), chByKey = new Map();
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
    $("#tabs").innerHTML =
      `<a href="#/" data-tab="">New</a>` +
      DB.topics.map((t) => `<a href="#/t/${t.key}" data-tab="${t.key}">${esc(t.name)}</a>`).join("") +
      `<a href="#/classics" data-tab="classics">Classics</a><a href="#/channels" data-tab="channels">Channels</a>`;
    const when = DB.generated ? new Date(DB.generated) : null;
    $("#meta").textContent = `${DB.videos.length} videos · ${DB.channels.length} channels` +
      (when ? ` · updated ${when.toLocaleDateString(undefined, { month: "short", day: "numeric" })}` : "");
    route();
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

  function search(q, pool) {
    const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
    return pool.filter((v) => {
      const hay = `${v.t} ${chName(v)} ${v.d || ""} ${S.notes[v.id] || ""}`.toLowerCase();
      return terms.every((t) => hay.includes(t));
    });
  }

  function sortList(list, mode) {
    const l = list.slice();
    if (mode === "old") l.sort((a, b) => (a.p || "").localeCompare(b.p || ""));
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
        <div class="byline">${v.ch ? `<a href="#/c/${encodeURIComponent(v.ch)}">${esc(chName(v))}</a>` : esc(chName(v))}${v.p ? `<span class="dot">·</span>${fmtDate(v.p)}` : ""}${v.ev ? `<span class="tag">classic</span>` : ""}${S.notes[v.id] ? `<span class="tag">note</span>` : ""}</div>
        ${v.d ? `<p class="desc">${esc(v.d)}</p>` : ""}
      </div>
      <div class="acts">
        <button class="icon" data-act="later" aria-pressed="${later}" title="Watch later (w)" aria-label="Watch later">${later ? "◆" : "◇"}</button>
        <button class="icon" data-act="like" aria-pressed="${liked}" title="Like (l)" aria-label="Like">${liked ? "♥" : "♡"}</button>
        <button class="icon" data-act="seen" aria-pressed="${seen}" title="Mark watched (x)" aria-label="Mark watched">${seen ? "✓" : "○"}</button>
      </div>
    </li>`;
  }

  function listView({ title, sub = "", list, empty, note = "", sortable = true, endText }) {
    const sortSel = sortable ? `<label class="tools">Sort
      <select id="sort">${[["new", "Newest"], ["old", "Oldest"], ["shuffle", "Today's shuffle"]]
        .map(([k, n]) => `<option value="${k}"${S.settings.sort === k ? " selected" : ""}>${n}</option>`).join("")}</select></label>` : "";
    const items = sortable ? sortList(list, S.settings.sort) : list;
    const page = items.slice(0, shown);
    main.innerHTML = `
      <div class="head"><div><h1>${esc(title)}</h1>${sub ? `<div class="sub">${sub}</div>` : ""}</div>${sortSel}</div>
      ${note ? `<div class="note">${note}</div>` : ""}
      ${items.length ? `<ul class="list">${page.map(card).join("")}</ul>` : `<p class="end">${empty}</p>`}
      ${items.length > shown ? `<div class="more"><button class="btn" id="more">Show ${Math.min(PAGE, items.length - shown)} more <span style="color:var(--text-3)">(${items.length - shown} left)</span></button></div>`
        : items.length ? `<p class="end">${endText || "That's everything here."}</p>` : ""}`;
    $("#sort")?.addEventListener("change", (e) => { S.settings.sort = e.target.value; save(); shown = PAGE; render(); });
    $("#more")?.addEventListener("click", () => { shown += PAGE; render(true); });
    sel = Math.min(sel, page.length - 1);
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
      const list = DB.videos.filter((v) => visible(v) && !v.ev && v.tp !== "review" && v.p >= cutoff && !S.watched[v.id]);
      const fresh = prevVisit ? list.filter((v) => v.p >= prevVisit.slice(0, 10)).length : 0;
      const sub = prevVisit
        ? (fresh ? `${fresh} posted since your last visit.` : "Nothing new since your last visit.")
        : "Unwatched videos from your channels, last three weeks.";
      listView({ title: "New", sub, list, sortable: true, note: budgetNote(),
        empty: "You're all caught up. Go outside, or check the <a href='#/classics'>classics</a>.",
        endText: "You're caught up." });
    },
    topic(key) {
      const t = DB.topics.find((x) => x.key === key);
      if (!t) return views.notFound();
      const list = DB.videos.filter((v) => v.tp === key && visible(v));
      const note = key === "review"
        ? "These channels are on probation and don't appear on the New page. Watch a few, then move each to a real topic in <code>channels.yml</code> or delete its line. <a href='#/channels'>Channel list</a>."
        : budgetNote();
      listView({ title: t.name, sub: `${list.length} videos`, list, empty: "Nothing here yet.", note });
    },
    channel(key) {
      const c = chByKey.get(key);
      const list = DB.videos.filter((v) => v.ch === key && (S.settings.clickbait || !v.cb));
      listView({ title: c?.name || key, sub: `${list.length} videos · <a href="https://www.youtube.com/${c?.cid ? "channel/" + c.cid : "@" + encodeURIComponent(key)}" target="_blank" rel="noopener">on YouTube ↗</a>`, list, empty: "No videos stored for this channel yet." });
    },
    classics() {
      listView({ title: "Classics", sub: "Pinned forever, from <code>classics.yml</code>.", list: DB.videos.filter((v) => v.ev), empty: "Add evergreen videos to classics.yml." });
    },
    later() {
      listView({ title: "Watch later", sub: "Your queue, oldest first.", sortable: false, list: S.later.map((id) => byId.get(id)).filter(Boolean), empty: "Queue is empty. Press <kbd>w</kbd> on anything you'd like to come back to." });
    },
    liked() {
      listView({ title: "Liked", sortable: false, list: S.liked.slice().reverse().map((id) => byId.get(id)).filter(Boolean), empty: "Nothing liked yet." });
    },
    history() {
      const seenIds = new Set(), list = [];
      for (const h of S.history.slice().reverse()) if (!seenIds.has(h.id) && byId.get(h.id)) { seenIds.add(h.id); list.push(byId.get(h.id)); }
      listView({ title: "History", sub: list.length ? `<button class="link" id="clearH">Clear history</button>` : "", sortable: false, list, empty: "No history yet." });
      $("#clearH")?.addEventListener("click", () => { if (confirm("Clear watch history?")) { S.history = []; save(); render(); } });
    },
    search(q) {
      const list = search(q, DB.videos.filter(visible));
      listView({ title: `“${q}”`, sub: `${list.length} result${list.length === 1 ? "" : "s"}`, list, empty: "No matches in your library." });
    },
    channels() {
      const count = {}; DB.videos.forEach((v) => (count[v.ch] = (count[v.ch] || 0) + 1));
      main.innerHTML = `<div class="head"><div><h1>Channels</h1><div class="sub">Your whole “algorithm”. Edit <code>channels.yml</code> to add or remove. Mute here to hide a channel on this device.</div></div></div>` +
        DB.topics.map((t) => {
          const cs = DB.channels.filter((c) => c.topic === t.key).sort((a, b) => a.name.localeCompare(b.name));
          if (!cs.length) return "";
          return `<h2 class="sec">${esc(t.name)}</h2><div class="grid2">${cs.map((c) => {
            const m = S.muted.includes(c.key);
            return `<div class="chan${m ? " muted" : ""}"><span><a href="#/c/${encodeURIComponent(c.key)}">${esc(c.name)}</a> <span class="n">${count[c.key] || 0}</span></span>
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
        <div class="set"><h3>Clickbait-flagged titles</h3><p>${cb} video${cb === 1 ? "" : "s"} flagged by title heuristics (caps, “shocking”, “!!”, …).</p>
          <div class="ctl">${opt("clickbait", false, S.settings.clickbait, "Hide")}${opt("clickbait", true, S.settings.clickbait, "Show")}</div></div>
        <div class="set"><h3>Daily budget</h3><p>Minutes of playback per day before a gentle reminder. 0 = off. Today: ${used} min.</p>
          <div class="ctl"><input type="number" min="0" step="5" id="budget" value="${S.settings.budget}" aria-label="Daily minutes"></div></div>
        <div class="set"><h3>Your data</h3><p>Likes, history, queue, notes and settings live only in this browser. Back them up or move them to another device.</p>
          <div class="ctl"><button class="btn" id="exp">Export</button><label class="btn">Import<input type="file" id="imp" accept="application/json" hidden></label><button class="btn" id="wipe">Reset</button></div></div>`;
      main.querySelectorAll("[data-set]").forEach((b) => b.addEventListener("click", () => {
        let v = b.dataset.val; if (v === "true") v = true; else if (v === "false") v = false;
        S.settings[b.dataset.set] = v; save(); applySettings(); render(true);
      }));
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
      main.innerHTML = `<article class="watch">
        <div class="player"><div id="yt"></div>
          <div class="done" id="done"><div><p>Finished. Good time to pause.</p><div class="row">
            <a class="btn primary" href="#/">Back to library</a>
            ${nextLater ? `<a class="btn" href="#/v/${nextLater.id}">Next in Watch later</a>` : ""}
            <button class="btn" id="replay">Replay</button></div></div></div>
        </div>
        <h1>${esc(v.t)}</h1>
        <div class="byline">${v.ch ? `<a href="#/c/${encodeURIComponent(v.ch)}">${esc(chName(v))}</a>` : esc(chName(v))}${v.p ? `<span class="dot">·</span>${fmtDate(v.p)}` : ""}</div>
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
      playerVars: { rel: 0, modestbranding: 1, iv_load_policy: 3, playsinline: 1, autoplay: 0 },
      events: {
        onStateChange: (e) => {
          clearInterval(tick); tick = null;
          if (e.data === YT.PlayerState.PLAYING) {
            tick = setInterval(() => {
              if (S.budget.day !== today()) S.budget = { day: today(), sec: 0 };
              S.budget.sec += 5; save(); updateBudgetLine();
            }, 5000);
          }
          if (e.data === YT.PlayerState.ENDED) {
            S.watched[id] = Date.now(); save();
            $("#done")?.classList.add("show");
            const b = main.querySelector('.row [data-act="seen"]'); if (b) { b.setAttribute("aria-pressed", "true"); b.textContent = "✓ Watched"; }
          }
        },
      },
    });
  }
  function destroyPlayer() { clearInterval(tick); tick = null; try { player?.destroy?.(); } catch (_) {} player = null; }
  function updateBudgetLine() {
    const el = $("#budgetLine"); if (!el) return;
    const b = S.settings.budget, used = S.budget.day === today() ? Math.floor(S.budget.sec / 60) : 0;
    el.textContent = b ? `${used} of ${b} minutes today${used >= b ? ". Budget reached." : ""}` : "";
  }

  // ---------------------------------------------------------------- actions
  function toggle(act, id) {
    if (act === "later") S.later = S.later.includes(id) ? S.later.filter((x) => x !== id) : [...S.later, id];
    if (act === "like") S.liked = S.liked.includes(id) ? S.liked.filter((x) => x !== id) : [...S.liked, id];
    if (act === "seen") { if (S.watched[id]) delete S.watched[id]; else S.watched[id] = Date.now(); }
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
    else if (views[a] && !["topic", "channel", "watch", "search", "notFound"].includes(a)) views[a]();
    else views.notFound();
    if (keep) window.scrollTo(0, y);
    const tab = a === "t" ? b : !a ? "" : a;
    document.querySelectorAll("#tabs a").forEach((x) => x.toggleAttribute("aria-current", x.dataset.tab === tab && a !== "v" && a !== "s"));
    document.querySelectorAll(".me a").forEach((x) => x.toggleAttribute("aria-current", x.dataset.nav === a));
    document.title = a === "v" ? `${byId.get(b)?.t || "Video"} · StepTube` : "StepTube";
  }
  const route = () => render();
  window.addEventListener("hashchange", route);

  let qTimer;
  $("#q").addEventListener("input", (e) => {
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
      const m = { l: "#/later", h: "#/history", c: "#/channels", s: "#/settings", k: "#/liked", n: "#/" }[e.key];
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
