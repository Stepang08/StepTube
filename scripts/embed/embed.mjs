// Build-time embeddings for StepTube's semantic search.
//
// Uses the exact model + library version the browser uses at query time
// (transformers.js 3.0.2, Xenova/multilingual-e5-small, q8), so document and
// query vectors live in the same space. Multilingual on purpose: the library
// mixes English, Russian and Armenian.
//
// Incremental: vectors are cached by (video id, text hash) in CACHE_DIR, so a
// daily run only embeds new or retitled videos.
//
//   node scripts/embed/embed.mjs [videos.json] [cacheDir]
//   STEPTUBE_FAKE_EMBED=1 ...   deterministic fake vectors (offline tests)
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";

const MODEL = "Xenova/multilingual-e5-small";
const DIM = 384;
const BATCH = 64;
const videosPath = process.argv[2] || "site/data/videos.json";
const cacheDir = process.argv[3] || ".cache/embeddings";

const db = JSON.parse(fs.readFileSync(videosPath, "utf8"));
const chName = new Map(db.channels.map((c) => [c.key, c.name]));

// e5 expects "passage: " for documents and "query: " for queries.
const textOf = (v) =>
  `passage: ${v.t}. ${chName.get(v.ch) || v.chn || ""}. ${(v.d || "").slice(0, 300)}`;
const hashOf = (s) => crypto.createHash("sha1").update(s).digest("base64").slice(0, 10);

// ---- cache ---------------------------------------------------------------
fs.mkdirSync(cacheDir, { recursive: true });
const keysFile = path.join(cacheDir, "keys.json");
const vecsFile = path.join(cacheDir, "vecs.f32");
let cache = new Map();
try {
  const keys = JSON.parse(fs.readFileSync(keysFile, "utf8"));
  const buf = fs.readFileSync(vecsFile);
  const all = new Float32Array(buf.buffer, buf.byteOffset, buf.byteLength / 4);
  if (all.length === keys.length * DIM) keys.forEach((k, i) => cache.set(k, all.subarray(i * DIM, (i + 1) * DIM)));
} catch { /* first run */ }

const want = db.videos.map((v) => { const t = textOf(v); return { key: `${v.id}:${hashOf(t)}`, text: t }; });
const missing = want.filter((w) => !cache.has(w.key));
console.log(`embeddings: ${db.videos.length} videos, ${cache.size} cached, ${missing.length} to embed`);

// ---- embedder --------------------------------------------------------------
async function makeEmbedder() {
  if (process.env.STEPTUBE_FAKE_EMBED) {
    return async (texts) => texts.map((t) => {
      const v = new Float32Array(DIM); let h = 2166136261;
      for (const w of t.toLowerCase().split(/[^\p{L}\p{N}]+/u)) {
        if (!w) continue; h = 2166136261;
        for (const ch of w) h = Math.imul(h ^ ch.codePointAt(0), 16777619);
        v[(h >>> 0) % DIM] += 1;
      }
      const n = Math.hypot(...v) || 1; return v.map((x) => x / n);
    });
  }
  const { pipeline } = await import("@huggingface/transformers");
  const extractor = await pipeline("feature-extraction", MODEL, { dtype: "q8" });
  return async (texts) => {
    const out = await extractor(texts, { pooling: "mean", normalize: true });
    const data = out.data;
    return texts.map((_, i) => data.slice(i * DIM, (i + 1) * DIM));
  };
}

if (missing.length) {
  const embed = await makeEmbedder();
  const t0 = Date.now();
  for (let i = 0; i < missing.length; i += BATCH) {
    const chunk = missing.slice(i, i + BATCH);
    const vecs = await embed(chunk.map((c) => c.text));
    chunk.forEach((c, j) => cache.set(c.key, Float32Array.from(vecs[j])));
    if ((i / BATCH) % 20 === 0) console.log(`  ${Math.min(i + BATCH, missing.length)}/${missing.length} (${((Date.now() - t0) / 1000).toFixed(0)}s)`);
  }
}

// ---- write: cache (only keys still in use) + ordered matrix for compression
const keys = want.map((w) => w.key);
const mat = new Float32Array(keys.length * DIM);
keys.forEach((k, i) => mat.set(cache.get(k), i * DIM));
fs.writeFileSync(keysFile, JSON.stringify(keys));
fs.writeFileSync(vecsFile, Buffer.from(mat.buffer));
console.log(`embeddings: wrote ${keys.length} x ${DIM} to ${vecsFile}`);
