/* StepTube search: a small, local version of how YouTube search works.

   YouTube's pipeline, roughly:
     1. Query understanding  - normalise, spell-correct, expand.
     2. Retrieval            - pull candidates from a keyword (inverted) index
                               AND from semantic/embedding neighbours.
     3. Ranking              - order candidates by relevance first, then lift
                               them with quality/engagement (views), freshness
                               and personalisation (your history, your channels).
   We do the same, entirely in your browser:
     - Lexical: BM25F over title (x3), channel (x2) and description (x1),
       with prefix matching (search-as-you-type), crude stemming (helps
       Russian/Armenian inflections) and typo tolerance (edit distance 1-2).
     - Semantic: multilingual-e5-small embeddings. Videos are embedded at
       build time; your query is embedded here with the same model
       (transformers.js, ~120 MB, downloaded once and cached by the browser).
     - Ranking: 0.5 semantic + 0.5 keyword relevance (meaning-only matches
       must be close and rank below strong keyword hits), multiplied by gentle
       boosts for views, your taste profile (mean embedding of what you liked
       and watched), channels you watch, and recency.
   What we can't copy: YouTube's click/watch-time data from billions of users
   and video transcripts. */
(() => {
  "use strict";
  const LIB = "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.0.2";
  const MODEL = "Xenova/multilingual-e5-small";
  const K1 = 1.2, B = 0.75, FIELD_W = [3, 2, 1]; // title, channel, description

  const tokenize = (s) => (s || "").normalize("NFKC").toLowerCase().replace(/ё/g, "е")
    .split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  const fnv1a = (s) => { let h = 0x811c9dc5; for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 0x01000193) >>> 0; return h; };

  let videos = [], chName = () => "", post = new Map(), vocab = [], docLen, avgLen = 1, N = 0;
  let idIndex = new Map();
  const status = { mode: "keyword", detail: "" };

  // ------------------------------------------------------------ lexical index
  function build(db, channelName) {
    videos = db.videos; chName = channelName; N = videos.length;
    post = new Map(); docLen = new Float32Array(N); idIndex = new Map();
    let total = 0;
    videos.forEach((v, i) => {
      idIndex.set(v.id, i);
      const tf = new Map();
      [v.t, channelName(v), v.d].forEach((text, f) => {
        for (const t of tokenize(text)) tf.set(t, (tf.get(t) || 0) + FIELD_W[f]);
      });
      let len = 0;
      for (const [t, w] of tf) {
        len += w;
        let p = post.get(t); if (!p) post.set(t, (p = []));
        p.push(i, w);
      }
      docLen[i] = len; total += len;
    });
    avgLen = total / Math.max(1, N);
    vocab = [...post.keys()].sort();
    sem.index = null; sem.cache.clear();
  }

  function lowerBound(arr, x) { let lo = 0, hi = arr.length; while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] < x) lo = m + 1; else hi = m; } return lo; }
  function withPrefix(p, cap = 60) {
    const out = []; for (let i = lowerBound(vocab, p); i < vocab.length && out.length < cap && vocab[i].startsWith(p); i++) out.push(vocab[i]);
    return out;
  }
  function editDistance(a, b, max) {
    if (Math.abs(a.length - b.length) > max) return max + 1;
    let prev = Array.from({ length: b.length + 1 }, (_, j) => j);
    for (let i = 1; i <= a.length; i++) {
      const cur = [i]; let best = i;
      for (let j = 1; j <= b.length; j++) {
        cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
        if (cur[j] < best) best = cur[j];
      }
      if (best > max) return max + 1;
      prev = cur;
    }
    return prev[b.length];
  }

  // Query understanding: each query term -> weighted index terms.
  function expand(term, isLast) {
    const out = new Map();
    if (post.has(term)) out.set(term, 1);
    if (term.length >= 2) for (const t of withPrefix(term)) if (!out.has(t)) out.set(t, isLast ? 0.8 : 0.6);
    if (term.length >= 6) { // crude stemming: матрица/матрицы, theorem/theorems
      for (const t of withPrefix(term.slice(0, -2))) if (!out.has(t)) out.set(t, 0.55);
    }
    if (!out.size && term.length >= 4) { // typo tolerance
      const max = term.length >= 8 ? 2 : 1;
      for (const t of vocab) {
        if (out.size >= 15) break;
        if (Math.abs(t.length - term.length) <= max && t[0] === term[0] && editDistance(term, t, max) <= max) out.set(t, 0.5);
      }
    }
    return out;
  }

  function lexical(q) {
    const terms = [...new Set(tokenize(q))];
    const scores = new Float32Array(N), hits = new Uint8Array(N);
    const corrected = [];
    terms.forEach((term, qi) => {
      const exp = expand(term, qi === terms.length - 1);
      if (!post.has(term) && exp.size && [...exp.values()].every((w) => w === 0.5)) corrected.push([term, [...exp.keys()][0]]);
      const best = new Float32Array(N), seen = [];
      for (const [t, w] of exp) {
        const p = post.get(t); const df = p.length / 2;
        const idf = Math.log(1 + (N - df + 0.5) / (df + 0.5));
        for (let k = 0; k < p.length; k += 2) {
          const d = p[k], tf = p[k + 1];
          const s = w * idf * (tf * (K1 + 1)) / (tf + K1 * (1 - B + B * docLen[d] / avgLen));
          if (s > best[d]) { if (!best[d]) seen.push(d); best[d] = s; }
        }
      }
      for (const d of seen) { scores[d] += best[d]; hits[d]++; }
    });
    // Prefer documents that match every term, and exact title phrases.
    const phrase = tokenize(q).join(" ");
    for (let d = 0; d < N; d++) {
      if (!scores[d]) continue;
      scores[d] *= 0.5 + 0.5 * (hits[d] / terms.length) ** 2;
      if (terms.length > 1 && tokenize(videos[d].t).join(" ").includes(phrase)) scores[d] *= 1.5;
    }
    return { scores, corrected };
  }

  // ------------------------------------------------------------ semantic
  const sem = { index: null, extractor: null, loading: null, cache: new Map(), failed: false };

  async function loadIndex() {
    if (sem.index || sem.failed) return sem.index;
    const [meta, pca, emb] = await Promise.all([
      fetch("data/search/meta.json").then((r) => { if (!r.ok) throw new Error("no index"); return r.json(); }),
      fetch("data/search/pca.f32").then((r) => r.arrayBuffer()),
      fetch("data/search/emb.i8").then((r) => r.arrayBuffer()),
    ]);
    if (meta.n !== N || meta.ids_fnv !== fnv1a(videos.map((v) => v.id).join("\n"))) throw new Error("index out of date");
    const f = new Float32Array(pca);
    sem.index = { dims: meta.dims, dimIn: meta.dim_in, mean: f.subarray(0, meta.dim_in), comps: f.subarray(meta.dim_in), rows: new Int8Array(emb) };
    return sem.index;
  }

  async function loadModel(onProgress) {
    if (sem.extractor) return sem.extractor;
    if (!sem.loading) sem.loading = (async () => {
      const { pipeline, env } = await import(LIB);
      env.allowLocalModels = false;
      const files = {};
      sem.extractor = await pipeline("feature-extraction", MODEL, {
        dtype: "q8",
        progress_callback: (p) => {
          if (p.status === "progress" && p.total) {
            files[p.file] = [p.loaded, p.total];
            const [l, t] = Object.values(files).reduce((a, [x, y]) => [a[0] + x, a[1] + y], [0, 0]);
            onProgress?.(Math.round((100 * l) / t));
          }
        },
      });
      return sem.extractor;
    })();
    return sem.loading;
  }

  function project(vec, ix) {
    const out = new Float32Array(ix.dims);
    for (let i = 0; i < ix.dimIn; i++) {
      const x = vec[i] - ix.mean[i]; if (!x) continue;
      const row = i * ix.dims;
      for (let j = 0; j < ix.dims; j++) out[j] += x * ix.comps[row + j];
    }
    const n = Math.hypot(...out) || 1;
    for (let j = 0; j < ix.dims; j++) out[j] /= n;
    return out;
  }
  function dotAll(q, ix) {
    const s = new Float32Array(N), D = ix.dims, r = ix.rows;
    for (let d = 0, o = 0; d < N; d++, o += D) { let acc = 0; for (let j = 0; j < D; j++) acc += q[j] * r[o + j]; s[d] = acc / 127; }
    return s;
  }
  function rowVec(d, ix) { const D = ix.dims, o = d * D, v = new Float32Array(D); for (let j = 0; j < D; j++) v[j] = ix.rows[o + j] / 127; return v; }

  /** Embed the query; resolves true once semantic scores for q are cached. */
  async function semantic(q, onStatus) {
    q = q.trim(); if (!q || sem.failed) return false;
    if (sem.cache.has(q)) return false;
    try {
      const ix = await loadIndex();
      if (!sem.extractor) { status.mode = "loading"; status.detail = "0%"; onStatus?.(); }
      const ex = await loadModel((pct) => { status.detail = `${pct}%`; onStatus?.(); });
      const out = await ex(`query: ${q}`, { pooling: "mean", normalize: true });
      sem.cache.set(q, dotAll(project(out.data, ix), ix));
      if (sem.cache.size > 30) sem.cache.delete(sem.cache.keys().next().value);
      status.mode = "smart"; status.detail = "";
      return true;
    } catch (e) {
      console.warn("[steptube] semantic search unavailable:", e);
      sem.failed = true; status.mode = "keyword"; status.detail = String(e.message || e);
      return false;
    }
  }

  // ------------------------------------------------------------ ranking
  function profile(S) {
    const ix = sem.index; if (!ix) return null;
    const acc = new Float32Array(ix.dims); let any = false;
    const add = (id, w) => { const d = idIndex.get(id); if (d == null) return; const v = rowVec(d, ix); for (let j = 0; j < ix.dims; j++) acc[j] += w * v[j]; any = true; };
    S.liked.forEach((id) => add(id, 2));
    S.history.slice(-300).forEach((h) => add(h.id, 1));
    S.later.forEach((id) => add(id, 0.5));
    if (!any) return null;
    const n = Math.hypot(...acc) || 1; return acc.map((x) => x / n);
  }

  function rank(q, { visible, S }) {
    q = q.trim(); if (!q || !N) return { list: [], corrected: [] };
    const { scores: lex, corrected } = lexical(q);
    const semS = sem.cache.get(q) || null;
    let maxLex = 0; for (let d = 0; d < N; d++) if (lex[d] > maxLex) maxLex = lex[d];

    // Semantic scores -> [0,1] relative to this query's own distribution.
    let semLo = 0, semHi = 1;
    if (semS) {
      const sorted = Float32Array.from(semS).sort();
      semHi = sorted[N - 1]; semLo = sorted[Math.max(0, N - 400)];
    }
    // Personal signals.
    const prof = semS ? profile(S) : null;
    const chCount = {}; let chMax = 1;
    for (const h of S.history) { const v = videos[idIndex.get(h.id)]; if (v) chMax = Math.max(chMax, (chCount[v.ch] = (chCount[v.ch] || 0) + 1)); }
    for (const id of S.liked) { const v = videos[idIndex.get(id)]; if (v) chMax = Math.max(chMax, (chCount[v.ch] = (chCount[v.ch] || 0) + 3)); }
    const now = Date.now();

    const out = [];
    for (let d = 0; d < N; d++) {
      const lexN = maxLex ? lex[d] / maxLex : 0;
      const semN = semS ? Math.max(0, (semS[d] - semLo) / (semHi - semLo || 1)) : 0;
      if (!lexN && !semN) continue;
      // Keyword matches anchor relevance; meaning-only matches must be close
      // to count, and are capped below a strong keyword hit (as on YouTube).
      let rel;
      if (!semS) rel = lexN;
      else if (lexN) rel = 0.5 * lexN + 0.5 * semN;
      else rel = semN >= 0.5 ? 0.6 * semN : 0;
      if (rel < 0.08) continue;
      const v = videos[d];
      if (!visible(v)) continue;
      const pop = v.vc ? Math.min(1, Math.log10(1 + v.vc) / 7) : 0.3;
      const aff = chCount[v.ch] ? Math.log1p(chCount[v.ch]) / Math.log1p(chMax) : 0;
      let pers = 0;
      if (prof) { const r = sem.index.rows, o = d * sem.index.dims; let acc = 0; for (let j = 0; j < sem.index.dims; j++) acc += prof[j] * r[o + j]; pers = Math.max(0, acc / 127); }
      const age = v.p ? (now - Date.parse(v.p)) / 864e5 : 3650;
      const fresh = Math.exp(-age / 1000);
      out.push([rel * (1 + 0.25 * pop + 0.15 * pers + 0.10 * aff + 0.05 * fresh), v]);
    }
    out.sort((a, b) => b[0] - a[0]);
    return { list: out.slice(0, 500).map((x) => x[1]), corrected };
  }

  // ------------------------------------------------------------ suggestions
  function suggest(q, channels, limit = 8) {
    const n = q.trim().toLowerCase(); if (n.length < 2) return [];
    const out = new Set();
    for (const c of channels) if (c.name.toLowerCase().includes(n)) { out.add(c.name); if (out.size >= 3) break; }
    for (const v of videos) {
      if (out.size >= limit) break;
      const t = v.t.toLowerCase(); const i = t.indexOf(n);
      if (i === 0 || (i > 0 && /[\s(«"']/.test(t[i - 1]))) out.add(v.t.length > 70 ? v.t.slice(0, 70) + "…" : v.t);
    }
    return [...out];
  }

  // ------------------------------------------------------------ recommendations
  /* "For you", built like YouTube's home feed but from your own data only:
     candidates come from vector neighbours of what you recently watched
     ("Because you watched X"), fresh uploads from channels you watch, and
     videos close to your overall taste. Uses the document vectors only, so it
     never needs the 120 MB query model. */
  async function recommend(S, { visible, now = Date.now() }) {
    let ix = null;
    try { ix = await loadIndex(); } catch { ix = null; }
    const seen = new Set(Object.keys(S.watched || {}));
    for (const h of S.history || []) seen.add(h.id);
    const nope = new Set(S.notInterested || []);
    const used = new Set(), perCh = {};
    const chKey = (v) => v.ch || v.ccid || v.chn || "";
    const ok = (d) => {
      const v = videos[d];
      return v && visible(v) && !seen.has(v.id) && !nope.has(v.id) && !used.has(v.id) && (perCh[chKey(v)] || 0) < 2;
    };
    const take = (d) => { const v = videos[d]; used.add(v.id); perCh[chKey(v)] = (perCh[chKey(v)] || 0) + 1; return v.id; };
    const ageDays = (v) => (v.p ? (now - Date.parse(v.p)) / 864e5 : 3650);
    const boost = (v) => 1 + 0.15 * (v.vc ? Math.min(1, Math.log10(1 + v.vc) / 7) : 0.3) + 0.05 * Math.exp(-ageDays(v) / 1000);

    // "Not interested": anything nearly identical to a dismissed video is out too.
    const nopeVecs = ix ? [...nope].slice(-50).map((id) => idIndex.get(id)).filter((d) => d != null).map((d) => rowVec(d, ix)) : [];
    const blocked = new Uint8Array(N);
    for (const q of nopeVecs) { const s = dotAll(q, ix); for (let d = 0; d < N; d++) if (s[d] > 0.9) blocked[d] = 1; }

    const best = (scores, k, filter = () => true) => {
      const order = [];
      for (let d = 0; d < N; d++) if (scores[d] > 0 && !blocked[d] && filter(videos[d])) order.push(d);
      order.sort((a, b) => scores[b] * boost(videos[b]) - scores[a] * boost(videos[a]));
      const out = [];
      for (const d of order) { if (out.length >= k) break; if (ok(d)) out.push(take(d)); }
      return out;
    };

    const rows = [];
    // 1. Because you watched X: seeds = latest distinct watched, then latest likes.
    const seeds = [];
    for (const h of (S.history || []).slice().reverse()) if (!seeds.includes(h.id) && idIndex.has(h.id)) { seeds.push(h.id); if (seeds.length >= 8) break; }
    for (const id of (S.liked || []).slice(-4).reverse()) if (!seeds.includes(id) && idIndex.has(id)) seeds.push(id);
    if (ix) {
      for (const id of seeds) {
        if (rows.length >= 4) break;
        const sd = idIndex.get(id);
        const ids = best(dotAll(rowVec(sd, ix), ix), 4, (v) => v.id !== id);
        if (ids.length >= 2) rows.push({ label: `Because you watched “${videos[sd].t}”`, seed: id, ids });
      }
    }
    // 2. New from channels you watch (last 30 days), by how much you watch them.
    const aff = {};
    for (const h of S.history || []) { const v = videos[idIndex.get(h.id)]; if (v) aff[chKey(v)] = (aff[chKey(v)] || 0) + 1; }
    for (const id of S.liked || []) { const v = videos[idIndex.get(id)]; if (v) aff[chKey(v)] = (aff[chKey(v)] || 0) + 3; }
    const fresh = new Float32Array(N);
    for (let d = 0; d < N; d++) { const v = videos[d]; const a = aff[chKey(v)]; if (a && ageDays(v) <= 30) fresh[d] = Math.log1p(a) * Math.exp(-ageDays(v) / 15); }
    const freshIds = best(fresh, 4);
    if (freshIds.length) rows.push({ label: "New from channels you watch", ids: freshIds });
    // 3. Picked for you: close to your overall taste, favouring older gems.
    const prof = ix ? profile(S) : null;
    if (prof) {
      const s = dotAll(prof, ix);
      const ids = best(s, 6, (v) => ageDays(v) > 30);
      if (ids.length) rows.push({ label: "Picked for you", ids });
    }
    // Cold start: nothing watched yet (or no index).
    if (!rows.length) {
      const yearAgo = now - 365 * 864e5;
      const pop = new Float32Array(N);
      for (let d = 0; d < N; d++) { const v = videos[d]; if (v.vc && v.p && Date.parse(v.p) >= yearAgo) pop[d] = Math.log10(1 + v.vc); }
      const ids = best(pop, 12);
      if (ids.length) rows.push({ label: "Popular in your library this year", ids });
      const ev = new Float32Array(N); for (let d = 0; d < N; d++) if (videos[d].ev) ev[d] = 1;
      const evIds = best(ev, 6);
      if (evIds.length) rows.push({ label: "Classics", ids: evIds });
    }
    return { rows, smart: !!ix, seeds: seeds.length };
  }

  window.StepSearch = { build, rank, semantic, suggest, recommend, status, isReady: (q) => sem.cache.has(q.trim()) };
})();
