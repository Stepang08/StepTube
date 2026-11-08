#!/usr/bin/env python3
"""Shrink build-time embeddings for the browser.

Reads the float32 matrix written by scripts/embed/embed.mjs (one row per video,
in site/data/videos.json order) and writes, under site/data/search/:

  pca.f32   mean (384) + projection matrix (384 x DIMS), float32
  emb.i8    one int8 row of DIMS per video (PCA-projected, L2-normalised, x127)
  meta.json model, sizes and an ids checksum so the page can detect a mismatch

384 float32 -> 128 int8 is ~12x smaller and costs little ranking quality
(PCA keeps most of the variance of these sentence embeddings).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DIM_IN, DIMS = 384, 128
MODEL = "Xenova/multilingual-e5-small"


def fnv1a(s: str) -> int:
    """32-bit FNV-1a over UTF-16 code units (matches the JS implementation)."""
    h = 0x811C9DC5
    for unit in memoryview(s.encode("utf-16-le")).cast("H"):
        h ^= unit
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def main() -> int:
    vecs_path = Path(sys.argv[1] if len(sys.argv) > 1 else ".cache/embeddings/vecs.f32")
    videos = json.loads((ROOT / "site/data/videos.json").read_text())["videos"]
    out_dir = ROOT / "site/data/search"
    out_dir.mkdir(parents=True, exist_ok=True)

    x = np.fromfile(vecs_path, dtype=np.float32)
    n = len(videos)
    if x.size != n * DIM_IN:
        print(f"! embeddings size {x.size} != {n} x {DIM_IN}; skipping search index", file=sys.stderr)
        return 1
    x = x.reshape(n, DIM_IN)

    mean = x.mean(axis=0)
    xc = x - mean
    cov = (xc.T @ xc) / max(1, n - 1)
    w, v = np.linalg.eigh(cov.astype(np.float64))
    order = np.argsort(w)[::-1][:DIMS]
    comps = v[:, order].astype(np.float32)               # (384, DIMS)
    kept = float(w[order].sum() / w.sum())

    p = xc @ comps
    p /= np.linalg.norm(p, axis=1, keepdims=True) + 1e-9
    q = np.clip(np.round(p * 127), -127, 127).astype(np.int8)

    np.concatenate([mean.astype(np.float32), comps.ravel()]).tofile(out_dir / "pca.f32")
    q.tofile(out_dir / "emb.i8")
    meta = {"model": MODEL, "dim_in": DIM_IN, "dims": DIMS, "n": n,
            "ids_fnv": fnv1a("\n".join(v["id"] for v in videos)),
            "variance_kept": round(kept, 4)}
    (out_dir / "meta.json").write_text(json.dumps(meta))
    print(f"search index: {n} x {DIMS} int8 ({q.nbytes / 1e6:.1f} MB), variance kept {kept:.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
