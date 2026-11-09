/* StepTube sync: keeps likes, history, queue, notes, resume positions and
   settings the same on every device, in a private GitHub Gist that only your
   token can read. No server of ours is involved.

   Merging is three-way (what we last synced, this device, the Gist), so a
   change made on one device, including removing something, is never undone by
   another device that hasn't seen it yet. */
(() => {
  "use strict";
  const FILE = "steptube-sync.json";
  const API = "https://api.github.com";
  const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

  function mergeSet(base = [], local = [], remote = []) {
    const gone = new Set([...base.filter((x) => !local.includes(x)), ...base.filter((x) => !remote.includes(x))]);
    return [...new Set([...local, ...remote])].filter((x) => !gone.has(x));
  }
  function mergeMap(base = {}, local = {}, remote = {}) {
    const out = {};
    for (const k of new Set([...Object.keys(local), ...Object.keys(remote)])) {
      // This device changed it since the last sync -> ours wins; otherwise take the Gist's.
      const v = eq(local[k], base[k]) ? remote[k] : local[k];
      if (v !== undefined) out[k] = v;
    }
    return out;
  }
  function merge3(base = {}, local = {}, remote = {}) {
    const out = {};
    for (const k of ["later", "liked", "muted", "notInterested"]) out[k] = mergeSet(base[k], local[k], remote[k]);
    for (const k of ["watched", "notes", "pos", "settings"]) out[k] = mergeMap(base[k], local[k], remote[k]);
    const seen = new Set(), hist = [];
    for (const h of [...(local.history || []), ...(remote.history || [])]) {
      const key = `${h.id}:${h.at}`; if (seen.has(key)) continue; seen.add(key); hist.push(h);
    }
    out.history = hist.sort((a, b) => a.at - b.at).slice(-2000);
    const lb = local.budget || { day: "", sec: 0 }, rb = remote.budget || { day: "", sec: 0 };
    out.budget = lb.day === rb.day ? { day: lb.day, sec: Math.max(lb.sec, rb.sec) } : (lb.day > rb.day ? lb : rb);
    return out;
  }

  async function gh(token, path, opts = {}) {
    const r = await fetch(API + path, {
      ...opts,
      headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", ...(opts.body ? { "Content-Type": "application/json" } : {}) },
    });
    if (r.status === 401) throw new Error("GitHub rejected the token (expired or mistyped).");
    if (r.status === 403) throw new Error("The token lacks the 'gist' permission, or GitHub is rate-limiting.");
    if (r.status === 404) throw new Error("not-found");
    if (!r.ok) throw new Error(`GitHub error ${r.status}`);
    return r.json();
  }
  async function findOrCreate(token) {
    const list = await gh(token, "/gists?per_page=100");
    const hit = list.find((g) => g.files && g.files[FILE]);
    if (hit) return hit.id;
    const made = await gh(token, "/gists", { method: "POST", body: JSON.stringify({
      description: "StepTube sync (private)", public: false, files: { [FILE]: { content: JSON.stringify({ v: 1, data: null }) } } }) });
    return made.id;
  }
  async function pull(token, gistId) {
    const g = await gh(token, `/gists/${gistId}`);
    const f = g.files && g.files[FILE]; if (!f) return null;
    const text = f.truncated ? await (await fetch(f.raw_url)).text() : f.content;
    try { return JSON.parse(text).data || null; } catch { return null; }
  }
  async function push(token, gistId, data) {
    await gh(token, `/gists/${gistId}`, { method: "PATCH", body: JSON.stringify({
      files: { [FILE]: { content: JSON.stringify({ v: 1, updatedAt: new Date().toISOString(), data }) } } }) });
  }

  window.StepSync = { merge3, findOrCreate, pull, push };
})();
