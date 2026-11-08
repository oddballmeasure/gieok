"""Local embeddings for the hot and warm tiers.

paraphrase-multilingual-MiniLM-L12-v2 (384 dims, handles Korean) through fastembed. It peaks at about
750 MB RSS, so only short-lived processes load it (the `gieok embed` pass, a search); the resident node
never does. Each memory keeps two quantized forms: 1 bit per dim (48 B) for a fast Hamming first pass,
and int8 (384 B) for rescoring."""

import numpy as np

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DIM = 384
MAX_CHARS = 2000  # the model reads ~128 tokens; the start of a memory carries its meaning


class Embedder:
    def __init__(self, threads: int = 2):
        from fastembed import TextEmbedding  # optional dependency: the `embed` extra

        self.model = TextEmbedding(MODEL, threads=threads)

    def __call__(self, texts: list[str]) -> np.ndarray:
        vecs = np.array(list(self.model.embed([t[:MAX_CHARS] for t in texts])), dtype=np.float32)
        return vecs / np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9)


def quantize(vecs: np.ndarray) -> tuple[list[bytes], list[bytes]]:
    bits = np.packbits(vecs > 0, axis=1)
    q8 = np.clip(np.round(vecs * 127 / np.maximum(np.abs(vecs).max(axis=1, keepdims=True), 1e-9)), -127, 127)
    return [b.tobytes() for b in bits], [q.astype(np.int8).tobytes() for q in q8]


def embed_missing(store, embedder: "Embedder", batch: int = 64) -> int:
    """Embed every live memory without a vector. Returns how many were embedded."""
    rows = store.db.execute("SELECT m.rid, m.id, m.title FROM memory m LEFT JOIN vec v ON v.rid=m.rid "
                            "WHERE m.deleted=0 AND v.rid IS NULL").fetchall()
    done = 0
    for i in range(0, len(rows), batch):
        part = rows[i: i + batch]
        texts = [f"{r['title']}\n{store.text(r['id'])}" for r in part]
        bits, q8 = quantize(embedder(texts))
        with store.tx() as db:
            db.executemany("INSERT OR REPLACE INTO vec (rid, bits, q8) VALUES (?, ?, ?)",
                           [(r["rid"], b, q) for r, b, q in zip(part, bits, q8)])
        done += len(part)
    return done
