"""Hybrid search: FTS5 bm25 and vector recall, merged by reciprocal rank fusion.

Vector recall is two-stage: Hamming distance on the 1-bit vectors picks candidates, int8 dot products
rescore them. Without an embedder (or before any memory is embedded) search is keyword-only."""

import json
import re
from dataclasses import dataclass

import numpy as np

from gieok.store import Memory, Store

RRF_K = 60
WORD = re.compile(r"\w+", re.U)


@dataclass
class Hit:
    memory: Memory
    score: float
    snippet: str
    via: str  # "fts", "vec" or "fts+vec"


def fts_query(text: str, any_word: bool = False) -> str | None:
    words = WORD.findall(text)
    if not words:
        return None
    return (" OR " if any_word else " ").join(f'"{w}"' for w in words)


def _fts(store: Store, query: str, limit: int, allowed: set[int] | None = None) -> list[int]:
    only = "" if allowed is None else " AND rowid IN (SELECT value FROM json_each(?))"
    extra = () if allowed is None else (json.dumps(sorted(allowed)),)
    for any_word in (False, True):  # all words first; any word if that finds nothing
        q = fts_query(query, any_word)
        if q is None:
            return []
        rows = store.db.execute(f"SELECT rowid FROM memory_fts WHERE memory_fts MATCH ?{only} ORDER BY rank LIMIT ?",
                                (q, *extra, limit)).fetchall()
        if rows:
            return [r[0] for r in rows]
    return []


def _vec(store: Store, qvec: np.ndarray, limit: int, candidates: int = 200, allowed: set[int] | None = None) -> list[int]:
    rows = [r for r in store.db.execute("SELECT rid, bits, q8 FROM vec") if allowed is None or r[0] in allowed]
    if not rows:
        return []
    rids = np.array([r[0] for r in rows])
    bits = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.uint8).reshape(len(rows), -1)
    qbits = np.packbits(qvec > 0)
    hamming = np.unpackbits(bits ^ qbits, axis=1).sum(axis=1)
    top = np.argsort(hamming)[:candidates]
    q8 = np.frombuffer(b"".join(rows[i][2] for i in top), dtype=np.int8).reshape(len(top), -1).astype(np.float32)
    scores = q8 @ qvec
    order = top[np.argsort(-scores)][:limit]
    return [int(rids[i]) for i in order]


def _snippet(text: str, query: str, width: int = 160) -> str:
    words = [w.lower() for w in WORD.findall(query)]
    low = text.lower()
    pos = min((p for p in (low.find(w) for w in words) if p >= 0), default=0)
    start = max(0, pos - width // 3)
    return ("…" if start else "") + " ".join(text[start: start + width].split()) + ("…" if start + width < len(text) else "")


def search(store: Store, query: str, k: int = 10, ns_prefix: str = "", embedder=None,
           allowed: set[int] | None = None, pool: int = 50) -> list[Hit]:
    """`allowed` (memory rids, from a grant) restricts candidates before ranking, so the ranks, the pool cutoff
    and the fused order only ever see allowed memories."""
    fts = _fts(store, query, pool, allowed)
    vec = _vec(store, embedder([query])[0], pool, allowed=allowed) if embedder is not None else []
    scores: dict[int, float] = {}
    via: dict[int, set] = {}
    for name, ranked in (("fts", fts), ("vec", vec)):
        for rank, rid in enumerate(ranked):
            scores[rid] = scores.get(rid, 0) + 1 / (RRF_K + rank + 1)
            via.setdefault(rid, set()).add(name)
    hits = []
    for rid in sorted(scores, key=scores.get, reverse=True):
        mem = store.by_rid(rid)
        if mem is None or mem.deleted or not mem.ns.startswith(ns_prefix) or (allowed is not None and rid not in allowed):
            continue
        hits.append(Hit(mem, scores[rid], _snippet(store.text(mem.id), query), "+".join(sorted(via[rid]))))
        if len(hits) == k:
            break
    return hits


def _like(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search_transcripts(store: Store, query: str, k: int = 10, ns_prefix: str = "") -> list[dict]:
    for any_word in (False, True):
        q = fts_query(query, any_word)
        if q is None:
            return []
        rows = store.db.execute(
            "SELECT t.rid, t.source, t.agent, t.host, t.project, t.ns, t.started, t.ended FROM transcript_fts f "
            "JOIN transcript t ON t.rid=f.rowid WHERE transcript_fts MATCH ? AND t.ns LIKE ? ESCAPE '\\' "
            "ORDER BY rank LIMIT ?", (q, _like(ns_prefix), k)).fetchall()
        if rows:
            return [dict(r) for r in rows]
    return []
